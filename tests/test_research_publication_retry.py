"""E7: publication products get a formal retry, and one automatic one.

Live 2026-09-25 legacy had 19 current products parked in ``pending`` for the
same source hash, which ``poll_once`` never prepares again: budget refusals and
spent pools from 09-14/15, "this request is already running", an oversized
attachment, the IBM initial screen (125d161b223a) and the weekly brief
(0f1dd7ae07e1) on "language checker quote is not in its source section", and
the NO_CHANGE judgements fixed by the check-only fallback.  The only way out
was deleting the state file by hand.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from dalton_core import research_publication_worker as worker
from dalton_core.research_publication_worker import (
    AUTO_RETRY_ROUND, EXHAUSTED_STATUS, poll_once, request_retry, retry_chain, retry_class)


MISSION = {"universe": [{"company_ref": "company:a"}]}
PRODUCT = {"kind": "initial_screen", "subject_ref": "company:a", "version_ref": "screen:1",
           "status": "available", "sections": [{"title": "结论", "body": "正文", "gaps": []}]}


def reader(connection, mission, company):
    return {"products": [PRODUCT]}


def failed(error):
    return {"status": "pending", "receipt": {"status": "incomplete", "failures": [
        {"product": 0, "section_start": 0, "error": error}]}}


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "products"
        self.outcomes = []
        self.prepared = 0

    def prepare(self, product):
        self.prepared += 1
        return self.outcomes.pop(0)

    def poll(self):
        return poll_once(None, MISSION, state_dir=self.state, prepare=self.prepare,
                         library_reader=reader)

    def identity_hash(self):
        return worker._hash(worker._identity(PRODUCT))

    # -- classification ---------------------------------------------------
    def test_retry_classes_cover_only_transient_or_fixed_failures(self):
        cases = {
            "today's research budget refused the call: thesis-impact budget refused the call "
            "(mission_budget_exceeded)": "budget_refused",
            "skipped:pool_exhausted: the coverage pool is spent for 2026-09-14": "daily_cap",
            "database is locked": "database_locked",
            "this request is already running": "already_running",
            "ValueError: display attachment exceeds the size limit": "attachment_size",
            "language checker quote is not in its source section": "checker_quote_unanchored",
            "check-only draft failed validation: localized section changed financial number "
            "tokens": "check_only_content",
            "language checker served an unexpected transport or model": None,
            "semantic verifier model is not independent of both authors": None,
        }
        for error, expected in cases.items():
            with self.subTest(error=error[:40]):
                self.assertEqual(retry_class(failed(error)), expected)
        self.assertEqual(retry_class({"reason": "database is locked"}), "database_locked")
        # One failure outside the classes holds the whole product.
        mixed = failed("database is locked")
        mixed["receipt"]["failures"].append({"error": "brain decision has an invalid shape"})
        self.assertIsNone(retry_class(mixed))

    # -- the automatic retry ---------------------------------------------
    def test_a_transient_failure_is_retried_once_and_only_once(self):
        self.outcomes = [failed("this request is already running"),
                         failed("this request is already running")]
        first = self.poll()
        self.assertEqual(first["pending"], 1)
        again = self.poll()
        self.assertEqual(again["retried"], 1)
        self.assertEqual(again["products"][0]["retry"], "automatic")
        [record] = retry_chain(self.state, self.identity_hash())
        self.assertEqual((record["trigger"], record["retry_class"], record["auto_round"]),
                         ("automatic", "already_running", AUTO_RETRY_ROUND))
        # The idempotency mark: failing again does not buy a third attempt.
        for _ in range(3):
            self.assertEqual(self.poll()["retried"], 0)
        self.assertEqual(self.prepared, 2)

    def test_a_non_transient_failure_is_not_retried_automatically(self):
        self.outcomes = [failed("language checker served an unexpected transport or model")]
        self.poll(); self.poll()
        self.assertEqual(self.prepared, 1)
        self.assertEqual(retry_chain(self.state, self.identity_hash()), [])

    def test_a_successful_automatic_retry_completes_the_product(self):
        self.outcomes = [failed("language checker quote is not in its source section"),
                         {"status": "completed"}]
        self.poll()
        self.assertEqual(self.poll()["completed"], 1)
        self.assertEqual(self.poll()["unchanged"], 1)
        self.assertEqual(self.prepared, 2)

    # -- the owner's retry -----------------------------------------------
    def test_owner_retry_is_a_dry_run_until_applied_and_reopens_once(self):
        self.outcomes = [failed("language checker served an unexpected transport or model"),
                         {"status": EXHAUSTED_STATUS, "receipt": {"status": "exhausted"}}]
        self.poll()
        dry = request_retry(self.state, self.identity_hash()[:12], actor_ref="human:owner",
                            reason="route fixed")
        self.assertEqual(dry["status"], "dry_run")
        self.assertEqual(retry_chain(self.state, self.identity_hash()), [])
        self.poll()
        self.assertEqual(self.prepared, 1)
        applied = request_retry(self.state, self.identity_hash()[:12], actor_ref="human:owner",
                                reason="route fixed", apply=True)
        self.assertEqual(applied["status"], "requested")
        self.assertEqual(applied["retry"]["sequence"], 1)
        # Asked twice before the worker ran: one record.
        self.assertEqual(request_retry(self.state, self.identity_hash()[:12],
                                       actor_ref="human:owner", reason="again", apply=True)["status"],
                         "already_requested")
        polled = self.poll()
        self.assertEqual(polled["products"][0]["retry"], "owner")
        self.assertEqual(polled["exhausted"], 1)
        # Consumed: the exhausted state stays put, and an owner can reopen it.
        self.poll()
        self.assertEqual(self.prepared, 2)
        self.outcomes = [{"status": "completed"}]
        request_retry(self.state, self.identity_hash()[:12], actor_ref="human:owner",
                      reason="once more", apply=True)
        self.assertEqual(self.poll()["completed"], 1)
        self.assertEqual([row["sequence"] for row in retry_chain(self.state, self.identity_hash())],
                         [1, 2])
        with self.assertRaisesRegex(ValueError, "already completed"):
            request_retry(self.state, self.identity_hash()[:12], actor_ref="human:owner",
                          reason="x", apply=True)

    def test_owner_retry_is_found_by_product_hash_and_needs_a_human_actor(self):
        self.outcomes = [failed("brain decision has an invalid shape")]
        self.poll()
        state = worker._read_state(self.state / (self.identity_hash() + ".json"))
        found = request_retry(self.state, state["product_hash"][:12], actor_ref="human:owner",
                              reason="checker fixed")
        self.assertEqual(found["identity_hash"], self.identity_hash())
        with self.assertRaisesRegex(ValueError, "human"):
            request_retry(self.state, state["product_hash"][:12], actor_ref="automation:x",
                          reason="x")
        with self.assertRaisesRegex(ValueError, "matches 0"):
            request_retry(self.state, "deadbeefdeadbeef", actor_ref="human:owner", reason="x")

    def test_a_tampered_retry_chain_stops_the_poll(self):
        self.outcomes = [failed("database is locked"), failed("database is locked")]
        self.poll(); self.poll()
        [path] = (self.state / "retries" / self.identity_hash()).glob("*.json")
        record = json.loads(path.read_text())
        record["actor_ref"] = "human:someone-else"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "retry chain drifted"):
            retry_chain(self.state, self.identity_hash())
        # The poll holds that product, on the record, and goes on.
        polled = self.poll()
        self.assertIn("drifted", polled["products"][0]["retry_error"])
        self.assertEqual(self.prepared, 2)

    def test_the_cli_is_retry_products_and_defaults_to_dry_run(self):
        from dalton_core import research_output_preparation as prep
        self.outcomes = [failed("brain decision has an invalid shape")]
        self.poll()
        out = io.StringIO()
        argv = ["x", "retry-products", "--state-dir", str(self.state),
                "--identity", self.identity_hash()[:12], "--actor", "human:owner",
                "--reason", "checked by hand"]
        with mock.patch("sys.argv", argv), redirect_stdout(out):
            self.assertEqual(prep.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "dry_run")
        self.assertEqual(retry_chain(self.state, self.identity_hash()), [])
        out = io.StringIO()
        with mock.patch("sys.argv", argv + ["--apply"]), redirect_stdout(out):
            prep.main()
        self.assertEqual(json.loads(out.getvalue())["status"], "requested")
        out = io.StringIO()
        with mock.patch("sys.argv", argv + ["--show"]), redirect_stdout(out):
            prep.main()
        self.assertEqual(len(json.loads(out.getvalue())["retries"]), 1)


if __name__ == "__main__":
    unittest.main()
