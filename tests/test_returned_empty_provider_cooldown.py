from __future__ import annotations

import unittest
from unittest.mock import patch
from dalton_core.transcript_polish_model_worker import RoutedTranscriptPolishModelWorker
from dalton_core.transcript_polish import TranscriptPolishWorker
from dalton_core.contracts import WorkOrder
from tests import test_transcript_polish_model_worker as fixture


class ReturnedEmptyProviderCooldownTests(unittest.TestCase):
    def scenario(self, purpose, *, retry):
        f = fixture.RoutedTranscriptPolishWorkerTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        preferred = fixture.profile()
        preferred.update(id='profile:preferred', profile_version_ref='model-profile-version:preferred:1',
                         model='preferred', family='preferred',
                         credential_slot_ref='credential-slot:openclaw:preferred')
        f.router.register_profile(preferred)
        policy = fixture.policy()
        policy.update(id='model-routing-policy:empty',
                      policy_version_ref='model-routing-policy-version:empty:1',
                      fallback_chains={'tiers': {'cheap': [preferred['id'], fixture.profile()['id']]}},
                      purpose_overrides={purpose: {'mode': 'explicit',
                          'chain': [preferred['id'], fixture.profile()['id']]}})
        policy['filters']['allowed_profile_ids'] = [preferred['id'], fixture.profile()['id']]
        f.router.register_policy(policy)
        work = f._prepare(**({'provider_retry': retry} if retry else {}))
        adapter = fixture.ReturnedFailureSequenceAdapter(fixture.candidate(), ['HOST_EMPTY_COMPLETION'])
        class Worker(RoutedTranscriptPolishModelWorker):
            pass
        Worker.purpose = purpose
        worker = Worker(scheduler=f.scheduler, router=f.router, adapter=adapter,
                        store=f.store, observability=f.observability,
                        polish_worker=TranscriptPolishWorker(f.authority),
                        routing_policy_ref=policy['policy_version_ref'],
                        credential_slot_refs=(preferred['credential_slot_ref'], fixture.profile()['credential_slot_ref']),
                        provider_retry=retry, clock=lambda: fixture.NOW)
        return f, work, adapter, worker, preferred

    def verify_terminal_and_next(self, purpose, retry):
        f, work, adapter, worker, preferred = self.scenario(purpose, retry=retry)
        result = worker.run_once(work)
        self.assertEqual(result['status'], 'failed')
        formal = f.scheduler.formal_result(work.id)
        self.assertEqual(formal['result_envelope']['error']['code'], 'HOST_EMPTY_COMPLETION')
        health = f.router.profile_health_events(profile_id=preferred['id'])
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]['outcome'], 'provider_failure')
        self.assertEqual(health[0]['failure_code'], 'HOST_EMPTY_COMPLETION')
        cooldown = f.router.active_cooldowns()[preferred['id']]
        self.assertEqual(cooldown['reason'], 'provider_unavailable')
        self.assertTrue(worker.run_once(work)['replayed'])
        self.assertEqual(f.scheduler.formal_result(work.id), formal)
        self.assertEqual(f.router.profile_health_events(profile_id=preferred['id']), health)
        self.assertEqual(adapter.selected_profile_ids, [preferred['id']])
        # A normal, genuinely different budget Work follows the original saved
        # chain, skipping the cooled endpoint. No new attempt of failed Work.
        probe_wire = f.probe.to_dict()
        probe_wire['id'] += '-different-input-parent'
        probe_wire['idempotency_key'] += '-different-input-parent'
        probe = WorkOrder.from_dict(probe_wire)
        f.scheduler.enqueue(probe)
        prepared = f.coordinator.prepare(probe, max_input_tokens=10000,
            max_output_tokens=4001, max_cost_usd=1, max_seconds=60,
            **({'provider_retry': retry} if retry else {}))
        next_work = prepared['work_order']
        result = worker.run_once(next_work)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(adapter.selected_profile_ids, [preferred['id'], fixture.profile()['id']])
        decisions = f.router.list_decisions(work_order_id=next_work['id'])
        self.assertTrue(any('provider_cooldown' in c['rejection_reasons']
            for d in decisions for c in d['candidate_snapshot']
            if c['profile_version_ref'] == preferred['profile_version_ref']))
        self.assertEqual(f.router.get_policy('model-routing-policy-version:empty:1')['purpose_overrides'][purpose]['chain'],
                         [preferred['id'], fixture.profile()['id']])

    def test_direct_provider_retry_records_empty_and_next_normal_work_falls_back(self):
        for purpose in ('document_extraction', 'claim_index'):
            with self.subTest(purpose=purpose):
                self.verify_terminal_and_next(purpose, {'max_same_profile_retries': 1, 'retry_backoff_seconds': 0})

    def test_chain_retains_paid_failed_envelope_without_false_served_health(self):
        self.verify_terminal_and_next('document_extraction', None)

    def test_health_failure_does_not_buy_retry_or_erase_formal(self):
        f, work, adapter, worker, _ = self.scenario('document_extraction', retry=None)
        with patch.object(f.router, 'record_provider_outcome', side_effect=RuntimeError('health unavailable')):
            self.assertEqual(worker.run_once(work)['status'], 'failed')
        self.assertTrue(worker.run_once(work)['replayed'])
        self.assertEqual(len(adapter.invocation_ids), 1)

    def test_other_failure_never_becomes_served_or_new_retry_class(self):
        f, work, adapter, worker, preferred = self.scenario('document_extraction', retry=None)
        adapter.failures = ['PROVIDER_BUDGET_EXCEEDED']
        self.assertEqual(worker.run_once(work)['status'], 'failed')
        self.assertEqual(f.router.profile_health_events(profile_id=preferred['id']), [])
        self.assertEqual(len(adapter.invocation_ids), 1)

