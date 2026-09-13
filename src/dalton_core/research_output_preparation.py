"""Explicit, resumable Chinese display preparation using governed model calls.

Snapshot and inspect are read-only. Build writes model/accounting records via
CockpitModel and presentation files, never edits research authorities.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from dalton_core.cockpit_model import CockpitModel, CockpitModelError, independent_model_call, unwrap_json_object
from dalton_core.company_dossier_draft import independence, router_family_resolver
from dalton_core.cockpit_research_library import research_library
from dalton_core.model_fallback_chain import register_purpose_tier
from dalton_core.research_localization import (build_prompt, build_verifier_prompt,
    build_localization, validate_localized_text, source_content_hash)
from dalton_core.research_localization_store import publish_attachment, publish_ui_texts, publish_reviewed_attachment, has_reviewed_attachment
from dalton_core.final_text_contract import FINAL_TEXT_RULES_VERSION
from dalton_core.research_language_review import (run_language_review, CHECKER_PURPOSE,
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PROVIDER)
from dalton_core.model_router import ModelRouter

PIPELINE_VERSION = "localization-with-one-language-review:0.1"
SEMANTIC_CONTRACT_VERSION = "localization-semantic-verification:0.1"



def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    from dalton_core.research_localization_store import _atomic_json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, value)


def snapshot(core_db: Path, *, include_surfaces: bool = False):
    connection = sqlite3.connect(core_db.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        row = connection.execute('SELECT v.record_json FROM coverage_mission_pointer p '
            'JOIN coverage_mission_versions v ON p.mission_version_id=v.mission_version_id '
            'ORDER BY p.mission_ref LIMIT 1').fetchone()
        mission = json.loads(row[0])
        products = {}
        if include_surfaces:
            from dalton_core.final_surface_products import final_surface_products
            from dalton_core.weekly_brief import WeeklyBriefAuthority
            from dalton_core.industry_research import IndustryResearchAuthority
            industry=IndustryResearchAuthority.__new__(IndustryResearchAuthority);industry.connection=connection
            weekly=WeeklyBriefAuthority.__new__(WeeklyBriefAuthority);weekly.connection=connection;weekly.industry_research=industry
        for member in mission['universe']:
            library = research_library(connection, mission, member['company_ref'], localize=False)
            for product in library['products']:
                if product['status'] == 'available' and product.get('sections'):
                    products[source_content_hash(product)] = product
            if include_surfaces:
                for product in final_surface_products(connection,mission,member['company_ref'],weekly_renderer=weekly.render_markdown):
                    products[source_content_hash(product)]=product
        return {'schema_version': 'localization-input:0.1', 'mission': mission,
                'products': list(products.values())}
    finally:
        connection.close()


def chunks(product, max_chars):
    start = 0
    current = []
    total = 0
    for section in product['sections']:
        size = len(json.dumps({k: section.get(k) for k in ['title','body','gaps']},ensure_ascii=False))
        if current and total + size > max_chars:
            yield start, dict(product, sections=current)
            start += len(current)
            current, total = [], 0
        current.append(section)
        total += size
    if current:
        yield start, dict(product, sections=current)


def pipeline_identity(product, configs):
    # Changing rules or an actual route configuration creates a new replay identity.
    return hashlib.sha256(json.dumps({"source": source_content_hash(product),
        "pipeline": PIPELINE_VERSION, "rules": FINAL_TEXT_RULES_VERSION,
        "configs": configs}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def style_stage_identity(product, *, draft_config, checker_config, brain_config):
    """Bind the paid one-check/one-revision stage without its later verifier."""
    return pipeline_identity(product, [draft_config, checker_config, brain_config])


def semantic_stage_identity(style_identity, verifier_config):
    return hashlib.sha256(json.dumps({"style_identity": style_identity,
        "contract": SEMANTIC_CONTRACT_VERSION, "verifier_config": verifier_config},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_STYLE_EVIDENCE_KEYS = ('draft', 'draft_localized', 'checker_call', 'brain_call',
                        'language_review')


def _migrate_legacy_style(*, product, work_dir, draft_config, checker_config,
                          brain_config, legacy_verifier_config, style_identity):
    """Import only a cryptographically identified legacy style stage.

    The legacy semantic call is deliberately excluded: a new verifier policy
    always gets its own call and independence proof.
    """
    if legacy_verifier_config is None:
        return None
    legacy_identity = pipeline_identity(product, [draft_config, legacy_verifier_config,
                                                   checker_config, brain_config])
    legacy_path = work_dir / 'stages' / (legacy_identity + '.json')
    if not legacy_path.exists():
        return None
    legacy = read_json(legacy_path)
    if (legacy.get('pipeline_identity') != legacy_identity or
            legacy.get('source_hash') != source_content_hash(product) or
            legacy.get('rules_version') != FINAL_TEXT_RULES_VERSION or
            legacy.get('pipeline_version') != PIPELINE_VERSION):
        raise ValueError('legacy style stage identity could not be confirmed')
    if not all(key in legacy for key in _STYLE_EVIDENCE_KEYS):
        raise ValueError('legacy style stage is incomplete')
    validate_localized_text(product, legacy['draft_localized'])
    review = legacy['language_review']
    if review.get('status') != 'ready_for_publication':
        raise ValueError('legacy style review is not complete')
    localized = {'sections': review['brain_revision']['sections']}
    validate_localized_text(product, localized)
    actual = selected_identity(checker_config, legacy['checker_call'])
    if actual != {'provider': CHECKER_PROVIDER, 'model': CHECKER_MODEL}:
        raise ValueError('legacy language checker identity could not be confirmed')
    review_product = dict(product, sections=legacy['draft_localized']['sections'])
    replayed_review = run_language_review(review_product,
        checker=lambda _: unwrap_json_object(legacy['checker_call']['text']),
        brain=lambda _: unwrap_json_object(legacy['brain_call']['text']),
        checker_identity={'provider': CHECKER_PROVIDER, 'model': CHECKER_MODEL})
    if replayed_review != review:
        raise ValueError('legacy language review does not replay exactly')
    migrated = {'source_hash': source_content_hash(product),
        'pipeline_identity': style_identity, 'rules_version': FINAL_TEXT_RULES_VERSION,
        'pipeline_version': PIPELINE_VERSION, 'migrated_legacy_pipeline_identity': legacy_identity}
    migrated.update({key: copy.deepcopy(legacy[key]) for key in _STYLE_EVIDENCE_KEYS})
    return migrated


def selected_identity(config, call):
    with ModelRouter(config["model_router_db"]) as router:
        decision = router.get_decision(call["route_decision_ref"])
        profile = router.get_profile(decision["selected_profile_version_ref"])
    return {"provider": profile["provider"], "model": profile["provider"]+"/"+profile["model"]}


def run_chunk(task, *, mission, draft_config, verifier_config, checker_config,
              brain_config, scheduler_db, work_dir, max_cost, attempts,
              legacy_verifier_config=None):
    product_index, start, product = task
    identity = style_stage_identity(product, draft_config=draft_config,
        checker_config=checker_config, brain_config=brain_config)
    semantic_identity = semantic_stage_identity(identity, verifier_config)
    target = work_dir / 'chunks' / (semantic_identity + '.json')
    stage_path = work_dir / 'stages' / (identity + '.json')
    semantic_path = work_dir / 'semantic-stages' / (semantic_identity + '.json')
    resolve = router_family_resolver(draft_config)
    evidence = read_json(stage_path) if stage_path.exists() else _migrate_legacy_style(
        product=product, work_dir=work_dir, draft_config=draft_config,
        checker_config=checker_config, brain_config=brain_config,
        legacy_verifier_config=legacy_verifier_config, style_identity=identity)
    if evidence is not None and not stage_path.exists():
        write_json(stage_path, evidence)
    evidence = evidence or {
        'source_hash': source_content_hash(product), 'pipeline_identity': identity,
        'rules_version': FINAL_TEXT_RULES_VERSION, 'pipeline_version': PIPELINE_VERSION}
    if target.exists():
        saved = read_json(target)
        if (saved['pipeline_identity'] != identity or
                saved.get('semantic_identity') != semantic_identity):
            raise ValueError('saved chunk pipeline changed')
        validate_localized_text(product, saved['localized'])
        proof = independence(draft_routes=saved['producer_routes'],
                             verifier_route=saved['verifier_call']['route_decision_ref'], resolve=resolve)
        if not proof['independent'] or saved['language_review']['status'] != 'ready_for_publication':
            raise ValueError('saved language review or verifier independence could not be confirmed')
        build_localization(product, saved['localized'], saved['verifier'])
        return product_index, start, saved

    def model(config, output_tokens):
        return CockpitModel(config, scheduler_db=scheduler_db, max_cost_usd=max_cost,
                            max_input_tokens=120000, max_output_tokens=output_tokens,
                            timeout_seconds=600)
    draft = model(draft_config, 10000)
    prompt = build_prompt(product)
    if 'draft_localized' not in evidence:
        for attempt in range(attempts):
            token = hashlib.sha256((identity+prompt).encode()).hexdigest()
            try:
                call = draft.call(purpose='research_localization', request_id='zh-draft-'+token,
                                  prompt=prompt, mission=mission)
                evidence['draft'] = call
                localized = unwrap_json_object(call['text'])
                validate_localized_text(product, localized)
                evidence['draft_localized'] = localized
                write_json(stage_path, evidence)
                break
            except Exception as exc:
                evidence.update(status='draft_failed', error=str(exc), attempt=attempt)
                write_json(work_dir/'attempts'/(identity+'-'+str(attempt)+'.json'),evidence)
                # Transport, routing and budget errors have their own governed adapter
                # retry. Redrafting cannot repair them and would spend unnecessarily.
                if isinstance(exc, CockpitModelError) or attempt+1 == attempts:
                    raise
                prompt = build_prompt(product)+'\nREPAIR FEEDBACK: '+str(exc)
                if evidence.get('draft'):
                    prompt += '\nPREVIOUS OUTPUT: '+evidence['draft']['text']

    prior_review = evidence.get('language_review') or {}
    resume_interrupted_call = (
        prior_review.get('status') == 'pending_brain_revision' and 'brain_call' not in evidence
        or prior_review.get('status') == 'pending_language_review' and 'checker_call' not in evidence
    )
    if 'language_review' not in evidence or resume_interrupted_call:
        review_product = dict(product, sections=evidence['draft_localized']['sections'])
        checker = model(checker_config, 12000)
        brain = model(brain_config, 16000)
        def check(check_prompt):
            if 'checker_call' not in evidence:
                check_id = hashlib.sha256((identity+check_prompt).encode()).hexdigest()
                evidence['checker_call'] = checker.call(purpose=CHECKER_PURPOSE,
                    request_id='zh-style-'+check_id,prompt=check_prompt,mission=mission)
                write_json(stage_path,evidence)
            actual = selected_identity(checker_config,evidence['checker_call'])
            if actual != {'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL}:
                raise ValueError('language checker served an unexpected transport or model')
            return unwrap_json_object(evidence['checker_call']['text'])
        def revise(brain_prompt):
            if 'brain_call' not in evidence:
                brain_id = hashlib.sha256((identity+brain_prompt).encode()).hexdigest()
                evidence['brain_call'] = brain.call(purpose=BRAIN_PURPOSE,
                    request_id='zh-revise-'+brain_id,prompt=brain_prompt,mission=mission)
                write_json(stage_path,evidence)
            return unwrap_json_object(evidence['brain_call']['text'])
        review = run_language_review(review_product,checker=check,brain=revise,
            checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL})
        evidence['language_review'] = review
        write_json(stage_path,evidence)
        if review.get('suggestions_markdown'):
            # Retain a readable complete report alongside the call receipts.
            report = work_dir/'language-reviews'/(identity+'.md')
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(review['suggestions_markdown'])
            report.chmod(0o600)
    review = evidence['language_review']
    if review['status'] == 'pending_brain_revision' and 'brain_call' in evidence:
        review_product=dict(product,sections=evidence['draft_localized']['sections'])
        review=run_language_review(review_product,
            checker=lambda _:unwrap_json_object(evidence['checker_call']['text']),
            brain=lambda _:unwrap_json_object(evidence['brain_call']['text']),
            checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL})
        evidence['language_review']=review
        write_json(stage_path,evidence)
    if review['status'] != 'ready_for_publication':
        raise ValueError(review.get('reason') or review['status'])
    localized = {'sections':review['brain_revision']['sections']}
    validate_localized_text(product,localized)
    # The brain actually rewrote the prose. Its family must also be excluded
    # from the independent semantic verifier, in addition to the initial drafter.
    routes = [evidence['draft']['route_decision_ref'],evidence['brain_call']['route_decision_ref']]
    check_prompt = build_verifier_prompt(product,localized)
    semantic_evidence = read_json(semantic_path) if semantic_path.exists() else {
        'source_hash': source_content_hash(product), 'pipeline_identity': identity,
        'semantic_identity': semantic_identity,
        'semantic_contract_version': SEMANTIC_CONTRACT_VERSION}
    if (semantic_evidence.get('pipeline_identity') != identity or
            semantic_evidence.get('semantic_identity') != semantic_identity):
        raise ValueError('semantic stage identity changed')
    if 'verifier_call' not in semantic_evidence:
        check_id = hashlib.sha256((SEMANTIC_CONTRACT_VERSION+semantic_identity+check_prompt).encode()).hexdigest()
        verifier = model(verifier_config,5000)
        semantic_evidence['verifier_call'] = independent_model_call(verifier,
            producer_route_decision_refs=routes, purpose='research_localization_verifier',
            request_id='zh-verify-'+check_id,prompt=check_prompt,mission=mission)
        write_json(semantic_path,semantic_evidence)
    checked = semantic_evidence['verifier_call']
    verdict = unwrap_json_object(checked['text'])
    proof = independence(draft_routes=routes,verifier_route=checked['route_decision_ref'],resolve=resolve)
    if not proof['independent']:
        raise ValueError('semantic verifier model is not independent of both authors')
    build_localization(product,localized,verdict)
    saved={**evidence,'semantic_identity':semantic_identity,
           'semantic_contract_version':SEMANTIC_CONTRACT_VERSION,
           'verifier_call':checked,'localized':localized,'producer_routes':routes,
           'verifier':verdict,'independence':proof,'status':'passed'}
    write_json(target,saved)
    print(json.dumps({'product':product_index,'section_start':start,'status':'passed',
        'cost_micros':sum(saved[k]['cost_micros'] for k in
          ['draft','checker_call','brain_call','verifier_call'])},ensure_ascii=False),flush=True)
    return product_index,start,saved


def build(args, data=None):
    data = data if data is not None else read_json(args.input)
    products = data['products']
    if args.only:
        products = [p for p in products if p['kind'] in args.only]
    register_purpose_tier('research_localization', 'cheap')
    register_purpose_tier('research_localization_verifier', 'verifier')
    register_purpose_tier(CHECKER_PURPOSE, 'cheap')
    register_purpose_tier(BRAIN_PURPOSE, 'brain')
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(i,start,part) for i,p in enumerate(products) for start,part in chunks(p,args.chunk_chars)]
    legacy_verifier_config = (read_json(args.legacy_verifier_config)
        if getattr(args, 'legacy_verifier_config', None) else None)
    found, failures = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_chunk,t,mission=data['mission'],draft_config=read_json(args.model_config),
            verifier_config=read_json(args.verifier_config),checker_config=read_json(args.checker_config),
            brain_config=read_json(args.brain_config),scheduler_db=args.scheduler_db,
            work_dir=work_dir,max_cost=args.max_cost_per_call,attempts=args.attempts,
            legacy_verifier_config=legacy_verifier_config):t for t in tasks}
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            try:
                i,start,result = future.result()
                found.setdefault(i,[]).append((start,result))
            except Exception as exc:
                failures.append({'product':task[0],'section_start':task[1],'error':str(exc)})
    published, ui_batches = [], []
    for i, product in enumerate(products):
        if any(f['product']==i for f in failures):
            continue
        rows, proof_rows = [], []
        for start,saved in sorted(found[i],key=lambda x:x[0]):
            for offset,row in enumerate(saved['localized']['sections']):
                rows.append(dict(row,index=start+offset))
            proof_rows.append(saved['verifier'])
        # Conjunction of actual clean, independent section verifications.
        verdict = {'verdict':'pass','faithful':all(v['faithful'] for v in proof_rows),
            'no_new_facts':all(v['no_new_facts'] for v in proof_rows),
            'meaning_preserved':all(v['meaning_preserved'] for v in proof_rows),
            'findings':[f for v in proof_rows for f in v['findings']]}
        candidate = build_localization(product,{'sections':rows},verdict)
        target=publish_reviewed_attachment(args.output_directory,product,candidate,
            [saved for _,saved in sorted(found[i],key=lambda x:x[0])])
        published.append({'source':product['version_ref'],'file':str(target),
                          'content_hash':candidate['content_hash']})
        if product['kind']=='ui_text' or product['kind'].startswith('surface_'):
            ui_batches.append({'source':product,'localization':candidate})
    if ui_batches and not any((products[f['product']]['kind']=='ui_text' or products[f['product']]['kind'].startswith('surface_')) for f in failures):
        target=publish_ui_texts(args.output_directory,ui_batches)
        published.append({'source':'ui_text','file':str(target),'batches':len(ui_batches)})
    result={'status':'passed' if not failures else 'incomplete','products':len(products),
            'chunks':len(tasks),'published':published,'failures':failures}
    write_json(work_dir/'result.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if not failures else 1


def validate_worker_config(cfg):
    """Validate the scheduled worker before opening databases or making calls."""
    paths = {'core_db', 'scheduler_db', 'model_config', 'verifier_config',
             'checker_config', 'brain_config', 'work_dir', 'output_directory'}
    required = paths | {'schema_version', 'workers', 'chunk_chars',
                        'max_cost_per_call', 'draft_attempts', 'publication_gate'}
    if (not isinstance(cfg, dict) or set(cfg) != required
            or cfg.get('schema_version') != 'research-publication-worker-config:0.1'):
        raise ValueError('unsupported research publication worker configuration')
    for name in paths:
        if not isinstance(cfg[name], str) or not Path(cfg[name]).is_absolute():
            raise ValueError('publication worker paths must be absolute')
    bounds = {'workers': (1, 8), 'chunk_chars': (100, 50000), 'draft_attempts': (1, 5)}
    for name, (low, high) in bounds.items():
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError('publication worker bounds are invalid')
    cost = cfg['max_cost_per_call']
    if type(cost) not in (int, float) or not 0 < cost <= 1:
        raise ValueError('publication worker cost bound is invalid')


def run_worker(config_path):
    """One scheduled preparation pass; all model calls use the existing ledger."""
    import fcntl
    from types import SimpleNamespace
    from dalton_core.research_publication_worker import poll_once
    from dalton_core.final_surface_products import final_surface_products
    cfg=read_json(config_path)
    validate_worker_config(cfg)
    root=Path(cfg['work_dir']);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    from dalton_core.research_publication_authority import published_runtime_gate
    gate=published_runtime_gate(cfg.get('publication_gate')or{})
    if gate['status']!='active':
        from datetime import datetime,timezone
        result={'schema_version':'research-publication-worker-checkpoint:0.1',**gate,
                'checked_at':datetime.now(timezone.utc).isoformat()}
        write_json(root/'worker-last-run.json',result)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if gate['status']=='waiting_for_release_publication' else 1
    fd=os.open(root/'.worker.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a+') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status':'already_running'}));return 0
        core=Path(cfg['core_db']).resolve()
        connection=sqlite3.connect(core.as_uri()+'?mode=ro',uri=True)
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        try:
            row=connection.execute('SELECT v.record_json FROM coverage_mission_pointer p '
                'JOIN coverage_mission_versions v ON p.mission_version_id=v.mission_version_id '
                'ORDER BY p.mission_ref LIMIT 1').fetchone()
            if row is None:
                result={'status':'no_current_mission'}
            else:
                mission=json.loads(row[0])
                args=SimpleNamespace(input=None,only=None,workers=int(cfg.get('workers',4)),
                    chunk_chars=int(cfg.get('chunk_chars',4500)),max_cost_per_call=float(cfg.get('max_cost_per_call',1)),
                    attempts=int(cfg.get('draft_attempts',2)),work_dir=root,
                    output_directory=Path(cfg['output_directory']),scheduler_db=Path(cfg['scheduler_db']),
                    model_config=Path(cfg['model_config']),verifier_config=Path(cfg['verifier_config']),
                    checker_config=Path(cfg['checker_config']),brain_config=Path(cfg['brain_config']))
                if not 1<=args.workers<=8 or not 1<=args.attempts<=5 or args.chunk_chars<100:
                    raise ValueError('publication worker bounds are invalid')
                def prepare(product):
                    if has_reviewed_attachment(args.output_directory,product):
                        return {'status':'completed','existing_reviewed_attachment':True}
                    code=build(args,{'mission':mission,'products':[product]})
                    return {'status':'completed' if code==0 else 'pending',
                            'receipt':read_json(root/'result.json')}
                from dalton_core.weekly_brief import WeeklyBriefAuthority
                from dalton_core.industry_research import IndustryResearchAuthority
                industry=IndustryResearchAuthority.__new__(IndustryResearchAuthority);industry.connection=connection
                weekly=WeeklyBriefAuthority.__new__(WeeklyBriefAuthority);weekly.connection=connection;weekly.industry_research=industry
                result=poll_once(connection,mission,state_dir=root/'products',prepare=prepare,
                    extra_reader=lambda con,mis,company:final_surface_products(con,mis,company,
                        weekly_renderer=weekly.render_markdown))
                result['status']='healthy' if not result['pending'] else 'pending'
        finally:
            connection.close()
        from datetime import datetime,timezone
        result['schema_version']='research-publication-worker-checkpoint:0.1'
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        write_json(root/'worker-last-run.json',result)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status'] in {'healthy','no_current_mission'} else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    worker=sub.add_parser('run-worker');worker.add_argument('--config',type=Path,required=True)
    snap=sub.add_parser('snapshot');snap.add_argument('--core-db',type=Path,required=True)
    snap.add_argument('--output',type=Path,required=True)
    snap.add_argument('--include-surfaces',action='store_true')
    run=sub.add_parser('build')
    for name in ['input','model-config','verifier-config','checker-config','brain-config','scheduler-db','work-dir','output-directory']:
        run.add_argument('--'+name,type=Path,required=True)
    run.add_argument('--legacy-verifier-config', type=Path,
                     help='exact old verifier config used only to identify a legacy style cache')
    run.add_argument('--workers',type=int,default=4,choices=range(1,9))
    run.add_argument('--chunk-chars',type=int,default=4500)
    run.add_argument('--max-cost-per-call',type=float,default=1.0)
    run.add_argument('--attempts',type=int,default=3,choices=range(1,6))
    run.add_argument('--only',nargs='+')
    args=parser.parse_args()
    if args.command=='run-worker':
        return run_worker(args.config)
    if args.command=='snapshot':
        data=snapshot(args.core_db,include_surfaces=args.include_surfaces);write_json(args.output,data)
        print(json.dumps({'products':len(data['products']),'sections':sum(len(p['sections']) for p in data['products'])}))
        return 0
    return build(args)

if __name__=='__main__':
    sys.exit(main())
