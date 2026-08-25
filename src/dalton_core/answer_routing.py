"""Deterministic ad-hoc answer context, routing, and bounded refresh admission.

An exact, already-answered ResearchQuestion may route to ``answer_direct``
when every Core-derived policy gate passes.  S5 additionally allows a stale-
only question to route to ``answer_after_refresh`` when one human-admitted,
single-round Bounded Planner loop and independent day budget are available.
Routing itself remains read-only.  A separate control plane may reserve that
budget and enqueue the existing loop's WorkOrder; it never calls a connector
or writes Evidence, Claim, Thesis, an Agenda item, or an answer artifact.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .agenda import AgendaStore, read_exact_mandate_version
from .bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
    BoundedPlannerNotFound,
)
from .contracts import ThesisVersion, ValidationError as ContractValidationError
from .industry_research import IndustryResearchAuthority
from .research_question_backlog import (
    ResearchQuestionBacklog,
    question_ref_for,
)
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.2"
LEGACY_SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("answer_routing_schema.sql")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SUBJECT_FIELDS = {
    "schema_version", "kind", "ref", "hash", "label", "company_ref",
    "mandate_ref", "mandate_version_ref", "mandate_version_hash",
    "policy_state", "policy_version_ref", "policy_version_hash",
}
_POLICY_FIELDS = {
    "schema_version", "id", "created_at", "policy_ref", "version",
    "prior_version_ref", "mandate_ref", "mandate_version_ref",
    "mandate_version_hash", "thresholds", "refresh_route",
    "adhoc_research_route", "effective_from", "effective_until", "actor_ref",
    "content_hash",
}
_THRESHOLD_FIELDS = {
    "min_driver_coverage_bps", "max_evidence_age_days_by_source_type",
    "allowed_contested_claims", "allowed_open_questions",
    "allowed_unobservable_terminals", "min_formal_claims",
    "min_formal_evidence",
}
_REFRESH_FIELDS = {
    "enabled", "max_cost_units", "probe_template_bindings",
}
_ADHOC_FIELDS = {"enabled", "max_cost_units", "max_rounds"}
ANSWER_REFRESH_OUTPUT_CONTRACT_REF = "schema:bounded-planner-probe-output:0.1"
ANSWER_REFRESH_VERIFIER_REF = "verifier:source-level-coverage:0.1"
_THESIS_CONTENT_FIELDS = (
    "statement", "mechanism", "confidence", "implied_expectation",
    "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason",
)
_ROUTE_REASON_ORDER = (
    "policy_unavailable",
    "policy_stale",
    "question_not_admitted",
    "question_not_answered",
    "answered_claim_superseded",
    "claim_without_support",
    "insufficient_formal_claims",
    "insufficient_formal_evidence",
    "unclassified_source_type",
    "stale_evidence",
    "too_many_contested_claims",
    "too_many_open_questions",
    "too_many_unobservable_terminals",
    "insufficient_driver_coverage",
    "refresh_disabled",
    "refresh_probe_unavailable",
    "refresh_probe_ambiguous",
    "refresh_budget_exhausted",
    "refresh_required",
)


class AnswerRoutingError(Exception):
    pass


class AnswerRoutingValidationError(AnswerRoutingError, ValueError):
    pass


class AnswerRoutingConflict(AnswerRoutingError):
    pass


class AnswerRoutingNotFound(AnswerRoutingError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnswerRoutingValidationError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise AnswerRoutingValidationError(f"{name} is too long")
    return result


def _hash(value: Any, name: str) -> str:
    value = _text(value, name, maximum=64)
    if _SHA256_RE.fullmatch(value) is None:
        raise AnswerRoutingValidationError(f"{name} must be lowercase SHA-256")
    return value


def _human(value: Any) -> str:
    value = _text(value, "actor_ref", maximum=256)
    if _HUMAN_RE.fullmatch(value) is None:
        raise AnswerRoutingValidationError("actor_ref must use the human: namespace")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise AnswerRoutingValidationError(
            f"{name} must be an integer from {minimum} to {maximum}"
        )
    return value


def _time(value: Any, name: str) -> str:
    value = _text(value, name, maximum=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnswerRoutingValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AnswerRoutingValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _datetime(value: Any, name: str) -> datetime:
    return datetime.fromisoformat(_time(value, name))


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AnswerRoutingValidationError(f"{name} must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise AnswerRoutingValidationError(
            f"{name} has invalid closed shape; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return dict(value)


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def _load_record(
    record_json: Any, stored_hash: Any, name: str
) -> dict[str, Any]:
    try:
        wire = json.loads(record_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnswerRoutingConflict(f"stored {name} is invalid JSON") from exc
    if not isinstance(wire, Mapping):
        raise AnswerRoutingConflict(f"stored {name} is not an object")
    result = dict(wire)
    asserted = result.pop("content_hash", None)
    if (
        asserted != stored_hash
        or not isinstance(asserted, str)
        or _SHA256_RE.fullmatch(asserted) is None
        or content_hash(result) != asserted
    ):
        raise AnswerRoutingConflict(f"stored {name} content hash drifted")
    result["content_hash"] = asserted
    return result


def _route_budget(
    value: Any, kind: str, *, schema_version: str = SCHEMA_VERSION
) -> dict[str, Any]:
    fields = _REFRESH_FIELDS if kind == "refresh" else _ADHOC_FIELDS
    wire = _closed(value, fields, f"{kind} route")
    if not isinstance(wire["enabled"], bool):
        raise AnswerRoutingValidationError(f"{kind} route enabled must be boolean")
    wire["max_cost_units"] = _integer(
        wire["max_cost_units"], f"{kind}.max_cost_units", minimum=0, maximum=1_000_000
    )
    if kind == "refresh":
        bindings = wire["probe_template_bindings"]
        if not isinstance(bindings, list):
            raise AnswerRoutingValidationError(
                "refresh probe_template_bindings must be an array"
            )
        normalized = []
        for index, item in enumerate(bindings):
            binding = _closed(item, {"ref", "hash"}, f"probe binding[{index}]")
            normalized.append({
                "ref": _text(binding["ref"], f"probe binding[{index}].ref"),
                "hash": _hash(binding["hash"], f"probe binding[{index}].hash"),
            })
        if len({canonical_json(item) for item in normalized}) != len(normalized):
            raise AnswerRoutingValidationError("refresh probe bindings must be unique")
        wire["probe_template_bindings"] = sorted(
            normalized, key=lambda item: (item["ref"], item["hash"])
        )
    else:
        wire["max_rounds"] = _integer(
            wire["max_rounds"], "adhoc.max_rounds", minimum=0, maximum=100
        )
    if kind == "adhoc":
        if wire["enabled"] or wire["max_cost_units"] != 0 or wire["max_rounds"] != 0:
            raise AnswerRoutingValidationError(
                "ad-hoc research must remain disabled in S5 v0.2"
            )
        return wire
    if schema_version == LEGACY_SCHEMA_VERSION:
        if wire["enabled"] or wire["max_cost_units"] != 0 or normalized:
            raise AnswerRoutingValidationError(
                "refresh route must remain disabled in S4 v0.1"
            )
        return wire
    if wire["enabled"]:
        if wire["max_cost_units"] == 0 or not normalized:
            raise AnswerRoutingValidationError(
                "enabled refresh requires positive day budget and probe bindings"
            )
    elif wire["max_cost_units"] != 0 or normalized:
        raise AnswerRoutingValidationError(
            "disabled refresh cannot retain budget or probe bindings"
        )
    return wire


def _thresholds(value: Any) -> dict[str, Any]:
    wire = _closed(value, _THRESHOLD_FIELDS, "answer thresholds")
    wire["min_driver_coverage_bps"] = _integer(
        wire["min_driver_coverage_bps"],
        "min_driver_coverage_bps",
        minimum=0,
        maximum=10_000,
    )
    for name in (
        "allowed_contested_claims", "allowed_open_questions",
        "allowed_unobservable_terminals", "min_formal_claims",
        "min_formal_evidence",
    ):
        minimum = 1 if name.startswith("min_") else 0
        wire[name] = _integer(wire[name], name, minimum=minimum, maximum=100_000)
    ages = wire["max_evidence_age_days_by_source_type"]
    if not isinstance(ages, Mapping) or not ages:
        raise AnswerRoutingValidationError(
            "max_evidence_age_days_by_source_type must be a non-empty object"
        )
    normalized_ages: dict[str, int] = {}
    for source_type, days in ages.items():
        name = _text(source_type, "source type", maximum=128)
        normalized_ages[name] = _integer(
            days, f"max age for {name}", minimum=0, maximum=36_500
        )
    wire["max_evidence_age_days_by_source_type"] = {
        name: normalized_ages[name] for name in sorted(normalized_ages)
    }
    return wire


def validate_answer_sufficiency_policy(value: Any) -> dict[str, Any]:
    wire = _closed(value, _POLICY_FIELDS, "AnswerSufficiencyPolicyVersion")
    if wire["schema_version"] not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise AnswerRoutingValidationError("answer policy schema version is unsupported")
    schema_version = wire["schema_version"]
    for name in (
        "id", "policy_ref", "mandate_ref", "mandate_version_ref",
    ):
        wire[name] = _text(wire[name], name, maximum=256)
    wire["mandate_version_hash"] = _hash(
        wire["mandate_version_hash"], "mandate_version_hash"
    )
    wire["content_hash"] = _hash(wire["content_hash"], "content_hash")
    wire["created_at"] = _time(wire["created_at"], "created_at")
    wire["effective_from"] = _time(wire["effective_from"], "effective_from")
    if wire["effective_until"] is not None:
        wire["effective_until"] = _time(
            wire["effective_until"], "effective_until"
        )
        if _datetime(wire["effective_until"], "effective_until") <= _datetime(
            wire["effective_from"], "effective_from"
        ):
            raise AnswerRoutingValidationError(
                "effective_until must be after effective_from"
            )
    wire["version"] = _integer(wire["version"], "version", minimum=1, maximum=1_000_000)
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(
            wire["prior_version_ref"], "prior_version_ref", maximum=256
        )
    wire["thresholds"] = _thresholds(wire["thresholds"])
    wire["refresh_route"] = _route_budget(
        wire["refresh_route"], "refresh", schema_version=schema_version
    )
    wire["adhoc_research_route"] = _route_budget(
        wire["adhoc_research_route"], "adhoc", schema_version=schema_version
    )
    wire["actor_ref"] = _human(wire["actor_ref"])
    asserted = wire.pop("content_hash")
    if content_hash(wire) != asserted:
        raise AnswerRoutingConflict("answer policy content hash drifted")
    wire["content_hash"] = asserted
    return wire


class AnswerRoutingAuthority:
    """Human-policy authority plus deterministic, non-writing answer reader."""

    def __init__(
        self,
        store: DaltonStore,
        agenda: AgendaStore,
        backlog: ResearchQuestionBacklog,
        bounded: BoundedPlannerAuthority,
        industry: IndustryResearchAuthority,
    ) -> None:
        if not (
            store.connection
            is agenda.connection
            is backlog.connection
            is bounded.connection
            is industry.connection
        ):
            raise TypeError("answer routing authorities must share one Core connection")
        self.store = store
        self.connection = store.connection
        self.agenda = agenda
        self.backlog = backlog
        self.bounded = bounded
        self.industry = industry
        self._authorized = False
        self.connection.create_function(
            "dalton_answer_routing_authorized",
            0,
            lambda: int(self._authorized),
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        if self._authorized:
            raise AnswerRoutingConflict("answer-routing operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @contextmanager
    def _snapshot(self) -> Iterator[None]:
        if self.connection.in_transaction:
            raise AnswerRoutingConflict("answer routing cannot nest a transaction")
        self.connection.execute("BEGIN")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _validate_refresh_templates(
        self, refresh_route: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        if not refresh_route["enabled"]:
            return []
        templates = []
        for binding in refresh_route["probe_template_bindings"]:
            try:
                template = self.bounded.probe_template(binding["ref"])
            except BoundedPlannerNotFound as exc:
                raise AnswerRoutingConflict(
                    "answer refresh ProbeTemplate is unavailable"
                ) from exc
            if template["content_hash"] != binding["hash"]:
                raise AnswerRoutingConflict(
                    "answer refresh ProbeTemplate hash drifted"
                )
            if (
                template["side_effect_class"] != "read_only"
                or template["output_contract_ref"]
                != ANSWER_REFRESH_OUTPUT_CONTRACT_REF
                or template["verifier_ref"] != ANSWER_REFRESH_VERIFIER_REF
                or template["cost"]["cost_units"]
                > refresh_route["max_cost_units"]
            ):
                raise AnswerRoutingConflict(
                    "answer refresh ProbeTemplate exceeds the closed S5 contract"
                )
            templates.append(template)
        return templates

    def publish_policy(
        self,
        *,
        policy_ref: str,
        mandate_version_ref: str,
        mandate_version_hash: str,
        thresholds: Mapping[str, Any],
        refresh_route: Mapping[str, Any],
        adhoc_research_route: Mapping[str, Any],
        effective_from: str,
        effective_until: str | None,
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        policy_ref = _text(policy_ref, "policy_ref", maximum=256)
        mandate_version_ref = _text(
            mandate_version_ref, "mandate_version_ref", maximum=256
        )
        mandate_version_hash = _hash(
            mandate_version_hash, "mandate_version_hash"
        )
        actor_ref = _human(actor_ref)
        version_id = _text(version_id, "version_id", maximum=256)
        idempotency_key = _text(idempotency_key, "idempotency_key", maximum=256)
        if prior_version_ref is not None:
            prior_version_ref = _text(
                prior_version_ref, "prior_version_ref", maximum=256
            )
        effective_from = _time(effective_from, "effective_from")
        effective_until = (
            None
            if effective_until is None
            else _time(effective_until, "effective_until")
        )
        threshold_wire = _thresholds(thresholds)
        refresh_wire = _route_budget(refresh_route, "refresh")
        adhoc_wire = _route_budget(adhoc_research_route, "adhoc")
        self._validate_refresh_templates(refresh_wire)
        request = {
            "policy_ref": policy_ref,
            "mandate_version_ref": mandate_version_ref,
            "mandate_version_hash": mandate_version_hash,
            "thresholds": threshold_wire,
            "refresh_route": refresh_wire,
            "adhoc_research_route": adhoc_wire,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = content_hash(request)
        created = _now()
        with self._transaction() as cur:
            duplicate = cur.execute(
                "SELECT operation,request_hash,result_json FROM "
                "answer_routing_idempotency WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if duplicate is not None:
                if (
                    duplicate["operation"] != "publish_policy"
                    or duplicate["request_hash"] != request_hash
                ):
                    raise AnswerRoutingConflict("answer policy idempotency conflict")
                return {"status": "duplicate", **json.loads(duplicate["result_json"])}
            mandate = read_exact_mandate_version(cur, mandate_version_ref)
            if mandate["content_hash"] != mandate_version_hash:
                raise AnswerRoutingConflict("answer policy mandate hash drifted")
            pointer = cur.execute(
                "SELECT version_id FROM mandate_pointer "
                "WHERE mandate_ref=? AND active=1",
                (mandate["mandate_ref"],),
            ).fetchone()
            if pointer is None or pointer["version_id"] != mandate_version_ref:
                raise AnswerRoutingConflict("answer policy requires the active mandate")
            latest = cur.execute(
                "SELECT version_id,version_number FROM "
                "answer_sufficiency_policy_versions WHERE policy_ref=? "
                "ORDER BY version_number DESC LIMIT 1",
                (policy_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise AnswerRoutingConflict("first answer policy cannot have a prior")
                version = 1
            else:
                if latest["version_id"] != prior_version_ref:
                    raise AnswerRoutingConflict("answer policy prior version is stale")
                version = int(latest["version_number"]) + 1
            existing_pointer = cur.execute(
                "SELECT v.policy_ref FROM answer_sufficiency_policy_pointer p "
                "JOIN answer_sufficiency_policy_versions v "
                "ON v.version_id=p.version_id WHERE p.mandate_ref=?",
                (mandate["mandate_ref"],),
            ).fetchone()
            if existing_pointer is not None and existing_pointer["policy_ref"] != policy_ref:
                raise AnswerRoutingConflict(
                    "mandate is already bound to another answer policy"
                )
            wire = _record({
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created,
                "policy_ref": policy_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                "mandate_ref": mandate["mandate_ref"],
                "mandate_version_ref": mandate["id"],
                "mandate_version_hash": mandate["content_hash"],
                "thresholds": threshold_wire,
                "refresh_route": refresh_wire,
                "adhoc_research_route": adhoc_wire,
                "effective_from": effective_from,
                "effective_until": effective_until,
                "actor_ref": actor_ref,
            })
            validate_answer_sufficiency_policy(wire)
            if cur.execute(
                "SELECT 1 FROM answer_sufficiency_policy_versions WHERE version_id=?",
                (version_id,),
            ).fetchone() is not None:
                raise AnswerRoutingConflict("answer policy version already exists")
            cur.execute(
                "INSERT INTO answer_sufficiency_policy_versions "
                "(version_id,policy_ref,version_number,prior_version_id,mandate_ref,"
                "mandate_version_ref,mandate_version_hash,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["policy_ref"], wire["version"],
                    wire["prior_version_ref"], wire["mandate_ref"],
                    wire["mandate_version_ref"], wire["mandate_version_hash"],
                    canonical_json(wire), wire["content_hash"], wire["actor_ref"],
                    wire["created_at"],
                ),
            )
            cur.execute(
                "INSERT INTO answer_sufficiency_policy_pointer "
                "(mandate_ref,version_id,content_hash,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(mandate_ref) DO UPDATE SET "
                "version_id=excluded.version_id,content_hash=excluded.content_hash,"
                "updated_at=excluded.updated_at",
                (
                    wire["mandate_ref"], wire["id"], wire["content_hash"],
                    wire["created_at"],
                ),
            )
            result = {"policy": wire}
            cur.execute(
                "INSERT INTO answer_routing_idempotency "
                "(idempotency_key,operation,request_hash,result_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    idempotency_key, "publish_policy", request_hash,
                    canonical_json(result), created,
                ),
            )
            return {"status": "fresh", **result}

    def policy(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "version_ref", maximum=256)
        row = self.connection.execute(
            "SELECT * FROM answer_sufficiency_policy_versions WHERE version_id=?",
            (version_ref,),
        ).fetchone()
        if row is None:
            raise AnswerRoutingNotFound("answer policy was not found")
        wire = validate_answer_sufficiency_policy(
            _load_record(row["record_json"], row["content_hash"], "answer policy")
        )
        if (
            wire["id"] != row["version_id"]
            or wire["policy_ref"] != row["policy_ref"]
            or wire["version"] != row["version_number"]
            or wire["mandate_ref"] != row["mandate_ref"]
            or wire["mandate_version_ref"] != row["mandate_version_ref"]
            or wire["mandate_version_hash"] != row["mandate_version_hash"]
        ):
            raise AnswerRoutingConflict("stored answer policy row drifted")
        return wire

    def _policy_state(
        self, mandate: Mapping[str, Any], as_of: datetime
    ) -> tuple[str, dict[str, Any] | None]:
        pointer = self.connection.execute(
            "SELECT version_id,content_hash FROM answer_sufficiency_policy_pointer "
            "WHERE mandate_ref=?",
            (mandate["mandate_ref"],),
        ).fetchone()
        if pointer is None:
            return "unavailable", None
        policy = self.policy(pointer["version_id"])
        if policy["content_hash"] != pointer["content_hash"]:
            raise AnswerRoutingConflict("answer policy pointer hash drifted")
        active = (
            policy["mandate_version_ref"] == mandate["id"]
            and policy["mandate_version_hash"] == mandate["content_hash"]
            and _datetime(policy["effective_from"], "policy effective_from") <= as_of
            and (
                policy["effective_until"] is None
                or as_of < _datetime(
                    policy["effective_until"], "policy effective_until"
                )
            )
        )
        return ("active" if active else "stale"), policy

    def subjects(self, *, as_of: str | None = None) -> list[dict[str, Any]]:
        when = _datetime(as_of or _now(), "as_of")
        bindings: list[dict[str, Any]] = []
        for mandate in self.agenda.active_mandates(at=when.isoformat()):
            state, policy = self._policy_state(mandate, when)
            for company_ref in mandate["scope_refs"]:
                body = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "answer_subject",
                    "label": f"{company_ref} · {mandate['objective']}",
                    "company_ref": company_ref,
                    "mandate_ref": mandate["mandate_ref"],
                    "mandate_version_ref": mandate["id"],
                    "mandate_version_hash": mandate["content_hash"],
                    "policy_state": state,
                    "policy_version_ref": None if policy is None else policy["id"],
                    "policy_version_hash": (
                        None if policy is None else policy["content_hash"]
                    ),
                }
                identity = content_hash(body)
                wire = {
                    **body,
                    "ref": f"answer-subject:{identity}",
                }
                wire["hash"] = content_hash(wire)
                bindings.append(wire)
        return sorted(bindings, key=lambda item: (item["company_ref"], item["ref"]))

    def _subject(
        self, value: Any, *, as_of: datetime
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        candidate = _closed(value, _SUBJECT_FIELDS, "answer subject binding")
        matches = [
            item
            for item in self.subjects(as_of=as_of.isoformat())
            if item["ref"] == candidate.get("ref")
        ]
        if len(matches) != 1 or canonical_json(matches[0]) != canonical_json(candidate):
            raise AnswerRoutingConflict("answer subject binding is stale or unavailable")
        mandate = read_exact_mandate_version(
            self.connection.cursor(), matches[0]["mandate_version_ref"]
        )
        policy = (
            None
            if matches[0]["policy_version_ref"] is None
            else self.policy(matches[0]["policy_version_ref"])
        )
        return matches[0], mandate, policy

    def _formal_claims(
        self, question: Mapping[str, Any] | None, company_ref: str, as_of: datetime
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        if question is None or question["state"] != "answered":
            return [], [], False
        claims: list[dict[str, Any]] = []
        evidence_by_ref: dict[str, dict[str, Any]] = {}
        superseded = False
        for binding in question["answer_bindings"]:
            row = self.connection.execute(
                "SELECT * FROM claim_versions WHERE claim_version_id=?",
                (binding["claim_version_ref"],),
            ).fetchone()
            if row is None:
                raise AnswerRoutingConflict("answered ClaimVersion is unavailable")
            claim = _load_record(row["claim_json"], row["content_hash"], "ClaimVersion")
            if (
                claim.get("id") != row["claim_version_id"]
                or claim.get("claim_ref") != row["claim_ref"]
                or claim.get("version") != row["version_number"]
                or claim.get("prior_version_ref") != row["prior_version_id"]
                or claim.get("created_at") != row["created_at"]
                or claim["content_hash"] != binding["claim_version_hash"]
                or claim.get("subject_ref") != company_ref
            ):
                raise AnswerRoutingConflict("answered ClaimVersion binding drifted")
            latest = self.connection.execute(
                "SELECT claim_version_id FROM claim_versions WHERE claim_ref=? "
                "ORDER BY version_number DESC,claim_version_id DESC LIMIT 1",
                (claim["claim_ref"],),
            ).fetchone()
            is_latest = latest is not None and latest["claim_version_id"] == claim["id"]
            superseded = superseded or not is_latest
            projection = self.store.get_claim(claim["id"])
            if projection is None:
                raise AnswerRoutingConflict("answered ClaimVersion projection is unavailable")
            relations = []
            for relation in projection["evidence_relations"]:
                relation_row = self.connection.execute(
                    "SELECT * FROM evidence_relations WHERE relation_id=?",
                    (relation.get("id"),),
                ).fetchone()
                if relation_row is None:
                    raise AnswerRoutingConflict("Claim evidence relation is unavailable")
                relation_wire = _load_record(
                    relation_row["relation_json"],
                    relation_row["content_hash"],
                    "EvidenceRelation",
                )
                if (
                    relation_wire.get("id") != relation_row["relation_id"]
                    or relation_wire.get("claim_ref") != relation_row["claim_ref"]
                    or relation_wire.get("claim_version_ref")
                    != relation_row["claim_version_id"]
                    or relation_wire.get("evidence_ref")
                    != relation_row["evidence_ref"]
                    or relation_wire.get("evidence_version_ref")
                    != relation_row["evidence_version_id"]
                    or relation_wire.get("relation") != relation_row["relation"]
                    or relation_wire.get("claim_version_ref") != claim["id"]
                ):
                    raise AnswerRoutingConflict("Claim evidence relation drifted")
                evidence_row = self.connection.execute(
                    "SELECT * FROM evidence_versions WHERE evidence_version_id=?",
                    (relation_wire["evidence_version_ref"],),
                ).fetchone()
                if evidence_row is None:
                    raise AnswerRoutingConflict("related EvidenceVersion is unavailable")
                evidence = _load_record(
                    evidence_row["evidence_json"],
                    evidence_row["content_hash"],
                    "EvidenceVersion",
                )
                if (
                    evidence.get("id") != evidence_row["evidence_version_id"]
                    or evidence.get("evidence_ref") != evidence_row["evidence_ref"]
                    or evidence.get("version") != evidence_row["version_number"]
                    or evidence.get("prior_version_ref")
                    != evidence_row["prior_version_id"]
                    or evidence.get("created_at") != evidence_row["created_at"]
                ):
                    raise AnswerRoutingConflict("related EvidenceVersion row drifted")
                latest_evidence = self.connection.execute(
                    "SELECT evidence_version_id FROM evidence_versions "
                    "WHERE evidence_ref=? ORDER BY version_number DESC,"
                    "evidence_version_id DESC LIMIT 1",
                    (evidence["evidence_ref"],),
                ).fetchone()
                retrieved = _datetime(evidence["retrieved_at"], "evidence retrieved_at")
                if retrieved > as_of:
                    raise AnswerRoutingConflict("EvidenceVersion was retrieved in the future")
                valid_until = evidence.get("valid_until")
                expired = (
                    valid_until is not None
                    and as_of >= _datetime(valid_until, "evidence valid_until")
                )
                evidence_by_ref.setdefault(evidence["id"], {
                    "evidence_version_ref": evidence["id"],
                    "evidence_version_hash": evidence["content_hash"],
                    "evidence_ref": evidence["evidence_ref"],
                    "source_type": evidence["source_type"],
                    "source_ref": evidence["source_ref"],
                    "retrieved_at": evidence["retrieved_at"],
                    "valid_until": valid_until,
                    "status": (
                        "expired" if expired else
                        "current" if latest_evidence is not None
                        and latest_evidence["evidence_version_id"] == evidence["id"]
                        else "superseded"
                    ),
                    "age_seconds": int((as_of - retrieved).total_seconds()),
                    "applicable_periods": [],
                    "source_lineage": evidence["source_lineage"],
                    "independence_group": evidence["independence_group"],
                })
                periods = evidence_by_ref[evidence["id"]]["applicable_periods"]
                if claim["period"] not in periods:
                    periods.append(claim["period"])
                relations.append({
                    "relation_ref": relation_wire["id"],
                    "relation_hash": relation_wire["content_hash"],
                    "relation": relation_wire["relation"],
                    "evidence_version_ref": evidence["id"],
                    "evidence_version_hash": evidence["content_hash"],
                })
            claims.append({
                "claim_version_ref": claim["id"],
                "claim_version_hash": claim["content_hash"],
                "claim_ref": claim["claim_ref"],
                "subject_ref": claim["subject_ref"],
                "metric_or_aspect": claim["metric_or_aspect"],
                "period": claim["period"],
                "basis": claim["basis"],
                "normalized_statement": claim["normalized_statement"],
                "claim_kind": claim["claim_kind"],
                "value": claim.get("value"),
                "unit": claim.get("unit"),
                "status": projection["status"],
                "is_latest": is_latest,
                "evidence_relations": sorted(
                    relations, key=lambda item: item["relation_ref"]
                ),
            })
        for evidence in evidence_by_ref.values():
            evidence["applicable_periods"] = sorted(
                evidence["applicable_periods"], key=canonical_json
            )
        return (
            sorted(claims, key=lambda item: item["claim_version_ref"]),
            sorted(
                evidence_by_ref.values(),
                key=lambda item: item["evidence_version_ref"],
            ),
            superseded,
        )

    def _open_questions(
        self, mandate_ref: str, company_ref: str
    ) -> list[dict[str, Any]]:
        result = []
        for question in self.backlog.questions(mandate_ref=mandate_ref):
            if (
                question["head"]["company_ref"] != company_ref
                or question["state"] in {"answered", "retired"}
            ):
                continue
            result.append({
                "question_ref": question["question_ref"],
                "question_version_ref": question["head"]["id"],
                "question_version_hash": question["head"]["content_hash"],
                "question": question["head"]["question"],
                "answer_criteria": question["head"]["answer_criteria"],
                "state": question["state"],
            })
        return sorted(result, key=lambda item: item["question_ref"])

    def _industry_context(
        self, company_ref: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int, int]:
        overlay_rows = self.connection.execute(
            "SELECT v.* FROM company_overlay_pointer p "
            "JOIN company_overlay_versions v ON v.version_id=p.version_id "
            "WHERE v.company_ref=? ORDER BY v.version_id",
            (company_ref,),
        ).fetchall()
        overlays = []
        packs: dict[str, dict[str, Any]] = {}
        driver_packs: dict[str, dict[str, Any]] = {}
        driver_coverage: dict[str, bool] = {}
        for row in overlay_rows:
            overlay = _load_record(
                row["record_json"], row["content_hash"], "CompanyOverlayVersion"
            )
            if overlay.get("id") != row["version_id"]:
                raise AnswerRoutingConflict("company overlay pointer drifted")
            overlays.append(overlay)
            pack_row = self.connection.execute(
                "SELECT * FROM industry_evidence_pack_versions WHERE version_id=?",
                (overlay["evidence_pack_version_ref"],),
            ).fetchone()
            if pack_row is None:
                raise AnswerRoutingConflict("company overlay evidence pack is unavailable")
            pack = _load_record(
                pack_row["record_json"], pack_row["content_hash"],
                "IndustryEvidencePackVersion",
            )
            if pack["content_hash"] != overlay["evidence_pack_version_hash"]:
                raise AnswerRoutingConflict("company overlay evidence pack drifted")
            packs[pack["id"]] = pack
            driver_row = self.connection.execute(
                "SELECT * FROM driver_pack_versions WHERE version_id=?",
                (pack["driver_pack_version_ref"],),
            ).fetchone()
            if driver_row is None:
                raise AnswerRoutingConflict("industry driver pack is unavailable")
            driver_pack = _load_record(
                driver_row["record_json"], driver_row["content_hash"],
                "DriverPackVersion",
            )
            if driver_pack["content_hash"] != pack["driver_pack_version_hash"]:
                raise AnswerRoutingConflict("industry driver pack drifted")
            driver_packs[driver_pack["id"]] = driver_pack
            for view in overlay["driver_views"]:
                covered = bool(view["metric_coverage"]) and all(
                    item["status"] == "observed"
                    for item in view["metric_coverage"]
                )
                prior = driver_coverage.get(view["driver_ref"])
                if prior is not None and prior != covered:
                    raise AnswerRoutingConflict("company driver coverage is divergent")
                driver_coverage[view["driver_ref"]] = covered
        total = len(driver_coverage)
        covered = sum(1 for value in driver_coverage.values() if value)
        return (
            overlays,
            sorted(packs.values(), key=lambda item: item["id"]),
            sorted(driver_packs.values(), key=lambda item: item["id"]),
            covered,
            total,
        )

    def _current_theses(
        self, company_ref: str, claim_refs: set[str]
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT v.* FROM current_pointers p JOIN thesis_versions v "
            "ON v.version_id=p.version_id ORDER BY v.thesis_id"
        ).fetchall()
        result = []
        for row in rows:
            try:
                content = json.loads(row["content_json"])
                thesis = ThesisVersion.from_dict(content)
            except (
                TypeError, json.JSONDecodeError, ContractValidationError
            ) as exc:
                raise AnswerRoutingConflict("current ThesisVersion is invalid") from exc
            thesis_wire = thesis.to_dict()
            thesis_content = {
                field: thesis_wire[field] for field in _THESIS_CONTENT_FIELDS
            }
            if thesis.schema_version == "0.1":
                authority_matches = (
                    thesis.verification_ref == row["verification_id"]
                    and row["authority_kind"] == "verification"
                    and row["authority_ref"] == thesis.verification_ref
                )
            else:
                authority_matches = (
                    thesis.authority_kind == row["authority_kind"]
                    and thesis.authority_ref == row["authority_ref"]
                )
            if (
                canonical_json(thesis_wire) != row["content_json"]
                or thesis.id != row["version_id"]
                or thesis.thesis_ref != row["thesis_id"]
                or thesis.version != row["version_number"]
                or not authority_matches
                or thesis.content_hash != row["content_hash"]
                or content_hash(thesis_content) != row["content_hash"]
            ):
                raise AnswerRoutingConflict("current ThesisVersion content hash drifted")
            admitted = self.connection.execute(
                "SELECT 1 FROM thesis_admission_candidates "
                "WHERE thesis_ref=? AND company_ref=? LIMIT 1",
                (row["thesis_id"], company_ref),
            ).fetchone() is not None
            referenced = bool(set(content.get("claim_refs", [])) & claim_refs)
            if not admitted and not referenced:
                continue
            result.append({
                "thesis_version_ref": row["version_id"],
                "thesis_version_hash": row["content_hash"],
                "thesis_ref": row["thesis_id"],
                "version": row["version_number"],
                "created_at": row["created_at"],
                "authority_kind": row["authority_kind"],
                "authority_ref": row["authority_ref"],
                "content": content,
            })
        return result

    def _unobservable_terminals(
        self, mandate_ref: str, company_ref: str
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT t.*,l.record_json AS loop_json,l.content_hash AS loop_hash "
            "FROM bounded_planner_terminal_events t "
            "JOIN bounded_planner_loop_versions l ON l.version_id=t.loop_version_ref "
            "JOIN backlog_question_versions q ON q.version_id=l.question_version_ref "
            "WHERE q.mandate_ref=? AND q.company_ref=? "
            "AND t.terminal_state='coverage_complete_unobservable_candidate' "
            "ORDER BY t.event_id",
            (mandate_ref, company_ref),
        ).fetchall()
        result = []
        for row in rows:
            terminal = _load_record(
                row["record_json"], row["content_hash"],
                "BoundedPlannerTerminalEvent",
            )
            loop = _load_record(
                row["loop_json"], row["loop_hash"], "BoundedPlannerLoopVersion"
            )
            if terminal["loop_version_ref"] != loop["id"]:
                raise AnswerRoutingConflict("unobservable terminal loop drifted")
            result.append({
                "terminal_ref": terminal["id"],
                "terminal_hash": terminal["content_hash"],
                "loop_version_ref": loop["id"],
                "loop_version_hash": loop["content_hash"],
                "terminal_state": terminal["terminal_state"],
            })
        return result

    def _refresh_budget_state(
        self, policy: Mapping[str, Any], when: datetime
    ) -> dict[str, Any]:
        budget_day = when.astimezone(timezone.utc).date().isoformat()
        reserved = self.connection.execute(
            "SELECT COALESCE(SUM(reserved_cost_units),0) AS total "
            "FROM answer_refresh_budget_reservations "
            "WHERE policy_version_ref=? AND budget_day=?",
            (policy["id"], budget_day),
        ).fetchone()["total"]
        limit = policy["refresh_route"]["max_cost_units"]
        return {
            "budget_day": budget_day,
            "daily_limit_cost_units": limit,
            "reserved_cost_units": int(reserved),
            "remaining_cost_units": max(limit - int(reserved), 0),
        }

    def _eligible_refresh_plan(
        self,
        *,
        policy: Mapping[str, Any],
        question: Mapping[str, Any],
        stale_evidence_refs: list[str],
        created_at: str,
        when: datetime,
    ) -> tuple[dict[str, Any] | None, str | None]:
        refresh = policy["refresh_route"]
        if not refresh["enabled"]:
            return None, "refresh_disabled"
        allowed = {
            (item["ref"], item["hash"])
            for item in refresh["probe_template_bindings"]
        }
        rows = self.connection.execute(
            "SELECT v.version_id FROM bounded_planner_loop_versions v "
            "WHERE v.question_version_ref=? AND v.version_number=("
            "SELECT MAX(v2.version_number) FROM bounded_planner_loop_versions v2 "
            "WHERE v2.loop_ref=v.loop_ref) ORDER BY v.version_id",
            (question["head"]["id"],),
        ).fetchall()
        structural: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            loop = self.bounded.loop(row["version_id"])
            if self.bounded.terminal(loop["id"]) is not None:
                continue
            if self.bounded.rounds(loop["id"]):
                continue
            if (
                loop["budget"]["max_rounds"] != 1
                or len(loop["required_coverage_items"]) != 1
                or len(loop["template_bindings"]) != 1
            ):
                continue
            binding = loop["template_bindings"][0]
            if (
                binding["coverage_item_ref"]
                != loop["required_coverage_items"][0]
                or (
                    binding["template_version_ref"],
                    binding["template_version_hash"],
                ) not in allowed
            ):
                continue
            template = self.bounded.probe_template(
                binding["template_version_ref"]
            )
            cost = template["cost"]
            if (
                template["content_hash"] != binding["template_version_hash"]
                or template["side_effect_class"] != "read_only"
                or template["output_contract_ref"]
                != ANSWER_REFRESH_OUTPUT_CONTRACT_REF
                or template["verifier_ref"] != ANSWER_REFRESH_VERIFIER_REF
                or cost["cost_units"] > refresh["max_cost_units"]
                or cost["cost_units"] > loop["budget"]["max_cost_units"]
                or cost["max_seconds"] > loop["budget"]["max_seconds"]
            ):
                continue
            structural.append((loop, binding, template))
        if not structural:
            return None, "refresh_probe_unavailable"
        if len(structural) != 1:
            return None, "refresh_probe_ambiguous"
        loop, binding, template = structural[0]
        budget = self._refresh_budget_state(policy, when)
        cost_units = template["cost"]["cost_units"]
        if cost_units > budget["remaining_cost_units"]:
            return None, "refresh_budget_exhausted"
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": "pending",
            "created_at": created_at,
            "policy_version_ref": policy["id"],
            "policy_version_hash": policy["content_hash"],
            "question_ref": question["question_ref"],
            "question_version_ref": question["head"]["id"],
            "question_version_hash": question["head"]["content_hash"],
            "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"],
            "coverage_item_ref": binding["coverage_item_ref"],
            "template_version_ref": template["id"],
            "template_version_hash": template["content_hash"],
            "parameters": binding["parameters"],
            "cost_units": cost_units,
            "budget": budget,
            "stale_evidence_refs": sorted(stale_evidence_refs),
            "candidate_staging_required": True,
        }
        identity = content_hash({
            name: value for name, value in body.items() if name != "id"
        })
        body["id"] = f"answer-refresh-plan:{identity}"
        return _record(body), None

    def route(
        self,
        *,
        subject_binding: Mapping[str, Any],
        question: str,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        question_text = _text(question, "question", maximum=4000)
        created = _time(as_of or _now(), "as_of")
        when = _datetime(created, "as_of")
        with self._snapshot():
            subject, mandate, policy = self._subject(
                subject_binding, as_of=when
            )
            question_ref = question_ref_for(
                mandate["mandate_ref"], subject["company_ref"], question_text
            )
            question_row = self.connection.execute(
                "SELECT 1 FROM backlog_questions WHERE question_ref=?",
                (question_ref,),
            ).fetchone()
            admitted_question = (
                None if question_row is None else self.backlog.question(question_ref)
            )
            claims, evidence, superseded = self._formal_claims(
                admitted_question, subject["company_ref"], when
            )
            open_questions = self._open_questions(
                mandate["mandate_ref"], subject["company_ref"]
            )
            overlays, evidence_packs, driver_packs, covered_drivers, total_drivers = (
                self._industry_context(subject["company_ref"])
            )
            unobservable = self._unobservable_terminals(
                mandate["mandate_ref"], subject["company_ref"]
            )
            theses = self._current_theses(
                subject["company_ref"],
                {item["claim_version_ref"] for item in claims},
            )
            contested = sum(
                1 for item in claims if item["status"] == "contested"
            )
            supporting_evidence = {
                relation["evidence_version_ref"]
                for claim in claims
                for relation in claim["evidence_relations"]
                if relation["relation"] == "supports"
            }
            claims_without_support = [
                claim["claim_version_ref"]
                for claim in claims
                if not any(
                    relation["relation"] == "supports"
                    for relation in claim["evidence_relations"]
                )
            ]
            coverage_bps = (
                0 if total_drivers == 0
                else covered_drivers * 10_000 // total_drivers
            )
            freshness = []
            unclassified = []
            stale = []
            ages = (
                {}
                if policy is None
                else policy["thresholds"][
                    "max_evidence_age_days_by_source_type"
                ]
            )
            for item in evidence:
                max_days = ages.get(item["source_type"])
                state = "current"
                if max_days is None:
                    state = "unclassified"
                    unclassified.append(item["evidence_version_ref"])
                elif (
                    item["status"] != "current"
                    or item["age_seconds"] > max_days * 86_400
                ):
                    state = "stale"
                    stale.append(item["evidence_version_ref"])
                freshness.append({
                    "evidence_version_ref": item["evidence_version_ref"],
                    "source_type": item["source_type"],
                    "age_seconds": item["age_seconds"],
                    "max_age_days": max_days,
                    "state": state,
                })
            metrics = {
                "formal_claim_count": len(claims),
                "formal_evidence_count": len(supporting_evidence),
                "contested_claim_count": contested,
                "open_question_count": len(open_questions),
                "unobservable_terminal_count": len(unobservable),
                "covered_driver_count": covered_drivers,
                "driver_count": total_drivers,
                "driver_coverage_bps": coverage_bps,
                "claims_without_support": claims_without_support,
                "unclassified_evidence_refs": sorted(unclassified),
                "stale_evidence_refs": sorted(stale),
                "answered_claim_superseded": superseded,
                "evidence_freshness": sorted(
                    freshness, key=lambda item: item["evidence_version_ref"]
                ),
            }
            pack = _record({
                "schema_version": SCHEMA_VERSION,
                "id": "pending",
                "created_at": created,
                "projection_kind": "answer_context_pack",
                "question": question_text,
                "subject_binding": subject,
                "mandate_version": {
                    "ref": mandate["id"], "hash": mandate["content_hash"],
                    "mandate_ref": mandate["mandate_ref"],
                    "objective": mandate["objective"],
                    "scope_refs": mandate["scope_refs"],
                },
                "policy_version": (
                    None if policy is None else {
                        "ref": policy["id"], "hash": policy["content_hash"],
                        "policy_ref": policy["policy_ref"],
                        "thresholds": policy["thresholds"],
                        "refresh_route": policy["refresh_route"],
                        "adhoc_research_route": policy["adhoc_research_route"],
                    }
                ),
                "matched_research_question": (
                    None if admitted_question is None else {
                        "question_ref": admitted_question["question_ref"],
                        "question_version_ref": admitted_question["head"]["id"],
                        "question_version_hash": admitted_question["head"]["content_hash"],
                        "state": admitted_question["state"],
                        "answer_binding_refs": [
                            item["id"] for item in admitted_question["answer_bindings"]
                        ],
                    }
                ),
                "claim_versions": claims,
                "evidence_versions": evidence,
                "current_thesis_versions": theses,
                "industry_driver_packs": driver_packs,
                "company_overlays": overlays,
                "industry_evidence_packs": evidence_packs,
                "open_questions": open_questions,
                "unobservable_terminals": unobservable,
                "metrics": metrics,
            })
            pack_identity = content_hash({
                name: value
                for name, value in pack.items()
                if name not in {"id", "content_hash"}
            })
            pack["id"] = f"answer-context-pack:{pack_identity}"
            without_hash = dict(pack)
            without_hash.pop("content_hash")
            pack["content_hash"] = content_hash(without_hash)
            reasons: set[str] = set()
            if subject["policy_state"] == "unavailable" or policy is None:
                reasons.add("policy_unavailable")
            elif subject["policy_state"] != "active":
                reasons.add("policy_stale")
            if admitted_question is None:
                reasons.add("question_not_admitted")
            elif admitted_question["state"] != "answered":
                reasons.add("question_not_answered")
            if superseded:
                reasons.add("answered_claim_superseded")
            if claims_without_support:
                reasons.add("claim_without_support")
            if policy is not None:
                thresholds = policy["thresholds"]
                if len(claims) < thresholds["min_formal_claims"]:
                    reasons.add("insufficient_formal_claims")
                if len(supporting_evidence) < thresholds["min_formal_evidence"]:
                    reasons.add("insufficient_formal_evidence")
                if unclassified:
                    reasons.add("unclassified_source_type")
                if stale:
                    reasons.add("stale_evidence")
                if contested > thresholds["allowed_contested_claims"]:
                    reasons.add("too_many_contested_claims")
                if len(open_questions) > thresholds["allowed_open_questions"]:
                    reasons.add("too_many_open_questions")
                if len(unobservable) > thresholds["allowed_unobservable_terminals"]:
                    reasons.add("too_many_unobservable_terminals")
                if coverage_bps < thresholds["min_driver_coverage_bps"]:
                    reasons.add("insufficient_driver_coverage")
            refresh_plan = None
            if (
                reasons == {"stale_evidence"}
                and policy is not None
                and admitted_question is not None
            ):
                refresh_plan, refresh_failure = self._eligible_refresh_plan(
                    policy=policy,
                    question=admitted_question,
                    stale_evidence_refs=sorted(stale),
                    created_at=created,
                    when=when,
                )
                if refresh_plan is None:
                    assert refresh_failure is not None
                    reasons.add(refresh_failure)
                else:
                    reasons.add("refresh_required")
            ordered_reasons = [
                reason for reason in _ROUTE_REASON_ORDER if reason in reasons
            ]
            if not ordered_reasons:
                route = "answer_direct"
            elif refresh_plan is not None:
                route = "answer_after_refresh"
            else:
                route = "recommend_agenda_item"
            decision = _record({
                "schema_version": SCHEMA_VERSION,
                "id": f"answer-route-decision:{content_hash({'context': pack['id'], 'route': route, 'refresh_plan': None if refresh_plan is None else refresh_plan['id']})}",
                "created_at": created,
                "route": route,
                "reason_codes": ordered_reasons or ["answer_direct_ready"],
                "answer_context_pack_ref": pack["id"],
                "answer_context_pack_hash": pack["content_hash"],
                "subject_ref": subject["ref"],
                "subject_hash": subject["hash"],
                "question": question_text,
                "direct_claim_refs": (
                    [item["claim_version_ref"] for item in claims]
                    if route == "answer_direct" else []
                ),
                "direct_evidence_refs": (
                    sorted(supporting_evidence) if route == "answer_direct" else []
                ),
                "refresh_route_available": refresh_plan is not None,
                "refresh_plan": refresh_plan,
                "adhoc_research_route_available": False,
                "agenda_recommendation": (
                    None if route != "recommend_agenda_item" else {
                        "mandate_version_ref": mandate["id"],
                        "mandate_version_hash": mandate["content_hash"],
                        "company_ref": subject["company_ref"],
                        "question": question_text,
                        "reason_codes": ordered_reasons,
                        "write_performed": False,
                    }
                ),
                "write_performed": False,
            })
            return {"context_pack": pack, "decision": decision}

    def refresh_reservation_for_decision(
        self, decision_ref: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM answer_refresh_budget_reservations WHERE decision_ref=?",
            (_text(decision_ref, "decision_ref", maximum=256),),
        ).fetchone()
        if row is None:
            return None
        return _load_record(
            row["record_json"], row["content_hash"],
            "AnswerRefreshBudgetReservation",
        )

    def refresh_dispatch_for_reservation(
        self, reservation_ref: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM answer_refresh_dispatch_receipts WHERE reservation_ref=?",
            (_text(reservation_ref, "reservation_ref", maximum=256),),
        ).fetchone()
        if row is None:
            return None
        return _load_record(
            row["record_json"], row["content_hash"],
            "AnswerRefreshDispatchReceipt",
        )

    def refresh_outcome_for_dispatch(
        self, dispatch_ref: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM answer_refresh_outcome_receipts WHERE dispatch_ref=?",
            (_text(dispatch_ref, "dispatch_ref", maximum=256),),
        ).fetchone()
        if row is None:
            return None
        return _load_record(
            row["record_json"], row["content_hash"],
            "AnswerRefreshOutcomeReceipt",
        )

    def _reserve_refresh(
        self, routed: Mapping[str, Any], *, actor_ref: str
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        decision = routed["decision"]
        pack = routed["context_pack"]
        if (
            decision["route"] != "answer_after_refresh"
            or not decision["refresh_route_available"]
            or decision["refresh_plan"] is None
        ):
            raise AnswerRoutingConflict("route decision does not authorize refresh")
        plan = decision["refresh_plan"]
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM answer_refresh_budget_reservations "
                "WHERE decision_ref=?",
                (decision["id"],),
            ).fetchone()
            if existing is not None:
                reservation = _load_record(
                    existing["record_json"], existing["content_hash"],
                    "AnswerRefreshBudgetReservation",
                )
                if (
                    reservation["decision_hash"] != decision["content_hash"]
                    or reservation["refresh_plan_hash"] != plan["content_hash"]
                    or reservation["actor_ref"] != actor_ref
                ):
                    raise AnswerRoutingConflict(
                        "answer refresh reservation identity conflicts"
                    )
                return {"status": "duplicate", **reservation}
            loop_reservation = cur.execute(
                "SELECT decision_ref FROM answer_refresh_budget_reservations "
                "WHERE loop_version_ref=?",
                (plan["loop_version_ref"],),
            ).fetchone()
            if loop_reservation is not None:
                raise AnswerRoutingConflict(
                    "answer refresh loop already has another budget reservation"
                )
            pointer = cur.execute(
                "SELECT version_id,content_hash FROM "
                "answer_sufficiency_policy_pointer WHERE mandate_ref=?",
                (pack["mandate_version"]["mandate_ref"],),
            ).fetchone()
            if (
                pointer is None
                or pointer["version_id"] != plan["policy_version_ref"]
                or pointer["content_hash"] != plan["policy_version_hash"]
            ):
                raise AnswerRoutingConflict(
                    "answer refresh policy changed before reservation"
                )
            policy = self.policy(plan["policy_version_ref"])
            loop = self.bounded.loop(plan["loop_version_ref"])
            template = self.bounded.probe_template(plan["template_version_ref"])
            binding = loop["template_bindings"]
            if (
                loop["content_hash"] != plan["loop_version_hash"]
                or self.bounded.terminal(loop["id"]) is not None
                or self.bounded.rounds(loop["id"])
                or len(binding) != 1
                or binding[0]["coverage_item_ref"] != plan["coverage_item_ref"]
                or binding[0]["template_version_ref"] != template["id"]
                or binding[0]["template_version_hash"] != template["content_hash"]
                or binding[0]["parameters"] != plan["parameters"]
                or template["content_hash"] != plan["template_version_hash"]
            ):
                raise AnswerRoutingConflict(
                    "answer refresh loop changed before reservation"
                )
            self._validate_refresh_templates(policy["refresh_route"])
            when = _datetime(decision["created_at"], "decision created_at")
            budget = self._refresh_budget_state(policy, when)
            if (
                budget["budget_day"] != plan["budget"]["budget_day"]
                or plan["cost_units"] > budget["remaining_cost_units"]
            ):
                raise AnswerRoutingConflict("answer refresh day budget is exhausted")
            identity = {
                "decision_ref": decision["id"],
                "decision_hash": decision["content_hash"],
                "answer_context_pack_ref": pack["id"],
                "answer_context_pack_hash": pack["content_hash"],
                "policy_version_ref": policy["id"],
                "policy_version_hash": policy["content_hash"],
                "loop_version_ref": loop["id"],
                "loop_version_hash": loop["content_hash"],
                "budget_day": budget["budget_day"],
                "reserved_cost_units": plan["cost_units"],
                "refresh_plan_ref": plan["id"],
                "refresh_plan_hash": plan["content_hash"],
                "refresh_plan": plan,
                "actor_ref": actor_ref,
            }
            reservation_id = (
                "answer-refresh-reservation:" + content_hash(identity)
            )
            reservation = _record({
                "schema_version": "0.1",
                "id": reservation_id,
                "created_at": _now(),
                **identity,
            })
            cur.execute(
                "INSERT INTO answer_refresh_budget_reservations "
                "(reservation_id,decision_ref,policy_version_ref,loop_version_ref,budget_day,"
                "reserved_cost_units,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    reservation["id"], reservation["decision_ref"],
                    reservation["policy_version_ref"], reservation["loop_version_ref"],
                    reservation["budget_day"],
                    reservation["reserved_cost_units"], canonical_json(reservation),
                    reservation["content_hash"], reservation["actor_ref"],
                    reservation["created_at"],
                ),
            )
            return {"status": "fresh", **reservation}

    def _record_refresh_dispatch(
        self,
        reservation: Mapping[str, Any],
        round_wire: Mapping[str, Any],
        *,
        actor_ref: str,
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM answer_refresh_dispatch_receipts "
                "WHERE reservation_ref=?",
                (reservation["id"],),
            ).fetchone()
            if existing is not None:
                receipt = _load_record(
                    existing["record_json"], existing["content_hash"],
                    "AnswerRefreshDispatchReceipt",
                )
                return {"status": "duplicate", **receipt}
            plan = reservation["refresh_plan"]
            if (
                round_wire["loop_version_ref"] != plan["loop_version_ref"]
                or round_wire["loop_version_hash"] != plan["loop_version_hash"]
            ):
                raise AnswerRoutingConflict(
                    "answer refresh round belongs to another loop"
                )
            proposal = self.bounded.proposal(round_wire["proposal_ref"])
            action = proposal["action"]
            if (
                action["kind"] != "probe"
                or action["coverage_item_ref"] != plan["coverage_item_ref"]
                or action["template_version_ref"] != plan["template_version_ref"]
                or action["template_version_hash"] != plan["template_version_hash"]
                or action["parameters"] != plan["parameters"]
            ):
                raise AnswerRoutingConflict(
                    "answer refresh proposal drifted from the reserved plan"
                )
            identity = {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "decision_ref": reservation["decision_ref"],
                "decision_hash": reservation["decision_hash"],
                "loop_version_ref": round_wire["loop_version_ref"],
                "loop_version_hash": round_wire["loop_version_hash"],
                "proposal_ref": proposal["id"],
                "proposal_hash": proposal["content_hash"],
                "round_ref": round_wire["id"],
                "round_hash": round_wire["content_hash"],
                "work_order_ref": round_wire["work_order_ref"],
                "work_order_hash": round_wire["work_order_hash"],
                "candidate_staging_required": True,
                "formal_authority_writes": 0,
                "actor_ref": actor_ref,
            }
            receipt = _record({
                "schema_version": "0.1",
                "id": "answer-refresh-dispatch:" + content_hash(identity),
                "created_at": _now(),
                **identity,
            })
            cur.execute(
                "INSERT INTO answer_refresh_dispatch_receipts "
                "(receipt_id,reservation_ref,decision_ref,loop_version_ref,round_ref,"
                "work_order_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt["id"], receipt["reservation_ref"],
                    receipt["decision_ref"], receipt["loop_version_ref"],
                    receipt["round_ref"], receipt["work_order_ref"],
                    canonical_json(receipt), receipt["content_hash"],
                    receipt["actor_ref"], receipt["created_at"],
                ),
            )
            return {"status": "fresh", **receipt}

    def _record_refresh_outcome(
        self,
        dispatch: Mapping[str, Any],
        outcome: Mapping[str, Any],
        terminal: Mapping[str, Any],
        candidate_binding: Mapping[str, Any] | None,
        *,
        actor_ref: str,
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM answer_refresh_outcome_receipts WHERE dispatch_ref=?",
                (dispatch["id"],),
            ).fetchone()
            if existing is not None:
                receipt = _load_record(
                    existing["record_json"], existing["content_hash"],
                    "AnswerRefreshOutcomeReceipt",
                )
                return {"status": "duplicate", **receipt}
            identity = {
                "dispatch_ref": dispatch["id"],
                "dispatch_hash": dispatch["content_hash"],
                "outcome_ref": outcome["id"],
                "outcome_hash": outcome["content_hash"],
                "outcome_kind": outcome["outcome_kind"],
                "terminal_ref": terminal["id"],
                "terminal_hash": terminal["content_hash"],
                "terminal_state": terminal["terminal_state"],
                "candidate_staging_binding": candidate_binding,
                "formal_authority_writes": 0,
                "actor_ref": actor_ref,
            }
            receipt = _record({
                "schema_version": "0.1",
                "id": "answer-refresh-outcome:" + content_hash(identity),
                "created_at": _now(),
                **identity,
            })
            cur.execute(
                "INSERT INTO answer_refresh_outcome_receipts "
                "(receipt_id,dispatch_ref,outcome_ref,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    receipt["id"], receipt["dispatch_ref"],
                    receipt["outcome_ref"], canonical_json(receipt),
                    receipt["content_hash"], receipt["actor_ref"],
                    receipt["created_at"],
                ),
            )
            return {"status": "fresh", **receipt}


class AnswerRefreshControlPlane:
    """Reserve one day-budget slot and reuse the existing bounded queue."""

    def __init__(
        self,
        answers: AnswerRoutingAuthority,
        bounded_control: BoundedPlannerControlPlane,
        *,
        candidate_staging: Any | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if answers.bounded is not bounded_control.authority:
            raise TypeError(
                "answer refresh and bounded control must share one authority"
            )
        self.answers = answers
        self.bounded = answers.bounded
        self.control = bounded_control
        self.scheduler = bounded_control.scheduler
        self.candidate_staging = candidate_staging
        self.fault_injector = fault_injector

    def _inject(self, seam: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(seam)

    def dispatch(
        self,
        *,
        subject_binding: Mapping[str, Any],
        question: str,
        route_decision_ref: str,
        route_decision_hash: str,
        route_as_of: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        route_decision_ref = _text(
            route_decision_ref, "route_decision_ref", maximum=256
        )
        route_decision_hash = _hash(
            route_decision_hash, "route_decision_hash"
        )
        route_as_of = _time(route_as_of, "route_as_of")
        current_day = _datetime(_now(), "current time").date()
        if _datetime(route_as_of, "route_as_of").date() != current_day:
            raise AnswerRoutingConflict(
                "answer refresh dispatch requires a current-day route decision"
            )
        reservation = self.answers.refresh_reservation_for_decision(
            route_decision_ref
        )
        if reservation is None:
            routed = self.answers.route(
                subject_binding=subject_binding,
                question=question,
                as_of=route_as_of,
            )
            decision = routed["decision"]
            if (
                decision["id"] != route_decision_ref
                or decision["content_hash"] != route_decision_hash
            ):
                raise AnswerRoutingConflict(
                    "answer refresh route decision is stale or drifted"
                )
            reservation = self.answers._reserve_refresh(
                routed, actor_ref=actor_ref
            )
            self._inject("after_reservation")
        else:
            if (
                reservation["decision_hash"] != route_decision_hash
                or reservation["actor_ref"] != actor_ref
            ):
                raise AnswerRoutingConflict(
                    "answer refresh reservation belongs to another decision or actor"
                )
        existing = self.answers.refresh_dispatch_for_reservation(
            reservation["id"]
        )
        if existing is not None:
            return {
                "status": "duplicate",
                "reservation": reservation,
                "dispatch": existing,
            }
        plan = reservation["refresh_plan"]
        rounds = self.bounded.rounds(plan["loop_version_ref"])
        if len(rounds) > 1:
            raise AnswerRoutingConflict(
                "single-round answer refresh loop has multiple rounds"
            )
        if rounds:
            round_wire = rounds[0]
        else:
            proposal = self.bounded.propose_next_capital_lease(
                plan["loop_version_ref"]
            )
            if proposal.get("action", {}).get("kind") != "probe":
                raise AnswerRoutingConflict(
                    "answer refresh loop did not produce one probe proposal"
                )
            self._inject("after_proposal")
            admitted = self.control.admit_proposal(proposal["id"])
            if admitted["status"] not in {"fresh", "duplicate"}:
                raise AnswerRoutingConflict(
                    "answer refresh proposal was not admitted"
                )
            round_wire = admitted["round"]
            self._inject("after_admission")
        dispatch = self.answers._record_refresh_dispatch(
            reservation, round_wire, actor_ref=actor_ref
        )
        self._inject("after_dispatch")
        return {
            "status": dispatch["status"],
            "reservation": reservation,
            "dispatch": dispatch,
        }

    def _candidate_staging_binding(
        self,
        value: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        result_hash: str,
    ) -> dict[str, str]:
        binding = _closed(
            value,
            {
                "candidate_evidence_ref", "candidate_evidence_hash",
                "candidate_claim_ref", "candidate_claim_hash",
            },
            "candidate staging binding",
        )
        normalized = {
            "candidate_evidence_ref": _text(
                binding["candidate_evidence_ref"], "candidate_evidence_ref"
            ),
            "candidate_evidence_hash": _hash(
                binding["candidate_evidence_hash"], "candidate_evidence_hash"
            ),
            "candidate_claim_ref": _text(
                binding["candidate_claim_ref"], "candidate_claim_ref"
            ),
            "candidate_claim_hash": _hash(
                binding["candidate_claim_hash"], "candidate_claim_hash"
            ),
        }
        if self.candidate_staging is None:
            raise AnswerRoutingConflict(
                "observed refresh requires the candidate-staging authority"
            )
        from .research_verification import (
            validate_candidate_claim,
            validate_candidate_evidence,
        )

        connection = self.candidate_staging.connection
        evidence_row = connection.execute(
            "SELECT * FROM candidate_evidence_versions WHERE version_id=?",
            (normalized["candidate_evidence_ref"],),
        ).fetchone()
        claim_row = connection.execute(
            "SELECT * FROM candidate_claim_versions WHERE version_id=?",
            (normalized["candidate_claim_ref"],),
        ).fetchone()
        if evidence_row is None or claim_row is None:
            raise AnswerRoutingConflict(
                "candidate-staging binding does not resolve"
            )
        evidence = validate_candidate_evidence(
            json.loads(evidence_row["record_json"])
        )
        claim = validate_candidate_claim(json.loads(claim_row["record_json"]))
        source_envelope_ref = _text(
            result["outputs"].get("source_envelope_ref"),
            "outputs.source_envelope_ref",
        )
        source_envelope_hash = _hash(
            result["outputs"].get("source_envelope_hash"),
            "outputs.source_envelope_hash",
        )
        if (
            evidence["content_hash"] != evidence_row["content_hash"]
            or evidence["content_hash"] != normalized["candidate_evidence_hash"]
            or claim["content_hash"] != claim_row["content_hash"]
            or claim["content_hash"] != normalized["candidate_claim_hash"]
            or claim_row["evidence_version_id"] != evidence["id"]
            or {"ref": evidence["id"], "hash": evidence["content_hash"]}
            not in claim["candidate_evidence_refs"]
            or evidence["source_envelope_ref"] != source_envelope_ref
            or evidence["source_envelope_hash"] != source_envelope_hash
        ):
            raise AnswerRoutingConflict(
                "candidate-staging evidence/claim binding drifted"
            )
        stage_receipt = None
        for row in connection.execute(
            "SELECT idempotency_key,request_hash,result_json "
            "FROM candidate_stage_requests"
        ).fetchall():
            stage_result = json.loads(row["result_json"])
            if all(
                stage_result.get(name) == item
                for name, item in normalized.items()
            ):
                stage_receipt = {
                    "candidate_stage_idempotency_key": row["idempotency_key"],
                    "candidate_stage_request_hash": row["request_hash"],
                }
                break
        if stage_receipt is None:
            raise AnswerRoutingConflict(
                "candidate-staging binding lacks an exact stage receipt"
            )
        return {
            **normalized,
            **stage_receipt,
            "result_envelope_ref": _text(result["id"], "result_envelope_ref"),
            "result_envelope_hash": _hash(result_hash, "result_envelope_hash"),
            "source_envelope_ref": source_envelope_ref,
            "source_envelope_hash": source_envelope_hash,
        }

    def finalize(
        self,
        dispatch_ref: str,
        *,
        candidate_staging_binding: Mapping[str, Any] | None,
        actor_ref: str,
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        dispatch_ref = _text(dispatch_ref, "dispatch_ref", maximum=256)
        existing = self.answers.refresh_outcome_for_dispatch(dispatch_ref)
        if existing is not None:
            return {"status": "duplicate", "outcome_receipt": existing}
        row = self.answers.connection.execute(
            "SELECT * FROM answer_refresh_dispatch_receipts WHERE receipt_id=?",
            (dispatch_ref,),
        ).fetchone()
        if row is None:
            raise AnswerRoutingNotFound("answer refresh dispatch was not found")
        dispatch = _load_record(
            row["record_json"], row["content_hash"],
            "AnswerRefreshDispatchReceipt",
        )
        formal = self.scheduler.formal_result(dispatch["work_order_ref"])
        if formal is None or formal["terminal_state"] != "succeeded":
            raise AnswerRoutingConflict(
                "answer refresh WorkOrder has no successful formal result"
            )
        result = formal["result_envelope"]
        matches = result["outputs"].get("matches")
        if not isinstance(matches, list) or not all(
            isinstance(item, Mapping) for item in matches
        ):
            raise AnswerRoutingConflict(
                "answer refresh result lacks machine-readable matches"
            )
        candidate_binding = None
        if matches:
            if candidate_staging_binding is None:
                raise AnswerRoutingConflict(
                    "observed refresh cannot bypass candidate staging"
                )
            candidate_binding = self._candidate_staging_binding(
                candidate_staging_binding,
                result=result,
                result_hash=formal["result_envelope_hash"],
            )
        elif candidate_staging_binding is not None:
            raise AnswerRoutingConflict(
                "no-match refresh cannot attach a candidate-staging binding"
            )
        before_formal = {
            table: self.answers.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        outcome_result = self.control.record_outcome(dispatch["round_ref"])
        outcome = outcome_result["outcome"]
        terminal = self.bounded.terminal(dispatch["loop_version_ref"])
        if terminal is None:
            terminal_proposal = self.bounded.propose_next_capital_lease(
                dispatch["loop_version_ref"]
            )
            if terminal_proposal.get("action", {}).get("kind") != "terminate":
                raise AnswerRoutingConflict(
                    "completed answer refresh did not reach a bounded terminal"
                )
            terminal_result = self.control.admit_proposal(terminal_proposal["id"])
            if terminal_result["status"] != "terminal":
                raise AnswerRoutingConflict(
                    "answer refresh terminal proposal was not admitted"
                )
            terminal = terminal_result["terminal_event"]
        expected_terminal = (
            "evidence_observed_for_review"
            if matches else "coverage_complete_unobservable_candidate"
        )
        if terminal["terminal_state"] != expected_terminal:
            raise AnswerRoutingConflict(
                "answer refresh reached an unexpected terminal state"
            )
        self._inject("after_terminal")
        after_formal = {
            table: self.answers.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in before_formal
        }
        if after_formal != before_formal:
            raise AnswerRoutingConflict(
                "answer refresh wrote formal Evidence, Claim, or Thesis"
            )
        receipt = self.answers._record_refresh_outcome(
            dispatch,
            outcome,
            terminal,
            candidate_binding,
            actor_ref=actor_ref,
        )
        return {"status": receipt["status"], "outcome_receipt": receipt}


__all__ = [
    "ANSWER_REFRESH_OUTPUT_CONTRACT_REF", "ANSWER_REFRESH_VERIFIER_REF",
    "AnswerRefreshControlPlane", "AnswerRoutingAuthority",
    "AnswerRoutingConflict", "AnswerRoutingError",
    "AnswerRoutingNotFound", "AnswerRoutingValidationError",
    "validate_answer_sufficiency_policy",
]
