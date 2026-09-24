"""2026-09-24: company aliases -- the packaged defaults, CJK names, competitor
clouds in call titles, and the owner's append-only alias ledger."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.company_aliases import (
    CompanyAliasError, load_overlay, load_revisions, main, record_revision)
from dalton_core.document_provenance import subjects_for_statement
from dalton_core.document_subject import (
    BRAND_NAMES, COMPANY_NAMES, document_names_subject, earnings_call_names_issuer)
from dalton_core.mission_company_names import mission_name_table

HYPERSCALERS = [
    {"company_ref": "company:ticker:amzn", "ticker": "AMZN"},
    {"company_ref": "company:ticker:googl", "ticker": "GOOGL"},
    {"company_ref": "company:ticker:meta", "ticker": "META"},
    {"company_ref": "company:ticker:msft", "ticker": "MSFT"},
]
# ws-7d's installed plan names, which used to replace the packaged ones.
WS_PLAN_NAMES = {"AMZN": ["Amazon.com"], "GOOGL": ["Alphabet"],
                 "META": ["Meta Platforms"], "MSFT": ["Microsoft"]}


class PackagedDefaultsTests(unittest.TestCase):
    def test_the_requested_aliases_are_packaged(self):
        wanted = {
            "GOOGL": {"Google", "谷歌", "Alphabet Inc."},
            "AMZN": {"Amazon", "亚马逊", "AWS", "Amazon Web Services"},
            "META": {"Meta", "Facebook", "脸书", "Instagram", "WhatsApp"},
            "MSFT": {"Microsoft", "微软", "Azure", "Microsoft Azure"},
            "ACN": {"埃森哲", "Accenture plc"},
            "CTSH": {"高知特", "Cognizant Technology Solutions"},
            "IBM": {"国际商业机器"},
        }
        for ticker, names in wanted.items():
            self.assertLessEqual(names, set(COMPANY_NAMES[ticker]), ticker)
        # The first name of the legacy five is unchanged, so their labels lead
        # with the same word they always did.
        self.assertEqual([COMPANY_NAMES[t][0] for t in ("ACN", "CTSH", "EPAM", "IBM", "DXC")],
                         ["Accenture", "Cognizant", "EPAM Systems", "IBM", "DXC Technology"])

    def test_a_workspace_plan_no_longer_hides_the_packaged_names(self):
        from dalton_core.workspace_lane_parity import build_mission_feed_plan

        mission = {"mission_ref": "coverage-mission:ws", "industry_ref": "industry:cloud",
                   "title": "Hyperscalers", "universe": HYPERSCALERS}
        plan = build_mission_feed_plan(mission, company_names=WS_PLAN_NAMES)
        # The generated plan still records only what the owner said ...
        self.assertEqual(plan["companies"]["company:ticker:amzn"]["names"],
                         ["Amazon.com", "AMZN"])
        # ... and the table the lanes run on adds the packaged names to it.
        table = mission_name_table(HYPERSCALERS, plan)
        self.assertEqual(table["AMZN"][0], "Amazon.com")
        for name in ("AWS", "亚马逊", "Amazon Web Services"):
            self.assertIn(name, table["AMZN"])
        self.assertTrue(document_names_subject(
            "亚马逊云科技 AWS 的资本开支继续上调", "AMZN", table)["names_subject"])

    def test_cjk_names_are_matched_and_no_longer_fold_to_nothing(self):
        self.assertEqual(document_names_subject("谷歌第三季度云收入增长", "GOOGL")["matched"],
                         ["谷歌"])
        self.assertTrue(document_names_subject("埃森哲 宣布收购", "ACN")["names_subject"])
        self.assertTrue(document_names_subject("国际商业机器 公司", "IBM")["names_subject"])
        # "IT 服务" used to fold to "it" and name the industry in any English text.
        self.assertFalse(document_names_subject(
            "It was a quiet quarter.", "industry:us-it-services")["names_subject"])
        self.assertTrue(document_names_subject(
            "美国IT服务市场增速放缓", "industry:us-it-services")["names_subject"])


class CompetitorCloudTests(unittest.TestCase):
    """AWS on AMZN and Azure on MSFT must not make the other's titles ambiguous."""

    def check(self, table):
        for title, ticker in (
            ("Microsoft Corp (Azure vs AWS share) Q1 2027 Earnings Call", "MSFT"),
            ("Amazon.com AWS growth vs Azure Q4 2025 Earnings Call", "AMZN"),
            ("Alphabet Google Cloud vs AWS and Azure Q3 2026 Earnings Call", "GOOGL"),
            ("Meta Platforms Instagram WhatsApp Q2 2026 Earnings Call", "META"),
        ):
            result = earnings_call_names_issuer(title, ticker, table)
            self.assertTrue(result["names_issuer"], (title, result))
            self.assertEqual(result["ambiguous_with"], [], title)
        # "Microsoft Azure" contains the issuer's own name, so in Amazon's title
        # it is a second issuer, as it should be.
        self.assertFalse(earnings_call_names_issuer(
            "Amazon.com vs Microsoft Azure Q4 2025 Earnings Call", "AMZN", table)["names_issuer"])
        # A second *issuer* in the title is still ambiguous.
        both = earnings_call_names_issuer(
            "Microsoft and Amazon Q1 2027 Earnings Call", "MSFT", table)
        self.assertFalse(both["names_issuer"])
        self.assertIn("Amazon", both["ambiguous_with"])
        # And a competitor's product never names the issuer by itself.
        self.assertFalse(earnings_call_names_issuer(
            "Microsoft Azure Q1 2027 Earnings Call", "AMZN", table)["names_issuer"])

    def test_with_the_packaged_table(self):
        self.check(None)

    def test_with_a_workspace_table(self):
        plan = {"companies": {f"company:ticker:{t.lower()}": {"names": names}
                              for t, names in WS_PLAN_NAMES.items()}}
        self.check(mission_name_table(HYPERSCALERS, plan))

    def test_brands_are_names_of_their_own_company(self):
        self.assertLessEqual(set(BRAND_NAMES), {
            name for names in COMPANY_NAMES.values() for name in names})

    def test_a_statement_about_a_competitor_cloud_gains_a_subject_and_keeps_its_own(self):
        # Additive by design: the review's company is always kept, and the
        # company the sentence is about is added -- never "ambiguous".
        result = subjects_for_statement(
            "AWS grew faster than the market this quarter.",
            company_ref="company:ticker:msft", company_names_key="MSFT",
            extra_subjects={"company:ticker:amzn": "AMZN"})
        self.assertEqual(result["subjects"], ["company:ticker:msft", "company:ticker:amzn"])


class AliasLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        (self.state / "feed-plans").mkdir(parents=True)

    def add(self, ticker, *names, action="add", apply=True, actor="human:owner"):
        return record_revision(self.state, ticker=ticker, action=action, names=list(names),
                               reason="巡检补别名", actor_ref=actor, apply=apply,
                               now="2026-09-24T12:00:00+00:00")

    def install_plan(self):
        from dalton_core.workspace_lane_parity import build_mission_feed_plan

        mission = {"mission_ref": "coverage-mission:ws", "industry_ref": "industry:cloud",
                   "title": "Hyperscalers", "universe": HYPERSCALERS}
        plan = build_mission_feed_plan(mission, company_names=WS_PLAN_NAMES)
        path = self.state / "feed-plans" / "mission-feeds-v1.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def test_dry_run_writes_nothing_and_apply_appends_one_revision(self):
        dry = self.add("AMZN", "Amazon Kuiper", apply=False)
        self.assertEqual(dry["status"], "dry_run")
        self.assertIn("Amazon Kuiper", dry["effective_names_after"])
        self.assertFalse((self.state / "company-alias-revisions").exists())
        done = self.add("AMZN", "Amazon Kuiper")
        self.assertEqual(done["status"], "recorded")
        self.assertEqual(len(load_revisions(self.state)), 1)
        self.assertEqual(self.add("AMZN", "amazon kuiper")["status"], "unchanged")
        self.assertEqual(self.add("AMZN", "AWS")["status"], "unchanged")  # packaged
        # "Meta" is the ticker in another case and always matches; a packaged
        # name that over-matches is retired like this instead.
        with self.assertRaisesRegex(CompanyAliasError, "ticker"):
            self.add("META", "Meta", action="retire")
        second = self.add("META", "Facebook", action="retire")
        self.assertEqual(second["revision"]["prior_revision_ref"], done["revision"]["id"])
        self.assertNotIn("Facebook", second["effective_names_after"])
        overlay = load_overlay(self.state)
        self.assertEqual(overlay["added"], {"AMZN": ["Amazon Kuiper"]})
        self.assertEqual(overlay["retired"], {"META": ["Facebook"]})
        self.assertEqual(overlay["revision_count"], 2)

    def test_only_a_person_with_a_reason_may_write(self):
        with self.assertRaisesRegex(CompanyAliasError, "human"):
            self.add("AMZN", "X", actor="automation:lane")
        with self.assertRaisesRegex(CompanyAliasError, "ticker"):
            self.add("AMZN", "AMZN", action="retire")
        with self.assertRaises(CompanyAliasError):
            record_revision(self.state, ticker="AMZN", action="add", names=["X"],
                            reason=" ", actor_ref="human:owner", apply=True)

    def test_an_edited_revision_breaks_the_chain_loudly(self):
        self.add("AMZN", "Amazon Kuiper")
        path = next((self.state / "company-alias-revisions").glob("*.json"))
        value = json.loads(path.read_text(encoding="utf-8"))
        value["names"] = ["Something Else"]
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(CompanyAliasError):
            load_revisions(self.state)

    def test_lanes_see_a_new_alias_on_their_next_plan_load(self):
        from dalton_core.claim_subject import mission_subject_needles
        from dalton_core.mission_feed_lane import (
            load_feed_discovery_plan, plan_company_names, validate_feed_discovery_plan)

        path = self.install_plan()
        before = load_feed_discovery_plan(path)
        self.assertNotIn("company_aliases", before)
        self.add("MSFT", "Microsoft Copilot")
        self.add("META", "Facebook", action="retire")
        plan = load_feed_discovery_plan(path)
        # The plan's own hash is untouched; the overlay rides beside it and
        # survives the coordinator's re-validation.
        self.assertEqual(plan["content_hash"], before["content_hash"])
        self.assertEqual(validate_feed_discovery_plan(plan), plan)
        table = plan_company_names(HYPERSCALERS, plan)
        self.assertIn("Microsoft Copilot", table["MSFT"])
        self.assertNotIn("Facebook", table["META"])
        self.assertIn("Meta Platforms", table["META"])
        needles = mission_subject_needles(HYPERSCALERS, plans=[plan])
        self.assertIn("copilot", needles["company:ticker:msft"])

    def test_a_broken_ledger_adds_nothing_and_says_so(self):
        from dalton_core.mission_feed_lane import load_feed_discovery_plan, plan_company_names

        path = self.install_plan()
        self.add("MSFT", "Microsoft Copilot")
        (self.state / "company-alias-revisions" / "junk.json").write_text("{}")
        plan = load_feed_discovery_plan(path)
        self.assertIn("error", plan["company_aliases"])
        self.assertNotIn("Microsoft Copilot", plan_company_names(HYPERSCALERS, plan)["MSFT"])

    def test_the_cli_is_a_dry_run_until_apply(self):
        from unittest.mock import patch

        argv = ["add", "--state-dir", str(self.state), "--ticker", "IBM",
                "--name", "Big Blue", "--reason", "常用别称", "--actor", "human:owner"]
        with patch("sys.stdout"):
            self.assertEqual(main(argv), 0)
            self.assertEqual(load_revisions(self.state), [])
            self.assertEqual(main([*argv, "--apply"]), 0)
            self.assertEqual(main(["show", "--state-dir", str(self.state),
                                   "--ticker", "IBM"]), 0)
            self.assertEqual(main(["add", "--state-dir", str(self.state), "--ticker", "IBM",
                                   "--name", "x", "--reason", "r",
                                   "--actor", "automation:x"]), 2)
        self.assertEqual(load_overlay(self.state)["added"], {"IBM": ["Big Blue"]})


if __name__ == "__main__":
    unittest.main()
