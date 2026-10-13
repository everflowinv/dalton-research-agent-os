"""Read unanswered memo questions from immutable, source-checked producer calls.

The result is attention for ordinary planning, never financial evidence, an
answered research question, or permission to spend. No writer is constructed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .store import content_hash

CONTRACT_REF = 'investment-memo-planner-feedback:0.1'
MAX_REQUESTS = 32
MAX_SECONDS = 12


def signature(state_dir):
    """Cheap wake-up hint only; the planner's reader proves the actual content."""
    from .planner_paid_output_recovery import _authority_connection
    path = Path(state_dir) / 'scheduler.sqlite'
    if not path.is_file():
        return 'not_installed'
    try:
        with _authority_connection(path) as c:
            deadline = time.monotonic() + 1
            c.execute('PRAGMA busy_timeout=250')
            c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            row = c.execute("SELECT COUNT(*),MAX(f.rowid) FROM scheduler_formal_results f "
                "JOIN scheduler_work_orders w ON w.work_order_id=f.work_order_id "
                "WHERE json_extract(w.work_order_json,'$.metadata.purpose')='investment_memo'").fetchone()
            return content_hash(list(row))
    except (OSError, ValueError, sqlite3.Error):
        return 'unavailable'


def _context(prompt):
    from .investment_memo_timeout_recovery import prompt_identity
    company, group, index, material_hash = prompt_identity(prompt)
    material = json.loads(prompt.split('\nEvidence rows:\n', 1)[1].split('\nReturn raw JSON only', 1)[0])
    contract = json.JSONDecoder().raw_decode(prompt.split(
        '\nReturn raw JSON only with exactly this shape and the exact requested titles/questions:\n', 1)[1])[0]
    return index, material_hash, dict(company=company, group=group, material=material,
        section_titles=[s['title'] for s in contract['sections']],
        questions=[{k: q[k] for k in ('question_ref', 'question')} for q in contract['key_questions']])


