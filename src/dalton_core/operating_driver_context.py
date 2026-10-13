"""Read-only source/paid/canonical replay of reported operating quantities.

No paragraph becomes a value here. Every value is taken from a completed
directed numeric result after its original Work, independent verification,
fees, exact Figure and canonical Claim have replayed through the executor's
ordinary reader. Nothing is inserted or repaired by this context.
"""
from __future__ import annotations

import copy
import json
from contextlib import AbstractContextManager, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from .store import canonical_json, content_hash
from .operating_driver_units import CONTRACT_REF

CONTEXT_REF = 'company-model-operating-driver-context:0.1'
MAX_RECORDS = 80
CURRENT_DURATION_WINDOWS = 8


def validate_context(value: Mapping[str, Any], *, company_ref: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != {
            'contract_ref','company_ref','records','content_hash'}
            or value['contract_ref'] != CONTEXT_REF or value['company_ref'] != company_ref
            or content_hash({k:v for k,v in value.items() if k != 'content_hash'}) != value['content_hash']
            or not isinstance(value['records'],list) or not 1 <= len(value['records']) <= MAX_RECORDS):
        raise ValueError('operating driver context has no exact company/hash/record bound')
    refs = set()
    fields = {'ref','metric_ref','metric_definition','metric_definition_hash',
        'period_start','period_end','value','unit','currency','scale','basis',
        'admission_ref','admission_hash','numeric_result_hash','target_ref',
        'figure_ref','figure_hash','claim_version_ref','claim_version_hash',
        'evidence_version_ref','evidence_version_hash','review_decision_ref',
        'registration_ref','registration_hash','source_manifest_hash','source_quote_hash',
        'content_hash'}
    from .operating_driver_units import monetary
    from .directed_numeric_research import normalize_targets
    import re
    from datetime import date
    for record in value['records']:
        if (not isinstance(record,dict) or set(record) != fields
                or record['ref'] in refs or not record['ref'].startswith('operating-driver-evidence:')
                or record['content_hash'] != content_hash({k:v for k,v in record.items() if k != 'content_hash'})
                or record['period_start'] > record['period_end']):
            raise ValueError('operating driver evidence record shape, duplicate or hash differs')
        refs.add(record['ref'])
        for key,field in record.items():
            if key.endswith('_hash') and (not isinstance(field,str) or re.fullmatch('[0-9a-f]{64}',field) is None):
                raise ValueError('operating evidence contains an invalid proof hash')
            if key.endswith('_ref') and (not isinstance(field,str) or not field.strip() or len(field)>240):
                raise ValueError('operating evidence contains an invalid authority ref')
        date.fromisoformat(record['period_start']);date.fromisoformat(record['period_end'])
        normalize_targets([{'target_ref':record['target_ref'], 'metric_ref':record['metric_ref'],
            'metric_definition':record['metric_definition'],'metric_definition_hash':record['metric_definition_hash'],
            'period':record['period_start']+'/'+record['period_end'], 'unit':record['unit'],
            'currency':record['currency'],'basis':record['basis']}], contract_ref=CONTRACT_REF)
        if record['basis'] not in {'management-reported','gaap-reported'}:
            raise ValueError('operating model history requires a reported company actual, not a projection')
        if monetary(record['unit']) != (record['currency'] is not None):
            raise ValueError('operating driver context currency differs from its explicit unit')
    return copy.deepcopy(dict(value))


