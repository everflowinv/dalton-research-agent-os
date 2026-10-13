"""A latest comparative total cannot erase or replace an executed original partition."""
import copy,unittest
from dalton_core.forecast_consumed_source_groups import select_rows,evaluate,RULE_REF
from dalton_core.economic_invariants import evaluate_forecast_model
from dalton_core.model_forecast_driver import ForecastModelValidationError

class ConsumedSourceGroupTests(unittest.TestCase):
    def setUp(self):
        axis='srt:ProductOrServiceAxis';self.filings=[{'ingest_id':'ingest:old','company_ref':'company:test','accession':'old','filed':'2025-01-01','form':'10-Q'},
            {'ingest_id':'ingest:new','company_ref':'company:test','accession':'new','filed':'2026-01-01','form':'10-Q'}]
        self.rows={'ingest:old':[],'ingest:new':[]};histories={'a':[],'b':[]}
        for n,(start,end)in enumerate([('2024-01-01','2024-03-31'),('2024-04-01','2024-06-30'),('2024-07-01','2024-09-30'),('2024-10-01','2024-12-31')]):
            for ingest,members in [('ingest:old',[('total',100),('a',40),('b',60)]),('ingest:new',[('total',110)])]:
                for member,value in members:
                    dims={}if member=='total'else{axis:member}
                    self.rows[ingest].append({'line_id':ingest+':'+end+':'+member,'ingest_id':ingest,'statement_context_ref':'context:'+ingest,
                        'ordinal':n*3+len(self.rows[ingest]),'concept':'revenue','statement':'income','period_start':start,'period_end':end,
                        'value':str(value),'unit':'USD','dimensions':dims,'dimension_axis':axis if dims else None,
                        'dimension_member':member if dims else None,'dimension_count':len(dims),'is_breakdown':bool(dims)})
            for member,value in [('a',40),('b',60)]:histories[member].append({'concept':'revenue','period_start':start,'period_end':end,'value':str(value),'basis':'reported','accessions':['old']})
        declarations=[{'ref':m,'basis_concept':'revenue','basis_identity':{'statement':'income','dimensions':{axis:m}}}for m in ('a','b')]
        self.record={'company_ref':'company:test','drivers':[{'ref':'economic:'+m,'concept':'revenue','statement':'income','unit':'usd','statement_identity':{'statement':'income','dimensions':{axis:m}},'history':histories[m]}for m in ('a','b')],
            'execution_contract':{'specification':{'revenue_drivers':declarations,'driver_formulas':[{'output_ref':'result:revenue','operator':'sum','driver_refs':['a','b']}]}},'results':[],'assumptions':[]}

    def test_latest_and_exact_consumed_original_complete_groups_coexist(self):
        rows,manifest=select_rows(self.record,self.filings,self.rows)
        self.assertEqual(len(rows),16)
        self.assertEqual(manifest['rule_ref'],RULE_REF)
        self.assertTrue(all(g['roles']==['consumed']if g['ingest_id']=='ingest:old'else g['roles']==['latest']for g in manifest['groups']))
        self.assertEqual(len({r['line_id']for r in rows}),16)
        old=evaluate_forecast_model(self.record,statement_rows=rows)
        new=evaluate(self.record,statement_rows=rows)
        self.assertNotEqual(old.status,'available')
        self.assertEqual(new.status,'available',new.reasons)
        self.assertEqual({r['value']for r in rows if not r['dimensions']},{'100','110'})

    def test_selection_never_uses_values_to_find_a_matching_filing(self):
        before,_=select_rows(self.record,self.filings,self.rows)
        self.record['drivers'][0]['history'][0]['value']='999'
        after,_=select_rows(self.record,self.filings,self.rows)
        self.assertEqual(before,after)

    def test_old_missing_member_or_total_cannot_borrow_new_same_value(self):
        for member in ('a','total'):
            with self.subTest(member=member):
                rows=copy.deepcopy(self.rows);rows['ingest:old']=[r for r in rows['ingest:old']if not r['line_id'].endswith(':'+member)]
                with self.assertRaisesRegex(ForecastModelValidationError,'source is missing|own consolidated'):
                    select_rows(self.record,self.filings,rows)

    def test_changed_original_member_or_other_company_is_refused(self):
        rows=copy.deepcopy(self.rows)
        for r in rows['ingest:old']:
            if r['dimension_member']=='a':r['dimensions']={'srt:ProductOrServiceAxis':'foreign'}
        with self.assertRaisesRegex(ForecastModelValidationError,'source is missing'):
            select_rows(self.record,self.filings,rows)
        bad=copy.deepcopy(self.filings);bad[0]['company_ref']='company:foreign'
        with self.assertRaisesRegex(ForecastModelValidationError,'foreign company'):
            select_rows(self.record,bad,self.rows)

    def test_bad_original_arithmetic_is_not_saved_by_restated_latest_total(self):
        next(r for r in self.rows['ingest:old']if not r['dimensions'])['value']='101'
        rows,_=select_rows(self.record,self.filings,self.rows)
        report=evaluate(self.record,statement_rows=rows)
        self.assertNotEqual(report.status,'available')


