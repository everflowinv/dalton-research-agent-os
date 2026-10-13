"""Quality sources certify an original activity/claim, never a requested ticker."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import initial_screen_quality_sources as sources
from dalton_core import research_quality_score as score
from dalton_core import mission_quality_score_lane as lane
from dalton_core.initial_screen_quality_feedback import read_feedback
from dalton_core.material_request_citation import request_citation_resolver
from dalton_core.material_requests import open_request
from dalton_core.store import content_hash

A='company:synthetic:alpha'; B='company:synthetic:beta'
NAMES={A:['Alpha Systems','ALPH'],B:['Beta Products','BETA']}


def proof(text, header=None):
    header=text if header is None else header
    return {'document_ref':'doc:one','document_sha256':'a'*64,'text':text,
            'quality_source':{'header':header,'header_hash':content_hash(header),'names':NAMES}}


def section(text, ref='doc-passage:doc:one:0-80:aaaaaaaaaaaa', numbers=()):
    return {'title':'Business','body':text,'numbers':list(numbers),'source_refs':[{'ref':ref,'company_ref':A,'company_refs':[A,B],'source_kind':'transcript'}]}


class SourceScopeTests(unittest.TestCase):
    def test_two_issuers_call_roles_use_original_activity_and_event_date(self):
        for issuer,name in ((A,'Alpha Systems'),(B,'Beta Products')):
            p=proof(f'Welcome to the {name} quarter two 2026 financial results teleconference. Joining the CEO and CFO from Investor Relations. Management views as of today, July 30th, 2026.')
            role=sources.call_role(p,issuer)
            self.assertEqual(role['role'],'issuer_earnings_call');self.assertEqual(role['event_date'],'2026-07-30')
            self.assertEqual(sources.call_role(p,B if issuer==A else A)['role'],'unverified')

    def test_keyword_and_title_not_issuer_call_and_missing_date_not_upload_date(self):
        for text in ('Alpha Systems Q2 2026 transcript: 发言人专家：品牌数据交流。',
                     'Alpha Systems financial results teleconference summary by a broker.',
                     'Welcome to the Alpha Systems financial results teleconference. CEO and CFO speaking.'):
            self.assertNotEqual(sources.call_role(proof(text),A)['role'],'issuer_earnings_call')
        p=proof('Welcome to the Beta Products financial results conference call comparing Alpha Systems. CFO. Views as of today, July 30, 2026.')
        self.assertEqual(sources.call_role(p,A)['role'],'unverified')

    def test_raw_header_tampering_rejected(self):
        p=proof('Alpha Systems');p['quality_source']['header']+='changed'
        with self.assertRaisesRegex(ValueError,'header hash'):sources.call_role(p,A)

    def test_peer_mentions_are_not_issuer_misattribution(self):
        ref='doc-passage:doc:one:0-80:aaaaaaaaaaaa'
        for text in ('Alpha Systems competes with Beta Products in this market.',
                     'Beta Products competes with Alpha Systems.'):
            s=section('Alpha Systems 与 Beta Products 的竞争比较。')
            self.assertIsNone(sources.issuer_finding(s,s['source_refs'][0],A,{'proofs':{ref:proof(text)}}))

    def test_foreign_number_cannot_be_assigned_to_subject_or_hidden_in_peer_list(self):
        ref='doc-passage:doc:one:0-80:aaaaaaaaaaaa'
        s=section('Alpha Systems revenue was $99 million.',numbers=[{'text':'revenue was $99 million','passage_ref':ref}])
        item=s['source_refs'][0];item['company_refs']=[A]
        p=proof('Alpha Systems competes with Beta Products. Beta Products revenue was $99 million.')
        self.assertEqual(sources.issuer_finding(s,item,A,{'proofs':{ref:p}}),'numeric_issuer_binding_unverified')
        own=proof('Beta Products is a competitor. Alpha Systems revenue was $99 million.')
        self.assertIsNone(sources.issuer_finding(s,item,A,{'proofs':{ref:own}}))

    def test_peer_fact_without_comparison_or_missing_original_stays_unverified(self):
        ref='doc-passage:doc:one:0-80:aaaaaaaaaaaa';s=section('Alpha Systems is expanding.')
        self.assertEqual(sources.issuer_finding(s,s['source_refs'][0],A,{'proofs':{ref:proof('Beta Products is expanding.')}}),'issuer_binding_unverified')
        self.assertEqual(sources.issuer_finding(s,s['source_refs'][0],A,{'proofs':{}}),'issuer_binding_unverified')

    def test_request_tokens_require_exact_same_section_company_period_and_keep_amount(self):
        for issuer in (A,B):
            with tempfile.TemporaryDirectory() as tmp:
                req=open_request(tmp,requester={'lane':'initial_screen'},subject=issuer,
                                 need={'kind':'document','doc_kinds':['sec_exhibit']},period='quarter_ending:2026-06-30')
                s={'title':'Gaps','body':req['id'],'numbers':[],
                   'gap_items':[{'kind':'not_acquired','need':'earnings release','request_ref':req['id'],'period':req['period']}]}
                a={'sections':[s]};context={'resolve_request':request_citation_resolver(tmp,issuer)}
                before=Path(tmp,'material-requests.sqlite').read_bytes()
                self.assertEqual(score.check_numbers_without_refs(a,context)['status'],'pass')
                for bad in ({**s,'body':s['body']+' Revenue987654USD'},
                            {**s,'gap_items':[]},
                            {**s,'gap_items':[{**s['gap_items'][0],'period':'quarter_ending:2025-06-30'}]}):
                    self.assertEqual(score.check_numbers_without_refs({'sections':[bad]},context)['status'],'fail')
                self.assertEqual(score.check_numbers_without_refs(a,{'resolve_request':request_citation_resolver(tmp,'wrong-company')})['status'],'fail')
                self.assertEqual(Path(tmp,'material-requests.sqlite').read_bytes(),before)

    def test_latest_uses_verified_issuer_event_and_keeps_missing_release(self):
        a={'subject_ref':A,'sections':[section('Analysis','doc-passage:doc:issuer:0-80:aaaaaaaaaaaa')],
           'contract':{'pack':{'inputs':[{'ref':'doc:expert','doc_kind':'transcript','date':'2026-09-14'}, {'ref':'doc:issuer','doc_kind':'transcript','date':'2026-07-31'}]}}}
        context={'source_state_supplied':True,'quality_sources':{'call_roles':{'doc:expert':{'role':'third_party_discussion'},'doc:issuer':{'role':'issuer_earnings_call','event_date':'2026-07-30'}}}}
        result=score.check_latest_transcript_and_release_cited(a,context)
        self.assertEqual([f['code'] for f in result['findings']],['no_release_cited'])
        context['quality_sources']=None
        self.assertEqual(score.check_latest_transcript_and_release_cited(a,context)['findings'][0]['code'],'earnings_call_source_unavailable')


class FreeScorerWakeTests(unittest.TestCase):
    def test_old_scored_target_gets_fixed_free_refresh_and_current_score_stops_it(self):
        from tests.test_mission_quality_score_lane import _core,_publish
        from dalton_core.research_quality_rubrics import rubric
        with tempfile.TemporaryDirectory() as tmp:
            c=_core(Path(tmp)/'core.sqlite');self.addCleanup(c.close)
            _publish(c,'mission-deliverable:initial_screen:1','v1','h1','2026-09-14')
            c.execute('INSERT INTO research_quality_score_versions(version_id,target_ref,target_hash,rubric_ref,scorer_version) VALUES(?,?,?,?,?)',('old','v1','h1',rubric('initial_screen').rubric_ref,'0.4'))
            target=lane.pending_targets(c)[0];self.assertTrue(target['deterministic_refresh'])
            c.execute('INSERT INTO research_quality_score_versions(version_id,target_ref,target_hash,rubric_ref,scorer_version) VALUES(?,?,?,?,?)',('new','v1','h1',rubric('initial_screen').rubric_ref,score.SCORER_VERSION))
            self.assertEqual(lane.pending_targets(c),[])
            self.assertNotEqual(lane.run_digest('v1','h1'), 'legacy digest')

    def test_current_or_old_failed_judge_cannot_be_replaced_with_free_refresh(self):
        from tests.test_mission_quality_score_lane import _core,_publish
        from dalton_core.research_quality_rubrics import rubric
        for version in ('0.4',score.SCORER_VERSION):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                c=_core(Path(tmp)/'core.sqlite')
                try:
                    _publish(c,'mission-deliverable:initial_screen:1','v1','h1','2026-09-14')
                    c.execute('INSERT INTO research_quality_score_versions(version_id,target_ref,target_hash,rubric_ref,scorer_version,judge_status) VALUES(?,?,?,?,?,?)',('failed','v1','h1',rubric('initial_screen').rubric_ref,version,'failed'))
                    target=lane.pending_targets(c)[0]
                    self.assertNotIn('deterministic_refresh',target)
                finally:c.close()

    def test_free_refresh_omits_both_paid_configs_and_does_not_reset_paid_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher=lane.QualityScoreLauncher(state_dir=tmp,judge_model_config_path=Path(tmp)/'judge',verifier_model_config_path=Path(tmp)/'verifier')
            command=launcher._command(ticket_dir=Path(tmp),target_ref='version:one',deterministic_refresh=True)
            self.assertNotIn('--model-config',command);self.assertNotIn('--verifier-model-config',command)
            normal=launcher._command(ticket_dir=Path(tmp),target_ref='version:one')
            self.assertIn('--model-config',normal);self.assertIn('--verifier-model-config',normal)

    def test_restart_same_scorer_failure_holds_but_old_scorer_does_not(self):
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc);coordinator=lane.QualityScoreLaneCoordinator(connection=lambda:None,launcher=None,clock=lambda:now)
        target={'target_ref':'v','target_hash':'h','deterministic_refresh':True}
        old={'target_ref':'v','target_hash':'h','started_at':now.isoformat(),'scorer_version':'0.4'}
        self.assertFalse(coordinator._recently_failed([old],target))
        self.assertTrue(coordinator._recently_failed([{**old,'scorer_version':score.SCORER_VERSION}],target))


class FeedbackTests(unittest.TestCase):
    def fixture(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close)
        c.executescript('CREATE TABLE mission_deliverable_pointer(version_id TEXT);CREATE TABLE mission_deliverable_versions(version_id TEXT,record_json TEXT,content_hash TEXT,subject_ref TEXT,kind TEXT,created_at TEXT);CREATE TABLE research_quality_score_versions(record_json TEXT,content_hash TEXT,target_ref TEXT,target_hash TEXT,rubric_ref TEXT,rubric_hash TEXT,scorer_version TEXT,created_at TEXT);')
        target={'id':'v','subject_ref':A,'kind':'initial_screen','sections':[{'gap_items':[{'kind':'not_acquired','need':'release','request_ref':'existing-MR'}]}]}
        target['content_hash']=content_hash(target);c.execute('INSERT INTO mission_deliverable_pointer VALUES(?)',('v',));c.execute('INSERT INTO mission_deliverable_versions VALUES(?,?,?,?,?,?)',('v',json.dumps(target),target['content_hash'],A,'initial_screen','now'))
        from dalton_core.research_quality_rubrics import INITIAL_SCREEN_STRL as r
        deterministic={'scorer_version':score.SCORER_VERSION,'target_ref':'v','target_hash':target['content_hash'],'checks':[{'check':'latest_transcript_and_release_cited','status':'fail','findings':[{'code':'no_release_cited'}]}]}
        record={'id':'quality:v','target_ref':'v','target_hash':target['content_hash'],'rubric_ref':r.rubric_ref,'rubric_hash':r.content_hash,'scorer_version':score.SCORER_VERSION,'deterministic':deterministic}
        record['content_hash']=content_hash(record)
        c.execute('INSERT INTO research_quality_score_versions VALUES(?,?,?,?,?,?,?,?)',(json.dumps(record),record['content_hash'],'v',target['content_hash'],r.rubric_ref,r.content_hash,score.SCORER_VERSION,'now'))
        return c

    def test_strong_current_feedback_has_existing_material_and_no_repair_authority(self):
        c=self.fixture();before=c.total_changes;out=read_feedback(c,company_ref=A)
        self.assertEqual(out['status'],'verified');self.assertEqual(out['items'][0]['existing_material_gaps'][0]['request_ref'],'existing-MR')
        self.assertIn('consumed',out['repair_authority']);self.assertEqual(c.total_changes,before)
        self.assertEqual(read_feedback(c,company_ref=B),{'status':'not_recorded'})

    def test_corrupt_record_or_target_projection_is_not_planning_evidence(self):
        for sql in ("UPDATE research_quality_score_versions SET content_hash='wrong'", "UPDATE mission_deliverable_versions SET version_id='foreign'"):
            c=self.fixture();c.execute(sql)
            if 'score' in sql:
                with self.assertRaises(ValueError):read_feedback(c,company_ref=A)
            else:self.assertEqual(read_feedback(c,company_ref=A),{'status':'not_recorded'})



class StrongOriginalTests(unittest.TestCase):
    def fixture(self):
        from tests.test_evidence_index import CorpusBuilder
        from tests.p9a_fixtures import bootstrap_method_authorities,mission_params
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.store import DaltonStore
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        builder=CorpusBuilder(root);self.addCleanup(builder.close)
        store=DaltonStore(':memory:');self.addCleanup(store.close)
        params=mission_params(bootstrap_method_authorities(store));ref=params.pop('mission_ref')
        mission=CoverageMissionAuthority(store).create_mission(ref,**params)
        mission.pop('status_marker',None);mission.pop('status',None)
        builder.db.execute('ALTER TABLE coverage_mission_versions ADD COLUMN content_hash TEXT')
        builder.db.execute('UPDATE coverage_mission_versions SET mission_version_id=?,mission_ref=?,record_json=?,content_hash=?',(mission['id'],mission['mission_ref'],json.dumps(mission),mission['content_hash']))
        builder.db.execute('UPDATE coverage_mission_pointer SET mission_version_id=?,mission_ref=?',(mission['id'],mission['mission_ref']))
        builder.db.commit()
        return root,builder,mission

    def test_acquired_raw_metadata_optional_legacy_output_byte_compatible_and_tamper_refused(self):
        from dalton_core import evidence_index as index
        from dalton_core.initial_screen_failure_feedback import _checked_originals
        root,builder,mission=self.fixture()
        for number, (issuer,name) in enumerate((('company:sec-cik:0001467373','Accenture'),('company:sec-cik:0001058290','Cognizant'))):
            text=f'Welcome to the {name} Q2 2026 financial results teleconference. CFO and CEO. Views as of today, July 30, 2026. ' + 'Operating results were reported as filed. '*30
            doc=builder.alphaengine('quality'+str(number),str(500000+number),text,spec='earnings-call-transcripts',title='Misleading upload title',broker=None,published='2026-09-14',named=[name],company=issuer)
            builder.db.commit();index.index_acquired_manifest(root,root/'acquisitions'/('quality'+str(number))/'manifest.json')
            span=index.passages_for_document(root,doc)[0];ref=span['passage_ref']
            old=_checked_originals(root,{ref});again=_checked_originals(root,{ref},include_quality_metadata=False)
            self.assertEqual(json.dumps(old,sort_keys=True),json.dumps(again,sort_keys=True))
            self.assertNotIn('quality_source',old[ref])
            new=_checked_originals(root,{ref},include_quality_metadata=True)
            self.assertEqual(sources.call_role(new[ref],issuer)['event_date'],'2026-07-30')
            self.assertEqual(new[ref]['quality_source']['mission_hash'],mission['content_hash'])
            manifest=root/'acquisitions'/('quality'+str(number))/'manifest.json'
            wire=json.loads(manifest.read_text());wire['declared_content_sha256']='0'*64;manifest.write_text(json.dumps(wire))
            with self.assertRaises(ValueError):_checked_originals(root,{ref},include_quality_metadata=True)

    def test_declared_peer_metric_does_not_assign_peer_value_to_target(self):
        ref='doc-passage:doc:one:0-80:aaaaaaaaaaaa';s=section('Alpha Systems 对比 Beta Products 的收入99美元。',numbers=[{'metric':'Beta Products revenue','text':'revenue99美元','passage_ref':ref}])
        p=proof('Alpha Systems is a peer. Beta Products revenue99美元.')
        self.assertIsNone(sources.issuer_finding(s,s['source_refs'][0],A,{'proofs':{ref:p}}))
        s['numbers'][0]['metric']='Alpha Systems revenue'
        self.assertEqual(sources.issuer_finding(s,s['source_refs'][0],A,{'proofs':{ref:p}}),'numeric_issuer_binding_unverified')

    def test_paid_day_cap_does_not_block_one_fixed_zero_model_refresh(self):
        from datetime import datetime,timezone
        from tests.test_mission_quality_score_lane import FakeLauncher
        now=datetime.now(timezone.utc);launcher=FakeLauncher();launcher.clock=lambda:now
        launcher.written=[{'started_at':now.isoformat()}]*lane.MAX_LAUNCHES_PER_DAY
        target={'target_ref':'version','target_hash':'hash','rubric':'initial_screen','deterministic_refresh':True,'deliverable_ref':'initial:one'}
        coordinator=lane.QualityScoreLaneCoordinator(connection=lambda:None,launcher=launcher,clock=lambda:now)
        with patch.object(lane,'pending_targets',return_value=[target]):
            result=coordinator.dispatch_once()
        self.assertEqual(result['status'],'launched');self.assertTrue(result['limits']['day_cap_reached'])
        self.assertTrue(launcher.started[0]['deterministic_refresh'])

class FeedbackBindingTests(unittest.TestCase):
    fixture = FeedbackTests.fixture
    def test_rehashed_foreign_deterministic_binding_refuses(self):
        c=self.fixture();row=c.execute('SELECT record_json FROM research_quality_score_versions').fetchone();record=json.loads(row[0])
        record['deterministic']['target_hash']='foreign'
        record['content_hash']=content_hash({k:v for k,v in record.items() if k!='content_hash'})
        c.execute('UPDATE research_quality_score_versions SET record_json=?,content_hash=?',(json.dumps(record),record['content_hash']))
        with self.assertRaisesRegex(ValueError,'deterministic binding'):read_feedback(c,company_ref=A)

    def test_quality_packet_is_preserved_in_seventeen_company_prompt_and_stable_state(self):
        from tests.test_research_planner import state,entry
        from dalton_core.research_planner import build_prompt
        from dalton_core.research_state import state_digest
        c=self.fixture();feedback=read_feedback(c,company_ref=A)
        entries=[entry('issuer:'+str(i),'TEST'+str(i)) for i in range(17)]
        full=state(checklist=entries,initial_screen_quality_feedback_by_company={e['company_ref']:feedback for e in entries})
        again=state(checklist=entries,initial_screen_quality_feedback_by_company={e['company_ref']:feedback for e in entries})
        self.assertEqual(state_digest(full),state_digest(again))
        prompt=build_prompt(full)
        self.assertLess(len(prompt.encode()),192000)
        self.assertIn('initial_screen_quality_feedback',prompt)
        self.assertIn('no_release_cited',prompt)
        self.assertIn('no new content repair attempt',prompt)
