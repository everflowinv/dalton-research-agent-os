"""Closed, rebuildable research context and one-shot connector planning contracts.

These objects are derived projections.  They never create Evidence, Claim, or
Thesis authority, and they never carry credential material or storage paths.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import validate_connector_runner_request
from .contracts import AdjudicationVersion, ClaimVersion, EvidenceRelation
from .recorded_alphaengine_adapter import load_recorded_alphaengine_fixture
from .recorded_source_adapter import load_recorded_source_fixture
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SEARCH_RE = re.compile(r"[a-z0-9_.:-]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "authorization", "cookie", "cookies", "password", "passwd", "secret", "token",
    "accesstoken", "apikey", "authtoken", "bearertoken", "clientsecret",
    "credentialvalue", "databasepath", "dbpath", "privatekey", "refreshtoken",
    "sessiontoken", "storagelocator", "connectionstring",
}
_SENSITIVE_KEY_SUFFIXES = (
    "apikey", "authtoken", "bearertoken", "clientsecret", "privatekey",
    "refreshtoken", "sessiontoken", "connectionstring",
)


def count_dalton_search_tokens(value: str) -> int:
    """Count tokens with the frozen ContextPack 0.1 accounting rule."""

    if not isinstance(value, str):
        raise ResearchContextError("tokenizer input must be text")
    return len(_SEARCH_RE.findall(value))


class ResearchContextError(ValueError):
    pass


class ResearchContextConflict(ResearchContextError):
    pass


def _json(value: Any, name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ResearchContextError(f"{name} must be finite JSON") from exc


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchContextError(f"{name} must be an object")
    wire = dict(value)
    missing = fields - set(wire)
    unknown = set(wire) - fields
    if missing or unknown:
        raise ResearchContextError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    return _json(wire, name)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchContextError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _HASH_RE.fullmatch(value):
        raise ResearchContextError(f"{name} must be lowercase SHA-256 hex")
    return value


def _integer(
    value: Any, name: str, *, minimum: int = 0, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResearchContextError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ResearchContextError(f"{name} must be an integer <= {maximum}")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchContextError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchContextError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _refs(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ResearchContextError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise ResearchContextError(f"{name} must contain unique values")
    return result


def _with_hash(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = dict(value)
    declared = _hash(wire.pop("content_hash"), f"{name}.content_hash")
    expected = content_hash(wire)
    if declared != expected:
        raise ResearchContextConflict(f"{name} content_hash mismatch")
    wire["content_hash"] = declared
    return wire


def _reject_sensitive(value: Any, name: str = "value") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (
                normalized_key in _SENSITIVE_KEYS
                or normalized_key.endswith(_SENSITIVE_KEY_SUFFIXES)
            ):
                raise ResearchContextError(f"{name} contains forbidden sensitive field")
            _reject_sensitive(item, f"{name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive(item, f"{name}[{index}]")


_STEP_FIELDS = {
    "id", "ordinal", "source_ref", "source_hash", "connector_profile_ref",
    "connector_profile_hash", "operation", "parameters", "query_hash",
    "input_schema_ref", "input_schema_hash", "output_schema_ref",
    "output_schema_hash", "completeness_required", "depends_on",
    "fallback_step_refs", "max_attempts", "content_hash",
}


def validate_compiled_connector_step(
    value: Mapping[str, Any], *, name: str = "CompiledConnectorStep"
) -> dict[str, Any]:
    wire = _closed(value, _STEP_FIELDS, name)
    for field in (
        "id", "source_ref", "connector_profile_ref", "operation",
        "input_schema_ref", "output_schema_ref",
    ):
        wire[field] = _text(wire[field], f"{name}.{field}")
    for field in (
        "source_hash", "connector_profile_hash", "query_hash",
        "input_schema_hash", "output_schema_hash",
    ):
        wire[field] = _hash(wire[field], f"{name}.{field}")
    wire["ordinal"] = _integer(wire["ordinal"], f"{name}.ordinal", minimum=1)
    wire["max_attempts"] = _integer(
        wire["max_attempts"], f"{name}.max_attempts", minimum=1, maximum=5
    )
    if not isinstance(wire["parameters"], Mapping):
        raise ResearchContextError(f"{name}.parameters must be an object")
    wire["parameters"] = _json(wire["parameters"], f"{name}.parameters")
    _reject_sensitive(wire["parameters"], f"{name}.parameters")
    expected_query = content_hash(
        {"operation": wire["operation"], "parameters": wire["parameters"]}
    )
    if wire["query_hash"] != expected_query:
        raise ResearchContextConflict(f"{name}.query_hash mismatch")
    if wire["completeness_required"] not in {
        "enumerated", "ranked", "partial_allowed",
    }:
        raise ResearchContextError(f"{name}.completeness_required is invalid")
    wire["depends_on"] = _refs(wire["depends_on"], f"{name}.depends_on")
    wire["fallback_step_refs"] = _refs(
        wire["fallback_step_refs"], f"{name}.fallback_step_refs"
    )
    if wire["id"] in wire["depends_on"] or wire["id"] in wire["fallback_step_refs"]:
        raise ResearchContextError(f"{name} cannot refer to itself")
    return _with_hash(wire, name)


_PLAN_FIELDS = {
    "schema_version", "id", "created_at", "task_ref", "task_hash",
    "planner_ref", "planner_hash", "routing_policy_ref", "routing_policy_hash",
    "steps", "content_hash",
}


def validate_compiled_connector_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _PLAN_FIELDS, "CompiledConnectorPlan")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchContextError("unsupported CompiledConnectorPlan schema_version")
    for field in ("id", "task_ref", "planner_ref", "routing_policy_ref"):
        wire[field] = _text(wire[field], field)
    for field in ("task_hash", "planner_hash", "routing_policy_hash"):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if not isinstance(wire["steps"], list) or not wire["steps"]:
        raise ResearchContextError("CompiledConnectorPlan.steps must be non-empty")
    steps = [
        validate_compiled_connector_step(item, name=f"steps[{index}]")
        for index, item in enumerate(wire["steps"])
    ]
    steps.sort(key=lambda item: item["ordinal"])
    if [item["ordinal"] for item in steps] != list(range(1, len(steps) + 1)):
        raise ResearchContextError("CompiledConnectorPlan ordinals must be contiguous")
    by_ref = {item["id"]: item for item in steps}
    if len(by_ref) != len(steps):
        raise ResearchContextError("CompiledConnectorPlan step ids must be unique")
    fallback_targets: set[str] = set()
    for step in steps:
        for dependency in step["depends_on"]:
            target = by_ref.get(dependency)
            if target is None or target["ordinal"] >= step["ordinal"]:
                raise ResearchContextError("step dependencies must refer to prior steps")
        for fallback in step["fallback_step_refs"]:
            target = by_ref.get(fallback)
            if target is None or target["ordinal"] <= step["ordinal"]:
                raise ResearchContextError("fallback steps must refer to later steps")
            if fallback in fallback_targets:
                raise ResearchContextError("one fallback step cannot serve two primaries")
            fallback_targets.add(fallback)
    wire["steps"] = steps
    _reject_sensitive(wire, "CompiledConnectorPlan")
    return _with_hash(wire, "CompiledConnectorPlan")


def build_compiled_connector_plan(
    *,
    task_ref: str,
    task_hash: str,
    planner_ref: str,
    planner_hash: str,
    routing_policy_ref: str,
    routing_policy_hash: str,
    step_specs: Sequence[Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    created = _timestamp(created_at, "created_at")
    identity = {
        "task_ref": _text(task_ref, "task_ref"),
        "task_hash": _hash(task_hash, "task_hash"),
        "planner_ref": _text(planner_ref, "planner_ref"),
        "planner_hash": _hash(planner_hash, "planner_hash"),
        "routing_policy_ref": _text(routing_policy_ref, "routing_policy_ref"),
        "routing_policy_hash": _hash(routing_policy_hash, "routing_policy_hash"),
    }
    steps: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(step_specs, start=1):
        spec = _closed(
            raw,
            {
                "source_ref", "source_hash", "connector_profile_ref",
                "connector_profile_hash", "operation", "parameters",
                "input_schema_ref", "input_schema_hash", "output_schema_ref",
                "output_schema_hash", "completeness_required", "depends_on",
                "fallback_step_refs", "max_attempts",
            },
            f"step_specs[{ordinal}]",
        )
        step_identity = {
            "task_ref": identity["task_ref"],
            "ordinal": ordinal,
            "source_ref": spec["source_ref"],
            "operation": spec["operation"],
        }
        base = {
            "id": "compiled-connector-step:" + content_hash(step_identity),
            "ordinal": ordinal,
            **spec,
            "query_hash": content_hash(
                {"operation": spec["operation"], "parameters": spec["parameters"]}
            ),
        }
        base["content_hash"] = content_hash(base)
        steps.append(validate_compiled_connector_step(base))
    plan_identity = {**identity, "steps": [item["content_hash"] for item in steps]}
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "compiled-connector-plan:" + content_hash(plan_identity),
        "created_at": created,
        **identity,
        "steps": steps,
    }
    base["content_hash"] = content_hash(base)
    return validate_compiled_connector_plan(base)


def build_reference_fixture_plan(
    *,
    task_ref: str,
    task_hash: str,
    created_at: str,
    max_attempts: int = 2,
) -> dict[str, Any]:
    inventory = load_packaged_connector_inventory()
    fixtures = {
        "cninfo": load_recorded_source_fixture("cninfo"),
        "sec": load_recorded_source_fixture("sec"),
        "alphaengine": load_recorded_alphaengine_fixture(),
    }
    operations = {
        "cninfo": "list_announcements",
        "sec": "list_filings",
        "alphaengine": "search_library",
    }
    specs: list[dict[str, Any]] = []
    for slug in ("cninfo", "sec", "alphaengine"):
        template = inventory["templates"][slug]
        operation = next(
            item for item in template["operations"]
            if item["operation"] == operations[slug]
        )
        completeness = operation["completeness_ceiling"]
        specs.append(
            {
                "source_ref": template["source_identity"]["source_ref"],
                "source_hash": content_hash(template["source_identity"]),
                "connector_profile_ref": template["id"],
                "connector_profile_hash": template["content_hash"],
                "operation": operation["operation"],
                "parameters": fixtures[slug]["parent_parameters"],
                "input_schema_ref": operation["input_schema_ref"],
                "input_schema_hash": operation["input_schema_hash"],
                "output_schema_ref": operation["output_schema_ref"],
                "output_schema_hash": operation["output_schema_hash"],
                "completeness_required": completeness,
                "depends_on": [],
                "fallback_step_refs": [],
                "max_attempts": max_attempts,
            }
        )
    planner_ref = "planner:fixture-research-coordinator:0.1"
    policy_ref = "connector-routing-policy:fixture-reference-sources:0.1"
    return build_compiled_connector_plan(
        task_ref=task_ref,
        task_hash=task_hash,
        planner_ref=planner_ref,
        planner_hash=content_hash({"planner_ref": planner_ref}),
        routing_policy_ref=policy_ref,
        routing_policy_hash=content_hash({"routing_policy_ref": policy_ref}),
        step_specs=specs,
        created_at=created_at,
    )


AGENDA_BINDER_REF = "agenda-context-binder:cycle-mandate-perception:0.1"
AGENDA_BINDER_HASH = content_hash({"binder_ref": AGENDA_BINDER_REF})
_AGENDA_BINDING_FIELDS = {
    "schema_version", "id", "created_at", "task_ref", "task_hash", "cycle_ref",
    "cycle_hash", "company_ref", "policy_version_ref", "policy_version_hash",
    "mandate_version_ref", "mandate_version_hash", "perception_snapshot_ref",
    "perception_snapshot_hash", "binder_ref", "binder_hash", "content_hash",
}
AGENDA_BINDING_FIELDS = frozenset(_AGENDA_BINDING_FIELDS)


def validate_agenda_context_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the Agenda plan binding accepted by the ContextMaterializer.

    An Agenda cycle has no connector plan: it reads no external source and
    compiles no step.  Rather than forge a ``CompiledConnectorPlan`` whose
    connector semantics would be a lie, an Agenda ContextPack binds to this
    closed record.  Every field is an exact Core ref/hash, so the binding is
    reconstructible from the AgendaCycle, its policy version, its mandate
    version, and its perception snapshot -- and from nothing else.
    """

    wire = _closed(value, _AGENDA_BINDING_FIELDS, "AgendaContextBinding")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchContextError("unsupported AgendaContextBinding schema_version")
    for field in (
        "id", "task_ref", "cycle_ref", "company_ref", "policy_version_ref",
        "mandate_version_ref", "perception_snapshot_ref", "binder_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "task_hash", "cycle_hash", "policy_version_hash", "mandate_version_hash",
        "perception_snapshot_hash", "binder_hash",
    ):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["binder_ref"] != AGENDA_BINDER_REF or wire["binder_hash"] != AGENDA_BINDER_HASH:
        raise ResearchContextConflict("AgendaContextBinding binder drifted")
    if wire["task_ref"] != wire["cycle_ref"] or wire["task_hash"] != wire["cycle_hash"]:
        raise ResearchContextConflict("AgendaContextBinding task must be its cycle")
    if wire["id"] != "agenda-context-binding:" + content_hash({
        key: item for key, item in wire.items()
        if key not in {"id", "content_hash", "created_at"}
    }):
        raise ResearchContextConflict("AgendaContextBinding id binding mismatch")
    _reject_sensitive(wire, "AgendaContextBinding")
    return _with_hash(wire, "AgendaContextBinding")


