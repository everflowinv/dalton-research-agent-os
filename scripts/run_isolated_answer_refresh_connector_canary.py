#!/usr/bin/env python3
"""Run the S5 observed-refresh isolation canary against the real connector stack.

The canary uses an in-process synthetic SEC transport, but every authority
boundary after that transport is the production implementation: Connector,
Artifact, SourceEnvelope, resolver, deterministic verification,
CandidateStaging, bounded outcome, and answer-refresh receipts.  It opens no
live database, makes no external network or paid-model call, and performs no
formal Evidence/Claim/Thesis promotion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.answer_routing import (
    AnswerRefreshControlPlane,
    AnswerRoutingConflict,
)
from dalton_core.connector_authority_port import ConnectorCompletionReceiptReader
from tests.test_answer_routing import AnswerRoutingTests, QUESTION
from tests.test_research_plan_executor import PlanExecutorHarness


ACTOR = "human:answer-refresh-connector-canary"
WORKER = "worker:answer-refresh-connector-canary"
FORMAL_TABLES = ("evidence_versions", "claim_versions", "thesis_versions")
STAGE_BINDING_FIELDS = (
    "candidate_evidence_ref",
    "candidate_evidence_hash",
    "candidate_claim_ref",
    "candidate_claim_hash",
)


def _formal_counts(connection: Any) -> dict[str, int]:
    return {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in FORMAL_TABLES
    }


def _record(
    connection: Any,
    table: str,
    column: str = "record_json",
) -> dict[str, Any]:
    row = connection.execute(f"SELECT {column} FROM {table}").fetchone()
    if row is None:
        raise RuntimeError(f"isolated canary expected one {table} record")
    return json.loads(row[0])


def run() -> dict[str, Any]:
    answers_fixture = AnswerRoutingTests(
        "test_stale_only_question_routes_to_exact_bounded_refresh"
    )
    answers_fixture.setUp()
    connector = PlanExecutorHarness(
        suffix="answer-refresh-connector-canary",
        company_facts=True,
    )
    try:
        subject, _, loop = answers_fixture.refresh_ready()
        routed = answers_fixture.answers.route(
            subject_binding=subject,
            question=QUESTION,
        )
        decision = routed["decision"]
        if decision["route"] != "answer_after_refresh":
            raise RuntimeError("stale-only question did not admit one bounded refresh")
        dispatched = answers_fixture.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref=ACTOR,
        )
        dispatch = dispatched["dispatch"]

        execution_outcomes = connector.run_to_complete()
        if [item["status"] for item in execution_outcomes] != [
            "admitted",
            "admitted",
            "admitted",
            "complete",
        ]:
            raise RuntimeError("connector-to-CandidateStaging tree did not complete")

        stage_result = _record(
            connector.staging.connection,
            "candidate_stage_requests",
            "result_json",
        )
        evidence = _record(
            connector.staging.connection,
            "candidate_evidence_versions",
        )
        claim = _record(
            connector.staging.connection,
            "candidate_claim_versions",
        )
        stage_binding = {
            field: stage_result[field] for field in STAGE_BINDING_FIELDS
        }
        receipts = ConnectorCompletionReceiptReader(
            connectors=connector.connectors,
            observability=connector.observability,
        )
        source = receipts.get_source_envelope(evidence["source_envelope_ref"])
        if (
            source is None
            or source["content_hash"] != evidence["source_envelope_hash"]
        ):
            raise RuntimeError("candidate evidence does not bind the connector source")
        artifact = receipts.get_artifact_version(
            source["raw_artifact_version_ref"]
        )
        if {
            "ref": artifact["id"],
            "hash": artifact["content_hash"],
        } not in evidence["artifact_refs"]:
            raise RuntimeError("candidate evidence does not bind the raw artifact")

        work_order_ref = dispatch["work_order_ref"]
        lease = answers_fixture.scheduler.claim(
            WORKER,
            work_order_id=work_order_ref,
        )
        if lease is None:
            raise RuntimeError("answer-refresh WorkOrder was not claimable")
        result = {
            "schema_version": "0.1",
            "id": "result-envelope:answer-refresh-connector-canary",
            "created_at": "2026-08-25T12:00:00.000000+00:00",
            "work_order_ref": work_order_ref,
            "invocation_ref": "execution:answer-refresh-connector-canary",
            "status": "succeeded",
            "outputs": {
                "matches": [{
                    "source_location": (
                        source["id"] + "#quarterly-revenue-yoy-growth"
                    ),
                    "candidate_evidence_ref": evidence["id"],
                    "candidate_evidence_hash": evidence["content_hash"],
                    "candidate_claim_ref": claim["id"],
                    "candidate_claim_hash": claim["content_hash"],
                }],
                "source_envelope_ref": source["id"],
                "source_envelope_hash": source["content_hash"],
            },
            "actual_side_effects": ["read:public-http"],
            "usage_refs": [],
            "artifact_refs": [artifact["id"]],
            "error": None,
            "metadata": {
                "mode": "isolated-connector-to-candidate-staging-canary",
                "child_plan_version_ref": connector.plan_wire["id"],
                "child_plan_version_hash": connector.plan_wire["content_hash"],
            },
        }
        completed = answers_fixture.scheduler.complete(
            work_order_ref,
            1,
            WORKER,
            lease["lease_token"],
            result,
            idempotency_key="answer-refresh-connector-canary:complete",
        )
        formal = answers_fixture.scheduler.formal_result(work_order_ref)
        if (
            formal is None
            or formal["terminal_state"] != "succeeded"
            or formal["result_envelope_hash"]
            != completed["result_envelope_hash"]
        ):
            raise RuntimeError("bounded refresh formal result binding drifted")

        before_formal = _formal_counts(answers_fixture.store.connection)
        untrusted_source_rejected = False
        without_connector_authority = AnswerRefreshControlPlane(
            answers_fixture.answers,
            answers_fixture.bounded_control,
            candidate_staging=connector.staging,
        )
        try:
            without_connector_authority.finalize(
                dispatch["id"],
                candidate_staging_binding=stage_binding,
                actor_ref=ACTOR,
            )
        except AnswerRoutingConflict:
            untrusted_source_rejected = True
        if not untrusted_source_rejected:
            raise RuntimeError("observed refresh trusted a caller-only SourceEnvelope")

        refresh = AnswerRefreshControlPlane(
            answers_fixture.answers,
            answers_fixture.bounded_control,
            candidate_staging=connector.staging,
            connector_receipts=receipts,
        )
        finalized = refresh.finalize(
            dispatch["id"],
            candidate_staging_binding=stage_binding,
            actor_ref=ACTOR,
        )
        replay = refresh.finalize(
            dispatch["id"],
            candidate_staging_binding=stage_binding,
            actor_ref=ACTOR,
        )
        receipt = finalized["outcome_receipt"]
        if (
            finalized["status"] != "fresh"
            or replay["status"] != "duplicate"
            or receipt["outcome_kind"] != "observed"
            or receipt["terminal_state"] != "evidence_observed_for_review"
            or receipt["candidate_staging_binding"] is None
            or _formal_counts(answers_fixture.store.connection) != before_formal
        ):
            raise RuntimeError("observed refresh did not remain candidate-only")
        if (
            answers_fixture.bounded.terminal(loop["id"])["id"]
            != receipt["terminal_ref"]
        ):
            raise RuntimeError("bounded terminal does not bind the refresh receipt")

        connector_formal = _formal_counts(connector.core.connection)
        if any(connector_formal.values()):
            raise RuntimeError("connector canary promoted formal research authority")
        integrity = {
            "answer_core": answers_fixture.store.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "connector_core": connector.core.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "candidate_staging": connector.staging.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
        }
        if set(integrity.values()) != {"ok"}:
            raise RuntimeError("isolated canary authority failed integrity_check")

        return {
            "status": "passed",
            "mode": "isolated-synthetic-transport-real-connector-authorities",
            "route": decision["route"],
            "dispatch_ref": dispatch["id"],
            "dispatch_hash": dispatch["content_hash"],
            "connector_plan_version_ref": connector.plan_wire["id"],
            "connector_plan_version_hash": connector.plan_wire["content_hash"],
            "connector_execution_statuses": [
                item["status"] for item in execution_outcomes
            ],
            "connector_physical_attempt_count": int(
                connector.connectors.connection.execute(
                    "SELECT COUNT(*) FROM connector_physical_attempts"
                ).fetchone()[0]
            ),
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "raw_artifact_version_ref": artifact["id"],
            "raw_artifact_version_hash": artifact["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "candidate_semantic_verification_status": claim[
                "semantic_verification_status"
            ],
            "candidate_stage_request_count": connector.staging.counts()[
                "candidate_stage_requests"
            ],
            "untrusted_source_rejected": untrusted_source_rejected,
            "outcome_receipt_ref": receipt["id"],
            "outcome_receipt_hash": receipt["content_hash"],
            "outcome_kind": receipt["outcome_kind"],
            "terminal_state": receipt["terminal_state"],
            "replay_status": replay["status"],
            "formal_answer_authority_counts_unchanged": True,
            "formal_connector_authority_counts": connector_formal,
            "external_network_calls": 0,
            "paid_model_calls": 0,
            "live_database_writes": 0,
            "integrity_check": integrity,
            "scheduler_completion_status": formal["terminal_state"],
        }
    finally:
        connector.close()
        answers_fixture.doCleanups()


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
