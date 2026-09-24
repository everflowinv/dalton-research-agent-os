"""2026-09-24: a formal retry for UI text batches, and recovery after a route fix.

Two live batches (``ui-text-batch:0296977a…`` and ``e9fbe811…``) were blocked
after six ``verifier_not_independent`` failures.  The only way to retry them was
to delete ``results/<digest>.json``, and even that could not work: the retry
reused the cached draft in ``stages/`` and the unclassified route decision that
produced it, so the verifier could never be shown independent.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import research_output_preparation as prep
from dalton_core.store import canonical_json, content_hash
from dalton_core.ui_text_discovery import (
    AUTOMATIC_RETRY_ACTOR, FAILURE_CLASS_ROUTE_FAMILY, LEGACY_RESULT_SCHEMA, _hash,
    failure_class, latest_retry, main, poll_ui_texts, request_retry)
from tests import test_research_output_preparation as preparation
from tests.test_research_output_preparation import SOURCE

ENGLISH = ("Management expects enterprise demand to improve during the next "
           "fiscal year across all major markets.")


def failed(error):
    return {"status": "pending", "receipt": {"chunks": 1, "failures": [
        {"error": error, "product": 0, "section_start": 0}]}}


class FormalRetryTests(unittest.TestCase):
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
        wire = {"id": "claim:1", "claim_ref": "claim:1", "subject_ref": "company:A",
                "normalized_statement": ENGLISH}
        self.db.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)", (
            "claim:1", "claim:1", 1, canonical_json(wire), content_hash(wire), None,
            "2026-09-01T00:00:00Z"))
        self.db.commit()
        self.calls = []
        self.outcomes = []
        self.fingerprint = "routes-v1"

    def prepare(self, product, **kwargs):
        self.calls.append(kwargs.get("redraft_generation", 0))
        return self.outcomes.pop(0) if self.outcomes else {"status": "completed"}

    def poll(self, **kwargs):
        library = {"products": [{"status": "available", "sections": [{"sources": [
            {"kind": "claim", "ref": "claim:1"}]}]}]}
        kwargs.setdefault("route_fingerprint", lambda: self.fingerprint)
        with patch("dalton_core.ui_text_discovery.research_library",
                   return_value=library):
            return poll_ui_texts(self.db, self.mission, state_dir=self.state,
                                 mapping={}, prepare=self.prepare, **kwargs)

    def result(self):
        return json.loads(next((self.state / "results").glob("*.json")).read_text("utf-8"))

    def batch_ref(self):
        return self.result()["batch_ref"]

    def test_failure_classes(self):
        self.assertEqual(failure_class(failed("verifier_not_independent")),
                         FAILURE_CLASS_ROUTE_FAMILY)
        self.assertEqual(failure_class(failed(
            "the model call did not succeed (MODEL_ROUTE_REJECTED)")),
            FAILURE_CLASS_ROUTE_FAMILY)
        self.assertEqual(failure_class({"reason": "CockpitModelError: verifier_not_independent"}),
                         FAILURE_CLASS_ROUTE_FAMILY)
        self.assertIsNone(failure_class(failed("brain suggestion decision index is invalid")))
        self.assertIsNone(failure_class(failed("no model route is available right now")))

    def test_a_route_family_failure_blocks_at_once_and_recovers_after_a_route_change(self):
        self.outcomes = [failed("verifier_not_independent")]
        first = self.poll()
        self.assertEqual(first["blocked"], 1)
        self.assertEqual(self.calls, [0])
        saved = self.result()
        self.assertEqual((saved["status"], saved["attempts"], saved["failure_class"],
                          saved["route_fingerprint"]),
                         ("blocked", 1, FAILURE_CLASS_ROUTE_FAMILY, "routes-v1"))
        self.assertIn("路由或模型家族", first["blocked_reasons"][0])
        # Same routes: nothing is spent, however often the worker polls.
        for _ in range(3):
            self.assertEqual(self.poll()["attempted"], 0)
        self.assertEqual(self.calls, [0])
        # The family is fixed: the poll retries from a fresh draft by itself.
        self.fingerprint = "routes-v2"
        fixed = self.poll()
        self.assertEqual(fixed["attempted"], 1)
        self.assertEqual(self.calls, [0, 1])
        self.assertEqual(self.result()["status"], "completed")
        retry = latest_retry(self.state, self.batch_ref())
        self.assertEqual((retry["generation"], retry["redraft"], retry["redraft_generation"],
                          retry["trigger"], retry["actor_ref"]),
                         (1, True, 1, "route_fingerprint_changed", AUTOMATIC_RETRY_ACTOR))
        self.assertEqual(retry["route_fingerprint"], "routes-v2")

    def test_a_route_that_is_still_broken_waits_for_the_next_change(self):
        self.outcomes = [failed("verifier_not_independent"),
                         failed("the model call did not succeed (MODEL_ROUTE_REJECTED)")]
        self.poll()
        self.fingerprint = "routes-v2"
        self.poll()
        self.assertEqual(self.calls, [0, 1])
        saved = self.result()
        self.assertEqual((saved["status"], saved["generation"], saved["route_fingerprint"]),
                         ("blocked", 1, "routes-v2"))
        self.poll()
        self.assertEqual(self.calls, [0, 1])
        self.fingerprint = "routes-v3"
        self.poll()
        self.assertEqual(self.calls, [0, 1, 2])
        self.assertEqual(self.result()["status"], "completed")

    def test_a_legacy_blocked_result_is_retried_once_the_worker_can_see_routes(self):
        # The live shape: a 0.1 result blocked after six verifier_not_independent.
        self.outcomes = [failed("brain suggestion decision index is invalid")]
        self.poll(route_fingerprint=None)
        path = next((self.state / "results").glob("*.json"))
        current = json.loads(path.read_text("utf-8"))
        legacy = {"schema_version": LEGACY_RESULT_SCHEMA, "batch_ref": current["batch_ref"],
                  "manifest_hash": current["manifest_hash"], "status": "blocked",
                  "attempts": 6, "result": failed("verifier_not_independent")}
        path.write_bytes((json.dumps({**legacy, "content_hash": _hash(legacy)},
                                     ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) + "\n").encode())
        # Without a route fingerprint nothing retries by itself.
        self.assertEqual(self.poll(route_fingerprint=None)["attempted"], 0)
        recovered = self.poll()
        self.assertEqual(recovered["attempted"], 1)
        self.assertEqual(self.calls[-1], 1)
        self.assertEqual(self.result()["status"], "completed")

    def test_the_owner_retry_is_append_only_and_dry_run_by_default(self):
        self.outcomes = [failed("brain suggestion decision index is invalid")] * 6
        for _ in range(6):
            self.poll()
        self.assertEqual(self.result()["status"], "blocked")
        ref = self.batch_ref()
        before = self.result()
        dry = request_retry(self.state, ref[len("ui-text-batch:"):][:12] + "…",
                            actor_ref="human:owner", reason="checker fixed")
        self.assertEqual((dry["status"], dry["redraft"], dry["generation"]),
                         ("dry_run", False, 1))
        self.assertIsNone(latest_retry(self.state, ref))
        with self.assertRaisesRegex(ValueError, "human"):
            request_retry(self.state, ref, actor_ref="automation:x", reason="r", apply=True)
        done = request_retry(self.state, ref, actor_ref="human:owner",
                             reason="checker fixed", apply=True)
        self.assertEqual(done["status"], "requested")
        self.assertEqual(self.result(), before)  # nothing deleted or rewritten
        again = request_retry(self.state, ref, actor_ref="human:owner",
                              reason="checker fixed", apply=True)
        self.assertEqual(again["status"], "already_requested")
        # The next poll gives it a fresh attempt budget on the cached draft.
        self.poll()
        self.assertEqual(self.calls[-1], 0)
        self.assertEqual((self.result()["status"], self.result()["generation"]),
                         ("completed", 1))
        with self.assertRaisesRegex(ValueError, "already completed"):
            request_retry(self.state, ref, actor_ref="human:owner", reason="r", apply=True)

    def test_the_owner_can_force_a_redraft_and_route_failures_redraft_by_default(self):
        self.outcomes = [failed("verifier_not_independent")]
        self.poll()
        ref = self.batch_ref()
        plan = request_retry(self.state, ref, actor_ref="human:owner", reason="family fixed")
        self.assertTrue(plan["redraft"])
        kept = request_retry(self.state, ref, actor_ref="human:owner", reason="x",
                             redraft=False)
        self.assertFalse(kept["redraft"])
        with patch("sys.stdout"):
            self.assertEqual(main(["retry", "--state-dir", str(self.state), "--batch", ref,
                                   "--actor", "human:owner", "--reason", "family fixed",
                                   "--apply"]), 0)
        self.poll()
        self.assertEqual(self.calls, [0, 1])


class RedraftGenerationTests(unittest.TestCase):
    """The preparer's half: a redraft generation discards the paid stage cache."""

    setUp = preparation.PreparationTests.setUp
    run_one = preparation.PreparationTests.run_one

    def test_generation_zero_replays_and_a_redraft_generation_calls_again(self):
        first = self.run_one()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.run_one(), first)
        self.assertEqual(len(self.calls), 4)
        ids = list(self.request_ids)
        redrafted = prep.run_chunk((0, 0, SOURCE), **self.args, redraft_generation=1)[2]
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.calls[4:], self.calls[:4])
        # Every paid call is a new request, so no work order is replayed.
        self.assertFalse(set(self.request_ids[4:]) & set(ids))
        self.assertNotEqual(redrafted["pipeline_identity"], first["pipeline_identity"])
        self.assertEqual(prep.redraft_identity("abc", 0), "abc")
        with self.assertRaises(ValueError):
            prep.redraft_identity("abc", -1)

    def test_the_route_fingerprint_moves_with_a_family_and_not_otherwise(self):
        root = Path(self.temp.name)
        db = root / "router.sqlite"
        connection = sqlite3.connect(db)
        connection.executescript("""
          CREATE TABLE model_endpoint_profile_versions(profile_id TEXT, version INTEGER,
            family TEXT);
          CREATE TABLE model_routing_policy_versions(policy_id TEXT, version INTEGER);
          INSERT INTO model_endpoint_profile_versions VALUES('profile:muse',1,'unclassified:muse');
          INSERT INTO model_routing_policy_versions VALUES('policy:cheap',1);
        """)
        connection.commit()
        config = root / "config.json"
        config.write_text(json.dumps({"model_router_db": str(db)}), encoding="utf-8")
        before = prep.model_route_fingerprint([config, config])
        self.assertEqual(prep.model_route_fingerprint([config]), before)
        connection.execute(
            "INSERT INTO model_endpoint_profile_versions VALUES('profile:muse',2,'meta')")
        connection.commit()
        connection.close()
        self.assertNotEqual(prep.model_route_fingerprint([config]), before)


if __name__ == "__main__":
    unittest.main()
