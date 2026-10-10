"""Synthetic structured sources preserve every original receipt and numeric lexeme."""
import gzip,hashlib,json,tempfile,unittest
from pathlib import Path
from dalton_core.public_web_extraction_source import render_public_web_text,render_public_web_text_candidates,verified_public_web_source,PublicWebSourceError,MAX_JSON_BYTES,MAX_JSON_DEPTH,MAX_JSON_NODES
from dalton_core.public_web_connector import PublicHttpTransport
from tests.test_public_web_fetch_lane import FetchHarness,_Response,URL_A

class JsonRenderTests(unittest.TestCase):
 def render(self,b,media='application/json',**kw):return render_public_web_text(b,raw_media_type=media,**kw)
 def test_paths_keys_and_number_lexemes_are_exact_and_deterministic(self):
  raw=b'{"a/b":{"~key":[1.2300,9007199254740993,-0,1e-9,"1.2300",true,null]},"empty":{}}';a=self.render(raw);self.assertEqual(a,self.render(raw));self.assertEqual(a['renderer'],'json-structured-paths:0.1')
  for line in ['"/a~1b/~0key/0" [number]: 1.2300','"/a~1b/~0key/1" [number]: 9007199254740993','"/a~1b/~0key/2" [number]: -0','"/a~1b/~0key/3" [number]: 1e-9','"/a~1b/~0key/4" [string]: "1.2300"','"/a~1b/~0key/5" [boolean]: true','"/a~1b/~0key/6" [null]: null','"/empty" [object]: {}']:self.assertIn(line,a['text'])
 def test_json_suffix_media_and_unicode_control_strings_are_not_html(self):
  for media in ['application/vnd.api+json','Application/ld+json; charset=UTF-8','application/problem+json']:
   r=self.render('{"article":"中文\\n<script>literal</script>"}'.encode(),media);self.assertIn('中文\\n<script>literal</script>',r['text']);self.assertNotEqual(r['renderer'],'html-visible-blocks:0.1')
 def test_duplicate_keys_and_nonfinite_values_refuse(self):
  for b,reason in [(b'{"a":1,"a":2}','duplicate'),(b'{"data":{"a":1,"a":2}}','duplicate'),(b'{"v":NaN}','non-finite'),(b'{"v":Infinity}','non-finite'),(b'{"v":-Infinity}','non-finite')]:
   with self.subTest(body=b),self.assertRaisesRegex(PublicWebSourceError,reason):self.render(b)
 def test_no_api_error_or_metadata_response_is_article_content(self):
  for b in [b'{"errors":[{"detail":"not found"}]}',b'{"error":"denied","data":"do not quote"}',b'{"status":500,"message":"unavailable"}',b'{"success":false,"data":"unavailable"}',b'{"status":"failed","message":"unavailable"}',b'{"meta":{"count":0},"data":[]}',b'{"status":"ok","message":"hello","success":true}',b'{}',b'[]',b'"not source"']:
   with self.subTest(body=b),self.assertRaises(PublicWebSourceError):self.render(b)
 def test_depth_byte_node_and_malformed_unicode_limits_fail_before_use(self):
  cases=[b'['*(MAX_JSON_DEPTH+1)+b'0'+b']'*(MAX_JSON_DEPTH+1),b'{"data":"'+b'a'*MAX_JSON_BYTES+b'"}',b'{"data":['+b'0,'*MAX_JSON_NODES+b'0]}',b'{"v":"\\ud800"}',b'{"v":1} trailing',b'{"v":1,}']
  for b in cases:
   with self.subTest(prefix=b[:30]),self.assertRaises(PublicWebSourceError):self.render(b)
  self.assertIn('[number]: 0',self.render(b'['*MAX_JSON_DEPTH+b'0'+b']'*MAX_JSON_DEPTH)['text'])
 def test_json_gzip_integrity_and_declared_truncation_remain(self):
  raw=gzip.compress(b'{"data":{"amount":1.2300,"text":"a long string"}}',mtime=0);r=self.render(raw);self.assertEqual(r['renderer'],'gzip:0.1|json-structured-paths:0.1');self.assertIn('1.2300',r['text'])
  with self.assertRaises(PublicWebSourceError):self.render(raw[:-4])
  with self.assertRaises(PublicWebSourceError):self.render(raw,max_decompressed_bytes=8)
  r=self.render(raw,max_source_chars=10);self.assertTrue(r['truncated']);self.assertEqual(len(r['text']),10)
 def test_complete_declared_json_html_keeps_explicit_mismatch_and_original_type(self):
  raw=b'<!DOCTYPE html><html><head><title>Original title</title></head><body><p>original 1.2300</p><script>hidden</script></body></html>'
  r=self.render(raw,'application/vnd.api+json');self.assertEqual(r['media_type'],'application/vnd.api+json');self.assertEqual(r['renderer'],'json-declared-html-document:0.1|html-visible-blocks:0.1');self.assertEqual(r['text'],'Original title\n\noriginal 1.2300');self.assertNotIn('hidden',r['text'])
  self.assertEqual(self.render(gzip.compress(raw,mtime=0),'application/json')['renderer'],'gzip:0.1|json-declared-html-document:0.1|html-visible-blocks:0.1')
 def test_fragments_mixed_payload_and_script_fake_document_markers_refuse(self):
  for b in [b'<p>article</p>',b'<html>fragment</html>',b'<!DOCTYPE html><p>fragment</p>',b'<!DOCTYPE html><html>missing closing root',b'<!DOCTYPE html><html>x</html>{"data":1}',b'<script>"<!DOCTYPE html><html>x</html>"</script>',b'<!DOCTYPE html><html><p>not complete</p><script>fake </html>',b'<!DOCTYPE html><html><style>fake </html>',b'{"content":"<!DOCTYPE html><html>x</html>"} trailing']:
   with self.subTest(body=b),self.assertRaises(PublicWebSourceError):self.render(b)
  r=self.render(b'{"content":"<!DOCTYPE html><html>x</html>"}');self.assertEqual(r['renderer'],'json-structured-paths:0.1')
 def test_old_inline_xbrl_renderings_stay_available_under_the_same_hashes(self):
  raw=b'<html><body><ix:header>hidden context</ix:header><p>Filed amount 123</p></body></html>';cs=render_public_web_text_candidates(raw,raw_media_type='text/html');self.assertEqual([c['renderer'] for c in cs],['html-ixbrl-filing:0.2','html-visible-blocks:0.1']);self.assertNotIn('hidden context',cs[0]['text']);self.assertIn('hidden context',cs[1]['text'])
 def test_verified_gzip_json_keeps_raw_original_bytes_manifest_and_receipt(self):
  raw=gzip.compress(b'{"article":{"company":"Accenture","amount":1.2300}}',mtime=0)
  class JsonResponse(_Response):
   def getheaders(self):return [('content-type','application/vnd.api+json'),('content-length',str(len(self._body)))]
  transport=PublicHttpTransport(resolver=lambda *_:('93.184.216.34',),exchange=lambda *_:JsonResponse(raw))
  with tempfile.TemporaryDirectory() as tmp:
   h=FetchHarness(Path(tmp),transport=transport)
   try:
    receipt=h.discover();f=h.fetch.fetch(h.fetch.build_request(h.authority(receipt,URL_A)));manifest=h.fetch.manifest(f);held,r=verified_public_web_source(h.core,h.spool,manifest,h.fetch.receipts);self.assertEqual(held,manifest);self.assertEqual(held['body_sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(h.spool.read_object(held['raw_response_hash']),raw);self.assertEqual(r['renderer'],'gzip:0.1|json-structured-paths:0.1');self.assertIn('1.2300',r['text'])
   finally:h.close()

if __name__=='__main__':unittest.main()
