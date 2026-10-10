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

from dalton_core.cockpit_model import CockpitModel, CockpitModelError, CockpitModelRouteUnavailable, independent_model_call, unwrap_json_object
from dalton_core.company_dossier_draft import independence, router_family_resolver
from dalton_core.cockpit_research_library import research_library
from dalton_core.model_fallback_chain import register_purpose_tier
from dalton_core.research_localization import (build_prompt, build_verifier_prompt,
    build_localization, validate_localized_text, source_content_hash, ResearchLocalizationError,
    validate_no_truncated_copies)
from dalton_core.research_localization_store import publish_attachment, publish_ui_texts, publish_reviewed_attachment, has_reviewed_attachment
from dalton_core.research_source_projection import publication_source_hash, projection_slice
from dalton_core.final_text_contract import FINAL_TEXT_RULES_VERSION
from dalton_core.research_language_review import (run_language_review, parse_stage_output, CHECKER_PURPOSE,
    BRAIN_PURPOSE, CHECKER_MODEL, CHECKER_PROVIDER, ResearchLanguageReviewError,
    DETERMINISTIC_REVISER, deterministic_revision, validate_checker_output)
from dalton_core.model_router import ModelRouter
from dalton_core.research_publication_spend import (PRIORITY_LOW,
    LANGUAGE_TIER_CHECK_ONLY, LANGUAGE_TIER_FULL, language_tier,
    PurposeDailyCapReached, DEFERRED_STATUS, DRAFT_PURPOSE, VERIFIER_PURPOSE,
    WORKER_CONFIG_FIELD, daily_caps_micros, ledger_gate, publication_priority)
from dalton_core.cockpit_model import CockpitModelPoolExhausted

from dalton_core.research_publication_worker import EXHAUSTED_STATUS

PIPELINE_VERSION = "localization-with-one-language-review:0.1"
SEMANTIC_CONTRACT_VERSION = "localization-semantic-verification:0.1"
from .research_publication_limits import (GovernedPublicationModel, PublicationParked,
    publication_scope, current_scope, cache_binding, MAX_LANGUAGE_ROUNDS)
from .research_publication_limits import raise_if_parked
from .research_publication_repair import local_repair_prompt, expand_local_patch,local_patch_quotes,repair_indices



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
            yield start, projection_slice(product, list(range(start, start + len(current))))
            start += len(current)
            current, total = [], 0
        current.append(section)
        total += size
    if current:
        yield start, projection_slice(product, list(range(start, start + len(current))))


