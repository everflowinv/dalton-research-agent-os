"""Unpaid discovery can stop; approved membership does not format displays."""
import json
import unittest
from unittest.mock import patch

from dalton_core import research_localization_store as store
from dalton_core import ui_text_discovery as ui
from dalton_core.research_preparation_checkpoint import PreparationQuiesced
from tests import test_research_localization_store as localization_fixture
from tests import test_ui_text_discovery as discovery_fixture
from tests import test_publication_cooperative_quiesce as worker_fixture


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.fixture = localization_fixture.LocalizationStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.f = self.fixture
        store.publish_ui_texts(self.f.directory, [
            {'source': self.f.product, 'localization': self.f.candidate}])

    def test_exact_approved_membership_avoids_every_display_formatter(self):
        displayed = store.load_ui_texts(self.f.db)
        before = {p: p.read_bytes() for p in self.f.directory.rglob('*.json')}
        with patch.object(store, '_shown_and_key', side_effect=AssertionError('formatted')):
            raw = store.load_ui_source_mapping(self.f.db)
        self.assertEqual(set(raw), set(displayed))
        self.assertEqual(raw, {'Revenue increased.': '收入增长。'})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_cancel_discards_partial_membership_and_next_read_is_complete(self):
        # Cancellation after one validated pair never returns/caches a partial
        # membership map; the next normal operation must still see all pairs.
        count = 0
        def stopped():
            nonlocal count
            count += 1
            return count == 6
        with self.assertRaises(PreparationQuiesced):
            store.load_ui_source_mapping(self.f.db, stop_requested=stopped)
        self.assertEqual(store.load_ui_source_mapping(self.f.db),
                         {'Revenue increased.': '收入增长。'})

    def test_corrupt_record_and_symlink_use_existing_refusal(self):
        path = next((self.f.directory / store.UI_RECORDS_DIRECTORY).glob('*.json'))
        path.write_text('{}')
        self.assertEqual(store.load_ui_source_mapping(self.f.db), {})
        path.unlink()
        path.symlink_to(self.f.directory / 'ui-texts.json')
        self.assertEqual(store.load_ui_source_mapping(self.f.db), {})


class DiscoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.f = discovery_fixture.UITextDiscoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.claim('claim:one', 'Management expects a sustained improvement in customer demand.')

    def poll(self, stop_requested=None, prepare=None):
        with patch.object(ui, 'research_library', return_value={'products': []}):
            return ui.poll_ui_texts(self.f.db, self.f.mission, state_dir=self.f.state,
                mapping={}, prepare=prepare or (lambda p: self.fail('paid')),
                stop_requested=stop_requested)

    def test_pre_requested_stop_never_opens_mapping_database_or_state(self):
        with patch.object(store, 'load_ui_source_mapping', side_effect=AssertionError('loaded')):
            result = ui.poll_ui_texts_from_store(None, self.f.mission,
                database=self.f.state / 'absent.sqlite', state_dir=self.f.state,
                prepare=lambda p: self.fail('paid'), stop_requested=lambda: True)
        self.assertTrue(result['quiesced'])
        self.assertFalse(result['discovery_complete'])
        self.assertTrue(result['pending_unknown'])
        self.assertFalse(self.f.state.exists())
        with self.assertRaises(ValueError):
            ui.poll_ui_texts_from_store(None, {}, database=self.f.state,
                state_dir=self.f.state, prepare=lambda p: None,
                max_attempts=0, stop_requested=lambda: True)

    def test_stop_during_claim_stream_has_no_batch_attempt_or_partial_cache(self):
        stopped = False
        real = ui._current_index_canonicality
        def read(*args, **kwargs):
            nonlocal stopped
            result = real(*args, **kwargs)
            stopped = True
            return result
        with patch.object(ui, '_current_index_canonicality', side_effect=read):
            result = self.poll(lambda: stopped)
        self.assertTrue(result['pending_unknown'])
        self.assertEqual(list((self.f.state / 'manifests').glob('*.json')), [])
        self.assertEqual(list((self.f.state / 'results').glob('*.json')), [])
        resumed = self.poll(prepare=lambda p: {'status': 'completed'})
        self.assertEqual(resumed['attempted'], 1)

    def test_stop_during_manifest_proof_preserves_all_old_results_and_rechecks(self):
        self.poll(prepare=lambda p: {'status': 'pending', 'reason': 'original result'})
        before = {p: p.read_bytes() for p in self.f.state.rglob('*.json')}
        stopped = False
        real = ui._validate_manifest
        def validate(*args):
            nonlocal stopped
            value = real(*args)
            stopped = True
            return value
        with patch.object(ui, '_validate_manifest', side_effect=validate):
            result = self.poll(lambda: stopped)
        self.assertTrue(result['pending_unknown'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.f.db.execute("UPDATE claim_versions SET content_hash='bad'")
        with self.assertRaisesRegex(ValueError, 'authority drifted'):
            self.poll()
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_paid_prepare_finishes_and_saves_before_stop_defers_next_batch(self):
        self.f.claim('claim:two', 'Another independent observation concerns changing enterprise demand.')
        stopped = False
        calls = []
        def prepare(product):
            nonlocal stopped
            calls.append(product)
            stopped = True
            return {'status': 'completed', 'fee_receipt': 'kept-original'}
        with patch.object(ui, '_chunks', side_effect=lambda entries: ([e] for e in entries)):
            result = self.poll(lambda: stopped, prepare)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['attempted'], 1)
        self.assertTrue(result['quiesced'])
        saved = [json.loads(p.read_text()) for p in (self.f.state / 'results').glob('*.json')]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['status'], 'completed')
        self.assertEqual(saved[0]['result']['fee_receipt'], 'kept-original')
        self.assertEqual(result['pending'], 1)

    def test_normal_callback_and_default_have_identical_full_results(self):
        first = self.poll(prepare=lambda p: {'status': 'completed'})
        self.f.state = self.f.state.with_name('same-input-second-state')
        second = self.poll(lambda: False, prepare=lambda p: {'status': 'completed'})
        self.assertEqual(second.pop('quiesced'), False)
        self.assertEqual(first, second)

    def test_actual_worker_skips_mapping_after_persisted_product_quiesce(self):
        fixture = worker_fixture.PublicationQuiesceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(store, 'load_ui_source_mapping', side_effect=AssertionError('loaded after stop')):
            fixture.test_actual_worker_ack_is_after_product_state_and_all_callbacks_returned()
        checkpoint = json.loads((fixture.work / 'worker-last-run.json').read_text())
        self.assertFalse(checkpoint['ui_texts']['discovery_complete'])
        self.assertTrue(checkpoint['ui_texts']['pending_unknown'])


if __name__ == '__main__':
    unittest.main()
