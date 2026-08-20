"""ResearchPlanVersion append-only authority ("Planner thin closure").

An exact selected AgendaDecision binds an exact research-question version;
this slice records one immutable ``ResearchPlanVersion`` from that exact
binding and keeps plan start behind either an explicit human approval or the
active versioned policy's narrow low-risk authorization.

Frozen plan state:

    created -> started

``created`` is recorded in the same Core transaction that advances the
backlog question ``selected -> planned`` with the exact plan binding;
``started`` is recorded in the same Core transaction that binds the
WorkflowRunVersion + root WorkOrder created by the plan control plane and
advances the backlog question ``planned -> in_progress``.  Approval is a
separate append-only authority: exactly one terminal ``accepted``/``rejected``
decision per exact plan version.  Automation/model principals, Agenda
approval and Discord reactions cannot impersonate that human authority; the
only autonomous path is a separate immutable policy authorization for the
closed public SEC read-only scope.

Version 1 is closed to a single SEC public, credential-free, read-only
``list_filings`` research plan:

- ``source_ref``/``connector_profile_ref``/``operation``/output contract are
  frozen to the packaged ``connector-profile-template:sec:0.1`` template;
- the frozen request parameters are exactly ``issuer_cik`` (CIK format), a
  form from the frozen allowlist and a bounded filing-date window;
- permissions are frozen to the public read scope, ``auth_mode=none``, the
  read-only side-effect class and the SEC source verifier output contract;
- the budget/retry bounds and the single deterministic step are rebuilt from
  the same constants/template on every read, so caller-injected mutable
  content, other connectors, credentials, writes, broadened permissions or
  extra steps fail closed.

This slice creates no capability lease and no auto-answer.  It reuses the
existing WorkflowRunVersion, WorkOrderLink and Scheduler authorities.  The
plan contains a closed four-step tree (SEC connector -> authority resolver ->
verifier -> candidate staging).  Start records the complete tree but enqueues
only the root connector WorkOrder; downstream nodes remain planned until a
coordinator observes the exact upstream result and admits them.  Candidate
staging still never writes Evidence/Claim/Thesis directly; the separate
Ledger commit boundary decides whether exact deterministic results qualify
for policy authorization or require human escalation.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agenda import (
    read_exact_agenda_cycle,
    read_exact_agenda_policy_version,
    read_exact_mandate_version,
    read_exact_perception_snapshot,
)
from .connector_inventory import load_packaged_connector_inventory
from .research_question_backlog import (
    _read_exact_agenda_candidate,
    _read_exact_agenda_decision,
    read_exact_backlog_question,
    read_exact_backlog_question_version,
)
from .research_context import build_agenda_context_binding
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_plan_schema.sql")
_IDENTITY_SCHEMA = "research-plan-identity-v1"

# Frozen planner identity for the thin closure planner.
PLANNER_REF = "planner:research-plan-thin:0.1"
PLANNER_HASH = content_hash({"planner_ref": PLANNER_REF})

# Closed SEC public read-only execution scope (v0.1 plan policy).
SEC_SOURCE_REF = "source:sec-edgar"
SEC_PROFILE_REF = "connector-profile-template:sec:0.1"
SEC_OPERATION = "list_filings"
SEC_OUTPUT_CONTRACT_REF = "schema:connector-inventory:sec:list_filings:output:0.1"
SEC_COMPANY_FACTS_OPERATION = "get_company_facts"
SEC_COMPANY_FACTS_OUTPUT_CONTRACT_REF = (
    "schema:connector-inventory:sec:get_company_facts:output:0.1"
)
SEC_VERIFIER_REF = "verifier:connector-authority-source:0.2"
SEC_CAPABILITY = "capability:dalton:connector:sec-edgar"
SEC_RUNTIME_PROFILE_REF = "runner-runtime:sec-public:v1"
RESOLVER_CAPABILITY = "capability:dalton:connector-authority-resolver"
RESOLVER_RUNTIME_PROFILE_REF = "runtime:dalton-core:authority-resolver:0.2"
VERIFIER_CAPABILITY = "capability:dalton:research-verification"
VERIFIER_RUNTIME_PROFILE_REF = "runtime:dalton-core:research-verification:0.2"
STAGING_CAPABILITY = "capability:dalton:candidate-staging"
STAGING_RUNTIME_PROFILE_REF = "runtime:dalton-core:candidate-staging:0.1"
SEC_ALLOWED_FORMS = frozenset({"10-K", "10-Q", "8-K"})
SEC_WINDOW_MAX_DAYS = 366
SEC_MAX_ATTEMPTS = 2
SEC_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SEC_MAX_SECONDS = 60
PERMISSION_SCOPE = "public_sec_list_filings"
COMPANY_FACTS_PERMISSION_SCOPE = "public_sec_company_facts"
SIDE_EFFECT_CLASS = "read_only_public"
PLAN_AUTO_START_RULE_REF = "research-plan-auto-start:sec-public-list-filings:v1"
PLAN_COMPANY_FACTS_AUTO_START_RULE_REF = (
    "research-plan-auto-start:sec-public-company-facts:v1"
)
PLAN_AUTO_START_ACTOR_REF = "system:research-plan-auto-start"

_CAMEL_CHARS = set("0123456789abcdef")

PLAN_STATES = ("pending", "approved", "rejected", "started")
_PLAN_TRANSITIONS = {
    None: {"created"},
    "created": {"started"},
    "approved": set(),
    "rejected": set(),
    "started": set(),
}

# Closed agenda binding carried by every plan version.
_AGENDA_BINDING_FIELDS = frozenset({
    "decision_ref", "decision_hash", "cycle_ref", "cycle_hash", "candidate_ref",
    "mandate_version_ref", "mandate_version_hash", "policy_version_ref",
    "policy_version_hash",
})
_EXECUTION_SCOPE_FIELDS = frozenset({
    "source_ref", "connector_profile_ref", "connector_profile_hash", "operation",
    "parameters", "permission_scope", "auth_mode", "side_effect_class",
    "declared_side_effects", "verifier_ref", "output_contract_ref",
    "output_contract_hash", "budget", "steps",
})
_PLAN_FIELDS = frozenset({
    "schema_version", "id", "created_at", "planner_ref", "planner_hash",
    "version", "prior_version_ref", "question_ref", "question_version_ref",
    "question_version_hash", "agenda_binding", "context_binding_ref",
    "context_binding_hash", "execution_scope", "actor_ref", "content_hash",
})
_APPROVAL_FIELDS = frozenset({
    "schema_version", "id", "created_at", "plan_version_ref",
    "plan_version_hash", "decision", "reason", "actor_ref", "content_hash",
})
_POLICY_AUTHORIZATION_FIELDS = frozenset({
    "schema_version", "id", "created_at", "plan_version_ref",
    "plan_version_hash", "decision", "reason", "actor_ref",
    "authorization", "policy_version_ref", "policy_version_hash",
    "rule_ref", "content_hash",
})
_START_FIELDS = frozenset({
    "schema_version", "id", "created_at", "plan_version_ref",
    "plan_version_hash", "approval_ref", "approval_hash", "workflow_ref",
    "workflow_version_ref", "workflow_version_hash", "root_work_order_ref",
    "root_work_order_hash", "event_ref", "actor_ref", "content_hash",
})
_EVENT_FIELDS = frozenset({
    "schema_version", "id", "created_at", "plan_version_ref", "state",
    "reason", "metadata", "actor_ref", "content_hash",
})
_STEP_FIELDS = frozenset({
    "schema_version", "id", "ordinal", "stage", "operation", "parameters",
    "depends_on", "requested_capabilities", "runtime_profile_ref",
    "declared_side_effects", "output_contract_ref", "max_attempts",
    "work_order_ref", "content_hash",
})
_BUDGET_FIELDS = frozenset({
    "max_attempts", "max_pages", "max_response_bytes", "max_seconds",
})

_HUMAN_ACTOR_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_CIK_RE = re.compile(r"[0-9]{1,10}\Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_XBRL_CONCEPT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,127}\Z")


class ResearchPlanError(Exception):
    pass


class ResearchPlanValidationError(ResearchPlanError, ValueError):
    pass


class ResearchPlanConflict(ResearchPlanError):
    pass


class ResearchPlanNotFound(ResearchPlanError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchPlanValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _hash_text(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in _CAMEL_CHARS for char in value):
        raise ResearchPlanValidationError(f"{name} must be lowercase SHA-256")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchPlanValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchPlanValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchPlanValidationError(f"{name} must be an object")
    return dict(value)


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ResearchPlanValidationError(f"{name} must be an array")
    refs = [_text(item, f"{name}[]") for item in value]
    if nonempty and not refs:
        raise ResearchPlanValidationError(f"{name} must not be empty")
    if len(set(refs)) != len(refs):
        raise ResearchPlanValidationError(f"{name} must contain unique values")
    return refs


def _validate_sec_request(value: Any) -> dict[str, str]:
    """Normalize and freeze the single SEC request of a plan version.

    The parameter set is closed: unknown keys, non-CIK issuers, forms outside
    the frozen allowlist, non-date window bounds, inverted windows or windows
    wider than the frozen span all fail closed.  The returned dict has a
    fixed key order so identity/comparisons are deterministic.
    """

    if not isinstance(value, Mapping):
        raise ResearchPlanValidationError("sec_request must be an object")
    extra = set(value) - {"issuer_cik", "form", "filing_date_from", "filing_date_to"}
    if extra:
        raise ResearchPlanValidationError(
            f"sec_request contains unsupported parameters: {sorted(extra)}"
        )
    issuer_cik = _text(value.get("issuer_cik"), "sec_request.issuer_cik")
    if _CIK_RE.fullmatch(issuer_cik) is None:
        raise ResearchPlanValidationError(
            "sec_request.issuer_cik must be a CIK with at most ten digits"
        )
    form = _text(value.get("form"), "sec_request.form")
    if form not in SEC_ALLOWED_FORMS:
        raise ResearchPlanValidationError(
            f"sec_request.form {form!r} is outside the frozen form allowlist"
        )
    filing_date_from = _text(
        value.get("filing_date_from"), "sec_request.filing_date_from"
    )
    filing_date_to = _text(value.get("filing_date_to"), "sec_request.filing_date_to")
    for label, raw in (("filing_date_from", filing_date_from), ("filing_date_to", filing_date_to)):
        if _DATE_RE.fullmatch(raw) is None:
            raise ResearchPlanValidationError(
                f"sec_request.{label} must be YYYY-MM-DD"
            )
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise ResearchPlanValidationError(
                f"sec_request.{label} is not a valid calendar date"
            ) from exc
        year = parsed.year
        if year < 1995 or year > 2100:
            raise ResearchPlanValidationError(
                f"sec_request.{label} is outside the SEC filing epoch"
            )
    if filing_date_from > filing_date_to:
        raise ResearchPlanValidationError("sec_request window is inverted")
    span = (date.fromisoformat(filing_date_to) - date.fromisoformat(filing_date_from)).days
    if span > SEC_WINDOW_MAX_DAYS:
        raise ResearchPlanValidationError(
            f"sec_request window exceeds the frozen {SEC_WINDOW_MAX_DAYS}-day bound"
        )
    return {
        # The public adapter and SEC submissions authority use the canonical
        # ten-digit CIK.  Equivalent caller spellings therefore cannot create
        # distinct plan identities or WorkOrders.
        "issuer_cik": issuer_cik.zfill(10),
        "form": form,
        "filing_date_from": filing_date_from,
        "filing_date_to": filing_date_to,
    }


def _validate_company_facts_request(value: Any) -> dict[str, str]:
    """Freeze one exact public SEC Company Concept quarterly comparison."""

    if not isinstance(value, Mapping):
        raise ResearchPlanValidationError("company_facts_request must be an object")
    fields = {"cik", "taxonomy", "concept", "unit", "form", "filed_to"}
    if set(value) != fields:
        raise ResearchPlanValidationError(
            "company_facts_request has an invalid closed shape"
        )
    cik = _text(value.get("cik"), "company_facts_request.cik")
    if _CIK_RE.fullmatch(cik) is None:
        raise ResearchPlanValidationError(
            "company_facts_request.cik must be a CIK with at most ten digits"
        )
    taxonomy = _text(value.get("taxonomy"), "company_facts_request.taxonomy")
    concept = _text(value.get("concept"), "company_facts_request.concept")
    unit = _text(value.get("unit"), "company_facts_request.unit")
    form = _text(value.get("form"), "company_facts_request.form")
    filed_to = _text(value.get("filed_to"), "company_facts_request.filed_to")
    if taxonomy != "us-gaap" or unit != "USD" or form != "10-Q":
        raise ResearchPlanValidationError(
            "company facts scope is closed to us-gaap/USD/10-Q"
        )
    if _XBRL_CONCEPT_RE.fullmatch(concept) is None:
        raise ResearchPlanValidationError(
            "company_facts_request.concept is not a safe XBRL concept"
        )
    if _DATE_RE.fullmatch(filed_to) is None:
        raise ResearchPlanValidationError(
            "company_facts_request.filed_to must be YYYY-MM-DD"
        )
    try:
        filed_date = date.fromisoformat(filed_to)
    except ValueError as exc:
        raise ResearchPlanValidationError(
            "company_facts_request.filed_to is not a calendar date"
        ) from exc
    if filed_date.year < 1995 or filed_date.year > 2100:
        raise ResearchPlanValidationError(
            "company_facts_request.filed_to is outside the SEC filing epoch"
        )
    return {
        "cik": cik.zfill(10),
        "taxonomy": taxonomy,
        "concept": concept,
        "unit": unit,
        "form": form,
        "filed_to": filed_to,
    }


def _validate_operation_request(operation: str, value: Any) -> dict[str, str]:
    if operation == SEC_OPERATION:
        return _validate_sec_request(value)
    if operation == SEC_COMPANY_FACTS_OPERATION:
        return _validate_company_facts_request(value)
    raise ResearchPlanValidationError("SEC research operation is not approved")


def _operation_permission_scope(operation: str) -> str:
    if operation == SEC_OPERATION:
        return PERMISSION_SCOPE
    if operation == SEC_COMPANY_FACTS_OPERATION:
        return COMPANY_FACTS_PERMISSION_SCOPE
    raise ResearchPlanValidationError("SEC research operation is not approved")


def _operation_policy_rule(operation: str) -> str:
    if operation == SEC_OPERATION:
        return PLAN_AUTO_START_RULE_REF
    if operation == SEC_COMPANY_FACTS_OPERATION:
        return PLAN_COMPANY_FACTS_AUTO_START_RULE_REF
    raise ResearchPlanValidationError("SEC research operation is not approved")


def _sec_template() -> dict[str, Any]:
    """The packaged SEC connector template; version 1 plans are closed to it."""

    inventory = load_packaged_connector_inventory()
    try:
        return inventory["templates"]["sec"]
    except KeyError as exc:
        raise ResearchPlanConflict(
            "packaged SEC connector template is unavailable"
        ) from exc


def _sec_operation(
    template: Mapping[str, Any], operation_name: str = SEC_OPERATION
) -> dict[str, Any]:
    for operation in template.get("operations", []):
        if (
            isinstance(operation, Mapping)
            and operation.get("operation") == operation_name
        ):
            return dict(operation)
    raise ResearchPlanConflict(
        f"packaged SEC template lacks the frozen {operation_name} operation"
    )


def _execution_budget(
    template: Mapping[str, Any], operation_name: str = SEC_OPERATION
) -> dict[str, int]:
    """Frozen budget/retry bounds for the single-step SEC plan.

    ``max_pages`` is taken from the packed template pagination so plan
    bounds stay authority-tied; the remaining bounds are the frozen v0.1
    plan policy constants.  The reader rebuilds the same dict and any drift
    fails closed.
    """

    pagination = _sec_operation(template, operation_name).get("pagination")
    max_pages = 20
    if isinstance(pagination, Mapping) and isinstance(pagination.get("max_pages"), int):
        max_pages = pagination["max_pages"]
    return {
        "max_attempts": SEC_MAX_ATTEMPTS,
        "max_pages": max_pages,
        "max_response_bytes": SEC_MAX_RESPONSE_BYTES,
        "max_seconds": SEC_MAX_SECONDS,
    }


_DOWNSTREAM_STEP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "stage": "authority_resolver",
        "operation": "resolve_connector_authority",
        "requested_capabilities": [RESOLVER_CAPABILITY],
        "runtime_profile_ref": RESOLVER_RUNTIME_PROFILE_REF,
        "declared_side_effects": [],
        "output_contract_ref": "schema:source-envelope:0.2",
    },
    {
        "stage": "verifier",
        "operation": "verify_source_and_numeric_material",
        "requested_capabilities": [VERIFIER_CAPABILITY],
        "runtime_profile_ref": VERIFIER_RUNTIME_PROFILE_REF,
        "declared_side_effects": [],
        "output_contract_ref": "schema:verification-bundle:0.1",
    },
    {
        "stage": "candidate_staging",
        "operation": "stage_verified_candidate",
        "requested_capabilities": [STAGING_CAPABILITY],
        "runtime_profile_ref": STAGING_RUNTIME_PROFILE_REF,
        "declared_side_effects": [],
        "output_contract_ref": "schema:candidate-claim:0.1",
    },
)


def _step_specs(operation: str) -> tuple[dict[str, Any], ...]:
    operation_wire = _sec_operation(_sec_template(), operation)
    return (
        {
            "stage": "connector",
            "operation": operation,
            "requested_capabilities": [SEC_CAPABILITY],
            "runtime_profile_ref": SEC_RUNTIME_PROFILE_REF,
            "declared_side_effects": ["read:public-http"],
            "output_contract_ref": operation_wire["output_schema_ref"],
        },
        *_DOWNSTREAM_STEP_SPECS,
    )


def build_research_plan_step(
    *,
    plan_version_ref: str,
    sec_request: Mapping[str, str],
    operation: str = SEC_OPERATION,
    ordinal: int = 1,
    prior_step_ref: str | None = None,
    max_attempts: int,
) -> dict[str, Any]:
    """Build one deterministic node of the closed SEC research tree."""

    specs = _step_specs(operation)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= len(specs):
        raise ResearchPlanValidationError("ordinal is outside the frozen plan tree")
    spec = specs[ordinal - 1]
    if ordinal == 1 and prior_step_ref is not None:
        raise ResearchPlanValidationError("the root plan step cannot depend on another step")
    if ordinal > 1:
        prior_step_ref = _text(prior_step_ref, "prior_step_ref")
    parameters = (
        _validate_operation_request(operation, sec_request)
        if ordinal == 1
        else {"upstream_step_ref": prior_step_ref}
    )

    step_id = f"research-plan-step:{spec['stage']}:" + content_hash({
        "plan_version_ref": plan_version_ref,
        "ordinal": ordinal,
        "stage": spec["stage"],
        "operation": spec["operation"],
        "parameters": parameters,
    })[:32]
    step = {
        "schema_version": SCHEMA_VERSION,
        "id": step_id,
        "ordinal": ordinal,
        "stage": spec["stage"],
        "operation": spec["operation"],
        "parameters": parameters,
        "depends_on": [] if prior_step_ref is None else [prior_step_ref],
        "requested_capabilities": list(spec["requested_capabilities"]),
        "runtime_profile_ref": spec["runtime_profile_ref"],
        "declared_side_effects": list(spec["declared_side_effects"]),
        "output_contract_ref": spec["output_contract_ref"],
        "max_attempts": max_attempts,
        "work_order_ref": "work:plan-" + content_hash({
            "plan_version_ref": plan_version_ref,
            "step_ref": step_id,
        })[:32],
    }
    step["content_hash"] = content_hash(step)
    return step


def build_research_plan_steps(
    *, plan_version_ref: str, sec_request: Mapping[str, str], max_attempts: int,
    operation: str = SEC_OPERATION,
) -> list[dict[str, Any]]:
    """Build the complete closed connector-to-staging task tree."""

    steps: list[dict[str, Any]] = []
    prior: str | None = None
    for ordinal in range(1, len(_step_specs(operation)) + 1):
        step = build_research_plan_step(
            plan_version_ref=plan_version_ref,
            sec_request=sec_request,
            operation=operation,
            ordinal=ordinal,
            prior_step_ref=prior,
            max_attempts=max_attempts,
        )
        steps.append(step)
        prior = step["id"]
    return steps


def _revalidate_plan_step(
    step: Any,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-derive one frozen tree node and compare canonical bytes."""

    if not isinstance(step, Mapping) or set(step) != _STEP_FIELDS:
        raise ResearchPlanConflict("plan execution step has an invalid closed shape")
    body = dict(step)
    asserted = body.pop("content_hash", None)
    if not isinstance(asserted, str) or asserted != content_hash(body):
        raise ResearchPlanConflict("plan step content_hash mismatch")
    if canonical_json(step) != canonical_json(expected):
        raise ResearchPlanConflict("plan step drifted from the frozen SEC definition")
    return dict(step)


