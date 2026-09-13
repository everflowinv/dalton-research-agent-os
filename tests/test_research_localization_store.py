import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_localization import build_localization
from dalton_core.research_localization_store import (
    localize_library, publish_attachment, publish_ui_texts, load_ui_texts)


class LocalizationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'core.sqlite'
        self.connection = sqlite3.connect(self.db)
        self.product = {'kind': 'initial_screen', 'version_ref': 'screen:acn:1',
                        'status': 'available', 'content_hash': 'a' * 64,
                        'approval': {'status': 'not_applicable'},
                        'sections': [{'title': 'Revenue', 'body': 'Revenue increased.',
                                      'sources': ['claim:one'], 'numbers': [{'value': 12}]}]}
        self.verifier = {'verdict': 'pass', 'faithful': True, 'no_new_facts': True,
                         'meaning_preserved': True, 'findings': []}
        self.candidate = build_localization(self.product, {'sections': [
            {'index': 0, 'title': '营业收入', 'body': '收入增长。', 'gaps': []}]}, self.verifier)
        self.directory = self.root / 'research-localization'

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_read_missing_attachment_does_not_write_or_hide_product(self):
        library = {'products': [self.product]}
        self.assertEqual(localize_library(self.connection, library), library)
        self.assertFalse(self.directory.exists())

    def test_exact_source_overlay_keeps_approvals_numbers_and_source_immutable(self):
        original = copy.deepcopy(self.product)
        publish_attachment(self.directory, self.product, self.candidate)
        result = localize_library(self.connection, {'products': [self.product]})['products'][0]
        self.assertEqual(result['sections'][0]['body'], '收入增长。')
        for key in ('sources', 'numbers'):
            self.assertEqual(result['sections'][0][key], original['sections'][0][key])
        for key in ('status', 'content_hash', 'version_ref', 'approval'):
            self.assertEqual(result[key], original[key])
        self.assertEqual(self.product, original)

    def test_new_version_cannot_reuse_previous_translation(self):
        publish_attachment(self.directory, self.product, self.candidate)
        product = dict(self.product, version_ref='screen:acn:2')
        self.assertEqual(localize_library(self.connection, {'products': [product]}), {'products': [product]})

    def test_corrupt_attachment_falls_back_to_original_and_explains(self):
        path = publish_attachment(self.directory, self.product, self.candidate)
        path.write_text('{}')
        result = localize_library(self.connection, {'products': [self.product]})['products'][0]
        self.assertEqual(result['sections'], self.product['sections'])
        self.assertIn('待同步', result['localization_status'])

    def test_ui_mapping_only_serves_verified_exact_text(self):
        publish_ui_texts(self.directory, [{'source': self.product, 'localization': self.candidate}])
        self.assertEqual(load_ui_texts(self.db), {'Revenue increased.': '收入增长。'})
        source = copy.deepcopy(self.product)
        source['sections'][0]['body'] = 'Revenue decreased.'
        with self.assertRaises(ValueError):
            publish_ui_texts(self.directory, [{'source': source, 'localization': self.candidate}])
        self.assertEqual(load_ui_texts(self.db), {'Revenue increased.': '收入增长。'})

    def test_symlinked_attachment_is_not_served(self):
        path = publish_attachment(self.directory, self.product, self.candidate)
        moved = self.root / 'outside.json'
        path.rename(moved)
        path.symlink_to(moved)
        result = localize_library(self.connection, {'products': [self.product]})['products'][0]
        self.assertEqual(result['sections'], self.product['sections'])


if __name__ == '__main__':
    unittest.main()