class SourceProofReplayTests(unittest.TestCase):
    setUp = ConsumedSourceGroupTests.setUp
    def test_frozen_snapshot_replays_and_missing_or_changed_members_are_rejected(self):
        import sqlite3
        from unittest.mock import patch
        from dalton_core.forecast_consumed_source_groups import replay_manifest
        connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id,company_ref,accession,filed,form)')
        for f in self.filings:connection.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?)',tuple(f.values()))
        rows,manifest=select_rows(self.record,self.filings,self.rows)
        def read(_c,ingest,**kwargs):
            self.assertEqual(kwargs['version_id'],'context:'+ingest)
            return self.rows[ingest]
        with patch('dalton_core.statement_context.statement_rows',side_effect=read):
            self.assertEqual(replay_manifest(self.record,rows,manifest,connection),manifest)
            altered=copy.deepcopy(rows);altered[0]['value']='101'
            with self.assertRaisesRegex(ForecastModelValidationError,'incomplete or changed'):
                replay_manifest(self.record,altered,manifest,connection)
            missing=rows[1:]
            with self.assertRaises(ForecastModelValidationError):replay_manifest(self.record,missing,manifest,connection)
            bad=copy.deepcopy(manifest);bad['groups'][0]['roles']=[]
            with self.assertRaisesRegex(ForecastModelValidationError,'hash differs'):
                replay_manifest(self.record,rows,bad,connection)
            # Later current authority cannot silently move a published role.
            connection.execute("UPDATE coverage_mission_statement_filings SET filed='2027-01-01' WHERE ingest_id='ingest:old'")
            with self.assertRaisesRegex(ForecastModelValidationError,'does not replay'):
                replay_manifest(self.record,rows,manifest,connection)

    def test_derived_quarter_keeps_exact_two_cumulative_source_periods(self):
        self.record['execution_contract']['specification']['driver_formulas']=[]
        driver=self.record['drivers'][0];driver['history']=[{'concept':'revenue','period_start':'2024-04-01','period_end':'2024-06-30','basis':'derived_from_cumulative','accessions':['old','new'],'value':'40'}]
        driver['statement_identity']['dimensions']={}
        self.rows={'ingest:old':[dict(self.rows['ingest:old'][0],period_start='2024-01-01',period_end='2024-03-31')],
                   'ingest:new':[dict(self.rows['ingest:new'][0],period_start='2024-01-01',period_end='2024-06-30')]}
        self.record['drivers']=[driver]
        rows,manifest=select_rows(self.record,self.filings,self.rows)
        self.assertEqual(len(rows),2);self.assertTrue(all('consumed'in g['roles']for g in manifest['groups']))
        self.rows['ingest:old'][0]['period_end']='2024-03-30'
        with self.assertRaisesRegex(ForecastModelValidationError,'source is missing'):
            select_rows(self.record,self.filings,self.rows)

class StoredCompatibilityTests(unittest.TestCase):
    def fixture(self):
        from tests import test_company_model_forecast as f
        case=f.LaneStateTests('runTest');case.setUp();self.addCleanup(case.doCleanups);return case

    def test_new_proof_durable_reader_and_tamper_rejection(self):
        from dalton_core.model_forecast_driver import ForecastModelAuthority,ForecastModelConflict
        f=self.fixture();result=f.child();a=ForecastModelAuthority(f.store)
        proof=a.filing_proof(result['model_version_ref'])
        self.assertEqual(proof['consumed_source_groups']['rule_ref'],RULE_REF)
        self.assertEqual(a.filing_proof(result['model_version_ref']),proof)
        f.store.connection.execute('DROP TRIGGER forecast_model_filing_proof_no_update')
        import json
        bad=copy.deepcopy(proof);bad['consumed_source_groups']['groups'][0]['roles']=[]
        f.store.connection.execute('UPDATE forecast_model_filing_proofs SET record_json=?',(json.dumps(bad),))
        with self.assertRaises(ForecastModelConflict):a.filing_proof(result['model_version_ref'])

    def test_old_proof_bytes_and_algorithm_are_unchanged_on_duplicate(self):
        from unittest.mock import patch
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        f=self.fixture()
        original=select_rows
        def legacy(*args):return original(*args)[0],None
        with patch('dalton_core.forecast_consumed_source_groups.select_rows',side_effect=legacy):result=f.child()
        a=ForecastModelAuthority(f.store);ref=result['model_version_ref'];before=f.store.connection.execute('SELECT record_json FROM forecast_model_filing_proofs WHERE model_version_id=?',(ref,)).fetchone()[0]
        self.assertNotIn('consumed_source_groups',a.filing_proof(ref))
        held=a.model(ref);rows,groups=__import__('dalton_core.forecast_consumed_source_groups',fromlist=['select_from_authority']).select_from_authority(held,f.store.connection)
        duplicate=a.publish(held,statement_rows=rows,source_groups=groups)
        self.assertEqual(duplicate['status'],'duplicate')
        self.assertEqual(f.store.connection.execute('SELECT record_json FROM forecast_model_filing_proofs WHERE model_version_id=?',(ref,)).fetchone()[0],before)
        with patch('dalton_core.forecast_consumed_source_groups.evaluate',side_effect=AssertionError('new algorithm used for old proof')):
            self.assertNotIn('consumed_source_groups',a.filing_proof(ref))