def pipeline_identity(product, configs):
    # Changing rules or an actual route configuration creates a new replay identity.
    return hashlib.sha256(json.dumps({"source": source_content_hash(product),
        "pipeline": PIPELINE_VERSION, "rules": FINAL_TEXT_RULES_VERSION,
        "configs": configs, "cache_binding":cache_binding(product)}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def style_stage_identity(product, *, draft_config, checker_config, brain_config):
    """Bind the paid one-check/one-revision stage without its later verifier."""
    return pipeline_identity(product, [draft_config, checker_config, brain_config])


def semantic_stage_identity(style_identity, verifier_config, revision_hash=None):
    value={"style_identity":style_identity,"contract":SEMANTIC_CONTRACT_VERSION,
           "verifier_config":verifier_config}
    if revision_hash is not None:value["revision_hash"]=revision_hash
    return hashlib.sha256(json.dumps(value,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _revision_hash(review):
    return hashlib.sha256(json.dumps(review['brain_revision']['sections'],ensure_ascii=False,
        sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _repair_prompt(*, product, draft_localized, review, failure, attempt):
    candidate=(review.get('brain_revision') or {}).get('sections') or draft_localized['sections']
    prompt,_=local_repair_prompt(product,candidate,failure,
        decisions=(review.get('brain_revision') or {}).get('decisions'))
    return prompt


def _repair_output(call):
    output=parse_stage_output(call['text'],stage='brain')
    binding=call.get('local_patch_binding')
    return output if binding is None else expand_local_patch(output,**binding)


def _draft_brain_repair_prompt(product, last_draft, feedback):
    display={key:copy.deepcopy(product[key]) for key in
             ('kind','version_ref','status','title','sections') if key in product}
    return '\n'.join((
        '你负责修订研究成品的中文初稿。初稿模型已用完本轮尝试次数，但内容校验仍未通过。',
        '只修复给出的校验错误，忠实保留原文的事实、数字、单位、来源、审批状态、缺口和章节结构。',
        '只输出 JSON：{"sections":[{"index":0,"title":"...","body":"...","gaps":[]}]}。',
        '原始展示内容：'+json.dumps(display,ensure_ascii=False,sort_keys=True,separators=(',',':')),
        '最后一次初稿：'+json.dumps(last_draft,ensure_ascii=False,sort_keys=True,separators=(',',':')),
        '准确校验反馈：'+str(feedback),
    ))


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
        checker=lambda _: parse_stage_output(legacy['checker_call']['text'], stage='checker'),
        brain=lambda _: parse_stage_output(legacy['brain_call']['text'], stage='brain'),
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


#: Stands in for the brain configuration in a check-only stage identity, so a
#: check-only stage can never be mistaken for (or reuse) a brain-revised one.
CHECK_ONLY_REVISER_CONFIG = {'language_tier': LANGUAGE_TIER_CHECK_ONLY,
                             'reviser': DETERMINISTIC_REVISER}


def redraft_identity(identity, redraft_generation):
    """The stage identity of one explicit redraft generation.

    Generation 0 is the ordinary identity, so every existing cache keeps its
    key.  A later generation is a new identity: a new ``stages/`` file, and new
    request ids for the draft, checker, brain and verifier calls, because the
    scheduler replays a completed work order by request id -- a redraft that
    reused them would be handed the same draft and the same route decision.
    """
    if type(redraft_generation) is not int or redraft_generation < 0:
        raise ValueError('redraft_generation must be a non-negative integer')
    if redraft_generation == 0:
        return identity
    return hashlib.sha256(f'{identity}:redraft:{redraft_generation}'.encode()).hexdigest()


class CheckOnlyContentFailure(ValueError):
    """A check-only chunk whose text, not its transport, failed.

    Raised only for content verdicts: the draft never passed the deterministic
    check, the checker's output could not be used, or the independent verifier
    rejected the text even after the unchanged-draft fallback.  A spend
    ceiling, a busy request, an unavailable route or an unexpected transport
    is never one of these; those stay pending and are retried.

    ``draft_call`` is the drafting call whose model family the fallback's
    verifier must differ from.
    """

    def __init__(self, message, *, draft_call=None):
        super().__init__(message)
        self.draft_call = copy.deepcopy(draft_call)


class CheckOnlyExhausted(ValueError):
    """A check-only chunk that has used its one deterministic fallback.

    Terminal for this source hash: nothing is published, and the product is
    not offered again until its source changes or the owner asks for a retry.
    """


#: The one fallback a check-only chunk has after a content failure: keep the
#: source text, which for these products is already Simplified Chinese, and
#: review *that* -- checker, suggestions set aside, independent verifier.
SOURCE_KEPT_FALLBACK = 'check-only-source-kept:0.1'
#: The fallback of a check-only chunk whose source is *not* Chinese, where
#: keeping the source is no translation at all: one fresh cheap draft, under
#: its own stage identity, through the same check-only pipeline.
FRESH_DRAFT_FALLBACK = 'check-only-fresh-draft:0.1'


def _source_kept_unavailable(product):
    """Why the kept-source rule cannot apply to ``product``, or ``None``."""
    source_localized = _source_as_localized(product)
    try:
        validate_localized_text(product, source_localized)
        if not _source_is_chinese(source_localized):
            raise ResearchLocalizationError('the source text is not predominantly Simplified Chinese')
    except ResearchLocalizationError as exc:
        return str(exc)
    return None


def run_chunk(task, **kwargs):
    source_hash = kwargs.pop('artifact_source_hash', None) or publication_source_hash(task[2])
    tick = kwargs.pop('publication_tick', None)
    with publication_scope(kwargs['work_dir']/'publication-limits.sqlite3', source_hash, tick=tick):
        return _run_governed_chunk(task, **kwargs)


def _run_governed_chunk(task, **kwargs):
    """Prepare one chunk; see :func:`_run_chunk`.

    A ``check_only`` chunk that fails on content falls back once, by a fixed
    rule, to :func:`_source_kept_chunk`.  Live 2026-09-25 about eight NO_CHANGE
    judgements sat in ``pending`` for good: ACN and CTSH drafts wrote "4 家"
    as "四家", and three IBM drafts rewrote an already-Chinese source until
    the verifier refused both the revision and the unchanged draft.

    2026-10-02: that rule only fits a source already in Chinese.  For any
    other source it ended the chunk at once -- "check-only fallback
    unavailable (the source text is not predominantly Simplified Chinese)" --
    so one cheap draft the verifier did not like left the product
    ``exhausted`` with nothing published: 77 cycle reflections and 8 NO_CHANGE
    judgements between 09-25 and 10-02, about a quarter of all reflections.
    Such a chunk now gets one fresh cheap draft (:data:`FRESH_DRAFT_FALLBACK`)
    -- still no brain -- and is exhausted only when that fails on content too.
    """
    try:
        return _route_redraft(_run_chunk, task, **kwargs)
    except CheckOnlyContentFailure as exc:
        unavailable = _source_kept_unavailable(task[2])
        if unavailable is None:
            return _source_kept_chunk(task, trigger=exc, **kwargs)
        try:
            return _route_redraft(_run_chunk, task, fresh_draft=True, **kwargs)
        except CheckOnlyContentFailure as second:
            raise CheckOnlyExhausted(
                f'check-only fresh draft failed too ({second}); kept-source fallback '
                f'unavailable ({unavailable}); first failure: {exc}') from second


def _route_redraft(run, task, **kwargs):
    """Run a chunk; once, from a fresh draft if the cached draft's family is stale."""
    try:
        return run(task, **kwargs)
    except StoredDraftNotIndependent as exc:
        return run(task, route_redraft=exc.family, **kwargs)


def _run_chunk(task, *, mission, draft_config, verifier_config, checker_config,
              brain_config, scheduler_db, work_dir, max_cost, attempts,
              legacy_verifier_config=None, repair_reviewed=False,
              extra_brain_repair=False, redraft_generation=0, spend_gate=None,
              language_tier=LANGUAGE_TIER_FULL, fresh_draft=False, route_redraft=None):
    """Prepare one chunk.  ``spend_gate(purpose)`` is asked before every paid
    call and raises :class:`PurposeDailyCapReached` to defer the chunk; it is
    asked outside every handler that would record a content failure, so a
    deferral never becomes a failed stage.

    ``language_tier='check_only'`` (a NO_CHANGE judgement, the cycle
    reflection) never calls the brain: the cheap draft, the cheap language
    check, :func:`deterministic_revision` and the independent semantic
    verifier.  A draft that fails validation is not repaired by the brain,
    and a semantic rejection of the applied suggestions falls back, once and
    without a model, to the unchanged draft."""
    if not isinstance(extra_brain_repair, bool) or (extra_brain_repair and not repair_reviewed):
        raise ValueError('one extra brain repair requires reviewed repair mode')
    if language_tier not in (LANGUAGE_TIER_FULL, LANGUAGE_TIER_CHECK_ONLY):
        raise ValueError('unknown language tier')
    check_only = language_tier == LANGUAGE_TIER_CHECK_ONLY
    attempts = min(attempts, 2)
    gate = spend_gate or (lambda purpose: None)
    product_index, start, product = task
    identity = redraft_identity(style_stage_identity(product, draft_config=draft_config,
        checker_config=checker_config,
        brain_config=CHECK_ONLY_REVISER_CONFIG if check_only else brain_config), redraft_generation)
    if fresh_draft:
        if not check_only:
            raise ValueError('a fresh-draft fallback is only for check-only chunks')
        identity = hashlib.sha256((identity + ':' + FRESH_DRAFT_FALLBACK).encode()).hexdigest()
    if route_redraft is not None:
        identity = route_redraft_identity(identity, route_redraft)
    stage_path = work_dir / 'stages' / (identity + '.json')
    resolve = router_family_resolver(draft_config)
    evidence = read_json(stage_path) if stage_path.exists() else None if (
        redraft_generation or fresh_draft or route_redraft is not None) \
        else _migrate_legacy_style(
        product=product, work_dir=work_dir, draft_config=draft_config,
        checker_config=checker_config, brain_config=brain_config,
        legacy_verifier_config=None if check_only else legacy_verifier_config,
        style_identity=identity)
    if evidence is not None and not stage_path.exists():
        write_json(stage_path, evidence)
    evidence = evidence or {
        'source_hash': source_content_hash(product), 'pipeline_identity': identity,
        'rules_version': FINAL_TEXT_RULES_VERSION, 'pipeline_version': PIPELINE_VERSION}
    evidence['cache_binding'] = cache_binding(product)
    if check_only:
        evidence['language_tier'] = LANGUAGE_TIER_CHECK_ONLY
    def model(config, output_tokens):
        return GovernedPublicationModel(CockpitModel(config, scheduler_db=scheduler_db, max_cost_usd=max_cost,
                            max_input_tokens=120000, max_output_tokens=output_tokens,
                            timeout_seconds=600),stage_id=identity)
    draft = model(draft_config, 10000)
    prompt = build_prompt(product)
    if 'draft_localized' not in evidence and evidence.get('draft_repair_call'):
        # A completed repair stays the one repair for this source/style. A
        # validator fix can resume it; a rerun must not buy a new draft or
        # derive another paid repair from the previous failed repair.
        repair = evidence['draft_repair_call']
        localized = parse_stage_output(repair['text'], stage='draft')
        validate_localized_text(product, localized)
        evidence['draft'] = copy.deepcopy(repair)
        evidence['draft_localized'] = localized
        for key in ('status', 'error', 'attempt'):
            evidence.pop(key, None)
        write_json(stage_path, evidence)
    if 'draft_localized' not in evidence:
        final_validation_error = None
        for attempt in range(attempts):
            token = hashlib.sha256((identity+prompt).encode()).hexdigest()
            gate(DRAFT_PURPOSE)
            try:
                call = draft.call(purpose=DRAFT_PURPOSE, request_id='zh-draft-'+token,
                                  prompt=prompt, mission=mission)
                evidence['draft'] = call
                localized = parse_stage_output(call['text'], stage='draft')
                validate_localized_text(product, localized)
                validate_no_truncated_copies(product, localized)
                evidence['draft_localized'] = localized
                write_json(stage_path, evidence)
                break
            except Exception as exc:
                content_failure = isinstance(exc, (ResearchLocalizationError, ResearchLanguageReviewError))
                if content_failure:
                    final_validation_error=exc
                    evidence.setdefault('draft_failures',[]).append({
                        'attempt':attempt,'error':str(exc),'draft_call':copy.deepcopy(evidence.get('draft'))})
                evidence.update(status='draft_failed', error=str(exc), attempt=attempt)
                write_json(work_dir/'attempts'/(identity+'-'+str(attempt)+'.json'),evidence)
                # Transport, routing and budget errors have their own governed adapter
                # retry. Redrafting cannot repair them and would spend unnecessarily.
                if not content_failure:
                    raise
                if attempt+1 == attempts:
                    break
                prompt = build_prompt(product)+'\nREPAIR FEEDBACK: '+str(exc)
                if evidence.get('draft'):
                    prompt += '\nPREVIOUS OUTPUT: '+evidence['draft']['text']
        if 'draft_localized' not in evidence:
            if final_validation_error is None or not evidence.get('draft'):
                raise ValueError('draft validation failed without repairable evidence')
            if check_only:
                # No brain for a low-value product: no Opus draft repair.
                raise CheckOnlyContentFailure(
                    'check-only draft failed validation: '+str(final_validation_error),
                    draft_call=evidence.get('draft'))
            try:last_draft=parse_stage_output(evidence['draft']['text'],stage='draft')
            except ValueError:last_draft={'unparsed_output':evidence['draft']['text']}
            repair_prompt=_draft_brain_repair_prompt(product,last_draft,final_validation_error)
            repair_id=hashlib.sha256((identity+repair_prompt).encode()).hexdigest()
            gate(BRAIN_PURPOSE)
            repair=model(brain_config,12000).call(purpose=BRAIN_PURPOSE,
                request_id='zh-draft-brain-repair-'+repair_id,prompt=repair_prompt,mission=mission)
            evidence['draft_repair_call']=copy.deepcopy(repair)
            evidence['draft']=copy.deepcopy(repair)
            write_json(stage_path,evidence)
            localized=parse_stage_output(repair['text'],stage='draft')
            validate_localized_text(product,localized)
            evidence['draft_localized']=localized
            evidence.pop('status',None);evidence.pop('error',None);evidence.pop('attempt',None)
            write_json(stage_path,evidence)

    prior_review = evidence.get('language_review') or {}
    resume_interrupted_call = (
        prior_review.get('status') == 'pending_brain_revision' and 'brain_call' not in evidence
        or prior_review.get('status') == 'pending_language_review'
    )
    if 'language_review' not in evidence or resume_interrupted_call:
        # Batch w: before the checker and the Opus revision are bought, make
        # sure the independent verifier at the end can be asked at all.
        draft_route = (evidence.get('draft') or {}).get('route_decision_ref')
        try:
            verifier_preflight(verifier_config, brain_config, draft_route_ref=draft_route,
                               check_only=check_only)
        except VerifierNotIndependent as refused:
            # The cached draft is the problem only if a draft written by the
            # chain as it is now would pass; otherwise the refusal stands and
            # the owner's card says what to change.
            family = None if route_redraft is not None else drafting_family(draft_config)
            if family is not None and verifier_preflight(
                    verifier_config, brain_config, draft_route_ref=None,
                    check_only=check_only, producer_family=family) is None:
                raise StoredDraftNotIndependent(str(refused), family=family) from refused
            raise
        # Asked here, not inside check()/revise(): run_language_review turns an
        # exception from either into a pending review, which is a failure.
        if 'checker_call' not in evidence:
            gate(CHECKER_PURPOSE)
        if 'brain_call' not in evidence and not check_only:
            gate(BRAIN_PURPOSE)
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
            return parse_stage_output(evidence['checker_call']['text'], stage='checker')
        def revise(brain_prompt):
            if check_only:
                checked = validate_checker_output(
                    parse_stage_output(evidence['checker_call']['text'], stage='checker'),
                    sections=list(review_product['sections']))
                return deterministic_revision(review_product, checked,
                                              numeric_source_product=product)
            if 'brain_call' not in evidence:
                brain_id = hashlib.sha256((identity+brain_prompt).encode()).hexdigest()
                try:
                    evidence['brain_call'] = brain.call(purpose=BRAIN_PURPOSE,
                        request_id='zh-revise-'+brain_id,prompt=brain_prompt,mission=mission)
                except CockpitModelRouteUnavailable:
                    # Only a proved routing rejection can create this smaller
                    # reservation. Busy, interrupted, or dispatched calls keep
                    # their original identity and are never bypassed.
                    bounded_brain = model(brain_config, 12000)
                    evidence['brain_call'] = bounded_brain.call(purpose=BRAIN_PURPOSE,
                        request_id='zh-revise-'+brain_id+'-output-12000',
                        prompt=brain_prompt,mission=mission)
                write_json(stage_path,evidence)
            return parse_stage_output(evidence['brain_call']['text'], stage='brain')
        review = run_language_review(review_product,checker=check,brain=revise,
            checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL},
            numeric_source_product=product)
        raise_if_parked(review.get('reason'))
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
            checker=lambda _:parse_stage_output(evidence['checker_call']['text'], stage='checker'),
            brain=lambda _:parse_stage_output(evidence['brain_call']['text'], stage='brain'),
            checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL},
            numeric_source_product=product)
        evidence['language_review']=review
        write_json(stage_path,evidence)

    review_product=dict(product,sections=evidence['draft_localized']['sections'])
    active_brain_call=evidence.get('brain_call')
    history=list(evidence.get('review_history') or [])
    repair_calls=list(evidence.get('repair_brain_calls') or [])
    repairs_used=len(repair_calls)
    if repair_calls:
        active_brain_call=repair_calls[-1]
        prior=next((row for row in reversed(history)
                    if row.get('stage')=='brain_repair'
                    and row.get('brain_call')==active_brain_call),None)
        if prior is None:
            review=run_language_review(review_product,
                checker=lambda _:parse_stage_output(evidence['checker_call']['text'],stage='checker'),
                brain=lambda _:_repair_output(active_brain_call),
                checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL},
                numeric_source_product=product)
            history.append({'stage':'brain_repair','attempt':repairs_used,
                'prior_brain_call':None,'prior_language_review':None,
                'brain_call':copy.deepcopy(active_brain_call),
                'language_review':copy.deepcopy(review),
                'trigger':{'stage':'interrupted_after_brain_call'}})
            evidence['review_history']=history;write_json(stage_path,evidence)
        else:
            review=prior['language_review']
            if review.get('status') == 'pending_brain_revision':
                numeric_hash=source_content_hash(product)
                revalidated=next((row for row in reversed(history)
                    if row.get('stage')=='brain_revalidation'
                    and row.get('brain_call')==active_brain_call
                    and row.get('numeric_source_hash')==numeric_hash),None)
                if revalidated is None:
                    replayed=run_language_review(review_product,
                        checker=lambda _:parse_stage_output(
                            evidence['checker_call']['text'],stage='checker'),
                        brain=lambda _:_repair_output(active_brain_call),
                        checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL},
                        numeric_source_product=product)
                    revalidated={'stage':'brain_revalidation','attempt':repairs_used,
                        'brain_call':copy.deepcopy(active_brain_call),
                        'numeric_source_hash':numeric_hash,
                        'prior_language_review':copy.deepcopy(review),
                        'language_review':copy.deepcopy(replayed)}
                    history.append(revalidated)
                    evidence['review_history']=history
                    write_json(stage_path,evidence)
                review=revalidated['language_review']
    # First revision plus one local repair are the entire language allowance.
    # Historic CLI switches cannot authorize a third language round.
    max_repairs=MAX_LANGUAGE_ROUNDS-1 if repair_reviewed else 0
    if check_only:
        # The one "repair" a check-only chunk has is deterministic: drop the
        # applied suggestions and verify the draft as it was.  Only worth it
        # when a suggestion was applied at all.
        adopted=sum(row.get('decision')=='adopt' for row in
                    (review.get('brain_revision') or {}).get('decisions') or [])
        max_repairs=1 if adopted else 0
        fallback=next((row for row in reversed(history)
                       if row.get('stage')=='deterministic_fallback'),None)
        if fallback is not None:
            review=fallback['language_review'];repairs_used=1
    failure={'stage':'brain_validation','reason':review.get('reason') or review.get('status')}
    if review.get('status') == 'pending_language_review' or (
            active_brain_call is None and not check_only):
        # A still-running or missing call is an infrastructure recovery issue;
        # issuing a new revision request would bypass its existing lease.
        raise ValueError(review.get('reason') or review.get('status'))
    while True:
        if review.get('status') != 'ready_for_publication':
            if repairs_used >= max_repairs:
                if check_only and _check_only_content_verdict(review, evidence):
                    raise CheckOnlyContentFailure(review.get('reason') or review['status'],
                                                  draft_call=evidence.get('draft'))
                raise ValueError(review.get('reason') or review['status'])
        else:
            revision=_revision_hash(review)
            semantic_identity=semantic_stage_identity(identity,verifier_config,revision)
            legacy_semantic_identity=semantic_stage_identity(identity,verifier_config)
            legacy_semantic_path=work_dir/'semantic-stages'/(legacy_semantic_identity+'.json')
            if repairs_used==0 and legacy_semantic_path.exists():
                semantic_identity=legacy_semantic_identity
            target=work_dir/'chunks'/(semantic_identity+'.json')
            semantic_path=work_dir/'semantic-stages'/(semantic_identity+'.json')
            if target.exists():
                saved=read_json(target)
                if (saved.get('pipeline_identity')!=identity
                        or saved.get('semantic_identity')!=semantic_identity
                        or (semantic_identity!=legacy_semantic_identity
                            and saved.get('revision_hash')!=revision)):
                    raise ValueError('saved chunk pipeline changed')
                validate_localized_text(product,saved['localized'])
                proof=independence(draft_routes=saved['producer_routes'],
                    verifier_route=saved['verifier_call']['route_decision_ref'],resolve=resolve)
                if not proof['independent'] or saved['language_review']['status']!='ready_for_publication':
                    raise ValueError('saved language review or verifier independence could not be confirmed')
                build_localization(product,saved['localized'],saved['verifier'])
                return product_index,start,saved
            semantic_evidence=None
            repairable_semantic_rejection=False
            try:
                localized={'sections':review['brain_revision']['sections']}
                validate_localized_text(product,localized)
                routes=[evidence['draft']['route_decision_ref'],
                        *([active_brain_call['route_decision_ref']] if active_brain_call else [])]
                check_prompt=build_verifier_prompt(product,localized)
                semantic_evidence=read_json(semantic_path) if semantic_path.exists() else {
                    'source_hash':source_content_hash(product),'pipeline_identity':identity,
                    'semantic_identity':semantic_identity,'revision_hash':revision,
                    'semantic_contract_version':SEMANTIC_CONTRACT_VERSION}
                if (semantic_evidence.get('pipeline_identity')!=identity
                        or semantic_evidence.get('semantic_identity')!=semantic_identity
                        or (semantic_identity!=legacy_semantic_identity
                            and semantic_evidence.get('revision_hash')!=revision)):
                    raise ValueError('semantic stage identity changed')
                if 'verifier_call' not in semantic_evidence:
                    gate(VERIFIER_PURPOSE)
                    check_id=hashlib.sha256((SEMANTIC_CONTRACT_VERSION+semantic_identity+check_prompt).encode()).hexdigest()
                    verifier=model(verifier_config,5000)
                    semantic_evidence['verifier_call']=independent_model_call(verifier,
                        producer_route_decision_refs=routes,purpose='research_localization_verifier',
                        request_id='zh-verify-'+check_id,prompt=check_prompt,mission=mission)
                    write_json(semantic_path,semantic_evidence)
                checked=semantic_evidence['verifier_call']
                verdict=unwrap_json_object(checked['text'])
                proof=independence(draft_routes=routes,
                    verifier_route=checked['route_decision_ref'],resolve=resolve)
                if not proof['independent']:
                    raise ValueError('semantic verifier model is not independent of both authors')
                repairable_semantic_rejection=isinstance(verdict,dict)
                if repairable_semantic_rejection and verdict.get('verdict')!='pass':
                    try:repair_indices(product,localized['sections'],{'verifier':verdict})
                    except ValueError:
                        repairable_semantic_rejection=False
                        raise
                build_localization(product,localized,verdict)
            except Exception as exc:
                if isinstance(exc,(PurposeDailyCapReached,CockpitModelPoolExhausted,PublicationParked)):raise
                semantic_record={'stage':'semantic','revision_hash':revision,
                    'semantic_identity':semantic_identity,'reason':str(exc),
                    'brain_call':copy.deepcopy(active_brain_call),'language_review':copy.deepcopy(review)}
                if semantic_evidence is not None and semantic_evidence.get('verifier_call'):
                    semantic_record['verifier_call']=copy.deepcopy(semantic_evidence['verifier_call'])
                    try:semantic_record['verifier']=copy.deepcopy(unwrap_json_object(
                        semantic_evidence['verifier_call']['text']))
                    except Exception:pass
                if not any(row==semantic_record for row in history):history.append(semantic_record)
                evidence['review_history']=history
                write_json(stage_path,evidence)
                failure={'stage':'semantic','reason':str(exc),
                    'verifier':semantic_record.get('verifier')}
                if (check_only and repairable_semantic_rejection
                        and repairs_used >= max_repairs):
                    raise CheckOnlyContentFailure(str(exc),
                                                  draft_call=evidence.get('draft')) from exc
                if not repairable_semantic_rejection or repairs_used >= max_repairs:raise
            else:
                all_calls=[evidence['draft'],evidence['checker_call'],evidence.get('brain_call'),
                    *(evidence.get('repair_brain_calls') or []),checked,
                    *(row.get('verifier_call') for row in history)]
                unique_calls={json.dumps(row,sort_keys=True,separators=(',',':')):row
                    for row in all_calls if isinstance(row,dict)}
                total_cost=sum(int(row.get('cost_micros') or 0) for row in unique_calls.values())
                saved={**evidence,'semantic_identity':semantic_identity,'revision_hash':revision,
                    'semantic_contract_version':SEMANTIC_CONTRACT_VERSION,
                    'brain_call':active_brain_call,'language_review':review,
                    'verifier_call':checked,'localized':localized,'producer_routes':routes,
                    'verifier':verdict,'independence':proof,'review_history':history,
                    'total_cost_micros':total_cost,'status':'passed'}
                write_json(target,saved)
                print(json.dumps({'product':product_index,'section_start':start,'status':'passed',
                    'cost_micros':total_cost},ensure_ascii=False),flush=True)
                return product_index,start,saved

        repairs_used+=1
        if check_only:
            prior_review=copy.deepcopy(review)
            checked_suggestions=validate_checker_output(
                parse_stage_output(evidence['checker_call']['text'],stage='checker'),
                sections=list(review_product['sections']))
            review=run_language_review(review_product,
                checker=lambda _:parse_stage_output(evidence['checker_call']['text'],stage='checker'),
                brain=lambda _:deterministic_revision(review_product,checked_suggestions,
                    numeric_source_product=product,apply_suggestions=False),
                checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL},
                numeric_source_product=product)
            history.append({'stage':'deterministic_fallback','attempt':repairs_used,
                'prior_language_review':prior_review,'language_review':copy.deepcopy(review),
                'trigger':copy.deepcopy(failure)})
            evidence['review_history']=history
            write_json(stage_path,evidence)
            failure={'stage':'brain_validation','reason':review.get('reason') or review.get('status')}
            continue
        repair_review=copy.deepcopy(review)
        if 'brain_revision' not in repair_review and active_brain_call is not None:
            repair_review['brain_revision']=_repair_output(active_brain_call)
        repair_prompt=_repair_prompt(product=product,draft_localized=evidence['draft_localized'],
            review=repair_review,failure=failure,attempt=repairs_used)
        candidate=(repair_review.get('brain_revision') or {}).get('sections') or evidence['draft_localized']['sections']
        _,indices=local_repair_prompt(product,candidate,failure,
            decisions=(repair_review.get('brain_revision') or {}).get('decisions'))
        # The router estimates input conservatively from UTF-8 bytes. Keep
        # room for the full source and advice within the unchanged $1 cap.
        repair_output_tokens=12000
        repair_id=hashlib.sha256((identity+str(repairs_used)+repair_prompt+
            ':output-tokens:'+str(repair_output_tokens)).encode()).hexdigest()
        gate(BRAIN_PURPOSE)
        brain=model(brain_config,repair_output_tokens)
        prior_brain_call=copy.deepcopy(active_brain_call)
        repair_call=brain.call(purpose=BRAIN_PURPOSE,request_id='zh-revise-repair-'+repair_id,
            prompt=repair_prompt,mission=mission)
        repair_call['local_patch_binding']={'candidate':copy.deepcopy(candidate),'indices':indices,
            'decisions':copy.deepcopy((repair_review.get('brain_revision') or {}).get('decisions') or []),
            'quotes':local_patch_quotes(candidate,failure,indices)}
        evidence.setdefault('repair_brain_calls',[]).append(copy.deepcopy(repair_call))
        write_json(stage_path,evidence)
        active_brain_call=repair_call
        prior_review=copy.deepcopy(review)
        repaired=run_language_review(review_product,
            checker=lambda _:parse_stage_output(evidence['checker_call']['text'],stage='checker'),
            brain=lambda _:_repair_output(repair_call),
            checker_identity={'provider':CHECKER_PROVIDER,'model':CHECKER_MODEL},
            numeric_source_product=product)
        history.append({'stage':'brain_repair','attempt':repairs_used,
            'prior_brain_call':prior_brain_call,
            'prior_language_review':prior_review,
            'brain_call':copy.deepcopy(repair_call),'language_review':copy.deepcopy(repaired),
            'trigger':copy.deepcopy(failure)})
        evidence['review_history']=history
        write_json(stage_path,evidence)
        review=repaired
        failure={'stage':'brain_validation','reason':review.get('reason') or review.get('status')}


