"""Regression tests for the Agenda context authority path.

These cover the invariants that make the Agenda prompt replayable: the
PerceptionSnapshot is Core-resident and append-only, the mandate is read from
its canonical record rather than a caller body, the plan binding is a closed
typed record instead of a forged connector plan, both facts are required, and
the coordinator can no longer splice data into the prompt by hand.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import (
    AgendaConflict,
    AgendaNotFound,
    AgendaStore,
    read_exact_agenda_cycle,
    read_exact_mandate_version,
    read_exact_perception_snapshot,
)
from dalton_core.agenda_context import (
    AgendaContextError,
    agenda_source_catalog,
    build_agenda_context,
)
from dalton_core.agenda_coordinator import build_prompt, parse_candidates, prompt_wrapper
from dalton_core.context_materializer import (
    ContextMaterializer,
    ContextMaterializerConflict,
    ContextMaterializerUnsupported,
    validate_context_materialization,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.research_context import (
    AGENDA_BINDER_REF,
    ResearchContextConflict,
    ResearchContextError,
    build_agenda_context_binding,
    build_claim_index,
    build_reference_fixture_plan,
    count_dalton_search_tokens,
    validate_agenda_context_binding,
)
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.agenda_fixtures import perception_wire, register_perception


NOW = "2026-08-14T10:00:00.000000+00:00"
LATER = "2026-09-14T10:00:00.000000+00:00"


def policy_wire() -> dict:
    return {
        "schema_version": "0.1", "enabled": True, "selected_count": 2,
        "max_model_calls_per_cycle": 1, "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5, "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000, "max_output_tokens": 2000,
        "feature_weights": {
            "mandate_relevance": 4, "catalyst_urgency": 3,
            "evidence_staleness": 2, "decision_impact": 4,
        },
        "trial_company_refs": ["wanhua"], "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


class AgendaContextTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "core.sqlite"
        self.store = DaltonStore(self.database)
        self.obs = ObservabilityStore(self.store)
        self.agenda = AgendaStore(self.store)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.policy = self.agenda.create_policy(
            policy_wire(), effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id="agenda-policy-version:1",
            idempotency_key="policy:1",
        )
        self.mandate = self.agenda.create_mandate(
            "mandate:coverage-quality",
            objective="Find the most decision-useful unanswered question",
            scope_refs=["wanhua"], constraints={"mode": "shadow"},
            success_criteria={"human_feedback_required": True},
            effective_from=NOW, effective_until=LATER, actor_ref="human:owner",
            version_id="mandate-version:1", idempotency_key="mandate:1",
        )
        self.snapshot = register_perception(self.agenda, "perception:1")
        self.cycle = self.agenda.start_cycle(
            "agenda:2026-08-14:wanhua",
            perception_snapshot_ref=self.snapshot["snapshot_id"],
            perception_snapshot_hash=self.snapshot["content_hash"],
            mandate_version_ref=self.mandate["id"],
            policy_version_ref=self.policy["id"], company_ref="wanhua",
            actor_ref="core", cycle_id="agenda-cycle:1", idempotency_key="cycle:1",
        )

    def context(self, **kwargs):
        params = {
            "cycle_id": self.cycle["cycle_id"], "max_tokens": 8000,
            "max_bytes": 200_000,
        }
        params.update(kwargs)
        return build_agenda_context(self.store, self.obs, **params)


class PerceptionAuthorityTests(AgendaContextTestCase):
    def test_snapshot_is_registered_read_exactly_and_is_idempotent(self):
        exact = read_exact_perception_snapshot(
            self.store.connection, self.snapshot["snapshot_id"]
        )
        self.assertEqual(exact["content_hash"], self.snapshot["content_hash"])
        again = self.agenda.register_perception_snapshot(
            self.snapshot, actor_ref="core", idempotency_key="perception:perception:1"
        )
        self.assertEqual(again["status"], "duplicate")

    def test_same_snapshot_id_with_other_content_conflicts(self):
        other = perception_wire("perception:1")
        other["company"] = dict(other["company"], name="changed")
        other.pop("content_hash")
        other["content_hash"] = content_hash(other)
        with self.assertRaises(AgendaConflict):
            self.agenda.register_perception_snapshot(other, actor_ref="core")

    def test_snapshot_rows_are_immutable_and_writer_gated(self):
        raw = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                raw.execute(
                    "UPDATE perception_snapshot_versions SET company_ref='other'"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                raw.execute("DELETE FROM perception_snapshot_versions")
            with self.assertRaises(sqlite3.DatabaseError):
                raw.execute(
                    "INSERT INTO perception_snapshot_versions"
                    "(snapshot_id,company_ref,source_kind,source_snapshot_hash,"
                    "generated_at,actor_ref,record_json,content_hash,created_at) "
                    "VALUES('x','x','x','x','x','x','{}','x','x')"
                )
        finally:
            raw.close()

    def test_missing_snapshot_and_unmigrated_database_fail_closed(self):
        with self.assertRaises(AgendaNotFound):
            read_exact_perception_snapshot(self.store.connection, "perception:absent")
        bare = DaltonStore(Path(self.tmp.name) / "bare.sqlite")
        try:
            with self.assertRaises(AgendaNotFound):
                read_exact_perception_snapshot(bare.connection, "perception:1")
        finally:
            bare.close()

    def test_start_cycle_requires_exact_perception_and_mandate(self):
        with self.assertRaises(AgendaNotFound):
            self.agenda.start_cycle(
                "agenda:absent", perception_snapshot_ref="perception:absent",
                perception_snapshot_hash="a" * 64,
                mandate_version_ref=self.mandate["id"],
                policy_version_ref=self.policy["id"], company_ref="wanhua",
                actor_ref="core",
            )
        with self.assertRaises(AgendaConflict):
            self.agenda.start_cycle(
                "agenda:wrong-hash",
                perception_snapshot_ref=self.snapshot["snapshot_id"],
                perception_snapshot_hash="a" * 64,
                mandate_version_ref=self.mandate["id"],
                policy_version_ref=self.policy["id"], company_ref="wanhua",
                actor_ref="core",
            )
        with self.assertRaises(AgendaConflict):
            self.agenda.start_cycle(
                "agenda:out-of-scope",
                perception_snapshot_ref=self.snapshot["snapshot_id"],
                perception_snapshot_hash=self.snapshot["content_hash"],
                mandate_version_ref=self.mandate["id"],
                policy_version_ref=self.policy["id"], company_ref="other",
                actor_ref="core",
            )


class ExactReaderTamperTests(AgendaContextTestCase):
    def _force(self, sql: str, parameters: tuple = ()) -> None:
        """Bypass the immutability triggers the way a tamperer would."""

        raw = sqlite3.connect(self.database)
        try:
            for table in (
                "perception_snapshot_versions", "mandate_versions",
                "agenda_policy_versions", "agenda_cycles",
            ):
                for action in ("update", "delete"):
                    raw.execute(f"DROP TRIGGER IF EXISTS {table}_no_{action}")
            raw.execute(sql, parameters)
            raw.commit()
        finally:
            raw.close()

    def test_perception_column_drift_is_detected(self):
        self._force(
            "UPDATE perception_snapshot_versions SET company_ref='other' "
            "WHERE snapshot_id=?",
            (self.snapshot["snapshot_id"],),
        )
        with self.assertRaises(AgendaConflict):
            read_exact_perception_snapshot(
                self.store.connection, self.snapshot["snapshot_id"]
            )

    def test_perception_record_body_edit_is_detected(self):
        tampered = dict(self.snapshot)
        tampered["company"] = dict(tampered["company"], name="rewritten")
        self._force(
            "UPDATE perception_snapshot_versions SET record_json=? WHERE snapshot_id=?",
            (canonical_json(tampered), self.snapshot["snapshot_id"]),
        )
        with self.assertRaises(AgendaConflict):
            read_exact_perception_snapshot(
                self.store.connection, self.snapshot["snapshot_id"]
            )

    def test_mandate_column_drift_is_detected(self):
        self._force(
            "UPDATE mandate_versions SET objective='rewritten' WHERE version_id=?",
            (self.mandate["id"],),
        )
        with self.assertRaises(AgendaConflict):
            read_exact_mandate_version(self.store.connection, self.mandate["id"])
        with self.assertRaises(AgendaConflict):
            self.agenda.active_mandates(at=NOW)

    def test_active_policy_is_reverified_from_exact_authority(self):
        self._force(
            "UPDATE agenda_policy_versions SET enabled=0 WHERE version_id=?",
            (self.policy["id"],),
        )
        with self.assertRaises(AgendaConflict):
            self.agenda.active_policy(at=NOW)

    def test_mandate_scope_projection_drift_is_detected(self):
        self._force(
            "UPDATE mandate_versions SET scope_refs_json=? WHERE version_id=?",
            (canonical_json(["other"]), self.mandate["id"]),
        )
        with self.assertRaises(AgendaConflict):
            read_exact_mandate_version(self.store.connection, self.mandate["id"])

    def test_cycle_column_drift_is_detected(self):
        self._force(
            "UPDATE agenda_cycles SET mandate_version_ref='mandate-version:other' "
            "WHERE cycle_id=?",
            (self.cycle["cycle_id"],),
        )
        with self.assertRaises(AgendaConflict):
            read_exact_agenda_cycle(self.store.connection, self.cycle["cycle_id"])

    def test_cycle_frozen_hash_rejects_a_self_consistent_mandate_rewrite(self):
        original = read_exact_mandate_version(
            self.store.connection, self.mandate["id"]
        )
        tampered = dict(original, objective="self-consistent rewrite")
        tampered["content_hash"] = content_hash(
            {key: value for key, value in tampered.items() if key != "content_hash"}
        )
        self._force(
            "UPDATE mandate_versions SET objective=?,record_json=?,content_hash=? "
            "WHERE version_id=?",
            (
                tampered["objective"], canonical_json(tampered),
                tampered["content_hash"], self.mandate["id"],
            ),
        )
        # The exact row is internally valid, but it is not the version hash
        # the cycle froze at start.
        self.assertEqual(
            read_exact_mandate_version(
                self.store.connection, self.mandate["id"]
            )["content_hash"],
            tampered["content_hash"],
        )
        with self.assertRaises(AgendaContextError):
            build_agenda_context(
                self.store,
                self.obs,
                cycle_id=self.cycle["cycle_id"],
                max_tokens=8000,
                max_bytes=200_000,
            )


class AgendaContextBindingTests(unittest.TestCase):
    def binding(self, **kwargs) -> dict:
        params = {
            "cycle_ref": "agenda-cycle:1", "cycle_hash": "a" * 64,
            "company_ref": "wanhua", "policy_version_ref": "agenda-policy-version:1",
            "policy_version_hash": "b" * 64,
            "mandate_version_ref": "mandate-version:1",
            "mandate_version_hash": "c" * 64,
            "perception_snapshot_ref": "perception:1",
            "perception_snapshot_hash": "d" * 64, "created_at": NOW,
        }
        params.update(kwargs)
        return build_agenda_context_binding(**params)

    def test_binding_is_closed_and_reconstructible(self):
        first = self.binding()
        self.assertEqual(first["binder_ref"], AGENDA_BINDER_REF)
        self.assertEqual(first["task_ref"], first["cycle_ref"])
        self.assertEqual(validate_agenda_context_binding(first), first)
        # Rebuilt from the same exact refs and hashes, the id is stable.
        self.assertEqual(self.binding(created_at=LATER)["id"], first["id"])
        different = self.binding(perception_snapshot_hash="e" * 64)
        self.assertNotEqual(different["id"], first["id"])

    def test_extra_or_missing_fields_are_rejected(self):
        wire = self.binding()
        extra = dict(wire, planner_ref="planner:smuggled")
        with self.assertRaises(ResearchContextError):
            validate_agenda_context_binding(extra)
        missing = dict(wire)
        missing.pop("company_ref")
        with self.assertRaises(ResearchContextError):
            validate_agenda_context_binding(missing)

    def test_drifted_binder_id_and_task_binding_are_rejected(self):
        wire = self.binding()
        drifted = dict(wire, binder_ref="agenda-context-binder:other")
        drifted["content_hash"] = content_hash(
            {k: v for k, v in drifted.items() if k != "content_hash"}
        )
        with self.assertRaises(ResearchContextConflict):
            validate_agenda_context_binding(drifted)
        forged_id = dict(wire, id="agenda-context-binding:" + "f" * 64)
        forged_id["content_hash"] = content_hash(
            {k: v for k, v in forged_id.items() if k != "content_hash"}
        )
        with self.assertRaises(ResearchContextConflict):
            validate_agenda_context_binding(forged_id)
        retask = dict(wire, task_ref="work-order:other")
        retask["content_hash"] = content_hash(
            {k: v for k, v in retask.items() if k != "content_hash"}
        )
        with self.assertRaises(ResearchContextConflict):
            validate_agenda_context_binding(retask)

    def test_content_hash_edit_is_rejected(self):
        wire = self.binding()
        with self.assertRaises(ResearchContextConflict):
            validate_agenda_context_binding(dict(wire, content_hash="0" * 64))

    def test_closed_json_schema_matches_the_binding_and_materializer_kinds(self):
        contracts = Path(__file__).resolve().parents[1] / "contracts"
        binding_schema = json.loads(
            (contracts / "agenda-context-binding.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(binding_schema["additionalProperties"])
        self.assertEqual(set(binding_schema["required"]), set(self.binding()))
        materialization_schema = json.loads(
            (contracts / "context-materialization.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(materialization_schema["$defs"]["input"]["properties"]["kind"]["enum"]),
            {"claim", "artifact", "mandate", "perception"},
        )

    def test_public_exports_are_available(self):
        import dalton_core

        self.assertIs(dalton_core.build_agenda_context, build_agenda_context)
        self.assertIs(
            dalton_core.build_agenda_context_binding,
            build_agenda_context_binding,
        )
        self.assertIs(
            dalton_core.validate_agenda_context_binding,
            validate_agenda_context_binding,
        )


class AgendaMaterializationTests(AgendaContextTestCase):
    def test_context_quotes_both_facts_from_core_only(self):
        context = self.context()
        manifest = validate_context_materialization(context["manifest"])
        kinds = sorted(item["kind"] for item in manifest["inputs"])
        self.assertEqual(kinds, ["mandate", "perception"])
        self.assertEqual(manifest["totals"]["selected_count"], 2)
        self.assertEqual(manifest["totals"]["failure_count"], 0)
        self.assertIn("evidence-1", context["rendered_text"])
        self.assertIn('"quoted_data_only":true', context["rendered_text"])
        # The manifest is durable-safe: no body, no locator, no database path.
        serialized = json.dumps(manifest)
        self.assertNotIn("evidence-1", serialized)
        self.assertNotIn(str(self.database), serialized)
        cycle = read_exact_agenda_cycle(
            self.store.connection, self.cycle["cycle_id"]
        )
        self.assertEqual(cycle["mandate_version_hash"], self.mandate["content_hash"])
        self.assertEqual(cycle["policy_version_hash"], self.policy["content_hash"])
        wrong_renderer = copy.deepcopy(manifest)
        wrong_renderer["renderer_ref"] = "context-materializer:quoted-json-lines:0.1"
        wrong_renderer["renderer_hash"] = content_hash(
            {"renderer_ref": wrong_renderer["renderer_ref"]}
        )
        with self.assertRaises(ContextMaterializerConflict):
            validate_context_materialization(wrong_renderer)

    def test_context_is_stable_across_a_restart(self):
        first = self.context()
        self.store.close()
        reopened = DaltonStore(self.database)
        observability = ObservabilityStore(reopened)
        AgendaStore(reopened)
        try:
            second = build_agenda_context(
                reopened, observability, cycle_id=self.cycle["cycle_id"],
                max_tokens=8000, max_bytes=200_000,
            )
        finally:
            reopened.close()
        self.assertEqual(first["rendered_text"], second["rendered_text"])
        self.assertEqual(first["binding"], second["binding"])
        self.assertEqual(
            first["manifest"]["rendered_content_hash"],
            second["manifest"]["rendered_content_hash"],
        )
        self.store = DaltonStore(self.database)

    def test_unrelated_ledger_growth_replays_the_same_model_prompt(self):
        """Compatibility plumbing may move; the Agenda prompt may not.

        ContextPack 0.1 carries a stable no-ClaimIndex sentinel for Agenda.
        An unrelated claim therefore cannot move the pack, manifest, renderer,
        WorkOrder input, or full prompt.
        """

        first = self.context()
        self.store.register_invocation({
            "schema_version": "0.1", "id": "inv:unrelated", "created_at": NOW,
            "work_order_ref": "work:unrelated", "profile_ref": "profile:unrelated",
            "granularity": "task", "capability": "research", "provider": "fixture",
            "model": "fixture", "model_family": "fixture", "input_refs": [],
            "output_refs": [], "started_at": NOW, "completed_at": NOW, "usage": {},
            "side_effects": [], "runtime_ref": "runtime:fixture",
            "actor_ref": "actor:fixture", "parent_ref": None,
            "environment_hash": "e" * 64,
        })
        self.store.register_claim({
            "claim_ref": "claim:unrelated", "subject_ref": "company:other",
            "metric_or_aspect": "business", "period": "2026Q2", "basis": "reported",
            "normalized_statement": "An unrelated claim was committed.",
            "claim_kind": "qualitative", "value": None, "unit": None,
            "producer_invocation_refs": ["inv:unrelated"], "actor_ref": "actor:fixture",
        })
        second = self.context()
        self.assertEqual(first["binding"], second["binding"])
        self.assertEqual(
            [item["ref"] for item in first["manifest"]["inputs"]],
            [item["ref"] for item in second["manifest"]["inputs"]],
        )
        self.assertEqual(
            [item["body_hash"] for item in first["manifest"]["inputs"]],
            [item["body_hash"] for item in second["manifest"]["inputs"]],
        )
        self.assertEqual(first["rendered_text"], second["rendered_text"])
        self.assertEqual(
            first["manifest"]["rendered_content_hash"],
            second["manifest"]["rendered_content_hash"],
        )
        self.assertEqual(
            build_prompt(first["rendered_text"]),
            build_prompt(second["rendered_text"]),
        )
        self.assertEqual(first["context_pack"], second["context_pack"])
        self.assertEqual(first["manifest"], second["manifest"])

    def test_allowed_source_refs_come_from_the_exact_snapshot(self):
        context = self.context()
        self.assertEqual(
            context["allowed_source_refs"], agenda_source_catalog(self.snapshot)
        )
        self.assertIn("evidence:evidence-1", context["allowed_source_refs"])
        self.assertIn("filing:0001", context["allowed_source_refs"])

    def test_token_budget_drop_of_a_required_fact_fails_closed(self):
        with self.assertRaises(ContextMaterializerConflict):
            self.context(max_tokens=40)
        with self.assertRaises(ContextMaterializerConflict):
            self.context(max_bytes=200)

    def test_absent_cycle_fails_closed(self):
        with self.assertRaises(AgendaNotFound):
            self.context(cycle_id="agenda-cycle:absent")

    def test_core_must_be_the_exact_store(self):
        with self.assertRaises(AgendaContextError):
            build_agenda_context(
                object(), self.obs, cycle_id=self.cycle["cycle_id"],
                max_tokens=8000, max_bytes=200_000,
            )


class MaterializerPlanUnionTests(AgendaContextTestCase):
    def _index(self) -> dict:
        return build_claim_index(
            ledger=self.store, claim_version_refs=[], created_at=NOW
        )

    def test_agenda_pack_cannot_be_replayed_under_a_connector_plan(self):
        """A connector plan cannot stand in for the Agenda binding.

        The pack's ``compiled_plan_ref`` is the binding id, so even a plan
        built over the same task fails the binding check before any input is
        resolved.  The reverse direction -- a connector-plan pack carrying a
        mandate input -- is rejected by the kind guard instead.
        """

        context = self.context()
        plan = build_reference_fixture_plan(
            task_ref=context["binding"]["task_ref"],
            task_hash=context["binding"]["task_hash"], created_at=NOW,
        )
        materializer = ContextMaterializer(self.store, self.obs, None)
        with self.assertRaises(ContextMaterializerConflict):
            materializer.materialize(
                context["context_pack"], max_rendered_tokens=8000,
                max_rendered_bytes=200_000, compiled_plan=plan,
                claim_index=self._index(), created_at=NOW,
            )
        kind, resolved = ContextMaterializer._plan_binding(plan)
        self.assertEqual(kind, "compiled_plan")
        self.assertEqual(resolved["id"], plan["id"])
        kind, resolved = ContextMaterializer._plan_binding(context["binding"])
        self.assertEqual(kind, "agenda_context_binding")
        self.assertEqual(resolved["id"], context["binding"]["id"])

    def test_unknown_binding_shape_is_rejected(self):
        context = self.context()
        materializer = ContextMaterializer(self.store, self.obs, None)
        with self.assertRaises(ContextMaterializerUnsupported):
            materializer.materialize(
                context["context_pack"], max_rendered_tokens=8000,
                max_rendered_bytes=200_000,
                compiled_plan={"id": "unknown:1", "content_hash": "a" * 64},
                claim_index=self._index(), created_at=NOW,
            )

    def test_binding_substitution_is_rejected(self):
        context = self.context()
        forged = build_agenda_context_binding(
            cycle_ref=context["binding"]["cycle_ref"],
            cycle_hash=context["binding"]["cycle_hash"],
            company_ref=context["binding"]["company_ref"],
            policy_version_ref=context["binding"]["policy_version_ref"],
            policy_version_hash=context["binding"]["policy_version_hash"],
            mandate_version_ref=context["binding"]["mandate_version_ref"],
            mandate_version_hash=context["binding"]["mandate_version_hash"],
            perception_snapshot_ref="perception:other",
            perception_snapshot_hash="9" * 64, created_at=NOW,
        )
        materializer = ContextMaterializer(self.store, self.obs, None)
        with self.assertRaises(ContextMaterializerConflict):
            materializer.materialize(
                context["context_pack"], max_rendered_tokens=8000,
                max_rendered_bytes=200_000, compiled_plan=forged,
                claim_index=None, created_at=NOW,
            )

    def test_agenda_rejects_a_claim_index_body(self):
        context = self.context()
        materializer = ContextMaterializer(self.store, self.obs, None)
        with self.assertRaises(ContextMaterializerConflict):
            materializer.materialize(
                context["context_pack"],
                max_rendered_tokens=8000,
                max_rendered_bytes=200_000,
                compiled_plan=context["binding"],
                claim_index=self._index(),
                created_at=NOW,
            )

    def test_materializer_without_a_spool_refuses_artifacts(self):
        materializer = ContextMaterializer(self.store, self.obs, None)
        with self.assertRaises(ContextMaterializerUnsupported):
            materializer.build_authority_context_pack(
                [{
                    "kind": "artifact", "ref": "artifact-version:x",
                    "hash": "a" * 64, "priority": 1,
                }],
                task_ref="work:x", task_hash="b" * 64,
                compiled_plan_ref="plan:x", compiled_plan_hash="c" * 64,
                claim_index_ref=self._index()["id"],
                claim_index_hash=self._index()["content_hash"],
                claim_index=self._index(), created_at=NOW,
                max_tokens=1000, max_bytes=10_000,
            )

    def test_caller_supplied_mandate_body_is_not_authority(self):
        context = self.context()
        tampered = copy.deepcopy(context["context_pack"])
        entry = tampered["inputs"][0]
        entry["hash"] = "5" * 64
        entry["content_hash"] = content_hash(
            {k: v for k, v in entry.items() if k != "content_hash"}
        )
        tampered["content_hash"] = content_hash(
            {k: v for k, v in tampered.items() if k != "content_hash"}
        )
        materializer = ContextMaterializer(self.store, self.obs, None)
        with self.assertRaises(ContextMaterializerConflict):
            materializer.materialize(
                tampered, max_rendered_tokens=8000, max_rendered_bytes=200_000,
                compiled_plan=context["binding"], claim_index=None,
                created_at=NOW,
            )


class PromptAssemblyTests(AgendaContextTestCase):
    def test_prompt_is_only_wrapper_plus_rendered_context(self):
        context = self.context()
        prompt = build_prompt(context["rendered_text"])
        head, tail = prompt_wrapper()
        self.assertEqual(prompt, head + context["rendered_text"] + tail)
        # The retired manual data path must not reappear in any form.
        self.assertNotIn("MANDATE=", prompt)
        self.assertNotIn("PERCEPTION=", prompt)
        self.assertEqual(prompt.count(context["rendered_text"]), 1)

    def test_prompt_tokens_are_measured_with_the_frozen_tokenizer(self):
        context = self.context()
        prompt = build_prompt(context["rendered_text"])
        head, tail = prompt_wrapper()
        self.assertEqual(
            count_dalton_search_tokens(prompt),
            count_dalton_search_tokens(head + context["rendered_text"] + tail),
        )
        self.assertGreaterEqual(
            count_dalton_search_tokens(prompt),
            context["manifest"]["totals"]["rendered_tokens"],
        )

    def test_empty_render_is_rejected(self):
        with self.assertRaises(Exception):
            build_prompt("")

    def test_candidates_may_only_cite_the_exact_catalog(self):
        context = self.context()
        good = json.dumps({"candidates": [
            {
                "question": f"Question {index}?",
                "answer_criteria": "criteria",
                "features": {
                    "mandate_relevance": 1, "catalyst_urgency": 1,
                    "evidence_staleness": 1, "decision_impact": 1,
                },
                "rationale": "display only",
                "source_refs": ["evidence:evidence-1"],
            }
            for index in range(3)
        ]})
        parsed = parse_candidates(
            good, allowed_source_refs=context["allowed_source_refs"],
            company_ref=context["company_ref"], cycle_id=self.cycle["cycle_id"],
        )
        self.assertEqual(len(parsed), 3)
        self.assertEqual({item["company_ref"] for item in parsed}, {"wanhua"})
        invented = json.loads(good)
        invented["candidates"][0]["source_refs"] = ["evidence:invented"]
        with self.assertRaises(Exception):
            parse_candidates(
                json.dumps(invented),
                allowed_source_refs=context["allowed_source_refs"],
                company_ref=context["company_ref"],
                cycle_id=self.cycle["cycle_id"],
            )


if __name__ == "__main__":
    unittest.main()
