"""A proved numeric admission supersedes its ancestor operation, not research.

Read-only operations feedback. No retry, source permission, budget, fee or
completion authority is created by this relationship.
"""
from .store import canonical_json,content_hash

CONTRACT_REF='document-numeric-capability-supersession:0.1'
MAX_CANDIDATES=32
BINDING_FIELDS=('admission_ref','admission_hash','rejection_ref','rejection_hash')


def _admission(connection,ref):
    from .mission_document_research import MissionDocumentResearchAuthority,_exact_plan
    from .research_task import inquiry_content_hash,inquiry_ref_for
    row=connection.execute('SELECT * FROM mission_document_research_admissions WHERE admission_id=?',(ref,)).fetchone()
    if row is None:raise ValueError('numeric supersession admission is unavailable')
    wire=MissionDocumentResearchAuthority._read_row(None,row)
    if canonical_json(wire)!=row['record_json']:
        raise ValueError('numeric supersession admission bytes differ')
    plan,_=_exact_plan(connection,wire['plan_ref'])
    ordinal=wire['inquiry_ordinal']
    if (plan['content_hash']!=wire['plan_hash'] or plan['mission_version_ref']!=wire['mission_version_ref']
            or type(ordinal)is not int or not 0<=ordinal<len(plan['inquiries'])
            or plan['inquiries'][ordinal]!=wire['planner_inquiry']
            or inquiry_content_hash(wire['planner_inquiry'])!=wire['inquiry_hash']
            or inquiry_ref_for(wire['inquiry_hash'])!=wire['inquiry_ref']):
        raise ValueError('numeric supersession saved plan/inquiry differs')
    return wire


def _withdrawn(connection,ref):
    # Any withdrawal row prevents suppressing an operation. This is a deny-only
    # test, not adopting the row as withdrawal authority or changing its state.
    try:return connection.execute('SELECT 1 FROM mission_document_research_withdrawals WHERE admission_ref=? LIMIT 1',(ref,)).fetchone() is not None
    except Exception as exc:
        if 'no such table' in str(exc).lower():return False
        raise


def successor_for(connection,rejection,*,mission):
    from .directed_numeric_research import numeric_rejection,normalize_targets
    from .directed_numeric_source_capabilities import NUMERIC_STRATEGIES
    from .document_research import validate_registration
    from .directed_numeric_executor import source_binding
    original=_admission(connection,rejection['admission_ref'])
    actual=numeric_rejection(connection,original['id'])
    if actual!=rejection:raise ValueError('numeric supersession rejection differs')
    if (original['mission_version_ref']!=mission['id']
            or original['company_ref'] not in {c['company_ref']for c in mission['universe']}
            or _withdrawn(connection,original['id'])):
        return None
    selected={k:rejection[k]for k in BINDING_FIELDS}
    rows=connection.execute('SELECT * FROM mission_document_research_admissions '
        'WHERE company_ref=? AND mission_version_ref=? '
        "AND json_extract(record_json,'$.planner_inquiry.directed_document.replaces_numeric_rejection.admission_ref')=? "
        'ORDER BY created_at,admission_id LIMIT ?',
        (original['company_ref'],original['mission_version_ref'],original['id'],MAX_CANDIDATES+1)).fetchall()
    if len(rows)>MAX_CANDIDATES:raise ValueError('numeric supersession candidate window exceeds bound')
    old=original['planner_inquiry'];old_source=validate_registration(original['request']['registration'])
    for row in rows:
        successor=_admission(connection,row['admission_id']);new=successor['planner_inquiry'];strategy=new.get('directed_document')or{}
        numeric=successor.get('numeric_authority')or{}
        if (_withdrawn(connection,successor['id']) or successor['id']==original['id']
                or strategy.get('strategy_version') not in NUMERIC_STRATEGIES
                or numeric.get('contract_ref')!='directed-document-numeric:0.1'
                or strategy.get('replaces_numeric_rejection')!=selected
                or rejection not in numeric.get('capability_upgrade_proofs',())
                or successor['mission_ref']!=original['mission_ref']
                or successor['company_ref']!=original['company_ref']
                or successor['source_ref']!=original['source_ref']
                or any(new.get(k)!=old.get(k)for k in ('company_ref','question'))
                or canonical_json(validate_registration(successor['request']['registration']))!=canonical_json(old_source)
                or any(strategy.get(k)!=(old.get('directed_document')or{}).get(k)
                       for k in ('document_ref','document_version_hash'))):
            continue
        targets=normalize_targets(strategy.get('numeric_targets'))
        source=source_binding(connection,old_source,original['company_ref'],strategy_version=strategy['strategy_version'])
        if not targets or targets!=numeric.get('targets') or source!=numeric.get('source_binding'):
            raise ValueError('numeric supersession admitted target identity differs')
        body={'contract_ref':CONTRACT_REF,'original_admission_ref':original['id'],
            'original_admission_hash':original['content_hash'],'rejection_binding':selected,
            'successor_admission_ref':successor['id'],'successor_admission_hash':successor['content_hash'],
            'company_ref':original['company_ref'],'document_ref':old_source['document_ref'],
            'document_version_hash':old_source['content_hash'],'numeric_targets_hash':content_hash(targets),
            'source_binding_hash':content_hash(source),
            'original_question_hash':content_hash(old['question']),
            'original_answer_criteria_hash':content_hash(old['wants']),
            'successor_answer_criteria_hash':content_hash(new['wants']),
            'research_status':'pending','financial_evidence':False,
            'meaning':'The original qualitative capability rejection has been taken up by this exact admitted numeric read. The research answer remains unresolved until independent execution succeeds; all original fees are unchanged.'}
        return {**body,'content_hash':content_hash(body)}
    return None
