"""Source-complete expense subtotals and non-calendar fiscal quarter regression."""
import copy
from calendar import monthrange
from datetime import date
from decimal import Decimal
import unittest

from tests.test_filed_period_composition import fixture, LOSS
from dalton_core.filed_period_composition import derive, verify, LEGACY_CONTRACT_REF, FiledCompositionError
from dalton_core.store import content_hash


def cumulative_fixture(offset=0, expense=False, company='company:fiscal-example'):
    spec, inputs, sources = fixture()
    spec['company_ref'] = inputs['company_ref'] = sources[0]['company_ref'] = company
    by = {line['concept']: line for line in inputs['filed_lines']}
    target = by['operating']; end = '2023-12-31'
    target['cells'][end]['basis'] = 'derived_from_cumulative'
    rows = sources[0]['rows']; templates = [r for r in rows if r['period_end'] == end]
    rows[:] = [r for r in rows if r['period_end'] != end]
    for upto in ('2023-09-30', end):
        for template in templates:
            value = sum((Decimal(v['value']) for e, v in by[template['concept']]['cells'].items()
                         if e.startswith('2023') and e <= upto), Decimal(0))
            rows.append({**template, 'period_start':'2023-01-01', 'period_end':upto,
                         'value':str(value), 'line_id':template['line_id']+upto})
        rows.append({**rows[-1], 'concept':LOSS, 'value':'-10', 'line_id':'loss:'+upto})
    def shift(value):
        d=date.fromisoformat(value); count=d.year*12+d.month-1+offset; y,m=divmod(count,12);m+=1
        day=monthrange(y,m)[1] if d.day==monthrange(d.year,d.month)[1] else d.day
        return date(y,m,day).isoformat()
    for line in inputs['filed_lines']:
        line['cells']={shift(end):{**cell,'period_start':shift(cell['period_start'])}
                       for end,cell in line['cells'].items()}
    inputs['periods']=[shift(p) for p in inputs['periods']]
    for row in rows:
        row['period_start']=shift(row['period_start']);row['period_end']=shift(row['period_end'])
    if expense:
        concept='us-gaap:CostsAndExpenses'
        target['concept']=concept
        for row in rows:
            if row['concept']=='operating':row['concept']=concept
            if row.get('parent_concept')=='operating':row['parent_concept']=concept
        for line in spec['financial_statement_structure']['lines']:
            if line['ref']=='operating':line['role']='company_presented_subtotal'
        for formula in spec['financial_statement_structure']['formulas']:
            if formula['output_ref']=='operating':formula['tie_out_concept']=concept
    target['source_evidence']={'rows':[r for r in rows if r['concept']==target['concept']]}
    sources[0]['rows_hash']=content_hash(rows)
    return spec,inputs,sources


class FiledExpenseFiscalCompositionTests(unittest.TestCase):
    def test_multiple_fiscal_years_and_companies_keep_every_history_period(self):
        for offset,company in ((3,'company:alpha'),(6,'company:beta'),(9,'company:gamma')):
            with self.subTest(offset=offset):
                spec,inputs,sources=cumulative_fixture(offset,True,company)
                original=copy.deepcopy((spec,inputs,sources));proof=derive(spec,inputs,sources)
                self.assertIsNotNone(proof);self.assertEqual(len(proof['periods']),12)
                self.assertEqual(proof['one_off_coefficients'],{LOSS:'1'})
                self.assertEqual(verify(proof,spec,inputs),proof)
                self.assertEqual((spec,inputs,sources),original)
                self.assertIsNone(derive(spec,inputs,sources,contract_ref=LEGACY_CONTRACT_REF))

    def test_operating_income_cross_calendar_year_and_calendar_year(self):
        for offset in (0,3,9):
            spec,inputs,sources=cumulative_fixture(offset)
            self.assertEqual(len(derive(spec,inputs,sources)['periods']),12)

    def test_legacy_proof_remains_byte_identical(self):
        spec,inputs,sources=fixture();proof=derive(spec,inputs,sources,contract_ref=LEGACY_CONTRACT_REF)
        self.assertEqual(proof['content_hash'],'8331727d0d0011cd8f279f16b6f10fc61335f603523f23c0035e890f42cd08bb')
        self.assertEqual(verify(proof,spec,inputs),proof)

    def test_foreign_parent_or_unknown_subtotal_not_admitted(self):
        spec,inputs,sources=cumulative_fixture(3,True)
        formula=next(f for f in spec['financial_statement_structure']['formulas'] if f['output_ref']=='operating')
        formula['tie_out_concept']='issuer:unexplained_subtotal'
        self.assertIsNone(derive(spec,inputs,sources))

    def test_nonadjacent_or_different_fiscal_starts_do_not_bridge(self):
        for mutate in ('gap','different_start','multi_year'):
            spec,inputs,sources=cumulative_fixture(3,True)
            for row in sources[0]['rows']:
                if row['period_start']=='2023-04-01' and row['period_end']=='2023-12-31':
                    if mutate=='gap':row['period_end']='2023-12-30'
                    if mutate=='different_start':row['period_start']='2023-04-02'
                if mutate=='multi_year' and row['period_start']=='2023-04-01':row['period_start']='2022-04-01'
            sources[0]['rows_hash']=content_hash(sources[0]['rows'])
            self.assertIsNone(derive(spec,inputs,sources),mutate)

    def test_unknown_proof_contract_refuses_even_when_rehashed(self):
        spec,inputs,sources=cumulative_fixture(3,True);proof=derive(spec,inputs,sources)
        proof['contract_ref']='filed-period-income-composition:999'
        proof['content_hash']=content_hash({k:v for k,v in proof.items() if k!='content_hash'})
        with self.assertRaises(FiledCompositionError):verify(proof,spec,inputs)


    def test_new_historical_replay_preserves_filed_one_off_in_dependent_profit(self):
        from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure
        from dalton_core.model_forecast_driver import build_structure_drivers, structure_historical_values
        spec,inputs,sources=fixture()
        for contract,expected in ((LEGACY_CONTRACT_REF,Decimal('240')),('filed-period-income-composition:0.2',Decimal('230'))):
            inputs['historical_income_composition']=derive(spec,inputs,sources,contract_ref=contract)
            structure,replay=materialize_financial_statement_structure(spec,inputs,complete=False)
            drivers=build_structure_drivers(inputs,structure)
            values,refs=structure_historical_values(drivers,structure)
            # The operating source row is 230; the ordinary formula alone is 240.
            self.assertEqual(values['operating']['2023-09-30'],expected)
            self.assertTrue(refs['operating']['2023-09-30'])
            if contract != LEGACY_CONTRACT_REF:
                filed={l['concept']:l for l in inputs['filed_lines']}
                for line in ('operating','pretax','net','parent'):
                    self.assertEqual(values[line]['2023-09-30'],Decimal(filed[line]['cells']['2023-09-30']['value']))

    def test_downstream_replay_refuses_drift_and_dropped_period(self):
        from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure
        from dalton_core.model_forecast_driver import build_structure_drivers, structure_historical_values
        spec,inputs,sources=fixture();inputs['historical_income_composition']=derive(spec,inputs,sources)
        structure,_=materialize_financial_statement_structure(spec,inputs,complete=False)
        for change in ('amount','period'):
            drivers=build_structure_drivers(inputs,structure)
            driver=next(d for d in drivers if d['structure_line_ref']=='cost')
            if change=='amount':driver['history'][0]['value']='999'
            else:driver['history'].pop()
            with self.assertRaises(FiledCompositionError):structure_historical_values(drivers,structure)
