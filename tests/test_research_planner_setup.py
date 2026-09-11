"""P13k: the planner gets its own routing policy, and nobody gets it by accident."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.model_router import ModelRouter
from dalton_core.research_planner_setup import (
    CONFIG_FILE_NAME,
    POLICY_ID,
    PlannerSetupError,
    credential_slots_for,
    ensure_planner_policy,
    install,
)
from dalton_core.writer_server import planner_budget_config

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
ASTRA = "profile:gpt-6-astra"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ModelRouter(str(Path(self.temp.name) / "router.sqlite"))
        self.addCleanup(self.router.close)
        for profile in openclaw_broker_profiles(checked_at=NOW):
            self.router.register_profile(profile)

    def test_the_planner_policy_is_not_the_extraction_policy(self):
        from dalton_core.document_extraction_setup import POLICY_ID as EXTRACTION

        self.assertNotEqual(POLICY_ID, EXTRACTION)

    def test_a_policy_pins_exactly_what_it_was_given(self):
        policy = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        wire = self.router.get_policy(policy["policy_version_ref"])
        self.assertEqual(wire["filters"]["allowed_profile_ids"], [ASTRA])

    def test_installing_the_same_policy_twice_appends_once(self):
        first = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        again = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(first["policy_version_ref"], again["policy_version_ref"])

    def test_changing_the_pinned_profile_appends_a_new_version(self):
        first = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        second = ensure_planner_policy(
            self.router, profile_ids=["profile:gpt-5-6-terra"], now=NOW)
        self.assertNotEqual(first["policy_version_ref"], second["policy_version_ref"])
        self.assertEqual(
            self.router.get_policy(second["policy_version_ref"])["prior_version_ref"],
            first["policy_version_ref"])

    def test_a_policy_pinning_nothing_is_refused(self):
        with self.assertRaises(PlannerSetupError):
            ensure_planner_policy(self.router, profile_ids=[], now=NOW)

    def test_the_credential_slot_is_read_from_the_profile_not_guessed(self):
        # A wrong slot is a call that fails at the broker with no useful reason.
        self.assertEqual(credential_slots_for(self.router, [ASTRA]),
                         ["credential-slot:openclaw:openai"])

    def test_pinning_a_profile_nobody_registered_is_refused(self):
        with self.assertRaises(PlannerSetupError) as caught:
            credential_slots_for(self.router, ["profile:not-installed"])
        self.assertIn("not registered", str(caught.exception))


class AstraProfileTests(unittest.TestCase):
    """The planner's model has to be usable for the capability it asks for."""

    def profile(self):
        return next(p for p in openclaw_broker_profiles(checked_at=NOW)
                    if p["id"] == ASTRA)

    def test_astra_is_trusted_for_research_not_only_verification(self):
        # Derived from the provider catalog alone it comes out verify-only, and
        # the planner asks for "research": it would never be selected.
        from dalton_core.research_planner import build_work

        self.assertIn("research", self.profile()["capabilities"])
        state = {"content_hash": "0" * 64, "companies": [], "goal": {}}
        self.assertIn("research", build_work(
            state, created_at=NOW.isoformat(), state_ref="research-state:1"
        ).requested_capabilities)

    def test_its_context_fits_the_planner_budget(self):
        from dalton_core.research_planner import build_work

        state = {"content_hash": "0" * 64, "companies": [], "goal": {}}
        work = build_work(state, created_at=NOW.isoformat(), state_ref="research-state:1")
        limits = self.profile()["limits"]
        self.assertGreater(limits["max_input_tokens"], work.budget["max_input_tokens"])
        self.assertGreaterEqual(limits["max_output_tokens"], work.budget["max_output_tokens"])

    def test_it_is_priced_as_the_expensive_model_it_is(self):
        # The reason nothing routes here by default.
        from dalton_core.model_deployment import openclaw_broker_profiles as profiles

        cheap = next(p for p in profiles(checked_at=NOW)
                     if p["id"] == "profile:deepseek-v4-flash")
        self.assertGreater(self.profile()["cost"]["input_per_million_usd"],
                           cheap["cost"]["input_per_million_usd"] * 10)


if __name__ == "__main__":
    unittest.main()


