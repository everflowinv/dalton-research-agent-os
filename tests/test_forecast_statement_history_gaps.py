import copy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, FinancialStatementStructureNotReady,
    forecast_structure_binding, materialize_financial_statement_structure,
    validate_structure_proposal,
)
from dalton_core.forecast_statement_history_gaps import plans
from dalton_core.forecast_material_gaps import open_gap_requests
from dalton_core.material_acquisition import TargetedSourceRung, evaluate_acceptance
from dalton_core.material_requests import open_requests
from dalton_core.mission_model_forecast_lane import MissionModelForecastLaneCoordinator
from tests import test_company_financial_statement_structure as f


def fixture(company='company:test', form='40-F', missing=True):
    inputs=f.financial_inputs(); inputs['company_ref']=company
    candidate=f.proposal(inputs)
    definition=validate_structure_proposal(
        {key:candidate[key] for key in ('schema_version','lines','formulas')},
        {**f.presentation_state(inputs),'company_ref':company},
        revenue_anchor_concept='revenue',expense_lines=f.company_spec()['expense_lines'])
    spec={**f.company_spec(),'company_ref':company,'financial_statement_structure':definition,
          'horizon':{'historical_quarters':4},'decided_by':'automation:test'}
    if missing:
        inputs['periods']=inputs['periods'][:1]
        for line in inputs['filed_lines']: line['cells']={k:v for k,v in list(line['cells'].items())[:1]}
        cost=next(l for l in inputs['filed_lines'] if l['concept']=='cost')
        cell=next(iter(cost['cells'].values()));cost['cells']={'2024-12-31':{**cell,'period_start':'2024-10-01'}}
    structure,replay=materialize_financial_statement_structure(spec,inputs)
    failure=FinancialStatementStructureNotReady(structure,replay)
    filings=[{'company_ref':company,'ingest_id':'statement-ingest:test','accession':f.ACCESSION,
              'form':form,'content_hash':'d'*64}]
    return spec,inputs,failure,filings