def build_agenda_context_binding(
    *,
    cycle_ref: str,
    cycle_hash: str,
    company_ref: str,
    policy_version_ref: str,
    policy_version_hash: str,
    mandate_version_ref: str,
    mandate_version_hash: str,
    perception_snapshot_ref: str,
    perception_snapshot_hash: str,
    created_at: str,
) -> dict[str, Any]:
    """Build an AgendaContextBinding from exact refs and hashes only."""

    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "pending",
        "created_at": _timestamp(created_at, "created_at"),
        "task_ref": _text(cycle_ref, "cycle_ref"),
        "task_hash": _hash(cycle_hash, "cycle_hash"),
        "cycle_ref": _text(cycle_ref, "cycle_ref"),
        "cycle_hash": _hash(cycle_hash, "cycle_hash"),
        "company_ref": _text(company_ref, "company_ref"),
        "policy_version_ref": _text(policy_version_ref, "policy_version_ref"),
        "policy_version_hash": _hash(policy_version_hash, "policy_version_hash"),
        "mandate_version_ref": _text(mandate_version_ref, "mandate_version_ref"),
        "mandate_version_hash": _hash(mandate_version_hash, "mandate_version_hash"),
        "perception_snapshot_ref": _text(
            perception_snapshot_ref, "perception_snapshot_ref"
        ),
        "perception_snapshot_hash": _hash(
            perception_snapshot_hash, "perception_snapshot_hash"
        ),
        "binder_ref": AGENDA_BINDER_REF,
        "binder_hash": AGENDA_BINDER_HASH,
    }
    base["id"] = "agenda-context-binding:" + content_hash({
        key: item for key, item in base.items()
        if key not in {"id", "content_hash", "created_at"}
    })
    base["content_hash"] = content_hash(base)
    return validate_agenda_context_binding(base)


