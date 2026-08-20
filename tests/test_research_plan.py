"""Adversarial tests for the Planner thin-closure authority."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.observability import ObservabilityStore
from dalton_core.research_plan import (
    PLAN_AUTO_START_RULE_REF,
    ResearchPlanAuthority,
    ResearchPlanConflict,
    ResearchPlanControlPlane,
    ResearchPlanValidationError,
)
from dalton_core.research_question_backlog import (
    ResearchQuestionBacklog,
    ResearchQuestionConflict,
)
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.agenda_fixtures import register_perception


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-15T10:00:00.000000+00:00"
LATER = "2026-09-15T10:00:00.000000+00:00"


def policy() -> dict:
    return {
        "schema_version": "0.1",
        "enabled": True,
        "selected_count": 2,
        "max_model_calls_per_cycle": 1,
        "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5,
        "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000,
        "max_output_tokens": 2000,
        "feature_weights": {
            "mandate_relevance": 4,
            "catalyst_urgency": 3,
            "evidence_staleness": 2,
            "decision_impact": 4,
        },
        "trial_company_refs": ["wanhua"],
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


class InjectedPlannerCrash(RuntimeError):
    pass


class ResearchPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.observability = ObservabilityStore(self.store)
        self.agenda = AgendaStore(self.store)
        self.backlog = ResearchQuestionBacklog(self.store)
        self.plans = ResearchPlanAuthority(self.store)
        self.scheduler = Scheduler(connection=self.store.connection)
        self.control = ResearchPlanControlPlane(
            self.plans,
            self.backlog,
            self.observability,
            self.scheduler,
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)
        self._govern()

    def _govern(self) -> None:
        self.agenda.create_policy(
            policy(),
            effective_from=NOW,
            effective_until=LATER,
            actor_ref="human:owner",
            version_id="agenda-policy-version:1",
            idempotency_key="policy:1",
        )
        self.agenda.create_mandate(
            "mandate:coverage-quality",
            objective="Resolve one decision-useful filing question",
            scope_refs=["wanhua"],
            constraints={"mode": "development_candidate"},
            success_criteria={"human_review_required": True},
            effective_from=NOW,
            effective_until=LATER,
            actor_ref="human:owner",
            version_id="mandate-version:1",
            idempotency_key="mandate:1",
        )
        self.agenda.set_pause(
            False,
            reason="owner approved planner development candidate",
            actor_ref="human:owner",
            version_id="agenda-control-version:2",
            idempotency_key="resume:1",
        )

    def _selected_questions(self, questions: list[tuple[str, str]]) -> tuple[dict, list[dict]]:
        suffix = content_hash(questions)[:12]
        snapshot = register_perception(
            self.agenda, f"perception:plan:{suffix}", company="wanhua"
        )
        cycle = self.agenda.start_cycle(
            f"agenda:plan:{suffix}",
            perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref="mandate-version:1",
            policy_version_ref="agenda-policy-version:1",
            company_ref="wanhua",
            actor_ref="core",
            cycle_id=f"agenda-cycle:plan:{suffix}",
            idempotency_key=f"cycle:plan:{suffix}",
        )
        candidates = []
        for index, (question, criteria) in enumerate(questions):
            candidates.append({
                "candidate_id": f"candidate:plan:{suffix}:{index}",
                "company_ref": "wanhua",
                "question": question,
                "answer_criteria": criteria,
                "features": {
                    "mandate_relevance": 3,
                    "catalyst_urgency": 2,
                    "evidence_staleness": 1,
                    "decision_impact": 3,
                },
                "rationale": "test candidate",
                "source_refs": ["source:sec-edgar"],
            })
        self.agenda.add_candidates(
            cycle["cycle_id"],
            candidates=candidates,
            actor_ref="core",
            idempotency_key=f"candidates:plan:{suffix}",
        )
        decision = self.agenda.decide_cycle(
            cycle["cycle_id"],
            actor_ref="core",
            decision_id=f"decision:plan:{suffix}",
            idempotency_key=f"decision:plan:{suffix}",
        )
        records = []
        for index, (question, criteria) in enumerate(questions):
            record = self.backlog.record_question(
                mandate_version_ref="mandate-version:1",
                company_ref="wanhua",
                question=question,
                answer_criteria=criteria,
                source_refs=["source:sec-edgar"],
                actor_ref="core",
                idempotency_key=f"record:plan:{suffix}:{index}",
            )
            self.backlog.select_question(
                question_ref=record["question_ref"],
                decision_ref=decision["id"],
                actor_ref="core",
                idempotency_key=f"select:plan:{suffix}:{index}",
            )
            records.append(record)
        return decision, records

    def _create_plan(self, *, suffix="one") -> dict:
        decision, records = self._selected_questions(
            [(f"Which SEC filings answer {suffix}?", "Return the official filing list")]
        )
        record = records[0]
        return self.plans.create_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"],
            issuer_cik="320193",
            form="10-Q",
            filing_date_from="2026-01-01",
            filing_date_to="2026-08-15",
            actor_ref="core:planner",
            idempotency_key=f"create-plan:{suffix}",
        )

    def _approve(self, plan: dict, *, decision="accepted", suffix="one") -> dict:
        return self.plans.approve_plan(
            plan_version_ref=plan["plan_version_ref"],
            decision=decision,
            reason="exact plan reviewed",
            actor_ref="human:owner",
            idempotency_key=f"approve-plan:{suffix}",
        )

    def _start(self, plan: dict, *, suffix="one", control=None) -> dict:
        return (control or self.control).start_plan(
            plan_version_ref=plan["plan_version_ref"],
            actor_ref="core:planner",
            idempotency_key=f"start-plan:{suffix}",
        )

    @staticmethod
    def _validate_contract(name: str, wire: dict) -> None:
        schema = json.loads((ROOT / "contracts" / name).read_text(encoding="utf-8"))
        if schema.get("additionalProperties") is False:
            assert set(wire) == set(schema["required"])
        assert wire["schema_version"] == schema["properties"]["schema_version"]["const"]

    def test_create_plan_is_exact_closed_four_step_tree(self) -> None:
        created = self._create_plan()
        wire = self.plans.plan_version(created["plan_version_ref"])
        self.assertEqual(created["question_state"], "planned")
        self.assertEqual(wire["execution_scope"]["parameters"]["issuer_cik"], "0000320193")
        self.assertEqual(
            [step["stage"] for step in wire["execution_scope"]["steps"]],
            ["connector", "authority_resolver", "verifier", "candidate_staging"],
        )
        self.assertEqual(wire["execution_scope"]["declared_side_effects"], ["read:public-http"])
        self.assertEqual(wire["execution_scope"]["auth_mode"], "none")
        self.assertEqual(wire["execution_scope"]["permission_scope"], "public_sec_list_filings")
        for index, step in enumerate(wire["execution_scope"]["steps"]):
            self.assertEqual(step["ordinal"], index + 1)
            self.assertEqual(step["depends_on"], [] if index == 0 else [wire["execution_scope"]["steps"][index - 1]["id"]])
        self._validate_contract("research-plan-version.schema.json", wire)

    def test_plan_binds_the_exact_selected_candidate_not_list_position(self) -> None:
        decision, records = self._selected_questions([
            ("First selected question?", "First answer"),
            ("Second selected question?", "Second answer"),
        ])
        second = records[1]
        created = self.plans.create_plan(
            question_ref=second["question_ref"],
            question_version_ref=second["question_version_ref"],
            decision_ref=decision["id"],
            issuer_cik="789019",
            form="10-K",
            filing_date_from="2025-01-01",
            filing_date_to="2025-12-31",
            actor_ref="core:planner",
            idempotency_key="create-plan:second",
        )
        wire = self.plans.plan_version(created["plan_version_ref"])
        self.assertEqual(wire["agenda_binding"]["candidate_ref"], decision["selected_candidate_refs"][1])

    def test_invalid_sec_scope_and_wrong_question_binding_leave_no_plan(self) -> None:
        decision, records = self._selected_questions([("Bound question?", "Bound answer")])
        record = records[0]
        unselected = self.backlog.record_question(
            mandate_version_ref="mandate-version:1",
            company_ref="wanhua",
            question="Question not selected by this decision?",
            answer_criteria="No candidate exists",
            source_refs=["source:sec-edgar"],
            actor_ref="core",
            idempotency_key="record:unselected-plan-question",
        )
        with self.assertRaises(ResearchPlanConflict):
            self.plans.create_plan(
                question_ref=unselected["question_ref"],
                question_version_ref=unselected["question_version_ref"],
                decision_ref=decision["id"],
                issuer_cik="320193",
                form="10-Q",
                filing_date_from="2026-01-01",
                filing_date_to="2026-08-15",
                actor_ref="core:planner",
            )
        for overrides in (
            {"issuer_cik": "CIK-1"},
            {"form": "6-K"},
            {"filing_date_from": "2026-09-01", "filing_date_to": "2026-08-01"},
            {"filing_date_from": "2020-01-01", "filing_date_to": "2026-08-01"},
        ):
            args = {
                "question_ref": record["question_ref"],
                "question_version_ref": record["question_version_ref"],
                "decision_ref": decision["id"],
                "issuer_cik": "320193",
                "form": "10-Q",
                "filing_date_from": "2026-01-01",
                "filing_date_to": "2026-08-15",
                "actor_ref": "core:planner",
            }
            args.update(overrides)
            with self.assertRaises(ResearchPlanValidationError):
                self.plans.create_plan(**args)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM research_plan_versions").fetchone()[0], 0
        )

    def test_plan_idempotency_and_duplicate_binding_converge(self) -> None:
        created = self._create_plan(suffix="idem")
        replay = self.plans.create_plan(
            question_ref=created["question_ref"],
            question_version_ref=self.backlog.question(created["question_ref"])["head"]["id"],
            decision_ref=self.plans.plan_version(created["plan_version_ref"])["agenda_binding"]["decision_ref"],
            issuer_cik="0000320193",
            form="10-Q",
            filing_date_from="2026-01-01",
            filing_date_to="2026-08-15",
            actor_ref="core:planner",
            idempotency_key="another-key:same-binding",
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["plan_version_ref"], created["plan_version_ref"])
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM research_plan_versions").fetchone()[0], 1
        )

    def test_only_exact_human_can_make_the_terminal_approval(self) -> None:
        created = self._create_plan(suffix="human")
        for actor in ("automation:timeout", "model:planner", "core:auto-accept", "human:"):
            with self.assertRaises(ResearchPlanValidationError):
                self.plans.approve_plan(
                    plan_version_ref=created["plan_version_ref"],
                    decision="accepted",
                    reason="not a human decision",
                    actor_ref=actor,
                )
        accepted = self._approve(created, suffix="human")
        self.assertEqual(accepted["plan_state"], "approved")
        with self.assertRaises(ResearchPlanConflict):
            self.plans.approve_plan(
                plan_version_ref=created["plan_version_ref"],
                decision="rejected",
                reason="second terminal decision",
                actor_ref="human:owner",
            )

    def test_unapproved_or_rejected_plan_cannot_start(self) -> None:
        pending = self._create_plan(suffix="pending")
        with self.assertRaises(ResearchPlanConflict):
            self._start(pending, suffix="pending")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0], 0
        )

        # A second fixture uses a separate selected question in the same Core.
        rejected = self._create_plan(suffix="rejected")
        self._approve(rejected, decision="rejected", suffix="rejected")
        with self.assertRaises(ResearchPlanConflict):
            self._start(rejected, suffix="rejected")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0], 0
        )

    def test_start_records_workflow_tree_and_queues_only_root(self) -> None:
        created = self._create_plan(suffix="tree")
        approval = self._approve(created, suffix="tree")
        started = self._start(created, suffix="tree")
        self.assertEqual(started["question_state"], "in_progress")
        self.assertEqual([node["admission_state"] for node in started["task_tree"]], ["queued", "planned", "planned", "planned"])
        self.assertEqual(len(started["work_order_links"]), 3)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM observability_work_order_links").fetchone()[0], 3
        )
        plan_view = self.plans.plan(created["plan_version_ref"])
        self.assertEqual(plan_view["state"], "started")
        self._validate_contract("research-plan-approval.schema.json", approval["approval"])
        self._validate_contract("research-plan-start.schema.json", started["start_binding"])
        self._validate_contract("research-plan-event.schema.json", started["started_event"])

    def test_active_policy_can_authorize_and_start_low_risk_plan(self) -> None:
        created = self._create_plan(suffix="policy-start")
        active = self.store.active_policy()
        policy_wire = dict(active["policy"])
        policy_wire["research_plan_auto_start"] = {
            "enabled": True,
            "rules": [PLAN_AUTO_START_RULE_REF],
        }
        self.store.create_policy(
            policy_wire,
            policy_version_id="policy:plan-auto-start:test:v2",
            version_number=2,
            prior_version_ref=active["policy_version_id"],
            actor_ref="human:test-owner",
            change_reason="authorize isolated public SEC plans",
            activate=True,
        )
        authorized = self.plans.authorize_plan_by_policy(
            plan_version_ref=created["plan_version_ref"],
            idempotency_key="authorize-plan:policy-start",
        )
        started = self._start(created, suffix="policy-start")
        self.assertEqual(authorized["plan_state"], "approved")
        self.assertEqual(
            authorized["authorization"]["authorization"],
            "versioned_governance_policy",
        )
        self._validate_contract(
            "research-plan-policy-authorization.schema.json",
            authorized["authorization"],
        )
        self.assertEqual(started["plan_state"], "started")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_plan_approvals"
            ).fetchone()[0],
            0,
        )
        view = self.plans.plan(created["plan_version_ref"])
        self.assertIsNone(view["approval"])
        self.assertEqual(
            view["policy_authorization"]["id"],
            authorized["authorization"]["id"],
        )

    def test_policy_start_fails_closed_when_rule_is_missing_or_superseded(self) -> None:
        created = self._create_plan(suffix="policy-start-closed")
        with self.assertRaises(ResearchPlanConflict):
            self.plans.authorize_plan_by_policy(
                plan_version_ref=created["plan_version_ref"],
                idempotency_key="authorize-plan:missing-policy",
            )
        active = self.store.active_policy()
        policy_wire = dict(active["policy"])
        policy_wire["research_plan_auto_start"] = {
            "enabled": True,
            "rules": [PLAN_AUTO_START_RULE_REF],
        }
        enabled = self.store.create_policy(
            policy_wire,
            policy_version_id="policy:plan-auto-start:enabled:v2",
            version_number=2,
            prior_version_ref=active["policy_version_id"],
            actor_ref="human:test-owner",
            change_reason="enable isolated public SEC plans",
            activate=True,
        )
        self.plans.authorize_plan_by_policy(
            plan_version_ref=created["plan_version_ref"],
            idempotency_key="authorize-plan:enabled-policy",
        )
        policy_wire["research_plan_auto_start"]["enabled"] = False
        self.store.create_policy(
            policy_wire,
            policy_version_id="policy:plan-auto-start:disabled:v3",
            version_number=3,
            prior_version_ref=enabled["policy_version_id"],
            actor_ref="human:test-owner",
            change_reason="supersede autonomous start authorization",
            activate=True,
        )
        with self.assertRaises(ResearchPlanConflict):
            self._start(created, suffix="policy-start-closed")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            0,
        )

    def test_completed_start_replays_before_any_side_effect(self) -> None:
        created = self._create_plan(suffix="replay")
        self._approve(created, suffix="replay")
        fresh = self._start(created, suffix="replay")
        duplicate = self._start(created, suffix="replay")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["root_work_order_hash"], fresh["root_work_order_hash"])
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM research_plan_starts").fetchone()[0], 1
        )

    def test_external_seam_crash_replays_to_one_exact_start(self) -> None:
        created = self._create_plan(suffix="seam")
        self._approve(created, suffix="seam")

        def fail(seam: str) -> None:
            if seam == "after_link:1":
                raise InjectedPlannerCrash(seam)

        crashing = ResearchPlanControlPlane(
            self.plans, self.backlog, self.observability, self.scheduler,
            fault_injector=fail,
        )
        with self.assertRaises(InjectedPlannerCrash):
            self._start(created, suffix="seam", control=crashing)
        recovered = self._start(created, suffix="seam")
        self.assertEqual(recovered["status"], "fresh")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM observability_workflow_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM observability_work_order_links").fetchone()[0], 3
        )

    def test_transaction_seam_crash_rolls_back_and_replays(self) -> None:
        created = self._create_plan(suffix="txn")
        self._approve(created, suffix="txn")

        def fail(seam: str) -> None:
            if seam == "after_start_binding":
                raise InjectedPlannerCrash(seam)

        crashing = ResearchPlanControlPlane(
            self.plans, self.backlog, self.observability, self.scheduler,
            fault_injector=fail,
        )
        with self.assertRaises(InjectedPlannerCrash):
            self._start(created, suffix="txn", control=crashing)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM research_plan_starts").fetchone()[0], 0
        )
        self.assertEqual(self.backlog.question(created["question_ref"])["state"], "planned")
        recovered = self._start(created, suffix="txn")
        self.assertEqual(recovered["question_state"], "in_progress")

    def test_recomputed_plan_scope_tamper_still_fails_closed(self) -> None:
        created = self._create_plan(suffix="plan-tamper")
        row = self.store.connection.execute(
            "SELECT record_json FROM research_plan_versions WHERE version_id=?",
            (created["plan_version_ref"],),
        ).fetchone()
        wire = json.loads(row["record_json"])
        wire["execution_scope"]["auth_mode"] = "api_key"
        wire["content_hash"] = content_hash({key: value for key, value in wire.items() if key != "content_hash"})
        self.store.connection.execute("DROP TRIGGER research_plan_versions_no_update")
        self.store.connection.execute(
            "UPDATE research_plan_versions SET record_json=?,content_hash=? WHERE version_id=?",
            (canonical_json(wire), wire["content_hash"], created["plan_version_ref"]),
        )
        with self.assertRaises(ResearchPlanConflict):
            self.plans.plan_version(created["plan_version_ref"])
        with self.assertRaises((ResearchPlanConflict, ResearchQuestionConflict)):
            self.backlog.question(created["question_ref"])

    def test_workflow_link_and_scheduler_tamper_fail_closed(self) -> None:
        created = self._create_plan(suffix="tree-tamper")
        self._approve(created, suffix="tree-tamper")
        started = self._start(created, suffix="tree-tamper")
        self.store.connection.execute("DROP TRIGGER observability_work_order_links_no_update")
        self.store.connection.execute(
            "UPDATE observability_work_order_links SET relation='follows_up' "
            "WHERE workflow_ref=? AND sequence_number=1",
            (started["workflow_ref"],),
        )
        with self.assertRaises(ResearchPlanConflict):
            self.plans.plan(created["plan_version_ref"])
        with self.assertRaises((ResearchPlanConflict, ResearchQuestionConflict)):
            self.backlog.question(created["question_ref"])

        # Restore is unnecessary: the same exact reader must also reject a
        # Scheduler hash drift on a clean fixture.
        other = self._create_plan(suffix="scheduler-tamper")
        self._approve(other, suffix="scheduler-tamper")
        other_start = self._start(other, suffix="scheduler-tamper")
        self.store.connection.execute("DROP TRIGGER scheduler_work_no_update")
        self.store.connection.execute(
            "UPDATE scheduler_work_orders SET work_order_hash=? WHERE work_order_id=?",
            ("0" * 64, other_start["root_work_order_ref"]),
        )
        with self.assertRaises(ResearchPlanConflict):
            self.plans.plan(other["plan_version_ref"])
        with self.assertRaises((ResearchPlanConflict, ResearchQuestionConflict)):
            self.backlog.question(other["question_ref"])

    def test_unauthorized_direct_plan_write_is_rejected(self) -> None:
        with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)):
            self.store.connection.execute(
                "INSERT INTO research_plan_versions(version_id,question_ref,"
                "question_version_ref,version_number,prior_version_id,decision_ref,"
                "cycle_ref,planner_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "research-plan:" + "0" * 32,
                    "research-question:" + "0" * 32,
                    "research-question-version:forged",
                    1,
                    None,
                    "decision:forged",
                    "cycle:forged",
                    "planner:forged",
                    "{}",
                    "0" * 64,
                    NOW,
                ),
            )


if __name__ == "__main__":
    unittest.main()
