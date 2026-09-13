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
from dalton_core.research_localization_store import publish_attachment, publish_ui_texts
from dalton_core.final_text_contract import FINAL_TEXT_RULES_VERSION
from dalton_core.research_language_review import (run_language_review, CHECKER_PURPOSE,
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PROVIDER)
from dalton_core.model_router import ModelRouter

PIPELINE_VERSION = "localization-with-one-language-review:0.1"



def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    from dalton_core.research_localization_store import _atomic_json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, value)


def snapshot(core_db: Path):
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
        for member in mission['universe']:
            library = research_library(connection, mission, member['company_ref'], localize=False)
            for product in library['products']:
                if product['status'] == 'available' and product.get('sections'):
                    products[source_content_hash(product)] = product
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


def selected_identity(config, call):
    with ModelRouter(config["model_router_db"]) as router:
        decision = router.get_decision(call["route_decision_ref"])
        profile = router.get_profile(decision["selected_profile_version_ref"])
    return {"provider": profile["provider"], "model": profile["provider"]+"/"+profile["model"]}


def run_chunk(task, *, mission, draft_config, verifier_config, checker_config,
              brain_config, scheduler_db, work_dir, max_cost, attempts):
    product_index, start, product = task
    identity = pipeline_identity(product, [draft_config, verifier_config, checker_config, brain_config])
    target = work_dir / 'chunks' / (identity + '.json')
    stage_path = work_dir / 'stages' / (identity + '.json')
    resolve = router_family_resolver(draft_config)
    evidence = read_json(stage_path) if stage_path.exists() else {
        'source_hash': source_content_hash(product), 'pipeline_identity': identity,
        'rules_version': FINAL_TEXT_RULES_VERSION, 'pipeline_version': PIPELINE_VERSION}
    if target.exists():
        saved = read_json(target)
        if saved['pipeline_identity'] != identity:
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

    if 'language_review' not in evidence:
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
    if review['status'] != 'ready_for_publication':
        raise ValueError(review.get('reason') or review['status'])
    localized = {'sections':review['brain_revision']['sections']}
    validate_localized_text(product,localized)
    # The brain actually rewrote the prose. Its family must also be excluded
    # from the independent semantic verifier, in addition to the initial drafter.
    routes = [evidence['draft']['route_decision_ref'],evidence['brain_call']['route_decision_ref']]
    check_prompt = build_verifier_prompt(product,localized)
    if 'verifier_call' not in evidence:
        check_id = hashlib.sha256((identity+check_prompt).encode()).hexdigest()
        verifier = model(verifier_config,5000)
        evidence['verifier_call'] = independent_model_call(verifier,
            producer_route_decision_refs=routes, purpose='research_localization_verifier',
            request_id='zh-verify-'+check_id,prompt=check_prompt,mission=mission)
        write_json(stage_path,evidence)
    checked = evidence['verifier_call']
    verdict = unwrap_json_object(checked['text'])
    proof = independence(draft_routes=routes,verifier_route=checked['route_decision_ref'],resolve=resolve)
    if not proof['independent']:
        raise ValueError('semantic verifier model is not independent of both authors')
    build_localization(product,localized,verdict)
    saved={**evidence,'localized':localized,'producer_routes':routes,'verifier':verdict,
           'independence':proof,'status':'passed'}
    write_json(target,saved)
    print(json.dumps({'product':product_index,'section_start':start,'status':'passed',
        'cost_micros':sum(saved[k]['cost_micros'] for k in
          ['draft','checker_call','brain_call','verifier_call'])},ensure_ascii=False),flush=True)
    return product_index,start,saved


def build(args):
    data = read_json(args.input)
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
    found, failures = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_chunk,t,mission=data['mission'],draft_config=read_json(args.model_config),
            verifier_config=read_json(args.verifier_config),checker_config=read_json(args.checker_config),
            brain_config=read_json(args.brain_config),scheduler_db=args.scheduler_db,
            work_dir=work_dir,max_cost=args.max_cost_per_call,attempts=args.attempts):t for t in tasks}
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
        if product['kind']=='ui_text':
            ui_batches.append({'source':product,'localization':candidate})
        else:
            target=publish_attachment(args.output_directory,product,candidate)
            published.append({'source':product['version_ref'],'file':str(target),
                              'content_hash':candidate['content_hash']})
    if ui_batches and not any(products[f['product']]['kind']=='ui_text' for f in failures):
        target=publish_ui_texts(args.output_directory,ui_batches)
        published.append({'source':'ui_text','file':str(target),'batches':len(ui_batches)})
    result={'status':'passed' if not failures else 'incomplete','products':len(products),
            'chunks':len(tasks),'published':published,'failures':failures}
    write_json(work_dir/'result.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if not failures else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    snap=sub.add_parser('snapshot');snap.add_argument('--core-db',type=Path,required=True)
    snap.add_argument('--output',type=Path,required=True)
    run=sub.add_parser('build')
    for name in ['input','model-config','verifier-config','checker-config','brain-config','scheduler-db','work-dir','output-directory']:
        run.add_argument('--'+name,type=Path,required=True)
    run.add_argument('--workers',type=int,default=4,choices=range(1,9))
    run.add_argument('--chunk-chars',type=int,default=4500)
    run.add_argument('--max-cost-per-call',type=float,default=1.0)
    run.add_argument('--attempts',type=int,default=3,choices=range(1,6))
    run.add_argument('--only',nargs='+')
    args=parser.parse_args()
    if args.command=='snapshot':
        data=snapshot(args.core_db);write_json(args.output,data)
        print(json.dumps({'products':len(data['products']),'sections':sum(len(p['sections']) for p in data['products'])}))
        return 0
    return build(args)

if __name__=='__main__':
    sys.exit(main())
