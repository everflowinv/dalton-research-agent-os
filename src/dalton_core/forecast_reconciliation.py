"""Forecast reconciliation: the first Outcome object of the research OS (P9c).

A ForecastReconciliation binds one exact ForecastLine version to one exact
formal ClaimVersion that reports the actual for the same subject, metric and
fiscal quarter, and records the deviation deterministically.  It is derived
authority: it never edits the forecast line, never edits the Claim and never
touches a Thesis.  When the absolute deviation reaches the frozen
``overturn_candidate`` band the record names the ``forecast_overturn`` human
checkpoint; the forecast is not revised by automation, a human records a
``ForecastOverturnDecision`` and, if they want a new forecast, publishes it
through the existing human forecast-line and model-input operations.

The actual value comes from the formal ClaimVersion itself.  SEC company-facts
Claims freeze their ``normalized_statement`` through the policy auto-commit
rule (``research_auto_commit``): the statement is rendered from the verified
payload with one fixed template, so the inverse parse is validated by
re-rendering the statement byte-for-byte from the parsed fields and comparing
it with the Claim's own hash-bound text.  A Claim whose statement does not
round-trip is not reconciled (fail closed).

Automation reconciliation runs only under a CoverageMission that grants the
``forecast_reconciliation`` write scope and lists ``forecast_overturn`` among
its human checkpoints; the receipt is recorded on every automation record.  A
``human:`` principal may request reconciliation without a mission.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from .model_forecast import validate_forecast_line
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
AUTOMATION_ACTOR = "automation:forecast-reconciler"
WRITE_SCOPE = "forecast_reconciliation"
HUMAN_CHECKPOINT = "forecast_overturn"
CONTRACT_REF = "outcome:forecast-reconciliation:1"
# Frozen metric bindings: which formal Claim shape carries the actual for a
# forecast metric, and which statement field holds the current-period value.
METRIC_BINDINGS: dict[str, dict[str, str]] = {
    "metric:revenue-usd": {
        "claim_metric_or_aspect": "quarterly_revenue_yoy_growth",
        "claim_basis": "official-filing-xbrl",
        "statement_value": "current",
        "currency": "USD",
    },
}
# Absolute deviation_percent thresholds (inclusive lower bounds).
NOTABLE_THRESHOLD_PERCENT = Decimal("1")
OVERTURN_THRESHOLD_PERCENT = Decimal("3")
BANDS: tuple[str, ...] = ("within_tolerance", "notable", "overturn_candidate")
DIRECTIONS: tuple[str, ...] = ("above_forecast", "below_forecast", "in_line")
OVERTURN_DECISIONS: tuple[str, ...] = ("keep_forecast", "revise_forecast")
CONTRACT = {
    "contract_ref": CONTRACT_REF,
    "semantics": (
        "actual = current-period value parsed from the formal ClaimVersion's "
        "frozen SEC company-facts statement and re-rendered byte-for-byte; "
        "actual is rescaled into the forecast line's unit; deviation_absolute = "
        "actual - forecast; deviation_percent = deviation_absolute / forecast * 100; "
        "bands by absolute deviation_percent"
    ),
    "metric_bindings": METRIC_BINDINGS,
    "bands": {
        "within_tolerance": f"< {NOTABLE_THRESHOLD_PERCENT}",
        "notable": f">= {NOTABLE_THRESHOLD_PERCENT} and < {OVERTURN_THRESHOLD_PERCENT}",
        "overturn_candidate": f">= {OVERTURN_THRESHOLD_PERCENT}",
    },
    "human_checkpoint": f"{HUMAN_CHECKPOINT} when band == overturn_candidate",
}
CONTRACT_HASH = content_hash(CONTRACT)
SCALE_FACTORS: dict[str, Decimal] = {
    "one": Decimal(1),
    "thousand": Decimal(10) ** 3,
    "million": Decimal(10) ** 6,
    "billion": Decimal(10) ** 9,
}

_SCHEMA_PATH = Path(__file__).with_name("forecast_reconciliation_schema.sql")
_VALUE_QUANT = Decimal("0.00000001")
_PERCENT_QUANT = Decimal("0.0001")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_AUTOMATION_RE = re.compile(r"^automation:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_STATEMENT_RE = re.compile(
    r"^(?P<entity>.+?) reported (?P<label>.+?) of (?P<unit>[A-Z]{3}) "
    r"(?P<current>-?[0-9]+(?:\.[0-9]+)?) for (?P<start>" + _DATE + r")\.\.(?P<end>" + _DATE + r"), "
    r"(?P<direction>up|down) (?P<growth>[0-9]+(?:\.[0-9]+)?)% year over year from "
    r"(?P<unit2>[A-Z]{3}) (?P<prior>-?[0-9]+(?:\.[0-9]+)?) in the comparable quarter\.$"
)
_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "contract_ref", "contract_hash",
    "subject_ref", "metric_ref", "period", "forecast_line_ref",
    "forecast_line_version_ref", "forecast_line_version_hash", "forecast_value",
    "forecast_value_kind", "model_run_version_ref", "claim_version_ref",
    "claim_version_hash", "claim_metric_or_aspect", "evidence_version_ref",
    "evidence_version_hash", "actual_value", "actual_source", "unit", "currency",
    "deviation_absolute", "deviation_percent", "direction", "band",
    "human_checkpoint", "mission_binding", "requested_by", "actor_ref", "content_hash",
})
_DECISION_FIELDS = frozenset({
    "schema_version", "id", "created_at", "reconciliation_ref", "reconciliation_hash",
    "decision", "rationale", "actor_ref", "content_hash",
})
_MISSION_BINDING_FIELDS = frozenset({
    "mission_version_ref", "mission_version_hash", "mission_ref", "automation_principal",
})


class ForecastReconciliationError(RuntimeError):
    """Base error for the forecast reconciliation authority."""


class ForecastReconciliationValidationError(ForecastReconciliationError):
    """A request does not satisfy the closed contract."""


class ForecastReconciliationConflict(ForecastReconciliationError):
    """A request conflicts with immutable authority."""


class ForecastReconciliationNotFound(ForecastReconciliationError):
    """A bound forecast line, Claim or reconciliation is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForecastReconciliationValidationError(f"{name} must be non-empty text")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise ForecastReconciliationValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise ForecastReconciliationValidationError(f"{name} must use the human: namespace")
    return value


