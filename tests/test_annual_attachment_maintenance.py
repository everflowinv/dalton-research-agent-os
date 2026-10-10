"""Held originals wake an empty extraction queue without retrieval or model calls."""
import concurrent.futures
import json
import stat
import unittest
from unittest.mock import patch

from dalton_core.document_extraction_launcher import (
    DocumentExtractionCoordinator, _write_attachment_cursor,
    register_held_annual_attachments,
)
from dalton_core.store import canonical_json
from tests import test_annual_report_extraction_source as fixture
from tests.test_document_extraction_launcher import FakeLauncher
from tests.test_mission_source_discovery import Clock


class Launcher(FakeLauncher):
    def start(self, *, support_only=False, **kwargs):
        ticket = super().start(**kwargs)
        self.starts[-1]['support_only'] = support_only
        return ticket


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        original = fixture.mission_params
        def granted(*args, **kwargs):
            params = original(*args, **kwargs)
            params['autonomy']['may_write'].append('source_discovery')
            return params
        with patch.object(fixture, 'mission_params', side_effect=granted):
            fixture.AnnualExhibitSourceTests.setUp(self)
    save = fixture.AnnualExhibitSourceTests.save

    def terminal_parent(self):
        with self.missions._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_document_reviews SET state='extraction_staged' WHERE review_id=?",
                (self.parent_review['review_id'],))

    def coordinator(self):
        launcher = Launcher(self.root)
        launcher.spool_dir = self.h.spool._root
        clock = Clock()
        coordinator = DocumentExtractionCoordinator(missions=self.missions, launcher=launcher, clock=clock)
        ticket = 'document-extraction:' + 'f' * 24
        launcher.tickets[ticket] = {'id': ticket, 'status': 'succeeded',
            'summary': {'stop_reason': 'nothing_to_draft'}, 'completed_at': clock().isoformat()}
        coordinator._latest_path.write_text(canonical_json({
            'ticket': ticket, 'settled': True, 'support_only': True,
            'awaiting_at_launch': 0, 'awaiting_fingerprint_at_launch': coordinator._awaiting_state()[1],
            'model_config_fingerprint': launcher.configuration_fingerprint(),
            'completed_at': clock().isoformat(), 'stop_reason': 'nothing_to_draft',
            'support_progress': False}))
        return coordinator, launcher

    def maintain(self):
        return register_held_annual_attachments(self.missions, state_dir=self.root, spool=self.h.spool)

    def add_parent(self, record_id, ticket='public-web-fetch:' + 'b' * 24):
        with self.missions._transaction() as cur:
            cur.execute(
                'INSERT INTO coverage_mission_discovered_documents '
                '(record_id,mission_version_ref,company_ref,source_ref,document_ref,discovery_ref,status,ticket_ref,created_at,updated_at,host) '
                'SELECT ?,mission_version_ref,company_ref,source_ref,?,discovery_ref,status,?,created_at,updated_at,host '
                "FROM coverage_mission_discovered_documents WHERE record_id='fixture:parent'",
                (record_id, f'sec:filing:0001467373-26-{1000 + int(record_id.split(":")[-1]):06}', ticket))

    def test_empty_queue_new_held_outcome_wakes_normal_child_once(self):
        self.terminal_parent()
        parent_before = self.missions.document_review(self.parent_review['review_id'])
        raw_before = self.h.spool.read_object(self.base['body_sha256'])
        outcome = self.directory / 'annual-report-exhibit.json'
        outcome.unlink()
        coordinator, launcher = self.coordinator()
        first = coordinator.dispatch_once()
        self.assertEqual(first['awaiting'], 0)
        self.assertEqual(launcher.starts, [])
        self.save()
        arrived = coordinator.dispatch_once()
        self.assertEqual((arrived['status'], arrived['awaiting']), ('launched', 1))
        self.assertEqual(len(arrived['annual_report_attachments']), 1)
        self.assertFalse(launcher.starts[0]['support_only'])
        rows = self.missions.connection.execute('SELECT count(*) FROM coverage_mission_document_reviews').fetchone()[0]
        self.assertEqual(coordinator.dispatch_once()['status'], 'busy')
        self.assertEqual(len(launcher.starts), 1)
        self.assertEqual(rows, self.missions.connection.execute('SELECT count(*) FROM coverage_mission_document_reviews').fetchone()[0])
        self.assertEqual(parent_before, self.missions.document_review(self.parent_review['review_id']))
        self.assertEqual(raw_before, self.h.spool.read_object(self.base['body_sha256']))

    def test_tampered_cached_original_is_refused_without_queue_or_child(self):
        self.terminal_parent()
        coordinator, launcher = self.coordinator()
        (self.directory / 'annual-report-exhibit-manifest.json').unlink()
        tick = coordinator.dispatch_once()
        self.assertEqual(tick['awaiting'], 0)
        self.assertEqual(len(tick['annual_report_attachment_refusals']), 1)
        self.assertEqual(launcher.starts, [])
        self.assertEqual(self.missions.connection.execute('SELECT count(*) FROM coverage_mission_document_reviews').fetchone()[0], 1)

    def test_more_than_100_parents_progress_and_wrap_without_starvation(self):
        self.terminal_parent()
        for index in range(105):
            self.add_parent(f'fixture:{index:03}')
        first = self.maintain()
        self.assertEqual((first['scanned'], first['registered']), (100, []))
        second = self.maintain()
        self.assertEqual(second['scanned'], 6)
        self.assertEqual(len(second['registered']), 1)
        self.assertEqual(self.maintain()['scanned'], 100)

    def test_two_refusals_bound_attempts_but_next_tick_reaches_valid_parent(self):
        self.terminal_parent()
        # Same verified outcome, but two discovery rows lack their own cached
        # receipt directory. The real authority rejects both, then re-proves
        # the genuine parent on the next tick.
        for index in range(2):
            self.add_parent(f'fixture:{index:03}')
        from dalton_core.sec_annual_report_exhibit import exhibit_outcome
        outcome = exhibit_outcome(self.root, fixture.TICKET)
        with patch('dalton_core.sec_annual_report_exhibit.exhibit_outcome', return_value=outcome):
            first = self.maintain()
        self.assertEqual((first['scanned'], len(first['refused']), first['registered']), (2, 2, []))
        second = self.maintain()
        self.assertEqual(len(second['registered']), 1)
        self.assertEqual(second['refused'], [])

    def test_shared_cursor_writes_are_atomic_unique_and_owner_only(self):
        directory = self.root / 'extractions'
        directory.mkdir()
        path = directory / 'annual-attachment-maintenance.json'
        _write_attachment_cursor(path, 'initial')
        def write(index):
            for attempt in range(10):
                _write_attachment_cursor(path, f'{index}:{attempt}')
                self.assertIsInstance(json.loads(path.read_text())['after_record_id'], str)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(16)))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(directory.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
