"""Real authority regressions for AT, without provider or connector calls."""
import sqlite3
import json
import unittest
from datetime import datetime, timedelta, timezone
from dalton_core.research_question_acceptance import answer_acceptance
from dalton_core.research_question_maintenance import maintain_questions
from dalton_core.store import canonical_json, content_hash
from tests import test_research_question_backlog as backlog_tests
from tests import test_research_review as review_tests
from tests import test_bounded_planner_loop as loop_tests

class QuestionConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.h = backlog_tests.ResearchQuestionBacklogTests('test_full_lifecycle_to_answered')
        self.h.setUp(); self.addCleanup(self.h.doCleanups); self.h.govern()

    def test_rephrasing_one_metric_period_appends_content_without_new_identity(self):
        a = self.h.record(question='What was revenue in 2027Q1?', answer_criteria='revenue', idempotency_key='a')
        b = self.h.record(question='Report 2027Q1 sales.', answer_criteria='revenue', idempotency_key='b')
        self.assertEqual(a['question_ref'], b['question_ref'])
        self.assertEqual(b['status'], 'revised')
        q = self.h.backlog.question(a['question_ref'])
        self.assertEqual(q['head']['version'], 2)
        self.assertEqual(self.h.backlog.connection.execute('SELECT question FROM backlog_question_versions WHERE version_id=?',(a['question_version_ref'],)).fetchone()[0], 'What was revenue in 2027Q1?')
        self.assertEqual(self.h.record(question='Report 2027Q1 sales.', answer_criteria='revenue', idempotency_key='b')['status'], 'duplicate')

    def test_different_period_does_not_merge_and_agenda_creates_no_shadow(self):
        a=self.h.record(question='Revenue in FY2027?',answer_criteria='revenue',idempotency_key='a')
        b=self.h.record(question='Revenue in 2027Q1?',answer_criteria='revenue',idempotency_key='b')
        self.assertNotEqual(a['question_ref'],b['question_ref'])
        self.h.start_decided_cycle('convergence:cycle','convergence:cycle-id','convergence:decision',questions=[{'question':'Which revenue fact?','answer_criteria':'revenue'}])
        self.assertEqual(self.h.store.connection.execute('SELECT COUNT(*) FROM research_question_versions').fetchone()[0],0)
        row=self.h.store.connection.execute('SELECT question_version_ref,canonical_question_version_ref FROM agenda_candidates').fetchone()
        self.assertIsNone(row[0]); self.assertIsNotNone(row[1])

    def test_metric_period_does_not_merge_different_qualitative_mechanisms(self):
        a=self.h.record(question='Why did FY2026 revenue weaken?',answer_criteria='revenue',idempotency_key='why')
        b=self.h.record(question='How did pricing affect FY2026 revenue?',answer_criteria='revenue',idempotency_key='pricing')
        self.assertNotEqual(a['question_ref'],b['question_ref'])
        c=self.h.record(question='为何FY2026营业收入下降？',answer_criteria='营业收入',idempotency_key='why-cn')
        d=self.h.record(question='定价如何影响FY2026营业收入？',answer_criteria='营业收入',idempotency_key='pricing-cn')
        self.assertNotEqual(c['question_ref'],d['question_ref'])

    def test_operational_question_gets_audited_terminal_reason(self):
        q=self.h.record(question='Gateway service status?',answer_criteria='revenue',idempotency_key='ops')
        result=maintain_questions(self.h.store)
        self.assertEqual(result['model_calls'],0)
        self.assertEqual(self.h.backlog.question(q['question_ref'])['state'],'retired')
        self.assertEqual(result['retired'][0]['metadata']['maintenance_kind'],'operations')
        self.assertEqual(maintain_questions(self.h.store)['retired'],[])


    def test_open_unanswered_question_has_zero_budget_material_request(self):
        q = self.h.record(question='Revenue FY2027?',answer_criteria='revenue',idempotency_key='request')
        result = maintain_questions(self.h.store)
        link = self.h.backlog.question(q['question_ref'])['waiting_material_request']
        self.assertIsNotNone(link)
        self.assertEqual(result['waiting_requests'][0]['request_ref'],link['request_ref'])
        self.assertEqual(maintain_questions(self.h.store)['waiting_requests'],[])
        from dalton_core.material_requests import open_requests
        requests=open_requests(self.h.tmp.name)
        self.assertEqual(requests[0]['budget']['max_cost_usd'],0)
        self.assertEqual(requests[0]['budget']['max_attempts'],0)

    def _accept(self, question, criteria, period='2026Q2', docs=()):
        claim={'id':'claim:1','subject_ref':'wanhua','metric_or_aspect':'revenue','normalized_statement':'Revenue was 123','value':123,'period':period}
        self.h.store.connection.execute('CREATE TEMP TABLE acceptance_claims(claim_version_id,claim_json)') if False else None
        # Store authority fixture registers an actual immutable claim.
        head={'id':'question:v1','content_hash':'a'*64,'company_ref':'wanhua','question':question,'answer_criteria':criteria,'source_refs':list(docs)}
        if period!='2026Q2':
            # Local isolated SQLite validates period semantics independently of Ledger shape.
            db=sqlite3.connect(':memory:'); db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE claim_versions(claim_version_id,claim_json)')
            db.execute('INSERT INTO claim_versions VALUES(?,?)',(claim['id'],json.dumps(claim)))
            self.addCleanup(db.close)
            return answer_acceptance(db,head,[{'claim_version_ref':claim['id'],'claim_version_hash':'b'*64}])
        c=self.h.formal_claim('acceptance-'+str(self._testMethodName)+str(content_hash(period))[:8])
        return answer_acceptance(self.h.store.connection,head,[{'claim_version_ref':c['claim_version_id'],'claim_version_hash':c['content_hash']}])

    def test_unanchored_chinese_criteria_refuse(self):
        with self.assertRaisesRegex(ValueError,'no independently checkable'):
            self._accept('定性问题','综合判断并充分解释')

    def test_wrong_annual_and_unproved_fiscal_quarter_refuse(self):
        with self.assertRaisesRegex(ValueError,'reporting-period'):
            self._accept('Revenue FY2026?','revenue',period='2026Q2')
        with self.assertRaisesRegex(ValueError,'reporting-period'):
            self._accept('Revenue fiscal 2026Q2?','revenue',period={'start':'2026-04-01','end':'2026-06-30'})


    def test_fiscal_quarter_both_word_orders_accept_exact_label_without_inventing_annual(self):
        for question in ('Revenue Q3 FY2026?','Revenue FY2026Q3?','Revenue FY2026 Q3?'):
            accepted=self._accept(question,'revenue',period='FY2026Q3')
            self.assertEqual(accepted['period'],'fy2026q3')
        with self.assertRaisesRegex(ValueError,'reporting-period'):
            self._accept('Revenue FY2026Q3?','revenue',period='2026Q3')