_CLAIM_ENTRY_FIELDS = {
    "claim_version_ref", "claim_version_hash", "claim_ref", "subject_ref",
    "metric_or_aspect", "period", "basis", "status", "source_types",
    "updated_at", "search_terms", "content_hash",
}


def _validate_claim_index_entry(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = _closed(value, _CLAIM_ENTRY_FIELDS, name)
    for field in (
        "claim_version_ref", "claim_ref", "subject_ref", "metric_or_aspect",
        "period", "basis",
    ):
        wire[field] = _text(wire[field], f"{name}.{field}")
    wire["claim_version_hash"] = _hash(
        wire["claim_version_hash"], f"{name}.claim_version_hash"
    )
    if wire["status"] not in {
        "proposed", "corroborated", "contested", "superseded", "retracted",
    }:
        raise ResearchContextError(f"{name}.status is invalid")
    wire["source_types"] = sorted(_refs(wire["source_types"], f"{name}.source_types"))
    wire["search_terms"] = sorted(_refs(wire["search_terms"], f"{name}.search_terms"))
    wire["updated_at"] = _timestamp(wire["updated_at"], f"{name}.updated_at")
    return _with_hash(wire, name)


_CLAIM_INDEX_FIELDS = {
    "schema_version", "id", "created_at", "ledger_snapshot_ref",
    "ledger_snapshot_hash", "index_builder_ref", "index_builder_hash",
    "entries", "content_hash",
}

_CLAIM_INDEX_SNAPSHOT_FIELDS = {
    "schema_version", "id", "created_at", "claim_versions",
    "latest_claim_version_refs", "evidence_relations", "claim_challenges",
    "latest_adjudications", "content_hash",
}
_CLAIM_INDEX_SNAPSHOT_CLAIM_FIELDS = {
    "claim_version_id", "claim_ref", "version", "content_hash", "created_at",
    "claim",
}
_CLAIM_INDEX_SNAPSHOT_ADJUDICATION_FIELDS = {
    "adjudication_version_id", "claim_ref", "claim_version_ref", "version",
    "adjudication", "content_hash", "created_at",
}
_CLAIM_INDEX_SNAPSHOT_CHALLENGE_FIELDS = {
    "challenge_id", "conflict_key", "claim_version_id",
    "conflicting_claim_version_id", "reason", "semantic_key", "values",
}


def _validate_claim_index_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the status-free snapshot emitted by ``DaltonStore``.

    A snapshot is the only admissible source for ClaimIndex status.  In
    particular, a caller cannot smuggle a status field into a claim bundle or
    replace a snapshot ref/hash while retaining the same claim bytes.
    """
    wire = _closed(value, _CLAIM_INDEX_SNAPSHOT_FIELDS, "ClaimIndexLedgerSnapshot")
    if wire["schema_version"] != "0.1":
        raise ResearchContextError("unsupported ClaimIndexLedgerSnapshot schema_version")
    for field in ("id",):
        wire[field] = _text(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if not isinstance(wire["claim_versions"], list):
        raise ResearchContextError("ClaimIndexLedgerSnapshot.claim_versions must be an array")
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(wire["claim_versions"]):
        item = _closed(raw, _CLAIM_INDEX_SNAPSHOT_CLAIM_FIELDS, f"claim_versions[{index}]")
        item["claim_version_id"] = _text(item["claim_version_id"], f"claim_versions[{index}].claim_version_id")
        item["claim_ref"] = _text(item["claim_ref"], f"claim_versions[{index}].claim_ref")
        item["content_hash"] = _hash(item["content_hash"], f"claim_versions[{index}].content_hash")
        item["created_at"] = _timestamp(item["created_at"], f"claim_versions[{index}].created_at")
        item["version"] = _integer(item["version"], f"claim_versions[{index}].version", minimum=1)
        if item["claim_version_id"] in seen:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot claim version ids must be unique")
        seen.add(item["claim_version_id"])
        claim_wire = item["claim"]
        if not isinstance(claim_wire, Mapping):
            raise ResearchContextError(f"claim_versions[{index}].claim must be an object")
        schema_version = claim_wire.get("schema_version")
        try:
            if schema_version == "0.1":
                claim = ClaimVersion.from_dict(claim_wire).to_dict()
            elif schema_version == "0.2":
                # Import lazily: research_review imports research_verification,
                # which intentionally imports this module for its context types.
                from .research_review import validate_claim_version_v0_2

                claim = validate_claim_version_v0_2(claim_wire)
            else:
                raise ResearchContextError("unsupported ClaimVersion schema_version")
        except ResearchContextError:
            raise
        except Exception as exc:
            raise ResearchContextError("ClaimIndexLedgerSnapshot contains an invalid ClaimVersion") from exc
        if claim["id"] != item["claim_version_id"] or claim["claim_ref"] != item["claim_ref"]:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot claim identity mismatch")
        if claim["content_hash"] != content_hash({
            key: value for key, value in claim.items() if key != "content_hash"
        }):
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot ClaimVersion content_hash mismatch")
        if claim["version"] != item["version"] or claim["content_hash"] != item["content_hash"]:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot claim binding mismatch")
        if _timestamp(
            claim["created_at"], f"claim_versions[{index}].claim.created_at"
        ) != item["created_at"]:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot claim timestamp mismatch")
        claims.append({**item, "claim": claim})
    latest = wire["latest_claim_version_refs"]
    if not isinstance(latest, Mapping) or any(
        not isinstance(key, str) or not isinstance(ref, str) for key, ref in latest.items()
    ):
        raise ResearchContextError("ClaimIndexLedgerSnapshot.latest_claim_version_refs must be a string map")
    expected_latest: dict[str, dict[str, Any]] = {}
    for item in claims:
        current = expected_latest.get(item["claim_ref"])
        if current is None or (item["version"], item["claim_version_id"]) > (
            current["version"], current["claim_version_id"]
        ):
            expected_latest[item["claim_ref"]] = item
    if dict(latest) != {
        claim_ref: item["claim_version_id"] for claim_ref, item in expected_latest.items()
    }:
        raise ResearchContextConflict("ClaimIndexLedgerSnapshot latest claim projection mismatch")
    wire["claim_versions"] = sorted(claims, key=lambda item: item["claim_version_id"])
    wire["latest_claim_version_refs"] = dict(sorted(latest.items()))
    claims_by_id = {item["claim_version_id"]: item for item in claims}
    for field in ("evidence_relations", "claim_challenges", "latest_adjudications"):
        if not isinstance(wire[field], list):
            raise ResearchContextError(f"ClaimIndexLedgerSnapshot.{field} must be an array")
    relations: list[dict[str, Any]] = []
    for index, relation_raw in enumerate(wire["evidence_relations"]):
        try:
            relation = EvidenceRelation.from_dict(relation_raw).to_dict()
        except Exception as exc:
            raise ResearchContextError(
                f"ClaimIndexLedgerSnapshot.evidence_relations[{index}] is invalid"
            ) from exc
        if relation["claim_version_ref"] not in seen:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot relation points to unknown claim")
        if relation["claim_ref"] != claims_by_id[relation["claim_version_ref"]]["claim_ref"]:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot relation claim_ref mismatch")
        relations.append(relation)
    wire["evidence_relations"] = sorted(relations, key=lambda item: item["id"])
    wire["evidence_relations"] = [
        _with_hash(item, f"evidence_relations[{index}]")
        for index, item in enumerate(wire["evidence_relations"])
    ]
    challenges: list[dict[str, Any]] = []
    challenge_ids: set[str] = set()
    conflict_keys: set[str] = set()
    for index, raw in enumerate(wire["claim_challenges"]):
        item = _closed(
            raw, _CLAIM_INDEX_SNAPSHOT_CHALLENGE_FIELDS,
            f"claim_challenges[{index}]",
        )
        for field in (
            "challenge_id", "conflict_key", "claim_version_id",
            "conflicting_claim_version_id", "reason",
        ):
            item[field] = _text(item[field], f"claim_challenges[{index}].{field}")
        if item["challenge_id"] in challenge_ids or item["conflict_key"] in conflict_keys:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot challenges must be unique")
        challenge_ids.add(item["challenge_id"])
        conflict_keys.add(item["conflict_key"])
        left_ref = item["claim_version_id"]
        right_ref = item["conflicting_claim_version_id"]
        if left_ref not in claims_by_id or right_ref not in claims_by_id:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot challenge points to unknown claim")
        if left_ref == right_ref:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot challenge must bind two claims")
        expected_key = "numeric:" + ":".join(sorted((left_ref, right_ref)))
        if item["conflict_key"] != expected_key or item["reason"] != "exact numeric claims conflict":
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot challenge identity mismatch")
        left = claims_by_id[left_ref]["claim"]
        right = claims_by_id[right_ref]["claim"]
        expected_semantic_key = list(DaltonStore._claim_semantic_key(left))
        if (
            expected_semantic_key != list(DaltonStore._claim_semantic_key(right))
            or item["semantic_key"] != expected_semantic_key
        ):
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot challenge semantic key mismatch")
        if not isinstance(item["values"], list) or len(item["values"]) != 2:
            raise ResearchContextError("ClaimIndexLedgerSnapshot challenge values must contain two items")
        if not (
            DaltonStore._claim_values_equal(item["values"][0], left.get("value"))
            and DaltonStore._claim_values_equal(item["values"][1], right.get("value"))
        ):
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot challenge values mismatch")
        challenges.append(item)
    wire["claim_challenges"] = sorted(
        challenges, key=lambda item: (item["conflict_key"], item["challenge_id"])
    )
    normalized_adjudications: list[dict[str, Any]] = []
    adjudicated_claim_refs: set[str] = set()
    for index, raw in enumerate(wire["latest_adjudications"]):
        item = _closed(
            raw, _CLAIM_INDEX_SNAPSHOT_ADJUDICATION_FIELDS,
            f"latest_adjudications[{index}]",
        )
        item["adjudication_version_id"] = _text(item["adjudication_version_id"], f"latest_adjudications[{index}].adjudication_version_id")
        item["claim_ref"] = _text(item["claim_ref"], f"latest_adjudications[{index}].claim_ref")
        item["claim_version_ref"] = _text(item["claim_version_ref"], f"latest_adjudications[{index}].claim_version_ref")
        item["version"] = _integer(item["version"], f"latest_adjudications[{index}].version", minimum=1)
        item["content_hash"] = _hash(item["content_hash"], f"latest_adjudications[{index}].content_hash")
        item["created_at"] = _timestamp(item["created_at"], f"latest_adjudications[{index}].created_at")
        if item["claim_version_ref"] not in seen:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot adjudication points to unknown claim")
        if item["claim_ref"] != claims_by_id[item["claim_version_ref"]]["claim_ref"]:
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot adjudication claim_ref mismatch")
        if item["claim_version_ref"] in adjudicated_claim_refs:
            raise ResearchContextConflict(
                "ClaimIndexLedgerSnapshot has multiple latest adjudications for one claim version"
            )
        adjudicated_claim_refs.add(item["claim_version_ref"])
        try:
            adjudication = AdjudicationVersion.from_dict(item["adjudication"]).to_dict()
        except Exception as exc:
            raise ResearchContextError(
                "latest adjudication must be a valid AdjudicationVersion"
            ) from exc
        if adjudication["content_hash"] != content_hash({
            key: value for key, value in adjudication.items() if key != "content_hash"
        }):
            raise ResearchContextConflict(
                "ClaimIndexLedgerSnapshot AdjudicationVersion content_hash mismatch"
            )
        if (
            adjudication["id"] != item["adjudication_version_id"]
            or adjudication["claim_ref"] != item["claim_ref"]
            or adjudication["claim_version_ref"] != item["claim_version_ref"]
            or adjudication["version"] != item["version"]
            or adjudication["content_hash"] != item["content_hash"]
        ):
            raise ResearchContextConflict("ClaimIndexLedgerSnapshot adjudication identity mismatch")
        if _timestamp(
            adjudication["created_at"],
            f"latest_adjudications[{index}].adjudication.created_at",
        ) != item["created_at"]:
            raise ResearchContextConflict(
                "ClaimIndexLedgerSnapshot adjudication timestamp mismatch"
            )
        item["adjudication"] = adjudication
        normalized_adjudications.append(item)
    wire["latest_adjudications"] = sorted(
        normalized_adjudications, key=lambda item: item["claim_version_ref"]
    )
    expected_snapshot_ref = "ledger-snapshot:claim-index:" + content_hash({
        key: value for key, value in wire.items() if key not in {"id", "content_hash"}
    })
    if wire["id"] != expected_snapshot_ref:
        raise ResearchContextConflict("ClaimIndexLedgerSnapshot ref is not Core-derived")
    return _with_hash(wire, "ClaimIndexLedgerSnapshot")


def validate_claim_index(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _CLAIM_INDEX_FIELDS, "ClaimIndex")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchContextError("unsupported ClaimIndex schema_version")
    for field in ("id", "ledger_snapshot_ref", "index_builder_ref"):
        wire[field] = _text(wire[field], field)
    for field in ("ledger_snapshot_hash", "index_builder_hash"):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if not isinstance(wire["entries"], list):
        raise ResearchContextError("ClaimIndex.entries must be an array")
    entries = [
        _validate_claim_index_entry(item, f"entries[{index}]")
        for index, item in enumerate(wire["entries"])
    ]
    entries.sort(key=lambda item: item["claim_version_ref"])
    refs = [item["claim_version_ref"] for item in entries]
    if len(refs) != len(set(refs)):
        raise ResearchContextError("ClaimIndex claim_version_ref values must be unique")
    wire["entries"] = entries
    return _with_hash(wire, "ClaimIndex")


def build_claim_index(
    claim_bundles: Sequence[Mapping[str, Any]] | None = None,
    *,
    ledger: DaltonStore | None = None,
    claim_version_refs: Sequence[str] | None = None,
    ledger_snapshot_ref: str | None = None,
    ledger_snapshot_hash: str | None = None,
    created_at: str,
) -> dict[str, Any]:
    """Build a ClaimIndex from an exact Core Ledger snapshot.

    Caller bundles are intentionally no longer accepted.  The old API let a
    caller provide both a claim and an arbitrary ``status`` string; that made
    it possible to label a contested claim ``corroborated``.  A ``DaltonStore``
    now owns snapshot creation and its projection is the only source of status,
    including for an empty fixture Ledger.
    """
    created_at_wire = _timestamp(created_at, "created_at")
    if claim_bundles is not None:
        raise ResearchContextError(
            "ClaimIndex requires a DaltonStore snapshot; caller claim bundles/status are not accepted"
        )
    if ledger_snapshot_ref is not None or ledger_snapshot_hash is not None:
        raise ResearchContextError("ledger_snapshot_ref/hash are always Core-derived")
    if not isinstance(ledger, DaltonStore):
        raise ResearchContextError("ledger must be a DaltonStore")

    snapshot = _validate_claim_index_snapshot(
        ledger.claim_index_snapshot(created_at=created_at_wire)
    )
    index_ref = snapshot["id"]
    index_hash = snapshot["content_hash"]
    requested_refs = (
        sorted(_refs(claim_version_refs, "claim_version_refs"))
        if claim_version_refs is not None else
        sorted(snapshot["latest_claim_version_refs"].values())
    )
    by_ref = {item["claim_version_id"]: item for item in snapshot["claim_versions"]}
    unknown = set(requested_refs) - set(by_ref)
    if unknown:
        raise ResearchContextConflict(
            f"claim_version_refs point to unknown Ledger claims: {sorted(unknown)}"
        )
    relation_by_claim: dict[str, list[dict[str, Any]]] = {}
    for relation in snapshot["evidence_relations"]:
        relation_by_claim.setdefault(relation["claim_version_ref"], []).append(relation)
    entries: list[dict[str, Any]] = []
    for index, claim_ref in enumerate(requested_refs):
        item = by_ref[claim_ref]
        claim = item["claim"]
        source_types = sorted({
            source
            for relation in relation_by_claim.get(claim_ref, [])
            for source in relation["source_lineage"]
        })
        status_projection = DaltonStore.project_claim_status_details(
            snapshot, claim_ref
        )
        status = status_projection["status"]
        # ClaimIndex 0.1 keeps a string period.  Structured periods from
        # Ledger 0.2 are projected losslessly as canonical JSON text.
        period = (
            claim["period"]
            if isinstance(claim["period"], str)
            else canonical_json(claim["period"])
        )
        terms = sorted(set(
            token.lower()
            for token in _SEARCH_RE.findall(
                " ".join(filter(None, (
                    claim["subject_ref"], claim["metric_or_aspect"], period,
                    claim["basis"], claim["normalized_statement"], claim.get("unit"),
                )))
            )
        ))
        base = {
            "claim_version_ref": claim["id"],
            "claim_version_hash": claim["content_hash"],
            "claim_ref": claim["claim_ref"],
            "subject_ref": claim["subject_ref"],
            "metric_or_aspect": claim["metric_or_aspect"],
            "period": period,
            "basis": claim["basis"],
            "status": status,
            "source_types": source_types,
            "updated_at": max(
                [status_projection["updated_at"]]
                + [
                    _timestamp(relation["created_at"], "EvidenceRelation.created_at")
                    for relation in relation_by_claim.get(claim_ref, [])
                ]
            ),
            "search_terms": terms,
        }
        base["content_hash"] = content_hash(base)
        entries.append(_validate_claim_index_entry(base, f"entries[{index}]"))
    builder_ref = "claim-index-builder:structured-sqlite:0.2"
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "claim-index:" + content_hash({
            "ledger_snapshot_ref": index_ref,
            "ledger_snapshot_hash": index_hash,
            "entries": [x["content_hash"] for x in entries],
        }),
        "created_at": created_at_wire,
        "ledger_snapshot_ref": index_ref,
        "ledger_snapshot_hash": index_hash,
        "index_builder_ref": builder_ref,
        "index_builder_hash": content_hash({"index_builder_ref": builder_ref}),
        "entries": sorted(entries, key=lambda item: item["claim_version_ref"]),
    }
    base["content_hash"] = content_hash(base)
    return validate_claim_index(base)


_CONTEXT_INPUT_FIELDS = {
    "kind", "ref", "hash", "priority", "selected", "selection_reason",
    "original_tokens", "original_bytes", "included_tokens", "included_bytes",
    "content_hash",
}


def _validate_context_input(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = _closed(value, _CONTEXT_INPUT_FIELDS, name)
    if wire["kind"] not in {"mandate", "claim", "perception", "artifact", "source"}:
        raise ResearchContextError(f"{name}.kind is invalid")
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    wire["hash"] = _hash(wire["hash"], f"{name}.hash")
    wire["priority"] = _integer(wire["priority"], f"{name}.priority")
    if not isinstance(wire["selected"], bool):
        raise ResearchContextError(f"{name}.selected must be boolean")
    if wire["selection_reason"] not in {
        "included", "budget_tokens", "budget_bytes", "duplicate",
    }:
        raise ResearchContextError(f"{name}.selection_reason is invalid")
    for field in (
        "original_tokens", "original_bytes", "included_tokens", "included_bytes",
    ):
        wire[field] = _integer(wire[field], f"{name}.{field}")
    if wire["selected"]:
        if wire["selection_reason"] != "included" or (
            wire["included_tokens"] != wire["original_tokens"]
            or wire["included_bytes"] != wire["original_bytes"]
        ):
            raise ResearchContextError(f"{name} selected accounting is inconsistent")
    elif (
        wire["selection_reason"] == "included"
        or wire["included_tokens"] != 0
        or wire["included_bytes"] != 0
    ):
        raise ResearchContextError(f"{name} omitted accounting is inconsistent")
    return _with_hash(wire, name)


_CONTEXT_PACK_FIELDS = {
    "schema_version", "id", "created_at", "task_ref", "task_hash",
    "compiled_plan_ref", "compiled_plan_hash", "claim_index_ref",
    "claim_index_hash", "builder_ref", "builder_hash", "selection_policy_ref",
    "selection_policy_hash", "tokenizer_ref", "tokenizer_hash",
    "truncation_ref", "truncation_hash", "budget", "inputs", "totals",
    "content_hash",
}


def validate_context_pack(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _CONTEXT_PACK_FIELDS, "ContextPack")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ResearchContextError("unsupported ContextPack schema_version")
    for field in (
        "id", "task_ref", "compiled_plan_ref", "claim_index_ref", "builder_ref",
        "selection_policy_ref", "tokenizer_ref", "truncation_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "task_hash", "compiled_plan_hash", "claim_index_hash", "builder_hash",
        "selection_policy_hash", "tokenizer_hash", "truncation_hash",
    ):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    budget = _closed(wire["budget"], {"max_tokens", "max_bytes"}, "budget")
    budget["max_tokens"] = _integer(budget["max_tokens"], "budget.max_tokens", minimum=1)
    budget["max_bytes"] = _integer(budget["max_bytes"], "budget.max_bytes", minimum=1)
    wire["budget"] = budget
    if not isinstance(wire["inputs"], list):
        raise ResearchContextError("ContextPack.inputs must be an array")
    inputs = [
        _validate_context_input(item, f"inputs[{index}]")
        for index, item in enumerate(wire["inputs"])
    ]
    expected_order = sorted(
        inputs, key=lambda item: (-item["priority"], item["kind"], item["ref"])
    )
    if inputs != expected_order:
        raise ResearchContextConflict("ContextPack inputs are not canonically ordered")
    selected_tokens = 0
    selected_bytes = 0
    seen: dict[str, str] = {}
    for item in inputs:
        if item["ref"] in seen and seen[item["ref"]] != item["hash"]:
            raise ResearchContextConflict(
                "ContextPack duplicate ref carries conflicting content hash"
            )
        if item["ref"] in seen:
            expected_selected = False
            expected_reason = "duplicate"
        elif selected_tokens + item["original_tokens"] > budget["max_tokens"]:
            expected_selected = False
            expected_reason = "budget_tokens"
        elif selected_bytes + item["original_bytes"] > budget["max_bytes"]:
            expected_selected = False
            expected_reason = "budget_bytes"
        else:
            expected_selected = True
            expected_reason = "included"
            selected_tokens += item["original_tokens"]
            selected_bytes += item["original_bytes"]
        if (
            item["selected"] != expected_selected
            or item["selection_reason"] != expected_reason
        ):
            raise ResearchContextConflict(
                "ContextPack selection does not match the frozen policy"
            )
        seen[item["ref"]] = item["hash"]
    totals = _closed(
        wire["totals"], {"selected_tokens", "selected_bytes", "omitted_count"}, "totals"
    )
    for field in totals:
        totals[field] = _integer(totals[field], f"totals.{field}")
    expected_totals = {
        "selected_tokens": sum(item["included_tokens"] for item in inputs),
        "selected_bytes": sum(item["included_bytes"] for item in inputs),
        "omitted_count": sum(not item["selected"] for item in inputs),
    }
    if totals != expected_totals:
        raise ResearchContextConflict("ContextPack totals mismatch")
    if totals["selected_tokens"] > budget["max_tokens"] or totals["selected_bytes"] > budget["max_bytes"]:
        raise ResearchContextConflict("ContextPack exceeds its frozen budget")
    wire["inputs"] = inputs
    wire["totals"] = totals
    _reject_sensitive(wire, "ContextPack")
    return _with_hash(wire, "ContextPack")


def build_context_pack(
    input_specs: Sequence[Mapping[str, Any]],
    *,
    task_ref: str,
    task_hash: str,
    compiled_plan_ref: str,
    compiled_plan_hash: str,
    claim_index_ref: str,
    claim_index_hash: str,
    created_at: str,
    max_tokens: int,
    max_bytes: int,
) -> dict[str, Any]:
    max_token_count = _integer(max_tokens, "max_tokens", minimum=1)
    max_byte_count = _integer(max_bytes, "max_bytes", minimum=1)
    specs: list[dict[str, Any]] = []
    for index, raw in enumerate(input_specs):
        spec = _closed(raw, {"kind", "ref", "hash", "priority", "content"}, f"input_specs[{index}]")
        if spec["kind"] not in {"mandate", "claim", "perception", "artifact", "source"}:
            raise ResearchContextError(f"input_specs[{index}].kind is invalid")
        spec["ref"] = _text(spec["ref"], f"input_specs[{index}].ref")
        spec["hash"] = _hash(spec["hash"], f"input_specs[{index}].hash")
        spec["priority"] = _integer(
            spec["priority"], f"input_specs[{index}].priority"
        )
        content = _text(spec["content"], f"input_specs[{index}].content")
        spec["tokens"] = count_dalton_search_tokens(content)
        spec["bytes"] = len(content.encode("utf-8"))
        specs.append(spec)
    specs.sort(key=lambda item: (-item["priority"], item["kind"], item["ref"]))
    selected_tokens = 0
    selected_bytes = 0
    seen: dict[str, str] = {}
    inputs: list[dict[str, Any]] = []
    for spec in specs:
        ref = _text(spec["ref"], "input.ref")
        tokens = _integer(spec["tokens"], "input.tokens")
        byte_count = _integer(spec["bytes"], "input.bytes")
        if ref in seen and seen[ref] != spec["hash"]:
            raise ResearchContextConflict(
                "ContextPack duplicate ref carries conflicting content hash"
            )
        if ref in seen:
            selected = False
            reason = "duplicate"
        elif selected_tokens + tokens > max_token_count:
            selected = False
            reason = "budget_tokens"
        elif selected_bytes + byte_count > max_byte_count:
            selected = False
            reason = "budget_bytes"
        else:
            selected = True
            reason = "included"
            selected_tokens += tokens
            selected_bytes += byte_count
        seen[ref] = spec["hash"]
        base = {
            "kind": spec["kind"],
            "ref": ref,
            "hash": spec["hash"],
            "priority": spec["priority"],
            "selected": selected,
            "selection_reason": reason,
            "original_tokens": tokens,
            "original_bytes": byte_count,
            "included_tokens": tokens if selected else 0,
            "included_bytes": byte_count if selected else 0,
        }
        base["content_hash"] = content_hash(base)
        inputs.append(_validate_context_input(base, "ContextPack.input"))
    builder_ref = "context-pack-builder:deterministic-ref-only:0.1"
    selector_ref = "context-selector:priority-kind-ref:0.1"
    tokenizer_ref = "tokenizer:dalton-search-token:0.1"
    truncation_ref = "truncation:whole-input-drop:0.1"
    identity = {
        "task_ref": task_ref,
        "compiled_plan_ref": compiled_plan_ref,
        "claim_index_ref": claim_index_ref,
        "inputs": [item["content_hash"] for item in inputs],
    }
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": "context-pack:" + content_hash(identity),
        "created_at": _timestamp(created_at, "created_at"),
        "task_ref": _text(task_ref, "task_ref"),
        "task_hash": _hash(task_hash, "task_hash"),
        "compiled_plan_ref": _text(compiled_plan_ref, "compiled_plan_ref"),
        "compiled_plan_hash": _hash(compiled_plan_hash, "compiled_plan_hash"),
        "claim_index_ref": _text(claim_index_ref, "claim_index_ref"),
        "claim_index_hash": _hash(claim_index_hash, "claim_index_hash"),
        "builder_ref": builder_ref,
        "builder_hash": content_hash({"builder_ref": builder_ref}),
        "selection_policy_ref": selector_ref,
        "selection_policy_hash": content_hash({"selection_policy_ref": selector_ref}),
        "tokenizer_ref": tokenizer_ref,
        "tokenizer_hash": content_hash({"tokenizer_ref": tokenizer_ref}),
        "truncation_ref": truncation_ref,
        "truncation_hash": content_hash({"truncation_ref": truncation_ref}),
        "budget": {
            "max_tokens": max_token_count,
            "max_bytes": max_byte_count,
        },
        "inputs": inputs,
        "totals": {
            "selected_tokens": selected_tokens,
            "selected_bytes": selected_bytes,
            "omitted_count": sum(not item["selected"] for item in inputs),
        },
    }
    base["content_hash"] = content_hash(base)
    return validate_context_pack(base)


def validate_runner_request_plan_binding(
    runner_request: Mapping[str, Any],
    plan: Mapping[str, Any],
    step: Mapping[str, Any],
) -> dict[str, Any]:
    request = validate_connector_runner_request(runner_request)
    plan_wire = validate_compiled_connector_plan(plan)
    step_wire = validate_compiled_connector_step(step)
    actual = (
        request.get("compiled_connector_plan_ref"),
        request.get("compiled_connector_plan_hash"),
        request.get("compiled_step_ref"),
        request.get("compiled_step_hash"),
        request["work_order_ref"],
        request["work_order_hash"],
        request["connector_profile_ref"],
        request["connector_profile_hash"],
    )
    expected = (
        plan_wire["id"], plan_wire["content_hash"], step_wire["id"],
        step_wire["content_hash"], plan_wire["task_ref"], plan_wire["task_hash"],
        step_wire["connector_profile_ref"], step_wire["connector_profile_hash"],
    )
    if actual != expected:
        raise ResearchContextConflict("RunnerRequest does not bind the exact compiled plan step")
    expected_call_spec_hash = content_hash(
        {
            "step_ref": step_wire["id"],
            "operation": step_wire["operation"],
            "parameters": step_wire["parameters"],
        }
    )
    if request["call_spec_hash"] != expected_call_spec_hash:
        raise ResearchContextConflict("RunnerRequest call spec does not bind plan parameters")
    return request


def build_fixture_runner_request(
    plan: Mapping[str, Any],
    step: Mapping[str, Any],
    *,
    attempt_number: int,
    created_at: str,
) -> dict[str, Any]:
    plan_wire = validate_compiled_connector_plan(plan)
    step_wire = validate_compiled_connector_step(step)
    attempt = _integer(attempt_number, "attempt_number", minimum=1)
    schema_version = "0.2" if step_wire["source_ref"] == "source:alphaengine" else "0.1"
    call_spec_ref = f"fixture-call-spec:{step_wire['id']}"
    call_spec_hash = content_hash(
        {
            "step_ref": step_wire["id"],
            "operation": step_wire["operation"],
            "parameters": step_wire["parameters"],
        }
    )
    identity = {"step_ref": step_wire["id"], "attempt_number": attempt}
    base = {
        "schema_version": schema_version,
        "id": "connector-runner-request:fixture:" + content_hash(identity),
        "created_at": _timestamp(created_at, "created_at"),
        "connector_invocation_ref": "connector-invocation:fixture:" + content_hash(identity),
        "connector_invocation_hash": content_hash({"connector_invocation": identity}),
        "execution_ref": "execution:fixture:" + content_hash(identity),
        "execution_hash": content_hash({"execution": identity}),
        "work_order_ref": plan_wire["task_ref"],
        "work_order_hash": plan_wire["task_hash"],
        "scheduler_attempt_number": attempt,
        "scheduler_lease_revision_ref": f"work-lease:fixture:{attempt}",
        "scheduler_lease_hash": content_hash({"work_lease": identity}),
        "connector_profile_ref": step_wire["connector_profile_ref"],
        "connector_profile_hash": step_wire["connector_profile_hash"],
        "call_spec_ref": call_spec_ref,
        "call_spec_hash": call_spec_hash,
        "capability_lease_ref": f"capability-lease:fixture:{step_wire['id']}:{attempt}",
        "capability_lease_hash": content_hash({"capability_lease": identity}),
        "principal_ref": "principal:fixture-research-coordinator",
        "runner_runtime_ref": "runtime:fixture-connector-runner:0.1",
        "runner_actor_ref": "runtime:fixture-connector-runner",
        "runner_environment_hash": content_hash({"environment": "fixture-only"}),
        "compiled_connector_plan_ref": plan_wire["id"],
        "compiled_connector_plan_hash": plan_wire["content_hash"],
        "compiled_step_ref": step_wire["id"],
        "compiled_step_hash": step_wire["content_hash"],
        "idempotency_key": f"fixture-runner:{step_wire['id']}:{attempt}",
    }
    if schema_version == "0.2":
        transport = {
            "plan": "recorded-mcp-shadow",
            "step_ref": step_wire["id"],
            "attempt_number": attempt,
        }
        base["transport_plan_ref"] = "recorded-mcp-shadow-plan:fixture:" + content_hash(transport)
        base["transport_plan_hash"] = content_hash(transport)
    base["content_hash"] = content_hash(base)
    request = validate_connector_runner_request(base)
    return validate_runner_request_plan_binding(request, plan_wire, step_wire)


__all__ = [
    "AGENDA_BINDER_HASH", "AGENDA_BINDER_REF", "AGENDA_BINDING_FIELDS",
    "ResearchContextConflict", "ResearchContextError",
    "build_agenda_context_binding", "build_claim_index",
    "build_compiled_connector_plan", "build_context_pack",
    "count_dalton_search_tokens",
    "build_fixture_runner_request", "build_reference_fixture_plan",
    "validate_agenda_context_binding", "validate_claim_index",
    "validate_compiled_connector_plan",
    "validate_compiled_connector_step", "validate_context_pack",
    "validate_runner_request_plan_binding",
]
