import copy
import json
import hashlib
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from dalton_core.research_localization import build_localization
from dalton_core.research_localization_store import (
    localize_library, publish_attachment, publish_ui_texts, load_ui_texts,
    publish_reviewed_attachment, has_reviewed_attachment)


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

    def test_required_publication_hides_unreviewed_prose_without_changing_authority(self):
        (self.root/'research-language-policy.json').write_text('{"required":true}')
        publish_attachment(self.directory,self.product,self.candidate)
        result=localize_library(self.connection,{'products':[self.product]})['products'][0]
        self.assertEqual(result['publication_status'],'pending_language_review')
        self.assertEqual(result['sections'],[])
        self.assertEqual(result['status'],'available')
        self.assertEqual(result['approval'],self.product['approval'])
        self.assertFalse(has_reviewed_attachment(self.directory,self.product))
        rows=[{k:r[k] for k in ('index','title','body','gaps')} for r in self.candidate['sections']]
        stages=[{'status':'passed','localized':{'sections':rows},'independence':{'independent':True},
                 'language_review':{'status':'ready_for_publication','suggestions_markdown':'# 建议\n表达清楚。'}}]
        publish_reviewed_attachment(self.directory,self.product,self.candidate,stages)
        self.assertTrue(has_reviewed_attachment(self.directory,self.product))
        shown=localize_library(self.connection,{'products':[self.product]})['products'][0]
        self.assertEqual(shown['publication_status'],'ready')
        self.assertEqual(shown['sections'][0]['body'],'收入增长。')
        changed=copy.deepcopy(self.product);changed['sections'][0]['body']='Revenue fell.'
        shown=localize_library(self.connection,{'products':[changed]})['products'][0]
        self.assertEqual(shown['publication_status'],'pending_language_review')

    def test_known_legacy_ui_mapping_remains_readable_without_rewrite(self):
        legacy=copy.deepcopy(self.candidate);legacy['rules_version']='simplified-chinese-research-prose:0.2'
        body=dict(legacy);body.pop('content_hash')
        legacy['content_hash']=hashlib.sha256((json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()).hexdigest()
        publish_ui_texts(self.directory,[{'source':self.product,'localization':legacy}])
        before=(self.directory/'ui-texts.json').read_bytes()
        self.assertEqual(load_ui_texts(self.db),{'Revenue increased.':'收入增长。'})
        self.assertEqual((self.directory/'ui-texts.json').read_bytes(),before)

    def test_new_ui_batch_keeps_other_approved_page_strings(self):
        publish_ui_texts(self.directory,[{'source':self.product,'localization':self.candidate}])
        second=copy.deepcopy(self.product);second['sections'][0]['body']='Revenue fell.'
        translated=build_localization(second,{'sections':[{'index':0,'title':'收入','body':'收入下降。','gaps':[]}]},self.verifier)
        publish_ui_texts(self.directory,[{'source':second,'localization':translated}])
        self.assertEqual(load_ui_texts(self.db),{'Revenue increased.':'收入增长。','Revenue fell.':'收入下降。'})


class UiTextShardingTests(unittest.TestCase):
    """0.2: one file per batch, so the mapping grows without a single-file ceiling.

    Live 2026-09-22 the 0.1 ``ui-texts.json`` held 836 batches in
    16,775,691 of its 16,777,216 permitted bytes; every later publication that
    merged a UI batch failed with "display attachment exceeds the size limit".
    """

    setUp = LocalizationStoreTests.setUp
    tearDown = LocalizationStoreTests.tearDown

    def batch(self, body, translated):
        source = copy.deepcopy(self.product)
        source['version_ref'] = 'ui:' + body
        source['sections'][0]['body'] = body
        localization = build_localization(source, {'sections': [
            {'index': 0, 'title': '界面文字', 'body': translated, 'gaps': []}]}, self.verifier)
        return {'source': source, 'localization': localization}

    def test_each_batch_is_its_own_record_and_the_index_only_lists_refs(self):
        publish_ui_texts(self.directory, [self.batch('Revenue increased.', '收入增长。')])
        publish_ui_texts(self.directory, [self.batch('Revenue fell.', '收入下降。')])
        index = json.loads((self.directory / 'ui-texts.json').read_text())
        self.assertEqual(index['schema_version'], 'cockpit-ui-texts:0.2')
        self.assertEqual(len(index['batch_refs']), 2)
        records = sorted((self.directory / 'ui-records').glob('*.json'))
        self.assertEqual(sorted(path.stem for path in records), sorted(index['batch_refs']))
        self.assertEqual(load_ui_texts(self.db),
                         {'Revenue increased.': '收入增长。', 'Revenue fell.': '收入下降。'})

    def test_the_mapping_can_outgrow_the_old_single_file_limit(self):
        from dalton_core import research_localization_store as store

        filler = 'x' * 3000
        with unittest.mock.patch.object(store, '_MAX_BYTES', 64 * 1024):
            for number in range(40):
                publish_ui_texts(self.directory, [self.batch(
                    f'Line {number} {filler}', f'第 {number} 行')])
            total = sum(path.stat().st_size for path in (self.directory / 'ui-records').glob('*.json'))
            self.assertGreater(total, 64 * 1024)
            self.assertLess((self.directory / 'ui-texts.json').stat().st_size, 64 * 1024)
            self.assertEqual(len(load_ui_texts(self.db)), 40)

    def test_a_legacy_single_file_is_migrated_in_order_on_the_next_publish(self):
        first = self.batch('Same text.', '第一版译文。')
        second = self.batch('Revenue fell.', '收入下降。')
        second['source']['sections'][0]['body'] = 'Same text.'
        second['localization'] = build_localization(second['source'], {'sections': [
            {'index': 0, 'title': '界面文字', 'body': '第二版译文。', 'gaps': []}]}, self.verifier)
        self.directory.mkdir(parents=True)
        (self.directory / 'ui-texts.json').write_text(json.dumps(
            {'schema_version': 'cockpit-ui-texts:0.1', 'batches': [first, second]},
            ensure_ascii=False))
        # Readable as it stands, first approved translation wins.
        self.assertEqual(load_ui_texts(self.db), {'Same text.': '第一版译文。'})
        publish_ui_texts(self.directory, [self.batch('Revenue increased.', '收入增长。')])
        index = json.loads((self.directory / 'ui-texts.json').read_text())
        self.assertEqual(index['schema_version'], 'cockpit-ui-texts:0.2')
        self.assertEqual(len(index['batch_refs']), 3)
        self.assertEqual(load_ui_texts(self.db), {'Same text.': '第一版译文。',
                                                  'Revenue increased.': '收入增长。'})

    def test_one_unreadable_record_costs_only_its_own_strings(self):
        publish_ui_texts(self.directory, [self.batch('Revenue increased.', '收入增长。')])
        publish_ui_texts(self.directory, [self.batch('Revenue fell.', '收入下降。')])
        index = json.loads((self.directory / 'ui-texts.json').read_text())
        (self.directory / 'ui-records' / (index['batch_refs'][0] + '.json')).write_text('{}')
        # A fresh index write moves the cache key, as every publish does.
        publish_ui_texts(self.directory, [self.batch('Margin rose.', '利润率上升。')])
        self.assertEqual(load_ui_texts(self.db),
                         {'Revenue fell.': '收入下降。', 'Margin rose.': '利润率上升。'})


if __name__ == '__main__':
    unittest.main()
