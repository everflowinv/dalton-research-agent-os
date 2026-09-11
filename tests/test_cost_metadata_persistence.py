"""Persistence boundaries for additive company-model cost metadata."""

from __future__ import annotations

import unittest

from dalton_core.company_model_inputs import ModelInputError, build_model_inputs
from dalton_core.company_model_spec import spec_from_response
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore, content_hash
from tests.test_company_model_inputs import FakeMissions, _line
from tests.test_company_model_spec import DECIDED_BY, STATE, _spec


MISSION = "coverage-mission-version:test:1"


class CostMetadataPersistenceTests(unittest.TestCase):
    def _decided(self, *, cost_bound: bool):
        body = _spec()
        if cost_bound:
            body["expense_lines"][0]["cost_driver_slot"] = "delivery_cost"
        state = {**STATE, "industry_classification": "contract_compounder"}
        return spec_from_response(state, body, decided_by=DECIDED_BY)

    def _inputs(self):
        return FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "100"),
            _line("us-gaap:CostOfRevenue", "2026-03-01", "2026-05-31", "70"),
            _line("us-gaap:SellingGeneralAndAdministrativeExpense",
                  "2026-03-01", "2026-05-31", "10"),
        ])

    def test_legacy_row_and_hash_survive_nullable_column_migration(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        authority = CoverageMissionAuthority(store)
        legacy = self._decided(cost_bound=False)
        legacy.pop("schema_version")
        legacy.pop("financial_statement_structure")
        legacy.pop("cash_flow_companion")
        legacy.pop("content_hash")
        legacy["content_hash"] = content_hash(legacy)
        original = authority.record_company_model_spec(
            legacy, mission_version_ref=MISSION)
        original.pop("status")

        # Reproduce the immediately preceding table contract. The migration is
        # additive and must not rewrite an immutable specification body/hash.
        store.connection.execute(
            "ALTER TABLE coverage_mission_company_model_specs DROP COLUMN metadata_json")
        CoverageMissionAuthority(store)
        replayed = authority.latest_company_model_spec(original["company_ref"])

        self.assertEqual(replayed, original)
        self.assertNotIn("cost_driver_template", replayed)
        self.assertEqual(replayed["content_hash"], original["content_hash"])

    def test_persisted_stale_registry_metadata_is_rejected_by_input_builder(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        authority = CoverageMissionAuthority(store)
        decided = self._decided(cost_bound=True)
        stale = dict(decided["cost_driver_template"])
        stale["registry_hash"] = "0" * 64
        # The authority's established boundary validates the closed storage
        # shape; the consumer validates whether the referenced registry is the
        # installed one. Persist a historically valid/stale reference through
        # that public API rather than disabling append-only triggers.
        stale_decided = {**decided, "cost_driver_template": stale}
        stale_decided.pop("content_hash")
        stale_decided["content_hash"] = content_hash(stale_decided)
        held = authority.record_company_model_spec(
            stale_decided, mission_version_ref=MISSION)

        replayed = authority.latest_company_model_spec(held["company_ref"])
        with self.assertRaisesRegex(ModelInputError, "stale cost template metadata"):
            build_model_inputs(self._inputs(), replayed)


if __name__ == "__main__":
    unittest.main()