def _check_only_content_verdict(review, evidence):
    """Whether a not-ready check-only review failed on its text.

    ``pending_brain_revision`` is the deterministic reviser refusing the text.
    ``pending_language_review`` is content only when the checker *did* answer
    and its answer cannot be used; a call that never returned is transport.
    """
    if review.get('status') == 'pending_brain_revision':
        return True
    if review.get('status') != 'pending_language_review' or 'checker_call' not in evidence:
        return False
    try:
        validate_checker_output(
            parse_stage_output(evidence['checker_call']['text'], stage='checker'),
            sections=list(evidence['draft_localized']['sections']))
    except (ResearchLanguageReviewError, ValueError):
        return True
    return False


def _source_as_localized(product):
    return {'sections': [{'index': index, 'title': str(row.get('title') or ''),
                          'body': str(row.get('body') or ''),
                          'gaps': [str(gap) for gap in row.get('gaps') or []]}
                         for index, row in enumerate(product.get('sections') or [])]}


def _source_is_chinese(localized):
    """Every non-empty title, body and gap is written in Chinese.

    Stricter than :func:`validate_localized_text`, which accepts a section
    whose title alone is Chinese: kept source text is shown as it is, so each
    field must carry Han text and no more Latin letters than Han characters
    (tickers, file names and product names stay readable; an English body
    under a Chinese heading does not pass).
    """
    import re
    for row in localized['sections']:
        for text in (row['title'], row['body'], *row['gaps']):
            if not text.strip():
                continue
            han = len(re.findall(r'[\u3400-\u9fff]', text))
            latin = len(re.findall(r'[A-Za-z]', text))
            if han == 0 or latin > han:
                return False
    return True


