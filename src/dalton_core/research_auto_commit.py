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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
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
# P9b (2026-09-02): the same same-accession quarterly growth rule applied to a
# 10-K that reports the fourth-quarter pair (Accenture).  It is a separate
# rule ref so a policy listing only the 10-Q rule keeps rejecting annual
# candidates; the FY - 9M derivation is not a rule yet.
COMPANY_FACTS_ANNUAL_RULE_REF = (
    "research-auto-commit:sec-public-company-facts-growth-annual:v1"
)
COMPANY_FACTS_RULE_REFS: dict[str, str] = {
    "10-Q": COMPANY_FACTS_RULE_REF,
    "10-K": COMPANY_FACTS_ANNUAL_RULE_REF,
}
# ADR-0005 / P9d-17b: a qualitative candidate drafted by the mission's own
# automation from an acquired, verified original, bound to an exact raw span
# through an automation_verified_raw_span correction set, may enter the Ledger
# under the active policy when this rule is listed.  No number is asserted
# (value/unit/scale are null); the SEC lanes keep numeric authority.
DOCUMENT_QUALITATIVE_RULE_REF = "research-auto-commit:mission-document-qualitative:v1"
KNOWN_RULE_REFS: frozenset[str] = frozenset({RULE_REF, *COMPANY_FACTS_RULE_REFS.values(), DOCUMENT_QUALITATIVE_RULE_REF})
_RULE_FINDINGS: dict[str, str] = {
    RULE_REF: "matched exact deterministic SEC filing-count rule",
    DOCUMENT_QUALITATIVE_RULE_REF: (
        "matched the mission document qualitative admission rule: automation draft, "
        "exact raw-span citation, verified original, no numeric assertion"
    ),
    COMPANY_FACTS_RULE_REF: "matched exact deterministic SEC company-facts growth rule",
    COMPANY_FACTS_ANNUAL_RULE_REF: (
        "matched exact deterministic SEC company-facts annual-filing quarterly growth rule"
    ),
}
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
    rules = rule["rules"]
    # Either the single filing-count rule, or a duplicate-free list drawn
    # from the company-facts growth rules (one per admitted form).  The
    # form of the staged candidate selects the rule that must be listed.
    if (
        not isinstance(rules, list)
        or not rules
        or len(set(rules)) != len(rules)
        or any(item not in KNOWN_RULE_REFS for item in rules)
        or (RULE_REF in rules and rules != [RULE_REF])
    ):
        raise ResearchAutoCommitRejected("active governance policy rule set is not supported")
    max_records = rule["max_records"]
    if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 100:
        raise ResearchAutoCommitRejected("research auto-commit max_records is invalid")
    return {**dict(rule), "selected_rule": rules[0], "rules": rules}


def policy_lists_document_rule(policy_version: Mapping[str, Any]) -> bool:
    """True only for a well-formed active policy that lists the document rule."""

    try:
        return DOCUMENT_QUALITATIVE_RULE_REF in _policy_rule(policy_version)["rules"]
    except ResearchAutoCommitRejected:
        return False


def _decision(policy_version: Mapping[str, Any], *, claim_wire: Mapping[str, Any],
              evidence_wire: Mapping[str, Any], rule_ref: str, rationale: str) -> dict[str, Any]:
    semantics = {
        field: claim_wire[field]
        for field in ("subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement")
    }
    policy_ref = policy_version.get("policy_version_id")
    policy_hash = policy_version.get("content_hash")
    if not isinstance(policy_ref, str) or not isinstance(policy_hash, str):
        raise ResearchAutoCommitRejected("active policy version binding is unavailable")
    created_at = max(claim_wire["created_at"], str(policy_version.get("created_at", "")))
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "policy-commit:" + content_hash({
            "candidate_claim_ref": claim_wire["id"], "candidate_claim_hash": claim_wire["content_hash"],
            "policy_version_ref": policy_ref, "policy_version_hash": policy_hash, "rule_ref": rule_ref,
        }),
        "created_at": created_at,
        "candidate_claim_ref": claim_wire["id"], "candidate_claim_hash": claim_wire["content_hash"],
        "candidate_evidence_ref": evidence_wire["id"], "candidate_evidence_hash": evidence_wire["content_hash"],
        "verdict": "accept", "reviewed_semantics": semantics, "proposed_revisions": None, "relation": "supports",
        "rationale": rationale, "findings": [_RULE_FINDINGS[rule_ref]],
        "reviewer_ref": ACTOR_REF, "authorization": "versioned_governance_policy", "source": "governance_policy",
        "source_event_ref": f"governance-policy:{policy_ref}:{policy_hash}",
        "policy_version_ref": policy_ref, "policy_version_hash": policy_hash, "rule_ref": rule_ref,
    }
    base["content_hash"] = content_hash(base)
    return validate_policy_commit_decision(base)


