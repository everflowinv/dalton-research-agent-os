"""S7c-5: isolated US IT Services brief v3 canary.

The canary is exercised as a subprocess with the synthetic ACN transcript
fixture from the Core-hosted acquisition tests; it must promote one formal
qualitative transcript claim through the ADR-0003 B path and render a
deterministic brief that carries it as corroboration only.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_alphaengine_core_acquisition import DIGEST, DOCUMENT, PAGE_ONE


ROOT = Path(__file__).resolve().parents[1]
ASPECT = "aspect:new-bookings-direction-local-currency"


class IndustryEvidenceBriefV3CanaryTests(unittest.TestCase):
    def test_isolated_brief_v3_adds_one_formal_qualitative_transcript_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            document = Path(temp) / "acn.txt"
            document.write_text(DOCUMENT, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_isolated_us_it_services_brief_v3_canary.py",
                    "--document-file", str(document),
                    "--page-chars", str(len(PAGE_ONE)),
                    "--include-report-body",
                ],
                cwd=ROOT, check=True, capture_output=True, text=True,
            )
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(DIGEST, result["document_content_sha256"])
        self.assertEqual("fake", result["acquisition"]["transport"])
        self.assertEqual("complete", result["acquisition"]["manifest_status"])
        self.assertEqual(2, result["acquisition"]["page_count"])
        self.assertEqual(1, result["acquisition"]["document_quota_units"])

        transcript = result["transcript"]
        self.assertEqual("qualitative", transcript["claim_kind"])
        self.assertIsNone(transcript["value"])
        self.assertEqual(1, transcript["unresolved_flag_count"])
        self.assertRegex(transcript["claim_version_ref"], r"^claim-version:[0-9a-f]{64}$")
        self.assertRegex(transcript["evidence_version_ref"], r"^evidence-version:[0-9a-f]{64}$")
        self.assertRegex(transcript["review_decision_ref"], r"^human-review:[0-9a-f]{64}$")

        counts = result["core_counts"]
        self.assertEqual(6, counts["evidence_versions"])
        self.assertEqual(22, counts["claim_versions"])
        self.assertEqual(22, counts["evidence_relations"])
        self.assertEqual(1, counts["reviewed_candidate_commits"])
        self.assertEqual(0, counts["thesis_versions"])

        self.assertEqual("driver-pack-version:us-it-services:4", result["driver_pack_version_ref"])
        self.assertEqual(4, result["driver_count"])
        self.assertEqual(19, result["metric_count"])
        self.assertEqual(4, result["coverage_company_count"])
        self.assertEqual(5, result["sec_evidence_count"])
        self.assertEqual(22, result["claim_count"])
        self.assertEqual(17, result["model_input_count"])
        self.assertEqual("industry-evidence-pack-version:us-it-services:3", result["evidence_pack_version_ref"])
        self.assertEqual(22, result["evidence_binding_count"])
        self.assertEqual({
            "company-overlay-version:acn:3", "company-overlay-version:ctsh:2",
            "company-overlay-version:epam:2", "company-overlay-version:ibm:2",
        }, set(result["company_overlay_version_refs"]))
        self.assertEqual(22, result["industry_brief_claim_count"])
        self.assertEqual(6, result["industry_brief_source_count"])
        self.assertEqual({
            "claim_kind": "qualitative", "value": None, "metric_or_aspect": ASPECT,
        }, result["industry_brief_transcript_claim"])
        self.assertEqual("source:alphaengine", result["industry_brief_transcript_source_ref"])
        self.assertEqual(4, result["driver_scoreboard_count"])
        self.assertEqual(20, result["metric_matrix_row_count"])
        self.assertEqual(80, result["metric_matrix_cell_count"])
        self.assertEqual(result["industry_brief_hash"], result["report_snapshot_hash"])
        self.assertTrue(result["report_replay_identical"])
        self.assertEqual(0, result["paid_model_calls"])
        self.assertEqual(0, result["network_calls"])
        self.assertEqual(0, result["live_core_writes"])

        body = result["report_body"]
        evidence_section = body.split("## KPI coverage gaps", 1)[0].split("## KPI evidence", 1)[1]
        self.assertIn("ACN — Accenture management said Q3 FY2026 new bookings declined", evidence_section)
        self.assertIn(f"Claim: {transcript['claim_version_ref']}", evidence_section)
        self.assertIn(f"Evidence: {transcript['evidence_version_ref']}", evidence_section)
        # The filing-backed -3% stays the numeric authority; the transcript adds direction only.
        self.assertIn("new bookings decreased 3% in local currency", evidence_section)
        gaps_section = body.split("## KPI coverage gaps", 1)[1].split("## Debates", 1)[0]
        self.assertIn("No authenticated earnings-call transcript for this issuer", gaps_section)
        self.assertEqual(3, gaps_section.count("No authenticated earnings-call transcript for this issuer"))
        debates_section = body.split("## Debates", 1)[1].split("## Falsifiers", 1)[0]
        self.assertIn("debate:ai-demand-versus-bookings-conversion-v3", debates_section)
        self.assertIn(transcript["claim_version_ref"], debates_section)
        sources_section = body.split("## Source authorities", 1)[1]
        self.assertIn("type=authenticated_transcript", sources_section)
        self.assertIn("transcript-claim-citation-binding:", sources_section)


if __name__ == "__main__":
    unittest.main()
