"""Batch D for the company dossier: ordering bugs, material pack, lint, substance.

Synthetic company only (``tests/synthetic_company.py``); no live state.
"""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest

from dalton_core.product_contract import normalise_pack, tag_entry
from unittest import mock

from dalton_core import company_dossier_cli as cli
from dalton_core.company_dossier import (
    batch_d_findings,
    classify_gap,
    failed_substance_sections,
    gap_classes,
)
from dalton_core.company_dossier_draft import MAX_ROW_CHARS, material_rows, pack_material_rows
from tests.synthetic_company import material_pack
from tests.test_company_dossier import body, drafted


class ClaimOrderTests(unittest.TestCase):
    def test_newest_claims_win_inside_an_importance_tier(self):
        rows = [
            {"claim_version_ref": f"claim-version:{index}", "normalized_statement": f"s{index}",
             "importance": importance, "as_of": as_of}
            for index, (importance, as_of) in enumerate([
                ("filing", "2024-01-31"), ("filing", "2026-05-31"),
                ("filing", "2025-05-31"), ("other", "2026-09-30"),
            ])
        ]
        with mock.patch("dalton_core.company_research_view.query_company_research",
                        return_value=rows):
            material = cli.claim_material(object(), "company:ticker:synw", "business_model",
                                          limit=3)
        self.assertEqual([row["ref"] for row in material],
                         ["claim-version:1", "claim-version:2", "claim-version:0"])


class ForecastCellOrderTests(unittest.TestCase):
    def rows(self):
        out = []
        for end, kind in [("2025-12-31", "actual"), ("2026-03-31", "actual"),
                          ("2026-06-30", "actual"), ("2026-09-30", "estimate"),
                          ("2026-12-31", "estimate"), ("2027-12-31", "estimate"),
                          ("2028-06-30", "estimate")]:
            out.append({"ref": f"forecast-cell:{end}", "period_end": end,
                        "importance": "filing" if kind == "actual" else "other"})
        return out

    def test_the_quota_buys_the_quarters_nearest_the_present(self):
        chosen = cli.nearest_cells_first(self.rows())[:4]
        ends = [row["period_end"] for row in chosen]
        self.assertEqual(ends[0], "2026-06-30")  # the latest actual
        self.assertIn("2026-09-30", ends)  # next quarter
        self.assertNotIn("2028-06-30", ends)  # the farthest forecast loses
        self.assertNotIn("2027-12-31", ends)

    def test_no_actual_anchors_on_the_earliest_cell(self):
        rows = [row for row in self.rows() if row["importance"] == "other"]
        self.assertEqual(cli.nearest_cells_first(rows)[0]["period_end"], "2026-09-30")


class PackRowsTests(unittest.TestCase):
    """Batch C's MaterialPack (synthetic content, real object) as dossier rows."""

    def setUp(self):
        self.pack = normalise_pack(material_pack())

    def ref(self, tag):
        return tag_entry(self.pack, tag)["ref"]

    def test_pack_tags_become_citable_figure_rows(self):
        rows = pack_material_rows(material_pack(), limit=40)
        refs = {row["ref"] for row in rows}
        self.assertIn(self.ref("P1"), refs)
        self.assertIn(self.ref("N4"), refs)
        self.assertIn(self.ref("N20"), refs)           # our model, an N tag into /our_model
        self.assertNotIn(self.ref("C1"), refs)         # Claims arrive via the index
        self.assertNotIn(self.ref("O1"), refs)         # our earlier view is background
        self.assertTrue(all(row["kind"] == "figure" for row in rows))
        self.assertTrue(all(len(row["text"]) <= MAX_ROW_CHARS for row in rows))
        ours = next(row for row in rows if row["ref"] == self.ref("N20"))
        self.assertEqual(ours["importance"], "ours")
        # They go through the ordinary material tagger as N rows.
        tagged = material_rows([], rows)
        self.assertTrue(all(row["tag"].startswith("N") for row in tagged))

    def test_a_figure_verbatim_from_a_cited_pack_row_is_sourced(self):
        from dalton_core.company_dossier import output_rubric_findings
        from tests.test_company_dossier import constitution, policy

        row = next(item for item in pack_material_rows(self.pack, limit=40)
                   if item["ref"] == self.ref("N4"))
        section = drafted("business_model", row["ref"], "一致预期 FY2027 EPS 4.50 美元。",
                          source_text=row["text"])
        section["sources"][0]["kind"] = "figure"
        record = body(drafted_sections={"business_model": section})
        self.assertEqual(output_rubric_findings(record, constitution=constitution(),
                                                policy=policy()), [])
        section["slots"][0]["sentences"][0]["text"] = "一致预期 FY2027 EPS 4.80 美元。"
        record = body(drafted_sections={"business_model": section})
        codes = {item["code"] for item in output_rubric_findings(
            record, constitution=constitution(), policy=policy())}
        self.assertEqual(codes, {"number_without_source"})

    def test_pack_refs_resolve_and_other_shapes_still_do_not(self):
        ref = self.ref("P1")
        section = drafted("business_model", ref, source_text="x")
        section["sources"][0]["kind"] = "figure"
        record = body(drafted_sections={"business_model": section})
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE claim_versions(claim_version_id TEXT)")
        with mock.patch("dalton_core.claim_retirement.retired_claim_version_refs",
                        return_value=set()):
            self.assertEqual(cli.unresolved_refs(connection, record, forecast_cells=set(),
                                                 pack_refs={ref}), [])
            missing = cli.unresolved_refs(connection, record, forecast_cells=set())
        self.assertEqual([item["ref"] for item in missing], [ref])

    def test_without_the_sibling_module_the_dossier_says_so(self):
        summary: dict = {}
        with mock.patch.dict(sys.modules, {"dalton_core.material_pack": None}):
            rows = cli.dossier_pack_rows("/nonexistent", "company:ticker:synw", summary)
        self.assertEqual(rows, [])
        self.assertEqual(summary["material_pack"]["status"], "unavailable")
        self.assertIn("material_pack", summary["material_pack"]["reason"])

    def test_with_a_pack_the_rows_and_its_hash_are_recorded(self):
        pack = material_pack()
        products = []
        module = types.ModuleType("dalton_core.material_pack")
        module.build = lambda state, ref, product, *args, **kwargs: products.append(product) or pack
        summary: dict = {}
        with mock.patch.dict(sys.modules, {"dalton_core.material_pack": module}):
            rows = cli.dossier_pack_rows("/nonexistent", "company:ticker:synw", summary)
        self.assertTrue(rows)
        self.assertEqual(products, ["dossier"])         # batch C's recipe name
        self.assertLessEqual(len(rows), cli.MAX_PACK_ROWS)
        self.assertEqual(summary["material_pack"]["status"], "used")
        self.assertEqual(summary["material_pack"]["content_hash"], pack.content_hash)


