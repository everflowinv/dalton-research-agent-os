"""2026-09-24: the support check, once, for the Claims admitted before it existed.

A rejection goes down the path a wrong Claim already takes -- challenge, then
retirement, both append-only -- and the retirement authority re-reads the
recorded verdict bound to the exact claim version before it writes.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_retirement import (
    ClaimRetirementAuthority,
    ClaimRetirementConflict,
    REASON_CODES,
    RECORDED_VERDICT_REASONS,
)
from dalton_core.claim_support_backfill import PASS_REF, SUPPORT_REASON, ClaimSupportBackfill, run_backfill
from dalton_core.claim_support_verification import (
    BACKFILL_PURPOSE,
    ClaimSupportVerifier,
    recorded_rejection,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_claim_retirement import AUTOMATION, EPAM, OWNER, ClaimRetirementHarness
from tests.test_claim_support_verification import FakeModel, _reply

SOURCE = "EPAM Systems said engineering demand improved in the quarter. Other remarks followed."


class BackfillHarness(ClaimRetirementHarness):
    def setUp(self) -> None:
        super().setUp()
        self.model = FakeModel()
        self.spent = 0
        self.verifier = ClaimSupportVerifier(
            store=self.store, model_call=self.model, purpose=BACKFILL_PURPOSE,
            daily_cap_micros=500_000, spend_today=lambda _p, _d: self.spent,
            producer_family=lambda ref: "deepseek-v4", max_items_per_call=20)

    def backfill(self) -> ClaimSupportBackfill:
        reader = self.driver()
        original = reader._citations

        def citations():
            # The harness's correction sets carry no drafting rationale; the
            # production chain parses the route out of it.
            return {ref: {**item, "route_decision_ref": "route-decision:drafter"}
                    for ref, item in original().items()}

        reader._citations = citations
        return ClaimSupportBackfill(
            store=self.store, missions=self.missions, verifier=self.verifier, reader=reader,
            challenges=self.authority, claim_sources=["test"])

    def claims(self):
        good = self.claim(statement="EPAM said engineering demand improved.", source=SOURCE, span=(0, 62))
        bad = self.claim(statement="EPAM expects margins to expand sharply.", source=SOURCE, span=(0, 62))
        lost = self.claim(statement="EPAM noted wins.", source="EPAM lost bytes.", store_source=False)
        numeric = self.claim(statement="EPAM revenue", kind="quantitative", value=1, source=SOURCE)
        contested = self.claim(statement="EPAM something.", source=SOURCE)
        self.authority.challenge(
            claim_version_ref=contested["ref"], claim_version_hash=contested["hash"],
            reason_code="boilerplate_disclaimer", rationale="x", actor_ref=OWNER)
        return good, bad, lost, numeric, contested


class BackfillTests(BackfillHarness):
    def test_rejections_are_recorded_then_challenged_and_retired_once_granted(self) -> None:
        good, bad, lost, _numeric, _contested = self.claims()
        # Newest first: ``bad`` was created after ``good``.
        self.model.replies.append(_reply(("not_supported", "about_subject", None),
                                         ("supported", "about_subject", None)))
        first = self.backfill().run_once(max_items=10)
        self.assertEqual(first["remaining_before"], 3)  # good, bad, lost
        self.assertEqual((first["examined"], first["supported"], first["rejected"],
                          first["unreadable"]), (2, 1, 1, 1), first)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0]["purpose"], BACKFILL_PURPOSE)
        self.assertEqual(self.model.calls[0]["producer_route_decision_refs"], ["route-decision:drafter"])
        # No grant: detected, recorded, nothing challenged.
        self.assertEqual((first["detected"], first["challenged"], first["retired"]), (1, [], []))
        self.assertEqual(first["remaining_after"], 0)
        self.assertEqual(self.authority.retired_claim_version_refs(), set())

        self.grant_claim_challenge()
        second = self.backfill().run_once(max_items=10)
        self.assertEqual(len(self.model.calls), 1)  # nothing asked twice
        self.assertEqual((second["challenged"], second["retired"]), ([bad["ref"]], [bad["ref"]]))
        self.assertEqual(self.authority.retired_claim_version_refs(), {bad["ref"]})
        challenge = self.authority.challenges(limit=10)
        mine = [c for c in challenge if c["reason_code"] == SUPPORT_REASON]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["actor_ref"], AUTOMATION)
        self.assertIn("所引原文句子不支持", mine[0]["rationale"])

        third = self.backfill().run_once(max_items=10)
        self.assertEqual((third["detected"], third["challenged"], third["retired"], third["calls"]),
                         (0, [], [], 0))
        # The Ledger itself is untouched: every Claim version is still there.
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM claim_versions").fetchone()[0], 5)
        self.assertIsNone(recorded_rejection(
            self.store.connection, claim_version_ref=good["ref"],
            claim=self.authority._claim(good["ref"])))
        self.assertIsNotNone(lost)

    def test_a_statement_judged_at_admission_is_read_not_asked(self) -> None:
        good = self.claim(statement="EPAM said engineering demand improved.", source=SOURCE, span=(0, 62))
        from dalton_core.claim_support_verification import support_item
        item = support_item(subject_ref=EPAM, subject_name="EPAM",
                            statement="EPAM said engineering demand improved.",
                            cited_text=SOURCE[0:62], producer_route_ref="route-decision:drafter")
        self.model.replies.append(_reply(("supported", "about_subject", None)))
        self.verifier.verify(mission={"id": "m"}, items=[item])
        self.assertEqual(len(self.model.calls), 1)
        result = self.backfill().run_once(max_items=10)
        self.assertEqual((result["already_judged"], result["examined"], result["calls"]), (1, 1, 0))
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNotNone(good)

    def test_the_ceiling_leaves_claims_unmarked_for_a_later_run(self) -> None:
        self.claims()
        self.spent = 500_000
        result = self.backfill().run_once(max_items=10)
        self.assertEqual(self.model.calls, [])
        self.assertIn("daily ceiling", result["deferred"])
        self.assertEqual(result["remaining_after"], 2)  # the unreadable one is settled

    def test_a_run_is_bounded_and_the_next_one_continues(self) -> None:
        self.claims()
        self.model.replies.extend([_reply(("supported", "about_subject", None))] * 3)
        # Newest first: the unreadable Claim is settled on the way, and one
        # statement is asked about per run.
        first = self.backfill().run_once(max_items=1)
        self.assertEqual((first["examined"], first["unreadable"], first["remaining_after"]), (1, 1, 1))
        second = self.backfill().run_once(max_items=1)
        self.assertEqual((second["examined"], second["remaining_after"]), (1, 0))
        self.assertEqual(len(self.model.calls), 2)

    def test_an_unclassified_drafter_is_marked_and_never_asked(self) -> None:
        self.claims()
        self.verifier.producer_family = lambda ref: "unclassified:deepseek"
        result = self.backfill().run_once(max_items=10)
        self.assertEqual((result["unverifiable"], len(self.model.calls)), (2, 0))
        marks = self.store.connection.execute(
            "SELECT outcome, COUNT(*) FROM claim_support_backfill_marks WHERE pass_ref=? GROUP BY 1",
            (PASS_REF,)).fetchall()
        self.assertEqual({row[0]: row[1] for row in marks}, {"unverifiable": 2, "unreadable": 1})

    def test_claims_held_only_by_the_contract_wiring_bug_are_asked_again(self) -> None:
        # 2026-09-25: before the output contract was wired, every backfill call
        # was refused before it was sent, and after three such refusals a batch
        # was marked unverifiable -- permanently, as far as this pass went.
        from dalton_core.claim_support_backfill import RETRY_PASS_REF

        good, bad, _lost, _numeric, _contested = self.claims()
        wiring = ("the support check failed 3 times (last: CockpitModelError: the cheap chain "
                  "halted on unclassified_failure: ... the model call failed: independent "
                  "verifier WorkOrder lacks the required output schema version)")
        self.verifier.records.mark(claim_version_ref=good["ref"], claim_version_hash=good["hash"],
                                   pass_ref=PASS_REF, outcome="unverifiable", detail=wiring)
        self.verifier.records.mark(claim_version_ref=bad["ref"], claim_version_hash=bad["hash"],
                                   pass_ref=PASS_REF, outcome="unverifiable",
                                   detail="the drafting model family 'unclassified:x' is unclassified")
        self.model.replies.append(_reply(("not_supported", "about_subject", None)))
        result = self.backfill().run_once(max_items=10)
        # ``good`` is asked again; ``bad``'s hold was about its drafter and stands.
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual((result["examined"], result["rejected"]), (1, 1), result)
        marks = {(row[0], row[1]): row[2] for row in self.store.connection.execute(
            "SELECT claim_version_ref, pass_ref, outcome FROM claim_support_backfill_marks")}
        self.assertEqual(marks[(good["ref"], RETRY_PASS_REF)], "verdict")
        self.assertNotIn((bad["ref"], RETRY_PASS_REF), marks)
        # Settled: the next run has nothing left to ask, and the rejection is
        # acted on exactly as a first-pass one would be.
        self.assertEqual(result["remaining_after"], 0)
        self.assertEqual(result["detected"], 1)
        self.grant_claim_challenge()
        again = self.backfill().run_once(max_items=10)
        self.assertEqual((again["calls"], again["retired"]), (0, [good["ref"]]))


    def test_claims_marked_unverifiable_for_want_of_a_route_are_asked_again(self) -> None:
        # 2026-09-26: every call was refused with "no model route" from 00:00
        # UTC, and after three refusals a batch's Claims were marked
        # unverifiable -- under the first pass for some, and under the
        # after-contract-wiring pass for Claims that pass had just re-opened.
        from dalton_core.claim_support_backfill import RETRY_PASS_REF, ROUTE_RETRY_PASS_REF

        good, bad, _lost, _numeric, _contested = self.claims()
        route = ("the support check failed 3 times (last: CockpitModelRouteUnavailable: "
                 "no model route is available right now)")
        wiring = ("the support check failed 3 times (last: CockpitModelError: ... independent "
                  "verifier WorkOrder lacks the required output schema version)")
        self.verifier.records.mark(claim_version_ref=good["ref"], claim_version_hash=good["hash"],
                                   pass_ref=PASS_REF, outcome="unverifiable", detail=route)
        self.verifier.records.mark(claim_version_ref=bad["ref"], claim_version_hash=bad["hash"],
                                   pass_ref=PASS_REF, outcome="unverifiable", detail=wiring)
        self.verifier.records.mark(claim_version_ref=bad["ref"], claim_version_hash=bad["hash"],
                                   pass_ref=RETRY_PASS_REF, outcome="unverifiable", detail=route)
        self.model.replies.append(_reply(("supported", "about_subject", None),
                                         ("not_supported", "about_subject", None)))
        result = self.backfill().run_once(max_items=10)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(result["examined"], 2, result)
        marks = {(row[0], row[1]): row[2] for row in self.store.connection.execute(
            "SELECT claim_version_ref, pass_ref, outcome FROM claim_support_backfill_marks")}
        # Each gets its next free pass ref, and the verdict settles it.
        self.assertEqual(marks[(good["ref"], RETRY_PASS_REF)], "verdict")
        self.assertEqual(marks[(bad["ref"], ROUTE_RETRY_PASS_REF)], "verdict")
        self.assertEqual(result["remaining_after"], 0)

    def test_a_route_outage_now_defers_the_backfill_instead_of_marking(self) -> None:
        from dalton_core.cockpit_model import CockpitModelRouteUnavailable

        from datetime import datetime, timedelta, timezone

        moment = [datetime(2026, 9, 26, 0, 20, tzinfo=timezone.utc)]
        self.verifier.clock = lambda: moment[0]
        self.claims()
        self.model.replies.extend(
            [CockpitModelRouteUnavailable("no model route is available right now")] * 4)
        for _ in range(4):
            result = self.backfill().run_once(max_items=10)
            self.assertEqual(result["unverifiable"], 0, result)
            self.assertIsNotNone(result["deferred"])
            moment[0] += timedelta(hours=1)
        self.assertEqual(len(self.model.calls), 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM claim_support_backfill_marks WHERE outcome='unverifiable'"
        ).fetchone()[0], 0)


class AuthorityTests(BackfillHarness):
    def test_automation_cannot_retire_on_the_reason_without_a_recorded_rejection(self) -> None:
        claim = self.claim(statement="EPAM said engineering demand improved.", source=SOURCE)
        self.grant_claim_challenge()
        challenge = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code=SUPPORT_REASON, rationale="said so", actor_ref=AUTOMATION)
        self.assertEqual(challenge["detector_ref"], "claim-verifier:citation-support:v1")
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
                                  decision="retired", actor_ref=AUTOMATION, rationale="said so")
        # A person may still decide it either way.
        decision = self.authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="kept", actor_ref=OWNER, rationale="reads fine to me")
        self.assertEqual(decision["decision"], "kept")

    def test_the_reason_is_in_the_vocabulary_and_is_not_deterministic(self) -> None:
        self.assertIn(SUPPORT_REASON, REASON_CODES)
        self.assertIn(SUPPORT_REASON, RECORDED_VERDICT_REASONS)


class MigrationTests(unittest.TestCase):
    OLD_CHECK = ("'subject_absent_from_source',\n        'boilerplate_disclaimer',\n"
                 "        'human_judgment'\n    ")

    def test_an_existing_challenges_table_is_widened_without_losing_a_row(self) -> None:
        import dalton_core

        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        schema = (Path(dalton_core.__file__).parent / "claim_retirement_schema.sql").read_text(encoding="utf-8")
        old = schema.replace(",\n        'citation_support_rejected'\n    ", "\n    ")
        self.assertNotIn("citation_support_rejected", old)
        claim = {"id": "claim-version:" + "1" * 64, "claim_ref": "claim:test:1",
                 "subject_ref": EPAM, "normalized_statement": "x"}
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        with store._transaction() as cur:
            cur.execute("INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                        "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                        (claim["id"], claim["claim_ref"], 1, json.dumps(claim), claim["content_hash"],
                         "2026-09-01T00:00:00+00:00"))
        store.connection.create_function("dalton_claim_retirement_authorized", 0, lambda: 1)
        store.connection.executescript(old)
        store.connection.execute(
            "INSERT INTO claim_retirement_challenges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("challenge:1", claim["id"], claim["content_hash"], claim["claim_ref"], EPAM,
             "human_judgment", None, None, "r", OWNER, "{}", "0" * 64, "2026-09-01"))
        store.connection.execute(
            "INSERT INTO claim_retirement_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("decision:1", claim["id"], "challenge:1", "0" * 64, "kept", OWNER, "r", "{}",
             "0" * 64, "2026-09-01"))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO claim_retirement_challenges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("challenge:2", claim["id"], claim["content_hash"], claim["claim_ref"], EPAM,
                 SUPPORT_REASON, None, None, "r", OWNER, "{}", "0" * 64, "2026-09-01"))

        ClaimRetirementAuthority(store)
        sql = store.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='claim_retirement_challenges'").fetchone()[0]
        self.assertIn(SUPPORT_REASON, sql)
        self.assertEqual(store.connection.execute(
            "SELECT challenge_id FROM claim_retirement_challenges").fetchall()[0][0], "challenge:1")
        self.assertEqual(store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The append-only guards came back with the table.
        with self.assertRaises(sqlite3.DatabaseError):
            store.connection.execute("DELETE FROM claim_retirement_challenges")
        with self.assertRaises(sqlite3.DatabaseError):
            store.connection.execute("UPDATE claim_retirement_challenges SET rationale='y'")
        # And a second construction is a no-op.
        ClaimRetirementAuthority(store)


class SettingsTests(unittest.TestCase):
    def test_zero_batches_disables_the_backfill_before_anything_is_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "claim-support-verification.json").write_text(
                json.dumps({"backfill_batches_per_run": 0}), encoding="utf-8")
            self.assertEqual(run_backfill(store=None, missions=None, model_config={},
                                          scheduler_db=None, state_dir=root, spool=None),
                             {"status": "disabled"})


if __name__ == "__main__":
    unittest.main()
