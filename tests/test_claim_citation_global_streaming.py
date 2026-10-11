"""Global canonical text identity survives streaming citation metadata reads."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core.claim_review import ClaimReviewDriver
import tests.test_claim_support_backfill as backfill_fixtures
from tests.test_claim_support_verification import _reply


class NoFetchAllCursor:
    def __init__(self, cursor):
        self.cursor = cursor
    def __iter__(self):
        return iter(self.cursor)
    def fetchall(self):
        raise AssertionError('citation metadata must stream rather than fetch all payloads')


class StreamingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.queries = []
    def execute(self, query, *args):
        self.queries.append(query)
        return NoFetchAllCursor(self.connection.execute(query, *args))


class GlobalCitationStreamingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
          CREATE TABLE evidence_relations(claim_version_id TEXT,evidence_version_id TEXT,relation TEXT);
          CREATE TABLE evidence_versions(evidence_version_id TEXT PRIMARY KEY,evidence_json TEXT);
          CREATE TABLE transcript_claim_citation_bindings(binding_id TEXT PRIMARY KEY,
            correction_set_version_ref TEXT,source_start INTEGER,source_end INTEGER);
          CREATE TABLE transcript_correction_set_versions(version_id TEXT PRIMARY KEY,record_json TEXT);
        ''')
        self.reader = ClaimReviewDriver.__new__(ClaimReviewDriver)
        self.reader.connection = StreamingConnection(self.connection)
        self.reader._document_refs = {}

    def chain(self, claim, suffix, digest, document, *, padding=''):
        evidence, binding, correction = 'evidence:'+suffix, 'transcript-claim-citation-binding:'+suffix, 'correction:'+suffix
        self.connection.execute('INSERT INTO evidence_relations VALUES(?,?,?)', (claim,evidence,'supports'))
        self.connection.execute('INSERT INTO evidence_versions VALUES(?,?)', (evidence,json.dumps({
            'id':evidence,'artifact_refs':[{'ref':binding}], 'source_envelope_ref':'envelope:'+suffix,
            'unused_large_payload':padding})))
        self.connection.execute('INSERT INTO transcript_claim_citation_bindings VALUES(?,?,?,?)', (binding,correction,2,9))
        self.connection.execute('INSERT INTO transcript_correction_set_versions VALUES(?,?)', (correction,json.dumps({
            'id':correction,'source_content_hash':digest,'document_ref':document,
            'unused_large_payload':padding,'raw_review':{'rationale':'drafted via route-decision:producer', 'unused_detail':padding}})))

    def test_first_global_claim_and_last_support_keep_canonical_over_lexical_order(self):
        digest = 'a'*64
        self.chain('claim:z-first','old',digest,'document:old')
        self.chain('claim:a-second','second',digest,'document:second')
        self.chain('claim:z-first','latest',digest,'document:canonical')
        citations = self.reader._citations()
        self.assertEqual(list(citations), ['claim:z-first','claim:a-second'])
        self.assertEqual(citations['claim:z-first']['document_ref'],'document:canonical')
        self.assertEqual(self.reader._document_refs[digest],'document:canonical')
        self.assertEqual(citations['claim:a-second']['document_ref'],'document:second')

    def test_current_document_missing_still_reads_hash_from_global_canonical(self):
        text = 'The canonical bytes are identical.'
        digest = hashlib.sha256(text.encode()).hexdigest()
        body_hash = 'b'*64
        self.chain('claim:canonical','first',digest,'web:body-sha256:'+body_hash)
        self.chain('claim:current','later',digest,'web:body-sha256:'+'c'*64)
        citations = self.reader._citations()
        self.assertNotEqual(citations['claim:current']['document_ref'],self.reader._document_refs[digest])
        reads=[]
        self.reader._read = lambda key: reads.append(key) or (b'body' if key == body_hash else None)
        self.reader._renders_left=1
        self.reader._web_media_types=lambda *_:['text/html']
        with patch('dalton_core.public_web_extraction_source.render_public_web_text_candidates',return_value=[{'text':text}]):
            self.assertEqual(self.reader.source_text(citations['claim:current']['digest']),text)
        self.assertEqual(reads,[digest,body_hash])
        self.assertEqual(self.reader._renders_left,0)

    def test_payloads_stream_in_four_queries_without_changing_span_or_route(self):
        self.chain('claim:chosen','one','d'*64,'document:one',padding='x'*100000)
        citations=self.reader._citations()
        self.assertEqual(len(self.reader.connection.queries),4)
        self.assertEqual(citations['claim:chosen'],{
            'digest':'d'*64,'start':2,'end':9,'document_ref':'document:one',
            'route_decision_ref':'route-decision:producer','source_envelope_ref':'envelope:one'})

    def test_malformed_unrelated_record_still_refuses_instead_of_being_filtered_away(self):
        self.chain('claim:chosen','one','d'*64,'document:one')
        self.connection.execute('INSERT INTO evidence_versions VALUES(?,?)',('unrelated','not JSON'))
        with self.assertRaises(json.JSONDecodeError):
            self.reader._citations()


class BackfillDigestCompatibilityTests(backfill_fixtures.BackfillHarness):
    def test_same_digest_current_doc_missing_keeps_shared_read_budget_and_paid_verdict_replay(self):
        source=backfill_fixtures.SOURCE
        available='web:body-sha256:'+'a'*64
        self.claim(statement='EPAM described engineering demand.',source=source,span=(0,62),document_ref=available,store_source=False)
        self.claim(statement='EPAM said demand improved.',source=source,span=(0,62),document_ref='web:body-sha256:'+'b'*64,store_source=False)
        backfill=self.backfill();reads=[]
        backfill.reader._read=lambda key:reads.append(key) or (b'body'if key=='a'*64 else None)
        backfill.reader._web_media_types=lambda *_:['text/html'];backfill.reader._renders_left=1
        self.model.replies.append(_reply(('supported','about_subject',None),('supported','about_subject',None)))
        with patch('dalton_core.public_web_extraction_source.render_public_web_text_candidates',return_value=[{'text':source}]):
            result=backfill.run_once(max_items=10,max_documents=1)
            self.assertEqual((result['examined'],result['unreadable'],result['remaining_after']),(2,0,0))
            self.assertEqual(len(reads),2)  # one digest lookup plus one canonical render
            backfill.run_once(max_items=10,max_documents=1)
        self.assertEqual(len(self.model.calls),1)  # no re-verification of already paid results

    def test_negative_digest_cache_retains_the_original_canonical_rule(self):
        source=backfill_fixtures.SOURCE
        self.claim(statement='EPAM described demand.',source=source,span=(0,62),document_ref='web:body-sha256:'+'a'*64,store_source=False)
        self.claim(statement='EPAM saw activity.',source=source,span=(0,62),document_ref='web:body-sha256:'+'b'*64,store_source=False)
        backfill=self.backfill();reads=[]
        backfill.reader._read=lambda key:reads.append(key) or (b'available other body' if key == 'b'*64 else None)
        backfill.reader._renders_left=1
        result=backfill.run_once(max_items=10,max_documents=1)
        self.assertEqual((result['unreadable'],result['calls'],result['remaining_after']),(2,0,0))
        self.assertEqual(len(reads),2)
        self.assertEqual(reads[-1],'a'*64)  # second doc does not silently change old canonical
        self.assertEqual(self.model.calls,[])


if __name__ == '__main__':
    unittest.main()
