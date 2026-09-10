from __future__ import annotations
import hashlib, json, sqlite3, tempfile, unittest
from pathlib import Path
from dalton_core.research_html_export import (
    render_research_html,
    export_research_html,
    ResearchHtmlExportError,
    _typed_claims,
)
from dalton_core.company_dossier import CompanyDossierAuthority
from dalton_core.cockpit_research_library import research_library
from tests.test_research_task import ResearchTaskFixture
from tests.test_claim_index_entries import LedgerFixture
from tests import test_company_dossier as dossier_fixtures
from tests import test_investment_memo_decision_real as memo_fixtures


class HtmlRenderTests(unittest.TestCase):
    def fixture(self):
        mission = {
            "id": "mission-version:1",
            "content_hash": "a" * 64,
            "title": "ACN Research",
        }
        lib = {
            "company_ref": "company:acn",
            "products": [
                {
                    "kind": "investment_memo",
                    "label": "Investment Memo",
                    "status": "available",
                    "mission_binding": "current",
                    "version_ref": "memo:1",
                    "content_hash": "b" * 64,
                    "approval": {"status": "pending_human_decision"},
                    "sections": [
                        {
                            "title": "Conclusion <script>",
                            "body": "Buy? <img src=x onerror=alert(1)>",
                            "sources": ["claim:1"],
                            "gaps": ["unknown"],
                            "numbers": [
                                {
                                    "period": "FY25",
                                    "text": "Revenue $10B",
                                    "claim_version_ref": "claim:1",
                                },
                                {
                                    "period": "FY26E",
                                    "text": "Revenue $12B",
                                    "claim_version_ref": "claim:2",
                                },
                            ],
                        }
                    ],
                },
                {
                    "kind": "debate_map",
                    "label": "Debate",
                    "status": "missing",
                    "sections": [],
                    "reason": "not published",
                },
            ],
        }
        return mission, lib

    def test_safe_deterministic_report_has_toc_approval_chart_and_unknown(self):
        mission, lib = self.fixture()
        claims = {
            "claim:1": {
                "id": "claim:1",
                "subject_ref": "company:acn",
                "value": "10",
                "unit": "usd",
                "scale": "billion",
                "currency": "USD",
                "metric_or_aspect": "revenue",
                "period": "FY2025",
                "basis": "reported",
            },
            "claim:2": {
                "id": "claim:2",
                "subject_ref": "company:acn",
                "value": "12",
                "unit": "usd",
                "scale": "billion",
                "currency": "USD",
                "metric_or_aspect": "revenue",
                "period": "FY2026",
                "basis": "reported",
            },
        }
        a = render_research_html(lib, mission=mission, claims=claims)
        b = render_research_html(lib, mission=mission, claims=claims)
        self.assertEqual(a, b)
        self.assertIn("Contents", a)
        self.assertIn("pending_human_decision", a)
        self.assertIn("<svg", a)
        self.assertIn("claim:1", a)
        self.assertIn("Unknown / unavailable", a)
        self.assertNotIn("<script>", a)
        self.assertNotIn("<img src=x", a)
        self.assertIn("&lt;script&gt;", a)
        self.assertNotIn('src="http', a)

    def test_incompatible_units_do_not_make_a_chart(self):
        mission, lib = self.fixture()
        nums = lib["products"][0]["sections"][0]["numbers"]
        nums[1]["text"] = "Margin 12%"
        claims = {
            "claim:1": {
                "id": "claim:1",
                "subject_ref": "company:acn",
                "value": "10",
                "unit": "usd",
                "scale": "billion",
                "currency": "USD",
                "metric_or_aspect": "revenue",
                "period": "FY2025",
                "basis": "reported",
            },
            "claim:2": {
                "value": "12",
                "unit": "percent",
                "scale": "one",
                "currency": None,
                "metric_or_aspect": "margin",
                "period": "FY26E",
            },
        }
        page = render_research_html(lib, mission=mission, claims=claims)
        self.assertIn("Chart unavailable", page)
        self.assertNotIn("<svg", page)


