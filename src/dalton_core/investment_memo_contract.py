"""Closed, replayable verification contract for Investment Memo candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .store import content_hash

SCHEMA_VERSION = "investment-memo-gate-0.1"
CHECK_REFS = (
    "key_questions_complete",
    "variant_consensus",
    "anti_thesis",
    "risk_reward",
)
QUESTION_COUNT = 12
PRODUCER_GROUPS = ("identity_background", "view", "economics", "monitoring")


class InvestmentMemoContractError(ValueError):
    pass


def _closed(value: Any, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise InvestmentMemoContractError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _ref(value: Any, name: str, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or ":" not in value:
        raise InvestmentMemoContractError(f"{name} must be a namespaced ref")
    if prefix is not None and not value.startswith(prefix):
        raise InvestmentMemoContractError(f"{name} must start with {prefix}")
    return value


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in "0123456789abcdef" for char in value):
        raise InvestmentMemoContractError(f"{name} must be a SHA-256 digest")
    return value


def memo_verified_material(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return all authored and frozen material covered by the verifier verdict."""

    gate = record.get("gate") or {}
    return {
        "kind": record.get("kind"),
        "subject_ref": record.get("subject_ref"),
        "mission_version_ref": record.get("mission_version_ref"),
        "mission_version_hash": record.get("mission_version_hash"),
        "playbook_version_ref": record.get("playbook_version_ref"),
        "playbook_version_hash": record.get("playbook_version_hash"),
        "template_ref": record.get("template_ref"),
        "summary": record.get("summary"),
        "sections": record.get("sections"),
        "gaps": record.get("gaps"),
        "key_questions": gate.get("key_questions"),
        "input_bindings": gate.get("input_bindings"),
    }


def verified_body_hash(record: Mapping[str, Any]) -> str:
    return content_hash(memo_verified_material(record))


