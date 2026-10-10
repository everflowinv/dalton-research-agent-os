"""Evidence-bound research work, explicit owners, and immutable close decisions."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from .store import authorization_flag, authorized_flag, canonical_json, content_hash

RULE_REF = "research-followup:14-day-review:1"
TERMINAL = frozenset({"completed", "no_revision_needed", "declined", "withdrawn"})
KINDS = frozenset({"model_redraft", "framework_repair", "research_reflection", "historical_period_review", "checkpoint_review"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_followup_requests (
 request_id TEXT PRIMARY KEY, request_ref TEXT NOT NULL, company_ref TEXT NOT NULL,
 evidence_hash TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(request_ref,evidence_hash)
);
CREATE TABLE IF NOT EXISTS research_followup_decisions (
 decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, action TEXT NOT NULL,
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS research_followup_one_terminal
ON research_followup_decisions(request_id)
WHERE action IN ('completed','no_revision_needed','declined','withdrawn');
CREATE UNIQUE INDEX IF NOT EXISTS research_followup_decision_idempotency
ON research_followup_decisions(request_id,json_extract(record_json,'$.idempotency_key'))
WHERE json_extract(record_json,'$.idempotency_key') IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS research_followup_request_guard BEFORE INSERT ON research_followup_requests
WHEN dalton_followup_authorized()=0 BEGIN SELECT RAISE(ABORT,'research followup requires authority'); END;
CREATE TRIGGER IF NOT EXISTS research_followup_decision_guard BEFORE INSERT ON research_followup_decisions
WHEN dalton_followup_authorized()=0 BEGIN SELECT RAISE(ABORT,'research followup requires authority'); END;
CREATE TRIGGER IF NOT EXISTS research_followup_request_no_update BEFORE UPDATE ON research_followup_requests
BEGIN SELECT RAISE(ABORT,'research followups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_followup_request_no_delete BEFORE DELETE ON research_followup_requests
BEGIN SELECT RAISE(ABORT,'research followups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_followup_decision_no_update BEFORE UPDATE ON research_followup_decisions
BEGIN SELECT RAISE(ABORT,'research followup decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_followup_decision_no_delete BEFORE DELETE ON research_followup_decisions
BEGIN SELECT RAISE(ABORT,'research followup decisions are immutable'); END;
"""

