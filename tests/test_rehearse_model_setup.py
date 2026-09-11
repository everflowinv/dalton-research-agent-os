"""Closed regression checks for the optional copied-state model setup."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from dalton_core.annual_report_setup import DEFAULT_PROVIDER_RETRY
from dalton_core.provider_retry import DEFAULT_RETURNED_PROVIDER_RETRY
from dalton_core.research_planner_setup import DEFAULT_PLANNER_TRANSPORT_RETRY
from scripts.rehearse_deploy import (
    REVIEWED_ANNUAL_CONFIGS,
    REVIEWED_DOCUMENT_CONFIG,
    REVIEWED_EXISTING_CONFIGS,
    REVIEWED_MODEL_SETUP_ROLES,
    Rehearsal,
    validate_reviewed_model_setup_transition,
)


def accepted_twelve() -> dict[str, dict[str, object]]:
    result = {}
    for index, name in enumerate(sorted(REVIEWED_EXISTING_CONFIGS), 1):
        result[name] = {
            "routing_policy_ref": f"model-routing-policy-version:test:{index}",
            "credential_slot_refs": ["credential-slot:test"],
            "model_router_db": "/owner/state/model-router.sqlite",
            "broker_socket": "/owner/broker.sock",
            "broker_auth_key": "/owner/broker.key",
            "broker_client_id": "owner-client",
            "expected_agent_id": "owner-agent",
            "budget_db": "/owner/state/budget.sqlite",
            "budget_policy_ref": f"budget-policy:{index}",
            "owner_metadata": {"keep": index},
        }
    # This policy existed before the reviewed transition; extraction setup
    # must preserve it while the eleven generic role setups add theirs.
    result[REVIEWED_DOCUMENT_CONFIG]["transport_retry"] = copy.deepcopy(
        DEFAULT_PLANNER_TRANSPORT_RETRY
    )
    return result


def reviewed_fourteen() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    before = accepted_twelve()
    after = copy.deepcopy(before)
    role_names = {row[0] for row in REVIEWED_MODEL_SETUP_ROLES}
    for name in REVIEWED_EXISTING_CONFIGS:
        after[name]["provider_retry"] = copy.deepcopy(DEFAULT_RETURNED_PROVIDER_RETRY)
        if name in role_names:
            after[name]["transport_retry"] = copy.deepcopy(
                DEFAULT_PLANNER_TRANSPORT_RETRY
            )
    for target, source in (
        ("registered-annual-report-draft-model-config.json", "dossier-model-config.json"),
        (
            "registered-annual-report-verifier-model-config.json",
            "company-dossier-verifier-model-config.json",
        ),
    ):
        config = copy.deepcopy(after[source])
        config["provider_retry"]["unknown_recovery"] = copy.deepcopy(
            DEFAULT_PROVIDER_RETRY["unknown_recovery"]
        )
        after[target] = config
    return before, after


class ReviewedModelSetupTransitionTests(unittest.TestCase):
    def test_exact_reviewed_transition_preserves_old_fields(self) -> None:
        before, after = reviewed_fourteen()
        deltas = validate_reviewed_model_setup_transition(before, after)
        self.assertEqual(set(after), REVIEWED_EXISTING_CONFIGS | REVIEWED_ANNUAL_CONFIGS)
        self.assertEqual(sum(len(fields) for fields in deltas.values()), 23)

    def test_owner_drift_and_non_derived_annual_config_are_refused(self) -> None:
        before, after = reviewed_fourteen()
        with self.subTest("owner budget"):
            drifted = copy.deepcopy(after)
            drifted[REVIEWED_DOCUMENT_CONFIG]["budget_policy_ref"] = "budget-policy:other"
            with self.assertRaisesRegex(RuntimeError, "owner fields"):
                validate_reviewed_model_setup_transition(before, drifted)
        with self.subTest("deleted null owner field"):
            null_before = copy.deepcopy(before)
            null_after = copy.deepcopy(after)
            null_before[REVIEWED_DOCUMENT_CONFIG]["owner_nullable"] = None
            # The value lookup is still None after deletion; presence must be
            # compared as well or this owner-field removal is invisible.
            null_after[REVIEWED_DOCUMENT_CONFIG].pop("owner_nullable", None)
            with self.assertRaisesRegex(RuntimeError, "owner fields"):
                validate_reviewed_model_setup_transition(null_before, null_after)
        with self.subTest("annual derivation"):
            drifted = copy.deepcopy(after)
            annual = next(iter(REVIEWED_ANNUAL_CONFIGS))
            drifted[annual]["provider_retry"]["unknown_recovery"][
                "max_fresh_work_orders"
            ] = 99
            with self.assertRaisesRegex(RuntimeError, "exact reviewed derivation"):
                validate_reviewed_model_setup_transition(before, drifted)

    def test_setup_step_is_explicit_and_runs_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rehearsal = Rehearsal(
                base / "live",
                base / "temp",
                openclaw_config=base / "openclaw.json",
                rehearse_reviewed_model_setup=True,
                log=lambda _message: None,
            )
            order: list[str] = []

            def operation(name: str):
                def run() -> tuple[str, list[str]]:
                    order.append(name)
                    return "", []
                return run

            for attribute in (
                "copy_state", "rewrite_config", "confine_to_temp_root",
                "run_bootstrap", "run_migrations", "run_seeds", "run_catalog_sync",
                "run_reviewed_model_setup", "render_plists", "check_mission",
                "check_lane_switches", "start_writer", "run_tick",
            ):
                setattr(rehearsal, attribute, operation(attribute))
            rehearsal.stop_writer = lambda: None
            self.assertEqual(rehearsal.run(), 0)
            self.assertLess(
                order.index("run_catalog_sync"), order.index("run_reviewed_model_setup")
            )
            self.assertLess(
                order.index("run_reviewed_model_setup"), order.index("render_plists")
            )


if __name__ == "__main__":
    unittest.main()