def validate_memo_gate(gate: Mapping[str, Any], *, material_hash: str,
                       expected_questions: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    gate = _closed(gate, {
        "schema_version", "passed", "checks", "verified_body_hash",
        "key_questions", "input_bindings", "producer_calls", "verifier",
    }, "gate")
    if gate["schema_version"] != SCHEMA_VERSION or gate["passed"] is not True:
        raise InvestmentMemoContractError("a published memo gate must be a passed 0.1 gate")
    _sha(material_hash, "material_hash")
    _sha(gate["verified_body_hash"], "verified_body_hash")
    if gate["verified_body_hash"] != material_hash:
        raise InvestmentMemoContractError("verifier hash does not bind the memo material")

    questions = gate["key_questions"]
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)) or len(questions) != QUESTION_COUNT:
        raise InvestmentMemoContractError("key_questions must contain the Playbook's 12 answers")
    expected = [(item.get("question_ref"), item.get("question")) for item in expected_questions]
    if len(expected) != QUESTION_COUNT or len(set(expected)) != QUESTION_COUNT:
        raise InvestmentMemoContractError("the bound Playbook questions are not a unique set of 12")
    seen_questions: set[str] = set()
    for index, question in enumerate(questions):
        q = _closed(question, {"question_ref", "question", "answer", "refs", "unknown", "falsifier"}, "question")
        ref = q["question_ref"]
        if not isinstance(ref, str) or not isinstance(q["question"], str) \
                or (ref, q["question"]) != expected[index] or ref in seen_questions:
            raise InvestmentMemoContractError("questions must exactly match the bound Playbook")
        seen_questions.add(ref)
        if q["unknown"] is not False or not isinstance(q["answer"], str) or not q["answer"].strip() \
                or not isinstance(q["falsifier"], str) or not q["falsifier"].strip():
            raise InvestmentMemoContractError("published memo questions must be answered with a falsifier")
        if not isinstance(q["refs"], list) or not q["refs"] or any(
                not isinstance(item, str) or not item.strip() or ":" not in item for item in q["refs"]):
            raise InvestmentMemoContractError("every memo answer must cite evidence")

    bindings = gate["input_bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise InvestmentMemoContractError("input_bindings must be a nonempty list")
    for binding in bindings:
        b = _closed(binding, {"ref", "hash", "kind"}, "input binding")
        _ref(b["ref"], "input binding ref")
        if not isinstance(b["kind"], str) or not b["kind"].strip():
            raise InvestmentMemoContractError("input bindings require ref, hash and kind")
        _sha(b["hash"], "input binding hash")

    checks = gate["checks"]
    if not isinstance(checks, list) or [c.get("check_ref") for c in checks if isinstance(c, Mapping)] != list(CHECK_REFS):
        raise InvestmentMemoContractError("checks must be the four fixed checks in order")
    for check in checks:
        c = _closed(check, {"check_ref", "status", "reason", "evidence_refs"}, "check")
        if c["status"] != "pass" or not isinstance(c["reason"], str) or not c["reason"].strip():
            raise InvestmentMemoContractError("all deterministic checks must pass")
        if not isinstance(c["evidence_refs"], list) or any(
                not isinstance(item, str) or not item.strip() or ":" not in item
                for item in c["evidence_refs"]):
            raise InvestmentMemoContractError("check evidence_refs must be a list")

    calls = gate["producer_calls"]
    if not isinstance(calls, list) or len(calls) != 4 or [
            c.get("group") for c in calls if isinstance(c, Mapping)] != list(PRODUCER_GROUPS):
        raise InvestmentMemoContractError("producer_calls must contain the four drafting calls")
    producer_routes, producer_works = [], []
    for call in calls:
        c = _closed(call, {"group", "work_order_ref", "route_decision_ref",
                           "result_envelope_ref", "invocation_ref"}, "producer call")
        producer_works.append(_ref(c["work_order_ref"], "producer work", "work:"))
        _ref(c["route_decision_ref"], "producer route", "route-decision:")
        _ref(c["result_envelope_ref"], "producer result")
        _ref(c["invocation_ref"], "producer invocation")
        producer_routes.append(c["route_decision_ref"])
    if len(set(producer_routes)) != 4 or len(set(producer_works)) != 4:
        raise InvestmentMemoContractError("producer work and route refs must be unique")

    verifier = _closed(gate["verifier"], {
        "verdict", "work_order_ref", "route_decision_ref",
        "producer_route_decision_refs", "finding_codes", "result_envelope_ref", "invocation_ref",
    }, "verifier")
    if verifier["verdict"] != "pass" or verifier["finding_codes"] != []:
        raise InvestmentMemoContractError("the independent verifier must pass without findings")
    verifier_work = _ref(verifier["work_order_ref"], "verifier work", "work:")
    verifier_route = _ref(verifier["route_decision_ref"], "verifier route", "route-decision:")
    _ref(verifier["result_envelope_ref"], "verifier result")
    _ref(verifier["invocation_ref"], "verifier invocation")
    if verifier_work in producer_works or verifier_route in producer_routes:
        raise InvestmentMemoContractError("verifier provenance must differ from producer provenance")
    if verifier["producer_route_decision_refs"] != producer_routes:
        raise InvestmentMemoContractError("verifier does not bind every producer route")
    return dict(gate)


def model_work_order_refs(gate: Mapping[str, Any]) -> list[str]:
    """Refs stored in MissionDeliverable.model_invocation_refs (historical name)."""
    return [str(call["work_order_ref"]) for call in gate["producer_calls"]] + [
        str(gate["verifier"]["work_order_ref"])
    ]


__all__ = [
    "CHECK_REFS", "PRODUCER_GROUPS", "QUESTION_COUNT", "SCHEMA_VERSION",
    "InvestmentMemoContractError", "memo_verified_material",
    "model_work_order_refs", "validate_memo_gate", "verified_body_hash",
]
