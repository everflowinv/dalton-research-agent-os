from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector_governance import build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import validate_discovery_plan
from dalton_core.store import DaltonStore, content_hash
from scripts.build_sec_8k_discovery_proposal import build_candidate_plan, build_review_bundle
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params


class Sec8KDiscoveryProposalTests(unittest.TestCase):
    def active_plan(self):
        body = {
            "schema_version": "0.4", "id": "discovery-plan:us-it-services:sec-filings:1",
            "created_at": "2026-09-08T00:00:00.000000+00:00",
            "mission_ref": "coverage-mission:us-it-services", "source_ref": "source:sec-edgar",
            "budget": {"max_calls_24h": 50},
            "companies": {f"company:sec-cik:{ref}": {"cik": cik} for ref, cik in (
                ("0000051143", "0000051143"), ("0001058290", "0001058290"),
                ("0001352010", "0001352010"), ("0001467373", "0001467373"),
                ("001688568", "0001688568"))},
            "specs": [{"spec_ref": "annual-report-10k", "form": "10-K", "lookback_days": 400,
                       "rediscovery_interval_days": 30, "retry_interval_days": 2}],
        }
        return validate_discovery_plan({**body, "content_hash": content_hash(body)})

    def test_candidate_preserves_policy_and_adds_only_8k(self):
        active = self.active_plan()
        candidate = build_candidate_plan(active, created_at="2026-09-10T00:00:00+00:00")
        self.assertEqual(candidate["id"], "discovery-plan:us-it-services:sec-filings:2")
        for field in ("schema_version", "mission_ref", "source_ref", "budget", "companies"):
            self.assertEqual(candidate[field], active[field])
        self.assertEqual(candidate["specs"][:-1], active["specs"])
        self.assertEqual(candidate["specs"][-1]["form"], "8-K")
        self.assertEqual(candidate["specs"][-1]["rediscovery_interval_days"], 1)

    def test_duplicate_8k_is_refused(self):
        candidate = build_candidate_plan(self.active_plan(), created_at="2026-09-10T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "already contains"):
            build_candidate_plan(candidate, created_at="2026-09-11T00:00:00+00:00")

    def test_bundle_binds_real_authority_inputs_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DaltonStore(str(Path(tmp) / "core.sqlite"))
            authorities = bootstrap_method_authorities(store)
            params = mission_params(authorities)
            params["autonomy"]["may_write"].append("source_discovery")
            next(row for row in params["source_plan"] if row["source_ref"] == "source:sec-edgar")["status"] = "connected"
            ref = params.pop("mission_ref")
            mission = CoverageMissionAuthority(store).create_mission(ref, **params)
            store.close()
            governance = build_governance_record("sec-filings-index", approved_by="human:lumos", status="approved")
            active = self.active_plan()
            candidate = build_candidate_plan(active, created_at="2026-09-10T00:00:00+00:00")
            bundle = build_review_bundle(active_plan=active, candidate_plan=candidate,
                                         mission=mission, governance=governance,
                                         created_at="2026-09-10T00:00:00+00:00")
            self.assertEqual(bundle["prior_plan"]["hash"], active["content_hash"])
            self.assertEqual(bundle["mission_binding"]["hash"], mission["content_hash"])
            self.assertIsNone(bundle["governance_change"])
            self.assertEqual(bundle["content_hash"], content_hash({k: v for k, v in bundle.items() if k != "content_hash"}))


if __name__ == "__main__":
    unittest.main()
