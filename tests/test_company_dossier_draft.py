"""P12a drafting: refused whole, never repaired, and never verified by itself.

The contract is small on purpose, and each way of leaving it has its own test,
because the failure that matters is the one that gets *repaired* rather than
refused. A reply that invented a tag did not read the table, and the rows it
happened to get right came out of the same reply; keeping them is keeping a
sample of an unreliable process and calling it evidence.

The independence check is the other half. D2 asks that a cognition-layer output
be verified by something that is not the thing that wrote it, and the family
that served is a fact about the route decision rather than about what was
asked for -- so it is read back afterwards, and an unresolvable family counts
as not independent. A verification whose independence is unknown is worth
exactly what no verification is worth.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.company_dossier import CLASSIFICATION_SLOTS, VARIANT_SLOTS
from dalton_core.company_dossier_draft import (
    DRAFT_PURPOSE,
    SLOT_SENTENCE_CAP,
    DossierDraftRefused,
    build_unit_prompt,
    build_verifier_prompt,
    draft_hash,
    draft_unit,
    independence,
    independence_precheck,
    material_rows,
    parse_unit_output,
    render_material,
    validate_verifier_output,
    verify,
)
from dalton_core.cockpit_model import purposes

COMPANY = {"company_ref": "company:sec-cik:0001467373", "ticker": "ACN"}
MISSION = {"id": "mission-version:1", "mission_ref": "mission:x",
           "content_hash": "a" * 64, "created_at": "2026-09-01T00:00:00+00:00",
           "budget": {"max_daily_paid_calls": 10, "max_daily_cost_usd": 1}}
STRUCTURE = ({"slot_id": "causal_chain:0", "prompt": "Bookings lead revenue."},
             {"slot_id": "causal_chain:1", "prompt": "AI budgets are a new pool."})


def material():
    return material_rows(
        [{"ref": "claim-version:a", "text": "管理层说预订量在本季转正",
          "period": "2026Q2", "importance": "management_statement"},
         {"ref": "claim-version:b", "text": "研报称 AI 预算在扩张",
          "period": "2026Q2", "importance": "sell_side"}],
        [{"kind": "figure", "ref": "statement-line:1",
          "text": "Revenues for the period ending 2026-05-31 was 18718144000 USD",
          "period": "2026-05-31"}],
    )


def reply(slots, gaps=(), **extra):
    return json.dumps({"slots": slots, "gaps": list(gaps), **extra}, ensure_ascii=False)


def one_sentence(slot_id, refs=("C1",), text="预订量在本季转正，这是需求的领先信号。"):
    return {"slot_id": slot_id, "sentences": [{"text": text, "refs": list(refs)}]}


class FakeModel:
    """Canned replies in order, and a record of what it was asked."""

    def __init__(self, *texts, route="route:1"):
        self.texts = list(texts)
        self.route = route
        self.prompts: list[str] = []
        self.purposes: list[str] = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.prompts.append(prompt)
        self.purposes.append(purpose)
        text = self.texts.pop(0) if self.texts else "{}"
        return {"text": text, "replayed": False, "cost_micros": 4321,
                "work_order_ref": f"work:cockpit-dossier-{len(self.prompts)}",
                "route_decision_ref": self.route}


class PromptTests(unittest.TestCase):
    def test_the_purpose_is_registered_by_importing_the_drafter(self):
        self.assertEqual(DRAFT_PURPOSE, "dossier")
        self.assertIn("dossier", purposes())

    def test_the_material_is_a_table_and_not_an_object_graph(self):
        rendered = render_material(material())
        self.assertIn("C1\t2026Q2\tmanagement_statement\t管理层说预订量在本季转正", rendered)
        self.assertIn("N1\t2026-05-31\tfigure\t", rendered)
        self.assertNotIn("{", rendered)

    def test_the_prompt_states_the_slots_and_forbids_tags_in_prose(self):
        prompt = build_unit_prompt(unit="demand_drivers", structure=STRUCTURE,
                                   material=material(), company=COMPANY)
        self.assertIn("causal_chain:0\tBookings lead revenue.", prompt)
        self.assertIn("NEVER write a C or N tag inside a sentence's text", prompt)
        self.assertIn("Every sentence must cite at least one tag", prompt)
        self.assertIn(f"At most {SLOT_SENTENCE_CAP} sentences in a slot", prompt)

    def test_the_classification_prompt_carries_the_closed_vocabulary(self):
        prompt = build_unit_prompt(
            unit="industry_classification",
            structure=[{"slot_id": slot, "prompt": slot} for slot in CLASSIFICATION_SLOTS],
            material=material(), company=COMPANY)
        self.assertIn("contract_compounder\t", prompt)
        self.assertIn("insufficient_evidence\t", prompt)

    def test_the_variant_prompt_says_there_is_no_market_view_when_there_is_none(self):
        prompt = build_unit_prompt(
            unit="variant_view",
            structure=[{"slot_id": slot, "prompt": slot} for slot in VARIANT_SLOTS
                       if slot != "market_view"],
            material=material(), company=COMPANY, market_view_available=False)
        self.assertIn("Do not speculate about what the market thinks", prompt)

    def test_the_guidance_prompt_hands_over_a_computed_table(self):
        prompt = build_unit_prompt(
            unit="guidance_style",
            structure=[{"slot_id": "guidance_style", "prompt": "how they guide"}],
            material=material(), company=COMPANY,
            profile_table="period\tmeasure\nQ1\trevenue_growth")
        self.assertIn("already computed and is not yours to change", prompt)
        self.assertIn("period\tmeasure", prompt)


class ReplyContractTests(unittest.TestCase):
    def parse(self, text, **kwargs):
        return parse_unit_output(text, unit="demand_drivers", structure=STRUCTURE,
                                 material=material(), **kwargs)

    def test_a_conforming_reply_becomes_a_section(self):
        section = self.parse(reply([
            one_sentence("causal_chain:0", ["C1", "N1"]),
            {"slot_id": "causal_chain:1", "unknown": "没有关于 AI 预算池的一手材料"},
        ]))
        self.assertEqual(section["status"], "drafted")
        self.assertEqual([row["ref"] for row in section["sources"]],
                         ["claim-version:a", "statement-line:1"])
        self.assertEqual(section["slots"][1]["unknown"],
                         "没有关于 AI 预算池的一手材料")

    def test_a_tag_that_was_never_shown_refuses_the_whole_reply(self):
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([one_sentence("causal_chain:0", ["C9"]),
                              {"slot_id": "causal_chain:1", "unknown": "x"}]))
        self.assertIn("refused whole", str(caught.exception))

    def test_a_sentence_that_cites_nothing_refuses_the_whole_reply(self):
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([
                {"slot_id": "causal_chain:0",
                 "sentences": [{"text": "需求在恢复。", "refs": []}]},
                {"slot_id": "causal_chain:1", "unknown": "x"}]))
        self.assertIn("cites nothing", str(caught.exception))

    def test_a_slot_that_is_neither_filled_nor_unknown_refuses(self):
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([{"slot_id": "causal_chain:0", "sentences": [],
                               "note": "留空"},
                              {"slot_id": "causal_chain:1", "unknown": "x"}]))
        self.assertIn("the contract is", str(caught.exception))

    def test_a_missing_slot_refuses(self):
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([one_sentence("causal_chain:0")]))
        self.assertIn("every slot", str(caught.exception))

    def test_a_slot_the_structure_never_named_refuses(self):
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([one_sentence("causal_chain:0"),
                              one_sentence("my_own_slot")]))
        self.assertIn("not the model's to choose", str(caught.exception))

    def test_more_sentences_than_the_cap_refuses(self):
        rows = [{"text": f"第 {index} 句。", "refs": ["C1"]}
                for index in range(SLOT_SENTENCE_CAP + 1)]
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([{"slot_id": "causal_chain:0", "sentences": rows},
                              {"slot_id": "causal_chain:1", "unknown": "x"}]))
        self.assertIn("the cap is", str(caught.exception))

    def test_a_key_the_contract_does_not_have_refuses(self):
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse(reply([one_sentence("causal_chain:0"),
                              {"slot_id": "causal_chain:1", "unknown": "x"}],
                             confidence="high"))
        self.assertIn("the contract is", str(caught.exception))

    def test_prose_around_the_json_is_not_a_reply(self):
        with self.assertRaises(DossierDraftRefused):
            self.parse("这是我的答案：不确定。")

    def test_the_classification_must_be_one_of_the_five_words(self):
        structure = [{"slot_id": slot, "prompt": slot} for slot in CLASSIFICATION_SLOTS]
        good = parse_unit_output(
            reply([one_sentence(CLASSIFICATION_SLOTS[0]),
                   one_sentence(CLASSIFICATION_SLOTS[1])],
                  classification="contract_compounder"),
            unit="industry_classification", structure=structure, material=material())
        self.assertEqual(good["classification"], "contract_compounder")
        with self.assertRaises(DossierDraftRefused):
            parse_unit_output(
                reply([one_sentence(CLASSIFICATION_SLOTS[0]),
                       one_sentence(CLASSIFICATION_SLOTS[1])],
                      classification="a_quality_compounder"),
                unit="industry_classification", structure=structure, material=material())


class MarketViewTests(unittest.TestCase):
    """Only the market may speak for the market."""

    def structure(self):
        return [{"slot_id": slot, "prompt": slot} for slot in VARIANT_SLOTS]

    def parse(self, tag):
        slots = [one_sentence(slot) if slot != "market_view"
                 else one_sentence("market_view", [tag], "市场付的是这个价。")
                 for slot in VARIANT_SLOTS]
        return parse_unit_output(reply(slots), unit="variant_view",
                                 structure=self.structure(), material=material())

    def test_a_sell_side_claim_may_fill_the_market_view(self):
        block = self.parse("C2")
        self.assertTrue(block["market_view_available"])

    def test_a_management_statement_may_not(self):
        # The company talking is not the street talking, and citing it as the
        # market's view turns a variant view into the company's own case with
        # the disagreement invented.
        with self.assertRaises(DossierDraftRefused) as caught:
            self.parse("C1")
        self.assertIn("the company talking", str(caught.exception))

    def test_a_filed_figure_may_not_either(self):
        with self.assertRaises(DossierDraftRefused):
            self.parse("N1")


class DraftCallTests(unittest.TestCase):
    def test_one_bounded_call_per_unit_on_the_dossier_purpose(self):
        model = FakeModel(reply([one_sentence("causal_chain:0"),
                                 {"slot_id": "causal_chain:1", "unknown": "x"}]))
        outcome = draft_unit(model, unit="demand_drivers", structure=STRUCTURE,
                             material=material(), company=COMPANY, mission=MISSION)
        self.assertEqual(outcome["status"], "drafted")
        self.assertEqual(model.purposes, ["dossier"])
        self.assertEqual(outcome["model"]["cost_micros"], 4321)

    def test_a_refused_reply_is_reported_and_nothing_is_kept(self):
        model = FakeModel(reply([one_sentence("causal_chain:0", ["C9"]),
                                 {"slot_id": "causal_chain:1", "unknown": "x"}]))
        outcome = draft_unit(model, unit="demand_drivers", structure=STRUCTURE,
                             material=material(), company=COMPANY, mission=MISSION)
        self.assertEqual(outcome["status"], "refused")
        self.assertNotIn("block", outcome)

    def test_the_previous_version_is_shown_so_the_new_one_can_advance(self):
        model = FakeModel(reply([one_sentence("causal_chain:0"),
                                 {"slot_id": "causal_chain:1", "unknown": "x"}]))
        draft_unit(model, unit="demand_drivers", structure=STRUCTURE,
                   material=material(), company=COMPANY, mission=MISSION,
                   prior_body="上一版说预订量还在下滑。")
        self.assertIn("上一版说预订量还在下滑。", model.prompts[0])


class VerifierTests(unittest.TestCase):
    def block(self):
        return parse_unit_output(
            reply([one_sentence("causal_chain:0"),
                   {"slot_id": "causal_chain:1", "unknown": "x"}]),
            unit="demand_drivers", structure=STRUCTURE, material=material())

    def test_the_verdict_is_bound_to_the_draft_it_read(self):
        blocks = {"demand_drivers": self.block()}
        model = FakeModel(json.dumps({"verdict": "pass", "findings": []}))
        verdict = verify(model, blocks, company=COMPANY, mission=MISSION)
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["verified_draft_hash"], draft_hash(blocks))

    def test_the_verifier_sees_the_sentences_and_the_rows_they_cite(self):
        blocks = {"demand_drivers": self.block()}
        prompt = build_verifier_prompt(blocks, company=COMPANY)
        self.assertIn("cites: 管理层说预订量在本季转正", prompt)
        self.assertIn("(unknown)", prompt)

    def test_a_pass_with_findings_is_not_a_verdict(self):
        with self.assertRaises(DossierDraftRefused):
            validate_verifier_output({"verdict": "pass", "findings": [
                {"unit": "demand_drivers", "code": "unsupported_sentence",
                 "detail": "一句话超出了它引用的材料"}]})
        with self.assertRaises(DossierDraftRefused):
            validate_verifier_output({"verdict": "reject", "findings": []})
        with self.assertRaises(DossierDraftRefused):
            validate_verifier_output({"verdict": "pass", "findings": [], "note": "x"})

    def test_a_finding_code_outside_the_closed_list_is_refused(self):
        with self.assertRaises(DossierDraftRefused):
            validate_verifier_output({"verdict": "reject", "findings": [
                {"unit": "demand_drivers", "code": "vibes", "detail": "读起来不对"}]})


class IndependenceTests(unittest.TestCase):
    families = {"route:draft": "family-a", "route:other": "family-b"}

    def resolve(self, ref):
        return self.families.get(ref)

    def test_a_different_family_is_independent(self):
        check = independence(draft_routes=["route:draft"], verifier_route="route:other",
                             resolve=self.resolve)
        self.assertTrue(check["independent"])
        self.assertEqual(check["verifier_family"], "family-b")

    def test_the_same_family_is_not(self):
        check = independence(draft_routes=["route:draft"], verifier_route="route:draft",
                             resolve=self.resolve)
        self.assertFalse(check["independent"])
        self.assertIn("also drafted", check["reason"])

    def test_an_unresolvable_family_fails_closed(self):
        for draft, verifier in (("route:draft", "route:missing"),
                                ("route:missing", "route:other"),
                                (None, "route:other")):
            check = independence(draft_routes=[draft], verifier_route=verifier,
                                 resolve=self.resolve)
            self.assertFalse(check["independent"], (draft, verifier))
            self.assertIn("could not be resolved", check["reason"])

    def test_one_drafting_call_on_the_verifiers_family_is_enough_to_fail(self):
        check = independence(draft_routes=["route:other", "route:draft"],
                             verifier_route="route:other", resolve=self.resolve)
        self.assertFalse(check["independent"])

    def test_the_precheck_refuses_before_the_second_call_is_paid_for(self):
        # The drafting families are already knowable when the draft is done,
        # so a verification that could not be independent is not worth making.
        self.assertIsNone(independence_precheck(draft_routes=["route:draft"],
                                                resolve=self.resolve))
        early = independence_precheck(draft_routes=["route:draft", "route:missing"],
                                      resolve=self.resolve)
        self.assertFalse(early["independent"])
        self.assertIn("could not be resolved", early["reason"])
        self.assertFalse(independence_precheck(draft_routes=[],
                                               resolve=self.resolve)["independent"])


if __name__ == "__main__":
    unittest.main()
