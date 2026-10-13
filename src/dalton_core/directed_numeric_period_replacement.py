"""One normal-plan replacement of an impossible frozen numeric period.

No original target, result, failure or fee is rewritten. The planner must
supply the same question and source, with only formerly unspecified periods
made explicit in the original excerpts. This proof is not paid-retry authority.
"""
from __future__ import annotations
import copy, re
from collections.abc import Mapping
from datetime import date
from .store import content_hash
from .directed_numeric_research import normalize_targets, unexecutable_period_targets

CONTRACT_REF = 'directed-numeric-source-period-replacement:0.1'
BINDING_FIELDS = frozenset({'admission_ref','admission_hash','failure_ref','failure_hash'})
PROOF_FIELDS = BINDING_FIELDS | {'contract_ref','work_order_ref','work_order_hash',
    'old_targets_hash','new_targets_hash','source_quotes_hash','failure'}
MAX_EXCERPTS = 64
MAX_EXCERPT_BYTES = 1_000_000
_MONTHS = ('January','February','March','April','May','June','July','August',
           'September','October','November','December')


def _original(connection, admission_ref, *, anchor=None):
    from .mission_document_research import MissionDocumentResearchAuthority
    from .document_execution_failure import read_execution_failure
    from .scheduler import Scheduler
    import json
    row=connection.execute('SELECT * FROM mission_document_research_admissions WHERE admission_id=?',(admission_ref,)).fetchone()
    if row is None:raise ValueError('numeric period replacement original unavailable')
    original=MissionDocumentResearchAuthority._read_row(None,row)
    failure=read_execution_failure(connection,admission_ref) if anchor is None else dict(anchor)
    if failure is None or failure['execution_state']!='execution_failed':return None
    old=original.get('numeric_authority')
    if not old or not unexecutable_period_targets(old['targets']):return None
    if old.get('period_replacement_proof') is not None:return None
    scheduler=object.__new__(Scheduler);scheduler.connection=connection
    saved=scheduler.work_order_authority(failure['failed_work_order_ref'])
    if saved is None:raise ValueError('numeric period replacement failed Work missing')
    work=saved['work_order'];meta=work['metadata']
    if anchor is not None:
        from .mission_document_research_executor import _terminal_model_failure, _formal_hash, read_mission_document_research_observations
        formal=_terminal_model_failure(scheduler,work)
        observations=read_mission_document_research_observations(connection,admission_ref=admission_ref)
        matching=[v for v in observations if v['id']==failure['observation_ref']
            and v['content_hash']==failure['observation_hash'] and v['work_order_ref']==work['id']]
        if (content_hash({k:v for k,v in failure.items() if k!='content_hash'})!=failure['content_hash']
                or len(matching)!=1 or formal is None
                or failure['admission_ref']!=original['id'] or failure['admission_hash']!=original['content_hash']
                or failure['company_ref']!=original['company_ref'] or failure['mission_version_ref']!=original['mission_version_ref']
                or failure['document_ref']!=original['request']['registration']['document_ref']
                or failure['document_version_hash']!=original['request']['registration']['content_hash']
                or failure['question']!=original['planner_inquiry']['question']
                or failure['formal_result_ref']!=formal.get('id',formal.get('result_record_id'))
                or failure['formal_result_hash']!=_formal_hash(formal)
                or failure['result_envelope_ref']!=formal['result_envelope_id']
                or failure['result_envelope_hash']!=formal['result_envelope_hash']):
            raise ValueError('numeric period replacement immutable failure anchor differs')
        from .document_execution_failure import failure_from_observations
        if failure_from_observations(connection,original,matching)!=failure:
            raise ValueError('numeric period replacement failure reconstruction differs')
    if (saved['work_order_hash']!=failure['failed_work_order_hash']
            or meta.get('stage')!='qualitative_model_draft'
            or meta.get('mission_document_research_admission_ref')!=original['id']
            or meta.get('mission_document_research_admission_hash')!=original['content_hash']
            or meta.get('numeric_contract_ref')!='directed-document-numeric:0.1'
            or meta.get('numeric_targets')!=old['targets']
            or meta.get('numeric_metric_definitions')!=old['metric_definitions']
            or content_hash(work['question'])!=meta.get('prompt_hash')):
        return None
    from .directed_numeric_recovery import _source
    _source(work)  # unchanged target/catalog/excerpt contents in the paid prompt
    from .directed_numeric_output import quotes
    held=quotes(meta['retrieval_proof'])
    if not held or len(held)>MAX_EXCERPTS or sum(len(v.encode()) for v in held.values())>MAX_EXCERPT_BYTES:
        raise ValueError('numeric period replacement excerpt limit exceeded')
    return original,failure,saved,held


def binding_for(connection, admission_ref):
    held=_original(connection,admission_ref)
    if held is None:return None
    original,failure,_,_=held
    return {'admission_ref':original['id'],'admission_hash':original['content_hash'],
            'failure_ref':failure['id'],'failure_hash':failure['content_hash']}