class CatalogTests(unittest.TestCase):
    """P13k: nothing in the deploy ever registered profiles, so the catalog drifted."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ModelRouter(str(Path(self.temp.name) / "router.sqlite"))
        self.addCleanup(self.router.close)

    def test_an_empty_router_gets_the_whole_catalog(self):
        from dalton_core.model_deployment import _ENDPOINTS, ensure_broker_profiles

        result = ensure_broker_profiles(self.router, checked_at=NOW)
        self.assertEqual(len(result["added"]), len(_ENDPOINTS))
        self.assertIn(ASTRA, result["added"])

    def test_a_second_run_adds_nothing(self):
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        again = ensure_broker_profiles(self.router, checked_at=NOW + timedelta(days=1))
        self.assertEqual(again["added"], [])

    def test_re_registering_would_churn_versions_which_is_why_it_does_not(self):
        # A profile carries an availability timestamp, so registering it again
        # appends a version that says nothing: the live deepseek profile is at
        # version eleven for exactly that reason.
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        ensure_broker_profiles(self.router, checked_at=NOW + timedelta(days=1))
        versions = self.router.connection.execute(
            "SELECT COUNT(*) FROM model_endpoint_profile_versions WHERE profile_id=?",
            (ASTRA,),
        ).fetchone()[0]
        self.assertEqual(versions, 1)

    def test_only_the_missing_one_is_added(self):
        from dalton_core.model_deployment import ensure_broker_profiles, openclaw_broker_profiles

        for profile in openclaw_broker_profiles(checked_at=NOW):
            if profile["id"] != ASTRA:
                self.router.register_profile(profile)
        result = ensure_broker_profiles(self.router, checked_at=NOW)
        self.assertEqual(result["added"], [ASTRA])

    def test_pinning_now_succeeds_because_the_profile_exists(self):
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        self.assertEqual(credential_slots_for(self.router, [ASTRA]),
                         ["credential-slot:openclaw:openai"])

    def test_a_lapsed_profile_is_refreshed_rather_than_left_dead(self):
        # Availability was advanced only by a *successful call*, and a call
        # requires valid availability -- so a lane that goes quiet for a week
        # could never start again. Live, deepseek-v4-flash expired four hours
        # after the extraction queue emptied and the lane could not make the
        # call that would have kept it alive.
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        later = NOW + timedelta(days=30)
        result = ensure_broker_profiles(self.router, checked_at=later)
        self.assertEqual(result["added"], [])
        self.assertIn(ASTRA, result["refreshed"])

    def test_a_current_profile_is_left_alone(self):
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        soon = ensure_broker_profiles(self.router, checked_at=NOW + timedelta(days=1))
        self.assertEqual((soon["added"], soon["refreshed"]), ([], []))

    def test_a_refreshed_profile_keeps_its_curated_substance(self):
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        before = self.router.connection.execute(
            "SELECT profile_json FROM model_endpoint_profile_versions WHERE profile_id=? "
            "ORDER BY rowid DESC LIMIT 1", (ASTRA,)).fetchone()
        ensure_broker_profiles(self.router, checked_at=NOW + timedelta(days=30))
        after = self.router.connection.execute(
            "SELECT profile_json FROM model_endpoint_profile_versions WHERE profile_id=? "
            "ORDER BY rowid DESC LIMIT 1", (ASTRA,)).fetchone()
        old, new = json.loads(before["profile_json"]), json.loads(after["profile_json"])
        self.assertEqual(old["capabilities"], new["capabilities"])
        self.assertEqual(old["cost"], new["cost"])
        self.assertEqual(new["version"], old["version"] + 1)
        self.assertEqual(new["prior_version_ref"], old["profile_version_ref"])
        self.assertGreater(new["availability"]["valid_until"], old["availability"]["valid_until"])


class PlannerBudgetConfigTests(unittest.TestCase):
    """C2b: the writer finds the planner's day ledger where the installer put it.

    There is no ``--planner-budget-db`` flag, on purpose.  The writer's argv is
    built by the launch agent, and a second copy of the budget wiring would be
    a second thing to repoint when the owner raises a cap -- and the one that
    was forgotten would be this one.  ``research_planner_setup.install`` already
    writes ``budget_db`` and ``budget_policy_ref`` into the model configuration
    beside the Core, and a cap raise already repoints that file, so the writer
    reads it from there.  The owner step is therefore "re-run install.sh", not
    "edit a service file".
    """

    def _install(self, root: Path) -> Path:
        from tests.test_document_extraction_setup import _service

        config_path = _service(root)
        service = json.loads(config_path.read_text(encoding="utf-8"))
        with ModelRouter(service["model_router_db"]) as router:
            for profile in openclaw_broker_profiles(checked_at=NOW):
                router.register_profile(profile)
        install(config_path, tier="cheap", now=NOW)
        # resolve(): the installer records resolved paths, and on macOS the
        # temporary directory is reached through a symlink.
        return Path(json.loads(
            config_path.read_text(encoding="utf-8"))["core_db"]).resolve().parent

    def test_setup_does_not_resurrect_the_static_catalog(self) -> None:
        from tests.test_document_extraction_setup import _service

        with tempfile.TemporaryDirectory() as directory:
            config_path = _service(Path(directory))
            service = json.loads(config_path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(PlannerSetupError, "not registered"):
                install(config_path, profile_ids=[ASTRA], now=NOW)
            with ModelRouter(service["model_router_db"]) as router:
                count = router.connection.execute(
                    "SELECT COUNT(*) FROM model_endpoint_profile_versions"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_what_the_installer_writes_is_what_the_writer_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = self._install(root)
            found = planner_budget_config(state_dir)
            self.assertEqual(found, {
                "budget_db": str(state_dir / "budget.sqlite"),
                "budget_policy_ref":
                    "thesis-impact-day-budget-policy:production:1",
                "provider_retry": {
                    "max_same_profile_retries": 1,
                    "retry_backoff_seconds": 2,
                },
                "transport_retry": {
                    "max_definitely_not_sent_retries": 1,
                    "queue_wait_seconds": 600,
                    "retry_backoff_seconds": 2,
                },
            })
            # The very same two keys the installed configuration carries: one
            # file, one repoint, and no way for them to disagree.
            installed = json.loads(
                (state_dir / CONFIG_FILE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(installed["budget_db"], found["budget_db"])
            self.assertEqual(
                installed["budget_policy_ref"], found["budget_policy_ref"])
            self.assertEqual(installed["provider_retry"], found["provider_retry"])
            self.assertEqual(installed["transport_retry"], found["transport_retry"])

    def test_setup_preserves_existing_owner_execution_policies(self) -> None:
        from tests.test_document_extraction_setup import _service

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _service(root)
            service = json.loads(config_path.read_text(encoding="utf-8"))
            with ModelRouter(service["model_router_db"]) as router:
                for profile_wire in openclaw_broker_profiles(checked_at=NOW):
                    router.register_profile(profile_wire)
            install(config_path, tier="cheap", now=NOW)
            state_dir = Path(service["core_db"]).resolve().parent
            target = state_dir / CONFIG_FILE_NAME
            configured = json.loads(target.read_text(encoding="utf-8"))
            configured["provider_retry"] = {
                "max_same_profile_retries": 4,
                "retry_backoff_seconds": 17,
            }
            configured["transport_retry"] = {
                "max_definitely_not_sent_retries": 3,
                "queue_wait_seconds": 901,
                "retry_backoff_seconds": 19,
            }
            configured["structured_output_repair"] = {"max_attempts": 37}
            configured["model_spec_numeric_context"] = {
                "max_periods_per_series": 5,
                "max_total_cells": 123,
            }
            target.write_text(json.dumps(configured), encoding="utf-8")

            install(config_path, tier="cheap", now=NOW)

            preserved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                preserved["provider_retry"], configured["provider_retry"]
            )
            self.assertEqual(
                preserved["transport_retry"], configured["transport_retry"]
            )
            self.assertEqual(
                preserved["structured_output_repair"],
                configured["structured_output_repair"],
            )
            self.assertEqual(
                preserved["model_spec_numeric_context"],
                configured["model_spec_numeric_context"],
            )

    def test_no_configuration_at_all_is_todays_behaviour(self) -> None:
        # An install that has not re-run the planner setup keeps working. The
        # writer reports "unbudgeted" in the op result rather than implying a
        # ledger saw the call.
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(planner_budget_config(Path(directory)), {})

    def test_a_pre_c2b_configuration_file_binds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / CONFIG_FILE_NAME).write_text(json.dumps({
                "routing_policy_ref": "model-routing-policy-version:x:1",
                "broker_client_id": "client:dalton-core",
            }), encoding="utf-8")
            self.assertEqual(planner_budget_config(root), {})

    def test_a_relative_ledger_path_is_not_a_ledger(self) -> None:
        # The writer's working directory is not part of any contract, so a
        # path it would have to interpret is refused rather than guessed at.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / CONFIG_FILE_NAME).write_text(json.dumps({
                "budget_db": "state/budget.sqlite",
                "budget_policy_ref": "thesis-impact-day-budget-policy:x:1",
            }), encoding="utf-8")
            self.assertEqual(planner_budget_config(root), {})

    def test_an_unreadable_configuration_is_not_a_writer_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / CONFIG_FILE_NAME).write_text("{not json", encoding="utf-8")
            self.assertEqual(planner_budget_config(root), {})