def _authorize_document_qualitative(
    *, connection: sqlite3.Connection, policy_version: Mapping[str, Any],
    evidence_wire: Mapping[str, Any], claim_wire: Mapping[str, Any],
    material: Mapping[str, Any] | None = None,
    source_verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """ADR-0005: admit one automation-drafted qualitative candidate from a verified original."""

    from .transcript_correction import (
        TRANSCRIPT_EVIDENCE_SOURCE_TYPE, TranscriptCorrectionError,
        validate_persisted_transcript_claim_citation,
    )
    if (
        evidence_wire["version"] != 1 or evidence_wire["prior_version_ref"] is not None
        or claim_wire["version"] != 1 or claim_wire["prior_version_ref"] is not None
    ):
        raise ResearchAutoCommitRejected("revised or chained candidates require escalation")
    if claim_wire["candidate_evidence_refs"] != [{"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}]:
        raise ResearchAutoCommitRejected("candidate claim does not bind the exact evidence")
    if (
        evidence_wire["source_verification_ref"] != claim_wire["source_verification_ref"]
        or evidence_wire["source_verification_hash"] != claim_wire["source_verification_hash"]
    ):
        raise ResearchAutoCommitRejected("candidate verification bindings disagree")
    actor = claim_wire["actor_ref"]
    if not isinstance(actor, str) or not actor.startswith("automation:") or evidence_wire["actor_ref"] != actor:
        raise ResearchAutoCommitRejected("document qualitative rule admits only mission automation candidates")
    if any(claim_wire.get(field) is not None for field in ("value", "unit", "scale", "currency")):
        raise ResearchAutoCommitRejected("document qualitative rule admits no numeric assertion")
    from .document_extraction import statement_asserts_a_value, statement_is_boilerplate
    if statement_asserts_a_value(claim_wire["normalized_statement"]):
        raise ResearchAutoCommitRejected("document qualitative rule admits no numeric statement")
    if statement_is_boilerplate(claim_wire["normalized_statement"]):
        raise ResearchAutoCommitRejected("document qualitative rule admits no disclaimer or boilerplate")
    if evidence_wire["source_type"] == "official_filing":
        return _authorize_registered_annual_qualitative(
            connection=connection, policy_version=policy_version,
            evidence_wire=evidence_wire, claim_wire=claim_wire,
            material=material, source_verification=source_verification,
        )
    expected_operation = {
        (TRANSCRIPT_EVIDENCE_SOURCE_TYPE, "source:alphaengine"): "get_document",
        ("public_web", "source:public-web"): "fetch_get",
    }.get((evidence_wire["source_type"], evidence_wire["source_ref"]))
    if expected_operation is None:
        raise ResearchAutoCommitRejected(
            "document qualitative rule requires an acquired AlphaEngine original or a fetched public-web page"
        )
    refs = evidence_wire["artifact_refs"]
    if len(refs) != 2 or not refs[1]["ref"].startswith("transcript-claim-citation-binding:"):
        raise ResearchAutoCommitRejected("document candidate is missing its exact citation binding")
    try:
        citation = validate_persisted_transcript_claim_citation(connection, refs[1]["ref"], refs[1]["hash"])
    except (TranscriptCorrectionError, sqlite3.Error) as exc:
        raise ResearchAutoCommitRejected("document candidate citation authority is unavailable") from exc
    if not citation.get("claim_eligible"):
        raise ResearchAutoCommitRejected("document candidate citation is not claim eligible")
    row = connection.execute(
        "SELECT record_json,content_hash FROM transcript_correction_set_versions WHERE version_id=?",
        (citation["correction_set_version_ref"],),
    ).fetchone()
    if row is None or row["content_hash"] != citation["correction_set_version_hash"]:
        raise ResearchAutoCommitRejected("document candidate correction set is not exact")
    correction_set = json.loads(row["record_json"])
    if correction_set.get("review_scope") != "automation_verified_raw_span" or correction_set.get("actor_ref") != actor:
        raise ResearchAutoCommitRejected(
            "document qualitative rule requires an automation-verified raw span by the same principal"
        )
    source_row = connection.execute(
        "SELECT record_json,content_hash FROM connector_source_envelopes WHERE source_envelope_id=?",
        (evidence_wire["source_envelope_ref"],),
    ).fetchone()
    source = _record(source_row, "SourceEnvelope")
    # A multi-page acquisition binds its page SourceEnvelope (status partial
    # with a cursor); the Ledger writer re-verifies that the citation's raw
    # bytes are exactly that envelope's artifact.  Here: right source, right
    # operation, exact hash.
    if (
        source["content_hash"] != evidence_wire["source_envelope_hash"]
        or source.get("source") != evidence_wire["source_ref"] or source.get("operation") != expected_operation
    ):
        raise ResearchAutoCommitRejected("document candidate source is not the acquired original")
    return _decision(
        policy_version, claim_wire=claim_wire, evidence_wire=evidence_wire,
        rule_ref=DOCUMENT_QUALITATIVE_RULE_REF,
        rationale="Mission automation draft bound to an exact verified raw span of an acquired original (ADR-0005).",
    )


def _scheduler_work(
    connection: sqlite3.Connection, work_ref: str
) -> dict[str, Any]:
    from .contracts import WorkOrder

    row = connection.execute(
        "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
        "WHERE work_order_id=?", (work_ref,),
    ).fetchone()
    if row is None:
        raise ResearchAutoCommitRejected("annual candidate WorkOrder is unavailable")
    try:
        wire = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
    except Exception as exc:
        raise ResearchAutoCommitRejected("annual candidate WorkOrder is invalid") from exc
    if canonical_json(wire) != row["work_order_json"] or content_hash(wire) != row["work_order_hash"]:
        raise ResearchAutoCommitRejected("annual candidate WorkOrder authority drifted")
    return wire


class _ReadOnlySchedulerAuthority:
    """Narrow Scheduler reader used by policy evaluation without queue writes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def work_order_authority(self, work_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?", (work_ref,)
        ).fetchone()
        if row is None:
            return None
        wire = _scheduler_work(self.connection, work_ref)
        return {"work_order": wire, "work_order_hash": content_hash(wire)}

    def formal_result(self, work_ref: str) -> dict[str, Any] | None:
        from .contracts import ResultEnvelope

        row = self.connection.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (work_ref,),
        ).fetchone()
        if row is None:
            return None
        try:
            envelope = ResultEnvelope.from_dict(
                json.loads(row["result_envelope_json"])
            ).to_dict()
        except Exception as exc:
            raise ResearchAutoCommitRejected(
                "annual candidate upstream formal result is invalid"
            ) from exc
        formal = {
            "id": row["result_record_id"], "work_order_id": row["work_order_id"],
            "attempt_number": row["attempt_number"],
            "result_envelope_id": row["result_envelope_id"],
            "result_envelope_hash": row["result_envelope_hash"],
            "terminal_state": row["terminal_state"], "created_at": row["created_at"],
        }
        if (
            canonical_json(envelope) != row["result_envelope_json"]
            or envelope["id"] != row["result_envelope_id"]
            or envelope["work_order_ref"] != work_ref
            or content_hash(envelope) != row["result_envelope_hash"]
            or content_hash(formal) != row["content_hash"]
        ):
            raise ResearchAutoCommitRejected(
                "annual candidate upstream formal result drifted"
            )
        return {
            **{key: row[key] for key in row.keys() if key != "result_envelope_json"},
            "result_envelope": envelope,
        }


def _model_invocation(
    connection: sqlite3.Connection, invocation_ref: str
) -> dict[str, Any]:
    from .contracts import ModelInvocation

    row = connection.execute(
        "SELECT * FROM model_invocations "
        "WHERE invocation_id=?", (invocation_ref,),
    ).fetchone()
    if row is None:
        raise ResearchAutoCommitRejected("annual candidate model invocation is unavailable")
    try:
        stored = json.loads(row["invocation_json"])
        if (
            not isinstance(stored, Mapping)
            or stored.get("invocation_id") != stored.get("id")
        ):
            raise ValueError("model invocation aliases disagree")
        normalized = dict(stored)
        normalized.pop("invocation_id")
        wire = ModelInvocation.from_dict(normalized).to_dict()
    except Exception as exc:
        raise ResearchAutoCommitRejected("annual candidate model invocation is invalid") from exc
    columns = {
        "id": row["invocation_id"], "profile_ref": row["profile_ref"],
        "provider": row["provider"], "model": row["model"],
        "capability": row["capability"], "runtime_ref": row["runtime_ref"],
        "actor_ref": row["actor_ref"], "environment_hash": row["environment_hash"],
        "granularity": row["granularity"], "work_order_ref": row["work_order_ref"],
        "model_family": row["model_family"],
    }
    if (
        canonical_json(stored) != row["invocation_json"]
        or stored.get("invocation_id") != row["invocation_id"]
        or any(wire.get(key) != value for key, value in columns.items())
    ):
        raise ResearchAutoCommitRejected("annual candidate model invocation drifted")
    return dict(wire)


def _annual_budget_proof(
    *, work: Mapping[str, Any], proof: Mapping[str, Any], phase: str,
    mission: Mapping[str, Any], purpose: str,
) -> dict[str, Any]:
    from .budget_pools import mission_pool_scope
    from .thesis_impact_budget import ThesisImpactBudgetError, ThesisImpactBudgetStore

    budget_path = Path(work["metadata"]["budget_db"])
    try:
        with ThesisImpactBudgetStore(budget_path, read_only=True) as budget_store:
            rows = budget_store.connection.execute(
                "SELECT attempt_number FROM thesis_impact_day_admissions "
                "WHERE work_order_ref=? AND phase=? AND route_decision_ref=?",
                (work["id"], phase, proof["route_decision_ref"]),
            ).fetchall()
            if len(rows) != 1:
                raise ResearchAutoCommitRejected(
                    "annual candidate has no unique model budget admission"
                )
            authority = budget_store.admission(
                work_order_ref=work["id"],
                attempt_number=rows[0]["attempt_number"], phase=phase,
            )
    except (OSError, sqlite3.Error, ThesisImpactBudgetError) as exc:
        raise ResearchAutoCommitRejected(
            "annual candidate model budget authority is unavailable"
        ) from exc
    if authority is None:
        raise ResearchAutoCommitRejected(
            "annual candidate model budget admission is unavailable"
        )
    admission = authority["admission"]
    binding = authority["mission_binding"]
    expected_binding = {
        "mission_ref": mission["mission_ref"],
        "mission_version_ref": mission["id"],
        "mission_version_hash": mission["content_hash"],
        "max_daily_paid_calls": mission["budget"]["max_daily_paid_calls"],
        "max_daily_cost_micros": int(
            Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1_000_000
        ),
        "outer_budget": dict(mission["outer_budget"]),
        **mission_pool_scope(mission, purpose=purpose),
    }
    ceiling = int(Decimal(str(work["budget"]["max_cost_usd"])) * 1_000_000)
    if (
        admission["policy_version_id"] != work["metadata"]["budget_policy_ref"]
        or admission["reserved_micros"] != ceiling
        or admission["route_decision_ref"] != proof["route_decision_ref"]
        or canonical_json(binding) != canonical_json(expected_binding)
    ):
        raise ResearchAutoCommitRejected(
            "annual candidate model budget binding drifted"
        )
    return authority


def _authorize_registered_annual_qualitative(
    *, connection: sqlite3.Connection, policy_version: Mapping[str, Any],
    evidence_wire: Mapping[str, Any], claim_wire: Mapping[str, Any],
    material: Mapping[str, Any] | None,
    source_verification: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Closed ADR-0005 branch for a mission-targeted registered SEC read."""

    from .annual_report_qualitative import (
        build_annual_report_candidate_bundle, validate_draft_output,
        validate_model_proof, validate_verifier_output,
    )
    from .mission_annual_research_executor import (
        AUTHORITY_KIND, _derive_mission_annual_work, _mission_annual_blueprints,
    )
    from .model_router import independent_families
    from .registered_annual_report import validate_retrieval_proof
    from .research_verification import (
        REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE,
        REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH,
        REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
        validate_source_verification_material, validate_verification_bundle,
    )
    from .sec_company_facts_lane import read_active_annual_budget_mission

    if material is None or source_verification is None:
        raise ResearchAutoCommitRejected(
            "annual qualitative candidate requires its exact staged authority bundle"
        )
    material_wire = validate_source_verification_material(material)
    verification = validate_verification_bundle(source_verification)
    if (
        material_wire.get("provenance_mode")
        != REGISTERED_ANNUAL_REPORT_AUTHORITY_MODE
        or material_wire.get("source_type") != "official_filing"
        or material_wire.get("source_ref") != "source:sec-edgar"
        or verification.get("kind") != "source"
        or verification.get("verdict") != "pass"
        or verification.get("subject_ref") != material_wire["id"]
        or verification.get("subject_hash") != material_wire["content_hash"]
        or (verification.get("verifier_ref"), verification.get("verifier_hash"))
        != (REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_REF,
            REGISTERED_ANNUAL_REPORT_SOURCE_VERIFIER_HASH)
        or evidence_wire["source_envelope_ref"] != material_wire["source_envelope_ref"]
        or evidence_wire["source_envelope_hash"] != material_wire["source_envelope_hash"]
        or evidence_wire["artifact_refs"] != [{
            "ref": material_wire["artifact_ref"], "hash": material_wire["artifact_hash"],
        }]
    ):
        raise ResearchAutoCommitRejected(
            "annual qualitative candidate source authority is not exact"
        )
    payload = material_wire.get("normalized_payload")
    if not isinstance(payload, Mapping) or set(payload) != {
        "question", "retrieval_proof", "draft_proof", "verifier_proof",
        "mission_admission",
    }:
        raise ResearchAutoCommitRejected("annual candidate payload has an invalid closed shape")
    admission_binding = payload["mission_admission"]
    if not isinstance(admission_binding, Mapping) or set(admission_binding) != {
        "ref", "hash", "mission_version_ref", "mission_version_hash",
        "repair_target_ref", "repair_target_hash",
    }:
        raise ResearchAutoCommitRejected("annual candidate mission binding is invalid")
    admission_row = connection.execute(
        "SELECT record_json,content_hash FROM mission_annual_research_admissions "
        "WHERE admission_id=?", (admission_binding["ref"],),
    ).fetchone()
    admission = _record(admission_row, "mission annual research admission")
    if (
        admission["content_hash"] != admission_binding["hash"]
        or admission["mission_version_ref"] != admission_binding["mission_version_ref"]
        or admission["mission_version_hash"] != admission_binding["mission_version_hash"]
        or admission["repair_target_ref"] != admission_binding["repair_target_ref"]
        or admission["repair_target_hash"] != admission_binding["repair_target_hash"]
        or admission["actor_ref"] != claim_wire["actor_ref"]
        or admission["company_ref"] != claim_wire["subject_ref"]
    ):
        raise ResearchAutoCommitRejected("annual candidate mission admission drifted")
    try:
        active = read_active_annual_budget_mission(
            connection, admission["mission_version_ref"], admission["company_ref"],
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        raise ResearchAutoCommitRejected("annual candidate mission is no longer active") from exc
    if active["content_hash"] != admission["mission_version_hash"] or (
        "claim" not in set(active["autonomy"]["may_write"])
        or "evidence" not in set(active["autonomy"]["may_write"])
        or "research_task" not in set(active["autonomy"]["may_write"])
    ):
        raise ResearchAutoCommitRejected(
            "annual candidate mission does not grant canonical research writes"
        )
    proof = validate_retrieval_proof(
        payload["retrieval_proof"], expected_request=admission["request"]
    )
    if (
        material_wire["source_content_hash"] != admission["request"]["source_content_hash"]
        or proof["registration"]["company_ref"] != admission["company_ref"]
        or proof["registration"]["source_manifest_ref"]
        not in material_wire["source_lineage"]
    ):
        raise ResearchAutoCommitRejected("annual candidate retrieval source drifted")
    draft_raw, verifier_raw = payload["draft_proof"], payload["verifier_proof"]
    if not isinstance(draft_raw, Mapping) or not isinstance(verifier_raw, Mapping):
        raise ResearchAutoCommitRejected("annual candidate model proofs are invalid")
    draft_work = _scheduler_work(connection, draft_raw.get("work_order_ref"))
    verifier_work = _scheduler_work(connection, verifier_raw.get("work_order_ref"))
    scheduler = _ReadOnlySchedulerAuthority(connection)
    blueprints = _mission_annual_blueprints(admission)
    expected_draft = _derive_mission_annual_work(
        admission, scheduler, blueprints, 1
    )
    expected_verifier = _derive_mission_annual_work(
        admission, scheduler, blueprints, 2
    )
    if (
        canonical_json(draft_work) != canonical_json(expected_draft)
        or canonical_json(verifier_work) != canonical_json(expected_verifier)
    ):
        raise ResearchAutoCommitRejected(
            "annual candidate Work drifted from exact mission derivation"
        )
    for work, stage in (
        (draft_work, "qualitative_model_draft"),
        (verifier_work, "independent_qualitative_verifier"),
    ):
        metadata = work["metadata"]
        if (
            metadata.get("authority_kind") != AUTHORITY_KIND
            or metadata.get("mission_annual_research_admission_ref") != admission["id"]
            or metadata.get("mission_annual_research_admission_hash")
            != admission["content_hash"]
            or metadata.get("repair_target_ref") != admission["repair_target_ref"]
            or metadata.get("repair_target_hash") != admission["repair_target_hash"]
            or metadata.get("stage") != stage
        ):
            raise ResearchAutoCommitRejected("annual candidate Work authority drifted")
    draft = validate_model_proof(draft_raw, stage="qualitative_model_draft", work=draft_work)
    verifier = validate_model_proof(
        verifier_raw, stage="independent_qualitative_verifier", work=verifier_work
    )
    validate_draft_output(draft["output"], match_count=len(proof["matches"]))
    checked = validate_verifier_output(verifier["output"], draft=draft["output"])
    source = _record(connection.execute(
        "SELECT record_json,content_hash FROM connector_source_envelopes "
        "WHERE source_envelope_id=?", (material_wire["source_envelope_ref"],),
    ).fetchone(), "annual candidate SourceEnvelope")
    artifact = _record(connection.execute(
        "SELECT record_json,content_hash FROM observability_artifact_versions_v2 "
        "WHERE version_id=?", (material_wire["artifact_ref"],),
    ).fetchone(), "annual candidate ArtifactVersion")
    connector_invocation = _record(connection.execute(
        "SELECT record_json,content_hash FROM connector_invocations "
        "WHERE connector_invocation_id=?", (source["connector_invocation_ref"],),
    ).fetchone(), "annual candidate ConnectorInvocation")
    if (
        source["content_hash"] != material_wire["source_envelope_hash"]
        or source.get("raw_artifact_version_ref") != artifact["id"]
        or artifact["content_hash"] != material_wire["artifact_hash"]
        or source.get("connector_invocation_ref") != connector_invocation["id"]
        or artifact.get("producer_execution_ref")
        != connector_invocation.get("execution_ref")
        or source.get("raw_response_hash") != artifact.get("artifact_content_hash")
        or source.get("raw_response_hash") != proof["registration"]["source_raw_hash"]
    ):
        raise ResearchAutoCommitRejected(
            "annual candidate Core source authority drifted"
        )
    source_authority = {
        "source_envelope_ref": source["id"],
        "source_envelope_hash": source["content_hash"],
        "raw_artifact_version_ref": artifact["id"],
        "raw_artifact_version_hash": artifact["content_hash"],
        "connector_invocation_ref": connector_invocation["id"],
        "connector_invocation_hash": connector_invocation["content_hash"],
        "source_manifest_ref": proof["registration"]["source_manifest_ref"],
        "source_manifest_hash": proof["registration"]["source_manifest_hash"],
        "source_raw_hash": proof["registration"]["source_raw_hash"],
    }
    expected_bundle = build_annual_report_candidate_bundle(
        question_ref=admission["repair_target_ref"],
        question=admission["planner_inquiry"]["question"], proof=proof,
        draft_proof=draft, verifier_proof=verifier,
        draft_work=expected_draft, verifier_work=expected_verifier,
        actor_ref=admission["actor_ref"], created_at=material_wire["created_at"],
        source_authority=source_authority, mission_admission=admission,
    )
    supplied_bundle = {
        "material": material_wire, "source_verification": verification,
        "evidence": evidence_wire, "claim": claim_wire,
    }
    if any(
        canonical_json(supplied_bundle[key]) != canonical_json(expected_bundle[key])
        for key in supplied_bundle
    ):
        raise ResearchAutoCommitRejected(
            "annual candidate bundle drifted from exact mission authority"
        )
    draft_invocation = _model_invocation(connection, draft["model_invocation_ref"])
    verifier_invocation = _model_invocation(connection, verifier["model_invocation_ref"])
    _annual_budget_proof(
        work=expected_draft, proof=draft, phase="assessment", mission=active,
        purpose="registered_annual_report_draft",
    )
    _annual_budget_proof(
        work=expected_verifier, proof=verifier, phase="verification", mission=active,
        purpose="registered_annual_report_verifier",
    )
    if (
        draft_invocation.get("work_order_ref") != draft_work["id"]
        or verifier_invocation.get("work_order_ref") != verifier_work["id"]
        or draft_invocation.get("model_family") != draft["model_family"]
        or verifier_invocation.get("model_family") != verifier["model_family"]
        or draft_invocation.get("parent_ref") != draft["route_decision_ref"]
        or verifier_invocation.get("parent_ref") != verifier["route_decision_ref"]
        or not independent_families(
            verifier_invocation["model_family"], draft_invocation["model_family"]
        )
        or checked["verdict"] != "pass"
        or checked["verified_statement"] != claim_wire["normalized_statement"]
        or draft["output"]["candidate"]["normalized_statement"]
        != claim_wire["normalized_statement"]
    ):
        raise ResearchAutoCommitRejected(
            "annual candidate lacks an exact independent passing model chain"
        )
    return _decision(
        policy_version, claim_wire=claim_wire, evidence_wire=evidence_wire,
        rule_ref=DOCUMENT_QUALITATIVE_RULE_REF,
        rationale=(
            "Mission automation draft bound to exact cited matches in a registered "
            "SEC annual report and an independent passing verifier (ADR-0005)."
        ),
    )


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
        or wire["rule_ref"] not in KNOWN_RULE_REFS
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
    expected_finding = _RULE_FINDINGS[wire["rule_ref"]]
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

    evidence_wire = validate_candidate_evidence(evidence)
    claim_wire = validate_candidate_claim(claim)
    if claim_wire["claim_kind"] != "quantitative":
        # ADR-0005: a qualitative mission-document candidate is policy
        # authorized only under the explicit document rule; otherwise, and
        # whenever the policy is absent or malformed, it still requires human
        # review (ADR-0003 B).
        if not policy_lists_document_rule(policy_version):
            raise ResearchAutoCommitRejected(
                "qualitative candidates require explicit human review; the active policy "
                "does not list the mission document qualitative rule"
            )
        return _authorize_document_qualitative(
            connection=connection, policy_version=policy_version,
            evidence_wire=evidence_wire, claim_wire=claim_wire,
            material=material, source_verification=source_verification,
        )
    rule = _policy_rule(policy_version)
    selected_rule = rule["selected_rule"]
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
        form = payload.get("form")
        if form not in COMPANY_FACTS_RULE_REFS:
            raise ResearchAutoCommitRejected(
                "company facts form is outside the frozen rule registry"
            )
        # The candidate's form selects the rule; the active policy must list
        # exactly that rule for the commit to be authorized.
        selected_rule = COMPANY_FACTS_RULE_REFS[form]
        if selected_rule not in rule["rules"]:
            raise ResearchAutoCommitRejected(
                "active governance policy does not list the company facts rule for this form"
            )
        expected_parameters = {
            "cik": payload.get("cik"),
            "taxonomy": payload.get("taxonomy"),
            "concept_candidates": payload.get("concept_candidates"),
            "unit": payload.get("unit"),
            "form": form,
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
            != f"ordered_allowlist_latest_{form}"
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
        finding = _RULE_FINDINGS[selected_rule]
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
    "COMPANY_FACTS_ANNUAL_RULE_REF",
    "COMPANY_FACTS_RULE_REF",
    "COMPANY_FACTS_RULE_REFS",
    "KNOWN_RULE_REFS",
    "RULE_REF",
    "ResearchAutoCommitError",
    "ResearchAutoCommitRejected",
    "authorize_policy_candidate",
    "DOCUMENT_QUALITATIVE_RULE_REF",
    "policy_lists_document_rule",
    "validate_policy_commit_decision",
]
