"""Source requests from a reconstructed not-ready statement replay.

No form/date/title is a financial period, and no missing actual is invented.
"""
from datetime import date
from .store import content_hash
from .company_model_series import QUARTER_MIN_DAYS, QUARTER_MAX_DAYS

RULE_REF = 'forecast-statement-history-gaps:0.1'


def _missing(structure, table, wanted):
    indexed = {l['concept']: l for l in table.get('filed_lines') or []}
    selected=[]
    histories={}
    all_ends=set()
    starts={}
    for line in structure['lines']:
        if line['kind']!='filed' or line['period_kind']!='duration':continue
        if line['statement']=='cash' and line.get('annual_semantics')=='direct_annual':continue
        selected.append(line)
        quarter_ends=[]
        for end,cell in (indexed.get(line['concept'],{}).get('cells') or {}).items():
            start=cell.get('period_start')
            if start and QUARTER_MIN_DAYS <= (date.fromisoformat(end)-date.fromisoformat(start)).days+1 <= QUARTER_MAX_DAYS:
                quarter_ends.append(end)
                starts.setdefault(end,set()).add(start)
        histories[line['ref']]=set(quarter_ends)
        all_ends.update(quarter_ends)
    # The newest actually evidenced source periods are fixed before checking
    # each operand. Never fill a new-period hole by taking older quarters.
    targets=sorted(all_ends)[-wanted:]
    coherent = all(len(starts[end])==1 for end in targets)
    if coherent:
        coherent = all((date.fromisoformat(next(iter(starts[current])))-date.fromisoformat(previous)).days==1
                       for previous,current in zip(targets,targets[1:]))
    missing = []
    for line in selected:
        quarter_ends=histories[line['ref']] & set(targets)
        if len(targets)<wanted or quarter_ends != set(targets) or not coherent:
            missing.append({'line_ref': line['ref'], 'role': line['role'],
                'statement': line['statement'], 'concept': line['concept'],
                'unit': line['unit'], 'required_quarters': wanted,
                'target_quarter_ends':targets,
                'quarter_periods_coherent':coherent,
                'held_quarter_ends': sorted(quarter_ends)})
    return missing


def plans(spec, table, failure, *, filings):
    from .company_financial_statement_structure import (
        FinancialStatementStructureNotReady, financial_input_authority,
        replay_historical_structure,
    )
    if not isinstance(failure, FinancialStatementStructureNotReady):
        return []
    structure, replay = failure.structure, failure.replay
    if (structure.get('company_ref') != spec.get('company_ref')
            or structure.get('company_ref') != table.get('company_ref')
            or structure.get('spec_ref') != spec.get('spec_id')
            or structure.get('spec_hash') != spec.get('content_hash')
            or structure.get('financial_input_hash') != financial_input_authority(table)['content_hash']
            or structure.get('content_hash') != content_hash({k:v for k,v in structure.items() if k!='content_hash'})
            or replay != replay_historical_structure(structure, table)
            or replay.get('ready_for_forecast') is not False):
        raise ValueError('statement history gap source/replay binding differs')
    wanted = spec.get('horizon', {}).get('historical_quarters')
    if type(wanted) is not int or not 1 <= wanted <= 24:
        return []  # No invented horizon, financial window or acquisition scope.
    missing = _missing(structure, table, wanted)
    if not missing:
        return []  # A non-history method/DAG failure needs a different diagnosis.
    sources = []
    for filing in filings:
        if filing.get('company_ref') != spec['company_ref']:
            raise ValueError('statement history gap filing issuer differs')
        if not all(isinstance(filing.get(k), str) and filing[k] for k in ('ingest_id', 'accession', 'form', 'content_hash')):
            raise ValueError('statement history gap filing authority is incomplete')
        sources.append({k: filing[k] for k in ('ingest_id', 'accession', 'form', 'content_hash')})
    foreign = any(f['form'] in {'20-F','20-F/A','40-F','40-F/A'} for f in sources)
    binding = {'contract_ref': RULE_REF, 'company_ref': spec['company_ref'],
        'spec_ref': spec['spec_id'], 'spec_hash': spec['content_hash'],
        'financial_input_hash': structure['financial_input_hash'],
        'structure_hash': structure['content_hash'], 'replay_hash': content_hash(replay),
        'source_filings': sorted(sources, key=lambda f:f['ingest_id']), 'missing_history': missing}
    return [{'key': 'statement_quarter_history', 'period': None,
        'need': {'kind':'document', 'doc_kinds':['sec_6k'] if foreign else ['sec_10q','sec_10k'],
            'data_item': None,
            'question': ('Obtain the original interim financial statement attachments with the declared historical quarter coverage. '
                'Prove issuer, actual statement periods, consolidated income/balance/cash identities and currency/scaling from contents. '
                'Preserve form and exact source cells; a 6-K cover or SEC reportDate does not prove a quarter. '
                'Do not split annual amounts into quarters; use directly reported quarterly weighted-average shares/EPS, never cumulative subtraction. '
                'Semiannual or year-to-date disclosures remain those periods; report any unavailable quarters explicitly. '
                'Exact history binding: '+content_hash(binding))},
        'source_priority':['indexed_corpus','sec_edgar','web'],
        'acceptance': {'min_passages':1, 'about_company':True,
            'model_gap_rule_ref':RULE_REF, 'statement_history_gap':binding,
            'financial_admission':'Original issuer/period/unit/full statement identity and complete history must pass the normal ledger and financial validators. Material acquisition alone does not admit numerical facts.'},
        'reason':'Strong statement replay lacks the complete coherent quarter history declared by the specification.'}]


