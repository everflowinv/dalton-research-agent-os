"""Versioned-policy authorization for one bounded autonomous research result.

This module intentionally supports one narrow rule.  It does not decide what
research to run and it does not grant new connector or budget authority.  It
only determines whether an already completed, deterministically verified SEC
``filing_count`` candidate may cross the existing Ledger commit boundary
without per-item human review.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .research_verification import (
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_numeric_verification_spec,
    validate_source_verification_material,
    validate_verification_bundle,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
RULE_REF = "research-auto-commit:sec-public-filing-count:v1"
COMPANY_FACTS_RULE_REF = "research-auto-commit:sec-public-company-facts-growth:v1"
ACTOR_REF = "system:research-auto-commit"
_CIK_RE = re.compile(r"^[0-9]{10}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_POLICY_FIELDS = {"enabled", "rules", "max_records"}
_DECISION_FIELDS = {
    "schema_version", "id", "created_at", "candidate_claim_ref",
    "candidate_claim_hash", "candidate_evidence_ref", "candidate_evidence_hash",
    "verdict", "reviewed_semantics", "proposed_revisions", "relation",
    "rationale", "findings", "reviewer_ref", "authorization", "source",
    "source_event_ref", "policy_version_ref", "policy_version_hash",
    "rule_ref", "content_hash",
}


class ResearchAutoCommitError(ValueError):
    """The autonomous commit contract is malformed."""


class ResearchAutoCommitRejected(ResearchAutoCommitError):
    """The active policy does not authorize this exact candidate."""


def _record(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ResearchAutoCommitRejected(f"{name} authority is unavailable")
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchAutoCommitRejected(f"{name} authority is corrupt") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ResearchAutoCommitRejected(f"{name} authority is not canonical JSON")
    declared = wire.get("content_hash")
    unhashed = {key: value for key, value in wire.items() if key != "content_hash"}
    if row["content_hash"] != declared or declared != content_hash(unhashed):
        raise ResearchAutoCommitRejected(f"{name} authority hash drifted")
    return wire


def _policy_rule(policy_version: Mapping[str, Any]) -> dict[str, Any]:
    policy = policy_version.get("policy")
    if not isinstance(policy, Mapping):
        raise ResearchAutoCommitRejected("active governance policy is malformed")
    rule = policy.get("research_candidate_auto_commit")
    if not isinstance(rule, Mapping) or set(rule) != _POLICY_FIELDS:
        raise ResearchAutoCommitRejected(
            "active governance policy does not contain the closed research auto-commit rule"
        )
    if rule["enabled"] is not True:
        raise ResearchAutoCommitRejected("research candidate auto-commit is disabled")
    if rule["rules"] not in ([RULE_REF], [COMPANY_FACTS_RULE_REF]):
        raise ResearchAutoCommitRejected("active governance policy rule set is not supported")
    max_records = rule["max_records"]
    if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 100:
        raise ResearchAutoCommitRejected("research auto-commit max_records is invalid")
    return {**dict(rule), "selected_rule": rule["rules"][0]}


def validate_policy_commit_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed receipt emitted by the policy evaluator."""

    if not isinstance(value, Mapping) or set(value) != _DECISION_FIELDS:
        raise ResearchAutoCommitError("PolicyCommitDecision has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchAutoCommitError("unsupported PolicyCommitDecision schema_version")
    if wire["verdict"] != "accept" or wire["relation"] != "supports":
        raise ResearchAutoCommitError("PolicyCommitDecision must authorize one supports relation")
    if wire["proposed_revisions"] is not None:
        raise ResearchAutoCommitError("policy authorization cannot rewrite candidate semantics")
    if (
        wire["reviewer_ref"] != ACTOR_REF
        or wire["authorization"] != "versioned_governance_policy"
        or wire["source"] != "governance_policy"
        or wire["rule_ref"] not in {RULE_REF, COMPANY_FACTS_RULE_REF}
    ):
        raise ResearchAutoCommitError("PolicyCommitDecision authority is invalid")
    for field in (
        "id", "created_at", "candidate_claim_ref", "candidate_evidence_ref",
        "rationale", "source_event_ref", "policy_version_ref",
    ):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ResearchAutoCommitError(f"PolicyCommitDecision.{field} is invalid")
    for field in (
        "candidate_claim_hash", "candidate_evidence_hash", "policy_version_hash",
    ):
        if not isinstance(wire[field], str) or re.fullmatch(r"[0-9a-f]{64}", wire[field]) is None:
            raise ResearchAutoCommitError(f"PolicyCommitDecision.{field} is invalid")
    if not isinstance(wire["reviewed_semantics"], Mapping) or set(wire["reviewed_semantics"]) != {
        "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement",
    }:
        raise ResearchAutoCommitError("PolicyCommitDecision.reviewed_semantics is invalid")
    expected_finding = {
        RULE_REF: "matched exact deterministic SEC filing-count rule",
        COMPANY_FACTS_RULE_REF: (
            "matched exact deterministic SEC company-facts growth rule"
        ),
    }[wire["rule_ref"]]
    if wire["findings"] != [expected_finding]:
        raise ResearchAutoCommitError("PolicyCommitDecision.findings is invalid")
    declared = wire.pop("content_hash")
    if declared != content_hash(wire):
        raise ResearchAutoCommitError("PolicyCommitDecision content_hash mismatch")
    wire["content_hash"] = declared
    return wire


def authorize_policy_candidate(
    *,
    connection: sqlite3.Connection,
    policy_version: Mapping[str, Any],
    evidence: Mapping[str, Any],
    claim: Mapping[str, Any],
    material: Mapping[str, Any] | None = None,
    numeric_spec: Mapping[str, Any] | None = None,
    source_verification: Mapping[str, Any] | None = None,
    numeric_verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one exact candidate against Core authority and active policy."""

    rule = _policy_rule(policy_version)
    selected_rule = rule["selected_rule"]
    evidence_wire = validate_candidate_evidence(evidence)
    claim_wire = validate_candidate_claim(claim)
    if (
        evidence_wire["version"] != 1
        or evidence_wire["prior_version_ref"] is not None
        or claim_wire["version"] != 1
        or claim_wire["prior_version_ref"] is not None
    ):
        raise ResearchAutoCommitRejected("revised or chained candidates require escalation")
    expected_evidence = [{"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}]
    if claim_wire["candidate_evidence_refs"] != expected_evidence:
        raise ResearchAutoCommitRejected("candidate claim does not bind the exact evidence")
    if (
        evidence_wire["source_verification_ref"] != claim_wire["source_verification_ref"]
        or evidence_wire["source_verification_hash"] != claim_wire["source_verification_hash"]
    ):
        raise ResearchAutoCommitRejected("candidate verification bindings disagree")

    source_row = connection.execute(
        "SELECT record_json,content_hash,connector_invocation_ref FROM connector_source_envelopes "
        "WHERE source_envelope_id=?",
        (evidence_wire["source_envelope_ref"],),
    ).fetchone()
    source = _record(source_row, "SourceEnvelope")
    if source["content_hash"] != evidence_wire["source_envelope_hash"]:
        raise ResearchAutoCommitRejected("candidate SourceEnvelope hash is not exact")
    invocation_row = connection.execute(
        "SELECT record_json,content_hash,call_spec_ref,call_spec_hash,connector_profile_ref,"
        "connector_profile_hash FROM connector_invocations WHERE connector_invocation_id=?",
        (source["connector_invocation_ref"],),
    ).fetchone()
    invocation = _record(invocation_row, "ConnectorInvocation")
    call_row = connection.execute(
        "SELECT record_json,content_hash FROM connector_call_specs WHERE call_spec_id=?",
        (invocation["call_spec_ref"],),
    ).fetchone()
    call = _record(call_row, "ConnectorCallSpec")
    profile_row = connection.execute(
        "SELECT record_json,content_hash FROM connector_profile_versions WHERE profile_version_id=?",
        (invocation["connector_profile_ref"],),
    ).fetchone()
    profile = _record(profile_row, "ConnectorProfile")
    if (
        call["content_hash"] != invocation["call_spec_hash"]
        or profile["content_hash"] != invocation["connector_profile_hash"]
        or call["connector_profile_ref"] != profile["id"]
    ):
        raise ResearchAutoCommitRejected("connector authority bindings drifted")

    plan_row = connection.execute(
        "SELECT p.record_json,p.content_hash FROM research_plan_starts s "
        "JOIN research_plan_versions p ON p.version_id=s.plan_version_ref "
        "WHERE s.root_work_order_ref=? AND s.root_work_order_hash=?",
        (call["work_order_ref"], call["work_order_hash"]),
    ).fetchone()
    plan = _record(plan_row, "ResearchPlanVersion")
    question_row = connection.execute(
        "SELECT record_json,content_hash FROM backlog_question_versions WHERE version_id=?",
        (plan["question_version_ref"],),
    ).fetchone()
    question = _record(question_row, "ResearchQuestionVersion")

    parameters = call.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ResearchAutoCommitRejected("SEC call parameters are unavailable")
    records = source.get("source_record_refs")
    if not isinstance(records, list) or len(records) > rule["max_records"]:
        raise ResearchAutoCommitRejected("SEC source record count exceeds policy")
    if (
        source.get("source") != "source:sec-edgar"
        or source.get("operation") != call.get("operation")
        or source.get("status") != "complete"
        or source.get("completeness") != "enumerated"
        or source.get("cursor") is not None
        or source.get("access_policy_ref") != "policy:access:public"
        or evidence_wire["source_ref"] != "source:sec-edgar"
        or evidence_wire["source_type"] != "official_filing"
        or evidence_wire["valid_until"] is not None
        or profile.get("auth_mode") != "none"
        or profile.get("credential_slot_refs") != []
    ):
        raise ResearchAutoCommitRejected("candidate source is not low-risk public SEC authority")

    plan_parameters = plan.get("execution_scope", {}).get("parameters")
    if selected_rule == RULE_REF:
        issuer = parameters.get("issuer")
        form = parameters.get("form")
        date_from = parameters.get("date_from")
        date_to = parameters.get("date_to")
        limit = parameters.get("limit")
        if (
            call.get("operation") != "list_filings"
            or not isinstance(issuer, str) or _CIK_RE.fullmatch(issuer) is None
            or form not in {"10-Q", "6-K"}
            or not isinstance(date_from, str) or _DATE_RE.fullmatch(date_from) is None
            or not isinstance(date_to, str) or _DATE_RE.fullmatch(date_to) is None
            or date_from > date_to
            or isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= rule["max_records"]
        ):
            raise ResearchAutoCommitRejected(
                "candidate is outside the bounded SEC filing-count rule"
            )
        period = f"{date_from}..{date_to}"
        count = len(records)
        if plan_parameters != {
            "filing_date_from": date_from,
            "filing_date_to": date_to,
            "form": form,
            "issuer_cik": issuer,
        }:
            raise ResearchAutoCommitRejected(
                "SEC call drifted from its exact ResearchPlan"
            )
        expected_statement = (
            f"The SEC public {form} filing list for CIK {issuer} in window "
            f"{period} contains {count} filings."
        )
        expected_claim = {
            "subject_ref": question["company_ref"],
            "metric_or_aspect": "filing_count",
            "period": period,
            "basis": "official-filing",
            "normalized_statement": expected_statement,
            "claim_kind": "quantitative",
            "value": str(count),
            "unit": "records",
            "currency": None,
            "scale": "one",
            "semantic_verification_status": "unverified",
            "actor_ref": "runner:research-plan-executor",
        }
        finding = "matched exact deterministic SEC filing-count rule"
    else:
        if any(
            item is None
            for item in (
                material, numeric_spec, source_verification, numeric_verification
            )
        ):
            raise ResearchAutoCommitRejected(
                "company facts policy requires the exact staged verification bundle"
            )
        material_wire = validate_source_verification_material(material or {})
        spec_wire = validate_numeric_verification_spec(numeric_spec or {})
        source_wire = validate_verification_bundle(source_verification or {})
        numeric_wire = validate_verification_bundle(numeric_verification or {})
        if (
            material_wire.get("schema_version") != "0.2"
            or material_wire.get("source_envelope_ref") != source["id"]
            or material_wire.get("source_envelope_hash") != source["content_hash"]
            or source_wire.get("verdict") != "pass"
            or numeric_wire.get("verdict") != "pass"
            or source_wire.get("subject_ref") != material_wire["id"]
            or source_wire.get("subject_hash") != material_wire["content_hash"]
            or claim_wire["source_verification_ref"] != source_wire["id"]
            or claim_wire["source_verification_hash"]
            != source_wire["content_hash"]
            or numeric_wire.get("subject_ref") != spec_wire["id"]
            or numeric_wire.get("subject_hash") != spec_wire["content_hash"]
            or claim_wire["numeric_spec_ref"] != spec_wire["id"]
            or claim_wire["numeric_spec_hash"] != spec_wire["content_hash"]
            or claim_wire["numeric_verification_ref"] != numeric_wire["id"]
            or claim_wire["numeric_verification_hash"]
            != numeric_wire["content_hash"]
        ):
            raise ResearchAutoCommitRejected(
                "company facts staged authority binding drifted"
            )
        payload = material_wire.get("normalized_payload")
        if not isinstance(payload, Mapping):
            raise ResearchAutoCommitRejected(
                "company facts normalized payload is unavailable"
            )
        expected_parameters = {
            "cik": payload.get("cik"),
            "taxonomy": payload.get("taxonomy"),
            "concept_candidates": payload.get("concept_candidates"),
            "unit": payload.get("unit"),
            "form": payload.get("form"),
            "filed_from": payload.get("filed_from"),
            "filed_to": payload.get("filed_to"),
        }
        current = payload.get("current")
        prior = payload.get("prior")
        if not isinstance(current, Mapping) or not isinstance(prior, Mapping):
            raise ResearchAutoCommitRejected("company facts comparison is unavailable")
        if (
            call.get("operation") != "get_company_facts"
            or parameters != expected_parameters
            or plan_parameters != expected_parameters
            or payload.get("source_record_refs") != records
            or len(records) != 2
            or payload.get("latest_accession") != current.get("accession")
            or payload.get("selection_basis")
            != "ordered_allowlist_latest_10-Q"
            or not isinstance(payload.get("eligible_concepts"), list)
            or not payload.get("eligible_concepts")
            or payload.get("eligible_concepts")[0] != payload.get("concept")
            or payload.get("next_cursor") is not None
            or spec_wire.get("operator") != "growth_percentage"
            or spec_wire.get("output_unit") != "percent"
            or spec_wire.get("output_currency") is not None
            or spec_wire.get("output_scale") != "one"
            or spec_wire.get("output_value") != claim_wire["value"]
        ):
            raise ResearchAutoCommitRejected(
                "candidate is outside the bounded SEC company-facts rule"
            )
        period = f"{current.get('start')}..{current.get('end')}"
        direction = "up" if not claim_wire["value"].startswith("-") else "down"
        expected_statement = (
            f"{payload.get('entity_name')} reported {payload.get('label')} of "
            f"{payload.get('unit')} {current.get('value')} for {period}, "
            f"{direction} {claim_wire['value'].lstrip('-')}% year over year from "
            f"{payload.get('unit')} {prior.get('value')} in the comparable quarter."
        )
        expected_claim = {
            "subject_ref": question["company_ref"],
            "metric_or_aspect": "quarterly_revenue_yoy_growth",
            "period": period,
            "basis": "official-filing-xbrl",
            "normalized_statement": expected_statement,
            "claim_kind": "quantitative",
            "value": spec_wire["output_value"],
            "unit": "percent",
            "currency": None,
            "scale": "one",
            "semantic_verification_status": "unverified",
            "actor_ref": "runner:research-plan-executor",
        }
        finding = "matched exact deterministic SEC company-facts growth rule"
    if any(claim_wire[field] != expected for field, expected in expected_claim.items()):
        raise ResearchAutoCommitRejected(
            "candidate semantics do not match the deterministic SEC policy rule"
        )

    semantics = {
        field: claim_wire[field]
        for field in (
            "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement"
        )
    }
    policy_ref = policy_version.get("policy_version_id")
    policy_hash = policy_version.get("content_hash")
    if not isinstance(policy_ref, str) or not isinstance(policy_hash, str):
        raise ResearchAutoCommitRejected("active policy version binding is unavailable")
    created_at = max(claim_wire["created_at"], str(policy_version.get("created_at", "")))
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "policy-commit:" + content_hash({
            "candidate_claim_ref": claim_wire["id"],
            "candidate_claim_hash": claim_wire["content_hash"],
            "policy_version_ref": policy_ref,
            "policy_version_hash": policy_hash,
            "rule_ref": selected_rule,
        }),
        "created_at": created_at,
        "candidate_claim_ref": claim_wire["id"],
        "candidate_claim_hash": claim_wire["content_hash"],
        "candidate_evidence_ref": evidence_wire["id"],
        "candidate_evidence_hash": evidence_wire["content_hash"],
        "verdict": "accept",
        "reviewed_semantics": semantics,
        "proposed_revisions": None,
        "relation": "supports",
        "rationale": "Candidate matched the active bounded research auto-commit policy.",
        "findings": [finding],
        "reviewer_ref": ACTOR_REF,
        "authorization": "versioned_governance_policy",
        "source": "governance_policy",
        "source_event_ref": f"governance-policy:{policy_ref}:{policy_hash}",
        "policy_version_ref": policy_ref,
        "policy_version_hash": policy_hash,
        "rule_ref": selected_rule,
    }
    base["content_hash"] = content_hash(base)
    return validate_policy_commit_decision(base)


__all__ = [
    "ACTOR_REF",
    "COMPANY_FACTS_RULE_REF",
    "RULE_REF",
    "ResearchAutoCommitError",
    "ResearchAutoCommitRejected",
    "authorize_policy_candidate",
    "validate_policy_commit_decision",
]
