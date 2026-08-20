#!/usr/bin/env python3
"""Run one isolated, policy-authorized SEC ResearchPlan through closure.

The canary keeps every authority database and the raw spool under an explicit
output directory.  It never opens Dalton's live databases, accepts no
credentials, performs only one bounded read from ``data.sec.gov``, and uses
one owner-installed versioned policy to start the plan and commit the exact
deterministic result without per-plan or per-claim human review.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.agenda import AgendaStore
from dalton_core.authority_resolver import ConnectorAuthorityResolver
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_authority_port import ConnectorAuthorityPort
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from dalton_core.connector_transport_executor import ConnectorTransportExecutor
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.research_coordinator import ResearchCoordinatorStore
from dalton_core.research_plan import (
    PLAN_AUTO_START_RULE_REF,
    ResearchPlanAuthority,
    ResearchPlanControlPlane,
)
from dalton_core.research_auto_commit import RULE_REF as AUTO_COMMIT_RULE_REF
from dalton_core.research_plan_closure import ResearchPlanClosureCoordinator
from dalton_core.research_plan_coordinator import ResearchPlanCoordinator
from dalton_core.research_plan_executor import (
    ResearchPlanExecutor,
    sec_connector_identity,
    sec_descriptor_spec,
)
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.sec_authority_harness import (
    PUBLIC_PERMISSIONS,
    MutableClock,
    _PublicAuthorities,
)
from dalton_core.sec_public_adapter import SecPublicHttpAdapter
from dalton_core.store import DaltonStore, content_hash


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _policy(now: datetime, company_ref: str) -> dict:
    return {
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
            "mandate_relevance": 4,
            "catalyst_urgency": 3,
            "evidence_staleness": 2,
            "decision_impact": 4,
        },
        "trial_company_refs": [company_ref],
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


def _register_selected_question(
    agenda: AgendaStore,
    backlog: ResearchQuestionBacklog,
    *,
    now: datetime,
    company_ref: str,
    question: str,
    answer_criteria: str,
) -> tuple[dict, dict]:
    suffix = content_hash(
        {"company_ref": company_ref, "question": question, "now": _wire_time(now)}
    )[:16]
    snapshot = {
        "schema_version": "0.1",
        "snapshot_id": f"perception:sec-plan-canary:{suffix}",
        "generated_at": _wire_time(now),
        "source_kind": "isolated-sec-plan-canary-v1",
        "source_snapshot_hash": content_hash(
            {"source": "isolated-sec-plan-canary", "suffix": suffix}
        ),
        "company": {
            "slug": company_ref,
            "name": company_ref,
            "ticker": company_ref,
        },
        "catalysts": [{"event_key": "sec-canary", "title": "Bounded filing check"}],
        "evidence": [],
        "filings": [],
    }
    snapshot["content_hash"] = content_hash(snapshot)
    agenda.register_perception_snapshot(
        snapshot,
        actor_ref="core",
        idempotency_key=f"perception:{suffix}",
    )
    cycle = agenda.start_cycle(
        f"agenda:sec-plan-canary:{suffix}",
        perception_snapshot_ref=snapshot["snapshot_id"],
        perception_snapshot_hash=snapshot["content_hash"],
        mandate_version_ref="mandate-version:sec-plan-canary:1",
        policy_version_ref="agenda-policy-version:sec-plan-canary:1",
        company_ref=company_ref,
        actor_ref="core",
        cycle_id=f"agenda-cycle:sec-plan-canary:{suffix}",
        idempotency_key=f"cycle:{suffix}",
    )
    agenda.add_candidates(
        cycle["cycle_id"],
        candidates=[
            {
                "candidate_id": f"candidate:sec-plan-canary:{suffix}",
                "company_ref": company_ref,
                "question": question,
                "answer_criteria": answer_criteria,
                "features": {
                    "mandate_relevance": 3,
                    "catalyst_urgency": 2,
                    "evidence_staleness": 1,
                    "decision_impact": 3,
                },
                "rationale": "Validate the first real SEC ResearchPlan tree",
                "source_refs": ["source:sec-edgar"],
            }
        ],
        actor_ref="core",
        idempotency_key=f"candidates:{suffix}",
    )
    decision = agenda.decide_cycle(
        cycle["cycle_id"],
        actor_ref="core",
        decision_id=f"decision:sec-plan-canary:{suffix}",
        idempotency_key=f"decision:{suffix}",
    )
    record = backlog.record_question(
        mandate_version_ref="mandate-version:sec-plan-canary:1",
        company_ref=company_ref,
        question=question,
        answer_criteria=answer_criteria,
        source_refs=["source:sec-edgar"],
        actor_ref="core",
        idempotency_key=f"question:{suffix}",
    )
    backlog.select_question(
        question_ref=record["question_ref"],
        decision_ref=decision["id"],
        actor_ref="core",
        idempotency_key=f"select:{suffix}",
    )
    return decision, record


def _runner_manifest(
    *, descriptor, identity: dict, created_at: str
) -> dict:
    binding = {
        "binding_ref": "runner-binding:sec-public:v1",
        "descriptor_revision_ref": descriptor.revision_ref,
        "descriptor_hash": descriptor.content_hash,
        "adapter_ref": identity["adapter_ref"],
        "adapter_hash": identity["adapter_hash"],
        "source_ref": identity["source_identity"]["source_ref"],
        "source_hash": identity["source_hash"],
        "operation": identity["operation"],
        "input_schema_ref": identity["input_schema_ref"],
        "input_schema_hash": identity["input_schema_hash"],
        "output_schema_ref": identity["output_schema_ref"],
        "output_schema_hash": identity["output_schema_hash"],
        "auth_mode": "none",
        "credential_slot_refs": [],
        "required_permissions": copy.deepcopy(PUBLIC_PERMISSIONS),
        "side_effects": ["read:public-http"],
        "rate_policy_ref": "connector-rate-policy:sec-public",
    }
    payload = {
        "schema_version": "0.1",
        "id": "runner-environment:sec-public:v1",
        "created_at": created_at,
        "runner_runtime_ref": "runner-runtime:sec-public:v1",
        "runner_actor_ref": "runner:research-plan-executor",
        "resolver_ref": "resolver:connector-static:0.1",
        "resolver_version": "0.1",
        "package_manifest_ref": "artifact:runner-packages:sec-public:v1",
        "package_manifest_hash": "9" * 64,
        "bindings": [binding],
    }
    return validate_runner_environment_manifest(
        {**payload, "content_hash": content_hash(payload)}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--issuer-cik", default="789019")
    parser.add_argument("--company-ref", default="company:sec-cik:0000789019")
    parser.add_argument("--form", choices=("10-Q",), default="10-Q")
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument(
        "--policy-owner",
        required=True,
        help="owner installing the isolated versioned policy; must use human: namespace",
    )
    parser.add_argument(
        "--user-agent",
        default="Dalton Research Agent isolated SEC ResearchPlan canary",
    )
    args = parser.parse_args()
    if not args.policy_owner.startswith("human:"):
        raise SystemExit("--policy-owner must use the human: namespace")

    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise SystemExit("--output-dir must not already exist")
    output_dir.mkdir(parents=True, mode=0o700)
    os.chmod(output_dir, 0o700)

    now = datetime.now(timezone.utc)
    created_at = _wire_time(now)
    clock = MutableClock(now)
    core = DaltonStore(output_dir / "core.sqlite")
    catalog = None
    connector_records = None
    staging = None
    review = None
    try:
        observability = ObservabilityStore(core)
        agenda = AgendaStore(core)
        backlog = ResearchQuestionBacklog(core)
        plans = ResearchPlanAuthority(core)
        scheduler = Scheduler(connection=core.connection)
        control = ResearchPlanControlPlane(plans, backlog, observability, scheduler)

        effective_from = _wire_time(now - timedelta(days=1))
        effective_until = _wire_time(now + timedelta(days=30))
        agenda.create_policy(
            _policy(now, args.company_ref),
            effective_from=effective_from,
            effective_until=effective_until,
            actor_ref=args.policy_owner,
            version_id="agenda-policy-version:sec-plan-canary:1",
            idempotency_key="agenda-policy:sec-plan-canary:1",
        )
        agenda.create_mandate(
            "mandate:sec-plan-canary",
            objective="Validate one real, bounded SEC ResearchPlan task tree",
            scope_refs=[args.company_ref],
            constraints={"mode": "isolated_public_read_only_canary"},
            success_criteria={"policy_authorized_low_risk_loop": True},
            effective_from=effective_from,
            effective_until=effective_until,
            actor_ref=args.policy_owner,
            version_id="mandate-version:sec-plan-canary:1",
            idempotency_key="mandate:sec-plan-canary:1",
        )
        agenda.set_pause(
            False,
            reason="owner authorized isolated SEC ResearchPlan canary",
            actor_ref=args.policy_owner,
            version_id="agenda-control-version:sec-plan-canary:1",
            idempotency_key="agenda-resume:sec-plan-canary:1",
        )
        question = f"Which official {args.form} filings fall inside the bounded date window?"
        answer_criteria = "Return the official SEC filing list and verify the filing count."
        decision, question_record = _register_selected_question(
            agenda,
            backlog,
            now=now,
            company_ref=args.company_ref,
            question=question,
            answer_criteria=answer_criteria,
        )
        created = plans.create_plan(
            question_ref=question_record["question_ref"],
            question_version_ref=question_record["question_version_ref"],
            decision_ref=decision["id"],
            issuer_cik=args.issuer_cik,
            form=args.form,
            filing_date_from=args.date_from,
            filing_date_to=args.date_to,
            actor_ref="core:planner",
            idempotency_key="create-plan:sec-plan-canary:1",
        )
        active = core.active_policy()
        governance_policy = dict(active["policy"])
        governance_policy["research_plan_auto_start"] = {
            "enabled": True,
            "rules": [PLAN_AUTO_START_RULE_REF],
        }
        governance_policy["research_candidate_auto_commit"] = {
            "enabled": True,
            "rules": [AUTO_COMMIT_RULE_REF],
            "max_records": 20,
        }
        installed_policy = core.create_policy(
            governance_policy,
            policy_version_id="policy:isolated-autonomous-sec:v2",
            version_number=2,
            prior_version_ref=active["policy_version_id"],
            actor_ref=args.policy_owner,
            change_reason="authorize isolated autonomous public SEC research",
            activate=True,
        )
        plan_authorization = plans.authorize_plan_by_policy(
            plan_version_ref=created["plan_version_ref"],
            idempotency_key="authorize-plan:sec-plan-canary:1",
        )
        control.start_plan(
            plan_version_ref=created["plan_version_ref"],
            actor_ref="core:planner",
            idempotency_key="start-plan:sec-plan-canary:1",
        )
        plan_wire = plans.plan_version(created["plan_version_ref"])

        connectors = ConnectorStore(core, clock=clock)
        authorities = _PublicAuthorities()
        catalog = CapabilityCatalog(
            output_dir / "capability.sqlite",
            clock=clock,
            approval_resolver=authorities.approval,
            policy_resolver=authorities.policy,
        )
        sec_template = load_packaged_connector_inventory()["templates"]["sec"]
        template_identity = sec_connector_identity(sec_template)
        descriptor = catalog.publish(
            sec_descriptor_spec(
                sec_template,
                PUBLIC_PERMISSIONS,
                created_at,
            )
        )
        manifest = _runner_manifest(
            descriptor=descriptor,
            identity=template_identity,
            created_at=created_at,
        )
        adapter = SecPublicHttpAdapter(user_agent=args.user_agent, clock=clock)
        binding = manifest["bindings"][0]
        static_resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: adapter},
            {
                binding["binding_ref"]: lambda params: set(params)
                == {"issuer", "form", "date_from", "date_to", "limit"}
            },
        )
        gate = ConnectorRunnerAdmissionGate(
            scheduler=scheduler,
            catalog=catalog,
            connectors=connectors,
            resolver=static_resolver,
            visibility_scopes=["research"],
            clock=clock,
        )
        journal = RunnerJournal(core, clock=clock)
        spool = RawSpool(output_dir / "raw-spool", max_total_bytes=6_000_000)
        authority_port = ConnectorAuthorityPort(
            connectors=connectors,
            observability=observability,
            scheduler=scheduler,
        )
        transport = ConnectorTransportExecutor(
            gate=gate,
            journal=journal,
            spool=spool,
            authority=authority_port,
            connector_reader=connectors,
            clock=clock,
        )
        connector_records = ResearchCoordinatorStore(
            output_dir / "research-coordinator.sqlite"
        )
        coordinator = ResearchPlanCoordinator(
            plan=plans,
            scheduler=scheduler,
            connector_records=connector_records,
        )
        staging_path = output_dir / "candidate-staging.sqlite"
        staging = CandidateStagingStore(staging_path)
        resolver = ConnectorAuthorityResolver(
            core=core,
            connectors=connectors,
            observability=observability,
            scheduler=scheduler,
            coordinator=connector_records,
            artifact_reader=lambda artifact: spool.read_object(
                artifact["artifact_content_hash"]
            ),
            runner_journal=journal,
        )
        executor = ResearchPlanExecutor(
            plan=plans,
            scheduler=scheduler,
            connector_records=connector_records,
            coordinator=coordinator,
            connectors=connectors,
            catalog=catalog,
            transport=transport,
            resolver=resolver,
            staging=staging,
            clock=clock,
            permissions=copy.deepcopy(PUBLIC_PERMISSIONS),
            policy_resolver=authorities.policy,
            principal_ref="principal:worker-1",
            runner_environment_hash=manifest["content_hash"],
            actor_ref=manifest["runner_actor_ref"],
        )

        outcomes = []
        for _ in range(8):
            outcome = executor.run_once(plan_version_ref=plan_wire["id"])
            outcomes.append(outcome)
            if outcome["status"] in {"complete", "blocked"}:
                break
            if outcome["status"] not in {"admitted", "succeeded"}:
                break
        if outcomes[-1]["status"] != "complete":
            raise RuntimeError(f"ResearchPlan canary did not complete: {outcomes[-1]}")

        review = HumanReviewAuthority(staging_path)
        candidates = review.list_candidates(limit=10)
        if len(candidates) != 1:
            raise RuntimeError("ResearchPlan canary must produce exactly one candidate")
        candidate = candidates[0]
        claim = candidate["claim"]
        evidence = candidate["evidence"]
        promotion = core.commit_policy_candidate(
            evidence=evidence,
            claim=claim,
            idempotency_key="policy-ledger:sec-plan-canary:1",
        )
        closure = ResearchPlanClosureCoordinator(
            plan=plans,
            backlog=backlog,
            coordinator=coordinator,
            review=review,
        ).close_policy_authorized(
            plan_version_ref=plan_wire["id"],
            authorization=promotion["authorization"],
        )
        tree = coordinator.tree_status(plan_wire["id"])
        result = {
            "status": "autonomous-closed",
            "generated_at": _wire_time(datetime.now(timezone.utc)),
            "output_dir": str(output_dir),
            "plan": {
                "ref": plan_wire["id"],
                "hash": plan_wire["content_hash"],
                "authorization_ref": plan_authorization["authorization"]["id"],
                "policy_version_ref": installed_policy["policy_version_id"],
                "policy_owner": args.policy_owner,
                "issuer_cik": plan_wire["execution_scope"]["parameters"]["issuer_cik"],
                "form": plan_wire["execution_scope"]["parameters"]["form"],
                "date_from": plan_wire["execution_scope"]["parameters"]["filing_date_from"],
                "date_to": plan_wire["execution_scope"]["parameters"]["filing_date_to"],
            },
            "outcomes": outcomes,
            "tree": [
                {
                    "ordinal": index,
                    "stage": item["stage"],
                    "attempt_state": item["attempt_state"],
                    "admission_state": item["admission_state"],
                }
                for index, item in enumerate(tree["nodes"], start=1)
            ],
            "candidate": {
                "claim_ref": claim["id"],
                "claim_hash": claim["content_hash"],
                "subject_ref": claim["subject_ref"],
                "metric_or_aspect": claim["metric_or_aspect"],
                "period": claim["period"],
                "basis": claim["basis"],
                "normalized_statement": claim["normalized_statement"],
                "value": claim["value"],
                "unit": claim["unit"],
                "scale": claim["scale"],
                "currency": claim["currency"],
                "semantic_verification_status": claim["semantic_verification_status"],
                "source_ref": evidence["source_ref"],
                "source_envelope_ref": evidence["source_envelope_ref"],
                "artifact_refs": evidence["artifact_refs"],
            },
            "formal_ledger_counts": {
                table: core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("evidence_versions", "claim_versions", "thesis_versions")
            },
            "promotion": {
                "authorization_ref": promotion["authorization"]["id"],
                "evidence_version_ref": promotion["evidence_version_ref"],
                "claim_version_ref": promotion["claim_version_ref"],
                "relation_ref": promotion["relation_ref"],
            },
            "closure": closure,
            "human_gate_counts": {
                "plan_approvals": core.connection.execute(
                    "SELECT COUNT(*) FROM research_plan_approvals"
                ).fetchone()[0],
                "claim_reviews": review.connection.execute(
                    "SELECT COUNT(*) FROM human_review_decisions"
                ).fetchone()[0],
            },
            "integrity": {
                "core": core.connection.execute("PRAGMA integrity_check").fetchone()[0],
                "staging": review.connection.execute("PRAGMA integrity_check").fetchone()[0],
                "coordinator": connector_records.connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0],
                "capability": catalog.connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0],
            },
            "network": {
                "host": "data.sec.gov",
                "auth_mode": "none",
                "side_effects": ["read:public-http"],
                "user_agent": args.user_agent,
            },
        }
        result_path = output_dir / "result.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(result_path, 0o600)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if review is not None:
            review.close()
        if staging is not None:
            staging.close()
        if connector_records is not None:
            connector_records.close()
        if catalog is not None:
            catalog.close()
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
