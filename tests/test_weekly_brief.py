from __future__ import annotations

import hashlib
import sqlite3
import unittest

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
