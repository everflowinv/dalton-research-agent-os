"""A selected read must preserve the accepted plan's exact historical source."""
import copy
import unittest
from unittest.mock import patch
from dalton_core.document_research_inventory import inventory_with_registry
from dalton_core.mission_document_research import PURPOSE
from tests import test_numeric_required_original_inventory as fixtures


class SelectedRegisteredOriginalTests(unittest.TestCase):
    def fixture(self):
        helper = fixtures.NumericOriginalInventoryTests('runTest')
        self.addCleanup(helper.doCleanups)
        f, authority, admission, executor, producer, verifier = helper.fixture()
        return f, authority, admission, helper.current_rendering(admission)

    def inventory(self, f, authority, admission, current):
        with patch.object(authority.registry, 'inspect_acquired_document',
                          return_value={'available': True, 'registration': current}):
            return inventory_with_registry(core=f.store, mission=f.mission,
                registry=authority.registry, purpose=PURPOSE,
                selected_document_refs=(admission['request']['registration']['document_ref'],))

    def test_registered_version_survives_selected_render_without_qualitative_rejection(self):
        f, authority, admission, current = self.fixture()
        before = f.store.connection.total_changes
        old = admission['request']['registration']
        with patch.object(authority.registry, '_reresolve', wraps=authority.registry._reresolve) as replay:
            result = self.inventory(f, authority, admission, current)
        self.assertEqual(result['registration_by_hash'][old['content_hash']], old)
        self.assertEqual(result['registration_by_hash'][current['content_hash']], current)
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(f.store.connection.total_changes, before)

    def test_mismatching_original_is_not_aliased_and_current_source_survives(self):
        f, authority, admission, current = self.fixture()
        with patch.object(authority.registry, '_reresolve', return_value=(current, 'irrelevant')):
            result = self.inventory(f, authority, admission, current)
        self.assertEqual(set(result['registration_by_hash']), {current['content_hash']})
        errors = result['unavailable_documents_by_company'][admission['company_ref']]
        self.assertEqual(errors[0]['reason'], 'selected_original_unavailable')
        self.assertEqual(errors[0]['document_version_hash'], admission['document_authority_hash'])

    def test_unselected_source_is_not_read(self):
        f, authority, admission, current = self.fixture()
        with patch.object(authority.registry, '_reresolve', side_effect=AssertionError('unselected source')):
            result = inventory_with_registry(core=f.store, mission=f.mission,
                registry=authority.registry, purpose=PURPOSE, selected_document_refs=('doc:unrelated',))
        self.assertEqual(result['registration_by_hash'], {})
