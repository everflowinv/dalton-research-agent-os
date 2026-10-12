"""Conservative import of an old gate that never reached downstream calls.

This is deliberately not a generic guesser for old anonymous repair requests.
An invalid successful root or any linked verifier makes the proof unavailable.
"""
from __future__ import annotations

from .store import content_hash
from .deep_insight_gate_accounting import works,billed_work

CONTRACT_REF='deep-insight-gate-normal-only-legacy-import:0.1'
MAX_VERIFIERS=128


def derive(connection,router,budget,*,entries,group_inputs,wanted,through_rowid,original_policy=None):
    from .deep_insight_gate_draft import parse_group_output,GateDraftRefused,group_contract
    from .draft_contract_repair import violations_of
    from .scheduler import Scheduler
    from .contracts import WorkOrder
    from .deep_insight_gate import GROUPS
    if set(group_inputs)!=set(GROUPS) or not wanted or not set(wanted)<=set(GROUPS):
        raise ValueError('legacy gate needs all four current consumer groups and a fixed wanted set')
    records=[];barriers=[];routes=set();seen=set()
    for entry in entries:
        matches=works(connection,entry,through_rowid=through_rowid)
        outcomes=[]
        for work,authority,formal in matches:
            if work.id in seen:raise ValueError('legacy gate normal identity occurs in two groups')
            seen.add(work.id)
            bill=billed_work(connection,router,budget,authority,formal)
            if work.metadata.get('timeout_recovery') is not None:
                original=work.metadata['timeout_recovery']['proof']
                parent=Scheduler.__new__(Scheduler);parent.connection=connection
                root=parent.work_order_authority(original['parent_work_ref'])
                if (root['work_order_hash']!=original['parent_work_hash']
                        or root['work_order']['question']!=work.question
                        or not str(root['work_order']['metadata']['request_id']).startswith(entry['request_id'])):
                    raise ValueError('legacy gate technical step has another original input')
                from .cockpit_timeout_recovery import verify,CONSUMERS
                certificate=work.metadata['timeout_recovery']
                verify(connection,router,budget,certificate,mission={
                    'id':certificate['proof']['mission_version_ref'],
                    'content_hash':certificate['proof']['mission_version_hash']})
                consumed=connection.execute('SELECT * FROM '+CONSUMERS+' WHERE authorization_id=?',(certificate['id'],)).fetchone()
                if consumed is None or consumed['work_ref']!=work.id or consumed['work_hash']!=authority['work_order_hash']:
                    raise ValueError('legacy gate technical step lacks its unique original consumer binding')
            if formal['terminal_state']=='succeeded':
                raw=formal['result_envelope']['outputs'].get('text')
                fields=group_inputs[entry['group']]
                # Both checks are mandatory. No new semantic allowances or
                # best-effort coercion are used to claim an old shape slot empty.
                violations=violations_of(raw,group_contract(entry['group'],material=fields['material']))
                if violations:raise ValueError('legacy successful root may have used an anonymous shape repair')
                try:parse_group_output(raw,group=entry['group'],questions=fields['questions'],material=fields['material'])
                except GateDraftRefused as exc:
                    raise ValueError('legacy successful root requires an unproved prior shape correction') from exc
                routes.add(formal['result_envelope']['metadata']['route_decision_ref'])
                outcomes.append('succeeded')
            else:
                outcomes.append('failed')
            records.append({**bill,'request_id':entry['request_id'],'group':entry['group']})
        if entry['group'] in wanted and outcomes and set(outcomes)=={'failed'}:
            barriers.append(entry['group'])
    if original_policy is not None:
        from .shared_call_budget_policy import validate_shared_call_budget_policy,effective_shared_limits
        from datetime import datetime
        original_policy=validate_shared_call_budget_policy(original_policy)
        run=effective_shared_limits(original_policy,'deep_insight_gate',kind='run')
        if run.get('max_units')!=4 or not records or any(datetime.fromisoformat(f['admitted_at'])<datetime.fromisoformat(original_policy['updated_at']) for r in records for f in r['fees']):
            raise ValueError('legacy gate original four-group human budget authority is unproved')
    if not barriers:
        raise ValueError('legacy gate has no permanently failed wanted group; downstream fees need complete import')
    # A real verifier link is an independent counterexample to the inferred
    # early-return boundary. Do not label a mixed/older execution normal-only.
    reader=Scheduler.__new__(Scheduler);reader.connection=connection
    rows=connection.execute('SELECT work_order_id FROM scheduler_work_orders WHERE '
        "work_order_id>='work:cockpit-deep_insight_gate_verifier-' AND "
        "work_order_id<'work:cockpit-deep_insight_gate_verifier.' AND rowid<=? LIMIT ?",
        (through_rowid,MAX_VERIFIERS+1)).fetchall()
    if len(rows)>MAX_VERIFIERS:
        raise ValueError('legacy gate verifier history exceeds bounded128 proof')
    for row in rows:
        authority=reader.work_order_authority(row['work_order_id'])
        work=WorkOrder.from_dict(authority['work_order'])
        if routes.intersection(work.metadata.get('producer_route_decision_refs')or()):
            raise ValueError('legacy gate has a linked verifier; anonymous downstream history cannot be omitted')
    proof={'contract_ref':CONTRACT_REF,'through_rowid':through_rowid,'normal_requests':entries,
        'consumer_hash':content_hash(group_inputs),'wanted':list(wanted),'early_return_groups':barriers,
        'historical_works':records,'verifier_census_refs':[r['work_order_id'] for r in rows],
        'accounted_micros':sum(r['accounted_micros'] for r in records),
        'original_policy':original_policy}
    return {**proof,'content_hash':content_hash(proof)}