def plan_identity(
    *,
    question_ref: str,
    question_version_ref: str,
    decision_ref: str,
    sec_request: Mapping[str, str],
    operation: str = SEC_OPERATION,
) -> dict[str, Any]:
    """Canonical identity binding for one research plan version.

    The binding is the only input to the plan ref: an identical question
    version selected by the same AgendaDecision with an identical frozen SEC
    request always resolves to the same plan identity.  Identity is never a
    caller-supplied id.
    """

    base = {
        "identity_schema": _IDENTITY_SCHEMA,
        "planner_ref": PLANNER_REF,
        "question_ref": _text(question_ref, "question_ref"),
        "question_version_ref": _text(question_version_ref, "question_version_ref"),
        "decision_ref": _text(decision_ref, "decision_ref"),
    }
    if operation == SEC_OPERATION:
        # Preserve the deployed v0.1 identity bytes for existing plans.
        base["sec_request"] = _validate_sec_request(sec_request)
    elif operation == SEC_COMPANY_FACTS_OPERATION:
        base["operation"] = operation
        base["company_facts_request"] = _validate_company_facts_request(sec_request)
    else:
        raise ResearchPlanValidationError("SEC research operation is not approved")
    return base


def plan_version_ref_for(
    *,
    question_ref: str,
    question_version_ref: str,
    decision_ref: str,
    sec_request: Mapping[str, str],
    operation: str = SEC_OPERATION,
) -> str:
    """Deterministic plan version ref derived from the identity binding."""

    return "research-plan:" + content_hash(plan_identity(
        question_ref=question_ref,
        question_version_ref=question_version_ref,
        decision_ref=decision_ref,
        sec_request=sec_request,
        operation=operation,
    ))[:32]


def plan_start_ref_for(plan_version_ref: str) -> str:
    """Deterministic plan-start binding ref for one exact plan version."""

    plan_version_ref = _text(plan_version_ref, "plan_version_ref")
    return "research-plan-start:" + content_hash({"plan_version_ref": plan_version_ref})[:32]


def _recompute_context_binding(cursor: Any, cycle: Mapping[str, Any]) -> dict[str, Any]:
    """Re-derive the exact AgendaContextBinding the plan freezes.

    Mirrors the agenda context authority: the binding is a deterministic
    function of the exact cycle, its frozen mandate/policy versions and its
    perception snapshot, with the cycle timestamp as the binding timestamp.
    Any drift in those authorities fails closed here.
    """

    policy = read_exact_agenda_policy_version(cursor, cycle["policy_version_ref"])
    mandate = read_exact_mandate_version(cursor, cycle["mandate_version_ref"])
    snapshot = read_exact_perception_snapshot(
        cursor, cycle["perception_snapshot_ref"]
    )
    if snapshot["content_hash"] != cycle["perception_snapshot_hash"]:
        raise ResearchPlanConflict(
            "cycle perception hash no longer matches Core authority"
        )
    if mandate["content_hash"] != cycle["mandate_version_hash"]:
        raise ResearchPlanConflict(
            "cycle mandate hash no longer matches Core authority"
        )
    if policy["content_hash"] != cycle["policy_version_hash"]:
        raise ResearchPlanConflict(
            "cycle policy hash no longer matches Core authority"
        )
    return build_agenda_context_binding(
        cycle_ref=cycle["cycle_id"],
        cycle_hash=cycle["content_hash"],
        company_ref=cycle["company_ref"],
        policy_version_ref=policy["id"],
        policy_version_hash=cycle["policy_version_hash"],
        mandate_version_ref=mandate["id"],
        mandate_version_hash=cycle["mandate_version_hash"],
        perception_snapshot_ref=snapshot["snapshot_id"],
        perception_snapshot_hash=snapshot["content_hash"],
        created_at=cycle["created_at"],
    )


