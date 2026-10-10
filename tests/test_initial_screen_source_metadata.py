"""Keep pack document associations through parsing and the publish boundary.

Associations are metadata, never sentence-level issuer verification.
"""
import copy
import json
import unittest

from dalton_core.initial_screen import _source_ref_entry, parse_document_output
from dalton_core.mission_deliverable import MissionDeliverableValidationError, validate_section
from dalton_core.product_contract import normalise_pack, tag_entry
from dalton_core.research_quality_score import artefact_from_deliverable
from tests.synthetic_company import COMPANY, material_pack, strl_document
from tests.test_mission_deliverable import DeliverableHarness


def held_pack():
    pack = copy.deepcopy(normalise_pack(material_pack()))
    pack['json'].setdefault('passage_tags', {})['P1'] = {
        'company_ref': COMPANY,
        'company_refs': ['company:synthetic:peer', COMPANY, 'company:synthetic:peer'],
        'doc_kind': 'transcript',
        'document_ref': 'doc:synthetic-held:' + 'x' * 250,
    }
    return pack


def section(source):
    return {'title': '来源说明', 'body': '持有资料讨论了交付方式与竞争环境。',
            'numbers': [], 'claim_refs': [], 'gaps': [], 'source_refs': [source]}


class SourceMetadataProjectionTests(unittest.TestCase):
    def test_string_tag_metadata_survives_both_projections_without_pack_mutation(self):
        pack = held_pack()
        before = copy.deepcopy(pack)
        entry = tag_entry(pack, 'P1')
        projected = _source_ref_entry(entry)
        for key in ('company_refs', 'doc_kind', 'document_ref'):
            self.assertEqual(projected[key], pack['json']['passage_tags']['P1'][key])
        self.assertEqual(pack, before)
        projected['company_refs'].append('company:synthetic:other')
        self.assertEqual(pack, before)
        entry['company_refs'].append('company:synthetic:another')
        self.assertEqual(pack, before)
        self.assertNotIn('role', projected)

    def test_mapping_tag_keeps_its_own_bound_metadata(self):
        pack = held_pack()
        pack['tags']['P1'] = {'ref': pack['tags']['P1'], 'source_kind': 'expert_call',
                             'company_refs': [COMPANY, 'company:synthetic:peer'],
                             'doc_kind': 'expert_call', 'document_ref': 'doc:synthetic:expert'}
        entry = tag_entry(pack, 'P1')
        projected = _source_ref_entry(entry)
        self.assertEqual(projected['company_refs'], pack['tags']['P1']['company_refs'])
        self.assertEqual(projected['doc_kind'], 'expert_call')
        self.assertEqual(projected['document_ref'], 'doc:synthetic:expert')
        entry['company_refs'].append('company:synthetic:other')
        self.assertEqual(pack['tags']['P1']['company_refs'], [COMPANY, 'company:synthetic:peer'])

    def test_document_parser_preserves_typed_associations(self):
        pack = held_pack()
        before = copy.deepcopy(pack)
        sections, findings = parse_document_output(strl_document(), pack=pack)
        self.assertEqual(findings, [])
        cited = [r for s in sections for r in s['source_refs'] if r['tag'] == 'P1']
        self.assertTrue(cited)
        for source in cited:
            checked = validate_section(section(source), strict=True)['source_refs'][0]
            self.assertEqual(checked['company_refs'], before['json']['passage_tags']['P1']['company_refs'])
            self.assertEqual(checked['document_ref'], before['json']['passage_tags']['P1']['document_ref'])
        self.assertEqual(pack, before)

    def test_legacy_projection_and_publish_shape_stay_identical(self):
        entry = {'ref': 'doc:legacy', 'tag': 'P1', 'kind': 'passage',
                 'source_kind': 'transcript', 'company_ref': COMPANY, 'title': '旧资料'}
        expected = {k: entry[k] for k in ('ref', 'tag', 'source_kind', 'company_ref', 'title')}
        self.assertEqual(_source_ref_entry(entry), expected)
        self.assertEqual(validate_section(section(expected), strict=True)['source_refs'], [expected])

    def test_publish_resource_limit_is_not_an_eight_company_financial_rule(self):
        refs = [f'company:synthetic:{i}' for i in range(256)]
        source = {'ref': 'doc:multi', 'company_refs': refs}
        self.assertEqual(validate_section(section(source))['source_refs'][0]['company_refs'], refs)
        with self.assertRaises(MissionDeliverableValidationError):
            validate_section(section({**source, 'company_refs': refs + ['company:synthetic:too-many']}))

    def test_invalid_list_and_fabricated_roles_remain_rejected(self):
        for refs in ('company:synthetic:peer', (), [], [''], [' '], [None], [7], [{}]):
            with self.subTest(refs=refs), self.assertRaises(MissionDeliverableValidationError):
                validate_section(section({'ref': 'doc:multi', 'company_refs': refs}))
        for key in ('doc_kind', 'document_ref'):
            for value in ('', ' ', None, [], 7):
                with self.subTest(key=key, value=value), self.assertRaises(MissionDeliverableValidationError):
                    validate_section(section({'ref': 'doc:multi', key: value}))
        for extra in ({'role': 'subject_fact'}, {'claim_subject_ref': COMPANY}, {'secret': 'x'}):
            with self.subTest(extra=extra), self.assertRaises(MissionDeliverableValidationError):
                validate_section(section({'ref': 'doc:multi', **extra}))


class SourceMetadataPublishingTests(DeliverableHarness):
    def test_publish_round_trip_preserves_lists_document_identity_and_old_records(self):
        self.grant('deliverable')
        legacy = {'ref': 'doc:legacy', 'source_kind': 'expert_call'}
        first = self.publish([section(legacy)])
        old_row = self.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_deliverable_versions WHERE version_id=?',
            (first['id'],)).fetchone()
        old_record = tuple(old_row)
        source = _source_ref_entry(tag_entry(held_pack(), 'P1'))
        second = self.publish([section(source)])
        record = self.authority.latest(second['deliverable_ref'])
        self.assertEqual(record['sections'][0]['source_refs'], [source])
        projected = artefact_from_deliverable(record)
        self.assertEqual(projected['sections'][0]['source_refs'], [source])
        persisted_old = self.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_deliverable_versions WHERE version_id=?',
            (first['id'],)).fetchone()
        self.assertEqual(tuple(persisted_old), old_record)
        self.assertEqual(json.loads(persisted_old['record_json'])['sections'][0]['source_refs'], [legacy])


if __name__ == '__main__':
    unittest.main()
