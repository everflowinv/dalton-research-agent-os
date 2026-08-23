"""Adversarial tests for the Bounded Planner Loop v1 development slice."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerConflict,
    BoundedPlannerControlPlane,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.agenda_fixtures import register_perception


NOW = "2026-08-23T12:00:00.000000+00:00"
LATER = "2026-09-23T12:00:00.000000+00:00"


def agenda_policy() -> dict:
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
        "trial_company_refs": ["acme"],
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


class InjectedCrash(RuntimeError):
    pass


class BoundedPlannerLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.observability = ObservabilityStore(self.store)
        self.agenda = AgendaStore(self.store)
        self.backlog = ResearchQuestionBacklog(self.store)
        self.scheduler = Scheduler(connection=self.store.connection)
        self.authority = BoundedPlannerAuthority(self.store)
        self.control = BoundedPlannerControlPlane(
            self.authority, self.observability, self.scheduler
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)
        self.question = self._selected_question()
        self.templates = self._publish_templates()

    def _selected_question(self) -> dict:
        self.agenda.create_policy(
            agenda_policy(), effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id="agenda-policy-version:bounded:1",
            idempotency_key="bounded:policy:1",
        )
        self.agenda.create_mandate(
            "mandate:capital-lease", objective="Determine capital-lease observability",
            scope_refs=["acme"], constraints={"mode": "development_candidate"},
            success_criteria={"formal_negative_claim_requires_human": True},
            effective_from=NOW, effective_until=LATER, actor_ref="human:owner",
            version_id="mandate-version:capital-lease:1",
            idempotency_key="bounded:mandate:1",
        )
        self.agenda.set_pause(
            False, reason="bounded planner development candidate",
            actor_ref="human:owner", version_id="agenda-control-version:bounded:1",
            idempotency_key="bounded:resume:1",
        )
        snapshot = register_perception(
            self.agenda, "perception:bounded:1", company="acme"
        )
        cycle = self.agenda.start_cycle(
            "agenda:bounded:1", perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref="mandate-version:capital-lease:1",
            policy_version_ref="agenda-policy-version:bounded:1",
            company_ref="acme", actor_ref="core",
            cycle_id="agenda-cycle:bounded:1", idempotency_key="bounded:cycle:1",
        )
        question = "Are capital-lease obligations observable in governed sources?"
        criteria = "Return source-level coverage and do not infer non-existence from a miss."
        self.agenda.add_candidates(
            cycle["cycle_id"],
            candidates=[{
                "candidate_id": "candidate:bounded:1", "company_ref": "acme",
                "question": question, "answer_criteria": criteria,
                "features": {
                    "mandate_relevance": 3, "catalyst_urgency": 1,
                    "evidence_staleness": 2, "decision_impact": 3,
                },
                "rationale": "capital lease vertical slice",
                "source_refs": ["source:sec-edgar"],
            }],
            actor_ref="core", idempotency_key="bounded:candidates:1",
        )
        decision = self.agenda.decide_cycle(
            cycle["cycle_id"], actor_ref="core", decision_id="decision:bounded:1",
            idempotency_key="bounded:decision:1",
        )
        record = self.backlog.record_question(
            mandate_version_ref="mandate-version:capital-lease:1",
            company_ref="acme", question=question, answer_criteria=criteria,
            source_refs=["source:sec-edgar"], actor_ref="core",
            idempotency_key="bounded:question:1",
        )
        self.backlog.select_question(
            question_ref=record["question_ref"], decision_ref=decision["id"],
            actor_ref="core", idempotency_key="bounded:select:1",
        )
        return record

    def _publish_templates(self) -> list[dict]:
        specifications = [
            ("capital-lease-keyword", "search_filing_keywords", "annual filing", ["capital lease", "finance lease"]),
            ("lease-footnote", "read_lease_footnote", "lease footnote", ["finance lease", "lease liabilities"]),
            ("commitments", "read_commitments", "commitments section", ["lease commitments", "debt maturity"]),
        ]
        templates = []
        for index, (item, operation, locator, terms) in enumerate(specifications, start=1):
            templates.append(self.authority.publish_probe_template(
                f"probe-template:capital-lease:{item}",
                capability_ref="capability:sec-read-only", operation=operation,
                runtime_profile_ref="runtime:sec-read-only:0.1",
                parameter_contract={
                    "allowed_fields": ["source_ref", "locator", "query_terms"],
                    "required_fields": ["source_ref", "locator", "query_terms"],
                    "constants": {"source_ref": "source:sec-edgar"},
                },
                output_contract_ref="schema:bounded-planner-probe-output:0.1",
                verifier_ref="verifier:source-level-coverage:0.1",
                permission_scope="public_sec_read", declared_side_effects=["read:public-http"],
                cost={"cost_units": 1, "max_attempts": 2, "max_seconds": 10},
                actor_ref="human:owner",
            ) | {"coverage_item": item, "parameters": {
                "source_ref": "source:sec-edgar", "locator": locator,
                "query_terms": terms,
            }})
        return templates

    def _loop(self, *, max_rounds: int = 3, max_cost: int = 3) -> dict:
        return self.authority.create_loop(
            f"bounded-loop:capital-lease:{max_rounds}:{max_cost}",
            question_version_ref=self.question["question_version_ref"],
            template_bindings=[{
                "coverage_item_ref": template["coverage_item"],
                "template_version_ref": template["id"],
                "parameters": template["parameters"],
            } for template in self.templates],
            required_coverage_items=[template["coverage_item"] for template in self.templates],
            budget={"max_rounds": max_rounds, "max_cost_units": max_cost, "max_seconds": 30},
            actor_ref="human:owner",
        )

    def _complete(self, round_wire: dict, matches: list[dict] | None = None) -> dict:
        lease = self.scheduler.claim(
            "worker:fixture", work_order_id=round_wire["work_order_ref"]
        )
        self.assertIsNotNone(lease)
        result_id = f"result:bounded:{round_wire['ordinal']}:{content_hash(matches or [])[:8]}"
        result = {
            "schema_version": "0.1", "id": result_id,
            "created_at": f"2026-08-23T12:00:{round_wire['ordinal']:02d}.000000+00:00",
            "work_order_ref": round_wire["work_order_ref"],
            "invocation_ref": f"invocation:bounded:{round_wire['ordinal']}",
            "status": "succeeded", "outputs": {"matches": matches or []},
            "actual_side_effects": [], "usage_refs": [], "artifact_refs": [],
            "error": None, "metadata": {"fixture": True},
        }
        completed = self.scheduler.complete(
            round_wire["work_order_ref"], 1, "worker:fixture", lease["lease_token"],
            result, idempotency_key=f"complete:{result_id}",
        )
        self.assertEqual(completed["work_state"], "succeeded")
        return self.control.record_outcome(round_wire["id"])

    def _run_one(self, loop: dict, matches: list[dict] | None = None) -> tuple[dict, dict]:
        proposal = self.authority.propose_next_capital_lease(loop["id"])
        admitted = self.control.admit_proposal(proposal["id"])
        self.assertEqual(admitted["status"], "fresh")
        outcome = self._complete(admitted["round"], matches)
        return admitted["round"], outcome

    def test_three_source_misses_form_candidate_not_negative_claim(self) -> None:
        loop = self._loop()
        rounds = [self._run_one(loop)[0] for _ in range(3)]
        terminal_proposal = self.authority.propose_next_capital_lease(loop["id"])
        self.assertEqual(
            terminal_proposal["action"]["reason"],
            "coverage_complete_unobservable_candidate",
        )
        terminal = self.control.admit_proposal(terminal_proposal["id"])
        self.assertEqual(terminal["status"], "terminal")
        self.assertFalse(terminal["terminal_event"]["formal_negative_claim_created"])
        self.assertEqual(
            [item["outcome_kind"] for item in self.authority.outcomes(loop["id"])],
            ["not_found_in_scope"] * 3,
        )
        manifest = self.authority._one(
            "bounded_coverage_manifests", "manifest_id",
            self.authority.outcomes(loop["id"])[-1]["coverage_manifest_ref"],
            "CoverageManifest",
        )
        self.assertTrue(manifest["coverage_complete"])
        self.assertTrue(manifest["negative_candidate_eligible"])
        self.assertEqual(len(self.observability.work_order_links(rounds[0]["workflow_ref"])), 2)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0],
            3,
        )
        table_names = {
            row[0] for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertFalse(any(name.startswith("bounded_") and "queue" in name for name in table_names))

    def test_crash_after_enqueue_converges_without_second_work_order(self) -> None:
        loop = self._loop()
        proposal = self.authority.propose_next_capital_lease(loop["id"])

        def crash(seam: str) -> None:
            if seam == "after_enqueue":
                raise InjectedCrash(seam)

        crashing = BoundedPlannerControlPlane(
            self.authority, self.observability, self.scheduler, fault_injector=crash
        )
        with self.assertRaises(InjectedCrash):
            crashing.admit_proposal(proposal["id"])
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0],
            1,
        )
        replay = self.control.admit_proposal(proposal["id"])
        self.assertEqual(replay["status"], "fresh")
        duplicate = self.control.admit_proposal(proposal["id"])
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0],
            1,
        )

    def test_second_round_crash_after_link_converges_to_one_workflow_tree(self) -> None:
        loop = self._loop()
        self._run_one(loop)
        proposal = self.authority.propose_next_capital_lease(loop["id"])

        def crash(seam: str) -> None:
            if seam == "after_link":
                raise InjectedCrash(seam)

        crashing = BoundedPlannerControlPlane(
            self.authority, self.observability, self.scheduler, fault_injector=crash
        )
        with self.assertRaises(InjectedCrash):
            crashing.admit_proposal(proposal["id"])
        replay = self.control.admit_proposal(proposal["id"])
        self.assertEqual(replay["status"], "fresh")
        self.assertEqual(len(self.authority.rounds(loop["id"])), 2)
        self.assertEqual(len(self.observability.work_order_links(replay["round"]["workflow_ref"])), 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM observability_workflow_versions WHERE workflow_ref=?",
                (replay["round"]["workflow_ref"],),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM scheduler_work_orders").fetchone()[0],
            2,
        )

    def test_mid_round_directive_preserves_current_round_and_changes_next(self) -> None:
        loop = self._loop()
        first = self.authority.propose_next_capital_lease(loop["id"])
        admitted = self.control.admit_proposal(first["id"])
        directive = self.authority.issue_directive(
            loop["id"], verbatim_text="先看 commitments，不要改当前正在跑的任务",
            control_effect="focus_coverage_item", target_coverage_item_ref="commitments",
            actor_ref="human:owner",
        )
        self.assertEqual(directive["receipt"]["current_round_ref"], admitted["round"]["id"])
        self.assertTrue(directive["receipt"]["current_round_unchanged"])
        self.assertEqual(directive["receipt"]["effective_round"], 2)
        self._complete(admitted["round"])
        second = self.authority.propose_next_capital_lease(loop["id"])
        self.assertEqual(second["action"]["coverage_item_ref"], "commitments")
        self.assertEqual(
            second["directive_bindings"][0]["directive_version_ref"],
            directive["directive"]["id"],
        )

    def test_premature_negative_terminal_is_rejected(self) -> None:
        loop = self._loop()
        proposal = self.authority.submit_proposal(
            loop["id"],
            action={"kind": "terminate", "reason": "coverage_complete_unobservable_candidate"},
            rationale="model incorrectly equated one miss with non-existence",
            actor_ref="planner:test-model",
        )
        decision = self.control.admit_proposal(proposal["id"])
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(
            decision["decision"]["reason"],
            "negative_terminal_requires_complete_no_match_coverage",
        )
        self.assertIsNone(self.authority.terminal(loop["id"]))

    def test_budget_exhaustion_is_blocked_not_unobservable(self) -> None:
        loop = self._loop(max_rounds=1, max_cost=1)
        self._run_one(loop)
        result = self.authority.propose_next_capital_lease(loop["id"])
        self.assertEqual(result["status"], "terminal")
        self.assertEqual(result["terminal_event"]["terminal_state"], "budget_exhausted")
        self.assertFalse(result["terminal_event"]["formal_negative_claim_created"])

    def test_duplicate_probe_and_manifest_tampering_fail_closed(self) -> None:
        loop = self._loop()
        first_round, first_outcome = self._run_one(loop)
        first_proposal = self.authority.proposal(first_round["proposal_ref"])
        duplicate = self.authority.submit_proposal(
            loop["id"], action=first_proposal["action"],
            rationale="repeat the same probe", actor_ref="planner:test-model",
        )
        rejected = self.control.admit_proposal(duplicate["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn(rejected["decision"]["reason"], {
            "coverage_item_already_terminal", "duplicate_probe",
        })
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE bounded_coverage_manifests SET record_json='{}' WHERE manifest_id=?",
                (first_outcome["manifest"]["id"],),
            )

    def test_observed_result_is_source_level_and_routes_to_review(self) -> None:
        loop = self._loop()
        self._run_one(loop, [{"source_location": "accession:0001#lease-note"}])
        self._run_one(loop)
        self._run_one(loop)
        proposal = self.authority.propose_next_capital_lease(loop["id"])
        self.assertEqual(proposal["action"]["reason"], "evidence_observed_for_review")
        terminal = self.control.admit_proposal(proposal["id"])
        self.assertEqual(terminal["status"], "terminal")
        observed = self.authority.outcomes(loop["id"])[0]
        self.assertEqual(observed["outcome_kind"], "observed")
        self.assertEqual(observed["formal_claim_refs"], [])


if __name__ == "__main__":
    unittest.main()
