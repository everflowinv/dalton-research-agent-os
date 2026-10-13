"""Batch D: the STRL-shaped Initial Screen contract, end to end on a synthetic company.

The pack is batch C's interface with synthetic content (``tests/synthetic_company``);
no real company stands in for a general rule.
"""

from __future__ import annotations

import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core import initial_screen_cli, product_contract
from dalton_core.initial_screen import (
    STRL_KEYS,
    STRL_TITLES,
    assess_strl_gate,
    build_document_prompt,
    build_verifier_prompt,
    build_worksheet_prompt,
    parse_document_output,
    parse_verifier_output,
    strl_contract_record,
    validate_worksheet,
    verify_document,
)
from dalton_core.product_contract import MaterialPackUnavailable, normalise_pack, tag_entry
from tests.synthetic_company import (
    COMPANY,
    material_pack,
    strl_document,
    strl_worksheet,
)
from tests.test_mission_deliverable import ACN, AUTOMATION, DeliverableHarness

SYNW = {"ticker": "SYNW", "company_ref": COMPANY}


def _draft(mutate=None):
    doc = strl_document()
    if mutate is not None:
        mutate(doc["sections"])
    return doc


def _verify(doc, worksheet=None):
    pack = material_pack()
    sheet, findings = validate_worksheet(worksheet or strl_worksheet(), pack)
    assert not findings, findings
    sections, parse_findings = parse_document_output(doc, pack=pack)
    verdict = verify_document(sections, worksheet=sheet, pack=pack)
    return sections, parse_findings + verdict["findings"]


def _codes(findings):
    return sorted({item["code"] for item in findings})


class PromptContractTests(unittest.TestCase):
    def test_the_document_prompt_carries_every_section_and_rule(self):
        pack = material_pack()
        prompt = build_document_prompt(company=SYNW, pack=pack, worksheet=strl_worksheet())
        for title in STRL_TITLES:
            self.assertIn(title, prompt)
        for needle in ("看多/看空/中性/观察", "观察价：暂无法给出（原因）", "倍数 × EPS = 观察价",
                       "FY+1、FY+2", "我们的估计", "声明算式", "MaterialRequest", "已尝试",
                       "只保留仍影响结论的问题", "company_not_disclosed", "not_acquired",
                       "〔数字见研判记录〕", "[consensus_without_authority]",
                       "[backlog_as_near_term_revenue]", "[usd_revenue_as_organic]"):
            self.assertIn(needle, prompt)
        # The old bans are gone.
        for banned in ("不要写目标价", "not a trade call", "上一版未决问题必须保留"):
            self.assertNotIn(banned, prompt)
        # The whole pack goes in, tags and all.
        self.assertIn("[P1]", prompt)
        self.assertIn("[N20]", prompt)

    def test_the_worksheet_prompt_is_step_one_and_asks_for_the_arithmetic(self):
        prompt = build_worksheet_prompt(company=SYNW, pack=material_pack())
        self.assertIn("Step 1 of 2", prompt)
        self.assertIn("diff_expression", prompt)
        self.assertIn("implied_expression", prompt)
        self.assertIn("reference_tags", prompt)

    def test_a_repair_names_the_findings_and_rewrites_the_whole_document(self):
        prompt = build_document_prompt(company=SYNW, pack=material_pack(), worksheet=strl_worksheet(),
                                       repair=[{"code": "view_missing", "section": "结论与观点"}])
        self.assertIn("Rewrite the whole document", prompt)
        self.assertIn("view_missing", prompt)

    def test_the_verifier_checks_and_never_deletes(self):
        sections, _ = parse_document_output(strl_document(), pack=material_pack())
        prompt = build_verifier_prompt(sections, worksheet=strl_worksheet(), pack=material_pack())
        self.assertIn("Do not rewrite it", prompt)
        self.assertIn("arithmetic", prompt)
        self.assertNotIn("delete", prompt.lower())


