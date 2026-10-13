"""Broker display headings stay outside exact-original passage citations."""
import copy
import unittest
from dalton_core.evidence_index import make_passage_ref, render_passage_block
from dalton_core.product_contract import tag_entry
from dalton_core.product_passage_validation import rendered_passage_matches


def pack_for(first, second='Second report original.', *, company='company:alpha', house='HSBC', extras=''):
    rows = [dict(document_ref='doc:alpha', passage_ref=make_passage_ref('doc:alpha',0,len(first),first),
                 text=first, kind='broker_report', house='Morgan Stanley', date='2026-09-16',title='First report'),
            dict(document_ref='doc:beta', passage_ref=make_passage_ref('doc:beta',0,len(second),second),
                 text=second, kind='broker_report', house=house, date='2026-09-09',title='Second report')]
    blocks = [render_passage_block([row],start=i+1) for i,row in enumerate(rows)]
    header = f'券商：{house}（偏多）· 2026-09-09{extras}\n《Second report》'
    return {'text':blocks[0]['text']+header+'\n'+blocks[1]['text'],
            'tags':{**blocks[0]['tags'],**blocks[1]['tags']},
            'json':{'passage_tags':{'P2':dict(doc_kind='broker_report',document_ref='doc:beta',
                                           house=house,date='2026-09-09',company_ref=company)}}}


class BrokerRendererBoundaryTests(unittest.TestCase):
    def test_two_houses_and_companies_leave_each_original_and_last_passage_intact(self):
        for company,house in [('company:alpha','HSBC'),('company:beta','Wells Fargo Securities, LLC')]:
            with self.subTest(company=company,house=house):
                first='  Revenue is 12.5.\n';last='Second report original.\n券商：Local（偏多）· 2026-09-09\n《A quoted title》'
                p=pack_for(first,last,company=company,house=house,
                           extras='· 评级 buy· 目标价 [N52] 338.00（2026-09-09）· FY2026 EPS [N40] 13.1')
                before=copy.deepcopy(p)
                self.assertTrue(rendered_passage_matches(first,tag_entry(p,'P1')['text']))
                self.assertEqual(tag_entry(p,'P2')['text'],last)
                self.assertEqual(p,before)

    def test_header_like_original_suffix_immediately_before_real_next_block_is_not_cut(self):
        original='An original quote:\n券商：HSBC（偏多）· 2026-09-09\n《Second report》'
        p=pack_for(original)
        # No report-level display header: the suffix belongs to P1's original.
        p['text']=p['text'].replace(original+'\n券商：HSBC（偏多）· 2026-09-09\n《Second report》',original)
        self.assertEqual(tag_entry(p,'P1')['text'],original)
        # Also retain this suffix when a separate real display header follows it.
        self.assertEqual(tag_entry(pack_for(original),'P1')['text'],original)

    def test_unregistered_or_mismatched_header_is_not_removed(self):
        for change in ('title','house','date','document','missing_metadata','unregistered_tag'):
            with self.subTest(change=change):
                p=pack_for('Original 12.5.')
                if change=='title':p['text']=p['text'].replace('\n《Second report》','\n《Foreign report》',1)
                elif change=='house':p['json']['passage_tags']['P2']['house']='Foreign Bank'
                elif change=='date':p['json']['passage_tags']['P2']['date']='2020-01-01'
                elif change=='document':p['json']['passage_tags']['P2']['document_ref']='doc:foreign'
                elif change=='missing_metadata':p['json']={}
                else:p['tags'].pop('P2')
                self.assertIn('券商：',tag_entry(p,'P1')['text'])
                self.assertFalse(rendered_passage_matches('Original 12.5.',tag_entry(p,'P1')['text']))

    def test_internal_corruption_is_not_silently_repaired(self):
        p=pack_for('Original revenue 12.5.')
        p['text']=p['text'].replace('revenue 12.5.','revenue 99.9.')
        self.assertFalse(rendered_passage_matches('Original revenue 12.5.',tag_entry(p,'P1')['text']))

    def test_old_plain_pack_and_mapping_tags_keep_their_bytes(self):
        raw='Original text with no renderer header.'
        p=pack_for(raw);p['text']=p['text'].replace('券商：HSBC（偏多）· 2026-09-09\n《Second report》\n','')
        self.assertEqual(tag_entry(p,'P1')['text'],raw)
        self.assertEqual(tag_entry(p,'P2')['text'],'Second report original.')
        p['tags']['P1']={'ref':p['tags']['P1'],'text':raw+'\n券商：Not a delimiter'}
        self.assertEqual(tag_entry(p,'P1')['text'],p['tags']['P1']['text'])
