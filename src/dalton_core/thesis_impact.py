"""Authority-bound Claim -> thesis impact judgments.

This slice gives formal financial claims one real downstream consumer without
letting a deterministic fact, a worker, or one model call mutate a thesis.
The producer and verifier must each have a successful immutable Scheduler
ResultEnvelope and a complete Core ModelInvocation.  Both model outputs bind
the exact ClaimVersion/ThesisVersion (or assessment) refs and hashes.

The resulting records are append-only pre-commit judgments.  A later thesis
updater may consume only ``eligible_assessment`` output; this module itself
never stages or commits a ThesisVersion and never adds a human approval gate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ClaimVersion, EvidenceRelation, ModelInvocation, ResultEnvelope, ThesisVersion, WorkOrder
from .policy import check_independence, evaluate_gate
from .research_review import validate_claim_version_v0_2
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
IMPACTS = frozenset({"supports", "weakens", "no_change", "insufficient"})
VERDICTS = frozenset({"pass", "reject"})
_SCHEMA_PATH = Path(__file__).with_name("thesis_impact_schema.sql")
_THESIS_CONTENT_FIELDS = (
    "statement",
    "mechanism",
    "confidence",
    "implied_expectation",
    "claim_refs",
    "catalyst_refs",
    "falsifier_refs",
    "change_reason",
)
_ASSESSMENT_FIELDS = {
    "schema_version", "id", "created_at", "claim_version_ref",
    "claim_version_hash", "thesis_version_ref", "thesis_version_hash",
    "driver_statement", "impact", "rationale", "follow_up_question",
    "producer_invocation_ref", "producer_invocation_hash",
    "producer_result_envelope_ref", "producer_result_envelope_hash",
    "input_context_hash", "content_hash",
}
_VERIFICATION_FIELDS = {
    "schema_version", "id", "created_at", "assessment_ref", "assessment_hash",
    "verdict", "findings", "verifier_invocation_ref", "verifier_invocation_hash",
    "verifier_result_envelope_ref", "verifier_result_envelope_hash",
    "policy_version_ref", "policy_version_hash", "content_hash",
}


class ThesisImpactError(Exception):
    pass


class ThesisImpactValidationError(ThesisImpactError, ValueError):
    pass


class ThesisImpactConflict(ThesisImpactError):
    pass


class ThesisImpactNotFound(ThesisImpactError):
    pass


class ThesisImpactIneligible(ThesisImpactError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThesisImpactValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ThesisImpactValidationError(f"{name} must be lowercase SHA-256")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ThesisImpactValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ThesisImpactValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _canonical_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ThesisImpactConflict(f"{name} canonical JSON is missing")
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ThesisImpactConflict(f"{name} is not valid JSON") from exc
    if type(result) is not dict or canonical_json(result) != value:
        raise ThesisImpactConflict(f"{name} is not a canonical JSON object")
    return result


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ThesisImpactValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        missing = sorted(fields - set(wire))
        extra = sorted(set(wire) - fields)
        raise ThesisImpactValidationError(
            f"{name} has invalid fields; missing={missing}, extra={extra}"
        )
    return wire


def validate_thesis_impact_model_output(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the model-owned, id-free assessment payload."""

    wire = _closed(value, {
        "schema_version", "claim_version_ref", "claim_version_hash",
        "thesis_version_ref", "thesis_version_hash", "driver_statement",
        "impact", "rationale", "follow_up_question",
    }, "ThesisImpactModelOutput")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ThesisImpactValidationError("unsupported assessment schema_version")
    for field in ("claim_version_ref", "thesis_version_ref", "driver_statement", "rationale"):
        wire[field] = _text(wire[field], field)
    wire["claim_version_hash"] = _hash(wire["claim_version_hash"], "claim_version_hash")
    wire["thesis_version_hash"] = _hash(wire["thesis_version_hash"], "thesis_version_hash")
    if wire["impact"] not in IMPACTS:
        raise ThesisImpactValidationError(f"invalid impact: {wire['impact']!r}")
    if wire["impact"] == "insufficient":
        wire["follow_up_question"] = _text(
            wire["follow_up_question"], "follow_up_question"
        )
    elif wire["follow_up_question"] is not None:
        raise ThesisImpactValidationError(
            "follow_up_question is only allowed for insufficient evidence"
        )
    return wire


