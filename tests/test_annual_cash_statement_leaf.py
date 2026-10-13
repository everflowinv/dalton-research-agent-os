import copy
import unittest
from dalton_core.annual_cash_statement_leaf import validate_source
from dalton_core.company_financial_statement_structure import (
    _normalize_statement_leaf, FinancialStatementStructureError,
    validate_structure_proposal, ANNUAL_CASH_SCHEMA_VERSION,
)
from dalton_core.forecast_coverage import complete_methods_by_role
from tests.test_company_model_spec import STATE,_statement_structure


class AnnualCashLeafTests(unittest.TestCase):
    def source(self, unit='usd'):
        return {'statement':'cash','status':'filed','period_basis':'duration','cells':{},'duration_facts':[
            {'period_start':'2025-01-01','period_end':'2025-12-31','value':'12','unit':unit,'source_accessions':['annual-a']} ]}

    def line(self):
        return {'ref':'buyback','role':'share_repurchase','label':'buybacks','kind':'filed',
                'concept':'issuer:AnnualRepurchases','statement':'cash','period_kind':'duration',
                'unit':'usd','annual_semantics':'direct_annual','forecast_method':'unavailable',
                'forecast_base_ref':None,'annual_forecast_method':None}

    def test_two_issuers_annual_facts_are_not_quarters(self):
        for concept in ['issuer-a:Repurchases','issuer-b:AnnualRepurchases']:
            source=self.source();before=copy.deepcopy(source);line=self.line();line['concept']=concept
            _normalize_statement_leaf(line,0,{concept:source},schema_version='0.7')
            self.assertEqual(source,before);self.assertEqual(source['cells'],{})
            self.assertEqual(line['annual_semantics'],'direct_annual')
            self.assertEqual(line['forecast_method'],'unavailable')

    def test_old_06_requires_original_sum_quarters(self):
        with self.assertRaisesRegex(FinancialStatementStructureError,'sum_quarters'):
            _normalize_statement_leaf(self.line(),0,{'issuer:AnnualRepurchases':self.source()},schema_version='0.6')

    def test_wrong_unit_missing_annual_nonfinite_and_sources_refuse(self):
        for change in ['unit','quarter','unknown','evidence','conflict']:
            source=self.source();fact=source['duration_facts'][0]
            if change=='unit':fact['unit']='eur'
            elif change=='quarter':fact['period_end']='2025-03-31'
            elif change=='unknown':fact['value']='NaN'
            elif change=='evidence':fact['source_accessions']=[]
            else:source['duration_facts'].append({**fact,'value':'13'})
            with self.assertRaises(FinancialStatementStructureError):validate_source(source,unit='usd',concept='a')

    def test_statement_period_and_forecast_base_gates_remain(self):
        for field,value in [('statement','income'),('period_kind','instant'),('forecast_base_ref','revenue')]:
            line=self.line();line[field]=value
            with self.assertRaises(FinancialStatementStructureError):
                _normalize_statement_leaf(line,0,{'issuer:AnnualRepurchases':self.source()},schema_version='0.7')

    def test_proposal_gets_new_version_without_raw_mutation(self):
        state=copy.deepcopy(STATE);line=self.line();definition=_statement_structure()
        definition['schema_version']='0.6';definition['lines'].append(line)
        for row in definition['lines']:row.setdefault('annual_forecast_method',None)
        state['statements']['cash']=[{'concept':line['concept'],'label':'Annual cash','is_breakdown':False,
            'dimension_axis':None,'unit':'USD','period_kind':'duration'}]
        original=copy.deepcopy(definition)
        result=validate_structure_proposal(definition,state,revenue_anchor_concept='us-gaap:Revenues',expense_lines=[])
        self.assertEqual(result['schema_version'],ANNUAL_CASH_SCHEMA_VERSION)
        self.assertEqual(definition,original)
        annual=next(r for r in result['lines']if r['ref']=='buyback')
        self.assertEqual(annual['forecast_method'],'unavailable')
        self.assertEqual(complete_methods_by_role(result)['lines'],result['lines'])

    def test_materialize_build_and_stored_forecast_keep_missing_quarter(self):
        from tests.test_forecast_coverage import load,materialize,build,stored
        data=load();definition=data['spec']['financial_statement_structure']
        definition['schema_version']='0.7'
        leaf=self.line();leaf['concept']='us-gaap:PaymentsForRepurchaseOfCommonStock';definition['lines'].append(leaf)
        leaf.update(annual_semantics='direct_annual',forecast_method='unavailable',forecast_base_ref=None)
        source=next(x for x in data['table']['filed_lines']if x['concept']==leaf['concept'])
        source['cells']={};source['duration_facts']=self.source()['duration_facts']
        structure,replay,binding=materialize(data)
        self.assertEqual(structure['schema_version'],'0.7')
        self.assertEqual(replay['schema_version'],'financial-statement-structure-replay-0.7')
        self.assertEqual(binding['schema_version'],'forecast-statement-structure-binding-0.7')
        result=stored(build(data))
        candidates=[x for x in result['drivers']if x.get('concept')==leaf['concept']]
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]['history'],[])
        values=next(r['cells']for r in result['results']if r['ref']=='result:share_repurchase')
        self.assertTrue(values)
        self.assertTrue(all(x['status']=='unavailable'and x.get('value')is None for x in values))

    def test_07_ifrs_share_averages_do_not_create_fourth_quarter(self):
        from dalton_core.company_model_series import quarterly_series
        for concept in ['ifrs-full:WeightedAverageShares','ifrs-full:AdjustedWeightedAverageShares']:
            facts=[{'concept':concept,'period_start':'2025-01-01','period_end':end,'value':value,'unit':'shares','accession':'a','filed':'2026-03-01'}
                   for end,value in [('2025-09-30','100'),('2025-12-31','110')]]
            self.assertTrue(quarterly_series(facts)['quarters'])  # frozen old policy
            current=quarterly_series(facts,ifrs_non_additive_duration_policy=True)
            self.assertEqual(current['quarters'],[])
            self.assertEqual(len(current['durations']),2)

    def test_normalized_spec_storage_and_reparse_preserve_07(self):
        import json
        from dalton_core.company_model_spec import spec_from_response
        from tests.test_company_model_spec import _spec, DECIDED_BY, CompanyModelSpecStorageTests
        owner=CompanyModelSpecStorageTests();owner.setUp();self.addCleanup(owner.doCleanups)
        state=copy.deepcopy(STATE);body=_spec();line=self.line()
        body['financial_statement_structure']['schema_version']='0.6'
        body['financial_statement_structure']['lines'].append(line)
        for row in body['financial_statement_structure']['lines']:row.setdefault('annual_forecast_method',None)
        state['statements']['cash']=[{'concept':line['concept'],'label':'Annual cash','is_breakdown':False,
            'dimension_axis':None,'unit':'USD','period_kind':'duration'}]
        spec=spec_from_response(state,json.dumps(body),decided_by=DECIDED_BY)
        owner.record(spec)
        held=owner.missions.latest_company_model_spec(spec['company_ref'])
        self.assertEqual(held['financial_statement_structure'],spec['financial_statement_structure'])
        self.assertEqual(held['financial_statement_structure']['schema_version'],'0.7')
        reparse=copy.deepcopy(body);reparse['financial_statement_structure']=held['financial_statement_structure']
        read_spec=spec_from_response(state,json.dumps(reparse),decided_by=DECIDED_BY)
        self.assertEqual(read_spec,spec)
