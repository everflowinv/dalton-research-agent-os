"""ADR-0012: structured scenario assumptions, and what we saw of them.

Vision-gap #3 (2026-09-30 report, section 1.1).  The dossier's scenario
thinking -- ``variant_view.convergence_pathway`` / ``observable_signals``, the
causal chains, the catalyst section -- was cited prose: readable, referable,
and impossible for a program to watch.  This module turns it into an object a
lane can evaluate every tick without a model:

``ScenarioVersion``
    One named scenario for one company: a probability, the theses and drivers
    it is about, the evidence it rests on, and two lists of *conditions* --
    ``triggers`` (what we would see if it is playing out) and
    ``invalidators`` (what would tell us it is not).  A condition is
    ``{metric_ref, observed_by, transform, comparator, threshold, horizon}``
    plus the refs its threshold was set against, so every number in it can be
    pointed at.  Versions are immutable; a new version supersedes, and the
    chain of one scenario runs across proposals.

``ScenarioSet``
    One proposal: every scenario one model call (or one owner act) said about
    one company at once, with the anchors it was drafted from (dossier,
    driver model, theses).  The owner decides a *set* -- accept, reject or
    defer -- because the probabilities only mean something together.

``ScenarioObservation``
    What the evaluator saw for one condition of one version: ``triggered``,
    ``not_triggered`` or ``unobservable``, with the value, its date and the
    exact ref it came from.  Appended only when something changed, so the
    table is the history of a condition, not a heartbeat.

**Who may write what.**  Automation may propose a set (scope ``dossier``:
these are the dossier's own scenarios made structured) and append
observations (scope ``observation``).  Only a person decides a set, and only
a person writes a revised version (a new probability, or retiring a
scenario); an owner version is in force the moment it is written because the
owner is the adjudicator.  Nothing proposed by automation is in force, fires
an event or moves a cadence until the owner has accepted it.

**What firing does.**  When a condition of an *in force* scenario turns
``triggered``, the observation lane records a ``scenario_trigger``
``ResearchEvent`` (ADR-0009: the one way in) -- the judgement lane then
decides, and ADR-0007's revision candidate or forecast proposal is what can
come of it -- and re-times the company's adjustable tracking sources through
:func:`tracking_cadence.publish_priority`, the same code the owner's
「优先看这家」 uses.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash
from .terminology import condition_sentence
from .terminology import label as _term

SCHEMA_VERSION = "company-scenario-0.2"
EVALUATOR_REF = "scenario-evaluator:0.1"
_SCHEMA_PATH = Path(__file__).with_name("company_scenario_schema.sql")

#: Automation proposes under the dossier's own scope: a structured scenario is
#: the dossier's scenario prose made checkable, not a new kind of output.
PROPOSAL_WRITE_SCOPE = "dossier"
#: Observations are the tracking scope's, like a cadence version.
OBSERVATION_WRITE_SCOPE = "observation"

SET_ORIGINS = ("automation", "owner")
DECISIONS = ("accept", "reject", "defer")
FINAL_DECISIONS = frozenset({"accept", "reject"})
STATUSES = ("triggered", "not_triggered", "unobservable")
CONDITION_KINDS = ("trigger", "invalidator")
VERSION_STATES = ("live", "retired")
CHANGE_REASONS = ("proposed", "owner_revision", "owner_retired", "owner_authored")

# Which lane or authority observes a condition.  Closed: an observer that is
# not here is a condition nobody will ever look at.
OBSERVERS: tuple[str, ...] = (
    # A filed figure: a driver of the company's driver model (``concept:`` or
    # ``row:`` ref), read from the model's filed history -- SEC statements.
    "sec_actual",
    "company_fcf",
    # The last settled close (``price:close``) of the market-price series.
    "market_price",
    # A multiple of the latest valuation snapshot (``valuation:trailing_pe``).
    "valuation",
    # The newest active quantitative Claim with this ``metric_or_aspect``.
    "claim",
    # How many event judgements with this decision word since the scenario
    # version was written (``judgement:THESIS_WEAKENED``).
    "event_judgement",
    # Nothing a program can read: the owner watches it.  Always unobservable.
    "owner",
)
TRANSFORMS = ("level", "yoy_pct", "qoq_pct")
COMPARATORS: dict[str, str] = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
DECISION_WORDS = ("THESIS_STRENGTHENED", "THESIS_WEAKENED", "THESIS_BROKEN", "NEW_THESIS")

MIN_SCENARIOS, MAX_SCENARIOS = 2, 4
MAX_TRIGGERS, MAX_INVALIDATORS = 4, 3
MIN_PROBABILITY, MAX_PROBABILITY = Decimal("0.01"), Decimal("0.95")
PROBABILITY_SUM_TOLERANCE = Decimal("0.01")
#: A probability move at least this large re-times tracking (ADR-0012).
MATERIAL_PROBABILITY_CHANGE = Decimal("0.10")
MAX_HORIZON_DAYS = 3 * 366
MAX_NAME_CHARS, MAX_NARRATIVE_CHARS, MAX_LABEL_CHARS = 40, 300, 160
MAX_REFS = 12
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_CONDITION_ID_RE = re.compile(r"^[ti][1-9]$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

OBSERVER_LABELS = {
    "sec_actual": "公司报表实际数", "market_price": "股价", "valuation": "估值倍数",
    "claim": "已入账的定量结论", "event_judgement": "事件研判", "owner": _term("scenario", "owner_observed"),
}
TRANSFORM_LABELS = {"level": "", "yoy_pct": "同比增速", "qoq_pct": "环比增速"}
#: The machine form kept under 技术详情 (and in the weekly brief's raw rule).
TRANSFORM_CODES = {"level": "", "yoy_pct": "同比%", "qoq_pct": "环比%"}
#: What an event judgement word means, in the owner's language.
DECISION_LABELS = {
    "THESIS_STRENGTHENED": "核心论点增强", "THESIS_WEAKENED": "核心论点受损",
    "THESIS_BROKEN": "核心论点被推翻", "NEW_THESIS": "新论点",
}
#: A filer's (or the proposal's) English metric name -> its Chinese name.
#: Concepts are matched first (``quantitative_claim_promotion.CONCEPT_METRICS``),
#: this is for a ``row:`` driver or an older condition carrying only a label.
METRIC_NAME_LABELS = {
    "revenue": "营业收入", "revenues": "营业收入", "total revenue": "营业收入",
    "net revenue": "营业收入", "net revenues": "营业收入",
    "operating income": "营业利润", "income from operations": "营业利润",
    "operating income (loss)": "营业利润", "operating profit": "营业利润",
    "operating cash flow": "经营活动现金流",
    "net cash provided by operating activities": "经营活动现金流",
    "capital expenditure": "资本开支", "capital expenditures": "资本开支",
    "net income": "净利润", "gross profit": "毛利",
    "diluted eps": "稀释每股收益", "basic eps": "基本每股收益",
    "free cash flow": "自由现金流", "cost of revenue": "营业成本",
    "trailing price / earnings": "滚动市盈率", "forward price / earnings": "预期市盈率",
}
VALUATION_LABELS = {
    "valuation:trailing_pe": "滚动市盈率", "valuation:forward_pe": "预期市盈率",
    "valuation:ev_ebitda": "企业价值/EBITDA", "valuation:ev_sales": "企业价值/收入",
    "valuation:price_to_book": "市净率", "valuation:fcf_yield": "自由现金流收益率",
}
STATUS_LABELS = {
    "triggered": "已触发", "not_triggered": "未触发", "unobservable": "暂时无法观察",
}
REASON_LABELS = {
    "observed": "已按最新数据判断",
    "horizon_passed": "已过观察期限",
    "no_value": "还没有可读的数据",
    "owner_observed": _term("scenario", "owner_observed"),
    "predates_scenario": "数据早于这个情景，不算触发",
    "no_prior_period": "缺少对比期，算不出变化率",
}


class CompanyScenarioError(RuntimeError):
    """Base error for the scenario authority."""


class CompanyScenarioValidationError(CompanyScenarioError, ValueError):
    """A request does not satisfy the closed contract."""


class CompanyScenarioConflict(CompanyScenarioError):
    """A request conflicts with what is already recorded."""


# ---------------------------------------------------------------------------
# small validators
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompanyScenarioValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise CompanyScenarioValidationError(f"{name} must be at most {maximum} characters")
    return value


def _refs(value: Any, name: str, *, allowed: set[str] | None = None,
          nonempty: bool = True) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CompanyScenarioValidationError(f"{name} must be a list of refs")
    refs = list(dict.fromkeys(_text(item, f"{name}[]", maximum=240) for item in value))
    if len(refs) > MAX_REFS:
        raise CompanyScenarioValidationError(f"{name} may name at most {MAX_REFS} refs")
    if nonempty and not refs:
        raise CompanyScenarioValidationError(f"{name} must name at least one ref")
    if allowed is not None:
        unknown = [ref for ref in refs if ref not in allowed]
        if unknown:
            raise CompanyScenarioValidationError(
                f"{name} names refs the material did not contain: {unknown[:3]}")
    return refs


def decimal_text(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CompanyScenarioValidationError(f"{name} must be a decimal number")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CompanyScenarioValidationError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise CompanyScenarioValidationError(f"{name} must be finite")
    return number


def probability_text(value: Any, name: str = "probability") -> str:
    number = decimal_text(value, name)
    if not MIN_PROBABILITY <= number <= MAX_PROBABILITY:
        raise CompanyScenarioValidationError(
            f"{name} must be between {MIN_PROBABILITY} and {MAX_PROBABILITY}")
    return str(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def _iso_date(value: Any, name: str) -> str:
    try:
        return date.fromisoformat(_text(value, name, maximum=10)).isoformat()
    except ValueError as exc:
        raise CompanyScenarioValidationError(f"{name} must be an ISO date") from exc


def scenario_ref_for(company_ref: str, key: str) -> str:
    return "company-scenario:" + content_hash({"company": company_ref, "key": key})[:32]


def _record(body: Mapping[str, Any]) -> dict[str, Any]:
    record = json.loads(canonical_json(dict(body)))
    record.pop("content_hash", None)
    record["content_hash"] = content_hash(record)
    return record


def _decode(row: Any, name: str) -> dict[str, Any]:
    if row is None:
        raise CompanyScenarioConflict(f"{name} is missing")
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise CompanyScenarioConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict):
        raise CompanyScenarioConflict(f"{name} record_json is not an object")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise CompanyScenarioConflict(f"{name} content hash drifted")
    return wire


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _row_factory(connection: sqlite3.Connection) -> None:
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row


# ---------------------------------------------------------------------------
# conditions
# ---------------------------------------------------------------------------


def validate_condition(
    value: Any,
    *,
    kind: str,
    today: date,
    observables: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    allowed_refs: set[str] | None = None,
) -> dict[str, Any]:
    """One condition, closed.  ``observables`` pins what a machine can read.

    ``observables`` maps ``(observed_by, metric_ref)`` to the row of the
    proposal material that offered it; a condition naming anything else is
    refused, because a trigger on a metric no lane observes is a sentence,
    not a trigger.  The row's label, unit and baseline travel into the
    condition so the threshold can always be read next to the number it was
    set against.
    """

    if kind not in CONDITION_KINDS:
        raise CompanyScenarioValidationError("condition kind is trigger or invalidator")
    if not isinstance(value, Mapping):
        raise CompanyScenarioValidationError("a condition must be an object")
    fields = {"condition_id", "label", "observed_by", "metric_ref", "transform",
              "comparator", "threshold", "horizon", "refs", "metric_label", "unit",
              "baseline", "kind", "owner_rule"}
    unknown = sorted(set(value) - fields)
    if unknown:
        raise CompanyScenarioValidationError(f"condition has undeclared fields: {unknown}")
    if value.get("kind") not in (None, kind):
        raise CompanyScenarioValidationError("a condition sits in the list its kind names")
    condition_id = _text(value.get("condition_id"), "condition_id", maximum=2)
    if not _CONDITION_ID_RE.fullmatch(condition_id) or condition_id[0] != kind[0]:
        raise CompanyScenarioValidationError(
            "condition_id is t1..t9 for a trigger and i1..i9 for an invalidator")
    observed_by = _text(value.get("observed_by"), "observed_by", maximum=32)
    if observed_by not in OBSERVERS:
        raise CompanyScenarioValidationError(f"observed_by must be one of {list(OBSERVERS)}")
    label = _text(value.get("label"), "label", maximum=MAX_LABEL_CHARS)
    horizon = _iso_date(value.get("horizon"), "horizon")
    horizon_day = date.fromisoformat(horizon)
    if horizon_day < today or horizon_day > today + timedelta(days=MAX_HORIZON_DAYS):
        raise CompanyScenarioValidationError(
            "horizon must be today or later and at most three years out")
    refs = _refs(value.get("refs"), "refs", allowed=allowed_refs)
    condition: dict[str, Any] = {
        "condition_id": condition_id, "kind": kind, "label": label,
        "observed_by": observed_by, "horizon": horizon, "refs": refs,
    }
    if observed_by == "owner":
        for name in ("metric_ref", "comparator", "threshold"):
            if value.get(name) not in (None, ""):
                raise CompanyScenarioValidationError(
                    "an owner-observed condition carries no metric, comparator or threshold")
        condition.update({"metric_ref": None, "transform": "level", "comparator": None,
                          "threshold": None, "metric_label": label, "unit": None,
                          "baseline": None})
        if "owner_rule" in value:
            condition["owner_rule"] = dict(value["owner_rule"])
        return condition
    metric_ref = _text(value.get("metric_ref"), "metric_ref", maximum=240)
    transform = value.get("transform") or "level"
    if transform not in TRANSFORMS:
        raise CompanyScenarioValidationError(f"transform must be one of {list(TRANSFORMS)}")
    if transform != "level" and observed_by != "sec_actual":
        raise CompanyScenarioValidationError("only a filed figure has a growth transform")
    comparator = _text(value.get("comparator"), "comparator", maximum=4)
    if comparator not in COMPARATORS:
        raise CompanyScenarioValidationError(f"comparator must be one of {list(COMPARATORS)}")
    threshold = decimal_text(value.get("threshold"), "threshold")
    _check_metric(observed_by, metric_ref)
    row = None
    if observables is not None:
        row = observables.get((observed_by, metric_ref))
        if row is None:
            raise CompanyScenarioValidationError(
                f"{observed_by} {metric_ref} is not something the material said can be observed")
    metric_label = (row or {}).get("label") or value.get("metric_label") or metric_ref
    unit = (row or {}).get("unit") if row is not None else value.get("unit")
    baseline = value.get("baseline")
    if row is not None:
        latest = (row.get("transforms") or {}).get(transform)
        baseline = None if latest is None else {
            key: latest.get(key) for key in ("value", "as_of", "source_ref")}
    condition.update({
        "metric_ref": metric_ref, "transform": transform, "comparator": comparator,
        "threshold": str(threshold), "metric_label": _text(metric_label, "metric_label"),
        "unit": None if unit in (None, "") else str(unit)[:24],
        "baseline": None if baseline is None else {
            "value": None if baseline.get("value") is None else str(baseline.get("value")),
            "as_of": baseline.get("as_of"), "source_ref": baseline.get("source_ref")},
    })
    return condition


def _check_metric(observed_by: str, metric_ref: str) -> None:
    if observed_by == "company_fcf" and metric_ref != "fcf:company":
        raise CompanyScenarioValidationError("company FCF observer reads fcf:company")
    if observed_by == "market_price" and metric_ref != "price:close":
        raise CompanyScenarioValidationError("the price observer reads price:close")
    if observed_by == "valuation" and not metric_ref.startswith("valuation:"):
        raise CompanyScenarioValidationError("a valuation condition names valuation:<metric>")
    if observed_by == "event_judgement":
        word = metric_ref.split(":", 1)[-1]
        if not metric_ref.startswith("judgement:") or word not in DECISION_WORDS:
            raise CompanyScenarioValidationError(
                "an event-judgement condition names judgement:<DECISION_WORD>")
    if observed_by == "sec_actual" and not metric_ref.startswith(("concept:", "row:")):
        raise CompanyScenarioValidationError(
            "a filed-figure condition names a driver-model driver (concept: or row:)")


_DECISION_WORD_RE = re.compile(r" ?\b(" + "|".join(DECISION_WORDS) + r")\b ?")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")


def decision_text(text: Any) -> str:
    """Replace judgement words (``THESIS_BROKEN``) in a sentence with Chinese."""

    return _DECISION_WORD_RE.sub(lambda m: f"「{DECISION_LABELS[m.group(1)]}」", str(text or ""))


def metric_display_label(condition: Mapping[str, Any]) -> str:
    """The condition's metric in Chinese: concept first, then the filer's name."""

    metric_ref = str(condition.get("metric_ref") or "")
    label = str(condition.get("metric_label") or "").strip()
    observed_by = condition.get("observed_by")
    if observed_by == "event_judgement":
        word = metric_ref.split(":", 1)[-1]
        if word in DECISION_LABELS:
            return f"此后研判为「{DECISION_LABELS[word]}」的事件数"
        return decision_text(label) or "事件研判数"
    if observed_by == "market_price":
        return "收盘价"
    if observed_by == "valuation" and metric_ref in VALUATION_LABELS:
        return VALUATION_LABELS[metric_ref]
    if metric_ref.startswith("concept:"):
        from .quantitative_claim_promotion import CONCEPT_METRICS

        known = CONCEPT_METRICS.get(metric_ref.split(":", 1)[1])
        if known is not None:
            return known[1]
        if metric_ref == "concept:revenue":
            return "营业收入"
    named = METRIC_NAME_LABELS.get(label.casefold())
    if named is not None:
        return named
    # Technical identity remains in condition_rule_technical. Unknown concepts
    # must not become a first-screen financial name merely by echoing the ID.
    return "指标口径尚未解析（请展开核对）"


def _plain_decimal(text: str) -> str:
    """``2.00`` -> ``2``; ``187.20`` -> ``187.2``; other text unchanged."""

    if re.fullmatch(r"-?\d+\.\d+", text):
        text = text.rstrip("0").rstrip(".")
    return text


def condition_value_text(condition: Mapping[str, Any], value: Any) -> str | None:
    """A threshold or an observed value in the condition's own unit, in Chinese.

    ``200000000`` on a filed dollar line reads ``2 亿美元``; a growth rate
    reads ``3%``; an event count ``1 次``; a multiple ``18 倍``.
    """

    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    raw = format(number.normalize(), "f") if number == number.to_integral_value() else str(value)
    raw = _plain_decimal(raw)
    transform = condition.get("transform") or "level"
    if transform != "level":
        return f"{raw}%"
    unit = str(condition.get("unit") or "").lower()
    observed_by = condition.get("observed_by")
    if observed_by == "event_judgement" or unit == "count":
        return f"{raw} 次"
    if observed_by == "market_price":
        return f"{raw} 美元"
    if unit == "usd":
        from .numeric_display import format_display_number

        shown = format_display_number(number, kind="amount_usd")
        amount, _, suffix = shown.partition(" ")
        return f"{_plain_decimal(amount)} {suffix}".strip()
    if unit == "usd_per_share":
        return f"{raw} 美元/股"
    if unit == "ratio" or observed_by == "valuation":
        return f"{raw} 倍"
    if unit in {"percent", "pct", "%"}:
        return f"{raw}%"
    return f"{raw} {condition.get('unit')}" if condition.get("unit") else raw


def _percent_one_decimal(text: str | None) -> str | None:
    """``3%`` -> ``3.0%``: a growth or margin threshold reads with one decimal."""

    if text and text.endswith("%"):
        try:
            return f"{Decimal(text[:-1]):.1f}%"
        except (InvalidOperation, ValueError):
            return text
    return text


def condition_rule(condition: Mapping[str, Any]) -> str:
    """The condition as an analyst reads it (batch am, text evaluation 5.3).

    e.g. ``预警触发线：营业收入同比增速放缓至 3.0% 以下（观察至 2027-03-31）`` --
    built by the terminology registry's :func:`condition_sentence` from the
    structured fields, never a code expression (no ``<``, no ``rule:``).
    :func:`condition_rule_technical` keeps the machine form for 技术详情.
    """

    kind = str(condition.get("kind") or "trigger")
    horizon = condition.get("horizon")
    if condition.get("observed_by") == "owner":
        return condition_sentence(kind=kind, metric="", transform_label="", comparator="",
                                  threshold=None, horizon=horizon,
                                  owner_label=decision_text(condition.get("label")))
    threshold = _percent_one_decimal(condition_value_text(condition, condition.get("threshold")))
    comparator = str(condition.get("comparator") or "")
    if condition.get("observed_by") == "event_judgement":
        word = str(condition.get("metric_ref") or "").split(":", 1)[-1]
        event = DECISION_LABELS.get(word) or decision_text(condition.get("metric_label")) or "相关研判"
        return condition_sentence(kind=kind, metric="", transform_label="", comparator=comparator,
                                  threshold=threshold, horizon=horizon, count_event=event)
    return condition_sentence(kind=kind, metric=metric_display_label(condition),
                              transform_label=TRANSFORM_LABELS.get(condition.get("transform"), ""),
                              comparator=comparator, threshold=threshold, horizon=horizon)


def condition_rule_technical(condition: Mapping[str, Any]) -> str:
    """The rule as recorded: metric ref, transform code, raw threshold and unit."""

    if condition.get("observed_by") == "owner":
        return f"owner: {condition.get('label')} (horizon {condition.get('horizon')})"
    unit = condition.get("unit") or ""
    return (f"{condition.get('metric_ref')} [{condition.get('metric_label')}]"
            f"{TRANSFORM_CODES.get(condition.get('transform'), '')}"
            f" {COMPARATORS.get(condition.get('comparator'), '?')} {condition.get('threshold')}"
            f"{(' ' + unit) if unit else ''} (horizon {condition.get('horizon')})")


# ---------------------------------------------------------------------------
# observing: one cheap, deterministic read per condition
# ---------------------------------------------------------------------------


def _q(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


class Observer:
    """Reads the latest observed value of a condition's metric.

    Holds one connection and memoises the per-company reads (the driver model
    is a quarter-megabyte JSON record), so one tick reads each input once
    however many conditions point at it.  Never writes, never installs a
    schema: the cockpit and a read-only audit can use it as well as the lane.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        _row_factory(connection)
        self.connection = connection
        self._models: dict[str, Any] = {}
        self._prices: dict[str, Any] = {}
        self._valuations: dict[str, Any] = {}
        self._retired: set[str] | None = None

    # -- the four inputs ----------------------------------------------------

    def model(self, company_ref: str) -> dict[str, Any] | None:
        if company_ref not in self._models:
            record = None
            if table_exists(self.connection, "forecast_model_versions"):
                row = self.connection.execute(
                    "SELECT record_json FROM forecast_model_versions WHERE company_ref=? "
                    "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
                if row is not None:
                    try:
                        record = json.loads(row["record_json"])
                    except (TypeError, ValueError):
                        record = None
            self._models[company_ref] = record
        return self._models[company_ref]

    def price(self, company_ref: str) -> dict[str, Any] | None:
        if company_ref not in self._prices:
            found = None
            if table_exists(self.connection, "market_price_series_versions"):
                row = self.connection.execute(
                    "SELECT version_id, record_json, content_hash FROM market_price_series_versions "
                    "WHERE company_ref=? ORDER BY version_number DESC LIMIT 1",
                    (company_ref,)).fetchone()
                if row is not None:
                    try:
                        bars = json.loads(row["record_json"]).get("bars") or []
                    except (TypeError, ValueError):
                        bars = []
                    if bars:
                        bar = bars[-1]
                        found = {"value": str(bar.get("close")), "as_of": bar.get("date"),
                                 "source_ref": row["version_id"]}
            self._prices[company_ref] = found
        return self._prices[company_ref]

    def valuation(self, company_ref: str) -> dict[str, Any] | None:
        if company_ref not in self._valuations:
            found = None
            if table_exists(self.connection, "valuation_snapshot_versions"):
                row = self.connection.execute(
                    "SELECT version_id, record_json FROM valuation_snapshot_versions "
                    "WHERE company_ref=? ORDER BY version_number DESC LIMIT 1",
                    (company_ref,)).fetchone()
                if row is not None:
                    try:
                        record = json.loads(row["record_json"])
                    except (TypeError, ValueError):
                        record = {}
                    found = {"record": record, "version_ref": row["version_id"]}
            self._valuations[company_ref] = found
        return self._valuations[company_ref]

    def retired_claims(self) -> set[str]:
        if self._retired is None:
            try:
                from .claim_retirement import retired_claim_version_refs

                self._retired = retired_claim_version_refs(self.connection)
            except sqlite3.Error:
                self._retired = set()
        return self._retired

    # -- one metric ---------------------------------------------------------

    def driver_series(self, company_ref: str, driver_ref: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        model = self.model(company_ref)
        if not model:
            return None
        for driver in model.get("drivers") or ():
            if driver.get("ref") == driver_ref:
                history = [cell for cell in driver.get("history") or ()
                           if cell.get("value") not in (None, "") and cell.get("period_end")]
                history.sort(key=lambda cell: cell["period_end"])
                return driver, history
        return None

    def read(self, company_ref: str, observed_by: str, metric_ref: str | None,
             transform: str = "level", *, since: str | None = None) -> dict[str, Any] | None:
        """``{value, as_of, source_ref}`` or ``None``; ``{reason}`` when it cannot be computed."""

        if observed_by == "sec_actual":
            found = self.driver_series(company_ref, metric_ref or "")
            if found is None:
                return None
            driver, history = found
            if not history:
                return None
            latest = history[-1]
            model = self.model(company_ref) or {}
            accessions = list(latest.get("accessions") or ())
            source = (f"{model.get('id')}#{driver.get('ref')}@{latest['period_end']}"
                      + (f"|{accessions[0]}" if accessions else ""))
            current = decimal_text(latest["value"], "history value")
            if transform == "level":
                return {"value": str(current), "as_of": latest["period_end"], "source_ref": source}
            target = date.fromisoformat(latest["period_end"][:10])
            if transform == "yoy_pct":
                wanted = target - timedelta(days=365)
                prior = [cell for cell in history[:-1]
                         if abs((date.fromisoformat(cell["period_end"][:10]) - wanted).days) <= 20]
            else:
                prior = history[-2:-1]
            if not prior:
                return {"reason": "no_prior_period", "as_of": latest["period_end"],
                        "source_ref": source}
            base = decimal_text(prior[-1]["value"], "history value")
            if base == 0:
                return {"reason": "no_prior_period", "as_of": latest["period_end"],
                        "source_ref": source}
            change = (current / base - 1) * 100
            return {"value": _q(change), "as_of": latest["period_end"], "source_ref": source}
        if observed_by == "market_price":
            return self.price(company_ref)
        if observed_by == "valuation":
            found = self.valuation(company_ref)
            if found is None:
                return None
            name = (metric_ref or "").split(":", 1)[-1]
            for metric in found["record"].get("metrics") or ():
                if metric.get("metric") == name and metric.get("status") == "available" \
                        and metric.get("value") not in (None, ""):
                    return {"value": str(metric["value"]),
                            "as_of": found["record"].get("as_of"),
                            "source_ref": found["version_ref"]}
            return None
        if observed_by == "claim":
            if not table_exists(self.connection, "claim_versions"):
                return None
            rows = self.connection.execute(
                "SELECT claim_version_id, claim_json, created_at FROM claim_versions "
                "WHERE json_extract(claim_json,'$.subject_ref')=? "
                "AND json_extract(claim_json,'$.metric_or_aspect')=?",
                (company_ref, metric_ref)).fetchall()
            retired = self.retired_claims()
            best: tuple[str, str, dict[str, Any]] | None = None
            for row in rows:
                if row["claim_version_id"] in retired:
                    continue
                try:
                    claim = json.loads(row["claim_json"])
                except (TypeError, ValueError):
                    continue
                if claim.get("claim_kind") == "qualitative" or claim.get("value") in (None, ""):
                    continue
                period_end = str(claim.get("period") or "").split("..")[-1]
                key = (period_end, str(row["created_at"]), claim)
                if best is None or key[:2] > best[:2]:
                    best = key
            if best is None:
                return None
            period_end, _created, claim = best
            try:
                value = str(decimal_text(claim["value"], "claim value"))
            except CompanyScenarioValidationError:
                return None
            return {"value": value, "as_of": period_end or None,
                    "source_ref": str(claim.get("id"))}
        if observed_by == "company_fcf":
            model = self.model(company_ref) or {}
            contract = model.get("execution_contract") or {}
            bridge = ((contract.get("fcf_definition") or {}).get("historical_bridge") or {})
            company = next((c for c in bridge.get("columns") or [] if c.get("basis") == "company"), {})
            if company.get("value") is None: return None
            through = company.get("through")
            if not through or len(str(through)) != 10: return None
            return {"value": company["value"], "as_of": through, "source_ref": (contract.get("fcf_definition") or {}).get("source_ref")}
        if observed_by == "event_judgement":
            return None  # legacy circular condition remains readable, never fires
        return None


def _compare(value: Decimal, comparator: str, threshold: Decimal) -> bool:
    return {"gt": value > threshold, "gte": value >= threshold,
            "lt": value < threshold, "lte": value <= threshold}[comparator]


def evaluate_condition(
    condition: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
    *,
    today: date,
    base_date: str,
) -> dict[str, Any]:
    """One condition against one observed value.  Pure.

    ``base_date`` is the day the scenario version was written: a value whose
    own date is earlier was already known when the threshold was set, so it
    cannot *trigger* the scenario (it is reported, as ``predates_scenario``).
    """

    if condition.get("observed_by") == "owner":
        return {"status": "unobservable", "reason": "owner_observed", "value": None,
                "as_of": None, "source_ref": None}
    if observed is None or observed.get("value") is None:
        reason = (observed or {}).get("reason") or "no_value"
        return {"status": "unobservable", "reason": reason, "value": None,
                "as_of": (observed or {}).get("as_of"),
                "source_ref": (observed or {}).get("source_ref")}
    value = decimal_text(observed["value"], "observed value")
    met = _compare(value, str(condition["comparator"]),
                   decimal_text(condition["threshold"], "threshold"))
    base = {"value": str(observed["value"]), "as_of": observed.get("as_of"),
            "source_ref": observed.get("source_ref")}
    if today > date.fromisoformat(str(condition["horizon"])):
        return {"status": "not_triggered", "reason": "horizon_passed", **base}
    as_of = str(observed.get("as_of") or "")[:10]
    if met and condition.get("observed_by") != "event_judgement" and as_of and as_of < base_date[:10]:
        return {"status": "not_triggered", "reason": "predates_scenario", **base}
    return {"status": "triggered" if met else "not_triggered", "reason": "observed", **base}


# ---------------------------------------------------------------------------
# reading the ledger (connection-only, usable read-only)
# ---------------------------------------------------------------------------


def _decisions(connection: sqlite3.Connection, set_ref: str) -> list[dict[str, Any]]:
    return [_decode(row, "ScenarioDecision") for row in connection.execute(
        "SELECT record_json, content_hash FROM company_scenario_decisions WHERE set_ref=? "
        "ORDER BY created_at, decision_id", (set_ref,)).fetchall()]


def set_state(decisions: Sequence[Mapping[str, Any]]) -> str:
    """``accepted`` / ``rejected`` / ``deferred`` / ``pending``, from the decision rows."""

    final = [item for item in decisions if item.get("decision") in FINAL_DECISIONS]
    if final:
        return "accepted" if final[0]["decision"] == "accept" else "rejected"
    return "deferred" if decisions else "pending"


def read_sets(connection: sqlite3.Connection, company_ref: str | None = None) -> list[dict[str, Any]]:
    """Every set (oldest first) with its state and the decisions on it."""

    if not table_exists(connection, "company_scenario_sets"):
        return []
    _row_factory(connection)
    query = "SELECT record_json, content_hash FROM company_scenario_sets"
    params: tuple[Any, ...] = ()
    if company_ref is not None:
        query += " WHERE company_ref=?"
        params = (company_ref,)
    out = []
    for row in connection.execute(query + " ORDER BY created_at, set_id", params).fetchall():
        record = _decode(row, "ScenarioSet")
        decisions = _decisions(connection, record["id"])
        out.append({**record, "state": set_state(decisions), "decisions": decisions})
    return out


def read_set_versions(connection: sqlite3.Connection, set_ref: str) -> list[dict[str, Any]]:
    _row_factory(connection)
    return [_decode(row, "ScenarioVersion") for row in connection.execute(
        "SELECT record_json, content_hash FROM company_scenario_versions WHERE set_ref=? "
        "ORDER BY scenario_ref, version_number", (set_ref,)).fetchall()]


def read_version(connection: sqlite3.Connection, version_ref: str) -> dict[str, Any] | None:
    _row_factory(connection)
    row = connection.execute(
        "SELECT record_json, content_hash FROM company_scenario_versions WHERE version_id=?",
        (version_ref,)).fetchone()
    return None if row is None else _decode(row, "ScenarioVersion")


def accepted_set(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    """The set in force: the most recently *accepted* one (owner sets are self-accepted)."""

    best = None
    for item in read_sets(connection, company_ref):
        if item["state"] != "accepted":
            continue
        accepted_at = next(d["created_at"] for d in item["decisions"] if d["decision"] == "accept")
        if best is None or accepted_at >= best[0]:
            best = (accepted_at, item)
    return None if best is None else {**best[1], "accepted_at": best[0]}


def in_force(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """The live scenarios now: the newest version of each scenario of the accepted set.

    Owner revisions carry the accepted set's ref, so they are the newest
    version inside it; a retired scenario is left out.  Versions of a later,
    still undecided proposal carry a different set ref and are not in force.
    """

    current = accepted_set(connection, company_ref)
    if current is None:
        return []
    newest: dict[str, dict[str, Any]] = {}
    for version in read_set_versions(connection, current["id"]):
        if version["version"] >= newest.get(version["scenario_ref"], {}).get("version", 0):
            newest[version["scenario_ref"]] = version
    return sorted((item for item in newest.values() if item["state"] == "live"),
                  key=lambda item: (-Decimal(item["probability"]), item["key"]))


def pending_set(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    """The newest undecided (pending or deferred) automation set, if any."""

    for item in reversed(read_sets(connection, company_ref)):
        if item["origin"] == "automation" and item["state"] in {"pending", "deferred"}:
            return item
    return None


def latest_observation(connection: sqlite3.Connection, version_ref: str,
                       condition_id: str) -> dict[str, Any] | None:
    if not table_exists(connection, "company_scenario_observations"):
        return None
    _row_factory(connection)
    row = connection.execute(
        "SELECT record_json, content_hash FROM company_scenario_observations "
        "WHERE scenario_version_ref=? AND condition_id=? ORDER BY sequence DESC LIMIT 1",
        (version_ref, condition_id)).fetchone()
    return None if row is None else _decode(row, "ScenarioObservation")


def handed_off_events(connection: sqlite3.Connection, company_ref: str) -> dict[str, str]:
    """observation ref -> the ``scenario_trigger`` event it became."""

    if not table_exists(connection, "research_events"):
        return {}
    out: dict[str, str] = {}
    for row in connection.execute(
        "SELECT event_id, json_extract(record_json,'$.payload.observation_ref') AS ref "
        "FROM research_events WHERE company_ref=? AND kind='scenario_trigger'",
        (company_ref,)).fetchall():
        if row["ref"]:
            out[str(row["ref"])] = row["event_id"]
    return out


def _condition_view(version: Mapping[str, Any], condition: Mapping[str, Any],
                    connection: sqlite3.Connection, events: Mapping[str, str]) -> dict[str, Any]:
    seen = latest_observation(connection, version["id"], condition["condition_id"])
    status = None if seen is None else seen["status"]
    return {
        "condition_id": condition["condition_id"], "kind": condition["kind"],
        "label": condition["label"], "display_label": decision_text(condition["label"]),
        "rule": condition_rule(condition),
        "rule_technical": condition_rule_technical(condition),
        "metric_display_label": (None if condition["observed_by"] == "owner"
                                 else metric_display_label(condition)),
        "threshold_label": condition_value_text(condition, condition.get("threshold")),
        "observed_by": condition["observed_by"],
        "observer_label": OBSERVER_LABELS.get(condition["observed_by"]),
        "metric_ref": condition.get("metric_ref"), "threshold": condition.get("threshold"),
        "horizon": condition["horizon"], "baseline": condition.get("baseline"),
        "refs": list(condition.get("refs") or ()),
        "status": status, "status_label": STATUS_LABELS.get(status, "还没观察过"),
        "reason": None if seen is None else seen.get("reason"),
        "reason_label": None if seen is None else REASON_LABELS.get(seen.get("reason")),
        "value": None if seen is None else seen.get("value"),
        "value_label": None if seen is None else condition_value_text(condition, seen.get("value")),
        "as_of": None if seen is None else seen.get("value_as_of"),
        "source_ref": None if seen is None else seen.get("source_ref"),
        "observed_at": None if seen is None else seen.get("created_at"),
        "observation_ref": None if seen is None else seen["id"],
        "handed_off_event_ref": None if seen is None else events.get(seen["id"]),
    }


def scenario_view(connection: sqlite3.Connection, version: Mapping[str, Any],
                  events: Mapping[str, str]) -> dict[str, Any]:
    conditions = [_condition_view(version, item, connection, events)
                  for item in list(version["triggers"]) + list(version["invalidators"])]
    return {
        "scenario_ref": version["scenario_ref"], "version_ref": version["id"],
        "version_hash": version["content_hash"], "version": version["version"],
        "key": version["key"], "name": version["name"], "narrative": version["narrative"],
        "probability": version["probability"],
        "probability_label": f"{int((Decimal(version['probability']) * 100).to_integral_value())}%",
        "linked_thesis_refs": list(version.get("linked_thesis_refs") or ()),
        "linked_driver_refs": list(version.get("linked_driver_refs") or ()),
        "evidence_refs": list(version.get("evidence_refs") or ()),
        "actor_ref": version["actor_ref"], "change_reason": version["change_reason"],
        "created_at": version["created_at"],
        **{k:copy.deepcopy(version[k]) for k in ("driver_deltas","probability_basis","partition","pe_override","model_binding") if k in version},
        "triggers": [item for item in conditions if item["kind"] == "trigger"],
        "invalidators": [item for item in conditions if item["kind"] == "invalidator"],
    }


def company_view(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    """The cockpit's 「情景与触发」 panel for one company, or ``None``."""

    if not table_exists(connection, "company_scenario_sets"):
        return None
    sets = read_sets(connection, company_ref)
    if not sets:
        return None
    events = handed_off_events(connection, company_ref)
    live = [scenario_view(connection, item, events) for item in in_force(connection, company_ref)]
    pending = pending_set(connection, company_ref)
    pending_view = None
    if pending is not None:
        versions = read_set_versions(connection, pending["id"])
        pending_view = {
            "set_ref": pending["id"], "set_hash": pending["content_hash"],
            "created_at": pending["created_at"], "state": pending["state"],
            "scenarios": [scenario_view(connection, item, events) for item in versions],
        }
    fired = sum(1 for item in live for cond in item["triggers"] if cond["status"] == "triggered")
    broken = sum(1 for item in live for cond in item["invalidators"] if cond["status"] == "triggered")
    total = sum((Decimal(item["probability"]) for item in live), Decimal("0"))
    if live:
        headline = f"{len(live)} 个情景在跟踪"
        if fired:
            headline += f"，{fired} 个触发条件已触发"
        if broken:
            headline += f"，{_term('scenario', 'invalidated')} {broken} 项"
    else:
        headline = "情景提案等你裁决" if pending_view else "还没有采纳的情景"
    from .scenario_valuation import current,valuation_view
    valuation=current(connection,company_ref)
    return {
        "valuation":valuation_view(valuation),
        "headline": headline, "in_force": live, "pending": pending_view,
        "probability_sum": str(total.quantize(Decimal("0.01"))) if live else None,
        "triggered": fired, "invalidated": broken,
        "accepted_set_ref": (accepted_set(connection, company_ref) or {}).get("id"),
    }


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


def _assert_actor(actor_ref: str, mission: Mapping[str, Any] | None, scope: str) -> str:
    actor_ref = _text(actor_ref, "actor_ref", maximum=160)
    if actor_ref.startswith("automation:"):
        if mission is None:
            raise CompanyScenarioConflict("an automation write needs the mission it acts under")
        if actor_ref != mission["autonomy"]["automation_principal"]:
            raise CompanyScenarioConflict("automation actor is not the mission principal")
        if scope not in mission["autonomy"]["may_write"]:
            raise CompanyScenarioConflict(f"mission does not grant {scope} writes to automation")
    elif not actor_ref.startswith("human:"):
        raise CompanyScenarioValidationError("actor_ref must be a human: or automation: principal")
    return actor_ref


def validate_scenario(value: Any, *, today: date,
                      observables: Mapping[tuple[str, str], Mapping[str, Any]] | None,
                      allowed_refs: set[str] | None, thesis_refs: set[str] | None,
                      driver_refs: set[str] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanyScenarioValidationError("a scenario must be an object")
    fields = {"key", "name", "narrative", "probability", "linked_thesis_refs",
              "linked_driver_refs", "evidence_refs", "triggers", "invalidators",
              "driver_deltas", "probability_basis", "partition", "pe_override"}
    unknown = sorted(set(value) - fields)
    if unknown:
        raise CompanyScenarioValidationError(f"scenario has undeclared fields: {unknown}")
    key = _text(value.get("key"), "key", maximum=24)
    if not _KEY_RE.fullmatch(key):
        raise CompanyScenarioValidationError("key is a short lowercase slug, e.g. base / bull / bear")
    triggers = value.get("triggers") or []
    invalidators = value.get("invalidators") or []
    if not isinstance(triggers, list) or not isinstance(invalidators, list):
        raise CompanyScenarioValidationError("triggers and invalidators are lists")
    if not 1 <= len(triggers) <= MAX_TRIGGERS or len(invalidators) > MAX_INVALIDATORS:
        raise CompanyScenarioValidationError(
            f"a scenario has 1-{MAX_TRIGGERS} triggers and at most {MAX_INVALIDATORS} invalidators")
    conditions = [validate_condition(item, kind="trigger", today=today, observables=observables,
                                     allowed_refs=allowed_refs) for item in triggers]
    conditions += [validate_condition(item, kind="invalidator", today=today,
                                      observables=observables, allowed_refs=allowed_refs)
                   for item in invalidators]
    ids = [item["condition_id"] for item in conditions]
    if len(ids) != len(set(ids)):
        raise CompanyScenarioValidationError("condition ids repeat inside a scenario")
    return {
        **{k: copy.deepcopy(value[k]) for k in ("driver_deltas", "probability_basis", "partition", "pe_override") if k in value},
        "key": key,
        "name": _text(value.get("name"), "name", maximum=MAX_NAME_CHARS),
        "narrative": _text(value.get("narrative"), "narrative", maximum=MAX_NARRATIVE_CHARS),
        "probability": probability_text(value.get("probability")),
        "linked_thesis_refs": _refs(value.get("linked_thesis_refs"), "linked_thesis_refs",
                                    allowed=thesis_refs, nonempty=False),
        "linked_driver_refs": _refs(value.get("linked_driver_refs"), "linked_driver_refs",
                                    allowed=driver_refs, nonempty=False),
        "evidence_refs": _refs(value.get("evidence_refs"), "evidence_refs", allowed=allowed_refs),
        "triggers": [item for item in conditions if item["kind"] == "trigger"],
        "invalidators": [item for item in conditions if item["kind"] == "invalidator"],
    }


def check_probabilities(scenarios: Sequence[Mapping[str, Any]]) -> None:
    if not MIN_SCENARIOS <= len(scenarios) <= MAX_SCENARIOS:
        raise CompanyScenarioValidationError(
            f"a proposal names {MIN_SCENARIOS}-{MAX_SCENARIOS} scenarios")
    keys = [item["key"] for item in scenarios]
    if len(keys) != len(set(keys)):
        raise CompanyScenarioValidationError("scenario keys repeat")
    total = sum((Decimal(item["probability"]) for item in scenarios), Decimal("0"))
    if abs(total - 1) > PROBABILITY_SUM_TOLERANCE:
        raise CompanyScenarioValidationError(
            f"the probabilities of one proposal sum to 1 (they sum to {total})")


def model_binding(anchors: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{"model_version_ref", "model_version_hash"}`` a scenario's baselines rest on.

    Read from the set's anchors (the model version the proposer was shown).
    ``None`` refs when the company had no model: the scenario then has no
    model baseline, and the consistency check reports it as unbound.
    """

    anchors = anchors if isinstance(anchors, Mapping) else {}
    ref = anchors.get("model_version_ref")
    return {"model_version_ref": None if not ref else str(ref),
            "model_version_hash": None if not ref else anchors.get("model_version_hash")}


class CompanyScenarioAuthority:
    """Sets, versions, decisions and observations.  Writes only through a DaltonStore."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("CompanyScenarioAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reads ---------------------------------------------------------------

    def sets(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        return read_sets(self.connection, company_ref)

    def get_set(self, set_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json, content_hash FROM company_scenario_sets WHERE set_id=?",
            (set_ref,)).fetchone()
        record = _decode(row, "ScenarioSet")
        decisions = _decisions(self.connection, set_ref)
        return {**record, "state": set_state(decisions), "decisions": decisions,
                "versions": read_set_versions(self.connection, set_ref)}

    def in_force(self, company_ref: str) -> list[dict[str, Any]]:
        return in_force(self.connection, company_ref)

    def pending_set(self, company_ref: str) -> dict[str, Any] | None:
        return pending_set(self.connection, company_ref)

    def companies(self) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT DISTINCT company_ref FROM company_scenario_sets ORDER BY company_ref")]

    def observations(self, company_ref: str) -> list[dict[str, Any]]:
        return [_decode(row, "ScenarioObservation") for row in self.connection.execute(
            "SELECT record_json, content_hash FROM company_scenario_observations "
            "WHERE company_ref=? ORDER BY created_at, observation_id", (company_ref,))]

    # -- writes --------------------------------------------------------------

    def propose_set(
        self,
        *,
        company_ref: str,
        scenarios: Sequence[Mapping[str, Any]],
        anchors: Mapping[str, Any],
        proposer: Mapping[str, Any] | None,
        dropped_conditions: Sequence[Mapping[str, Any]] = (),
        mission: Mapping[str, Any],
        actor_ref: str,
        today: date | None = None,
        observables: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
        allowed_refs: set[str] | None = None,
        thesis_refs: set[str] | None = None,
        driver_refs: set[str] | None = None,
    ) -> dict[str, Any]:
        """Write one proposal (automation) or one owner-authored set.

        Automation: the actor is the mission principal with the ``dossier``
        scope, the company is in the universe, and at most one undecided
        automation set exists per company -- a second proposal while the
        owner has not answered the first is noise, not news.  An owner set is
        accepted in the same transaction, because the owner wrote it.
        """

        company_ref = _text(company_ref, "company_ref")
        actor_ref = _assert_actor(actor_ref, mission, PROPOSAL_WRITE_SCOPE)
        if not any(member["company_ref"] == company_ref for member in mission["universe"]):
            raise CompanyScenarioConflict("company is not in the mission universe")
        day = today or datetime.now(timezone.utc).date()
        cleaned = [validate_scenario(item, today=day, observables=observables,
                                     allowed_refs=allowed_refs, thesis_refs=thesis_refs,
                                     driver_refs=driver_refs) for item in scenarios]
        check_probabilities(cleaned)
        from .scenario_rules import validate_new_scenario_rules
        try:
            assessment_day = date.fromisoformat(str(anchors.get("price_as_of") or day.isoformat()))
            rule_ref = validate_new_scenario_rules(cleaned, anchors, today=assessment_day)
            if rule_ref=="rule:scenario-partition-12m:1":
                _validate_proposal_authority(self.connection,company_ref,cleaned,anchors,allowed_refs)
        except ValueError as exc:
            raise CompanyScenarioValidationError(str(exc)) from exc
        origin = "automation" if actor_ref.startswith("automation:") else "owner"
        created_at = _now()
        with self.store._transaction() as cur:
            if origin == "automation":
                for item in read_sets(self.connection, company_ref):
                    if item["origin"] == "automation" and item["state"] in {"pending", "deferred"}:
                        return {**item, "status": "pending_exists"}
            set_id = "company-scenario-set:" + content_hash({
                "company": company_ref, "scenarios": cleaned, "at": created_at,
                "actor": actor_ref})[:32]
            version_records = []
            for item in cleaned:
                scenario_ref = scenario_ref_for(company_ref, item["key"])
                prior = cur.execute(
                    "SELECT version_id, version_number FROM company_scenario_versions "
                    "WHERE scenario_ref=? ORDER BY version_number DESC LIMIT 1",
                    (scenario_ref,)).fetchone()
                number = 1 if prior is None else int(prior["version_number"]) + 1
                version_records.append(_record({
                    "schema_version": SCHEMA_VERSION,
                    "condition_rules_ref": rule_ref,
                    "id": "company-scenario-version:" + content_hash(
                        {"ref": scenario_ref, "version": number})[:32],
                    "scenario_ref": scenario_ref, "version": number,
                    "prior_version_ref": None if prior is None else prior["version_id"],
                    "set_ref": set_id, "company_ref": company_ref, **item,
                    # Batch an: the model version every baseline above was
                    # read from; product_consistency marks the scenario stale
                    # once that model has a newer version.
                    "model_binding": model_binding(anchors),
                    "state": "live",
                    "change_reason": "proposed" if origin == "automation" else "owner_authored",
                    "reason": None, "idempotency_key": None,
                    "mission_version_ref": mission["id"],
                    "mission_version_hash": mission["content_hash"],
                    "actor_ref": actor_ref, "created_at": created_at,
                }))
            set_record = _record({
                "schema_version": SCHEMA_VERSION, "id": set_id,
                "company_ref": company_ref, "origin": origin,
                "anchors": json.loads(canonical_json(dict(anchors))),
                "proposer": None if proposer is None else json.loads(canonical_json(dict(proposer))),
                "scenario_keys": [item["key"] for item in cleaned],
                "scenario_version_refs": [item["id"] for item in version_records],
                "scenario_version_hashes": [item["content_hash"] for item in version_records],
                "dropped_conditions": json.loads(canonical_json(list(dropped_conditions))),
                "mission_version_ref": mission["id"],
                "mission_version_hash": mission["content_hash"],
                "actor_ref": actor_ref, "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO company_scenario_sets(set_id,company_ref,origin,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                (set_id, company_ref, origin, canonical_json(set_record),
                 set_record["content_hash"], actor_ref, created_at))
            for version in version_records:
                cur.execute(
                    "INSERT INTO company_scenario_versions(version_id,scenario_ref,version_number,"
                    "prior_version_id,set_ref,company_ref,record_json,content_hash,actor_ref,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (version["id"], version["scenario_ref"], version["version"],
                     version["prior_version_ref"], set_id, company_ref,
                     canonical_json(version), version["content_hash"], actor_ref, created_at))
            if origin == "owner":
                self._insert_decision(cur, set_record=set_record, decision="accept",
                                      reason="owner-authored set", actor_ref=actor_ref,
                                      idempotency_key=None, created_at=created_at)
        return {**self.get_set(set_id), "status": "published"}

    @staticmethod
    def _insert_decision(cur: Any, *, set_record: Mapping[str, Any], decision: str,
                         reason: str, actor_ref: str, idempotency_key: str | None,
                         created_at: str) -> dict[str, Any]:
        record = _record({
            "schema_version": SCHEMA_VERSION,
            "id": "company-scenario-decision:" + content_hash({
                "set": set_record["id"], "decision": decision, "at": created_at,
                "key": idempotency_key})[:32],
            "set_ref": set_record["id"], "set_hash": set_record["content_hash"],
            "company_ref": set_record["company_ref"], "decision": decision,
            "reason": reason, "idempotency_key": idempotency_key,
            "actor_ref": actor_ref, "created_at": created_at,
        })
        cur.execute(
            "INSERT INTO company_scenario_decisions(decision_id,set_ref,set_hash,company_ref,"
            "decision,record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (record["id"], record["set_ref"], record["set_hash"], record["company_ref"],
             decision, canonical_json(record), record["content_hash"], actor_ref, created_at))
        return record

    def decide_set(self, *, set_ref: str, set_hash: str, decision: str, reason: str,
                   actor_ref: str, idempotency_key: str | None = None) -> dict[str, Any]:
        """A person's answer to one proposal, bound to the bytes they read."""

        actor_ref = _text(actor_ref, "actor_ref", maximum=160)
        if not actor_ref.startswith("human:"):
            raise CompanyScenarioConflict("only a person decides a scenario proposal")
        if decision not in DECISIONS:
            raise CompanyScenarioValidationError(f"decision must be one of {list(DECISIONS)}")
        reason = _text(reason, "reason", maximum=4000)
        if not isinstance(set_hash, str) or not _HASH_RE.fullmatch(set_hash):
            raise CompanyScenarioValidationError("set_hash must be a sha256 hex digest")
        before_force = None
        with self.store._transaction() as cur:
            row = cur.execute(
                "SELECT record_json, content_hash FROM company_scenario_sets WHERE set_id=?",
                (set_ref,)).fetchone()
            set_record = _decode(row, "ScenarioSet")
            if set_record["content_hash"] != set_hash:
                raise CompanyScenarioConflict("the proposal changed since it was read")
            decisions = _decisions(self.connection, set_ref)
            if idempotency_key is not None:
                for item in decisions:
                    if item.get("idempotency_key") == idempotency_key:
                        if item["decision"] != decision:
                            raise CompanyScenarioConflict(
                                "idempotency key was already used for a different decision")
                        return {**item, "status": "replayed"}
            state = set_state(decisions)
            if state in {"accepted", "rejected"}:
                raise CompanyScenarioConflict(f"this proposal is already {state}")
            if decision == "accept":
                before = accepted_set(self.connection, set_record["company_ref"])
                if before is not None and before["created_at"] > set_record["created_at"]:
                    raise CompanyScenarioConflict(
                        "a newer proposal for this company is already accepted")
                before_force = in_force(self.connection, set_record["company_ref"])
            written = self._insert_decision(
                cur, set_record=set_record, decision=decision, reason=reason,
                actor_ref=actor_ref, idempotency_key=idempotency_key, created_at=_now())
        changes = []
        if decision == "accept":
            changes = probability_changes(before_force or [], self.in_force(set_record["company_ref"]))
        return {**written, "status": "decided", "probability_changes": changes}

    def revise(self, *, scenario_ref: str, version_ref: str, version_hash: str,
               probability: Any = None, retire: bool = False, reason: str,
               actor_ref: str, mission: Mapping[str, Any],
               idempotency_key: str | None = None) -> dict[str, Any]:
        """The owner moves a probability or retires a scenario that is in force."""

        actor_ref = _text(actor_ref, "actor_ref", maximum=160)
        if not actor_ref.startswith("human:"):
            raise CompanyScenarioConflict("only a person revises an adopted scenario")
        reason = _text(reason, "reason", maximum=1200)
        if not retire and probability is None:
            raise CompanyScenarioValidationError("a revision changes the probability or retires")
        new_probability = None if probability is None else probability_text(probability)
        with self.store._transaction() as cur:
            current = read_version(self.connection, version_ref)
            if current is None or current["scenario_ref"] != scenario_ref:
                raise CompanyScenarioConflict("that scenario version does not exist")
            if current["content_hash"] != version_hash:
                raise CompanyScenarioConflict("the scenario changed since it was read")
            if idempotency_key is not None:
                row = cur.execute(
                    "SELECT record_json, content_hash FROM company_scenario_versions "
                    "WHERE scenario_ref=? AND json_extract(record_json,'$.idempotency_key')=?",
                    (scenario_ref, idempotency_key)).fetchone()
                if row is not None:
                    return {**_decode(row, "ScenarioVersion"), "status": "replayed",
                            "probability_changes": []}
            live = {item["id"]: item for item in in_force(self.connection, current["company_ref"])}
            if version_ref not in live:
                raise CompanyScenarioConflict("only the version in force can be revised")
            if current.get("condition_rules_ref")=="rule:scenario-partition-12m:1" and (retire or new_probability!=current["probability"]):
                raise CompanyScenarioValidationError("new three-scenario probabilities must be revised together through revise_group")
            if not retire and new_probability == current["probability"]:
                return {**current, "status": "duplicate", "probability_changes": []}
            latest = cur.execute(
                "SELECT MAX(version_number) AS n FROM company_scenario_versions WHERE scenario_ref=?",
                (scenario_ref,)).fetchone()
            number = int(latest["n"]) + 1
            created_at = _now()
            record = _record({
                **{key: current[key] for key in current if key not in {"content_hash"}},
                "id": "company-scenario-version:" + content_hash(
                    {"ref": scenario_ref, "version": number})[:32],
                "version": number, "prior_version_ref": current["id"],
                "probability": new_probability or current["probability"],
                "state": "retired" if retire else "live",
                "change_reason": "owner_retired" if retire else "owner_revision",
                "reason": reason, "idempotency_key": idempotency_key,
                "mission_version_ref": mission["id"],
                "mission_version_hash": mission["content_hash"],
                "actor_ref": actor_ref, "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO company_scenario_versions(version_id,scenario_ref,version_number,"
                "prior_version_id,set_ref,company_ref,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record["id"], scenario_ref, number, current["id"], record["set_ref"],
                 record["company_ref"], canonical_json(record), record["content_hash"],
                 actor_ref, created_at))
        changes = [] if retire else probability_changes([current], [record])
        return {**record, "status": "published", "probability_changes": changes}

    def revise_group(self, *, company_ref: str, changes: Any, reason: str | None,
                     actor_ref: str, mission: Mapping[str, Any],
                     idempotency_key: str | None = None) -> dict[str, Any]:
        """The owner's whole probability group in one append-only transaction.

        2026-10-09 (UX review C23/C24): the cockpit sent one ``revise`` per
        scenario, so a stale row could leave half a group saved.  Here every
        scenario in force is named once with the version the owner read; any
        stale version, a kept set under ``MIN_SCENARIOS`` or a sum other than
        100% refuses the whole group and nothing is written.  Unchanged
        scenarios get no new version. New exhaustive partitions require an
        explicit probability rationale; historical groups retain their old rule.
        """

        actor_ref = _text(actor_ref, "actor_ref", maximum=160)
        if not actor_ref.startswith("human:"):
            raise CompanyScenarioConflict("only a person revises an adopted scenario")
        company_ref = _text(company_ref, "company_ref", maximum=512)
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)) or not changes:
            raise CompanyScenarioValidationError("changes must be a non-empty list")
        if len(changes) > MAX_SCENARIOS:
            raise CompanyScenarioValidationError(f"a group has at most {MAX_SCENARIOS} scenarios")
        wanted: dict[str, dict[str, Any]] = {}
        for item in changes:
            if not isinstance(item, Mapping):
                raise CompanyScenarioValidationError("each change must be an object")
            unknown = sorted(set(item) - {"scenario_ref", "version_ref", "version_hash",
                                          "probability", "retire"})
            if unknown:
                raise CompanyScenarioValidationError(f"change has undeclared fields: {unknown}")
            ref = _text(item.get("scenario_ref"), "scenario_ref", maximum=240)
            if ref in wanted:
                raise CompanyScenarioValidationError("a scenario is named twice in one group")
            retire = item.get("retire", False)
            if not isinstance(retire, bool):
                raise CompanyScenarioValidationError("retire must be true or false")
            digest = item.get("version_hash")
            if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
                raise CompanyScenarioValidationError("version_hash must be a sha256 hex digest")
            probability = None
            if not retire:
                if item.get("probability") is None:
                    raise CompanyScenarioValidationError(
                        "a kept scenario carries its probability")
                probability = probability_text(item.get("probability"))
            wanted[ref] = {"scenario_ref": ref,
                           "version_ref": _text(item.get("version_ref"), "version_ref", maximum=240),
                           "version_hash": digest, "probability": probability, "retire": retire}
        kept = [item for item in wanted.values() if not item["retire"]]
        if len(kept) < MIN_SCENARIOS:
            raise CompanyScenarioValidationError(
                f"keep at least {MIN_SCENARIOS} scenarios; to drop the whole group reject it")
        total = sum((Decimal(item["probability"]) for item in kept), Decimal("0"))
        if abs(total - 1) > PROBABILITY_SUM_TOLERANCE:
            raise CompanyScenarioValidationError(
                f"the kept probabilities sum to 1 (they sum to {total})")
        why = reason.strip() if isinstance(reason, str) else ""
        if len(why) > 1200:
            raise CompanyScenarioValidationError("reason must be at most 1200 characters")
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key", maximum=200)
        request_digest = content_hash({"company": company_ref,
                                       "changes": sorted(wanted.values(),
                                                         key=lambda x: x["scenario_ref"]),
                                       "reason": why})
        with self.store._transaction() as cur:
            if idempotency_key is not None:
                stored = []
                for ref in wanted:
                    row = cur.execute(
                        "SELECT record_json, content_hash FROM company_scenario_versions "
                        "WHERE scenario_ref=? AND json_extract(record_json,'$.idempotency_key')=?",
                        (ref, f"{idempotency_key}#{ref}")).fetchone()
                    if row is not None:
                        stored.append(_decode(row, "ScenarioVersion"))
                if stored:
                    if any(item.get("revision_request_hash") != request_digest for item in stored):
                        raise CompanyScenarioConflict(
                            "idempotency key was already used for a different group")
                    return {"status": "replayed", "company_ref": company_ref,
                            "group_ref": stored[0].get("revision_group_ref"),
                            "versions": stored, "probability_changes": [],
                            "reason": stored[0].get("reason"),
                            "reason_auto": bool(stored[0].get("reason_auto"))}
            before = in_force(self.connection, company_ref)
            live = {item["scenario_ref"]: item for item in before}
            strict_group = any(v.get("condition_rules_ref")=="rule:scenario-partition-12m:1" for v in before)
            if strict_group:
                if not why:
                    raise CompanyScenarioValidationError("new scenario probabilities require a non-empty evidence rationale")
                if len(kept)!=3 or any(item["retire"] for item in wanted.values()):
                    raise CompanyScenarioValidationError("retiring part of an exhaustive three-scenario partition requires a new complete proposal")
            if set(live) != set(wanted):
                raise CompanyScenarioConflict(
                    "the group no longer matches the scenarios in force")
            for ref, item in wanted.items():
                current = live[ref]
                if current["id"] != item["version_ref"] or current["content_hash"] != item["version_hash"]:
                    raise CompanyScenarioConflict("the scenario changed since it was read")
            moved = [item for item in wanted.values()
                     if item["retire"] or item["probability"] != live[item["scenario_ref"]]["probability"]]
            if not moved:
                return {"status": "duplicate", "company_ref": company_ref, "group_ref": None,
                        "versions": [], "probability_changes": [], "reason": why,
                        "reason_auto": False}
            template_reset = strict_group and {
                live[item["scenario_ref"]]["key"]:Decimal(item["probability"]) for item in kept
            } == {"bear":Decimal(".25"), "base":Decimal(".5"), "bull":Decimal(".25")}
            if template_reset:
                # The complete default distribution has one explicit prior basis.
                # An unchanged weight must not retain an evidence-assessment label.
                moved = list(wanted.values())
            reason_auto = not why
            if reason_auto:
                why = group_reason(live, moved)
            created_at = _now()
            group_ref = "company-scenario-revision-group:" + content_hash({
                "company": company_ref, "key": idempotency_key or created_at,
                "request": request_digest})[:32]
            written = []
            for item in sorted(moved, key=lambda x: x["scenario_ref"]):
                current = live[item["scenario_ref"]]
                latest = cur.execute(
                    "SELECT MAX(version_number) AS n FROM company_scenario_versions "
                    "WHERE scenario_ref=?", (item["scenario_ref"],)).fetchone()
                number = int(latest["n"]) + 1
                record = _record({
                    **{key: current[key] for key in current if key not in {"content_hash"}},
                    "id": "company-scenario-version:" + content_hash(
                        {"ref": item["scenario_ref"], "version": number})[:32],
                    "version": number, "prior_version_ref": current["id"],
                    "probability": item["probability"] or current["probability"],
                    **({"probability_basis":{"kind":"template_prior" if template_reset else "owner_assessment","because":why,"as_of":created_at[:10],
                        "source_refs":list(current.get("evidence_refs") or [])+[current["id"]]}} if current.get("condition_rules_ref")=="rule:scenario-partition-12m:1" else {}),
                    "state": "retired" if item["retire"] else "live",
                    "change_reason": "owner_retired" if item["retire"] else "owner_revision",
                    "reason": why, "reason_auto": reason_auto,
                    "idempotency_key": (None if idempotency_key is None
                                        else f"{idempotency_key}#{item['scenario_ref']}"),
                    "revision_group_ref": group_ref, "revision_group_size": len(moved),
                    "revision_request_hash": request_digest,
                    "mission_version_ref": mission["id"],
                    "mission_version_hash": mission["content_hash"],
                    "actor_ref": actor_ref, "created_at": created_at,
                })
                cur.execute(
                    "INSERT INTO company_scenario_versions(version_id,scenario_ref,version_number,"
                    "prior_version_id,set_ref,company_ref,record_json,content_hash,actor_ref,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (record["id"], item["scenario_ref"], number, current["id"], record["set_ref"],
                     record["company_ref"], canonical_json(record), record["content_hash"],
                     actor_ref, created_at))
                written.append(record)
        after = in_force(self.connection, company_ref)
        return {"status": "published", "company_ref": company_ref, "group_ref": group_ref,
                "versions": written, "probability_changes": probability_changes(before, after),
                "reason": why, "reason_auto": reason_auto}

    def record_observation(
        self,
        *,
        version: Mapping[str, Any],
        condition: Mapping[str, Any],
        outcome: Mapping[str, Any],
        in_force: bool,
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """Append what was seen, unless it is what was seen last time."""

        actor_ref = _assert_actor(actor_ref, mission, OBSERVATION_WRITE_SCOPE)
        if outcome.get("status") not in STATUSES:
            raise CompanyScenarioValidationError(f"status must be one of {list(STATUSES)}")
        with self.store._transaction() as cur:
            row = cur.execute(
                "SELECT record_json, content_hash, sequence FROM company_scenario_observations "
                "WHERE scenario_version_ref=? AND condition_id=? ORDER BY sequence DESC LIMIT 1",
                (version["id"], condition["condition_id"])).fetchone()
            prior = None if row is None else _decode(row, "ScenarioObservation")
            facts = {"status": outcome["status"], "reason": outcome.get("reason"),
                     "value": outcome.get("value"), "value_as_of": outcome.get("as_of"),
                     "source_ref": outcome.get("source_ref"), "in_force": bool(in_force)}
            if prior is not None and all(prior.get(key) == facts[key] for key in facts):
                return {**prior, "status_change": "unchanged"}
            sequence = 1 if prior is None else int(row["sequence"]) + 1
            created_at = _now()
            record = _record({
                "schema_version": SCHEMA_VERSION,
                "id": "company-scenario-observation:" + content_hash({
                    "version": version["id"], "condition": condition["condition_id"],
                    "sequence": sequence})[:32],
                "scenario_version_ref": version["id"],
                "scenario_version_hash": version["content_hash"],
                "scenario_ref": version["scenario_ref"], "set_ref": version["set_ref"],
                "company_ref": version["company_ref"],
                "condition_id": condition["condition_id"], "condition_kind": condition["kind"],
                "observed_by": condition["observed_by"], "metric_ref": condition.get("metric_ref"),
                "transform": condition.get("transform"), "comparator": condition.get("comparator"),
                "threshold": condition.get("threshold"), "horizon": condition["horizon"],
                **facts,
                "prior_status": None if prior is None else prior["status"],
                "prior_observation_ref": None if prior is None else prior["id"],
                "sequence": sequence, "evaluator_ref": EVALUATOR_REF,
                "mission_version_ref": mission["id"], "actor_ref": actor_ref,
                "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO company_scenario_observations(observation_id,scenario_version_ref,"
                "condition_id,sequence,company_ref,status,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record["id"], version["id"], condition["condition_id"], sequence,
                 version["company_ref"], outcome["status"], canonical_json(record),
                 record["content_hash"], actor_ref, created_at))
        # Fired: an in-force condition is triggered now and was not, as far as
        # an in-force reading goes, the last time -- so a condition that turned
        # while its proposal waited fires on the tick after the owner accepts.
        fired = (outcome["status"] == "triggered" and in_force
                 and (prior is None or prior["status"] != "triggered"
                      or not prior.get("in_force")))
        change = "fired" if fired else "changed"
        return {**record, "status_change": change}


SCENARIO_KEY_NAMES = {"bull": "乐观", "base": "基准", "bear": "悲观"}


def _percent(text: Any) -> str:
    value = (Decimal(str(text)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return f"{value}%"


def group_reason(live: Mapping[str, Mapping[str, Any]],
                 moved: Sequence[Mapping[str, Any]]) -> str:
    """The generated reason of a group revision written without one."""

    def name(ref: str) -> str:
        current = live[ref]
        return SCENARIO_KEY_NAMES.get(current.get("key"), str(current.get("name") or "情景"))

    order = {"bull": 0, "base": 1, "bear": 2}
    items = sorted(moved, key=lambda x: (order.get(live[x["scenario_ref"]].get("key"), 9),
                                         x["scenario_ref"]))
    moves = [f"{name(x['scenario_ref'])} {_percent(live[x['scenario_ref']]['probability'])}→"
             f"{_percent(x['probability'])}" for x in items if not x["retire"]]
    retired = [name(x["scenario_ref"]) for x in items if x["retire"]]
    parts = list(moves) + ([f"停用：{'、'.join(retired)}"] if retired else [])
    return ("调整情景概率：" + "；".join(parts))[:1200]


def probability_changes(before: Sequence[Mapping[str, Any]],
                        after: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Scenarios whose in-force probability moved by at least the material step.

    Only scenarios that had a probability before count: the first adoption of
    a company's scenarios is a baseline, not a move.
    """

    prior = {item["scenario_ref"]: Decimal(item["probability"]) for item in before}
    out = []
    for item in after:
        if item["scenario_ref"] not in prior:
            continue
        delta = Decimal(item["probability"]) - prior[item["scenario_ref"]]
        if abs(delta) >= MATERIAL_PROBABILITY_CHANGE:
            out.append({"scenario_ref": item["scenario_ref"], "version_ref": item["id"],
                        "name": item["name"], "from": str(prior[item["scenario_ref"]]),
                        "to": item["probability"], "delta": str(delta)})
    return out


# ---------------------------------------------------------------------------
# the weekly brief's section (deterministic; replayed by ref and hash)
# ---------------------------------------------------------------------------


def brief_entries(connection: sqlite3.Connection, company_refs: Sequence[str],
                  period_start: datetime, period_end: datetime) -> list[dict[str, Any]]:
    """Scenario state per company at publish time, plus what moved in the window.

    Each entry names exact refs and hashes so the brief can replay it:
    the in-force versions (with each condition's latest status at or before
    ``period_end``), decisions and observations created inside the window.
    """

    if not table_exists(connection, "company_scenario_sets"):
        return []
    _row_factory(connection)
    end_text = period_end.isoformat()
    out = []
    for company_ref in sorted(company_refs):
        sets = [item for item in read_sets(connection, company_ref)
                if item["created_at"] <= end_text]
        if not sets:
            continue
        live = in_force(connection, company_ref)
        live = [item for item in live if item["created_at"] <= end_text]
        scenarios = []
        for version in live:
            conditions = []
            for condition in list(version["triggers"]) + list(version["invalidators"]):
                row = connection.execute(
                    "SELECT observation_id, content_hash, status FROM company_scenario_observations "
                    "WHERE scenario_version_ref=? AND condition_id=? AND created_at<=? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (version["id"], condition["condition_id"], end_text)).fetchone() \
                    if table_exists(connection, "company_scenario_observations") else None
                conditions.append({
                    "condition_id": condition["condition_id"], "kind": condition["kind"],
                    "rule": condition_rule(condition),
                    "status": None if row is None else row["status"],
                    "observation_ref": None if row is None else row["observation_id"],
                    "observation_hash": None if row is None else row["content_hash"],
                })
            scenarios.append({"ref": version["id"], "hash": version["content_hash"],
                              "name": version["name"], "probability": version["probability"],
                              "conditions": conditions})
        moved = []
        for item in sets:
            for decision in item["decisions"]:
                created = datetime.fromisoformat(decision["created_at"])
                if period_start < created <= period_end:
                    moved.append({"kind": "decision", "ref": decision["id"],
                                  "hash": decision["content_hash"],
                                  "decision": decision["decision"], "set_ref": item["id"]})
            created = datetime.fromisoformat(item["created_at"])
            if item["origin"] == "automation" and period_start < created <= period_end:
                moved.append({"kind": "proposal", "ref": item["id"], "hash": item["content_hash"],
                              "decision": None, "set_ref": item["id"]})
        if table_exists(connection, "company_scenario_observations"):
            for row in connection.execute(
                "SELECT observation_id, content_hash, status, record_json FROM "
                "company_scenario_observations WHERE company_ref=? AND created_at>? AND "
                "created_at<=? ORDER BY created_at, observation_id",
                (company_ref, period_start.isoformat(), end_text)).fetchall():
                record = json.loads(row["record_json"])
                if record.get("prior_status") == row["status"] or not record.get("in_force"):
                    continue
                moved.append({"kind": "observation", "ref": row["observation_id"],
                              "hash": row["content_hash"], "decision": row["status"],
                              "set_ref": record.get("set_ref")})
        out.append({"company_ref": company_ref, "scenarios": scenarios, "moved": moved})
    return out


def replay_brief_entries(connection: sqlite3.Connection, entries: Any) -> None:
    """Every ref the frozen section names still resolves to the same bytes."""

    if entries is None:
        return
    if not isinstance(entries, list):
        raise CompanyScenarioConflict("weekly brief scenario section is malformed")
    _row_factory(connection)
    tables = {"decision": ("company_scenario_decisions", "decision_id"),
              "proposal": ("company_scenario_sets", "set_id"),
              "observation": ("company_scenario_observations", "observation_id"),
              "version": ("company_scenario_versions", "version_id")}

    def check(kind: str, ref: Any, digest: Any) -> None:
        table, key = tables[kind]
        row = connection.execute(
            f"SELECT content_hash FROM {table} WHERE {key}=?", (ref,)).fetchone() \
            if table_exists(connection, table) else None
        if row is None or row["content_hash"] != digest:
            raise CompanyScenarioConflict("weekly brief scenario section no longer replays")

    for entry in entries:
        for scenario in entry.get("scenarios") or ():
            check("version", scenario.get("ref"), scenario.get("hash"))
            for condition in scenario.get("conditions") or ():
                if condition.get("observation_ref") is not None:
                    check("observation", condition["observation_ref"],
                          condition.get("observation_hash"))
        for moved in entry.get("moved") or ():
            if moved.get("kind") not in {"decision", "proposal", "observation"}:
                raise CompanyScenarioConflict("weekly brief scenario section is malformed")
            check(moved["kind"], moved.get("ref"), moved.get("hash"))


def brief_lines(entries: Sequence[Mapping[str, Any]], tickers: Mapping[str, str]) -> list[str]:
    """The section's markdown lines, in Chinese."""

    lines: list[str] = []
    if not entries:
        return ["- 本期还没有任何公司的结构化情景；情景提案经你采纳后才会在这里跟踪。"]
    for entry in entries:
        ticker = tickers.get(entry["company_ref"], entry["company_ref"])
        if not entry["scenarios"]:
            lines.append(f"- {ticker}：有情景提案，尚未采纳，不跟踪触发。")
        for scenario in entry["scenarios"]:
            pct = int((Decimal(scenario["probability"]) * 100).to_integral_value())
            lines.append(f"- {ticker}｜{scenario['name']}（{pct}%）｜{scenario['ref']}")
            for condition in scenario["conditions"]:
                status = STATUS_LABELS.get(condition["status"], "还没观察过")
                lines.append(f"  - {condition['rule']}——{status}")
        for moved in entry["moved"]:
            if moved["kind"] == "decision":
                word = {"accept": "采纳", "reject": "驳回", "defer": "暂缓"}.get(
                    moved["decision"], moved["decision"])
                lines.append(f"  - 本期你{word}了一组情景提案｜{moved['set_ref']}")
            elif moved["kind"] == "proposal":
                lines.append(f"  - 本期新提交了一组情景提案，等你裁决｜{moved['ref']}")
            else:
                lines.append(f"  - 本期条件状态变为「{STATUS_LABELS.get(moved['decision'])}」｜{moved['ref']}")
    return lines


__all__ = [
    "CHANGE_REASONS", "COMPARATORS", "CompanyScenarioAuthority", "CompanyScenarioConflict",
    "CompanyScenarioError", "CompanyScenarioValidationError", "DECISIONS", "EVALUATOR_REF",
    "MATERIAL_PROBABILITY_CHANGE", "OBSERVATION_WRITE_SCOPE", "OBSERVERS", "Observer",
    "PROPOSAL_WRITE_SCOPE", "STATUSES", "STATUS_LABELS", "TRANSFORMS", "accepted_set",
    "brief_entries", "brief_lines", "check_probabilities", "company_view", "condition_rule",
    "condition_rule_technical", "condition_value_text", "decision_text", "metric_display_label",
    "evaluate_condition", "in_force", "pending_set", "probability_changes",
    "probability_text", "read_sets", "read_set_versions", "replay_brief_entries",
    "scenario_ref_for", "validate_condition", "validate_scenario",
]


def _validate_proposal_authority(connection,company_ref,scenarios,anchors,allowed_refs):
    observer=Observer(connection);model=observer.model(company_ref);valuation=observer.valuation(company_ref);price=observer.price(company_ref)
    if not model or not valuation or not price:
        raise CompanyScenarioValidationError("scenario proposal requires held model, valuation and current price")
    if model_binding(anchors)!={"model_version_ref":model.get("id"),"model_version_hash":model.get("content_hash")}:
        raise CompanyScenarioValidationError("scenario proposal model binding is stale")
    if anchors.get("valuation_snapshot_ref")!=valuation.get("version_ref") or anchors.get("price_as_of")!=price.get("as_of"):
        raise CompanyScenarioValidationError("scenario valuation or price anchor is stale")
    if model.get("execution_contract",{}).get("drivers",{}).get("status")!="executed":
        raise CompanyScenarioValidationError("scenario baseline has no executed economic driver plan")
    from .forecast_sensitivity import recompute_linked
    admitted=allowed_refs
    if admitted is None:
        admitted={model["id"],valuation["version_ref"],price["source_ref"]}
        for a in model.get("assumptions") or []:
            admitted.update(str(r["ref"]) for r in a.get("refs") or [] if r.get("ref"))
    for s in scenarios:
        refs=list(s["probability_basis"]["source_refs"])+list(s["partition"]["source_refs"])
        for d in s["driver_deltas"]: refs.extend(d["refs"])
        for t in [*s["triggers"],*s["invalidators"]]:
            if t.get("owner_rule"): refs.append(t["owner_rule"]["source_ref"])
        if set(refs)-admitted: raise CompanyScenarioValidationError("scenario assumption source was not admitted from held material")
        if s.get("pe_override") and s["pe_override"].get("source_ref") not in admitted:
            raise CompanyScenarioValidationError("P/E override source was not admitted")
        recompute_linked(model,s["driver_deltas"])
