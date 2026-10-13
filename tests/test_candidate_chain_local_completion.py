"""A fixed local continuation archives exact already-paid Figure results."""
import copy
import json
import types
import unittest
from unittest.mock import patch

from tests import test_directed_numeric_figure_support as support
from tests import test_candidate_exact_evidence_reuse as evidence
from tests import test_document_completed_model_replay as replay
from dalton_core.store import content_hash
from dalton_core.document_completed_model_replay import candidate, CONTRACT, CHAIN_CONTRACT, CHAIN_ERROR
from dalton_core.directed_numeric_local_reconciliation import _chain,reconcile
from dalton_core.claim_index_figures import promote_figure
from dalton_core.research_verification import CandidateStagingStore,ResearchVerificationConflict
from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
from dalton_core.lane_child_launcher import LaneChildRejected


class PriorLabelIdentity(evidence.LegacyPeriodResolver):
    def identity(self,figure):
        # A prior deterministic label normalizer named the same filed fact
        # differently. This changes claim identity, not evidence or amount.
        return {**super().identity(figure),'label':figure['as_reported_label'].title(),
                'version':'hermetic-prior-label:0.1'}


class CandidateChainLocalCompletionTests(unittest.TestCase):
    def test_real_old_chain_failure_reuses_paid_prefix_and_exact_evidence_then_canonical_completion(self):
        h=support.NumericFigureSupportTests('runTest');self.addCleanup(h.doCleanups)
        w,f,a,admission,ex,p,v=h.fixture()
        figures=h.seed(f,a,admission,ex,p)
        old=promote_figure(f.store,ex.staging,figure=figures[0],actor_ref=admission['actor_ref'],
            idempotency_key='legacy-exact-evidence',resolver=PriorLabelIdentity(f.store.connection))
        previous=h.rows(ex.staging.connection,'candidate_evidence_versions')
        check=CandidateStagingStore._check_candidate_chain
        def historical_gate(cur,**kwargs):
            kwargs.pop('exact_reusable',None)
            return check(cur,**kwargs)
        with patch.object(CandidateStagingStore,'_check_candidate_chain',side_effect=historical_gate):
            with self.assertRaisesRegex(ResearchVerificationConflict,'candidate version chain mismatch'):
                w.drive(ex,admission)
        works=_chain(ex,admission);f.harness.clock.advance(1800)
        ex.scheduler.sweep_expired()
        paid={t:h.rows(f.store.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')}
        parent=ex.scheduler.work_order_authority(works[3]['id']);attempts=ex.scheduler.attempt_history(works[3]['id'])
        with (patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no extra model')),
              patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no extra verifier'))):
            done=reconcile(ex,admission['id'],expected_admission_hash=admission['content_hash'],requested_by='automation:synthetic')
            self.assertEqual(done['model_calls'],0);self.assertEqual(done['research_status'],'numeric_targets_completed')
            self.assertEqual(reconcile(ex,admission['id'],expected_admission_hash=admission['content_hash'],requested_by='automation:synthetic'),done)
            self.assertEqual(ex.run_once(admission['id'])['numeric_result'],done['numeric_result'])
        self.assertEqual((p.calls,v.calls),(1,1));self.assertEqual(paid,{t:h.rows(f.store.connection,t) for t in paid})
        self.assertEqual(ex.scheduler.work_order_authority(works[3]['id']),parent)
        self.assertEqual(ex.scheduler.attempt_history(works[3]['id']),attempts)
        self.assertTrue(all(row in h.rows(ex.staging.connection,'candidate_evidence_versions') for row in previous))
        work=ex.scheduler.work_order_authority(done['numeric_result']['work_order_ref'])['work_order']
        self.assertEqual(work['budget']['max_cost_usd'],0);self.assertEqual(work['budget']['max_attempts'],1)
        self.assertEqual(len(done['numeric_result']['canonical_claims']),3)

    def chain_proof_fixture(self):
        h=replay.CompletedModelReplayTests('runTest');self.addCleanup(h.doCleanups)
        f,admission,ex,c,ticket=h.proof_fixture()
        old=candidate(c,admission,ticket['id'])
        body={k:v for k,v in ticket['summary'].items() if k!='content_hash'};body['error']=CHAIN_ERROR
        ticket['summary']={**body,'content_hash':content_hash(body)}
        c.launcher._ticket_path(ticket['id']).with_name('summary.json').write_text(json.dumps(ticket['summary']))
        return f,admission,ex,c,ticket,old

    def test_legacy_proof_preserved_chain_exact_summary_new_contract_and_unknown_refused(self):
        f,admission,ex,c,ticket,old=self.chain_proof_fixture()
        self.assertEqual(old['contract_ref'],CONTRACT)
        new=candidate(c,admission,ticket['id'])
        self.assertEqual(new['contract_ref'],CHAIN_CONTRACT);self.assertEqual(new['model_calls_allowed'],0)
        self.assertEqual(new['completed_stages'],old['completed_stages'])
        for error in ('provider TIMEOUT','CoverageMissionValidationError: existing figure uses different units',
                      CHAIN_ERROR+' extra','ResearchVerificationConflict: different error'):
            body={k:v for k,v in ticket['summary'].items() if k!='content_hash'};body['error']=error
            ticket['summary']={**body,'content_hash':content_hash(body)}
            c.launcher._ticket_path(ticket['id']).with_name('summary.json').write_text(json.dumps(ticket['summary']))
            self.assertIsNone(candidate(c,admission,ticket['id']))

    def test_new_contract_one_child_across_restart_config_day_and_no_paid_budget(self):
        f,admission,ex,c,ticket,old=self.chain_proof_fixture();proof=candidate(c,admission,ticket['id'])
        launcher=object.__new__(MissionDocumentResearchLauncher)
        directory=f.state/'chain-local-ticket';directory.mkdir()
        launcher._ticket_path=lambda ref:directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'profile':'different'}
        spawned=[]
        def spawn(**kw):
            self.assertTrue(kw['completed_models_only']);spawned.append(kw)
            ref=launcher.TICKET_PREFIX+':'+kw['digest'];launcher._ticket_path(ref).write_text('{}')
            return {'id':ref,'status':'running'}
        launcher.spawn=spawn;launcher.status=lambda ref:{'id':ref,'status':'failed'}
        launcher.replay_completed_models(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        launcher.configuration=lambda:{'profile':'another'}
        with self.assertRaisesRegex(LaneChildRejected,'already attempted'):
            launcher.replay_completed_models(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        self.assertEqual(len(spawned),1)
        wrong=copy.deepcopy(proof);wrong['model_calls_allowed']=1;wrong['content_hash']=content_hash({k:v for k,v in wrong.items() if k!='content_hash'})
        with self.assertRaisesRegex(LaneChildRejected,'proof differs'):
            launcher.replay_completed_models(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=wrong)

    def test_missing_or_failed_paid_prefix_durable_summary_drift_and_running_child_refused(self):
        f,admission,ex,c,ticket,old=self.chain_proof_fixture()
        ticket['status']='running';self.assertIsNone(candidate(c,admission,ticket['id']))
        ticket['status']='failed'
        with patch.object(c,'_effective_work_hints',return_value=['work:missing']):
            self.assertIsNone(candidate(c,admission,ticket['id']))
        c.launcher._ticket_path(ticket['id']).with_name('summary.json').write_text('{}')
        self.assertIsNone(candidate(c,admission,ticket['id']))
