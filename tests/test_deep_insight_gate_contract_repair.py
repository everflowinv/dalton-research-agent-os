"""WP-C1-2 on the deep insight gate: a stray key is a shape, not an answer.

WP-D's reading of 101 live ``deep-insight-gate-runs`` summaries: 9 of IBM's and
CTSH's 12 questions were ``unknown``, and the direct cause was one refusal with
one shape, repeated::

    q9 has keys ['evidence_that_would_answer', 'gaps', 'missing',
                 'question_ref', 'refs', 'status']        (thesis group x5)
    q1 has keys [... the same ...]                        (industry group x3)

The model put a ``refs`` key on an *unknown* answer -- where refs mean nothing,
because an unknown cites nothing -- and ``parse_group_output`` refused all four
questions in that group.  Nothing about the evidence was wrong.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.cockpit_model import CockpitModelError
from dalton_core.deep_insight_gate import CONFIDENCE_LEVELS, GROUP_QUESTIONS
from dalton_core.deep_insight_gate_draft import (
    GateDraftRefused,
    draft_group,
    group_contract,
    group_contract_reminder,
    material_rows,
    parse_group_output,
)
from dalton_core.draft_contract_repair import check_contract

COMPANY = {"company_ref": "company:sec-cik:0000051143", "ticker": "IBM"}
MISSION = {"id": "mission-version:x", "content_hash": "b" * 64}
QUESTIONS = {ref: f"question {ref}?" for group in GROUP_QUESTIONS
             for ref in GROUP_QUESTIONS[group]}


def material():
    return material_rows(
        statements=[{"ref": "claim-version:1", "text": "需求在本季转正。",
                     "kind": "claim", "period": "FY2026Q1",
                     "importance": "filing"}],
        numbers=[{"ref": "figure:1", "kind": "figure", "text": "收入 165 亿美元",
                  "period": "FY2026Q1"}],
    )


def unknown(ref, **extra):
    return {"question_ref": ref, "status": "unknown",
            "missing": "没有可以回答这一题的材料",
            "evidence_that_would_answer": "IBM 的 10-K 分部披露",
            "gaps": [], **extra}


def answered(ref):
    return {"question_ref": ref, "status": "answered", "confidence": "medium",
            "sentences": [{"text": "需求在本季转正。", "refs": ["C1"]}],
            "gaps": []}


def reply(group, rows, **extra):
    body = {"answers": rows, **extra}
    if "q1" in GROUP_QUESTIONS[group]:
        body.setdefault("classification", "contract_compounder")
    return json.dumps(body, ensure_ascii=False)


class GroupContractTests(unittest.TestCase):
    def contract(self, group="thesis"):
        return group_contract(group, material=material())

    def test_the_live_refusal_is_named_as_one_stray_key(self):
        value = json.loads(reply("thesis", [
            unknown("q9", refs=["C1"]), unknown("q10"), unknown("q11"),
            unknown("q12")]))
        found = check_contract(value, self.contract())
        self.assertEqual([item.rule for item in found], ["shape"])
        self.assertIn("answers[0]", found[0].path)
        self.assertIn("'refs'", found[0].detail)
        # And it names both closed shapes, which is what tells the model to
        # drop the key rather than to invent the four others.
        self.assertIn("'evidence_that_would_answer'", found[0].detail)
        self.assertIn("'confidence'", found[0].detail)

    def test_the_same_refusal_on_the_industry_group(self):
        value = json.loads(reply("industry", [
            unknown("q1", refs=["C1"]), unknown("q2"), unknown("q3"),
            unknown("q4")]))
        found = check_contract(value, group_contract("industry", material=material()))
        self.assertEqual([item.rule for item in found], ["shape"])

    def test_a_clean_group_has_nothing_to_say(self):
        value = json.loads(reply("thesis", [
            answered("q9"), unknown("q10"), unknown("q11"), unknown("q12")]))
        self.assertEqual(check_contract(value, self.contract()), [])

    def test_the_other_deterministic_rules_are_covered(self):
        rows = [answered("q9"), unknown("q10"), unknown("q11"), unknown("q12")]
        cases = {
            "enum": json.loads(reply("thesis", [
                {**rows[0], "confidence": "very high"}, *rows[1:]])),
            "allowed_refs": json.loads(reply("thesis", [
                {**rows[0], "sentences": [{"text": "t", "refs": ["C9"]}]},
                *rows[1:]])),
            "min_items": json.loads(reply("thesis", rows[:3])),
            "keys": json.loads(reply("thesis", rows, commentary="unasked for")),
        }
        for rule, value in cases.items():
            found = check_contract(value, self.contract())
            self.assertIn(rule, [item.rule for item in found], rule)

    def test_the_reminder_tells_an_unknown_to_drop_the_key(self):
        reminder = group_contract_reminder("thesis")
        self.assertIn("carries no refs", reminder)
        self.assertIn("Drop any extra key", reminder)
        self.assertIn("never turn an unknown into an answer", reminder)
        self.assertIn(CONFIDENCE_LEVELS[0], reminder)


class FakeModel:
    def __init__(self, *texts, cost=1000, raises=None):
        self.texts = list(texts)
        self.cost = cost
        self.raises = raises
        self.calls: list[dict] = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.calls.append({"purpose": purpose, "request_id": request_id,
                           "prompt": prompt})
        if self.raises is not None:
            raise self.raises
        text = self.texts.pop(0) if self.texts else "{}"
        return {"text": text, "replayed": False, "cost_micros": self.cost,
                "work_order_ref": f"work:gate-{len(self.calls)}",
                "route_decision_ref": "route:1"}


class GroupRepairTests(unittest.TestCase):
    def rows(self):
        return [answered("q9"), unknown("q10"), unknown("q11"), unknown("q12")]

    def draft(self, model, group="thesis", **kwargs):
        return draft_group(
            model, group=group,
            questions={ref: QUESTIONS[ref] for ref in GROUP_QUESTIONS[group]},
            material=material(), company=COMPANY, mission=MISSION, **kwargs)

    def test_the_stray_key_is_repaired_once_and_the_group_survives(self):
        broken = reply("thesis", [{**self.rows()[0]}, unknown("q10", refs=["C1"]),
                                  unknown("q11"), unknown("q12")])
        good = reply("thesis", self.rows())
        model = FakeModel(broken, good)
        outcome = self.draft(model)
        self.assertEqual(outcome["status"], "drafted")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(outcome["contract_repair"]["status"], "repaired")
        self.assertEqual(outcome["contract_repair"]["repair_attempts"], 1)
        self.assertEqual(sorted(outcome["answers"]), ["q10", "q11", "q12", "q9"])
        # All four questions come back, which is the point: the group was
        # being refused whole over one key.
        self.assertEqual(outcome["model"]["cost_micros"], 2000)

    def test_the_repair_prompt_names_the_violation_and_both_shapes(self):
        broken = reply("thesis", [self.rows()[0], unknown("q10", refs=["C1"]),
                                  unknown("q11"), unknown("q12")])
        model = FakeModel(broken, reply("thesis", self.rows()))
        self.draft(model)
        repair = model.calls[1]["prompt"]
        self.assertIn("answers[1]", repair)
        self.assertIn("'refs'", repair)
        self.assertIn("carries no refs", repair)
        self.assertIn("CITABLE ROW TAGS", repair)
        # It is a repair, not a second draft: the question table is not resent.
        self.assertNotIn("Statements available", repair)
        self.assertLess(len(repair.encode("utf-8")),
                        len(model.calls[0]["prompt"].encode("utf-8")))

    def test_a_reply_that_stays_broken_is_refused_with_the_list(self):
        broken = reply("thesis", [self.rows()[0], unknown("q10", refs=["C1"]),
                                  unknown("q11"), unknown("q12")])
        model = FakeModel(broken, broken, reply("thesis", self.rows()))
        outcome = self.draft(model)
        self.assertEqual(outcome["status"], "refused")
        # One repair, not a loop: the third canned reply is never bought.
        self.assertEqual(len(model.calls), 2)
        self.assertIn("after one repair", outcome["reason"])
        self.assertEqual(outcome["model"]["cost_micros"], 2000)

    def test_a_clean_reply_buys_no_repair(self):
        model = FakeModel(reply("thesis", self.rows()))
        outcome = self.draft(model)
        self.assertEqual(outcome["status"], "drafted")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(outcome["contract_repair"]["repair_attempts"], 0)

    def test_a_repair_that_will_not_fit_the_run_budget_is_never_made(self):
        broken = reply("thesis", [self.rows()[0], unknown("q10", refs=["C1"]),
                                  unknown("q11"), unknown("q12")])
        model = FakeModel(broken, reply("thesis", self.rows()))
        outcome = self.draft(model, budget_remaining_micros=1_200,
                             repair_reserve_micros=1_000)
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(len(model.calls), 1)
        self.assertIn("run cost bound reached", outcome["reason"])

    def test_an_unavailable_model_is_still_unavailable(self):
        model = FakeModel(raises=CockpitModelError("no route"))
        outcome = self.draft(model)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("no route", outcome["reason"])

    def test_the_parser_still_refuses_the_stray_key_on_its_own(self):
        # The contract is checked *in addition to* the parser, never instead
        # of it: the closed shape is still closed.
        with self.assertRaisesRegex(GateDraftRefused, "an unknown answer is"):
            parse_group_output(
                reply("thesis", [self.rows()[0], unknown("q10", refs=["C1"]),
                                 unknown("q11"), unknown("q12")]),
                group="thesis",
                questions={ref: QUESTIONS[ref] for ref in GROUP_QUESTIONS["thesis"]},
                material=material())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