class CandidateSlaTests(unittest.TestCase):
    def test_candidate_expires_with_exact_version_and_immutable_reason(self):
        h=review_tests.ResearchReviewTests('runTest'); h.setUp(); self.addCleanup(h.doCleanups)
        claim=h.review.list_candidates()[0]['claim']
        now=datetime.fromisoformat(claim['created_at'])+timedelta(hours=25)
        done=h.review.expire_staged(now=now)
        self.assertEqual(len(done['expired']),1)
        self.assertEqual(h.review.candidate_status(claim['id'])['review_state'],'expired')
        self.assertEqual(h.review.expire_staged(now=now)['expired'],[])
        with self.assertRaises(sqlite3.IntegrityError):
            h.review.connection.execute('DELETE FROM candidate_terminal_events')

class SourceReuseTests(unittest.TestCase):
    def test_same_terminal_source_in_another_loop_is_not_scanned_again(self):
        h=loop_tests.BoundedPlannerLoopTests('test_external_scheduler_crash_replay_and_outcome_use_exact_authority');h.setUp();self.addCleanup(h.doCleanups)
        loop=h._loop();h._run_one(loop)
        second=h._loop(max_rounds=4,max_cost=4)
        proposal=h.authority.propose_next_capital_lease(second['id'])
        result=h.control.admit_proposal(proposal['id'])
        self.assertEqual(result['decision']['reason'],'source_material_unchanged_no_rescan')
        self.assertEqual(h.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],1)


    def test_new_exact_local_source_hash_allows_another_loop_without_changing_query(self):
        h=loop_tests.BoundedPlannerLoopTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        loop=h._loop();h._run_one(loop)
        h.store.register_evidence({'evidence_ref':'source-material:annual','source_type':'sec-filing','source_ref':'source:sec-edgar','independence_group':'SEC','artifact_refs':['annual filing']})
        second=h._loop(max_rounds=4,max_cost=4)
        proposal=h.authority.propose_next_capital_lease(second['id'])
        result=h.control.admit_proposal(proposal['id'])
        self.assertEqual(result['status'],'fresh')
        self.assertEqual(len(result['decision']['source_material_binding']['material_hashes']),1)
        # The frozen proof is part of the append-only decision and repeated
        # material cannot unlock a third copy of the same query.
        h._complete(result['round'], [{'source_location':'accession:0002#lease-note'}])
        third=h._loop(max_rounds=5,max_cost=5)
        proposal=h.authority.propose_next_capital_lease(third['id'])
        repeated=h.control.admit_proposal(proposal['id'])
        self.assertEqual(repeated['decision']['reason'],'source_material_unchanged_no_rescan')
        self.assertEqual(h.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],2)

