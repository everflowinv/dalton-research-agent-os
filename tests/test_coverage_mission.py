"""Adversarial tests for the CoverageMission authority and stage ledger."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionNotFound,
    CoverageMissionValidationError,
    fold_stage_status,
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


class MissionHarness(unittest.TestCase):
    """A Core with the method authorities bootstrapped and one mission to publish."""

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


class CoverageMissionTests(MissionHarness):
    def test_signed_mission_can_publish_explicit_daily_pool_caps(self) -> None:
        from dalton_core.budget_pools import pool_caps
        from dalton_core.event_judgement import pool as event_pool

        params = mission_params(self.state)
        daily = params["budget"]["max_daily_cost_usd"]
        params["budget"]["pools"] = {
            "coverage": daily * 0.5,
            "event_response": daily * 0.2,
            "adhoc": daily * 0.2,
            "maintenance": daily * 0.1,
        }
        mission = self.authority.create_mission(
            params.pop("mission_ref"), **params
        )
        caps = pool_caps(mission["budget"])
        self.assertFalse(caps["defaulted"])
        self.assertEqual(
            event_pool(mission)["cap_micros"], caps["caps_micros"]["event_response"]
        )

    def test_signed_mission_can_bound_alphaengine_probes_below_its_total_cap(self) -> None:
        params = mission_params(self.state)
        total = params["budget"]["max_alphaengine_calls_24h"]
        params["budget"]["max_alphaengine_probe_calls_24h"] = total - 1
        mission = self.authority.create_mission(params.pop("mission_ref"), **params)
        self.assertEqual(
            mission["budget"]["max_alphaengine_probe_calls_24h"], total - 1
        )

        params = mission_params(self.state)
        params["budget"]["max_alphaengine_probe_calls_24h"] = (
            params["budget"]["max_alphaengine_calls_24h"] + 1
        )
        with self.assertRaisesRegex(CoverageMissionValidationError, "cannot exceed"):
            self.authority.create_mission(params.pop("mission_ref"), **params)

    def test_explicit_pool_caps_are_closed_finite_and_within_the_daily_cap(self) -> None:
        valid = {"coverage": 1, "event_response": 1, "adhoc": 1, "maintenance": 1}
        cases = [
            {key: value for key, value in valid.items() if key != "maintenance"},
            {**valid, "unknown": 0},
            {**valid, "coverage": float("nan")},
            {**valid, "coverage": float("inf")},
            {**valid, "coverage": -1},
            {**valid, "coverage": "1"},
            {**valid, "coverage": True},
            {**valid, "coverage": 1_000_000},
        ]
        for pools in cases:
            with self.subTest(pools=pools):
                params = mission_params(self.state)
                params["version_id"] += ":" + str(len(pools))
                params["idempotency_key"] += ":" + str(len(pools))
                params["budget"]["pools"] = pools
                with self.assertRaises(CoverageMissionValidationError):
                    self.authority.create_mission(
                        params.pop("mission_ref"), **params
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
        self.assertNotIn("pools", mission["budget"])

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


class StageLadderAcrossVersionsTests(MissionHarness):
    """P14-S: a stage state is a fact about (mission_ref, company).

    The live mission rolled v7 -> v13 in two days and each roll left the
    ladder looking empty: every version carries its own five ``entered`` rows
    and only v13 carries the four ``gate_passed``.  These pin the rule that
    the version is provenance and the state carries forward.
    """

    def roll(self, prior, number: int):
        """Publish the next version of the same mission and return it."""

        return self.create(
            version_id=f"coverage-mission-version:us-it-services:{number}",
            idempotency_key=f"coverage-mission:us-it-services:{number}",
            prior_version_ref=prior["id"],
            title=f"v{number}",
        )

    def test_the_fold_rules_are_last_decision_wins_and_entered_never_supersedes(self) -> None:
        self.assertIsNone(fold_stage_status([]))
        self.assertEqual(fold_stage_status(["entered"]), "entered")
        self.assertEqual(fold_stage_status(["entered", "gate_failed"]), "gate_failed")
        self.assertEqual(
            fold_stage_status(["entered", "gate_failed", "gate_passed"]), "gate_passed"
        )
        # A reopen: the later gate_failed supersedes the earlier gate_passed.
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", "gate_failed"]), "gate_failed"
        )
        # A new version re-seeding ``entered`` cannot walk a decision back.
        self.assertEqual(
            fold_stage_status(["entered", "gate_passed", "entered"]), "gate_passed"
        )

    def test_a_gate_passed_two_versions_ago_still_opens_the_next_stage(self) -> None:
        # P12d's shape, end to end: the screen passes under v1, the owner rolls
        # the mission twice, and the deep-insight decision is taken under v3.
        # Before P14-S the enter was refused with "deep_insight_gate cannot be
        # entered before initial_screen gate_passed" -- on a company that had
        # demonstrably passed it -- and the decision died with it.
        first = self.create()
        self.stage(first, ACN, "initial_screen", "entered")
        self.stage(
            first, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v1"],
        )
        second = self.roll(first, 2)
        third = self.roll(second, 3)

        def decide_deep_insight_gate(mission, *, company_ref, passed, evidence):
            """A stand-in for P12d: enter the stage, then a person decides it."""

            entered = self.authority.record_stage(
                mission_version_ref=mission["id"],
                mission_version_hash=mission["content_hash"],
                company_ref=company_ref, stage_ref="deep_insight_gate", status="entered",
                evidence_refs=[mission["id"]], rationale="deep insight work started",
                actor_ref=AUTOMATION,
                idempotency_key=f"{mission['id']}:{company_ref}:deep_insight_gate:entered",
            )
            decided = self.authority.record_stage(
                mission_version_ref=mission["id"],
                mission_version_hash=mission["content_hash"],
                company_ref=company_ref, stage_ref="deep_insight_gate",
                status="gate_passed" if passed else "gate_failed",
                evidence_refs=list(evidence), rationale="the four questions are answered",
                actor_ref=OWNER,
                idempotency_key=f"{mission['id']}:{company_ref}:deep_insight_gate:decided",
            )
            return entered, decided

        entered, decided = decide_deep_insight_gate(
            third, company_ref=ACN, passed=True,
            evidence=["artifact-version:acn-deep-insights-v1"],
        )
        self.assertEqual(entered["status_marker"], "fresh")
        self.assertEqual(decided["status"], "gate_passed")
        # The record binds the ACTIVE version, which is the provenance.
        self.assertEqual(entered["mission_version_ref"], third["id"])
        self.assertEqual(decided["mission_version_ref"], third["id"])

        state = self.authority.current_stage_state(third["mission_ref"], ACN)
        self.assertEqual(state["current_stage"], "deep_insight_gate")
        self.assertEqual(state["current_status"], "gate_passed")
        self.assertEqual(state["next_stage"], "industry_model")
        self.assertEqual(
            state["completed_stages"], ["initial_screen", "deep_insight_gate"]
        )
        # Provenance per stage: where each fact was written, not where it is read.
        self.assertEqual(
            state["stages"]["initial_screen"]["mission_version_ref"], first["id"]
        )
        self.assertEqual(state["stages"]["initial_screen"]["mission_version_number"], 1)
        self.assertEqual(
            state["stages"]["deep_insight_gate"]["mission_version_ref"], third["id"]
        )
        # ...and the per-version reader still shows only that version's rows.
        self.assertEqual(self.authority.stage_records(second["id"], ACN), [])
        self.assertEqual(
            [r["status"] for r in self.authority.stage_records(first["id"], ACN)],
            ["entered", "gate_passed"],
        )
        progress = self.authority.mission_progress(first["mission_ref"])
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual(acn["current_stage"], "deep_insight_gate")
        self.assertEqual(acn["record_count"], 4)

    def test_a_new_version_resets_nothing_the_company_already_did(self) -> None:
        first = self.create()
        self.stage(first, ACN, "initial_screen", "entered")
        second = self.roll(first, 2)
        # Entering again under the new version is the same fact, and refused.
        with self.assertRaisesRegex(CoverageMissionConflict, "already entered"):
            self.stage(second, ACN, "initial_screen", "entered", key="reseed")
        # The gate can be decided under the new version against the old entry.
        passed = self.stage(
            second, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v1"],
        )
        self.assertEqual(passed["mission_version_ref"], second["id"])
        third = self.roll(second, 3)
        with self.assertRaisesRegex(CoverageMissionConflict, "already passed"):
            self.stage(
                third, ACN, "initial_screen", "gate_passed",
                evidence=["artifact-version:acn-initial-screen-v2"], key="repass",
            )
        self.assertEqual(
            self.authority.current_stage_state(third["mission_ref"], ACN)["current_status"],
            "gate_passed",
        )

    def reopened(self, mission, company_ref: str, stage_ref: str) -> None:
        """A ``gate_failed`` written after a ``gate_passed``, straight into the table.

        ``record_stage`` will not write this one: a pass is terminal to the
        writer, because reopening a gate a company has passed is a human
        checkpoint (``gate_reopen``) and ADR-0008 gives it no automatic writer.
        The *fold* must nonetheless get it right the day that writer exists,
        and two live readers already depend on it -- the reopen lane must not
        offer an already-reopened gate a second time.  So the row is written
        the way the authority would write it, and the reading is tested.
        """

        with self.authority._transaction() as cur:
            # One microsecond after the newest record there is, so it folds
            # after the pass and a later real-clock write still folds after it.
            latest = cur.execute(
                "SELECT MAX(created_at) FROM coverage_mission_stage_records"
            ).fetchone()[0]
            at = (
                datetime.fromisoformat(latest) + timedelta(microseconds=1)
            ).isoformat(timespec="microseconds")
            cur.execute(
                "INSERT INTO coverage_mission_stage_records(record_id,mission_version_ref,"
                "company_ref,stage_ref,status,actor_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f"mission-stage-record:reopen:{company_ref}:{at}", mission["id"], company_ref,
                 stage_ref, "gate_failed", OWNER, '{"rationale":"reopened"}', "e" * 64, at),
            )

    def test_a_later_gate_failed_supersedes_an_earlier_gate_passed(self) -> None:
        first = self.create()
        self.stage(first, ACN, "initial_screen", "entered")
        self.stage(
            first, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v1"],
        )
        second = self.roll(first, 2)
        self.assertEqual(
            self.authority.current_stage_state(second["mission_ref"], ACN)["current_status"],
            "gate_passed",
        )
        self.reopened(second, ACN, "initial_screen")
        state = self.authority.current_stage_state(second["mission_ref"], ACN)
        self.assertEqual(state["current_status"], "gate_failed")
        self.assertEqual(state["completed_stages"], [])
        self.assertEqual(state["next_stage"], "initial_screen")
        # Provenance follows the fact that decided it: the roll, not the pass.
        self.assertEqual(state["stages"]["initial_screen"]["mission_version_ref"], second["id"])
        # A superseded pass does not open the next stage.
        with self.assertRaisesRegex(CoverageMissionConflict, "cannot be entered before"):
            self.stage(second, ACN, "deep_insight_gate", "entered")
        # ...and the stage can be decided again, which is the point of a reopen.
        self.stage(
            second, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v2"], key="repass",
        )
        self.assertEqual(
            self.authority.current_stage_state(second["mission_ref"], ACN)["current_status"],
            "gate_passed",
        )

    def test_companies_at_or_past_crosses_versions_and_stays_monotone(self) -> None:
        first = self.create()
        self.stage(first, ACN, "initial_screen", "entered")
        self.stage(first, CTSH, "initial_screen", "entered")
        self.assertEqual(
            self.authority.companies_at_or_past("initial_screen", first["mission_ref"]),
            sorted([ACN, CTSH]),
        )
        self.assertEqual(
            self.authority.companies_at_or_past("deep_insight_gate", first["mission_ref"]),
            [],
        )
        self.stage(
            first, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v1"],
        )
        second = self.roll(first, 2)
        # Passing the screen is the same fact as being past it, and it crosses
        # the roll: this is P14a's residency rule.
        self.assertEqual(
            self.authority.companies_at_or_past("deep_insight_gate", second["mission_ref"]),
            [ACN],
        )
        # Monotone: a reopen does not un-reach a stage that was reached.
        self.reopened(second, ACN, "initial_screen")
        self.assertEqual(
            self.authority.companies_at_or_past("deep_insight_gate", second["mission_ref"]),
            [ACN],
        )
        # ...which is exactly where it differs from the current state.
        self.assertEqual(
            self.authority.current_stage_state(second["mission_ref"], ACN)["current_status"],
            "gate_failed",
        )

    def test_the_folded_map_carries_every_version_in_time_order(self) -> None:
        first = self.create()
        self.stage(first, ACN, "initial_screen", "entered")
        self.stage(first, ACN, "initial_screen", "gate_failed", rationale="thin")
        second = self.roll(first, 2)
        self.stage(
            second, ACN, "initial_screen", "gate_passed",
            evidence=["artifact-version:acn-initial-screen-v1"],
        )
        state = self.authority.stage_state_by_company(second["mission_ref"])
        self.assertEqual(
            state[ACN]["initial_screen"], ["entered", "gate_failed", "gate_passed"]
        )
        self.assertNotIn(CTSH, state)
        history = self.authority.current_stage_state(
            second["mission_ref"], ACN
        )["stages"]["initial_screen"]["history"]
        self.assertEqual(
            [(item["status"], item["mission_version_number"]) for item in history],
            [("entered", 1), ("gate_failed", 1), ("gate_passed", 2)],
        )

    def test_a_company_with_no_records_reads_as_never_started(self) -> None:
        mission = self.create()
        state = self.authority.current_stage_state(mission["mission_ref"], CTSH)
        self.assertEqual(state["record_count"], 0)
        self.assertEqual(state["stages"], {})
        self.assertIsNone(state["current_stage"])
        self.assertIsNone(state["current_status"])
        self.assertEqual(state["next_stage"], "initial_screen")


if __name__ == "__main__":
    unittest.main()
