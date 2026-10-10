"""Batch H: rubric H for the STRL-contract Initial Screen, check by check,
plus the batch C integration the contract relies on (real modules, temp state)."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from dalton_core.initial_screen import (
    STRL_TEMPLATE_REF,
    parse_document_output,
    strl_contract_record,
    validate_worksheet,
    verify_document,
)
from dalton_core.product_contract import normalise_pack
from dalton_core.research_quality_rubrics import INITIAL_SCREEN_STRL, rubric
from dalton_core.research_quality_score import (
    artefact_from_deliverable,
    run_deterministic,
    strl_artefact,
)
from tests.synthetic_company import COMPANY, material_pack, strl_document, strl_worksheet


def _art(mutate=None, *, worksheet=None):
    pack = normalise_pack(material_pack())
    doc = strl_document()
    if mutate is not None:
        mutate(doc["sections"])
    sheet, _ = validate_worksheet(worksheet or strl_worksheet(), pack)
    sections, _ = parse_document_output(doc, pack=pack)
    verdict = verify_document(sections, worksheet=sheet, pack=pack)
    contract = strl_contract_record(pack=pack, worksheet=sheet, verdict=verdict)
    return strl_artefact(ref="draft:test", hash="0" * 64, sections=sections, subject_ref=COMPANY,
                         contract=contract)


def _check(art, name):
    result = run_deterministic(art, INITIAL_SCREEN_STRL)
    return next(item for item in result["checks"] if item["check"] == name)


class RubricShapeTests(unittest.TestCase):
    def test_it_is_registered_under_its_own_ref_and_short_name(self):
        self.assertIs(rubric("initial_screen_strl"), INITIAL_SCREEN_STRL)
        self.assertEqual(INITIAL_SCREEN_STRL.rubric_ref, "rubric:initial-screen-strl")
        for check in ("stance_and_watch_price", "sourced_quantitative_statements", "ours_vs_consensus",
                      "valuation_six_elements", "source_kind_diversity",
                      "latest_transcript_and_release_cited", "material_missing_bound", "fact_repetition",
                      "reasoning_lint", "issuer_attribution", "actual_vs_estimate_labelling",
                      "one_off_normalisation", "cash_bridge_consistency", "cross_product_consistency",
                      "required_sections_present"):
            self.assertIn(check, INITIAL_SCREEN_STRL.deterministic_checks)

    def test_the_strl_draft_passes_and_the_score_binds_its_standards(self):
        result = run_deterministic(_art(), INITIAL_SCREEN_STRL)
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(set(result["bindings"]),
                         {"lint", "product_contract", "substance", "artefact_contract", "artefact_lint"})
        self.assertEqual(result["bindings"]["artefact_contract"], "initial-screen-contract:strl:1")
        self.assertEqual(result["bindings"]["lint"], result["bindings"]["artefact_lint"])

    def test_a_published_strl_record_is_read_with_the_strl_sections(self):
        art = _art()
        record = {"id": "mission-deliverable-version:x", "content_hash": "1" * 64,
                  "template_ref": STRL_TEMPLATE_REF, "subject_ref": COMPANY,
                  "sections": art["sections"], "contract": art["contract"]}
        read = artefact_from_deliverable(record)
        self.assertEqual(read["expected_sections"], art["expected_sections"])
        self.assertTrue(run_deterministic(read, INITIAL_SCREEN_STRL)["passed"])


class CheckTests(unittest.TestCase):
    def test_stance_must_be_in_the_closed_vocabulary(self):
        art = _art(lambda s: s[0].__setitem__("body", s[0]["body"].replace("立场：看多。", "立场：谨慎乐观。")))
        self.assertEqual(_check(art, "stance_and_watch_price")["status"], "fail")

    def test_too_few_sourced_statements_in_a_core_section(self):
        def thin(sections):
            sections[1]["body"] = "服务是利润中心，第二季度服务占收入 60%。硬件主要作用是带出后续服务合同。"
        check = _check(_art(thin), "sourced_quantitative_statements")
        self.assertEqual(check["status"], "fail")
        self.assertIn("core_section_thin", {item["code"] for item in check["findings"]})

    def test_ours_vs_consensus_needs_a_percent_and_an_expression(self):
        def no_gap(sections):
            sections[3]["body"] = "我们预计 FY2027 EPS 5.00 美元，一致预期 4.50 美元。"
            sections[3]["numbers"] = [n for n in sections[3]["numbers"] if not n.get("derivation")]
        check = _check(_art(no_gap), "ours_vs_consensus")
        self.assertEqual({item["code"] for item in check["findings"]},
                         {"no_ours_vs_consensus_percent", "difference_without_expression"})

    def test_valuation_six_elements(self):
        def bare(sections):
            sections[4]["body"] = "观察价 85.0 美元（我们的估计）。"
        check = _check(_art(bare), "valuation_six_elements")
        self.assertEqual({item["code"] for item in check["findings"]},
                         {"missing_multiple", "missing_eps", "missing_current_price",
                          "missing_implied_return", "missing_multiple_rationale"})

    def test_an_unavailable_watch_price_needs_references(self):
        def unavailable(sections):
            sections[4]["body"] = "观察价：暂无法给出（模型未通过不变量检查）。等模型修复后再定。"
        check = _check(_art(unavailable), "valuation_six_elements")
        self.assertEqual([item["code"] for item in check["findings"]], ["unavailable_without_reference"])

    def test_four_source_kinds(self):
        art = _art()
        for section in art["sections"]:
            for item in section.get("source_refs") or ():
                item["source_kind"] = "transcript"
        self.assertEqual(_check(art, "source_kind_diversity")["status"], "fail")

    def test_the_latest_transcript_must_be_cited(self):
        def older_call(sections):
            for section in sections:
                section["sources"] = [tag for tag in section.get("sources") or () if tag != "P1"]
                section["numbers"] = [n for n in section.get("numbers") or () if n.get("tag") != "P1"]
            sections[1]["body"] = sections[1]["body"].split("\n\n")[1]
        check = _check(_art(older_call), "latest_transcript_and_release_cited")
        self.assertEqual([item["code"] for item in check["findings"]], ["latest_transcript_not_cited"])

    def test_a_fact_in_three_sections(self):
        def repeat(sections):
            sections[6]["body"] += "\n\n若我们的观察价 85.0 美元被证伪，需重估。"
            # Repeat the same verified fact, including its exact period/unit
            # and the cited operands that replay the watch-price derivation.
            sections[6]["numbers"] = copy.deepcopy(sections[4]["numbers"])
            sections[6]["sources"] = copy.deepcopy(sections[4]["sources"])
        art = _art(repeat)
        check = _check(art, "fact_repetition")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["count"], 1)
        self.assertEqual(check["findings"][0]["fact_identity"][1:4], ["观察价", "12M", "美元"])
        self.assertEqual(check["findings"][0]["sections"], ["估值与观察价", "结论与观点", "风险与证伪"])

    def test_an_unbound_equal_number_is_a_source_gap_not_a_proven_equal_fact(self):
        art = _art(lambda s: s[6].__setitem__("body", s[6]["body"] + "\n\n若观察价 85.0 美元被证伪，需重估。"))
        self.assertEqual(_check(art, "fact_repetition")["status"], "pass")
        self.assertEqual(_check(art, "numbers_without_refs")["status"], "fail")
        self.assertFalse(run_deterministic(art, INITIAL_SCREEN_STRL)["passed"])

    def test_issuer_attribution(self):
        art = _art()
        art["sections"][1]["source_refs"][0]["company_ref"] = "company:ticker:other"
        art["sections"][1]["source_refs"][0]["source_kind"] = "transcript"
        self.assertEqual(_check(art, "issuer_attribution")["status"], "fail")
        art["sections"][1]["source_refs"][0]["company_ref"] = COMPANY
        self.assertEqual(_check(art, "issuer_attribution")["status"], "pass")
        self.assertEqual(_check(_art(), "issuer_attribution")["status"], "skipped")

    def test_actual_vs_estimate_labelling(self):
        art = _art()
        art["sections"][5]["body"] += "\n\nFY2027 收入 5,700 百万美元。"
        estimate = next(n for n in art["sections"][3]["numbers"]
                        if n.get("estimate") and n.get("metric") == "收入" and n.get("period") == "FY2027")
        art["sections"][5]["numbers"].append(copy.deepcopy(estimate))
        self.assertEqual(_check(art, "actual_vs_estimate_labelling")["status"], "fail")

    def test_calendar_year_without_a_binding_does_not_prove_a_forecast(self):
        art = _art()
        art["sections"][5]["body"] += "\n\nFY2027 收入 5,700 百万美元。"
        self.assertEqual(_check(art, "actual_vs_estimate_labelling")["status"], "pass")
        self.assertEqual(_check(art, "numbers_without_refs")["status"], "fail")
        self.assertFalse(run_deterministic(art, INITIAL_SCREEN_STRL)["passed"])

    def test_one_off_normalisation(self):
        art = _art()
        art["sections"][1]["body"] += "\n\n上一季度有一次性重组费用。"
        self.assertEqual(_check(art, "one_off_normalisation")["status"], "fail")
        art["sections"][1]["body"] += "按调整后口径剔除后，利润率不变。"
        self.assertEqual(_check(art, "one_off_normalisation")["status"], "pass")

    def test_cash_bridge_is_skipped_without_cash_statements_and_checked_with_them(self):
        self.assertEqual(_check(_art(), "cash_bridge_consistency")["status"], "skipped")
        art = _art()
        art["sections"][1]["body"] += "\n\n自由现金流 900 百万美元。"
        self.assertEqual(_check(art, "cash_bridge_consistency")["status"], "fail")

    def test_cross_product_consistency_is_skipped_without_a_core_connection(self):
        # Batch an landed: the check consults product_consistency.conflicts_for
        # when a Core connection is in the context; the bare artifact here has
        # none, so it is skipped with the honest reason rather than a placeholder.
        check = _check(_art(), "cross_product_consistency")
        self.assertEqual(check["status"], "skipped")
        self.assertIn("没有 Core 连接", check["detail"])

    def test_material_missing_limit_and_binding(self):
        def three(sections):
            sections[5]["body"] += ("\n\n现有材料缺少期权持仓数据（MR-1）。现有材料缺少做空比例（MR-2）。"
                                    "现有材料缺少 ETF 持仓（MR-3）。")
        check = _check(_art(three), "material_missing_bound")
        self.assertIn("material_missing_over_limit", {item["code"] for item in check["findings"]})


class BatchCIntegrationTests(unittest.TestCase):
    """The real batch-C modules, against an empty temp state (no live data)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def test_a_request_opened_through_the_contract_is_a_material_request_ref(self):
        from dalton_core.product_contract import open_material_request

        ref = open_material_request(
            self.state, requester={"lane": "initial_screen", "section": "附录"}, subject=COMPANY,
            need={"kind": "data", "data_item": "services segment margin history"}, period="FY2023-FY2026")
        self.assertTrue(ref.startswith("material-request:"))
        again = open_material_request(
            self.state, requester={"lane": "initial_screen", "section": "附录"}, subject=COMPANY,
            need={"kind": "data", "data_item": "services segment margin history"}, period="FY2023-FY2026")
        self.assertEqual(ref, again)

    def test_the_gate_standard_opens_an_answer_request(self):
        from dalton_core.deep_insight_gate_standards import open_request

        ref, reason = open_request(self.state, company_ref=COMPANY, question_ref="q9",
                                   question="哪些高频指标能证伪我们的判断？", problems=["没有高于季度的指标"])
        self.assertIsNone(reason)
        self.assertTrue(ref.startswith("material-request:"))

    def test_sufficiency_on_an_empty_workspace_holds_without_raising(self):
        from dalton_core.product_contract import check_sufficiency

        check = check_sufficiency(self.state, COMPANY, "initial_screen", "2026-08-15",
                                  requester={"lane": "initial_screen"}, record=False)
        self.assertFalse(check["drafting_allowed"])

    def test_the_pack_record_goes_to_the_build_ledger(self):
        from dalton_core.material_requests import last_pack_build
        from dalton_core.product_contract import record_pack_build

        pack = normalise_pack(material_pack())
        record_pack_build(self.state, pack, run_ref="initial_screen-run:test")
        last = last_pack_build(self.state, COMPANY, "initial_screen")
        self.assertEqual(last["content_hash"], pack["content_hash"])

    def test_a_c_built_pack_on_an_empty_workspace_normalises(self):
        from dalton_core.product_contract import build_pack

        pack = build_pack(self.state, COMPANY, "initial_screen", "2026-08-15")
        self.assertIn("材料包", pack["text"])
        self.assertIsNotNone(pack["record"])


if __name__ == "__main__":
    unittest.main()
