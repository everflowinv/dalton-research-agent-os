"""Revalidate a closed paid screen; never repurchase its drafting/repair slots.

Frozen JSON is an input hint. Exact stored Work, formal, route, all admissions,
original spans and current mission permission must independently close it.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from pathlib import Path
from .store import canonical_json, content_hash
from .initial_screen_failure_feedback import (_read, _write, _body, _settled_call,
    validation_prompt, _source_resolvers, PROOF_ERRORS)

CONTRACT = 'initial-screen-validator-recheck:0.1'
MAX_TICKETS = 8192
MAX_RECENT = 64
MAX_WORKS = 512
MAX_WORK_BYTES = 64 * 1024 * 1024


def validator_fingerprint():
    from .product_contract import contract_fingerprint
    from .research_reasoning_lint import lint_fingerprint
    from .mission_deliverable import NUMBER_SOURCE_CONTRACT_VERSION
    from .initial_screen import STRL_CONTRACT_VERSION
    from .initial_screen_estimate_citation_repair import CONTRACT_REF as ESTIMATE_REPAIR_REF
    return content_hash({'contract': CONTRACT, 'producer': STRL_CONTRACT_VERSION,
        'product': contract_fingerprint(), 'numbers': NUMBER_SOURCE_CONTRACT_VERSION, 'estimate_citation_repair': ESTIMATE_REPAIR_REF,
        'lint': lint_fingerprint()})


def _base(mission, context):
    return f"{mission['id']}:{context['company']['company_ref']}:initial_screen:strl:{context['pack']['content_hash'][:16]}"


def _works(model, base):
    # The PK range is index bounded, never a JSON-filter full-table scan. An
    # overfull history refuses recovery instead of silently ignoring old slots.
    from .planner_paid_output_recovery import _authority_connection
    from .contracts import WorkOrder
    found = []
    with _authority_connection(model.scheduler_db) as connection:
        deadline=time.monotonic()+2
        connection.set_progress_handler(lambda: int(time.monotonic()>deadline), 1000)
        total=0
        for purpose in ('initial_screen','initial_screen_verifier'):
            prefix='work:cockpit-'+purpose+'-'
            rows=connection.execute('SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id>=? AND work_order_id<? ORDER BY work_order_id LIMIT ?',
                (prefix,prefix+'g',MAX_WORKS+1)).fetchall()
            if len(rows)>MAX_WORKS:raise ValueError('initial-screen recovery Work inventory exceeds bound')
            for row in rows:
                total+=len(row['work_order_json'].encode())
                if total>MAX_WORK_BYTES:raise ValueError('initial-screen recovery Work bytes exceed bound')
                value=WorkOrder.from_dict(json.loads(row['work_order_json'])).to_dict()
                request=value['metadata'].get('request_id','')
                if not request.startswith(base+':'):continue
                if canonical_json(value)!=row['work_order_json'] or content_hash(value)!=row['work_order_hash']:
                    raise ValueError('initial-screen recovery Work hash differs')
                if value['metadata'].get('purpose')!=purpose:raise ValueError('initial-screen recovery purpose differs')
                found.append(value)
    return found


def _role(request, base):
    # Retain the ordinary existing capacity/transport suffix; it does not
    # create a semantic slot. Every returned Work is still formally verified.
    match=re.fullmatch(re.escape(base)+r':(worksheet(?::repair)?|document:[01]|verify:[01])(?::(?:capacity-policy|transport-policy|producer|route-admission|operator-recovery|retired-rule):[0-9a-f]+|:capacity-recovery:[0-9]+:[0-9a-f]+)*',request)
    if not match:raise ValueError('initial-screen recovery has an unknown original slot')
    return match[1]


def _answer(model, mission, work):
    from .planner_paid_output_recovery import _authority_connection
    with _authority_connection(model.scheduler_db) as connection:
        row=connection.execute('SELECT result_envelope_json FROM scheduler_formal_results WHERE work_order_id=?',(work['id'],)).fetchone()
    if row is None:raise ValueError('initial-screen original slot has no terminal formal')
    result=json.loads(row[0]);meta=result.get('metadata') or {}
    call={'work_order_ref':work['id'],'result_envelope_ref':result['id'],
        'invocation_ref':result.get('invocation_ref'),'route_decision_ref':meta.get('route_decision_ref'),'cost_status':'replayed'}
    proof,held,envelope=_settled_call(model,call,mission,purpose=work['metadata']['purpose'])
    if held!=work:raise ValueError('initial-screen original Work changed')
    from .cockpit_model import unwrap_json_object
    return unwrap_json_object(envelope['outputs']['text']),call,proof


def _repair_question(question, expected, context, *, worksheet=False):
    marker=validation_prompt('',context)
    if worksheet:
        # Worksheet repair appends its instruction after the bound root.
        expected=validation_prompt(expected,context)
        tail=question.removeprefix(expected)
        if not question.startswith(expected) or not tail.startswith('\n\nYour previous worksheet was rejected: ') or not tail.endswith('\nReturn the complete worksheet again with every finding fixed.') or len(tail)>16000:
            raise ValueError('initial-screen worksheet repair prompt differs')
    else:
        if not question.endswith(marker):raise ValueError('initial-screen original lacks complete validation-context binding')
        plain=question[:-len(marker)]
        tail=plain.removeprefix(expected)
        header='\n\nThe verifier rejected your previous document for these findings. Rewrite the whole document fixing every one of them; keep everything else:\n'
        if not plain.startswith(expected) or not tail.startswith(header) or len(tail)>16000 or not all(line.startswith('- ') for line in tail[len(header):].splitlines()):
            raise ValueError('initial-screen document repair prompt differs')


def _root(state, binding, previous, mission, model, authority):
    from .initial_screen_failed_input import input_binding
    from .initial_screen_quiesce import _binding, _scope
    root=Path(state)/'initial-screens'
    if root.is_symlink():raise ValueError('initial-screen child root is a symlink')
    explicit=previous.get('source_ticket_ref')
    if explicit:
        if not re.fullmatch(r'initial-screen:[0-9a-f]{24}',explicit):raise ValueError('initial-screen failed source ticket differs')
        directories=[root/explicit.split(':')[1]]
    else:
        # Legacy failures omitted the ticket pointer. Only a bounded recent
        # private inventory is a hint; its summary cannot certify paid results.
        import os
        started=time.monotonic()
        entries=[]
        if root.exists():
            with os.scandir(root) as inventory:
                for entry in inventory:
                    if len(entries)>=MAX_TICKETS or time.monotonic()-started>2:
                        raise ValueError('initial-screen legacy metadata inventory exceeds bound')
                    if entry.is_dir(follow_symlinks=False) and re.fullmatch('[0-9a-f]{24}',entry.name):
                        entries.append((entry.stat(follow_symlinks=False).st_mtime_ns,Path(entry.path)))
        if len(entries)>MAX_TICKETS:raise ValueError('initial-screen legacy ticket inventory exceeds bound')
        directories=[path for _,path in sorted(entries,reverse=True)[:MAX_RECENT]]
    for directory in directories:
        if not (directory/'frozen-context.json').exists():continue
        if any((directory/name).stat().st_size>128000 for name in ('ticket.json','summary.json')):
            raise ValueError('initial-screen legacy ticket summary exceeds bound')
        ticket=_read(directory/'ticket.json');summary=_read(directory/'summary.json')
        if (ticket.get('status')!='failed' or ticket.get('exit_code')!=1 or summary.get('status')!='failed'
            or summary.get('drafted',{}).get('company_ref')!=binding['company_ref']
            or str(summary.get('failure_reason') or '')[:700]!=previous['failure_reason']):continue
        from .launch_drain import _pid_alive
        from .lane_child_launcher import ticket_identity
        if _pid_alive(ticket.get('pid')) and ticket_identity(ticket,directory/'ticket.json') is not False:
            raise ValueError('initial-screen original caller is still live or its exit is unproved')
        frozen=_read(directory/'frozen-context.json');_body(frozen);context=frozen['context']
        if (frozen['schema_version']!='initial-screen-frozen-context:0.1' or frozen['ticket_binding']!=_binding(ticket)
            or context.get('mode')!='strl' or frozen['scope']!=_scope(authority,mission,context)):
            raise ValueError('initial-screen failed frozen scope differs')
        candidate=input_binding(mission,context['company'],context['pack'],context.get('prior_reference'),context['source_bindings'],reopen=context['entry'].get('reopen'),execution=binding['execution_binding'])
        if canonical_json(candidate)!=canonical_json(binding) or content_hash(context['source_bindings'])!=previous['source_binding_hash']:continue
        return directory,ticket,context
    raise ValueError('initial-screen failed input has no closed frozen-context candidate')


def qualify(*, state, binding, previous, mission, model, authority):
    from .initial_screen import (build_worksheet_prompt,build_document_prompt,validate_worksheet,
        parse_document_output,verify_document,InitialScreenContractError)
    directory,ticket,context=_root(state,binding,previous,mission,model,authority)
    base=_base(mission,context);works=_works(model,base);slots={};calls=[];proofs=[]
    for work in works:
        role=_role(work['metadata']['request_id'],base)
        if role in slots:raise ValueError('initial-screen original slot has multiple Work identities; no paid recovery')
        value,call,proof=_answer(model,mission,work)
        slots[role]=(work,value,call);calls.append(call);proofs.append(proof)
    sheet_role='worksheet:repair' if 'worksheet:repair' in slots else 'worksheet'
    if sheet_role not in slots:raise ValueError('initial-screen original worksheet is absent')
    sheet_work,raw,_=slots[sheet_role]
    expected=build_worksheet_prompt(company=context['company'],pack=context['pack'],prior_reference=context.get('prior_reference'))
    if sheet_role.endswith(':repair'):_repair_question(sheet_work['question'],expected,context,worksheet=True)
    elif sheet_work['question']!=validation_prompt(expected,context):raise ValueError('initial-screen original worksheet context differs')
    try:worksheet,findings=validate_worksheet(raw,context['pack'])
    except InitialScreenContractError as exc:worksheet,findings=None,[{'code':'worksheet_shape','detail':str(exc)}]
    sections=[];attempt=None
    if worksheet is not None and not findings:
        attempt=1 if 'document:1' in slots else 0
        role='document:'+str(attempt)
        if role not in slots:raise ValueError('initial-screen original document is absent; cannot draft on recovery')
        work,raw,_=slots[role]
        expected=build_document_prompt(company=context['company'],pack=context['pack'],worksheet=worksheet,prior_reference=context.get('prior_reference'))
        if attempt:_repair_question(work['question'],expected,context)
        elif work['question']!=validation_prompt(expected,context):raise ValueError('initial-screen original document context differs')
        try:sections,findings=parse_document_output(raw,pack=context['pack'])
        except InitialScreenContractError as exc:findings=[{'code':'document_shape','detail':str(exc)}]
        if sections:
            live,cell,passage,sources=_source_resolvers(authority,sections,context['pack'],context['source_bindings'],original_proof_scope='initial_screen_checkpoint')
            resolve_request=authority.request_resolver(binding['company_ref'])
            def request(ref):
                row=resolve_request(ref) if resolve_request else None
                if row:sources[ref]={'record_hash':row['content_hash']}
                return row
            findings+=verify_document(sections,worksheet=worksheet,pack=context['pack'],live_claim_refs=live,resolve_cell=cell,resolve_passage=passage,resolve_request=request)['findings']
        else:sources={}
    else:sources={}
    body={'contract_ref':CONTRACT,'validator_fingerprint':validator_fingerprint(),
        'source_ticket_ref':ticket['id'],'source_ticket_hash':content_hash(ticket),'input_hash':binding['content_hash'],
        'binding':binding,'previous':previous,
        'frozen_context_hash':content_hash(context),'pack_hash':context['pack']['content_hash'],'paid_prefix':[proof for work,proof in zip(works,proofs) if work['metadata']['purpose']=='initial_screen'],'source_proofs':sources,
        'findings':findings,'stage':'document' if attempt is not None else 'worksheet',
        'verifier_slot':None if attempt is None else base+':verify:'+str(attempt)}
    body=json.loads(canonical_json(body))
    return {'record':{**body,'content_hash':content_hash(body)},'context':context,'worksheet':worksheet,'sections':sections,
        'slots':slots,'calls':calls,'attempt':attempt,'directory':directory,'all_paid_proofs':proofs}


def persist(state, result):
    record=result['record'];directory=Path(state)/'initial-screen-validator-rechecks'
    directory.mkdir(mode=0o700,exist_ok=True)
    key=record['content_hash']
    _write(directory/(key+'.json'),record,immutable=True)
    _write(directory/(content_hash(result['context']['company']['company_ref'])+'.json'),{'receipt':key+'.json'})
    return record


def verifier(result, *, model, mission, state, summary_dir=None):
    from .initial_screen import build_verifier_prompt,parse_verifier_output
    from .cockpit_model import independent_model_call,unwrap_json_object
    record=result['record']
    if record['findings'] or not result['sections']:raise ValueError('initial-screen current deterministic validation rejected')
    request=record['verifier_slot'];role='verify:'+str(result['attempt'])
    prompt=build_verifier_prompt(result['sections'],worksheet=result['worksheet'],pack=result['context']['pack'])
    current_works=_works(model,_base(mission,result['context']))
    if [work['id'] for work in current_works]!=[slot[0]['id'] for slot in result['slots'].values()]:
        raise ValueError('initial-screen verifier inventory changed before admission')
    routes=[slot[2]['route_decision_ref'] for role,slot in result['slots'].items() if not role.startswith('verify:')]
    routes+=result.get('maintenance_route_refs',[])
    if role in result['slots']:
        work,value,call=result['slots'][role]
        if work['question']!=prompt or sorted(work['metadata'].get('producer_route_decision_refs',[]))!=sorted(set(routes)):
            raise ValueError('initial-screen original verifier input differs')
        return parse_verifier_output(value),call
    if summary_dir is not None and (Path(summary_dir)/'quiesce-request.json').exists():
        raise ValueError('cooperative stop requested before the unconsumed verifier; no slot issued')
    # Slot consumption is fixed on the original parent/request, NEVER on the
    # new validator, day, model, budget or configuration revision.
    directory=Path(state)/'initial-screen-verifier-slots';directory.mkdir(mode=0o700,exist_ok=True)
    path=directory/(content_hash({'mission':mission['id'],'request':request})+'.json')
    if path.exists():raise ValueError('initial-screen original verifier was issued; missing/failed/unknown outcome cannot authorize another call')
    from decimal import Decimal
    from .call_budget import resolve_run_budget
    from .host_research_reconcile import resolved_model_config
    effective=model.budget_for('initial_screen_verifier')
    call_ceiling=int(Decimal(str(effective['max_cost_usd']))*1_000_000)
    charged=sum(proof['accounted_micros'] for proof in result['all_paid_proofs'])
    configuration=resolved_model_config(state,model.config,'initial_screen_verifier')
    # Initial-screen legacy contracts had no dedicated macro ledger. Honor
    # any real current run limit, and keep this original unconsumed slot to
    # ONE admitted attempt. Do not mistake a per-call cap for an unlimited run.
    run=resolve_run_budget(configuration,'initial_screen',defaults={'max_cost_usd':float(Decimal(charged+call_ceiling)/1_000_000)})
    if charged+call_ceiling>int(Decimal(str(run['max_cost_usd']))*1_000_000):
        raise ValueError('initial-screen retained paid prefix leaves no authorized verifier reserve')
    def deny_extra_admission(*,work,attempt_number,reserved_micros,mission_binding):
        from .planner_paid_output_recovery import _authority_connection
        if (work.metadata.get('purpose')!='initial_screen_verifier'
                or _role(work.metadata.get('request_id',''),_base(mission,result['context']))!=role
                or work.question!=prompt or work.metadata.get('mission_version_ref')!=mission['id']
                or mission_binding.get('mission_version_ref')!=mission['id']
                or mission_binding.get('mission_version_hash')!=mission['content_hash']
                or int(Decimal(str(work.budget['max_cost_usd']))*1_000_000)>call_ceiling
                or reserved_micros>call_ceiling):
            raise ValueError('initial-screen original verifier admission binding exceeds its fixed slot')
        # All possible aliases of this original slot are PK-inventoried again.
        # Any prior reservation remains charged/unknown, never treated as free.
        current=_works(model,_base(mission,result['context']))
        with _authority_connection(model.config['budget_db']) as budget:
            for saved in current:
                if _role(saved['metadata']['request_id'],_base(mission,result['context']))!=role:continue
                if budget.execute('SELECT 1 FROM thesis_impact_day_admissions WHERE work_order_ref=? LIMIT 1',(saved['id'],)).fetchone():
                    raise ValueError('initial-screen recovered verifier already reserved its one admitted attempt')
    from copy import copy
    bounded_model=copy(model)
    prior_guard=getattr(model,'_run_cost_admission_guard',None)
    def guard(**kwargs):
        if prior_guard is not None:prior_guard(**kwargs)
        deny_extra_admission(**kwargs)
    bounded_model._run_cost_admission_guard=guard
    body={'contract_ref':CONTRACT,'parent':record['source_ticket_ref'],'request_id':request,
        'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'paid_prefix_hash':content_hash(record['paid_prefix']),
        'mission_ref':mission['id'],'mission_hash':mission['content_hash']}
    # Owner-only single child runs under the existing launcher lock. Failures
    # after this atomic issue keep the slot closed; no reset or refund occurs.
    import os
    record={**body,'content_hash':content_hash(body)}
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(canonical_json(record)+'\n');stream.flush();os.fsync(stream.fileno())
    call=independent_model_call(bounded_model,producer_route_decision_refs=routes,purpose='initial_screen_verifier',request_id=request,prompt=prompt,mission=mission)
    _,work,envelope=_settled_call(model,call,mission,purpose='initial_screen_verifier')
    if work['question']!=prompt:raise ValueError('initial-screen resumed verifier prompt differs')
    return parse_verifier_output(unwrap_json_object(envelope['outputs']['text'])),call


def read_feedback(authority, *, state, mission, model, company_ref):
    """Current diagnostics are recomputed, not certified by an old summary."""
    directory=Path(state)/'initial-screen-validator-rechecks'
    pointer=directory/(content_hash(company_ref)+'.json')
    if not pointer.exists():return None
    try:
        ref=_read(pointer)['receipt']
        if not re.fullmatch(r'[0-9a-f]{64}\.json',ref):raise ValueError('initial-screen recheck pointer differs')
        record=_read(directory/ref);_body(record)
        if record['binding']['company_ref']!=company_ref or record['validator_fingerprint']!=validator_fingerprint():
            raise ValueError('initial-screen current validation contract differs')
        latest=authority.latest('mission-deliverable:initial_screen:'+company_ref.rsplit(':',1)[-1])
        if latest:
            _body(latest)
            from datetime import datetime
            ticket=_read(Path(state)/'initial-screens'/record['source_ticket_ref'].split(':')[1]/'ticket.json')
            if (latest.get('subject_ref')!=company_ref or latest.get('kind')!='initial_screen'):
                raise ValueError('initial-screen recheck completion identity differs')
            if latest.get('mission_version_ref')==mission['id'] and datetime.fromisoformat(latest['created_at'])>=datetime.fromisoformat(ticket['completed_at']):
                return {'status':'cleared_by_published_product','items':[]}
        reconstructed=qualify(state=state,binding=record['binding'],previous=record['previous'],mission=mission,model=model,authority=authority)
        from .initial_screen_content_maintenance import read_feedback as maintenance_feedback
        maintained=maintenance_feedback(authority,state=state,mission=mission,model=model,original=reconstructed)
        if maintained is not None:return maintained
        current=reconstructed['record']
        if current!=record:raise ValueError('initial-screen current diagnostic proof differs')
        if not current['findings']:return {'status':'deterministic_pass_verifier_pending','items':[]}
        from .initial_screen_failure_feedback import _compact_findings
        compact,omitted=_compact_findings(current['findings'])
        return {'status':'verified','items':[{'contract_ref':CONTRACT,'company_ref':company_ref,
            'source_ticket_ref':record['source_ticket_ref'],'receipt_hash':record['content_hash'],
            'semantic_hash':content_hash({'input':record['input_hash'],'validator':record['validator_fingerprint'],'findings':record['findings']}),
            'stage':record['stage'],'pack_hash':record['pack_hash'],
            'findings':compact,'full_findings_hash':content_hash(record['findings']),
            'omitted':len(omitted),'omitted_hash':content_hash(omitted),'financial_evidence':False,'retry_authority':False}]}
    except PROOF_ERRORS as exc:
        return {'status':'unverified','items':[],'reason':'initial_screen_validator_recheck_unproved','error_type':type(exc).__name__}