class CashOperandPublicationTests(unittest.TestCase):
    def publish(self, *, change=None, wrong_form=False):
        import sqlite3
        from tests import test_structured_cash_flow_companion as f
        from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure,forecast_structure_binding
        from dalton_core.model_forecast_driver import build_structured_forecast_model,ForecastModelAuthority
        from dalton_core.store import DaltonStore
        inputs,spec,*_=f.authorities()
        cell=inputs['cash_flow_inputs'][0]['series']['quarters'][1]
        cell.update(basis='derived_from_cumulative',source_accessions=[f.ACCESSION,'second'],source_forms=['10-Q'],derived_from=[
            {'period_start':'2025-01-01','period_end':'2025-03-31','value':'100','unit':'usd','accession':f.ACCESSION,'form':'10-Q'},
            {'period_start':'2025-01-01','period_end':'2025-06-30','value':'210','unit':'usd','accession':'second','form':'10-Q'}])
        structure,replay=materialize_financial_statement_structure(spec,inputs)
        body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=forecast_structure_binding(structure,replay,inputs))
        store=DaltonStore(':memory:');self.addCleanup(store.close);c=store.connection
        c.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id PRIMARY KEY,company_ref,accession,form,filed,report_date)')
        for accession,form in [(f.ACCESSION,'10-Q'),('second','10-Q'),('annual','10-K')]:c.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?)',(accession,body['company_ref'],accession,form,'2026-01-01','2025-12-31'))
        c.execute('CREATE TABLE coverage_mission_statement_lines(line_id PRIMARY KEY,ingest_id,ordinal,concept,statement,period_start,period_end,value,unit,dimension_axis,dimension_member,is_breakdown,dimensions_json)')
        keys=set();rows=[]
        for driver in body['drivers']:
            for cell in driver['history']:
                operands=cell.get('derived_from') or [{**cell,'accession':cell['accessions'][0],'unit':driver['unit']}]
                for operand in operands:
                    key=(operand['accession'],driver['concept'],operand.get('period_start'),operand['period_end'])
                    if key in keys:continue
                    keys.add(key);row=dict(line_id='line:'+str(len(rows)),ingest_id=operand['accession'],ordinal=len(rows),concept=driver['concept'],statement=driver['statement'],period_start=operand.get('period_start'),period_end=operand['period_end'],value=operand['value'],unit='USD'if operand['unit']=='usd'else operand['unit'],dimension_axis=None,dimension_member=None,is_breakdown=False,dimensions_json='{}')
                    if key==('second','us-gaap:NetCashProvidedByUsedInOperatingActivities','2025-01-01','2025-06-30') and change:
                        row.update(change)
                    c.execute('INSERT INTO coverage_mission_statement_lines VALUES('+','.join('?'for _ in row)+')',tuple(row.values()));rows.append(row)
        if wrong_form:c.execute("UPDATE coverage_mission_statement_filings SET form='10-K' WHERE ingest_id='second'")
        from dalton_core.coverage_mission import _statement_line_row
        rows=[_statement_line_row(r)for r in c.execute('SELECT * FROM coverage_mission_statement_lines')]
        a=ForecastModelAuthority(store);held=a.publish(body,statement_rows=rows)
        self.assertEqual(a.filing_proof(held['id'])['invariant_report']['status'],'available')
        return held

    def test_actual_cash_operand_reconciliation_accepts_existing_structured_usd_rule(self):
        self.assertEqual(self.publish()['status'],'fresh')

    def test_cash_operand_wrong_currency_value_period_and_form_still_refuse(self):
        for change in [{'unit':'EUR'},{'value':'211'},{'period_end':'2025-06-29'}]:
            with self.subTest(change=change),self.assertRaises(ForecastModelValidationError):self.publish(change=change)
        with self.assertRaises(ForecastModelValidationError):self.publish(wrong_form=True)

if __name__ == "__main__":
    unittest.main()
