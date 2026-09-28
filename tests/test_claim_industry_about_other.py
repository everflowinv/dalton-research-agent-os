"""2026-09-28: a support retirement "about another subject" can be industry evidence.

The morning-note support check retires a Claim on ``citation_support_rejected``
when an independent model finds that the cited sentences support it but that
it is about someone else (``subject_relation='about_other'``), and names who
(``other_subject``).  Right at company level; a loss when the someone is the
mission's industry ("IT Services sector", "hyperscalers").  Such a Claim is
reattributed to the mission's own industry only when every governing verdict is
supported and about another subject, the subject is this mission's industry
(never a company, another industry or another region), and the deterministic
industry-level rule (v2) fires on the exact span.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import dalton_core.claim_support_backfill as backfill_module
import dalton_core.claim_support_verification as verification_module
from dalton_core.claim_industry_reattribution import (
    ABOUT_OTHER_RULE_REF,
    ClaimIndustryReattributionAuthority,
    ClaimReattributionConflict,
    RULE_REF,
    industry_reattributions,
)
from dalton_core.claim_industry_rule import other_subject_scope, statement_names_industry
from dalton_core.claim_retirement import retired_claim_version_refs
from tests.test_claim_retirement import ACN, AUTOMATION, EPAM, OWNER
from tests.test_claim_support_backfill import V1, BackfillHarness, StatementModel

CTSH = "company:sec-cik:0001058290"
HYPERSCALER = "industry:美国-hyperscaler-云计算与-ai-基础设施"
IT_SERVICES = "industry:us-it-services"
CLOUD_ROSTER = {"company:ticker:amzn": ["amazon", "aws", "amzn"],
                "company:ticker:msft": ["microsoft", "azure", "msft"],
                "company:ticker:meta": ["meta"], "company:ticker:googl": ["google", "alphabet"]}
IT_ROSTER = {ACN: ["accenture", "acn"], CTSH: ["cognizant", "ctsh"], EPAM: ["epam"]}

SURVEY = ("Most surveyed enterprise decision makers expect discretionary IT services "
          "spending with external vendors to recover only beyond 2026.")
OUTSOURCING = ("Most enterprise buyers plan to keep IT services spending with outsourcing "
               "vendors flat through the end of 2026.")
PUBLIC_SECTOR = ("Enterprise demand for IT services from public sector buyers stayed strong "
                 "across vendors during the quarter.")
CONSULTING = ("Enterprises plan to shift consulting budgets toward vendors with proven AI "
              "delivery capability over the next year.")
PRICING = ("Enterprise clients keep discretionary IT services budgets flat while vendors "
           "compete harder on pricing for new deals.")


def scope(name, industry=HYPERSCALER, roster=CLOUD_ROSTER):
    return other_subject_scope(name, industry_ref=industry, roster=roster)


class OtherSubjectRuleTests(unittest.TestCase):
    def test_a_company_is_never_the_industry(self) -> None:
        for name in ("CoreWeave", "Infineon Technologies (IFX)", "Anthropic", "OKLO",
                     "Big Tech / Microsoft", "GOOG / Tech sector", "S&P 500 / AI sector",
                     "Amazon (Amazon.com, 亚马逊, AWS, Amazon Web Services, AMZN)"):
            verdict = scope(name)
            self.assertFalse(verdict["ok"], name)
            self.assertTrue(verdict["refusal"].startswith("other_subject_names_company"), verdict)
        verdict = scope("digital engineering players (EPAM, GLOB, GDYN)", IT_SERVICES, IT_ROSTER)
        self.assertTrue(verdict["refusal"].startswith("other_subject_names_company"), verdict)

    def test_the_missions_industry_and_its_umbrellas(self) -> None:
        for name in ("hyperscalers", "Cloud Service Providers (CSPs)", "US Hyperscalers",
                     "AI Capex / Cloud Service Providers", "Hyperscalers / Mag7",
                     "AI compute market / Neoclouds"):
            self.assertEqual((scope(name)["ok"], scope(name)["scope"]), (True, "industry"), name)
        self.assertEqual(scope("AI trade / Tech Sector")["scope"], "umbrella")
        self.assertEqual(scope("IT Services sector", IT_SERVICES, IT_ROSTER)["scope"], "industry")
        # A generic part says nothing of its own.
        self.assertTrue(scope("IT Services vendors / industry", IT_SERVICES, IT_ROSTER)["ok"])
        self.assertEqual(scope("US Info Tech Sector", IT_SERVICES, IT_ROSTER)["scope"], "umbrella")

    def test_another_industry_or_region_is_not_the_missions(self) -> None:
        for name, refusal in (
            ("European Semiconductor Companies", "other_subject_other_region"),
            ("China AI data center sector", "other_subject_other_region"),
            ("Security and Data Infra software sector", "other_subject_other_industry"),
            ("Semiconductors / AI Compute Ecosystem", "other_subject_other_industry"),
            ("Hyperscalers / Industrials", "other_subject_other_industry"),
            ("AI Infrastructure / Semiconductors", "other_subject_other_industry"),
            ("US Consumer Sector", "other_subject_other_industry"),
            ("hyperscalers / market", "other_subject_other_industry"),
        ):
            verdict = scope(name)
            self.assertFalse(verdict["ok"], name)
            self.assertTrue(verdict["refusal"].startswith(refusal), verdict)
        for name, refusal in (
            ("Indian IT Services", "other_subject_other_region"),
            ("European IT Services", "other_subject_other_region"),
            ("Software and IT Services sector", "other_subject_other_industry"),
            ("Semiconductors vs Software", "other_subject_other_industry"),
        ):
            verdict = scope(name, IT_SERVICES, IT_ROSTER)
            self.assertTrue(str(verdict["refusal"]).startswith(refusal), (name, verdict))

    def test_no_name_or_no_lexicon_refuses(self) -> None:
        self.assertEqual(scope(None)["refusal"], "other_subject_missing")
        self.assertEqual(scope("  ")["refusal"], "other_subject_missing")
        self.assertEqual(scope("hyperscalers", "industry:industrial-robotics")["refusal"],
                         "no_industry_lexicon")

    def test_an_umbrella_needs_the_industry_in_the_statement(self) -> None:
        self.assertEqual(statement_names_industry(
            "The note says the AI trade is only stabilizing and still de-grossing.", HYPERSCALER), [])
        self.assertEqual(statement_names_industry(
            "Hyperscaler capex revisions drove the AI trade higher across the group.", HYPERSCALER),
            ["hyperscaler"])


class _AboutOther(BackfillHarness):
    def setUp(self) -> None:
        super().setUp()
        self.reattributions = ClaimIndustryReattributionAuthority(self.store)
        self.answers: dict[str, tuple] = {}
        self.verifier.model_call = StatementModel(self.answers)

    def filed(self, statement: str, *, subject: str = CTSH) -> dict:
        source = "Payments, Processors, and IT Services weekly. " + statement + " More filler."
        start = source.index(statement)
        return self.claim(subject=subject, statement=statement, source=source,
                          span=(start, start + len(statement)))

    def retire(self, answers: dict[str, tuple], *, contract: str | None = None) -> dict:
        self.answers.update(answers)
        self.verifier.model_call = StatementModel(self.answers)
        if contract is None:
            return self.backfill().run_once(max_items=20)
        with patch.object(verification_module, "CONTRACT_REF", contract), \
                patch.object(backfill_module, "CONTRACT_REF", contract):
            return self.backfill().run_once(max_items=20)

    def reattribution_driver(self):
        from dalton_core.claim_review import ClaimReviewDriver

        return ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=self.spool, needles={EPAM: ["epam"], ACN: ["accenture", "acn"],
                                       CTSH: ["cognizant", "ctsh"]},
            reattributions=self.reattributions)

    def tick(self, principal: str | None = AUTOMATION) -> dict:
        return self.reattribution_driver().reattribute_industry_findings(principal=principal, show=None)


class BackfillTests(_AboutOther):
    def test_supported_about_the_industry_is_kept_as_industry_evidence_once(self) -> None:
        self.grant_claim_challenge()
        kept = self.filed(SURVEY)
        company = self.filed(OUTSOURCING)
        region = self.filed(PUBLIC_SECTOR)
        unsupported = self.filed(PRICING)
        acted = self.retire({
            SURVEY: ("supported", "about_other", "IT Services sector"),
            OUTSOURCING: ("supported", "about_other", "Globant (GLOB)"),
            PUBLIC_SECTOR: ("supported", "about_other", "European IT Services"),
            PRICING: ("not_supported", "about_other", "IT Services sector"),
        })
        retired = {kept["ref"], company["ref"], region["ref"], unsupported["ref"]}
        self.assertEqual(set(acted["retired"]), retired, acted)
        before = retired_claim_version_refs(self.store.connection)

        first = self.tick()
        self.assertEqual([item["claim_version_ref"] for item in first["reattributed"]],
                         [kept["ref"]], json.dumps(first, indent=1, default=str)[-3000:])
        refusals = {item["claim_version_ref"]: item["refusal"] for item in first["refused_examples"]}
        self.assertTrue(refusals[company["ref"]].startswith("other_subject_names_company"))
        self.assertTrue(refusals[region["ref"]].startswith("other_subject_other_region"))
        self.assertEqual(refusals[unsupported["ref"]], "verdict_not_supported_about_other")
        [record] = self.reattributions.reattributions()
        self.assertEqual((record["industry_ref"], record["retired_reason_code"], record["rule_ref"],
                          record["actor_ref"]),
                         (IT_SERVICES, "citation_support_rejected", RULE_REF, AUTOMATION))
        evidence = record["rule_evidence"]["about_other"]
        self.assertEqual((evidence["rule_ref"], evidence["other_subjects"], evidence["scope"],
                          evidence["contract_ref"]),
                         (ABOUT_OTHER_RULE_REF, ["IT Services sector"], "industry",
                          verification_module.CONTRACT_REF))
        self.assertIn("IT Services sector", record["rationale"])
        # The retirement is untouched: company-level reads still skip it.
        self.assertEqual(retired_claim_version_refs(self.store.connection), before)
        self.assertEqual(set(industry_reattributions(self.store.connection, IT_SERVICES)),
                         {kept["ref"]})

        # Idempotent: nothing is read or written again.
        reads = self.spool.reads
        again = self.tick()
        self.assertEqual((again["reattributed"], again["examined"], again["already_reviewed"]),
                         ([], 0, 3), again)
        self.assertEqual(self.spool.reads, reads)
        self.assertEqual(len(self.reattributions.reattributions()), 1)

    def test_an_umbrella_subject_needs_the_industry_named_by_the_statement(self) -> None:
        self.grant_claim_challenge()
        survey = self.filed(SURVEY)
        consulting = self.filed(CONSULTING)
        self.retire({SURVEY: ("supported", "about_other", "US Info Tech Sector"),
                     CONSULTING: ("supported", "about_other", "US Info Tech Sector")})
        result = self.tick()
        self.assertEqual([item["claim_version_ref"] for item in result["reattributed"]],
                         [survey["ref"]], result)
        [refused] = [item for item in result["refused_examples"]
                     if item["claim_version_ref"] == consulting["ref"]]
        self.assertEqual(refused["refusal"], "umbrella_subject_without_industry_in_statement")

    def test_the_verdicts_are_decided_before_any_original_is_read(self) -> None:
        self.grant_claim_challenge()
        self.filed(PRICING)
        self.filed(OUTSOURCING)
        self.retire({PRICING: ("not_supported", "about_subject", None),
                     OUTSOURCING: ("supported", "about_other", "Cognizant")})
        reads = self.spool.reads
        result = self.tick()
        self.assertEqual(self.spool.reads, reads)
        self.assertEqual(result["refusals"], {"verdict_not_supported_about_subject": 1,
                                              "other_subject_names_company": 1}, result)
        # A per-tick cap on verdict-only reviews: the rest wait.
        from dalton_core.claim_industry_reattribution import run_backfill

        self.filed(CONSULTING)
        self.retire({CONSULTING: ("not_supported", "about_other", "IT Services sector")})
        capped = run_backfill(self.reattribution_driver(), authority=self.reattributions,
                              principal=AUTOMATION, max_verdict_reviews=1)
        self.assertEqual((capped["examined"], capped["already_reviewed"]), (1, 2), capped)

    def test_without_the_grant_it_reports_and_writes_no_reattribution(self) -> None:
        self.grant_claim_challenge()
        kept = self.filed(SURVEY)
        self.retire({SURVEY: ("supported", "about_other", "IT Services sector")})
        held = self.tick(principal=None)
        self.assertEqual([item["claim_version_ref"] for item in held["would_reattribute"]],
                         [kept["ref"]])
        self.assertEqual(self.reattributions.reattributions(), [])


class RereviewTests(_AboutOther):
    def test_an_earlier_contracts_verdict_waits_for_the_rereview(self) -> None:
        self.grant_claim_challenge()
        kept = self.filed(SURVEY)
        dropped = self.filed(OUTSOURCING)
        self.retire({SURVEY: ("supported", "about_other", "IT Services sector"),
                     OUTSOURCING: ("supported", "about_other", "IT Services sector")},
                    contract=V1)
        self.assertEqual(retired_claim_version_refs(self.store.connection),
                         {kept["ref"], dropped["ref"]})
        waiting = self.tick()
        self.assertEqual((waiting["awaiting_rereview"], waiting["reattributed"],
                          waiting["examined"]), (2, [], 0), waiting)
        self.assertEqual(self.reattributions.reviews(), {})  # not marked: looked at again
        # The current contract's re-review answers; one verdict now says the
        # cited sentences do not support it.
        self.retire({SURVEY: ("supported", "about_other", "IT Services sector"),
                     OUTSOURCING: ("not_supported", "about_other", "IT Services sector")})
        done = self.tick()
        self.assertEqual([item["claim_version_ref"] for item in done["reattributed"]],
                         [kept["ref"]], done)
        self.assertEqual({item["claim_version_ref"]: item["refusal"]
                          for item in done["refused_examples"]},
                         {dropped["ref"]: "verdict_not_supported_about_other"})

    def test_a_later_contract_that_rejects_support_withdraws_the_reattribution(self) -> None:
        self.grant_claim_challenge()
        kept = self.filed(SURVEY)
        self.retire({SURVEY: ("supported", "about_other", "IT Services sector")})
        self.assertEqual(len(self.tick()["reattributed"]), 1)
        v5 = "claim-support-verification:v5"
        with patch.object(verification_module, "CONTRACT_REF", v5), \
                patch.object(backfill_module, "CONTRACT_REF", v5), \
                patch.object(backfill_module, "REREVIEW_PASS_REF", "claim-support-rereview:" + v5):
            driver = self.reattribution_driver()
            pending = driver.recheck_industry_reattributions(principal=AUTOMATION)
            self.assertEqual((pending["awaiting_rereview"], pending["withdrawn"]), (1, []), pending)
            rereview = self.retire({SURVEY: ("not_supported", "about_other",
                                             "IT Services sector")})["rereview"]
            self.assertEqual(rereview["still_rejected"], 1, rereview)
            withdrawn = driver.recheck_industry_reattributions(principal=AUTOMATION)
        self.assertEqual([item["claim_version_ref"] for item in withdrawn["withdrawn"]],
                         [kept["ref"]], withdrawn)
        self.assertEqual(industry_reattributions(self.store.connection), {})
        self.assertIn(kept["ref"], retired_claim_version_refs(self.store.connection))


class AuthorityTests(_AboutOther):
    def test_automation_needs_a_supported_verdict_about_the_industry(self) -> None:
        self.grant_claim_challenge()
        unsupported = self.filed(PRICING)
        company = self.filed(OUTSOURCING)
        self.retire({PRICING: ("not_supported", "about_other", "IT Services sector"),
                     OUTSOURCING: ("supported", "about_other", "Cognizant")})
        for claim, why in ((unsupported, "verdict_not_supported_about_other"),
                           (company, "other_subject_names_company")):
            source = "Payments, Processors, and IT Services weekly. "
            with self.assertRaises(ClaimReattributionConflict) as caught:
                self.reattributions.reattribute(
                    claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
                    cited_span=PRICING if claim is unsupported else OUTSOURCING,
                    source_text=source)
            self.assertIn(why, str(caught.exception))
        # A person still may, on their own judgement.
        record = self.reattributions.reattribute(
            claim_version_ref=unsupported["ref"], actor_ref=OWNER, rationale="行业结论")
        self.assertEqual(record["reason_code"], "human_judgment")


if __name__ == "__main__":
    unittest.main()
