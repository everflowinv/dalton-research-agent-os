"""2026-09-24: an independent cheap-model support check before a statement is admitted.

The deterministic repairs made the citation the sentence and held spans that
never name the company.  What they cannot see is whether the statement says
what the sentence says, or is really about somebody else.  One cheap call per
window answers that for every statement of the window; anything but
supported-and-about-the-subject is held for a person, and a check that cannot
run keeps the window out of the Ledger.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import document_extraction_cli
from dalton_core.claim_support_verification import (
    BACKFILL_PURPOSE,
    MAX_ATTEMPTS,
    PURPOSE,
    ClaimSupportError,
    ClaimSupportVerifier,
    build_prompt,
    hold_reason,
    item_key,
    load_settings,
    parse_verdicts,
    purpose_spend_micros,
    support_item,
)
from dalton_core.cockpit_model import (
    CockpitModelError,
    CockpitModelPoolExhausted,
    CockpitModelRouteUnavailable,
)
from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.document_extraction_cli import run_extraction
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.store import content_hash
from tests.test_document_extraction_automation import AutomationDraftingTests

MISSION = {"id": "coverage-mission-version:fixture:1", "mission_ref": "coverage-mission:fixture",
           "content_hash": "0" * 64, "created_at": "2026-09-24T00:00:00+00:00",
           "budget": {"max_daily_paid_calls": 100, "max_daily_cost_usd": 5}}


def _item(n: int, *, route: str | None = "route-decision:producer", statement: str | None = None):
    return support_item(
        subject_ref="company:ticker:amzn", subject_name="Amazon (AMZN)",
        statement=statement or f"Amazon expects AWS growth to accelerate ({n}).",
        cited_text=f"Amazon said AWS growth should accelerate into next year ({n}).",
        producer_route_ref=route)


def _reply(*verdicts):
    return json.dumps({"schema_version": "0.1", "verdicts": [
        {"item_id": f"i{index + 1}", "support": support, "subject": subject,
         "other_subject": other}
        for index, (support, subject, other) in enumerate(verdicts)]})


class FakeModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return {"text": reply, "cost_micros": 700, "work_order_ref": f"work:cockpit-{kwargs['purpose']}-x",
                "route_decision_ref": "route-decision:verifier", "invocation_ref": "invocation:v"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 24, 12, 5, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def _store():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return SimpleNamespace(connection=connection)


class ContractTests(unittest.TestCase):
    def test_the_prompt_carries_every_statement_with_its_subject_and_cited_text(self) -> None:
        prompt = build_prompt([_item(1), _item(2)])
        self.assertIn('"item_id":"i2"', prompt)
        self.assertIn("Amazon (AMZN)", prompt)
        self.assertIn("not_supported", prompt)
        self.assertIn("about_other", prompt)
        self.assertIn("UNTRUSTED_ITEMS=", prompt)

    def test_only_closed_well_formed_answers_are_read(self) -> None:
        fenced = "```json\n" + _reply(("supported", "about_subject", None),
                                      ("not_supported", "about_other", "CoreWeave")) + "\n```"
        found, problems = parse_verdicts(fenced, 2)
        self.assertEqual(found[1], {"support": "not_supported", "subject_relation": "about_other",
                                    "other_subject": "CoreWeave"})
        self.assertEqual(problems, [])
        bad = json.dumps({"schema_version": "0.1", "verdicts": [
            {"item_id": "i1", "support": "maybe", "subject": "about_subject"},
            {"item_id": "i2", "support": "supported", "subject": "about_subject"},
            {"item_id": "i2", "support": "supported", "subject": "about_subject"},
            {"item_id": "i9", "support": "supported", "subject": "about_subject"}]})
        found, problems = parse_verdicts(bad, 2)
        self.assertEqual(found, {})
        self.assertEqual(len(problems), 3)
        self.assertEqual(parse_verdicts("not json", 1), ({}, ["reply is not strict JSON"]))

    def test_anything_but_supported_about_the_subject_is_a_hold(self) -> None:
        self.assertIsNone(hold_reason({"support": "supported", "subject_relation": "about_subject"}, "AMZN"))
        reason = hold_reason({"support": "not_supported", "subject_relation": "about_other",
                              "other_subject": "CoreWeave"}, "AMZN")
        self.assertTrue(reason.startswith("held for human review"))
        self.assertIn("does not support", reason)
        self.assertIn("CoreWeave, not AMZN", reason)

    def test_the_key_is_the_content_judged(self) -> None:
        base = dict(subject_ref="company:ticker:amzn", statement="s", cited_text="c")
        self.assertEqual(item_key(**base), item_key(**base))
        self.assertNotEqual(item_key(**base), item_key(**{**base, "cited_text": "c2"}))
        self.assertNotEqual(item_key(**base), item_key(**{**base, "subject_ref": "company:ticker:msft"}))

    def test_both_purposes_are_verifier_tier_coverage_pool_and_labelled(self) -> None:
        from dalton_core.budget_pools import pool_for_purpose
        from dalton_core.call_budget import default_call_budget
        from dalton_core.model_fallback_chain import tier_for
        from dalton_core.model_selection import PURPOSE_LABELS

        for purpose in (PURPOSE, BACKFILL_PURPOSE):
            # 2026-09-26: verifier, not cheap -- their WorkOrders carry a
            # provider contract, which no cheap link could serve.
            self.assertEqual(tier_for(purpose), "verifier")
            self.assertEqual(pool_for_purpose(purpose), "coverage")
            self.assertIn(purpose, PURPOSE_LABELS)
            # The owner's rule: every packaged single-call ceiling is a dollar;
            # what bounds this lane is its daily ceiling.
            self.assertEqual(default_call_budget(purpose)["max_cost_usd"], 1.0)


class SettingsTests(unittest.TestCase):
    def test_defaults_then_an_owner_file_and_a_bad_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(load_settings(root)["daily_cap_usd"], 0.15)
            # Retirement is append-only: the backfill starts at one call a run.
            self.assertEqual(load_settings(root)["backfill_batches_per_run"], 1)
            (root / "claim-support-verification.json").write_text(
                json.dumps({"daily_cap_usd": 0.2, "backfill_batches_per_run": 0}), encoding="utf-8")
            settings = load_settings(root)
            self.assertEqual((settings["daily_cap_usd"], settings["backfill_batches_per_run"]), (0.2, 0))
            (root / "claim-support-verification.json").write_text(
                json.dumps({"daily_cap_usd": "lots"}), encoding="utf-8")
            with self.assertRaises(ClaimSupportError):
                load_settings(root)
            (root / "claim-support-verification.json").write_text(
                json.dumps({"enabled": False}), encoding="utf-8")
            with self.assertRaises(ClaimSupportError):
                load_settings(root)

    def test_spend_is_read_per_purpose_per_day_from_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE thesis_impact_day_admissions (admission_id TEXT, day TEXT,
                    work_order_ref TEXT, reserved_micros INTEGER);
                CREATE TABLE thesis_impact_day_settlements (admission_id TEXT, actual_micros INTEGER);
            """)
            connection.executemany("INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?)", [
                ("a", "2026-09-24", f"work:cockpit-{PURPOSE}-1", 50000),
                ("b", "2026-09-24", f"work:cockpit-{PURPOSE}-2", 40000),
                ("c", "2026-09-23", f"work:cockpit-{PURPOSE}-3", 40000),
                ("d", "2026-09-24", f"work:cockpit-{BACKFILL_PURPOSE}-4", 40000),
                ("e", "2026-09-24", "work:document-extraction-5", 40000)])
            connection.execute("INSERT INTO thesis_impact_day_settlements VALUES('a', 900)")
            connection.commit()
            connection.close()
            self.assertEqual(purpose_spend_micros(path, PURPOSE, "2026-09-24"), 40900)
            with self.assertRaises(ClaimSupportError):
                purpose_spend_micros(Path(temp) / "missing.sqlite", PURPOSE, "2026-09-24")

    def test_a_refusal_never_sent_is_not_spend_and_nothing_else_is_dropped(self) -> None:
        # 2026-09-25: before 4fa1e3c4 the adapter's contract-wiring refusal
        # (raised before the broker request exists) was settled at the full
        # reservation with no usage entry; both ceilings filled on money never
        # spent (backfill 511,231 of 500,000, verifier 172,991 of 150,000).
        wiring = ("the model call failed: independent verifier WorkOrder lacks "
                  "the required output schema version")
        day = "2026-09-25"
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "thesis-impact-budget.sqlite"
            connection = sqlite3.connect(ledger)
            connection.executescript("""
                CREATE TABLE thesis_impact_day_admissions (admission_id TEXT, day TEXT,
                    work_order_ref TEXT, attempt_number INTEGER, reserved_micros INTEGER);
                CREATE TABLE thesis_impact_day_settlements (settlement_id TEXT,
                    admission_id TEXT, actual_micros INTEGER, usage_entry_ref TEXT);
                CREATE TABLE thesis_impact_settlement_corrections (admission_id TEXT,
                    corrected_micros INTEGER);
            """)
            rows = {
                # name: (reserved, actual, usage_entry_ref, chain failure messages)
                "wired": (102137, 102137, None, [wiring]),
                "budget": (100000, 100000, None, ["provider max_output_tokens telemetry "
                                                  "exceeds WorkOrder budget"]),
                "mixed": (100000, 100000, None, [wiring, "TIMEOUT"]),
                "reported": (100000, 100000, "usage:x", [wiring]),
                "partial": (100000, 700, None, [wiring]),
                "succeeded": (100000, 100000, None, None),
                "open": (30000, None, None, [wiring]),
            }
            for name, (reserved, actual, usage, _) in rows.items():
                work = f"work:cockpit-{PURPOSE}-{name}"
                connection.execute(
                    "INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?,?)",
                    (name, day, work, 1, reserved))
                if actual is not None:
                    connection.execute(
                        "INSERT INTO thesis_impact_day_settlements VALUES(?,?,?,?)",
                        ("s-" + name, name, actual, usage))
            connection.commit()
            connection.close()
            everything = sum(actual if actual is not None else reserved
                             for reserved, actual, _u, _m in rows.values())
            # No scheduler beside the ledger: nothing can be shown unsent.
            self.assertEqual(purpose_spend_micros(ledger, PURPOSE, day), everything)
            scheduler = sqlite3.connect(Path(temp) / "scheduler.sqlite")
            scheduler.execute(
                "CREATE TABLE scheduler_result_envelopes (work_order_id TEXT, "
                "attempt_number INTEGER, result_envelope_json TEXT, created_at TEXT)")
            for name, (_r, _a, _u, messages) in rows.items():
                envelope = ({"status": "succeeded", "metadata": {}} if messages is None else {
                    "status": "failed",
                    "error": {"code": "MODEL_CHAIN_EXHAUSTED", "message": messages[0]},
                    "metadata": {"chain_failures": [
                        {"code": "unclassified_failure", "message": text}
                        for text in messages]}})
                scheduler.execute(
                    "INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?)",
                    (f"work:cockpit-{PURPOSE}-{name}", 1, json.dumps(envelope), day))
            scheduler.commit()
            scheduler.close()
            self.assertEqual(purpose_spend_micros(ledger, PURPOSE, day),
                             everything - 102137)
            # The scheduler named explicitly is the one read.
            self.assertEqual(
                purpose_spend_micros(ledger, PURPOSE, day,
                                     scheduler_db=Path(temp) / "elsewhere.sqlite"),
                everything)


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _store()
        self.addCleanup(self.store.connection.close)
        self.clock = Clock()
        self.spent = 0

    def verifier(self, model, **kwargs):
        options = dict(store=self.store, model_call=model, daily_cap_micros=100_000,
                       spend_today=lambda _p, _d: self.spent, clock=self.clock,
                       producer_family=lambda ref: "deepseek-v4")
        options.update(kwargs)
        return ClaimSupportVerifier(**options)

    def test_one_call_answers_the_window_and_the_answer_is_kept(self) -> None:
        model = FakeModel(_reply(("supported", "about_subject", None),
                                 ("not_supported", "about_subject", None)))
        verifier = self.verifier(model)
        items = [_item(1), _item(2)]
        outcome = verifier.verify(mission=MISSION, items=items)
        self.assertEqual(outcome["status"], "verified")
        self.assertEqual(len(model.calls), 1)
        call = model.calls[0]
        self.assertEqual(call["purpose"], PURPOSE)
        self.assertEqual(call["producer_route_decision_refs"], ["route-decision:producer"])
        self.assertTrue(call["request_id"].endswith(":2026-09-24T12"))
        self.assertEqual(outcome["verdicts"][items[1]["item_key"]]["support"], "not_supported")
        # Asked once: the same statements again cost nothing.
        again = verifier.verify(mission=MISSION, items=items)
        self.assertEqual((again["status"], again["calls"], len(model.calls)), ("verified", 0, 1))

    def test_verdicts_are_append_only(self) -> None:
        verifier = self.verifier(FakeModel(_reply(("supported", "about_subject", None))))
        verifier.verify(mission=MISSION, items=[_item(1)])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("UPDATE claim_support_verdicts SET support='not_supported'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM claim_support_verdicts")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO claim_support_verdicts SELECT * FROM claim_support_verdicts")

    def test_the_daily_ceiling_defers_without_a_call(self) -> None:
        model = FakeModel()
        self.spent = 100_000
        outcome = self.verifier(model).verify(mission=MISSION, items=[_item(1)])
        self.assertEqual((outcome["status"], model.calls), ("deferred", []))
        self.assertIn("daily ceiling", outcome["reason"])

    def test_an_unreadable_ceiling_defers_too(self) -> None:
        def broken(_purpose, _day):
            raise ClaimSupportError("no day ledger")
        outcome = self.verifier(FakeModel(), spend_today=broken).verify(mission=MISSION, items=[_item(1)])
        self.assertEqual(outcome["status"], "deferred")

    def test_a_spent_pool_defers_and_is_not_a_failed_attempt(self) -> None:
        model = FakeModel(CockpitModelPoolExhausted("spent", {"pool": "coverage"}),
                          _reply(("supported", "about_subject", None)))
        verifier = self.verifier(model)
        self.assertEqual(verifier.verify(mission=MISSION, items=[_item(1)])["status"], "deferred")
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM claim_support_attempts").fetchone()[0], 0)
        self.assertEqual(verifier.verify(mission=MISSION, items=[_item(1)])["status"], "verified")

    def test_failures_retry_hourly_then_hold(self) -> None:
        model = FakeModel(*[CockpitModelError("provider down")] * MAX_ATTEMPTS)
        verifier = self.verifier(model)
        items = [_item(1)]
        first = verifier.verify(mission=MISSION, items=items)
        self.assertEqual(first["status"], "deferred")
        # Same hour: not asked again (the failed WorkOrder would only replay).
        self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "deferred")
        self.assertEqual(len(model.calls), 1)
        for _ in range(MAX_ATTEMPTS - 1):
            self.clock.now += timedelta(hours=1)
            outcome = verifier.verify(mission=MISSION, items=items)
        self.assertEqual(len(model.calls), MAX_ATTEMPTS)
        self.assertEqual(outcome["status"], "exhausted")
        self.assertIn("failed 3 times", outcome["unverifiable"][items[0]["item_key"]])
        # And it stays settled without another paid attempt.
        self.clock.now += timedelta(hours=1)
        self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "exhausted")
        self.assertEqual(len(model.calls), MAX_ATTEMPTS)

    WIRING = ("CockpitModelError: the cheap chain halted on unclassified_failure: "
              "profile:gemini-3-8-flash-antigravity (unclassified_failure); broker details: "
              "profile:gemini-3-8-flash-antigravity [unclassified_failure]: the model call failed: "
              "independent verifier WorkOrder lacks the required output schema version")

    def test_contract_wiring_failures_defer_hourly_and_never_hold(self) -> None:
        # 2026-09-25: the live failure, verbatim.  It is about this code, not
        # the statements; counting it held every batch for a person.
        model = FakeModel(*[CockpitModelError(self.WIRING.split(": ", 1)[1])] * (MAX_ATTEMPTS + 1),
                          _reply(("supported", "about_subject", None)))
        verifier = self.verifier(model)
        items = [_item(1)]
        for _ in range(MAX_ATTEMPTS + 1):
            outcome = verifier.verify(mission=MISSION, items=items)
            self.assertEqual(outcome["status"], "deferred", outcome)
            self.assertEqual(outcome["unverifiable"], {})
            # Still once an hour, not every tick.
            self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "deferred")
            self.clock.now += timedelta(hours=1)
        self.assertEqual(len(model.calls), MAX_ATTEMPTS + 1)
        self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "verified")

    def test_a_batch_exhausted_by_contract_wiring_is_asked_again(self) -> None:
        # What a deploy before this fix could leave behind: three counted
        # failures, the last of them the wiring refusal.
        verifier = self.verifier(FakeModel(_reply(("supported", "about_subject", None))))
        items = [_item(1)]
        request_key = content_hash(
            {"purpose": PURPOSE, "items": [items[0]["item_key"]]})[:32]
        verifier.records.write(
            "INSERT INTO claim_support_attempts(request_key,purpose,attempts,last_bucket,last_reason,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (request_key, PURPOSE, MAX_ATTEMPTS, "2026-09-24T07", self.WIRING, "2026-09-24T07:00:00+00:00"))
        self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "verified")
        # A batch exhausted by anything else stays held.
        other = [_item(2)]
        other_key = content_hash(
            {"purpose": PURPOSE, "items": [other[0]["item_key"]]})[:32]
        verifier.records.write(
            "INSERT INTO claim_support_attempts(request_key,purpose,attempts,last_bucket,last_reason,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (other_key, PURPOSE, MAX_ATTEMPTS, "2026-09-24T07", "CockpitModelError: provider down",
             "2026-09-24T07:00:00+00:00"))
        self.assertEqual(verifier.verify(mission=MISSION, items=other)["status"], "exhausted")

    ROUTE = "CockpitModelRouteUnavailable: no model route is available right now"

    def test_no_route_defers_hourly_and_never_holds(self) -> None:
        # 2026-09-26: the router refused every call from 00:00 UTC (the cheap
        # chain had no link that could serve a verifier WorkOrder).  Counted,
        # three of those held a day's statements for a person.
        route = CockpitModelRouteUnavailable("no model route is available right now")
        model = FakeModel(*[route] * (MAX_ATTEMPTS + 2),
                          _reply(("supported", "about_subject", None)))
        verifier = self.verifier(model)
        items = [_item(1)]
        for _ in range(MAX_ATTEMPTS + 2):
            outcome = verifier.verify(mission=MISSION, items=items)
            self.assertEqual(outcome["status"], "deferred", outcome)
            self.assertEqual(outcome["unverifiable"], {})
            # Once an hour, not every tick.
            self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "deferred")
            self.clock.now += timedelta(hours=1)
        self.assertEqual(len(model.calls), MAX_ATTEMPTS + 2)
        self.assertEqual(verifier.verify(mission=MISSION, items=items)["status"], "verified")
        rows = dict(self.store.connection.execute(
            "SELECT request_key, attempts FROM claim_support_attempts").fetchall())
        self.assertEqual(len(rows), 1)
        self.assertTrue(next(iter(rows)).endswith(":route-unavailable"))

    def test_the_route_refusal_is_recognised_by_its_text_as_well(self) -> None:
        # ``_answer`` spells the same refusal differently; either is systemic.
        model = FakeModel(*[CockpitModelError(
            "no model route is available right now")] * (MAX_ATTEMPTS + 1))
        verifier = self.verifier(model)
        for _ in range(MAX_ATTEMPTS + 1):
            self.assertEqual(verifier.verify(mission=MISSION, items=[_item(1)])["status"],
                             "deferred")
            self.clock.now += timedelta(hours=1)

    def test_a_batch_exhausted_by_no_route_is_asked_again_and_recounted(self) -> None:
        # What the 2026-09-26 outage left behind: three (or four) counted
        # failures, all "no model route".  The batch is asked again, and a
        # real failure after that is the first of three, not the fourth.
        model = FakeModel(*[CockpitModelError("provider down")] * MAX_ATTEMPTS)
        verifier = self.verifier(model)
        items = [_item(1)]
        request_key = content_hash(
            {"purpose": PURPOSE, "items": [items[0]["item_key"]]})[:32]
        verifier.records.write(
            "INSERT INTO claim_support_attempts(request_key,purpose,attempts,last_bucket,last_reason,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (request_key, PURPOSE, MAX_ATTEMPTS + 1, "2026-09-24T04", self.ROUTE,
             "2026-09-24T04:02:41+00:00"))
        for attempt in range(1, MAX_ATTEMPTS + 1):
            outcome = verifier.verify(mission=MISSION, items=items)
            self.assertEqual(len(model.calls), attempt)
            expected = "exhausted" if attempt == MAX_ATTEMPTS else "deferred"
            self.assertEqual(outcome["status"], expected, outcome)
            self.clock.now += timedelta(hours=1)
        self.assertIn("provider down", outcome["unverifiable"][items[0]["item_key"]])

    def test_a_drafter_no_capable_link_is_independent_of_is_held_without_a_call(self) -> None:
        from dalton_core.claim_support_verification import no_independent_link

        links = [("profile:gemini-3-8-flash", "google-gemini-3"),
                 ("profile:gemini-3-1-pro-preview", "google-gemini-3")]
        model = FakeModel(_reply(("supported", "about_subject", None)))
        verifier = self.verifier(
            model, producer_family=lambda ref: ("google-gemini-3" if ref.endswith("gemini")
                                                else "deepseek-v4"),
            independent_route=lambda family: no_independent_link(
                links, family, purpose=PURPOSE))
        gemini, deepseek = _item(1, route="route-decision:gemini"), _item(2)
        outcome = verifier.verify(mission=MISSION, items=[gemini, deepseek])
        self.assertEqual(outcome["status"], "exhausted")
        self.assertIn("independent of the drafting family 'google-gemini-3'",
                      outcome["unverifiable"][gemini["item_key"]])
        self.assertIn(deepseek["item_key"], outcome["verdicts"])
        self.assertEqual(len(model.calls), 1)
        # A chain with no capable link at all says nothing about the drafter:
        # it is the router's to refuse, and that refusal defers.
        self.assertIsNone(no_independent_link([], "deepseek-v4", purpose=PURPOSE))

    def test_a_partial_answer_keeps_what_was_answered_and_defers_the_rest(self) -> None:
        model = FakeModel(_reply(("supported", "about_subject", None)),
                          _reply(("supported", "about_subject", None)))
        verifier = self.verifier(model)
        items = [_item(1), _item(2)]
        outcome = verifier.verify(mission=MISSION, items=items)
        self.assertEqual(outcome["status"], "deferred")
        self.assertIn(items[0]["item_key"], outcome["verdicts"])
        self.clock.now += timedelta(hours=1)
        outcome = verifier.verify(mission=MISSION, items=items)
        self.assertEqual(outcome["status"], "verified")
        self.assertIn("i1", json.dumps(model.calls[1]["prompt"]))
        self.assertNotIn("(1)", model.calls[1]["prompt"])

    def test_an_unclassified_or_unknown_drafter_is_never_verified_by_anyone(self) -> None:
        model = FakeModel()
        verifier = self.verifier(model, producer_family=lambda ref: "unclassified:deepseek")
        outcome = verifier.verify(mission=MISSION, items=[_item(1), _item(2, route=None)])
        self.assertEqual((outcome["status"], model.calls), ("exhausted", []))
        self.assertEqual(len(outcome["unverifiable"]), 2)

    def test_a_large_window_is_split_inside_the_input_bound(self) -> None:
        items = [_item(n, statement="Amazon " + "x" * 900 + f" {n}") for n in range(6)]
        model = FakeModel(*[_reply(*[("supported", "about_subject", None)] * 6)] * 6)
        verifier = self.verifier(model, max_prompt_bytes=4000)
        outcome = verifier.verify(mission=MISSION, items=items)
        self.assertGreater(len(model.calls), 1)
        self.assertTrue(all(len(call["prompt"].encode()) <= 4000 for call in model.calls))
        self.assertEqual(len(outcome["verdicts"]), 6)


