"""2026-09-25b: a lone "capex" is not the hyperscaler industry's word (rule v2).

ws-7d sales_notes:dc1dc728 -- GS on capital markets "heavily supported by AI
capex spend" -- was reattributed to the hyperscaler industry because the
lexicon counted "capex" on its own.  v2 counts a spending word only beside one
of the industry's subject words, and the reattributions v1 made that v2
refuses are withdrawn by an append-only record.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core import claim_industry_reattribution as module
from dalton_core.claim_industry_reattribution import (
    ClaimReattributionConflict,
    industry_reattributions,
    reattribution_state_probe,
    withdrawn_reattribution_refs,
)
from dalton_core.claim_industry_rule import PRIOR_RULE_REFS, RULE_REF, judge
from dalton_core.claim_retirement import retired_claim_version_refs, retirement_state_probe
from tests.test_claim_industry_reattribution import INDUSTRY, _Reattribution
from tests.test_claim_retirement import AUTOMATION, OWNER

HYPERSCALER = "industry:us-hyperscaler"
ROSTER = {
    "company:ticker:amzn": ["amazon", "amzn", "aws"],
    "company:ticker:googl": ["alphabet", "google", "googl"],
    "company:ticker:meta": ["meta", "facebook"],
    "company:ticker:msft": ["microsoft", "msft", "azure"],
}
GS_CAPITAL_MARKETS = ("GS cited a GS research note arguing that near-term capital markets "
                      "activity is heavily supported by AI capex spend, with a majority of "
                      "investment banking fee and volume growth described as AI driven.")
V1 = "claim-industry-reattribution:industry-level:v1"


def hyperscaler(statement: str) -> dict:
    return judge(statement=statement, cited_span="a span that names no company at all",
                 industry_ref=HYPERSCALER, roster=ROSTER)


class CapexCoOccurrenceTests(unittest.TestCase):
    def test_the_rule_is_v2_and_v1_is_the_prior(self) -> None:
        self.assertEqual(RULE_REF, "claim-industry-reattribution:industry-level:v2")
        self.assertIn(V1, PRIOR_RULE_REFS)

    def test_capex_alone_is_not_the_industrys(self) -> None:
        verdict = hyperscaler(GS_CAPITAL_MARKETS)
        self.assertFalse(verdict["industry_level"])
        self.assertEqual(verdict["refusal"], "spending_term_without_industry_subject:capex")
        verdict = hyperscaler("A searing market debate persists over the sustainability of "
                              "large-scale AI capex, even as primary trends remain intact.")
        self.assertFalse(verdict["industry_level"])
        verdict = hyperscaler("The note's author argues that moderating capital expenditure "
                              "across the market is a large leap from pacing frontier models.")
        self.assertTrue(verdict["refusal"].startswith("spending_term_without_industry_subject"))

    def test_capex_beside_an_industry_subject_word_still_is(self) -> None:
        for statement in (
            "Capex growth expectations for the hyperscaler group were revised upward for the "
            "near and following year versus prior forecasts.",
            "Investor bogeys for cloud capex across the group sit above the street, with "
            "investors focused on the gap.",
            "Big tech capex expectations across the group keep rising into results season, "
            "according to the desk.",
            "Data center capex across the industry is expected to stay elevated for years.",
        ):
            verdict = hyperscaler(statement)
            self.assertTrue(verdict["industry_level"], (statement, verdict))
        # A lexicon term that stands on its own is unchanged.
        self.assertTrue(hyperscaler(
            "Power availability across the market is the binding constraint for new data "
            "centers, according to utilities.")["industry_level"])


class WithdrawalTests(_Reattribution):
    """Mechanism tests under the fixture's IT-services mission: a v1
    reattribution that today's rule refuses is withdrawn, append-only."""

    REFUSED = {"rule_ref": RULE_REF, "industry_level": False,
               "refusal": "spending_term_without_industry_subject:capex"}

    def v1_reattribution(self) -> dict:
        claim = self.retired()
        with patch.object(module, "RULE_REF", V1):
            record = self.reattributions.reattribute(
                claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="v1",
                cited_span=claim["span"], source_text=claim["source"])
        self.assertEqual(record["rule_ref"], V1)
        return {**claim, "reattribution": record}

    def test_the_patrol_withdraws_what_todays_rule_refuses_once(self) -> None:
        self.grant_claim_challenge()
        claim = self.v1_reattribution()
        self.assertIn(claim["ref"], industry_reattributions(self.store.connection))
        probe = reattribution_state_probe(self.store.connection)
        retired_probe = retirement_state_probe(self.store.connection)
        with patch.object(module, "judge", return_value=self.REFUSED):
            summary = self.driver().run_once()
        recheck = summary["industry_reattribution_recheck"]
        self.assertEqual([item["claim_version_ref"] for item in recheck["withdrawn"]],
                         [claim["ref"]])
        self.assertEqual(summary["status"], "acted")
        # The industry no longer reads it; the company-level retirement stands.
        self.assertNotIn(claim["ref"], industry_reattributions(self.store.connection))
        self.assertIn(claim["ref"], retired_claim_version_refs(self.store.connection))
        self.assertEqual(withdrawn_reattribution_refs(self.store.connection),
                         {claim["reattribution"]["id"]})
        self.assertNotEqual(probe, reattribution_state_probe(self.store.connection))
        self.assertNotEqual(retired_probe, retirement_state_probe(self.store.connection))
        row = self.store.connection.execute(
            "SELECT reason_code, rule_ref, actor_ref FROM "
            "claim_industry_reattribution_withdrawals").fetchone()
        self.assertEqual(tuple(row), ("not_industry_level_under_current_rule", RULE_REF,
                                      AUTOMATION))
        for sql in ("UPDATE claim_industry_reattribution_withdrawals SET rule_ref='x'",
                    "DELETE FROM claim_industry_reattribution_withdrawals"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(sql)
        # Next tick: nothing left to recheck, and the backfill does not re-add it.
        with patch.object(module, "judge", return_value=self.REFUSED):
            again = self.driver().run_once()
        self.assertEqual(again["industry_reattribution_recheck"]["candidates"], 0)
        self.assertEqual(again["industry_reattribution"]["reattributed"], [])

    def test_a_reattribution_todays_rule_keeps_is_confirmed_and_not_read_again(self) -> None:
        self.grant_claim_challenge()
        claim = self.v1_reattribution()
        first = self.driver().run_once()["industry_reattribution_recheck"]
        self.assertEqual((first["confirmed"], first["withdrawn"]), (1, []))
        reads = self.spool.reads
        second = self.driver().run_once()["industry_reattribution_recheck"]
        self.assertEqual(second["already_reviewed"], 1)
        self.assertEqual(self.spool.reads, reads)
        self.assertIn(claim["ref"], industry_reattributions(self.store.connection))

    def test_without_a_principal_it_only_reports(self) -> None:
        self.grant_claim_challenge()
        claim = self.v1_reattribution()
        with patch.object(module, "judge", return_value=self.REFUSED):
            summary = self.driver().recheck_industry_reattributions(principal=None)
        self.assertEqual([item["claim_version_ref"] for item in summary["would_withdraw"]],
                         [claim["ref"]])
        self.assertEqual(summary["withdrawn"], [])
        self.assertIn(claim["ref"], industry_reattributions(self.store.connection))

    def test_who_may_withdraw(self) -> None:
        self.grant_claim_challenge()
        claim = self.v1_reattribution()
        # Automation: only when today's rule, re-run here, refuses.
        with self.assertRaises(ClaimReattributionConflict):
            self.reattributions.withdraw(
                claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
                cited_span=claim["span"], source_text=claim["source"])
        with self.assertRaises(ClaimReattributionConflict):  # never unverified
            self.reattributions.withdraw(claim_version_ref=claim["ref"], actor_ref=AUTOMATION,
                                         rationale="r")
        with self.assertRaises(ClaimReattributionConflict):
            self.reattributions.withdraw(claim_version_ref=claim["ref"], actor_ref=OWNER,
                                         rationale="r", reattribution_hash="0" * 64)
        # A person may withdraw any, once.
        record = self.reattributions.withdraw(
            claim_version_ref=claim["ref"], actor_ref=OWNER, rationale="讲的是资本市场",
            reattribution_hash=claim["reattribution"]["content_hash"])
        self.assertEqual((record["status"], record["reason_code"]), ("fresh", "human_judgment"))
        again = self.reattributions.withdraw(claim_version_ref=claim["ref"], actor_ref=OWNER,
                                             rationale="again")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(industry_reattributions(self.store.connection, INDUSTRY), {})

    def test_automation_never_withdraws_a_current_rule_or_a_persons_reattribution(self) -> None:
        self.grant_claim_challenge()
        claim = self.retired()
        self.reattributions.reattribute(claim_version_ref=claim["ref"], actor_ref=OWNER,
                                        rationale="a person said so")
        with patch.object(module, "judge", return_value=self.REFUSED):
            with self.assertRaises(ClaimReattributionConflict):
                self.reattributions.withdraw(
                    claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
                    cited_span=claim["span"], source_text=claim["source"])
            summary = self.driver().run_once()["industry_reattribution_recheck"]
        self.assertEqual(summary["candidates"], 0)


class DoorTests(unittest.TestCase):
    def test_the_owner_door_and_the_cli(self) -> None:
        from dalton_core import claim_industry_reattribution_cli as cli
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS,
            OPERATION_ACTOR_FIELDS,
            OPERATION_FIELDS,
        )

        self.assertIn("withdraw_industry_reattribution", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS["withdraw_industry_reattribution"], frozenset({
            "claim_version_ref", "reattribution_hash", "rationale", "actor_ref"}))
        self.assertEqual(OPERATION_ACTOR_FIELDS["withdraw_industry_reattribution"], "actor_ref")
        self.assertEqual(cli.WITHDRAW_OPERATION, "withdraw_industry_reattribution")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["withdraw", "--state-dir", "/nonexistent"])


if __name__ == "__main__":
    unittest.main()
