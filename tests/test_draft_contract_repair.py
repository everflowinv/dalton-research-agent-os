"""WP-C1-2: the deterministic contract check, and exactly one repair."""

from __future__ import annotations

import json
import unittest

from dalton_core.draft_contract_repair import (
    MAX_REPAIR_PROMPT_BYTES,
    Contract,
    ContractRepairError,
    Violation,
    build_repair_prompt,
    check_contract,
    repair_request_id,
    run_with_contract_repair,
    single_json_object,
    violations_of,
)


class Refused(ValueError):
    """Stands in for DossierDraftRefused / DebateDraftRefused."""


class Insufficient(ValueError):
    """Stands in for an honest "the material does not answer this"."""


class Unavailable(RuntimeError):
    """Stands in for CockpitModelError."""


SLOT_CONTRACT = Contract(
    name="test-contract:0.1",
    keys={"": ({"slots", "gaps"}, frozenset())},
    shapes={"slots[]": ({"slot_id", "sentences"}, {"slot_id", "unknown"})},
    max_items={"slots[].sentences": 3, "gaps": 2},
    min_items={"slots[].sentences[].refs": 1},
    max_chars={"slots[].sentences[].text": 40},
    enums={"slots[].slot_id": ("state", "why")},
    allowed_refs={"slots[].sentences[].refs": ("C1", "C2")},
    nonempty=("slots",),
)


def sentence(text="预订量转正。", refs=("C1",)):
    return {"text": text, "refs": list(refs)}


def reply(slots, gaps=(), **extra):
    return json.dumps({"slots": slots, "gaps": list(gaps), **extra},
                      ensure_ascii=False)


class FakeModel:
    """Canned replies in order, and a record of what it was asked."""

    def __init__(self, *texts, cost=1000, raises=None):
        self.texts = list(texts)
        self.cost = cost
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, *, prompt, request_id):
        if self.raises is not None and not self.calls:
            self.calls.append({"prompt": prompt, "request_id": request_id})
            raise self.raises
        self.calls.append({"prompt": prompt, "request_id": request_id})
        text = self.texts.pop(0) if self.texts else "{}"
        return {"text": text, "cost_micros": self.cost,
                "work_order_ref": f"work:{len(self.calls)}"}


def parse(text):
    value, problems = single_json_object(text)
    if value is None:
        raise Refused(problems[0].detail)
    found = check_contract(value, SLOT_CONTRACT)
    if found:
        raise Refused(found[0].line())
    if value["slots"] and all("unknown" in slot for slot in value["slots"]):
        raise Insufficient("the material answers nothing")
    return value


GOOD = reply([{"slot_id": "state", "sentences": [sentence()]}])