def requirement_status(state_dir, request):
    """A cover or acquired attachment cannot close a numerical-history need.

    Read current formal statements only, under a bounded read-only snapshot.
    No constructors, extraction/provider calls, ledger creation or writes.
    """
    import time
    from pathlib import Path
    from types import SimpleNamespace
    waiting = {'met':False, 'partial':False, 'items':[],
               'reason':'季度资料需正常核验入正式三表账本；封面或相关文档不满足历史数字需求'}
    b=(request.get('acceptance') or {}).get('statement_history_gap')
    fields={'contract_ref','company_ref','spec_ref','spec_hash','financial_input_hash',
            'structure_hash','replay_hash','source_filings','missing_history'}
    if (not isinstance(b,dict) or set(b)!=fields or b.get('contract_ref')!=RULE_REF
            or b.get('company_ref')!=request.get('subject')
            or not b.get('missing_history')):
        return {**waiting,'reason':'季度历史请求绑定不完整，不能据文档关闭'}
    path=Path(state_dir)/'core.sqlite'
    if not path.is_file():return waiting
    from .planner_paid_output_recovery import _authority_connection
    from .coverage_mission import CoverageMissionAuthority
    from .company_model_inputs import build_model_inputs
    from .company_financial_statement_structure import materialize_financial_statement_structure
    started=time.monotonic()
    try:
        with _authority_connection(path) as connection:
            connection.execute('BEGIN')
            connection.set_progress_handler(lambda:int(time.monotonic()-started>8),1000)
            authority=CoverageMissionAuthority.__new__(CoverageMissionAuthority)
            authority.connection=connection
            authority.store=SimpleNamespace(path=str(path),connection=connection)
            spec=authority.latest_company_model_spec(b['company_ref'])
            if spec is None or spec.get('spec_id')!=b['spec_ref'] or spec.get('content_hash')!=b['spec_hash']:
                return {**waiting,'reason':'当前模型规格已改变；旧季度资料请求不由另一规格替代关闭'}
            wanted=spec.get('horizon',{}).get('historical_quarters')
            if type(wanted) is not int or not 1<=wanted<=24:
                return waiting
            table=build_model_inputs(authority,spec)
            structure,replay=materialize_financial_statement_structure(spec,table)
            # Validate all current filed history, never just a possibly smaller
            # requested subset, and do not count annual/half-year cells.
            missing=_missing(structure,table,wanted)
            assert connection.total_changes==0
            if missing:
                return {**waiting,'reason':f'正式三表仍有 {len(missing)} 条原行缺少所声明的季度历史',
                        'history_input_hash':structure['financial_input_hash']}
            return {'met':True,'partial':False,'items':[{'kind':'verified_statement_history',
                    'spec_ref':spec['spec_id'],'financial_input_hash':structure['financial_input_hash'],
                    'replay_hash':content_hash(replay)}],
                'reason':'所需季度历史已在当前正式三表中通过单位/期间/来源与完整结构重放；这不等于预测或文章已完成'}
    except Exception as exc:  # unavailable proofs remain unknown, not satisfied
        return {**waiting,'reason':'季度历史强核验暂不可用：'+type(exc).__name__}
