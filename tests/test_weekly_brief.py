from __future__ import annotations

import hashlib
import sqlite3
import unittest

from dalton_core.agenda import AgendaStore
from dalton_core.industry_research import REQUIRED_INDUSTRY_BRIEF_SECTIONS
from dalton_core.weekly_brief import (
    WeeklyBriefAuthority,
    WeeklyBriefConflict,
    WeeklyBriefValidationError,
)
from tests import test_industry_research as industry_fixture


BRIEF_REF = "weekly-brief:us-it-services"
ISSUE_1 = "weekly-brief-version:us-it-services:2026-w35"
ISSUE_2 = "weekly-brief-version:us-it-services:2026-w36"
INDUSTRY_REF = industry_fixture.INDUSTRY
ACN_REF = industry_fixture.ACN


class WeeklyBriefAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = industry_fixture.IndustryResearchAuthorityTests(
            methodName="test_snapshot_is_self_contained_and_renderer_requires_complete_contract"
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        params = fixture.pack_params()
        params["coverage_universe"] = [params["coverage_universe"][0]]
        params["report_contract"]["industry_brief_sections"] = list(
            REQUIRED_INDUSTRY_BRIEF_SECTIONS
        )
        self.pack = fixture.authority.register_evidence_pack(
            "industry-evidence-pack:us-it-services", **params
        )
        self.overlay = fixture.authority.register_company_overlay(
            "company-overlay:acn", **fixture.overlay_params(self.pack)
        )
        self.weekly = WeeklyBriefAuthority(fixture.store, fixture.authority)

    def issue_params(self, *, second: bool = False) -> dict:
        return {
            "brief_ref": BRIEF_REF,
            "period_start": (
                "2026-08-27T00:00:00+00:00"
                if second else "2026-08-20T00:00:00+00:00"
            ),
            "period_end": (
                "2026-09-03T00:00:00+00:00"
                if second else "2026-08-27T00:00:00+00:00"
            ),
            "evidence_pack_version_id": self.pack["id"],
            "company_overlay_version_ids": [self.overlay["id"]],
            "company_thesis_refs": {},
            "actor_ref": "human:coverage-owner",
            "version_id": ISSUE_2 if second else ISSUE_1,
            "prior_version_ref": ISSUE_1 if second else None,
            "idempotency_key": "weekly-brief:w36" if second else "weekly-brief:w35",
        }

    def publish(self, *, second: bool = False) -> dict:
        params = self.issue_params(second=second)
        brief_ref = params.pop("brief_ref")
        return self.weekly.publish_issue(brief_ref, **params)

    def test_issue_binds_forecast_reconciliations_created_in_its_window(self) -> None:
        """P9c: reconciliations inside the window are listed, rendered and replayed exactly."""
        from datetime import datetime, timedelta, timezone
        from tests.test_forecast_reconciliation import ForecastReconciliationFixture

        fixture = ForecastReconciliationFixture(self.fixture.store)
        fixture.line("17300", period={
            "start": "2025-06-01", "end": "2025-08-31",
            "calendar": "company:fiscal", "kind": "quarter",
        }, subject=ACN_REF)
        fixture.claim(current="17596260000", prior="16405819000", period={
            "start": "2025-06-01", "end": "2025-08-31",
            "calendar": "company:fiscal", "kind": "quarter",
        }, subject=ACN_REF)
        created = fixture.reconciler.reconcile_pending(
            requested_by="human:coverage-owner", mission_resolver=None,
        )["created"]
        self.assertEqual(len(created), 1)
        now = datetime.now(timezone.utc)
        params = self.issue_params()
        params["period_start"] = (now - timedelta(days=1)).isoformat(timespec="seconds")
        params["period_end"] = (now + timedelta(days=1)).isoformat(timespec="seconds")
        brief_ref = params.pop("brief_ref")
        issue = self.weekly.publish_issue(brief_ref, **params)
        self.assertEqual(len(issue["forecast_reconciliations"]), 1)
        entry = issue["forecast_reconciliations"][0]
        self.assertEqual(entry["ref"], created[0]["id"])
        self.assertEqual(entry["hash"], created[0]["content_hash"])
        self.assertEqual(entry["company_ref"], ACN_REF)
        self.assertEqual(entry["band"], "notable")
        self.assertIn("预测对账", issue["sections"])
        replayed = self.weekly.issue(issue["id"])
        self.assertEqual(replayed["forecast_reconciliations"], issue["forecast_reconciliations"])
        markdown = self.weekly.render_markdown(issue["id"])["body"]
        self.assertIn("## 预测对账", markdown)
        self.assertIn(created[0]["id"], markdown)
        self.assertIn("偏差 1.7125%", markdown)
        # A window before the reconciliation existed lists nothing, and says so.
        params = self.issue_params(second=True)
        params["prior_version_ref"] = issue["id"]
        params["period_start"] = (now + timedelta(days=1)).isoformat(timespec="seconds")
        params["period_end"] = (now + timedelta(days=2)).isoformat(timespec="seconds")
        brief_ref = params.pop("brief_ref")
        later = self.weekly.publish_issue(brief_ref, **params)
        self.assertEqual(later["forecast_reconciliations"], [])
        self.assertIn("本期没有预测线被实际数对账", self.weekly.render_markdown(later["id"])["body"])

    def test_render_omits_reconciliation_section_for_pre_p9c_issues(self) -> None:
        """Issues published before P9c froze a six-section list; re-rendering
        them must reproduce the original bytes and not grow a new section."""
        issue = self.publish()
        self.assertIn("## 预测对账", self.weekly.render_markdown(issue["id"])["body"])

        class PreP9cAuthority(WeeklyBriefAuthority):
            def issue(self, version_id: str) -> dict:
                record = dict(super().issue(version_id))
                record["sections"] = [
                    name for name in record["sections"] if name != "预测对账"
                ]
                record.pop("forecast_reconciliations", None)
                return record

        old = PreP9cAuthority(self.fixture.store, self.fixture.authority)
        body = old.render_markdown(issue["id"])["body"]
        self.assertNotIn("预测对账", body)
        self.assertIn("## 公司与 driver 分化", body)
        self.assertEqual(body, old.render_markdown(issue["id"])["body"])

    def test_first_issue_is_baseline_not_fake_weekly_delta_and_replays(self) -> None:
        issue = self.publish()
        replay = self.publish()
        self.assertEqual("fresh", issue["status"])
        self.assertEqual("duplicate", replay["status"])
        self.assertTrue(issue["change_summary"]["is_baseline"])
        self.assertEqual([], issue["change_summary"]["new_claim_version_refs"])
        self.assertEqual(2, len(issue["change_summary"]["baseline_claim_version_refs"]))
        self.assertEqual("insufficient", issue["thesis_bindings"][0]["status"])
        rendered = self.weekly.render_markdown(issue["id"])
        self.assertIn("首次基线", rendered["body"])
        self.assertIn("不把此前累积的正式 Claim 冒充为本周新增", rendered["body"])
        self.assertIn("尚无正式当前 ThesisVersion", rendered["body"])
        self.assertEqual(rendered, self.weekly.render_markdown(issue["id"]))
        self.assertEqual(issue["content_hash"], self.weekly.issue(issue["id"])["content_hash"])

    def test_admitted_thesis_resolves_company_binding_and_industry_subject_stays_separate(self) -> None:
        agenda = AgendaStore(self.fixture.store)
        coverage = self.fixture.coverage
        owner = "human:coverage-owner"
        mandate = agenda.create_mandate(
            "mandate:us-it-services-brief", actor_ref=owner,
            objective="Admit the industry and ACN theses for the brief mapping.",
            scope_refs=[INDUSTRY_REF, ACN_REF], constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
        )
        pack = self.fixture.driver_pack
        content = {
            "statement": "AI demand can support growth.",
            "mechanism": "Bookings convert into revenue.",
            "confidence": "low",
            "implied_expectation": "Demand signals are followed by bookings and revenue.",
            "claim_refs": [],
            "catalyst_refs": [],
            "falsifier_refs": ["falsifier:conversion-breaks"],
            "change_reason": "Brief mapping test admission.",
        }

        def admit(candidate_id: str, thesis_ref: str, subject_ref: str) -> dict:
            candidate = coverage.propose_thesis_admission(
                candidate_id=candidate_id, thesis_ref=thesis_ref,
                company_ref=subject_ref, industry_ref=INDUSTRY_REF,
                template_ref="template:demand-conversion",
                driver_refs=["driver:demand-and-conversion"],
                mandate_version_ref=mandate["id"],
                mandate_version_hash=mandate["content_hash"],
                driver_pack_version_ref=pack["id"],
                driver_pack_version_hash=pack["content_hash"],
                content=content, actor_ref=owner,
                idempotency_key=candidate_id,
            )
            return coverage.decide_thesis_admission(
                candidate_id=candidate["id"],
                candidate_hash=candidate["content_hash"],
                verdict="admit",
                rationale="Bound to the active mandate, pack, template and falsifiers.",
                decision_id=f"thesis-admission-decision:{candidate_id}",
                actor_ref=owner,
                idempotency_key=f"thesis-admission-decision:{candidate_id}",
            )

        industry = admit(
            "thesis-admission-candidate:industry:1",
            "thesis:us-it-services:demand-bottoming",
            INDUSTRY_REF,
        )
        company = admit(
            "thesis-admission-candidate:acn:1",
            "thesis:acn:demand-conversion",
            ACN_REF,
        )
        connection = self.fixture.store.connection
        bindings = self.weekly._thesis_bindings(
            connection, [ACN_REF], {ACN_REF: "thesis:acn:demand-conversion"}
        )
        self.assertEqual("current", bindings[0]["status"])
        self.assertEqual(company["thesis_version"]["id"], bindings[0]["thesis_version_ref"])
        self.assertEqual("low", bindings[0]["confidence"])
        with self.assertRaises(WeeklyBriefConflict):
            self.weekly._thesis_bindings(
                connection, [ACN_REF], {ACN_REF: "thesis:us-it-services:demand-bottoming"}
            )
        self.assertEqual("human_admission", industry["thesis_version"]["authority_kind"])
        params = self.issue_params()
        params["company_thesis_refs"] = {ACN_REF: "thesis:acn:demand-conversion"}
        brief_ref = params.pop("brief_ref")
        issue = self.weekly.publish_issue(brief_ref, **params)
        self.assertEqual("current", issue["thesis_bindings"][0]["status"])
        rendered = self.weekly.render_markdown(issue["id"])["body"]
        self.assertIn("AI demand can support growth. (confidence=low", rendered)
        self.assertNotIn("尚无正式当前 ThesisVersion", rendered)

    def test_next_issue_reports_carry_forward_without_inventing_changes(self) -> None:
        first = self.publish()
        second = self.publish(second=True)
        changes = second["change_summary"]
        self.assertFalse(changes["is_baseline"])
        self.assertEqual([], changes["new_claim_version_refs"])
        self.assertEqual(2, len(changes["carried_claim_version_refs"]))
        self.assertEqual([], changes["changed_driver_refs"])
        self.assertEqual(first["id"], second["prior_version_ref"])
        body = self.weekly.render_markdown(second["id"])["body"]
        self.assertIn("新增正式 Claim：0 条", body)
        self.assertIn("延续正式 Claim：2 条", body)

    def test_delivery_binds_exact_rendered_artifact_and_is_idempotent(self) -> None:
        issue = self.publish()
        rendered = self.weekly.render_markdown(issue["id"])
        digest = hashlib.sha256(rendered["body"].encode("utf-8")).hexdigest()
        params = {
            "issue_version_ref": issue["id"],
            "issue_version_hash": issue["content_hash"],
            "destination_ref": "discord:channel:test",
            "external_message_ref": "discord-message:123",
            "artifact_sha256": digest,
            "delivered_at": "2026-08-27T12:00:00+00:00",
            "delivery_id": "weekly-brief-delivery:test:1",
            "actor_ref": "human:coverage-owner",
            "idempotency_key": "weekly-brief-delivery:test:1",
        }
        wrong = dict(params)
        wrong["artifact_sha256"] = "0" * 64
        wrong["idempotency_key"] = "weekly-brief-delivery:test:wrong"
        with self.assertRaises(WeeklyBriefConflict):
            self.weekly.record_delivery(**wrong)
        delivery = self.weekly.record_delivery(**params)
        replay = self.weekly.record_delivery(**params)
        self.assertEqual("fresh", delivery["status"])
        self.assertEqual("duplicate", replay["status"])

    def test_feedback_is_human_exact_targeted_and_versionable(self) -> None:
        issue = self.publish()
        params = {
            "issue_version_ref": issue["id"],
            "issue_version_hash": issue["content_hash"],
            "verdict": "needs_more_evidence",
            "target_kind": "brief",
            "target_ref": issue["id"],
            "notes": "Separate weekly research changes from the baseline evidence pack.",
            "feedback_id": "weekly-brief-feedback:test:1",
            "prior_feedback_ref": None,
            "subject_ref": "human:coverage-owner",
            "actor_ref": "human:coverage-owner",
            "idempotency_key": "weekly-brief-feedback:test:1",
        }
        first = self.weekly.record_feedback(**params)
        self.assertEqual("fresh", first["status"])
        self.assertEqual("duplicate", self.weekly.record_feedback(**params)["status"])
        revised = dict(params)
        revised.update({
            "verdict": "useful",
            "notes": "The weekly delta is now explicit.",
            "feedback_id": "weekly-brief-feedback:test:2",
            "prior_feedback_ref": first["id"],
            "idempotency_key": "weekly-brief-feedback:test:2",
        })
        self.weekly.record_feedback(**revised)
        self.assertEqual(2, len(self.weekly.feedback(issue["id"])))
        fork = dict(revised)
        fork.update({
            "verdict": "disagree",
            "notes": "A second successor must not fork the immutable feedback chain.",
            "feedback_id": "weekly-brief-feedback:test:fork",
            "idempotency_key": "weekly-brief-feedback:test:fork",
        })
        with self.assertRaises(WeeklyBriefConflict):
            self.weekly.record_feedback(**fork)
        outside = dict(params)
        outside.update({
            "target_kind": "company", "target_ref": "company:outside",
            "feedback_id": "weekly-brief-feedback:test:outside",
            "idempotency_key": "weekly-brief-feedback:test:outside",
        })
        with self.assertRaises(WeeklyBriefConflict):
            self.weekly.record_feedback(**outside)
        wrong_subject = dict(params)
        wrong_subject.update({
            "subject_ref": "human:someone-else",
            "feedback_id": "weekly-brief-feedback:test:wrong-subject",
            "idempotency_key": "weekly-brief-feedback:test:wrong-subject",
        })
        with self.assertRaises(WeeklyBriefValidationError):
            self.weekly.record_feedback(**wrong_subject)

    def test_integrity_and_sql_guards(self) -> None:
        self.publish()
        report = self.weekly.integrity_report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(1, report["issue_versions"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.fixture.store.connection.execute(
                "UPDATE weekly_brief_issue_versions SET actor_ref='tampered'"
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.fixture.store.connection.execute(
                "INSERT INTO weekly_brief_feedback"
                "(feedback_id,issue_version_ref,issue_version_hash,verdict,target_kind,"
                "target_ref,prior_feedback_ref,subject_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES('x',?,?,?,?,?,?,?,?,?,?,?)",
                (ISSUE_1, "0" * 64, "read", "brief", ISSUE_1, None,
                 "human:x", "{}", "0" * 64, "human:x", "2026-08-27T00:00:00+00:00"),
            )


if __name__ == "__main__":
    unittest.main()