class WorksheetTests(unittest.TestCase):
    def test_the_strl_worksheet_is_clean(self):
        sheet, findings = validate_worksheet(strl_worksheet(), material_pack())
        self.assertEqual(findings, [])
        self.assertEqual(sheet["stance"], "看多")

    def test_wrong_arithmetic_and_a_non_consensus_source_are_findings(self):
        sheet = strl_worksheet()
        sheet["watch_price"]["value"] = "90.0"
        sheet["forecasts"][3]["consensus_tag"] = "P4"   # a single broker is not consensus
        sheet["forecasts"][0]["diff_pct"] = "3.0"
        _, findings = validate_worksheet(sheet, material_pack())
        self.assertIn("watch_price_arithmetic", _codes(findings))
        self.assertIn("forecast_consensus_not_consensus_authority", _codes(findings))
        self.assertIn("forecast_diff_arithmetic", _codes(findings))

    def test_a_stance_outside_the_vocabulary_is_a_finding(self):
        sheet = strl_worksheet()
        sheet["stance"] = "谨慎乐观"
        _, findings = validate_worksheet(sheet, material_pack())
        self.assertIn("stance_not_in_vocabulary", _codes(findings))

    def test_an_unavailable_watch_price_needs_a_reason_and_references(self):
        sheet = strl_worksheet()
        sheet["watch_price"] = {"status": "unavailable", "reason": "", "reference_tags": []}
        _, findings = validate_worksheet(sheet, material_pack())
        self.assertIn("watch_price_unavailable_without_reason", _codes(findings))
        self.assertIn("watch_price_unavailable_without_reference", _codes(findings))
        sheet["watch_price"] = {"status": "unavailable", "reason": "模型未通过不变量检查",
                                "reference_tags": ["N12", "P4"]}
        _, findings = validate_worksheet(sheet, material_pack())
        self.assertEqual(findings, [])

    def test_forecasts_need_two_years_of_revenue_and_eps(self):
        sheet = strl_worksheet()
        sheet["forecasts"] = sheet["forecasts"][:2]
        _, findings = validate_worksheet(sheet, material_pack())
        self.assertIn("forecast_needs_two_years", _codes(findings))


class VerifierTests(unittest.TestCase):
    def test_it_accepts_the_strl_shaped_draft(self):
        sections, findings = _verify(_draft())
        self.assertEqual(findings, [])
        self.assertEqual([s["title"] for s in sections], list(STRL_TITLES))

    def test_it_rejects_an_unsourced_number(self):
        _, findings = _verify(_draft(lambda s: s[2].__setitem__(
            "body", s[2]["body"].replace("公司份额 4%", "公司份额 7%"))))
        self.assertTrue(any("7%" in item.get("detail", "") for item in findings), findings)

    def test_it_rejects_a_view_less_draft(self):
        _, findings = _verify(_draft(lambda s: s[0].__setitem__(
            "body", "核心判断：服务业务的经营杠杆是关键变量，利润率改善来自服务占比上升，下半年订单转化需要跟踪。")))
        self.assertIn("view_missing", _codes(findings))
        self.assertIn("watch_price_missing", _codes(findings))

    def test_it_rejects_material_missing_without_a_request(self):
        _, findings = _verify(_draft(lambda s: s[1].__setitem__(
            "body", s[1]["body"] + "\n\n现有材料缺少分部利润率数据。")))
        self.assertTrue(any("material_missing_without_request" in item.get("detail", "")
                            for item in findings), findings)

    def test_material_missing_bound_to_a_request_is_accepted(self):
        def mutate(sections):
            sections[1]["body"] += "\n\n现有材料缺少分部利润率数据（material-request:synw-seg-margin）。"
        _, findings = _verify(_draft(mutate))
        self.assertEqual(findings, [])

    def test_it_rejects_a_reasoning_lint_hit(self):
        _, findings = _verify(_draft(lambda s: s[2].__setitem__(
            "body", s[2]["body"] + "\n\n公司正在赢得份额。")))
        self.assertIn("lint:management_share_gain_as_fact", _codes(findings))

    def test_it_rejects_wrong_arithmetic(self):
        def mutate(sections):
            numbers = sections[3]["numbers"]
            for item in numbers:
                if item.get("derivation", {}).get("result") == "11.1":
                    item["derivation"] = {"expression": "(5.00/4.50-1)*100", "result": "12.1"}
                    item["text"] = "EPS 高 12.1%"
            sections[3]["body"] = sections[3]["body"].replace("11.1%", "12.1%")
        _, findings = _verify(_draft(mutate))
        self.assertTrue(any("does not compute" in item.get("detail", "") for item in findings), findings)

    def test_an_unlabelled_estimate_is_rejected(self):
        def mutate(sections):
            sections[3]["body"] = sections[3]["body"].replace(
                "我们预计 FY2026 收入 5,050 百万美元", "FY2026 收入 5,050 百万美元")
        _, findings = _verify(_draft(mutate))
        self.assertTrue(any("我们的估计" in item.get("detail", "") for item in findings), findings)

    def test_an_indirect_placeholder_is_rejected(self):
        _, findings = _verify(_draft(lambda s: s[5].__setitem__(
            "body", s[5]["body"] + "\n\n财报反应的具体数字〔数字见研判记录〕。")))
        self.assertTrue(any("indirect_placeholder" in item.get("detail", "") for item in findings), findings)

    def test_a_quote_that_is_not_in_its_passage_is_a_finding(self):
        def mutate(sections):
            sections[1]["numbers"][0]["quote"] = "Services were 65% of revenue"
        _, findings = _verify(_draft(mutate))
        self.assertIn("quote_not_in_passage", _codes(findings))

    def test_the_appendix_must_bind_not_acquired_gaps(self):
        def mutate(sections):
            sections[8]["gap_items"][1].pop("request_ref")
        _, findings = _verify(_draft(mutate))
        self.assertIn("gap_item_invalid", _codes(findings))

    def test_the_model_verifier_output_is_closed(self):
        self.assertEqual(parse_verifier_output({"verdict": "pass", "findings": []})["verdict"], "pass")
        with self.assertRaises(ValueError):
            parse_verifier_output({"verdict": "pass", "findings": [{"code": "view_missing"}]})
        with self.assertRaises(ValueError):
            parse_verifier_output({"verdict": "reject", "findings": [{"code": "made_up"}]})


