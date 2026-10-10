"""Append-only system handling of evidence-bound followups.

Original requests and human decisions stay immutable. This authority neither
raises budgets nor calls a model: normal lanes own dispatch, admission and proof.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from .store import authorization_flag, authorized_flag, canonical_json, content_hash
from .research_followup import _exists, _verified, requests, resolve_evidence, TERMINAL

RULE = 'research-followup-automation:1'
from pathlib import Path
_SCHEMA = Path(__file__).with_name('research_followup_automation_schema.sql').read_text(encoding='utf-8')


def _model_scope(request):
    scope = request['scope']
    if not scope.get('cell_ref') or not isinstance(scope.get('period'), dict):
        return None
    sources = request['evidence']
    if not sources or any(s['kind'] != 'model_cell_reconciliation' for s in sources):
        return None
    return {'company_ref': request['company_ref'], 'cell_ref': scope['cell_ref'], 'period': scope['period']}


def governing_mission(connection, request):
    """Current grant of the same verified project, not the old reflection grant."""
    if not _exists(connection,'coverage_mission_pointer'):return None
    from .coverage_mission import CoverageMissionAuthority
    reader=object.__new__(CoverageMissionAuthority);reader.connection=connection
    original=request.get('owner_binding')
    if not original:return None
    held=reader._mission_record(original['mission_version_ref'])
    if held['content_hash']!=original['mission_hash']:raise ValueError('followup original mission hash differs')
    pointer=connection.execute('SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?',(held['mission_ref'],)).fetchone()
    if pointer is None:return None
    current=reader._mission_record(pointer[0]);cursor=current;seen=set()
    while cursor['id'] not in seen:
        seen.add(cursor['id'])
        if cursor['mission_ref']!=held['mission_ref'] or cursor['industry_ref']!=held['industry_ref'] or cursor['autonomy']['automation_principal']!=held['autonomy']['automation_principal']:
            return None
        if cursor['id']==held['id']:return current
        if cursor['prior_version_ref'] is None:return None
        cursor=reader._mission_record(cursor['prior_version_ref'])
    raise ValueError('followup governing mission lineage contains a cycle')


def reflection_route(connection, request):
    """Identify old and new candidates from typed metrics, never prose keywords."""
    if request['kind'] != 'research_reflection': return None
    source = next((resolve_evidence(connection, e) for e in request['evidence'] if e['kind'] == 'reflection'), None)
    if source is None or not source.get('metrics') or not source.get('window'): return 'needs_review'
    from .research_cycle_reflection import backlog_candidates
    try:
        candidates = backlog_candidates(source['metrics'], source['window'])
        index = request['scope']['candidate_index']
        original = request['scope']['candidate']
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or candidates[index] != original or (source.get('backlog_candidates') or [])[index] != original:
            return 'needs_review'
        m = source['metrics']; routes = []
        spend, backlog, planner = m['spend'], m['backlog'], m['planner']
        if spend.get('available') and spend['total_cost_usd'] > 0 and backlog.get('available') and backlog['new_questions'] == 0: routes.append('administrator_diagnosis')
        if planner.get('available') and planner['inquiries_raised'] > 0 and planner['dispatched'] == 0:
            current=governing_mission(connection,request)
            if current is not None:
                routes.append('research_dispatch' if {'research_task','model_run','stage_record'}.issubset(set(current['autonomy']['may_write'])) else 'owner_permission')
            else:
                routes.append('research_dispatch' if planner.get('research_task_granted') and planner.get('probe_templates') else 'owner_permission')
        if backlog.get('available') and backlog['new_questions'] > 3 * max(backlog['answered'], 1): routes.append('backlog_research')
        checkpoints = m['human_checkpoints']
        if checkpoints.get('available') and (checkpoints['oldest_age_days'] or 0) >= 14: routes.append('owner_checkpoint')
        if m['ticks'].get('available') and m['ticks']['ratio'] >= .5: routes.append('administrator_diagnosis')
        if m['quality_scores'].get('available') and m['quality_scores']['published'] == 0: routes.append('quality_scoring')
        if m['journal'].get('available') and m['journal']['entries'] == 0: routes.append('optional_feedback')
        return routes[index]
    except (KeyError, IndexError, TypeError, ValueError):
        return 'needs_review'


def groups(connection, *, include_human_closed=False):
    output = {}
    for request in requests(connection):
        if request['state'] in TERMINAL and not include_human_closed: continue
        # An actual human assignment is an instruction; automatic owner binding
        # at creation is not a human review decision.
        if request['decisions']:
            route = 'owner_checkpoint'
        elif request['kind'] == 'model_redraft' and _model_scope(request): route = 'model_redraft'
        elif request['kind'] == 'research_reflection': route = reflection_route(connection, request)
        elif request['kind']=='framework_repair': route='framework_repair'
        elif request['kind']=='historical_period_review':
            route='dossier_repair' if any(e['kind']=='dossier' for e in request['evidence']) else 'framework_repair' if any(e['kind']=='framework' for e in request['evidence']) else 'needs_review'
        else: route = 'needs_review'
        scope = _model_scope(request) if route == 'model_redraft' else {'company_ref': request['company_ref'], 'request_ref': request['id']}
        if request['kind']=='research_reflection' and route not in {'needs_review','owner_permission','owner_checkpoint'}:
            source=next(resolve_evidence(connection,e) for e in request['evidence'] if e['kind']=='reflection')
            scope={'company_ref':request['company_ref'],'window':source['window']}
        elif route in {'dossier_repair','framework_repair'}:
            scope={'company_ref':request['company_ref'],'source_version_ref':request['scope']['source_version_ref']}

        group_ref = 'research-followup-group:' + content_hash({'route': route, 'scope': scope})[:32]
        group = output.setdefault(group_ref, {'group_ref': group_ref, 'company_ref': request['company_ref'], 'route': route, 'scope': scope, 'requests': []})
        if request['kind']=='research_reflection':
            current=governing_mission(connection,request)
            if current is not None:
                group['execution_grant']={'mission_version_ref':current['id'],'mission_hash':current['content_hash'],
                                          'may_write':current['autonomy']['may_write'],'budget':current['budget']}
        group['requests'].append({'ref': request['id'], 'hash': request['content_hash']})
    for group in output.values():
        group['requests'].sort(key=lambda item: item['ref'])
        group['content_hash'] = content_hash(group)
    return sorted(output.values(), key=lambda item: item['group_ref'])


def model_redraft_context(connection, company_ref):
    """Stable source-only state binding, including already processed groups.

    System completion does not remove a context and trigger a reverse re-draft.
    No record is added when there is no exact source-bound comparison.
    """
    entries=[]
    for group in groups(connection):
        if group['company_ref'] != company_ref or group['route'] != 'model_redraft': continue
        comparisons=[]
        for item in group['requests']:
            request=next(r for r in requests(connection, company_ref=company_ref) if r['id']==item['ref'])
            for e in request['evidence']:
                comparison=resolve_evidence(connection,e)
                if comparison.get('company_ref') != company_ref or comparison.get('cell_ref') != group['scope']['cell_ref'] or comparison.get('period') != group['scope']['period'] or comparison.get('model_version_ref') != request['scope']['model_version_ref'] or comparison.get('status') != 'compared' or comparison.get('redraft_required') is not True:
                    raise ValueError('redraft group does not bind exact material comparison')
                comparisons.append({'ref':comparison['id'],'hash':comparison['content_hash'], 'model_version_ref':comparison['model_version_ref'],
                                    'forecast':comparison['forecast'], 'actual':comparison['actual'], 'unit':comparison['unit'],
                                    'result_ref':comparison['result_ref'], 'assumption_refs':comparison.get('assumption_refs') or [], 'method_refs':comparison.get('method_refs') or []})
        entries.append({**group,'comparisons':comparisons})
    if not entries: return None
    body={'rule_ref':RULE,'company_ref':company_ref,'groups':entries}
    return {**body,'content_hash':content_hash(body)}


def latest(connection, group_ref):
    if not _exists(connection,'research_followup_automation_versions'): return None
    row=connection.execute('SELECT * FROM research_followup_automation_versions WHERE group_ref=? ORDER BY rowid DESC LIMIT 1',(group_ref,)).fetchone()
    return None if row is None else _verified(row)


def _spec(connection, spec_ref):
    from .coverage_mission import CoverageMissionAuthority
    row=connection.execute('SELECT * FROM coverage_mission_company_model_specs WHERE spec_id=?',(spec_ref,)).fetchone()
    if row is None: raise ValueError('redraft specification is missing')
    record=CoverageMissionAuthority._model_spec_row(row)
    fields=('schema_version','company_ref','state_hash','assessment','revenue_anchor_concept','revenue_drivers','expense_lines',
            'forecast_statements','operating_metrics','horizon','financial_statement_structure','cash_flow_companion','decided_by','task_hash',
            'driver_formulas','fcf_definition','cost_driver_template')
    if content_hash({k:record[k] for k in fields if k in record}) != record['content_hash']:
        raise ValueError('redraft specification authority hash drifted')
    return record


def binding(connection, spec_ref):
    if not _exists(connection,'research_followup_spec_bindings'): return None
    row=connection.execute('SELECT * FROM research_followup_spec_bindings WHERE spec_ref=?',(spec_ref,)).fetchone()
    if row is None: return None
    record=_verified(row); spec=_spec(connection,spec_ref)
    if spec['content_hash'] != record['spec_hash'] or spec['state_hash'] != record['state_hash'] or spec['company_ref'] != record['company_ref']:
        raise ValueError('redraft specification binding drifted')
    # Frozen request bytes and actual source proof still have to resolve.
    context=record['context']
    if content_hash({k:v for k,v in context.items() if k!='content_hash'}) != context['content_hash']:
        raise ValueError('redraft context hash drifted')
    held={r['id']:r for r in requests(connection, company_ref=record['company_ref'])}
    for group in context['groups']:
        for request in group['requests']:
            if request['ref'] not in held or held[request['ref']]['content_hash'] != request['hash']:
                raise ValueError('redraft binding names different request bytes')
        for comparison in group['comparisons']:
            resolve_evidence(connection,{'kind':'model_cell_reconciliation','ref':comparison['ref'],'hash':comparison['hash']})
    return record


def planner_context(connection, mission):
    """Frozen sorting suggestions for the normal brain, not task admissions.

    No observation status enters this hash: adopting a plan does not cause a
    second paid plan, and a tick or later spend never invents a new suggestion.
    """
    held={r['id']:r for r in requests(connection)};output=[];questions={}
    for group in groups(connection):
        if group['route'] not in {'research_dispatch','backlog_research'}:continue
        if not all((governing_mission(connection,held[x['ref']]) or {}).get('id')==mission['id'] for x in group['requests']):continue
        suggestions=[]
        for item in group['requests']:
            request=held[item['ref']]
            source=next(resolve_evidence(connection,e) for e in request['evidence'] if e['kind']=='reflection')
            candidate=request['scope']['candidate'];question_views=[]
            from .research_question_backlog import ResearchQuestionBacklog,ResearchQuestionNotFound
            backlog=object.__new__(ResearchQuestionBacklog);backlog.connection=connection
            for ref in candidate.get('refs') or []:
                if not str(ref).startswith('research-question:'):continue
                question_views.append(ref)
                if ref in questions:continue
                if not _exists(connection,'backlog_questions'):
                    questions[ref]={'question_ref':ref,'status':'not_held'};continue
                try:question=backlog.question(ref)
                except ResearchQuestionNotFound:
                    questions[ref]={'question_ref':ref,'status':'not_held'};continue
                subject=question['identity']['identity']['company_ref']
                if subject and subject not in {x['company_ref'] for x in mission['universe']}:
                    questions[ref]={'question_ref':ref,'status':'outside_current_scope'};continue
                questions[ref]={'question_ref':ref,'status':'held','identity_hash':question['identity']['content_hash'],
                    'head':{key:question['head'][key] for key in ('id','content_hash','company_ref','question','answer_criteria','source_refs')},
                    'state':question['state'],'answer_bindings':question['answer_bindings'],
                    'waiting_material_request':question['waiting_material_request']}
            suggestions.append({'request_ref':request['id'],'request_hash':request['content_hash'],
                'reflection_ref':source['id'],'reflection_hash':source['content_hash'],
                'candidate':candidate,'backlog_question_refs':question_views})
        output.append({'group':group,'suggestions':suggestions})
    if not output:return None
    body={'rule_ref':'reflection-normal-planner-input:1','mission_ref':mission['id'],
          'mission_hash':mission['content_hash'],'groups':output,'backlog_questions':list(questions.values())}
    return {**body,'content_hash':content_hash(body)}


def verify_actual_source(connection, actual, company_ref):
    """Replay fixed source versions; a later restatement never rewrites history."""
    from decimal import Decimal
    from .company_model_inputs import _model_unit
    if actual['basis']=='filed':
        from .statement_context import resolve_line_refs
        from .company_model_annual_projection import verify_statement_filing
        from .company_model_series import quarterly_series
        refs=actual['refs']; line_refs=[r['line_id'] for r in refs if r.get('line_id')]
        if not line_refs or len(line_refs)!=len(refs):raise ValueError('filed redraft actual source refs are incomplete')
        bindings={r['ingest_id']:r['statement_context_ref'] for r in refs if r.get('statement_context_ref')}
        rows=resolve_line_refs(connection,line_refs,bindings); enriched=[]
        for row in rows:
            filing=connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?',(row['ingest_id'],)).fetchone()
            if filing is None or filing['company_ref']!=company_ref:raise ValueError('redraft actual filing company differs')
            verify_statement_filing(connection,dict(filing),allow_legacy_metadata=bool(bindings.get(row['ingest_id'])))
            if row.get('dimensions') is None and (row.get('dimension_axis') or row.get('dimension_count')):
                raise ValueError('redraft actual dimension identity is missing')
            if row['concept']!=actual['concept'] or row['statement']!=actual['statement'] or (row.get('dimensions') or {})!=actual['dimensions']:
                raise ValueError('redraft actual source identity differs')
            enriched.append({**row,'dimensions':row.get('dimensions') or {},'accession':filing['accession'],'filed':filing['filed'],'form':filing['form']})
        series=quarterly_series(enriched)
        match=[cell for cell in [*series['quarters'],*series['instants']] if cell['period_end']==actual['period_end'] and cell.get('period_start')==actual.get('period_start') and _model_unit(cell['unit'])==actual['unit'] and Decimal(cell['value'])==Decimal(actual['value'])]
        if len(match)!=1:raise ValueError('redraft actual value cannot be replayed from fixed filing rows')
        return {'kind':'fixed_filing_actual','refs':refs,'rows_hash':content_hash(enriched),'cell_hash':content_hash(match[0])}
    if actual['basis']=='preliminary':
        from .earnings_release_intake import SCALES,preliminary_actuals
        held={r['id']:r for r in preliminary_actuals(connection,company_ref,include_superseded=True)}
        refs=actual['refs']
        if len(refs)!=1 or refs[0]['ref'] not in held:raise ValueError('redraft preliminary source is missing')
        row=held[refs[0]['ref']]
        if row['content_hash']!=actual['source_hash'] or row['content_hash']!=refs[0]['source_hash'] or row['period_start']!=actual['period_start'] or row['period_end']!=actual['period_end'] or _model_unit(row['unit'])!=actual['unit'] or Decimal(row['value'])*SCALES[row['scale']]!=Decimal(actual['value']):
            raise ValueError('redraft preliminary actual differs from its exact source')
        return {'kind':'fixed_preliminary_actual','ref':row['id'],'hash':row['content_hash']}
    raise ValueError('redraft actual basis is not an admitted financial source')


class FollowupAutomationAuthority:
    _authorized=authorized_flag()
    def __init__(self, store, *, clock=None):
        self.store,self.connection=store,store.connection
        self.clock=clock or (lambda:datetime.now(timezone.utc).isoformat(timespec='microseconds'))
        self._authorization_flag=authorization_flag(self.connection,'dalton_followup_automation_authorized')
        self.connection.executescript(_SCHEMA)

    def _insert(self, table, columns, values):
        self._authorized=True
        try:
            with self.store._transaction() as cursor: cursor.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})',values)
        finally:self._authorized=False

    def observe(self, group, *, state, reason, evidence=None):
        if state not in {'pending','running','waiting_budget','waiting_evidence','administrator','optional_feedback','completed','needs_review'} or not str(reason).strip():
            raise ValueError('followup automation needs an explicit declared state')
        current=next((g for g in groups(self.connection) if g['group_ref']==group['group_ref']),None)
        if current != group: raise ValueError('automation group source changed or human already decided')
        if state=='completed':
            if not evidence:raise ValueError('automatic completion requires an exact verified model redraft')
            if evidence.get('kind')=='verified_model_redraft' and group['route']=='model_redraft':
                proven=self.model_result(evidence['model_version_ref'],group)
            elif evidence.get('kind')=='verified_product_repair' and group['route'] in {'dossier_repair','framework_repair'}:
                from .research_followup_product_repair import verify_result
                proven=verify_result(self.connection,evidence,group)
            else:raise ValueError('automatic completion requires an exact verified model redraft or product repair')
            if proven != evidence: raise ValueError('automatic result differs from held exact proof')
        body={'rule_ref':RULE,'group':group,'group_ref':group['group_ref'],'company_ref':group['company_ref'],
              'state':state,'reason':reason,'evidence':evidence}
        prior=latest(self.connection,group['group_ref'])
        if prior and all(prior.get(k)==v for k,v in body.items()):return prior
        body['created_at']=self.clock(); body['id']='research-followup-automation:'+content_hash(body)[:32];body['content_hash']=content_hash(body)
        self._insert('research_followup_automation_versions','version_id,group_ref,company_ref,record_json,content_hash,created_at',
                     (body['id'],body['group_ref'],body['company_ref'],canonical_json(body),body['content_hash'],body['created_at']))
        return body

    def bind_spec(self, spec, state):
        context=state.get('model_redraft_context')
        if context is None:return None
        if context != model_redraft_context(self.connection,spec['company_ref']) or spec['state_hash'] != state['state_hash'] or content_hash({k:v for k,v in state.items() if k!='state_hash'}) != state['state_hash']:
            raise ValueError('redraft spec does not bind exact current model input')
        held=_spec(self.connection,spec['spec_id'])
        if held['content_hash']!=spec['content_hash']:raise ValueError('redraft spec differs from held authority')
        old=binding(self.connection,spec['spec_id'])
        body={'rule_ref':RULE,'company_ref':spec['company_ref'],'spec_ref':spec['spec_id'],'spec_hash':spec['content_hash'],
              'state_hash':state['state_hash'],'context':context}
        if old:
            if any(old[k]!=v for k,v in body.items()):raise ValueError('redraft spec retry changes its binding')
            return old
        body['created_at']=self.clock();body['content_hash']=content_hash(body)
        self._insert('research_followup_spec_bindings','spec_ref,company_ref,record_json,content_hash,created_at',
                     (body['spec_ref'],body['company_ref'],canonical_json(body),body['content_hash'],body['created_at']))
        return body

    def model_result(self, model_ref, group):
        from .model_forecast_driver import ForecastModelAuthority, is_structured_schema
        reader=object.__new__(ForecastModelAuthority);reader.connection=self.connection
        model=reader.model(model_ref); bound=binding(self.connection,model['spec_ref'])
        if bound is None or model['company_ref']!=group['company_ref'] or model['spec_hash']!=bound['spec_hash'] or not is_structured_schema(model.get('schema_version')):
            raise ValueError('redraft model lacks exact specification/input binding')
        frozen=next((g for g in bound['context']['groups'] if g['group_ref']==group['group_ref']),None)
        if frozen is None or {k:v for k,v in frozen.items() if k!='comparisons'} != group:
            raise ValueError('redraft model does not consume exact request group')
        if model_ref in {r['model_version_ref'] for r in frozen['comparisons']}:raise ValueError('original forecast is not a completed redraft')
        proof=reader.filing_proof(model_ref)
        if proof is None or (proof.get('invariant_report') or {}).get('status')!='available' or (proof.get('invariant_report') or {}).get('failed'):
            raise ValueError('redraft model has no passing exact filing proof')
        # The affected future result must genuinely compute. No unrelated
        # revenue-only model or unavailable net-income chain can close it.
        results=[]; actual_proofs=[]
        for comparison in frozen['comparisons']:
            actual=comparison['actual']
            actual_proofs.append(verify_actual_source(self.connection,actual,group['company_ref']))
            original=reader.model(comparison['model_version_ref'])
            target=next((r for r in original['results'] if r['ref']==comparison['result_ref']),None)
            if target is None:raise ValueError('original affected result is missing')
            candidates=[r for r in model['results'] if r.get('role')==target.get('role') and r['unit'].lower()==target['unit'].lower()]
            if len(candidates)!=1:raise ValueError('redraft affected result identity is ambiguous or missing')
            result=candidates[0]
            if actual['basis']=='filed':
                line_refs={r['line_id'] for r in actual['refs'] if r.get('line_id')}
                if actual['period_end'] not in model['history_periods'] or not line_refs or not line_refs.issubset(set(proof['statement_line_refs'])):
                    raise ValueError('redraft did not consume exact affected actual rows')
            else:
                preliminary=(model.get('forecast_policy') or {}).get('preliminary') or {}
                role=str(target.get('role') or '')
                metric={'diluted_eps':'eps','diluted_weighted_average_shares':'diluted_shares'}.get(role,role)
                from decimal import Decimal
                if preliminary.get('period_end')!=actual['period_end'] or metric not in (preliminary.get('values') or {}) or Decimal(preliminary['values'][metric])!=Decimal(actual['value']):
                    raise ValueError('redraft did not consume exact affected preliminary actual')
            results.append(result)
        result_refs={r['ref'] for r in results}
        forward={canonical_json(p) for p in model['forecast_periods']}
        cells=[c for r in model['results'] if r['ref'] in result_refs for c in r.get('cells') or [] if canonical_json(c['period']) in forward and c.get('kind')=='estimate']
        if not cells or any(c.get('status')!='computed' or c.get('value') is None for c in cells):
            raise ValueError('affected redraft forecast cells are unavailable')
        return {'kind':'verified_model_redraft','model_version_ref':model_ref,'model_hash':model['content_hash'],'spec_ref':model['spec_ref'],
                'spec_hash':model['spec_hash'],'input_binding_hash':bound['content_hash'],'filing_proof_hash':proof['content_hash'],
                'forecast_cells_hash':content_hash(cells),'actual_proofs':actual_proofs,'group_hash':group['content_hash']}

    def complete_model(self, model_ref):
        output=[]
        for group in groups(self.connection):
            if group['route']!='model_redraft':continue
            try:evidence=self.model_result(model_ref,group)
            except ValueError:continue
            output.append(self.observe(group,state='completed',reason='已针对这些冻结偏差重新起草模型；正式新模型和受影响预测单元通过现有财务核验。旧预测与偏差保留。',evidence=evidence))
        return output

    def observe_planner(self, *, context, stored_plan, full_state, call=None, prompt=None):
        """Record actual normal planner consumption; never research completion."""
        if context is None:return []
        from pathlib import Path
        from .research_followup_product_repair import _ro
        from .research_planner import plan_from_response
        from .research_state import state_content_hash
        row=self.connection.execute('SELECT * FROM coverage_mission_research_plans WHERE plan_id=?',(stored_plan['plan_id'],)).fetchone()
        if row is None or not row['plan_json']:raise ValueError('followup planner exact plan is unavailable')
        exact=json.loads(row['plan_json'])
        if call is None:
            state_dir=Path(next(x[2] for x in self.connection.execute('PRAGMA database_list') if x[1]=='main')).parent
            scheduler=_ro(state_dir/'scheduler.sqlite')
            try:
                workrow=scheduler.execute('SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?',(row['work_order_ref'],)).fetchone()
                formal=scheduler.execute('SELECT * FROM scheduler_formal_results WHERE work_order_id=?',(row['work_order_ref'],)).fetchone()
                if workrow is None or formal is None or formal['terminal_state']!='succeeded':
                    raise ValueError('followup stored plan has no actual successful normal Work')
                prompt=json.loads(workrow[0])['question'];result=json.loads(formal['result_envelope_json'])
                call={'work_order_ref':row['work_order_ref'],'result_envelope_ref':formal['result_envelope_id'],
                      'text':result.get('outputs',{}).get('text')}
            finally:scheduler.close()
        if (content_hash({k:v for k,v in exact.items() if k!='content_hash'})!=row['content_hash']
            or exact['content_hash']!=row['content_hash'] or row['work_order_ref']!=call['work_order_ref']
            or exact['mission_version_ref']!=context['mission_ref']):
            raise ValueError('followup planner stored plan binding differs')
        if (state_content_hash(full_state)!=full_state.get('content_hash')
            or full_state['content_hash']!=exact['state_hash']
            or full_state.get('reflection_followup_context')!=context):
            raise ValueError('followup full planner state hash or suggestions differ')
        frozen=json.loads(prompt.rsplit('RESEARCH_STATE=',1)[1])
        from .research_planner import followup_context_matches_prompt
        if not followup_context_matches_prompt(frozen.get('reflection_followup_context'), context) or frozen['content_hash']!=exact['state_hash']:
            raise ValueError('followup suggestions are not in the exact planner input')
        state_dir=Path(next(x[2] for x in self.connection.execute('PRAGMA database_list') if x[1]=='main')).parent
        scheduler=_ro(state_dir/'scheduler.sqlite')
        try:
            workrow=scheduler.execute('SELECT * FROM scheduler_work_orders WHERE work_order_id=?',(call['work_order_ref'],)).fetchone()
            resultrow=scheduler.execute('SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?',(call['result_envelope_ref'],)).fetchone()
            if workrow is None or resultrow is None:raise ValueError('followup normal planner Work/result unavailable')
            work=json.loads(workrow['work_order_json']);envelope=json.loads(resultrow['result_envelope_json'])
            if (content_hash(work)!=workrow['work_order_hash'] or work['id']!=call['work_order_ref']
                or work['question']!=prompt or (work.get('metadata') or {}).get('purpose')!='plan'
                or content_hash(envelope)!=resultrow['result_envelope_hash']
                or resultrow['outcome']!='succeeded' or resultrow['work_order_id']!=work['id']
                or envelope['id']!=call['result_envelope_ref'] or envelope.get('work_order_ref')!=work['id']
                or envelope.get('status')!='succeeded' or envelope.get('outputs',{}).get('text')!=call['text']):
                raise ValueError('followup normal planner Work/result hash or input differs')
            if plan_from_response(full_state,call['text'],created_at=exact['created_at'])!=exact:
                raise ValueError('followup formal plan does not replay the actual planner output')
            evidence={'kind':'normal_planner_consumption','input_context':context,'state_hash':exact['state_hash'],
                'prompt_hash':content_hash(prompt),'work_ref':work['id'],'work_hash':workrow['work_order_hash'],
                'result_ref':envelope['id'],'result_hash':resultrow['result_envelope_hash'],
                'plan_ref':stored_plan['plan_id'],'plan_hash':row['content_hash'],
                'directives':len(exact['directives']),'inquiries':len(exact['inquiries']),
                'assessment':exact['assessment']}
        finally:scheduler.close()
        current={g['group_ref']:g for g in groups(self.connection)};output=[]
        for item in context['groups']:
            group=item['group']
            if current.get(group['group_ref'])!=group:continue
            # A later lane status cannot make this exact plan a second adoption.
            prior_match=None
            for held in self.connection.execute('SELECT * FROM research_followup_automation_versions WHERE group_ref=?',(group['group_ref'],)):
                existing=_verified(held)
                if existing['group']==group and existing['evidence']==evidence:
                    prior_match=existing;break
            if prior_match is not None:
                output.append(prior_match);continue
            output.append(self.observe(group,state='waiting_evidence',
                reason='冻结的研究排序建议已进入正常规划并生成正式研究计划；具体安排与判断保留，尚不代表研究问题已回答。',evidence=evidence))
        return output

    def observe_lane(self, operation, result):
        routes={'dispatch_research_task':{'research_dispatch','backlog_research'},
                'dispatch_quality_scoring':{'quality_scoring'},'dispatch_company_model_spec':{'model_redraft'},
                'dispatch_company_model_forecast':{'model_redraft'},
                'dispatch_company_dossier':{'dossier_repair'},'dispatch_industry_framework':{'framework_repair'}}
        if operation not in routes or not isinstance(result,Mapping):return []
        output=[]; status=str(result.get('status') or '')
        lane_evidence={'kind':'lane_observation','operation':operation,'result_hash':content_hash(result),
                       'result':dict(result)}
        for group in groups(self.connection):
            if group['route'] not in routes[operation]:continue
            if group['route'] in {'model_redraft','dossier_repair'} and result.get('company_ref') != group['company_ref']:continue
            if group['route']=='framework_repair':
                target=result.get('industry_ref')
                if target is None:
                    from .coverage_mission import CoverageMissionAuthority
                    mr=object.__new__(CoverageMissionAuthority);mr.connection=self.connection
                    active={mr._mission_record(row[0])['industry_ref'] for row in self.connection.execute('SELECT mission_version_id FROM coverage_mission_pointer')}
                    if group['company_ref'] not in active:continue
                elif target!=group['company_ref']:continue
            prior=latest(self.connection,group['group_ref'])
            if prior and prior['state']=='completed' and prior['group']==group:continue
            if status in {'running','busy','launched'}:state,reason='running','最近一次正常研究流程已启动；这项跟进尚无核验完成证明。'
            elif 'pool_exhausted' in status or 'budget' in status or result.get('reason') in {'daily launch cap reached','minimum launch interval'}:state,reason='waiting_budget','等待现有预算池恢复；不提高预算，也不要求你逐项确认。'
            elif status in {'not_granted','unconfigured','unavailable','held','refused','rejected','failed'}:state,reason='administrator','现有自动流程暂未能继续，请管理员核对任务配置或失败原因。'
            else:
                state,reason='waiting_evidence','本次正常流程未承接合格产物；跟进保留，尚无完成证明。'
                if group['route'] in {'dossier_repair','framework_repair'}:
                    cause=str(result.get('reason') or result.get('failure_reason') or status or '')
                    if cause=='no screened company needs a dossier':
                        reason='本次没有符合已有产物修复与资料门槛的档案；原复核请求保留，尚无完整重写结果。'
                    else:
                        reason='本次正常研究流程未承接合格重写输入，实际门槛与原因保留在运行记录；复核尚未完成。'
            output.append(self.observe(group,state=state,reason=reason,evidence=lane_evidence))
        return output


def _system_subject_label(connection, target, label):
    if not str(target).startswith(('industry:','coverage-mission:')):return label(target)
    if _exists(connection,'coverage_mission_pointer'):
        from .coverage_mission import CoverageMissionAuthority
        from .research_gap_display import owner_terms_text
        reader=object.__new__(CoverageMissionAuthority);reader.connection=connection
        matches=[]
        for row in connection.execute('SELECT mission_version_id FROM coverage_mission_pointer'):
            mission=reader._mission_record(row[0])
            if target in {mission['mission_ref'],mission['industry_ref']}:matches.append(mission)
        if len(matches)==1:return owner_terms_text(matches[0]['title'])
    return '行业研究' if str(target).startswith('industry:') else '本研究项目'


def _system_reason(observed, fallback):
    if not observed:return fallback
    if observed['state']=='running':return '研究已启动，结果仍待核验。'
    if observed['state']=='waiting_budget':return '等待研究预算恢复后继续。'
    if (observed.get('evidence') or {}).get('kind')=='normal_planner_consumption':
        return '已纳入研究计划，后续结果仍待核验。'
    return fallback


def system_cards(connection, label):
    cards=[]
    held_requests={request["id"]: request for request in requests(connection)}
    for group in groups(connection):
        route=group['route']
        if route in {'needs_review','owner_permission','owner_checkpoint'}:continue
        old=latest(connection,group['group_ref'])
        if old and old['group']!=group:old=None
        if old and old['state']=='completed':
            reader=object.__new__(FollowupAutomationAuthority);reader.connection=connection
            if old['evidence']['kind']=='verified_product_repair':
                from .research_followup_product_repair import verify_result
                proven=verify_result(connection,old['evidence'],group)
            else:proven=reader.model_result(old['evidence']['model_version_ref'],group)
            if proven != old['evidence']:
                raise ValueError('automatic followup completion proof does not replay')
            continue
        role='optional_feedback' if route=='optional_feedback' else 'administrator' if route=='administrator_diagnosis' or old and old['state']=='administrator' else 'background'
        reason=(old or {}).get('reason') or {'model_redraft':'系统将自动检查冻结偏差并重新起草核验模型；原预测保留。',
               'research_dispatch':'现有研究安排流程将按权限和预算继续安排专项研究。','backlog_research':'系统按现有研究计划推进问题清单，等待可用资料与预算。',
               'quality_scoring':'系统评分流程等待合格研究产物；尚无评分不等同于需要你批准。',
               'optional_feedback':'你可以随时提供使用反馈；没有反馈不会阻断研究。',
               'administrator_diagnosis':'后台运行记录需要管理员诊断，不要求你做研究判断。',
               'dossier_repair':'系统按当前权限、资料和预算检查已有档案的修复单位；尚无完整核验通过的重写结果，原文和复核请求保留。',
               'framework_repair':'系统按现有行业研究流程分批核对并重写对应单位；资料不足或预算不足时等待，完整核验通过前保持未完成。'}[route]
        reason=_system_reason(old,reason)
        titles={'model_redraft':'系统复核预测偏差','research_dispatch':'后台安排专项研究','backlog_research':'后台推进研究问题','quality_scoring':'后台核对研究评分','optional_feedback':'可选：提供研究反馈','administrator_diagnosis':'后台运行诊断','dossier_repair':'后台复核公司历史段落','framework_repair':'后台修复行业研究框架'}
        source_times=[]
        for item in group['requests']:
            request=held_requests[item['ref']]
            if request['content_hash']!=item['hash']:
                raise ValueError('automatic followup card source hash differs')
            source_times.append(request['created_at'])
        if old:source_times.append(old['created_at'])
        at=max(source_times)
        repair_details={}
        if route in {'dossier_repair','framework_repair'}:
            from .research_followup_product_repair import record, affected_units
            from .redraft_policy import _unit_payload
            product='dossier' if route=='dossier_repair' else 'industry_framework'
            source=record(connection,product,group['scope']['source_version_ref'])
            unit_labels=[]
            for item in group['requests']:
                for unit in affected_units(source,held_requests[item['ref']],product):
                    block=_unit_payload(source,product,unit) or {}
                    name=block.get('title') or block.get('aspect') or block.get('link') or {'long_term_drivers':'长期行业驱动','short_term_drivers':'短期行业驱动','characteristics':'行业特征','industry_classification':'行业分类','variant_view':'差异观点'}.get(unit) or ('因果链第'+unit.split(':')[1]+'环' if unit.startswith('causal_chain:') else unit)
                    if name not in unit_labels:unit_labels.append(name)
            repair_details={'复核章节':'、'.join(unit_labels),'原研究版本':'第 '+str(source['version'])+' 版'}
        cards.append({'kind':'research_followup','ref':group['group_ref'],'hash':group['content_hash'],'at':at,'title':titles[route],
                      'who':_system_subject_label(connection,group['company_ref'],label),'summary':reason,'decision_required':False,'responsibility':role,
                      'automation_state':(old or {}).get('state') or ('optional_feedback' if role=='optional_feedback' else 'waiting_evidence' if route in {'dossier_repair','framework_repair'} else 'pending'),
                      'source_requests':group['requests'],'actions':[],'followup_fields':False,'needs_rationale':False,
                      'details':{'处理方式':'按现有单位上限分批处理，等待资料与核验' if route in {'dossier_repair','framework_repair'} else '系统自动处理' if role=='background' else '可选反馈' if role=='optional_feedback' else '管理员诊断',
                                 '关联研究事项':str(len(group['requests']))+'项',**repair_details},'detail_labels':{'处理方式':'处理方式','关联研究事项':'关联研究事项','复核章节':'复核章节','原研究版本':'原研究版本'},'next_step':reason})
    return cards


def automatic_request_ids(connection):
    return {r['ref'] for group in groups(connection) if group['route'] not in {'needs_review','owner_permission','owner_checkpoint'} for r in group['requests']}
