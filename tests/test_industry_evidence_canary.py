import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IndustryEvidenceCanaryTests(unittest.TestCase):
    def test_isolated_acn_seed_pack_creates_no_thesis_or_paid_call(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_isolated_us_it_services_evidence_canary.py"],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(4, result["driver_count"])
        self.assertEqual(4, result["coverage_company_count"])
        self.assertEqual(6, result["claim_count"])
        self.assertEqual(5, result["model_input_count"])
        self.assertEqual(0, result["thesis_version_count"])
        self.assertEqual(0, result["paid_model_calls"])
        self.assertEqual("0001467373-26-000031", result["source_accession"])


if __name__ == "__main__":
    unittest.main()
