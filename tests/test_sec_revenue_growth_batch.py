"""Contract tests for the Gate 1 SEC revenue-growth batch tooling."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


batch = _load_script("sec_revenue_growth_batch", "run_sec_revenue_growth_batch.py")
replay = _load_script("sec_research_plan_replay", "replay_sec_research_plan_canary.py")


def success_result() -> dict:
    facts = {
        "entity_name": "EXAMPLE INC.",
        "cik": "0000789019",
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "concept_candidates": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        ],
        "eligible_concepts": ["Revenues"],
        "label": "Revenue",
        "unit": "USD",
        "filed_from": "2025-08-21",
        "filed_to": "2026-08-21",
        "latest_accession": "0000789019-26-000001",
        "selection_basis": "ordered_allowlist_latest_10-Q",
        "current": {
            "accession": "0000789019-26-000001",
            "start": "2026-01-01",
            "end": "2026-03-31",
            "filed": "2026-04-30",
            "form": "10-Q",
            "fp": "Q3",
            "fy": 2026,
            "frame": "CY2026Q1",
            "record_hash": "1" * 64,
            "value": "110000000000",
        },
        "prior": {
            "accession": "0000789019-26-000001",
            "start": "2025-01-01",
            "end": "2025-03-31",
            "filed": "2026-04-30",
            "form": "10-Q",
            "fp": "Q3",
            "fy": 2026,
            "frame": "CY2025Q1",
            "record_hash": "2" * 64,
            "value": "100000000000",
        },
        "growth_percent": "10.00",
        "source_record_refs": ["sec:current", "sec:prior"],
        "content_hash": "3" * 64,
    }
    return {
        "status": "autonomous-closed",
        "plan": {
            "ref": "research-plan:test",
            "hash": "4" * 64,
            "operation": "get_company_facts",
            "parameters": {"cik": "0000789019"},
        },
        "candidate": {
            "value": "10.00",
            "semantic_verification_status": "unverified",
            "artifact_refs": [{"ref": "artifact:test", "hash": "5" * 64}],
            "source_facts": facts,
            "verifications": [
                {"kind": "source", "verdict": "pass"},
                {"kind": "numeric", "verdict": "pass"},
            ],
        },
        "formal_ledger_counts": {
            "evidence_versions": 1,
            "claim_versions": 1,
            "thesis_versions": 0,
        },
        "promotion": {
            "claim_version_ref": "claim-version:test",
            "claim_version_hash": "6" * 64,
        },
        "closure": {
            "status": "fresh",
            "answer_binding_ref": "research-question-answer:test",
        },
        "closure_replay": {
            "status": "duplicate",
            "answer_binding_ref": "research-question-answer:test",
        },
        "human_gate_counts": {"plan_approvals": 0, "claim_reviews": 0},
        "model_accounting_counts": {
            "model_invocations": 0,
            "observability_usage_entries": 0,
            "observability_cost_entries": 0,
        },
        "integrity": {
            "core": "ok",
            "staging": "ok",
            "coordinator": "ok",
            "capability": "ok",
        },
    }


def fail_closed_result() -> dict:
    return {
        "status": "expected-fail-closed",
        "plan": {
            "parameters": {
                "cik": "0000104169",
                "concept_candidates": ["SalesRevenueNet"],
            }
        },
        "failure": {"status": "blocked"},
        "formal_ledger_counts": {
            "evidence_versions": 0,
            "claim_versions": 0,
            "thesis_versions": 0,
        },
        "candidate_counts": {
            "candidate_source_materials": 0,
            "candidate_numeric_specs": 0,
            "candidate_verifications": 0,
            "candidate_evidence_versions": 0,
            "candidate_claim_versions": 0,
            "candidate_stage_requests": 0,
        },
        "human_gate_counts": {"plan_approvals": 0, "claim_reviews": 0},
        "model_accounting_counts": {
            "model_invocations": 0,
            "observability_usage_entries": 0,
            "observability_cost_entries": 0,
        },
        "integrity": {
            "core": "ok",
            "staging": "ok",
            "coordinator": "ok",
            "capability": "ok",
        },
    }


class SecRevenueGrowthBatchTests(unittest.TestCase):
    def test_default_universe_is_exactly_five_unique_sec_issuers(self) -> None:
        self.assertEqual(len(batch.ISSUERS), 5)
        self.assertEqual(len({item["ticker"] for item in batch.ISSUERS}), 5)
        self.assertEqual(len({item["cik"] for item in batch.ISSUERS}), 5)
        self.assertEqual(
            [item["ticker"] for item in batch.ISSUERS],
            ["MSFT", "AAPL", "NVDA", "WMT", "AMZN"],
        )

    def test_success_requires_same_filing_passed_verifiers_and_duplicate_replay(self) -> None:
        result = success_result()
        facts = batch.validate_success_result(result, ticker="MSFT", cik="789019")
        self.assertEqual(facts["growth_percent"], "10.00")
        for mutate, message in (
            (
                lambda value: value["candidate"]["source_facts"]["prior"].update(
                    accession="different"
                ),
                "same-filing",
            ),
            (
                lambda value: value["candidate"]["verifications"][1].update(
                    verdict="reject"
                ),
                "verifier status",
            ),
            (
                lambda value: value["closure_replay"].update(status="fresh"),
                "closure replay",
            ),
        ):
            changed = copy.deepcopy(result)
            mutate(changed)
            with self.assertRaisesRegex(batch.BatchVerificationError, message):
                batch.validate_success_result(changed, ticker="MSFT", cik="789019")

    def test_summary_binds_authority_and_uses_zero_model_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text(json.dumps(success_result()), encoding="utf-8")
            summary = batch.summarize_success(
                success_result(),
                issuer={"ticker": "MSFT", "name": "Microsoft", "cik": "789019"},
                result_path=result_path,
            )
        self.assertEqual(summary["thesis_impact"], "not yet run")
        self.assertEqual(summary["actual_model_cost"]["amount"], "0.00")
        self.assertEqual(len(summary["authority_hash"]), 64)
        self.assertIn("000078901926000001", summary["filing_url"])

    def test_fail_closed_control_rejects_any_candidate_or_formal_claim(self) -> None:
        result = fail_closed_result()
        summary = batch.validate_fail_closed_result(
            result, expected_concepts=["SalesRevenueNet"]
        )
        self.assertEqual(summary["failure_status"], "blocked")
        changed = copy.deepcopy(result)
        changed["candidate_counts"]["candidate_claim_versions"] = 1
        with self.assertRaisesRegex(batch.BatchVerificationError, "staged a candidate"):
            batch.validate_fail_closed_result(
                changed, expected_concepts=["SalesRevenueNet"]
            )

    def test_brief_preserves_required_fields_without_inventing_thesis_impact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text(json.dumps(success_result()), encoding="utf-8")
            sample = batch.summarize_success(
                success_result(),
                issuer={"ticker": "MSFT", "name": "Microsoft", "cik": "789019"},
                result_path=result_path,
            )
        samples = []
        for issuer in batch.ISSUERS:
            item = copy.deepcopy(sample)
            item["ticker"] = issuer["ticker"]
            samples.append(item)
        control = batch.validate_fail_closed_result(
            fail_closed_result(), expected_concepts=["SalesRevenueNet"]
        )
        brief = batch.render_brief({
            "generated_at": "2026-08-21T12:00:00+00:00",
            "repo_commit": "a" * 40,
            "samples": samples,
            "fail_closed_sample": control,
        })
        self.assertIn("5/5 家公司", brief)
        self.assertIn("USD 110,000,000,000", brief)
        self.assertIn("us-gaap:Revenues", brief)
        self.assertIn("not yet run", brief)
        self.assertIn("USD 0.00", brief)
        self.assertIn("SalesRevenueNet", brief)

    def test_replay_result_loader_rejects_nonclosed_or_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(replay.ReplayVerificationError, "invalid"):
                replay._load_result(path)
            path.write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
            with self.assertRaisesRegex(
                replay.ReplayVerificationError, "not an autonomous closed"
            ):
                replay._load_result(path)


if __name__ == "__main__":
    unittest.main()
