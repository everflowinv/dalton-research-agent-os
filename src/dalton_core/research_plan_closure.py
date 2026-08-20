"""Close an authorized ResearchPlan into one exact Backlog answer.

The executor stops at candidate staging and the review control plane stops
after formal Ledger promotion.  This coordinator joins those existing
authorities without creating another queue or mutable status table:

* the complete four-node plan tree is re-read from Scheduler authority;
* the final stage proof must name the exact reviewed candidate pair;
* a human path requires an explicit accept and committed delivery event;
  a low-risk path requires an exact active-policy authorization receipt;
* the promoted Evidence/Claim/supports relation are revalidated from Core;
* only that formal ClaimVersion may answer the plan's exact Backlog question.

The final write uses Backlog's deterministic idempotency.  A crash after the
answer transaction therefore converges on replay instead of creating a
second binding.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .contracts import EvidenceRelation
from .research_plan import (
    ResearchPlanAuthority,
    ResearchPlanError,
    _plan_work_orders,
)
from .research_plan_coordinator import ResearchPlanCoordinator
from .research_plan_executor import re_read_stage_records
from .research_question_backlog import ResearchQuestionBacklog
from .research_review import (
    HumanReviewAuthority,
    validate_claim_version_v0_2,
    validate_evidence_version_v0_2,
    validate_human_review_decision,
)
from .research_auto_commit import validate_policy_commit_decision
from .store import canonical_json, content_hash


class ResearchPlanClosureError(ResearchPlanError):
    """Base error for the review-to-Backlog closure boundary."""


class ResearchPlanClosureConflict(ResearchPlanClosureError):
    """The exact plan, candidate, review or Ledger chain did not agree."""


class ResearchPlanClosurePending(ResearchPlanClosureError):
    """The plan or accepted review is not yet durably ready to close."""


_PROMOTION_RESULT_FIELDS = {
    "status", "idempotency_key", "review_decision_ref",
    "evidence_version_ref", "claim_version_ref", "relation_ref",
    "claim_status", "event_refs",
}


def _json_record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ResearchPlanClosureConflict(f"{name} is missing")
    try:
        wire = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ResearchPlanClosureConflict(f"{name} is not valid JSON") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != value:
        raise ResearchPlanClosureConflict(f"{name} is not canonical JSON")
    return wire


def _promotion_result(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROMOTION_RESULT_FIELDS:
        raise ResearchPlanClosureConflict(f"{name} has an invalid closed shape")
    wire = dict(value)
    if wire["status"] not in {"fresh", "duplicate"}:
        raise ResearchPlanClosureConflict(f"{name} has an invalid status")
    for field in (
        "idempotency_key", "review_decision_ref", "evidence_version_ref",
        "claim_version_ref", "relation_ref", "claim_status",
    ):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ResearchPlanClosureConflict(f"{name}.{field} is invalid")
    refs = wire["event_refs"]
    if (
        not isinstance(refs, list)
        or len(refs) != 3
        or any(not isinstance(ref, str) or not ref for ref in refs)
        or len(set(refs)) != 3
    ):
        raise ResearchPlanClosureConflict(f"{name}.event_refs is invalid")
    return wire


class ResearchPlanClosureCoordinator:
    """Authority-bound, replayable ResearchPlan answer coordinator."""

    def __init__(
        self,
        *,
        plan: ResearchPlanAuthority,
        backlog: ResearchQuestionBacklog,
        coordinator: ResearchPlanCoordinator,
        review: HumanReviewAuthority,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if backlog.connection is not plan.connection:
            raise TypeError("plan and backlog must share one Core connection")
        if coordinator.plan is not plan or coordinator.scheduler.connection is not plan.connection:
            raise TypeError(
                "plan coordinator must bind the same plan and Core authorities"
            )
        self.plan = plan
        self.backlog = backlog
        self.coordinator = coordinator
        self.scheduler = coordinator.scheduler
        self.review = review
        self.fault_injector = fault_injector

    def _inject(self, seam: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(seam)

    def _final_candidates(self, plan_wire: Mapping[str, Any]) -> dict[str, dict[str, str]]:
        tree = self.coordinator.tree_status(plan_wire["id"])
        nodes = tree["nodes"]
        if (
            len(nodes) != len(plan_wire["execution_scope"]["steps"])
            or any(node["attempt_state"] != "succeeded" for node in nodes)
        ):
            raise ResearchPlanClosurePending(
                "research plan tree is not terminally succeeded"
            )
        work_orders = _plan_work_orders(plan_wire)
        final = self.scheduler.formal_result(work_orders[-1]["id"])
        if final is None or final["terminal_state"] != "succeeded":
            raise ResearchPlanClosurePending(
                "candidate staging has no succeeded formal result"
            )
        records = re_read_stage_records(
            final, expected_kinds=("candidate_evidence", "candidate_claim")
        )
        by_kind = {record["kind"]: record for record in records}
        if set(by_kind) != {"candidate_evidence", "candidate_claim"}:
            raise ResearchPlanClosureConflict(
                "candidate staging proof does not contain one exact candidate pair"
            )
        return by_kind

    def _formal_promotion(
        self,
        *,
        bundle: Mapping[str, Any],
        review_result: Mapping[str, Any] | None,
        policy_authorized: bool = False,
    ) -> dict[str, Any]:
        validator = (
            validate_policy_commit_decision
            if policy_authorized
            else validate_human_review_decision
        )
        decision = validator(bundle["decision"])
        evidence = bundle["evidence"]
        claim = bundle["claim"]
        cursor = self.plan.connection.cursor()
        row = cursor.execute(
            "SELECT * FROM reviewed_candidate_commits WHERE review_decision_ref=?",
            (decision["id"],),
        ).fetchone()
        if row is None:
            raise ResearchPlanClosurePending(
                "accepted review has no formal Ledger promotion receipt"
            )
        stored_decision = validator(
            _json_record(row["decision_json"], "reviewed commit decision")
        )
        stored_result = _promotion_result(
            _json_record(row["result_json"], "reviewed commit result"),
            "reviewed commit result",
        )
        delivered = (
            None
            if review_result is None
            else _promotion_result(review_result, "review delivery result")
        )
        expected_request_hash = content_hash({
            "decision_hash": decision["content_hash"],
            "evidence_hash": evidence["content_hash"],
            "claim_hash": claim["content_hash"],
        })
        if (
            canonical_json(stored_decision) != canonical_json(decision)
            or row["request_hash"] != expected_request_hash
            or row["candidate_evidence_ref"] != evidence["id"]
            or row["candidate_claim_ref"] != claim["id"]
            or stored_result["status"] != "fresh"
            or stored_result["idempotency_key"] != row["idempotency_key"]
        ):
            raise ResearchPlanClosureConflict(
                "formal promotion receipt drifted from the exact review bundle"
            )
        identity_fields = (
            "idempotency_key", "review_decision_ref", "evidence_version_ref",
            "claim_version_ref", "relation_ref", "claim_status", "event_refs",
        )
        if delivered is not None and any(
            delivered[field] != stored_result[field] for field in identity_fields
        ):
            raise ResearchPlanClosureConflict(
                "review delivery event and Core promotion receipt disagree"
            )

        evidence_row = cursor.execute(
            "SELECT * FROM evidence_versions WHERE evidence_version_id=?",
            (stored_result["evidence_version_ref"],),
        ).fetchone()
        claim_row = cursor.execute(
            "SELECT * FROM claim_versions WHERE claim_version_id=?",
            (stored_result["claim_version_ref"],),
        ).fetchone()
        relation_row = cursor.execute(
            "SELECT * FROM evidence_relations WHERE relation_id=?",
            (stored_result["relation_ref"],),
        ).fetchone()
        if evidence_row is None or claim_row is None or relation_row is None:
            raise ResearchPlanClosureConflict(
                "formal promotion is missing Evidence, Claim or relation authority"
            )
        formal_evidence = validate_evidence_version_v0_2(
            _json_record(evidence_row["evidence_json"], "formal EvidenceVersion")
        )
        formal_claim = validate_claim_version_v0_2(
            _json_record(claim_row["claim_json"], "formal ClaimVersion")
        )
        relation_doc = _json_record(
            relation_row["relation_json"], "formal EvidenceRelation"
        )
        try:
            formal_relation = EvidenceRelation.from_dict(relation_doc).to_dict()
        except Exception as exc:
            raise ResearchPlanClosureConflict(
                "formal EvidenceRelation is invalid"
            ) from exc
        if (
            formal_evidence["id"] != evidence_row["evidence_version_id"]
            or formal_evidence["content_hash"] != evidence_row["content_hash"]
            or formal_evidence["candidate_origin_ref"] != evidence["id"]
            or formal_evidence["candidate_origin_hash"] != evidence["content_hash"]
            or formal_evidence["review_decision_ref"] != decision["id"]
            or formal_claim["id"] != claim_row["claim_version_id"]
            or formal_claim["content_hash"] != claim_row["content_hash"]
            or formal_claim["candidate_origin_ref"] != claim["id"]
            or formal_claim["candidate_origin_hash"] != claim["content_hash"]
            or formal_claim["semantic_review_ref"] != decision["id"]
            or formal_relation["id"] != relation_row["relation_id"]
            or formal_relation["content_hash"] != relation_row["content_hash"]
            or formal_relation["evidence_version_ref"] != formal_evidence["id"]
            or formal_relation["claim_version_ref"] != formal_claim["id"]
            or formal_relation["relation"] != "supports"
        ):
            raise ResearchPlanClosureConflict(
                "formal promotion chain drifted from the reviewed candidate"
            )
        event_rows = cursor.execute(
            "SELECT * FROM domain_events WHERE event_id IN (?,?,?)",
            tuple(stored_result["event_refs"]),
        ).fetchall()
        if {item["event_id"] for item in event_rows} != set(stored_result["event_refs"]):
            raise ResearchPlanClosureConflict(
                "formal promotion receipt is missing its domain events"
            )
        expected_events = {
            "evidence_versioned": {
                "aggregate_type": "evidence",
                "aggregate_id": formal_evidence["evidence_ref"],
                "payload": formal_evidence,
                "content_hash": formal_evidence["content_hash"],
                "idempotency_key": f"reviewed-evidence:{decision['id']}",
            },
            "claim_versioned": {
                "aggregate_type": "claim",
                "aggregate_id": formal_claim["claim_ref"],
                "payload": formal_claim,
                "content_hash": formal_claim["content_hash"],
                "idempotency_key": f"reviewed-claim:{decision['id']}",
            },
            "evidence_related": {
                "aggregate_type": "claim",
                "aggregate_id": formal_claim["claim_ref"],
                "payload": formal_relation,
                "content_hash": formal_relation["content_hash"],
                "idempotency_key": f"reviewed-relation:{decision['id']}",
            },
        }
        if {item["event_type"] for item in event_rows} != set(expected_events):
            raise ResearchPlanClosureConflict(
                "formal promotion domain event types are incomplete"
            )
        for item in event_rows:
            event = _json_record(item["event_json"], "formal DomainEvent")
            expected = expected_events[item["event_type"]]
            if (
                event.get("schema_version") != "0.1"
                or event.get("id") != item["event_id"]
                or event.get("event_type") != item["event_type"]
                or event.get("aggregate_type") != item["aggregate_type"]
                or event.get("aggregate_id") != item["aggregate_id"]
                or event.get("aggregate_version") != item["aggregate_version"]
                or event.get("version_ref") != item["version_ref"]
                or event.get("change_ref") != item["change_id"]
                or event.get("verification_ref") != item["verification_id"]
                or event.get("content_hash") != item["content_hash"]
                or event.get("actor_ref") != item["actor_id"]
                or event.get("idempotency_key") != item["idempotency_key"]
                or event.get("correlation_id") != item["correlation_id"]
                or event.get("occurred_at") != item["occurred_at"]
                or event.get("created_at") != item["created_at"]
                or item["aggregate_type"] != expected["aggregate_type"]
                or item["aggregate_id"] != expected["aggregate_id"]
                or item["content_hash"] != expected["content_hash"]
                or item["actor_id"] != decision["reviewer_ref"]
                or item["idempotency_key"] != expected["idempotency_key"]
                or item["correlation_id"] != expected["aggregate_id"]
                or event.get("payload") != expected["payload"]
            ):
                raise ResearchPlanClosureConflict(
                    "formal promotion DomainEvent drifted from its authority"
                )
        return {
            "receipt": stored_result,
            "evidence": formal_evidence,
            "claim": formal_claim,
            "relation": formal_relation,
        }

    def close(
        self,
        *,
        plan_version_ref: str,
        review_decision_ref: str,
    ) -> dict[str, Any]:
        """Bind one committed accepted claim to its plan's exact question."""

        view = self.plan.plan(plan_version_ref)
        if view["state"] != "started" or view["start_binding"] is None:
            raise ResearchPlanClosurePending("research plan is not started")
        plan_wire = view["plan_version"]
        candidates = self._final_candidates(plan_wire)
        bundle = self.review.decision_bundle(review_decision_ref)
        decision = validate_human_review_decision(bundle["decision"])
        if decision["verdict"] != "accept":
            raise ResearchPlanClosureConflict(
                "only an explicitly accepted review can close a plan"
            )
        if candidates["candidate_evidence"] != {
            "kind": "candidate_evidence",
            "ref": bundle["evidence"]["id"],
            "hash": bundle["evidence"]["content_hash"],
        }:
            raise ResearchPlanClosureConflict(
                "review decision does not retain the plan's exact candidate evidence"
            )
        final_claim = candidates["candidate_claim"]
        accepted_claim = bundle["claim"]
        if final_claim != {
            "kind": "candidate_claim",
            "ref": accepted_claim["id"],
            "hash": accepted_claim["content_hash"],
        }:
            lineage = self.review.revision_lineage(accepted_claim["id"])
            if (
                len(lineage["claims"]) != 2
                or len(lineage["revision_decisions"]) != 1
                or final_claim != {
                    "kind": "candidate_claim",
                    "ref": lineage["claims"][0]["id"],
                    "hash": lineage["claims"][0]["content_hash"],
                }
                or canonical_json(lineage["claims"][1])
                != canonical_json(accepted_claim)
                or any(
                    canonical_json(item) != canonical_json(bundle["evidence"])
                    for item in lineage["evidences"]
                )
            ):
                raise ResearchPlanClosureConflict(
                    "accepted candidate is not the plan's exact single-step human revision"
                )
        event = self.review.commit_event(decision["id"])
        if event is None or event["state"] != "committed":
            raise ResearchPlanClosurePending(
                "accepted review has not committed to the formal Ledger"
            )
        formal = self._formal_promotion(
            bundle=bundle, review_result=event["ledger_result"]
        )
        question = self.backlog.question(plan_wire["question_ref"])
        if question["state"] == "answered":
            bindings = question["answer_bindings"]
            if (
                len(bindings) != 1
                or bindings[0]["claim_version_ref"] != formal["claim"]["id"]
                or bindings[0]["claim_version_hash"]
                != formal["claim"]["content_hash"]
            ):
                raise ResearchPlanClosureConflict(
                    "answered question is bound to a different formal claim"
                )
            return {
                "status": "duplicate",
                "plan_version_ref": plan_wire["id"],
                "question_ref": plan_wire["question_ref"],
                "review_decision_ref": decision["id"],
                "candidate_claim_ref": bundle["claim"]["id"],
                "evidence_version_ref": formal["evidence"]["id"],
                "claim_version_ref": formal["claim"]["id"],
                "relation_ref": formal["relation"]["id"],
                "answer_event_ref": bindings[0]["event_ref"],
                "answer_binding_ref": bindings[0]["id"],
            }
        if question["state"] != "in_progress":
            raise ResearchPlanClosurePending(
                "research plan question is not ready to be answered"
            )
        self._inject("before_backlog_answer")
        answer = self.backlog.answer_question(
            question_ref=plan_wire["question_ref"],
            claim_version_refs=[formal["claim"]["id"]],
            actor_ref="core:research-plan-closure",
            idempotency_key=(
                f"research-plan-closure:{plan_wire['id']}:{decision['id']}"
            ),
        )
        self._inject("after_backlog_answer")
        if answer["state"] != "answered":
            raise ResearchPlanClosureConflict(
                "Backlog answer transition did not converge"
            )
        return {
            "status": answer["status"],
            "plan_version_ref": plan_wire["id"],
            "question_ref": plan_wire["question_ref"],
            "review_decision_ref": decision["id"],
            "candidate_claim_ref": bundle["claim"]["id"],
            "evidence_version_ref": formal["evidence"]["id"],
            "claim_version_ref": formal["claim"]["id"],
            "relation_ref": formal["relation"]["id"],
            "answer_event_ref": answer["event"]["id"],
            "answer_binding_ref": answer["answer_bindings"][0]["id"],
        }

    def close_policy_authorized(
        self,
        *,
        plan_version_ref: str,
        authorization: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Close one exact low-risk plan without a per-item human decision."""

        decision = validate_policy_commit_decision(authorization)
        view = self.plan.plan(plan_version_ref)
        if view["state"] != "started" or view["start_binding"] is None:
            raise ResearchPlanClosurePending("research plan is not started")
        plan_wire = view["plan_version"]
        candidates = self._final_candidates(plan_wire)
        candidate = self.review.candidate_bundle(decision["candidate_claim_ref"])
        evidence = candidate["evidence"]
        claim = candidate["claim"]
        if candidates["candidate_evidence"] != {
            "kind": "candidate_evidence",
            "ref": evidence["id"],
            "hash": evidence["content_hash"],
        } or candidates["candidate_claim"] != {
            "kind": "candidate_claim",
            "ref": claim["id"],
            "hash": claim["content_hash"],
        }:
            raise ResearchPlanClosureConflict(
                "policy authorization does not bind the plan's exact final candidate"
            )
        if (
            decision["candidate_evidence_ref"] != evidence["id"]
            or decision["candidate_evidence_hash"] != evidence["content_hash"]
            or decision["candidate_claim_hash"] != claim["content_hash"]
        ):
            raise ResearchPlanClosureConflict(
                "policy authorization candidate binding drifted"
            )
        formal = self._formal_promotion(
            bundle={"decision": decision, "evidence": evidence, "claim": claim},
            review_result=None,
            policy_authorized=True,
        )
        question = self.backlog.question(plan_wire["question_ref"])
        if question["state"] == "answered":
            bindings = question["answer_bindings"]
            if (
                len(bindings) != 1
                or bindings[0]["claim_version_ref"] != formal["claim"]["id"]
                or bindings[0]["claim_version_hash"]
                != formal["claim"]["content_hash"]
            ):
                raise ResearchPlanClosureConflict(
                    "answered question is bound to a different formal claim"
                )
            return {
                "status": "duplicate",
                "plan_version_ref": plan_wire["id"],
                "question_ref": plan_wire["question_ref"],
                "authorization_ref": decision["id"],
                "candidate_claim_ref": claim["id"],
                "evidence_version_ref": formal["evidence"]["id"],
                "claim_version_ref": formal["claim"]["id"],
                "relation_ref": formal["relation"]["id"],
                "answer_event_ref": bindings[0]["event_ref"],
                "answer_binding_ref": bindings[0]["id"],
            }
        if question["state"] != "in_progress":
            raise ResearchPlanClosurePending(
                "research plan question is not ready to be answered"
            )
        self._inject("before_backlog_answer")
        answer = self.backlog.answer_question(
            question_ref=plan_wire["question_ref"],
            claim_version_refs=[formal["claim"]["id"]],
            actor_ref="core:research-plan-policy-closure",
            idempotency_key=(
                f"research-plan-policy-closure:{plan_wire['id']}:{decision['id']}"
            ),
        )
        self._inject("after_backlog_answer")
        if answer["state"] != "answered":
            raise ResearchPlanClosureConflict(
                "Backlog answer transition did not converge"
            )
        return {
            "status": answer["status"],
            "plan_version_ref": plan_wire["id"],
            "question_ref": plan_wire["question_ref"],
            "authorization_ref": decision["id"],
            "candidate_claim_ref": claim["id"],
            "evidence_version_ref": formal["evidence"]["id"],
            "claim_version_ref": formal["claim"]["id"],
            "relation_ref": formal["relation"]["id"],
            "answer_event_ref": answer["event"]["id"],
            "answer_binding_ref": answer["answer_bindings"][0]["id"],
        }


__all__ = [
    "ResearchPlanClosureCoordinator",
    "ResearchPlanClosureConflict",
    "ResearchPlanClosureError",
    "ResearchPlanClosurePending",
]
