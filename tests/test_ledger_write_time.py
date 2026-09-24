"""2026-09-24b: a policy-admitted Claim is dated when it entered the Ledger.

Every candidate staged before policy-17 entered the legacy Ledger dated
2026-09-17T02:35:49 -- the policy's own creation -- a week before it was
written, so "what was admitted since" could not find it.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from dalton_core.document_extraction_cli import run_extraction
# Through the module, so this file does not collect the harness's tests again.
from tests import test_document_extraction_automation as _automation


def _own_tests_only(cls):
    """Run only this class's tests, not the drafting harness's it inherits."""

    for name in dir(_automation.AutomationDraftingTests):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@_own_tests_only
class LedgerWriteTimeTests(_automation.AutomationDraftingTests):
    """A policy-admitted Claim is dated when it entered the Ledger."""

    def test_a_claim_admitted_now_is_not_backdated_to_its_policy_or_candidate(self) -> None:
        self._grant_automation()
        self._policy_with_document_rule()
        context = self._active_context()
        fixture = self.root / "time-fixture.json"
        fixture.write_text(json.dumps({"schema_version": "0.1", "suggestions": [{
            "quote_id": context["quotes"][0]["quote_id"],
            "normalized_statement": "Accenture management says client decisions remain cautious.",
            "metric_or_aspect": "aspect:client-decisions", "period": "FY26",
            "basis": "fixture management commentary"}]}), encoding="utf-8")
        before = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        summary = run_extraction(
            state_dir=self.root, model_config_path=self._model_config(),
            summary_dir=self.root / "time-summary", spool_dir=self.root / "spool",
            scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
            max_windows=2, max_numeric_windows=0, max_discovery_windows=0,
            connector_governance=None, web_fetch_governance=None,
            hermetic_fixture=fixture, candidate_staging=self.root / "staging.sqlite",
        )
        [admitted] = summary["admitted"]
        self.assertEqual(admitted["status"], "admitted", admitted)
        core = self.h.h.core
        claim = core.get_claim(admitted["claim_version_ref"])["claim"]
        row = core.connection.execute(
            "SELECT created_at FROM claim_versions WHERE claim_version_id=?",
            (admitted["claim_version_ref"],)).fetchone()
        decision = core.connection.execute(
            "SELECT result_json FROM reviewed_candidate_commits WHERE candidate_claim_ref=?",
            (claim["candidate_origin_ref"],)).fetchone()
        self.assertGreaterEqual(claim["created_at"], before)
        self.assertEqual(row["created_at"], claim["created_at"])
        self.assertIsNotNone(decision)
        policy_created = core.active_policy_version().to_dict()["created_at"]
        # The decision keeps its deterministic time; only the Ledger row moved.
        self.assertLess(policy_created, before)


if __name__ == "__main__":
    unittest.main()