def _source_kept_chunk(task, *, trigger, mission, draft_config, verifier_config,
                       checker_config, brain_config, scheduler_db, work_dir, max_cost,
                       attempts, legacy_verifier_config=None, repair_reviewed=False,
                       extra_brain_repair=False, redraft_generation=0, spend_gate=None,
                       language_tier=LANGUAGE_TIER_FULL):
    """The single deterministic fallback of a check-only chunk.

    Rule: when the source is already Simplified Chinese (it passes the same
    structural, numeric and Chinese-text check a translation must pass), the
    text shown is the source itself.  It is still reviewed like any other
    text -- a checker call on the source (suggestions recorded, none applied)
    and the independent semantic verifier, whose family must differ from the
    failed draft's -- and published only if both pass, under the same
    receipt the required language policy checks.  A source that is not
    Chinese, or that fails either review, ends in :class:`CheckOnlyExhausted`:
    nothing is published and nothing is retried for this source hash.
    """
    gate = spend_gate or (lambda purpose: None)
    product_index, start, product = task
    style_identity = redraft_identity(style_stage_identity(product, draft_config=draft_config,
        checker_config=checker_config, brain_config=CHECK_ONLY_REVISER_CONFIG),
        redraft_generation)
    identity = hashlib.sha256((style_identity + ':' + SOURCE_KEPT_FALLBACK).encode()).hexdigest()
    draft_call = getattr(trigger, 'draft_call', None)
    source_localized = _source_as_localized(product)
    try:
        validate_localized_text(product, source_localized)
        if not _source_is_chinese(source_localized):
            raise ResearchLocalizationError('the source text is not predominantly Simplified Chinese')
    except ResearchLocalizationError as exc:
        raise CheckOnlyExhausted(
            f'check-only fallback unavailable ({exc}); first failure: {trigger}') from trigger
    if not isinstance(draft_call, dict) or not draft_call.get('route_decision_ref'):
        raise CheckOnlyExhausted(
            f'check-only fallback has no drafting route to prove independence; first failure: {trigger}')
    stage_path = work_dir / 'stages' / (identity + '.json')
    semantic_identity = semantic_stage_identity(identity, verifier_config)
    target = work_dir / 'chunks' / (semantic_identity + '.json')
    resolve = router_family_resolver(draft_config)
    routes = [draft_call['route_decision_ref']]
    if target.exists():
        saved = read_json(target)
        if saved.get('pipeline_identity') != identity or saved.get('semantic_identity') != semantic_identity:
            raise ValueError('saved chunk pipeline changed')
        validate_localized_text(product, saved['localized'])
        proof = independence(draft_routes=saved['producer_routes'],
                             verifier_route=saved['verifier_call']['route_decision_ref'],
                             resolve=resolve)
        if not proof['independent'] or saved['language_review']['status'] != 'ready_for_publication':
            raise ValueError('saved language review or verifier independence could not be confirmed')
        build_localization(product, saved['localized'], saved['verifier'])
        return product_index, start, saved
    evidence = read_json(stage_path) if stage_path.exists() else {
        'source_hash': source_content_hash(product), 'pipeline_identity': identity,
        'rules_version': FINAL_TEXT_RULES_VERSION, 'pipeline_version': PIPELINE_VERSION,
        'language_tier': LANGUAGE_TIER_CHECK_ONLY,
        'fallback': {'rule': SOURCE_KEPT_FALLBACK, 'style_identity': style_identity,
                     'trigger': str(trigger)},
        'draft': copy.deepcopy(draft_call), 'draft_localized': source_localized}

    def model(config, output_tokens):
        return GovernedPublicationModel(CockpitModel(config, scheduler_db=scheduler_db, max_cost_usd=max_cost,
                            max_input_tokens=120000, max_output_tokens=output_tokens,
                            timeout_seconds=600),stage_id=identity)

    review_product = dict(product, sections=source_localized['sections'])
    if 'checker_call' not in evidence:
        gate(CHECKER_PURPOSE)
        from dalton_core.research_language_review import build_checker_prompt
        check_prompt = build_checker_prompt(review_product)
        check_id = hashlib.sha256((identity + check_prompt).encode()).hexdigest()
        evidence['checker_call'] = model(checker_config, 12000).call(
            purpose=CHECKER_PURPOSE, request_id='zh-style-source-kept-' + check_id,
            prompt=check_prompt, mission=mission)
        write_json(stage_path, evidence)
    if selected_identity(checker_config, evidence['checker_call']) != {
            'provider': CHECKER_PROVIDER, 'model': CHECKER_MODEL}:
        raise ValueError('language checker served an unexpected transport or model')

    def keep(_):
        checked = validate_checker_output(
            parse_stage_output(evidence['checker_call']['text'], stage='checker'),
            sections=list(review_product['sections']))
        return deterministic_revision(review_product, checked, numeric_source_product=product,
                                      apply_suggestions=False)

    review = run_language_review(
        review_product,
        checker=lambda _: parse_stage_output(evidence['checker_call']['text'], stage='checker'),
        brain=keep, checker_identity={'provider': CHECKER_PROVIDER, 'model': CHECKER_MODEL},
        numeric_source_product=product)
    evidence['language_review'] = review
    write_json(stage_path, evidence)
    if review.get('status') != 'ready_for_publication':
        raise CheckOnlyExhausted(
            f'check-only fallback language review failed ({review.get("reason")}); '
            f'first failure: {trigger}')
    localized = {'sections': review['brain_revision']['sections']}
    semantic_path = work_dir / 'semantic-stages' / (semantic_identity + '.json')
    semantic = read_json(semantic_path) if semantic_path.exists() else {
        'source_hash': source_content_hash(product), 'pipeline_identity': identity,
        'semantic_identity': semantic_identity,
        'semantic_contract_version': SEMANTIC_CONTRACT_VERSION}
    check_prompt = build_verifier_prompt(product, localized)
    if 'verifier_call' not in semantic:
        gate(VERIFIER_PURPOSE)
        check_id = hashlib.sha256((SEMANTIC_CONTRACT_VERSION + semantic_identity + check_prompt).encode()).hexdigest()
        semantic['verifier_call'] = independent_model_call(model(verifier_config, 5000),
            producer_route_decision_refs=routes, purpose='research_localization_verifier',
            request_id='zh-verify-' + check_id, prompt=check_prompt, mission=mission)
        write_json(semantic_path, semantic)
    checked = semantic['verifier_call']
    proof = independence(draft_routes=routes, verifier_route=checked['route_decision_ref'],
                         resolve=resolve)
    if not proof['independent']:
        raise ValueError('semantic verifier model is not independent of both authors')
    verdict = unwrap_json_object(checked['text'])
    try:
        build_localization(product, localized, verdict)
    except (ResearchLocalizationError, ValueError) as exc:
        raise CheckOnlyExhausted(
            f'check-only fallback was refused by the verifier ({exc}); first failure: {trigger}'
        ) from exc
    calls = [row for row in (evidence.get('draft'), evidence['checker_call'], checked)
             if isinstance(row, dict)]
    saved = {**evidence, 'semantic_identity': semantic_identity,
             'semantic_contract_version': SEMANTIC_CONTRACT_VERSION,
             'brain_call': None, 'language_review': review, 'verifier_call': checked,
             'localized': localized, 'producer_routes': routes, 'verifier': verdict,
             'independence': proof, 'review_history': [],
             'total_cost_micros': sum(int(row.get('cost_micros') or 0) for row in calls),
             'status': 'passed'}
    write_json(target, saved)
    print(json.dumps({'product': product_index, 'section_start': start, 'status': 'passed',
                      'fallback': SOURCE_KEPT_FALLBACK,
                      'cost_micros': saved['total_cost_micros']}, ensure_ascii=False), flush=True)
    return product_index, start, saved