def _source_period(period, excerpts):
    # No latest/current interpretation and no arithmetic. A date is accepted
    # only in explicit ISO or original English/Chinese calendar spelling.
    m=re.fullmatch(r'(?:截至\s*|as of\s*)?(\d{4})-(\d{2})-(\d{2})',period,re.I)
    if m:
        try:d=date(*map(int,m.groups()))
        except ValueError:return False
        forms=(d.isoformat(),f'{_MONTHS[d.month-1]} {d.day}, {d.year}',
               f'{d.year}年{d.month}月{d.day}日')
        return any(s.casefold() in text.casefold() for s in forms for text in excerpts.values())
    m=re.fullmatch(r'(?:FY\s*)?(\d{4})(?:\s*全年|\s*年)?',period,re.I)
    if m:return any(re.search(r'(?<!\d)'+m[1]+r'(?!\d)',text) for text in excerpts.values())
    # Quarter/half labels need the literal period, not a year appearing in an
    # unrelated footnote. More source spellings can be added explicitly later.
    return any(period.casefold() in text.casefold() for text in excerpts.values())


def derive(authority, mission_ref, inquiry, *, frozen=None):
    strategy=inquiry.get('directed_document') or {}
    binding=strategy.get('replaces_numeric_contract_failure')
    if binding is None:return None
    if not isinstance(binding,Mapping) or set(binding)!=BINDING_FIELDS:
        raise ValueError('numeric period replacement binding differs')
    if frozen is not None and (not isinstance(frozen,Mapping) or set(frozen)!=PROOF_FIELDS):
        raise ValueError('numeric period replacement frozen proof shape differs')
    held=_original(authority.connection,binding['admission_ref'],anchor=None if frozen is None else frozen['failure'])
    if held is None:raise ValueError('numeric period replacement lacks terminal impossible target')
    original,failure,saved,excerpts=held
    expected_binding={'admission_ref':original['id'],'admission_hash':original['content_hash'],
                      'failure_ref':failure['id'],'failure_hash':failure['content_hash']}
    if binding!=expected_binding:
        raise ValueError('numeric period replacement failure proof differs')
    # Existing source/mission ancestry and permissions are resolved by the
    # normal authority, never inferred from failure timestamps or same company.
    authority.resolve_for_execution(original['id'])
    from .mission_document_research_executor import (
        _blueprints, _historical_derived_work, _recovery_rows,
        _read_recovery_link, _recovery_work)
    from .scheduler import Scheduler
    scheduler=object.__new__(Scheduler);scheduler.connection=authority.connection
    base=_historical_derived_work(original,scheduler,authority.registry,_blueprints(original),1)
    links=[]
    for row in _recovery_rows(authority.connection,original,1):
        if base['id']==saved['work_order']['id']:break
        link=_read_recovery_link(authority.connection,original,1,row,base,links)
        base=_recovery_work(base,link);links.append(link)
    if base!=saved['work_order']:
        raise ValueError('numeric period replacement original full Work derivation differs')
    old_strategy=original['planner_inquiry']['directed_document']
    reg=original['request']['registration']
    if (original['mission_ref']!=mission_ref or original['company_ref']!=inquiry['company_ref']
            or inquiry['question']!=original['planner_inquiry']['question']
            or inquiry['wants']!=original['planner_inquiry']['wants']
            or strategy['strategy_version']!=old_strategy['strategy_version']
            or strategy['document_ref']!=reg['document_ref']
            or strategy['document_version_hash']!=reg['content_hash']):
        raise ValueError('numeric period replacement question/source authority differs')
    old=normalize_targets(original['numeric_authority']['targets']);new=normalize_targets(strategy['numeric_targets'])
    _verify_period_changes(old,new,excerpts)
    proof={**binding,'contract_ref':CONTRACT_REF,'work_order_ref':saved['work_order']['id'],
            'work_order_hash':saved['work_order_hash'],'old_targets_hash':content_hash(old),
            'new_targets_hash':content_hash(new),'source_quotes_hash':content_hash(excerpts),'failure':failure}
    if frozen is not None and proof!=dict(frozen):raise ValueError('numeric period replacement frozen proof differs')
    return proof


def consumer(connection, proof):
    rows=connection.execute("SELECT * FROM mission_document_research_admissions WHERE "
        "company_ref=(SELECT company_ref FROM mission_document_research_admissions WHERE admission_id=?) AND "
        "json_extract(record_json,'$.numeric_authority.period_replacement_proof.admission_ref')=? "
        "ORDER BY created_at,admission_id LIMIT 2",(proof['admission_ref'],proof['admission_ref'])).fetchall()
    if len(rows)>1:raise ValueError('numeric period replacement consumed more than once')
    if not rows:return None
    from .mission_document_research import MissionDocumentResearchAuthority
    record=MissionDocumentResearchAuthority._read_row(None,rows[0])
    held=record['numeric_authority']['period_replacement_proof']
    if (set(held)!=PROOF_FIELDS or held['contract_ref']!=CONTRACT_REF
            or any(held.get(k)!=proof[k] for k in BINDING_FIELDS)
            or held['new_targets_hash']!=content_hash(record['numeric_authority']['targets'])
            or any(held[k]!=proof[k] for k in ('work_order_ref','work_order_hash','old_targets_hash','source_quotes_hash') if k in proof)):
        raise ValueError('numeric period replacement consumer proof differs')
    return record['id']


