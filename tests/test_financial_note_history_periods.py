"""Exact filing-period advertising and canonical typed-note consumption."""
import copy
import json
import sqlite3
import unittest
from dalton_core.document_research_inventory import financial_note_targets_for_registration
from dalton_core.document_research_strategy import normalize_evidence_target, DocumentResearchStrategyError
from dalton_core.financial_note_evidence import resolve_financial_note_evidence
from dalton_core.financial_note_context import FinancialNoteReadContext
from dalton_core.research_planner import _history_note_prompt_schema, OUTPUT_SCHEMA
from dalton_core.store import content_hash
from tests import test_financial_note_evidence as note_fixtures


class HistoryNotePeriodsTests(unittest.TestCase):
    def filing(self, *, company='company:A', form='10-Q', extra=()):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;self.addCleanup(db.close)
        db.executescript('''CREATE TABLE coverage_mission_statement_dispatches(
            dispatch_id TEXT,mission_version_ref TEXT,mission_version_hash TEXT,company_ref TEXT,form TEXT,status TEXT);
          CREATE TABLE coverage_mission_statement_filings(
            ingest_id TEXT,dispatch_id TEXT,company_ref TEXT,cik TEXT,entity_name TEXT,accession TEXT,form TEXT,filed TEXT,
            report_date TEXT,line_count INTEGER,source_record_refs_json TEXT,governance_ref TEXT,governance_hash TEXT,
            recorded_at TEXT,content_hash TEXT);
          CREATE TABLE coverage_mission_statement_lines(line_id TEXT,ingest_id TEXT,statement TEXT,ordinal INTEGER,
            concept TEXT,label TEXT,level INTEGER,parent_concept TEXT,is_breakdown INTEGER,dimension_axis TEXT,
            dimension_member TEXT,dimension_count INTEGER,period_start TEXT,period_end TEXT,value TEXT,unit TEXT,balance TEXT);''')
        lines=[]
        for start,end in [('2024-07-01','2024-09-30'),('2025-07-01','2025-09-30'),
                          ('2025-01-01','2025-09-30'),('2024-01-01','2024-12-31')]:
            for concept,value,unit in [('EarningsPerShareDiluted','1.25','USDPerShare'),
                                      ('WeightedAverageNumberOfDilutedSharesOutstanding','100','shares')]:
                lines.append({'statement':'income','concept':'us-gaap:'+concept,'label':concept,'level':0,
                  'parent_concept':None,'is_breakdown':False,'dimension_axis':None,'dimension_member':None,
                  'dimension_count':0,'period_start':start,'period_end':end,'value':value,'unit':unit,'balance':None})
        lines.extend(extra)
        identity={'company_ref':company,'cik':'0000000001','accession':'0000000001-26-000001','form':form,'line_count':len(lines)}
        ingest='statement-ingest:'+content_hash(identity)[:32]
        body={**identity,'entity_name':'Issuer','filed':'2026-02-01','report_date':'2025-12-31',
              'source_record_refs':['raw:exact'],'governance_ref':'gov:1','governance_hash':'8'*64,
              'statement_lines_hash':content_hash(lines)}
        digest=content_hash(body);mission={'id':'mission:1','content_hash':'9'*64}
        db.execute('INSERT INTO coverage_mission_statement_dispatches VALUES(?,?,?,?,?,?)',
                   ('dispatch:1',mission['id'],mission['content_hash'],company,form,'succeeded'))
        db.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ingest,'dispatch:1',company,identity['cik'],body['entity_name'],identity['accession'],form,body['filed'],
             body['report_date'],len(lines),json.dumps(body['source_record_refs']),body['governance_ref'],body['governance_hash'],
             '2026-02-01T00:00:00+00:00',digest))
        for i,l in enumerate(lines):
            db.execute('INSERT INTO coverage_mission_statement_lines VALUES('+','.join('?'*17)+')',
              (f'{ingest}#{i}',ingest,l['statement'],i,l['concept'],l['label'],l['level'],l['parent_concept'],
               int(l['is_breakdown']),l['dimension_axis'],l['dimension_member'],l['dimension_count'],l['period_start'],
               l['period_end'],l['value'],l['unit'],l['balance']))
        registration={'source_ref':'source:sec-edgar','document_ref':'sec:filing:'+identity['accession'],
              'source_authority':{'kind':'coverage-mission-acquired-document','mission_version_ref':mission['id'],'company_ref':company}}
        return db,mission,registration,lines

    def targets(self, data, **kw):
        db,mission,registration,_=data
        return financial_note_targets_for_registration(connection=db,mission=mission,
            company_ref=registration['source_authority']['company_ref'],registration=registration,**kw)

    def test_two_companies_comparative_quarters_not_report_date_and_ytd_excluded(self):
        for company in ('company:A','company:B'):
            with self.subTest(company=company):
                data=self.filing(company=company);targets=self.targets(data)
                self.assertEqual(len(targets),3)
                self.assertEqual([t['periods'][0]['period_end'] for t in targets],['2024-12-31','2024-09-30','2025-09-30'])
                self.assertTrue(all(t['schema_version']=='financial-note-target-0.2' for t in targets))
                self.assertTrue(all(t['statement_filing_hash']==data[0].execute('SELECT content_hash FROM coverage_mission_statement_filings').fetchone()[0] for t in targets))
                self.assertEqual(self.targets(data,target_schema_version='financial-note-target-0.1'),[])

    def test_four_supported_forms_preserve_identity_and_old_annual_round_trip(self):
        for form in ('10-Q','10-K','10-Q/A','10-K/A'):
            with self.subTest(form=form):
                targets=self.targets(self.filing(form=form))
                self.assertEqual(len(targets),3);self.assertTrue(all(t['form']==form for t in targets))
                for t in targets:self.assertEqual(normalize_evidence_target(t),t)
        old=self.filing(form='10-K');old[0].execute("UPDATE coverage_mission_statement_filings SET report_date='2024-12-31'")
        # A changed report_date without rehashing is not new authority.
        self.assertEqual(self.targets(old),[])

    def test_duplicate_currency_or_dimension_cannot_advertise_ambiguous_period(self):
        base=self.filing()[3][2]
        for change in ({'unit':'EURPerShare'},{'value':'2.5'}):
            extra={**base,**change};targets=self.targets(self.filing(extra=[extra]))
            self.assertFalse(any(t['periods'][0]['period_end']=='2025-09-30' for t in targets))
        dimensional={**base,'dimension_axis':'a','dimension_member':'m','dimension_count':1}
        self.assertEqual(len(self.targets(self.filing(extra=[dimensional]))),3)

    def test_tampered_source_hash_mission_company_and_unsupported_form_do_not_advertise(self):
        data=self.filing();data[0].execute("UPDATE coverage_mission_statement_lines SET value='999' WHERE ordinal=0")
        self.assertEqual(self.targets(data),[])
        for kind in ('mission','company','form'):
            data=self.filing()
            if kind=='mission':data[1]['content_hash']='0'*64
            elif kind=='company':data[2]['source_authority']['company_ref']='other'
            else:data[0].execute("UPDATE coverage_mission_statement_dispatches SET form='8-K'")
            self.assertEqual(self.targets(data),[])

    def test_schema_versions_do_not_extend_old_scope_or_accept_new_ytd(self):
        t=self.targets(self.filing())[0]
        for change in ({'schema_version':'financial-note-target-0.1'},{'form':'8-K'},
                       {'schema_version':'financial-note-target-0.9'},
                       {'applicability_kind':'quarter','periods':[{'period_start':'2025-01-01','period_end':'2025-09-30'}]}):
            with self.subTest(change=change),self.assertRaises(DocumentResearchStrategyError):normalize_evidence_target({**t,**change})

    def test_output_schema_extends_only_when_new_exact_target_is_advertised(self):
        self.assertIs(_history_note_prompt_schema({'companies':[]}),OUTPUT_SCHEMA)
        t=self.targets(self.filing())[0];state={'companies':[{'readable_documents':[{'evidence_targets':[t]}]}]}
        schema=_history_note_prompt_schema(state)
        p=schema['properties']['inquiries']['items']['properties']['directed_document']['properties']['evidence_target']['oneOf'][1]['properties']
        self.assertIn('10-Q',p['form']['enum'])
        self.assertEqual(OUTPUT_SCHEMA['properties']['inquiries']['items']['properties']['directed_document']['properties']['evidence_target']['properties']['form'],{'const':'10-K'})

    def test_exact_modern_companyfacts_context_is_bound_and_tampering_is_refused(self):
        import hashlib
        from tests.test_ar_segment_bridge_identity import ContextBackfillTests, COMPANY
        from dalton_core.statement_context import StatementContextAuthority
        from dalton_core.sec_financials_normalise import normalise_filing
        from dalton_core.raw_spool import RawSpool
        from dalton_core.store import canonical_json
        from dalton_core.financial_note_evidence import _statement_filing, FinancialNoteEvidenceError
        f=ContextBackfillTests('test_companyfacts_supplement_uses_independent_refs_and_same_series_proof_resolver')
        f.setUp();self.addCleanup(f.doCleanups)
        rawfiling={'accession':'0000999999-26-000001','form':'10-Q','filed':'2026-08-01','report_date':'2026-06-30',
          'statements':{'income':{'structure':[{'concept':'us-gaap_EarningsPerShareDiluted','label':'Diluted EPS',
            'level':1,'parent_concept':None,'abstract':False,'is_breakdown':False,'balance':None}],
            'facts':[{'concept':'us-gaap:EarningsPerShareDiluted','period_start':'2026-04-01','period_end':'2026-06-30',
                      'value':'1.25','unit_ref':'usdPerShare','balance':None,'dim_srt_ConsolidationItemsAxis':None}]}}}
        f.raw_parse({'cik':'0000999999','entity_name':'Synthetic Company','filings':[rawfiling]})
        lines=normalise_filing(**{k:rawfiling[k]for k in ('accession','form','filed','report_date','statements')})['lines']
        filing,_=f.ingest(lines)
        f.authority.settle_statement_dispatch(f.dispatch,outcome='succeeded')
        fact={'start':'2026-04-01','end':'2026-06-30','val':100,'accn':filing['accession'],'filed':filing['filed'],'form':'10-Q'}
        payload={'cik':999999,'facts':{'us-gaap':{'WeightedAverageNumberOfDilutedSharesOutstanding':{'units':{'shares':[fact]}}}}}
        raw=canonical_json(payload).encode();digest=hashlib.sha256(raw).hexdigest()
        spool=RawSpool(str(f.state_dir/'raw-spool'),max_total_bytes=1000000)
        sink=spool.open_sink('raw-sink:'+digest,max_response_bytes=100000);sink.write(raw);sink.finalize()
        context=StatementContextAuthority(f.store).backfill(f.state_dir,filing['ingest_id'],
            companyfacts_source={'raw_hash':digest,'facts':payload['facts']},discover_companyfacts=False)
        self.assertEqual(context['status'],'available',context.get('reason'))
        d=f.store.connection.execute('SELECT * FROM coverage_mission_statement_dispatches WHERE dispatch_id=?',(f.dispatch,)).fetchone()
        m=f.authority._mission_record(d['mission_version_ref'])
        reg={'source_ref':'source:sec-edgar','document_ref':'sec:filing:'+filing['accession'],
             'source_authority':{'kind':'coverage-mission-acquired-document','company_ref':COMPANY,'mission_version_ref':m['id']}}
        targets=financial_note_targets_for_registration(connection=f.store.connection,mission=m,company_ref=COMPANY,registration=reg)
        self.assertEqual(len(targets),1);target=targets[0]
        self.assertEqual(target['statement_context_ref'],context['version_id'])
        self.assertEqual(target['statement_context_hash'],context['content_hash'])
        self.assertEqual(_statement_filing(f.store.connection,target,company_ref=COMPANY,registration=reg)['ref'],filing['ingest_id'])
        bad={**target,'statement_context_hash':'0'*64}
        with self.assertRaisesRegex(FinancialNoteEvidenceError,'context hash'):
            _statement_filing(f.store.connection,bad,company_ref=COMPANY,registration=reg)
        f.store.connection.execute('DROP TRIGGER statement_context_no_update')
        changed=copy.deepcopy(context);changed['lines'][-1]['value']='999'
        f.store.connection.execute('UPDATE statement_context_versions SET record_json=? WHERE version_id=?',
            (canonical_json(changed),context['version_id']))
        self.assertEqual(financial_note_targets_for_registration(connection=f.store.connection,mission=m,
            company_ref=COMPANY,registration=reg),[])

    def test_producer_refuses_different_period_metric_and_basis(self):
        from dalton_core.document_research_qualitative import parse_directed_draft_text
        from dalton_core.annual_report_qualitative import AnnualReportQualitativeError
        target=self.targets(self.filing())[0]
        from dalton_core.document_research_strategy import history_note_candidate_scope
        scope=history_note_candidate_scope(target)
        body={'schema_version':'0.1','status':'answered','answer':'Issuer attribution is disclosed.',
              'candidate':{**scope,'normalized_statement':'Issuer attributes its diluted numerator to parent earnings.',
                           'cited_match_indexes':[0]},'missing':[]}
        metadata={'draft_contract_version':'0.2','question':'How does the issuer attribute diluted earnings?',
                  'retrieval_match_count':1,'financial_note_target':target}
        self.assertEqual(parse_directed_draft_text(json.dumps(body),metadata=metadata),body)
        for key,value in [('period','2025-07-01/2025-09-30'),('metric_or_aspect','operating income'),('basis','estimate')]:
            changed=copy.deepcopy(body);changed['candidate'][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(AnnualReportQualitativeError,'metric/period/basis'):
                parse_directed_draft_text(json.dumps(changed),metadata=metadata)
        wrong=copy.deepcopy(target);wrong['periods'].append({'period_start':'2025-07-01','period_end':'2025-09-30'})
        with self.assertRaisesRegex(DocumentResearchStrategyError,'one exact period'):normalize_evidence_target(wrong)

    def test_real_quarter_promotion_flows_through_model_context_without_numeric_invention(self):
        helper=note_fixtures.FinancialNoteResolverTests();self.addCleanup(helper.doCleanups)
        fixture,executor,admission,target=helper._completed_typed(target_options={
            'form':'10-Q','start':'2025-07-01','end':'2025-09-30','report_date':'2025-12-31','kind':'quarter'})
        full=resolve_financial_note_evidence(core_connection=fixture.store.connection,
            router_connection=fixture.router.connection,staging_connection=executor.staging.connection,
            registry=executor.registry,admission_ref=admission['id'])
        self.assertEqual(full['periods'],target['periods']);self.assertEqual(full['form'],'10-Q')
        context=FinancialNoteReadContext(store=fixture.store,state_dir=fixture.state)
        context._registry=executor.registry;context._router=fixture.router.connection;context._staging=executor.staging.connection
        context._config={'fixture':True};context._config_file_sha256='8'*64
        from unittest.mock import patch
        with patch('dalton_core.financial_note_context._read_config',return_value=({'fixture':True},'8'*64)):
            projected=context.projection(admission['company_ref'])
            binding=projected['records'][0]['binding']
            self.assertEqual(binding['periods'],target['periods']);self.assertEqual(binding['applicability_kind'],'quarter')
            self.assertEqual(context.resolver(binding['ref'],expected_context=projected),binding)
        context._router=context._staging=None
        # It is provenance-bound note prose; no amount has been guessed or inserted.
        self.assertNotIn('value',full);self.assertNotIn('value',binding)
