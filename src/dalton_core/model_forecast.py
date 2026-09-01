"""Versioned forecast line authority and a deterministic growth-extension.

This is M1 of the modeling engine: research gains a formal, versioned place
for forecast lines with the SPEC's frozen value-kind vocabulary
(observed / assumption / derived_deterministic / estimate / simulation).
Lines bind the exact Model Input Ledger versions (scenario, base actual,
growth assumption) they were derived from; ``derived_deterministic`` lines are
only writable with the closed ``quarterly-growth-extend:1`` formula contract
and re-validated input hashes, so automation can extend an admitted model but
never invent its inputs.  Human lines (assumption / estimate / simulation)
carry a rationale and remain human-only at the writer boundary.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from .model_input import ModelInputConflict, ModelInputLedger, ModelInputNotFound
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
FORECAST_VALUE_KINDS = frozenset({
    "observed", "assumption", "derived_deterministic", "estimate", "simulation",
})
HUMAN_LINE_KINDS = frozenset({"observed", "assumption", "estimate", "simulation"})
FORMULA_REF = "formula:quarterly-growth-extend:1"
FORMULA_HASH = content_hash({
    "formula_ref": FORMULA_REF,
    "semantics": (
        "line[k].value = base_actual.value * (1 + growth.value) ** k for the "
        "k consecutive fiscal quarters immediately after the base actual's "
        "period; sequential compounding, seasonality carried by the growth "
        "assumption"
    ),
    "value_kind": "derived_deterministic",
})
AUTOMATION_ACTOR = "automation:forecast-extender"

_SCHEMA_PATH = Path(__file__).with_name("model_forecast_schema.sql")
_QUANT = Decimal("0.00000001")


class ModelForecastError(RuntimeError):
    """Base error for the forecast line authority."""


class ModelForecastValidationError(ModelForecastError):
    """A request does not satisfy the closed contract."""


class ModelForecastConflict(ModelForecastError):
    """A request conflicts with immutable authority."""


class ModelForecastNotFound(ModelForecastError):
    """A bound input, run or forecast line version is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelForecastValidationError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ModelForecastValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ModelForecastValidationError(f"{name} must be a decimal value")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ModelForecastValidationError(f"{name} must be a decimal value") from exc
    if not parsed.is_finite():
        raise ModelForecastValidationError(f"{name} must be finite")
    return parsed


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return datetime(year, month, min(value.day, days[month - 1]))


def _quarter_after(period: Mapping[str, Any]) -> dict[str, str]:
    end = datetime.fromisoformat(str(period["end"]))
    next_start = end + timedelta(days=1)
    next_end = _add_months(next_start, 3) - timedelta(days=1)
    return {
        "start": next_start.date().isoformat(),
        "end": next_end.date().isoformat(),
        "calendar": str(period.get("calendar", "company:fiscal")),
        "kind": "quarter",
    }


def _quarter_period(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "start", "end", "calendar", "kind",
    }:
        raise ModelForecastValidationError(f"{name} must be a closed quarter period")
    wire = {
        "start": _text(value["start"], f"{name}.start"),
        "end": _text(value["end"], f"{name}.end"),
        "calendar": _text(value["calendar"], f"{name}.calendar"),
        "kind": _text(value["kind"], f"{name}.kind"),
    }
    if wire["kind"] != "quarter":
        raise ModelForecastValidationError(f"{name}.kind must be quarter")
    try:
        start = datetime.fromisoformat(wire["start"])
        end = datetime.fromisoformat(wire["end"])
    except ValueError as exc:
        raise ModelForecastValidationError(f"{name} dates must be ISO dates") from exc
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ModelForecastValidationError(f"{name} dates must be naive dates")
    if not start < end:
        raise ModelForecastValidationError(f"{name} start must precede end")
    return wire


def _human(value: Any, name: str) -> str:
    value = _text(value, name)
    if not value.startswith("human:") or len(value) == len("human:"):
        raise ModelForecastValidationError(f"{name} must use the human: namespace")
    return value


