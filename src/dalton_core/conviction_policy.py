"""Conviction v2: the policy a person publishes (ADR-0013).

v1 (``conviction_call.CONVICTION_POLICY``) admitted a company only when we
stood somewhere the market did not, and a call that agreed with the street was
refused before a model was asked -- however much it was expected to make.  v2
keeps three things apart that v1 folded into "is it a disagreement":

* **evidence confidence** -- how well the call's own view is supported, an
  ordinal resting on named thesis and claim refs;
* **scenarios** -- a small table of outcomes, each with a probability, a
  payoff over the horizon and the observable trigger that would tell us we
  are in it; the **expected return** is derived from the table, never typed;
* **market-view alignment** -- whether the street leans the same way.  A
  same-direction call is admitted when its expected return clears a higher,
  owner-set bar; disagreeing is no longer required, only priced.

The numbers that decide all of that are this module's object: versioned,
append-only, published by a ``human:`` principal through the writer, with
the defaults in code so a new workspace starts on v2 with nothing to install.
The same code runs in every workspace; only the published versions differ.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .store import canonical_json, content_hash

POLICY_SCHEMA_VERSION = "conviction-policy-0.2"
VERSION_SCHEMA_VERSION = "conviction-policy-version-0.1"
POLICY_REF = "conviction-policy:v2"
DEFAULT_VERSION_REF = "conviction-policy:v2:code-default"
TABLE = "conviction_policy_versions"
_SCHEMA_PATH = Path(__file__).with_name("conviction_policy_schema.sql")

GATES: tuple[str, ...] = ("v1", "v2")
CONFIDENCE_ORDER: tuple[str, ...] = ("low", "medium", "high")
HORIZONS: tuple[str, ...] = ("3_6_months", "6_12_months", "1_3_years", "3_5_years")
DIRECTIONS: tuple[str, ...] = ("long", "short", "avoid")
THESIS_SCOPES: tuple[str, ...] = ("company_only", "company_or_industry")

MIN_SCENARIOS, MAX_SCENARIOS = 2, 6
MAX_REASON_CHARS = 1000
# A return threshold over the bound is a typing accident (the same bound the
# call's own percentages are held to).
MAX_THRESHOLD_PERCENT = Decimal("1000")
MAX_DAILY_CAP_USD = Decimal("100")

# The default every workspace starts on.  Each number is an owner decision the
# ADR lists; they are here so that "no policy published yet" is a well-defined
# state rather than a refusal.
DEFAULT_POLICY: Mapping[str, Any] = MappingProxyType({
    "schema_version": POLICY_SCHEMA_VERSION,
    "policy_ref": POLICY_REF,
    "gate": "v2",
    # Ordinal floor on the call's own evidence confidence.
    "min_evidence_confidence": "medium",
    # How many scenarios a call must weigh (at least).  Three is base / bull /
    # bear; two would let a call hide its tail.
    "required_scenarios": 3,
    "allowed_horizons": ["6_12_months"],
    # ``avoid`` is not a position; v2 does not draft it unless the owner says.
    "allowed_directions": ["long", "short"],
    # The position's probability-weighted return over the horizon, in per
    # cent, that any call must clear ...
    "min_expected_return_percent": {
        "3_6_months": "8", "6_12_months": "12", "1_3_years": "25", "3_5_years": "50",
    },
    # ... and the higher bar for a call that leans the same way as the street
    # (or cannot say where the street leans).  Agreeing is allowed; it has to
    # pay for itself.
    "same_direction_min_expected_return_percent": {
        "3_6_months": "15", "6_12_months": "20", "1_3_years": "40", "3_5_years": "80",
    },
    # Which theses count as "an active thesis" for a company.
    "thesis_scope": "company_only",
    # The valuation snapshot must be on the latest price series and at most
    # this old.
    "max_valuation_age_days": 7,
    # The lane's daily drafting budget, across every company.
    "daily_drafting_cost_cap_usd": "1.00",
    "max_drafts_per_day": 3,
    # A call names at least this many invalidators.
    "min_invalidators": 1,
    "min_upside_downside_ratio": "2",
})

from .terminology import label as _term

FIELD_LABELS: Mapping[str, str] = MappingProxyType({
    "gate": "判断规则版本",
    "min_evidence_confidence": _term("policy", "confidence"),
    "required_scenarios": "至少要列出的情景数",
    "allowed_horizons": "允许的投资期限",
    "allowed_directions": "允许的方向",
    "min_expected_return_percent": _term("policy", "hurdle"),
    "same_direction_min_expected_return_percent": "与市场同向时的期望回报门槛（%）",
    "thesis_scope": "哪些论点算作公司的生效论点",
    "max_valuation_age_days": "估值快照最长可用天数",
    "daily_drafting_cost_cap_usd": "每日起草预算（美元）",
    "max_drafts_per_day": "每日最多起草次数",
    "min_invalidators": "至少要写明的失效条件数",
    "min_upside_downside_ratio": "最低潜在收益／损失比",
})
VALUE_LABELS: Mapping[str, str] = MappingProxyType({
    "low": "低", "medium": "中", "high": "高",
    "long": "做多", "short": "做空", "avoid": "回避",
    "3_6_months": "3–6 个月", "6_12_months": "6–12 个月",
    "1_3_years": "1–3 年", "3_5_years": "3–5 年",
    "company_only": "只算公司自己的论点",
    "company_or_industry": "公司论点或所属行业论点",
    "v1": "v1（必须与市场分歧）", "v2": "v2（证据 × 情景 × 期望回报）",
})

_POLICY_FIELDS = frozenset(DEFAULT_POLICY)
_VERSION_FIELDS = frozenset({
    "schema_version", "id", "created_at", "version", "prior_version_ref",
    "policy", "policy_hash", "reason", "actor_ref", "content_hash",
})


class ConvictionPolicyError(RuntimeError):
    """Base error for the conviction policy."""


class ConvictionPolicyValidationError(ConvictionPolicyError, ValueError):
    """A policy or a publish request is malformed."""


class ConvictionPolicyConflict(ConvictionPolicyError):
    """A publish raced another, or a stored row disagrees with itself."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _percent(value: Any, name: str, *, allow_negative: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConvictionPolicyValidationError(f"{name} must be a decimal string")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConvictionPolicyValidationError(f"{name} is not a number") from exc
    if not number.is_finite():
        raise ConvictionPolicyValidationError(f"{name} must be finite")
    if (number < 0 and not allow_negative) or abs(number) > MAX_THRESHOLD_PERCENT:
        raise ConvictionPolicyValidationError(
            f"{name} must be between 0 and {MAX_THRESHOLD_PERCENT}")
    return format(number.normalize(), "f") if number != 0 else "0"


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConvictionPolicyValidationError(f"{name} must be an integer {low}–{high}")
    return value