class ExtractionEmptyAccountingTests(unittest.TestCase):
    def setUp(self):
        from tests import test_document_extraction_output_hold as extraction_fixture
        self.f = extraction_fixture.ExtractionOutputHoldTests('test_paid_failure_next_normal_window_uses_owner_next_profile_without_same_window_retry')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def respond(self, adapter, work, route, profile, *, before_send=None):
        from dataclasses import replace
        invocation, result = self.f.respond(adapter, work, route, profile, before_send=before_send)
        if profile['id'] == self.f.f.pr['id']:
            invocation = replace(invocation, usage={
                "input_tokens": None, "output_tokens": None, "total_tokens": None,
                "cache_read_tokens": None, "cache_write_tokens": None,
                "raw_provider_telemetry": {"cost": {"available": False, "usd": None}}})
            result = replace(result, status='failed', outputs={}, usage_refs=(),
                error={'code': 'HOST_EMPTY_COMPLETION', 'source': 'openclaw-model-broker', 'message': 'provider unavailable or usage-limited'},
                metadata=dict(result.metadata) | {'dispatch_proof': None})
        return invocation, result

    def generate(self, offset=0):
        from dalton_core.openclaw_model_adapter import OpenClawModelAdapter
        self.f.f.h.params['offset'] = offset
        with patch.object(OpenClawModelAdapter, 'execute', autospec=True, side_effect=self.respond):
            return self.f.f.h.generate()

    def test_unknown_empty_fee_reservation_retained_next_window_normal_fallback(self):
        from tests import test_document_extraction_admission as extraction_fixture
        result = self.generate()
        self.assertEqual(result['status'], 'failed')
        budget = self.f.f.b.connection
        admission = dict(budget.execute('SELECT * FROM thesis_impact_day_admissions').fetchone())
        self.assertGreater(admission['reserved_micros'], 0)
        self.assertEqual(budget.execute('SELECT count(*) FROM thesis_impact_day_settlements').fetchone()[0], 0)
        scheduler = self.f.f.h.h.scheduler
        formal = scheduler.formal_result(result['work_order_ref'])
        events = self.f.f.router.profile_health_events(profile_id=self.f.f.pr['id'])
        self.assertEqual(len(events), 1)
        self.generate()
        self.assertEqual(self.f.served, [self.f.f.pr['id']])
        self.assertEqual(scheduler.formal_result(result['work_order_ref']), formal)
        self.assertEqual(self.f.f.router.profile_health_events(profile_id=self.f.f.pr['id']), events)
        next_result = self.generate(extraction_fixture.WINDOW_CHARS)
        self.assertEqual(next_result['status'], 'succeeded')
        self.assertEqual(self.f.served, [self.f.f.pr['id'], self.f.backup['id']])
        self.assertEqual(dict(budget.execute('SELECT * FROM thesis_impact_day_admissions WHERE admission_id=?', (admission['admission_id'],)).fetchone()), admission)
        self.assertIsNone(budget.execute('SELECT 1 FROM thesis_impact_day_settlements WHERE admission_id=?', (admission['admission_id'],)).fetchone())
        self.assertEqual(len(self.f.f.router.active_cooldowns()), 1)
