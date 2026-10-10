"""An exhausted execution re-entry belongs to maintenance, not source purchase."""
import json
from pathlib import Path
import tempfile
import unittest

from dalton_core.needs_human import held_lanes


class ReentryExhaustionResponsibilityTests(unittest.TestCase):
    REASON = "reentry_failed_after_automatic_rebind:controlled reentry was already attempted"
    ADMISSION = "mission-document-research-admission:" + "a" * 32

    def ledger(self, root, reason):
        path = Path(root) / "mission-document-research-runs" / "holds.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"holds": {self.ADMISSION: {
            "admission_hash": "b" * 64, "ticket_ref": "mission-document-research:" + "c" * 24,
            "disposition": "recovery_required", "reason": reason}}}))
        return path

    def test_exact_technical_exhaustion_retains_card_doors_and_ledger_without_owner_count(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.ledger(root, self.REASON)
            before = path.read_bytes()
            rows = held_lanes(None, state_dir=Path(root))
            self.assertEqual(path.read_bytes(), before)
        item, group = rows
        self.assertEqual(item["detail"]["admission_ref"], self.ADMISSION)
        self.assertEqual(item["detail"]["reason"], self.REASON)
        self.assertFalse(item["actionable"])
        self.assertEqual(item["responsibility"], "administrator")
        self.assertEqual(item["status"], "system_repair_pending")
        self.assertEqual([r["door"] for r in item["detail"]["owner_doors"]], ["reentry", "withdraw"])
        self.assertTrue(item["detail"]["commands"])
        self.assertIn("原调用与费用记录保留", item["action"])
        self.assertNotIn("等你决定", item["title"])
        self.assertFalse(group["actionable"])
        self.assertEqual(group["responsibility"], "administrator")
        self.assertNotIn("项等你决定", group["why_blocked"])

    def test_exact_heartbeat_fallback_is_also_maintenance_not_an_analyst_decision(self):
        rows = held_lanes({"bounded_planner": {"last_result": {"mission_document_research": {
            "status": "recovery_required", "reason": self.REASON}}}})
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["actionable"])
        self.assertEqual(rows[0]["responsibility"], "administrator")
        self.assertEqual(rows[0]["detail"]["reason"], self.REASON)

    def test_paid_and_unknown_recovery_authorizations_are_not_reclassified(self):
        for reason in ("contract_failed_after_automatic_retry", "unproved_send_failed_after_automatic_retry",
                       "fresh_work_recovery_exhausted", "provider_budget_exceeded_after_corrected_retry:input=100>80"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                self.ledger(root, reason)
                item = held_lanes(None, state_dir=Path(root))[0]
                self.assertTrue(item["actionable"])
                self.assertNotEqual(item.get("responsibility"), "administrator")
                self.assertEqual(item["detail"]["reason"], reason)

    def test_arbitrary_error_prose_and_other_reentry_failures_keep_existing_policy(self):
        for reason in ("reentry_failed_after_automatic_rebind:other failure",
                       "other_error:" + self.REASON):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                self.ledger(root, reason)
                item = held_lanes(None, state_dir=Path(root))[0]
                self.assertTrue(item["actionable"])
                self.assertNotEqual(item.get("responsibility"), "administrator")

    def test_the_same_text_in_another_lane_cannot_claim_this_classification(self):
        rows = held_lanes({"bounded_planner": {"last_result": {"other_lane": {
            "status": "recovery_required", "reason": self.REASON}}}})
        self.assertTrue(rows[0]["actionable"])
        self.assertNotEqual(rows[0].get("responsibility"), "administrator")
