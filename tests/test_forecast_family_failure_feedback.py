import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.forecast_consumed_source_groups import select_rows
from dalton_core.forecast_execution_statement_scope import execution_scope
from dalton_core.forecast_family_failure_feedback import build, pointer, read, CONTRACT_REF
from dalton_core.store import content_hash


class FamilyFeedbackTests(unittest.TestCase):
    def fixture(self, company='company:manufacturer'):
        from tests.test_forecast_source_family_partition import SourceFamilyPartitionTests
        record, filings, sources = SourceFamilyPartitionTests('runTest').fixture(company, True)
        # Preserve an independent complete pure family; the selected fixed
        # family has only a, and its b driver has no history in that period.
        missing = sources['ingest:old'][2]
        end = missing['period_end']
        for row in list(sources['ingest:old'][:3]):
            if row['dimensions']:
                pure = copy.deepcopy(row); pure['line_id'] += ':pure'
                pure['dimensions'].pop('srt:ConsolidationItemsAxis')
                pure['dimension_count'] = 1
                sources['ingest:old'].append(pure)
        sources['ingest:old'].remove(missing)
        record['drivers'][1]['history'] = [h for h in record['drivers'][1]['history'] if h['period_end'] != end]
        record.update(mission_version_ref='mission:current', spec_ref='company-model-spec:current',
                      spec_hash='a'*64, inputs_hash='b'*64)
        rows, groups = select_rows(record, filings, sources)
        _, scope = execution_scope(record, rows, groups)
        return record, rows, scope

    def test_two_companies_exact_gap_and_independent_choice_not_a_spliced_repair(self):
        for company in ('company:manufacturer', 'company:services'):
            record, rows, scope = self.fixture(company)
            before = copy.deepcopy((record, rows, scope))
            detail = build(record, rows, scope)
            self.assertEqual(detail['total_count'], 1)
            item = detail['items'][0]
            self.assertEqual(item['member_sum'], '40')
            self.assertEqual(item['reported_total'], '100')
            self.assertEqual(item['difference'], '60')
            self.assertEqual(item['fixed_dimensions'], {'srt:ConsolidationItemsAxis':'us-gaap:OperatingSegmentsMember'})
            choice = item['same_source_complete_choices'][0]
            self.assertEqual(choice['fixed_dimensions'], {})
            self.assertEqual([p['value'] for p in choice['group']['parts']], ['40','60'])
            self.assertFalse(detail['financial_evidence']); self.assertFalse(detail['retry_authority'])
            self.assertEqual((record, rows, scope), before)

    def test_bounded_items_retain_total_and_omitted_hash(self):
        record, rows, scope = self.fixture()
        # Three distinct original periods, all genuinely nonclosing.
        for row in rows:
            if not row['dimensions'] and row['ingest_id']=='ingest:old': row['value']='101'
        # Rebuild the strong manifests after changing private fixture inputs.
        from tests.test_forecast_source_family_partition import SourceFamilyPartitionTests
        r, filings, sources = SourceFamilyPartitionTests('runTest').fixture(fixed=True)
        r.update(mission_version_ref=record['mission_version_ref'],spec_ref=record['spec_ref'],spec_hash=record['spec_hash'],inputs_hash=record['inputs_hash'])
        for row in sources['ingest:old']:
            if not row['dimensions']: row['value']='101'
        rr, gg=select_rows(r,filings,sources);_, ss=execution_scope(r,rr,gg)
        detail=build(r,rr,ss)
        self.assertEqual(detail['total_count'],4);self.assertEqual(len(detail['items']),2)
        self.assertEqual(detail['omitted_count'],2)
        self.assertNotEqual(detail['omitted_items_hash'],content_hash([]))

    def prepare(self):
        record, rows, scope = self.fixture()
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);state=Path(tmp.name)
        run='1'*24; path=state/'model-forecast-runs'/run/'summary.json';path.parent.mkdir(parents=True)
        summary={'company_ref':record['company_ref'],'spec_ref':record['spec_ref'],'digest':'d'*64,
                 'forecast_status':'unavailable:economic_invariants','family_partition_failures':build(record,rows,scope)}
        path.write_text(json.dumps(summary)+'\n')
        token=pointer(state,'model-forecast-run:'+run,summary)
        event={'reason':token+'; refusal','item_key':record['company_ref']+'|'+summary['digest']+'|rules'}
        core=sqlite3.connect(':memory:');core.row_factory=sqlite3.Row;self.addCleanup(core.close)
        core.execute('CREATE TABLE coverage_mission_pointer(mission_ref,mission_version_id)')
        core.execute("INSERT INTO coverage_mission_pointer VALUES('mission-ref','mission:current')")
        core.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id, company_ref)')
        core.execute('INSERT INTO coverage_mission_statement_filings VALUES (?,?)',('ingest:old',record['company_ref']))
        spec={'spec_id':record['spec_ref'],'content_hash':record['spec_hash'],
              'revenue_drivers':record['execution_contract']['specification']['revenue_drivers']}
        return record,rows,state,path,summary,event,core,spec

    def checked(self, values, **changes):
        record,rows,state,path,summary,event,core,spec=values
        with patch('dalton_core.coverage_mission.CoverageMissionAuthority.mission',return_value={'id':'mission:current','mission_ref':'mission-ref','universe':[{'company_ref':record['company_ref']}]}), \
             patch('dalton_core.coverage_mission.CoverageMissionAuthority.latest_company_model_spec',return_value=changes.get('spec',spec)), \
             patch('dalton_core.statement_context._latest',return_value=changes.get('context','context:ingest:old')), \
             patch('dalton_core.statement_context.statement_rows',return_value=changes.get('rows',rows)), \
             patch('dalton_core.company_model_annual_projection.verify_statement_filing',return_value='verified') as strong:
            result=read(core,state_dir=state,company_ref=record['company_ref'],
                        mission=changes.get('mission',{'id':'mission:current','mission_ref':'mission-ref','universe':[{'company_ref':record['company_ref']}]}),event=event)
            if result is not None:self.assertTrue(strong.called)
            return result

    def test_pointer_and_reader_rebuild_every_fact_under_current_spec(self):
        v=self.prepare();before=v[-2].total_changes
        self.assertEqual(self.checked(v),v[4]['family_partition_failures'])
        self.assertEqual(v[-2].total_changes,before)
        self.assertLess(len(v[5]['reason']),500)

    def test_new_spec_or_context_does_not_report_old_gap_as_current(self):
        v=self.prepare()
        self.assertIsNone(self.checked(v,spec={**v[-1],'spec_id':'company-model-spec:new'}))
        self.assertIsNone(self.checked(v,context='context:changed'))
        with self.assertRaisesRegex(ValueError,'mission identity'):
            self.checked(v,mission={'id':'mission:other'})

    def test_original_mutation_and_foreign_source_refuse_even_rehashed_summary(self):
        v=self.prepare();rows=copy.deepcopy(v[1]);rows[0]['value']='999'
        with self.assertRaisesRegex(ValueError,'original source rows differ'):self.checked(v,rows=rows)
        v[-2].execute("UPDATE coverage_mission_statement_filings SET company_ref='company:foreign'")
        with self.assertRaisesRegex(ValueError,'another issuer'):self.checked(v)

    def test_summary_event_input_and_driver_bindings_cannot_be_borrowed(self):
        v=self.prepare();v[5]['item_key']='company:other|'+v[4]['digest']+'|rules'
        with self.assertRaisesRegex(ValueError,'another input'):self.checked(v)
        v=self.prepare();v[-1]['revenue_drivers'][0]['basis_identity']['dimensions']={}
        with self.assertRaisesRegex(ValueError,'declared driver differs'):self.checked(v)
        v=self.prepare();v[3].write_text(v[3].read_text()+' ')
        with self.assertRaisesRegex(ValueError,'summary drifted'):self.checked(v)

    def test_real_lane_pointer_active_ledger_and_planner_core_clear_together(self):
        from types import SimpleNamespace
        from datetime import datetime,timedelta,timezone
        from dalton_core.mission_model_forecast_lane import MissionModelForecastLaneCoordinator
        from dalton_core.lane_failure_ledger import LaneFailureLedger
        from dalton_core.model_execution_failure_feedback import last_failures
        from dalton_core.research_state import company_state
        from dalton_core.research_planner import _COMPANY_CORE_FIELDS
        v=self.prepare();record,rows,state,path,summary,event,core,spec=v
        ticket_ref='model-forecast-run:'+path.parent.name
        ticket={'status':'succeeded','company_ref':record['company_ref'],'model_digest':summary['digest'],
                'validator_contract_hash':'rules','summary':summary}
        coordinator=MissionModelForecastLaneCoordinator.__new__(MissionModelForecastLaneCoordinator)
        coordinator.launcher=SimpleNamespace(state_dir=state,status=lambda ref:ticket)
        settled=coordinator._settle(ticket_ref)
        self.assertEqual(settled['family_failure_pointer'],event['reason'].split(';',1)[0])
        ledger=LaneFailureLedger(state/'lane-failure-ledger.sqlite');self.addCleanup(ledger.close)
        at=datetime(2026,1,1,tzinfo=timezone.utc)
        ledger.append_event(lane='mission_model_forecast',item_key=event['item_key'],event='terminal',
            failure_class='content_refused',reason=event['reason'],rule='content',status='refused',at=at)
        mission={'id':'mission:current','mission_ref':'mission-ref','universe':[{'company_ref':record['company_ref']}]}
        with patch('dalton_core.coverage_mission.CoverageMissionAuthority.mission',return_value=mission), \
             patch('dalton_core.coverage_mission.CoverageMissionAuthority.latest_company_model_spec',return_value=spec), \
             patch('dalton_core.statement_context._latest',return_value='context:ingest:old'), \
             patch('dalton_core.statement_context.statement_rows',return_value=rows), \
             patch('dalton_core.company_model_annual_projection.verify_statement_filing',return_value='verified'):
            feedback=last_failures(core,state_dir=state,company_ref=record['company_ref'],mission=mission)
            self.assertEqual(feedback['items'][0]['family_partition_failures'],summary['family_partition_failures'])
            company=company_state({'company_ref':record['company_ref']},financial_model={
                'model_execution_failures':feedback['items'],'model_execution_failure_summary':{'status':'checked'}})
            self.assertEqual(company['model_execution_failures'],feedback['items'])
            self.assertIn('model_execution_failures',_COMPANY_CORE_FIELDS)
            ledger.append_event(lane='mission_model_forecast',item_key=event['item_key'],event='resumed',
                failure_class='content_refused',reason='resolved',rule='content',status='success',at=at+timedelta(seconds=1))
            self.assertEqual(last_failures(core,state_dir=state,company_ref=record['company_ref'],mission=mission)['items'],[])

    def test_new_diagnostics_contract_wakes_one_free_child_not_paid_spec_identity(self):
        from tests import test_mission_model_forecast_lane as lanes
        from tests import test_company_financial_statement_structure as structures
        from tests import test_financial_structure_forecast_consumer as consumers
        from dalton_core.company_model_forecast import model_digest
        fixture=lanes.LaneTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        table=consumers.financial_inputs();table['company_ref']=lanes.ACN
        spec=structures.company_spec();spec['company_ref']=lanes.ACN;spec['decided_by']='automation:test'
        spec['financial_statement_structure']=consumers.forecastable_proposal(table)
        before=copy.deepcopy(spec)
        def old_hash(body):
            if isinstance(body,dict) and 'filed_family_failure_feedback_rule' in body:
                body={k:v for k,v in body.items()if k!='filed_family_failure_feedback_rule'}
            return content_hash(body)
        with patch('dalton_core.mission_model_forecast_lane.pending_companies',return_value=[(lanes.ACN,spec,table)]), \
             patch('dalton_core.mission_model_forecast_lane.forecast_reporting_boundary',return_value=None):
            with patch('dalton_core.company_model_forecast.content_hash',side_effect=old_hash):
                old_digest=model_digest(spec,table);first=fixture.lane.dispatch_once()
            fixture.launcher.finish(first['ticket_ref'],status='failed',summary={
                'forecast_status':'unavailable:economic_invariants','failure_reason':'original family refusal'})
            old_ticket=copy.deepcopy(fixture.launcher.tickets[first['ticket_ref']])
            second=fixture.lane.dispatch_once()
            self.assertEqual(second['status'],'launched');self.assertNotEqual(second['model_digest'],old_digest)
            self.assertEqual(fixture.launcher.tickets[first['ticket_ref']],old_ticket)
            fixture.launcher.finish(second['ticket_ref'],status='failed',summary={
                'forecast_status':'unavailable:economic_invariants','failure_reason':'same genuine source gap'})
            self.assertEqual(fixture.lane.dispatch_once()['status'],'held')
            self.assertEqual(len(fixture.launcher.started),2)
        self.assertEqual(spec,before)
