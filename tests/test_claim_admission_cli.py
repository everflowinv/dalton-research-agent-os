"""2026-09-25b: retire what admission would hold today -- by hand, dry run first.

The Claims already admitted from the SEO statistics pages cannot be held any
more; they are retired through the writer's human door
(``retire_claim_by_hand``) by ``claim_admission_cli retire``, which is a dry run
unless a person applies it with the selection hash the dry run printed.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_claim_admission_quality import AMZN, GOOGL, NEWS_PAGE, STATS_THIRD_PARTY

RELATIVE_YEAR_STATEMENT = (
    "Amazon declined to forecast its spending for the following year, while its cloud "
    "chief called the potential AI business for the company 'just massive'.")


class RetireByHandTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_claim_retirement import ClaimRetirementHarness

        self.harness = ClaimRetirementHarness("run")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_a_person_retires_an_admitted_claim_once(self) -> None:
        from dalton_core.claim_retirement import (
            ClaimRetirementConflict,
            retired_claim_version_refs,
        )

        h = self.harness
        claim = h.claim(statement="Amazon's retail media ad spend share rose in 2026.",
                        source="TOP 20 AMAZON ADS STATISTICS 2026")
        with self.assertRaises(ClaimRetirementConflict):
            h.authority.retire_by_hand(claim_version_ref=claim["ref"],
                                       claim_version_hash=claim["hash"],
                                       actor_ref="automation:coverage-mission", rationale="r")
        with self.assertRaises(ClaimRetirementConflict):
            h.authority.retire_by_hand(claim_version_ref=claim["ref"],
                                       claim_version_hash="0" * 64,
                                       actor_ref="human:lumos", rationale="r")
        result = h.authority.retire_by_hand(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            actor_ref="human:lumos", rationale="SEO 统计汇编页，数字无原始出处")
        self.assertEqual(result["status"], "fresh")
        self.assertIn(claim["ref"], retired_claim_version_refs(h.store.connection))
        row = h.store.connection.execute(
            "SELECT reason_code, actor_ref FROM claim_retirement_challenges "
            "WHERE claim_version_ref=?", (claim["ref"],)).fetchone()
        self.assertEqual((row["reason_code"], row["actor_ref"]), ("human_judgment", "human:lumos"))
        again = h.authority.retire_by_hand(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            actor_ref="human:lumos", rationale="again")
        self.assertEqual(again["status"], "already_decided")
        # Still undoable the ordinary way.
        h.authority.reinstate(claim_version_ref=claim["ref"], actor_ref="human:lumos",
                              rationale="其实可信")
        self.assertNotIn(claim["ref"], retired_claim_version_refs(h.store.connection))


class DoorAndCliTests(unittest.TestCase):
    def test_the_owner_doors_are_human_governance_operations(self) -> None:
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS,
            OPERATION_ACTOR_FIELDS,
            OPERATION_FIELDS,
        )

        self.assertIn("retire_claim_by_hand", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS["retire_claim_by_hand"], frozenset({
            "claim_version_ref", "claim_version_hash", "rationale", "actor_ref"}))
        self.assertEqual(OPERATION_ACTOR_FIELDS["retire_claim_by_hand"], "actor_ref")

    def test_retire_is_a_dry_run_and_apply_needs_the_reviewed_selection(self) -> None:
        from dalton_core import claim_admission_cli as cli

        self.assertEqual(cli.OPERATION, "retire_claim_by_hand")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["retire", "--state-dir", "/nonexistent"])  # --reason is required
        with patch.object(cli, "replay", return_value={
                "selected": [{"claim_version_ref": "claim-version:a", "claim_version_hash": "h",
                              "statement": "s", "checks": ["statistics_compilation"]}],
                "selection_sha256": cli.selection_hash(["claim-version:a"])}):
            dry = cli.retire(Path("/nonexistent"), from_replay=["statistics_compilation"],
                             reason="r", apply=False, actor=None)
            self.assertEqual((dry["status"], dry["count"]), ("dry_run", 1))
            with self.assertRaises(SystemExit):  # apply without the reviewed hash
                cli.retire(Path("/nonexistent"), from_replay=["statistics_compilation"],
                           reason="r", apply=True, actor="human:lumos")
            with self.assertRaises(SystemExit):  # the selection moved since review
                cli.retire(Path("/nonexistent"), from_replay=["statistics_compilation"],
                           expect_selection="0" * 64, reason="r", apply=True,
                           actor="human:lumos")
            with patch("dalton_core.governance_cli.ephemeral_call",
                       return_value={"status": "fresh"}) as call:
                applied = cli.retire(
                    Path("/nonexistent"), from_replay=["statistics_compilation"],
                    expect_selection=dry["selection_sha256"], reason="r", apply=True,
                    actor="human:lumos")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(call.call_args.kwargs["operation"], "retire_claim_by_hand")
        self.assertEqual(call.call_args.kwargs["params"]["claim_version_ref"], "claim-version:a")

    def test_the_replay_names_what_admission_would_hold_today(self) -> None:
        from dalton_core.claim_admission_cli import judge_claim

        found = judge_claim(
            claim={"subject_ref": "company:ticker:googl", "period": "Q2 2026",
                   "normalized_statement": "Synergy Research Group measured the wider cloud "
                                           "market growing at its fastest rate in eight years."},
            evidence={"source_type": "public_web", "retrieved_at": "2026-09-24T11:00:00+00:00"},
            span=STATS_THIRD_PARTY.splitlines()[-1], text=STATS_THIRD_PARTY, title=None,
            published_at=None, needles=GOOGL, peers=AMZN)
        self.assertEqual(sorted(found), ["statistics_compilation", "web_subject_strict"])
        found = judge_claim(
            claim={"subject_ref": "company:ticker:amzn", "period": "2026",
                   "normalized_statement": RELATIVE_YEAR_STATEMENT},
            evidence={"source_type": "public_web", "retrieved_at": "2026-09-24T11:00:00+00:00"},
            span=NEWS_PAGE.splitlines()[-1], text=NEWS_PAGE, title=None, published_at=None,
            needles=AMZN, peers=GOOGL)
        self.assertEqual(sorted(found), ["relative_year"])

if __name__ == "__main__":
    unittest.main()