class ServiceGateTests(unittest.TestCase):
    """``support_verdicts`` without the whole admission chain."""

    def _service(self, verifier=None):
        writer = SimpleNamespace(_claim_support_verifier=verifier)
        return DocumentExtractionService(writer)

    def _pairs(self, held=None):
        suggestion = {"normalized_statement": "Amazon expects AWS to accelerate.",
                      "citation": {"raw_text": "Amazon said AWS should accelerate."},
                      "route_ref": "route-decision:producer"}
        return [(suggestion, "company:ticker:amzn", "named", held),
                (suggestion, "industry:hyperscalers", "named", None)]

    def test_a_paid_draft_with_no_verifier_is_deferred_never_admitted(self) -> None:
        result = self._service().support_verdicts(
            {"source_ref": "source:sales-notes", "model_binding": {"x": 1}}, MISSION, self._pairs())
        self.assertEqual(result["status"], "deferred")

    def test_other_sources_held_spans_and_industry_subjects_are_not_asked(self) -> None:
        service = self._service()
        for context, pairs in (
            ({"source_ref": "source:web-search", "model_binding": {"x": 1}}, self._pairs()),
            ({"source_ref": "source:sales-notes", "model_binding": {"x": 1}}, self._pairs(held="held")),
            ({"source_ref": "source:alphaengine"}, self._pairs()),
        ):
            with self.subTest(context=context):
                self.assertEqual(service.support_verdicts(context, MISSION, pairs)["status"],
                                 "not_applicable")


