"""WP-B2-3: a batch that cannot succeed must stop spending.

329 of 329 UI text batches were deferred, every one of them against a language
checker refusing every call, and the four that were tried each pass had reached
seventeen attempts. Nothing in the loop could ever stop: a failed batch went
back into the same queue with a higher attempt count and no ceiling. A bound
turns "retry forever" into one readable, owner-visible decision.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.store import canonical_json, content_hash
from dalton_core.ui_text_discovery import (
    DEFAULT_BATCHES_PER_RUN, DEFAULT_MAX_ATTEMPTS, poll_ui_texts)

ENGLISH = ("Management expects enterprise demand to improve during the next "
           "fiscal year across all major markets.")


class RetryBoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
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
        self.mission = {"mission_ref": "mission:test", "industry_ref": "industry:test",
                        "universe": [{"company_ref": "company:A"}]}
        self.calls = 0

    def claim(self, ref, text):
        wire = {"id": ref, "claim_ref": ref, "subject_ref": "company:A",
                "normalized_statement": text}
        self.db.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)", (
            ref, ref, 1, canonical_json(wire), content_hash(wire), None,
            "2026-09-01T00:00:00Z"))
        self.db.commit()

    def poll(self, **kwargs):
        def failing(_product):
            self.calls += 1
            return {"status": "pending", "receipt": {"chunks": 2, "failures": [
                {"error": "language checker served an unexpected transport or model",
                 "product": 0, "section_start": 0}]}}

        library = {"products": [{"status": "available", "sections": [{"sources": [
            {"kind": "claim", "ref": "claim:1"}]}]}]}
        with patch("dalton_core.ui_text_discovery.research_library",
                   return_value=library):
            return poll_ui_texts(self.db, self.mission, state_dir=self.state,
                                 mapping={}, prepare=kwargs.pop("prepare", failing),
                                 **kwargs)

    def test_a_failing_batch_is_blocked_after_the_attempt_bound(self):
        self.claim("claim:1", ENGLISH)
        for round_number in range(1, DEFAULT_MAX_ATTEMPTS + 1):
            result = self.poll()
            statuses = {row["batch_ref"]: row["status"] for row in result["batches"]}
            self.assertEqual(len(statuses), 1)
            expected = "blocked" if round_number >= DEFAULT_MAX_ATTEMPTS else "pending"
            self.assertEqual(list(statuses.values()), [expected], round_number)
        spent = self.calls
        # Every later pass must cost nothing at all.
        for _ in range(3):
            result = self.poll()
            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["blocked"], 1)
            self.assertEqual(result["pending"], 0)
        self.assertEqual(self.calls, spent)

    def test_a_blocked_batch_carries_a_readable_reason(self):
        self.claim("claim:1", ENGLISH)
        for _ in range(DEFAULT_MAX_ATTEMPTS):
            result = self.poll()
        reason = result["blocked_reasons"][0]
        self.assertIn("停止重试", reason)
        self.assertIn(str(DEFAULT_MAX_ATTEMPTS), reason)
        self.assertIn("language checker served an unexpected transport or model",
                      reason)

    def test_the_block_survives_a_restart_because_it_is_on_disk(self):
        self.claim("claim:1", ENGLISH)
        for _ in range(DEFAULT_MAX_ATTEMPTS):
            self.poll()
        saved = json.loads(next(
            (self.state / "results").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["attempts"], DEFAULT_MAX_ATTEMPTS)

    def test_a_lower_bound_blocks_sooner(self):
        self.claim("claim:1", ENGLISH)
        result = self.poll(max_attempts=1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.calls, 1)

    def test_batches_per_run_bounds_how_many_are_paid_for(self):
        # Batches cap at 4,500 characters, so pad each claim past that bound to
        # get one batch per claim.
        filler = " Enterprise services demand across markets remains uncertain." * 60
        for index in range(7):
            self.claim(f"claim:{index}", f"{ENGLISH} Item {index}.{filler}")
        library = {"products": [{"status": "available", "sections": [{"sources": [
            {"kind": "claim", "ref": f"claim:{index}"} for index in range(7)]}]}]}

        def failing(_product):
            self.calls += 1
            return {"status": "pending", "reason": "no"}

        with patch("dalton_core.ui_text_discovery.research_library",
                   return_value=library):
            result = poll_ui_texts(self.db, self.mission, state_dir=self.state,
                                   mapping={}, prepare=failing, batches_per_run=2)
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["batches_per_run"], 2)
        self.assertEqual(self.calls, 2)

    def test_the_defaults_are_the_documented_ones(self):
        self.assertEqual((DEFAULT_BATCHES_PER_RUN, DEFAULT_MAX_ATTEMPTS), (4, 6))

    def test_bounds_are_validated_before_anything_runs(self):
        for kwargs in ({"batches_per_run": 0}, {"max_attempts": 0},
                       {"batches_per_run": 65}, {"max_attempts": True}):
            with self.assertRaises(ValueError):
                self.poll(**kwargs)


class WorkerExitCodeTests(unittest.TestCase):
    """A backlog is not a fault; only a fault is."""

    def test_a_pending_backlog_is_reported_but_not_a_failure(self):
        from dalton_core import research_output_preparation as rop
        source = Path(rop.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "return 0 if result['status'] in {'healthy','no_current_mission','pending'} else 1",
            source)

    def test_optional_batch_bounds_are_accepted_and_checked(self):
        from dalton_core.research_output_preparation import validate_worker_config
        base = {
            "schema_version": "research-publication-worker-config:0.1",
            "workers": 4, "chunk_chars": 4500, "max_cost_per_call": 1.0,
            "draft_attempts": 2, "publication_gate": {},
        }
        for name in ("core_db", "scheduler_db", "model_config", "verifier_config",
                     "checker_config", "brain_config", "work_dir", "output_directory"):
            base[name] = f"/tmp/{name}"
        validate_worker_config(dict(base))
        validate_worker_config({**base, "ui_text_batches_per_run": 2,
                                "ui_text_max_attempts": 6})
        with self.assertRaises(ValueError):
            validate_worker_config({**base, "ui_text_max_attempts": 0})
        with self.assertRaises(ValueError):
            validate_worker_config({**base, "unexpected_key": 1})


if __name__ == "__main__":
    unittest.main()