class VerifierNotIndependent(ValueError):
    """The verifier chain cannot be independent of the models that produce."""


def drafting_family(draft_config):
    """The model family the drafting chain would write a fresh draft with now.

    The first live link of ``research_localization``'s current chain, read
    from the router without writing anything; ``None`` when it cannot be read.
    """
    from dalton_core.model_fallback_chain import profile_families, tier_chain, tier_for
    from dalton_core.model_router import live_links, resolve_chain
    try:
        with ModelRouter(draft_config['model_router_db'], read_only=True) as router:
            held = profile_families(router)
            policy = router.get_policy(draft_config['routing_policy_ref'])
            tier = tier_for(DRAFT_PURPOSE)
            resolved = resolve_chain(policy, tier=tier, purpose=DRAFT_PURPOSE, profiles=held)
            chain = tuple(resolved['chain']) if resolved is not None else tier_chain(tier)
    except Exception:  # noqa: BLE001 - unreadable: no redraft decision
        return None
    first = next(iter(live_links(chain, held)), None)
    family = (held.get(first) or {}).get('family') if first is not None else None
    return family if isinstance(family, str) and family else None


class StoredDraftNotIndependent(ValueError):
    """The cached draft's family blocks the verifier; the current chain would not.

    2026-10-09: the owner moved ``research_localization`` off Gemini on 10-08,
    yet eight products kept failing the preflight every day, because the
    stage cache (keyed by the configuration files, not by the router's chain)
    still held the draft Gemini wrote before the change and the preflight
    judged that draft.  Raised only when a fresh draft under the current
    chain would pass; ``run_chunk`` then prepares the chunk again from a new
    draft (:func:`route_redraft_identity`).
    """

    def __init__(self, message, *, family):
        super().__init__(message)
        self.family = family


def route_redraft_identity(identity, family):
    """The stage identity of a redraft forced by a drafting-chain change.

    A new ``stages/`` file and new request ids (the scheduler replays a
    completed work order by request id), one per drafting family, so the
    same chain change never buys a second redraft.
    """
    return hashlib.sha256(f'{identity}:route-redraft:{family}'.encode()).hexdigest()