def validate_thesis_impact_verifier_output(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the independent verifier's exact target and verdict."""

    wire = _closed(value, {
        "schema_version", "assessment_ref", "assessment_hash", "verdict", "findings",
    }, "ThesisImpactVerifierOutput")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ThesisImpactValidationError("unsupported verification schema_version")
    wire["assessment_ref"] = _text(wire["assessment_ref"], "assessment_ref")
    wire["assessment_hash"] = _hash(wire["assessment_hash"], "assessment_hash")
    if wire["verdict"] not in VERDICTS:
        raise ThesisImpactValidationError(f"invalid verdict: {wire['verdict']!r}")
    if not isinstance(wire["findings"], list) or not all(
        isinstance(item, Mapping) for item in wire["findings"]
    ):
        raise ThesisImpactValidationError("findings must be an array of objects")
    wire["findings"] = [dict(item) for item in wire["findings"]]
    return wire


class ThesisImpactAuthority:
    """Append-only impact authority layered on Core and Scheduler."""

    def __init__(self, store: Any, scheduler: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("store must be a DaltonStore-like authority")
        if not hasattr(scheduler, "connection"):
            raise TypeError("scheduler must be a Scheduler-like authority")
        self.store = store
        self.scheduler = scheduler
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _record(base: Mapping[str, Any]) -> dict[str, Any]:
        wire = dict(base)
        wire["content_hash"] = content_hash(wire)
        return wire

    @staticmethod
    def _read_claim(cur: sqlite3.Cursor, claim_version_ref: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM claim_versions WHERE claim_version_id=?",
            (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ThesisImpactNotFound(f"formal ClaimVersion not found: {claim_version_ref}")
        wire = _canonical_object(row["claim_json"], "ClaimVersion")
        if (
            wire.get("id") != claim_version_ref
            or wire.get("claim_ref") != row["claim_ref"]
            or wire.get("version") != row["version_number"]
            or wire.get("prior_version_ref") != row["prior_version_id"]
            or wire.get("created_at") != row["created_at"]
            or wire.get("content_hash") != row["content_hash"]
        ):
            raise ThesisImpactConflict("ClaimVersion SQL columns drifted")
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        if asserted != content_hash(body):
            raise ThesisImpactConflict("ClaimVersion content hash mismatch")
        try:
            if wire.get("schema_version") == "0.1":
                ClaimVersion.from_dict(wire)
            elif wire.get("schema_version") == "0.2":
                wire = validate_claim_version_v0_2(wire)
            else:
                raise ThesisImpactConflict("unsupported ClaimVersion schema_version")
        except ThesisImpactConflict:
            raise
        except Exception as exc:
            raise ThesisImpactConflict("formal ClaimVersion is invalid") from exc
        relations = cur.execute(
            "SELECT * FROM evidence_relations WHERE claim_version_id=? ORDER BY relation_id",
            (claim_version_ref,),
        ).fetchall()
        if not relations:
            raise ThesisImpactIneligible("formal ClaimVersion has no EvidenceRelation")
        for relation_row in relations:
            relation = _canonical_object(relation_row["relation_json"], "EvidenceRelation")
            try:
                validated = EvidenceRelation.from_dict(relation).to_dict()
            except Exception as exc:
                raise ThesisImpactConflict("EvidenceRelation is invalid") from exc
            if (
                validated["id"] != relation_row["relation_id"]
                or validated["claim_version_ref"] != claim_version_ref
                or validated["content_hash"] != relation_row["content_hash"]
                or validated["content_hash"] != content_hash({
                    key: value for key, value in validated.items() if key != "content_hash"
                })
            ):
                raise ThesisImpactConflict("EvidenceRelation authority drifted")
        return wire

    @staticmethod
    def _read_current_thesis(
        cur: sqlite3.Cursor, thesis_version_ref: str
    ) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM thesis_versions WHERE version_id=?", (thesis_version_ref,)
        ).fetchone()
        if row is None:
            raise ThesisImpactNotFound(f"ThesisVersion not found: {thesis_version_ref}")
        wire = _canonical_object(row["content_json"], "ThesisVersion")
        try:
            wire = ThesisVersion.from_dict(wire).to_dict()
        except Exception as exc:
            raise ThesisImpactConflict("ThesisVersion is invalid") from exc
        payload = {field: wire[field] for field in _THESIS_CONTENT_FIELDS}
        if (
            wire["id"] != thesis_version_ref
            or wire["thesis_ref"] != row["thesis_id"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["verification_ref"] != row["verification_id"]
            or wire["content_hash"] != row["content_hash"]
            or content_hash(payload) != row["content_hash"]
        ):
            raise ThesisImpactConflict("ThesisVersion authority drifted")
        pointer = cur.execute(
            "SELECT * FROM current_pointers WHERE thesis_id=?", (wire["thesis_ref"],)
        ).fetchone()
        if (
            pointer is None
            or pointer["version_id"] != thesis_version_ref
            or pointer["version_number"] != wire["version"]
            or pointer["content_hash"] != row["content_hash"]
        ):
            raise ThesisImpactIneligible("assessment must target the current ThesisVersion")
        return wire

    @staticmethod
    def _read_invocation(
        cur: sqlite3.Cursor, invocation_id: str
    ) -> tuple[dict[str, Any], str]:
        row = cur.execute(
            "SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise ThesisImpactConflict("Core model invocation is missing")
        wire = _canonical_object(row["invocation_json"], "ModelInvocation")
        wire.pop("invocation_id", None)
        try:
            wire = ModelInvocation.from_dict(wire).to_dict()
        except Exception as exc:
            raise ThesisImpactConflict("Core ModelInvocation is invalid") from exc
        if (
            wire["id"] != row["invocation_id"]
            or wire["work_order_ref"] != row["work_order_ref"]
            or wire["model_family"] != row["model_family"]
            or wire["provider"] != row["provider"]
            or wire["model"] != row["model"]
        ):
            raise ThesisImpactConflict("Core ModelInvocation SQL columns drifted")
        return wire, content_hash(wire)

    @classmethod
    def _ensure_invocation(
        cls, store: Any, cur: sqlite3.Cursor, invocation: Any
    ) -> tuple[dict[str, Any], str]:
        try:
            store._ensure_invocation(cur, invocation)
        except Exception as exc:
            raise ThesisImpactValidationError("model invocation is invalid") from exc
        invocation_id = (
            invocation.id
            if isinstance(invocation, ModelInvocation)
            else dict(invocation).get("id")
        )
        if not invocation_id:
            raise ThesisImpactValidationError("model invocation id is required")
        return cls._read_invocation(cur, invocation_id)

    def _scheduler_model_output(
        self, result_envelope_ref: str, expected_input_refs: Sequence[str]
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        result_envelope_ref = _text(result_envelope_ref, "result_envelope_ref")
        conn = self.scheduler.connection
        row = conn.execute(
            "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (result_envelope_ref,),
        ).fetchone()
        if row is None:
            raise ThesisImpactNotFound(
                f"Scheduler ResultEnvelope not found: {result_envelope_ref}"
            )
        result = _canonical_object(row["result_envelope_json"], "ResultEnvelope")
        try:
            result = ResultEnvelope.from_dict(result).to_dict()
        except Exception as exc:
            raise ThesisImpactConflict("Scheduler ResultEnvelope is invalid") from exc
        result_hash = content_hash(result)
        receipt = {
            "result_envelope_id": row["result_envelope_id"],
            "work_order_id": row["work_order_id"],
            "attempt_number": row["attempt_number"],
            "result_envelope_hash": row["result_envelope_hash"],
            "outcome": row["outcome"],
            "created_at": row["created_at"],
        }
        if (
            result["id"] != result_envelope_ref
            or result["status"] != "succeeded"
            or result.get("error") is not None
            or row["outcome"] != "succeeded"
            or row["result_envelope_hash"] != result_hash
            or row["content_hash"] != content_hash(receipt)
        ):
            raise ThesisImpactConflict("Scheduler ResultEnvelope authority drifted")
        formal = conn.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (result["work_order_ref"],),
        ).fetchone()
        if formal is None:
            raise ThesisImpactConflict("Scheduler formal result is missing")
        formal_body = {
            "id": formal["result_record_id"],
            "work_order_id": formal["work_order_id"],
            "attempt_number": formal["attempt_number"],
            "result_envelope_id": formal["result_envelope_id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "terminal_state": formal["terminal_state"],
            "created_at": formal["created_at"],
        }
        if (
            formal["terminal_state"] != "succeeded"
            or formal["result_envelope_id"] != result_envelope_ref
            or formal["result_envelope_hash"] != result_hash
            or formal["result_envelope_json"] != canonical_json(result)
            or formal["content_hash"] != content_hash(formal_body)
        ):
            raise ThesisImpactConflict("Scheduler formal result authority drifted")
        work_row = conn.execute(
            "SELECT * FROM scheduler_work_orders WHERE work_order_id=?",
            (result["work_order_ref"],),
        ).fetchone()
        if work_row is None:
            raise ThesisImpactConflict("Scheduler WorkOrder is missing")
        work = _canonical_object(work_row["work_order_json"], "WorkOrder")
        try:
            work = WorkOrder.from_dict(work).to_dict()
        except Exception as exc:
            raise ThesisImpactConflict("Scheduler WorkOrder is invalid") from exc
        expected = list(expected_input_refs)
        if (
            work["id"] != result["work_order_ref"]
            or work_row["work_order_hash"] != content_hash(work)
            or work["input_refs"] != expected
        ):
            raise ThesisImpactConflict("Scheduler WorkOrder input binding drifted")
        outputs = result["outputs"]
        if set(outputs) != {"text", "content_hash"}:
            raise ThesisImpactValidationError(
                "model ResultEnvelope.outputs must contain only text and content_hash"
            )
        text = _text(outputs["text"], "ResultEnvelope.outputs.text")
        if outputs["content_hash"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ThesisImpactConflict("model output text hash mismatch")
        try:
            model_output = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ThesisImpactValidationError("model output text must be JSON") from exc
        if not isinstance(model_output, Mapping):
            raise ThesisImpactValidationError("model output JSON must be an object")
        return result, dict(model_output), result_hash

    def record_assessment(
        self,
        *,
        claim_version_ref: str,
        thesis_version_ref: str,
        producer_invocation: ModelInvocation | Mapping[str, Any],
        producer_result_envelope_ref: str,
    ) -> dict[str, Any]:
        """Persist one model judgment without changing the thesis."""

        claim_version_ref = _text(claim_version_ref, "claim_version_ref")
        thesis_version_ref = _text(thesis_version_ref, "thesis_version_ref")
        result, raw_output, result_hash = self._scheduler_model_output(
            producer_result_envelope_ref,
            [claim_version_ref, thesis_version_ref],
        )
        output = validate_thesis_impact_model_output(raw_output)
        with self.store._transaction() as cur:
            claim = self._read_claim(cur, claim_version_ref)
            thesis = self._read_current_thesis(cur, thesis_version_ref)
            if (
                output["claim_version_ref"] != claim_version_ref
                or output["claim_version_hash"] != claim["content_hash"]
                or output["thesis_version_ref"] != thesis_version_ref
                or output["thesis_version_hash"] != thesis["content_hash"]
                or output["driver_statement"] != thesis["mechanism"]
            ):
                raise ThesisImpactConflict("model assessment input binding drifted")
            invocation, invocation_hash = self._ensure_invocation(
                self.store, cur, producer_invocation
            )
            if (
                result["invocation_ref"] != invocation["id"]
                or result["work_order_ref"] != invocation["work_order_ref"]
                or invocation["input_refs"] != [claim_version_ref, thesis_version_ref]
            ):
                raise ThesisImpactConflict("producer invocation binding drifted")
            input_context_hash = content_hash({
                "claim_version_ref": claim_version_ref,
                "claim_version_hash": claim["content_hash"],
                "thesis_version_ref": thesis_version_ref,
                "thesis_version_hash": thesis["content_hash"],
                "driver_statement": thesis["mechanism"],
            })
            identity = {
                "model_output": output,
                "producer_invocation_ref": invocation["id"],
                "producer_result_envelope_ref": result["id"],
            }
            assessment_id = "thesis-impact:" + content_hash(identity)[:32]
            existing = cur.execute(
                "SELECT record_json FROM thesis_impact_assessments WHERE assessment_id=? "
                "OR producer_result_envelope_ref=?",
                (assessment_id, result["id"]),
            ).fetchone()
            if existing is not None:
                saved = _canonical_object(existing["record_json"], "ThesisImpactAssessment")
                if saved["id"] == assessment_id:
                    return {"status": "duplicate", "assessment": saved}
                raise ThesisImpactConflict("producer result already identifies another assessment")
            created_at = _now()
            record = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": assessment_id,
                "created_at": created_at,
                **output,
                "producer_invocation_ref": invocation["id"],
                "producer_invocation_hash": invocation_hash,
                "producer_result_envelope_ref": result["id"],
                "producer_result_envelope_hash": result_hash,
                "input_context_hash": input_context_hash,
            })
            cur.execute(
                "INSERT INTO thesis_impact_assessments(assessment_id,claim_version_ref,"
                "claim_version_hash,thesis_version_ref,thesis_version_hash,impact,"
                "producer_invocation_ref,producer_result_envelope_ref,"
                "producer_result_envelope_hash,input_context_hash,record_json,content_hash,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record["claim_version_ref"], record["claim_version_hash"],
                    record["thesis_version_ref"], record["thesis_version_hash"], record["impact"],
                    record["producer_invocation_ref"], record["producer_result_envelope_ref"],
                    record["producer_result_envelope_hash"], record["input_context_hash"],
                    canonical_json(record), record["content_hash"], record["created_at"],
                ),
            )
            return {"status": "fresh", "assessment": record}

    def route_claim(
        self,
        *,
        claim_version_ref: str,
        thesis_ref: str,
        mandate_version_ref: str,
        company_ref: str,
        backlog: Any,
        actor_ref: str = "system:thesis-impact",
    ) -> dict[str, Any]:
        """Route a formal claim to assessment or to one durable follow-up.

        A company with a current thesis gets the exact version/hash needed by
        ``record_assessment``.  If no thesis exists, the Claim is preserved and
        an idempotent ResearchQuestionBacklog item asks which driver it should
        test.  The fallback creates no placeholder thesis and needs no owner
        review.
        """

        claim_version_ref = _text(claim_version_ref, "claim_version_ref")
        thesis_ref = _text(thesis_ref, "thesis_ref")
        mandate_version_ref = _text(mandate_version_ref, "mandate_version_ref")
        company_ref = _text(company_ref, "company_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        if not hasattr(backlog, "record_question"):
            raise TypeError("backlog must be a ResearchQuestionBacklog-like authority")
        cur = self.connection.cursor()
        claim = self._read_claim(cur, claim_version_ref)
        if claim["subject_ref"] != company_ref:
            raise ThesisImpactConflict("ClaimVersion subject_ref does not match company_ref")
        pointer = cur.execute(
            "SELECT version_id FROM current_pointers WHERE thesis_id=?", (thesis_ref,)
        ).fetchone()
        if pointer is not None:
            thesis = self._read_current_thesis(cur, pointer["version_id"])
            return {
                "status": "assessment_required",
                "claim_version_ref": claim_version_ref,
                "claim_version_hash": claim["content_hash"],
                "thesis_version_ref": thesis["id"],
                "thesis_version_hash": thesis["content_hash"],
                "driver_statement": thesis["mechanism"],
            }
        question = (
            f"Which investment driver should the formal claim '{claim['normalized_statement']}' "
            "test, and what evidence would distinguish supports, weakens, or no change?"
        )
        answer_criteria = (
            "Identify a current or proposed thesis driver, bind the exact formal ClaimVersion, "
            "and determine supports, weakens, no_change, or insufficient with independent verification."
        )
        recorded = backlog.record_question(
            mandate_version_ref=mandate_version_ref,
            company_ref=company_ref,
            question=question,
            answer_criteria=answer_criteria,
            source_refs=[claim_version_ref],
            actor_ref=actor_ref,
            idempotency_key="thesis-impact:unmapped:" + content_hash({
                "claim_version_ref": claim_version_ref,
                "claim_version_hash": claim["content_hash"],
                "thesis_ref": thesis_ref,
                "mandate_version_ref": mandate_version_ref,
                "company_ref": company_ref,
            }),
        )
        return {
            "status": "follow_up_recorded",
            "claim_version_ref": claim_version_ref,
            "thesis_ref": thesis_ref,
            "backlog": recorded,
        }

    def _read_assessment(self, cur: sqlite3.Cursor, assessment_ref: str) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM thesis_impact_assessments WHERE assessment_id=?",
            (assessment_ref,),
        ).fetchone()
        if row is None:
            raise ThesisImpactNotFound(f"assessment not found: {assessment_ref}")
        wire = _canonical_object(row["record_json"], "ThesisImpactAssessment")
        if set(wire) != _ASSESSMENT_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
            raise ThesisImpactConflict("assessment has an invalid closed shape")
        _timestamp(wire.get("created_at"), "assessment.created_at")
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        if asserted != content_hash(body):
            raise ThesisImpactConflict("assessment content hash mismatch")
        columns = {
            "id": row["assessment_id"],
            "claim_version_ref": row["claim_version_ref"],
            "claim_version_hash": row["claim_version_hash"],
            "thesis_version_ref": row["thesis_version_ref"],
            "thesis_version_hash": row["thesis_version_hash"],
            "impact": row["impact"],
            "producer_invocation_ref": row["producer_invocation_ref"],
            "producer_result_envelope_ref": row["producer_result_envelope_ref"],
            "producer_result_envelope_hash": row["producer_result_envelope_hash"],
            "input_context_hash": row["input_context_hash"],
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
        }
        if any(wire.get(field) != value for field, value in columns.items()):
            raise ThesisImpactConflict("assessment SQL columns drifted")
        validate_thesis_impact_model_output({
            key: wire[key] for key in (
                "schema_version", "claim_version_ref", "claim_version_hash",
                "thesis_version_ref", "thesis_version_hash", "driver_statement",
                "impact", "rationale", "follow_up_question",
            )
        })
        claim = self._read_claim(cur, wire["claim_version_ref"])
        thesis = self._read_current_thesis(cur, wire["thesis_version_ref"])
        producer, producer_hash = self._read_invocation(
            cur, wire["producer_invocation_ref"]
        )
        result, raw_output, result_hash = self._scheduler_model_output(
            wire["producer_result_envelope_ref"],
            [wire["claim_version_ref"], wire["thesis_version_ref"]],
        )
        output = validate_thesis_impact_model_output(raw_output)
        if (
            claim["content_hash"] != wire["claim_version_hash"]
            or thesis["content_hash"] != wire["thesis_version_hash"]
            or thesis["mechanism"] != wire["driver_statement"]
            or producer_hash != wire["producer_invocation_hash"]
            or producer["input_refs"]
            != [wire["claim_version_ref"], wire["thesis_version_ref"]]
            or result["invocation_ref"] != producer["id"]
            or result["work_order_ref"] != producer["work_order_ref"]
            or result_hash != wire["producer_result_envelope_hash"]
            or canonical_json(output)
            != canonical_json({
                key: wire[key]
                for key in (
                    "schema_version", "claim_version_ref", "claim_version_hash",
                    "thesis_version_ref", "thesis_version_hash", "driver_statement",
                    "impact", "rationale", "follow_up_question",
                )
            })
        ):
            raise ThesisImpactConflict("assessment authority chain drifted")
        return wire

    def verify_assessment(
        self,
        *,
        assessment_ref: str,
        verifier_invocation: ModelInvocation | Mapping[str, Any],
        verifier_result_envelope_ref: str,
    ) -> dict[str, Any]:
        """Persist one independent verification; still no thesis mutation."""

        assessment_ref = _text(assessment_ref, "assessment_ref")
        result, raw_output, result_hash = self._scheduler_model_output(
            verifier_result_envelope_ref, [assessment_ref]
        )
        output = validate_thesis_impact_verifier_output(raw_output)
        with self.store._transaction() as cur:
            assessment = self._read_assessment(cur, assessment_ref)
            if (
                output["assessment_ref"] != assessment_ref
                or output["assessment_hash"] != assessment["content_hash"]
            ):
                raise ThesisImpactConflict("verifier target binding drifted")
            verifier, verifier_hash = self._ensure_invocation(
                self.store, cur, verifier_invocation
            )
            if (
                result["invocation_ref"] != verifier["id"]
                or result["work_order_ref"] != verifier["work_order_ref"]
                or verifier["input_refs"] != [assessment_ref]
            ):
                raise ThesisImpactConflict("verifier invocation binding drifted")
            self._read_invocation(cur, assessment["producer_invocation_ref"])
            producer_row = cur.execute(
                "SELECT * FROM model_invocations WHERE invocation_id=?",
                (assessment["producer_invocation_ref"],),
            ).fetchone()
            producer = dict(producer_row)
            policy = cur.execute(
                "SELECT v.* FROM governance_policy_pointer p "
                "JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id "
                "WHERE p.pointer_id=1"
            ).fetchone()
            if policy is None:
                raise ThesisImpactIneligible("no active governance policy")
            self.store._assert_policy_effective(policy)
            policy_doc = json.loads(policy["policy_json"])
            independent, reason = check_independence(policy_doc, producer, dict(
                cur.execute(
                    "SELECT * FROM model_invocations WHERE invocation_id=?", (verifier["id"],)
                ).fetchone()
            ))
            if not independent:
                raise ThesisImpactIneligible(reason or "verification is not independent")
            if output["verdict"] == "pass":
                allowed, reason = evaluate_gate(
                    policy_doc,
                    "pass",
                    producer,
                    dict(cur.execute(
                        "SELECT * FROM model_invocations WHERE invocation_id=?", (verifier["id"],)
                    ).fetchone()),
                )
                if not allowed:
                    raise ThesisImpactIneligible(reason or "active policy rejected verification")
            existing = cur.execute(
                "SELECT record_json FROM thesis_impact_verifications WHERE assessment_ref=? "
                "OR verifier_result_envelope_ref=?",
                (assessment_ref, result["id"]),
            ).fetchone()
            verification_id = "thesis-impact-verification:" + content_hash({
                "assessment_ref": assessment_ref,
                "verifier_invocation_ref": verifier["id"],
                "verifier_result_envelope_ref": result["id"],
            })[:32]
            if existing is not None:
                saved = _canonical_object(existing["record_json"], "ThesisImpactVerification")
                if saved["id"] == verification_id:
                    return {"status": "duplicate", "verification": saved}
                raise ThesisImpactConflict("assessment already has another verification")
            created_at = _now()
            record = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": verification_id,
                "created_at": created_at,
                "assessment_ref": assessment_ref,
                "assessment_hash": assessment["content_hash"],
                "verdict": output["verdict"],
                "findings": output["findings"],
                "verifier_invocation_ref": verifier["id"],
                "verifier_invocation_hash": verifier_hash,
                "verifier_result_envelope_ref": result["id"],
                "verifier_result_envelope_hash": result_hash,
                "policy_version_ref": policy["policy_version_id"],
                "policy_version_hash": policy["content_hash"],
            })
            cur.execute(
                "INSERT INTO thesis_impact_verifications(verification_id,assessment_ref,"
                "assessment_hash,verdict,verifier_invocation_ref,verifier_result_envelope_ref,"
                "verifier_result_envelope_hash,policy_version_ref,policy_version_hash,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record["assessment_ref"], record["assessment_hash"],
                    record["verdict"], record["verifier_invocation_ref"],
                    record["verifier_result_envelope_ref"], record["verifier_result_envelope_hash"],
                    record["policy_version_ref"], record["policy_version_hash"],
                    canonical_json(record), record["content_hash"], record["created_at"],
                ),
            )
            return {"status": "fresh", "verification": record}

    def eligible_assessment(self, assessment_ref: str) -> dict[str, Any]:
        """Return an assessment only if its pass is still policy/current-head valid."""

        assessment_ref = _text(assessment_ref, "assessment_ref")
        cur = self.connection.cursor()
        assessment = self._read_assessment(cur, assessment_ref)
        row = cur.execute(
            "SELECT * FROM thesis_impact_verifications WHERE assessment_ref=?",
            (assessment_ref,),
        ).fetchone()
        if row is None:
            raise ThesisImpactIneligible("assessment has no independent verification")
        verification = _canonical_object(row["record_json"], "ThesisImpactVerification")
        if (
            set(verification) != _VERIFICATION_FIELDS
            or verification.get("schema_version") != SCHEMA_VERSION
        ):
            raise ThesisImpactConflict("verification has an invalid closed shape")
        _timestamp(verification.get("created_at"), "verification.created_at")
        body = dict(verification)
        asserted = body.pop("content_hash", None)
        columns = {
            "id": row["verification_id"],
            "assessment_ref": row["assessment_ref"],
            "assessment_hash": row["assessment_hash"],
            "verdict": row["verdict"],
            "verifier_invocation_ref": row["verifier_invocation_ref"],
            "verifier_result_envelope_ref": row["verifier_result_envelope_ref"],
            "verifier_result_envelope_hash": row["verifier_result_envelope_hash"],
            "policy_version_ref": row["policy_version_ref"],
            "policy_version_hash": row["policy_version_hash"],
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
        }
        if (
            asserted != content_hash(body)
            or asserted != row["content_hash"]
            or any(verification.get(field) != value for field, value in columns.items())
            or verification["assessment_hash"] != assessment["content_hash"]
        ):
            raise ThesisImpactConflict("verification content hash mismatch")
        verifier, verifier_hash = self._read_invocation(
            cur, verification["verifier_invocation_ref"]
        )
        result, raw_output, result_hash = self._scheduler_model_output(
            verification["verifier_result_envelope_ref"], [assessment_ref]
        )
        output = validate_thesis_impact_verifier_output(raw_output)
        if (
            verifier_hash != verification["verifier_invocation_hash"]
            or verifier["input_refs"] != [assessment_ref]
            or result["invocation_ref"] != verifier["id"]
            or result["work_order_ref"] != verifier["work_order_ref"]
            or result_hash != verification["verifier_result_envelope_hash"]
            or output["assessment_ref"] != assessment_ref
            or output["assessment_hash"] != assessment["content_hash"]
            or output["verdict"] != verification["verdict"]
            or canonical_json(output["findings"])
            != canonical_json(verification["findings"])
        ):
            raise ThesisImpactConflict("verification authority chain drifted")
        if verification["verdict"] != "pass":
            raise ThesisImpactIneligible("assessment verification did not pass")
        policy = cur.execute(
            "SELECT v.* FROM governance_policy_pointer p "
            "JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id "
            "WHERE p.pointer_id=1"
        ).fetchone()
        if (
            policy is None
            or verification["policy_version_ref"] != policy["policy_version_id"]
            or verification["policy_version_hash"] != policy["content_hash"]
        ):
            raise ThesisImpactIneligible("assessment verification policy is no longer active")
        self.store._assert_policy_effective(policy)
        producer_row = cur.execute(
            "SELECT * FROM model_invocations WHERE invocation_id=?",
            (assessment["producer_invocation_ref"],),
        ).fetchone()
        verifier_row = cur.execute(
            "SELECT * FROM model_invocations WHERE invocation_id=?",
            (verification["verifier_invocation_ref"],),
        ).fetchone()
        allowed, reason = evaluate_gate(
            json.loads(policy["policy_json"]),
            "pass",
            dict(producer_row) if producer_row is not None else {},
            dict(verifier_row) if verifier_row is not None else {},
        )
        if not allowed:
            raise ThesisImpactIneligible(reason or "active policy rejected verification")
        return {"assessment": assessment, "verification": verification}


__all__ = [
    "IMPACTS",
    "ThesisImpactAuthority",
    "ThesisImpactConflict",
    "ThesisImpactError",
    "ThesisImpactIneligible",
    "ThesisImpactNotFound",
    "ThesisImpactValidationError",
    "validate_thesis_impact_model_output",
    "validate_thesis_impact_verifier_output",
]
