"""W4: the frozen driver-template registry and the three outputs that read it."""

import json
import unittest
from pathlib import Path

from dalton_core.claim_index_authority import (
    EVIDENCE_KINDS,
    EVIDENCE_KIND_DEFINITIONS,
    MARKET_PROXY,
)
from dalton_core.company_dossier import INDUSTRY_CLASSIFICATIONS
from dalton_core.driver_template import (
    DRIVER_TEMPLATES,
    GENERIC,
    MAX_TEMPLATE_GAPS,
    REGISTRY_HASH,
    DriverTemplateError,
    basis_concept_plan,
    covered_slots,
    debate_map_gaps,
    dossier_demand_driver_gaps,
    evidence_kinds_for,
    is_generic,
    known_classifications,
    prompt_block,
    slot_ids,
    spec_gaps,
    template_for,
    template_gaps,
)

ROOT = Path(__file__).parents[1]
PUBLISHED = ROOT / "deploy/phase9/w4-driver-template-v1.json"
CONTRACT = ROOT / "contracts/driver-template-registry.schema.json"


class RegistryTests(unittest.TestCase):
    def test_the_published_bytes_are_the_constants(self):
        published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
        self.assertEqual(published.pop("content_hash"), REGISTRY_HASH)
        self.assertEqual(published, {key: value for key, value in DRIVER_TEMPLATES.items()})

    def test_the_registry_satisfies_its_own_contract(self):
        schema = json.loads(CONTRACT.read_text(encoding="utf-8"))
        published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
        self.assertEqual(set(published), set(schema["required"]))
        template_props = schema["$defs"]["template"]["properties"]
        slot_props = schema["$defs"]["slot"]["properties"]
        for template in published["templates"]:
            self.assertEqual(set(template), set(template_props))
            self.assertIn(template["classification"],
                          template_props["classification"]["enum"])
            for slot in template["slots"]:
                self.assertEqual(set(slot), set(slot_props))
                self.assertTrue(slot["evidence_kinds"])
                self.assertTrue(slot["cues"])
                for kind in slot["evidence_kinds"]:
                    self.assertIn(kind, EVIDENCE_KINDS)

    def test_every_dossier_classification_reaches_a_template(self):
        for word in INDUSTRY_CLASSIFICATIONS:
            with self.subTest(word=word):
                template = template_for(word)
                self.assertTrue(template["slots"])
                if word == "insufficient_evidence":
                    self.assertEqual(template["classification"], GENERIC)
                else:
                    self.assertEqual(template["classification"], word)

    def test_the_five_kinds_carry_the_slots_the_retrospective_named(self):
        self.assertEqual(
            slot_ids("commodity_cycle"),
            ("spread", "utilisation", "cost_curve_position", "capacity_additions"))
        self.assertEqual(slot_ids("capital_cycle"),
                         ("capex", "returns_on_capital", "capacity_cycle"))
        self.assertEqual(slot_ids("contract_compounder"),
                         ("pricing", "retention", "unit_economics"))
        self.assertEqual(slot_ids("structural_growth"),
                         ("penetration", "tam", "adoption"))
        self.assertEqual(slot_ids("turnaround"), ("milestones", "cash_runway"))

    def test_slot_ids_are_unique_within_a_template(self):
        for word in known_classifications():
            ids = slot_ids(word)
            self.assertEqual(len(ids), len(set(ids)), word)

    def test_market_proxy_is_expected_where_nothing_is_filed(self):
        # The whole reason the kind exists: a spread and a TAM have no filed
        # counterpart, so the slot that asks for them asks for a proxy.
        self.assertIn(MARKET_PROXY, evidence_kinds_for("commodity_cycle", "spread"))
        self.assertIn(MARKET_PROXY, evidence_kinds_for("structural_growth", "tam"))
        self.assertEqual(template_for("structural_growth")["slots"][1]["basis_concepts"], [])

    def test_an_unknown_evidence_kind_cannot_enter_a_slot(self):
        from dalton_core import driver_template

        with self.assertRaises(DriverTemplateError):
            driver_template._slot("x", "x", "x?", (), ("not_a_kind",), ("x",))

    def test_every_evidence_kind_has_a_definition(self):
        self.assertEqual(set(EVIDENCE_KINDS), set(EVIDENCE_KIND_DEFINITIONS))


class SelectionTests(unittest.TestCase):
    def test_nothing_known_selects_the_generic_template_and_says_so(self):
        for value in (None, "", "not_a_classification", 7, [],
                      "insufficient_evidence"):
            with self.subTest(value=value):
                self.assertTrue(is_generic(value))
                self.assertEqual(template_for(value)["classification"], GENERIC)

    def test_a_dossier_block_may_be_passed_whole(self):
        block = {"classification": "commodity_cycle", "slots": [], "sources": [],
                 "gaps": []}
        self.assertEqual(template_for(block)["classification"], "commodity_cycle")
        self.assertFalse(is_generic(block))

    def test_the_generic_prompt_block_labels_itself(self):
        text = prompt_block(None)
        self.assertIn("GENERIC", text)
        self.assertIn("no industry classification", text)
        self.assertNotIn("GENERIC", prompt_block("commodity_cycle"))

    def test_evidence_kinds_for_an_unknown_slot_is_an_error(self):
        with self.assertRaises(DriverTemplateError):
            evidence_kinds_for("commodity_cycle", "penetration")