def _principal(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None and _AUTOMATION_RE.fullmatch(value) is None:
        raise ForecastReconciliationValidationError(
            f"{name} must use the human: or automation: namespace"
        )
    return value


def _decimal(value: Any, name: str) -> Decimal:
    # ClaimVersion 0.1 stores quantitative values as JSON numbers; 0.2 and
    # every record written here use decimal strings.
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ForecastReconciliationValidationError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive
        raise ForecastReconciliationValidationError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ForecastReconciliationValidationError(f"{name} must be finite")
    return parsed


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    return text


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ForecastReconciliationConflict(f"{name} record is missing")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForecastReconciliationConflict(f"{name} record is invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ForecastReconciliationConflict(f"{name} record is not canonical")
    return value


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def render_company_facts_statement(
    *, entity: str, label: str, unit: str, current: str, start: str, end: str,
    direction: str, growth: str, prior: str,
) -> str:
    """The frozen SEC company-facts statement template (see research_auto_commit)."""

    return (
        f"{entity} reported {label} of {unit} {current} for {start}..{end}, "
        f"{direction} {growth}% year over year from {unit} {prior} in the comparable quarter."
    )


def parse_company_facts_claim(claim: Mapping[str, Any]) -> dict[str, Any]:
    """Recover the current-period actual from a formal SEC company-facts Claim.

    Fails closed unless the statement re-renders byte-for-byte from the parsed
    fields and agrees with the Claim's own value, unit and period.
    """

    statement = _text(claim.get("normalized_statement"), "normalized_statement")
    match = _STATEMENT_RE.fullmatch(statement)
    if match is None:
        raise ForecastReconciliationValidationError(
            "claim statement is not the frozen SEC company-facts template"
        )
    fields = match.groupdict()
    if fields["unit"] != fields["unit2"]:
        raise ForecastReconciliationValidationError("claim statement units disagree")
    rendered = render_company_facts_statement(
        entity=fields["entity"], label=fields["label"], unit=fields["unit"],
        current=fields["current"], start=fields["start"], end=fields["end"],
        direction=fields["direction"], growth=fields["growth"], prior=fields["prior"],
    )
    if rendered != statement:
        raise ForecastReconciliationValidationError(
            "claim statement does not round-trip through the frozen template"
        )
    expected_value = ("-" if fields["direction"] == "down" else "") + fields["growth"]
    period = f"{fields['start']}..{fields['end']}"
    # ClaimVersion 0.1 rows carry no ``scale``; 0.2 SEC rows freeze it to "one".
    if (
        claim.get("claim_kind") != "quantitative"
        or claim.get("unit") != "percent"
        or claim.get("scale") not in (None, "one")
        or claim.get("period") != period
        or _decimal(claim.get("value"), "claim value") != _decimal(expected_value, "statement growth")
    ):
        raise ForecastReconciliationValidationError(
            "claim value, unit or period disagree with its statement"
        )
    return {
        "entity": fields["entity"],
        "label": fields["label"],
        "currency": fields["unit"],
        "current": fields["current"],
        "prior": fields["prior"],
        "start": fields["start"],
        "end": fields["end"],
        "growth_percent": expected_value,
    }


def _mission_binding(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _MISSION_BINDING_FIELDS:
        raise ForecastReconciliationValidationError(
            "mission_binding has an invalid closed shape"
        )
    return {
        "mission_version_ref": _text(value["mission_version_ref"], "mission_binding.mission_version_ref"),
        "mission_version_hash": _sha256(
            value["mission_version_hash"], "mission_binding.mission_version_hash"
        ),
        "mission_ref": _text(value["mission_ref"], "mission_binding.mission_ref"),
        "automation_principal": _text(
            value["automation_principal"], "mission_binding.automation_principal"
        ),
    }


def validate_forecast_reconciliation(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _RECORD_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ForecastReconciliationValidationError(
            "forecast reconciliation has an invalid closed shape"
        )
    for field in (
        "id", "created_at", "subject_ref", "metric_ref", "forecast_line_ref",
        "forecast_line_version_ref", "forecast_value_kind", "claim_version_ref",
        "claim_metric_or_aspect", "evidence_version_ref", "unit", "currency",
    ):
        wire[field] = _text(wire[field], field)
    if wire["contract_ref"] != CONTRACT_REF or wire["contract_hash"] != CONTRACT_HASH:
        raise ForecastReconciliationValidationError(
            "forecast reconciliation must bind the frozen outcome contract"
        )
    for field in ("forecast_line_version_hash", "claim_version_hash", "evidence_version_hash"):
        wire[field] = _sha256(wire[field], field)
    if wire["model_run_version_ref"] is not None:
        wire["model_run_version_ref"] = _text(wire["model_run_version_ref"], "model_run_version_ref")
    period = wire["period"]
    if not isinstance(period, Mapping) or set(period) != {"start", "end", "calendar", "kind"}:
        raise ForecastReconciliationValidationError("period must be a closed quarter period")
    wire["period"] = {key: _text(period[key], f"period.{key}") for key in ("start", "end", "calendar", "kind")}
    forecast = _decimal(wire["forecast_value"], "forecast_value")
    actual = _decimal(wire["actual_value"], "actual_value")
    deviation = _decimal(wire["deviation_absolute"], "deviation_absolute")
    percent = _decimal(wire["deviation_percent"], "deviation_percent")
    if forecast == 0:
        raise ForecastReconciliationValidationError("forecast_value must be non-zero")
    if deviation != (actual - forecast).quantize(_VALUE_QUANT, ROUND_HALF_UP):
        raise ForecastReconciliationValidationError("deviation_absolute does not replay")
    if percent != (deviation / forecast * Decimal(100)).quantize(_PERCENT_QUANT, ROUND_HALF_UP):
        raise ForecastReconciliationValidationError("deviation_percent does not replay")
    if wire["direction"] != _direction(deviation) or wire["band"] != _band(percent):
        raise ForecastReconciliationValidationError("direction or band does not replay")
    expected_checkpoint = HUMAN_CHECKPOINT if wire["band"] == "overturn_candidate" else None
    if wire["human_checkpoint"] != expected_checkpoint:
        raise ForecastReconciliationValidationError("human_checkpoint does not replay")
    source = wire["actual_source"]
    if not isinstance(source, Mapping) or set(source) != {
        "kind", "field", "raw_value", "raw_currency", "raw_scale",
    } or source["kind"] != "claim_normalized_statement":
        raise ForecastReconciliationValidationError("actual_source has an invalid closed shape")
    wire["actual_source"] = {key: _text(source[key], f"actual_source.{key}") for key in sorted(source)}
    wire["mission_binding"] = _mission_binding(wire["mission_binding"])
    wire["requested_by"] = _principal(wire["requested_by"], "requested_by")
    wire["actor_ref"] = _text(wire["actor_ref"], "actor_ref")
    if wire["actor_ref"] != AUTOMATION_ACTOR:
        raise ForecastReconciliationValidationError(
            "forecast reconciliations are derived by the reconciler automation actor"
        )
    if wire["mission_binding"] is None and not wire["requested_by"].startswith("human:"):
        raise ForecastReconciliationValidationError(
            "automation reconciliation must carry its mission binding"
        )
    if wire["mission_binding"] is not None and wire["requested_by"] != wire["mission_binding"]["automation_principal"]:
        raise ForecastReconciliationValidationError(
            "mission reconciliation must be requested by the mission principal"
        )
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected = base.pop("content_hash")
    if content_hash(base) != expected:
        raise ForecastReconciliationValidationError("forecast reconciliation content_hash is invalid")
    return wire


def validate_forecast_overturn_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _DECISION_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ForecastReconciliationValidationError(
            "forecast overturn decision has an invalid closed shape"
        )
    for field in ("id", "created_at", "reconciliation_ref", "rationale"):
        wire[field] = _text(wire[field], field)
    wire["reconciliation_hash"] = _sha256(wire["reconciliation_hash"], "reconciliation_hash")
    if wire["decision"] not in OVERTURN_DECISIONS:
        raise ForecastReconciliationValidationError(
            f"decision must be one of {list(OVERTURN_DECISIONS)}"
        )
    wire["actor_ref"] = _human(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected = base.pop("content_hash")
    if content_hash(base) != expected:
        raise ForecastReconciliationValidationError("forecast overturn decision content_hash is invalid")
    return wire


def _direction(deviation: Decimal) -> str:
    if deviation > 0:
        return "above_forecast"
    if deviation < 0:
        return "below_forecast"
    return "in_line"


def _band(percent: Decimal) -> str:
    magnitude = abs(percent)
    if magnitude >= OVERTURN_THRESHOLD_PERCENT:
        return "overturn_candidate"
    if magnitude >= NOTABLE_THRESHOLD_PERCENT:
        return "notable"
    return "within_tolerance"


class ForecastReconciliationAuthority:
    """Append-only forecast reconciliations and human overturn decisions."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_forecast_reconciliation_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ForecastReconciliationAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- exact reads of the two bound authorities ---------------------------

    def _latest_forecast_lines(self, company_ref: str | None) -> list[dict[str, Any]]:
        if not _table_exists(self.connection, "model_forecast_line_versions"):
            return []
        query = (
            "SELECT v.* FROM model_forecast_line_versions v JOIN ("
            "SELECT line_ref, MAX(version_number) AS version_number "
            "FROM model_forecast_line_versions GROUP BY line_ref) latest "
            "ON latest.line_ref=v.line_ref AND latest.version_number=v.version_number"
        )
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE v.subject_ref=?"
            params.append(company_ref)
        query += " ORDER BY v.line_ref"
        lines = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_forecast_line(json.loads(row["record_json"]))
            if wire["id"] != row["version_id"] or wire["content_hash"] != row["content_hash"]:
                raise ForecastReconciliationConflict("forecast line authority drifted")
            if wire["metric_or_aspect"] in METRIC_BINDINGS:
                lines.append(wire)
        return lines

    def _latest_claims(self, subject_ref: str, metric_or_aspect: str, period: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT c.* FROM claim_versions c JOIN ("
            "SELECT claim_ref, MAX(version_number) AS version_number FROM claim_versions "
            "GROUP BY claim_ref) latest ON latest.claim_ref=c.claim_ref "
            "AND latest.version_number=c.version_number "
            "WHERE json_extract(c.claim_json,'$.subject_ref')=? "
            "AND json_extract(c.claim_json,'$.metric_or_aspect')=? "
            "AND json_extract(c.claim_json,'$.period')=? "
            "ORDER BY c.created_at, c.claim_version_id",
            (subject_ref, metric_or_aspect, period),
        ).fetchall()
        claims = []
        for row in rows:
            claim = json.loads(row["claim_json"])
            if claim.get("id") != row["claim_version_id"] or claim.get("content_hash") != row["content_hash"]:
                raise ForecastReconciliationConflict("claim authority drifted")
            claims.append(claim)
        return claims

    def _supporting_evidence(self, cur: sqlite3.Cursor | sqlite3.Connection, claim_version_ref: str) -> tuple[str, str]:
        row = cur.execute(
            "SELECT r.evidence_version_id, e.content_hash FROM evidence_relations r "
            "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
            "WHERE r.claim_version_id=? AND r.relation='supports' "
            "ORDER BY r.created_at, r.relation_id LIMIT 1",
            (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ForecastReconciliationValidationError(
                "formal claim has no supporting EvidenceVersion"
            )
        return row["evidence_version_id"], row["content_hash"]

    # -- candidate discovery --------------------------------------------------

    def pending_pairs(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        """Latest forecast lines whose period now has a formal actual Claim."""

        if company_ref is not None:
            company_ref = _text(company_ref, "company_ref")
        pairs: list[dict[str, Any]] = []
        for line in self._latest_forecast_lines(company_ref):
            binding = METRIC_BINDINGS[line["metric_or_aspect"]]
            period = f"{line['period']['start']}..{line['period']['end']}"
            for claim in self._latest_claims(
                line["subject_ref"], binding["claim_metric_or_aspect"], period
            ):
                exists = self.connection.execute(
                    "SELECT 1 FROM forecast_reconciliations "
                    "WHERE forecast_line_version_ref=? AND claim_version_ref=?",
                    (line["id"], claim["id"]),
                ).fetchone()
                if exists is not None:
                    continue
                pairs.append({
                    "company_ref": line["subject_ref"],
                    "metric_ref": line["metric_or_aspect"],
                    "period": period,
                    "forecast_line_version_ref": line["id"],
                    "forecast_line_version_hash": line["content_hash"],
                    "claim_version_ref": claim["id"],
                    "claim_version_hash": claim["content_hash"],
                })
        return pairs

    def pairs_for_claim(self, claim_version_ref: str) -> list[dict[str, Any]]:
        claim_version_ref = _text(claim_version_ref, "claim_version_ref")
        row = self.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ForecastReconciliationNotFound("claim version was not found")
        subject = json.loads(row["claim_json"]).get("subject_ref")
        return [
            pair for pair in self.pending_pairs(subject)
            if pair["claim_version_ref"] == claim_version_ref
        ]

    # -- reconciliation -------------------------------------------------------

    def reconcile(
        self,
        *,
        forecast_line_version_ref: str,
        claim_version_ref: str,
        requested_by: str,
        mission_binding: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        forecast_line_version_ref = _text(forecast_line_version_ref, "forecast_line_version_ref")
        claim_version_ref = _text(claim_version_ref, "claim_version_ref")
        requested_by = _principal(requested_by, "requested_by")
        binding = _mission_binding(mission_binding)
        if binding is None and not requested_by.startswith("human:"):
            raise ForecastReconciliationValidationError(
                "automation reconciliation requires a CoverageMission grant"
            )
        if binding is not None and requested_by != binding["automation_principal"]:
            raise ForecastReconciliationValidationError(
                "mission reconciliation must be requested by the mission principal"
            )
        identity = {
            "forecast_line_version_ref": forecast_line_version_ref,
            "claim_version_ref": claim_version_ref,
            "contract_ref": CONTRACT_REF,
        }
        reconciliation_id = f"forecast-reconciliation:{content_hash(identity)[:32]}"
        existing = self.connection.execute(
            "SELECT record_json FROM forecast_reconciliations WHERE reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        if existing is not None:
            return {
                **validate_forecast_reconciliation(
                    _canonical_record(existing["record_json"], "forecast reconciliation")
                ),
                "status": "duplicate",
            }
        line_row = self.connection.execute(
            "SELECT * FROM model_forecast_line_versions WHERE version_id=?",
            (forecast_line_version_ref,),
        ).fetchone() if _table_exists(self.connection, "model_forecast_line_versions") else None
        if line_row is None:
            raise ForecastReconciliationNotFound("forecast line version was not found")
        line = validate_forecast_line(json.loads(line_row["record_json"]))
        if line["content_hash"] != line_row["content_hash"]:
            raise ForecastReconciliationConflict("forecast line authority drifted")
        latest_line = self.connection.execute(
            "SELECT version_id FROM model_forecast_line_versions WHERE line_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (line["line_ref"],),
        ).fetchone()
        if latest_line["version_id"] != line["id"]:
            raise ForecastReconciliationConflict("only the latest forecast line version is reconciled")
        binding_spec = METRIC_BINDINGS.get(line["metric_or_aspect"])
        if binding_spec is None:
            raise ForecastReconciliationValidationError(
                "forecast metric has no frozen actual binding"
            )
        claim_view = self.store.get_claim(claim_version_ref)
        if claim_view is None:
            raise ForecastReconciliationNotFound("claim version was not found")
        claim = claim_view["claim"]
        if claim.get("content_hash") != claim_view["content_hash"]:
            raise ForecastReconciliationConflict("claim authority drifted")
        latest_claim = self.connection.execute(
            "SELECT claim_version_id FROM claim_versions WHERE claim_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (claim_view["claim_ref"],),
        ).fetchone()
        if latest_claim["claim_version_id"] != claim_version_ref:
            raise ForecastReconciliationConflict("only the latest claim version is reconciled")
        if (
            claim.get("subject_ref") != line["subject_ref"]
            or claim.get("metric_or_aspect") != binding_spec["claim_metric_or_aspect"]
            or claim.get("basis") != binding_spec["claim_basis"]
        ):
            raise ForecastReconciliationValidationError(
                "claim does not carry the actual for this forecast line"
            )
        parsed = parse_company_facts_claim(claim)
        period = f"{line['period']['start']}..{line['period']['end']}"
        if claim["period"] != period:
            raise ForecastReconciliationValidationError(
                "claim period does not match the forecast line period"
            )
        if parsed["currency"] != line["currency"] or parsed["currency"] != binding_spec["currency"]:
            raise ForecastReconciliationValidationError(
                "claim currency does not match the forecast line currency"
            )
        scale = SCALE_FACTORS.get(line["unit"])
        if scale is None:
            raise ForecastReconciliationValidationError(
                "forecast line unit is outside the frozen scale table"
            )
        raw_actual = _decimal(parsed["current"], "actual")
        actual = (raw_actual / scale).quantize(_VALUE_QUANT, ROUND_HALF_UP)
        forecast = _decimal(line["value"], "forecast value")
        if forecast == 0:
            raise ForecastReconciliationValidationError("forecast_value must be non-zero")
        deviation = (actual - forecast).quantize(_VALUE_QUANT, ROUND_HALF_UP)
        percent = (deviation / forecast * Decimal(100)).quantize(_PERCENT_QUANT, ROUND_HALF_UP)
        band = _band(percent)
        evidence_ref, evidence_hash = self._supporting_evidence(self.connection, claim_version_ref)
        created_at = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": reconciliation_id,
            "created_at": created_at,
            "contract_ref": CONTRACT_REF,
            "contract_hash": CONTRACT_HASH,
            "subject_ref": line["subject_ref"],
            "metric_ref": line["metric_or_aspect"],
            "period": dict(line["period"]),
            "forecast_line_ref": line["line_ref"],
            "forecast_line_version_ref": line["id"],
            "forecast_line_version_hash": line["content_hash"],
            "forecast_value": _plain(forecast),
            "forecast_value_kind": line["value_kind"],
            "model_run_version_ref": line["model_run_version_ref"],
            "claim_version_ref": claim["id"],
            "claim_version_hash": claim["content_hash"],
            "claim_metric_or_aspect": claim["metric_or_aspect"],
            "evidence_version_ref": evidence_ref,
            "evidence_version_hash": evidence_hash,
            "actual_value": _plain(actual),
            "actual_source": {
                "field": binding_spec["statement_value"],
                "kind": "claim_normalized_statement",
                "raw_currency": parsed["currency"],
                "raw_scale": "one",
                "raw_value": parsed["current"],
            },
            "unit": line["unit"],
            "currency": line["currency"],
            "deviation_absolute": _plain(deviation),
            "deviation_percent": _plain(percent),
            "direction": _direction(deviation),
            "band": band,
            "human_checkpoint": HUMAN_CHECKPOINT if band == "overturn_candidate" else None,
            "mission_binding": binding,
            "requested_by": requested_by,
            "actor_ref": AUTOMATION_ACTOR,
        }
        wire = dict(record)
        wire["content_hash"] = content_hash(record)
        wire = validate_forecast_reconciliation(wire)
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM forecast_reconciliations WHERE reconciliation_id=?",
                (reconciliation_id,),
            ).fetchone():
                raise ForecastReconciliationConflict("forecast reconciliation was written concurrently")
            cur.execute(
                "INSERT INTO forecast_reconciliations"
                "(reconciliation_id,subject_ref,metric_ref,period_start,period_end,"
                "forecast_line_ref,forecast_line_version_ref,forecast_line_version_hash,"
                "claim_version_ref,claim_version_hash,band,human_checkpoint,mission_version_ref,"
                "requested_by,actor_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    reconciliation_id, wire["subject_ref"], wire["metric_ref"],
                    wire["period"]["start"], wire["period"]["end"], wire["forecast_line_ref"],
                    wire["forecast_line_version_ref"], wire["forecast_line_version_hash"],
                    wire["claim_version_ref"], wire["claim_version_hash"], wire["band"],
                    wire["human_checkpoint"],
                    None if binding is None else binding["mission_version_ref"],
                    wire["requested_by"], wire["actor_ref"], canonical_json(wire),
                    wire["content_hash"], created_at,
                ),
            )
        return {**wire, "status": "fresh"}

    def reconcile_pending(
        self,
        *,
        requested_by: str,
        mission_resolver: Callable[[str], Mapping[str, Any]] | None,
        company_ref: str | None = None,
        claim_version_ref: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile every pending pair the caller is authorized for.

        ``mission_resolver`` maps a company_ref to a mission binding for the
        automation path (raising when the mission does not grant the scope).
        Human callers pass ``None`` and are recorded as ``requested_by``.
        Skips are reported, never hidden.
        """

        requested_by = _principal(requested_by, "requested_by")
        if requested_by.startswith("human:"):
            mission_resolver = None
        elif mission_resolver is None:
            raise ForecastReconciliationValidationError(
                "automation reconciliation requires a mission resolver"
            )
        pairs = (
            self.pairs_for_claim(claim_version_ref)
            if claim_version_ref is not None else self.pending_pairs(company_ref)
        )
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for pair in pairs:
            binding = None
            actor = requested_by
            if mission_resolver is not None:
                try:
                    resolved = mission_resolver(pair["company_ref"])
                except Exception as exc:  # the resolver's own error types
                    skipped.append({**pair, "reason": f"{type(exc).__name__}: {exc}"})
                    continue
                binding = {
                    "mission_version_ref": resolved["mission_version_ref"],
                    "mission_version_hash": resolved["mission_version_hash"],
                    "mission_ref": resolved["mission_ref"],
                    "automation_principal": resolved["actor_ref"],
                }
                actor = resolved["actor_ref"]
            try:
                created.append(self.reconcile(
                    forecast_line_version_ref=pair["forecast_line_version_ref"],
                    claim_version_ref=pair["claim_version_ref"],
                    requested_by=actor,
                    mission_binding=binding,
                ))
            except ForecastReconciliationError as exc:
                skipped.append({**pair, "reason": f"{type(exc).__name__}: {exc}"})
        return {
            "status": "reconciled" if created else ("idle" if not skipped else "skipped"),
            "created": created,
            "skipped": skipped,
        }

    # -- reads -----------------------------------------------------------------

    def _read_row(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise ForecastReconciliationNotFound("forecast reconciliation was not found")
        wire = validate_forecast_reconciliation(
            _canonical_record(row["record_json"], "forecast reconciliation")
        )
        if (
            wire["id"] != row["reconciliation_id"]
            or wire["content_hash"] != row["content_hash"]
            or wire["forecast_line_version_ref"] != row["forecast_line_version_ref"]
            or wire["claim_version_ref"] != row["claim_version_ref"]
            or wire["band"] != row["band"]
            or wire["created_at"] != row["created_at"]
        ):
            raise ForecastReconciliationConflict("forecast reconciliation authority drifted")
        return wire

    def checkpoint_status(self, reconciliation_ref: str) -> str:
        row = self.connection.execute(
            "SELECT human_checkpoint FROM forecast_reconciliations WHERE reconciliation_id=?",
            (reconciliation_ref,),
        ).fetchone()
        if row is None:
            raise ForecastReconciliationNotFound("forecast reconciliation was not found")
        if row["human_checkpoint"] is None:
            return "not_required"
        decision = self.connection.execute(
            "SELECT decision FROM forecast_overturn_decisions WHERE reconciliation_ref=?",
            (reconciliation_ref,),
        ).fetchone()
        if decision is None:
            return "pending_human"
        return f"decided:{decision['decision']}"

    def reconciliation(self, reconciliation_ref: str) -> dict[str, Any]:
        reconciliation_ref = _text(reconciliation_ref, "reconciliation_ref")
        wire = self._read_row(self.connection.execute(
            "SELECT * FROM forecast_reconciliations WHERE reconciliation_id=?",
            (reconciliation_ref,),
        ).fetchone())
        return {**wire, "checkpoint_status": self.checkpoint_status(reconciliation_ref)}

    def reconciliations(
        self,
        *,
        company_ref: str | None = None,
        claim_version_ref: str | None = None,
        forecast_line_ref: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if company_ref is not None:
            clauses.append("subject_ref=?")
            params.append(_text(company_ref, "company_ref"))
        if claim_version_ref is not None:
            clauses.append("claim_version_ref=?")
            params.append(_text(claim_version_ref, "claim_version_ref"))
        if forecast_line_ref is not None:
            clauses.append("forecast_line_ref=?")
            params.append(_text(forecast_line_ref, "forecast_line_ref"))
        if created_from is not None:
            clauses.append("created_at>=?")
            params.append(_text(created_from, "created_from"))
        if created_to is not None:
            clauses.append("created_at<=?")
            params.append(_text(created_to, "created_to"))
        query = "SELECT * FROM forecast_reconciliations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, reconciliation_id"
        return [
            {**self._read_row(row), "checkpoint_status": self.checkpoint_status(row["reconciliation_id"])}
            for row in self.connection.execute(query, params).fetchall()
        ]

    # -- human checkpoint --------------------------------------------------------

    def decide_overturn(
        self,
        *,
        reconciliation_ref: str,
        reconciliation_hash: str,
        decision: str,
        rationale: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        reconciliation_ref = _text(reconciliation_ref, "reconciliation_ref")
        reconciliation_hash = _sha256(reconciliation_hash, "reconciliation_hash")
        decision = _text(decision, "decision")
        if decision not in OVERTURN_DECISIONS:
            raise ForecastReconciliationValidationError(
                f"decision must be one of {list(OVERTURN_DECISIONS)}"
            )
        rationale = _text(rationale, "rationale")
        actor_ref = _human(actor_ref)
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "reconciliation_ref": reconciliation_ref,
            "reconciliation_hash": reconciliation_hash,
            "decision": decision, "rationale": rationale, "actor_ref": actor_ref,
        }
        request_hash = content_hash({"operation": "decide_overturn", "request": request})
        with self._transaction() as cur:
            prior = cur.execute(
                "SELECT * FROM forecast_reconciliation_idempotency WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["operation"] != "decide_overturn" or prior["request_hash"] != request_hash:
                    raise ForecastReconciliationConflict("idempotency key conflicts with prior request")
                return {**json.loads(prior["result_json"]), "status": "duplicate"}
            reconciliation = self._read_row(cur.execute(
                "SELECT * FROM forecast_reconciliations WHERE reconciliation_id=?",
                (reconciliation_ref,),
            ).fetchone())
            if reconciliation["content_hash"] != reconciliation_hash:
                raise ForecastReconciliationConflict("reconciliation hash binding failed")
            if reconciliation["human_checkpoint"] != HUMAN_CHECKPOINT:
                raise ForecastReconciliationConflict(
                    "reconciliation did not raise the forecast_overturn checkpoint"
                )
            if cur.execute(
                "SELECT 1 FROM forecast_overturn_decisions WHERE reconciliation_ref=?",
                (reconciliation_ref,),
            ).fetchone():
                raise ForecastReconciliationConflict("forecast overturn was already decided")
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": f"forecast-overturn-decision:{content_hash(request)[:32]}",
                "created_at": created_at,
                **request,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            wire = validate_forecast_overturn_decision(wire)
            cur.execute(
                "INSERT INTO forecast_overturn_decisions"
                "(decision_id,reconciliation_ref,reconciliation_hash,decision,actor_ref,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    wire["id"], reconciliation_ref, reconciliation_hash, decision, actor_ref,
                    canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            result = {**wire, "status": "fresh"}
            cur.execute(
                "INSERT INTO forecast_reconciliation_idempotency"
                "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
                (idempotency_key, "decide_overturn", request_hash, canonical_json(result), created_at),
            )
            return result

    def overturn_decision(self, reconciliation_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM forecast_overturn_decisions WHERE reconciliation_ref=?",
            (_text(reconciliation_ref, "reconciliation_ref"),),
        ).fetchone()
        if row is None:
            return None
        wire = validate_forecast_overturn_decision(
            _canonical_record(row["record_json"], "forecast overturn decision")
        )
        if wire["id"] != row["decision_id"] or wire["content_hash"] != row["content_hash"]:
            raise ForecastReconciliationConflict("forecast overturn decision authority drifted")
        return wire

    # -- integrity -----------------------------------------------------------------

    def integrity_report(self) -> dict[str, Any]:
        issues: list[str] = []
        count = 0
        for row in self.connection.execute("SELECT * FROM forecast_reconciliations").fetchall():
            count += 1
            try:
                wire = self._read_row(row)
            except ForecastReconciliationError as exc:
                issues.append(f"forecast_reconciliations: {exc}")
                continue
            line = self.connection.execute(
                "SELECT content_hash FROM model_forecast_line_versions WHERE version_id=?",
                (wire["forecast_line_version_ref"],),
            ).fetchone()
            if line is None or line["content_hash"] != wire["forecast_line_version_hash"]:
                issues.append(f"{wire['id']}: forecast line binding drifted")
            claim = self.connection.execute(
                "SELECT content_hash FROM claim_versions WHERE claim_version_id=?",
                (wire["claim_version_ref"],),
            ).fetchone()
            if claim is None or claim["content_hash"] != wire["claim_version_hash"]:
                issues.append(f"{wire['id']}: claim binding drifted")
        decisions = 0
        for row in self.connection.execute("SELECT * FROM forecast_overturn_decisions").fetchall():
            decisions += 1
            try:
                wire = validate_forecast_overturn_decision(
                    _canonical_record(row["record_json"], "forecast overturn decision")
                )
            except ForecastReconciliationError as exc:
                issues.append(f"forecast_overturn_decisions: {exc}")
                continue
            if wire["content_hash"] != row["content_hash"]:
                issues.append(f"{wire['id']}: row hash mismatch")
        return {
            "status": "ok" if not issues else "issues",
            "reconciliation_count": count,
            "decision_count": decisions,
            "issues": issues,
        }


__all__ = [
    "AUTOMATION_ACTOR",
    "BANDS",
    "CONTRACT",
    "CONTRACT_HASH",
    "CONTRACT_REF",
    "DIRECTIONS",
    "HUMAN_CHECKPOINT",
    "METRIC_BINDINGS",
    "OVERTURN_DECISIONS",
    "WRITE_SCOPE",
    "ForecastReconciliationAuthority",
    "ForecastReconciliationConflict",
    "ForecastReconciliationError",
    "ForecastReconciliationNotFound",
    "ForecastReconciliationValidationError",
    "parse_company_facts_claim",
    "render_company_facts_statement",
    "validate_forecast_overturn_decision",
    "validate_forecast_reconciliation",
]
