"""Offline preflight/read-only regression. All data and actors are synthetic."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.document_extraction import DocumentExtractionService, build_work
from dalton_core.model_router import ModelRouter
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter
from dalton_core.research_verification import CandidateStagingStore, ResearchVerificationError
from dalton_core.writer_server import Principal, HUMAN_GOVERNANCE_OPERATIONS
from dalton_core.writer_protocol import ProtocolError
from tests.test_document_extraction import ExtractionHarness
from tests import test_document_extraction_admission as admission_fixtures
from tests.test_transcript_polish_model_worker import policy


def disk_state(root):
    # SQLite read-only WAL readers update SHM read-mark/locking slots.
    # Assert authority DB/WAL bytes and all file names/modes/mtime/size; SHM
    # coordination bytes are not persisted authority or a schema migration.
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_size,
            p.stat().st_mtime_ns, ("sqlite_volatile_read_marks" if p.name.endswith("-shm") else
            hashlib.sha256(p.read_bytes()).hexdigest()))
            for p in root.rglob('*') if p.is_file()}


class ReadOnlyModelAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_files_and_parents_are_never_created(self):
        for cls in (ModelRouter, ThesisImpactBudgetStore):
            p = self.root / cls.__name__ / 'missing.sqlite'
            with self.assertRaises((OSError, sqlite3.Error)):
                cls(p, read_only=True)
            self.assertFalse(p.parent.exists())

    def test_memory_and_supplied_connections_cannot_masquerade_as_readonly_files(self):
        for cls in (ModelRouter, ThesisImpactBudgetStore):
            with self.assertRaises(ValueError):
                cls(read_only=True)
        with sqlite3.connect(':memory:') as c:
            with self.assertRaises(ValueError):
                ModelRouter(connection=c, read_only=True)

    def test_special_path_encoding_readonly_permissions_and_database_write_denial(self):
        for cls in (ModelRouter, ThesisImpactBudgetStore):
            with self.subTest(cls=cls):
                path = self.root / (cls.__name__ + ' 空格 # ? % &.sqlite')
                with cls(path) as writable:
                    if cls is ModelRouter:
                        writable.register_policy(policy())
                    else:
                        writable.register_policy(policy_version_id='budget:test:1', day_cap_micros=100000)
                    writable.connection.execute('PRAGMA journal_mode=DELETE')
                path.chmod(0o444)
                before = disk_state(self.root)
                with patch('os.chmod', side_effect=AssertionError('readonly chmod')), \
                     patch.object(Path, 'touch', side_effect=AssertionError('readonly touch')), \
                     patch.object(Path, 'mkdir', side_effect=AssertionError('readonly mkdir')), \
                     cls(path, read_only=True) as reader:
                    self.assertEqual(reader.connection.execute('PRAGMA query_only').fetchone()[0], 1)
                    # Compare resolved paths: on macOS /var is a symlink to
                    # /private/var, and SQLite reports the resolved file while
                    # tempfile hands back the unresolved one. The claim being
                    # made is "the reader opened exactly this file", which
                    # survives resolution; string equality did not.
                    self.assertEqual(
                        Path(
                            reader.connection.execute('PRAGMA database_list').fetchone()[2]
                        ).resolve(),
                        path.resolve(),
                    )
                    self.assertEqual(reader.connection.total_changes, 0)
                    if cls is ModelRouter:
                        self.assertEqual(reader.get_policy(policy()['policy_version_ref'])['id'], policy()['id'])
                        with self.assertRaises(sqlite3.OperationalError): reader.register_policy(policy())
                    else:
                        self.assertEqual(reader.policy('budget:test:1')['day_cap_micros'], 100000)
                        with self.assertRaises(sqlite3.OperationalError):
                            reader.register_policy(policy_version_id='budget:test:2', day_cap_micros=2)
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.connection.execute('CREATE TABLE forbidden(x)')
                    # mode=ro is an independent guard even if query_only is cleared.
                    reader.connection.execute('PRAGMA query_only=OFF')
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.connection.execute('CREATE TABLE still_forbidden(x)')
                self.assertEqual(disk_state(self.root), before)

    def test_old_schema_snapshot_is_not_migrated(self):
        for cls in (ModelRouter, ThesisImpactBudgetStore):
            path = self.root / (cls.__name__ + '.sqlite')
            with sqlite3.connect(path) as c:
                c.execute('CREATE TABLE legacy_only(value)')
                c.execute('INSERT INTO legacy_only VALUES(7)')
            before = disk_state(self.root)
            with cls(path, read_only=True) as reader, reader.memory_snapshot() as snapshot:
                tables = [r[0] for r in snapshot.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                self.assertEqual(tables, ['legacy_only'])
                self.assertEqual(snapshot.connection.execute('SELECT value FROM legacy_only').fetchone()[0], 7)
                with self.assertRaises(sqlite3.Error):
                    if cls is ModelRouter: snapshot.get_policy('policy:missing')
                    else: snapshot.policy('budget:missing')
            self.assertEqual(disk_state(self.root), before)

    def test_wal_without_sidecars_fails_without_creating_them(self):
        path = self.root / 'closed-wal.sqlite'
        with ThesisImpactBudgetStore(path) as b:
            b.register_policy(policy_version_id='budget:test:1', day_cap_micros=100000)
        self.assertFalse(Path(str(path) + '-wal').exists())
        before = disk_state(self.root)
        with self.assertRaisesRegex(sqlite3.OperationalError, 'existing WAL/SHM'):
            ThesisImpactBudgetStore(path, read_only=True)
        self.assertEqual(disk_state(self.root), before)

    def test_live_wal_commits_are_present_in_readonly_snapshot(self):
        path = self.root / 'wal.sqlite'
        with ThesisImpactBudgetStore(path) as b:
            b.register_policy(policy_version_id='budget:test:1', day_cap_micros=100000)
            before = disk_state(self.root)
            with ThesisImpactBudgetStore(path, read_only=True) as reader, reader.memory_snapshot() as snapshot:
                self.assertEqual(snapshot.policy('budget:test:1'), b.policy('budget:test:1'))
                snapshot.register_policy(policy_version_id='budget:test:2', day_cap_micros=200000)
                self.assertEqual(b.connection.execute('SELECT count(*) FROM thesis_impact_budget_policies').fetchone()[0], 1)
            self.assertEqual(disk_state(self.root), before)


class ExtractionPreflightTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixture preparation, not its test methods or a real model.
        self.fixture = admission_fixtures.BrokerAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h, self.router, self.b = self.fixture.h, self.fixture.router, self.fixture.b
        self.staging = CandidateStagingStore(self.h.root / 'staging.sqlite')
        self.addCleanup(self.staging.close)
        self.h.writer._candidate_staging = self.staging

    def check(self, **changes):
        return self.h.service.preflight(**{**self.h.params, **changes})

    def assert_blocked(self, result, text):
        self.assertFalse(result['local_checks_passed'], result)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn(text, json.dumps(result['blockers']))
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['reservation_created'])

    def seed_spend(self, *, calls=1, amount=50000, prior_day=False, settled=False, other_mission=False):
        day = '2020-01-01' if prior_day else datetime.now(timezone.utc).date().isoformat()
        for n in range(calls):
            scope = None
            if other_mission:
                scope = {'mission_ref':'mission:other','mission_version_ref':'mission:other:1',
                         'mission_version_hash':'a'*64,'max_daily_paid_calls':100,'max_daily_cost_micros':10000000}
            a = self.b.admit(policy_version_id='budget:owner:1', day=day, work_order_ref=f'work:seed-{n}',
                attempt_number=1, phase='verification', route_decision_ref=f'route:seed-{n}',
                reserved_micros=amount, mission_binding=scope)
            if settled: self.b.settle(a['admission_id'], actual_micros=amount)

    def test_pass_is_non_reserving_and_no_core_scheduler_staging_or_disk_writes(self):
        h = self.h
        connections = [h.h.core.connection, h.h.scheduler.connection, self.staging.connection]
        logical = [('\n'.join(c.iterdump()), c.total_changes) for c in connections]
        for c in connections: c.execute('PRAGMA query_only=ON')
        before = disk_state(h.root)
        with patch.object(OpenClawModelAdapter, '__init__', side_effect=AssertionError('broker constructed')), \
             patch('socket.socket.connect', side_effect=AssertionError('socket connected')), \
             patch('os.chmod', side_effect=AssertionError('readonly chmod')):
            result = self.check()
            repeat = self.check()
            page = h.service.view(**h.params)
        self.assertTrue(result['local_checks_passed'], result)
        self.assertTrue(repeat['local_checks_passed'], repeat)
        self.assertTrue(result['preview_only'])
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['reservation_created'])
        self.assertEqual(result['context_binding']['content_hash'], page['context']['content_hash'])
        self.assertEqual([('\n'.join(c.iterdump()), c.total_changes) for c in connections], logical)
        self.assertEqual(disk_state(h.root), before)
        wire = json.dumps(result)
        self.assertNotIn('route-decision:', wire)
        self.assertNotIn('thesis-impact-admission:', wire)
        self.assertNotIn('work:document-extraction', wire)
        self.assertIn('source_to_model_permission', result['unverified'])
        self.assertFalse(Path(h.writer._document_extraction_model_config['broker_auth_key']).exists())

    def test_canonical_route_and_admit_used_only_on_memory_copies(self):
        original_route, original_admit = ModelRouter.route, ThesisImpactBudgetStore.admit
        calls = []
        def route(router, work, **kw):
            self.assertEqual(router.path, ':memory:')
            self.assertEqual(router.connection.execute('PRAGMA database_list').fetchone()[2], '')
            self.assertEqual(kw['required_context_tokens'], len(work.question.encode('utf-8')) + 3000)
            self.assertEqual(kw['credential_slot_refs'], self.h.writer._document_extraction_model_config['credential_slot_refs'])
            self.assertEqual(kw['idempotency_key'], f'document-extraction-route:{work.id}:1')
            calls.append('route')
            return original_route(router, work, **kw)
        def admit(budget, **kw):
            self.assertEqual(budget.path, ':memory:')
            self.assertIn('outer_budget', kw['mission_binding'])
            calls.append('admit')
            return original_admit(budget, **kw)
        with patch.object(ModelRouter, 'route', route), patch.object(ThesisImpactBudgetStore, 'admit', admit):
            self.assertTrue(self.check()['local_checks_passed'])
        self.assertEqual(calls, ['route', 'admit'])

    def test_readback_paid_suggestions_uses_only_readonly_constructors(self):
        with self.fixture.patch_execute(): result = self.h.generate()
        before = disk_state(self.h.root)
        with patch('os.chmod', side_effect=AssertionError('readback chmod')):
            page = self.h.service.view(**self.h.params)
        self.assertEqual(page, result)
        self.assertEqual(disk_state(self.h.root), before)
        self.assert_blocked(self.check(), 'existing extraction work')

    def test_writer_authenticated_actor_closed_params_and_no_automation_exception(self):
        writer = self.h.writer
        def request(token, params):
            return SimpleNamespace(auth_token=token, operation='document_extraction_preflight', params=params)
        p = {k:v for k,v in self.h.params.items() if k != 'actor_ref'}
        # Harness's principals are normally installed by start; set the map
        # without starting a writer (which would provision Core schemas).
        writer._by_token = {'fixture-token':writer.principals['human'],
            'auto':Principal('auto','auto',HUMAN_GOVERNANCE_OPERATIONS,actor_ref='automation:coverage-mission')}
        with self.assertRaises(PermissionError): writer._handle(request('bad', p))
        with self.assertRaises(PermissionError): writer._handle(request('auto', p))
        with self.assertRaises(PermissionError): writer._handle(request('fixture-token', {**p,'actor_ref':'human:forged'}))
        with self.assertRaises(ProtocolError): writer._handle(request('fixture-token', {**p,'enable':True}))
        self.assertTrue(writer._handle(request('fixture-token', p))['local_checks_passed'])

    def test_source_hash_review_offset_and_expected_context_fail_closed(self):
        expected = self.h.context()['content_hash']
        self.assert_blocked(self.check(expected_review_hash='0'*64), 'review_source_mission')
        self.assert_blocked(self.check(offset=1), 'review_source_mission')
        self.assert_blocked(self.check(expected_context_hash='0'*64), 'source_context_changed_reload')
        self.assertTrue(self.check(expected_context_hash=expected)['local_checks_passed'])
        self.h.files['summary.json']['assembled_content_sha256'] = '0'*64
        self.h.save_files()
        self.assert_blocked(self.check(expected_context_hash=expected), 'review_source_mission')

    def test_governance_rollover_fails_closed(self):
        self.h.h.core.create_policy({**self.h.h.core.active_policy_version().policy},policy_version_id='policy:fixture:3',
            actor_ref=self.h.params['actor_ref'],change_reason='Synthetic rollover')
        self.assert_blocked(self.check(), 'current governance policy')

    def test_missing_outer_caps_never_uses_mission_budget_as_permission(self):
        for name in ('mandate', 'governance'):
            root = self.h.root / name; root.mkdir()
            h = ExtractionHarness(root, paid_budget_authorized=True, paid_budget_overrides={name:None})
            try:
                h.writer._document_extraction_model_config = self.h.writer._document_extraction_model_config
                self.assert_blocked(h.service.preflight(**h.params), name + ' lacks explicit')
            finally: h.close()

    def test_missing_configuration_still_reports_source_and_outer_checks(self):
        self.h.writer._document_extraction_model_config = None
        r = self.check()
        self.assert_blocked(r, 'not_installed')
        self.assertIn({'check':'review_source_mission','status':'passed'}, r['checks'])
        self.assertIn({'check':'outer_authority','status':'passed'}, r['checks'])

    def test_missing_both_databases_lists_both_blockers_without_creating_files(self):
        config = self.h.writer._document_extraction_model_config
        config['model_router_db'] = str(self.h.root / 'missing-router.sqlite')
        config['budget_db'] = str(self.h.root / 'missing-budget.sqlite')
        before = disk_state(self.h.root)
        r = self.check()
        self.assert_blocked(r, 'router_snapshot')
        self.assert_blocked(r, 'budget_snapshot')
        self.assertEqual(disk_state(self.h.root), before)

    def test_old_budget_schema_is_not_silently_bootstrapped_on_preview(self):
        path = self.h.root / 'old.sqlite'
        with sqlite3.connect(path) as c: c.execute('CREATE TABLE legacy_only(x)')
        self.h.writer._document_extraction_model_config['budget_db'] = str(path)
        before = disk_state(self.h.root)
        self.assert_blocked(self.check(), 'shared_budget_policy')
        self.assertEqual(disk_state(self.h.root), before)

    def test_superseded_routing_policy_is_blocked_in_preview_and_execution_context(self):
        p = policy(); p.update(version=2, prior_version_ref=p['policy_version_ref'], policy_version_ref='model-routing-policy-version:test-transcript:2')
        self.router.register_policy(p)
        self.assert_blocked(self.check(), 'routing policy was superseded')
        with self.assertRaisesRegex(ResearchVerificationError, 'superseded'): self.h.context()

    def test_superseded_budget_policy_is_blocked(self):
        self.b.register_policy(policy_version_id='budget:owner:2', day_cap_micros=1000000, prior_version_id='budget:owner:1')
        self.assert_blocked(self.check(), 'superseded')

    def test_missing_credential_slot_is_a_routing_blocker_not_a_key_read(self):
        self.h.writer._document_extraction_model_config['credential_slot_refs'] = ['credential-slot:wrong']
        r = self.check()
        self.assert_blocked(r, 'model_route_rejected')
        self.assertIn('credential_slot_unavailable', r['route_preview']['rejection_reasons'])

    def test_expired_profile_is_a_canonical_route_blocker(self):
        p = copy.deepcopy(self.fixture.pr); p.update(version=2, prior_version_ref=p['profile_version_ref'], profile_version_ref='model-profile-version:test-transcript:2')
        p['availability']['valid_until'] = '2026-08-24T22:00:00+00:00'
        self.router.register_profile(p)
        r = self.check(); self.assert_blocked(r, 'model_route_rejected')
        self.assertIn('availability_expired', r['route_preview']['rejection_reasons'])

    def test_owner_cap_and_cross_day_unsettled_reservations_remain_charged(self):
        self.seed_spend(amount=1000000, prior_day=True)
        before = disk_state(self.h.root)
        self.assert_blocked(self.check(), 'owner_budget_exceeded')
        self.assertEqual(disk_state(self.h.root), before)

    def test_cross_mission_cross_day_outer_call_cap_cannot_be_reset(self):
        self.seed_spend(calls=40, amount=1000, prior_day=True, other_mission=True)
        self.assert_blocked(self.check(), 'outer_research_budget_exceeded')

    def test_shared_unbound_paid_call_count_remains_charged_after_settlement(self):
        # Fixture mission budget is 20 calls; old unbound lanes are not free.
        limit = self.h.mission['budget']['max_daily_paid_calls']
        self.seed_spend(calls=limit, amount=1000, settled=True)
        self.assert_blocked(self.check(), 'mission_budget_exceeded')

    def test_overrun_alert_blocks_without_creating_an_admission_or_rejection(self):
        self.b.record_alert(alert_id='alert:synthetic-overrun',kind='work_order_failed',severity='high',
            work_order_ref='work:other',phase='assessment',detail={'reason':'model_reservation_overrun'})
        before = disk_state(self.h.root)
        self.assert_blocked(self.check(), 'overrun requires owner reconciliation')
        self.assertEqual(disk_state(self.h.root), before)

    def test_preview_expiration_never_reserves_or_enables_real_execution(self):
        r = self.check(); self.assertTrue(r['local_checks_passed'])
        self.seed_spend(amount=1000000)
        self.assert_blocked(self.check(), 'owner_budget_exceeded')
        with self.fixture.patch_execute():
            self.assertEqual(self.h.generate()['status'], 'failed')
        self.assertEqual(self.fixture.calls, 0)

    def test_mission_rollover_rejects_the_old_review_binding(self):
        from tests.p9a_fixtures import mission_params
        p = mission_params(self.h.state)
        p.update(version_id='coverage-mission-version:fixture:2', prior_version_ref=self.h.mission['id'],
                 idempotency_key='fixture:mission:2')
        self.h.missions.create_mission(p.pop('mission_ref'), **p)
        self.assert_blocked(self.check(), 'review_source_mission')

    def test_invalid_identity_and_relative_paths_fail_before_any_snapshot(self):
        config = self.h.writer._document_extraction_model_config
        original = dict(config)
        for key, value in (('expected_agent_id', 'not an agent'), ('broker_client_id', 'invalid'),
                           ('broker_socket', 'relative.sock'), ('budget_db', ':memory:')):
            config.clear(); config.update(original); config[key] = value
            with self.subTest(key=key), patch.object(ModelRouter, 'memory_snapshot', side_effect=AssertionError('snapshot')):
                self.assert_blocked(self.check(), 'configuration')

    def test_source_raw_object_change_is_rejected_not_normalized_away(self):
        context = self.h.context()
        from dalton_core.raw_spool import RawSpoolError
        with patch.object(self.h.writer._transcript_spool, 'read_object', side_effect=RawSpoolError('fixture raw hash mismatch')):
            self.assert_blocked(self.check(expected_context_hash=context['content_hash']), 'review_source_mission')

    def test_route_failure_also_reports_independent_shared_budget_blocker(self):
        self.seed_spend(amount=1000000)
        self.h.writer._document_extraction_model_config['credential_slot_refs'] = ['credential-slot:unavailable']
        r = self.check()
        self.assert_blocked(r, 'model_route_rejected')
        self.assert_blocked(r, 'owner_budget_exceeded')

    def test_input_byte_bound_is_the_canonical_work_order_gate(self):
        from dataclasses import replace
        import dalton_core.document_extraction_preflight as module
        work_builder = module.build_work
        with patch.object(module, 'build_work', side_effect=lambda c: replace(work_builder(c), question='中' * 6000)):
            r = self.check()
        self.assert_blocked(r, 'model_route_rejected')
        self.assertIn('work_order_budget_input_exceeded', r['route_preview']['rejection_reasons'])


if __name__ == '__main__': unittest.main()