class RealReadonlyExportTests(ResearchTaskFixture):
    def test_real_authority_partial_export_is_stable_and_does_not_write_core(self):
        before = self.state_dir.joinpath("core.sqlite").stat().st_size
        one = self.state_dir / "one.html"
        m1 = export_research_html(
            self.state_dir / "core.sqlite", "company:sec-cik:0001467373", one
        )
        html_bytes = one.read_bytes()
        manifest_bytes = (self.state_dir / "one.html.manifest.json").read_bytes()
        m2 = export_research_html(
            self.state_dir / "core.sqlite", "company:sec-cik:0001467373", one
        )
        self.assertEqual(html_bytes, one.read_bytes())
        self.assertEqual(
            manifest_bytes, (self.state_dir / "one.html.manifest.json").read_bytes()
        )
        self.assertEqual(m1["html_sha256"], m2["html_sha256"])
        self.assertEqual(m1["mission_version_hash"], self.mission["content_hash"])
        self.assertTrue(all(p["status"] == "missing" for p in m1["product_versions"]))
        self.assertEqual(before, self.state_dir.joinpath("core.sqlite").stat().st_size)

    def test_sqlite_uri_escapes_question_and_hash_in_real_path(self):
        unusual = self.state_dir / "core?#copy.sqlite"
        target = sqlite3.connect(unusual)
        try:
            self.store.connection.backup(target)
        finally:
            target.close()
        result = export_research_html(
            unusual, "company:sec-cik:0001467373", self.state_dir / "escaped.html"
        )
        self.assertEqual(result["mission_version_hash"], self.mission["content_hash"])

    def test_two_real_claim_rows_resolve_as_one_typed_series(self):
        fixture = LedgerFixture()
        self.addCleanup(fixture.close)
        refs = [
            fixture.add_claim(
                f"claim:html:revenue:{period}",
                value=value,
                unit="usd",
                metric="revenue",
                period=period,
                statement=f"Revenue USD {value}",
            )["claim_version_id"]
            for value, period in ((10, "2025"), (12, "2026"))
        ]
        library = {
            "products": [
                {
                    "sections": [
                        {"numbers": [{"claim_version_ref": ref} for ref in refs]}
                    ]
                }
            ]
        }
        claims = _typed_claims(fixture.store.connection, library)
        self.assertEqual(set(claims), set(refs))
        page = render_research_html(
            {
                "company_ref": "company:sec-cik:0001467373",
                "products": [
                    {
                        "kind": "memo",
                        "label": "Memo",
                        "status": "available",
                        "sections": [
                            {
                                "title": "Revenue",
                                "body": "Series",
                                "numbers": [
                                    {"text": "first", "claim_version_ref": refs[0]},
                                    {"text": "second", "claim_version_ref": refs[1]},
                                ],
                            }
                        ],
                    }
                ],
            },
            mission=self.mission,
            claims=claims,
        )
        self.assertIn("Typed Claim series: revenue", page)


class PublishedAuthorityExportTests(unittest.TestCase):
    setUp = memo_fixtures.RealInvestmentMemoDecisionTests.setUp

    def _file_copy(self, name):
        path = self.chain.root / name
        target = sqlite3.connect(path)
        try:
            self.store.connection.backup(target)
        finally:
            target.close()
        return path

    def test_real_approved_memo_exports_exact_human_decision(self):
        memo_fixtures.RealInvestmentMemoDecisionTests.test_real_store_scheduler_router_publish_and_approve(
            self
        )
        library = research_library(self.store.connection, self.mission, self.company)
        memo = next(
            product
            for product in library["products"]
            if product["kind"] == "investment_memo"
        )
        self.assertEqual(memo["approval"]["status"], "approved")

        output = self.chain.root / "approved-memo.html"
        manifest = export_research_html(
            self._file_copy("memo-core.sqlite"),
            self.company,
            output,
            mission_ref=self.mission["mission_ref"],
        )
        page = output.read_text(encoding="utf-8")
        for field in ("decision_record_ref", "actor_ref", "decided_at"):
            self.assertIn(memo["approval"][field], page)
        exported = next(
            product
            for product in manifest["product_versions"]
            if product["kind"] == "investment_memo"
        )
        self.assertEqual(exported["version_ref"], memo["version_ref"])
        self.assertEqual(exported["content_hash"], memo["content_hash"])
        self.assertEqual(exported["approval"], "approved")

    def test_real_published_dossier_renders_structured_sources(self):
        candidate = dossier_fixtures.body(
            company_ref=self.company,
            drafted_sections={
                "business_model": dossier_fixtures.drafted(
                    "business_model", "claim-version:structured-source"
                )
            },
        )
        candidate["bindings"]["mission_version_ref"] = self.mission["id"]
        published = CompanyDossierAuthority(self.store).publish(candidate)

        output = self.chain.root / "dossier.html"
        manifest = export_research_html(
            self._file_copy("dossier-core.sqlite"),
            self.company,
            output,
            mission_ref=self.mission["mission_ref"],
        )
        page = output.read_text(encoding="utf-8")
        self.assertIn("claim-version:structured-source", page)
        self.assertNotIn("{&#x27;kind&#x27;", page)
        exported = next(
            product
            for product in manifest["product_versions"]
            if product["kind"] == "dossier"
        )
        self.assertEqual(exported["version_ref"], published["id"])
        self.assertEqual(exported["content_hash"], published["content_hash"])

    def test_asset_hash_and_shape_fail_closed(self):
        asset = self.chain.root / "figure.png"
        asset.write_bytes(b"not-a-real-png-but-local-bytes")
        manifest = self.chain.root / "assets.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "assets": [
                        {
                            "path": str(asset),
                            "sha256": "0" * 64,
                            "media_type": "image/png",
                            "caption": "x",
                            "source_refs": ["claim:1"],
                        }
                    ],
                }
            )
        )
        with self.assertRaisesRegex(ResearchHtmlExportError, "hash mismatch"):
            export_research_html(
                self._file_copy("asset-core.sqlite"),
                self.company,
                self.chain.root / "x.html",
                mission_ref=self.mission["mission_ref"],
                asset_manifest=manifest,
            )


if __name__ == "__main__":
    unittest.main()
