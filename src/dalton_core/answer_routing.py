"""Deterministic, read-only ad-hoc answer context and routing.

S4 deliberately supports only two routes.  An exact, already-answered
ResearchQuestion may route to ``answer_direct`` when every Core-derived policy
gate passes.  Everything else routes to ``recommend_agenda_item``.  The module
does not call a model, execute a connector, create a WorkOrder, or write any
Evidence, Claim, Thesis, Agenda item, or answer artifact.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .agenda import AgendaStore, read_exact_mandate_version
from .bounded_planner_loop import BoundedPlannerAuthority
from .industry_research import IndustryResearchAuthority
from .research_question_backlog import (
    ResearchQuestionBacklog,
    question_ref_for,
)
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
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


def _route_budget(value: Any, kind: str) -> dict[str, Any]:
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
    if wire["enabled"] or wire["max_cost_units"] != 0:
        raise AnswerRoutingValidationError(
            f"{kind} route must remain disabled in S4 v0.1"
        )
    if kind == "refresh" and wire["probe_template_bindings"]:
        raise AnswerRoutingValidationError(
            "refresh bindings require a later enabled route version"
        )
    if kind == "adhoc" and wire["max_rounds"] != 0:
        raise AnswerRoutingValidationError(
            "ad-hoc rounds require a later enabled route version"
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
    if wire["schema_version"] != SCHEMA_VERSION:
        raise AnswerRoutingValidationError("answer policy schema version is unsupported")
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
    wire["refresh_route"] = _route_budget(wire["refresh_route"], "refresh")
    wire["adhoc_research_route"] = _route_budget(
        wire["adhoc_research_route"], "adhoc"
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
            except (TypeError, json.JSONDecodeError) as exc:
                raise AnswerRoutingConflict("current ThesisVersion is invalid") from exc
            if (
                canonical_json(content) != row["content_json"]
                or content_hash(content) != row["content_hash"]
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
            ordered_reasons = [
                reason for reason in _ROUTE_REASON_ORDER if reason in reasons
            ]
            route = "answer_direct" if not ordered_reasons else "recommend_agenda_item"
            decision = _record({
                "schema_version": SCHEMA_VERSION,
                "id": f"answer-route-decision:{content_hash({'context': pack['id'], 'route': route})}",
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
                "refresh_route_available": False,
                "adhoc_research_route_available": False,
                "agenda_recommendation": (
                    None if route == "answer_direct" else {
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


__all__ = [
    "AnswerRoutingAuthority", "AnswerRoutingConflict", "AnswerRoutingError",
    "AnswerRoutingNotFound", "AnswerRoutingValidationError",
    "validate_answer_sufficiency_policy",
]
