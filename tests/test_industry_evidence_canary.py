import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IndustryEvidenceCanaryTests(unittest.TestCase):
    def test_isolated_peer_pack_creates_four_overlays_without_thesis_or_paid_call(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/run_isolated_us_it_services_evidence_canary.py",
                "--include-report-body",
            ],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(4, result["driver_count"])
        self.assertEqual(18, result["metric_count"])
        self.assertEqual(4, result["coverage_company_count"])
        self.assertEqual(5, result["evidence_count"])
        self.assertEqual(21, result["claim_count"])
        self.assertEqual(17, result["model_input_count"])
        self.assertEqual(4, result["company_overlay_count"])
        self.assertEqual(21, result["industry_brief_claim_count"])
        self.assertEqual(5, result["industry_brief_source_count"])
        self.assertEqual(4, result["driver_scoreboard_count"])
        self.assertEqual(19, result["metric_matrix_row_count"])
        self.assertEqual(76, result["metric_matrix_cell_count"])
        self.assertRegex(result["industry_brief_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["report_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["industry_brief_hash"], result["report_snapshot_hash"])
        self.assertTrue(result["report_replay_identical"])
        self.assertEqual({
            "company-overlay-version:acn:2", "company-overlay-version:ctsh:1",
            "company-overlay-version:epam:1", "company-overlay-version:ibm:1",
        }, set(result["company_overlay_version_refs"]))
        self.assertEqual(0, result["thesis_version_count"])
        self.assertEqual(0, result["paid_model_calls"])
        self.assertEqual([
            "0001467373-26-000031", "0001058290-26-000030",
            "0001352010-26-000043", "0000051143-26-000077",
            "0000051143-26-000078",
        ], result["source_accessions"])
        body = result["report_body"]
        for section in (
            "Boundary and universe", "Driver scoreboard", "KPI evidence",
            "KPI coverage gaps", "Debates", "Falsifiers", "Open questions",
            "Source authorities",
        ):
            self.assertIn(f"## {section}", body)
        evidence_section = body.split("## KPI coverage gaps", 1)[0].split(
            "## KPI evidence", 1
        )[1]
        self.assertNotIn("not_comparable", evidence_section)
        self.assertNotIn(
            "Total bookings are not identified as AI-specific bookings.",
            evidence_section,
        )
        gaps_section = body.split("## KPI coverage gaps", 1)[1].split("## Debates", 1)[0]
        self.assertIn("status=not_comparable", gaps_section)
        self.assertIn(
            "Total bookings are not identified as AI-specific bookings.",
            gaps_section,
        )
        for source_ref in (
            "sec:0001467373-26-000031:q3fy26earnings8-kexhibit.htm",
            "sec:0001058290-26-000030:exhibit9916302026.htm",
            "sec:0001352010-26-000043:exhibit99_q2x2026.htm",
            "sec:0000051143-26-000077:ibm-20260722xex991.htm",
            "sec:0000051143-26-000078:ibm-20260630.htm",
        ):
            self.assertIn(source_ref, body)


if __name__ == "__main__":
    unittest.main()
