from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_symlinked_state_root_is_rejected_before_resolution(self) -> None:
        self._write("real", completed_at="2026-09-11T12:00:00+00:00",
                    targets=[{"unit": "kpi_dictionary"}])
        link = self.state.parent / "linked-state"
        link.symlink_to(self.state, target_is_directory=True)
        self.addCleanup(link.unlink)
        self.assertEqual(read_dossier_repair_feedback(link), {})

    def test_file_that_grows_during_its_bounded_read_is_rejected(self) -> None:
        directory = self._write(
            "growing", completed_at="2026-09-11T12:00:00+00:00",
            targets=[{"unit": "kpi_dictionary"}],
        )
        original_read = os.read
        reads = 0

        def grow_after_read(descriptor: int, size: int) -> bytes:
            nonlocal reads
            reads += 1
            payload = original_read(descriptor, size)
            if reads == 2:
                with (directory / "summary.json").open("ab") as handle:
                    handle.write(b" ")
            return payload

        with patch("dalton_core.dossier_repair_feedback.os.read",
                   side_effect=grow_after_read):
            self.assertEqual(read_dossier_repair_feedback(self.state), {})

    def test_naive_time_is_rejected_and_offsets_are_compared_in_utc(self) -> None:
        self._write("naive", completed_at="2026-09-11T23:00:00",
                    targets=[{"detail": "naive"}])
        first = self._write("offset-first",
                            completed_at="2026-09-11T14:00:00+02:00",
                            targets=[{"detail": "12:00 UTC"}])
        later = self._write("offset-later",
                            completed_at="2026-09-11T12:30:00+00:00",
                            targets=[{"detail": "12:30 UTC"}])
        feedback = read_dossier_repair_feedback(self.state)[COMPANY]
        self.assertNotEqual(first.name, later.name)
        self.assertEqual(feedback["source_ticket_ref"],
                         f"company-dossier-run:{later.name}")
        self.assertEqual(feedback["repair_targets"][0]["detail"], "12:30 UTC")

    def test_ticket_signature_must_still_derive_its_directory_identity(self) -> None:
        directory = self._write(
            "identity", completed_at="2026-09-11T12:00:00+00:00",
            targets=[{"unit": "kpi_dictionary"}],
        )
        path = directory / "ticket.json"
        ticket = json.loads(path.read_text(encoding="utf-8"))
        ticket["signature"] = "ledger:tampered"
        path.write_text(json.dumps(ticket), encoding="utf-8")
        os.chmod(path, 0o600)
        self.assertEqual(read_dossier_repair_feedback(self.state), {})


if __name__ == "__main__":
    unittest.main()
