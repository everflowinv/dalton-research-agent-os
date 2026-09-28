"""2026-09-27 (contract v3): direct conclusions are supported; a rejection needs two families.

Legacy's v2 rejections of 2026-09-27 still included statements the cited text
gave in so many words -- EPAM "the software and high tech decline was due to
non-AI ramp-downs outweighing AI growth" against the call's own "project ramp
downs concentrated in non-AI services, which outweighed the growth in AI".
The question now says what a directly drawn conclusion is, with examples
either way, and a first answer that does not admit a statement is put to a
model of another family before it counts.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_support_verification import (
    CONTRACT_REF,
    ClaimSupportError,
    ClaimSupportVerifier,
    build_prompt,
    load_settings,
    no_link_outside,
)
from tests.test_claim_support_verification import MISSION, Clock, _item, _reply, _store


class RoutedModel:
    """A fake model whose every call is served by the next family in line."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        n = len(self.calls)
        return {"text": reply, "cost_micros": 700, "work_order_ref": f"work:cockpit-x-{n}",
                "route_decision_ref": f"route-decision:verifier-{n}", "invocation_ref": f"invocation:{n}"}


FAMILIES = {"route-decision:producer": "deepseek-v4", "route-decision:verifier-1": "google-gemini-3",
            "route-decision:verifier-2": "anthropic-claude", "route-decision:verifier-3": "google-gemini-3"}
YES = ("supported", "about_subject", None)
NO = ("not_supported", "about_subject", None)


class PromptTests(unittest.TestCase):
    def test_v3_says_a_directly_drawn_conclusion_is_supported_with_examples_either_way(self) -> None:
        self.assertEqual(CONTRACT_REF, "claim-support-verification:v4")
        prompt = build_prompt([_item(1)])
        self.assertIn("(d) a conclusion that one passage of the cited_text states in other words", prompt)
        # The positive examples: the EPAM vertical and a paraphrased analyst view.
        self.assertIn("ramp-downs that outweigh the growth are a decline", prompt)
        self.assertIn("Fred sees the sell-off as an overreaction", prompt)
        # The negative ones: an unstated cause, a hedge made firm, a part made
        # the whole, an owner the cited text does not show.
        self.assertIn("revenue fell because hiring slowed", prompt)
        self.assertIn("'will raise guidance'", prompt)
        self.assertIn("a heading or company name outside cited_text is not shown", prompt)
        # Still inside the forward purpose's input bound with a full window.
        full = build_prompt([_item(n, statement="Amazon " + "x" * 1900) for n in range(12)])
        self.assertLess(len(full.encode("utf-8")), 48000)


class SecondOpinionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _store()
        self.addCleanup(self.store.connection.close)
        self.spent = 0

    def verifier(self, model, **kwargs):
        options = dict(store=self.store, model_call=model, daily_cap_micros=100_000,
                       spend_today=lambda _p, _d: self.spent, clock=Clock(),
                       producer_family=lambda ref: FAMILIES.get(ref, "deepseek-v4"),
                       second_opinion=True, second_opinion_route=lambda families: None)
        options.update(kwargs)
        return ClaimSupportVerifier(**options)

    def test_a_rejection_the_second_family_admits_is_supported_and_says_so(self) -> None:
        model = RoutedModel(_reply(YES, NO), _reply(YES))
        items = [_item(1), _item(2)]
        outcome = self.verifier(model).verify(mission=MISSION, items=items)
        self.assertEqual((outcome["status"], outcome["calls"]), ("verified", 2))
        # Only the rejected statement went to the second call, and the router
        # was told to be independent of the first verifier as well.
        second = model.calls[1]
        self.assertEqual(len(json.loads(second["prompt"].split("UNTRUSTED_ITEMS=")[1])), 1)
        self.assertEqual(second["producer_route_decision_refs"],
                         ["route-decision:producer", "route-decision:verifier-1"])
        self.assertTrue(second["request_id"].split(":")[1] == "second")
        verdict = outcome["verdicts"][items[1]["item_key"]]
        self.assertEqual((verdict["support"], verdict["route_decision_ref"]),
                         ("supported", "route-decision:verifier-2"))
        self.assertEqual(verdict["first_opinion"]["support"], "not_supported")
        self.assertEqual(verdict["second_opinion"]["status"], "answered")
        self.assertEqual(outcome["second_opinions"],
                         {"asked": 1, "overturned": 1, "confirmed": 0, "unavailable": None})
        # The first statement needed no second opinion.
        self.assertNotIn("second_opinion", outcome["verdicts"][items[0]["item_key"]])

    def test_both_families_rejecting_is_a_rejection(self) -> None:
        model = RoutedModel(_reply(NO), _reply(("not_supported", "about_other", "Capgemini")))
        [item] = [_item(1)]
        outcome = self.verifier(model).verify(mission=MISSION, items=[item])
        verdict = outcome["verdicts"][item["item_key"]]
        # The first answer stands, with the second on the record.
        self.assertEqual((verdict["support"], verdict["subject_relation"], verdict["route_decision_ref"]),
                         ("not_supported", "about_subject", "route-decision:verifier-1"))
        self.assertEqual(verdict["second_opinion"]["other_subject"], "Capgemini")
        self.assertEqual(outcome["second_opinions"]["confirmed"], 1)
        # Stored: asking again costs nothing.
        again = self.verifier(RoutedModel()).verify(mission=MISSION, items=[item])
        self.assertEqual((again["calls"], again["verdicts"][item["item_key"]]["support"]),
                         (0, "not_supported"))

    def test_a_second_opinion_that_must_wait_never_pays_for_the_first_again(self) -> None:
        model = RoutedModel(_reply(NO))
        [item] = [_item(1)]
        verifier = self.verifier(model)
        real = verifier.spend_today
        calls = {"n": 0}

        def spend(purpose, day):  # the first call fits; the ceiling is reached before the second
            calls["n"] += 1
            return 0 if calls["n"] == 1 else 100_000
        verifier.spend_today = spend
        outcome = verifier.verify(mission=MISSION, items=[item])
        self.assertEqual((outcome["status"], outcome["verdicts"]), ("deferred", {}))
        self.assertIn("ceiling is reached", outcome["reason"])
        # Next run, room again: one call, the second opinion only.
        verifier.spend_today = real
        later = RoutedModel(_reply(YES))
        verifier.model_call = later
        outcome = verifier.verify(mission=MISSION, items=[item])
        self.assertEqual((outcome["status"], len(later.calls)), ("verified", 1))
        self.assertIn("route-decision:verifier-1", later.calls[0]["producer_route_decision_refs"])
        self.assertEqual(outcome["verdicts"][item["item_key"]]["support"], "supported")

    def test_with_no_other_family_in_the_chain_the_first_answer_stands_and_says_why(self) -> None:
        model = RoutedModel(_reply(NO))
        [item] = [_item(1)]
        why = "no model of the claim_support_verifier chain that can verify (p) is outside ..."
        outcome = self.verifier(model, second_opinion_route=lambda families: why).verify(
            mission=MISSION, items=[item])
        verdict = outcome["verdicts"][item["item_key"]]
        self.assertEqual((len(model.calls), verdict["support"]), (1, "not_supported"))
        self.assertEqual(verdict["second_opinion"], {"status": "unavailable", "reason": why})
        self.assertEqual(outcome["second_opinions"]["unavailable"], why)

    def test_a_router_refusal_for_independence_keeps_the_first_answer(self) -> None:
        from dalton_core.cockpit_model import CockpitModelError

        refusal = CockpitModelError(
            "no model left for claim_support_verifier is independent of the deepseek-v4, "
            "google-gemini-3 family set that produced the work being checked")
        model = RoutedModel(_reply(NO), refusal)
        [item] = [_item(1)]
        outcome = self.verifier(model).verify(mission=MISSION, items=[item])
        verdict = outcome["verdicts"][item["item_key"]]
        self.assertEqual((outcome["status"], verdict["support"], verdict["second_opinion"]["status"]),
                         ("verified", "not_supported", "unavailable"))

    def test_off_is_the_single_answer_as_before(self) -> None:
        model = RoutedModel(_reply(NO))
        [item] = [_item(1)]
        outcome = self.verifier(model, second_opinion=False).verify(mission=MISSION, items=[item])
        self.assertEqual((len(model.calls), outcome["verdicts"][item["item_key"]]["support"]),
                         (1, "not_supported"))
        self.assertNotIn("second_opinion", outcome["verdicts"][item["item_key"]])


class WiringTests(unittest.TestCase):
    def test_the_setting_is_a_boolean_and_on_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(load_settings(temp)["second_opinion_on_rejection"])
            (Path(temp) / "claim-support-verification.json").write_text(
                '{"second_opinion_on_rejection": false}', encoding="utf-8")
            self.assertFalse(load_settings(temp)["second_opinion_on_rejection"])
            (Path(temp) / "claim-support-verification.json").write_text(
                '{"second_opinion_on_rejection": "yes"}', encoding="utf-8")
            with self.assertRaises(ClaimSupportError):
                load_settings(temp)

    def test_a_chain_of_one_verify_capable_family_has_no_second_opinion(self) -> None:
        live = [("profile:gemini-3-8-flash", "google-gemini-3"),
                ("profile:gemini-3-1-pro-preview", "google-gemini-3")]
        involved = frozenset({"deepseek-v4", "google-gemini-3"})
        self.assertIn("is outside the families", no_link_outside(live, involved, purpose="p"))
        self.assertIsNone(no_link_outside([*live, ("profile:claude", "anthropic-claude")],
                                          involved, purpose="p"))


if __name__ == "__main__":
    unittest.main()
