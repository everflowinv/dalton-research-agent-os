from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path

from dalton_core.mission_document_model_authority import (
    DRAFT_MODEL_CONFIG_NAME,
    DRAFT_PURPOSE,
    MissionDocumentModelAuthority,
    MissionDocumentModelAuthorityError,
    VERIFIER_MODEL_CONFIG_NAME,
    VERIFIER_PURPOSE,
    load_mission_document_model_configs,
)
from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME as ANNUAL_DRAFT_CONFIG,
    VERIFIER_MODEL_CONFIG_NAME as ANNUAL_VERIFIER_CONFIG,
)
from dalton_core.store import canonical_json, content_hash
from tests.test_mission_annual_research import MissionAnnualFixture


class MissionDocumentModelAuthorityTests(unittest.TestCase):
    def _fixture(self, *, same_family: bool = False):
        fixture = MissionAnnualFixture(self, same_family=same_family)
        for source, target in (
            (ANNUAL_DRAFT_CONFIG, DRAFT_MODEL_CONFIG_NAME),
            (ANNUAL_VERIFIER_CONFIG, VERIFIER_MODEL_CONFIG_NAME),
        ):
            value = json.loads((fixture.state / source).read_text(encoding="utf-8"))
            (fixture.state / target).write_text(
                canonical_json(value) + "\n", encoding="utf-8"
            )
            os.chmod(fixture.state / target, 0o600)
        return fixture

    def test_exact_generic_configs_router_and_budget_build_closed_authority(self):
        fixture = self._fixture()
        resolver = MissionDocumentModelAuthority(
            state_dir=fixture.state,
            router=fixture.router,
            clock=fixture.harness.clock,
        )
        executions, proof = resolver()
        self.assertEqual(set(executions), {"draft", "verifier"})
        self.assertEqual(proof["draft"]["purpose"], DRAFT_PURPOSE)
        self.assertEqual(proof["verifier"]["purpose"], VERIFIER_PURPOSE)
        self.assertEqual(proof["draft"]["workflow_capability"],
                         "capability:dalton:model:qualitative-research")
        self.assertEqual(proof["draft"]["router_capability"], "research")
        self.assertEqual(proof["verifier"]["workflow_capability"],
                         "capability:dalton:model:qualitative-verifier")
        self.assertEqual(proof["verifier"]["router_capability"], "verify")
        self.assertEqual(
            proof["draft"]["config_hash"],
            content_hash(load_mission_document_model_configs(fixture.state)[0]),
        )
        self.assertEqual(
            proof["verifier"]["budget_policy_ceiling"]["policy_version_ref"],
            executions["verifier"]["budget_policy_ref"],
        )

    def _flaky_budget(self, failures, error):
        """Replace the budget store with one that loses `failures` races."""

        import dalton_core.mission_document_model_authority as module

        real = module.ThesisImpactBudgetStore
        opens = []

        def flaky(path, **kwargs):
            opens.append(path)
            if len(opens) <= failures:
                raise error
            return real(path, **kwargs)

        return module, flaky, opens

    def test_a_budget_ledger_lost_to_a_writer_is_retried_not_failed(self):
        """Live: two admissions died on a policy that was there all along.

        The budget ledger is a live WAL database another lane writes; the
        read lost the race and a blanket failure turned it into a dead run
        that a person then had to look at.
        """

        import sqlite3
        from unittest.mock import patch

        fixture = self._fixture()
        module, flaky, opens = self._flaky_budget(
            1, sqlite3.OperationalError("database is locked"))
        waits = []
        resolver = MissionDocumentModelAuthority(
            state_dir=fixture.state, router=fixture.router,
            clock=fixture.harness.clock, budget_retry_sleep=waits.append,
        )
        with patch.object(module, "ThesisImpactBudgetStore", flaky):
            _executions, proof = resolver()
        # One lost race, one short wait, then both stages read their ceiling.
        self.assertEqual((len(opens), waits), (3, [0.5]))
        for stage in ("draft", "verifier"):
            self.assertGreater(
                proof[stage]["budget_policy_ceiling"]["day_cap_micros"], 0)

    def test_a_ledger_that_stays_locked_reports_the_underlying_error(self):
        import sqlite3
        from unittest.mock import patch

        fixture = self._fixture()
        module, flaky, opens = self._flaky_budget(
            99, sqlite3.OperationalError("database is locked"))
        waits = []
        resolver = MissionDocumentModelAuthority(
            state_dir=fixture.state, router=fixture.router,
            clock=fixture.harness.clock, budget_retry_sleep=waits.append,
        )
        with patch.object(module, "ThesisImpactBudgetStore", flaky):
            with self.assertRaisesRegex(
                MissionDocumentModelAuthorityError,
                "draft budget policy is unavailable: OperationalError: database is locked",
            ):
                resolver()
        self.assertEqual((len(opens), waits), (3, [0.5, 0.5]))

    def test_cleanly_closed_wal_policy_is_read_without_creating_sidecars(self):
        """A child may start in the cold interval between budget writers."""

        fixture = self._fixture()
        budget_path = Path(fixture.budget.path)
        fixture.budget.close()
        sidecars = [Path(str(budget_path) + suffix) for suffix in ("-wal", "-shm")]
        self.assertTrue(all(not path.exists() for path in sidecars))
        before_bytes = budget_path.read_bytes()
        before_stat = budget_path.stat()
        before_names = sorted(path.name for path in budget_path.parent.iterdir())

        executions, proof = MissionDocumentModelAuthority(
            state_dir=fixture.state,
            router=fixture.router,
            clock=fixture.harness.clock,
        )()

        self.assertEqual(executions["draft"]["budget_policy_ref"],
                         "budget-policy:mission-annual:1")
        self.assertEqual(
            proof["verifier"]["budget_policy_ceiling"]["day_cap_micros"],
            10_000_000,
        )
        self.assertEqual(budget_path.read_bytes(), before_bytes)
        after_stat = budget_path.stat()
        self.assertEqual(
            (after_stat.st_size, after_stat.st_mtime_ns, after_stat.st_ctime_ns),
            (before_stat.st_size, before_stat.st_mtime_ns, before_stat.st_ctime_ns),
        )
        self.assertEqual(
            sorted(path.name for path in budget_path.parent.iterdir()), before_names
        )
        self.assertTrue(all(not path.exists() for path in sidecars))

    def test_cold_wal_snapshot_rejects_a_sidecar_race(self):
        from dalton_core.readonly_sqlite import connect_cold_wal_snapshot

        fixture = self._fixture()
        budget_path = Path(fixture.budget.path)
        fixture.budget.close()
        wal = Path(str(budget_path) + "-wal")
        self.assertFalse(wal.exists())
        try:
            with self.assertRaisesRegex(
                sqlite3.OperationalError, "changed during immutable snapshot"
            ):
                with connect_cold_wal_snapshot(budget_path) as connection:
                    self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
                    wal.write_bytes(b"concurrent-writer-marker")
        finally:
            wal.unlink(missing_ok=True)

    def test_an_unregistered_budget_policy_still_fails_on_the_first_read(self):
        fixture = self._fixture()
        for name in (DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME):
            path = fixture.state / name
            value = json.loads(path.read_text(encoding="utf-8"))
            value["budget_policy_ref"] = "thesis-impact-day-budget-policy:absent"
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        waits = []
        with self.assertRaisesRegex(
            MissionDocumentModelAuthorityError,
            "budget policy is unavailable: ThesisImpactBudgetConflict: "
            "budget policy is not registered",
        ):
            MissionDocumentModelAuthority(
                state_dir=fixture.state, router=fixture.router,
                clock=fixture.harness.clock, budget_retry_sleep=waits.append,
            )()
        self.assertEqual(waits, [])

    def test_missing_generic_configs_never_fall_back_to_annual_configs(self):
        fixture = MissionAnnualFixture(self)
        with self.assertRaisesRegex(
            MissionDocumentModelAuthorityError, "mission document draft.*not configured"
        ):
            MissionDocumentModelAuthority(
                state_dir=fixture.state,
                router=fixture.router,
                clock=fixture.harness.clock,
            )()

    def test_draft_and_verifier_must_share_router_and_budget_authority(self):
        fixture = self._fixture()
        path = fixture.state / VERIFIER_MODEL_CONFIG_NAME
        value = json.loads(path.read_text(encoding="utf-8"))
        value["budget_policy_ref"] = "budget-policy:foreign"
        path.write_text(canonical_json(value) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(MissionDocumentModelAuthorityError, "one budget authority"):
            load_mission_document_model_configs(fixture.state)

    def test_verifier_family_must_be_independent(self):
        fixture = self._fixture(same_family=True)
        with self.assertRaisesRegex(MissionDocumentModelAuthorityError, "independent"):
            MissionDocumentModelAuthority(
                state_dir=fixture.state,
                router=fixture.router,
                clock=fixture.harness.clock,
            )()

    def _install_verifier_route(self, fixture, *, mode="explicit",
                                in_chain=True, reject_provider=False):
        profile = json.loads(json.dumps(fixture.verifier_profile))
        profile.update({
            "id": "profile:explicit-verifier",
            "profile_version_ref": "model-profile-version:explicit-verifier:1",
            "family": "explicit-independent",
        })
        fixture.router.register_profile(profile)
        policy = json.loads(json.dumps(fixture.verifier_policy))
        policy.update({
            "policy_version_ref": "routing-policy:annual-mission-verifier:2",
            "version": 2,
            "prior_version_ref": fixture.verifier_policy["policy_version_ref"],
        })
        if reject_provider:
            policy["filters"]["allowed_providers"] = ["another-provider"]
        chain = [profile["id"]] if in_chain else [fixture.verifier_profile["id"]]
        if mode == "explicit":
            policy["purpose_overrides"] = {
                VERIFIER_PURPOSE: {"mode": "explicit", "chain": chain}
            }
        else:
            policy["fallback_chains"]["tiers"]["verifier"] = chain
        fixture.router.register_policy(policy)
        path = fixture.state / VERIFIER_MODEL_CONFIG_NAME
        config = json.loads(path.read_text(encoding="utf-8"))
        config["routing_policy_ref"] = policy["policy_version_ref"]
        path.write_text(canonical_json(config) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def test_explicit_purpose_chain_can_select_outside_legacy_profile_allowlist(self):
        fixture = self._fixture()
        self._install_verifier_route(fixture)
        executions, proof = MissionDocumentModelAuthority(
            state_dir=fixture.state, router=fixture.router,
            clock=fixture.harness.clock,
        )()
        self.assertEqual(executions["verifier"]["routing_policy_ref"],
                         "routing-policy:annual-mission-verifier:2")
        candidates = proof["verifier"]["configured_candidate_profiles"]
        self.assertEqual([row["profile_ref"] for row in candidates],
                         ["profile:explicit-verifier"])
        self.assertEqual(candidates[0]["preflight_reasons"], [])

    def test_tier_chain_does_not_override_legacy_profile_allowlist(self):
        fixture = self._fixture()
        self._install_verifier_route(fixture, mode="tier")
        with self.assertRaisesRegex(MissionDocumentModelAuthorityError,
                                    "no eligible model"):
            MissionDocumentModelAuthority(
                state_dir=fixture.state, router=fixture.router,
                clock=fixture.harness.clock,
            )()

    def test_explicit_purpose_does_not_relax_other_profile_filters(self):
        fixture = self._fixture()
        self._install_verifier_route(fixture, reject_provider=True)
        with self.assertRaisesRegex(MissionDocumentModelAuthorityError,
                                    "no eligible model"):
            MissionDocumentModelAuthority(
                state_dir=fixture.state, router=fixture.router,
                clock=fixture.harness.clock,
            )()

    def test_explicit_purpose_cannot_select_a_profile_outside_its_chain(self):
        fixture = self._fixture()
        self._install_verifier_route(fixture, in_chain=False)
        executions, proof = MissionDocumentModelAuthority(
            state_dir=fixture.state, router=fixture.router,
            clock=fixture.harness.clock,
        )()
        candidates = proof["verifier"]["configured_candidate_profiles"]
        self.assertEqual([row["profile_ref"] for row in candidates],
                         [fixture.verifier_profile["id"]])
        self.assertNotIn("profile:explicit-verifier",
                         [row["profile_ref"] for row in candidates])


if __name__ == "__main__":
    unittest.main()
