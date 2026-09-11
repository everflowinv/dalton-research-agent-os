from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda_coordinator import AgendaCoordinatorConfig
from dalton_core.annual_report_runtime import plan_model_execution
from dalton_core.cockpit_model import build_work
from dalton_core.human_intent import IntentComposerConfig
from dalton_core.model_transport import (
    DEFAULT_BROKER_MAX_FRAME_BYTES,
    LEGACY_BROKER_MAX_FRAME_BYTES,
    ModelTransportConfigError,
    broker_frame_execution_binding,
    resolve_broker_max_frame_bytes,
)
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter


class ModelTransportConfigTests(unittest.TestCase):
    def test_default_and_explicit_frame_bounds_are_closed(self) -> None:
        self.assertEqual(DEFAULT_BROKER_MAX_FRAME_BYTES, 1_048_576)
        self.assertEqual(resolve_broker_max_frame_bytes({}), 1_048_576)
        self.assertEqual(
            resolve_broker_max_frame_bytes({
                "broker_max_frame_bytes": LEGACY_BROKER_MAX_FRAME_BYTES,
            }),
            262_144,
        )
        for invalid in (True, 1023, 1_048_577, 1.0, "1048576"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ModelTransportConfigError):
                    resolve_broker_max_frame_bytes({
                        "broker_max_frame_bytes": invalid,
                    })

    def test_adapter_default_uses_shared_one_mib_frame_bound(self) -> None:
        adapter = OpenClawModelAdapter(
            "/tmp/model-broker.sock",
            route_resolver=lambda _ref: None,
            auth_client_id="client:test",
            auth_key_provider=lambda: b"key",
        )
        self.assertEqual(adapter._max_frame_bytes, DEFAULT_BROKER_MAX_FRAME_BYTES)

    def test_cockpit_work_identity_binds_effective_frame_policy(self) -> None:
        common = {
            "purpose": "plan",
            "request_id": "request:frame-bound",
            "prompt": "plan",
            "mission_version_ref": "coverage-mission-version:test:1",
            "max_input_tokens": 10,
            "max_output_tokens": 10,
            "max_cost_usd": 1.0,
            "max_seconds": 30,
            "created_at": "2026-09-11T17:00:00.000000+00:00",
        }
        current = broker_frame_execution_binding({})
        legacy = broker_frame_execution_binding({
            "broker_max_frame_bytes": LEGACY_BROKER_MAX_FRAME_BYTES,
        })
        current_work = build_work(**common, broker_frame_policy=current)
        legacy_work = build_work(**common, broker_frame_policy=legacy)
        self.assertNotEqual(current_work.id, legacy_work.id)
        self.assertEqual(current_work.metadata["broker_frame_policy"], current)

    def test_annual_execution_binds_default_and_explicit_frame_policy(self) -> None:
        base = {
            "routing_policy_ref": "routing-policy:test:1",
            "credential_slot_refs": ["credential-slot:test"],
            "budget_db": "/tmp/budget.sqlite",
            "budget_policy_ref": "budget-policy:test:1",
        }
        current = plan_model_execution(base, "registered_annual_report_draft")
        legacy = plan_model_execution({
            **base,
            "broker_max_frame_bytes": LEGACY_BROKER_MAX_FRAME_BYTES,
        }, "registered_annual_report_draft")
        self.assertEqual(
            current["broker_frame_policy"]["broker_max_frame_bytes"],
            DEFAULT_BROKER_MAX_FRAME_BYTES,
        )
        self.assertEqual(
            legacy["broker_frame_policy"]["broker_max_frame_bytes"],
            LEGACY_BROKER_MAX_FRAME_BYTES,
        )
        self.assertNotEqual(current["broker_frame_policy"], legacy["broker_frame_policy"])

    def test_intent_and_agenda_configs_use_same_default_and_allow_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            common = {
                "scheduler_db": str(root / "scheduler.sqlite"),
                "model_router_db": str(root / "router.sqlite"),
                "broker_socket": str(root / "broker.sock"),
                "broker_auth_key": str(root / "broker.key"),
                "routing_policy_ref": "routing-policy:test:1",
                "credential_slot_refs": ["credential-slot:test"],
                "broker_client_id": "client:test",
                "expected_agent_id": "agent:model-broker",
                "timeout_seconds": 30,
            }
            intent = IntentComposerConfig.from_mapping({
                **common,
                "staging_path": str(root / "staging.sqlite"),
                "max_input_tokens": 100,
                "max_output_tokens": 100,
                "max_cost_usd": 1.0,
            })
            agenda = AgendaCoordinatorConfig.from_mapping({
                **common,
                "writer_socket": str(root / "writer.sock"),
                "core_token_config": str(root / "principals.json"),
                "perception_source_db": str(root / "perception.sqlite"),
                "perception_snapshot_path": str(root / "snapshot.json"),
                "company_ref": "company:test",
                "broker_max_frame_bytes": LEGACY_BROKER_MAX_FRAME_BYTES,
            })
            self.assertEqual(
                intent.broker_max_frame_bytes, DEFAULT_BROKER_MAX_FRAME_BYTES
            )
            self.assertEqual(
                agenda.broker_max_frame_bytes, LEGACY_BROKER_MAX_FRAME_BYTES
            )


if __name__ == "__main__":
    unittest.main()
