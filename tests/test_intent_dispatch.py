from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
from dalton_core.intent_dispatch import IntentDispatchConflict, IntentWriterAuthority
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.store import DaltonStore
from tests.agenda_fixtures import register_perception


class IntentWriterAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.addCleanup(self.store.close)
        self.agenda = AgendaStore(self.store)
        self.backlog = ResearchQuestionBacklog(self.store)
        self.bounded = BoundedPlannerAuthority(self.store)
        self.writer = IntentWriterAuthority(
            self.agenda, self.backlog, self.bounded
        )
        self.mandate = self.agenda.create_mandate(
            "mandate:acn",
            objective="Explain ACN growth conversion",
            scope_refs=["acn"],
            constraints={"public_sources_only": True},
            success_criteria={"decision_useful": True},
            effective_from="2026-01-01T00:00:00+00:00",
            effective_until="2030-01-01T00:00:00+00:00",
            actor_ref="human:owner",
            version_id="mandate-version:acn:1",
            idempotency_key="intent-dispatch:mandate",
        )

    def _mandate_binding(self):
        return next(
            item for item in self.writer.context_bindings()
            if item["kind"] == "mandate"
        )

    def _question(self):
        return self.writer.admit_question(
            subject_binding=self._mandate_binding(),
            question="Why did bookings not convert to organic growth?",
            answer_criteria="Reconcile bookings, organic revenue, and timing.",
            candidate_version_ref="intent-candidate-version:question",
            candidate_version_hash="a" * 64,
            confirmation_ref="intent-confirmation:question",
            confirmation_hash="b" * 64,
            actor_ref="human:owner",
            idempotency_key="intent-question:1",
        )

    def test_question_admission_resolves_exact_mandate_and_is_idempotent(self):
        first = self._question()
        second = self._question()
        self.assertEqual(first["authority_result"]["status"], "fresh")
        self.assertEqual(second["authority_result"]["status"], "duplicate")
        self.assertEqual(first["mandate_version_ref"], self.mandate["id"])
        saved = self.backlog.question(
            first["authority_result"]["question_ref"]
        )
        self.assertEqual(saved["head"]["actor_ref"], "human:owner")
        self.assertEqual(
            saved["head"]["source_refs"],
            [
                "intent-candidate-version:question",
                "intent-confirmation:question",
                self.mandate["id"],
            ],
        )

    def test_question_admission_rejects_stale_exact_binding(self):
        binding = self._mandate_binding()
        binding["hash"] = "f" * 64
        with self.assertRaises(IntentDispatchConflict):
            self.writer.admit_question(
                subject_binding=binding,
                question="Why?",
                answer_criteria="Answer from governed sources.",
                candidate_version_ref="intent-candidate-version:stale",
                candidate_version_hash="a" * 64,
                confirmation_ref="intent-confirmation:stale",
                confirmation_hash="b" * 64,
                actor_ref="human:owner",
                idempotency_key="intent-question:stale",
            )

    def test_question_admission_resolves_exact_agenda_decision(self):
        policy = self.agenda.create_policy(
            {
                "schema_version": "0.1",
                "enabled": True,
                "selected_count": 1,
                "max_model_calls_per_cycle": 1,
                "max_daily_cycles": 1,
                "max_daily_cost_usd": 0.5,
                "max_monthly_cost_usd": 10.0,
                "max_input_tokens": 8000,
                "max_output_tokens": 2000,
                "feature_weights": {
                    "mandate_relevance": 3,
                    "catalyst_urgency": 3,
                    "evidence_staleness": 2,
                    "decision_impact": 3,
                },
                "trial_company_refs": ["acn"],
                "cutover_enabled": False,
                "cutover_acceptance_threshold": None,
            },
            effective_from="2026-01-01T00:00:00+00:00",
            effective_until="2030-01-01T00:00:00+00:00",
            actor_ref="human:owner",
            version_id="agenda-policy-version:intent",
            idempotency_key="intent-dispatch:policy",
        )
        snapshot = register_perception(
            self.agenda,
            "perception:intent-dispatch",
            company="acn",
            generated_at="2026-08-25T00:00:00.000000+00:00",
        )
        cycle = self.agenda.start_cycle(
            "agenda:intent-dispatch:acn",
            perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref=self.mandate["id"],
            policy_version_ref=policy["id"],
            company_ref="acn",
            actor_ref="core",
            cycle_id="agenda-cycle:intent-dispatch",
            idempotency_key="intent-dispatch:cycle",
        )
        self.agenda.add_candidates(
            cycle["cycle_id"],
            actor_ref="core",
            idempotency_key="intent-dispatch:candidate",
            candidates=[{
                "candidate_id": "candidate:intent-dispatch",
                "company_ref": "acn",
                "question": "Why did bookings not convert?",
                "answer_criteria": "Reconcile bookings and organic growth.",
                "features": {
                    "mandate_relevance": 3,
                    "catalyst_urgency": 3,
                    "evidence_staleness": 2,
                    "decision_impact": 3,
                },
                "rationale": "Decision useful",
                "source_refs": ["source:agenda"],
            }],
        )
        self.agenda.decide_cycle(
            cycle["cycle_id"],
            actor_ref="core",
            decision_id="decision:intent-dispatch",
            idempotency_key="intent-dispatch:decision",
        )
        outbox = self.agenda.connection.execute(
            "SELECT payload_hash FROM agenda_outbox_messages "
            "WHERE json_extract(payload_json,'$.decision_ref')=?",
            ("decision:intent-dispatch",),
        ).fetchone()
        binding = {
            "kind": "agenda_decision",
            "ref": "decision:intent-dispatch",
            "hash": outbox["payload_hash"],
            "label": "Agenda · acn",
            "state": "pending",
            "authority": True,
            "parent_ref": None,
            "allowed_intents": ["approval", "meta", "priority", "question"],
        }
        result = self.writer.admit_question(
            subject_binding=binding,
            question="What evidence would resolve this Agenda decision?",
            answer_criteria="Bind the answer to the selected question and filing evidence.",
            candidate_version_ref="intent-candidate-version:agenda-question",
            candidate_version_hash="a" * 64,
            confirmation_ref="intent-confirmation:agenda-question",
            confirmation_hash="b" * 64,
            actor_ref="human:owner",
            idempotency_key="intent-question:agenda",
        )
        self.assertEqual(result["mandate_version_ref"], self.mandate["id"])
        self.assertEqual(result["company_ref"], "acn")

    def test_directive_uses_existing_bounded_planner_writer(self):
        question = self._question()["authority_result"]
        template = self.bounded.publish_probe_template(
            "probe-template:acn-bookings",
            capability_ref="capability:sec-read-only",
            operation="search_filing_keywords",
            runtime_profile_ref="runtime:sec-read-only:0.1",
            parameter_contract={
                "allowed_fields": ["source_ref"],
                "required_fields": ["source_ref"],
                "constants": {"source_ref": "source:sec-edgar"},
            },
            output_contract_ref="schema:probe-output:0.1",
            verifier_ref="verifier:source-coverage:0.1",
            permission_scope="public_sec_read",
            declared_side_effects=["read:public-http"],
            cost={"cost_units": 1, "max_attempts": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        loop = self.bounded.create_loop(
            "bounded-loop:acn",
            question_version_ref=question["question_version_ref"],
            template_bindings=[{
                "coverage_item_ref": "coverage-item:bookings",
                "template_version_ref": template["id"],
                "parameters": {"source_ref": "source:sec-edgar"},
            }],
            required_coverage_items=["coverage-item:bookings"],
            budget={"max_rounds": 1, "max_cost_units": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        bindings = self.writer.context_bindings()
        loop_binding = next(
            item for item in bindings if item["ref"] == loop["id"]
        )
        coverage = next(
            item for item in bindings if item["kind"] == "coverage_item"
        )
        result = self.writer.issue_directive(
            loop_binding=loop_binding,
            target_coverage_item_binding=coverage,
            verbatim_text="下一轮先查 bookings。",
            control_effect="focus_coverage_item",
            candidate_version_ref="intent-candidate-version:directive",
            candidate_version_hash="c" * 64,
            confirmation_ref="intent-confirmation:directive",
            confirmation_hash="d" * 64,
            actor_ref="human:owner",
        )
        self.assertEqual(result["authority_result"]["status"], "fresh")
        self.assertEqual(
            result["authority_result"]["directive"]["loop_version_ref"],
            loop["id"],
        )
        self.assertTrue(
            result["authority_result"]["receipt"]["current_round_unchanged"]
        )


if __name__ == "__main__":
    unittest.main()