def project_result(*, executor: Any, admission: Mapping[str, Any], works: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Full canonical reader, never a bare SQL Claim/number lookup."""
    from .directed_numeric_executor import read_numeric_result
    from .claim_index_figures import MissionFigureAuthorityResolver
    if admission.get('numeric_authority',{}).get('contract_ref') != CONTRACT_REF:
        raise ValueError('operating context requires its explicitly admitted numeric contract')
    formal = executor.scheduler.formal_result(works[3]['id'])
    checked = read_numeric_result(executor,admission,works,formal)
    receipt = checked['numeric_result']
    if receipt['status'] != 'completed' or receipt['pending_targets']:
        return []
    claims = {row['target_ref']:row for row in receipt['canonical_claims']}
    completed = {row['target_ref']:row for row in receipt['completed_targets']}
    targets = admission['numeric_authority']['targets']
    registration = admission['request']['registration']
    origin = admission['planner_inquiry']['directed_document']
    result = []
    resolver = MissionFigureAuthorityResolver(executor.connection)
    for target in targets:
        period = target['period'].split('/')
        if len(period) != 2:
            raise ValueError('operating numeric period must retain exact start/end dates')
        item = completed[target['target_ref']]
        figure = resolver.figure(item['figure_ref'])
        claim = claims[target['target_ref']]
        from .contracts import AdjudicatedStatus
        status = executor.authority.store._claim_status(executor.connection.cursor(),claim['claim_version_ref'])
        if status not in {'proposed',AdjudicatedStatus.CORROBORATED.value}:
            raise ValueError('operating numeric Claim is no longer current and uncontested')
        body = {'ref':'operating-driver-evidence:' + content_hash({
                    'admission_ref':admission['id'],'target_ref':target['target_ref']})[:32],
            'metric_ref':target['metric_ref'],'metric_definition':target['metric_definition'],
            'metric_definition_hash':target['metric_definition_hash'],
            'period_start':period[0],'period_end':period[1],
            **{k:figure[k] for k in ('value','unit','currency','scale','basis')},
            'admission_ref':admission['id'],'admission_hash':admission['content_hash'],
            'numeric_result_hash':receipt['content_hash'],'target_ref':target['target_ref'],
            'figure_ref':figure['figure_id'],'figure_hash':figure['content_hash'],
            **{k:claim[k] for k in ('claim_version_ref','claim_version_hash',
                'evidence_version_ref','evidence_version_hash','review_decision_ref')},
            'registration_ref':registration['id'],'registration_hash':registration['content_hash'],
            'source_manifest_hash':registration['manifest_hash'],
            'source_quote_hash':content_hash(figure['citation_text'])}
        result.append({**body,'content_hash':content_hash(body)})
    return result


class OperatingDriverReadContext(AbstractContextManager):
    """Lazy supporting readers; no model/runtime/authority constructors."""
    def __init__(self, *, store: Any, state_dir: str | Path, model_scheduler_db: str | Path | None = None):
        self.store = store
        self.state_dir = Path(state_dir)
        self.model_scheduler_db = Path(model_scheduler_db) if model_scheduler_db is not None else self.state_dir/'scheduler.sqlite'
        self._resources = ExitStack()

    def _refs(self, company_ref, bindings=None):
        connection = self.store.connection
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mission_document_numeric_results'").fetchone() is None:
            return []
        if bindings is not None:
            if not isinstance(bindings,list) or not 1<=len(bindings)<=MAX_RECORDS:
                raise ValueError('operating historical read has no bounded exact evidence set')
            refs=sorted({b['admission_ref'] for b in bindings})
            for ref in refs:
                row=connection.execute("SELECT company_ref,record_json FROM mission_document_research_admissions WHERE admission_id=?",(ref,)).fetchone()
                if row is None or row['company_ref']!=company_ref or json.loads(row['record_json'])['numeric_authority']['contract_ref']!=CONTRACT_REF:
                    raise ValueError('operating historical evidence admission is missing or foreign')
            return refs
        rows = connection.execute(
            "WITH periods AS (SELECT DISTINCT json_extract(a.record_json,'$.numeric_authority.targets[0].period') AS period "
            "FROM mission_document_research_admissions a JOIN mission_document_numeric_results n ON n.admission_ref=a.admission_id "
            "WHERE a.company_ref=? AND json_extract(a.record_json,'$.numeric_authority.contract_ref')=? "
            "AND json_extract(n.record_json,'$.status')='completed' ORDER BY period DESC LIMIT ?) "
            "SELECT a.admission_id FROM mission_document_research_admissions a JOIN mission_document_numeric_results n ON n.admission_ref=a.admission_id "
            "WHERE a.company_ref=? AND json_extract(a.record_json,'$.numeric_authority.contract_ref')=? "
            "AND json_extract(n.record_json,'$.status')='completed' "
            "AND json_extract(a.record_json,'$.numeric_authority.targets[0].period') IN (SELECT period FROM periods) "
            "ORDER BY a.admission_id LIMIT ?",
            (company_ref,CONTRACT_REF,CURRENT_DURATION_WINDOWS,company_ref,CONTRACT_REF,MAX_RECORDS+1)).fetchall()
        if len(rows) > MAX_RECORDS:
            raise ValueError('operating driver context exceeds the bounded recorded history')
        return [row[0] for row in rows]

    def projection(self, company_ref: str, *, evidence_bindings=None) -> dict[str, Any] | None:
        refs = self._refs(company_ref,evidence_bindings)
        if not refs:
            return None
        records = []
        for ref in refs:
            executor, admission = self._reader(ref)
            from .mission_document_research_executor import effective_mission_document_work_orders
            works = effective_mission_document_work_orders(executor.authority,executor.scheduler,ref,
                draft_worker=executor.draft_worker,verifier_worker=executor.verifier_worker)
            if len(works) != 4:
                raise ValueError('operating driver result lacks its complete formal Work prefix')
            records.extend(project_result(executor=executor,admission=admission,works=works))
        if not records:
            return None
        body = {'contract_ref':CONTEXT_REF,'company_ref':company_ref,
                'records':sorted(records,key=lambda r:r['ref'])}
        return validate_context({**body,'content_hash':content_hash(body)},company_ref=company_ref)

    def _reader(self, admission_ref):
        from .financial_note_context import _read_config
        from .coverage_mission import CoverageMissionAuthority
        from .mission_document_research import MissionDocumentResearchAuthority
        from .mission_document_research_executor import MissionDocumentResearchExecutor
        from .mission_document_research_runtime import ACTOR_REF
        from .scheduler import Scheduler
        from .model_router import ModelRouter
        from .observability import ObservabilityStore
        from .thesis_impact_budget import ThesisImpactBudgetStore
        from .readonly_sqlite import connect_read_only
        from .annual_report_runtime import load_annual_report_model_config
        from .operating_driver_source_plan import replay_plan
        import sqlite3
        connection = self.store.connection
        config,config_sha = _read_config(self.state_dir/'document-research-config.json')
        if hasattr(self,'_source_config_sha') and self._source_config_sha != config_sha:
            raise ValueError('operating document configuration changed during strong replay')
        if not hasattr(self,'_registry'):
            from .connector_authority_port import ReadOnlyConnectorReceiptReader
            from .public_web_fetch_launcher import ReadOnlyPublicWebFetchManifestReader
            from .raw_spool import RawSpoolReader
            from .document_research import build_document_research_registry
            # FILED source_binding below remains authoritative. A public page
            # is not automatically filed, and unrelated feed/wiki adapters are
            # not prerequisites for replaying an already admitted original.
            refs=[r for r in config['enabled_sources'] if r in {'source:sec-edgar','source:public-web'}]
            if 'source:web-search' in config['enabled_sources'] and 'source:public-web' not in refs:
                refs.append('source:public-web')
            if not refs:
                raise ValueError('operating original has no enabled fetched-source reader')
            self._registry=build_document_research_registry(core=self.store,state_dir=self.state_dir,
                spool=RawSpoolReader(config['spool_dir']),receipt_reader=ReadOnlyConnectorReceiptReader(connection),
                policy=config['policy'],feed_launchers={},alphaengine_launcher=None,
                public_web_launcher=ReadOnlyPublicWebFetchManifestReader(state_dir=self.state_dir),
                public_web_source_refs=refs,source_reading_limits=config['source_reading_limits'])
            self._source_config_sha=config_sha
            self._router=connect_read_only(self.state_dir/'model-router.sqlite')
            self._resources.callback(self._router.close);self._router.row_factory=sqlite3.Row
        scheduler = object.__new__(Scheduler); scheduler.connection = connection
        scheduler.clock = lambda: datetime.now(timezone.utc)
        router = object.__new__(ModelRouter);router.connection = self._router
        observability = object.__new__(ObservabilityStore)
        observability.store = self.store;observability.connection = connection
        authority = object.__new__(MissionDocumentResearchAuthority)
        authority.store = self.store;authority.connection = connection;authority.registry = self._registry
        authority.clock = scheduler.clock
        missions = object.__new__(CoverageMissionAuthority);missions.store=self.store;missions.connection=connection
        authority._missions = missions
        if not hasattr(self,'_parent_scheduler'):
            self._parent_scheduler = connect_read_only(self.model_scheduler_db)
            self._resources.callback(self._parent_scheduler.close);self._parent_scheduler.row_factory = sqlite3.Row
        parent_scheduler=self._parent_scheduler
        authority.planner_scheduler_connection = parent_scheduler;authority.planner_router_connection = self._router
        def registration(ref):
            rows = connection.execute('SELECT record_json FROM mission_document_research_admissions WHERE document_authority_ref=?', (ref,)).fetchall()
            values = {canonical_json(json.loads(row[0])['request']['registration']) for row in rows}
            if len(values) != 1:raise ValueError('operating source registration is absent or ambiguous')
            return json.loads(values.pop())
        authority.registration_resolver = registration
        wire = authority.admission(admission_ref)
        # Historical execution authority is already immutable. This reader
        # exposes no submit/enqueue method and cannot grant a new model call.
        authority.model_execution_resolver = lambda:(wire['model_execution'],wire['model_authority'])
        config = load_annual_report_model_config(self.state_dir/'initial-screen-model-config.json','model source original configuration')
        authority.model_source_plan_resolver = lambda plan,mission: replay_plan(authority,plan,mission,
            state_dir=self.state_dir,model_config=config)
        admission = authority.resolve_for_execution(admission_ref)
        if not hasattr(self,'_budgets'):
            self._budgets = {}
        path=admission['model_execution']['draft']['budget_db']
        if path not in self._budgets:
            self._budgets[path]=self._resources.enter_context(ThesisImpactBudgetStore(path,read_only=True))
        budget=self._budgets[path]
        common = dict(store=self.store,scheduler=scheduler,router=router,observability=observability,
                      budget_store=budget,mission_document_research_authority=authority,
                      worker_ref='worker:registered-annual-report-model')
        executor = object.__new__(MissionDocumentResearchExecutor)
        executor.authority=authority;executor.scheduler=scheduler;executor.registry=authority.registry
        executor.connection=connection;executor.actor_ref=ACTOR_REF
        executor.draft_worker=SimpleNamespace(**common);executor.verifier_worker=SimpleNamespace(**common)
        return executor,admission

    def __exit__(self,*exc):
        self._resources.close()


def metric_history(context, *, company_ref, metric_ref, expected_hash=None):
    """One explicit reported series; no cross-period value or unit guessing."""
    from decimal import Decimal, localcontext
    from datetime import date
    checked = validate_context(context, company_ref=company_ref)
    if expected_hash is not None and checked['content_hash'] != expected_hash:
        raise ValueError('operating driver context changed since the specification chose its source')
    records = [r for r in checked['records'] if r['metric_ref'] == metric_ref]
    if not records:
        raise ValueError('operating driver has no exact independently verified metric source')
    definitions = {(r['metric_definition_hash'],r['unit'],r['currency'],r['basis']) for r in records}
    if len(definitions) != 1:
        raise ValueError('operating driver combines incompatible reported definitions or units')
    windows = set()
    scales = {None:Decimal(1),'thousand':Decimal(1000),'million':Decimal(1000000),
              'billion':Decimal(1000000000),'trillion':Decimal(1000000000000)}
    history = []
    for r in records:
        start,end = date.fromisoformat(r['period_start']),date.fromisoformat(r['period_end'])
        if start > end or (start,end) in windows or r['scale'] not in scales:
            raise ValueError('operating driver history is ambiguous or has an unknown amount scale')
        windows.add((start,end))
        amount = Decimal(r['value'])
        if not amount.is_finite() or len(r['value']) > 160 or abs(amount.adjusted()) > 1000:
            raise ValueError('operating amount exceeds the bounded finite numeric contract')
        with localcontext() as arithmetic:
            arithmetic.prec = 200
            value = amount * scales[r['scale']]
        history.append({'concept':metric_ref, 'period_start':r['period_start'],
            'period_end':r['period_end'],'value':format(value,'f'),'basis':r['basis'],
            'accessions':[r['claim_version_ref']]})
    ordered = sorted(history,key=lambda r:(r['period_start'],r['period_end']))
    if any(a['period_end'] >= b['period_start'] for a,b in zip(ordered,ordered[1:])):
        raise ValueError('operating history mixes overlapping annual and quarterly quantities')
    unit,currency = records[0]['unit'],records[0]['currency']
    unit = (currency.lower() if unit == 'currency' else
            currency.lower()+unit.removeprefix('currency') if unit.startswith('currency_per_') else unit)
    return {'metric_ref':metric_ref,'metric_definition':records[0]['metric_definition'],
        'context_hash':checked['content_hash'],'unit':unit,'history':ordered,
        'evidence_refs':[{'ref':r['ref'],'hash':r['content_hash']} for r in records],
        'context_evidence_refs':[{'ref':r['ref'],'hash':r['content_hash'],'admission_ref':r['admission_ref']} for r in checked['records']]}


def selected_context(context, bindings, expected_hash):
    """Rebuild a prior exact context from fresh strong records, not new arrivals."""
    checked=validate_context(context,company_ref=context['company_ref'])
    if not isinstance(bindings,list) or not bindings or len(bindings)>MAX_RECORDS:
        raise ValueError('operating source context has no bounded original record set')
    index={r['ref']:r for r in checked['records']}
    if len({b.get('ref') for b in bindings})!=len(bindings):
        raise ValueError('operating source context contains duplicated original records')
    records=[]
    for binding in bindings:
        if not isinstance(binding,dict) or set(binding)!={'ref','hash','admission_ref'}:
            raise ValueError('operating original context binding shape differs')
        record=index.get(binding['ref'])
        if record is None or record['content_hash']!=binding['hash'] or record['admission_ref']!=binding['admission_ref']:
            raise ValueError('operating original context record is unavailable or changed')
        records.append(record)
    body={**{k:v for k,v in checked.items() if k not in {'records','content_hash'}},
          'records':sorted(records,key=lambda r:r['ref'])}
    result={**body,'content_hash':content_hash(body)}
    if result['content_hash']!=expected_hash:
        raise ValueError('operating source original context cannot be reconstructed exactly')
    return result
