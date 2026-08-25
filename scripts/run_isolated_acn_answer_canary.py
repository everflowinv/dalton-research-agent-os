#!/usr/bin/env python3
"""Replay the S4 ACN answer router entirely inside an in-memory Core.

The harness consumes the recorded ACN SEC evidence manifests already checked
into the repository.  It performs no connector or model call, never opens a
live database, and proves that answer routing leaves every authority byte
unchanged.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.agenda import AgendaStore
from dalton_core.answer_routing import (
    AnswerRoutingAuthority,
    AnswerRoutingConflict,
)
from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
from dalton_core.observability import ObservabilityStore
from dalton_core.research_plan import (
    ResearchPlanAuthority,
    ResearchPlanControlPlane,
)
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from run_isolated_us_it_services_evidence_canary import (
    DEFAULT_BASE,
    DEFAULT_MANIFEST,
    seed_acn_industry_authority,
)


ACTOR = "human:isolated-acn-answer-canary"
COMPANY_REF = "company:sec-cik:0001467373"
FRESH_AT = "2026-08-25T07:00:00.000000+00:00"
STALE_AT = "2026-09-24T07:00:00.000000+00:00"
QUESTION = (
    "What were Accenture's Q3 FY2026 total new bookings and local-currency "
    "bookings growth?"
)
ANSWER_CRITERIA = (
    "Return the formal total-bookings and local-currency bookings-growth "
    "ClaimVersions supported by the recorded SEC exhibit."
)


def _agenda_policy() -> dict[str, Any]:
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
        "trial_company_refs": [COMPANY_REF],
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


def _perception() -> dict[str, Any]:
    wire = {
        "schema_version": "0.1",
        "snapshot_id": "perception:isolated-acn-answer-canary:1",
        "generated_at": FRESH_AT,
        "source_kind": "recorded-acn-sec-answer-canary-v1",
        "source_snapshot_hash": content_hash({
            "source": "recorded-acn-sec-answer-canary-v1",
            "accession": "0001467373-26-000031",
        }),
        "company": {
            "slug": COMPANY_REF,
            "name": "Accenture plc",
            "ticker": "ACN",
        },
        "catalysts": [{
            "event_key": "acn-q3-fy2026",
            "title": "Accenture Q3 FY2026 earnings",
        }],
        "evidence": [{
            "evidence_key": "acn-q3fy26-release",
            "claim": "Recorded SEC exhibit authority is available.",
        }],
        "filings": [{
            "accession_no": "0001467373-26-000031",
            "form": "8-K",
        }],
    }
    wire["content_hash"] = content_hash(wire)
    return wire


def _table_counts(store: DaltonStore) -> dict[str, int]:
    return {
        row["name"]: store.connection.execute(
            f"SELECT COUNT(*) FROM {row['name']}"
        ).fetchone()[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    }


def _authority_fingerprint(store: DaltonStore) -> str:
    return content_hash({"sqlite_dump": list(store.connection.iterdump())})


def _read_only_call(
    store: DaltonStore, call: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    before_counts = _table_counts(store)
    before_changes = store.connection.total_changes
    before_fingerprint = _authority_fingerprint(store)
    result = call()
    after_fingerprint = _authority_fingerprint(store)
    after_changes = store.connection.total_changes
    after_counts = _table_counts(store)
    if (
        before_counts != after_counts
        or before_changes != after_changes
        or before_fingerprint != after_fingerprint
    ):
        raise RuntimeError("answer route mutated isolated Core authority")
    return result


def _publish_thesis(
    seed: dict[str, Any], mandate: dict[str, Any]
) -> dict[str, Any]:
    candidate_params = copy.deepcopy(seed["base_manifest"]["candidate"])
    candidate = seed["coverage"].propose_thesis_admission(
        mandate_version_ref=mandate["id"],
        mandate_version_hash=mandate["content_hash"],
        driver_pack_version_ref=seed["driver_pack_v2"]["id"],
        driver_pack_version_hash=seed["driver_pack_v2"]["content_hash"],
        actor_ref=ACTOR,
        **candidate_params,
    )
    decision_params = copy.deepcopy(seed["base_manifest"]["decision"])
    return seed["coverage"].decide_thesis_admission(
        candidate_id=candidate["id"],
        candidate_hash=candidate["content_hash"],
        actor_ref=ACTOR,
        **decision_params,
    )


def _create_answered_question(
    agenda: AgendaStore,
    backlog: ResearchQuestionBacklog,
    plans: ResearchPlanAuthority,
    control: ResearchPlanControlPlane,
    seed: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    agenda.create_policy(
        _agenda_policy(),
        effective_from="2026-08-23T00:00:00.000000+00:00",
        effective_until=None,
        actor_ref=ACTOR,
        version_id="agenda-policy-version:isolated-acn-answer:1",
        idempotency_key="agenda-policy:isolated-acn-answer:1",
    )
    mandate_params = copy.deepcopy(seed["base_manifest"]["mandate"])
    mandate_ref = mandate_params.pop("mandate_ref")
    mandate = agenda.create_mandate(
        mandate_ref, actor_ref=ACTOR, **mandate_params
    )
    agenda.set_pause(
        False,
        reason="owner authorized isolated read-only ACN answer canary",
        actor_ref=ACTOR,
        version_id="agenda-control-version:isolated-acn-answer:1",
        idempotency_key="agenda-resume:isolated-acn-answer:1",
    )
    perception = _perception()
    agenda.register_perception_snapshot(
        perception,
        actor_ref="core",
        idempotency_key="perception:isolated-acn-answer:1",
    )
    cycle = agenda.start_cycle(
        "agenda:isolated-acn-answer:1",
        perception_snapshot_ref=perception["snapshot_id"],
        perception_snapshot_hash=perception["content_hash"],
        mandate_version_ref=mandate["id"],
        policy_version_ref="agenda-policy-version:isolated-acn-answer:1",
        company_ref=COMPANY_REF,
        actor_ref="core",
        cycle_id="agenda-cycle:isolated-acn-answer:1",
        idempotency_key="agenda-cycle:isolated-acn-answer:1",
    )
    agenda.add_candidates(
        cycle["cycle_id"],
        candidates=[{
            "candidate_id": "candidate:isolated-acn-answer:1",
            "company_ref": COMPANY_REF,
            "question": QUESTION,
            "answer_criteria": ANSWER_CRITERIA,
            "features": {
                "mandate_relevance": 3,
                "catalyst_urgency": 3,
                "evidence_staleness": 2,
                "decision_impact": 3,
            },
            "rationale": "Exercise the exact formal ACN answer binding.",
            "source_refs": [
                seed["evidence_by_key"]["acn-q3fy26-release"][
                    "evidence_version_id"
                ]
            ],
        }],
        actor_ref="core",
        idempotency_key="agenda-candidates:isolated-acn-answer:1",
    )
    decision = agenda.decide_cycle(
        cycle["cycle_id"],
        actor_ref="core",
        decision_id="agenda-decision:isolated-acn-answer:1",
        idempotency_key="agenda-decision:isolated-acn-answer:1",
    )
    recorded = backlog.record_question(
        mandate_version_ref=mandate["id"],
        company_ref=COMPANY_REF,
        question=QUESTION,
        answer_criteria=ANSWER_CRITERIA,
        source_refs=[
            seed["evidence_by_key"]["acn-q3fy26-release"][
                "evidence_version_id"
            ]
        ],
        actor_ref="core",
        idempotency_key="question:isolated-acn-answer:1",
    )
    backlog.select_question(
        question_ref=recorded["question_ref"],
        decision_ref=decision["id"],
        actor_ref="core",
        idempotency_key="question-select:isolated-acn-answer:1",
    )
    question = backlog.question(recorded["question_ref"])
    plan = plans.create_plan(
        question_ref=recorded["question_ref"],
        question_version_ref=question["head"]["id"],
        decision_ref=decision["id"],
        issuer_cik="1467373",
        form="8-K",
        filing_date_from="2026-05-01",
        filing_date_to="2026-08-25",
        actor_ref="core:planner",
        idempotency_key="research-plan:isolated-acn-answer:1",
    )
    plans.approve_plan(
        plan_version_ref=plan["plan_version_ref"],
        decision="accepted",
        reason="owner approved isolated recorded-authority replay",
        actor_ref=ACTOR,
        idempotency_key="research-plan-approval:isolated-acn-answer:1",
    )
    control.start_plan(
        plan_version_ref=plan["plan_version_ref"],
        actor_ref="core:planner",
        idempotency_key="research-plan-start:isolated-acn-answer:1",
    )
    claims = [
        seed["claim_by_key"]["bookings-total"],
        seed["claim_by_key"]["bookings-growth-lc"],
    ]
    answered = backlog.answer_question(
        question_ref=recorded["question_ref"],
        claim_version_refs=[item["claim_version_id"] for item in claims],
        actor_ref="core",
        idempotency_key="question-answer:isolated-acn-answer:1",
    )
    return mandate, answered


def _policy_thresholds() -> dict[str, Any]:
    return {
        "min_driver_coverage_bps": 0,
        "max_evidence_age_days_by_source_type": {
            "sec-filing-exhibit": 30,
        },
        "allowed_contested_claims": 0,
        "allowed_open_questions": 0,
        "allowed_unobservable_terminals": 0,
        "min_formal_claims": 2,
        "min_formal_evidence": 1,
    }


def _publish_answer_policy(
    answers: AnswerRoutingAuthority,
    mandate: dict[str, Any],
    *,
    version: int,
    prior_version_ref: str | None,
) -> dict[str, Any]:
    return answers.publish_policy(
        policy_ref="answer-policy:isolated-acn:1",
        mandate_version_ref=mandate["id"],
        mandate_version_hash=mandate["content_hash"],
        thresholds=_policy_thresholds(),
        refresh_route={
            "enabled": False,
            "max_cost_units": 0,
            "probe_template_bindings": [],
        },
        adhoc_research_route={
            "enabled": False,
            "max_cost_units": 0,
            "max_rounds": 0,
        },
        effective_from="2026-08-23T00:00:00.000000+00:00",
        effective_until=None,
        actor_ref=ACTOR,
        version_id=f"answer-policy-version:isolated-acn:{version}",
        prior_version_ref=prior_version_ref,
        idempotency_key=f"answer-policy:isolated-acn:{version}",
    )


def run(base_path: Path, evidence_manifest_path: Path) -> dict[str, Any]:
    with DaltonStore(":memory:") as store:
        observability = ObservabilityStore(store)
        agenda = AgendaStore(store)
        backlog = ResearchQuestionBacklog(store)
        plans = ResearchPlanAuthority(store)
        scheduler = Scheduler(connection=store.connection)
        plan_control = ResearchPlanControlPlane(
            plans, backlog, observability, scheduler
        )
        bounded = BoundedPlannerAuthority(store)
        seed = seed_acn_industry_authority(
            store, base_path=base_path, manifest_path=evidence_manifest_path
        )
        mandate, answered = _create_answered_question(
            agenda, backlog, plans, plan_control, seed
        )
        thesis = _publish_thesis(seed, mandate)
        answers = AnswerRoutingAuthority(
            store, agenda, backlog, bounded, seed["industry"]
        )
        policy_v1 = _publish_answer_policy(
            answers, mandate, version=1, prior_version_ref=None
        )["policy"]
        subject = next(
            item
            for item in answers.subjects(as_of=FRESH_AT)
            if item["company_ref"] == COMPANY_REF
        )

        routed = _read_only_call(
            store,
            lambda: {
                "direct": answers.route(
                    subject_binding=subject, question=QUESTION, as_of=FRESH_AT
                ),
                "unmatched": answers.route(
                    subject_binding=subject,
                    question=(
                        "How strong were Accenture's Q3 FY2026 bookings?"
                    ),
                    as_of=FRESH_AT,
                ),
                "stale": answers.route(
                    subject_binding=subject, question=QUESTION, as_of=STALE_AT
                ),
            },
        )
        direct = routed["direct"]
        unmatched = routed["unmatched"]
        stale = routed["stale"]

        policy_v2 = _publish_answer_policy(
            answers,
            mandate,
            version=2,
            prior_version_ref=policy_v1["id"],
        )["policy"]
        before_rotation_check = _authority_fingerprint(store)
        before_rotation_changes = store.connection.total_changes
        old_subject_rejected = False
        try:
            answers.route(
                subject_binding=subject, question=QUESTION, as_of=FRESH_AT
            )
        except AnswerRoutingConflict:
            old_subject_rejected = True
        if (
            not old_subject_rejected
            or before_rotation_changes != store.connection.total_changes
            or before_rotation_check != _authority_fingerprint(store)
        ):
            raise RuntimeError("rotated answer policy did not invalidate old subject")

        direct_pack = direct["context_pack"]
        direct_decision = direct["decision"]
        unmatched_decision = unmatched["decision"]
        stale_decision = stale["decision"]
        if direct_decision["route"] != "answer_direct":
            raise RuntimeError("exact answered ACN question did not route direct")
        if (
            unmatched_decision["route"] != "recommend_agenda_item"
            or "question_not_admitted" not in unmatched_decision["reason_codes"]
        ):
            raise RuntimeError("unmatched ACN question did not fail closed")
        if (
            stale_decision["route"] != "recommend_agenda_item"
            or "stale_evidence" not in stale_decision["reason_codes"]
        ):
            raise RuntimeError("stale ACN evidence did not fail closed")
        if any(
            item["decision"]["write_performed"]
            for item in (direct, unmatched, stale)
        ):
            raise RuntimeError("answer route reported an authority write")
        integrity = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("isolated ACN answer Core failed integrity_check")

        counts = _table_counts(store)
        return {
            "status": "passed",
            "mode": "isolated-in-memory-recorded-authority",
            "company_ref": COMPANY_REF,
            "question": QUESTION,
            "fresh_as_of": FRESH_AT,
            "stale_as_of": STALE_AT,
            "direct_route": direct_decision["route"],
            "direct_reason_codes": direct_decision["reason_codes"],
            "direct_claim_refs": [
                item["claim_ref"] for item in direct_pack["claim_versions"]
            ],
            "direct_evidence_refs": [
                item["evidence_ref"] for item in direct_pack["evidence_versions"]
            ],
            "direct_context_pack_ref": direct_pack["id"],
            "direct_context_pack_hash": direct_pack["content_hash"],
            "direct_route_decision_ref": direct_decision["id"],
            "direct_route_decision_hash": direct_decision["content_hash"],
            "unmatched_route": unmatched_decision["route"],
            "unmatched_reason_codes": unmatched_decision["reason_codes"],
            "stale_route": stale_decision["route"],
            "stale_reason_codes": stale_decision["reason_codes"],
            "route_write_performed": False,
            "route_table_counts_unchanged": True,
            "route_total_changes_unchanged": True,
            "route_authority_fingerprint_unchanged": True,
            "old_subject_rejected_after_policy_rotation": old_subject_rejected,
            "answer_policy_version_refs": [policy_v1["id"], policy_v2["id"]],
            "matched_question_ref": answered["question_ref"],
            "answer_binding_count": len(answered["answer_bindings"]),
            "thesis_version_ref": thesis["thesis_version"]["id"],
            "thesis_version_count": counts["thesis_versions"],
            "context_thesis_count": len(direct_pack["current_thesis_versions"]),
            "context_driver_pack_count": len(direct_pack["industry_driver_packs"]),
            "context_company_overlay_count": len(direct_pack["company_overlays"]),
            "context_formal_claim_count": direct_pack["metrics"][
                "formal_claim_count"
            ],
            "context_formal_evidence_count": direct_pack["metrics"][
                "formal_evidence_count"
            ],
            "context_driver_coverage_bps": direct_pack["metrics"][
                "driver_coverage_bps"
            ],
            "formal_evidence_version_count": counts["evidence_versions"],
            "formal_claim_version_count": counts["claim_versions"],
            "formal_evidence_relation_count": counts["evidence_relations"],
            "recorded_model_invocation_count": counts["model_invocations"],
            "recorded_cost_entry_count": counts["observability_cost_entries"],
            "paid_model_calls": 0,
            "network_calls": 0,
            "live_database_writes": 0,
            "integrity_check": integrity,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument(
        "--evidence-manifest", type=Path, default=DEFAULT_MANIFEST
    )
    args = parser.parse_args()
    result = run(args.base.resolve(), args.evidence_manifest.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
