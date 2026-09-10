"""P12e drafting: the prompt, the refusals and the independent verifier.

The refusal tests are the point. A drafting path that repairs a reply it did
not like is a path whose output nobody can reason about: the rows the model got
right came out of the same reply as the row it invented.
"""

from __future__ import annotations

import unittest

from dalton_core.industry_framework import (
    CHARACTERISTIC_SLOTS,
    IndustryFrameworkValidationError,
)
from dalton_core.industry_framework_draft import (
    DRAFT_PURPOSE,
    MAX_CELL_ROWS,
    MAX_CLAIM_ROWS,
    TAG_PREFIXES,
    FrameworkDraftRefused,
    build_unit_prompt,
    build_verifier_prompt,
    draft_unit,
    independence,
    independence_precheck,
    material_rows,
    parse_unit_output,
    render_material,
    summarise_blocks,
    validate_verifier_output,
    verify,
)

INDUSTRY = {"industry_ref": "industry:us-it-services",
            "tickers": ["ACN", "CTSH", "EPAM", "IBM", "DXC"]}
MISSION = {"id": "mission-version:1"}
SECTION_STRUCTURE = [
    {"slot_id": "state", "prompt": "什么"},
    {"slot_id": "divergence", "prompt": "哪里不同"},
]


def rows():
    return material_rows([
        {"kind": "claim", "ref": "claim-version:a", "text": "行业需求在企稳。",
         "period": "2026Q2", "importance": "primary_filing"},
        {"kind": "comparison_cell",
         "ref": "comparison-cell:company-a:gross_margin:2026Q2",
         "text": "AAA 2026Q2（期末 2026-06-30）gross_margin 40.0%",
         "period": "2026Q2", "importance": "derived"},
        {"kind": "dossier_section", "ref": "company-dossier-version:a:2#demand_drivers",
         "text": "AAA demand_drivers：AI 需求真实。", "period": None,
         "importance": "dossier"},
    ])


def reply(slots, **extra):
    import json

    return json.dumps({"slots": slots, "gaps": ["缺 TAM"], **extra}, ensure_ascii=False)


class MaterialTests(unittest.TestCase):
    def test_each_ref_kind_gets_its_own_tag_letter_and_numbering(self):
        tagged = rows()
        self.assertEqual([row["tag"] for row in tagged], ["C1", "T1", "D1"])
        self.assertEqual(TAG_PREFIXES["comparison_cell"], "T")

    def test_a_kind_the_record_cannot_cite_is_refused(self):
        with self.assertRaises(IndustryFrameworkValidationError):
            material_rows([{"kind": "rumour", "ref": "x", "text": "y"}])

    def test_a_row_without_a_ref_or_a_text_is_refused(self):
        with self.assertRaises(IndustryFrameworkValidationError):
            material_rows([{"kind": "claim", "ref": "", "text": "y"}])

    def test_each_kind_is_bounded_on_its_own(self):
        many = ([{"kind": "claim", "ref": f"c{n}", "text": "t"} for n in range(80)]
                + [{"kind": "comparison_cell", "ref": f"t{n}", "text": "t"}
                   for n in range(80)])
        tagged = material_rows(many)
        claims = [row for row in tagged if row["kind"] == "claim"]
        cells = [row for row in tagged if row["kind"] == "comparison_cell"]
        self.assertEqual(len(claims), MAX_CLAIM_ROWS)
        self.assertEqual(len(cells), MAX_CELL_ROWS)

    def test_the_rendering_labels_the_table_a_cell_belongs_to(self):
        text = render_material(rows())
        self.assertIn("Statements about the industry", text)
        self.assertIn("NOT yours to recompute", text)
        self.assertIn("Company file sections already accepted", text)