class GateTests(unittest.TestCase):
    PLAYBOOK = {"stages": [{"stage_ref": "initial_screen", "exit_gate": {"questions": ["q1", "q2", "q3", "q4"]}}]}
    ENTRY = {"items": [{"label": "x", "status": "complete"}]}

    def test_a_clean_document_passes(self):
        sections, findings = _verify(_draft())
        gate = assess_strl_gate(playbook=self.PLAYBOOK, checklist_entry=self.ENTRY, sections=sections,
                                verdict={"verdict": "pass", "findings": findings})
        self.assertTrue(gate["passed"], gate["rationale"])

    def test_a_gap_only_section_cannot_pass(self):
        def mutate(sections):
            sections[4]["body"] = "现有材料缺少估值快照，估值一节暂缺。"
            sections[4]["numbers"] = []
        pack = material_pack()
        sections, _ = parse_document_output(_draft(mutate), pack=pack)
        gate = assess_strl_gate(playbook=self.PLAYBOOK, checklist_entry=self.ENTRY, sections=sections,
                                verdict={"verdict": "pass", "findings": []})
        self.assertFalse(gate["passed"])
        self.assertEqual([item["title"] for item in gate["failed_sections"]], ["估值与观察价"])


class FakeModel:
    """Answers the worksheet, document and verifier calls in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def call(self, *, purpose, request_id, prompt, mission, producer_route_decision_refs=()):
        self.calls.append({"purpose": purpose, "request_id": request_id, "chars": len(prompt)})
        reply = self.replies.pop(0)
        n = len(self.calls)
        return {"text": json.dumps(reply, ensure_ascii=False), "invocation_ref": f"invocation:{n}",
                "route_decision_ref": f"route-decision:{n}", "cost_micros": 0, "replayed": False}


class LaneTests(DeliverableHarness):
    def setUp(self):
        super().setUp()
        self.grant("deliverable")
        self.mission = self.missions.active_mission(self.mission_ref)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_dir = Path(temp.name)
        self.live_pack_claim()

    def live_pack_claim(self):
        """The pack's C1 is a live Claim in this Core, as batch C would only tag live ones."""

        from dalton_core.store import content_hash
        from tests.synthetic_company import CLAIMS

        ref, statement = CLAIMS["C1"]
        claim = {
            "schema_version": "0.2", "id": ref, "claim_ref": "claim:test:pack-c1", "version": 1,
            "subject_ref": ACN, "metric_or_aspect": "aspect:test", "period": "2026Q2",
            "basis": "fixture", "normalized_statement": statement, "claim_kind": "qualitative",
            "value": None, "unit": None, "currency": None, "scale": None,
            "producer_execution_refs": [], "semantic_review_ref": None, "semantic_review_hash": None,
            "candidate_origin_ref": None, "candidate_origin_hash": None,
            "actor_ref": "system:research-auto-commit", "prior_version_ref": None,
            "created_at": "2026-08-01T00:00:00+00:00",
        }
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1, json.dumps(claim, sort_keys=True),
                 claim["content_hash"], claim["created_at"]))

    def run_strl(self, model=None, *, pack=None, dry_run=False):
        args = argparse.Namespace(state_dir=str(self.state_dir), model_config=None,
                                  summary_dir=str(self.state_dir), scheduler_db=None,
                                  timeout_seconds=30, dry_run=dry_run, quiet=True, as_of=None)
        entry = {"company_ref": ACN, "ticker": "ACN", "items": [{"label": "x", "status": "complete"}],
                 "reopen": None}
        summary = {"status": "succeeded", "sections": [], "formal_authority_writes": 0}
        if pack is None:
            check = MaterialPackUnavailable("dalton_core.material_pack is not installed")
        else:
            check = lambda *a, **k: dict(pack.sufficiency)  # noqa: E731
        with mock.patch.object(initial_screen_cli, "check_sufficiency", side_effect=check), \
                mock.patch.object(initial_screen_cli, "build_pack",
                                  side_effect=lambda *a, **k: normalise_pack(pack)):
            return initial_screen_cli._run_strl(
                args, state=self.state_dir, store=self.store, mission=self.mission,
                playbook=self.playbook, entry=entry, summary=summary,
                deliverable_authority=self.authority, missions=self.missions, claims={},
                model=model)

    def test_no_material_pack_holds_with_a_named_reason(self):
        summary = self.run_strl()
        self.assertEqual(summary["status"], "held")
        self.assertEqual(summary["reason_code"], "material_pack_unavailable")
        self.assertIn("material_pack", summary["failure_reason"])

    def test_the_sibling_module_absence_is_a_clear_error(self):
        def missing(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        with mock.patch.object(product_contract.importlib, "import_module", side_effect=missing):
            with self.assertRaises(MaterialPackUnavailable) as caught:
                product_contract.build_pack(self.state_dir, ACN, "initial_screen")
        self.assertIn("batch C", str(caught.exception))

    def test_an_insufficient_pack_holds_and_lists_the_requests_on_a_dry_run(self):
        summary = self.run_strl(pack=material_pack(sufficiency="insufficient"), dry_run=True)
        self.assertEqual(summary["status"], "held")
        self.assertEqual(summary["reason_code"], "material_insufficient")
        self.assertEqual(summary["material_requests"], ["material-request:synw-call"])
        self.assertIn("缺最新季度电话会纪要", summary["failure_reason"])

    def test_a_dry_run_builds_the_prompts_and_calls_nothing(self):
        summary = self.run_strl(pack=material_pack(company=ACN), dry_run=True)
        self.assertEqual([row["status"] for row in summary["sections"]], ["dry_run"] * 9)
        self.assertGreater(summary["prompts"]["worksheet_chars"], 1000)

    def test_two_steps_then_publish_with_the_contract_and_the_score(self):
        model = FakeModel([strl_worksheet(), strl_document(), {"verdict": "pass", "findings": []}])
        summary = self.run_strl(model, pack=material_pack(company=ACN))
        self.assertEqual(summary["status"], "succeeded", summary.get("failure_reason"))
        self.assertTrue(model.calls[0]["request_id"].endswith(":worksheet"))
        self.assertTrue(model.calls[1]["request_id"].endswith(":document:0"))
        self.assertEqual(len(model.calls), 3)        # worksheet, one document, one verifier
        self.assertEqual(model.calls[2]["purpose"], "initial_screen_verifier")
        record = self.authority.latest("mission-deliverable:initial_screen:" + ACN.rsplit(":", 1)[-1])
        self.assertEqual([s["title"] for s in record["sections"]], list(STRL_TITLES))
        self.assertEqual(record["contract"]["stance"], "看多")
        self.assertEqual(record["contract"]["watch_price"]["value"], "85.0")
        # Batch C's provenance block, where redraft_policy reads the watermark.
        self.assertEqual(record["material_pack"]["recipe_version"], "initial_screen:synthetic")
        self.assertEqual(record["material_pack"]["watermark"]["as_of"], "2026-08-15")
        self.assertTrue(summary["gate"]["passed"], summary["gate"]["rationale"])
        # This synthetic pack has no acquired originals. Publication's full
        # contract still passes; current quality cannot certify an issuer call
        # from its declared transcript label alone.
        self.assertFalse(summary["quality"]["passed"])
        self.assertIn("latest_transcript_and_release_cited", summary["quality"]["failed_checks"])
        self.assertIn(summary["quality"]["recorded"], {"fresh", "recorded"})

    def test_a_rejected_document_gets_one_repair_and_is_not_published_if_still_wrong(self):
        bad = strl_document()
        bad["sections"][0]["body"] = "核心判断：服务业务的经营杠杆是关键变量，利润率改善来自服务占比上升，下半年订单转化需要跟踪。"
        model = FakeModel([strl_worksheet(), bad, copy.deepcopy(bad)])
        summary = self.run_strl(model, pack=material_pack())
        self.assertEqual(summary["status"], "failed")
        self.assertIn("view_missing", summary["failure_reason"])
        self.assertEqual(len(summary["verification"]), 2)
        self.assertIsNone(self.authority.latest("mission-deliverable:initial_screen:" + ACN.rsplit(":", 1)[-1]))

    def test_a_repair_that_fixes_the_document_is_published(self):
        bad = strl_document()
        bad["sections"][2]["body"] += "\n\n公司正在赢得份额。"
        model = FakeModel([strl_worksheet(), bad, strl_document(), {"verdict": "pass", "findings": []}])
        summary = self.run_strl(model, pack=material_pack())
        self.assertEqual(summary["status"], "succeeded", summary.get("failure_reason"))
        self.assertEqual([a["verdict"] for a in summary["verification"]], ["reject", "pass"])


class PublishContractTests(DeliverableHarness):
    def test_strict_publish_refuses_a_gap_only_section(self):
        self.grant("deliverable")
        pack = material_pack()
        sections, _ = parse_document_output(_draft(lambda s: (
            s[4].__setitem__("body", "现有材料缺少估值快照，估值一节暂缺。"),
            s[4].__setitem__("numbers", []))), pack=pack)
        from dalton_core.initial_screen import pack_resolvers
        from dalton_core.mission_deliverable import MissionDeliverableConflict

        numbers, passages = pack_resolvers(pack)
        with self.assertRaises(MissionDeliverableConflict):
            self.authority.publish(
                kind="initial_screen", subject_ref=ACN,
                mission=self.missions.active_mission(self.mission_ref), playbook=self.playbook,
                template_ref="contract:initial-screen-contract:strl:1", sections=sections,
                summary="摘要。", actor_ref=AUTOMATION,
                pack_numbers=numbers, pack_passages=passages,
                contract=strl_contract_record(pack=pack, worksheet=strl_worksheet(),
                                              verdict={"verdict": "pass", "findings": []}))

    def test_a_tampered_pack_passage_does_not_resolve(self):
        from dalton_core.mission_deliverable import pack_passage_resolver

        entry = tag_entry(normalise_pack(material_pack()), "P1")
        ref = entry["ref"]
        good = pack_passage_resolver(None, {ref: entry["text"]})
        bad = pack_passage_resolver(None, {ref: entry["text"].replace("60%", "70%")})
        self.assertIsNotNone(good(ref))
        self.assertIsNone(bad(ref))


if __name__ == "__main__":
    unittest.main()
