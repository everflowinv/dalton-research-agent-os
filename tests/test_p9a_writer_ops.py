"""Writer ops for P9a: playbook publish/read and coverage mission ops."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    WriterServer,
)
from tests.p9a_fixtures import (
    INDUSTRY,
    OWNER,
    constitution_method,
    load_mission_manifest,
    playbook_params,
)


GOVERNANCE_TOKEN = "governance-test-token"
CORE_TOKEN = "core-test-token"
WORKER_TOKEN = "worker-test-token"
AUTOMATION_TOKEN = "automation-token"
ACN = "company:sec-cik:0001467373"


class P9aWriterHarness:
    def __init__(self, root: Path):
        self.socket = str(root / "writer.sock")
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal(
                "coverage-governance", GOVERNANCE_TOKEN, HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref=OWNER,
            ),
            "worker": Principal(
                "worker", WORKER_TOKEN, frozenset({"stage_change", "propose_model_input"}),
            ),
            "mission-automation": Principal(
                "mission-automation", AUTOMATION_TOKEN,
                frozenset({
                    "record_mission_stage", "coverage_mission_progress",
                    "get_active_coverage_mission", "publish_research_playbook",
                    "create_coverage_mission",
                }),
                actor_ref="automation:coverage-mission",
            ),
        }
        self.server = WriterServer(root / "core.sqlite", self.socket, principals)
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.governance = WriterClient(self.socket, GOVERNANCE_TOKEN)
        self.core = WriterClient(self.socket, CORE_TOKEN)
        self.worker = WriterClient(self.socket, WORKER_TOKEN)
        self.automation = WriterClient(self.socket, AUTOMATION_TOKEN)

    def bootstrap(self) -> dict:
        mandate = self.governance.call("create_mandate", {
            "mandate_ref": "mandate:us-it-services", "actor_ref": OWNER,
            "objective": "Establish US IT Services coverage.",
            "scope_refs": [INDUSTRY], "constraints": {}, "success_criteria": {},
            "effective_from": "2026-08-23T00:00:00+00:00", "effective_until": None,
        })
        pack = self.governance.call("register_driver_pack", {
            "driver_pack_ref": "driver-pack:us-it-services", "actor_ref": OWNER,
            "industry_ref": INDUSTRY, "title": "US IT Services Driver Pack",
            "drivers": [{
                "driver_ref": "driver:d", "label": "D", "mechanism": "m",
                "metric_refs": ["metric:x"],
            }],
            "metric_specs": [{
                "metric_ref": "metric:x", "label": "X", "definition": "d", "unit": "USD",
                "periodicity": "quarterly", "preferred_source_refs": ["source:sec-edgar"],
                "verification_kind": "numeric", "caveats": [],
            }],
            "thesis_templates": [{
                "template_ref": "template:x", "statement": "s", "mechanism": "m",
                "driver_refs": ["driver:d"], "implied_expectation": "e",
                "falsifier_refs": ["falsifier:x"],
            }],
            "version_id": "driver-pack-version:us-it-services:1", "prior_version_ref": None,
            "idempotency_key": "driver-pack:us-it-services:1",
        })
        policy = self.core.call("active_policy", {})
        constitution = self.governance.call("publish_research_constitution", {
            "constitution_ref": "constitution:us-it-services",
            "industry_ref": INDUSTRY,
            "title": "US IT Services Research Constitution v1",
            "bindings": {
                "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
                "driver_pack_version": {"ref": pack["id"], "hash": pack["content_hash"]},
                "governance_policy_version": {
                    "ref": policy["policy_version_id"], "hash": policy["content_hash"],
                },
                "doctrine_pack_version": None,
                "weekly_brief_plan": None,
            },
            "method": constitution_method(),
            "actor_ref": OWNER,
            "version_id": "constitution-version:us-it-services:1",
            "prior_version_ref": None,
            "idempotency_key": "constitution:us-it-services:1",
        })
        playbook = self.governance.call("publish_research_playbook", playbook_params())
        return {"mandate": mandate, "pack": pack, "constitution": constitution, "playbook": playbook}

    def mission_params(self, state: dict) -> dict:
        params = load_mission_manifest()
        for key in ("playbook_ref", "constitution_ref", "mandate_ref"):
            params.pop(key)
        params["bindings"] = {
            "playbook_version": {"ref": state["playbook"]["id"], "hash": state["playbook"]["content_hash"]},
            "constitution_version": {
                "ref": state["constitution"]["id"], "hash": state["constitution"]["content_hash"],
            },
            "mandate_version": {"ref": state["mandate"]["id"], "hash": state["mandate"]["content_hash"]},
        }
        params["actor_ref"] = OWNER
        return params

    def close(self) -> None:
        self.server.stop()
        self.thread.join(timeout=10)


class P9aWriterOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.h = P9aWriterHarness(Path(root.name))
        self.addCleanup(self.h.close)

    def test_playbook_and_mission_ops_are_human_governed(self) -> None:
        state = self.h.bootstrap()
        self.assertEqual(state["playbook"]["status"], "fresh")
        self.assertEqual(state["playbook"]["actor_ref"], OWNER)
        replay = self.h.governance.call("publish_research_playbook", playbook_params())
        self.assertEqual(replay["status"], "duplicate")
        active = self.h.governance.call(
            "get_active_research_playbook", {"playbook_ref": state["playbook"]["playbook_ref"]}
        )
        self.assertEqual(active["content_hash"], state["playbook"]["content_hash"])
        report = self.h.governance.call("research_playbook_report", {})
        self.assertEqual(report["playbook_count"], 1)

        # Non-human principals cannot publish method authorities even when the
        # operation is in their allow-list: the server binds actor_ref to the
        # authenticated principal and the authority rejects non-human actors.
        with self.assertRaises(RemoteAuthorizationError):
            self.h.worker.call("publish_research_playbook", playbook_params())
        with self.assertRaises(RemoteError):
            self.h.automation.call("publish_research_playbook", playbook_params())
        spoof = playbook_params(actor_ref="human:someone-else")
        with self.assertRaises(RemoteAuthorizationError):
            self.h.governance.call("publish_research_playbook", spoof)

        mission = self.h.governance.call("create_coverage_mission", self.h.mission_params(state))
        self.assertEqual(mission["status"], "fresh")
        self.assertEqual(mission["actor_ref"], OWNER)
        with self.assertRaises(RemoteError):
            self.h.automation.call("create_coverage_mission", self.h.mission_params(state))
        fetched = self.h.governance.call("get_coverage_mission", {"version_id": mission["id"]})
        self.assertEqual(fetched["content_hash"], mission["content_hash"])
        active_mission = self.h.governance.call(
            "get_active_coverage_mission", {"mission_ref": mission["mission_ref"]}
        )
        self.assertEqual(active_mission["id"], mission["id"])

        # The mission's automation principal may record non-checkpoint stages
        # but cannot pass a human-checkpoint gate.
        entered = self.h.automation.call("record_mission_stage", {
            "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
            "company_ref": ACN, "stage_ref": "initial_screen", "status": "entered",
            "evidence_refs": [], "rationale": "start", "idempotency_key": "acn:is:entered",
        })
        self.assertEqual(entered["actor_ref"], "automation:coverage-mission")
        self.assertEqual(entered["status_marker"], "fresh")
        passed = self.h.automation.call("record_mission_stage", {
            "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
            "company_ref": ACN, "stage_ref": "initial_screen", "status": "gate_passed",
            "evidence_refs": ["artifact-version:acn-initial-screen-v1"], "rationale": "done",
            "idempotency_key": "acn:is:passed",
        })
        self.assertEqual(passed["status"], "gate_passed")
        self.h.automation.call("record_mission_stage", {
            "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
            "company_ref": ACN, "stage_ref": "deep_insight_gate", "status": "entered",
            "evidence_refs": [], "rationale": "start gate", "idempotency_key": "acn:dig:entered",
        })
        with self.assertRaises(RemoteError):
            self.h.automation.call("record_mission_stage", {
                "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
                "company_ref": ACN, "stage_ref": "deep_insight_gate", "status": "gate_passed",
                "evidence_refs": ["artifact-version:acn-deep-insights-v1"], "rationale": "auto",
                "idempotency_key": "acn:dig:auto-pass",
            })
        human_pass = self.h.governance.call("record_mission_stage", {
            "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
            "company_ref": ACN, "stage_ref": "deep_insight_gate", "status": "gate_passed",
            "evidence_refs": ["artifact-version:acn-deep-insights-v1"], "rationale": "reviewed",
            "idempotency_key": "acn:dig:human-pass",
        })
        self.assertEqual(human_pass["actor_ref"], OWNER)
        progress = self.h.automation.call(
            "coverage_mission_progress", {"mission_ref": mission["mission_ref"]}
        )
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual(acn["next_stage"], "industry_model")
        records = self.h.governance.call(
            "coverage_mission_stage_records", {"mission_version_ref": mission["id"]}
        )
        self.assertEqual(len(records["records"]), 4)
        with self.assertRaises(RemoteAuthorizationError):
            self.h.worker.call("coverage_mission_progress", {"mission_ref": mission["mission_ref"]})
        with self.assertRaises(RemoteError):
            self.h.governance.call("get_coverage_mission", {"version_id": "coverage-mission-version:missing"})


if __name__ == "__main__":
    unittest.main()