def _automation_or_human(value: Any, name: str) -> str:
    value = _text(value, name)
    if not (
        value.startswith("human:") or value.startswith("automation:")
    ) or value.count(":") < 1 or len(value.split(":", 1)[1]) == 0:
        raise ModelForecastValidationError(f"{name} must use a principal namespace")
    return value


def _normalize_line(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "line_ref", "version",
        "prior_version_ref", "subject_ref", "metric_or_aspect", "period",
        "unit", "currency", "value", "value_kind",
        "scenario_version_ref", "scenario_version_hash",
        "base_input_version_ref", "base_input_version_hash",
        "growth_input_version_ref", "growth_input_version_hash",
        "formula_ref", "formula_hash", "model_run_version_ref",
        "actor_ref", "rationale", "content_hash",
    }
    wire = dict(value)
    if set(wire) - {"content_hash"} != fields - {"content_hash"} or set(wire) - fields:
        raise ModelForecastValidationError("forecast line has an invalid closed shape")
    for field in ("id", "created_at", "line_ref", "subject_ref", "metric_or_aspect", "unit"):
        wire[field] = _text(wire[field], field)
    wire["currency"] = _text(wire["currency"], "currency")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise ModelForecastValidationError("forecast line version must be positive")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref")
    wire["period"] = _quarter_period(wire["period"], "period")
    wire["value"] = str(_decimal(wire["value"], "value").quantize(_QUANT, ROUND_HALF_UP))
    if wire["value_kind"] not in FORECAST_VALUE_KINDS:
        raise ModelForecastValidationError("value_kind is outside the frozen vocabulary")
    wire["scenario_version_ref"] = _text(wire["scenario_version_ref"], "scenario_version_ref")
    wire["scenario_version_hash"] = _hash(wire["scenario_version_hash"], "scenario_version_hash")
    if wire["value_kind"] == "derived_deterministic":
        wire["base_input_version_ref"] = _text(wire["base_input_version_ref"], "base_input_version_ref")
        wire["base_input_version_hash"] = _hash(wire["base_input_version_hash"], "base_input_version_hash")
        wire["growth_input_version_ref"] = _text(wire["growth_input_version_ref"], "growth_input_version_ref")
        wire["growth_input_version_hash"] = _hash(wire["growth_input_version_hash"], "growth_input_version_hash")
        wire["formula_ref"] = _text(wire["formula_ref"], "formula_ref")
        wire["formula_hash"] = _hash(wire["formula_hash"], "formula_hash")
        wire["model_run_version_ref"] = _text(wire["model_run_version_ref"], "model_run_version_ref")
        if wire["formula_ref"] != FORMULA_REF or wire["formula_hash"] != FORMULA_HASH:
            raise ModelForecastValidationError("derived line must use the frozen growth-extend formula")
        if wire["rationale"] is not None:
            raise ModelForecastValidationError("derived lines carry no human rationale")
    else:
        for field in (
            "base_input_version_ref", "base_input_version_hash",
            "growth_input_version_ref", "growth_input_version_hash",
            "formula_ref", "formula_hash", "model_run_version_ref",
        ):
            if wire[field] is not None:
                raise ModelForecastValidationError(f"{field} is reserved for derived lines")
        if wire["value_kind"] in HUMAN_LINE_KINDS and wire["value_kind"] != "observed":
            wire["rationale"] = _text(wire["rationale"], "rationale")
    wire["actor_ref"] = _automation_or_human(wire["actor_ref"], "actor_ref")
    if wire["value_kind"] == "derived_deterministic" and not wire["actor_ref"].startswith("automation:"):
        raise ModelForecastValidationError("derived lines must name their automation actor")
    if wire["value_kind"] != "derived_deterministic" and not wire["actor_ref"].startswith("human:"):
        raise ModelForecastValidationError("non-derived lines must be human-authored")
    return wire