def _read(core, *, state_dir, mission, company_ref, config, playbook):
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    from .investment_memo_macro_accounting import MemoMacroAccounting, SCOPES, REQUESTS, IMPORTS, _body, _works
    from .investment_memo_draft import GROUPS
    from .investment_memo_group_maintenance import validate_answer, free_revalidated_parent
    from .investment_memo_legacy_accounting import bill
    from .scheduler import Scheduler
    from .cockpit_model import CockpitModel
    from .investment_memo_question_gaps import build

    state = Path(state_dir)
    now = datetime.now(timezone.utc)
    deadline = time.monotonic() + MAX_SECONDS
    model = SimpleNamespace(config=config, scheduler_db=str(state/'scheduler.sqlite'), clock=lambda: now)
    with _authority_connection(model.scheduler_db) as c:
        c.execute('BEGIN')
        c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SCOPES,)).fetchone() is None:
            return {'status': 'not_recorded', 'items': []}
        row = c.execute(f'SELECT * FROM {SCOPES} WHERE '
            "json_extract(record_json,'$.company_ref')=? ORDER BY rowid DESC LIMIT 1", (company_ref,)).fetchone()
        if row is None:
            return {'status': 'not_recorded', 'items': []}
        scope = _body(row)
        ref = 'memo-accounting-macro:' + content_hash({k: scope[k] for k in
            ('contract_ref', 'company_ref', 'material_hash', 'normal_requests')})
        if (scope['contract_ref'] != 'investment-memo-macro-accounting:0.1'
                or row['macro_ref'] != ref or row['macro_binding'] != ref
                or len(scope['normal_requests']) != 4):
            raise ValueError('memo feedback scope identity differs')
        reader = Scheduler.__new__(Scheduler); reader.connection = c
        contexts, prompts = [], []
        for index, entry in enumerate(scope['normal_requests']):
            works = _works(c, entry)
            if not works:
                return {'status': 'awaiting_groups', 'items': []}
            work = works[0][0]
            i, mh, context = _context(work.question)
            if i != index or mh != scope['material_hash'] or context['company']['company_ref'] != company_ref:
                raise ValueError('memo feedback normal group scope differs')
            titles = [playbook['deliverable_templates']['investment_memo'][i] for i in GROUPS[index][1]]
            questions = [{'question_ref': f'memo_q{i+1:02d}', 'question': playbook['key_questions'][i]}
                         for i in GROUPS[index][2]]
            if context['section_titles'] != titles or context['questions'] != questions:
                raise ValueError('memo feedback playbook questions or sections differ')
            contexts.append(context); prompts.append(work.question)
        macro = MemoMacroAccounting.__new__(MemoMacroAccounting)
        macro.model = model; macro.prompts = tuple(prompts); macro.mission = mission
        macro.ref = ref; macro.scope = scope; macro.scan_error = None
        old = c.execute(f'SELECT * FROM {IMPORTS} WHERE macro_ref=?', (ref,)).fetchone()
        macro.legacy = None if old is None else _body(old)
        rows = c.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? LIMIT ?', (ref, MAX_REQUESTS+1)).fetchall()
        if len(rows) > MAX_REQUESTS:
            raise ValueError('memo feedback request bound exceeded')
        entries = [_body(r) for r in rows] + macro._legacy_entries(c, check_growth=False)
        if len(entries) > 2 * MAX_REQUESTS:
            raise ValueError('memo feedback legacy request bound exceeded')
        unique = {}
        for entry in entries:
            key = entry['request_id']
            if key in unique and unique[key] != entry:
                raise ValueError('memo feedback request identity conflict')
            unique[key] = entry
        selected = {}
        for entry in unique.values():
            if entry['purpose'] != 'investment_memo':
                continue
            for work, authority in _works(c, entry):
                i, mh, context = _context(work.question)
                if (i != entry['group_index'] or context != contexts[i]
                        or mh != scope['material_hash']):
                    raise ValueError('memo feedback repair changed original material or question')
                if i not in selected or authority['append_rowid'] > selected[i][1]['append_rowid']:
                    selected[i] = work, authority
        if set(selected) != set(range(4)):
            return {'status': 'awaiting_groups', 'items': []}
        answers, calls, proof = [], [], []
        with recovery_readers(config['model_router_db'], config['budget_db'], now=now) as (router, budget):
            if budget is None:
                raise ValueError('memo feedback selected call has no fee authority')
            for i in range(4):
                if time.monotonic() > deadline:
                    raise ValueError('memo feedback verification time bound exceeded')
                work, authority = selected[i]
                # Honor the same free validated-parent selection as production.
                free = free_revalidated_parent(macro, {'purpose': 'investment_memo',
                    'prompt': prompts[i], 'request_id': scope['normal_requests'][i]['request_id'], 'mission': mission})
                if free is not None:
                    from .contracts import WorkOrder
                    authority = reader.work_order_authority(free['work_order_ref'])
                    work = WorkOrder.from_dict(authority['work_order'])
                if (work.metadata.get('mission_version_ref') != mission['id']
                        or work.metadata.get('mission_version_hash') != mission['content_hash']):
                    return {'status': 'previous_mission', 'items': []}
                formal = reader.formal_result(work.id)
                if formal is None or formal['terminal_state'] != 'succeeded':
                    return {'status': 'awaiting_groups', 'items': []}
                paid = bill(c, router, budget, authority, formal)
                call = CockpitModel._answer(formal, work, True, 0, 'replayed')
                outcome = validate_answer(macro, contexts[i], call)
                if outcome.get('status') != 'drafted':
                    return {'status': 'content_or_source_refused', 'items': []}
                answers.extend(outcome['key_questions'])
                calls.append({'group': GROUPS[i][0], **{k: call[k] for k in
                    ('work_order_ref', 'route_decision_ref', 'result_envelope_ref', 'invocation_ref')}})
                proof.append({'work_order_ref': work.id, 'work_order_hash': authority['work_order_hash'],
                    'formal_hash': paid['formal_hash']})
        frozen = {'company': contexts[0]['company'], 'mission': mission, 'playbook': playbook,
                  'material': contexts[0]['material']}
        if time.monotonic() > deadline:
            raise ValueError('memo feedback verification time bound exceeded')
        gaps = build(frozen, answers, calls)
        if not gaps['items']:
            return {'status': 'questions_answered', 'items': []}
        semantic = {'contract_ref': CONTRACT_REF, 'company_ref': company_ref,
            'mission': gaps['mission'], 'playbook': gaps['playbook'],
            'items': [{k: item[k] for k in ('question_ref', 'question', 'falsifier')} for item in gaps['items']]}
        return {'status': 'verified', 'contract_ref': CONTRACT_REF, 'items': gaps['items'],
            'semantic_hash': content_hash(semantic), 'proof_hash': content_hash(proof),
            'material_hash': gaps['material_hash'], 'financial_evidence': False, 'retry_authority': False}


def read_feedback(core, *, state_dir, mission, company_ref, config):
    from .coverage_mission import CoverageMissionAuthority
    from .research_playbook import read_exact_playbook_version
    from .initial_screen_failure_feedback import PROOF_ERRORS
    try:
        owner = CoverageMissionAuthority.__new__(CoverageMissionAuthority); owner.connection = core
        held = owner._mission_record(mission['id'])
        if held != mission or company_ref not in {e['company_ref'] for e in held['universe']}:
            raise ValueError('memo feedback current mission or company differs')
        binding = mission['bindings']['playbook_version']
        playbook = read_exact_playbook_version(core, binding['ref'])
        if playbook['content_hash'] != binding['hash']:
            raise ValueError('memo feedback bound playbook differs')
        return _read(core, state_dir=state_dir, mission=mission, company_ref=company_ref,
                     config=config, playbook=playbook)
    except PROOF_ERRORS as exc:
        return {'status': 'unverified', 'items': [], 'reason': 'memo_question_proof_unavailable',
                'error_type': type(exc).__name__}
