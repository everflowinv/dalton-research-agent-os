"""2026-09-24b: the extraction child checks subjects against the mission's names.

The deployed extraction child opened its feed lanes through read-only manifest
readers that carried no ``feed_plan_path``, so ``claim_subject.writer_feed_plans``
found no plan there, and the P13c/P13i document check was called with the
ticker alone.  On a deploy whose packaged table had no AMZN row, five AMZN
statements quoting "Amazon" were held as "the cited span never names the
subject (amzn)", and Amazon web pages naming Amazon two hundred times were
dismissed as "document never names this company".

These tests take the packaged row away (the deployed state) and show that the
child now reads the names from where a workspace keeps them: its feed plan and
the owner's alias ledger.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import document_subject, mission_company_names
from dalton_core.claim_subject import writer_feed_plans
from dalton_core.document_extraction import (
    SUBJECT_RULE_REF,
    UNATTRIBUTED_REASON,
    DocumentExtractionService,
)
from dalton_core.document_extraction_cli import ExtractionHost, run_extraction
from dalton_core.document_subject import COMPANY_NAMES
# Through the module, so this file does not collect the harness's tests again.
from tests import test_document_extraction_automation as _automation

WITHOUT_ACN = {key: value for key, value in COMPANY_NAMES.items() if key != "ACN"}


def _own_tests_only(cls):
    """Run only this class's tests, not the drafting harness's it inherits."""

    for name in dir(_automation.AutomationDraftingTests):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


def _without_packaged_acn():
    """The deployed state: no packaged names for the covered ticker."""

    return [patch.object(document_subject, "COMPANY_NAMES", WITHOUT_ACN),
            patch.object(mission_company_names, "COMPANY_NAMES", WITHOUT_ACN)]


@_own_tests_only
class ChildNameTableEndToEndTests(_automation.AutomationDraftingTests):
    """The child process, end to end, with ACN's packaged names removed."""

    def setUp(self) -> None:
        super().setUp()
        for item in _without_packaged_acn():
            item.start()
            self.addCleanup(item.stop)

    def _write_feed_plan(self, names: list[str]) -> Path:
        from dalton_core.workspace_lane_parity import build_mission_feed_plan

        mission = self.h.missions.active_mission("coverage-mission:us-it-services")
        company_names = {str(m["ticker"]).upper(): names if str(m["ticker"]).upper() == "ACN"
                         else [f"{m['ticker']} fixture name"] for m in mission["universe"]}
        plan = build_mission_feed_plan(mission, company_names=company_names)
        path = self.root / "feed-plans" / "mission-feeds-v1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _run(self) -> dict:
        self._grant_automation()
        self._policy_with_document_rule()
        context = self._active_context()
        fixture = self.root / "names-fixture.json"
        fixture.write_text(json.dumps({"schema_version": "0.1", "suggestions": [{
            "quote_id": context["quotes"][0]["quote_id"],
            "normalized_statement": "Accenture management says client decisions remain cautious.",
            "metric_or_aspect": "aspect:client-decisions", "period": "FY26",
            "basis": "fixture management commentary",
            "excerpt": "Accenture management says client decisions remain cautious"}]}),
            encoding="utf-8")
        return run_extraction(
            state_dir=self.root, model_config_path=self._model_config(),
            summary_dir=self.root / "names-summary", spool_dir=self.root / "spool",
            scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
            max_windows=2, max_numeric_windows=0, max_discovery_windows=0,
            connector_governance=None, web_fetch_governance=None,
            hermetic_fixture=fixture, candidate_staging=self.root / "staging.sqlite",
        )

    def test_without_a_plan_the_ticker_alone_refuses_the_issuers_own_call(self) -> None:
        # The regression as deployed: nothing but "ACN" to look for, and the
        # call title says "Accenture".
        summary = self._run()
        self.assertEqual(summary["admitted"], [])
        [resolved] = summary["resolved_reviews"]
        self.assertEqual(resolved["status"], "dismissed", resolved)
        self.assertIn(f"[{SUBJECT_RULE_REF}]", resolved["reason"])

    def test_the_child_reads_the_names_in_the_workspace_feed_plan(self) -> None:
        self._write_feed_plan(["Accenture"])
        summary = self._run()
        [admitted] = summary["admitted"]
        self.assertEqual(admitted["status"], "admitted", admitted)

    def test_the_child_reads_the_owners_alias_ledger_through_the_plan(self) -> None:
        from dalton_core.company_aliases import record_revision

        self._write_feed_plan(["Fixture Consulting Group"])
        record_revision(self.root, ticker="ACN", action="add", names=["Accenture"],
                        reason="fixture: the issuer's own name", actor_ref="human:owner",
                        apply=True)
        summary = self._run()
        [admitted] = summary["admitted"]
        self.assertEqual(admitted["status"], "admitted", admitted)

    def test_the_host_hands_the_plan_to_every_name_check(self) -> None:
        self._write_feed_plan(["Accenture"])
        host = ExtractionHost(state_dir=self.root, spool_dir=self.root / "spool",
                              scheduler_db=self.root / "scheduler.sqlite",
                              connector_governance=None, web_fetch_governance=None,
                              model_config=None)
        self.addCleanup(host.close)
        self.assertEqual(host.feed_plan_path, self.root / "feed-plans" / "mission-feeds-v1.json")
        [plan] = writer_feed_plans(host)
        self.assertIn("Accenture", json.dumps(plan))
        mission = host.coverage_mission.active_mission("coverage-mission:us-it-services")
        service = DocumentExtractionService(host)
        self.assertIn("Accenture", service.mission_names(mission)["ACN"])
        acn = next(m["company_ref"] for m in mission["universe"] if m["ticker"] == "ACN")
        check = service.admission_subject_check(
            {"mission_version_ref": mission["id"], "document_ref": "sales-note:none",
             "offset": 0, "quotes": [{"raw_text": "Morning digest."}]}, None)
        self.assertIsNone(check(acn, "Accenture expects bookings to convert."))
        self.assertIn("held for human review", check(acn, "Peers expect bookings to convert."))