class AdmissionTests(AutomationDraftingTests):
    """End to end through the extraction child, the fixture drafter and a fake verifier model."""

    STATEMENT = {
        "normalized_statement": "Accenture management says client decisions remain cautious.",
        "metric_or_aspect": "aspect:client-decisions", "period": "FY26",
        "basis": "fixture management commentary",
        "excerpt": "Accenture management says client decisions remain cautious"}

    def _run(self, model, *, max_attempts=MAX_ATTEMPTS, first=True):
        fixture = self.root / "support-fixture.json"
        if first:
            self._grant_automation()
            self._policy_with_document_rule()
            context = self._active_context()
            fixture.write_text(json.dumps({"schema_version": "0.1", "suggestions": [
                {**self.STATEMENT, "quote_id": context["quotes"][0]["quote_id"]}]}), encoding="utf-8")
        base = document_extraction_cli.ExtractionHost

        class Host(base):
            def __init__(inner, **kwargs):
                super().__init__(**kwargs)
                inner._claim_support_verifier = ClaimSupportVerifier(
                    store=inner.store, model_call=model, daily_cap_micros=100_000,
                    producer_family=lambda ref: "deepseek-v4", max_attempts=max_attempts)

        before = self.h.counts()
        with patch.object(document_extraction_cli, "ExtractionHost", Host):
            summary = run_extraction(
                state_dir=self.root, model_config_path=self._model_config(),
                summary_dir=self.root / "support-summary", spool_dir=self.root / "spool",
                scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
                max_windows=2, max_numeric_windows=0, max_discovery_windows=0,
                connector_governance=None, web_fetch_governance=None,
                hermetic_fixture=fixture, candidate_staging=self.root / "staging.sqlite")
        return summary, before

    def test_a_supported_statement_about_the_subject_is_admitted_with_its_verdict(self) -> None:
        model = FakeModel(_reply(("supported", "about_subject", None)))
        summary, before = self._run(model)
        [admitted] = summary["admitted"]
        self.assertEqual(admitted["status"], "admitted", admitted)
        self.assertEqual(admitted["support_verdict"]["support"], "supported")
        self.assertEqual(len(model.calls), 1)
        self.assertIn("Accenture", model.calls[0]["prompt"])
        self.assertEqual(self.h.counts()["claim_versions"], before["claim_versions"] + 1)
        self.assertEqual(summary["support_checked"], 1)

    def test_an_unsupported_or_misattributed_statement_is_staged_for_a_person(self) -> None:
        model = FakeModel(_reply(("supported", "about_other", "Infosys")))
        summary, before = self._run(model)
        [held] = summary["admitted"]
        self.assertEqual(held["status"], "held", held)
        self.assertIn("about Infosys", held["reason"])
        self.assertEqual(self.h.counts()["claim_versions"], before["claim_versions"])
        self.assertEqual(summary["held_by_support_check"], 1)
        [resolved] = summary["resolved_reviews"]
        self.assertEqual((resolved["status"], resolved["held"]), ("extraction_staged", 1))
        review = HumanReviewAuthority(self.root / "staging.sqlite")
        self.addCleanup(review.close)
        self.assertEqual(review.candidate_status(held["candidate_claim_ref"])["review_state"], "staged")

    def test_a_check_that_cannot_run_keeps_the_review_open_and_writes_nothing(self) -> None:
        model = FakeModel(CockpitModelError("provider down"))
        summary, before = self._run(model)
        self.assertEqual(summary["admitted"], [])
        [resolved] = summary["resolved_reviews"]
        self.assertEqual(resolved["status"], "held")
        self.assertIn("claim_support_verification_deferred", resolved["reason"])
        self.assertEqual(summary["support_deferred_reviews"], 1)
        self.assertEqual(self.h.counts(), before)
        active = self.h.missions.active_mission("coverage-mission:us-it-services")
        states = {r["state"] for r in self.h.missions.document_reviews(active["id"])}
        self.assertIn("awaiting_human_extraction", states)


    def test_a_route_outage_keeps_the_review_open_and_holds_nothing(self) -> None:
        model = FakeModel(CockpitModelRouteUnavailable("no model route is available right now"))
        summary, before = self._run(model, max_attempts=1)
        self.assertEqual(summary["admitted"], [])
        [resolved] = summary["resolved_reviews"]
        self.assertEqual(resolved["status"], "held")
        self.assertIn("claim_support_verification_deferred", resolved["reason"])
        self.assertEqual(self.h.counts(), before)

    def _outage_leftovers(self):
        """What 2026-09-26 left behind, reproduced with the code as it was.

        "No model route" counted towards the attempts (the old code saw it as
        an ordinary CockpitModelError), the batch exhausted, the statement was
        staged as held and the review closed.  The Scheduler kept the prompt
        the batch was asked with.
        """

        from dalton_core import claim_support_verification as csv
        from dalton_core.claim_support_verification import ClaimSupportVerdictStore
        from dalton_core.cockpit_model import build_work
        from dalton_core.scheduler import Scheduler

        outage = FakeModel(CockpitModelError("no model route is available right now"))
        with patch.object(csv, "systemic_failure", csv.contract_wiring_failure):
            summary, before = self._run(outage, max_attempts=1)
        [held] = summary["admitted"]
        self.assertEqual(held["status"], "held", held)
        self.assertIn("could not be run", held["reason"])
        [resolved] = summary["resolved_reviews"]
        self.assertEqual(resolved["status"], "extraction_staged")
        ClaimSupportVerdictStore(self.h.h.core).write(
            "UPDATE claim_support_attempts SET attempts=?", (MAX_ATTEMPTS,))
        [asked] = outage.calls
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            scheduler.enqueue(build_work(
                purpose=PURPOSE, request_id=asked["request_id"], prompt=asked["prompt"],
                mission_version_ref=asked["mission"]["id"], max_input_tokens=120_000,
                max_output_tokens=1_600, max_cost_usd=1.0, max_seconds=120))
        return held, before

    def test_candidates_the_outage_held_are_asked_again_and_committed(self) -> None:
        held, before = self._outage_leftovers()
        recovered = FakeModel(_reply(("supported", "about_subject", None)))
        summary, _ = self._run(recovered, first=False)
        recheck = summary["support_recheck"]
        self.assertEqual(recheck["remaining_before"], 1, recheck)
        [admitted] = recheck["admitted"]
        # The very candidate that was staged, not a second copy of it.
        self.assertEqual(admitted["candidate_claim_ref"], held["candidate_claim_ref"])
        self.assertEqual(len(recovered.calls), 1)
        self.assertIn("Accenture", recovered.calls[0]["prompt"])
        self.assertEqual(self.h.counts()["claim_versions"], before["claim_versions"] + 1)
        # Settled: nothing left to ask, nothing paid again.
        again = FakeModel()
        summary, _ = self._run(again, first=False)
        self.assertEqual(summary["support_recheck"]["remaining_before"], 0)
        self.assertEqual(again.calls, [])

    def test_a_recheck_that_finds_the_statement_unsupported_leaves_it_held(self) -> None:
        held, before = self._outage_leftovers()
        recovered = FakeModel(_reply(("not_supported", "about_subject", None)))
        summary, _ = self._run(recovered, first=False)
        recheck = summary["support_recheck"]
        self.assertEqual((recheck["admitted"], recheck["still_held"]), ([], 1), recheck)
        self.assertEqual(self.h.counts()["claim_versions"], before["claim_versions"])
        review = HumanReviewAuthority(self.root / "staging.sqlite")
        self.addCleanup(review.close)
        self.assertEqual(review.candidate_status(held["candidate_claim_ref"])["review_state"],
                         "staged")

    def test_a_recheck_during_the_outage_defers_and_holds_nothing_more(self) -> None:
        held, before = self._outage_leftovers()
        still = FakeModel(CockpitModelRouteUnavailable("no model route is available right now"))
        summary, _ = self._run(still, first=False)
        recheck = summary["support_recheck"]
        self.assertEqual(recheck["admitted"], [])
        self.assertIsNotNone(recheck["deferred"])
        self.assertEqual(self.h.counts()["claim_versions"], before["claim_versions"])

if __name__ == "__main__":
    unittest.main()