class LintAndSubstanceTests(unittest.TestCase):
    def test_a_reasoning_lint_hit_is_a_repairable_finding_on_a_drafted_part(self):
        section = drafted("competitive_position", "claim-version:a", "公司正在赢得份额。")
        record = body(drafted_sections={"competitive_position": section})
        findings = batch_d_findings(record, units=["competitive_position"])
        self.assertEqual([(item["code"], item["rule"]) for item in findings],
                         [("reasoning_lint", "management_share_gain_as_fact")])
        self.assertEqual(findings[0]["section"], "competitive_position")
        # Carried-forward parts are not re-litigated.
        self.assertEqual(batch_d_findings(record, units=["business_model"]), [])
        # Attributed to management, it is a company claim and passes.
        section["slots"][0]["sentences"][0]["text"] = "管理层称公司正在赢得份额。"
        self.assertEqual(batch_d_findings(body(drafted_sections={
            "competitive_position": section}), units=["competitive_position"]), [])

    def test_a_placeholder_is_a_finding(self):
        section = drafted("business_model", "claim-version:a", "收入〔数字见研判记录〕。")
        findings = batch_d_findings(body(drafted_sections={"business_model": section}),
                                    units=["business_model"])
        self.assertIn("indirect_placeholder", {item["code"] for item in findings})

    def test_empty_parts_fail_on_substance(self):
        record = body(drafted_sections={"business_model": drafted(
            "business_model", "claim-version:a",
            "公司收入主要来自多年期服务合同，服务占比上升带动利润率改善，硬件只起带动作用，"
            "续约率决定收入可见度。")})
        failed = {item["title"] for item in failed_substance_sections(record)}
        self.assertNotIn("business_model", failed)
        self.assertIn("segments_and_mix", failed)  # unavailable: empty


class GapClassTests(unittest.TestCase):
    def test_gaps_are_classified_and_unbound_not_acquired_are_listed(self):
        self.assertEqual(classify_gap("公司未披露：按客户的续约数据"), "company_not_disclosed")
        self.assertEqual(classify_gap("未取得：分部利润率历史"), "not_acquired")
        self.assertEqual(classify_gap("缺少数据"), "unclassified")
        section = drafted("business_model", "claim-version:a")
        section["gaps"] = ["公司未披露：按客户的续约数据", "未取得：分部利润率历史",
                           "未取得（material-request:x1）：分部收入"]
        out = gap_classes(body(drafted_sections={"business_model": section}),
                          units=["business_model"])
        self.assertEqual(out["company_not_disclosed"], 1)
        self.assertEqual(out["not_acquired"], 2)
        self.assertEqual([item["need"] for item in out["requests_needed"]],
                         ["未取得：分部利润率历史"])


if __name__ == "__main__":
    unittest.main()
