"""Adversarial tests for the CoverageMission authority and stage ledger."""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionNotFound,
    CoverageMissionValidationError,
    validate_coverage_mission_version,
    validate_mission_stage_claim,
    validate_mission_stage_record,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import (
    INDUSTRY,
    OWNER,
    ROOT,
    bootstrap_method_authorities,
    mission_params,
    playbook_params,
)


ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
AUTOMATION = "automation:coverage-mission"


class CoverageMissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)

    def create(self, **overrides):
        params = mission_params(self.state)
        params.update(overrides)
        ref = params.pop("mission_ref")
        return self.authority.create_mission(ref, **params)

    def stage(self, mission, company, stage_ref, status, *, actor=AUTOMATION, evidence=(), key=None, rationale="r"):
        return self.authority.record_stage(
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
            company_ref=company,
            stage_ref=stage_ref,
            status=status,
            evidence_refs=list(evidence),
            rationale=rationale,
            actor_ref=actor,
            idempotency_key=key or f"{company}:{stage_ref}:{status}:{actor}",
        )

    def test_manifest_creates_mission_and_replays(self) -> None:
        mission = self.create()
        self.assertEqual(mission["status"], "fresh")
        self.assertEqual(mission["version"], 1)
        self.assertEqual(len(mission["universe"]), 5)
        self.assertEqual(mission["bindings"]["playbook_version"]["ref"], self.state["playbook"]["id"])
        replay = self.create()
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(self.authority.active_mission(mission["mission_ref"])["content_hash"], mission["content_hash"])
        self.assertEqual(self.authority.mission(mission["id"])["id"], mission["id"])
        statuses = {item["status"] for item in mission["source_plan"]}
        self.assertEqual(statuses, {"connected", "probe_only", "not_connected"})

    def test_json_contracts_match_record_shapes(self) -> None:
        mission = self.create()
        record = dict(mission)
        record.pop("status")
        schema = json.loads((ROOT / "contracts/coverage-mission-version.schema.json").read_text())
        self.assertEqual(set(schema["required"]), set(record))
        validate_coverage_mission_version(record)
        stage = self.stage(mission, ACN, "initial_screen", "entered")
        stage_record = dict(stage)
        stage_record.pop("status_marker")
        schema = json.loads((ROOT / "contracts/coverage-mission-stage-record.schema.json").read_text())
        self.assertEqual(set(schema["required"]), set(stage_record))
        validate_mission_stage_record(stage_record)

    def test_bindings_must_be_exact_and_active(self) -> None:
        params = mission_params(self.state)
        params["bindings"]["playbook_version"]["hash"] = "0" * 64
        with self.assertRaises(CoverageMissionConflict):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(self.state)
        params["bindings"]["constitution_version"]["ref"] = "constitution-version:missing"
        with self.assertRaises(CoverageMissionNotFound):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(self.state)
        params["industry_ref"] = "industry:eu-chemicals"
        with self.assertRaises(CoverageMissionConflict):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        # Supersede the playbook; the old exact binding is no longer active.
        newer = playbook_params()
        newer.update({
            "version_id": "research-playbook-version:team-analyst-manual:2",
            "idempotency_key": "research-playbook:team-analyst-manual:2",
            "prior_version_ref": self.state["playbook"]["id"],
            "title": "v2",
        })
        self.state["playbook_authority"].publish_playbook(newer.pop("playbook_ref"), **newer)
        with self.assertRaises(CoverageMissionConflict):
            self.create()

    def test_autonomy_cannot_escape_human_only_objects(self) -> None:
        params = mission_params(self.state)
        params["autonomy"]["may_write"].append("thesis")
        with self.assertRaises(CoverageMissionValidationError):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(self.state)
        params["autonomy"]["automation_principal"] = "human:someone"
        with self.assertRaises(CoverageMissionValidationError):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(self.state)
        params["autonomy"]["human_checkpoints"].remove("thesis_admission")
        with self.assertRaises(CoverageMissionValidationError):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(self.state)
        params["autonomy"]["human_checkpoints"].remove("deep_insight_gate")
        with self.assertRaises(CoverageMissionConflict):
            self.authority.create_mission(params.pop("mission_ref"), **params)
        for actor in ("automation:coverage-mission", "core", "system:planner"):
            with self.subTest(actor=actor):
                with self.assertRaises(CoverageMissionValidationError):
                    self.create(actor_ref=actor)

    def test_stage_ledger_enforces_order_evidence_and_human_gates(self) -> None:
        mission = self.create()
        entered = self.stage(mission, ACN, "initial_screen", "entered")
        self.assertEqual(entered["status_marker"], "fresh")
        self.assertEqual(self.stage(mission, ACN, "initial_screen", "entered")["status_marker"], "duplicate")
        with self.assertRaises(CoverageMissionConflict):
            self.stage(mission, ACN, "initial_screen", "entered", key="again")
        with self.assertRaises(CoverageMissionValidationError):
            self.stage(mission, ACN, "initial_screen", "gate_passed")
        with self.assertRaises(CoverageMissionConflict):
            self.stage(mission, ACN, "deep_insight_gate", "entered")
        failed = self.stage(mission, ACN, "initial_screen", "gate_failed", rationale="missing transcripts")
        self.assertEqual(failed["status"], "gate_failed")
        passed = self.stage(
            mission, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v1"],
        )
        self.assertEqual(passed["status_marker"], "fresh")
        with self.assertRaises(CoverageMissionConflict):
            self.stage(
                mission, ACN, "initial_screen", "gate_passed",
                evidence=["artifact-version:acn-initial-screen-v2"], key="second-pass",
            )
        self.stage(mission, ACN, "deep_insight_gate", "entered")
        with self.assertRaises(CoverageMissionConflict):
            self.stage(
                mission, ACN, "deep_insight_gate", "gate_passed",
                evidence=["artifact-version:acn-deep-insights-v1"],
            )
        human_pass = self.stage(
            mission, ACN, "deep_insight_gate", "gate_passed",
            actor=OWNER, evidence=["artifact-version:acn-deep-insights-v1"],
        )
        self.assertEqual(human_pass["actor_ref"], OWNER)
        with self.assertRaises(CoverageMissionConflict):
            self.stage(mission, CTSH, "initial_screen", "entered", actor="automation:someone-else")
        with self.assertRaises(CoverageMissionConflict):
            self.stage(mission, "company:sec-cik:0000000000", "initial_screen", "entered")
        with self.assertRaises(CoverageMissionValidationError):
            self.stage(mission, CTSH, "initial_screen", "entered", actor="core")
        with self.assertRaises(CoverageMissionConflict):
            self.authority.record_stage(
                mission_version_ref=mission["id"], mission_version_hash="0" * 64,
                company_ref=CTSH, stage_ref="initial_screen", status="entered",
                evidence_refs=[], rationale="r", actor_ref=AUTOMATION, idempotency_key="bad-hash",
            )
        progress = self.authority.mission_progress(mission["mission_ref"])
        by_company = {item["company_ref"]: item for item in progress["companies"]}
        self.assertEqual(by_company[ACN]["current_stage"], "deep_insight_gate")
        self.assertEqual(by_company[ACN]["current_status"], "gate_passed")
        self.assertEqual(by_company[ACN]["next_stage"], "industry_model")
        self.assertEqual(by_company[ACN]["completed_stages"], ["initial_screen", "deep_insight_gate"])
        self.assertEqual(by_company[ACN]["record_count"], 5)
        self.assertIsNone(by_company[CTSH]["current_stage"])
        self.assertEqual(by_company[CTSH]["next_stage"], "initial_screen")
        records = self.authority.stage_records(mission["id"], ACN)
        self.assertEqual([r["status"] for r in records], ["entered", "gate_failed", "gate_passed", "entered", "gate_passed"])

    def test_stage_records_bind_only_the_active_mission_version(self) -> None:
        first = self.create()
        second = self.create(
            version_id="coverage-mission-version:us-it-services:2",
            idempotency_key="coverage-mission:us-it-services:2",
            prior_version_ref=first["id"],
            title="v2",
        )
        self.assertEqual(second["version"], 2)
        with self.assertRaises(CoverageMissionConflict):
            self.stage(first, ACN, "initial_screen", "entered")
        self.assertEqual(self.stage(second, ACN, "initial_screen", "entered")["status_marker"], "fresh")

    def test_sec_lane_authorization_and_stage_claim_ledger(self) -> None:
        mission = self.create()
        authorization = self.authority.sec_lane_authorization_for_company(ACN)
        self.assertEqual(authorization["ticker"], "ACN")
        self.assertEqual(authorization["paid_calls_reserved"], 0)
        self.assertEqual(authorization["cost_usd_reserved"], 0.0)
        dispatch = self.authority.queue_sec_dispatch(
            authorization=authorization, form="10-Q", filed_from="2026-01-01",
            filed_to="2026-09-02", expected_accession="0001467373-26-000031",
            observation_ref="research-outcome:acn-2026q3",
        )
        self.assertEqual(dispatch["status_marker"], "fresh")
        self.assertEqual(len(self.authority.pending_sec_dispatches()), 1)
        duplicate_dispatch = self.authority.queue_sec_dispatch(
            authorization=authorization, form="10-Q", filed_from="2026-01-01",
            filed_to="2026-09-02", expected_accession="0001467373-26-000031",
            observation_ref="research-outcome:acn-2026q3",
        )
        self.assertEqual(duplicate_dispatch["status_marker"], "duplicate")
        launched = self.authority.mark_sec_dispatch_launched(
            dispatch["dispatch_id"], "sec-lane-run:" + "1" * 24
        )
        self.assertEqual(launched["status"], "launched")
        self.assertEqual(self.authority.pending_sec_dispatches(), [])
        self.assertEqual(
            self.authority.mark_sec_dispatch_launched(
                dispatch["dispatch_id"], "sec-lane-run:" + "1" * 24
            )["status_marker"],
            "duplicate",
        )
        with self.assertRaises(CoverageMissionConflict):
            self.authority.authorize_sec_lane(
                company_ref=ACN, ticker="ACN", actor_ref="automation:someone-else",
                mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            )

        evidence = self.store.register_evidence({
            "evidence_ref": "evidence:acn:sec:2026q3",
            "source_type": "filing",
            "source_ref": "sec:accession:0001467373-26-000031",
            "retrieved_at": "2026-09-02T00:00:00+00:00",
            "source_lineage": ["sec:accession:0001467373-26-000031"],
            "independence_group": "sec:0001467373-26-000031",
            "actor_ref": "automation:coverage-mission",
        })
        invocation = {
            "schema_version": "0.1", "id": "invocation:mission-sec-test",
            "created_at": "2026-09-02T00:00:00+00:00", "work_order_ref": "work:sec-test",
            "profile_ref": "profile:sec-test", "granularity": "task", "capability": "research",
            "provider": "deterministic", "model": "none", "model_family": "none",
            "runtime_ref": "runtime:sec-test", "actor_ref": "automation:coverage-mission",
            "usage": {"tokens": 0}, "input_refs": [], "output_refs": [],
            "started_at": "2026-09-02T00:00:00+00:00", "completed_at": None,
            "side_effects": ["read:public-http"], "parent_ref": None,
        }
        self.store.register_invocation(invocation)
        claim = self.store.register_claim({
            "claim_ref": "claim:acn:revenue-growth:2026q3", "subject_ref": ACN,
            "metric_or_aspect": "reported-quarterly-revenue-growth", "period": "2026Q3",
            "basis": "reported", "normalized_statement": "ACN revenue grew year over year.",
            "claim_kind": "quantitative", "value": 7.0, "unit": "percent",
            "producer_invocation_refs": [invocation["id"]],
            "actor_ref": "automation:coverage-mission",
        })
        recorded = self.authority.record_stage_claim(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=ACN, ticker="ACN", claim_version_ref=claim["claim_version_id"],
            claim_version_hash=claim["content_hash"],
            evidence_version_ref=evidence["evidence_version_id"],
            evidence_version_hash=evidence["content_hash"],
            source_location="sec:accession:0001467373-26-000031",
            actor_ref=AUTOMATION,
        )
        self.assertEqual(recorded["status"], "fresh")
        self.assertEqual(recorded["stage_ref"], "initial_screen")
        contract = json.loads(
            (ROOT / "contracts/coverage-mission-stage-claim.schema.json").read_text()
        )
        wire = dict(recorded)
        wire.pop("status")
        self.assertEqual(set(contract["required"]), set(wire))
        validate_mission_stage_claim(wire)
        replay = self.authority.record_stage_claim(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=ACN, ticker="ACN", claim_version_ref=claim["claim_version_id"],
            claim_version_hash=claim["content_hash"],
            evidence_version_ref=evidence["evidence_version_id"],
            evidence_version_hash=evidence["content_hash"],
            source_location="sec:accession:0001467373-26-000031",
            actor_ref=AUTOMATION,
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(len(self.authority.stage_claims(mission["id"], ACN)), 1)
        progress = self.authority.mission_progress(mission["mission_ref"])
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual(acn["current_stage"], "initial_screen")
        self.assertEqual(acn["claim_count"], 1)

    def test_schema_triggers_block_direct_writes(self) -> None:
        mission = self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO coverage_mission_stage_records(record_id,mission_version_ref,company_ref,stage_ref,"
                "status,actor_ref,record_json,content_hash,created_at) VALUES('x',?,?,'initial_screen',"
                "'gate_passed','automation:x','{}','h','t')",
                (mission["id"], ACN),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM coverage_mission_versions")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE coverage_mission_pointer SET content_hash='x' WHERE mission_ref=?",
                (mission["mission_ref"],),
            )


if __name__ == "__main__":
    unittest.main()