def _revalidate_agenda_binding(
    cursor: Any,
    *,
    question_ref: str,
    question: Mapping[str, Any],
    question_version: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    """Re-derive the exact AgendaDecision/cycle/candidate binding.

    The decision is re-read from its canonical row, the cycle from its frozen
    start hash, the mandate scope cross-checked, and the selected candidate
    must still canonically match the question content.  The question must
    also be bound to this exact decision by the backlog selection link, so a
    forged plan cannot point at a decision that never selected the question.
    """

    if not isinstance(binding, Mapping) or set(binding) != _AGENDA_BINDING_FIELDS:
        raise ResearchPlanConflict(
            "plan agenda binding has an invalid closed shape"
        )
    decision = _read_exact_agenda_decision(cursor, binding["decision_ref"])
    cycle = read_exact_agenda_cycle(cursor, binding["cycle_ref"])
    mandate = read_exact_mandate_version(cursor, binding["mandate_version_ref"])
    policy = read_exact_agenda_policy_version(cursor, binding["policy_version_ref"])
    if decision["content_hash"] != binding["decision_hash"]:
        raise ResearchPlanConflict("plan agenda decision hash drifted")
    if decision["cycle_ref"] != binding["cycle_ref"]:
        raise ResearchPlanConflict("plan agenda decision cycle drifted")
    if cycle["content_hash"] != binding["cycle_hash"]:
        raise ResearchPlanConflict("plan agenda cycle hash drifted")
    if cycle["policy_version_ref"] != binding["policy_version_ref"]:
        raise ResearchPlanConflict(
            "plan agenda policy version drifted from the frozen cycle"
        )
    if policy["content_hash"] != binding["policy_version_hash"]:
        raise ResearchPlanConflict("plan agenda policy hash drifted")
    if mandate["mandate_ref"] != question["identity"]["mandate_ref"]:
        raise ResearchPlanConflict(
            "plan agenda decision belongs to a different mandate"
        )
    if cycle["company_ref"] != question["identity"]["company_ref"]:
        raise ResearchPlanConflict(
            "plan agenda decision covers a different company"
        )
    if cycle["mandate_version_ref"] != binding["mandate_version_ref"]:
        raise ResearchPlanConflict(
            "plan agenda mandate version drifted from the frozen cycle"
        )
    matched_refs = []
    for candidate_ref in decision["selected_candidate_refs"]:
        candidate = _read_exact_agenda_candidate(cursor, candidate_ref)
        if (
            candidate["cycle_ref"] == cycle["cycle_id"]
            and candidate["valid"]
            and candidate["proposed_question"] == question_version["question"]
            and candidate["answer_criteria"] == question_version["answer_criteria"]
            and candidate["source_refs"] == question_version["source_refs"]
        ):
            matched_refs.append(candidate_ref)
    if len(matched_refs) != 1 or matched_refs[0] != binding["candidate_ref"]:
        raise ResearchPlanConflict(
            "plan agenda decision does not select this exact question content"
        )
    link_id = "research-question-selection:" + content_hash({
        "question_ref": question_ref,
        "decision_ref": decision["id"],
        "cycle_ref": cycle["cycle_id"],
    })[:32]
    link = cursor.execute(
        "SELECT link_id FROM backlog_selection_links WHERE link_id=?",
        (link_id,),
    ).fetchone()
    if link is None:
        raise ResearchPlanConflict(
            "question is not bound to this exact AgendaDecision by a selection link"
        )


def _selected_candidate_ref_for_question(
    cursor: Any,
    *,
    decision: Mapping[str, Any],
    cycle: Mapping[str, Any],
    question_version: Mapping[str, Any],
) -> str:
    """Resolve the unique selected candidate that canonically became a question."""

    matches: list[str] = []
    for candidate_ref in decision["selected_candidate_refs"]:
        candidate = _read_exact_agenda_candidate(cursor, candidate_ref)
        if (
            candidate["cycle_ref"] == cycle["cycle_id"]
            and candidate["valid"]
            and candidate["proposed_question"] == question_version["question"]
            and candidate["answer_criteria"] == question_version["answer_criteria"]
            and candidate["source_refs"] == question_version["source_refs"]
        ):
            matches.append(candidate_ref)
    if len(matches) != 1:
        raise ResearchPlanConflict(
            "AgendaDecision must select exactly one candidate for this question"
        )
    return matches[0]


def _revalidate_execution_scope(
    *,
    plan_version_ref: str,
    scope: Any,
) -> None:
    """The first version is closed to one SEC public read-only step.

    Every scope field is rebuilt from the packaged SEC template or the
    frozen constants; a caller cannot inject connectors, credentials, writes,
    broadened permissions, different operations, extra steps or a mutable
    budget.
    """

    if not isinstance(scope, Mapping) or set(scope) != _EXECUTION_SCOPE_FIELDS:
        raise ResearchPlanConflict(
            "plan execution scope has an invalid closed shape"
        )
    template = _sec_template()
    operation_name = scope.get("operation")
    if operation_name not in {SEC_OPERATION, SEC_COMPANY_FACTS_OPERATION}:
        raise ResearchPlanConflict("plan operation is outside the approved SEC reads")
    operation = _sec_operation(template, operation_name)
    if scope["source_ref"] != template["source_identity"]["source_ref"]:
        raise ResearchPlanConflict("plan source drifted from the SEC template")
    if scope["connector_profile_ref"] != template["id"]:
        raise ResearchPlanConflict("plan connector profile drifted")
    if scope["connector_profile_hash"] != template["content_hash"]:
        raise ResearchPlanConflict("plan connector profile hash drifted")
    if scope["permission_scope"] != _operation_permission_scope(operation_name):
        raise ResearchPlanConflict("plan permission scope drifted from the frozen read scope")
    if scope["auth_mode"] != "none":
        raise ResearchPlanConflict("plan requests credentials (auth_mode must be none)")
    if scope["side_effect_class"] != SIDE_EFFECT_CLASS:
        raise ResearchPlanConflict("plan side-effect class drifted from read-only")
    if scope["declared_side_effects"] != ["read:public-http"]:
        raise ResearchPlanConflict("plan declared side effects drifted from public read-only")
    if scope["verifier_ref"] != SEC_VERIFIER_REF:
        raise ResearchPlanConflict("plan verifier drifted from the frozen source verifier")
    if scope["output_contract_ref"] != operation["output_schema_ref"]:
        raise ResearchPlanConflict("plan output contract drifted")
    if scope["output_contract_hash"] != operation["output_schema_hash"]:
        raise ResearchPlanConflict("plan output contract hash drifted")
    parameters = _validate_operation_request(operation_name, scope["parameters"])
    budget = scope["budget"]
    if not isinstance(budget, Mapping) or set(budget) != _BUDGET_FIELDS:
        raise ResearchPlanConflict("plan budget has an invalid closed shape")
    expected_budget = _execution_budget(template, operation_name)
    if canonical_json(budget) != canonical_json(expected_budget):
        raise ResearchPlanConflict("plan budget drifted from the frozen bounds")
    steps = scope["steps"]
    expected_steps = build_research_plan_steps(
        plan_version_ref=plan_version_ref,
        sec_request=parameters,
        max_attempts=SEC_MAX_ATTEMPTS,
        operation=operation_name,
    )
    if not isinstance(steps, list) or len(steps) != len(expected_steps):
        raise ResearchPlanConflict("plan must define the complete frozen four-step tree")
    for step, expected in zip(steps, expected_steps, strict=True):
        _revalidate_plan_step(step, expected=expected)


def _authority_row(cursor: Any, sql: str, parameters: tuple[Any, ...], name: str) -> Any:
    try:
        return cursor.execute(sql, parameters).fetchone()
    except sqlite3.OperationalError as exc:
        raise ResearchPlanNotFound(f"{name} authority table is unavailable") from exc


def _reverify_record(
    record_json: Any, columns: Mapping[str, Any], *, name: str
) -> dict[str, Any]:
    """Re-derive one append-only record from its own canonical bytes."""

    if type(record_json) is not str:
        raise ResearchPlanConflict(f"{name} record_json is missing")
    try:
        wire = json.loads(record_json)
    except (TypeError, ValueError) as exc:
        raise ResearchPlanConflict(f"{name} record_json is not valid JSON") from exc
    if type(wire) is not dict:
        raise ResearchPlanConflict(f"{name} record_json must be an object")
    if canonical_json(wire) != record_json:
        raise ResearchPlanConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if not isinstance(asserted, str) or asserted != content_hash(body):
        raise ResearchPlanConflict(f"{name} content_hash mismatch")
    for field, value in columns.items():
        if wire.get(field) != value:
            raise ResearchPlanConflict(f"{name} SQL column for {field} drifted")
    return wire


def read_exact_research_plan_version(cursor: Any, version_ref: str) -> dict[str, Any]:
    """Read one plan version and re-derive every ref/hash from Core authority.

    The closed plan body is re-computed from the exact question version, the
    exact AgendaDecision/cycle/candidate/mandate/policy and the packaged SEC
    template; the context binding is rebuilt from the exact cycle.  Tampered
    rows, forged bindings, drifted agenda authority and drifted SEC scope all
    fail closed.
    """

    version_ref = _text(version_ref, "plan_version_ref")
    row = _authority_row(
        cursor,
        "SELECT * FROM research_plan_versions WHERE version_id=?",
        (version_ref,),
        "ResearchPlanVersion",
    )
    if row is None:
        raise ResearchPlanNotFound(f"research plan version {version_ref}")
    wire = _reverify_record(
        row["record_json"],
        {
            "question_ref": row["question_ref"],
            "question_version_ref": row["question_version_ref"],
            "version": row["version_number"],
            "prior_version_ref": row["prior_version_id"],
            "planner_ref": row["planner_ref"],
            "created_at": row["created_at"],
        },
        name="ResearchPlanVersion",
    )
    if wire["content_hash"] != row["content_hash"]:
        raise ResearchPlanConflict("ResearchPlanVersion content_hash column drifted")
    if set(wire) != _PLAN_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchPlanConflict("ResearchPlanVersion has an invalid closed shape")
    if (
        wire["agenda_binding"].get("decision_ref") != row["decision_ref"]
        or wire["agenda_binding"].get("cycle_ref") != row["cycle_ref"]
    ):
        raise ResearchPlanConflict(
            "ResearchPlanVersion agenda SQL columns drifted"
        )
    try:
        _text(wire["id"], "ResearchPlanVersion.id")
        _timestamp(wire["created_at"], "ResearchPlanVersion.created_at")
        _text(wire["planner_ref"], "ResearchPlanVersion.planner_ref")
        _hash_text(wire["planner_hash"], "ResearchPlanVersion.planner_hash")
        _text(wire["question_ref"], "ResearchPlanVersion.question_ref")
        _text(wire["question_version_ref"], "ResearchPlanVersion.question_version_ref")
        _hash_text(
            wire["question_version_hash"], "ResearchPlanVersion.question_version_hash"
        )
        _object(wire["agenda_binding"], "ResearchPlanVersion.agenda_binding")
        _text(wire["context_binding_ref"], "ResearchPlanVersion.context_binding_ref")
        _hash_text(wire["context_binding_hash"], "ResearchPlanVersion.context_binding_hash")
        _text(wire["actor_ref"], "ResearchPlanVersion.actor_ref")
        if wire["prior_version_ref"] is not None:
            _text(wire["prior_version_ref"], "ResearchPlanVersion.prior_version_ref")
    except ResearchPlanValidationError as exc:
        raise ResearchPlanConflict("ResearchPlanVersion canonical record is invalid") from exc
    if (
        isinstance(wire["version"], bool)
        or not isinstance(wire["version"], int)
        or wire["version"] < 1
    ):
        raise ResearchPlanConflict("ResearchPlanVersion version is invalid")
    if wire["planner_ref"] != PLANNER_REF or wire["planner_hash"] != PLANNER_HASH:
        raise ResearchPlanConflict("ResearchPlanVersion planner binding is invalid")
    if wire["version"] == 1:
        if wire["prior_version_ref"] is not None:
            raise ResearchPlanConflict("ResearchPlanVersion v1 cannot have a prior version")
    else:
        if wire["prior_version_ref"] is None:
            raise ResearchPlanConflict(
                "ResearchPlanVersion revision is missing its prior version"
            )
        prior = read_exact_research_plan_version(cursor, wire["prior_version_ref"])
        if (
            prior["question_ref"] != wire["question_ref"]
            or prior["version"] != wire["version"] - 1
        ):
            raise ResearchPlanConflict(
                "ResearchPlanVersion prior-version chain is invalid"
            )
    question = read_exact_backlog_question(cursor, wire["question_ref"])
    question_version = read_exact_backlog_question_version(
        cursor, wire["question_version_ref"]
    )
    if (
        question_version["question_ref"] != wire["question_ref"]
        or question_version["content_hash"] != wire["question_version_hash"]
    ):
        raise ResearchPlanConflict(
            "ResearchPlanVersion drifted from its exact question version"
        )
    scope = wire["execution_scope"]
    operation_name = scope.get("operation")
    parameters = _validate_operation_request(operation_name, scope["parameters"])
    identity = plan_identity(
        question_ref=wire["question_ref"],
        question_version_ref=wire["question_version_ref"],
        decision_ref=wire["agenda_binding"]["decision_ref"],
        sec_request=parameters,
        operation=operation_name,
    )
    expected_ref = "research-plan:" + content_hash(identity)[:32]
    if wire["id"] != expected_ref or expected_ref != row["version_id"]:
        raise ResearchPlanConflict(
            "ResearchPlanVersion ref drifted from its identity binding"
        )
    _revalidate_agenda_binding(
        cursor,
        question_ref=wire["question_ref"],
        question=question,
        question_version=question_version,
        binding=wire["agenda_binding"],
    )
    from .research_question_backlog import _read_exact_event_history

    question_events = _read_exact_event_history(
        cursor, wire["question_ref"], revalidate_plan=False
    )
    planned_binding = next(
        (
            event for event in question_events
            if event["state"] == "planned"
            and event["metadata"] == {
                "plan_version_ref": wire["id"],
                "plan_version_hash": wire["content_hash"],
            }
        ),
        None,
    )
    if planned_binding is None:
        raise ResearchPlanConflict(
            "ResearchPlanVersion is not bound by an exact planned question event"
        )
    cycle = read_exact_agenda_cycle(cursor, wire["agenda_binding"]["cycle_ref"])
    context = _recompute_context_binding(cursor, cycle)
    if context["id"] != wire["context_binding_ref"] or context["content_hash"] != wire["context_binding_hash"]:
        raise ResearchPlanConflict(
            "ResearchPlanVersion context binding drifted from the exact cycle"
        )
    _revalidate_execution_scope(
        plan_version_ref=wire["id"], scope=scope,
    )
    if row["record_json"] != canonical_json(wire):
        raise ResearchPlanConflict("ResearchPlanVersion record_json column drifted")
    return wire


def read_exact_research_plan_event(cursor: Any, event_id: str) -> dict[str, Any]:
    """Read one immutable plan event from its canonical row."""

    event_id = _text(event_id, "event_id")
    row = _authority_row(
        cursor,
        "SELECT * FROM research_plan_events WHERE event_id=?",
        (event_id,),
        "ResearchPlanEvent",
    )
    if row is None:
        raise ResearchPlanNotFound(f"research plan event {event_id}")
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchPlanConflict(
            "ResearchPlanEvent metadata_json is not valid JSON"
        ) from exc
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": row["event_id"],
        "plan_version_ref": row["plan_version_ref"],
        "state": row["state"],
        "reason": row["reason"],
        "metadata": metadata,
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchPlanConflict("ResearchPlanEvent columns drifted from their content hash")
    wire["content_hash"] = row["content_hash"]
    if row["metadata_json"] != canonical_json(metadata):
        raise ResearchPlanConflict("ResearchPlanEvent metadata_json is not canonical")
    if set(wire) != _EVENT_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchPlanConflict("ResearchPlanEvent has an invalid closed shape")
    try:
        _text(wire["id"], "ResearchPlanEvent.id")
        _text(wire["plan_version_ref"], "ResearchPlanEvent.plan_version_ref")
        _text(wire["reason"], "ResearchPlanEvent.reason")
        _object(wire["metadata"], "ResearchPlanEvent.metadata")
        _text(wire["actor_ref"], "ResearchPlanEvent.actor_ref")
        _timestamp(wire["created_at"], "ResearchPlanEvent.created_at")
    except ResearchPlanValidationError as exc:
        raise ResearchPlanConflict("ResearchPlanEvent canonical record is invalid") from exc
    if wire["state"] not in {"created", "started"}:
        raise ResearchPlanConflict("ResearchPlanEvent state is invalid")
    return wire


def read_exact_research_plan_approval(cursor: Any, approval_id: str) -> dict[str, Any]:
    """Read one exact human approval decision from its canonical row."""

    approval_id = _text(approval_id, "approval_id")
    row = _authority_row(
        cursor,
        "SELECT * FROM research_plan_approvals WHERE approval_id=?",
        (approval_id,),
        "ResearchPlanApproval",
    )
    if row is None:
        raise ResearchPlanNotFound(f"research plan approval {approval_id}")
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": row["approval_id"],
        "plan_version_ref": row["plan_version_ref"],
        "plan_version_hash": row["plan_version_hash"],
        "decision": row["decision"],
        "reason": row["reason"],
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchPlanConflict("ResearchPlanApproval columns drifted from their content hash")
    wire["content_hash"] = row["content_hash"]
    if set(wire) != _APPROVAL_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchPlanConflict("ResearchPlanApproval has an invalid closed shape")
    try:
        _text(wire["id"], "ResearchPlanApproval.id")
        _text(wire["plan_version_ref"], "ResearchPlanApproval.plan_version_ref")
        _hash_text(wire["plan_version_hash"], "ResearchPlanApproval.plan_version_hash")
        _text(wire["reason"], "ResearchPlanApproval.reason")
        _text(wire["actor_ref"], "ResearchPlanApproval.actor_ref")
        _timestamp(wire["created_at"], "ResearchPlanApproval.created_at")
        if not _HUMAN_ACTOR_RE.fullmatch(wire["actor_ref"]):
            raise ResearchPlanValidationError(
                "ResearchPlanApproval actor must be an exact human principal"
            )
    except ResearchPlanValidationError as exc:
        raise ResearchPlanConflict("ResearchPlanApproval canonical record is invalid") from exc
    if wire["decision"] not in {"accepted", "rejected"}:
        raise ResearchPlanConflict("ResearchPlanApproval decision is invalid")
    plan = read_exact_research_plan_version(cursor, wire["plan_version_ref"])
    if plan["content_hash"] != wire["plan_version_hash"]:
        raise ResearchPlanConflict(
            "ResearchPlanApproval drifted from its exact plan version"
        )
    return wire


def read_exact_research_plan_policy_authorization(
    cursor: Any, authorization_id: str
) -> dict[str, Any]:
    """Read one exact low-risk plan authorization from versioned policy."""

    authorization_id = _text(authorization_id, "authorization_id")
    row = _authority_row(
        cursor,
        "SELECT * FROM research_plan_policy_authorizations WHERE authorization_id=?",
        (authorization_id,),
        "ResearchPlanPolicyAuthorization",
    )
    if row is None:
        raise ResearchPlanNotFound(
            f"research plan policy authorization {authorization_id}"
        )
    wire = _reverify_record(
        row["record_json"],
        {
            "id": row["authorization_id"],
            "plan_version_ref": row["plan_version_ref"],
            "plan_version_hash": row["plan_version_hash"],
            "policy_version_ref": row["policy_version_ref"],
            "policy_version_hash": row["policy_version_hash"],
            "rule_ref": row["rule_ref"],
            "created_at": row["created_at"],
        },
        name="ResearchPlanPolicyAuthorization",
    )
    if set(wire) != _POLICY_AUTHORIZATION_FIELDS:
        raise ResearchPlanConflict(
            "ResearchPlanPolicyAuthorization has an invalid closed shape"
        )
    plan = read_exact_research_plan_version(cursor, wire["plan_version_ref"])
    expected_rule = _operation_policy_rule(plan["execution_scope"]["operation"])
    if (
        wire["schema_version"] != SCHEMA_VERSION
        or wire["decision"] != "accepted"
        or wire["actor_ref"] != PLAN_AUTO_START_ACTOR_REF
        or wire["authorization"] != "versioned_governance_policy"
        or wire["rule_ref"] != expected_rule
    ):
        raise ResearchPlanConflict(
            "ResearchPlanPolicyAuthorization authority is invalid"
        )
    if plan["content_hash"] != wire["plan_version_hash"]:
        raise ResearchPlanConflict(
            "ResearchPlanPolicyAuthorization drifted from its exact plan"
        )
    policy = cursor.execute(
        "SELECT content_hash FROM governance_policy_versions WHERE policy_version_id=?",
        (wire["policy_version_ref"],),
    ).fetchone()
    if policy is None or policy["content_hash"] != wire["policy_version_hash"]:
        raise ResearchPlanConflict(
            "ResearchPlanPolicyAuthorization policy binding drifted"
        )
    return wire


def read_exact_research_plan_start_authorization(
    cursor: Any, authorization_id: str
) -> dict[str, Any]:
    """Resolve the immutable human or policy authority named by a start."""

    human = cursor.execute(
        "SELECT 1 FROM research_plan_approvals WHERE approval_id=?",
        (authorization_id,),
    ).fetchone()
    if human is not None:
        return read_exact_research_plan_approval(cursor, authorization_id)
    return read_exact_research_plan_policy_authorization(cursor, authorization_id)


def read_exact_research_plan_start(
    cursor: Any,
    start_id: str,
    *,
    require_question_binding: bool = True,
) -> dict[str, Any]:
    """Read one authorized plan start binding from its canonical row.

    The start row freezes the exact WorkflowRunVersion row (ref/hash) and the
    exact root WorkOrder (ref/hash) plus the exact accepted human-or-policy
    authorization.  The
    observability workflow row lives in the same Core DB and is re-verified
    here from its canonical record; the root WorkOrder row lives in the
    Scheduler authority and is re-verified by the plan control plane.
    """

    start_id = _text(start_id, "start_id")
    row = _authority_row(
        cursor,
        "SELECT * FROM research_plan_starts WHERE start_id=?",
        (start_id,),
        "ResearchPlanStart",
    )
    if row is None:
        raise ResearchPlanNotFound(f"research plan start {start_id}")
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": row["start_id"],
        "plan_version_ref": row["plan_version_ref"],
        "plan_version_hash": row["plan_version_hash"],
        "approval_ref": row["approval_ref"],
        "approval_hash": row["approval_hash"],
        "workflow_ref": row["workflow_ref"],
        "workflow_version_ref": row["workflow_version_ref"],
        "workflow_version_hash": row["workflow_version_hash"],
        "root_work_order_ref": row["root_work_order_ref"],
        "root_work_order_hash": row["root_work_order_hash"],
        "event_ref": row["event_ref"],
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchPlanConflict("ResearchPlanStart columns drifted from their content hash")
    wire["content_hash"] = row["content_hash"]
    if set(wire) != _START_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchPlanConflict("ResearchPlanStart has an invalid closed shape")
    try:
        _text(wire["id"], "ResearchPlanStart.id")
        _text(wire["plan_version_ref"], "ResearchPlanStart.plan_version_ref")
        _hash_text(wire["plan_version_hash"], "ResearchPlanStart.plan_version_hash")
        _text(wire["approval_ref"], "ResearchPlanStart.approval_ref")
        _hash_text(wire["approval_hash"], "ResearchPlanStart.approval_hash")
        _text(wire["workflow_ref"], "ResearchPlanStart.workflow_ref")
        _text(wire["workflow_version_ref"], "ResearchPlanStart.workflow_version_ref")
        _hash_text(wire["workflow_version_hash"], "ResearchPlanStart.workflow_version_hash")
        _text(wire["root_work_order_ref"], "ResearchPlanStart.root_work_order_ref")
        _hash_text(wire["root_work_order_hash"], "ResearchPlanStart.root_work_order_hash")
        _text(wire["event_ref"], "ResearchPlanStart.event_ref")
        _text(wire["actor_ref"], "ResearchPlanStart.actor_ref")
        _timestamp(wire["created_at"], "ResearchPlanStart.created_at")
    except ResearchPlanValidationError as exc:
        raise ResearchPlanConflict("ResearchPlanStart canonical record is invalid") from exc
    plan = read_exact_research_plan_version(cursor, wire["plan_version_ref"])
    if plan["content_hash"] != wire["plan_version_hash"]:
        raise ResearchPlanConflict(
            "ResearchPlanStart drifted from its exact plan version"
        )
    approval = read_exact_research_plan_start_authorization(
        cursor, wire["approval_ref"]
    )
    if approval["content_hash"] != wire["approval_hash"]:
        raise ResearchPlanConflict(
            "ResearchPlanStart drifted from its exact approval decision"
        )
    if approval["decision"] != "accepted":
        raise ResearchPlanConflict(
            "ResearchPlanStart requires an accepted approval decision"
        )
    _read_exact_workflow_tree(
        cursor, plan_wire=plan, start_wire=wire,
    )
    expected_ref = plan_start_ref_for(wire["plan_version_ref"])
    if wire["id"] != expected_ref or expected_ref != row["start_id"]:
        raise ResearchPlanConflict(
            "ResearchPlanStart ref drifted from its plan binding"
        )
    event = read_exact_research_plan_event(cursor, wire["event_ref"])
    if (
        event["plan_version_ref"] != wire["plan_version_ref"]
        or event["state"] != "started"
        or event["metadata"] != {
            "workflow_version_ref": wire["workflow_version_ref"],
            "workflow_version_hash": wire["workflow_version_hash"],
            "root_work_order_ref": wire["root_work_order_ref"],
            "root_work_order_hash": wire["root_work_order_hash"],
        }
    ):
        raise ResearchPlanConflict(
            "ResearchPlanStart drifted from its started plan event"
        )
    from .research_question_backlog import _read_exact_event_history

    question_events = _read_exact_event_history(
        cursor, plan["question_ref"], revalidate_plan=False
    )
    expected_question_metadata = {
        "plan_version_ref": wire["plan_version_ref"],
        "plan_version_hash": wire["plan_version_hash"],
        "workflow_version_ref": wire["workflow_version_ref"],
        "workflow_version_hash": wire["workflow_version_hash"],
        "root_work_order_ref": wire["root_work_order_ref"],
        "root_work_order_hash": wire["root_work_order_hash"],
    }
    if require_question_binding and not any(
        item["state"] == "in_progress"
        and item["metadata"] == expected_question_metadata
        for item in question_events
    ):
        raise ResearchPlanConflict(
            "ResearchPlanStart lacks its exact in_progress question binding"
        )
    return wire


def revalidate_plan_binds_question(
    cursor: Any,
    plan_version_ref: str,
    question_ref: str,
    *,
    head_version_id: str | None = None,
) -> dict[str, Any]:
    """Re-derive one exact plan and require it to bind the exact question.

    Used by the backlog's plan-gated transitions: the plan must exist, must
    bind this question and (when a head version is known) the exact question
    version that will be carried by the transition event.
    """

    plan = read_exact_research_plan_version(cursor, plan_version_ref)
    if plan["question_ref"] != question_ref:
        raise ResearchPlanConflict(
            "plan is bound to a different question than the transition target"
        )
    if head_version_id is not None and plan["question_version_ref"] != head_version_id:
        raise ResearchPlanConflict(
            "plan is bound to a different question version than the transition target"
        )
    return plan


def _revalidate_plan_start_binding(
    cursor: Any,
    *,
    plan_version_ref: str,
    plan_version_hash: str,
    workflow_version_ref: str,
    workflow_version_hash: str,
    root_work_order_ref: str,
    root_work_order_hash: str,
) -> dict[str, Any]:
    """Re-derive the exact approved start binding for a backlog transition."""

    plan = read_exact_research_plan_version(cursor, plan_version_ref)
    if plan["content_hash"] != plan_version_hash:
        raise ResearchPlanConflict(
            "start binding plan hash drifted from the exact plan"
        )
    start = read_exact_research_plan_start(
        cursor, plan_start_ref_for(plan_version_ref),
        require_question_binding=False,
    )
    if (
        start["plan_version_hash"] != plan_version_hash
        or start["workflow_version_ref"] != workflow_version_ref
        or start["workflow_version_hash"] != workflow_version_hash
        or start["root_work_order_ref"] != root_work_order_ref
        or start["root_work_order_hash"] != root_work_order_hash
    ):
        raise ResearchPlanConflict(
            "start binding drifted from the exact approved plan start"
        )
    return start


def _read_plan_event_history(
    cursor: Any, plan_version_ref: str
) -> list[dict[str, Any]]:
    """Replay and validate the plan event chain."""

    rows = cursor.execute(
        "SELECT event_id FROM research_plan_events WHERE plan_version_ref=? "
        "ORDER BY event_seq",
        (plan_version_ref,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    prior: str | None = None
    for row in rows:
        event = read_exact_research_plan_event(cursor, row["event_id"])
        if event["plan_version_ref"] != plan_version_ref:
            raise ResearchPlanConflict(
                "ResearchPlanEvent drifted from its plan history"
            )
        if event["state"] not in _PLAN_TRANSITIONS.get(prior, set()):
            raise ResearchPlanConflict(
                f"research plan transition {prior!r} -> {event['state']!r} is invalid"
            )
        events.append(event)
        prior = event["state"]
    return events


class ResearchPlanAuthority:
    """ResearchPlanVersion authority layered on a ``DaltonStore``."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("store must be a DaltonStore-like authority")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _record(base: Mapping[str, Any]) -> dict[str, Any]:
        wire = dict(base)
        wire["content_hash"] = content_hash(wire)
        return wire

    def _idem(
        self, cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        key = _text(key, "idempotency_key")
        row = cur.execute(
            "SELECT operation,request_hash,result_json FROM research_plan_idempotency "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] == operation and row["request_hash"] == request_hash:
            result = json.loads(row["result_json"])
            result["status"] = "duplicate"
            return result
        return {"status": "conflict", "idempotency_key": key}

    @staticmethod
    def _save_idem(
        cur: sqlite3.Cursor,
        key: str | None,
        operation: str,
        request_hash: str,
        result: Mapping[str, Any],
    ) -> None:
        if key is None:
            return
        key = _text(key, "idempotency_key")
        cur.execute(
            "INSERT INTO research_plan_idempotency(idempotency_key,operation,request_hash,"
            "result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), _now()),
        )

    def _event(
        self,
        cur: sqlite3.Cursor,
        plan_version_ref: str,
        state: str,
        reason: str,
        metadata: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        events = _read_plan_event_history(cur, plan_version_ref)
        prior = events[-1]["state"] if events else None
        if state not in _PLAN_TRANSITIONS.get(prior, set()):
            raise ResearchPlanConflict(
                f"research plan transition {prior!r} -> {state!r} is invalid"
            )
        created_at = _now()
        wire = self._record({
            "schema_version": SCHEMA_VERSION,
            "id": f"research-plan-event:{uuid.uuid4().hex}",
            "plan_version_ref": _text(plan_version_ref, "plan_version_ref"),
            "state": state,
            "reason": _text(reason, "reason"),
            "metadata": dict(metadata),
            "actor_ref": _text(actor_ref, "actor_ref"),
            "created_at": created_at,
        })
        cur.execute(
            "INSERT INTO research_plan_events(event_id,plan_version_ref,state,reason,"
            "metadata_json,actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?)",
            (
                wire["id"], wire["plan_version_ref"], state, wire["reason"],
                canonical_json(wire["metadata"]), wire["actor_ref"], created_at,
                wire["content_hash"],
            ),
        )
        return wire

    def create_plan(
        self,
        *,
        question_ref: str,
        question_version_ref: str,
        decision_ref: str,
        issuer_cik: str,
        form: str,
        filing_date_from: str,
        filing_date_to: str,
        actor_ref: str,
        idempotency_key: str | None = None,
        company_facts_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one immutable plan from an exact selected question/decision.

        The plan ref is derived from the canonical binding, never from a
        caller id/hash.  Inside one Core transaction the plan version row and
        its ``created`` event are appended AND the backlog question advances
        ``selected -> planned`` with the exact plan binding; a failure
        anywhere rolls everything back.  Replays return the original result;
        an identical binding resolves to the existing plan version
        (duplicate).  Question/decision/candidate/cycle/mandate/policy/SEC
        scope mismatches all fail closed and leave no residue.
        """

        question_ref = _text(question_ref, "question_ref")
        question_version_ref = _text(question_version_ref, "question_version_ref")
        decision_ref = _text(decision_ref, "decision_ref")
        if company_facts_request is None:
            operation_name = SEC_OPERATION
            sec_request = _validate_sec_request({
                "issuer_cik": issuer_cik,
                "form": form,
                "filing_date_from": filing_date_from,
                "filing_date_to": filing_date_to,
            })
            request_scope = {"sec_request": sec_request}
        else:
            operation_name = SEC_COMPANY_FACTS_OPERATION
            sec_request = _validate_company_facts_request(company_facts_request)
            if sec_request["cik"] != _text(issuer_cik, "issuer_cik").zfill(10):
                raise ResearchPlanValidationError(
                    "issuer_cik must match company_facts_request.cik"
                )
            if sec_request["form"] != _text(form, "form"):
                raise ResearchPlanValidationError(
                    "form must match company_facts_request.form"
                )
            request_scope = {
                "operation": operation_name,
                "company_facts_request": sec_request,
            }
        actor_ref = _text(actor_ref, "actor_ref")
        request = {
            "question_ref": question_ref,
            "question_version_ref": question_version_ref,
            "decision_ref": decision_ref,
            **request_scope,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "create_research_plan", request_hash)
            if duplicate is not None:
                return duplicate
            question = read_exact_backlog_question(cur, question_ref)
            head = cur.execute(
                "SELECT v.* FROM backlog_question_pointer p "
                "JOIN backlog_question_versions v ON v.version_id=p.version_id "
                "WHERE p.question_ref=?",
                (question_ref,),
            ).fetchone()
            if head is None:
                raise ResearchPlanConflict("backlog question has no head version")
            head_wire = read_exact_backlog_question_version(cur, head["version_id"])
            if (
                head_wire["id"] != question_version_ref
                or head_wire["question_ref"] != question_ref
            ):
                raise ResearchPlanConflict(
                    "question_version_ref is not the exact head version of the question"
                )
            decision = _read_exact_agenda_decision(cur, decision_ref)
            cycle = read_exact_agenda_cycle(cur, decision["cycle_ref"])
            mandate = read_exact_mandate_version(cur, cycle["mandate_version_ref"])
            policy = read_exact_agenda_policy_version(cur, cycle["policy_version_ref"])
            candidate_ref = _selected_candidate_ref_for_question(
                cur, decision=decision, cycle=cycle, question_version=head_wire
            )
            agenda_binding = {
                "decision_ref": decision["id"],
                "decision_hash": decision["content_hash"],
                "cycle_ref": cycle["cycle_id"],
                "cycle_hash": cycle["content_hash"],
                "candidate_ref": candidate_ref,
                "mandate_version_ref": mandate["id"],
                "mandate_version_hash": mandate["content_hash"],
                "policy_version_ref": policy["id"],
                "policy_version_hash": policy["content_hash"],
            }
            _revalidate_agenda_binding(
                cur, question_ref=question_ref, question=question,
                question_version=head_wire,
                binding=agenda_binding,
            )
            plan_ref = plan_version_ref_for(
                question_ref=question_ref,
                question_version_ref=question_version_ref,
                decision_ref=decision["id"],
                sec_request=sec_request,
                operation=operation_name,
            )
            existing = cur.execute(
                "SELECT * FROM research_plan_versions WHERE version_id=?",
                (plan_ref,),
            ).fetchone()
            created_at = _now()
            if existing is not None:
                existing_wire = read_exact_research_plan_version(cur, plan_ref)
                from .research_question_backlog import _read_exact_event_history

                question_events = _read_exact_event_history(
                    cur, question_ref, revalidate_plan=False
                )
                planned_binding = next(
                    (
                        event for event in question_events
                        if event["state"] == "planned"
                        and event["metadata"] == {
                            "plan_version_ref": plan_ref,
                            "plan_version_hash": existing_wire["content_hash"],
                        }
                    ),
                    None,
                )
                if planned_binding is None:
                    raise ResearchPlanConflict(
                        "existing plan is not bound by the question's planned event"
                    )
                return {
                    "status": "duplicate",
                    "plan_version_ref": plan_ref,
                    "plan_version_hash": existing_wire["content_hash"],
                    "question_ref": question_ref,
                    "question_state": self._latest_backlog_state(cur, question_ref),
                    "plan_state": self._plan_state(cur, plan_ref),
                }
            if self._latest_backlog_state(cur, question_ref) != "selected":
                raise ResearchPlanConflict(
                    "only a selected question can produce a new plan"
                )
            context = _recompute_context_binding(cur, cycle)
            template = _sec_template()
            operation = _sec_operation(template, operation_name)
            scope = {
                "source_ref": template["source_identity"]["source_ref"],
                "connector_profile_ref": template["id"],
                "connector_profile_hash": template["content_hash"],
                "operation": operation_name,
                "parameters": sec_request,
                "permission_scope": _operation_permission_scope(operation_name),
                "auth_mode": template["auth_boundary"]["mode"],
                "side_effect_class": SIDE_EFFECT_CLASS,
                "declared_side_effects": ["read:public-http"],
                "verifier_ref": SEC_VERIFIER_REF,
                "output_contract_ref": operation["output_schema_ref"],
                "output_contract_hash": operation["output_schema_hash"],
                "budget": _execution_budget(template, operation_name),
                "steps": build_research_plan_steps(
                    plan_version_ref=plan_ref,
                    sec_request=sec_request,
                    max_attempts=SEC_MAX_ATTEMPTS,
                    operation=operation_name,
                ),
            }
            plan_wire = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": plan_ref,
                "created_at": created_at,
                "planner_ref": PLANNER_REF,
                "planner_hash": PLANNER_HASH,
                "version": 1,
                "prior_version_ref": None,
                "question_ref": question_ref,
                "question_version_ref": head_wire["id"],
                "question_version_hash": head_wire["content_hash"],
                "agenda_binding": agenda_binding,
                "context_binding_ref": context["id"],
                "context_binding_hash": context["content_hash"],
                "execution_scope": scope,
                "actor_ref": actor_ref,
            })
            cur.execute(
                "INSERT INTO research_plan_versions(version_id,question_ref,"
                "question_version_ref,version_number,prior_version_id,decision_ref,"
                "cycle_ref,planner_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_ref, question_ref, head_wire["id"], 1, None,
                    decision["id"], cycle["cycle_id"], PLANNER_REF,
                    canonical_json(plan_wire), plan_wire["content_hash"], created_at,
                ),
            )
            event = self._event(
                cur, plan_ref, "created", "plan_recorded",
                {
                    "question_ref": question_ref,
                    "question_version_ref": head_wire["id"],
                },
                actor_ref,
            )
            from .research_question_backlog import _append_event_row

            planned_event = _append_event_row(
                cur, question_ref=question_ref, state="planned",
                reason="exact_plan_bound",
                metadata={
                    "plan_version_ref": plan_ref,
                    "plan_version_hash": plan_wire["content_hash"],
                },
                actor_ref=actor_ref,
            )
            result = {
                "status": "fresh",
                "plan_version_ref": plan_ref,
                "plan_version_hash": plan_wire["content_hash"],
                "question_ref": question_ref,
                "question_state": "planned",
                "plan_state": "pending",
                "event": event,
                "planned_event": planned_event,
            }
            self._save_idem(
                cur, idempotency_key, "create_research_plan", request_hash, result
            )
            return result

    def create_company_facts_plan(
        self,
        *,
        question_ref: str,
        question_version_ref: str,
        decision_ref: str,
        cik: str,
        concept: str,
        filed_to: str,
        actor_ref: str,
        taxonomy: str = "us-gaap",
        unit: str = "USD",
        form: str = "10-Q",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record the closed Company Concept variant of the SEC plan."""

        return self.create_plan(
            question_ref=question_ref,
            question_version_ref=question_version_ref,
            decision_ref=decision_ref,
            issuer_cik=cik,
            form=form,
            # The list-filings date arguments are not part of this operation;
            # bind them to filed_to so the wrapper cannot smuggle a second
            # temporal scope into the generic creation path.
            filing_date_from=filed_to,
            filing_date_to=filed_to,
            actor_ref=actor_ref,
            idempotency_key=idempotency_key,
            company_facts_request={
                "cik": cik,
                "taxonomy": taxonomy,
                "concept": concept,
                "unit": unit,
                "form": form,
                "filed_to": filed_to,
            },
        )

    def approve_plan(
        self,
        *,
        plan_version_ref: str,
        decision: str,
        reason: str,
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record the single terminal human approval decision for a plan.

        Exactly one decision per exact plan version: the first decision
        (``accepted`` or ``rejected``) is terminal and a second decision
        fails closed.  Only exact ``human:`` principals may decide; auto-
        accept timeouts, automation/model principals, Agenda approval and
        Discord reactions are not plan-start authority.  The plan hash is
        re-derived inside the transaction, so approval of a tampered or
        forged plan version fails closed.
        """

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        decision = _text(decision, "decision")
        if decision not in {"accepted", "rejected"}:
            raise ResearchPlanValidationError(
                "decision must be accepted or rejected"
            )
        reason = _text(reason, "reason")
        actor_ref = _text(actor_ref, "actor_ref")
        if _HUMAN_ACTOR_RE.fullmatch(actor_ref) is None:
            raise ResearchPlanValidationError(
                "only an exact human principal can approve a research plan"
            )
        request = {
            "plan_version_ref": plan_version_ref,
            "decision": decision,
            "reason": reason,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_plan_approval", request_hash)
            if duplicate is not None:
                return duplicate
            plan = read_exact_research_plan_version(cur, plan_version_ref)
            existing = cur.execute(
                "SELECT approval_id FROM research_plan_approvals "
                "WHERE plan_version_ref=?",
                (plan_version_ref,),
            ).fetchone()
            policy_existing = cur.execute(
                "SELECT authorization_id FROM research_plan_policy_authorizations "
                "WHERE plan_version_ref=?",
                (plan_version_ref,),
            ).fetchone()
            if existing is not None or policy_existing is not None:
                raise ResearchPlanConflict(
                    "research plan already has a terminal start authorization"
                )
            created_at = _now()
            approval_id = "research-plan-approval:" + uuid.uuid4().hex
            approval_wire = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": approval_id,
                "plan_version_ref": plan_version_ref,
                "plan_version_hash": plan["content_hash"],
                "decision": decision,
                "reason": reason,
                "actor_ref": actor_ref,
                "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO research_plan_approvals(approval_id,plan_version_ref,"
                "plan_version_hash,decision,reason,actor_ref,created_at,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    approval_id, plan_version_ref, plan["content_hash"], decision,
                    reason, actor_ref, created_at, approval_wire["content_hash"],
                ),
            )
            result = {
                "status": "fresh",
                "plan_version_ref": plan_version_ref,
                "plan_state": "approved" if decision == "accepted" else "rejected",
                "approval": approval_wire,
            }
            self._save_idem(
                cur, idempotency_key, "record_plan_approval", request_hash, result
            )
            return result

    def authorize_plan_by_policy(
        self,
        *,
        plan_version_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Authorize one closed public SEC plan under the active policy."""

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        request_hash = content_hash({"plan_version_ref": plan_version_ref})
        with self.store._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "authorize_plan_by_policy", request_hash
            )
            if duplicate is not None:
                return duplicate
            plan = read_exact_research_plan_version(cur, plan_version_ref)
            if plan["execution_scope"]["parameters"]["form"] != "10-Q":
                raise ResearchPlanConflict(
                    "active autonomous plan rule currently accepts 10-Q only"
                )
            policy_row = cur.execute(
                "SELECT v.* FROM governance_policy_pointer p "
                "JOIN governance_policy_versions v "
                "ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1"
            ).fetchone()
            if policy_row is None:
                raise ResearchPlanConflict("no active governance policy")
            self.store._assert_policy_effective(policy_row)
            try:
                policy = json.loads(policy_row["policy_json"])
            except (TypeError, ValueError) as exc:
                raise ResearchPlanConflict("active governance policy is corrupt") from exc
            rule = policy.get("research_plan_auto_start")
            expected_rule = _operation_policy_rule(
                plan["execution_scope"]["operation"]
            )
            if (
                not isinstance(rule, Mapping)
                or set(rule) != {"enabled", "rules"}
                or rule["enabled"] is not True
                or rule["rules"] != [expected_rule]
            ):
                raise ResearchPlanConflict(
                    "active governance policy does not authorize this research plan"
                )
            human = cur.execute(
                "SELECT approval_id FROM research_plan_approvals WHERE plan_version_ref=?",
                (plan_version_ref,),
            ).fetchone()
            if human is not None:
                raise ResearchPlanConflict(
                    "research plan already has a terminal human decision"
                )
            existing = cur.execute(
                "SELECT authorization_id FROM research_plan_policy_authorizations "
                "WHERE plan_version_ref=?",
                (plan_version_ref,),
            ).fetchone()
            if existing is not None:
                authorization = read_exact_research_plan_policy_authorization(
                    cur, existing["authorization_id"]
                )
                result = {
                    "status": "duplicate",
                    "plan_version_ref": plan_version_ref,
                    "plan_state": "approved",
                    "authorization": authorization,
                }
                self._save_idem(
                    cur, idempotency_key, "authorize_plan_by_policy", request_hash, result
                )
                return result
            policy_ref = policy_row["policy_version_id"]
            policy_hash = policy_row["content_hash"]
            authorization_id = "research-plan-policy-authorization:" + content_hash({
                "plan_version_ref": plan_version_ref,
                "plan_version_hash": plan["content_hash"],
                "policy_version_ref": policy_ref,
                "policy_version_hash": policy_hash,
                "rule_ref": expected_rule,
            })[:32]
            authorization = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": authorization_id,
                "plan_version_ref": plan_version_ref,
                "plan_version_hash": plan["content_hash"],
                "decision": "accepted",
                "reason": "matched active low-risk public SEC plan policy",
                "actor_ref": PLAN_AUTO_START_ACTOR_REF,
                "authorization": "versioned_governance_policy",
                "policy_version_ref": policy_ref,
                "policy_version_hash": policy_hash,
                "rule_ref": expected_rule,
                "created_at": max(plan["created_at"], policy_row["created_at"]),
            })
            cur.execute(
                "INSERT INTO research_plan_policy_authorizations("
                "authorization_id,plan_version_ref,plan_version_hash,policy_version_ref,"
                "policy_version_hash,rule_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    authorization["id"], authorization["plan_version_ref"],
                    authorization["plan_version_hash"], authorization["policy_version_ref"],
                    authorization["policy_version_hash"], authorization["rule_ref"],
                    canonical_json(authorization), authorization["content_hash"],
                    authorization["created_at"],
                ),
            )
            result = {
                "status": "fresh",
                "plan_version_ref": plan_version_ref,
                "plan_state": "approved",
                "authorization": authorization,
            }
            self._save_idem(
                cur, idempotency_key, "authorize_plan_by_policy", request_hash, result
            )
            return result

    def _latest_backlog_state(self, cur: sqlite3.Cursor, question_ref: str) -> str | None:
        from .research_question_backlog import _read_exact_event_history

        events = _read_exact_event_history(
            cur, question_ref, revalidate_plan=False
        )
        return None if not events else events[-1]["state"]

    def _plan_state(self, cur: sqlite3.Cursor, plan_version_ref: str) -> str:
        events = _read_plan_event_history(cur, plan_version_ref)
        if events and events[-1]["state"] == "started":
            return "started"
        approval = cur.execute(
            "SELECT decision FROM research_plan_approvals WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        if approval is not None:
            return "approved" if approval["decision"] == "accepted" else "rejected"
        policy_authorization = cur.execute(
            "SELECT 1 FROM research_plan_policy_authorizations WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        if policy_authorization is not None:
            return "approved"
        return "pending"

    def plan(self, plan_version_ref: str) -> dict[str, Any]:
        """Exact reader: plan version, event history, approval and start binding."""

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        cursor = self.connection.cursor()
        plan = read_exact_research_plan_version(cursor, plan_version_ref)
        events = _read_plan_event_history(cursor, plan_version_ref)
        approval_row = cursor.execute(
            "SELECT approval_id FROM research_plan_approvals WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        approval = None
        if approval_row is not None:
            approval = read_exact_research_plan_approval(
                cursor, approval_row["approval_id"]
            )
        policy_row = cursor.execute(
            "SELECT authorization_id FROM research_plan_policy_authorizations "
            "WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        policy_authorization = None
        if policy_row is not None:
            policy_authorization = read_exact_research_plan_policy_authorization(
                cursor, policy_row["authorization_id"]
            )
        start_row = cursor.execute(
            "SELECT start_id FROM research_plan_starts WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        start_binding = None
        if start_row is not None:
            start_binding = read_exact_research_plan_start(
                cursor, start_row["start_id"]
            )
        is_started = bool(events and events[-1]["state"] == "started")
        if is_started != (start_binding is not None):
            raise ResearchPlanConflict(
                "research plan event history and start binding disagree"
            )
        return {
            "plan_version": plan,
            "events": events,
            "state": self._plan_state(cursor, plan_version_ref),
            "approval": approval,
            "policy_authorization": policy_authorization,
            "start_binding": start_binding,
        }

    def plan_version(self, version_ref: str) -> dict[str, Any]:
        """Exact reader for one immutable plan version."""

        return read_exact_research_plan_version(self.connection.cursor(), version_ref)

    def approval(self, approval_id: str) -> dict[str, Any]:
        """Exact reader for one approval decision."""

        return read_exact_research_plan_approval(self.connection.cursor(), approval_id)

    def plans(self, *, question_ref: str | None = None) -> list[dict[str, Any]]:
        """List plan heads, optionally filtered by logical question."""

        cursor = self.connection.cursor()
        rows = cursor.execute(
            "SELECT version_id FROM research_plan_versions "
            "WHERE (? IS NULL OR question_ref=?) ORDER BY version_id",
            (question_ref, question_ref),
        ).fetchall()
        return [self.plan(row["version_id"]) for row in rows]


def _plan_work_orders(plan_wire: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rebuild every planned WorkOrder solely from immutable plan authority."""

    steps = plan_wire["execution_scope"]["steps"]
    work_orders: list[dict[str, Any]] = []
    prior_work_ref: str | None = None
    for step in steps:
        if step["stage"] == "connector":
            request = step["parameters"]
            if step["operation"] == SEC_OPERATION:
                question = (
                    "SEC public read-only list_filings for CIK "
                    f"{request['issuer_cik']}, form {request['form']}, window "
                    f"{request['filing_date_from']}..{request['filing_date_to']}"
                )
            elif step["operation"] == SEC_COMPANY_FACTS_OPERATION:
                question = (
                    "SEC public read-only get_company_facts for CIK "
                    f"{request['cik']}, concept {request['taxonomy']}:"
                    f"{request['concept']}, unit {request['unit']}, form "
                    f"{request['form']}, filed through {request['filed_to']}"
                )
            else:
                raise ResearchPlanConflict(
                    "connector WorkOrder operation is outside the approved SEC reads"
                )
        else:
            question = (
                f"Research plan {plan_wire['id']} stage {step['ordinal']}: "
                f"{step['operation']}"
            )
        input_refs = [
            plan_wire["question_version_ref"],
            plan_wire["agenda_binding"]["decision_ref"],
            step["id"],
        ]
        if prior_work_ref is not None:
            input_refs.append(prior_work_ref)
        wire = {
            "schema_version": "0.1",
            "id": step["work_order_ref"],
            # Plan time, not start-attempt time, makes crash recovery and
            # repeated admission byte-identical.
            "created_at": plan_wire["created_at"],
            "updated_at": plan_wire["created_at"],
            "question": question,
            "requested_capabilities": list(step["requested_capabilities"]),
            "runtime_profile_ref": step["runtime_profile_ref"],
            "budget": {
                **dict(plan_wire["execution_scope"]["budget"]),
                "step_max_attempts": step["max_attempts"],
            },
            "idempotency_key": f"research-plan-work:{plan_wire['id']}:{step['ordinal']}",
            "declared_side_effects": list(step["declared_side_effects"]),
            "status": "ready",
            "input_refs": input_refs,
            "metadata": {
                "plan_version_ref": plan_wire["id"],
                "plan_version_hash": plan_wire["content_hash"],
                "question_version_ref": plan_wire["question_version_ref"],
                "step_ref": step["id"],
                "step_hash": step["content_hash"],
                "stage": step["stage"],
                "operation": step["operation"],
                "permission_scope": plan_wire["execution_scope"]["permission_scope"],
                "upstream_work_order_ref": prior_work_ref,
            },
        }
        from .contracts import WorkOrder

        wire = WorkOrder.from_dict(wire).to_dict()
        work_orders.append(wire)
        prior_work_ref = wire["id"]
    return work_orders


def _plan_link_specs(
    plan_wire: Mapping[str, Any], work_orders: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Build deterministic WorkOrderLink semantics for the frozen task tree."""

    relations = ("decomposed_from", "verifies", "follows_up")
    specs: list[dict[str, Any]] = []
    for index, (parent, child, relation) in enumerate(
        zip(work_orders[:-1], work_orders[1:], relations, strict=True), start=1
    ):
        specs.append({
            "link_id": "work-order-link:research-plan:" + content_hash({
                "plan_version_ref": plan_wire["id"],
                "parent_work_order_ref": parent["id"],
                "child_work_order_ref": child["id"],
            })[:32],
            "parent_work_order_ref": parent["id"],
            "child_work_order_ref": child["id"],
            "relation": relation,
            "sequence": index,
        })
    return specs


def _read_exact_workflow_tree(
    cursor: Any,
    *,
    plan_wire: Mapping[str, Any],
    start_wire: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Recompute workflow, WorkOrder and link authority for a started plan."""

    work_orders = _plan_work_orders(plan_wire)
    root = work_orders[0]
    if (
        root["id"] != start_wire["root_work_order_ref"]
        or content_hash(root) != start_wire["root_work_order_hash"]
    ):
        raise ResearchPlanConflict("ResearchPlanStart root WorkOrder binding drifted")
    for index, expected in enumerate(work_orders):
        row = cursor.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?",
            (expected["id"],),
        ).fetchone()
        # Only the root is admitted at plan start.  A downstream node may be
        # admitted later by the coordinator, but if present it must still be
        # byte-identical to the immutable plan.
        if row is None:
            if index == 0:
                raise ResearchPlanConflict("started plan root WorkOrder is missing")
            continue
        if (
            row["work_order_json"] != canonical_json(expected)
            or row["work_order_hash"] != content_hash(expected)
        ):
            raise ResearchPlanConflict(
                "Scheduler WorkOrder drifted from immutable plan authority"
            )

    workflow_row = cursor.execute(
        "SELECT * FROM observability_workflow_versions "
        "WHERE version_id=?",
        (start_wire["workflow_version_ref"],),
    ).fetchone()
    if workflow_row is None:
        raise ResearchPlanNotFound(
            f"WorkflowRunVersion {start_wire['workflow_version_ref']} is missing"
        )
    workflow_wire = _reverify_record(
        workflow_row["record_json"],
        {
            "id": start_wire["workflow_version_ref"],
            "workflow_ref": start_wire["workflow_ref"],
            "version": workflow_row["version_number"],
            "prior_version_ref": workflow_row["prior_version_id"],
            "created_at": workflow_row["created_at"],
        },
        name="WorkflowRunVersion",
    )
    if (
        workflow_wire["content_hash"] != workflow_row["content_hash"]
        or workflow_wire["content_hash"] != start_wire["workflow_version_hash"]
        or workflow_wire.get("root_work_order_refs") != [root["id"]]
        or workflow_wire.get("scope_refs")
        != [plan_wire["question_ref"], plan_wire["question_version_ref"]]
        or workflow_wire.get("governance_policy_ref")
        != plan_wire["agenda_binding"]["policy_version_ref"]
        or workflow_wire.get("actor_ref") != start_wire["actor_ref"]
        or workflow_wire.get("version") != 1
        or workflow_wire.get("prior_version_ref") is not None
    ):
        raise ResearchPlanConflict(
            "ResearchPlanStart drifted from its WorkflowRunVersion row"
        )

    link_specs = _plan_link_specs(plan_wire, work_orders)
    rows = cursor.execute(
        "SELECT * FROM observability_work_order_links WHERE workflow_ref=? "
        "ORDER BY sequence_number,link_id",
        (start_wire["workflow_ref"],),
    ).fetchall()
    if len(rows) != len(link_specs):
        raise ResearchPlanConflict("ResearchPlanStart workflow tree is incomplete")
    links: list[dict[str, Any]] = []
    for row, expected in zip(rows, link_specs, strict=True):
        link = _reverify_record(
            row["record_json"],
            {
                "id": row["link_id"],
                "workflow_ref": row["workflow_ref"],
                "parent_work_order_ref": row["parent_work_order_ref"],
                "child_work_order_ref": row["child_work_order_ref"],
                "relation": row["relation"],
                "sequence": row["sequence_number"],
                "created_at": row["created_at"],
            },
            name="WorkOrderLink",
        )
        if link["content_hash"] != row["content_hash"]:
            raise ResearchPlanConflict("WorkOrderLink content_hash column drifted")
        for field in (
            "link_id", "parent_work_order_ref", "child_work_order_ref",
            "relation", "sequence",
        ):
            link_field = "id" if field == "link_id" else field
            if link[link_field] != expected[field]:
                raise ResearchPlanConflict(
                    "WorkOrderLink drifted from the immutable research plan tree"
                )
        links.append(link)
    return workflow_wire, work_orders, links


class ResearchPlanControlPlane:
    """Service seam that starts an approved plan on the existing authorities.

    Start reuses the existing observability WorkflowRunVersion authority and
    the Scheduler queue authority; it does not create a second queue/DAG
    system.  The exact root WorkOrder is derived solely from the plan
    authority (no caller content), enqueued idempotently, then the plan
    start binding, the started event and the backlog ``planned ->
    in_progress`` transition are appended in one Core transaction.  A crash
    on any seam leaves at most one deterministic duplicate of each row and
    converges on replay; conflicts fail closed.

    The connector WorkOrder is the sole admitted root.  Three deterministic
    WorkOrderLink rows describe the resolver, verifier and staging children;
    those children are not admitted until their exact predecessor completes.
    """

    def __init__(
        self,
        plan: ResearchPlanAuthority,
        backlog: Any,
        observability: Any,
        scheduler: Any,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self.plan = plan
        self.backlog = backlog
        self.observability = observability
        self.scheduler = scheduler
        self.fault_injector = fault_injector
        authority_connections = [
            getattr(backlog, "connection", None),
            getattr(observability, "connection", None),
            getattr(scheduler, "connection", None),
        ]
        if any(connection is not plan.connection for connection in authority_connections):
            raise TypeError(
                "plan, backlog, observability and scheduler must share one Core connection"
            )

    def _inject(self, seam: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(seam)

    @staticmethod
    def _work_orders(plan_wire: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _plan_work_orders(plan_wire)

    def start_plan(
        self,
        *,
        plan_version_ref: str,
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Start one authorized plan: workflow version + root WorkOrder + gate.

        Validation is re-run on every seam, so a concurrent authority change,
        a replayed request or a tampered row fails closed instead of
        double-starting the plan.  The Scheduler stays the only queue
        authority; duplicate replays return the original outcome.
        """

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        request = {"plan_version_ref": plan_version_ref, "actor_ref": actor_ref}
        request_hash = content_hash(request)
        idempotency_key = _text(idempotency_key, "idempotency_key") if idempotency_key else None
        cursor = self.plan.connection.cursor()
        duplicate = self.plan._idem(
            cursor, idempotency_key, "start_research_plan", request_hash
        )
        if duplicate is not None:
            return duplicate
        plan_wire = read_exact_research_plan_version(cursor, plan_version_ref)
        approval = self._exact_start_authorization(cursor, plan_wire)
        existing_start = cursor.execute(
            "SELECT start_id FROM research_plan_starts WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        if existing_start is not None:
            start_wire = read_exact_research_plan_start(
                cursor, existing_start["start_id"]
            )
            return self._duplicate_start_result(cursor, plan_wire, start_wire)
        self._assert_startable(cursor, plan_wire)
        created_at = _now()
        work_orders = self._work_orders(plan_wire)
        root_work_order = work_orders[0]
        self._inject("before_enqueue")
        enqueued = self.scheduler.enqueue(root_work_order)
        if enqueued["status"] == "conflict":
            raise ResearchPlanConflict(
                "root WorkOrder enqueue conflict; plan authority drifted"
            )
        root_work_order_hash = enqueued["work_order_hash"]
        self._inject("after_enqueue")
        workflow_ref = "workflow:research-plan:" + content_hash(
            {"plan_version_ref": plan_version_ref}
        )[:32]
        workflow_version_ref = "workflow-version:research-plan:" + content_hash(
            {"plan_version_ref": plan_version_ref}
        )[:32]
        workflow_result = self.observability.create_workflow_version(
            workflow_ref,
            title=f"Research plan {plan_version_ref}",
            objective=(
                f"Execute the authorized SEC public read-only plan for question "
                f"{plan_wire['question_ref']}"
            ),
            scope_refs=[plan_wire["question_ref"], plan_wire["question_version_ref"]],
            root_work_order_refs=[root_work_order["id"]],
            governance_policy_ref=plan_wire["agenda_binding"]["policy_version_ref"],
            actor_ref=actor_ref,
            version_id=workflow_version_ref,
            idempotency_key=f"research-plan-workflow:{plan_version_ref}",
        )
        if workflow_result["status"] == "conflict":
            raise ResearchPlanConflict(
                "WorkflowRunVersion idempotency conflict; plan authority drifted"
            )
        workflow_wire = self.observability.get_workflow_version(workflow_version_ref)
        workflow_version_hash = workflow_wire["content_hash"]
        self._inject("after_workflow")
        link_specs = _plan_link_specs(plan_wire, work_orders)
        for spec in link_specs:
            linked = self.observability.link_work_order(
                workflow_ref,
                spec["parent_work_order_ref"],
                spec["child_work_order_ref"],
                relation=spec["relation"],
                sequence=spec["sequence"],
                actor_ref=actor_ref,
                link_id=spec["link_id"],
                idempotency_key=f"research-plan-link:{plan_version_ref}:{spec['sequence']}",
            )
            if linked["status"] == "conflict":
                raise ResearchPlanConflict(
                    "WorkOrderLink idempotency conflict; plan authority drifted"
                )
            self._inject(f"after_link:{spec['sequence']}")
        self._inject("before_plan_binding")
        with self.plan.store._transaction() as cur:
            duplicate = self.plan._idem(
                cur, idempotency_key, "start_research_plan", request_hash
            )
            if duplicate is not None:
                return duplicate
            plan_recheck = read_exact_research_plan_version(cur, plan_version_ref)
            if plan_recheck["content_hash"] != plan_wire["content_hash"]:
                raise ResearchPlanConflict("plan drifted between start seams")
            approval_recheck = self._exact_start_authorization(cur, plan_recheck)
            self._assert_startable(cur, plan_recheck)
            if approval_recheck["content_hash"] != approval["content_hash"]:
                raise ResearchPlanConflict("approval drifted between start seams")
            provisional_start = {
                "workflow_ref": workflow_ref,
                "workflow_version_ref": workflow_version_ref,
                "workflow_version_hash": workflow_version_hash,
                "root_work_order_ref": root_work_order["id"],
                "root_work_order_hash": root_work_order_hash,
                "actor_ref": actor_ref,
            }
            _read_exact_workflow_tree(
                cur, plan_wire=plan_recheck, start_wire=provisional_start,
            )
            start_id = plan_start_ref_for(plan_version_ref)
            started_event = self.plan._event(
                cur, plan_version_ref, "started", "approved_plan_started",
                {
                    "workflow_version_ref": workflow_version_ref,
                    "workflow_version_hash": workflow_version_hash,
                    "root_work_order_ref": root_work_order["id"],
                    "root_work_order_hash": root_work_order_hash,
                },
                actor_ref,
            )
            self._inject("after_started_event")
            start_wire = self.plan._record({
                "schema_version": SCHEMA_VERSION,
                "id": start_id,
                "plan_version_ref": plan_version_ref,
                "plan_version_hash": plan_recheck["content_hash"],
                "approval_ref": approval["id"],
                "approval_hash": approval["content_hash"],
                "workflow_ref": workflow_ref,
                "workflow_version_ref": workflow_version_ref,
                "workflow_version_hash": workflow_version_hash,
                "root_work_order_ref": root_work_order["id"],
                "root_work_order_hash": root_work_order_hash,
                "event_ref": started_event["id"],
                "actor_ref": actor_ref,
                "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO research_plan_starts(start_id,plan_version_ref,"
                "plan_version_hash,approval_ref,approval_hash,workflow_ref,"
                "workflow_version_ref,workflow_version_hash,root_work_order_ref,"
                "root_work_order_hash,event_ref,actor_ref,created_at,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    start_id, plan_version_ref, start_wire["plan_version_hash"],
                    start_wire["approval_ref"], start_wire["approval_hash"],
                    start_wire["workflow_ref"], start_wire["workflow_version_ref"],
                    start_wire["workflow_version_hash"], start_wire["root_work_order_ref"],
                    start_wire["root_work_order_hash"], start_wire["event_ref"],
                    start_wire["actor_ref"], start_wire["created_at"],
                    start_wire["content_hash"],
                ),
            )
            self._inject("after_start_binding")
            from .research_question_backlog import _append_event_row

            in_progress_event = _append_event_row(
                cur, question_ref=plan_recheck["question_ref"], state="in_progress",
                reason="approved_plan_started",
                metadata={
                    "plan_version_ref": plan_version_ref,
                    "plan_version_hash": plan_recheck["content_hash"],
                    "workflow_version_ref": workflow_version_ref,
                    "workflow_version_hash": workflow_version_hash,
                    "root_work_order_ref": root_work_order["id"],
                    "root_work_order_hash": root_work_order_hash,
                },
                actor_ref=actor_ref,
            )
            self._inject("after_question_transition")
            result = {
                "status": "fresh",
                "plan_version_ref": plan_version_ref,
                "plan_version_hash": plan_recheck["content_hash"],
                "question_ref": plan_recheck["question_ref"],
                "question_state": "in_progress",
                "plan_state": "started",
                "workflow_ref": workflow_ref,
                "workflow_version_ref": workflow_version_ref,
                "workflow_version_hash": workflow_version_hash,
                "root_work_order_ref": root_work_order["id"],
                "root_work_order_hash": root_work_order_hash,
                "task_tree": [
                    {
                        "step_ref": step["id"],
                        "work_order_ref": work["id"],
                        "work_order_hash": content_hash(work),
                        "admission_state": "queued" if index == 0 else "planned",
                    }
                    for index, (step, work) in enumerate(
                        zip(plan_recheck["execution_scope"]["steps"], work_orders, strict=True)
                    )
                ],
                "work_order_links": link_specs,
                "start_binding": start_wire,
                "started_event": started_event,
                "in_progress_event": in_progress_event,
            }
            self.plan._save_idem(
                cur, idempotency_key, "start_research_plan", request_hash, result
            )
            return result

    def _duplicate_start_result(
        self,
        cursor: Any,
        plan_wire: Mapping[str, Any],
        start_wire: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Rebuild a completed start response without replaying side effects."""

        _, work_orders, links = _read_exact_workflow_tree(
            cursor, plan_wire=plan_wire, start_wire=start_wire,
        )
        started_event = read_exact_research_plan_event(
            cursor, start_wire["event_ref"]
        )
        from .research_question_backlog import _read_exact_event_history

        question_events = _read_exact_event_history(
            cursor, plan_wire["question_ref"], revalidate_plan=False
        )
        expected_metadata = {
            "plan_version_ref": plan_wire["id"],
            "plan_version_hash": plan_wire["content_hash"],
            "workflow_version_ref": start_wire["workflow_version_ref"],
            "workflow_version_hash": start_wire["workflow_version_hash"],
            "root_work_order_ref": start_wire["root_work_order_ref"],
            "root_work_order_hash": start_wire["root_work_order_hash"],
        }
        in_progress_event = next(
            (
                event for event in question_events
                if event["state"] == "in_progress"
                and event["metadata"] == expected_metadata
            ),
            None,
        )
        if in_progress_event is None:
            raise ResearchPlanConflict(
                "started plan lacks its exact in_progress question binding"
            )
        task_tree = []
        for step, work in zip(
            plan_wire["execution_scope"]["steps"], work_orders, strict=True
        ):
            row = cursor.execute(
                "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
                (work["id"],),
            ).fetchone()
            task_tree.append({
                "step_ref": step["id"],
                "work_order_ref": work["id"],
                "work_order_hash": content_hash(work),
                "admission_state": "queued" if row is not None else "planned",
            })
        return {
            "status": "duplicate",
            "plan_version_ref": plan_wire["id"],
            "plan_version_hash": plan_wire["content_hash"],
            "question_ref": plan_wire["question_ref"],
            "question_state": question_events[-1]["state"],
            "plan_state": "started",
            "workflow_ref": start_wire["workflow_ref"],
            "workflow_version_ref": start_wire["workflow_version_ref"],
            "workflow_version_hash": start_wire["workflow_version_hash"],
            "root_work_order_ref": start_wire["root_work_order_ref"],
            "root_work_order_hash": start_wire["root_work_order_hash"],
            "task_tree": task_tree,
            "work_order_links": [
                {
                    "link_id": link["id"],
                    "parent_work_order_ref": link["parent_work_order_ref"],
                    "child_work_order_ref": link["child_work_order_ref"],
                    "relation": link["relation"],
                    "sequence": link["sequence"],
                }
                for link in links
            ],
            "start_binding": dict(start_wire),
            "started_event": started_event,
            "in_progress_event": in_progress_event,
        }

    def _exact_start_authorization(
        self, cursor: Any, plan_wire: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Require one exact human or active-policy start authorization."""

        row = cursor.execute(
            "SELECT approval_id FROM research_plan_approvals WHERE plan_version_ref=?",
            (plan_wire["id"],),
        ).fetchone()
        if row is not None:
            approval = read_exact_research_plan_approval(cursor, row["approval_id"])
            if approval["decision"] != "accepted":
                raise ResearchPlanConflict(
                    "plan approval decision is not accepted; start is not authorized"
                )
            if approval["plan_version_hash"] != plan_wire["content_hash"]:
                raise ResearchPlanConflict(
                    "plan approval drifted from the exact plan version"
                )
            return approval
        policy_row = cursor.execute(
            "SELECT authorization_id FROM research_plan_policy_authorizations "
            "WHERE plan_version_ref=?",
            (plan_wire["id"],),
        ).fetchone()
        if policy_row is None:
            raise ResearchPlanConflict(
                "plan has no human or policy start authorization"
            )
        authorization = read_exact_research_plan_policy_authorization(
            cursor, policy_row["authorization_id"]
        )
        active = cursor.execute(
            "SELECT v.policy_version_id,v.content_hash,v.effective_from,v.effective_until "
            "FROM governance_policy_pointer p JOIN governance_policy_versions v "
            "ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1"
        ).fetchone()
        if active is None:
            raise ResearchPlanConflict("no active governance policy")
        self.plan.store._assert_policy_effective(active)
        if (
            active["policy_version_id"] != authorization["policy_version_ref"]
            or active["content_hash"] != authorization["policy_version_hash"]
        ):
            raise ResearchPlanConflict(
                "plan policy authorization is no longer active"
            )
        return authorization

    def _assert_startable(
        self, cursor: Any, plan_wire: Mapping[str, Any]
    ) -> None:
        """The plan must be unstarted and its question must be planned."""

        events = _read_plan_event_history(cursor, plan_wire["id"])
        if events and events[-1]["state"] == "started":
            raise ResearchPlanConflict("plan is already started")
        if not events or events[-1]["state"] != "created":
            raise ResearchPlanConflict("plan is not in a startable state")
        start_row = cursor.execute(
            "SELECT start_id FROM research_plan_starts WHERE plan_version_ref=?",
            (plan_wire["id"],),
        ).fetchone()
        if start_row is not None:
            raise ResearchPlanConflict(
                "plan is already started; replay with the original idempotency key"
            )
        from .research_question_backlog import _read_exact_event_history

        question_events = _read_exact_event_history(
            cursor, plan_wire["question_ref"], revalidate_plan=False
        )
        state = question_events[-1]["state"] if question_events else None
        if state != "planned":
            raise ResearchPlanConflict(
                f"question state is {state!r}; only a planned question can start"
            )
        head = cursor.execute(
            "SELECT v.* FROM backlog_question_pointer p "
            "JOIN backlog_question_versions v ON v.version_id=p.version_id "
            "WHERE p.question_ref=?",
            (plan_wire["question_ref"],),
        ).fetchone()
        if head is None:
            raise ResearchPlanConflict("backlog question has no head version")
        head_wire = read_exact_backlog_question_version(cursor, head["version_id"])
        if plan_wire["question_version_ref"] != head_wire["id"]:
            raise ResearchPlanConflict(
                "plan binds an older question version than the current head"
            )

    def plan_start(self, plan_version_ref: str) -> dict[str, Any]:
        """Exact reader for the plan start binding, if present."""

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        row = self.plan.connection.execute(
            "SELECT start_id FROM research_plan_starts WHERE plan_version_ref=?",
            (plan_version_ref,),
        ).fetchone()
        if row is None:
            raise ResearchPlanNotFound(f"research plan start {plan_version_ref}")
        start = read_exact_research_plan_start(
            self.plan.connection.cursor(), row["start_id"]
        )
        status = self.scheduler.status(start["root_work_order_ref"])
        if status["work_order_hash"] != start["root_work_order_hash"]:
            raise ResearchPlanConflict(
                "root WorkOrder drifted from the plan start binding"
            )
        return {"start_binding": start, "scheduler_status": status}


__all__ = [
    "ResearchPlanAuthority",
    "ResearchPlanControlPlane",
    "ResearchPlanError",
    "ResearchPlanValidationError",
    "ResearchPlanConflict",
    "ResearchPlanNotFound",
    "PLAN_STATES",
    "PLANNER_REF",
    "SEC_ALLOWED_FORMS",
    "SEC_OPERATION",
    "SEC_COMPANY_FACTS_OPERATION",
    "PERMISSION_SCOPE",
    "COMPANY_FACTS_PERMISSION_SCOPE",
    "PLAN_AUTO_START_RULE_REF",
    "PLAN_COMPANY_FACTS_AUTO_START_RULE_REF",
    "SIDE_EFFECT_CLASS",
    "plan_identity",
    "plan_version_ref_for",
    "plan_start_ref_for",
    "build_research_plan_step",
    "read_exact_research_plan_version",
    "read_exact_research_plan_event",
    "read_exact_research_plan_approval",
    "read_exact_research_plan_start",
    "revalidate_plan_binds_question",
]
