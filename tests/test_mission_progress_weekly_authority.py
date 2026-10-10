"""Progress admissions bind real governance, exact UTC slots and local delivery."""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from dalton_core.agenda import AgendaStore
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.industry_research import IndustryResearchAuthority
from dalton_core.mission_progress_weekly import ISSUE_PREFIX
from dalton_core.store import DaltonStore, content_hash
from dalton_core.weekly_brief import WeeklyBriefAuthority, WeeklyBriefError
from dalton_core.weekly_brief_coordinator import (
    WeeklyBriefCoordinatorPrecondition, WeeklyBriefSchedulePlan,
    _utc, run_weekly_brief_cycle,
)
from tests import test_mission_progress_weekly as progress_fixture
from tests.test_industry_research import invocation


class MissionProgressAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.h = progress_fixture.MissionProgressLocalCycleTests(
            'test_normal_cycle_delivers_exact_progress_body_and_replays_once')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        for name in ('socket.socket.connect', 'urllib.request.urlopen',
                     'dalton_core.cockpit_model.CockpitModel'):
            guard = patch(name, side_effect=AssertionError('external/model call attempted'))
            guard.start()
            self.addCleanup(guard.stop)

    def admission_args(self, plan=None):
        schedule = WeeklyBriefSchedulePlan.from_mapping(plan or self.h.plan)
        slot = _utc(self.h.now)
        digest = content_hash({
            'plan_ref': schedule.plan_ref, 'plan_hash': schedule.content_hash,
            'scheduled_for': slot})[:32]
        return {
            'schedule': schedule, 'active': self.h.store.active_policy(),
            'cycle_id': 'weekly-brief-cycle:' + digest,
            'issue_id': ISSUE_PREFIX + digest, 'scheduled_for': slot,
            'period_start': _utc(self.h.now - timedelta(days=7)),
            'as_of': _utc(self.h.now), 'actor_ref': 'core',
            'idempotency_key': 'review:' + digest,
        }

    def issue_count(self):
        return self.h.store.connection.execute(
            'SELECT COUNT(*) FROM weekly_brief_progress_issues').fetchone()[0]

    def authorize(self, plan):
        old = self.h.store.active_policy()
        policy = copy.deepcopy(old['policy'])
        schedule = WeeklyBriefSchedulePlan.from_mapping(plan)
        policy['weekly_brief_auto_publish']['allowed_plan_bindings'] = [{
            'plan_ref': schedule.plan_ref, 'plan_hash': schedule.content_hash}]
        number = old['version_number'] + 1
        self.h.store.create_policy(
            policy, policy_version_id='policy:review:' + str(number),
            version_number=number, prior_version_ref=old['policy_version_id'],
            actor_ref='human:review-owner', effective_from=old['effective_from'],
            change_reason='Authorize isolated review plan')

    def test_forged_policy_body_cannot_authorize_an_unapproved_plan(self):
        plan = copy.deepcopy(self.h.plan)
        plan['plan_ref'] = 'weekly-brief-plan:unapproved:review'
        args = self.admission_args(plan)
        args['active'] = copy.deepcopy(args['active'])
        # The ID and hash are genuine; only the supplied policy body is forged.
        args['active']['policy']['weekly_brief_auto_publish']['allowed_plan_bindings'] = [{
            'plan_ref': args['schedule'].plan_ref,
            'plan_hash': args['schedule'].content_hash}]
        with self.assertRaisesRegex(WeeklyBriefCoordinatorPrecondition, 'exact weekly brief plan hash'):
            self.h.weekly.progress.admit(**args)
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.h.agenda.pending_outbox(), [])

    def test_real_approved_progress_cannot_be_external(self):
        plan = copy.deepcopy(self.h.plan)
        plan['plan_ref'] = 'weekly-brief-plan:external:review'
        plan['destination_ref'] = 'openclaw:discord:review:channel:review'
        self.authorize(plan)
        with self.assertRaisesRegex(WeeklyBriefError, 'locally'):
            self.h.weekly.progress.admit(**self.admission_args(plan))
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.h.agenda.pending_outbox(), [])

    def test_same_slot_utc_alias_cannot_create_a_second_issue(self):
        self.h.cycle()
        args = self.admission_args()
        args['scheduled_for'] = self.h.now.isoformat().replace('+00:00', 'Z')
        digest = content_hash({
            'plan_ref': args['schedule'].plan_ref,
            'plan_hash': args['schedule'].content_hash,
            'scheduled_for': args['scheduled_for']})[:32]
        args.update(cycle_id='weekly-brief-cycle:' + digest,
                    issue_id=ISSUE_PREFIX + digest, idempotency_key='alias:' + digest)
        with self.assertRaises(WeeklyBriefError):
            self.h.weekly.progress.admit(**args)
        self.assertEqual(self.issue_count(), 1)
        self.assertEqual(self.h.store.connection.execute(
            'SELECT COUNT(*) FROM weekly_brief_progress_cycle_admissions').fetchone()[0], 1)

    def test_window_and_exact_cycle_identity_are_checked(self):
        for changed in (
            {'period_start': _utc(self.h.now - timedelta(days=8))},
            {'scheduled_for': _utc(self.h.now + timedelta(days=7))},
            {'cycle_id': 'weekly-brief-cycle:not-the-plan'},
        ):
            with self.subTest(changed=changed), self.assertRaises(WeeklyBriefError):
                self.h.weekly.progress.admit(**{**self.admission_args(), **changed})
        self.assertEqual(self.issue_count(), 0)

    def test_concurrent_normal_cycles_leave_one_issue_and_one_outbox(self):
        barrier = threading.Barrier(2)

        def run(_):
            # Separate real connections and authorities; only SQLite serializes
            # admissions. There is no shared mocked coordinator result.
            store = DaltonStore(str(self.h.state / 'core.sqlite'))
            try:
                weekly = WeeklyBriefAuthority(store, IndustryResearchAuthority(store))
                agenda = AgendaStore(store)
                barrier.wait(timeout=10)
                return run_weekly_brief_cycle(
                    store, weekly, agenda, plan=self.h.plan,
                    as_of=_utc(self.h.now), actor_ref='core')
            finally:
                store.close()

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(len({r['issue_version_ref'] for r in results}), 1)
        self.assertEqual(self.issue_count(), 1)
        self.assertEqual(len(self.h.agenda.pending_outbox()), 1)

    def test_uninitialized_sources_are_distinct_from_zero_and_no_stance_is_created(self):
        result = self.h.cycle()
        issue = self.h.weekly.issue(result['issue_version_ref'])
        snapshot = issue['snapshot']
        mission = CoverageMissionAuthority(self.h.store).active_mission(self.h.mission['mission_ref'])
        self.assertEqual(snapshot['mission_version_ref'], mission['id'])
        self.assertEqual(snapshot['mission_version_hash'], mission['content_hash'])
        self.assertEqual(snapshot['universe'], mission['universe'])
        self.assertFalse(snapshot['material_requests']['available'])
        self.assertEqual(snapshot['material_requests']['entries'], [])
        self.assertNotIn('stance', snapshot)
        self.assertNotIn('return', snapshot)
        self.assertEqual(issue['markdown_hash'], hashlib.sha256(issue['body'].encode()).hexdigest())
        self.assertEqual(self.h.weekly.render_markdown(issue['id'])['body'], issue['body'])

    def test_research_insert_cannot_reuse_progress_slot_in_another_utc_spelling(self):
        self.h.cycle()
        connection = self.h.store.connection
        columns = [row[1] for row in connection.execute('PRAGMA table_info(weekly_brief_cycle_admissions)')]
        values = {name: 'synthetic-unused' for name in columns}
        values.update(brief_ref=self.h.plan['brief_ref'],
                      scheduled_for=self.h.now.isoformat().replace('+00:00', 'Z'))
        # Exercise the real SQL trigger independently of the coordinator's
        # early replay lookup; it must reject before any unrelated FK check.
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'slot already admitted'):
            with self.h.weekly._transaction() as cursor:
                cursor.execute(
                    'INSERT INTO weekly_brief_cycle_admissions (' + ','.join(columns)
                    + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                    [values[name] for name in columns])
        self.assertEqual(self.issue_count(), 1)
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM weekly_brief_cycle_admissions').fetchone()[0], 0)

    def test_selected_source_time_column_must_match_immutable_source_record(self):
        self.h.store.register_invocation(invocation('invocation:time-proof'))
        company = self.h.mission['universe'][0]['company_ref']
        claim = self.h.store.register_claim({
            'claim_ref': 'claim:time-proof', 'subject_ref': company,
            'metric_or_aspect': 'aspect:source', 'period': 'synthetic',
            'basis': 'source-commentary', 'normalized_statement': 'Synthetic source observation.',
            'claim_kind': 'qualitative', 'value': None, 'unit': None,
            'producer_invocation_refs': ['invocation:time-proof'],
            'actor_ref': 'automation:researcher'})
        connection = self.h.store.connection
        original = connection.execute(
            'SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?',
            (claim['claim_version_id'],)).fetchone()
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='claim_versions'"
        ).fetchall():
            connection.execute('DROP TRIGGER "' + row[0] + '"')
        # Disposable corruption: preserve exact canonical source bytes/hash,
        # but move its SQL timestamp within the reporting window.
        connection.execute('UPDATE claim_versions SET created_at=? WHERE claim_version_id=?',
                           (_utc(self.h.now - timedelta(days=1)), claim['claim_version_id']))
        self.assertEqual(tuple(original), tuple(connection.execute(
            'SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?',
            (claim['claim_version_id'],)).fetchone()))
        with self.assertRaisesRegex(WeeklyBriefError, 'source identity/hash'):
            self.h.cycle()
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.h.agenda.pending_outbox(), [])


if __name__ == '__main__':
    unittest.main()