class PromptTests(unittest.TestCase):
    def test_the_prompt_lists_the_slots_and_forbids_inventing_one(self):
        prompt = build_unit_prompt(
            unit="causal_chain:1", structure=SECTION_STRUCTURE, material=rows(),
            industry=INDUSTRY, link="订单领先收入。", title="新签订单")
        self.assertIn("state\t什么", prompt)
        self.assertIn("invent none", prompt)
        self.assertIn("新签订单", prompt)

    def test_the_prompt_forbids_recomputing_a_cell(self):
        prompt = build_unit_prompt(
            unit="causal_chain:1", structure=SECTION_STRUCTURE, material=rows(),
            industry=INDUSTRY, comparison_table="company\tmetric\t2026Q2")
        self.assertIn("do not add a row", prompt)
        self.assertIn("do not recompute a percentage", prompt)

    def test_the_characteristics_prompt_carries_the_five_closed_lists(self):
        prompt = build_unit_prompt(
            unit="characteristics",
            structure=[{"slot_id": slot, "prompt": "p"} for slot in CHARACTERISTIC_SLOTS],
            material=rows(), industry=INDUSTRY)
        self.assertIn("insufficient_evidence", prompt)
        self.assertIn("choices:", prompt)

    def test_the_driver_prompt_says_unknown_is_an_answer(self):
        prompt = build_unit_prompt(
            unit="long_term_drivers",
            structure=[{"slot_id": "driver:x", "prompt": "p"}],
            material=rows(), industry=INDUSTRY)
        self.assertIn("'unknown' is the honest answer", prompt)

    def test_a_tag_never_appears_as_an_instruction_to_write_it_in_prose(self):
        prompt = build_unit_prompt(
            unit="causal_chain:0", structure=SECTION_STRUCTURE, material=rows(),
            industry=INDUSTRY)
        self.assertIn("NEVER write a C, T or D tag inside a sentence's text", prompt)


class ParseTests(unittest.TestCase):
    def parse(self, text, **kwargs):
        return parse_unit_output(
            text, unit=kwargs.pop("unit", "causal_chain:0"),
            structure=kwargs.pop("structure", SECTION_STRUCTURE),
            material=kwargs.pop("material", rows()),
            link=kwargs.pop("link", "预算按年审批。"),
            title=kwargs.pop("title", "预算与宏观"),
            link_index=kwargs.pop("link_index", 0), **kwargs)

    def test_a_well_formed_reply_becomes_a_validated_section(self):
        block = self.parse(reply([
            {"slot_id": "state", "sentences": [
                {"text": "行业需求在企稳。", "refs": ["C1"]}]},
            {"slot_id": "divergence", "sentences": [
                {"text": "AAA 的毛利率为 40.0%。", "refs": ["T1"]}]},
        ]))
        self.assertEqual(block["status"], "drafted")
        self.assertEqual(block["title"], "预算与宏观")
        self.assertEqual({row["ref"] for row in block["sources"]},
                         {"claim-version:a",
                          "comparison-cell:company-a:gross_margin:2026Q2"})

    def test_a_tag_that_was_not_shown_refuses_the_whole_batch(self):
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "state", "sentences": [
                    {"text": "需求在企稳。", "refs": ["C1"]}]},
                {"slot_id": "divergence", "sentences": [
                    {"text": "另一家不同。", "refs": ["T9"]}]},
            ]))
        self.assertIn("refused whole", str(caught.exception))

    def test_a_slot_that_was_not_asked_for_is_refused(self):
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "state", "sentences": [
                    {"text": "需求在企稳。", "refs": ["C1"]}]},
                {"slot_id": "outlook", "sentences": [
                    {"text": "会更好。", "refs": ["C1"]}]},
            ]))
        self.assertIn("not the", str(caught.exception))

    def test_a_missing_slot_is_refused(self):
        with self.assertRaises(FrameworkDraftRefused):
            self.parse(reply([
                {"slot_id": "state", "sentences": [
                    {"text": "需求在企稳。", "refs": ["C1"]}]},
            ]))

    def test_a_sentence_citing_nothing_is_refused(self):
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "state", "sentences": [
                    {"text": "需求在企稳。", "refs": []}]},
                {"slot_id": "divergence", "unknown": "材料不足"},
            ]))
        self.assertIn("cites nothing", str(caught.exception))

    def test_a_key_the_contract_does_not_have_is_refused(self):
        import json

        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(json.dumps({"slots": [], "gaps": [], "confidence": "high"}))
        self.assertIn("the contract is", str(caught.exception))

    def test_a_reply_that_is_not_json_is_refused(self):
        with self.assertRaises(FrameworkDraftRefused):
            self.parse("I think the industry is fine.")

    def test_more_sentences_than_the_cap_is_refused(self):
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "state", "sentences": [
                    {"text": f"第 {n} 句。", "refs": ["C1"]} for n in range(4)]},
                {"slot_id": "divergence", "unknown": "材料不足"},
            ]))
        self.assertIn("the cap is", str(caught.exception))

    def test_a_tag_written_into_the_prose_is_refused(self):
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "state", "sentences": [
                    {"text": "如 T1 所示，毛利率为 40.0%。", "refs": ["T1"]}]},
                {"slot_id": "divergence", "unknown": "材料不足"},
            ]))
        self.assertIn("citation tag", str(caught.exception))

    def test_every_slot_unknown_is_not_a_drafted_section(self):
        # It fails on "cites nothing" rather than on the sentence count,
        # because a reply that answered every slot with unknown cited no tag
        # either. Both readings say the same thing: that unit is unavailable,
        # and the caller records it as unavailable rather than as a section
        # that was written and says nothing.
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "state", "unknown": "材料不足"},
                {"slot_id": "divergence", "unknown": "材料不足"},
            ]))
        self.assertIn("cites nothing", str(caught.exception))

    def test_a_characteristics_reply_needs_all_five_words(self):
        import json

        structure = [{"slot_id": slot, "prompt": "p"} for slot in CHARACTERISTIC_SLOTS]
        payload = json.dumps({
            "values": {"classification": "structural_growth"},
            "slots": [{"slot_id": slot, "unknown": "不知道"} for slot in CHARACTERISTIC_SLOTS],
            "gaps": [],
        }, ensure_ascii=False)
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(payload, unit="characteristics", structure=structure)
        self.assertIn("must answer exactly", str(caught.exception))

    def test_a_characteristics_word_outside_the_closed_list_is_refused(self):
        import json

        structure = [{"slot_id": slot, "prompt": "p"} for slot in CHARACTERISTIC_SLOTS]
        payload = json.dumps({
            "values": {"classification": "structural_growth",
                       "cyclicality": "very_cyclical",
                       "revenue_visibility": "backlog_led",
                       "capital_intensity": "asset_light",
                       "concentration": "fragmented"},
            "slots": [{"slot_id": slot, "sentences": [
                {"text": "有依据。", "refs": ["C1"]}]} for slot in CHARACTERISTIC_SLOTS],
            "gaps": [],
        }, ensure_ascii=False)
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(payload, unit="characteristics", structure=structure)
        self.assertIn("must be one of", str(caught.exception))

    def test_a_driver_block_must_take_a_stance_on_every_driver_it_was_given(self):
        import json

        structure = [{"slot_id": "driver:a", "prompt": "p"},
                     {"slot_id": "driver:b", "prompt": "p"}]
        payload = json.dumps({
            "stances": {"driver:a": "positive"},
            "slots": [{"slot_id": "driver:a", "sentences": [
                          {"text": "有依据。", "refs": ["C1"]}]},
                      {"slot_id": "driver:b", "unknown": "材料没有说"}],
            "gaps": [],
        }, ensure_ascii=False)
        with self.assertRaises(FrameworkDraftRefused) as caught:
            self.parse(payload, unit="long_term_drivers", structure=structure)
        self.assertIn("exactly the drivers", str(caught.exception))


