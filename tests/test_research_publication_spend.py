"""Per-purpose daily ceilings on the publication worker's model calls.

Live 2026-09-24 12:30-16:40 UTC: 181 ``research_language_revision`` calls cost
$51.80, 71% of the mission's spend, preparing backlog (UI text batches,
NO_CHANGE judgements, the cycle reflection).  Nothing capped the purpose but
the mission's $500 day.  A purpose at its ceiling must defer -- stay pending,
count no attempt, resume the next UTC day -- and high-value products must be
prepared before backlog.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import research_output_preparation as prep
from dalton_core import research_publication_spend as spend
from dalton_core.research_publication_worker import poll_once
from dalton_core.store import canonical_json, content_hash
from dalton_core.ui_text_discovery import DEFAULT_MAX_ATTEMPTS, poll_ui_texts

from tests.test_research_output_preparation import (CHINESE, REVISION, SOURCE, STYLE,
                                                    VERDICT)

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)


def gate_with(spent: dict[str, int], **caps_usd) -> spend.PurposeSpendGate:
    return spend.PurposeSpendGate(
        caps_micros=spend.daily_caps_micros(caps_usd or None),
        spend_today=lambda purpose, day: spent.get(purpose, 0), clock=lambda: NOW)


class CeilingTests(unittest.TestCase):
    def test_defaults_cover_every_publication_stage(self):
        caps = spend.daily_caps_micros()
        self.assertEqual(caps, {
            "research_language_revision": 25_000_000,
            "research_language_check": 5_000_000,
            "research_localization": 3_000_000,
            "research_localization_verifier": 3_000_000,
        })

    def test_owner_override_is_validated_not_silently_repaired(self):
        caps = spend.daily_caps_micros({"research_language_revision": 10})
        self.assertEqual(caps["research_language_revision"], 10_000_000)
        self.assertEqual(caps["research_language_check"], 5_000_000)
        for bad in ({"research_language_revision": -1}, {"research_language_revision": 101},
                    {"research_language_revision": True}, {"ask": 5},
                    {"research_language_revision": "NaN"}, []):
            with self.assertRaises(ValueError, msg=bad):
                spend.daily_caps_micros(bad)

    def test_worker_config_accepts_and_checks_the_field(self):
        base = {"schema_version": "research-publication-worker-config:0.1",
                "workers": 4, "chunk_chars": 4500, "max_cost_per_call": 1.0,
                "draft_attempts": 2, "publication_gate": {}}
        for name in ("core_db", "scheduler_db", "model_config", "verifier_config",
                     "checker_config", "brain_config", "work_dir", "output_directory"):
            base[name] = f"/tmp/{name}"
        prep.validate_worker_config({**base, "purpose_daily_cap_usd": {
            "research_language_revision": 25}})
        with self.assertRaises(ValueError):
            prep.validate_worker_config({**base, "purpose_daily_cap_usd": {
                "research_language_revision": 500}})

    def test_gate_defers_at_the_ceiling_and_low_priority_at_its_share(self):
        revision = "research_language_revision"
        gate_with({revision: 24_999_999}).check(revision, priority=spend.PRIORITY_HIGH)
        with self.assertRaises(spend.PurposeDailyCapReached) as caught:
            gate_with({revision: 25_000_000}).check(revision, priority=spend.PRIORITY_HIGH)
        self.assertEqual(caught.exception.day, "2026-09-24")
        self.assertIn("purpose_daily_cap_reached", str(caught.exception))
        # Backlog stops at 60% so later high-value work still has room.
        with self.assertRaises(spend.PurposeDailyCapReached):
            gate_with({revision: 15_000_000}).check(revision, priority=spend.PRIORITY_LOW)
        gate_with({revision: 15_000_000}).check(revision, priority=spend.PRIORITY_HIGH)
        gate_with({revision: 14_999_999}).check(revision, priority=spend.PRIORITY_LOW)
        # A purpose this module does not gate is never refused here.
        gate_with({"ask": 10**12}).check("ask")

    def test_an_unreadable_ledger_defers(self):
        def broken(purpose, day):
            raise OSError("locked")
        gate = spend.PurposeSpendGate(caps_micros=spend.daily_caps_micros(),
                                      spend_today=broken, clock=lambda: NOW)
        with self.assertRaises(spend.PurposeDailyCapReached):
            gate.check("research_language_revision")

    def test_ledger_gate_reads_the_day_ledger_by_work_order_purpose(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger dir" / "budget.sqlite"
            path.parent.mkdir()
            db = sqlite3.connect(path)
            db.executescript("""
              CREATE TABLE thesis_impact_day_admissions(admission_id TEXT, day TEXT,
                work_order_ref TEXT, reserved_micros INTEGER);
              CREATE TABLE thesis_impact_day_settlements(admission_id TEXT, actual_micros INTEGER);
            """)
            rows = [("a1", "2026-09-24", "work:cockpit-research_language_revision-1", 9_000_000, 20_000_000),
                    ("a2", "2026-09-24", "work:cockpit-research_language_revision-2", 1_000_000, None),
                    ("a3", "2026-09-23", "work:cockpit-research_language_revision-3", 1, 90_000_000),
                    ("a4", "2026-09-24", "work:cockpit-research_localization_verifier-4", 1, 9_000_000)]
            for ref, day, work, reserved, actual in rows:
                db.execute("INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?)",
                           (ref, day, work, reserved))
                if actual is not None:
                    db.execute("INSERT INTO thesis_impact_day_settlements VALUES(?,?)",
                               (ref, actual))
            db.commit(); db.close()
            gate = spend.ledger_gate(str(path), clock=lambda: NOW)
            # 20.0 settled + 1.0 still reserved today; yesterday does not count.
            gate.check("research_language_revision", priority=spend.PRIORITY_HIGH)
            with self.assertRaises(spend.PurposeDailyCapReached) as caught:
                gate.check("research_language_revision", priority=spend.PRIORITY_LOW)
            self.assertEqual(caught.exception.spent_micros, 21_000_000)
            # The verifier's spend is not the draft's (``_`` is not a prefix).
            gate.check("research_localization")
            with self.assertRaises(spend.PurposeDailyCapReached):
                gate.check("research_localization_verifier")


class RecordingModel:
    """The same fake model as the preparation tests, with a switchable gate."""

    def __init__(self, test):
        self.test = test

    def install(self):
        owner = self.test

        class Model:
            def __init__(self, *a, **kw): pass

            def call(self, *, purpose, **kw):
                owner.calls.append(purpose)
                return {"text": json.dumps(owner.responses[purpose]),
                        "route_decision_ref": purpose, "cost_micros": 1}

        def independent(model, *, producer_route_decision_refs, **kw):
            return model.call(**kw)
        owner.enterContext(patch.object(prep, "CockpitModel", Model))
        owner.enterContext(patch.object(prep, "independent_model_call", independent))
        owner.enterContext(patch.object(prep, "selected_identity", return_value={
            "provider": prep.CHECKER_PROVIDER, "model": prep.CHECKER_MODEL}))
        owner.enterContext(patch.object(prep, "router_family_resolver",
                                        return_value=lambda ref: ref))


class DeferralInPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.responses = {"research_localization": CHINESE, prep.CHECKER_PURPOSE: STYLE,
                          prep.BRAIN_PURPOSE: REVISION,
                          "research_localization_verifier": VERDICT}
        RecordingModel(self).install()
        self.closed: set[str] = set()
        self.asked: list[str] = []

    def gate(self, purpose):
        self.asked.append(purpose)
        if purpose in self.closed:
            raise spend.PurposeDailyCapReached(
                purpose, spent_micros=1, limit_micros=1, cap_micros=1,
                priority=spend.PRIORITY_NORMAL, day="2026-09-24")

    def run_chunk(self):
        return prep.run_chunk((0, 0, SOURCE), mission={}, draft_config={"name": "draft"},
            verifier_config={"name": "verify"}, checker_config={"name": "checker"},
            brain_config={"name": "brain"}, scheduler_db=Path("unused"),
            work_dir=Path(self.temp.name), max_cost=.2, attempts=3,
            repair_reviewed=True, spend_gate=self.gate)[2]

    def test_every_paid_call_asks_the_gate_first(self):
        self.run_chunk()
        self.assertEqual(self.calls, ["research_localization", prep.CHECKER_PURPOSE,
                                      prep.BRAIN_PURPOSE, "research_localization_verifier"])
        for purpose in set(self.calls):
            self.assertIn(purpose, self.asked)

    def test_a_capped_revision_buys_no_check_and_resumes_without_rebuying_the_draft(self):
        self.closed = {prep.BRAIN_PURPOSE}
        with self.assertRaises(spend.PurposeDailyCapReached):
            self.run_chunk()
        # The draft is kept; the checker is not bought for a revision that
        # cannot be afforded today, and nothing was recorded as a failure.
        self.assertEqual(self.calls, ["research_localization"])
        stage = json.loads(next(Path(self.temp.name, "stages").glob("*.json")).read_text())
        self.assertNotIn("language_review", stage)
        self.assertNotIn("status", stage)
        self.closed = set()
        self.run_chunk()
        self.assertEqual(self.calls, ["research_localization", prep.CHECKER_PURPOSE,
                                      prep.BRAIN_PURPOSE, "research_localization_verifier"])

    def test_a_capped_verifier_is_not_recorded_as_a_semantic_failure(self):
        self.closed = {"research_localization_verifier"}
        with self.assertRaises(spend.PurposeDailyCapReached):
            self.run_chunk()
        stage = json.loads(next(Path(self.temp.name, "stages").glob("*.json")).read_text())
        self.assertEqual(stage.get("review_history") or [], [])
        self.closed = set()
        result = self.run_chunk()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 1)

    def test_build_reports_deferred_and_publishes_nothing(self):
        self.closed = {prep.BRAIN_PURPOSE}
        root = Path(self.temp.name)
        paths = {}
        for name in ("model", "verifier", "checker", "brain"):
            paths[name] = root / f"{name}.json"
            paths[name].write_text(json.dumps({"name": name}))
        args = SimpleNamespace(work_dir=root / "work", output_directory=root / "out",
            scheduler_db=Path("unused"), model_config=paths["model"],
            verifier_config=paths["verifier"], checker_config=paths["checker"],
            brain_config=paths["brain"], workers=1, chunk_chars=4500,
            max_cost_per_call=.2, attempts=3, only=None, repair_reviewed=True,
            spend_gate=self.gate)
        product = {"kind": "ui_text", "label": "界面文字", "status": "available",
                   "subject_ref": "mission:test", "version_ref": "ui-text-batch:test",
                   "sections": [{"title": "界面文字", "body": "Revenue was 123 USD.",
                                 "gaps": [], "sources": []}], "gaps": []}
        outcome = prep.prepare_ui_batch(args, {}, product)
        self.assertEqual(outcome["status"], "deferred")
        self.assertEqual(outcome["receipt"]["status"], "deferred")
        self.assertTrue(outcome["receipt"]["failures"][0]["deferred"])
        self.assertFalse((root / "out").exists())


MISSION = {"universe": [{"company_ref": "company:a"}, {"company_ref": "company:b"}]}


def library(connection, mission, company):
    return {"products": [
        {"kind": "surface_event_judgement", "subject_ref": company,
         "version_ref": f"judgement:{company[-1]}", "status": "available",
         "sections": [{"title": "事件研判", "body": company, "gaps": []}]},
        {"kind": "dossier", "subject_ref": company, "version_ref": f"dossier:{company[-1]}",
         "status": "available", "sections": [{"title": "结论", "body": company, "gaps": []}]},
    ]}


class WorkerOrderAndDeferralTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.db = sqlite3.connect(":memory:"); self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE event_judgements(judgement_id TEXT, decision TEXT)")
        self.db.executemany("INSERT INTO event_judgements VALUES(?,?)",
                            [("judgement:a", "NO_CHANGE"), ("judgement:b", "UPDATE_THESIS")])

    def priority(self, product):
        return spend.publication_priority(self.db, product)

    def test_priority_classes(self):
        cases = {("dossier", "dossier:a"): spend.PRIORITY_HIGH,
                 ("surface_weekly_brief", "w"): spend.PRIORITY_HIGH,
                 ("surface_event_judgement", "judgement:b"): spend.PRIORITY_HIGH,
                 ("surface_event_judgement", "judgement:a"): spend.PRIORITY_LOW,
                 ("surface_cycle_reflection", "r"): spend.PRIORITY_LOW,
                 ("ui_text", "ui-text-batch:x"): spend.PRIORITY_LOW,
                 ("surface_model_notes", "m"): spend.PRIORITY_NORMAL}
        for (kind, ref), expected in cases.items():
            self.assertEqual(self.priority({"kind": kind, "version_ref": ref}), expected, kind)
        # Without a judgement ledger nothing is assumed NO_CHANGE.
        self.assertEqual(spend.publication_priority(
            sqlite3.connect(":memory:"),
            {"kind": "surface_event_judgement", "version_ref": "judgement:a"}),
            spend.PRIORITY_HIGH)

    def test_high_value_products_are_prepared_first_in_a_fixed_order(self):
        order = []
        poll_once(self.db, MISSION, state_dir=self.state, library_reader=library,
                  priority=self.priority,
                  prepare=lambda p: order.append(p["version_ref"]) or {"status": "completed"})
        self.assertEqual(order, ["dossier:a", "dossier:b", "judgement:b", "judgement:a"])

    def test_deferred_is_retried_every_poll_and_pending_is_not(self):
        calls = []
        def capped(product):
            calls.append(product["version_ref"])
            if product["version_ref"] == "judgement:a":
                return {"status": "deferred", "receipt": {"status": "deferred"}}
            return {"status": "pending"}
        first = poll_once(self.db, MISSION, state_dir=self.state, library_reader=library,
                          priority=self.priority, prepare=capped)
        self.assertEqual((first["deferred"], first["pending"]), (1, 4))
        second = poll_once(self.db, MISSION, state_dir=self.state, library_reader=library,
                           priority=self.priority, prepare=lambda p: calls.append(
                               p["version_ref"]) or {"status": "completed"})
        # Only the deferred product is offered again; it now completes.
        self.assertEqual(calls[4:], ["judgement:a"])
        self.assertEqual(second["completed"], 1)
        self.assertEqual(second["deferred"], 0)


ENGLISH = ("Management expects enterprise demand to improve during the next "
           "fiscal year across all major markets.")


class UiTextDeferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.db = sqlite3.connect(":memory:"); self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE claim_versions(
            claim_version_id TEXT PRIMARY KEY, claim_ref TEXT, version_number INTEGER,
            claim_json TEXT, content_hash TEXT, prior_version_id TEXT, created_at TEXT);
          CREATE TABLE claim_retirement_decisions(claim_version_ref TEXT, decision TEXT);
          CREATE TABLE claim_index_entry_versions(
            version_id TEXT,entry_ref TEXT,version_number INTEGER,
            claim_version_ref TEXT,is_canonical INTEGER);
        """)
        filler = " Enterprise services demand across markets remains uncertain." * 60
        for index in range(3):
            ref = f"claim:{index}"
            wire = {"id": ref, "claim_ref": ref, "subject_ref": "company:A",
                    "normalized_statement": f"{ENGLISH} Item {index}.{filler}"}
            self.db.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)", (
                ref, ref, 1, canonical_json(wire), content_hash(wire), None,
                "2026-09-01T00:00:00Z"))
        self.db.commit()
        self.mission = {"mission_ref": "mission:test", "industry_ref": "industry:test",
                        "universe": [{"company_ref": "company:A"}]}
        self.calls = 0

    def poll(self, outcome):
        def prepare(_product):
            self.calls += 1
            return dict(outcome)
        library = {"products": [{"status": "available", "sections": [{"sources": [
            {"kind": "claim", "ref": f"claim:{index}"} for index in range(3)]}]}]}
        with patch("dalton_core.ui_text_discovery.research_library", return_value=library):
            return poll_ui_texts(self.db, self.mission, state_dir=self.state, mapping={},
                                 prepare=prepare, batches_per_run=3)

    def test_a_ceiling_never_counts_as_an_attempt_or_blocks_a_batch(self):
        deferred = {"status": "deferred", "receipt": {"status": "deferred", "failures": [
            {"error": "purpose_daily_cap_reached: ...", "deferred": True}]}}
        for _ in range(DEFAULT_MAX_ATTEMPTS + 3):
            result = self.poll(deferred)
            # The first deferral stops the queue: one prepare per poll.
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["blocked"], 0)
            self.assertEqual(sorted(row["status"] for row in result["batches"]),
                             ["deferred"] * 3)
            self.assertEqual(result["pending"], 3)
        self.assertEqual(self.calls, DEFAULT_MAX_ATTEMPTS + 3)
        self.assertFalse(list((self.state / "results").glob("*.json")))
        # A real failure afterwards is attempt one, not attempt ten.
        result = self.poll({"status": "pending", "reason": "no"})
        self.assertEqual(sorted(row["attempts"] for row in result["batches"]), [1, 1, 1])

    def test_the_discovery_constant_matches_the_spend_module(self):
        from dalton_core import ui_text_discovery
        self.assertEqual(ui_text_discovery.DEFERRED_STATUS, spend.DEFERRED_STATUS)


if __name__ == "__main__":
    unittest.main()
