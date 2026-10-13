"""Closed taxonomy roles do not turn labels, dimensions or annuals into facts."""
import copy
import json
import unittest
from dalton_core.company_model_spec import spec_from_response, CompanyModelSpecError, build_prompt
from dalton_core.model_forecast_driver import (
    build_drivers, CONCEPT_ROLES, IFRS_INCOME_CONCEPT_ROLES,
    INCOME_TAXONOMY_ROLE_REF, ROLE_STATEMENTS, REVENUE, revenue_anchor, ForecastModelError,
)
from dalton_core.company_model_series import quarterly_series
from dalton_core.company_model_cli import model_spec_validation_contract
from tests.test_company_model_spec import STATE, _spec, DECIDED_BY


class IFRSIncomeRolesTests(unittest.TestCase):
    def state_and_body(self, concept, company='company:ticker:foreign-a'):
        def change(value):
            if isinstance(value, dict):return {k:change(v)for k,v in value.items()}
            if isinstance(value, list):return [change(v)for v in value]
            return concept if value=='us-gaap:Revenues'else value
        state,body=change(copy.deepcopy(STATE)),change(_spec())
        state['company_ref']=company
        return state,body

    def test_two_foreign_companies_exact_revenue_concepts(self):
        for company,concept in [('company:ticker:foreign-a','ifrs-full:Revenue'),
                                ('company:ticker:foreign-b','ifrs-full:RevenueFromContractsWithCustomers')]:
            state,body=self.state_and_body(concept,company)
            raw=json.dumps(body);prompt=build_prompt(state)
            result=spec_from_response(state,raw,decided_by=DECIDED_BY)
            self.assertEqual(result['revenue_anchor_concept'],concept)
            self.assertEqual(result['company_ref'],company)
            self.assertEqual(build_prompt(state),prompt)
            self.assertEqual(json.dumps(body),raw)

    def test_extension_and_similar_names_are_not_automatic_roles(self):
        for concept in ['issuer:Revenue','ifrs-full:RevenueNet','IFRS-full:Revenue','ifrs-full:RevenueFromContractsWithCustomer']:
            state,body=self.state_and_body(concept)
            with self.assertRaisesRegex(CompanyModelSpecError,'consolidated filed revenue'):
                spec_from_response(state,json.dumps(body),decided_by=DECIDED_BY)

    def test_dimensional_or_wrong_statement_anchor_is_still_refused(self):
        for mode in ['dimensional','cash','missing']:
            state,body=self.state_and_body('ifrs-full:Revenue')
            income=state['statements']['income']
            if mode=='cash':state['statements']['cash']=income;state['statements']['income']=[]
            elif mode=='missing':state['statements']['income']=[r for r in income if r['concept']!='ifrs-full:Revenue']
            else:
                for row in income:
                    if row['concept']=='ifrs-full:Revenue':row['is_breakdown']=True;row['dimension_axis']='ifrs-full:SegmentsAxis'
            with self.assertRaisesRegex(CompanyModelSpecError,'consolidated filed revenue'):
                spec_from_response(state,json.dumps(body),decided_by=DECIDED_BY)

    def test_driver_role_preserves_original_identity_amount_unit(self):
        for concept,role in IFRS_INCOME_CONCEPT_ROLES.items():
            statement=ROLE_STATEMENTS[role]
            source={'concept':concept,'statement':statement,'status':'filed','cells':{
                '2025-03-31':{'period_start':'2025-01-01','period_end':'2025-03-31','value':'12','unit':'eur','basis':'reported','source_accessions':['exact-a']}}}
            table={'rows':[{'ref':'leaf','kind':'revenue_anchor'if role==REVENUE else'expense_line','basis_concept':concept}],
                   'filed_lines':[source]}
            d=build_drivers(table)[0]
            self.assertEqual((d['concept'],d['role'],d['unit']),(concept,role,'eur'))
            self.assertEqual(d['history'][0]['value'],'12')
            self.assertEqual(d['history'][0]['accessions'],['exact-a'])
            source['statement']='balance'
            self.assertIsNone(build_drivers(table)[0]['role'])

    def test_annual_revenue_does_not_create_quarters(self):
        rows=[{'period_start':'2025-01-01','period_end':'2025-12-31','value':'100','unit':'usd','accession':'a','form':'40-F','filed':'2026-03-01'}]
        result=quarterly_series([{**r,'concept':'ifrs-full:RevenueFromContractsWithCustomers'}for r in rows])
        self.assertFalse(result['quarters'])

    def test_old_domestic_parser_and_frozen_markers_remain(self):
        result=spec_from_response(copy.deepcopy(STATE),json.dumps(_spec()),decided_by=DECIDED_BY)
        self.assertEqual(result['revenue_anchor_concept'],'us-gaap:Revenues')
        self.assertEqual(CONCEPT_ROLES['us-gaap:Revenues'],REVENUE)
        self.assertEqual(model_spec_validation_contract()['income_taxonomy_roles'],INCOME_TAXONOMY_ROLE_REF)

    def test_equal_amounts_do_not_merge_distinct_ifrs_metrics(self):
        concepts=['ifrs-full:Revenue','ifrs-full:RevenueFromContractsWithCustomers']
        table={'rows':[{'ref':str(i),'kind':'revenue_anchor','basis_concept':concept}for i,concept in enumerate(concepts)],
               'filed_lines':[{'concept':c,'statement':'income','status':'filed','cells':{
                    '2025-03-31':{'period_start':'2025-01-01','period_end':'2025-03-31','value':'12','unit':'eur','basis':'reported','source_accessions':['a']}}}for c in concepts]}
        drivers=build_drivers(table)
        self.assertEqual([d['concept']for d in drivers],concepts)
        with self.assertRaises(ForecastModelError):revenue_anchor(drivers)
