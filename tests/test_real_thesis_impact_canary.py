"""Regression tests for the isolated real thesis-impact canary harness."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core.model_deployment import (
    ASSESSMENT_POLICY_REF,
    ASSESSMENT_PROFILE_ID,
    VERIFIER_POLICY_REF,
    VERIFIER_PROFILE_ID,
)
from dalton_core.model_router import ModelRouter


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_real_thesis_impact_canary",
    ROOT / "scripts" / "run_real_thesis_impact_canary.py",
)
assert SPEC is not None and SPEC.loader is not None
CANARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CANARY)


class RealThesisImpactCanaryTests(unittest.TestCase):
    def test_router_install_uses_two_exact_phase_policies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "router.sqlite"
            installed = CANARY._install_router(path)
            self.assertEqual(
                {item["profile"]["id"] for item in installed["profiles"]},
                {ASSESSMENT_PROFILE_ID, VERIFIER_PROFILE_ID},
            )
            self.assertEqual(
                {
                    item["policy"]["policy_version_ref"]
                    for item in installed["policies"]
                },
                {ASSESSMENT_POLICY_REF, VERIFIER_POLICY_REF},
            )
            with ModelRouter(path) as router:
                self.assertEqual(
                    router.get_policy(ASSESSMENT_POLICY_REF)["filters"][
                        "allowed_profile_ids"
                    ],
                    [ASSESSMENT_PROFILE_ID],
                )
                self.assertEqual(
                    router.get_policy(VERIFIER_POLICY_REF)["filters"][
                        "allowed_profile_ids"
                    ],
                    [VERIFIER_PROFILE_ID],
                )

    def test_worker_receives_phase_pins_and_exact_credential_slots(self) -> None:
        fake_router = SimpleNamespace(get_decision=lambda _: None)
        fake_adapter = object()
        fake_worker = object()
        authorities = SimpleNamespace(
            scheduler=object(), impact=object(), observability=object()
        )
        with (
            mock.patch.object(CANARY, "ModelRouter", return_value=fake_router),
            mock.patch.object(
                CANARY, "OpenClawModelAdapter", return_value=fake_adapter
            ),
            mock.patch.object(
                CANARY, "ThesisImpactModelWorker", return_value=fake_worker
            ) as worker_class,
        ):
            router, worker = CANARY._worker(
                authorities=authorities,
                router_path=Path("/tmp/router.sqlite"),
                socket_path=Path("/tmp/broker.sock"),
                auth_key_path=Path("/tmp/broker.key"),
            )
        self.assertIs(router, fake_router)
        self.assertIs(worker, fake_worker)
        kwargs = worker_class.call_args.kwargs
        self.assertEqual(kwargs["routing_policy_ref"], ASSESSMENT_POLICY_REF)
        self.assertEqual(
            kwargs["assessment_routing_policy_ref"], ASSESSMENT_POLICY_REF
        )
        self.assertEqual(kwargs["verifier_routing_policy_ref"], VERIFIER_POLICY_REF)
        self.assertEqual(
            kwargs["credential_slot_refs"],
            (
                "credential-slot:openclaw:openai",
                "credential-slot:openclaw:google",
            ),
        )


if __name__ == "__main__":
    unittest.main()
