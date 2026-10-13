import copy
import tempfile
import unittest
from pathlib import Path
from dalton_core.store import canonical_json, content_hash, DaltonStore
from dalton_core.model_forecast_driver import (build_cash_flow_companion, build_structured_forecast_model,
    ForecastModelUnavailable, ForecastModelValidationError, ForecastModelAuthority, validate_forecast_model)
from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure, forecast_structure_binding
from dalton_core.cash_flow_filed_tie_base import RULE_REF
from tests.test_derived_cash_forecast_base import derived_fixture


def fixture(company='company:test:gamma', unit='usd'):
    inputs, spec, _, _, _ = derived_fixture(company, unit)
    formula = next(f for f in spec['financial_statement_structure']['formulas'] if f['output_ref']=='net')
    source = next(f for f in inputs['filed_lines'] if f['concept']==formula['tie_out_concept'])
    source['duration_facts'] = [{'period_start':c['period_start'],'period_end':end,'period_kind':'quarter',
        'value':c['value'],'unit':c['unit'],'source_accessions':c['source_accessions'],'source_forms':['10-Q']}
        for end,c in source['cells'].items()]
    cost = next(f for f in inputs['filed_lines'] if f['concept']=='cost')
    del cost['cells'][next(iter(cost['cells']))]
    structure, replay = materialize_financial_statement_structure(spec, inputs, complete=False)
    return inputs, spec, structure, replay, forecast_structure_binding(structure,replay,inputs), source


