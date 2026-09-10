from __future__ import annotations
import hashlib, json, sqlite3, tempfile, unittest
from pathlib import Path
from dalton_core.research_html_export import (render_research_html, export_research_html,
    ResearchHtmlExportError, _typed_claims)
from dalton_core.store import content_hash
from tests.test_research_task import ResearchTaskFixture

class HtmlRenderTests(unittest.TestCase):
    def fixture(self):
        mission={"id":"mission-version:1","content_hash":"a"*64,"title":"ACN Research"}
        lib={"company_ref":"company:acn","products":[
          {"kind":"investment_memo","label":"Investment Memo","status":"available","mission_binding":"current","version_ref":"memo:1","content_hash":"b"*64,"approval":{"status":"pending_human_decision"},"sections":[{"title":"Conclusion <script>","body":"Buy? <img src=x onerror=alert(1)>","sources":["claim:1"],"gaps":["unknown"],"numbers":[{"period":"FY25","text":"Revenue $10B","claim_version_ref":"claim:1"},{"period":"FY26E","text":"Revenue $12B","claim_version_ref":"claim:2"}]}]},
          {"kind":"debate_map","label":"Debate","status":"missing","sections":[],"reason":"not published"}]}
        return mission,lib
    def test_safe_deterministic_report_has_toc_approval_chart_and_unknown(self):
        mission,lib=self.fixture()
        claims={"claim:1":{"id":"claim:1","subject_ref":"company:acn","value":"10","unit":"usd","scale":"billion","currency":"USD","metric_or_aspect":"revenue","period":"FY2025","basis":"reported"},"claim:2":{"id":"claim:2","subject_ref":"company:acn","value":"12","unit":"usd","scale":"billion","currency":"USD","metric_or_aspect":"revenue","period":"FY2026","basis":"reported"}}
        a=render_research_html(lib,mission=mission,claims=claims); b=render_research_html(lib,mission=mission,claims=claims)
        self.assertEqual(a,b); self.assertIn('Contents',a); self.assertIn('pending_human_decision',a)
        self.assertIn('<svg',a); self.assertIn('claim:1',a); self.assertIn('Unknown / unavailable',a)
        self.assertNotIn('<script>',a); self.assertNotIn('<img src=x',a)
        self.assertIn('&lt;script&gt;',a); self.assertNotIn('src="http',a)
    def test_incompatible_units_do_not_make_a_chart(self):
        mission,lib=self.fixture(); nums=lib['products'][0]['sections'][0]['numbers']; nums[1]['text']='Margin 12%'
        claims={"claim:1":{"id":"claim:1","subject_ref":"company:acn","value":"10","unit":"usd","scale":"billion","currency":"USD","metric_or_aspect":"revenue","period":"FY2025","basis":"reported"},"claim:2":{"value":"12","unit":"percent","scale":"one","currency":None,"metric_or_aspect":"margin","period":"FY26E"}}
        page=render_research_html(lib,mission=mission,claims=claims)
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
    def test_sqlite_uri_escapes_question_and_hash_in_real_path(self):
        unusual = self.state_dir / "core?#copy.sqlite"
        target = sqlite3.connect(unusual)
        try:
            self.store.connection.backup(target)
        finally:
            target.close()
        result = export_research_html(
            unusual, "company:sec-cik:0001467373", self.state_dir / "escaped.html")
        self.assertEqual(result["mission_version_hash"], self.mission["content_hash"])

    def test_two_real_claim_rows_resolve_as_one_typed_series(self):
        refs=[]
        for index, value in enumerate(("10", "12"), 1):
            claim={"schema_version":"0.2","id":f"claim-version:html:{index}",
                "claim_ref":f"claim:html:revenue:{index}","version":index,"subject_ref":"company:sec-cik:0001467373",
                "metric_or_aspect":"revenue","period":f"FY202{4+index}","basis":"reported",
                "normalized_statement":f"Revenue USD {value} billion","claim_kind":"quantitative",
                "value":value,"unit":"usd","currency":"USD","scale":"billion",
                "producer_execution_refs":[],"semantic_review_ref":None,"semantic_review_hash":None,
                "candidate_origin_ref":None,"candidate_origin_hash":None,"actor_ref":"system:test",
                "prior_version_ref":None,"created_at":f"2026-09-0{index}T00:00:00+00:00"}
            claim["content_hash"]=content_hash(claim)
            with self.store._transaction() as cur:
                cur.execute("INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,content_hash,created_at) VALUES(?,?,?,?,?,?)",(claim["id"],claim["claim_ref"],index,json.dumps(claim),claim["content_hash"],claim["created_at"]))
            refs.append(claim["id"])
        library={"products":[{"sections":[{"numbers":[{"claim_version_ref":ref} for ref in refs]}]}]}
        claims=_typed_claims(self.store.connection,library)
        self.assertEqual(set(claims),set(refs))
        page=render_research_html({"company_ref":"company:sec-cik:0001467373","products":[{"kind":"memo","label":"Memo","status":"available","sections":[{"title":"Revenue","body":"Series","numbers":[{"text":"first","claim_version_ref":refs[0]},{"text":"second","claim_version_ref":refs[1]}]}]}]},mission=self.mission,claims=claims)
        self.assertIn("Typed Claim series: revenue",page)

    def test_asset_hash_and_shape_fail_closed(self):
        asset=self.state_dir/'figure.png'; asset.write_bytes(b'not-a-real-png-but-local-bytes')
        manifest=self.state_dir/'assets.json'; manifest.write_text(json.dumps({'schema_version':'0.1','assets':[{'path':str(asset),'sha256':'0'*64,'media_type':'image/png','caption':'x','source_refs':['claim:1']}]}))
        with self.assertRaisesRegex(ResearchHtmlExportError,'hash mismatch'):
            export_research_html(self.state_dir/'core.sqlite','company:sec-cik:0001467373',self.state_dir/'x.html',asset_manifest=manifest)

if __name__=='__main__': unittest.main()