class StatementHistoryGapTests(unittest.TestCase):
    def test_two_foreign_issuers_and_domestic_select_original_forms_without_inferred_dates(self):
        for company,form,expected in [('company:a','40-F','foreign-disclosure-6k'),
                ('company:b','20-F','foreign-disclosure-6k'),('company:c','10-K','quarterly-report-10q')]:
            spec,table,error,filings=fixture(company,form)
            p=plans(spec,table,error,filings=filings)
            self.assertEqual(len(p),1)
            self.assertIsNone(p[0]['period'])
            self.assertEqual(TargetedSourceRung('sec_edgar').spec_for(p[0]),expected)
            proof=p[0]['acceptance']['statement_history_gap']
            self.assertEqual(proof['spec_hash'],spec['content_hash'])
            self.assertTrue(all(x['required_quarters']==4 and len(x['held_quarter_ends'])==1
                                for x in proof['missing_history']))
            self.assertEqual(proof['source_filings'][0]['form'],form)
            self.assertNotIn('period_end',proof)

    def test_complete_history_or_nontyped_failure_does_not_invent_gap(self):
        spec,table,error,filings=fixture(missing=False)
        # A forged not-ready exception carrying a genuinely ready replay rejects.
        with self.assertRaises(ValueError):plans(spec,table,error,filings=filings)
        self.assertEqual(plans(spec,table,FinancialStatementStructureError('unready'),filings=filings),[])

    def test_annual_values_not_counted_as_quarters_and_eps_history_not_imputed(self):
        spec,table,error,filings=fixture()
        for l in table['filed_lines']:
            old=next(iter(l['cells'].values()))
            l['cells']={'2025-12-31':{**old,'period_start':'2025-01-01'}}
        # Annual income formula cannot be treated as a valid quarter tie.
        cost=next(l for l in table['filed_lines'] if l['concept']=='cost')
        cell=next(iter(cost['cells'].values()));cost['cells']={'2024-12-31':{**cell,'period_start':'2024-01-01'}}
        structure,replay=materialize_financial_statement_structure(spec,table)
        result=plans(spec,table,FinancialStatementStructureNotReady(structure,replay),filings=filings)
        missing=result[0]['acceptance']['statement_history_gap']['missing_history']
        self.assertTrue(all(not x['held_quarter_ends'] for x in missing))
        self.assertTrue(any(x['role']=='diluted_weighted_average_shares' for x in missing))

    def test_changed_input_replay_company_or_source_binding_refused(self):
        for change in ('input','replay','company','source','hash'):
            spec,table,error,filings=fixture()
            if change=='input':table['filed_lines'][0]['cells']={'2025-03-31':{'period_start':'2025-01-01','value':'1','unit':'usd','basis':'reported','source_accessions':[f.ACCESSION]}}
            elif change=='replay':error.replay['ready_for_forecast']=True
            elif change=='company':spec['company_ref']='company:other'
            elif change=='source':filings[0]['company_ref']='company:other'
            else:error.structure['content_hash']='f'*64
            with self.subTest(change=change),self.assertRaises(ValueError):plans(spec,table,error,filings=filings)

    def test_half_year_and_short_stub_never_count_as_quarters(self):
        for start,end in [('2025-01-01','2025-06-30'),('2025-01-01','2025-03-15')]:
            spec,table,error,filings=fixture()
            for l in table['filed_lines']:
                old=next(iter(l['cells'].values()))
                l['cells']={end:{**old,'period_start':start}}
            cost=next(l for l in table['filed_lines'] if l['concept']=='cost')
            cell=next(iter(cost['cells'].values()))
            cost['cells']={'2024-06-30':{**cell,'period_start':'2024-01-01'}}
            structure,replay=materialize_financial_statement_structure(spec,table)
            result=plans(spec,table,FinancialStatementStructureNotReady(structure,replay),filings=filings)
            self.assertTrue(all(not x['held_quarter_ends'] for x in
                result[0]['acceptance']['statement_history_gap']['missing_history']))

    def test_strong_binding_error_remains_not_ready_and_material_open_is_idempotent(self):
        spec,table,error,filings=fixture()
        with self.assertRaises(FinancialStatementStructureNotReady):
            forecast_structure_binding(error.structure,error.replay,table)
        with tempfile.TemporaryDirectory() as state:
            for _ in range(2):
                open_gap_requests(state,spec,table,structure_failure=error,source_filings=filings)
            typed=[x for x in open_requests(state) if x['acceptance'].get('statement_history_gap')]
            self.assertEqual(len(typed),1)
            self.assertEqual(typed[0]['need']['kind'],'document')
            self.assertEqual(typed[0]['need']['doc_kinds'],['sec_6k'])

    def test_normal_forecast_coordinator_opens_gap_and_continues_second_company_without_paid_call(self):
        spec,table,error,filings=fixture()
        second={**spec,'company_ref':'company:other','spec_id':'spec:other'}
        models=SimpleNamespace(latest=lambda _:None,connection=None)
        mission={'universe':[{'company_ref':spec['company_ref']},{'company_ref':second['company_ref']}]}
        missions=SimpleNamespace(latest_company_model_spec=lambda ref:spec if ref==spec['company_ref'] else second,
                                 statement_filings=lambda _:filings)
        with tempfile.TemporaryDirectory() as state:
            launcher=SimpleNamespace(state_dir=state,start=lambda **kw:{'id':'fixture:ticket'})
            lane=MissionModelForecastLaneCoordinator(missions=missions,models=models,launcher=launcher,
                mission=lambda:mission,failure_ledger_dir=state)
            with patch('dalton_core.mission_model_forecast_lane.pending_companies',return_value=[
                    (spec['company_ref'],spec,table),(second['company_ref'],second,table)]), \
                 patch('dalton_core.mission_model_forecast_lane.model_digest',side_effect=[error,'digest']), \
                 patch('dalton_core.mission_model_forecast_lane.forecast_reporting_boundary',return_value={}), \
                 patch('dalton_core.mission_model_forecast_lane.current_permission',return_value='permission'), \
                 patch.object(lane.budget,'blocked',return_value=None):
                outcome=lane.dispatch_once()
            self.assertEqual(outcome['status'],'launched')
            self.assertEqual(outcome['company_ref'],'company:other')
            typed=[x for x in open_requests(state) if x['acceptance'].get('statement_history_gap')]
            self.assertEqual(len(typed),1)
            self.assertEqual(typed[0]['subject'],spec['company_ref'])

    def test_acquired_cover_never_satisfies_quarter_history_request(self):
        spec,table,error,filings=fixture()
        with tempfile.TemporaryDirectory() as state:
            open_gap_requests(state,spec,table,structure_failure=error,source_filings=filings)
            request=next(x for x in open_requests(state) if x['acceptance'].get('statement_history_gap'))
            with patch('dalton_core.evidence_index.documents',return_value=[{'kind':'sec_6k','text':'issuer cover'}]) as docs:
                result=evaluate_acceptance(state,request,identity={'name':'issuer','aliases':[]})
            self.assertFalse(result['met']); docs.assert_not_called()

    def test_completion_checks_full_current_formal_history_and_refuses_different_spec(self):
        import sqlite3
        from contextlib import contextmanager
        from pathlib import Path
        from dalton_core.forecast_statement_history_gaps import requirement_status
        spec,table,error,filings=fixture()
        plan=plans(spec,table,error,filings=filings)[0]
        req={'subject':spec['company_ref'],'need':plan['need'],'acceptance':plan['acceptance']}
        @contextmanager
        def read(_):
            c=sqlite3.connect(':memory:');c.execute('PRAGMA query_only=ON')
            try:yield c
            finally:c.close()
        with tempfile.TemporaryDirectory() as state:
            Path(state,'core.sqlite').touch()
            complete=f.financial_inputs()
            with patch('dalton_core.planner_paid_output_recovery._authority_connection',side_effect=read), \
                 patch('dalton_core.coverage_mission.CoverageMissionAuthority.latest_company_model_spec',return_value=spec), \
                 patch('dalton_core.company_model_inputs.build_model_inputs',return_value=complete) as builder:
                result=requirement_status(state,req)
                self.assertTrue(result['met'])
                self.assertEqual(result['items'][0]['kind'],'verified_statement_history')
                builder.assert_called_once()
            with patch('dalton_core.planner_paid_output_recovery._authority_connection',side_effect=read), \
                 patch('dalton_core.coverage_mission.CoverageMissionAuthority.latest_company_model_spec',return_value={**spec,'spec_id':'spec:new'}), \
                 patch('dalton_core.company_model_inputs.build_model_inputs') as builder:
                self.assertFalse(requirement_status(state,req)['met']);builder.assert_not_called()

    def test_changed_strong_history_creates_new_request_without_resetting_old_record(self):
        spec,table,error,filings=fixture()
        with tempfile.TemporaryDirectory() as state:
            open_gap_requests(state,spec,table,structure_failure=error,source_filings=filings)
            before=next(x for x in open_requests(state) if x['acceptance'].get('statement_history_gap'))
            newer={**spec,'spec_id':'spec:new','content_hash':'e'*64}
            table={**table,'spec_ref':newer['spec_id']}
            structure,replay=materialize_financial_statement_structure(newer,table)
            open_gap_requests(state,newer,table,structure_failure=FinancialStatementStructureNotReady(structure,replay),source_filings=filings)
            typed=[x for x in open_requests(state) if x['acceptance'].get('statement_history_gap')]
            self.assertEqual(len(typed),2)
            self.assertEqual(next(x for x in typed if x['id']==before['id'])['content_hash'],before['content_hash'])

    def test_four_old_periods_do_not_fill_new_target_hole_and_disjoint_windows_do_not_close(self):
        from dalton_core.forecast_statement_history_gaps import _missing
        spec,table,error,filings=fixture(missing=False)
        cost=next(l for l in table['filed_lines'] if l['concept']=='cost')
        cost['cells']['2024-12-31']={**cost['cells'].pop('2025-12-31'),'period_start':'2024-10-01'}
        missing=_missing(error.structure,table,4)
        self.assertEqual(next(x for x in missing if x['concept']=='cost')['held_quarter_ends'],
                         ['2025-03-31','2025-06-30','2025-09-30'])
        spec,table,error,filings=fixture(missing=False)
        for line in table['filed_lines']:
            line['cells']['2023-12-31']={**line['cells'].pop('2025-03-31'),'period_start':'2023-10-01'}
        missing=_missing(error.structure,table,4)
        self.assertTrue(missing)
        self.assertTrue(all(not x['quarter_periods_coherent'] for x in missing))


if __name__=='__main__':unittest.main()