def _subset(value: Any, allowed: Sequence[str], name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConvictionPolicyValidationError(f"{name} must be a non-empty list")
    if any(item not in allowed for item in value) or len(set(value)) != len(value):
        raise ConvictionPolicyValidationError(
            f"{name} may only name {', '.join(allowed)}, once each")
    # Canonical order, so two equal policies hash equal.
    return [item for item in allowed if item in value]


def _threshold_map(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(HORIZONS):
        raise ConvictionPolicyValidationError(
            f"{name} must give one threshold for each of {', '.join(HORIZONS)}")
    return {horizon: _percent(value[horizon], f"{name}.{horizon}") for horizon in HORIZONS}


def validate_policy(value: Any) -> dict[str, Any]:
    """The closed shape of one v2 policy body, normalised."""

    if not isinstance(value, Mapping):
        raise ConvictionPolicyValidationError("the policy must be an object")
    wire = dict(value)
    if set(wire) not in (set(_POLICY_FIELDS), set(_POLICY_FIELDS)-{"min_upside_downside_ratio"}):
        raise ConvictionPolicyValidationError(
            "the policy has an invalid closed shape; "
            f"missing={sorted(_POLICY_FIELDS - set(wire))}, "
            f"unknown={sorted(set(wire) - _POLICY_FIELDS)}")
    if wire["schema_version"] != POLICY_SCHEMA_VERSION:
        raise ConvictionPolicyValidationError("unsupported conviction policy schema_version")
    if wire["policy_ref"] != POLICY_REF:
        raise ConvictionPolicyValidationError(f"policy_ref must be {POLICY_REF}")
    if wire["gate"] != "v2":
        # v1 is frozen in code (``conviction_call.CONVICTION_POLICY``) and is
        # shown for comparison; it is not something a version can switch back
        # to.  Pausing the lane is the owner's lever for "stop drafting".
        raise ConvictionPolicyValidationError("a published conviction policy is gate v2")
    if wire["min_evidence_confidence"] not in CONFIDENCE_ORDER:
        raise ConvictionPolicyValidationError(
            "min_evidence_confidence must be low, medium or high")
    if wire["thesis_scope"] not in THESIS_SCOPES:
        raise ConvictionPolicyValidationError(
            f"thesis_scope must be one of {', '.join(THESIS_SCOPES)}")
    checked = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_ref": POLICY_REF,
        "gate": "v2",
        "min_evidence_confidence": wire["min_evidence_confidence"],
        "required_scenarios": _int(wire["required_scenarios"], "required_scenarios",
                                   MIN_SCENARIOS, MAX_SCENARIOS),
        "allowed_horizons": _subset(wire["allowed_horizons"], HORIZONS, "allowed_horizons"),
        "allowed_directions": _subset(wire["allowed_directions"], DIRECTIONS,
                                      "allowed_directions"),
        "min_expected_return_percent": _threshold_map(
            wire["min_expected_return_percent"], "min_expected_return_percent"),
        "same_direction_min_expected_return_percent": _threshold_map(
            wire["same_direction_min_expected_return_percent"],
            "same_direction_min_expected_return_percent"),
        "thesis_scope": wire["thesis_scope"],
        "max_valuation_age_days": _int(wire["max_valuation_age_days"],
                                       "max_valuation_age_days", 1, 90),
        "daily_drafting_cost_cap_usd": _percent(
            wire["daily_drafting_cost_cap_usd"], "daily_drafting_cost_cap_usd"),
        "max_drafts_per_day": _int(wire["max_drafts_per_day"], "max_drafts_per_day", 0, 50),
        "min_invalidators": _int(wire["min_invalidators"], "min_invalidators", 1, 8),
    }
    if "min_upside_downside_ratio" in wire:
        checked["min_upside_downside_ratio"]=_percent(wire["min_upside_downside_ratio"], "min_upside_downside_ratio")
        if Decimal(checked["min_upside_downside_ratio"])<=0:
            raise ConvictionPolicyValidationError("minimum upside/downside ratio must be positive")
    if Decimal(checked["daily_drafting_cost_cap_usd"]) > MAX_DAILY_CAP_USD:
        raise ConvictionPolicyValidationError(
            f"daily_drafting_cost_cap_usd is over the {MAX_DAILY_CAP_USD} USD bound")
    for horizon in HORIZONS:
        # Agreeing with the market can never be the cheaper way in.
        if Decimal(checked["same_direction_min_expected_return_percent"][horizon]) < \
                Decimal(checked["min_expected_return_percent"][horizon]):
            raise ConvictionPolicyValidationError(
                "the same-direction threshold must be at least the general threshold "
                f"at {horizon}")
    return checked


DEFAULT_POLICY_HASH: str = content_hash(validate_policy(DEFAULT_POLICY))


def default_effective() -> dict[str, Any]:
    policy = validate_policy(DEFAULT_POLICY)
    return {"source": "code_default", "version_ref": DEFAULT_VERSION_REF,
            "version": 0, "policy": policy, "policy_hash": content_hash(policy),
            "created_at": None, "actor_ref": None, "reason": None}


def v1_view() -> dict[str, Any]:
    """The frozen v1 policy, in the comparison's terms (read-only)."""

    from .conviction_call import CONVICTION_POLICY, POLICY_HASH, POLICY_REF as V1_REF

    return {"source": "frozen_v1", "version_ref": V1_REF, "version": None,
            "policy_hash": POLICY_HASH,
            "policy": {"gate": "v1",
                       "min_consensus_gap_percent": CONVICTION_POLICY["min_consensus_gap_percent"],
                       "allowed_horizons": list(CONVICTION_POLICY["time_horizons"]),
                       "allowed_directions": list(DIRECTIONS)}}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _table_exists(connection: Any) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _decode(row: Any) -> dict[str, Any]:
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConvictionPolicyConflict("conviction policy record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ConvictionPolicyConflict("conviction policy record_json is not canonical")
    if set(wire) != set(_VERSION_FIELDS):
        raise ConvictionPolicyConflict("conviction policy version has an invalid shape")
    body = {key: item for key, item in wire.items() if key != "content_hash"}
    if wire["content_hash"] != content_hash(body) or wire["content_hash"] != row["content_hash"]:
        raise ConvictionPolicyConflict("conviction policy content hash drifted")
    policy = validate_policy(wire["policy"])
    if content_hash(policy) != wire["policy_hash"]:
        raise ConvictionPolicyConflict("conviction policy hash drifted")
    return wire


def published_versions(connection: Any) -> list[dict[str, Any]]:
    """Every published version, oldest first (none on a fresh Core)."""

    if not _table_exists(connection):
        return []
    rows = connection.execute(
        f"SELECT * FROM {TABLE} ORDER BY version_number").fetchall()
    return [_decode(row) for row in rows]


def effective_policy(connection: Any) -> dict[str, Any]:
    """The policy in force: the newest published version, else the default.

    Read-only -- it never installs the table -- so the cockpit and a lane can
    ask on any Core.  An unreadable table is the default *with a note*, never
    a crash: a lane that could not read its policy should not stop everything
    else on the tick.
    """

    try:
        versions = published_versions(connection)
    except (ConvictionPolicyError, sqlite3.Error) as exc:
        return {**default_effective(), "note": f"published policy unreadable: {exc}"}
    if not versions:
        return default_effective()
    latest = versions[-1]
    return {"source": "published", "version_ref": latest["id"],
            "version": latest["version"], "policy": dict(latest["policy"]),
            "policy_hash": latest["policy_hash"], "created_at": latest["created_at"],
            "actor_ref": latest["actor_ref"], "reason": latest["reason"]}


def compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Field-by-field comparison of two policy bodies, labelled in Chinese."""

    rows: list[dict[str, Any]] = []
    for field, label in FIELD_LABELS.items():
        a, b = left.get(field), right.get(field)
        rows.append({"field": field, "label": label, "left": a, "right": b,
                     "left_label": display_value(a), "right_label": display_value(b),
                     "changed": a != b})
    return rows


def display_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        return "、".join(VALUE_LABELS.get(str(item), str(item)) for item in value) or "—"
    if isinstance(value, Mapping):
        return "；".join(f"{VALUE_LABELS.get(str(key), str(key))} {item}"
                        for key, item in value.items())
    return VALUE_LABELS.get(str(value), str(value))


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------

class ConvictionPolicyAuthority:
    """Append-only policy versions, published by a person."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ConvictionPolicyAuthority requires a DaltonStore")
        self.store = store
        self.connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def versions(self) -> list[dict[str, Any]]:
        return published_versions(self.connection)

    def effective(self) -> dict[str, Any]:
        return effective_policy(self.connection)

    def publish(
        self, *, policy: Mapping[str, Any], reason: str, actor_ref: str,
        prior_version_ref: str | None = None, idempotency_key: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Append one version.  ``prior_version_ref`` must name the newest one.

        A stale ``prior_version_ref`` is a conflict, not a merge: two people
        editing the same thresholds from two pages must see each other.
        Publishing bytes identical to the version in force is refused as
        ``unchanged`` -- a version that says nothing new is noise in the chain.
        """

        if not isinstance(actor_ref, str) or not actor_ref.startswith("human:"):
            raise ConvictionPolicyValidationError(
                "a conviction policy is published by a human: principal")
        if not isinstance(reason, str) or not reason.strip() \
                or len(reason.strip()) > MAX_REASON_CHARS:
            raise ConvictionPolicyValidationError(
                f"reason must be non-empty text of at most {MAX_REASON_CHARS} characters")
        checked = validate_policy(policy)
        if checked["required_scenarios"]!=3 or checked["allowed_horizons"]!=["6_12_months"] or "min_upside_downside_ratio" not in checked:
            raise ConvictionPolicyValidationError("new policies require three scenarios, the twelve-month horizon and a minimum upside/downside ratio")
        policy_hash = content_hash(checked)
        key = None if idempotency_key is None else str(idempotency_key).strip()[:256] or None
        with self.store._transaction() as cur:
            if key is not None:
                seen = cur.execute(
                    f"SELECT * FROM {TABLE} WHERE idempotency_key=?", (key,)).fetchone()
                if seen is not None:
                    record = _decode(seen)
                    if record["policy_hash"] != policy_hash:
                        raise ConvictionPolicyConflict(
                            "this idempotency key already published a different policy")
                    return {"status": "duplicate", "version": record}
            latest = cur.execute(
                f"SELECT * FROM {TABLE} ORDER BY version_number DESC LIMIT 1").fetchone()
            latest_record = None if latest is None else _decode(latest)
            expected_prior = None if latest_record is None else latest_record["id"]
            if (prior_version_ref or None) != expected_prior:
                raise ConvictionPolicyConflict(
                    "the policy changed since this page was loaded; reload and compare")
            in_force = (latest_record["policy_hash"] if latest_record is not None
                        else DEFAULT_POLICY_HASH)
            if policy_hash == in_force:
                return {"status": "unchanged", "version": latest_record,
                        "reason": "these thresholds are already the ones in force"}
            number = 1 if latest_record is None else latest_record["version"] + 1
            body = {
                "schema_version": VERSION_SCHEMA_VERSION,
                "id": f"conviction-policy-version:v2:{number}",
                "created_at": created_at or _now(),
                "version": number,
                "prior_version_ref": expected_prior,
                "policy": checked,
                "policy_hash": policy_hash,
                "reason": reason.strip(),
                "actor_ref": actor_ref,
            }
            record = {**body, "content_hash": content_hash(body)}
            cur.execute(
                f"INSERT INTO {TABLE}(version_id,policy_ref,version_number,prior_version_id,"
                "policy_hash,reason,idempotency_key,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], POLICY_REF, number, expected_prior, policy_hash,
                 record["reason"], key, canonical_json(record), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
        return {"status": "published", "version": record}


__all__ = [
    "CONFIDENCE_ORDER", "DEFAULT_POLICY", "DEFAULT_POLICY_HASH", "DEFAULT_VERSION_REF",
    "FIELD_LABELS", "GATES", "HORIZONS", "POLICY_REF", "POLICY_SCHEMA_VERSION", "TABLE",
    "THESIS_SCOPES", "VALUE_LABELS", "ConvictionPolicyAuthority", "ConvictionPolicyConflict",
    "ConvictionPolicyError", "ConvictionPolicyValidationError", "compare", "default_effective",
    "display_value", "effective_policy", "published_versions", "v1_view", "validate_policy",
]