class FiledTieCashBaseTests(unittest.TestCase):
    def test_two_currencies_full_builder_and_durable_replay_keep_missing_formula_history(self):
        for company,unit in [('company:test:gamma','usd'),('company:test:delta','eur')]:
            inputs,spec,structure,replay,binding,source=fixture(company,unit)
            before=canonical_json(inputs)
            body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
            companion=body['cash_flow_companion'];self.assertEqual(companion['schema_version'],'cash-flow-companion-0.2')
            basis=companion['lines'][0]['historical_base'];self.assertEqual(len(basis['missing_formula_periods']),1)
            self.assertEqual(len(basis['comparisons']),3);self.assertEqual(basis['source_basis'],'independent_reported_tie')
            driver=next(d for d in body['drivers'] if d['ref']==basis['driver']['ref'])
            self.assertEqual(driver['forecast_method'],'unavailable');self.assertEqual(driver['history'],basis['driver']['history'])
            self.assertTrue(any(a['provenance']['rule_ref']==RULE_REF for a in body['assumptions']))
            self.assertEqual(canonical_json(inputs),before)
            with tempfile.TemporaryDirectory()as directory:
                store=DaltonStore(str(Path(directory)/'core.sqlite'))
                try:
                    authority=ForecastModelAuthority(store);saved=authority.publish(body)
                    self.assertEqual(authority.model(saved['id'])['content_hash'],saved['content_hash'])
                    tampered=copy.deepcopy(saved);d=next(d for d in tampered['drivers']if d['ref']==basis['driver']['ref']);d['history'][0]['value']='999'
                    with self.assertRaises(ForecastModelValidationError):validate_forecast_model(tampered)
                finally:store.close()

    def test_missing_latest_period_unit_duration_or_source_proof_still_refused(self):
        for kind in ('missing_latest','wrong_unit','wrong_duration','missing_source','no_originals','ambiguous_originals'):
            inputs,spec,structure,_,_,source=fixture()
            end=max(source['cells'])
            if kind=='missing_latest':del source['cells'][end]
            elif kind=='wrong_unit':source['cells'][end]['unit']='eur'
            elif kind=='wrong_duration':source['cells'][end]['period_start']='2025-12-01'
            elif kind=='missing_source':source['cells'][end]['source_accessions']=[]
            elif kind=='no_originals':source['duration_facts']=[]
            else:source['duration_facts'].append({**source['duration_facts'][-1],'source_forms':['10-K']})
            with self.subTest(kind=kind),self.assertRaises(ForecastModelUnavailable):
                build_cash_flow_companion(spec,inputs,structure)

    def test_computed_period_mismatch_and_unknown_tie_are_not_fixed_by_reported_amounts(self):
        inputs,spec,structure,_,_,source=fixture()
        end=sorted(source['cells'])[1];source['cells'][end]['value']='999'
        source['duration_facts'][1]['value']='999'
        with self.assertRaisesRegex(ForecastModelUnavailable,'disagrees'):
            build_cash_flow_companion(spec,inputs,structure)
        inputs,spec,structure,_,_,source=fixture()
        structure=copy.deepcopy(structure)
        next(f for f in structure['formulas']if f['output_ref']=='net')['tie_out_concept']='missing:total'
        with self.assertRaises(ForecastModelUnavailable):build_cash_flow_companion(spec,inputs,structure)

    def test_unknown_rule_forged_lineage_and_missing_independent_driver_refused(self):
        inputs,spec,structure,replay,binding,source=fixture()
        body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
        for kind in ('unknown_rule','source','missing_driver'):
            bad=copy.deepcopy(body);b=bad['cash_flow_companion']['lines'][0]['historical_base']
            if kind=='unknown_rule':b['rule_ref']='unknown:1'
            elif kind=='source':b['source']['cells'][max(b['source']['cells'])]['value']='999'
            else:bad['drivers']=[d for d in bad['drivers']if d['ref']!=b['driver']['ref']]
            with self.subTest(kind=kind),self.assertRaises(ForecastModelValidationError):validate_forecast_model(bad)

    def test_independent_reported_driver_is_checked_against_exact_filing_rows(self):
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from tests.test_company_financial_statement_structure import ACCESSION
        inputs,spec,structure,replay,binding,source=fixture()
        body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
        rows=[]
        for ordinal,driver in enumerate(body['drivers']):
            for index,cell in enumerate(driver['history']):
                rows.append({'line_id':f'line:{ordinal}:{index}','ingest_id':'ingest:source-proof',
                    'statement':driver['statement'],'ordinal':ordinal*10+index,'concept':driver['concept'],
                    'label':driver['label'],'level':0,'parent_concept':None,'is_breakdown':False,
                    'dimension_axis':None,'dimension_member':None,'period_start':cell['period_start'],
                    'period_end':cell['period_end'],'value':cell['value'],'unit':driver['unit'],
                    'balance':None,'dimension_count':0})
        for wrong in (None,'value','unit','period','dimension','issuer'):
            with self.subTest(wrong=wrong),tempfile.TemporaryDirectory()as directory:
                store=DaltonStore(str(Path(directory)/'core.sqlite'))
                try:
                    CoverageMissionAuthority(store);c=store.connection
                    for trigger in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('coverage_mission_statement_filings','coverage_mission_statement_lines')").fetchall():
                        c.execute(f"DROP TRIGGER {trigger['name']}")
                    c.execute('PRAGMA foreign_keys=OFF')
                    c.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('ingest:source-proof','dispatch:source-proof',spec['company_ref']if wrong!='issuer'else'company:other',
                         '0000000001','Synthetic',ACCESSION,'10-Q','2026-01-01','2025-12-31',len(rows),'[]','gov:test',
                         'a'*64,'2026-01-01T00:00:00+00:00','b'*64))
                    actual=copy.deepcopy(rows)
                    original=next(r for r in actual if r['concept']==source['concept'])
                    if wrong=='value':original['value']='999'
                    elif wrong=='unit':original['unit']='eur'
                    elif wrong=='period':original['period_start']='2025-02-01'
                    elif wrong=='dimension':original.update(dimension_axis='srt:ProductOrServiceAxis',dimension_member='product:one',dimension_count=1,is_breakdown=True)
                    columns=tuple(actual[0]);c.executemany('INSERT INTO coverage_mission_statement_lines('+','.join(columns)+')VALUES('+','.join('?'for _ in columns)+')',[tuple(r[k]for k in columns)for r in actual]);c.commit()
                    authority=ForecastModelAuthority(store)
                    if wrong is None:
                        held=authority.publish(body,statement_rows=actual)
                        self.assertIsNotNone(authority.filing_proof(held['id']))
                    else:
                        with self.assertRaises(ForecastModelValidationError):authority.publish(body,statement_rows=actual)
                finally:store.close()

    def test_cumulative_original_operands_and_zero_mixed_sign_refusal_are_preserved(self):
        from decimal import Decimal
        from dalton_core.cash_flow_filed_tie_base import derive
        from dalton_core.model_forecast_driver import build_structure_drivers
        from tests.test_company_financial_statement_structure import ACCESSION
        inputs,spec,structure,_,_,source=fixture()
        end=max(source['cells']);start=source['cells'][end]['period_start']
        older=[f for f in source['duration_facts'] if f['period_end']<end]
        prior=sum(Decimal(f['value'])for f in older);annual=prior+Decimal(source['cells'][end]['value'])
        source['duration_facts']=[f for f in source['duration_facts']if f['period_end']!=end]
        source['duration_facts'] += [
            {'period_start':'2025-01-01','period_end':'2025-09-30','period_kind':'cumulative','value':str(prior),'unit':'usd','source_accessions':[ACCESSION],'source_forms':['10-Q']},
            {'period_start':'2025-01-01','period_end':end,'period_kind':'cumulative','value':str(annual),'unit':'usd','source_accessions':['0000000001-26-000002'],'source_forms':['10-K']}]
        source['cells'][end].update(basis='derived_from_cumulative',source_accessions=[ACCESSION,'0000000001-26-000002'])
        c=build_cash_flow_companion(spec,inputs,structure)
        h=c['lines'][0]['historical_base']['driver']['history'][-1]
        self.assertEqual(len(h['derived_from']),2)
        self.assertEqual(Decimal(h['derived_from'][1]['value'])-Decimal(h['derived_from'][0]['value']),Decimal(h['value']))
        # This is a method applicability gate, not missing source data.
        for amount,reason in [('0','zero denominator'),('-10','mixed-sign')]:
            raw=copy.deepcopy(source);first=min(raw['cells']);raw['cells'][first]['value']=amount
            next(f for f in raw['duration_facts']if f['period_end']==first)['value']=amount
            with self.subTest(amount=amount),self.assertRaisesRegex(ForecastModelUnavailable,reason):
                derive(structure,'net',inputs['cash_flow_inputs'][0],raw,build_structure_drivers(inputs,structure))

    def test_automatic_assumption_cannot_claim_a_different_rate_or_operand(self):
        from dalton_core.cash_flow_filed_tie_base import validate_assumptions
        inputs,spec,structure,replay,binding,source=fixture()
        body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
        original=next(a for a in body['assumptions']if a['provenance']['rule_ref']==RULE_REF)
        for wrong in ('value','refs','kind','driver'):
            a=copy.deepcopy(original)
            if wrong=='value':a['value']='0.12345'
            elif wrong=='refs':a['refs'][0]['accession']='foreign'
            elif wrong=='kind':a['kind']='actual'
            else:a['driver_ref']='concept:foreign'
            with self.subTest(wrong=wrong),self.assertRaises(ForecastModelValidationError):
                validate_assumptions(body['cash_flow_companion'],[a])
