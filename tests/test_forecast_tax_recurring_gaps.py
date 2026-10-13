"""Cross-company source-bound forecast execution and acquisition regression."""
import copy
import unittest
from decimal import Decimal
from dalton_core.forecast_coverage_assumptions import coverage_structure_assumptions
from dalton_core.forecast_financial_execution import (ASSUMPTION_EXECUTION_REF,
    driver_execution,execute_financial_model,validate_execution_contract)
from dalton_core.forecast_material_gaps import gap_plans
from dalton_core.company_model_forecast import pending_action
from dalton_core.store import content_hash
from tests.test_forecast_financial_execution import economic_model

ENDS=['2024-03-31','2024-06-30','2024-09-30','2024-12-31',
      '2025-03-31','2025-06-30','2025-09-30','2025-12-31']

def tax_fixture(taxes=None):
    values=[100,100,100,100,100,100,-50,100]
    taxes=taxes or [20,20,20,20,20,20,-10,20]
    drivers=[];lines=[]
    for ref,role,data,method,base in [('pretax','pretax_income',values,'unavailable',None),
            ('tax','income_tax_expense',taxes,'share_of_line','pretax'),
            ('net','net_income',values,'unavailable',None),
            ('nci','noncontrolling_interest',[10]*8,'share_of_line','net')]:
        lines.append({'ref':ref,'role':role,'kind':'filed','forecast_method':method,'forecast_base_ref':base,'concept':ref,'unit':'usd'})
        drivers.append({'ref':ref,'structure_line_ref':ref,'role':role,'concept':ref,
            'statement':'income','unit':'usd','status':'filed',
            'history':[{'concept':ref,'period_start':end[:4]+'-01-01','period_end':end,
                        'value':str(value),'accessions':['filing:'+end]}for end,value in zip(ENDS,data)]})
    period={'start':'2026-01-01','end':'2026-03-31','calendar':'company:fiscal','kind':'quarter'}
    return drivers,[period],{'lines':lines,'formulas':[]}

def recurring_fixture():
    body,spec=economic_model()
    d=next(d for d in body['drivers'] if d['concept']=='opex')
    old=copy.deepcopy(d['history'])
    d['history']=[{**c,'period_start':c['period_start'].replace('2025','2024'),
        'period_end':c['period_end'].replace('2025','2024')}for c in old]+old
    spec['expense_lines']=[{'ref':'recurring-charge','behaviour':'one_off','basis_concept':'opex'}]
    body.pop('execution_contract',None)
    return body,spec

