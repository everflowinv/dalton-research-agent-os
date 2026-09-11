"""Operation-scoped Claim snapshot reuse for deterministic dossier inputs."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.company_dossier import CompanyDossierAuthority
from dalton_core.company_dossier_cli import (
    _ReadOnlyStoreView, plan_units, reconstruct_dossier_input,
)
from dalton_core.company_research_view import (
    CompanyResearchViewValidationError, prepare_company_claim_query,
    query_company_research,
)
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.store import DaltonStore
from tests.test_dossier_lane import ACN, Harness, policy_document


class DossierSnapshotPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def _count_snapshots(self):
        original = DaltonStore.claim_index_snapshot
        calls = []

        def counted(store, *args, **kwargs):
            calls.append(store)
            return original(store, *args, **kwargs)

        return calls, patch.object(DaltonStore, "claim_index_snapshot", counted)

    def _constitution(self):
        binding = self.harness.mission["bindings"]["constitution_version"]
        return ResearchConstitutionAuthority(self.harness.store).constitution(binding["ref"])

    def test_all_twelve_reconstructed_inputs_are_byte_identical_to_legacy_reads(self):
        summary = self.harness.run(max_units=3)
        self.assertEqual(summary["status"], "succeeded")
        record = CompanyDossierAuthority(self.harness.store).latest(ACN)
        self.assertIsNotNone(record)

        calls, counter = self._count_snapshots()
        with counter:
            legacy = reconstruct_dossier_input(
                self.harness.store.connection, record, self.harness.mission,
                policy_document(), _reuse_claim_snapshot=False)
            legacy_calls = len(calls)
            calls.clear()
            shared = reconstruct_dossier_input(
                self.harness.store.connection, record, self.harness.mission,
                policy_document())
            shared_calls = len(calls)

        self.assertEqual(set(shared), set(legacy))
        self.assertEqual(len(shared), 12)
        self.assertEqual(shared, legacy)
        self.assertGreater(legacy_calls, 12)
        self.assertEqual(shared_calls, 1)

        # A pre-provenance record takes the same reconstruction path and does
        # not gain synthetic bindings merely because its Claim view is shared.
        legacy_record = dict(record)
        legacy_record["schema_version"] = "company-dossier-0.2"
        legacy_record.pop("unit_provenance", None)
        old = reconstruct_dossier_input(
            self.harness.store.connection, legacy_record, self.harness.mission,
            policy_document(), _reuse_claim_snapshot=False)
        new = reconstruct_dossier_input(
            self.harness.store.connection, legacy_record, self.harness.mission,
            policy_document())
        self.assertEqual(new, old)

    def test_empty_company_plan_reads_one_snapshot_and_remains_unavailable(self):
        calls, counter = self._count_snapshots()
        with counter:
            plan = plan_units(
                store=self.harness.store, company_ref="company:empty",
                constitution=self._constitution(),
                policy=policy_document(), prior=None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(plan), 12)
        self.assertTrue(all(row["status"] == "unavailable" for row in plan.values()))

    def test_snapshot_failure_is_single_and_produces_no_partial_plan(self):
        calls = []

        def fail(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("snapshot unavailable")

        with patch.object(DaltonStore, "claim_index_snapshot", fail):
            with self.assertRaisesRegex(RuntimeError, "snapshot unavailable"):
                plan_units(
                    store=self.harness.store, company_ref=ACN,
                    constitution=self._constitution(),
                    policy=policy_document(), prior=None)
        self.assertEqual(calls, [1])

    def test_context_cannot_cross_company_or_authority_connection(self):
        context = prepare_company_claim_query(self.harness.store, ACN)
        with self.assertRaisesRegex(CompanyResearchViewValidationError,
                                    "different company"):
            query_company_research(
                self.harness.store, company_ref="company:other",
                claim_context=context)
        second = Harness()
        self.addCleanup(second.close)
        with self.assertRaisesRegex(CompanyResearchViewValidationError,
                                    "different authority"):
            query_company_research(
                second.store, company_ref=ACN, claim_context=context)


if __name__ == "__main__":
    unittest.main()