class DeterministicCheckTests(unittest.TestCase):
    def test_it_collects_every_violation_rather_than_the_first(self):
        # The whole point: a model told about slots[0] returns a reply whose
        # slots[1] is still wrong, and buys a second refusal.
        value = {
            "slots": [
                {"slot_id": "state", "sentences": [sentence()], "unknown": "x"},
                {"slot_id": "invented", "sentences": [sentence(), sentence(),
                                                      sentence(), sentence()]},
            ],
            "gaps": [],
            "commentary": "unasked for",
        }
        rules = {item.rule for item in check_contract(value, SLOT_CONTRACT)}
        self.assertEqual(rules, {"keys", "shape", "enum", "max_items"})

    def test_a_slot_carrying_both_sentences_and_unknown_is_named_exactly(self):
        # The commonest live violation on 2026-09-16.
        found = check_contract(
            {"slots": [{"slot_id": "state", "sentences": [sentence()],
                        "unknown": "x"}], "gaps": []},
            SLOT_CONTRACT)
        self.assertEqual([item.rule for item in found], ["shape"])
        self.assertIn("slots[0]", found[0].path)
        self.assertIn("'sentences', 'slot_id'", found[0].detail)

    def test_a_stray_key_on_a_slot_is_named_with_the_contract(self):
        found = check_contract(
            {"slots": [{"slot_id": "state", "sentences": [sentence()],
                        "refs": ["C1"]}], "gaps": []},
            SLOT_CONTRACT)
        self.assertIn("'refs'", found[0].detail)

    def test_a_citation_that_was_not_shown_names_what_may_be_cited(self):
        found = check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [sentence(refs=["C9"])]}], "gaps": []},
            SLOT_CONTRACT)
        self.assertEqual(found[0].rule, "allowed_refs")
        self.assertIn("C1, C2", found[0].detail)

    def test_a_character_cap_counts_characters_and_says_so(self):
        found = check_contract(
            {"slots": [{"slot_id": "state",
                        "sentences": [sentence(text="x" * 41)]}], "gaps": []},
            SLOT_CONTRACT)
        self.assertEqual(found[0].rule, "max_chars")
        self.assertIn("41 characters", found[0].detail)
        self.assertIn("the cap is 40", found[0].detail)

    def test_a_reply_that_is_not_one_json_object_says_why(self):
        value, found = single_json_object("here you go: {\"slots\": [,]}")
        self.assertIsNone(value)
        self.assertEqual(found[0].rule, "single_json_object")
        self.assertIn("not valid JSON", found[0].detail)

    def test_a_fenced_object_is_the_object(self):
        value, found = single_json_object("```json\n{\"slots\": []}\n```")
        self.assertEqual(value, {"slots": []})
        self.assertEqual(found, [])

    def test_the_semantic_validators_own_message_survives_beside_the_list(self):
        found = violations_of(
            reply([{"slot_id": "state", "sentences": [sentence(refs=["C9"])]}]),
            SLOT_CONTRACT, parse_error=Refused("market_view cites the company"))
        self.assertIn("allowed_refs", [item.rule for item in found])
        self.assertIn("validator", [item.rule for item in found])


class RepairPromptTests(unittest.TestCase):
    def test_it_does_not_repeat_the_drafting_prompt(self):
        prompt = build_repair_prompt(
            original_prompt="THE WHOLE EXPENSIVE TABLE" * 500,
            reply_text=GOOD,
            violations=[Violation("slots[0]", "shape", "has keys x")])
        self.assertNotIn("THE WHOLE EXPENSIVE TABLE", prompt)
        self.assertIn("slots[0]", prompt)

    def test_it_stays_inside_its_bound_without_splitting_a_character(self):
        prompt = build_repair_prompt(
            original_prompt="", reply_text="预" * 40_000,
            violations=[Violation("slots", "max_items", "too many")])
        self.assertLessEqual(len(prompt.encode("utf-8")), MAX_REPAIR_PROMPT_BYTES)
        self.assertIn("truncated here", prompt)
        prompt.encode("utf-8").decode("utf-8")  # no half character survived

    def test_a_bound_too_small_for_the_rules_refuses_rather_than_redrafts(self):
        with self.assertRaises(ContractRepairError):
            build_repair_prompt(
                original_prompt="", reply_text=GOOD,
                violations=[Violation("slots", "max_items", "x")], max_bytes=10)

    def test_the_repair_identity_is_content_addressed_on_the_violations(self):
        one = [Violation("slots[0]", "shape", "a")]
        two = [Violation("slots[1]", "shape", "b")]
        self.assertEqual(
            repair_request_id("r", contract_name="c", violations=one),
            repair_request_id("r", contract_name="c", violations=one))
        self.assertNotEqual(
            repair_request_id("r", contract_name="c", violations=one),
            repair_request_id("r", contract_name="c", violations=two))


