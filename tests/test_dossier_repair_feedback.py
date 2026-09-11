from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dalton_core.dossier_repair_feedback import (
    dossier_repair_feedback_signature,
    read_dossier_repair_feedback,
)
from dalton_core.company_dossier_launcher import run_digest


COMPANY = "company:sec-cik:001688568"


class DossierRepairFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.runs = self.state / "company-dossier-runs"
        self.runs.mkdir(mode=0o700)

    def _write(self, marker: str, *, completed_at: str, targets: list[dict],
               ticket_status: str = "succeeded", summary_status: str = "succeeded",
               summary_company: str = COMPANY, mode: int = 0o600) -> Path:
        signature = f"ledger:{marker}"
        suffix = run_digest(COMPANY, signature)
        directory = self.runs / suffix
        directory.mkdir(mode=0o700)
        values = {
            "ticket.json": {
                "id": f"company-dossier-run:{suffix}",
                "company_ref": COMPANY,
                "signature": signature,
                "run_digest": suffix,
                "status": ticket_status,
                "exit_code": 0 if ticket_status == "succeeded" else 1,
                "completed_at": completed_at,
            },
            "summary.json": {
                "status": summary_status,
                "company_ref": summary_company,
                "dossier_status": "insufficient_evidence" if targets else "published",
                "repair_targets": targets,
            },
        }
        for name, value in values.items():
            path = directory / name
            path.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(path, mode)
        return directory

    def test_reads_exact_latest_successful_outcome_and_bounds_target_shape(self) -> None:
        directory = self._write("one", completed_at="2026-09-11T12:00:00+00:00", targets=[{
            "unit": "kpi_dictionary", "code": "missing_evidence",
            "detail": "retention numerator and denominator", "ignored": "no",
        }])
        feedback = read_dossier_repair_feedback(self.state)[COMPANY]
        self.assertEqual(feedback["source_ticket_ref"],
                         f"company-dossier-run:{directory.name}")
        self.assertEqual(feedback["repair_targets"], [{
            "unit": "kpi_dictionary", "code": "missing_evidence",
            "detail": "retention numerator and denominator",
        }])
        self.assertEqual(len(feedback["content_hash"]), 64)
        self.assertTrue(feedback["id"].endswith(feedback["content_hash"][:32]))

    def test_failed_or_mismatched_newer_ticket_cannot_add_or_clear_feedback(self) -> None:
        old = self._write("old", completed_at="2026-09-11T12:00:00+00:00",
                    targets=[{"unit": "catalyst_calendar",
                              "code": "missing_evidence"}])
        self._write("failed", completed_at="2026-09-11T13:00:00+00:00",
                    targets=[], ticket_status="failed")
        self._write("mismatch", completed_at="2026-09-11T14:00:00+00:00",
                    targets=[], summary_company="company:other")
        feedback = read_dossier_repair_feedback(self.state)[COMPANY]
        self.assertEqual(feedback["source_ticket_ref"],
                         f"company-dossier-run:{old.name}")

    def test_later_successful_outcome_clears_prior_targets_and_changes_signature(self) -> None:
        self._write("target", completed_at="2026-09-11T12:00:00+00:00",
                    targets=[{"unit": "history_of_price_drivers",
                              "code": "missing_evidence"}])
        before = dossier_repair_feedback_signature(self.state)
        self._write("clear", completed_at="2026-09-11T13:00:00+00:00", targets=[])
        feedback = read_dossier_repair_feedback(self.state)[COMPANY]
        self.assertEqual(feedback["repair_targets"], [])
        self.assertNotEqual(before, dossier_repair_feedback_signature(self.state))

    def test_symlink_or_group_readable_input_is_not_planner_feedback(self) -> None:
        target = self._write("unsafe",
                             completed_at="2026-09-11T12:00:00+00:00",
                             targets=[{"unit": "kpi_dictionary"}], mode=0o640)
        self.assertEqual(read_dossier_repair_feedback(self.state), {})
        os.chmod(target / "ticket.json", 0o600)
        os.chmod(target / "summary.json", 0o600)
        (target / "summary.json").unlink()
        external = self.state / "external.json"
        external.write_text("{}", encoding="utf-8")
        os.chmod(external, 0o600)
        (target / "summary.json").symlink_to(external)
        self.assertEqual(read_dossier_repair_feedback(self.state), {})


if __name__ == "__main__":
    unittest.main()
