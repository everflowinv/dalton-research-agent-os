import copy
import hashlib
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from dalton_core.forecast_source_accuracy import (
    RULE_REF, _facts, radius, expression, combine, assess, history_assessment,
    family_assessment, validate_shape, validate_originals,
)
from dalton_core.sec_filing_units import parse_filing_units
from dalton_core.store import content_hash
from dalton_core.model_forecast_driver import ForecastModelValidationError
from dalton_core.forecast_financial_execution import driver_execution, execute_financial_model, validate_execution_contract


class SourceAccuracyTests(unittest.TestCase):
    def fixture(self, *, issuer='42', negative=False, decimals='-6', duplicate=False, delta=1):
        company = 'company:sec-cik:' + issuer.zfill(10)
        periods = [('2025-01-01','2025-03-31'),('2025-04-01','2025-06-30'),('2025-07-01','2025-09-30'),('2025-10-01','2025-12-31')]
        head = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:iso="http://www.xbrl.org/2003/iso4217" xmlns:gaap="http://example.test/gaap" xmlns:srt="http://fasb.org/srt/2025"><body>'
        contexts, elements, drivers = [], [], []
        for member in ('Product','Service','Total'):
            history = []
            for i,(start,end) in enumerate(periods):
                a,b = 40+4*i, (-10-i if negative else 60+6*i)
                value = a if member=='Product' else b if member=='Service' else a+b+delta
                ref = member+str(i)
                dims = {} if member=='Total' else {'srt:ProductOrServiceAxis':'gaap:'+member}
                segment = '' if not dims else '<xbrli:segment><xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">gaap:'+member+'</xbrldi:explicitMember></xbrli:segment>'
                contexts.append(f'<xbrli:context id="{ref}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{issuer}</xbrli:identifier>{segment}</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')
                attr = '' if decimals is None else f' decimals="{decimals}"'
                element = f'<ix:nonFraction name="gaap:Revenue" contextRef="{ref}" unitRef="dollars" scale="6"{attr}>{value}</ix:nonFraction>'
                elements += [element] * (2 if duplicate else 1)
                history.append({'concept':'gaap:Revenue','period_start':start,'period_end':end,'value':str(value*1000000),'basis':'reported','accessions':['accession:synthetic']})
            drivers.append({'ref':member,'concept':'gaap:Revenue','unit':'usd','statement':'income','history':history,**({'statement_identity':{'statement':'income','dimensions':dims}}if dims else {})})
        raw = (head+''.join(contexts)+'<xbrli:unit id="dollars"><xbrli:measure>iso:USD</xbrli:measure></xbrli:unit>'+''.join(elements)+'</body></html>').encode()
        parsed = parse_filing_units(raw)
        source = {'ingest_id':'ingest:synthetic','filing_hash':'a'*64,'source':{},'raw_sha256':hashlib.sha256(raw).hexdigest(),'concepts':['{http://example.test/gaap}Revenue'],'namespace_bindings':parsed['namespace_bindings'],'facts':_facts(raw,parsed,'accession:synthetic',{'{http://example.test/gaap}Revenue'})}
        proof = {'rule_ref':RULE_REF,'company_ref':company,'sources':[source],'unavailable_sources':[]};proof['content_hash']=content_hash(proof)
        return raw,proof,drivers

    def test_scaled_decimals_and_valid_significant_precision(self):
        self.assertEqual(radius('40000000','-6'),Decimal('500000'))
        self.assertEqual(radius('-40000000',None,'2'),Decimal('500000'))
        self.assertEqual(radius('0','INF'),0)
        for d,p in [(None,None),('garbage',None),(True,None),('-6','2'),(None,'0'),(None,True)]:
            self.assertIsNone(radius('40000000',d,p))
        self.assertIsNone(radius('0',None,'2'))

    def test_other_issuers_positive_and_negative_reporting_consistent_not_exact(self):
        for issuer,negative in [('42',False),('77',True)]:
            with self.subTest(issuer=issuer):
                raw,p,ds=self.fixture(issuer=issuer,negative=negative)
                validate_shape(p,p['company_ref'])
                q=history_assessment(p,ds[:2],ds[2],ds[0]['history'][0]['period_end'])
                self.assertEqual(q['status'],'within_disclosed_accuracy')
                self.assertEqual(q['reported_delta'],'-1000000')
                self.assertEqual(Decimal(q['accuracy_bound']),Decimal('1500000'))
                self.assertEqual(ds[2]['history'][0]['value'],str((31 if negative else 101)*1000000))

    def test_no_accuracy_invalid_accuracy_or_outside_interval_stays_strict(self):
        for decimals in [None,'invalid','INF']:
            raw,p,ds=self.fixture(decimals=decimals)
            q=history_assessment(p,ds[:2],ds[2],ds[0]['history'][0]['period_end'])
            self.assertTrue(q is None or q['status']=='refused')
        raw,p,ds=self.fixture(delta=3)
        self.assertEqual(history_assessment(p,ds[:2],ds[2],ds[0]['history'][0]['period_end'])['status'],'refused')
        raw,p,ds=self.fixture();ds[2]['history'][0]['value']='103000000'
        self.assertIsNone(history_assessment(p,ds[:2],ds[2],ds[0]['history'][0]['period_end']))

    def test_same_fact_is_one_variable_not_repeated_uncertainty(self):
        raw,p,ds=self.fixture(duplicate=True)
        e=expression(p,ds[0],ds[0]['history'][0])
        self.assertEqual(assess(combine([(e,1),(e,-1)]))['accuracy_bound'],'0')
        q=history_assessment(p,ds[:2],ds[2],ds[0]['history'][0]['period_end'])
        self.assertEqual(Decimal(q['accuracy_bound']),Decimal('1500000'))
        self.assertIsNone(history_assessment(p,[ds[0],ds[0]],ds[2],ds[0]['history'][0]['period_end']))

    def test_wrong_issuer_unit_period_dimension_or_conflicting_fact_has_no_permission(self):
        raw,p,ds=self.fixture();end=ds[0]['history'][0]['period_end']
        for field,value in [('unit','eur'),('concept','gaap:Different'),('statement','cash')]:
            altered=copy.deepcopy(ds);altered[0][field]=value
            self.assertIsNone(history_assessment(p,altered[:2],altered[2],end))
        altered=copy.deepcopy(ds);altered[0]['history'][0]['period_start']='2024-12-31'
        self.assertIsNone(history_assessment(p,altered[:2],altered[2],end))
        altered=copy.deepcopy(p);altered['company_ref']='company:sec-cik:0000000999'
        self.assertIsNone(history_assessment(altered,ds[:2],ds[2],end))
        altered=copy.deepcopy(p);f=copy.deepcopy(altered['sources'][0]['facts'][0]);f['value']=str(Decimal(f['value'])+1);altered['sources'][0]['facts'].append(f)
        # Any conflicting matching original is unavailable, not chosen by amount.
        target=next(d for d in ds if any(c['value']==p['sources'][0]['facts'][0]['value']for c in d['history']))
        self.assertIsNone(expression(altered,target,next(c for c in target['history']if c['period_end']==p['sources'][0]['facts'][0]['period_end'])))

    def test_fy_minus_ytd_atomic_accuracy_propagates_and_period_change_refuses(self):
        raw,p,ds=self.fixture();d=ds[0]
        # Reuse physical original facts as a structural adjacent cumulative pair
        # for pure propagation; original revalidation rejects synthetic edits.
        fs=copy.deepcopy(p['sources'][0]['facts'][:0]);dims=d['statement_identity']['dimensions']
        from dalton_core.sec_filing_units import _expanded
        ns=p['sources'][0]['namespace_bindings'];expanded={_expanded(a,ns):_expanded(m,ns)for a,m in dims.items()}
        for end,val,acc in [('2025-09-30','120000000','nine'),('2025-12-31','160000000','year')]:
            f=copy.deepcopy(next(x for x in p['sources'][0]['facts']if x['dimensions']==expanded));f.update(period_start='2025-01-01',period_end=end,value=val,accession=acc)
            identity={k:f[k]for k in ('accession','cik','concept','period_start','period_end','dimensions','unit')};f['atomic_ref']=content_hash(identity);fs.append(f)
        p['sources'][0]['facts']=fs
        c={'period_start':'2025-10-01','period_end':'2025-12-31','value':'40000000','basis':'derived_from_cumulative','accessions':['nine','year'],'derived_from':[{'period_start':'2025-01-01','period_end':f['period_end'],'value':f['value'],'unit':'usd','accession':f['accession']}for f in fs]}
        e=expression(p,d,c);self.assertEqual(Decimal(assess(e)['accuracy_bound']),Decimal('1000000'))
        del c['derived_from'];self.assertEqual(expression(p,d,c),e) # strict old income-cell reconstruction
        c['period_start']='2025-09-30';self.assertIsNone(expression(p,d,c))

    def test_family_hidden_duplicate_member_units_and_missing_total_remain_refused(self):
        raw,p,ds=self.fixture();end=ds[0]['history'][0]['period_end'];start=ds[0]['history'][0]['period_start']
        rows=[]
        for d in ds:
            cell=d['history'][0];dims=(d.get('statement_identity')or{}).get('dimensions')or{}
            rows.append({'ingest_id':'ingest:synthetic','concept':d['concept'],'period_start':start,'period_end':end,'dimensions':dims,'unit':'USD','value':cell['value'],'is_breakdown':bool(dims)})
        group={'line':'gaap:Revenue','period':start+'..'+end,'axis':'srt:ProductOrServiceAxis','total':ds[2]['history'][0]['value'],'parts':[{'member':'gaap:Product','value':ds[0]['history'][0]['value']},{'member':'gaap:Service','value':ds[1]['history'][0]['value']}]}
        self.assertEqual(family_assessment(p,group,rows)['status'],'within_disclosed_accuracy')
        for changed in [rows+rows[:1],rows[:2],rows+[{**rows[0],'dimensions':{'srt:ProductOrServiceAxis':'gaap:Hidden'},'value':'0'}]]:
            self.assertIsNone(family_assessment(p,group,changed))
        changed=copy.deepcopy(rows);changed[0]['unit']='EUR';self.assertIsNone(family_assessment(p,group,changed))
        changed=copy.deepcopy(group);changed['undeclared_members']=['hidden'];self.assertIsNone(family_assessment(p,changed,rows))

    def test_forged_radius_or_original_bytes_never_gain_publish_authority(self):
        raw,p,ds=self.fixture();f=p['sources'][0]['facts'][0];f['radius']='999999999';p['content_hash']=content_hash({k:v for k,v in p.items()if k!='content_hash'})
        with self.assertRaisesRegex(ForecastModelValidationError,'radius differs'):validate_shape(p,p['company_ref'])
        raw,p,ds=self.fixture();c=sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        with self.assertRaisesRegex(ForecastModelValidationError,'physical original authority'):validate_originals(c,{'company_ref':p['company_ref'],'execution_contract':{'source_accuracy':p}})

    def test_actual_execution_sum_uses_source_accuracy_and_retains_difference_on_replay(self):
        from tests.test_forecast_financial_execution import economic_model
        body,spec=economic_model('sum');raw,p,ds=self.fixture()
        body['company_ref']=p['company_ref'];spec['company_ref']=p['company_ref']
        inputs=[]
        for ref,synthetic in zip(['economic:a','economic:b'],ds[:2]):
            d=next(x for x in body['drivers']if x['ref']==ref);d.update({k:copy.deepcopy(synthetic[k])for k in ['concept','unit','statement','history','statement_identity']});inputs.append(d)
        target=next(d for d in body['drivers']if d.get('structure_line_ref')=='revenue');target.update({k:copy.deepcopy(ds[2][k])for k in ['concept','unit','statement','history']})
        for decl,d in zip(spec['revenue_drivers'],inputs):decl.update(basis_concept=d['concept'],basis_identity=d['statement_identity'])
        spec['source_accuracy']=p
        strict=driver_execution(body,spec)[0];self.assertEqual(strict['status'],'unexecuted')
        report=driver_execution(body,spec,source_accuracy=p)[0];self.assertEqual(report['status'],'executed')
        self.assertEqual(len(report['formulas'][0]['reported_accuracy_checks']),4)
        body['execution_contract']['source_accuracy']=p
        execute_financial_model(body,spec)
        validate_execution_contract(body,publishing=True)
        self.assertEqual(target['history'][0]['value'],'101000000')
        self.assertTrue(all(x['reported_delta']=='-1000000'for x in body['execution_contract']['drivers']['formulas'][0]['reported_accuracy_checks']))

    def test_original_receipt_replay_rejects_rehashed_accuracy_and_changed_bytes(self):
        raw,p,ds=self.fixture()
        with tempfile.TemporaryDirectory()as td:
            c=sqlite3.connect(Path(td)/'core.sqlite');c.row_factory=sqlite3.Row
            self.addCleanup(c.close)
            c.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT,content_hash TEXT,company_ref TEXT,accession TEXT)')
            c.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?)',('ingest:synthetic','a'*64,p['company_ref'],'accession:synthetic'))
            record={'company_ref':p['company_ref'],'execution_contract':{'source_accuracy':p}}
            with patch('dalton_core.company_model_annual_projection.verify_statement_filing'),patch('dalton_core.sec_filing_units._source',return_value=({},raw)):
                validate_originals(c,record)
                bad=copy.deepcopy(record);f=bad['execution_contract']['source_accuracy']['sources'][0]['facts'][0]
                f['decimals']='-9';f['radius']=str(radius(f['value'],'-9'))
                proof=bad['execution_contract']['source_accuracy'];proof['content_hash']=content_hash({k:v for k,v in proof.items()if k!='content_hash'})
                with self.assertRaisesRegex(ForecastModelValidationError,'does not replay'):validate_originals(c,bad)
            with patch('dalton_core.company_model_annual_projection.verify_statement_filing'),patch('dalton_core.sec_filing_units._source',return_value=({},raw.replace(b'decimals="-6"',b'decimals="-9"'))):
                with self.assertRaisesRegex(ForecastModelValidationError,'does not replay'):validate_originals(c,record)
            with patch('dalton_core.sec_filing_units._source',side_effect=AssertionError('old proofs must not read accuracy sources')):
                validate_originals(c,{'execution_contract':{}})
                validate_originals(c,{})

    def test_fixed_extra_axis_or_different_source_cannot_borrow_rounding_budget(self):
        raw,p,ds=self.fixture();end=ds[0]['history'][0]['period_end']
        other=copy.deepcopy(ds);other[0]['history'][0]['accessions']=['other-file']
        self.assertIsNone(history_assessment(p,other[:2],other[2],end))
        other=copy.deepcopy(ds)
        for d in other[:2]:d['statement_identity']['dimensions']['gaap:LayerAxis']='gaap:Parent'
        self.assertIsNone(history_assessment(p,other[:2],other[2],end))

    def test_interval_report_keeps_direction_and_domain_failure_and_all_source_values(self):
        from dalton_core.forecast_source_accuracy import evaluate_segment_accuracy
        from dalton_core.economic_invariants import evaluate,FORECAST_MODEL
        raw,p,ds=self.fixture();start,end=ds[0]['history'][0]['period_start'],ds[0]['history'][0]['period_end']
        rows=[{'ingest_id':'ingest:synthetic','concept':d['concept'],'period_start':start,'period_end':end,'dimensions':(d.get('statement_identity')or{}).get('dimensions')or{},'unit':'USD','value':d['history'][0]['value'],'is_breakdown':d is not ds[2]}for d in ds]
        group={'line':'gaap:Revenue','period':start+'..'+end,'axis':'srt:ProductOrServiceAxis','total':ds[2]['history'][0]['value'],'parts':[{'member':'gaap:Product','value':ds[0]['history'][0]['value']},{'member':'gaap:Service','value':ds[1]['history'][0]['value']}]}
        subject={'segments':[group],'rate_quantities':[{'rate_kind':'tax_rate','value':'1.2'}]}
        old=evaluate(output_kind=FORECAST_MODEL,output_ref='candidate',company_ref=p['company_ref'],subject=subject)
        record={'company_ref':p['company_ref'],'execution_contract':{'source_accuracy':p}}
        report=evaluate_segment_accuracy(record,subject,{('source',):rows},[(('source',),group)],old)
        self.assertEqual(next(v for v in report.results if v.invariant=='segment_sum').status,'pass')
        # Domain remains the same verdict; the sum proof does not replace it.
        self.assertEqual([v for v in report.results if v.invariant!='segment_sum'],[v for v in old.results if v.invariant!='segment_sum'])
        self.assertEqual(report.status,'unavailable')
        self.assertIn('reported_delta',next(v for v in report.results if v.invariant=='segment_sum').findings[0])
        self.assertEqual(rows[-1]['value'],'101000000')
        self.assertIs(evaluate_segment_accuracy({'company_ref':p['company_ref']},subject,{('source',):rows},[(('source',),group)],old),old)

    def test_interval_arithmetic_retains_small_difference_next_to_large_values(self):
        e={'a':{'value':Decimal('100000000000000000000000000000000000001'),'radius':Decimal('0.5'),'coefficient':Decimal(1)},
           'b':{'value':Decimal('100000000000000000000000000000000000000'),'radius':Decimal('0.5'),'coefficient':Decimal(-1)}}
        report=assess(e)
        self.assertEqual(report['reported_delta'],'1')
        self.assertEqual(report['accuracy_bound'],'1.0')
        self.assertEqual(report['difference_interval'],['0.0','2.0'])

    def test_new_actualization_carries_new_source_proof_without_changing_prior(self):
        from tests import test_financial_structure_forecast_consumer as f
        from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure,forecast_structure_binding
        from dalton_core.model_forecast_driver import build_structured_forecast_model,actualize_model,ForecastModelAuthority
        from dalton_core.store import DaltonStore
        inputs=f.financial_inputs();candidate=f.forecastable_proposal(inputs)
        spec={**f.company_spec(),'decided_by':'automation:test','financial_statement_structure':{'schema_version':f.STRUCTURE_SCHEMA_VERSION,'lines':candidate['lines'],'formulas':candidate['formulas']}}
        structure,replay=materialize_financial_statement_structure(spec,inputs)
        body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=forecast_structure_binding(structure,replay,inputs))
        store=DaltonStore(':memory:');self.addCleanup(store.close)
        prior=ForecastModelAuthority(store).publish(body);before=copy.deepcopy(prior)
        current=copy.deepcopy(inputs);current['periods'].append('2026-03-31')
        actuals={'revenue':1400,'cost':840,'opex':280,'operating':280,'interest_income':10,'interest_expense':20,'pretax':270,'tax':60,'net':210,'nci':5,'parent':205,'eps_numerator':205,'shares':100,'eps':'2.05','us-gaap:LongTermDebtNoncurrent':1000}
        for line in current['filed_lines']:
            line['cells']['2026-03-31']={'period_start':'2026-01-01','value':str(actuals[line['concept']]),'unit':next(iter(line['cells'].values()))['unit'],'basis':'reported','source_accessions':['0000000000-26-000002']}
        structure,replay=materialize_financial_statement_structure(spec,current)
        proof={'rule_ref':RULE_REF,'company_ref':prior['company_ref'],'sources':[],'unavailable_sources':[]};proof['content_hash']=content_hash(proof)
        updated=actualize_model(prior,current,structure=structure,replay=replay,binding=forecast_structure_binding(structure,replay,current),source_accuracy=proof)
        self.assertEqual(updated['execution_contract']['source_accuracy'],proof)
        self.assertEqual(prior,before)
        validate_execution_contract(updated,publishing=True)

    def test_accuracy_rule_does_not_change_paid_producer_or_repair_identity(self):
        from dalton_core.company_model_cli import REPAIR_CONTRACT_REF,REPAIR_CONTRACT_HASH
        from dalton_core.company_model_spec import TASK_HASH
        self.assertEqual(TASK_HASH,'608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertEqual(REPAIR_CONTRACT_REF,'contract:company-model-spec-structured-output-repair:0.11')
        self.assertEqual(REPAIR_CONTRACT_HASH,'044ffd4812e6beb89152f2cc120e793265bffeb5e9022c86fac04bb44487912d')