class RepairRunTests(unittest.TestCase):
    def run_it(self, model, **kwargs):
        return run_with_contract_repair(
            call=model, parse=parse, prompt="DRAFT", request_id="req-1",
            contract=SLOT_CONTRACT, refusal_errors=(Refused,),
            passthrough_errors=(Insufficient,), unavailable_errors=(Unavailable,),
            contract_reminder="* one slot per slot_id", **kwargs)

    def test_a_reply_that_holds_the_contract_costs_one_call(self):
        model = FakeModel(GOOD)
        outcome = self.run_it(model)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(outcome.cost_micros, 1000)

    def test_a_broken_shape_is_repaired_once_and_accepted(self):
        broken = reply([{"slot_id": "state", "sentences": [sentence()],
                         "unknown": "both at once"}])
        model = FakeModel(broken, GOOD)
        outcome = self.run_it(model)
        self.assertEqual(outcome.status, "repaired")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(outcome.cost_micros, 2000)
        self.assertIn("slots[0]", model.calls[1]["prompt"])
        self.assertIn("one slot per slot_id", model.calls[1]["prompt"])
        self.assertEqual(outcome.summary()["repair_attempts"], 1)

    def test_it_repairs_once_and_then_refuses_with_the_whole_list(self):
        broken = reply([{"slot_id": "state", "sentences": [sentence(refs=["C9"])]}])
        model = FakeModel(broken, broken, GOOD)
        outcome = self.run_it(model)
        self.assertEqual(outcome.status, "refused")
        # One repair, not a loop: the third canned reply is never bought.
        self.assertEqual(len(model.calls), 2)
        self.assertIn("allowed_refs", [item.rule for item in outcome.violations])
        self.assertIn("after one repair", outcome.reason)

    def test_a_repair_that_will_not_fit_the_run_budget_is_never_made(self):
        broken = reply([{"slot_id": "state", "sentences": [sentence(refs=["C9"])]}])
        model = FakeModel(broken, GOOD)
        outcome = self.run_it(model, budget_remaining_micros=1_500,
                              repair_reserve_micros=1_000)
        self.assertEqual(outcome.status, "budget_refused")
        self.assertEqual(len(model.calls), 1)
        self.assertIn("run cost bound reached", outcome.reason)
        self.assertIn("500 micros left", outcome.reason)

    def test_a_repair_that_does_fit_the_run_budget_is_made(self):
        broken = reply([{"slot_id": "state", "sentences": [sentence(refs=["C9"])]}])
        model = FakeModel(broken, GOOD)
        outcome = self.run_it(model, budget_remaining_micros=5_000,
                              repair_reserve_micros=1_000)
        self.assertEqual(outcome.status, "repaired")

    def test_an_honest_insufficient_answer_is_never_repaired(self):
        model = FakeModel(reply([{"slot_id": "state", "unknown": "材料没有回答"}]))
        with self.assertRaises(Insufficient):
            self.run_it(model)
        self.assertEqual(len(model.calls), 1)

    def test_an_unavailable_model_is_not_a_contract_violation(self):
        model = FakeModel(GOOD, raises=Unavailable("no route"))
        outcome = self.run_it(model)
        self.assertEqual(outcome.status, "unavailable")
        self.assertIn("no route", outcome.reason)

    def test_an_unavailable_repair_refuses_with_the_original_violations(self):
        broken = reply([{"slot_id": "state", "sentences": [sentence(refs=["C9"])]}])

        class OneThenGone(FakeModel):
            def __call__(self, *, prompt, request_id):
                if self.calls:
                    self.calls.append({"prompt": prompt, "request_id": request_id})
                    raise Unavailable("the pool is spent")
                return super().__call__(prompt=prompt, request_id=request_id)

        outcome = self.run_it(OneThenGone(broken))
        self.assertEqual(outcome.status, "refused")
        self.assertIn("the pool is spent", outcome.reason)

    def test_the_repair_goes_to_the_same_call_closure(self):
        # No model substitution: this module never picks a model, so a run
        # routed to a degraded family stays on it for its repair.
        broken = reply([{"slot_id": "state", "sentences": [sentence(refs=["C9"])]}])
        model = FakeModel(broken, GOOD)
        self.run_it(model)
        self.assertEqual(len(model.calls), 2)
        self.assertNotEqual(model.calls[0]["request_id"], model.calls[1]["request_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
