from __future__ import annotations
import copy
import json
import sqlite3
import unittest
from pathlib import Path
from dalton_core.needs_human import _current_skipped_source, parked_publications
from dalton_core.final_surface_products import final_surface_products
from dalton_core.research_source_projection import publication_source_hash
from dalton_core.research_publication_worker import _hash, _identity
from dalton_core.store import content_hash, canonical_json
from tests.test_mission_web_search_discovery import WebSearchChildTests, ACN

class CurrentSourceCardsTests(unittest.TestCase):
    setUp = WebSearchChildTests.setUp
    def source(self,text='Revenue',version='model:synthetic:1'):
        db=self.state/'core.sqlite';c=sqlite3.connect(db)
        c.execute('CREATE TABLE IF NOT EXISTS forecast_model_versions(version_id TEXT PRIMARY KEY,version_number INTEGER,company_ref TEXT,content_hash TEXT,record_json TEXT)')
        record={'id':version,'company_ref':ACN,'drivers':[{'label':text}]}
        record['content_hash']=content_hash(record)
        c.execute('INSERT INTO forecast_model_versions VALUES (?,?,?,?,?)',(version,1,ACN,record['content_hash'],canonical_json(record)));c.commit();c.row_factory=sqlite3.Row
        product=final_surface_products(c,self.mission,ACN,kinds={'surface_model_notes'})[0];c.close()
        return product,_identity(product),publication_source_hash(product)
    def parked(self,product,identity,oldhash):
        work=self.state/'research-publication-work';(work/'products').mkdir(parents=True)
        record={'products':[{'identity':identity,'product_hash':oldhash,'parked':True}], 'ui_texts':{}}
        (work/'worker-last-run.json').write_text(json.dumps(record))
        value={'status':'skipped','product_hash':oldhash,'identity':identity,
               'result':{'skip_reason':'source_section_empty','empty_sections':['模型说明'],'skipped_at':'old'}}
        (work/'products'/f'{_hash(identity)}.json').write_text(json.dumps(value))
        return work
    def test_current_typed_source_is_pending_not_empty_published_or_owner_retry(self):
        product,identity,key=self.source();work=self.parked(product,identity,key)
        before={str(p):p.read_bytes() for p in work.rglob('*.json')}
        cards=parked_publications(self.state,{ACN:'Synthetic Co'})
        self.assertEqual(len(cards),1);card=cards[0]
        self.assertIn('正等待中文版核验',card['title'])
        self.assertFalse(card['actionable']);self.assertEqual(card['responsibility'],'background')
        self.assertEqual(card['detail']['status'],'waiting_publication')
        self.assertEqual(card['detail']['prior_projection_hash'],key)
        self.assertNotIn('retry',card['detail'])
        self.assertEqual({str(p):p.read_bytes() for p in work.rglob('*.json')},before)
        self.assertFalse((work/'publication-limits.sqlite3').exists())
    def test_unproved_key_or_body_placeholder_keeps_recorded_warning(self):
        _,identity,key=self.source(text='TBD')
        self.assertIsNone(_current_skipped_source(self.state,identity,key))
        self.assertIsNone(_current_skipped_source(self.state,identity,'0'*64))
    def test_formal_record_tamper_or_wrong_sql_subject_cannot_hide_warning(self):
        _,identity,key=self.source()
        c=sqlite3.connect(self.state/'core.sqlite')
        c.execute('UPDATE forecast_model_versions SET company_ref=?',('company:other',));c.commit();c.close()
        self.assertIsNone(_current_skipped_source(self.state,identity,key))
    def test_formal_hash_tamper_refuses_even_with_same_display_body(self):
        _,identity,key=self.source();c=sqlite3.connect(self.state/'core.sqlite')
        c.execute('UPDATE forecast_model_versions SET content_hash=?',('0'*64,));c.commit();c.close()
        self.assertIsNone(_current_skipped_source(self.state,identity,key))
    def test_replaced_formal_version_is_not_claimed_current(self):
        _,identity,key=self.source()
        c=sqlite3.connect(self.state/'core.sqlite');record={'id':'model:synthetic:2','company_ref':ACN,'drivers':[{'label':'Revenue'}]};record['content_hash']=content_hash(record)
        c.execute('INSERT INTO forecast_model_versions VALUES (?,?,?,?,?)',(record['id'],2,ACN,record['content_hash'],canonical_json(record)));c.commit();c.close()
        self.assertIsNone(_current_skipped_source(self.state,identity,key))
    def test_missing_or_corrupt_authority_retains_original_card(self):
        product,identity,key=self.source();self.parked(product,identity,key)
        (self.state/'core.sqlite').unlink()
        cards=parked_publications(self.state)
        self.assertIn('源稿有空节',cards[0]['title'])
        self.assertNotEqual(cards[0]['detail']['status'],'waiting_publication')

del WebSearchChildTests
