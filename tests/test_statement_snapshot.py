"""StatementSnapshot v1 authority, Decimal verifier, and loop integration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.scheduler import Scheduler
from dalton_core.statement_snapshot import (
    STATEMENT_SNAPSHOT_CAPABILITY,
    STATEMENT_SNAPSHOT_OPERATION,
    STATEMENT_SNAPSHOT_OUTPUT_CONTRACT,
    STATEMENT_SNAPSHOT_PERMISSION,
    STATEMENT_SNAPSHOT_RUNTIME,
    STATEMENT_SNAPSHOT_VERIFIER,
    StatementSnapshotAuthority,
    StatementSnapshotConflict,
    StatementSnapshotValidationError,
    StatementSnapshotWorker,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_connector import assert_wire_schema


ACCESSION = "0000789019-26-000054"
PERIOD_END = "2026-03-31"
FILED = "2026-04-29"


def concept_set_rows() -> list[dict]:
    return [
        {"line_item_ref": "assets", "concepts": ["Assets"]},
        {"line_item_ref": "liabilities", "concepts": ["Liabilities"]},
        {
            "line_item_ref": "stockholders-equity",
            "concepts": [
                "StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            ],
        },
    ]


def equations() -> list[dict]:
    return [{
        "equation_ref": "assets-equal-liabilities-plus-equity",
        "left_line_items": ["assets"],
        "right_line_items": ["liabilities", "stockholders-equity"],
        "tolerance": "0",
    }]


def company_facts_payload(*, assets: int = 1000) -> dict:
    def concept(label: str, value: int) -> dict:
        return {
            "label": label,
            "description": f"Exact {label} fact",
            "units": {"USD": [{
                "end": PERIOD_END,
                "val": value,
                "accn": ACCESSION,
                "fy": 2026,
                "fp": "Q1",
                "form": "10-Q",
                "filed": FILED,
                "frame": "CY2026Q1I",
            }]},
        }

    return {
        "cik": 789019,
        "entityName": "Example Corporation",
        "facts": {"us-gaap": {
            "Assets": concept("Assets", assets),
            "Liabilities": concept("Liabilities", 600),
            "StockholdersEquity": concept("Stockholders' Equity", 400),
            "IssuerSpecificAssets": concept("Assets", assets),
        }},
    }


def raw(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class StatementSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.artifacts: dict[str, dict] = {}
        self.authority = StatementSnapshotAuthority(
            self.store, artifact_resolver=lambda ref: self.artifacts[ref]
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)
        self.concept_set = self.authority.publish_concept_set(
            "statement-concept-set:us-gaap:balance-sheet-core",
            line_items=concept_set_rows(), equations=equations(), actor_ref="human:owner",
        )

    def materialize(
        self,
        payload: dict | None = None,
        *,
        artifact_access_class: str = "internal",
        artifact_kind: str = "connector_raw_response",
        **overrides: object,
    ) -> dict:
        body = raw(payload or company_facts_payload())
        content_digest = hashlib.sha256(body).hexdigest()
        artifact_ref = "artifact-version:sec-companyfacts:example"
        artifact = {
            "schema_version": "0.2",
            "id": artifact_ref,
            "artifact_content_hash": content_digest,
            "size_bytes": len(body),
            "kind": artifact_kind,
            "media_type": "application/sec-companyfacts+json",
            "access_class": artifact_access_class,
        }
        artifact["content_hash"] = content_hash(artifact)
        self.artifacts[artifact_ref] = artifact
        parameters = {
            "raw_payload": body,
            "source_artifact_version_ref": artifact_ref,
            "source_artifact_version_hash": artifact["content_hash"],
            "source_content_hash": content_digest,
            "concept_set_version_ref": self.concept_set["id"],
            "concept_set_version_hash": self.concept_set["content_hash"],
            "issuer_cik": "0000789019",
            "accession": ACCESSION,
            "form": "10-Q",
            "period_end": PERIOD_END,
        }
        parameters.update(overrides)
        return self.authority.materialize_snapshot(**parameters)

    def test_human_concept_set_is_versioned_and_immutable(self) -> None:
        duplicate = self.authority.publish_concept_set(
            "statement-concept-set:us-gaap:balance-sheet-core",
            line_items=concept_set_rows(), equations=equations(), actor_ref="human:owner",
        )
        self.assertEqual(duplicate["status"], "duplicate")
        with self.assertRaises(StatementSnapshotValidationError):
            self.authority.publish_concept_set(
                "statement-concept-set:model-candidate",
                line_items=concept_set_rows(), equations=equations(), actor_ref="model:planner",
            )
        revised_rows = concept_set_rows()
        revised_rows[2]["concepts"].append("PartnersCapital")
        revised = self.authority.publish_concept_set(
            "statement-concept-set:us-gaap:balance-sheet-core",
            line_items=revised_rows,
            equations=equations(), actor_ref="human:owner",
            prior_version_ref=self.concept_set["id"],
        )
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["prior_version_ref"], self.concept_set["id"])
        assert_wire_schema(
            self,
            "statement-concept-set-version.schema.json",
            {key: value for key, value in revised.items() if key != "status"},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE statement_concept_set_versions SET actor_ref='human:other' WHERE version_id=?",
                (self.concept_set["id"],),
            )

    def test_exact_artifact_materializes_flat_decimal_rows_once(self) -> None:
        snapshot = self.materialize()
        self.assertEqual(snapshot["status"], "fresh")
        self.assertEqual(snapshot["verification_status"], "verified")
        assert_wire_schema(
            self,
            "statement-snapshot-version.schema.json",
            {key: value for key, value in snapshot.items() if key != "status"},
        )
        self.assertEqual(
            snapshot["reconciliations"],
            [{
                "equation_ref": "assets-equal-liabilities-plus-equity",
                "left_value": "1000", "right_value": "1000",
                "difference": "0", "tolerance": "0", "status": "passed",
            }],
        )
        self.assertEqual(
            [row["value"] for row in snapshot["fact_rows"]], ["1000", "600", "400"]
        )
        duplicate = self.materialize()
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["id"], snapshot["id"])
        selected = self.authority.fact_rows(
            snapshot["id"], ["stockholders-equity", "assets"]
        )
        self.assertEqual([row["line_item_ref"] for row in selected], [
            "stockholders-equity", "assets",
        ])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM statement_snapshot_versions"
            ).fetchone()[0],
            1,
        )

    def test_hash_ambiguity_imbalance_and_fuzzy_label_fail_closed(self) -> None:
        with self.assertRaises(StatementSnapshotConflict):
            self.materialize(source_content_hash="0" * 64)
        with self.assertRaises(StatementSnapshotConflict):
            self.materialize(artifact_access_class="restricted")
        with self.assertRaises(StatementSnapshotConflict):
            self.materialize(artifact_kind="deliverable")

        ambiguous = company_facts_payload()
        ambiguous["facts"]["us-gaap"]["Assets"]["units"]["USD"].append({
            **ambiguous["facts"]["us-gaap"]["Assets"]["units"]["USD"][0],
            "val": 999,
        })
        with self.assertRaises(StatementSnapshotConflict):
            self.materialize(ambiguous)

        with self.assertRaises(StatementSnapshotConflict):
            self.materialize(company_facts_payload(assets=1001))

        fuzzy_only = company_facts_payload()
        del fuzzy_only["facts"]["us-gaap"]["Assets"]
        with self.assertRaises(StatementSnapshotValidationError):
            self.materialize(fuzzy_only)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM statement_snapshot_versions"
            ).fetchone()[0],
            0,
        )


class StatementSnapshotLoopIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.observability = ObservabilityStore(self.store)
        self.scheduler = Scheduler(connection=self.store.connection)
        self.artifacts: dict[str, dict] = {}
        self.snapshots = StatementSnapshotAuthority(
            self.store, artifact_resolver=lambda ref: self.artifacts[ref]
        )
        self.worker = StatementSnapshotWorker(self.snapshots)
        self.agenda = AgendaStore(self.store)
        self.backlog = ResearchQuestionBacklog(self.store)
        self.planner = BoundedPlannerAuthority(self.store)
        self.control = BoundedPlannerControlPlane(
            self.planner, self.observability, self.scheduler
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)

    def test_admitted_snapshot_probe_uses_existing_scheduler_and_forms_observed_outcome(self) -> None:
        mandate = self.agenda.create_mandate(
            "mandate:statement-snapshot",
            objective="Verify balance-sheet totals from one exact accession",
            scope_refs=["example"], constraints={"mode": "development_candidate"},
            success_criteria={"decimal_tie_out_required": True},
            effective_from="2026-08-23T00:00:00+00:00",
            effective_until="2026-09-23T00:00:00+00:00",
            actor_ref="human:owner", version_id="mandate-version:statement-snapshot:1",
            idempotency_key="statement-snapshot:mandate:1",
        )
        question = self.backlog.record_question(
            mandate_version_ref=mandate["id"], company_ref="example",
            question="Does the exact filing balance sheet reconcile?",
            answer_criteria="Return only exact-accession Decimal facts and tie-outs.",
            source_refs=["source:sec-edgar"], actor_ref="core",
            idempotency_key="statement-snapshot:question:1",
        )
        concept_set = self.snapshots.publish_concept_set(
            "statement-concept-set:us-gaap:balance-sheet-core",
            line_items=concept_set_rows(), equations=equations(), actor_ref="human:owner",
        )
        body = raw(company_facts_payload())
        content_digest = hashlib.sha256(body).hexdigest()
        artifact_ref = "artifact-version:sec-companyfacts:example"
        artifact = {
            "schema_version": "0.2",
            "id": artifact_ref,
            "artifact_content_hash": content_digest,
            "size_bytes": len(body),
            "kind": "connector_raw_response",
            "media_type": "application/sec-companyfacts+json",
            "access_class": "internal",
        }
        artifact["content_hash"] = content_hash(artifact)
        self.artifacts[artifact_ref] = artifact
        parameters = {
            "source_ref": "source:sec-edgar",
            "locator": f"sec:filing:{ACCESSION}#companyfacts",
            "query_terms": [item["line_item_ref"] for item in concept_set["line_items"]],
            "source_artifact_version_ref": artifact_ref,
            "source_artifact_version_hash": artifact["content_hash"],
            "source_content_hash": content_digest,
            "concept_set_version_ref": concept_set["id"],
            "concept_set_version_hash": concept_set["content_hash"],
            "issuer_cik": "0000789019",
            "accession": ACCESSION,
            "form": "10-Q",
            "period_end": PERIOD_END,
            "prior_snapshot_version_ref": None,
        }
        template = self.planner.publish_probe_template(
            "probe-template:statement-snapshot:balance-sheet",
            capability_ref=STATEMENT_SNAPSHOT_CAPABILITY,
            operation=STATEMENT_SNAPSHOT_OPERATION,
            runtime_profile_ref=STATEMENT_SNAPSHOT_RUNTIME,
            parameter_contract={
                "allowed_fields": list(parameters),
                "required_fields": list(parameters),
                "constants": {
                    "source_ref": "source:sec-edgar",
                    "accession": ACCESSION,
                    "concept_set_version_ref": concept_set["id"],
                    "concept_set_version_hash": concept_set["content_hash"],
                },
            },
            output_contract_ref=STATEMENT_SNAPSHOT_OUTPUT_CONTRACT,
            verifier_ref=STATEMENT_SNAPSHOT_VERIFIER,
            permission_scope=STATEMENT_SNAPSHOT_PERMISSION,
            declared_side_effects=[],
            cost={"cost_units": 1, "max_attempts": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        loop = self.planner.create_loop(
            "bounded-loop:statement-snapshot:example",
            question_version_ref=question["question_version_ref"],
            template_bindings=[{
                "coverage_item_ref": "balance-sheet-snapshot",
                "template_version_ref": template["id"],
                "parameters": parameters,
            }],
            required_coverage_items=["balance-sheet-snapshot"],
            budget={"max_rounds": 1, "max_cost_units": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        proposal = self.planner.propose_next_capital_lease(loop["id"])
        admitted = self.control.admit_proposal(proposal["id"])
        round_wire = admitted["round"]
        row = self.store.connection.execute(
            "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
            (round_wire["work_order_ref"],),
        ).fetchone()
        work_order = json.loads(row["work_order_json"])
        lease = self.scheduler.claim(
            "worker:statement-snapshot", work_order_id=round_wire["work_order_ref"]
        )
        outputs = self.worker.execute(work_order, body)
        assert_wire_schema(
            self, "statement-snapshot-probe-output.schema.json", outputs
        )
        result = {
            "schema_version": "0.1",
            "id": "result:statement-snapshot:1",
            "created_at": "2026-08-23T12:00:00.000000+00:00",
            "work_order_ref": round_wire["work_order_ref"],
            "invocation_ref": "invocation:statement-snapshot:1",
            "status": "succeeded", "outputs": outputs,
            "actual_side_effects": [], "usage_refs": [], "artifact_refs": [],
            "error": None, "metadata": {"local_decimal_worker": True},
        }
        completed = self.scheduler.complete(
            round_wire["work_order_ref"], 1, "worker:statement-snapshot",
            lease["lease_token"], result,
            idempotency_key="statement-snapshot:complete:1",
        )
        self.assertEqual(completed["work_state"], "succeeded")
        outcome = self.control.record_outcome(round_wire["id"])
        self.assertEqual(outcome["outcome"]["outcome_kind"], "observed")
        self.assertEqual(outcome["outcome"]["formal_claim_refs"], [])
        self.assertEqual(outcome["manifest"]["entries"][0]["match_count"], 1)
        snapshot_ref = outputs["matches"][0]["statement_snapshot_ref"]
        self.assertEqual(
            self.snapshots.fact_rows(snapshot_ref, ["assets"])[0]["value"], "1000"
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
