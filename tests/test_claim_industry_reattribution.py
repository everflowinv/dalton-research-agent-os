"""2026-09-25: a retired industry-level Claim is kept as industry evidence.

The span-level subject-absent detector retires a Claim whose statement and
cited span never name the company it is filed under -- right at company
level, and a loss when the Claim is about the industry (the Wells Fargo
mid-year IT services CIO survey, filed 54 times under CTSH and EPAM).  The
ClaimVersion is frozen, so the Claim is never re-filed: an append-only
reattribution names it and the mission's industry, and only industry-level
reads pick it up.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_industry_reattribution import (
    ClaimIndustryReattributionAuthority,
    ClaimReattributionConflict,
    ClaimReattributionNotFound,
    RULE_REF,
    covering_missions,
    industry_reattributions,
    reattributed_claim_version_refs,
    reattribution_state_probe,
    run_backfill,
)
from dalton_core.claim_industry_rule import judge, main_clause
from dalton_core.claim_retirement import retired_claim_version_refs
from tests.test_claim_retirement import ACN, AUTOMATION, EPAM, OWNER, ClaimRetirementHarness

CTSH = "company:sec-cik:0001058290"
IBM = "company:sec-cik:0000051143"
INDUSTRY = "industry:us-it-services"
ROSTER = {
    ACN: ["accenture", "acn"], CTSH: ["cognizant", "ctsh"], EPAM: ["epam", "epam systems"],
    IBM: ["ibm", "red hat"], "company:sec-cik:001688568": ["dxc", "dxc technology"],
}
SURVEY = ("Most surveyed enterprise decision makers expect discretionary IT services "
          "spending with external vendors to recover only beyond 2026.")
SURVEY_DOC = ("Payments, Processors, and IT Services: Mid-Year IT Services CIO Survey Says... "
              + "Macro filler about rates and flows. " * 15 + SURVEY
              + " Respondents cited the Middle East conflict.")


def industry_level(statement: str, span: str = "a span that names no company at all",
                   industry: str = INDUSTRY, **kwargs) -> dict:
    return judge(statement=statement, cited_span=span, industry_ref=industry,
                 roster=ROSTER, **kwargs)


class IndustryRuleTests(unittest.TestCase):
    def test_a_survey_finding_about_the_industry_is_industry_level(self) -> None:
        verdict = industry_level(SURVEY, document_title="Mid-Year IT Services CIO Survey Says")
        self.assertTrue(verdict["industry_level"], verdict)
        self.assertEqual(verdict["rule_ref"], RULE_REF)
        self.assertEqual(verdict["form"], "company_free")
        self.assertIn("it services", verdict["industry_terms"])
        self.assertTrue(verdict["survey_source"])
        # A source the finding is attributed to is not a company it is about.
        self.assertTrue(industry_level(
            "Wells Fargo's June 2026 CIO survey found most enterprises partner with "
            "IT services vendors on AI projects.")["industry_level"])

    def test_a_single_company_or_an_unknown_name_is_company_level(self) -> None:
        for statement, refusal in (
            ("EPAM's advantage is attributed to greater focus on complex software "
             "engineering versus more commoditized areas of the IT services market.",
             "names_one_company"),
            ("Globant's guidance cut highlights continued weakness in discretionary "
             "spending for IT services vendors.", "single_company_word"),
            ("Kyndryl noted revenue headwinds from macro-induced extended sales cycles in "
             "IT services, a common theme from peers.", "names_unknown_proper_noun"),
            ("Management expects IT services demand from enterprise clients to recover "
             "in the second half.", "leans_on_antecedent"),
            ("The analyst sees the IT services market debate hinging on cyclical versus "
             "secular drivers.", "single_company_word"),
        ):
            verdict = industry_level(statement)
            self.assertFalse(verdict["industry_level"], statement)
            self.assertTrue(str(verdict["refusal"]).startswith(refusal), verdict)

    def test_a_comparison_across_covered_companies_is_industry_level_once_each(self) -> None:
        self.assertTrue(industry_level(
            "IT services spending plans tilt toward Accenture and EPAM among enterprise "
            "buyers, per the survey.")["industry_level"])
        dwelling = industry_level(
            "Enterprise buyers favour Accenture over EPAM in IT services, and Accenture "
            "gains share in consulting.")
        self.assertEqual(dwelling["refusal"], "comparison_dwells_on_one_company")

    def test_the_span_and_the_document_can_make_it_another_companys(self) -> None:
        self.assertEqual(industry_level(
            SURVEY, span="Cognizant said discretionary spending will recover later.",
        )["refusal"], "span_names_one_company")
        self.assertTrue(industry_level(
            SURVEY, span="Cognizant and EPAM both flagged it.")["industry_level"])
        verdict = industry_level(SURVEY, document_title="Accenture plc Q3 FY26 earnings call")
        self.assertTrue(verdict["refusal"].startswith("document_is_one_company_own"), verdict)
        self.assertEqual(industry_level(SURVEY, issuer_document=True)["refusal"],
                         "issuer_document")

    def test_the_industry_terms_must_be_the_finding_not_the_gloss(self) -> None:
        statement = ("The desk notes markets repricing toward rate hikes by year-end, a macro "
                     "overhang relevant to how investors value sustained IT services spending.")
        self.assertTrue(main_clause(statement).endswith("by year-end"))
        self.assertFalse(industry_level(statement)["industry_level"])
        self.assertEqual(industry_level(
            "Enterprises continue to review their budgets for the coming year.")["refusal"],
            "no_industry_term")

    def test_an_industry_without_a_lexicon_reattributes_nothing(self) -> None:
        self.assertEqual(industry_level(SURVEY, industry="industry:semiconductors")["refusal"],
                         "no_industry_lexicon")
        self.assertEqual(industry_level(SURVEY, industry="company:x")["refusal"],
                         "no_industry_lexicon")
        # Hyperscaler lexicon, selected by a word of the mission's own ref.
        self.assertTrue(judge(
            statement="Capex growth expectations for the hyperscaler group were revised "
                      "upward for both years ahead of Q2 results.",
            cited_span="Hyperscaler capex tracker raised.",
            industry_ref="industry:美国-hyperscaler-云计算与-ai-基础设施",
            roster={"company:ticker:amzn": ["amazon", "aws"],
                    "company:ticker:meta": ["meta"]})["industry_level"])

    def test_a_chinese_statement_must_put_the_collective_first(self) -> None:
        self.assertTrue(industry_level(
            "行业调研显示多数企业客户预计 IT服务 可自由支配支出要到 2026 年以后才恢复。")["industry_level"])
        self.assertFalse(industry_level(
            "尤其值得注意的是某咨询商称其 IT服务 需求和行业客户预算正在回暖。")["industry_level"])


class _Reattribution(ClaimRetirementHarness):
    def setUp(self) -> None:
        super().setUp()
        self.reattributions = ClaimIndustryReattributionAuthority(self.store)

    def retired(self, *, statement: str = SURVEY, source: str = SURVEY_DOC,
                subject: str = CTSH, needles: tuple[str, ...] = ("cognizant", "ctsh")) -> dict:
        start = source.index(statement) if statement in source else 0
        claim = self.claim(subject=subject, statement=statement, source=source,
                           span=(start, start + len(statement)))
        challenge = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="subject_absent_from_source", rationale="原文里没有这家公司",
            actor_ref=AUTOMATION)
        decision = self.authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref=AUTOMATION, rationale="r",
            subject_needles=list(needles), source_text=source)
        return {**claim, "span": source[start:start + len(statement)], "source": source,
                "decision_hash": decision["content_hash"]}

    def driver(self):
        from dalton_core.claim_review import ClaimReviewDriver

        return ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=self.spool, needles={EPAM: ["epam"], ACN: ["accenture", "acn"]},
            reattributions=self.reattributions)


class AuthorityTests(_Reattribution):
    def test_automation_needs_the_grant_and_the_rule(self) -> None:
        claim = self.retired()
        with self.assertRaises(ClaimReattributionConflict):  # no claim_challenge grant
            self.reattributions.reattribute(
                claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
                cited_span=claim["span"], source_text=claim["source"])
        self.grant_claim_challenge()
        with self.assertRaises(ClaimReattributionConflict):  # never unverified
            self.reattributions.reattribute(
                claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r")
        with self.assertRaises(ClaimReattributionConflict):  # the rule is re-run here
            self.reattributions.reattribute(
                claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
                cited_span="Cognizant said so.", source_text=claim["source"])
        with self.assertRaises(ClaimReattributionConflict):  # the mission's industry only
            self.reattributions.reattribute(
                claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
                industry_ref="industry:elsewhere", cited_span=claim["span"])
        with self.assertRaises(ClaimReattributionConflict):  # another principal
            self.reattributions.reattribute(
                claim_version_ref=claim["ref"], actor_ref="automation:someone-else",
                rationale="r", cited_span=claim["span"])
        retired_before = retired_claim_version_refs(self.store.connection)
        decisions_before = self.store.connection.execute(
            "SELECT record_json, content_hash FROM claim_retirement_decisions").fetchall()
        record = self.reattributions.reattribute(
            claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="r",
            decision_hash=claim["decision_hash"], cited_span=claim["span"],
            source_text=claim["source"])
        self.assertEqual(record["status"], "fresh")
        self.assertEqual(record["industry_ref"], INDUSTRY)
        self.assertEqual(record["from_subject_ref"], CTSH)
        self.assertEqual(record["reason_code"], "industry_level_under_rule")
        self.assertEqual(record["rule_ref"], RULE_REF)
        self.assertEqual(record["claim_version_hash"], claim["hash"])
        self.assertEqual(record["decision_hash"], claim["decision_hash"])
        self.assertEqual(record["mission_version_ref"],
                         covering_missions(self.store.connection)[CTSH]["mission_version_ref"])
        # Company level: still retired, and the retirement untouched byte for byte.
        self.assertEqual(retired_before, retired_claim_version_refs(self.store.connection))
        self.assertEqual(decisions_before, self.store.connection.execute(
            "SELECT record_json, content_hash FROM claim_retirement_decisions").fetchall())
        self.assertEqual(reattributed_claim_version_refs(self.store.connection, INDUSTRY),
                         {claim["ref"]})
        again = self.reattributions.reattribute(
            claim_version_ref=claim["ref"], actor_ref=AUTOMATION, rationale="again",
            cited_span=claim["span"], source_text=claim["source"])
        self.assertEqual(again["status"], "duplicate")
        self.reattributions.record_review(claim_version_ref=claim["ref"], inputs_hash="h",
                                          outcome="reattributed")
        for sql in ("UPDATE claim_industry_reattributions SET industry_ref='industry:x'",
                    "DELETE FROM claim_industry_reattributions",
                    "UPDATE claim_industry_reattribution_reviews SET outcome='unreadable'",
                    "DELETE FROM claim_industry_reattribution_reviews"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(sql)
        with self.assertRaises(sqlite3.DatabaseError):  # only through the authority
            self.store.connection.execute(
                "INSERT INTO claim_industry_reattribution_reviews VALUES "
                "('x','r','h','unreadable',NULL,1,'t')")

    def test_a_person_may_reattribute_what_the_rule_would_not(self) -> None:
        claim = self.retired(statement="EPAM's advantage in the IT services market is "
                                       "attributed to complex engineering focus.",
                             source="EPAM's advantage in the IT services market is "
                                    "attributed to complex engineering focus. " * 2)
        with self.assertRaises(ClaimReattributionNotFound):
            self.reattributions.reattribute(claim_version_ref="claim-version:unknown",
                                            actor_ref=OWNER, rationale="r")
        record = self.reattributions.reattribute(
            claim_version_ref=claim["ref"], actor_ref=OWNER, rationale="调查的行业结论")
        self.assertEqual(record["reason_code"], "human_judgment")
        self.assertIsNone(record["rule_ref"])

    def test_never_a_disclaimer_a_live_claim_or_a_withdrawn_retirement(self) -> None:
        live = self.claim(subject=CTSH, statement=SURVEY, source=SURVEY_DOC)
        with self.assertRaises(ClaimReattributionNotFound):
            self.reattributions.reattribute(claim_version_ref=live["ref"], actor_ref=OWNER,
                                            rationale="r")
        text = "Past performance is not indicative of future results."
        disclaimer = self.claim(subject=CTSH, statement=text, source=text)
        challenge = self.authority.challenge(
            claim_version_ref=disclaimer["ref"], claim_version_hash=disclaimer["hash"],
            reason_code="boilerplate_disclaimer", rationale="套话", actor_ref=AUTOMATION)
        self.authority.decide(challenge_ref=challenge["id"],
                              challenge_hash=challenge["content_hash"], decision="retired",
                              actor_ref=AUTOMATION, rationale="r")
        with self.assertRaises(ClaimReattributionConflict):
            self.reattributions.reattribute(claim_version_ref=disclaimer["ref"],
                                            actor_ref=OWNER, rationale="r")
        claim = self.retired()
        self.reattributions.reattribute(claim_version_ref=claim["ref"], actor_ref=OWNER,
                                        rationale="r")
        # A later reinstatement makes it its company's again, and not the industry's.
        self.authority.reinstate(claim_version_ref=claim["ref"], actor_ref=OWNER,
                                 rationale="其实说的就是 Cognizant")
        self.assertEqual(industry_reattributions(self.store.connection), {})
        other = self.retired()
        self.authority.reinstate(claim_version_ref=other["ref"], actor_ref=OWNER, rationale="r")
        with self.assertRaises(ClaimReattributionConflict):
            self.reattributions.reattribute(claim_version_ref=other["ref"], actor_ref=OWNER,
                                            rationale="r")


class BackfillTests(_Reattribution):
    def setUp(self) -> None:
        super().setUp()
        self.survey = self.retired()
        company = "Cognizant said its discretionary IT services demand from clients recovered."
        self.company = self.retired(
            subject=EPAM, statement=company, source="Digest. " * 30 + company,
            needles=("epam",))

    def test_under_the_grant_it_appends_once_and_is_idempotent(self) -> None:
        self.grant_claim_challenge()
        summary = self.driver().run_once()["industry_reattribution"]
        self.assertEqual([item["claim_version_ref"] for item in summary["reattributed"]],
                         [self.survey["ref"]])
        self.assertEqual(summary["not_industry_level"], 1)
        self.assertEqual(summary["refusals"], {"single_company_word": 1})
        probe = reattribution_state_probe(self.store.connection)
        reads = self.spool.reads
        again = self.driver().run_once()["industry_reattribution"]
        self.assertEqual(again["candidates"], 1)
        self.assertEqual(again["already_reviewed"], 1)
        self.assertEqual(again["reattributed"], [])
        self.assertEqual(self.spool.reads, reads)
        self.assertEqual(probe, reattribution_state_probe(self.store.connection))
        self.assertEqual(len(self.reattributions.reattributions()), 1)

    def test_without_the_grant_it_only_reports(self) -> None:
        summary = self.driver().run_once()["industry_reattribution"]
        self.assertEqual(summary["reattributed"], [])
        self.assertEqual([item["claim_version_ref"] for item in summary["would_reattribute"]],
                         [self.survey["ref"]])
        self.assertEqual(self.reattributions.reattributions(), [])
        # The refusal is remembered (a read's cache); the finding waits for the grant.
        self.assertEqual({ref: row["outcome"] for ref, row in self.reattributions.reviews().items()},
                         {self.company["ref"]: "not_industry_level"})
        self.assertEqual(reattributed_claim_version_refs(self.store.connection, INDUSTRY), set())

    def test_a_driver_without_the_authority_reads_nothing(self) -> None:
        from dalton_core.claim_review import ClaimReviewDriver

        self.grant_claim_challenge()
        driver = ClaimReviewDriver(store=self.store, missions=self.missions,
                                   challenges=self.authority, spool=self.spool)
        reads = self.spool.reads
        summary = driver.reattribute_industry_findings(principal=AUTOMATION)
        self.assertEqual(summary["reattributed"], [])
        self.assertEqual(self.spool.reads, reads)

    def test_it_is_bounded_per_tick(self) -> None:
        self.grant_claim_challenge()
        more = self.retired(statement=SURVEY.replace("2026", "2027"),
                            source=SURVEY_DOC.replace("2026", "2027"))
        first = run_backfill(self.driver(), authority=self.reattributions,
                             principal=AUTOMATION, max_writes=1)
        self.assertEqual(len(first["reattributed"]), 1)
        self.assertGreaterEqual(first["deferred"], 1)
        second = run_backfill(self.driver(), authority=self.reattributions,
                              principal=AUTOMATION, max_writes=1)
        self.assertEqual({item["claim_version_ref"] for item in
                          first["reattributed"] + second["reattributed"]},
                         {self.survey["ref"], more["ref"]})
        docs = run_backfill(self.driver(), authority=self.reattributions,
                            principal=AUTOMATION, max_documents=1)
        self.assertEqual(docs["reattributed"], [])

    def test_the_read_only_driver_simulates_over_a_mode_ro_connection(self) -> None:
        from dalton_core.claim_review import ClaimReviewDriver

        self.grant_claim_challenge()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "core.sqlite"
            backup = sqlite3.connect(path)
            self.store.connection.backup(backup)
            backup.close()
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            driver = ClaimReviewDriver.read_only(
                connection=connection, missions=self.missions, spool=self.spool)
            summary = driver.reattribute_industry_findings(principal=None, dry_run=True)
        self.assertEqual([item["claim_version_ref"] for item in summary["would_reattribute"]],
                         [self.survey["ref"]])


class DoorTests(unittest.TestCase):
    def test_the_owner_door_is_a_human_governance_operation(self) -> None:
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS,
            OPERATION_ACTOR_FIELDS,
            OPERATION_FIELDS,
        )

        self.assertIn("reattribute_claim_to_industry", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS["reattribute_claim_to_industry"], frozenset({
            "claim_version_ref", "decision_hash", "industry_ref", "rationale", "actor_ref"}))
        self.assertEqual(OPERATION_ACTOR_FIELDS["reattribute_claim_to_industry"], "actor_ref")

    def test_the_cli_is_a_dry_run_unless_a_person_applies_it(self) -> None:
        from dalton_core import claim_industry_reattribution_cli as cli

        self.assertEqual(cli.OPERATION, "reattribute_claim_to_industry")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["reattribute", "--state-dir", "/nonexistent"])


class CliTests(_Reattribution):
    def test_dry_run_then_apply_needs_a_person(self) -> None:
        from dalton_core import claim_industry_reattribution_cli as cli

        claim = self.retired()
        with tempfile.TemporaryDirectory() as temp:
            backup = sqlite3.connect(Path(temp) / "core.sqlite")
            self.store.connection.backup(backup)
            backup.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["reattribute", "--state-dir", temp, "--claim-version-ref",
                          claim["ref"], "--reason", "行业结论"])
            preview = json.loads(out.getvalue())
            self.assertEqual(preview["status"], "dry_run")
            self.assertEqual(preview["industry_ref"], INDUSTRY)
            self.assertTrue(preview["retired_now"])
            with self.assertRaises(SystemExit):
                cli.reattribute(Path(temp), claim_version_ref=claim["ref"], reason="r",
                                apply=True, actor="automation:coverage-mission")
            with contextlib.redirect_stdout(io.StringIO()) as status:
                cli.main(["status", "--state-dir", temp])
            self.assertEqual(json.loads(status.getvalue())["live"], 0)


if __name__ == "__main__":
    unittest.main()