def validate_forecast_line(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _normalize_line(value)
    wire["content_hash"] = _hash(wire.get("content_hash"), "content_hash")
    base = dict(wire)
    expected = base.pop("content_hash")
    if content_hash(base) != expected:
        raise ModelForecastValidationError("forecast line content_hash is invalid")
    return wire


class ModelForecastAuthority:
    """Append-only forecast line versions over one DaltonStore."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_model_forecast_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ModelForecastAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def publish_line(
        self,
        line_ref: str,
        *,
        subject_ref: str,
        metric_or_aspect: str,
        period: Mapping[str, Any],
        unit: str,
        currency: str,
        value: Any,
        value_kind: str,
        scenario_version_ref: str,
        scenario_version_hash: str,
        actor_ref: str,
        rationale: str | None = None,
        base_input_version_ref: str | None = None,
        base_input_version_hash: str | None = None,
        growth_input_version_ref: str | None = None,
        growth_input_version_hash: str | None = None,
        formula_ref: str | None = None,
        formula_hash: str | None = None,
        model_run_version_ref: str | None = None,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        line_ref = _text(line_ref, "line_ref")
        actor_ref = _automation_or_human(actor_ref, "actor_ref")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request = {
            "line_ref": line_ref, "subject_ref": subject_ref,
            "metric_or_aspect": metric_or_aspect, "period": dict(period),
            "unit": unit, "currency": currency, "value": value,
            "value_kind": value_kind,
            "scenario_version_ref": scenario_version_ref,
            "scenario_version_hash": scenario_version_hash,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash({"operation": "publish_line", "request": request})
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM model_forecast_idempotency WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["operation"] != "publish_line" or row["request_hash"] != request_hash:
                    raise ModelForecastConflict("idempotency key conflicts with prior request")
                import json as _json
                return {**_json.loads(row["result_json"]), "status": "duplicate"}
            latest = cur.execute(
                "SELECT version_id,version_number FROM model_forecast_line_versions "
                "WHERE line_ref=? ORDER BY version_number DESC LIMIT 1",
                (line_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ModelForecastConflict("first forecast line cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["version_id"]:
                    raise ModelForecastConflict("forecast line must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM model_forecast_line_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise ModelForecastConflict("forecast line version id already exists")
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "line_ref": line_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                "subject_ref": subject_ref,
                "metric_or_aspect": metric_or_aspect,
                "period": dict(period),
                "unit": unit,
                "currency": currency,
                "value": value,
                "value_kind": value_kind,
                "scenario_version_ref": scenario_version_ref,
                "scenario_version_hash": scenario_version_hash,
                "base_input_version_ref": base_input_version_ref,
                "base_input_version_hash": base_input_version_hash,
                "growth_input_version_ref": growth_input_version_ref,
                "growth_input_version_hash": growth_input_version_hash,
                "formula_ref": formula_ref,
                "formula_hash": formula_hash,
                "model_run_version_ref": model_run_version_ref,
                "actor_ref": actor_ref,
                "rationale": rationale,
            }
            wire = _normalize_line(record)
            wire["content_hash"] = content_hash(wire)
            wire = validate_forecast_line(wire)
            cur.execute(
                "INSERT INTO model_forecast_line_versions"
                "(version_id,line_ref,version_number,prior_version_id,subject_ref,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    version_id, line_ref, version, prior_version_ref, subject_ref,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            cur.execute(
                "INSERT INTO model_forecast_idempotency"
                "(idempotency_key,operation,request_hash,result_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (idempotency_key, "publish_line", request_hash, canonical_json(result), created_at),
            )
            return result

    def line(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "version_ref")
        row = self.connection.execute(
            "SELECT * FROM model_forecast_line_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise ModelForecastNotFound("forecast line version was not found")
        import json as _json
        wire = validate_forecast_line(_json.loads(row["record_json"]))
        if (
            wire["id"] != row["version_id"]
            or wire["line_ref"] != row["line_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["subject_ref"] != row["subject_ref"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise ModelForecastConflict("forecast line authority drifted")
        return wire


def extend_growth(
    ledger: ModelInputLedger,
    authority: ModelForecastAuthority,
    *,
    base_input_version_ref: str,
    growth_input_version_ref: str,
    periods: int,
    line_ref_prefix: str,
    model_run_ref: str,
    actor_ref: str = AUTOMATION_ACTOR,
    idempotency_key: str,
) -> dict[str, Any]:
    """Deterministically extend one admitted actual by a quarterly growth assumption."""

    if isinstance(periods, bool) or not isinstance(periods, int) or not 1 <= periods <= 12:
        raise ModelForecastValidationError("periods must be between 1 and 12")
    connection = authority.connection
    identity = content_hash({
        "operation": "extend_growth", "base": base_input_version_ref,
        "growth": growth_input_version_ref, "periods": periods,
        "line_ref_prefix": line_ref_prefix, "model_run_ref": model_run_ref,
        "actor_ref": actor_ref,
    })
    import json as _json
    with authority._transaction() as cur:
        prior = cur.execute(
            "SELECT * FROM model_forecast_idempotency WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            if prior["operation"] != "extend_growth" or prior["request_hash"] != identity:
                raise ModelForecastConflict("idempotency key conflicts with prior request")
            return {**_json.loads(prior["result_json"]), "status": "duplicate"}
    extension_seed = connection.execute(
        "SELECT request_hash FROM model_forecast_idempotency WHERE idempotency_key=?",
        (f"{idempotency_key}:seed",),
    ).fetchone()
    if extension_seed is not None:
        if extension_seed["request_hash"] != identity:
            raise ModelForecastConflict("extension seed conflicts with prior request")
        replayed = {"status": "duplicate", "model_run": None, "lines": [
            authority.line(row["version_id"]) for row in connection.execute(
                "SELECT version_id FROM model_forecast_line_versions WHERE line_ref LIKE ? "
                "ORDER BY line_ref, version_number", (f"{line_ref_prefix}:q%",),
            ).fetchall()
        ]}
        with authority._transaction() as cur:
            cur.execute(
                "INSERT INTO model_forecast_idempotency"
                "(idempotency_key,operation,request_hash,result_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (idempotency_key, "extend_growth", identity,
                 canonical_json(replayed), _now()),
            )
        return replayed
    with authority._transaction() as cur:
        cur.execute(
            "INSERT INTO model_forecast_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (f"{idempotency_key}:seed", "extend_growth", identity,
             canonical_json({"seeded": True}), _now()),
        )
    base_row = connection.execute(
        "SELECT * FROM model_input_versions WHERE version_id=?", (base_input_version_ref,)
    ).fetchone()
    growth_row = connection.execute(
        "SELECT * FROM model_input_versions WHERE version_id=?", (growth_input_version_ref,)
    ).fetchone()
    if base_row is None or growth_row is None:
        raise ModelForecastNotFound("base or growth input version was not found")
    import json as _json
    base_payload = _json.loads(base_row["payload_json"])
    growth_payload = _json.loads(growth_row["payload_json"])
    if base_row["input_kind"] != "actual":
        raise ModelForecastValidationError("base input must be an admitted actual")
    if growth_row["input_kind"] != "assumption":
        raise ModelForecastValidationError("growth input must be an admitted assumption")
    if growth_payload.get("value") is None:
        raise ModelForecastValidationError("growth assumption must carry a concrete value")
    if base_payload["subject_ref"] != growth_payload["subject_ref"]:
        raise ModelForecastValidationError("base and growth inputs must share a subject")
    scenario_ref = growth_payload["scenario_version_ref"]
    scenario_hash = growth_payload["scenario_version_hash"]
    base_value = _decimal(base_payload["value"], "base value")
    growth = _decimal(growth_payload["value"], "growth value")
    if growth_payload.get("unit") == "percent":
        growth = growth / Decimal(100)
    if growth <= Decimal("-1"):
        raise ModelForecastValidationError("growth must exceed -100%")

    prior_run = connection.execute(
        "SELECT version_id, version_number FROM model_run_pointer WHERE model_run_ref=?",
        (model_run_ref,),
    ).fetchone()
    prior_run_ref = None if prior_run is None else prior_run["version_id"]
    run_version_id = f"model-run-version:{model_run_ref.split('model-run:')[-1]}:{int(prior_run['version_number']) + 1 if prior_run else 1}"

    period = _quarter_period(base_payload["period"], "base period")
    lines: list[dict[str, Any]] = []
    running = base_value
    for index in range(1, periods + 1):
        running = running * (Decimal(1) + growth)
        period = _quarter_after(period)
        lines.append({
            "period": period,
            "value": running.quantize(_QUANT, ROUND_HALF_UP),
        })

    run = ledger.record_model_run(
        version_id=run_version_id,
        model_run_ref=model_run_ref,
        prior_version_ref=prior_run_ref,
        scenario_version_ref=scenario_ref,
        scenario_version_hash=scenario_hash,
        input_bindings=[
            {
                "binding_ref": "base-actual", "role": "base",
                "version_ref": base_input_version_ref,
                "version_hash": base_row["content_hash"],
            },
            {
                "binding_ref": "growth-assumption", "role": "growth",
                "version_ref": growth_input_version_ref,
                "version_hash": growth_row["content_hash"],
            },
        ],
        formula_version_ref=FORMULA_REF,
        formula_version_hash=FORMULA_HASH,
        status="completed",
        outputs=[
            {
                "output_ref": f"growth-extend:q{index}",
                "output_kind": "metric",
                "metric_ref": base_payload["metric_ref"],
                "period": item["period"],
                "unit": base_payload["unit"],
                "currency": base_payload["currency"],
                "value": str(item["value"]),
                "authority_bindings": [],
            }
            for index, item in enumerate(lines, start=1)
        ],
        errors=[],
        started_at=_now(),
        completed_at=_now(),
        actor_ref=actor_ref,
        idempotency_key=f"{idempotency_key}:run",
    )

    published: list[dict[str, Any]] = []
    for index, item in enumerate(lines, start=1):
        line_ref = f"{line_ref_prefix}:q{index}"
        prior_line = connection.execute(
            "SELECT version_id FROM model_forecast_line_versions WHERE line_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (line_ref,)
        ).fetchone()
        published.append(authority.publish_line(
            line_ref,
            subject_ref=base_payload["subject_ref"],
            metric_or_aspect=base_payload["metric_ref"],
            period=item["period"],
            unit=base_payload["unit"],
            currency=base_payload["currency"],
            value=str(item["value"]),
            value_kind="derived_deterministic",
            scenario_version_ref=scenario_ref,
            scenario_version_hash=scenario_hash,
            base_input_version_ref=base_input_version_ref,
            base_input_version_hash=base_row["content_hash"],
            growth_input_version_ref=growth_input_version_ref,
            growth_input_version_hash=growth_row["content_hash"],
            formula_ref=FORMULA_REF,
            formula_hash=FORMULA_HASH,
            model_run_version_ref=run["model_run"]["id"],
            actor_ref=actor_ref,
            version_id=f"forecast-line-version:{line_ref.split('forecast-line:')[-1]}:{int(prior_line['version_number']) + 1 if prior_line else 1}",
            prior_version_ref=None if prior_line is None else prior_line["version_id"],
            idempotency_key=f"{idempotency_key}:line:{index}",
        ))
    result = {"status": "fresh", "model_run": run, "lines": published}
    with authority._transaction() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO model_forecast_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (idempotency_key, "extend_growth", identity,
             canonical_json(result), _now()),
        )
    return result


__all__ = [
    "AUTOMATION_ACTOR",
    "FORMULA_HASH",
    "FORMULA_REF",
    "FORECAST_VALUE_KINDS",
    "ModelForecastAuthority",
    "ModelForecastConflict",
    "ModelForecastError",
    "ModelForecastNotFound",
    "ModelForecastValidationError",
    "extend_growth",
    "validate_forecast_line",
]