class BasisConceptTests(unittest.TestCase):
    def test_the_plan_fills_only_concepts_the_company_filed(self):
        plan = basis_concept_plan(
            "capital_cycle",
            ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
             "us-gaap:OperatingIncomeLoss"])
        by_slot = {row["slot_id"]: row for row in plan}
        self.assertEqual(by_slot["capex"]["filed_concepts"],
                         ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"])
        self.assertEqual(by_slot["returns_on_capital"]["filed_concepts"],
                         ["us-gaap:OperatingIncomeLoss"])
        self.assertEqual(by_slot["capacity_cycle"]["filed_concepts"], [])

    def test_a_slot_with_nothing_filed_and_a_proxy_kind_needs_a_proxy(self):
        plan = {row["slot_id"]: row for row in basis_concept_plan("commodity_cycle", [])}
        self.assertTrue(plan["spread"]["needs_proxy"])
        self.assertTrue(plan["capacity_additions"]["needs_proxy"])
        # ``cash_runway`` expects only company figures, so its emptiness is a
        # gap rather than a proxy.
        turnaround = {row["slot_id"]: row
                      for row in basis_concept_plan("turnaround", [])}
        self.assertFalse(turnaround["cash_runway"]["needs_proxy"])

    def test_the_prefix_of_a_filed_concept_does_not_decide_the_match(self):
        plan = {row["slot_id"]: row
                for row in basis_concept_plan("generic", ["ifrs-full:Revenues"])}
        self.assertEqual(plan["price"]["filed_concepts"], ["ifrs-full:Revenues"])

    def test_the_prompt_block_shows_what_was_filed_and_what_was_not(self):
        text = prompt_block("commodity_cycle", ["us-gaap:CostOfRevenue"])
        self.assertIn("us-gaap:CostOfRevenue", text)
        self.assertIn("none filed", text)
        self.assertIn("market_proxy", text)


class CoverageTests(unittest.TestCase):
    def test_a_cue_in_any_language_covers_its_slot(self):
        self.assertEqual(covered_slots("commodity_cycle", ["MDI 价差走阔"]),
                         {"spread"})
        self.assertEqual(covered_slots("commodity_cycle", ["the operating rate rose"]),
                         {"utilisation"})

    def test_an_uncovered_slot_becomes_a_gap_naming_its_evidence_kinds(self):
        gaps = template_gaps("turnaround", ["里程碑已完成两项"], subject="档案")
        self.assertEqual(len(gaps), 1)
        self.assertIn("cash_runway", gaps[0])
        self.assertIn("company_figure", gaps[0])
        self.assertIn("档案", gaps[0])

    def test_covering_every_slot_produces_no_gap(self):
        self.assertEqual(
            template_gaps("turnaround",
                          ["里程碑已完成两项", "现金跑道还有六个季度"],
                          subject="档案"),
            [])

    def test_the_gap_list_is_bounded(self):
        gaps = template_gaps("commodity_cycle", [], subject="档案", limit=2)
        self.assertEqual(len(gaps), 2)
        self.assertLessEqual(
            len(template_gaps("commodity_cycle", [], subject="档案")),
            MAX_TEMPLATE_GAPS)

    def test_a_generic_gap_says_it_is_generic(self):
        gaps = template_gaps(None, [], subject="档案")
        self.assertTrue(gaps)
        self.assertTrue(all("通用模板" in gap for gap in gaps))

    def test_the_check_is_deterministic(self):
        first = template_gaps("capital_cycle", ["capex fell"], subject="s")
        second = template_gaps("capital_cycle", ["capex fell"], subject="s")
        self.assertEqual(first, second)


class ReaderTests(unittest.TestCase):
    def test_a_dossier_section_is_read_through_its_sentences(self):
        section = {
            "structure": ["causal_chain:0"],
            "slots": [{"slot_id": "causal_chain:0",
                       "sentences": [{"text": "开工率维持在九成", "refs": ["c"]}]}],
            "gaps": [],
        }
        gaps = dossier_demand_driver_gaps(section, "commodity_cycle")
        self.assertTrue(all("utilisation" not in gap for gap in gaps))
        self.assertTrue(any("spread" in gap for gap in gaps))
        self.assertTrue(all(gap.startswith("档案 demand_drivers") for gap in gaps))

    def test_an_unknown_slot_reason_counts_as_coverage(self):
        section = {"structure": [], "gaps": [],
                   "slots": [{"slot_id": "causal_chain:0",
                              "unknown": "没有任何成本曲线位置的材料"}]}
        gaps = dossier_demand_driver_gaps(section, "commodity_cycle")
        self.assertTrue(all("cost_curve_position" not in gap for gap in gaps))

    def test_a_missing_section_is_all_gaps_and_not_an_error(self):
        self.assertEqual(len(dossier_demand_driver_gaps(None, "turnaround")), 2)

    def test_a_debate_map_is_read_through_questions_and_positions(self):
        debates = [{
            "question": "扩产是否会压垮价差？",
            "driver_refs": ["driver:spread"],
            "bull_position": {"statement": "新增产能推迟"},
            "bear_position": {"statement": "开工率已在高位"},
        }]
        gaps = debate_map_gaps(debates, "commodity_cycle")
        self.assertEqual(len(gaps), 1)
        self.assertIn("cost_curve_position", gaps[0])
        self.assertTrue(gaps[0].startswith("DebateMap"))

    def test_a_specification_is_read_through_labels_and_reasons(self):
        spec = {
            "assessment": "定价能力是这家公司的全部",
            "revenue_drivers": [{"label": "price", "because": "提价传导", "basis_concept": None}],
            "expense_lines": [],
            "operating_metrics": [],
        }
        gaps = spec_gaps(spec, "contract_compounder")
        self.assertTrue(all("pricing" not in gap for gap in gaps))
        self.assertTrue(any("retention" in gap for gap in gaps))
        self.assertTrue(gaps[0].startswith("建模规格"))

    def test_nothing_at_all_is_read_as_no_coverage_rather_than_an_error(self):
        self.assertEqual(len(spec_gaps(None, "turnaround")), 2)
        self.assertEqual(len(debate_map_gaps([], "turnaround")), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
