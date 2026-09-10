from __future__ import annotations
import hashlib, json, tempfile, unittest
from pathlib import Path
from dalton_core.research_html_export import render_research_html, export_research_html, ResearchHtmlExportError
from tests.test_research_task import ResearchTaskFixture

class HtmlRenderTests(unittest.TestCase):
    def fixture(self):
        mission={"id":"mission-version:1","content_hash":"a"*64,"title":"ACN Research"}
        lib={"company_ref":"company:acn","products":[
          {"kind":"investment_memo","label":"Investment Memo","status":"available","mission_binding":"current","version_ref":"memo:1","content_hash":"b"*64,"approval":{"status":"pending_human_decision"},"sections":[{"title":"Conclusion <script>","body":"Buy? <img src=x onerror=alert(1)>","sources":["claim:1"],"gaps":["unknown"],"numbers":[{"period":"FY25","text":"Revenue $10B","claim_version_ref":"claim:1"},{"period":"FY26E","text":"Revenue $12B","claim_version_ref":"claim:2"}]}]},
          {"kind":"debate_map","label":"Debate","status":"missing","sections":[],"reason":"not published"}]}
        return mission,lib
    def test_safe_deterministic_report_has_toc_approval_chart_and_unknown(self):
        mission,lib=self.fixture(); a=render_research_html(lib,mission=mission); b=render_research_html(lib,mission=mission)
        self.assertEqual(a,b); self.assertIn('Contents',a); self.assertIn('pending_human_decision',a)
        self.assertIn('<svg',a); self.assertIn('claim:1',a); self.assertIn('Unknown / unavailable',a)
        self.assertNotIn('<script>',a); self.assertNotIn('<img src=x',a)
        self.assertIn('&lt;script&gt;',a); self.assertNotIn('src="http',a)
    def test_incompatible_units_do_not_make_a_chart(self):
        mission,lib=self.fixture(); nums=lib['products'][0]['sections'][0]['numbers']; nums[1]['text']='Margin 12%'
        page=render_research_html(lib,mission=mission)
        self.assertIn('Chart unavailable',page); self.assertNotIn('<svg',page)

class RealReadonlyExportTests(ResearchTaskFixture):
    def test_real_authority_partial_export_is_stable_and_does_not_write_core(self):
        before=self.state_dir.joinpath('core.sqlite').stat().st_size
        one=self.state_dir/'one.html'
        m1=export_research_html(self.state_dir/'core.sqlite','company:sec-cik:0001467373',one)
        html_bytes=one.read_bytes(); manifest_bytes=(self.state_dir/'one.html.manifest.json').read_bytes()
        m2=export_research_html(self.state_dir/'core.sqlite','company:sec-cik:0001467373',one)
        self.assertEqual(html_bytes,one.read_bytes())
        self.assertEqual(manifest_bytes,(self.state_dir/'one.html.manifest.json').read_bytes())
        self.assertEqual(m1['html_sha256'],m2['html_sha256'])
        self.assertEqual(m1['mission_version_hash'],self.mission['content_hash'])
        self.assertTrue(all(p['status']=='missing' for p in m1['product_versions']))
        self.assertEqual(before,self.state_dir.joinpath('core.sqlite').stat().st_size)
    def test_asset_hash_and_shape_fail_closed(self):
        asset=self.state_dir/'figure.png'; asset.write_bytes(b'not-a-real-png-but-local-bytes')
        manifest=self.state_dir/'assets.json'; manifest.write_text(json.dumps({'schema_version':'0.1','assets':[{'path':str(asset),'sha256':'0'*64,'media_type':'image/png','caption':'x','source_refs':['claim:1']}]}))
        with self.assertRaisesRegex(ResearchHtmlExportError,'hash mismatch'):
            export_research_html(self.state_dir/'core.sqlite','company:sec-cik:0001467373',self.state_dir/'x.html',asset_manifest=manifest)

if __name__=='__main__': unittest.main()