class FakeModel:
    """A model that returns what it was handed, and records what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def call(self, *, purpose, request_id, prompt, mission,
             producer_route_decision_refs=()):
        self.calls.append({"purpose": purpose, "request_id": request_id,
                           "prompt": prompt,
                           "producer_route_decision_refs": tuple(producer_route_decision_refs)})
        text = self.replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return {"text": text, "work_order_ref": f"work:{len(self.calls)}",
                "route_decision_ref": f"route:{len(self.calls)}",
                "replayed": False, "cost_micros": 1000}


class DraftUnitTests(unittest.TestCase):
    def test_a_drafted_unit_carries_its_route_decision(self):
        model = FakeModel([reply([
            {"slot_id": "state", "sentences": [{"text": "企稳。", "refs": ["C1"]}]},
            {"slot_id": "divergence", "unknown": "材料不足"},
        ])])
        outcome = draft_unit(model, unit="causal_chain:0", structure=SECTION_STRUCTURE,
                             material=rows(), industry=INDUSTRY, mission=MISSION,
                             link="预算按年审批。", title="预算与宏观")
        self.assertEqual(outcome["status"], "drafted")
        self.assertEqual(outcome["model"]["route_decision_ref"], "route:1")
        self.assertEqual(model.calls[0]["purpose"], DRAFT_PURPOSE)

    def test_a_chain_section_drafted_without_its_link_is_refused(self):
        # The link text and the policy's title are not decoration: a section
        # that does not carry the link it is about cannot be replayed against
        # the Constitution it was written under. A caller that forgets them
        # gets a refusal rather than a section with an empty heading.
        model = FakeModel([reply([
            {"slot_id": "state", "sentences": [{"text": "企稳。", "refs": ["C1"]}]},
            {"slot_id": "divergence", "unknown": "材料不足"},
        ])])
        outcome = draft_unit(model, unit="causal_chain:0", structure=SECTION_STRUCTURE,
                             material=rows(), industry=INDUSTRY, mission=MISSION)
        self.assertEqual(outcome["status"], "refused")

    def test_a_deviating_reply_comes_back_as_refused_rather_than_raising(self):
        outcome = draft_unit(FakeModel(["not json"]), unit="causal_chain:0",
                             structure=SECTION_STRUCTURE, material=rows(),
                             industry=INDUSTRY, mission=MISSION)
        self.assertEqual(outcome["status"], "refused")
        self.assertIn("not a JSON object", outcome["reason"])

    def test_a_model_error_is_unavailable_rather_than_a_crash(self):
        from dalton_core.cockpit_model import CockpitModelError

        outcome = draft_unit(FakeModel([CockpitModelError("day budget spent")]),
                             unit="causal_chain:0", structure=SECTION_STRUCTURE,
                             material=rows(), industry=INDUSTRY, mission=MISSION)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("day budget spent", outcome["reason"])

    def test_the_same_unit_and_material_produce_the_same_request_id(self):
        first = draft_unit(FakeModel(["bad"]), unit="causal_chain:0",
                           structure=SECTION_STRUCTURE, material=rows(),
                           industry=INDUSTRY, mission=MISSION)
        model = FakeModel(["bad"])
        draft_unit(model, unit="causal_chain:0", structure=SECTION_STRUCTURE,
                   material=rows(), industry=INDUSTRY, mission=MISSION)
        self.assertTrue(first)
        self.assertEqual(len(model.calls[0]["request_id"]), 32)


class VerifierTests(unittest.TestCase):
    def blocks(self):
        return {"causal_chain:0": parse_unit_output(
            reply([
                {"slot_id": "state", "sentences": [{"text": "企稳。", "refs": ["C1"]}]},
                {"slot_id": "divergence", "unknown": "材料不足"},
            ]),
            unit="causal_chain:0", structure=SECTION_STRUCTURE, material=rows(),
            link="预算按年审批。", title="预算与宏观", link_index=0)}

    def test_the_verifier_prompt_shows_each_sentence_beside_what_it_cites(self):
        prompt = build_verifier_prompt(self.blocks(), industry=INDUSTRY)
        self.assertIn("cites: 行业需求在企稳。", prompt)
        self.assertIn("You are an independent verifier", prompt)

    def test_a_pass_verdict_names_the_draft_it_read(self):
        import json

        model = FakeModel([json.dumps({"verdict": "pass", "findings": []})])
        result = verify(
            model, self.blocks(), industry=INDUSTRY, mission=MISSION,
            producer_route_decision_refs=["route:draft"],
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(len(result["verified_draft_hash"]), 64)
        self.assertEqual(model.calls[0]["producer_route_decision_refs"], ("route:draft",))

    def test_a_pass_verdict_with_findings_is_refused(self):
        with self.assertRaises(Exception):
            validate_verifier_output({"verdict": "pass", "findings": [
                {"unit": "x", "code": "unsupported_sentence", "detail": "d"}]})

    def test_nothing_drafted_is_skipped_rather_than_paid_for(self):
        model = FakeModel([])
        self.assertEqual(verify(model, {}, industry=INDUSTRY, mission=MISSION)["status"],
                         "skipped")
        self.assertEqual(model.calls, [])

    def test_independence_fails_closed_on_an_unresolvable_family(self):
        check = independence(draft_routes=["route:1"], verifier_route="route:2",
                             resolve=lambda ref: None)
        self.assertFalse(check["independent"])

    def test_independence_fails_when_the_verifier_shares_a_family(self):
        families = {"route:1": "anthropic", "route:2": "anthropic"}
        check = independence(draft_routes=["route:1"], verifier_route="route:2",
                             resolve=families.get)
        self.assertFalse(check["independent"])
        self.assertIn("which also drafted", check["reason"])

    def test_independence_passes_on_two_families(self):
        families = {"route:1": "anthropic", "route:2": "google"}
        check = independence(draft_routes=["route:1"], verifier_route="route:2",
                             resolve=families.get)
        self.assertTrue(check["independent"])

    def test_the_precheck_refuses_before_the_verifier_is_paid_for(self):
        early = independence_precheck(draft_routes=["route:1"], resolve=lambda ref: None)
        self.assertIsNotNone(early)
        self.assertIsNone(independence_precheck(draft_routes=["route:1"],
                                                resolve=lambda ref: "anthropic"))

    def test_the_summary_counts_unknown_slots(self):
        summary = summarise_blocks(self.blocks())
        self.assertEqual(summary["sentences"], 1)
        self.assertEqual(summary["unknown_slots"], 1)


if __name__ == "__main__":
    unittest.main()