class AnswerDocumentBindingTests(unittest.TestCase):
    def test_question_source_cannot_substitute_for_claim_evidence_or_missing_kind(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;self.addCleanup(db.close)
        db.executescript('CREATE TABLE claim_versions(claim_version_id,claim_json);CREATE TABLE evidence_relations(claim_version_id,evidence_version_id,relation);CREATE TABLE evidence_versions(evidence_version_id,evidence_json,content_hash);CREATE TABLE coverage_mission_statement_filings(company_ref,accession,form,report_date);')
        acc_k='0000000001-27-000001';acc_q='0000000001-27-000002'
        claim={'id':'c1','subject_ref':'wanhua','metric_or_aspect':'revenue','normalized_statement':'Revenue was 123','value':123,'period':{'kind':'fiscal_year','label':'FY2027','start':'2027-01-01','end':'2027-12-31'}}
        db.execute('INSERT INTO claim_versions VALUES(?,?)',('c1',json.dumps(claim)))
        evidence={'source_ref':'sec:filing:'+acc_q};evidence['content_hash']=content_hash(evidence)
        db.execute('INSERT INTO evidence_versions VALUES(?,?,?)',('e1',canonical_json(evidence),evidence['content_hash']))
        db.execute('INSERT INTO evidence_relations VALUES(?,?,?)',('c1','e1','supports'))
        db.executemany('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?)',[('wanhua',acc_k,'10-K','2027-12-31'),('wanhua',acc_q,'10-Q','2027-12-31')])
        q={'id':'q1','content_hash':'a'*64,'company_ref':'wanhua','question':'Revenue FY2027 10-K?','answer_criteria':'revenue','source_refs':['sec:filing:'+acc_k]}
        with self.assertRaisesRegex(ValueError,"accepted claim's evidence"):
            answer_acceptance(db,q,[{'claim_version_ref':'c1','claim_version_hash':'b'*64}])
        q['question']='Revenue FY2027 10-K and 10-Q?'
        evidence={'source_ref':'sec:filing:'+acc_k};evidence['content_hash']=content_hash(evidence)
        db.execute('UPDATE evidence_versions SET evidence_json=?,content_hash=?',(canonical_json(evidence),evidence['content_hash']))
        with self.assertRaisesRegex(ValueError,"accepted claim's evidence"):
            answer_acceptance(db,q,[{'claim_version_ref':'c1','claim_version_hash':'b'*64}])

class SourceFailureRequestTests(unittest.TestCase):
    def test_two_exact_source_failures_join_material_request_without_third_dispatch(self):
        h=loop_tests.BoundedPlannerLoopTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        for n in (3,4):
            loop=h._loop(max_rounds=n,max_cost=n)
            proposal=h.authority.propose_next_capital_lease(loop['id'])
            admitted=h.control.admit_proposal(proposal['id'])
            work=admitted['round']['work_order_ref'];lease=h.scheduler.claim('worker:fixture',work_order_id=work)
            result={'schema_version':'0.1','id':'result:source-failure:'+str(n),'created_at':'2026-08-23T12:00:00+00:00','work_order_ref':work,'invocation_ref':'invocation:failure:'+str(n),'status':'failed','outputs':{},'actual_side_effects':[],'usage_refs':[],'artifact_refs':[],'error':{'code':'SOURCE_UNAVAILABLE','message':'source absent'},'metadata':{}}
            h.scheduler.complete(work,1,'worker:fixture',lease['lease_token'],result,idempotency_key=result['id'])
            h.control.record_outcome(admitted['round']['id'])
        third=h._loop(max_rounds=5,max_cost=5)
        proposal=h.authority.propose_next_capital_lease(third['id'])
        rejected=h.control.admit_proposal(proposal['id'])
        self.assertTrue(rejected['decision']['reason'].startswith('source_unavailable_material_request:material-request:'))
        self.assertEqual(h.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],2)
        from dalton_core.material_requests import open_requests
        requests=open_requests(h.authority.state_dir)
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['budget']['max_cost_usd'],0)