def superseding_admission(connection, original):
    """Operations view: an admitted replacement owns this pending question.

    This neither declares its new answer complete nor grants it execution.
    Current source/mission/budget permission is still resolved by its caller.
    """
    numeric=original.get('numeric_authority')
    if not numeric or not unexecutable_period_targets(numeric['targets']):return None
    row=connection.execute("SELECT * FROM mission_document_research_admissions WHERE company_ref=? "
        "AND json_extract(record_json,'$.numeric_authority.period_replacement_proof.admission_ref')=? "
        "ORDER BY created_at,admission_id LIMIT 1",(original['company_ref'],original['id'])).fetchone()
    if row is None:return None
    from .mission_document_research import MissionDocumentResearchAuthority
    replacement=MissionDocumentResearchAuthority._read_row(None,row)
    proof=replacement['numeric_authority']['period_replacement_proof']
    used=consumer(connection,proof)
    held=_original(connection,original['id'],anchor=proof['failure'])
    if held is None:raise ValueError('numeric period replacement original no longer matches')
    parent,failure,saved,excerpts=held
    binding={'admission_ref':parent['id'],'admission_hash':parent['content_hash'],
        'failure_ref':failure['id'],'failure_hash':failure['content_hash']}
    if (parent!=original or any(proof[k]!=binding[k] for k in BINDING_FIELDS)
            or proof['work_order_ref']!=saved['work_order']['id']
            or proof['work_order_hash']!=saved['work_order_hash']
            or proof['old_targets_hash']!=content_hash(numeric['targets'])
            or proof['source_quotes_hash']!=content_hash(excerpts)
            or replacement['mission_ref']!=original['mission_ref']
            or replacement['request']['registration']!=original['request']['registration']
            or any(replacement['planner_inquiry'][k]!=original['planner_inquiry'][k] for k in ('question','wants'))):
        raise ValueError('numeric period replacement operations binding differs')
    _verify_period_changes(numeric['targets'],replacement['numeric_authority']['targets'],excerpts)
    return used


def _verify_period_changes(old,new,excerpts):
    old=normalize_targets(old);new=normalize_targets(new)
    if len(old)!=len(new):raise ValueError('numeric period replacement loses required targets')
    missing={v['target_ref'] for v in unexecutable_period_targets(old)}
    for a,b in zip(old,new):
        expected=copy.deepcopy(a)
        if a['target_ref'] in missing:
            if b['period']==a['period'] or not _source_period(b['period'],excerpts):
                raise ValueError('numeric replacement period not explicit in original excerpts')
            expected['period']=b['period']
        if expected!=b:raise ValueError('numeric period replacement changed another target field')
    if unexecutable_period_targets(new):raise ValueError('numeric replacement period remains unexecutable')


def historical_feedback(core, mission):
    """Bounded old failed-period hints on the verified current mission ancestry."""
    from .document_mission_lineage import document_mission_versions
    from .mission_document_research_withdrawal import read_withdrawals
    versions=document_mission_versions(core.connection,mission)
    if len(versions)>64:raise ValueError('numeric period replacement mission bound exceeded')
    old=[v for v in versions if v!=mission['id']]
    if not old:return []
    companies={v['company_ref'] for v in mission['universe']}
    marks=','.join('?' for _ in old)
    rows=core.connection.execute('SELECT o.admission_ref,MAX(o.created_at) AS last_at '
        'FROM mission_document_research_observations o JOIN mission_document_research_admissions a '
        'ON a.admission_id=o.admission_ref WHERE o.mission_version_ref IN ('+marks+
        ") AND o.outcome='recovery_required' AND json_extract(a.record_json,'$.numeric_authority') IS NOT NULL "
        'GROUP BY o.admission_ref ORDER BY last_at DESC,o.admission_ref LIMIT 48',tuple(old)).fetchall()
    withdrawn=read_withdrawals(core.connection);result=[]
    for row in rows:
        if row[0] in withdrawn:continue
        held=_original(core.connection,row[0])
        if held is None:continue
        original,failure,_,_=held
        if original['mission_ref']!=mission['mission_ref'] or original['company_ref'] not in companies:continue
        binding={'admission_ref':original['id'],'admission_hash':original['content_hash'],
                 'failure_ref':failure['id'],'failure_hash':failure['content_hash']}
        if consumer(core.connection,binding) is not None:continue
        result.append({**failure,'governing_mission_version_ref':mission['id'],
                       'replaces_numeric_contract_failure':binding})
    return result