def verifier_preflight(verifier_config, brain_config, *, draft_route_ref, check_only,
                       producer_family=None):
    """Refuse, for free, the product whose verifier would be refused anyway.

    Live 10-06..10-08: 235 localization-verifier calls were refused because
    the verifier chain (gemini-3-8-flash, gemini-3-1-pro-preview) is all
    google-gemini-3 and the draft was written by gemini-3-8-flash-antigravity
    -- after the checker and the Opus revision had been paid for.  This asks
    the router, read-only, what the verifier call will ask: is any live link
    of the verifier chain of a family other than the draft's (and, unless
    check-only, the revision chain's first live link)?  If none is, the chunk
    stops here with the same Chinese reason the cockpit would give.  Anything
    it cannot read is not a reason to stop: the call itself still decides.
    """
    from dalton_core.cockpit_model import (VERIFIER_NOT_INDEPENDENT,
                                           chain_refusal_message, chain_refusal_record)
    from dalton_core.model_fallback_chain import (live_links, profile_families,
                                                  served_family, tier_chain, tier_for)
    from dalton_core.model_router import resolve_chain
    if producer_family is None and (not isinstance(draft_route_ref, str) or not draft_route_ref):
        return None
    try:
        with ModelRouter(verifier_config['model_router_db'], read_only=True) as router:
            held = profile_families(router)
            policy = router.get_policy(verifier_config['routing_policy_ref'])
            tier = tier_for(VERIFIER_PURPOSE)
            resolved = resolve_chain(policy, tier=tier, purpose=VERIFIER_PURPOSE, profiles=held)
            chain = tuple(resolved['chain']) if resolved is not None else tier_chain(tier)
            # ``producer_family``: judge a draft not yet written -- the one the
            # current drafting chain would write -- instead of the cached one.
            families = ({producer_family} if producer_family is not None
                        else {served_family(router, draft_route_ref)})
        if not check_only:
            with ModelRouter(brain_config['model_router_db'], read_only=True) as router:
                brain_held = profile_families(router)
                brain_policy = router.get_policy(brain_config['routing_policy_ref'])
                brain_tier = tier_for(BRAIN_PURPOSE)
                brain = resolve_chain(brain_policy, tier=brain_tier, purpose=BRAIN_PURPOSE,
                                      profiles=brain_held)
                brain_chain = (tuple(brain['chain']) if brain is not None
                               else tier_chain(brain_tier))
                first = next(iter(live_links(brain_chain, brain_held)), None)
                if first is not None and (brain_held.get(first) or {}).get('family'):
                    families.add(brain_held[first]['family'])
    except Exception:  # noqa: BLE001 - unreadable: the call decides, as before
        return None
    from dalton_core.cockpit_model import verifier_route_capability
    capability = verifier_route_capability(VERIFIER_PURPOSE)
    available = live_links(chain, held)
    # Only a link that can serve the verifier's capability counts: on legacy
    # 10-08 only the direct-API Gemini profiles carry provider-controlled-verify.
    capable = [profile for profile in available
               if capability in ((held.get(profile) or {}).get('capabilities') or ())]
    if any((held.get(profile) or {}).get('family') not in families for profile in capable):
        return None
    record = chain_refusal_record(
        {'reason': VERIFIER_NOT_INDEPENDENT, 'purpose': VERIFIER_PURPOSE, 'tier': tier,
         'chain': list(chain), 'producer_families': sorted(families),
         'retired_links': [profile for profile in chain if profile not in available]},
        policy=policy, credential_slot_refs=verifier_config.get('credential_slot_refs') or ())
    raise VerifierNotIndependent(chain_refusal_message(record)
                                 + '（预检拦下：语言检查和修订没有花钱）')


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
    # Refused before a single call is bought: a source whose sections repeat
    # each other is translated faithfully into a document that repeats itself.
    duplicated = {}
    for i, product in enumerate(products):
        from .research_localization import localization_blockers
        blockers=localization_blockers(product)
        if blockers:
            duplicated[i]='a product with a failed section is not localised: '+json.dumps(blockers,ensure_ascii=False)
            continue
        try:
            validate_no_truncated_copies(product)
        except ResearchLocalizationError as exc:
            duplicated[i] = str(exc)
    config_set=[read_json(path) for path in (args.model_config,args.checker_config,args.brain_config,args.verifier_config)]
    reusable={}
    tasks=[]
    for i,product in enumerate(products):
        if i in duplicated: continue
        missing=[]
        for index,section in enumerate(product['sections']):
            key=_section_cache_key(product,section,config_set)
            path=work_dir/'section-cache'/(key+'.json')
            if path.exists():
                cached=read_json(path)
                digest=cached.pop('content_hash',None)
                if digest!=hashlib.sha256(json.dumps(cached,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest():
                    raise ValueError('publication section cache hash drifted')
                if cached.get('source_section')!=section: raise ValueError('publication section cache source drifted')
                saved=cached['review']
                validate_localized_text(projection_slice(product,[index]),saved['localized'])
                build_localization(projection_slice(product,[index]),saved['localized'],saved['verifier'])
                if saved.get('status')!='passed' or not saved.get('independence',{}).get('independent'):
                    raise ValueError('publication section cache proof incomplete')
                reusable.setdefault(i,[]).append((index,saved))
            else: missing.append(index)
        # Keep document order, with one task per contiguous changed section run.
        for index in missing:
            if (tasks and tasks[-1][0]==i and tasks[-1][1]+len(tasks[-1][2]['sections'])==index
                    and len(json.dumps(tasks[-1][2]['sections']+[product['sections'][index]],ensure_ascii=False))<=args.chunk_chars):
                first=tasks[-1][1]
                tasks[-1]=(i,first,projection_slice(product,list(range(first,index+1))))
            else: tasks.append((i,index,projection_slice(product,[index])))
    import uuid
    held_scope = current_scope()
    tick = held_scope[2] if held_scope else uuid.uuid4().hex
    legacy_verifier_config = (read_json(args.legacy_verifier_config)
        if getattr(args, 'legacy_verifier_config', None) else None)
    found = dict(reusable)
    failures = [{'product':i,'section_start':0,'error':error} for i,error in duplicated.items()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_chunk,t,mission=data['mission'],draft_config=read_json(args.model_config),
            verifier_config=read_json(args.verifier_config),checker_config=read_json(args.checker_config),
            brain_config=read_json(args.brain_config),scheduler_db=args.scheduler_db,
            work_dir=work_dir,max_cost=args.max_cost_per_call,attempts=args.attempts,
            legacy_verifier_config=legacy_verifier_config,
            repair_reviewed=getattr(args,'repair_reviewed',False),
            extra_brain_repair=getattr(args,'extra_brain_repair',False),
            redraft_generation=getattr(args,'redraft_generation',0),
            spend_gate=getattr(args,'spend_gate',None),
            language_tier=getattr(args,'language_tier',LANGUAGE_TIER_FULL),
            artifact_source_hash=publication_source_hash(products[t[0]]), publication_tick=tick):t for t in tasks}
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            try:
                i,start,result = future.result()
                found.setdefault(i,[]).append((start,result))
            except PublicationParked as exc:
                failures.append({'product':task[0],'section_start':task[1],'error':str(exc),
                                 'deferred':True,'parked':True,'reason':exc.reason,
                                 'retry_after':exc.retry_after})
            except (PurposeDailyCapReached,CockpitModelPoolExhausted) as exc:
                # Out of today's share, not broken: nothing is published and
                # nothing counts as an attempt; the next UTC day resumes it.
                failures.append({'product':task[0],'section_start':task[1],'error':str(exc),
                                 'deferred':True,'reason':getattr(exc,'reason','pool_exhausted')})
            except CheckOnlyExhausted as exc:
                failures.append({'product':task[0],'section_start':task[1],'error':str(exc),
                                 'exhausted':True})
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
        reviews=[saved for _,saved in sorted(found[i],key=lambda x:x[0])]
        if reusable.get(i) and not all(review.get('full_verification_binding')=={
                'source_hash':source_content_hash(product),'candidate_hash':candidate['content_hash']} for review in reviews):
            # Cached unchanged prose is sent only here, with full original
            # context: a new version can change cross-section meaning. The
            # drafting/check/revision prompts above contain changed sections.
            try:
                _verify_merged_product(product,candidate,reviews,mission=data['mission'],args=args,tick=tick)
            except PublicationParked as exc:
                failures.append({'product':i,'section_start':0,'error':str(exc),'deferred':True,
                                 'parked':True,'reason':exc.reason,'retry_after':exc.retry_after})
                continue
            except Exception as exc:
                failures.append({'product':i,'section_start':0,'error':str(exc)})
                continue
        target=publish_reviewed_attachment(args.output_directory,product,candidate,
            reviews)
        for index,section in enumerate(product['sections']):
            start,saved=next((start,saved) for start,saved in sorted(found[i],key=lambda x:x[0])
                            if start<=index<start+len(saved['localized']['sections']))
            part=copy.deepcopy(saved)
            part['localized']={'sections':[dict(rows[index],index=0)]}
            if reusable.get(i) or len(reviews)==1:
                part['full_verification_binding']={'source_hash':source_content_hash(product),
                                                   'candidate_hash':candidate['content_hash']}
            cache={'source_section':section,'review':part}
            cache['content_hash']=hashlib.sha256(json.dumps(cache,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            path=work_dir/'section-cache'/(_section_cache_key(product,section,config_set)+'.json')
            if not path.exists(): write_json(path,cache)
        published.append({'source':product['version_ref'],'file':str(target),
                          'content_hash':candidate['content_hash']})
        if product['kind']=='ui_text' or product['kind'].startswith('surface_'):
            ui_batches.append({'source':product,'localization':candidate})
    if ui_batches and not any((products[f['product']]['kind']=='ui_text' or products[f['product']]['kind'].startswith('surface_')) for f in failures):
        target=publish_ui_texts(args.output_directory,ui_batches)
        published.append({'source':'ui_text','file':str(target),'batches':len(ui_batches)})
    deferred_only=bool(failures) and all(f.get('deferred') for f in failures)
    exhausted=bool(failures) and all(f.get('exhausted') for f in failures)
    result={'status':'passed' if not failures else DEFERRED_STATUS if deferred_only
            else EXHAUSTED_STATUS if exhausted else 'incomplete',
            'products':len(products),
            'chunks':len(tasks),'published':published,'failures':failures}
    parked=[failure for failure in failures if failure.get('parked')]
    if parked:
        result.update(parked=True,reason=parked[0]['reason'],
                      retry_after=max((failure.get('retry_after') or 0 for failure in parked),default=0))
    write_json(getattr(args, 'result_output', None) or work_dir/'result.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if not failures else 1


def _section_cache_key(product,section,configs):
    value={'kind':product.get('kind'),'subject_ref':product.get('subject_ref'),
           'section':section,'configs':configs,'rules':FINAL_TEXT_RULES_VERSION,
           'contract':SEMANTIC_CONTRACT_VERSION,'convergence':cache_binding(product)['limits_version']}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _verify_merged_product(product,candidate,reviews,*,mission,args,tick):
    config=read_json(args.verifier_config)
    plain={'sections':[{key:row[key] for key in ('index','title','body','gaps')} for row in candidate['sections']]}
    identity=semantic_stage_identity(pipeline_identity(product,[config]),config,
        hashlib.sha256(json.dumps(candidate['sections'],sort_keys=True).encode()).hexdigest())
    path=args.work_dir/'merged-verification'/(identity+'.json')
    routes=sorted({ref for review in reviews for ref in review['producer_routes']})
    resolve=router_family_resolver(read_json(args.model_config))
    if path.exists(): proof=read_json(path)
    else:
        with publication_scope(args.work_dir/'publication-limits.sqlite3',publication_source_hash(product),tick=tick):
            if getattr(args,'spend_gate',None): args.spend_gate(VERIFIER_PURPOSE)
            model=GovernedPublicationModel(CockpitModel(config,scheduler_db=args.scheduler_db,
                max_cost_usd=args.max_cost_per_call,max_input_tokens=120000,max_output_tokens=5000,timeout_seconds=600))
            call=independent_model_call(model,producer_route_decision_refs=routes,purpose=VERIFIER_PURPOSE,
                request_id='zh-verify-merged-'+identity,prompt=build_verifier_prompt(product,plain),mission=mission)
            proof={'call':call,'verifier':unwrap_json_object(call['text']),'identity':identity,
                   'source_hash':source_content_hash(product),'context':'full_source_and_merged_candidate_required'}
            write_json(path,proof)
    if proof.get('identity')!=identity or proof.get('source_hash')!=source_content_hash(product):
        raise ValueError('merged verification cache binding drifted')
    independence_proof=independence(draft_routes=routes,verifier_route=proof['call']['route_decision_ref'],resolve=resolve)
    if not independence_proof['independent']: raise ValueError('merged verifier is not independent of cached authors')
    build_localization(product,plain,proof['verifier'])
    for review in reviews: review['merged_verification']=proof


def prepare_ui_batch(args, mission, product, *, redraft_generation=0):
    """Replay one sealed UI batch through publication, including its mapping.

    An existing reviewed attachment is not enough: its UI dictionary entry
    may still need merging after an interrupted publication.
    ``redraft_generation`` > 0 prepares it from a fresh draft (see
    ``redraft_identity``); the discovery poll asks for it after a formal retry.
    """
    from types import SimpleNamespace
    result_path = args.work_dir / 'ui-text-build-results' / (source_content_hash(product) + '.json')
    ui_args = SimpleNamespace(**{**vars(args), 'result_output': result_path,
                                 'redraft_generation': redraft_generation})
    code = build(ui_args, {'mission': mission, 'products': [product]})
    receipt = read_json(result_path)
    return {'status': 'completed' if code == 0 else _unfinished_status(receipt),
            'receipt': receipt,**{key:receipt[key] for key in ('parked','reason','retry_after') if key in receipt}}


def _unfinished_status(receipt):
    """``deferred`` when every failure was a spend ceiling, ``exhausted`` when
    every failure used its one check-only fallback, else ``pending``."""
    if receipt.get('status') == DEFERRED_STATUS:
        return DEFERRED_STATUS
    if receipt.get('status') == EXHAUSTED_STATUS:
        return EXHAUSTED_STATUS
    return 'pending'


def with_spend_gate(args, gate, priority):
    """A copy of ``args`` whose paid calls ask ``gate`` at ``priority``."""
    from types import SimpleNamespace
    return SimpleNamespace(**{**vars(args), 'spend_gate': gate.bound(priority)})


def model_route_fingerprint(config_paths):
    """What the routers these configs use would decide families by, as one hash.

    Every profile's current family and every policy's current version.  A
    UI text batch that failed on routing or model family is retried from a
    fresh draft when this changes -- a family reclassified, a chain edited --
    and not otherwise, because nothing else can make it pass.
    """
    rows = []
    for db in sorted({str(read_json(Path(path))['model_router_db']) for path in config_paths}):
        connection = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            profiles = connection.execute(
                'SELECT v.profile_id,v.family FROM model_endpoint_profile_versions v '
                'JOIN (SELECT profile_id,MAX(version) AS version FROM '
                'model_endpoint_profile_versions GROUP BY profile_id) latest '
                'ON latest.profile_id=v.profile_id AND latest.version=v.version '
                'ORDER BY v.profile_id').fetchall()
            policies = connection.execute(
                'SELECT policy_id,MAX(version) FROM model_routing_policy_versions '
                'GROUP BY policy_id ORDER BY policy_id').fetchall()
        finally:
            connection.close()
        rows.append({'profiles': [list(row) for row in profiles],
                     'policies': [list(row) for row in policies]})
    return hashlib.sha256(json.dumps(rows, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def validate_worker_config(cfg):
    """Validate the scheduled worker before opening databases or making calls."""
    paths = {'core_db', 'scheduler_db', 'model_config', 'verifier_config',
             'checker_config', 'brain_config', 'work_dir', 'output_directory'}
    required = paths | {'schema_version', 'workers', 'chunk_chars',
                        'max_cost_per_call', 'draft_attempts', 'publication_gate'}
    # Optional because every installed config predates them; absent means the
    # module defaults, which are the values these two knobs used to hard-code.
    optional = {'ui_text_batches_per_run', 'ui_text_max_attempts', WORKER_CONFIG_FIELD,
                'stable_window_seconds'}
    if (not isinstance(cfg, dict) or not required <= set(cfg)
            or set(cfg) - required - optional
            or cfg.get('schema_version') != 'research-publication-worker-config:0.1'):
        raise ValueError('unsupported research publication worker configuration')
    for name in paths:
        if not isinstance(cfg[name], str) or not Path(cfg[name]).is_absolute():
            raise ValueError('publication worker paths must be absolute')
    bounds = {'workers': (1, 8), 'chunk_chars': (100, 50000), 'draft_attempts': (1, 5)}
    for name, (low, high) in bounds.items():
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError('publication worker bounds are invalid')
    if 'stable_window_seconds' in cfg and (
            type(cfg['stable_window_seconds']) is not int
            or not 0 <= cfg['stable_window_seconds'] <= 7 * 24 * 3600):
        raise ValueError('publication worker stable window is invalid')
    for name in optional - {WORKER_CONFIG_FIELD, 'stable_window_seconds'}:
        if name in cfg and (type(cfg[name]) is not int or not 1 <= cfg[name] <= 64):
            raise ValueError('publication worker bounds are invalid')
    if WORKER_CONFIG_FIELD in cfg:
        daily_caps_micros(cfg[WORKER_CONFIG_FIELD])
    cost = cfg['max_cost_per_call']
    if type(cost) not in (int, float) or not 0 < cost <= 1:
        raise ValueError('publication worker cost bound is invalid')


_STDOUT_TEXT_LIMIT = 200
WORKER_STDOUT_SUMMARY_SCHEMA = 'research-publication-worker-stdout-summary:0.1'


#: Small count tables kept whole on the stdout line: 74 distinct blocked
#: sentences said less than "163 attachment_size, 52 numeric_tokens_changed".
_STDOUT_COUNT_TABLES = frozenset({'blocked_reason_classes'})


def _stdout_scalars(value):
    """Scalars kept (long text clipped), collections reduced to their size."""
    summary = {}
    for key, item in value.items():
        if (key in _STDOUT_COUNT_TABLES and isinstance(item, dict) and len(item) <= 32
                and all(isinstance(count, int) for count in item.values())):
            summary[key] = dict(item)
        elif item is None or isinstance(item, (bool, int, float)):
            summary[key] = item
        elif isinstance(item, str):
            summary[key] = (item if len(item) <= _STDOUT_TEXT_LIMIT
                            else item[:_STDOUT_TEXT_LIMIT] + '…')
        elif isinstance(item, (list, tuple, dict)):
            summary[key + '_count'] = len(item)
    return summary


def worker_stdout_summary(result, checkpoint_path):
    """The one line the scheduled worker prints: counts, never payloads.

    launchd appends stdout to publication-worker.stdout.log every five minutes.
    Printing the whole checkpoint (every product identity, every UI-text batch
    and every blocked reason) made each line about 415 KB and the log about
    300 MB. The full checkpoint is still written to worker-last-run.json, which
    is where the cockpit and the operator scripts read it; this line names that
    file so the details are one step away.
    """
    summary = _stdout_scalars(result)
    products = result.get('products')
    if isinstance(products, list):
        statuses = {}
        for product in products:
            status = product.get('status') if isinstance(product, dict) else None
            statuses[str(status)] = statuses.get(str(status), 0) + 1
        summary['product_statuses'] = dict(sorted(statuses.items()))
    ui_texts = result.get('ui_texts')
    if isinstance(ui_texts, dict):
        summary.pop('ui_texts_count', None)
        summary['ui_texts'] = _stdout_scalars(ui_texts)
    summary['schema_version'] = WORKER_STDOUT_SUMMARY_SCHEMA
    summary['checkpoint'] = str(checkpoint_path)
    return summary


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
        print(json.dumps(worker_stdout_summary(result,root/'worker-last-run.json'),
                         ensure_ascii=False),flush=True)
        return 0 if gate['status']=='waiting_for_release_publication' else 1
    fd=os.open(root/'.worker.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a+') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status':'already_running'}));return 0
        from .research_publication_quiesce import QuiesceControl
        quiesce = QuiesceControl(config_path, root)
        if quiesce.requested():
            previous = (read_json(root/'worker-last-run.json')
                        if (root/'worker-last-run.json').exists() else {})
            if not isinstance(previous, dict):
                raise ValueError('publication checkpoint is invalid')
            result = {**previous, 'schema_version':'research-publication-worker-checkpoint:0.1',
                      'status':'quiesced','quiesced':True,
                      'checked_at':quiesce.now(), 'reason':'deployment_quiesce',
                      'last_pass_checked_at':previous.get('last_pass_checked_at')
                          or previous.get('checked_at')}
            write_json(root/'worker-last-run.json',result)
            quiesce.acknowledge(root/'worker-last-run.json')
            print(json.dumps(worker_stdout_summary(result,root/'worker-last-run.json'),
                             ensure_ascii=False),flush=True)
            return 0
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
                args=SimpleNamespace(input=None,only=None,repair_reviewed=True,workers=int(cfg.get('workers',4)),
                    chunk_chars=int(cfg.get('chunk_chars',4500)),max_cost_per_call=float(cfg.get('max_cost_per_call',1)),
                    attempts=int(cfg.get('draft_attempts',2)),work_dir=root,
                    output_directory=Path(cfg['output_directory']),scheduler_db=Path(cfg['scheduler_db']),
                    model_config=Path(cfg['model_config']),verifier_config=Path(cfg['verifier_config']),
                    checker_config=Path(cfg['checker_config']),brain_config=Path(cfg['brain_config']))
                if not 1<=args.workers<=8 or not 1<=args.attempts<=5 or args.chunk_chars<100:
                    raise ValueError('publication worker bounds are invalid')
                budget_db=read_json(args.brain_config).get('budget_db')
                if not isinstance(budget_db,str) or not budget_db:
                    raise ValueError('publication worker brain configuration has no budget_db')
                gate=ledger_gate(budget_db,cfg.get(WORKER_CONFIG_FIELD))
                def priority(product):
                    return publication_priority(connection,product)
                def prepare(product,redraft_generation=0):
                    if has_reviewed_attachment(args.output_directory,product):
                        return {'status':'completed','existing_reviewed_attachment':True}
                    tiered=with_spend_gate(args,gate,priority(product))
                    tiered.language_tier=language_tier(connection,product)
                    # An owner's 「重新进行语言质检」: a fresh draft, not a replay.
                    tiered.redraft_generation=redraft_generation
                    code=build(tiered,{'mission':mission,'products':[product]})
                    receipt=read_json(root/'result.json')
                    return {'status':'completed' if code==0 else _unfinished_status(receipt),
                            'receipt':receipt,**{key:receipt[key] for key in ('parked','reason','retry_after') if key in receipt}}
                from dalton_core.weekly_brief import WeeklyBriefAuthority
                from dalton_core.industry_research import IndustryResearchAuthority
                industry=IndustryResearchAuthority.__new__(IndustryResearchAuthority);industry.connection=connection
                weekly=WeeklyBriefAuthority.__new__(WeeklyBriefAuthority);weekly.connection=connection;weekly.industry_research=industry
                from dalton_core.research_publication_worker import (
                    DEFAULT_STABLE_WINDOW_SECONDS)
                from dalton_core.cockpit_research_library import research_library
                extra=lambda con,mis,company:final_surface_products(con,mis,company,
                    weekly_renderer=weekly.render_markdown)
                def still_current(product,company_ref):
                    # 2026-10-07: asked right before paying for a product;
                    # a version replaced since discovery is skipped.
                    found=list(research_library(connection,mission,company_ref,
                                                localize=False).get('products')or[])
                    found.extend(extra(connection,mission,company_ref))
                    return any(isinstance(item,dict)and item.get('status')=='available'
                               and item.get('kind')==product.get('kind')
                               and item.get('subject_ref')==product.get('subject_ref')
                               and item.get('version_ref')==product.get('version_ref')
                               for item in found)
                result=poll_once(connection,mission,state_dir=root/'products',prepare=prepare,
                    stop_requested=quiesce.requested,
                    priority=priority,extra_reader=extra,still_current=still_current,
                    stable_window_seconds=int(cfg.get('stable_window_seconds',
                                                      DEFAULT_STABLE_WINDOW_SECONDS)))
                from .ui_text_discovery import (
                    DEFAULT_BATCHES_PER_RUN, DEFAULT_MAX_ATTEMPTS, poll_ui_texts)
                from .research_localization_store import load_ui_texts
                ui_result=poll_ui_texts(connection,mission,state_dir=root/'ui-text-products',
                    stop_requested=quiesce.requested,
                    mapping=load_ui_texts(core),
                    # UI text is Claim display backlog: always low priority.
                    prepare=lambda product,redraft_generation=0:prepare_ui_batch(
                        with_spend_gate(args,gate,PRIORITY_LOW),mission,product,
                        redraft_generation=redraft_generation),
                    batches_per_run=int(cfg.get('ui_text_batches_per_run',
                                                DEFAULT_BATCHES_PER_RUN)),
                    max_attempts=int(cfg.get('ui_text_max_attempts',
                                             DEFAULT_MAX_ATTEMPTS)),
                    route_fingerprint=lambda:model_route_fingerprint([
                        args.model_config,args.verifier_config,
                        args.checker_config,args.brain_config]))
                result['ui_texts']=ui_result
                result['pending']+=ui_result['pending']
                # Both are UI-text batches only: no publication product is
                # ever "blocked" (a product is pending, exhausted or deferred).
                result['blocked']=ui_result.get('blocked',0)
                result['blocked_reasons']=ui_result.get('blocked_reasons',[])
                result['blocked_reason_classes']=ui_result.get('blocked_reason_classes',{})
                result['quiesced'] = bool(result.get('quiesced') or ui_result.get('quiesced')
                                         or quiesce.requested())
                result['status']=('quiesced' if result['quiesced'] else
                                  'healthy' if not result['pending'] else 'pending')
        finally:
            connection.close()
        from datetime import datetime,timezone
        result['schema_version']='research-publication-worker-checkpoint:0.1'
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        write_json(root/'worker-last-run.json',result)
        quiesce.acknowledge(root/'worker-last-run.json')
        print(json.dumps(worker_stdout_summary(result,root/'worker-last-run.json'),
                         ensure_ascii=False),flush=True)
        # A backlog is not a fault. This returned 1 whenever anything was still
        # pending, so launchd recorded a failing service every five minutes for
        # a worker that was doing exactly what it is supposed to do. A real
        # fault -- an unreadable config, an unusable gate, an exception -- is
        # still non-zero, through the gate branch above or by propagating.
        return 0 if result['status'] in {'healthy','no_current_mission','pending','quiesced'} else 1


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
    run.add_argument('--result-output', type=Path,
                     help='save this batch receipt separately when processing disjoint product sets')
    run.add_argument('--repair-reviewed',action='store_true',
                     help='allow at most two brain-only repairs after a completed language check')
    run.add_argument('--extra-brain-repair',action='store_true',
                     help='explicit recovery: allow a third total brain repair, reusing the original language check')
    run.add_argument('--workers',type=int,default=4,choices=range(1,9))
    run.add_argument('--chunk-chars',type=int,default=4500)
    run.add_argument('--max-cost-per-call',type=float,default=1.0)
    run.add_argument('--attempts',type=int,default=3,choices=range(1,6))
    run.add_argument('--only',nargs='+')
    from dalton_core.research_publication_worker import add_retry_arguments, run_retry_command
    retry=sub.add_parser('retry-products',
        help='owner retry of one pending or exhausted publication product (dry run unless --apply)')
    where=retry.add_mutually_exclusive_group(required=True)
    where.add_argument('--config',type=Path,help='the worker configuration (work_dir/products)')
    where.add_argument('--state-dir',type=Path,help='…/research-publication-work/products')
    add_retry_arguments(retry)
    args=parser.parse_args()
    if args.command=='run-worker':
        return run_worker(args.config)
    if args.command=='retry-products':
        state_dir=args.state_dir or Path(read_json(args.config)['work_dir'])/'products'
        print(json.dumps(run_retry_command(state_dir,args),ensure_ascii=False,indent=1,
                         sort_keys=True))
        return 0
    if args.command=='snapshot':
        data=snapshot(args.core_db,include_surfaces=args.include_surfaces);write_json(args.output,data)
        print(json.dumps({'products':len(data['products']),'sections':sum(len(p['sections']) for p in data['products'])}))
        return 0
    return build(args)

if __name__=='__main__':
    sys.exit(main())