class DocumentCheckUsesTheMissionTableTests(unittest.TestCase):
    """P13i: the whole-document check is asked with the mission's names."""

    def _service(self, text: str, *, plan_names: list[str]) -> DocumentExtractionService:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        universe = [{"company_ref": "company:ticker:acn", "ticker": "ACN"}]
        writer = type("W", (), {})()
        writer.store = type("S", (), {"connection": connection})()
        writer.coverage_mission = type("M", (), {"mission": lambda self, ref: {
            "id": ref, "universe": universe}})()
        writer.feed_plan_path = None
        service = DocumentExtractionService(writer)
        service._document_text = lambda context: text
        service._document_spec_ref = lambda context: "sell-side-research"
        service._plans_cache = [{"companies": {"company:ticker:acn": {"names": plan_names}}}]
        return service

    def test_a_page_naming_the_company_by_its_plan_name_is_attributed(self) -> None:
        with _without_packaged_acn()[0], _without_packaged_acn()[1]:
            service = self._service("Accenture said bookings grew. " * 50,
                                    plan_names=["Accenture"])
            subject = service.document_names_subject(
                {"document_ref": "d", "company_ref": "company:ticker:acn",
                 "company_ticker": "ACN", "mission_version_ref": "m"})
        self.assertTrue(subject["names_subject"], subject)
        self.assertIn("Accenture", subject["matched"])

    def test_the_rule_version_is_carried_by_every_new_p13i_dismissal(self) -> None:
        self.assertTrue(f"P13i: {UNATTRIBUTED_REASON}".startswith("P13i:"))
        self.assertIn(f"[{SUBJECT_RULE_REF}]", UNATTRIBUTED_REASON)


if __name__ == "__main__":
    unittest.main()