class ForecastTaxRecurringGapsTests(unittest.TestCase):
    def test_tax_mixed_pretax_signs_reach_existing_rate_rule_with_original_refs(self):
        drivers,periods,structure=tax_fixture();original=copy.deepcopy((drivers,periods,structure))
        actual=coverage_structure_assumptions(drivers,periods,structure,decided_by='automation:test')
        tax=next(a for a in actual if a['driver_ref']=='tax')
        self.assertEqual(Decimal(tax['value']),Decimal('0.2'))
        self.assertTrue(any(r['concept']=='tax'for r in tax['refs']))
        self.assertTrue(any(r['concept']=='pretax'for r in tax['refs']))
        self.assertFalse(any(a['driver_ref']=='nci'for a in actual))
        self.assertEqual((drivers,periods,structure),original)

    def test_discrete_tax_and_invalid_rates_keep_existing_bounded_rules(self):
        for values,expected in [([20,20,20,20,20,20,-10,80],Decimal('.2')),
                                ([-20]*8,None),([200]*8,None)]:
            drivers,periods,structure=tax_fixture(values)
            actual=[a for a in coverage_structure_assumptions(drivers,periods,structure,
                decided_by='automation:test')if a['driver_ref']=='tax']
            self.assertEqual(None if not actual else Decimal(actual[0]['value']),expected)

    def test_recurring_source_proof_executes_without_prose_keyword_for_multiple_companies(self):
        for company in ('company:alpha','company:beta'):
            body,spec=recurring_fixture();body['company_ref']=company
            original=copy.deepcopy(body['drivers'])
            execute_financial_model(body,spec)
            report=body['execution_contract']['drivers']
            row=next(r for r in report['drivers'] if r['ref']=='recurring-charge')
            self.assertEqual(row['status'],'executed')
            self.assertEqual(row['recurring_source_proof']['contract_ref'],ASSUMPTION_EXECUTION_REF)
            self.assertEqual(body['drivers'],original)
            validate_execution_contract(body,publishing=True)

    def test_missing_source_nonrecurring_or_forecast_cells_cannot_claim_proof(self):
        for kind in ('few','rare','missing_result','method_mismatch','no_refs'):
            body,spec=recurring_fixture();d=next(d for d in body['drivers']if d['concept']=='opex')
            if kind=='few':d['history']=d['history'][-4:]
            if kind=='rare':
                for c in d['history'][:5]:c['value']='0'
            if kind=='missing_result':
                result=next(r for r in body['results']if r['role']=='operating_expense');result['cells']=[]
            if kind=='method_mismatch':d['forecast_method']='quarterly_growth'
            if kind=='no_refs':
                for a in body['assumptions']:
                    if a['driver_ref']==d['ref']:a['refs']=[]
            report,_=driver_execution(body,spec,assumption_policy=ASSUMPTION_EXECUTION_REF)
            self.assertEqual(next(r for r in report['drivers']if r['ref']=='recurring-charge')['status'],'unexecuted',kind)

    def test_legacy_contract_replay_preserves_original_report_and_hash(self):
        body,spec=recurring_fixture()
        body['execution_contract']={} # Existing unmarked execution uses its old branch.
        execute_financial_model(body,spec);before=copy.deepcopy(body)
        self.assertNotIn('assumption_execution_ref',body['execution_contract'])
        self.assertEqual(body['execution_contract']['drivers']['status'],'unexecuted')
        validate_execution_contract(body,publishing=False)
        execute_financial_model(body,spec)
        self.assertEqual(body,before)

    def test_nci_and_missing_share_quarters_have_distinct_governed_source_requests(self):
        spec={'company_ref':'company:alpha','spec_id':'spec:current','content_hash':'a'*64,'revenue_drivers':[]}
        history=[{'period_end':end,'period_start':end[:4]+'-01-01'}for end in ENDS if end not in ('2024-12-31','2025-12-31')]
        record={'spec_ref':spec['spec_id'],'spec_hash':spec['content_hash'],'history_periods':ENDS,'drivers':[{'ref':'shares','role':'diluted_weighted_average_shares','concept':'us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding','history':history}],
            'results':[{'status':'unavailable','role':'noncontrolling_interest','reason':'Mixed sign net income base'},
                       {'status':'unavailable','role':'diluted_weighted_average_shares','reason':'Quarter base absent'},
                       {'status':'unavailable','role':'capital_expenditure','reason':'No declared cash assumption'}]}
        original=copy.deepcopy(record);plans=gap_plans(spec,{},record=record);bykey={p['key']:p for p in plans}
        self.assertIn('noncontrolling_interest_basis',bykey);self.assertIn('capital_expenditure_basis',bykey)
        for end in ('2024-12-31','2025-12-31'):
            plan=bykey['diluted_share_history:'+end]
            self.assertEqual(plan['period'],'quarter_ending:'+end)
            self.assertEqual(plan['acceptance']['forecast_gap_identity']['spec_hash'],spec['content_hash'])
            self.assertIn('do not infer Q4',plan['need']['question'])
            self.assertEqual(plan['need']['doc_kinds'],['sec_10k','sec_10q'])
        self.assertEqual(record,original)
        for field in ('spec_ref','spec_hash'):
            stale=copy.deepcopy(record);stale[field]='wrong'
            self.assertFalse(any(p['key'].startswith('diluted_share_history:')
                                 for p in gap_plans(spec,{},record=stale)))

    def test_unknown_rule_and_rehashed_fake_recurring_proof_fail_replay(self):
        from dalton_core.model_forecast_driver import ForecastModelValidationError
        body,spec=recurring_fixture();execute_financial_model(body,spec)
        forged=copy.deepcopy(body);contract=forged['execution_contract']
        row=next(r for r in contract['drivers']['drivers']if r['ref']=='recurring-charge')
        row['recurring_source_proof']['source_history_hash']='0'*64
        row['recurring_source_proof']['content_hash']=content_hash({k:v for k,v in row['recurring_source_proof'].items()if k!='content_hash'})
        contract['content_hash']=content_hash({k:v for k,v in contract.items()if k!='content_hash'})
        with self.assertRaisesRegex(ForecastModelValidationError,'does not replay'):
            validate_execution_contract(forged,publishing=True)
        contract['assumption_execution_ref']='unknown:999'
        contract['content_hash']=content_hash({k:v for k,v in contract.items()if k!='content_hash'})
        with self.assertRaisesRegex(ForecastModelValidationError,'unsupported'):
            validate_execution_contract(forged)

    def test_current_good_legacy_model_does_not_rebuild_but_held_recurring_rule_does_once(self):
        body,spec=economic_model();body['execution_contract'].pop('assumption_execution_ref')
        body['execution_contract']['content_hash']=content_hash({k:v for k,v in body['execution_contract'].items()if k!='content_hash'})
        body['spec_ref']=spec['spec_id']
        self.assertIsNone(pending_action(body,spec,{'periods':[]}))
        old,spec=recurring_fixture();old['execution_contract']={};execute_financial_model(old,spec)
        old['spec_ref']=spec['spec_id']
        self.assertEqual(pending_action(old,spec,{'periods':[]}),'rules_upgrade')
        new=copy.deepcopy(old);new.pop('execution_contract');execute_financial_model(new,spec)
        self.assertIsNone(pending_action(new,spec,{'periods':[]}))

    def test_intrinsically_oneoff_concept_is_never_reclassified_as_recurring(self):
        from dalton_core.forecast_coverage import ONE_OFF_CONCEPTS
        body,spec=recurring_fixture();concept=sorted(ONE_OFF_CONCEPTS)[0]
        d=next(d for d in body['drivers']if d['concept']=='opex');d['concept']=concept
        for c in d['history']:c['concept']=concept
        line=next(l for l in body['financial_statement_structure']['lines']if l['ref']=='opex');line['concept']=concept
        spec['expense_lines'][0]['basis_concept']=concept
        report,_=driver_execution(body,spec,assumption_policy=ASSUMPTION_EXECUTION_REF)
        self.assertEqual(next(r for r in report['drivers']if r['ref']=='recurring-charge')['status'],'unexecuted')