def _exists(c: Any, table: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

def _verified(row: Any) -> dict[str, Any]:
    wire=json.loads(row["record_json"])
    if row["content_hash"] != wire.get("content_hash") or content_hash({k:v for k,v in wire.items() if k!='content_hash'}) != wire["content_hash"]:
        raise ValueError("research followup source hash drifted")
    return wire

def owner_binding(c: Any, company_ref: str) -> dict[str, Any] | None:
    if not _exists(c,"coverage_mission_pointer"): return None
    from .coverage_mission import CoverageMissionAuthority
    reader=object.__new__(CoverageMissionAuthority);reader.connection=c
    matches=[]
    for row in c.execute('SELECT mission_version_id FROM coverage_mission_pointer'):
        mission=reader._mission_record(row[0])
        if (company_ref in {mission['mission_ref'], mission['industry_ref']} or company_ref in {member['company_ref'] for member in mission['universe']}) and str(mission.get('actor_ref')).startswith('human:'):
            matches.append({'owner_ref':mission['actor_ref'],'mission_version_ref':mission['id'],'mission_hash':mission['content_hash']})
    return matches[0] if len(matches)==1 else None

def resolve_evidence(c: Any, evidence: Mapping[str, Any]) -> dict[str, Any]:
    if set(evidence)!={'ref','hash','kind'}: raise ValueError("followup evidence requires exact kind/ref/hash")
    ref=evidence['ref'];kind=evidence['kind']
    if kind=='model_cell_reconciliation':
        from .model_cell_reconciliation import read_existing
        row=c.execute('SELECT company_ref FROM forecast_model_cell_reconciliations WHERE reconciliation_id=?',(ref,)).fetchone()
        if not row: raise ValueError('followup reconciliation is missing')
        record=next(record for record in read_existing(c,row[0]) if record['id']==ref)
    else:
        tables={'forecast_model':('forecast_model_versions','version_id'), 'event_judgement':('event_judgements','judgement_id'),
                'reflection':('research_cycle_reflection_versions','version_id'), 'dossier':('company_dossier_versions','version_id'),
                'framework':('industry_framework_versions','version_id'), 'thesis_candidate':('thesis_revision_candidates','candidate_id'),
                'zero_base_candidate':('zero_base_revision_candidates','candidate_id'), 'forecast_proposal':('forecast_revision_proposals','proposal_id')}
        if kind not in tables: raise ValueError('unsupported followup evidence authority')
        table,key=tables[kind]
        if not _exists(c,table): raise ValueError('followup source authority is missing')
        row=c.execute(f'SELECT record_json,content_hash FROM {table} WHERE {key}=?',(ref,)).fetchone()
        if not row: raise ValueError('followup source version is missing')
        record=_verified(row)
    if record['content_hash']!=evidence['hash']: raise ValueError('followup evidence version hash differs')
    return record

def requests(c: Any, *, company_ref: str | None=None, today: str | None=None, as_of: str | None=None) -> list[dict[str, Any]]:
    """A read-only projection; overdue is never manufactured as a close decision."""
    if not _exists(c,'research_followup_requests'): return []
    today=today or datetime.now(timezone.utc).date().isoformat()
    out=[]
    for row in c.execute('SELECT * FROM research_followup_requests ORDER BY created_at,request_id'):
        r=_verified(row)
        if as_of and r['created_at'] > as_of: continue
        if company_ref and r['company_ref']!=company_ref: continue
        sources=[resolve_evidence(c,evidence) for evidence in r['evidence']]
        bound=r.get('owner_binding')
        if bound:
            from .coverage_mission import CoverageMissionAuthority
            reader=object.__new__(CoverageMissionAuthority);reader.connection=c
            mission=reader._mission_record(bound['mission_version_ref'])
            if mission['content_hash'] != bound['mission_hash'] or mission['actor_ref'] != bound['owner_ref']:
                raise ValueError('followup owner authority differs')
        projection=r['scope'].get('reading_projection')
        if projection:
            from .historical_research_context import reading_projection
            source=next((s for s in sources if s.get('id')==projection['source_version_ref']),None)
            if source is None or reading_projection(source,latest_disclosure=projection.get('latest_disclosure')) != projection:
                raise ValueError('historical reading projection differs from original paragraph evidence')
            disclosed=projection.get('latest_disclosure')
            if disclosed:
                from .company_model_annual_projection import verify_statement_filing
                filing=c.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?',(disclosed['ingest_id'],)).fetchone()
                if filing is None or filing['content_hash'] != disclosed['content_hash']:
                    raise ValueError('historical comparison calendar authority differs')
                verify_statement_filing(c,dict(filing),allow_legacy_metadata=True)
                from .statement_context import resolve_line_refs,context_record
                for identity in disclosed.get('current_disclosed_identities') or []:
                    context_ref=identity['context_version_ref']
                    if context_ref and context_record(c,context_ref)['content_hash'] != identity['context_hash']:
                        raise ValueError('historical reading current identity context differs')
                    bindings={disclosed['ingest_id']:context_ref} if context_ref else {}
                    line=resolve_line_refs(c,[identity['line_id']],bindings)[0]
                    if line.get('dimensions') != identity['dimensions']:
                        raise ValueError('historical reading dimension identity differs')
        decisions=[_verified(d) for d in c.execute('SELECT * FROM research_followup_decisions WHERE request_id=? ORDER BY created_at,decision_id',(r['id'],)) if not as_of or d['created_at'] <= as_of]
        owner=r.get('owner_ref');due=r['due_on'];terminal=None
        for d in decisions:
            if d['request_ref'] != r['id'] or d['request_hash'] != r['content_hash']:
                raise ValueError('followup decision binds different request bytes')
            if terminal: raise ValueError('followup decision exists after a terminal decision')
            if d['action']=='assign':owner=d['owner_ref'];due=d['due_on']
            elif d['action'] in TERMINAL:
                for evidence in d['closure_evidence']:resolve_evidence(c,evidence)
                terminal=d
        state=terminal['action'] if terminal else 'open' if owner else 'unassigned'
        escalation_on=min(due,r['due_on'])
        overdue=terminal is None and today>=escalation_on
        escalation_ref='followup-escalation:'+content_hash({'request':r['id'],'due_on':escalation_on})[:32] if overdue else None
        out.append({**r,'state':state,'owner_ref':owner,'due_on':due,'overdue':overdue,'escalation_ref':escalation_ref,
                    'terminal_decision':terminal,'decisions':decisions,'escalation_on':escalation_on,'missing_reason':None if owner else 'responsible_owner_not_bound'})
    return out

class ResearchFollowupAuthority:
    _authorized=authorized_flag()
    def __init__(self, store: Any, *, clock=None):
        self.store,self.connection=store,store.connection
        self.clock=clock or (lambda:datetime.now(timezone.utc).isoformat(timespec='microseconds'))
        self._authorization_flag=authorization_flag(self.connection,'dalton_followup_authorized')
        self.connection.executescript(_SCHEMA)

    def record(self, *, kind: str, company_ref: str, scope: Mapping[str,Any], reason: str,
               evidence: Sequence[Mapping[str,Any]], owner_ref: str | None=None) -> dict[str,Any]:
        if kind not in KINDS or not str(reason).strip() or not evidence: raise ValueError('followup needs a declared kind, reason and held evidence')
        if owner_ref is not None and not owner_ref.startswith('human:'): raise ValueError('followup owner must be a human principal')
        sources=[resolve_evidence(self.connection,item) for item in evidence]
        for source in sources:
            if source.get('company_ref') and source['company_ref'] != company_ref:
                raise ValueError('followup evidence belongs to a different company')
        binding=owner_binding(self.connection,company_ref)
        if owner_ref is None: owner_ref=(binding or {}).get('owner_ref')
        ref='research-followup:'+content_hash({'kind':kind,'company_ref':company_ref,'scope':scope})[:32]
        evidence_hash=content_hash(sorted(evidence,key=lambda x:x['ref']))
        seen=self.connection.execute('SELECT * FROM research_followup_requests WHERE request_ref=? AND evidence_hash=?',(ref,evidence_hash)).fetchone()
        if seen:return _verified(seen)
        now=self.clock()
        origin=min([str(source.get('created_at') or now)[:10] for source in sources]+[now[:10]])
        body={'rule_ref':RULE_REF,'request_ref':ref,'kind':kind,'company_ref':company_ref,'scope':dict(scope),
                              'reason':reason.strip(),'evidence':list(evidence),'evidence_hash':evidence_hash,'owner_ref':owner_ref,
                              'owner_binding':binding,'due_on':(date.fromisoformat(origin)+timedelta(days=14)).isoformat(),
                              'created_at':now,'human_checkpoint':True,'automatically_changes_forecast':False}
        body['id']='research-followup-request:'+content_hash(body)[:32];body['content_hash']=content_hash(body)
        self._authorized=True
        try:
            with self.store._transaction() as cur:
                cur.execute('INSERT INTO research_followup_requests VALUES(?,?,?,?,?,?,?)',(body['id'],ref,company_ref,evidence_hash,canonical_json(body),body['content_hash'],now))
        finally:self._authorized=False
        return body

    def capture_reconciliations(self, records: Sequence[Mapping[str,Any]]) -> list[dict[str,Any]]:
        output=[]
        for r in records:
            if r.get('status')=='compared' and r.get('redraft_required'):
                output.append(self.record(kind='model_redraft',company_ref=r['company_ref'],scope={'model_version_ref':r['model_version_ref'],'cell_ref':r['cell_ref'],'period':r['period']},
                    reason='实际数与该冻结预测单元的偏差超过重大阈值；需要复核假设与方法，形成预测草案或明确无需修订的决定。',
                    evidence=[{'kind':'model_cell_reconciliation','ref':r['id'],'hash':r['content_hash']}]))
        return output

    def capture_existing(self, source_refs: Sequence[str] | None=None) -> list[dict[str,Any]]:
        """Register held reflection, blocked framework and checkpoint work without changing them."""
        output=[]
        tables=[('reflection','research_cycle_reflection_versions','version_id'),
                ('dossier','company_dossier_versions','version_id'),('framework','industry_framework_versions','version_id'),
                ('thesis_candidate','thesis_revision_candidates','candidate_id'),('zero_base_candidate','zero_base_revision_candidates','candidate_id'),
                ('forecast_proposal','forecast_revision_proposals','proposal_id')]
        for source_kind,table,key in tables:
            if not _exists(self.connection,table): continue
            for row in self.connection.execute(f'SELECT {key},record_json,content_hash FROM {table}'):
                source=_verified(row);ref=row[key]
                if source_refs is not None and ref not in source_refs: continue
                if source_refs is None and source_kind in {'dossier','framework','reflection'}:
                    scope_field={'dossier':'company_ref','framework':'industry_ref','reflection':'reflection_ref'}[source_kind]
                    latest=self.connection.execute(f'SELECT {key} FROM {table} WHERE {scope_field}=? ORDER BY version_number DESC LIMIT 1',
                                                   (source[scope_field],)).fetchone()
                    if latest[0] != ref: continue
                evidence=[{'kind':source_kind,'ref':ref,'hash':source['content_hash']}]
                company=source.get('company_ref') or source.get('mission_ref') or source.get('industry_ref')
                if not company: continue
                if source_kind=='reflection':
                    for index,candidate in enumerate(source.get('backlog_candidates') or []):
                        output.append(self.record(kind='research_reflection',company_ref=company,
                            scope={'reflection_ref':ref,'candidate_index':index,'candidate':candidate},
                            reason=candidate.get('because') or candidate.get('question') or '本周反思需明确处理决定。',evidence=evidence))
                elif source_kind in {'dossier','framework'}:
                    from .historical_research_context import latest_disclosure, reading_projection
                    context=latest_disclosure(self.connection,company) if source.get('company_ref') else None
                    projection=reading_projection(source,latest_disclosure=context)
                    if any(p['state'] in {'historical_comparison','period_unknown'} for p in projection['paragraphs']):
                        output.append(self.record(kind='historical_period_review',company_ref=company,
                            scope={'source_version_ref':ref,'reading_projection':projection},
                            reason='逐段历史定量披露已标记；需按最新已持有披露身份独立重写当前判断。期间未知的旧数字不得当作当前事实。',evidence=evidence))
                    text=canonical_json(source)
                    if 'causal_chain_unmapped' in text:
                        output.append(self.record(kind='framework_repair',company_ref=company,
                            scope={'source_version_ref':ref,'defect':'causal_chain_unmapped'},
                            reason='该持有研究产物缺少驱动到因果链的明确映射；需独立补全映射并重写受影响段落。',evidence=evidence))
                else:
                    if source_kind in {'thesis_candidate','zero_base_candidate'} and _exists(self.connection,'thesis_revision_decisions'):
                        closed=self.connection.execute('SELECT 1 FROM thesis_revision_decisions WHERE candidate_ref=? AND terminal=1',(ref,)).fetchone()
                        if closed: continue
                    output.append(self.record(kind='checkpoint_review',company_ref=company,
                        scope={'checkpoint_kind':source_kind,'checkpoint_ref':ref},
                        reason='持有的人工作决定尚需处理；十四天未关闭时升级为逾期待办。',evidence=evidence))
        return output

    def decide(self, request_id: str, *, action: str, actor_ref: str, reason: str,
               closure_evidence: Sequence[Mapping[str,Any]]=(), owner_ref: str | None=None, due_on: str | None=None,
               idempotency_key: str | None=None) -> dict[str,Any]:
        if not actor_ref.startswith('human:') or not str(reason).strip(): raise ValueError('followup decision requires a human and an explicit reason')
        current=next((r for r in requests(self.connection) if r['id']==request_id),None)
        if not current:raise ValueError('followup request is missing')
        if idempotency_key:
            seen=next((d for d in current['decisions'] if d.get('idempotency_key')==idempotency_key),None)
            if seen:
                expected={'action':action,'actor_ref':actor_ref,'reason':reason.strip(),'closure_evidence':list(closure_evidence),'owner_ref':owner_ref,'due_on':due_on}
                if any(seen.get(k)!=v for k,v in expected.items()):raise ValueError('followup retry changes the exact decision')
                return seen
        if current['state'] in TERMINAL:raise ValueError('followup already has its one terminal decision')
        if action not in TERMINAL|{'assign'}:raise ValueError('followup action is not declared')
        if action=='assign':
            if not owner_ref or not owner_ref.startswith('human:') or not due_on:raise ValueError('assignment requires owner and deadline')
            date.fromisoformat(due_on)
            if due_on<self.clock()[:10]:raise ValueError('assignment deadline cannot be backdated')
        elif action in {'completed','no_revision_needed'} and not closure_evidence:
            raise ValueError('completion/no-revision requires held close evidence')
        closed_sources=[resolve_evidence(self.connection,item) for item in closure_evidence]
        if any(source.get('company_ref') and source['company_ref'] != current['company_ref'] for source in closed_sources):
            raise ValueError('close evidence belongs to a different company')
        if action=='completed' and not any(item not in current['evidence'] for item in closure_evidence):
            raise ValueError('completed work requires new held result evidence')
        now=self.clock();body={'request_ref':request_id,'request_hash':current['content_hash'],'action':action,'actor_ref':actor_ref,
                              'reason':reason.strip(),'closure_evidence':list(closure_evidence),'owner_ref':owner_ref,'due_on':due_on,'created_at':now}
        if idempotency_key:body['idempotency_key']=idempotency_key
        body['id']='research-followup-decision:'+content_hash(body)[:32];body['content_hash']=content_hash(body)
        self._authorized=True
        try:
            with self.store._transaction() as cur:
                terminal=cur.execute("SELECT 1 FROM research_followup_decisions WHERE request_id=? AND action IN ('completed','no_revision_needed','declined','withdrawn')",(request_id,)).fetchone()
                if terminal:raise ValueError('followup already has its one terminal decision')
                cur.execute('INSERT INTO research_followup_decisions VALUES(?,?,?,?,?,?)',(body['id'],request_id,action,canonical_json(body),body['content_hash'],now))
        finally:self._authorized=False
        return body


def approval_items(c: Any, label, *, today: str | None=None) -> list[dict[str,Any]]:
    titles={'model_redraft':'实际结果偏离预测：重新检查模型','framework_repair':'补充研究框架中的量化因果关系',
            'research_reflection':'处理本周研究反思','historical_period_review':'复核历史披露段落','checkpoint_review':'处理长期等待的研究决定'}
    from .research_followup_automation import automatic_request_ids, system_cards
    automatic = automatic_request_ids(c)
    output=system_cards(c,label)
    for request in requests(c,today=today):
        if request["id"] in automatic: continue
        if request['state'] in TERMINAL: continue
        choices=[]
        for kind,table,key in [('forecast_model','forecast_model_versions','version_id'),('dossier','company_dossier_versions','version_id'),('event_judgement','event_judgements','judgement_id')]:
            if not _exists(c,table):continue
            for row in c.execute(f'SELECT {key},record_json,content_hash FROM {table} ORDER BY created_at DESC LIMIT 100'):
                source=_verified(row)
                if source.get('company_ref') != request['company_ref']: continue
                choices.append({'kind':kind,'ref':row[key],'hash':source['content_hash'],
                                'label':{'forecast_model':'预测模型','dossier':'公司研究档案','event_judgement':'独立事件研判'}[kind]+' · '+str(source.get('created_at',''))[:10]})
        # Explicit original review evidence permits an explained no-revision decision,
        # but the authority never treats it as a newly completed result.
        for evidence in request['evidence']:
            choices.append({**evidence,'label':'该事项的原始证据（仅可支持无需修订的明确决定）'})
        output.append({'kind':'research_followup','ref':request['id'],'hash':request['content_hash'],'at':request['created_at'],
                       'title':titles[request['kind']],'who':label(request['company_ref']),'summary':request['reason'],
                       'display_reason':'已超过十四天，请明确处理或说明阻碍。' if request['overdue'] else request['missing_reason'],
                       'details':{'责任人':'已指定负责人（姓名未提供）' if request['owner_ref'] else '尚未分配','处理期限':request['due_on']},
                       'owner_ref':request['owner_ref'],
                       'detail_labels':{'责任人':'责任人','处理期限':'处理期限'},'followup_fields':True,
                       'close_evidence_choices':choices,'due_on':request['due_on'],'needs_rationale':True,
                       'historical_paragraphs':(request['scope'].get('reading_projection') or {}).get('paragraphs') or [],
                       'rationale_hint':'必填：处理理由、结果或仍需补充的证据。完成或无需修订时须选择已持有的结果证据。',
                       'actions':[{'decision':'assign','label':'由我负责'}, {'decision':'completed','label':'已完成并附结果'},
                                  {'decision':'no_revision_needed','label':'审查后无需修订'}, {'decision':'declined','label':'不开展并说明原因'},
                                  {'decision':'withdrawn','label':'撤回该事项'}],
                       'next_step':'只登记责任和关闭决定；不会自动修改模型、论点或研究授权。'})
    return output


def open_redraft_requests(c: Any, company_ref: str) -> list[dict[str,Any]]:
    """Exact unresolved comparison requests; no timestamp-only consumption inference."""
    return [r for r in requests(c,company_ref=company_ref) if r['kind']=='model_redraft' and r['state'] not in TERMINAL]


def brief_entries(c: Any, company_refs: Sequence[str], *, as_of: str) -> list[dict[str,Any]] | None:
    if not _exists(c,'research_followup_requests'): return None
    output=[]
    for request in requests(c,today=as_of[:10],as_of=as_of):
        bound=request.get('owner_binding')
        applies=request['company_ref'] in company_refs
        if bound and not applies:
            from .coverage_mission import CoverageMissionAuthority
            reader=object.__new__(CoverageMissionAuthority);reader.connection=c
            mission=reader._mission_record(bound['mission_version_ref'])
            if mission['content_hash'] != bound['mission_hash']:raise ValueError('followup owner binding hash differs')
            applies=bool(set(company_refs)&{m['company_ref'] for m in mission['universe']})
        if applies:output.append(request)
    return output


def replay_brief_entries(c: Any, entries: Sequence[Mapping[str,Any]], company_refs: Sequence[str], *, as_of: str) -> None:
    if brief_entries(c,company_refs,as_of=as_of) != list(entries):
        raise ValueError('weekly research followups differ from their frozen authority')
