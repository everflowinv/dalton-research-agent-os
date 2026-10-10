"""Batch an: cross-product consistency and version binding.

Research artifact quality review (2026-10-09), attachments 03 and 05: 60 of
64 forecast lines were bound to an older model version than the current one;
a sensitivity projection, every economic-invariant verdict object and a
return bridge did the same; EPAM's gate said ``turnaround`` while its newest
dossier said ``structural_growth`` with no new evidence named; a return bridge
showed positive expected earnings beside a model forecasting operating losses.
Each product was internally fine.  Together they told the reader two stories.

Three rules, for every company in every workspace (no per-company patch):

**Version binding.**  Every derived object -- forecast lines, sensitivity
projections, annual projections, scenario baselines, earnings-preview gap
statements, invariant verdicts, quality scores, zero-base reviews, return
bridges -- records the upstream version refs it was computed from
(``upstream_bindings``; older rows are read from the fields they already
carry).  :func:`evaluate_bindings` resolves each bound ref against the head of
its own version chain: a newer upstream version makes the object
``stale_binding``.  A stale object is displayed as stale (never as the current
result) and feeds the redraft thresholds through :func:`stale_bindings` /
:func:`redraft_signals`.

**Cross-product conflicts.**  :func:`detect_conflicts` is deterministic and
read-only: classification across gate / dossier / initial screen (naming the
new evidence a switch rests on, or that there is none), the variant view's
own evidence used as the counter-thesis, different consensus sources across
preview / event note / dossier / initial screen, a confidence field against
its own prose, a zero-base review against the catalyst calendar, the model's
operating income against a return bridge's earnings sign, and forecast-line
unit / currency mismatches.  :class:`ProductConsistencyAuthority` writes them
once each into the append-only ``product_consistency_conflicts`` table, with
resolution events appended (``superseded`` when a version a conflict names is
no longer current).  Open high/medium conflicts are redraft triggers.

**Company identity.**  Conflicts and aliases carry ten-digit CIK refs
(``mission_company_cik.validate_company_ref``); a non-canonical spelling
already in history is mapped through the append-only ``company_ref_aliases``
table, never rewritten (:func:`scan_noncanonical_refs` reports them).

Nothing here calls a model.  Everything except the authority is read-only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash

SCHEMA_VERSION = "product-consistency:0.1"
RULE_REF = "product-consistency:v1"
_SCHEMA_PATH = Path(__file__).with_name("product_consistency_schema.sql")

CURRENT = "current"
STALE = "stale_binding"
UNBOUND = "unbound"
HISTORICAL = "historical"
UNKNOWN = "unknown"

STALE_LABEL = "已过期"


class ProductConsistencyError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _table_exists(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _columns(connection: Any, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _loads(text: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _dec(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _sign(value: Decimal | None) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


def _canon(company_ref: Any, aliases: Mapping[str, str] | None = None) -> str:
    from .mission_company_cik import resolve_alias

    return resolve_alias(str(company_ref), aliases)


# ---------------------------------------------------------------------------
# version chains: what "the current version" of a ref is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chain:
    """One versioned table: a version id's chain, and its head."""

    prefix: str
    kind: str
    label: str
    table: str
    id_col: str
    chain_col: str
    order_col: str = "version_number"


VERSION_CHAINS: tuple[Chain, ...] = (
    Chain("forecast-model-version:", "forecast_model", "预测模型", "forecast_model_versions",
          "version_id", "model_ref"),
    Chain("sensitivity-projection:", "sensitivity_projection", "敏感性测算", "sensitivity_projections",
          "projection_id", "projection_ref"),
    Chain("consensus-estimate-version:", "consensus", "一致预期", "consensus_estimate_versions",
          "version_id", "consensus_ref"),
    Chain("mission-deliverable-version:", "deliverable", "研究报告", "mission_deliverable_versions",
          "version_id", "deliverable_ref"),
    Chain("company-dossier-version:", "company_dossier", "公司档案", "company_dossier_versions",
          "version_id", "dossier_ref"),
    Chain("deep-insight-gate-version:", "deep_insight_gate", "深度研究门", "deep_insight_gate_versions",
          "version_id", "gate_ref"),
    Chain("debate-map-version:", "debate_map", "分歧图", "debate_map_versions", "version_id", "map_ref"),
    Chain("thesis-version:", "thesis", "投资论点", "thesis_versions", "version_id", "thesis_id"),
    Chain("catalyst-calendar-version:", "catalyst_calendar", "催化剂日历", "catalyst_calendar_versions",
          "version_id", "calendar_ref"),
    Chain("valuation-snapshot-version:", "valuation_snapshot", "估值快照", "valuation_snapshot_versions",
          "version_id", "snapshot_ref"),
    Chain("company-scenario-version:", "company_scenario", "情景", "company_scenario_versions",
          "version_id", "scenario_ref"),
    Chain("forecast-line-version:", "forecast_line", "预测行", "model_forecast_line_versions",
          "version_id", "line_ref"),
    Chain("input-version:", "model_input", "模型输入", "model_input_versions", "version_id",
          "model_input_ref"),
    Chain("zero-base-review-version:", "zero_base_review", "零基审查", "zero_base_review_versions",
          "version_id", "review_ref"),
    Chain("quality-score-version:", "quality_score", "质量评分", "research_quality_score_versions",
          "version_id", "score_ref"),
    Chain("holding-period-return-version:", "return_bridge", "回报拆解", "holding_period_return_bridges",
          "version_id", "bridge_ref"),
    Chain("industry-framework-version:", "industry_framework", "行业框架", "industry_framework_versions",
          "version_id", "framework_ref"),
)
_VERSION_REF_RE = re.compile(
    "(?:" + "|".join(re.escape(chain.prefix) for chain in VERSION_CHAINS) + r")[A-Za-z0-9:._-]+")


def chain_for(ref: Any) -> Chain | None:
    text = str(ref or "")
    return next((chain for chain in VERSION_CHAINS if text.startswith(chain.prefix)), None)


class Heads:
    """Resolves a version ref to its chain head, once per ref (a read cache)."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._cache: dict[str, dict[str, Any]] = {}
        self._usable: dict[str, bool] = {}

    def _usable_chain(self, chain: Chain) -> bool:
        if chain.table not in self._usable:
            columns = _columns(self.connection, chain.table) if _table_exists(
                self.connection, chain.table) else set()
            self._usable[chain.table] = {chain.id_col, chain.chain_col, chain.order_col} <= columns
        return self._usable[chain.table]

    def resolve(self, ref: str) -> dict[str, Any]:
        ref = str(ref)
        if ref in self._cache:
            return self._cache[ref]
        chain = chain_for(ref)
        out: dict[str, Any] = {"ref": ref, "status": UNKNOWN, "current_ref": None,
                               "kind": None if chain is None else chain.kind,
                               "label": None if chain is None else chain.label}
        if chain is not None and self._usable_chain(chain):
            row = self.connection.execute(
                f"SELECT {chain.chain_col}, {chain.order_col} FROM {chain.table} "
                f"WHERE {chain.id_col}=?", (ref,)).fetchone()
            if row is not None:
                head = self.connection.execute(
                    f"SELECT {chain.id_col}, {chain.order_col} FROM {chain.table} "
                    f"WHERE {chain.chain_col}=? ORDER BY {chain.order_col} DESC LIMIT 1",
                    (row[0],)).fetchone()
                out.update({"chain_ref": row[0], "bound_version": row[1],
                            "current_ref": head[0], "current_version": head[1],
                            "status": CURRENT if head[0] == ref else "superseded"})
        self._cache[ref] = out
        return out


def version_refs_in(value: Any) -> list[str]:
    """Every version ref a value names (strings anywhere inside it), in order."""

    out: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for inner in item.values():
                walk(inner)
        elif isinstance(item, (list, tuple)):
            for inner in item:
                walk(inner)
        elif isinstance(item, str):
            for match in _VERSION_REF_RE.finditer(item):
                ref = match.group(0).rstrip(".:-")
                if ref not in out:
                    out.append(ref)

    walk(value)
    return out


def binding_entry(ref: str, *, role: str | None = None, content_hash_value: str | None = None
                  ) -> dict[str, Any]:
    """One ``upstream_bindings`` row: the upstream ref, its kind and role."""

    chain = chain_for(ref)
    return {"ref": str(ref), "kind": None if chain is None else chain.kind,
            "role": role, "content_hash": content_hash_value}


def upstream_bindings(refs: Iterable[Any], *, roles: Mapping[str, str] | None = None
                      ) -> list[dict[str, Any]]:
    """The ``upstream_bindings`` field a derived object records.

    Every distinct versioned ref among ``refs`` (strings or ``{"ref", "role",
    "content_hash"}`` mappings), in a stable order.  Writers call this when
    they publish; readers fall back to the fields older rows already carry.
    """

    out: dict[str, dict[str, Any]] = {}
    for item in refs:
        if isinstance(item, Mapping):
            ref, role, digest = item.get("ref"), item.get("role"), item.get("content_hash")
        else:
            ref, role, digest = item, None, None
        if not ref or chain_for(ref) is None or str(ref) in out:
            continue
        out[str(ref)] = binding_entry(str(ref), role=role or (roles or {}).get(str(ref)),
                                      content_hash_value=digest)
    return [out[key] for key in sorted(out)]


# ---------------------------------------------------------------------------
# derived objects: what each one was computed from
# ---------------------------------------------------------------------------

DERIVED_LABELS: Mapping[str, str] = {
    "forecast_line": "预测行", "sensitivity_projection": "敏感性测算", "annual_projection": "年度汇总",
    "scenario_baseline": "情景基准", "earnings_preview": "业绩前瞻", "invariant_verdict": "经济不变量检查结论",
    "quality_score": "质量评分", "zero_base_review": "零基审查", "return_bridge": "回报拆解",
}
#: Which redraft product a derived kind's staleness belongs to.
REDRAFT_PRODUCT_KINDS: Mapping[str, tuple[str, ...]] = {
    "earnings_preview": ("earnings_preview",),
    "initial_screen": ("quality_score",),
    "dossier": ("quality_score",),
    "investment_memo": ("quality_score", "return_bridge", "scenario_baseline"),
    "event_judgement": (),
    "industry_framework": (),
    "ask": ("quality_score",),
}


def _heads_rows(connection: Any, table: str, chain_col: str, *, where: str = "",
                params: Sequence[Any] = (), order_col: str = "version_number") -> list[Any]:
    if not _table_exists(connection, table):
        return []
    return connection.execute(
        f"SELECT t.* FROM {table} t JOIN (SELECT {chain_col} AS c, MAX({order_col}) AS top "
        f"FROM {table} GROUP BY {chain_col}) h ON h.c=t.{chain_col} AND h.top=t.{order_col}"
        + (f" WHERE {where}" if where else ""), tuple(params)).fetchall()


def _company_filter(column: str, company_ref: str | None) -> tuple[str, tuple[Any, ...]]:
    if company_ref is None:
        return "", ()
    from .mission_company_cik import company_ref_variants

    variants = company_ref_variants(company_ref)
    return (f"t.{column} IN ({','.join('?' * len(variants))})", tuple(variants))


def _explicit(record: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    rows = record.get("upstream_bindings")
    if not isinstance(rows, list):
        return []
    return [(str(item.get("ref")), item.get("role")) for item in rows
            if isinstance(item, Mapping) and item.get("ref")]


def _object(kind: str, row: Any, record: Mapping[str, Any], *, version_ref: str, object_ref: str,
            company_ref: Any, bindings: Sequence[tuple[str, str | None]],
            **extra: Any) -> dict[str, Any]:
    seen: dict[str, str | None] = {}
    for ref, role in list(_explicit(record)) + list(bindings):
        if ref and chain_for(ref) is not None and ref not in seen:
            seen[ref] = role
    return {"kind": kind, "label": DERIVED_LABELS[kind], "version_ref": version_ref,
            "object_ref": object_ref, "company_ref": company_ref,
            "created_at": record.get("created_at") or (row["created_at"] if "created_at" in row.keys()
                                                      else None),
            "bindings": [{"ref": ref, "role": role} for ref, role in seen.items()], **extra}


def _forecast_lines(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("subject_ref", company_ref)
    for row in _heads_rows(connection, "model_forecast_line_versions", "line_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        refs = [(record.get(key), key) for key in ("scenario_version_ref", "model_run_version_ref",
                                                    "base_input_version_ref", "growth_input_version_ref")]
        yield _object("forecast_line", row, record, version_ref=row["version_id"],
                      object_ref=row["line_ref"], company_ref=row["subject_ref"],
                      bindings=[(ref, role) for ref, role in refs if ref],
                      unit=record.get("unit"), currency=record.get("currency"),
                      metric=record.get("metric_or_aspect"),
                      period_end=(record.get("period") or {}).get("end"))


def _sensitivity(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("company_ref", company_ref)
    for row in _heads_rows(connection, "sensitivity_projections", "projection_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        bindings = [(row["model_version_ref"], "model")]
        bindings += [(ref, "consensus") for ref in version_refs_in(record.get("consensus_bridge"))
                     if ref.startswith("consensus-estimate-version:")]
        yield _object("sensitivity_projection", row, record, version_ref=row["projection_id"],
                      object_ref=row["projection_ref"], company_ref=row["company_ref"],
                      bindings=bindings, unit=record.get("unit"), currency=record.get("currency"))


def _annual(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    if not _table_exists(connection, "forecast_model_annual_projections"):
        return
    sql = ("SELECT t.* FROM forecast_model_annual_projections t JOIN (SELECT company_ref AS c, "
           "MAX(created_at) AS top FROM forecast_model_annual_projections GROUP BY company_ref) h "
           "ON h.c=t.company_ref AND h.top=t.created_at")
    where, params = _company_filter("company_ref", company_ref)
    for row in connection.execute(sql + (f" WHERE {where}" if where else ""), params).fetchall():
        record = _loads(row["record_json"]) or {}
        yield _object("annual_projection", row, record,
                      version_ref=record.get("projection_ref") or row["model_version_id"],
                      object_ref=f"annual-projection:{row['company_ref']}", company_ref=row["company_ref"],
                      bindings=[(row["model_version_id"], "model")])


def _scenario_sets(connection: Any) -> dict[str, Mapping[str, Any]]:
    if not _table_exists(connection, "company_scenario_sets"):
        return {}
    return {row[0]: (_loads(row[1]) or {}) for row in connection.execute(
        "SELECT set_id, record_json FROM company_scenario_sets").fetchall()}


def scenario_model_ref(record: Mapping[str, Any], set_record: Mapping[str, Any] | None) -> str | None:
    """The model version a scenario's baseline was taken from.

    ``model_binding`` when the version records it (batch an); else the set's
    anchors; else the first trigger / invalidator baseline whose source names
    a model version (``forecast-model-version:...#cell``).
    """

    binding = record.get("model_binding")
    if isinstance(binding, Mapping) and binding.get("model_version_ref"):
        return str(binding["model_version_ref"])
    anchors = (set_record or {}).get("anchors") or {}
    if isinstance(anchors, Mapping) and anchors.get("model_version_ref"):
        return str(anchors["model_version_ref"])
    for condition in list(record.get("triggers") or ()) + list(record.get("invalidators") or ()):
        source = str(((condition or {}).get("baseline") or {}).get("source_ref") or "")
        if source.startswith("forecast-model-version:"):
            return source.split("#", 1)[0]
    return None


def _scenarios(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("company_ref", company_ref)
    sets = None
    for row in _heads_rows(connection, "company_scenario_versions", "scenario_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        if record.get("state") not in (None, "live"):
            continue
        if sets is None:
            sets = _scenario_sets(connection)
        model = scenario_model_ref(record, sets.get(row["set_ref"]))
        yield _object("scenario_baseline", row, record, version_ref=row["version_id"],
                      object_ref=row["scenario_ref"], company_ref=row["company_ref"],
                      bindings=[(model, "model")] if model else [], scenario_key=record.get("key"))


_PREVIEW_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) preview")


def _previews(connection: Any, company_ref: str | None, today: str) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("subject_ref", company_ref)
    where = "t.kind='earnings_preview'" + (f" AND {where}" if where else "")
    for row in _heads_rows(connection, "mission_deliverable_versions", "deliverable_ref",
                           where=where, params=params):
        record = _loads(row["record_json"]) or {}
        summary = str(record.get("summary") or "")
        refs = [(ref, "model" if ref.startswith("forecast-model-version:") else "consensus")
                for ref in version_refs_in(summary)
                if ref.startswith(("forecast-model-version:", "consensus-estimate-version:"))]
        match = _PREVIEW_DATE_RE.search(summary)
        expected = match.group(1) if match else None
        yield _object("earnings_preview", row, record, version_ref=row["version_id"],
                      object_ref=row["deliverable_ref"], company_ref=row["subject_ref"], bindings=refs,
                      expected_date=expected,
                      historical=bool(expected and expected < today))


def _verdicts(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("company_ref", company_ref)
    for row in _heads_rows(connection, "economic_invariant_verdicts", "verdict_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        yield _object("invariant_verdict", row, record, version_ref=row["verdict_id"],
                      object_ref=row["verdict_ref"], company_ref=row["company_ref"],
                      bindings=[(row["output_ref"], "output")], verdict_status=row["status"],
                      output_kind=row["output_kind"])


def _rubric_hash(rubric_ref: Any) -> str | None:
    try:
        from .research_quality_rubrics import rubric

        return rubric(str(rubric_ref)).content_hash
    except Exception:  # noqa: BLE001 - an unknown rubric has no current hash
        return None


def _scores(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("subject_ref", company_ref)
    for row in _heads_rows(connection, "research_quality_score_versions", "score_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        current_rubric = _rubric_hash(row["rubric_ref"])
        yield _object("quality_score", row, record, version_ref=row["version_id"],
                      object_ref=row["score_ref"], company_ref=row["subject_ref"],
                      bindings=[(row["target_ref"], "target")],
                      rubric_stale=bool(current_rubric and current_rubric != row["rubric_hash"]),
                      rubric_ref=row["rubric_ref"])


def _zero_base(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("company_ref", company_ref)
    for row in _heads_rows(connection, "zero_base_review_versions", "review_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        answers = record.get("answers") or {}
        refs = [(ref, "cited") for ref in version_refs_in(answers.get("citations"))]
        if record.get("calibration_ref"):
            refs.append((record["calibration_ref"], "calibration"))
        yield _object("zero_base_review", row, record, version_ref=row["version_id"],
                      object_ref=row["review_ref"], company_ref=row["company_ref"], bindings=refs,
                      next_verification_date=(answers.get("next_verification") or {}).get("date"))


def _bridges(connection: Any, company_ref: str | None) -> Iterator[dict[str, Any]]:
    where, params = _company_filter("company_ref", company_ref)
    for row in _heads_rows(connection, "holding_period_return_bridges", "bridge_ref", where=where,
                           params=params):
        record = _loads(row["record_json"]) or {}
        inputs = record.get("inputs") or {}
        refs = [(inputs.get("forecast_model_version_ref"), "model"),
                (inputs.get("consensus_version_ref"), "consensus")]
        yield _object("return_bridge", row, record, version_ref=row["version_id"],
                      object_ref=row["bridge_ref"], company_ref=row["company_ref"],
                      bindings=[(ref, role) for ref, role in refs if ref])


def derived_objects(connection: Any, company_ref: str | None = None, *,
                    today: str | None = None) -> list[dict[str, Any]]:
    """The current version of every derived object, with what it was computed from."""

    day = today or datetime.now(timezone.utc).date().isoformat()
    out: list[dict[str, Any]] = []
    for reader in (_forecast_lines, _sensitivity, _annual, _scenarios, _verdicts, _scores,
                   _zero_base, _bridges):
        try:
            out.extend(reader(connection, company_ref))
        except sqlite3.Error:
            continue
    try:
        out.extend(_previews(connection, company_ref, day))
    except sqlite3.Error:
        pass
    for item in out:
        if item.get("kind") == "zero_base_review":
            deadline = item.get("next_verification_date")
            if deadline and str(deadline) < day:
                item["verification_expired"] = deadline
    return out


def _stale_text(item: Mapping[str, Any], stale: Sequence[Mapping[str, Any]]) -> str:
    first = stale[0]
    upstream = first.get("label") or "上游"
    bound, current = first.get("bound_version"), first.get("current_version")
    versions = (f"（第 {bound} 版，当前第 {current} 版）"
                if bound is not None and current is not None else "")
    more = f"等 {len(stale)} 项上游" if len(stale) > 1 else ""
    return (f"{STALE_LABEL}：这份{item['label']}基于旧版{upstream}{versions}{more}计算，"
            "不作为当前结果展示，等待按新版本重算")


def evaluate_bindings(connection: Any, company_ref: str | None = None, *,
                      today: str | None = None, heads: Heads | None = None) -> list[dict[str, Any]]:
    """Every current derived object with ``binding_status`` and its stale upstreams.

    ``stale_binding``: at least one bound upstream ref has a newer version in
    its own chain (or, for a quality score, its rubric changed).
    ``unbound``: the object names no upstream version at all -- reported, so
    an object that cannot say what it was computed from is not taken as
    current.  ``historical``: an earnings preview whose report date passed.
    """

    resolver = heads or Heads(connection)
    out = []
    for item in derived_objects(connection, company_ref, today=today):
        stale = []
        for binding in item["bindings"]:
            head = resolver.resolve(binding["ref"])
            binding.update({"status": head["status"], "current_ref": head.get("current_ref")})
            if head["status"] == "superseded":
                stale.append({**binding, "label": head.get("label"),
                              "bound_version": head.get("bound_version"),
                              "current_version": head.get("current_version")})
        if item.get("rubric_stale"):
            stale.append({"ref": item.get("rubric_ref"), "role": "rubric", "label": "评分标准",
                          "status": "superseded"})
        if item.get("historical"):
            status = HISTORICAL
        elif stale or item.get("verification_expired"):
            status = STALE
        elif not item["bindings"]:
            status = UNBOUND
        else:
            status = CURRENT
        item["binding_status"] = status
        item["stale_upstreams"] = stale
        item["display_status"] = {CURRENT: "当前", STALE: STALE_LABEL, UNBOUND: "未记录上游版本",
                                  HISTORICAL: "历史记录"}[status]
        item["display_note"] = (_stale_text(item, stale) if status == STALE and stale else
                                "这份结果没有记录它基于哪一版上游数据，不能当作当前结果"
                                if status == UNBOUND else None)
        if item.get("verification_expired"):
            item["display_note"] = f"约定核验日 {item['verification_expired']} 已过，已到零基重审阈值"
        out.append(item)
    return out


def stale_bindings(connection: Any, company_ref: str | None = None, *,
                   today: str | None = None) -> dict[str, Any]:
    """The summary ``redraft_policy`` and the cockpit read.

    ``{"total", "stale", "unbound", "by_kind": {kind: {"total", "stale"}},
    "by_company": {company: {...}}, "stale_objects": [...]}``.
    """

    rows = evaluate_bindings(connection, company_ref, today=today)
    summary: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "total": 0, "stale": 0,
                               "unbound": 0, "historical": 0, "by_kind": {}, "by_company": {},
                               "stale_objects": []}
    for item in rows:
        company = _canon(item["company_ref"])
        status = item["binding_status"]
        for bucket in (summary, summary["by_company"].setdefault(
                company, {"total": 0, "stale": 0, "unbound": 0, "historical": 0, "by_kind": {}})):
            bucket["total"] += 1
            if status == STALE:
                bucket["stale"] += 1
            elif status == UNBOUND:
                bucket["unbound"] += 1
            elif status == HISTORICAL:
                bucket["historical"] += 1
            kind = bucket["by_kind"].setdefault(item["kind"], {"total": 0, "stale": 0, "unbound": 0})
            kind["total"] += 1
            if status == STALE:
                kind["stale"] += 1
            elif status == UNBOUND:
                kind["unbound"] += 1
        if status == STALE:
            summary["stale_objects"].append({
                key: item.get(key) for key in ("kind", "label", "version_ref", "object_ref",
                                               "company_ref", "display_note")}
                | {"stale_upstreams": [{k: up.get(k) for k in ("ref", "role", "current_ref",
                                                                 "bound_version", "current_version")}
                                       for up in item["stale_upstreams"]]})
    return summary


def binding_status_index(connection: Any, company_ref: str | None = None, *,
                         today: str | None = None) -> dict[str, dict[str, Any]]:
    """version_ref -> its binding status, for a display that marks one object."""

    return {item["version_ref"]: {key: item.get(key) for key in (
        "kind", "binding_status", "display_status", "display_note", "stale_upstreams")}
        for item in evaluate_bindings(connection, company_ref, today=today)}


# ---------------------------------------------------------------------------
# current products (the narrative ones) per company
# ---------------------------------------------------------------------------

#: product -> (table, chain column, company column, extra where)
NARRATIVE_PRODUCTS: Mapping[str, tuple[str, str, str, str]] = {
    "deep_insight_gate": ("deep_insight_gate_versions", "gate_ref", "company_ref", ""),
    "company_dossier": ("company_dossier_versions", "dossier_ref", "company_ref", ""),
    "initial_screen": ("mission_deliverable_versions", "deliverable_ref", "subject_ref",
                       "t.kind='initial_screen'"),
    "earnings_preview": ("mission_deliverable_versions", "deliverable_ref", "subject_ref",
                         "t.kind='earnings_preview'"),
    "event_note": ("mission_deliverable_versions", "deliverable_ref", "subject_ref",
                   "t.kind='event_note'"),
    "debate_map": ("debate_map_versions", "map_ref", "subject_ref", ""),
    "zero_base_review": ("zero_base_review_versions", "review_ref", "company_ref", ""),
    "catalyst_calendar": ("catalyst_calendar_versions", "calendar_ref", "company_ref", ""),
    "forecast_model": ("forecast_model_versions", "model_ref", "company_ref", ""),
    "return_bridge": ("holding_period_return_bridges", "bridge_ref", "company_ref", ""),
}
PRODUCT_LABELS: Mapping[str, str] = {
    "deep_insight_gate": "深度研究门", "company_dossier": "公司档案", "initial_screen": "初筛",
    "earnings_preview": "业绩前瞻", "event_note": "事件汇总", "debate_map": "分歧图",
    "zero_base_review": "零基审查", "catalyst_calendar": "催化剂日历", "forecast_model": "预测模型",
    "return_bridge": "回报拆解", "thesis": "投资论点", "forecast_line": "预测行",
    "sensitivity_projection": "敏感性测算",
}


def current_products(connection: Any, company_ref: str | None = None) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """company (canonical) -> product -> current versions ``{"version_ref", "record", "created_at"}``."""

    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for product, (table, chain_col, company_col, extra) in NARRATIVE_PRODUCTS.items():
        if not _table_exists(connection, table):
            continue
        where, params = _company_filter(company_col, company_ref)
        clause = " AND ".join(part for part in (extra, where) if part)
        try:
            rows = _heads_rows(connection, table, chain_col, where=clause, params=params)
        except sqlite3.Error:
            continue
        for row in rows:
            record = _loads(row["record_json"])
            if not isinstance(record, Mapping):
                continue
            version_ref = row["version_id"] if "version_id" in row.keys() else record.get("id")
            out.setdefault(_canon(row[company_col]), {}).setdefault(product, []).append({
                "version_ref": version_ref, "record": record, "company_ref": row[company_col],
                "created_at": record.get("created_at") or row["created_at"], "product": product})
    return out


# ---------------------------------------------------------------------------
# conflict detectors (deterministic, read-only)
# ---------------------------------------------------------------------------

CONFLICT_KINDS: Mapping[str, str] = {
    "classification": "行业分类不一致",
    "variant_counter": "差异化观点与反论点用同一证据",
    "consensus_source": "一致预期来源不一致",
    "confidence_prose": "置信度字段与正文措辞不一致",
    "zero_base_catalyst": "零基审查与催化剂日历不一致",
    "operating_income_vs_return": "模型营业利润与回报拆解盈利方向相反",
    "unit_currency": "预测行单位或币种不一致",
}
#: High/medium conflicts are redraft triggers; low ones are shown only.
TRIGGER_SEVERITIES = frozenset({"high", "medium"})

CLASSIFICATION_LABELS: Mapping[str, str] = {
    "commodity_cycle": "商品周期型", "capital_cycle": "资本周期型", "contract_compounder": "合同型复利",
    "structural_growth": "结构性成长", "turnaround": "困境反转", "insufficient_evidence": "证据不足",
}
#: Prose spellings of each classification (initial screens carry no field).
_CLASSIFICATION_WORDS: Mapping[str, tuple[str, ...]] = {
    "commodity_cycle": ("商品周期", "commodity cycle", "commodity-cycle"),
    "capital_cycle": ("资本周期", "capital cycle", "capital-cycle"),
    "contract_compounder": ("合同型复利", "合约型复利", "合同型 compounder", "合约复利", "合同复利",
                            "contract compounder", "contract-compounder"),
    "structural_growth": ("结构性增长", "结构性成长", "结构成长", "structural growth", "structural-growth"),
    "turnaround": ("困境反转", "反转型", "转型公司", "turnaround"),
}
_CLASSIFY_VERB = r"(?:归为|归入|归类为|分类为|属于|更接近|定位为|界定为|是一家|应归为|is an?|classif(?:y|ied) as)"
_NEGATION = re.compile(r"(?:不|并非|而非|不是|not)\s*$")


def classification_in_prose(text: str) -> str | None:
    """The one classification a text asserts with a classifying verb, else None."""

    found: set[str] = set()
    lowered = text.lower()
    for key, words in _CLASSIFICATION_WORDS.items():
        for word in words:
            pattern = _CLASSIFY_VERB + r"[^。；;，,.\n]{0,8}?" + re.escape(word.lower())
            for match in re.finditer(pattern, lowered):
                before = lowered[max(0, match.start() - 4):match.start()]
                if not _NEGATION.search(before):
                    found.add(key)
    return next(iter(found)) if len(found) == 1 else None


def _claim_created(connection: Any, refs: Iterable[str]) -> dict[str, str]:
    refs = [ref for ref in dict.fromkeys(refs) if str(ref).startswith("claim-version:")]
    if not refs or not _table_exists(connection, "claim_versions"):
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(refs), 400):
        chunk = refs[start:start + 400]
        for row in connection.execute(
                "SELECT claim_version_id, created_at FROM claim_versions WHERE claim_version_id IN ("
                + ",".join("?" * len(chunk)) + ")", chunk).fetchall():
            out[row[0]] = row[1]
    return out


_CLAIM_RE = re.compile(r"claim-version:[0-9a-f]{64}")


def _claim_refs(value: Any) -> set[str]:
    return set(_CLAIM_RE.findall(json.dumps(value, ensure_ascii=False) if not isinstance(value, str)
                                 else value))


def _classification_entries(products: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    entries = []
    for item in products.get("deep_insight_gate", ()):
        record = item["record"]
        value = record.get("classification")
        q1 = next((answer for answer in record.get("answers") or ()
                   if isinstance(answer, Mapping) and answer.get("question_ref") == "q1"), {})
        entries.append({**item, "classification": value, "evidence": _claim_refs(q1)})
    for item in products.get("company_dossier", ()):
        section = item["record"].get("industry_classification") or {}
        value = section.get("classification") if isinstance(section, Mapping) else None
        entries.append({**item, "classification": value, "evidence": _claim_refs(section)})
    for item in products.get("initial_screen", ()):
        record = item["record"]
        hits: dict[str, set[str]] = {}
        for section in record.get("sections") or ():
            if not isinstance(section, Mapping):
                continue
            value = classification_in_prose(str(section.get("body") or ""))
            if value:
                hits.setdefault(value, set()).update(_claim_refs(section.get("claim_refs")))
        if len(hits) == 1:
            value, evidence = next(iter(hits.items()))
            entries.append({**item, "classification": value, "evidence": evidence})
    return [entry for entry in entries
            if entry["classification"] in CLASSIFICATION_LABELS
            and entry["classification"] != "insufficient_evidence"]


def _conflict(kind: str, company_ref: str, severity: str, versions: Sequence[Mapping[str, Any]],
              detail: str, *, key: Any = None, **evidence: Any) -> dict[str, Any]:
    refs = sorted({str(item["version_ref"]) for item in versions if item.get("version_ref")})
    involved = sorted({str(item.get("product")) for item in versions if item.get("product")})
    conflict_id = "product-conflict:" + content_hash({
        "kind": kind, "company": company_ref, "versions": refs, "key": key, "rule": RULE_REF})[:32]
    return {"id": conflict_id, "conflict_kind": kind, "label": CONFLICT_KINDS[kind],
            "company_ref": company_ref, "severity": severity, "version_refs": refs,
            "products": involved, "detail": detail, "evidence": evidence, "rule_ref": RULE_REF}


def detect_classification(connection: Any, company_ref: str,
                          products: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    entries = _classification_entries(products)
    values = {entry["classification"] for entry in entries}
    if len(values) < 2:
        return []
    entries.sort(key=lambda entry: str(entry["created_at"] or ""))
    newest = entries[-1]
    older = [entry for entry in entries if entry["classification"] != newest["classification"]]
    created = _claim_created(connection, newest["evidence"])
    switch = []
    for ref in sorted(newest["evidence"]):
        cited_before = any(ref in _claim_refs(entry["record"]) for entry in older)
        newer_than = all(str(created.get(ref) or "") > str(entry["created_at"] or "")
                         for entry in older)
        if not cited_before and newer_than:
            switch.append(ref)
    labels = "；".join(f"{PRODUCT_LABELS.get(entry['product'], entry['product'])}："
                      f"{CLASSIFICATION_LABELS[entry['classification']]}" for entry in entries)
    detail = (f"{labels}。最新的{PRODUCT_LABELS.get(newest['product'])}改判为"
              f"{CLASSIFICATION_LABELS[newest['classification']]}，"
              + (f"引用了 {len(switch)} 条在旧判断之后才出现的新证据" if switch
                 else "没有引用任何在旧判断之后出现的新证据，切换理由不明"))
    return [_conflict("classification", company_ref, "medium" if switch else "high", entries, detail,
                      classifications={entry["version_ref"]: entry["classification"] for entry in entries},
                      switch_from=[entry["version_ref"] for entry in older],
                      switch_to=newest["version_ref"], switch_evidence_refs=switch[:20])]


#: The variant view's slots that state our view.
_OUR_VIEW_SLOTS = frozenset({"our_view", "where_market_is_wrong"})
#: Minimum shared evidence for a variant / counter conflict.
VARIANT_SHARED_MIN = 2
_RISK_TITLE = re.compile(r"(?:anti-?thesis|风险|反论|空头|bear)", re.IGNORECASE)


def detect_variant_counter(connection: Any, company_ref: str,
                           products: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for dossier in products.get("company_dossier", ()):
        variant = dossier["record"].get("variant_view") or {}
        ours: set[str] = set()
        for slot in (variant.get("slots") or ()) if isinstance(variant, Mapping) else ():
            if isinstance(slot, Mapping) and slot.get("slot_id") in _OUR_VIEW_SLOTS:
                ours |= _claim_refs(slot)
        if not ours:
            continue
        counters: list[tuple[dict[str, Any], set[str]]] = []
        for debate_map in products.get("debate_map", ()):
            refs: set[str] = set()
            for debate in debate_map["record"].get("debates") or ():
                side = ((debate or {}).get("our_position") or {}).get("side")
                opposite = {"bull": "bear_position", "bear": "bull_position"}.get(str(side))
                if opposite:
                    refs |= _claim_refs((debate or {}).get(opposite))
            counters.append((debate_map, refs))
        for gate in products.get("deep_insight_gate", ()):
            q11 = next((answer for answer in gate["record"].get("answers") or ()
                        if isinstance(answer, Mapping) and answer.get("question_ref") == "q11"), {})
            counters.append((gate, _claim_refs(q11)))
        for screen in products.get("initial_screen", ()):
            refs = set()
            for section in screen["record"].get("sections") or ():
                if isinstance(section, Mapping) and _RISK_TITLE.search(str(section.get("title") or "")):
                    refs |= _claim_refs(section.get("claim_refs"))
            counters.append((screen, refs))
        for product, refs in counters:
            shared = sorted(ours & refs)
            if len(shared) < VARIANT_SHARED_MIN:
                continue
            label = PRODUCT_LABELS.get(product["product"], product["product"])
            out.append(_conflict(
                "variant_counter", company_ref, "medium", [dossier, product],
                f"公司档案的差异化观点引用的 {len(shared)} 条证据，同时被{label}列为反方论据；"
                "需要说明这些证据究竟支持哪一方", shared_evidence_refs=shared[:20]))
    return out


#: Named consensus providers, as they are written in claims.
CONSENSUS_PROVIDERS: Mapping[str, tuple[str, ...]] = {
    "FactSet": ("factset",), "LSEG": ("lseg", "refinitiv", "i/b/e/s", "ibes"),
    "Bloomberg": ("bloomberg", "彭博"), "Visible Alpha": ("visible alpha",),
    "S&P Capital IQ": ("capital iq", "s&p global market intelligence"), "Zacks": ("zacks",),
    "Yahoo Finance": ("yahoo", "yfinance"), "Wind": ("wind 一致预期", "万得"),
    "Consensus Metrix": ("consensus metrix",), "公司汇编一致预期": ("company-compiled consensus",
                                                                "company compiled consensus"),
}
_CONSENSUS_WORD = re.compile(r"consensus|一致预期|市场预期|street estimate", re.IGNORECASE)
CONSENSUS_PRODUCTS = ("earnings_preview", "event_note", "company_dossier", "initial_screen")


def _providers_in(text: str) -> set[str]:
    lowered = text.lower()
    return {name for name, words in CONSENSUS_PROVIDERS.items() if any(word in lowered for word in words)}


def _consensus_versions(connection: Any, refs: Iterable[str]) -> dict[str, str]:
    refs = list(dict.fromkeys(refs))
    if not refs or not _table_exists(connection, "consensus_estimate_versions"):
        return {}
    out = {}
    for row in connection.execute(
            "SELECT version_id, source_ref, source_kind FROM consensus_estimate_versions WHERE version_id IN ("
            + ",".join("?" * len(refs)) + ")", refs).fetchall():
        source = str(row[1] or "")
        named = _providers_in(source)
        out[row[0]] = (next(iter(named)) if len(named) == 1 else
                       f"一致预期库（{row[2]}）")
    return out


def _consensus_claims(connection: Any, refs: Iterable[str]) -> dict[str, set[str]]:
    refs = [ref for ref in dict.fromkeys(refs)]
    if not refs or not _table_exists(connection, "claim_versions"):
        return {}
    out: dict[str, set[str]] = {}
    for start in range(0, len(refs), 400):
        chunk = refs[start:start + 400]
        for row in connection.execute(
                "SELECT claim_version_id, claim_json FROM claim_versions WHERE claim_version_id IN ("
                + ",".join("?" * len(chunk)) + ")", chunk).fetchall():
            claim = _loads(row[1]) or {}
            text = " ".join(str(claim.get(key) or "") for key in (
                "metric_or_aspect", "normalized_statement", "basis", "statement"))
            if not _CONSENSUS_WORD.search(text):
                continue
            providers = _providers_in(text)
            out[row[0]] = providers or {"未注明来源"}
    return out


def detect_consensus_source(connection: Any, company_ref: str,
                            products: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    per_product: list[tuple[dict[str, Any], set[str]]] = []
    for product in CONSENSUS_PRODUCTS:
        for item in products.get(product, ()):
            record = item["record"]
            text = json.dumps(record, ensure_ascii=False)
            versions = [ref for ref in version_refs_in(text)
                        if ref.startswith("consensus-estimate-version:")]
            claims = _claim_refs(record.get("sections") or record.get("variant_view") or record)
            if product == "company_dossier":
                claims = _claim_refs({key: value for key, value in record.items()
                                      if key not in ("unit_provenance", "input_fingerprints")})
            providers: set[str] = set(_consensus_versions(connection, versions).values())
            for found in _consensus_claims(connection, claims).values():
                providers |= found
            if providers:
                per_product.append((item, providers))
    if len(per_product) < 2:
        return []
    disjoint = [(a, b) for i, a in enumerate(per_product) for b in per_product[i + 1:]
                if not (a[1] & b[1])]
    if not disjoint:
        return []
    involved = {id(item): item for pair in disjoint for item, _ in pair}
    parts = "；".join(f"{PRODUCT_LABELS.get(item['product'], item['product'])}：{'、'.join(sorted(found))}"
                     for item, found in per_product if id(item) in involved)
    return [_conflict("consensus_source", company_ref, "medium", list(involved.values()),
                      f"同一家公司的产品引用了不同来源的一致预期——{parts}；应统一到一致预期库或注明差异",
                      providers={item["version_ref"]: sorted(found) for item, found in per_product})]


_LEVEL_ORDER = {"low": 0.0, "medium_low": 0.5, "medium": 1.0, "medium_high": 1.5, "high": 2.0}
_CN_LEVEL = (("中等偏低", "medium_low"), ("中等偏高", "medium_high"), ("较低", "low"), ("较高", "high"),
             ("中等", "medium"), ("中", "medium"), ("高", "high"), ("低", "low"))
_CN_CONFIDENCE = re.compile(r"(?:置信度|把握|信心)(?:为|是|：|:|在)?\s*(中等偏低|中等偏高|较低|较高|中等|中|高|低)")
_EN_CONFIDENCE = re.compile(
    r"\b(?:(high|medium|moderate|low)(?:-|\s)confidence|confidence (?:is |remains |of )?(high|medium|moderate|low))\b",
    re.IGNORECASE)


def confidence_in_prose(text: str) -> str | None:
    levels = set()
    for match in _CN_CONFIDENCE.finditer(text):
        word = match.group(1)
        levels.add(next(level for spelling, level in _CN_LEVEL if spelling == word))
    for match in _EN_CONFIDENCE.finditer(text):
        word = (match.group(1) or match.group(2)).lower()
        levels.add("medium" if word == "moderate" else word)
    return next(iter(levels)) if len(levels) == 1 else None


def _field_level(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return {"high": "high", "medium": "medium", "low": "low", "高": "high", "中": "medium",
            "低": "low", "moderate": "medium"}.get(text)


def detect_confidence_prose(connection: Any, company_ref: str,
                            products: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for gate in products.get("deep_insight_gate", ()):
        for answer in gate["record"].get("answers") or ():
            if not isinstance(answer, Mapping):
                continue
            field = _field_level(answer.get("confidence"))
            text = " ".join(str((sentence or {}).get("text") or "") for sentence in answer.get("sentences") or ())
            prose = confidence_in_prose(text)
            if field and prose and abs(_LEVEL_ORDER[field] - _LEVEL_ORDER[prose]) >= 1.0:
                out.append(_conflict(
                    "confidence_prose", company_ref, "medium", [gate],
                    f"深度研究门 {answer.get('question_ref')} 的置信度字段为「{field}」，正文却写「{prose}」",
                    key=answer.get("question_ref"), field=field, prose=prose,
                    question_ref=answer.get("question_ref")))
    return out


def detect_thesis_confidence(connection: Any) -> list[dict[str, Any]]:
    """Thesis versions carry no company column; read once, keyed by thesis."""

    if not _table_exists(connection, "thesis_versions"):
        return []
    out = []
    for row in _heads_rows(connection, "thesis_versions", "thesis_id"):
        record = _loads(row["content_json"]) or {}
        field = _field_level(record.get("confidence"))
        prose = confidence_in_prose(" ".join(str(record.get(key) or "") for key in (
            "statement", "mechanism", "implied_expectation")))
        if field and prose and abs(_LEVEL_ORDER[field] - _LEVEL_ORDER[prose]) >= 1.0:
            subject = str(record.get("company_ref") or record.get("subject_ref") or row["thesis_id"])
            out.append(_conflict("confidence_prose", subject, "medium",
                                 [{"version_ref": row["version_id"], "product": "thesis"}],
                                 f"投资论点的置信度字段为「{field}」，正文却写「{prose}」",
                                 field=field, prose=prose))
    return out


_NO_CONFIRMED_DATE = re.compile(
    r"not (?:a|an) (?:confirmed|asserted|assumed) earnings date|no confirmed (?:reporting|earnings) date"
    r"|不是已确认的(?:财报|业绩)|没有(?:已)?确认的(?:财报|业绩)(?:发布)?日", re.IGNORECASE)


def _calendar_as_of(connection: Any, company_ref: str, moment: str) -> dict[str, Any] | None:
    if not _table_exists(connection, "catalyst_calendar_versions"):
        return None
    from .mission_company_cik import company_ref_variants

    variants = company_ref_variants(company_ref)
    row = connection.execute(
        "SELECT version_id, record_json FROM catalyst_calendar_versions WHERE company_ref IN ("
        + ",".join("?" * len(variants)) + ") AND created_at<=? ORDER BY created_at DESC LIMIT 1",
        (*variants, moment)).fetchone()
    return None if row is None else {"version_ref": row[0], "record": _loads(row[1]) or {},
                                      "product": "catalyst_calendar"}


def detect_zero_base_catalyst(connection: Any, company_ref: str,
                              products: Mapping[str, list[dict[str, Any]]], *,
                              today: str) -> list[dict[str, Any]]:
    out = []
    calendars = products.get("catalyst_calendar") or []
    for review in products.get("zero_base_review", ()):
        record = review["record"]
        answers = record.get("answers") or {}
        verification = answers.get("next_verification") or {}
        as_of = str(record.get("as_of") or "")[:10]
        next_date = str(verification.get("date") or "")[:10]
        text = " ".join(str(verification.get(key) or "") for key in ("what", "because"))
        # (1) the review says no confirmed date exists; the calendar it could
        # have read (as of its own creation) held one.
        if _NO_CONFIRMED_DATE.search(text):
            calendar = _calendar_as_of(connection, review["company_ref"], str(review["created_at"] or ""))
            confirmed = [entry for entry in ((calendar or {}).get("record") or {}).get("entries") or ()
                         if isinstance(entry, Mapping) and entry.get("event_kind") == "earnings"
                         and entry.get("confidence") == "confirmed"
                         and str(entry.get("expected_date") or "") >= as_of]
            if calendar and confirmed:
                first = min(confirmed, key=lambda entry: str(entry.get("expected_date")))
                out.append(_conflict(
                    "zero_base_catalyst", company_ref, "medium", [review, calendar],
                    f"零基审查写「没有已确认的财报日」，但当时的催化剂日历已确认 {first.get('expected_date')} 发布财报",
                    key="unconfirmed_claim", entry_ref=first.get("entry_ref"),
                    expected_date=first.get("expected_date")))
        # (2) an earnings print the calendar shows as past happened after the
        # review and before its own next verification date.
        for calendar in calendars:
            for entry in calendar["record"].get("entries") or ():
                if not isinstance(entry, Mapping) or entry.get("event_kind") != "earnings":
                    continue
                when = str(entry.get("expected_date") or "")[:10]
                if (entry.get("confidence") == "confirmed" and as_of < when <= today
                        and next_date and when < next_date
                        and str(review["created_at"] or "")[:10] < when):
                    out.append(_conflict(
                        "zero_base_catalyst", company_ref, "medium", [review, calendar],
                        f"催化剂日历显示 {when} 已发布财报，零基审查（{as_of}）的下次核验日却排在 {next_date}，"
                        "审查没有覆盖这次财报",
                        key=f"print:{entry.get('entry_ref')}", entry_ref=entry.get("entry_ref"),
                        expected_date=when, next_verification=next_date))
    return out


def _model_role_cells(model: Mapping[str, Any], role: str) -> list[Mapping[str, Any]]:
    for result in model.get("results") or ():
        if isinstance(result, Mapping) and (result.get("role") == role or result.get("ref") == f"result:{role}"):
            return [cell for cell in result.get("cells") or ()
                    if isinstance(cell, Mapping) and (cell.get("period") or {}).get("kind") == "quarter"
                    and cell.get("kind", "estimate") == "estimate"]
    return []


def operating_income_by_period(model: Mapping[str, Any]) -> dict[str, Decimal]:
    """Forecast operating income per quarter end: the result line, else derived.

    Derived = revenue − cost of revenue − every operating expense line, when
    the model carries no operating-income result (a company-presented
    subtotal structure).
    """

    def values(role: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for result in model.get("results") or ():
            if not isinstance(result, Mapping) or result.get("role") != role:
                continue
            for cell in result.get("cells") or ():
                if (isinstance(cell, Mapping) and (cell.get("period") or {}).get("kind") == "quarter"
                        and cell.get("kind", "estimate") == "estimate" and cell.get("status") == "computed"):
                    value = _dec(cell.get("value"))
                    if value is not None:
                        end = str(cell["period"]["end"])
                        out[end] = out.get(end, Decimal(0)) + value
        return out

    direct = values("operating_income")
    if direct:
        return direct
    revenue, cost, opex = values("revenue"), values("cost_of_revenue"), values("operating_expense")
    return {end: revenue[end] - cost.get(end, Decimal(0)) - opex.get(end, Decimal(0))
            for end in revenue if end in cost or end in opex}


def _leg_earnings_sign(leg: Mapping[str, Any]) -> tuple[int, str | None]:
    for key in ("net_income_forward", "core_earnings_forward"):
        value = _dec(leg.get(key))
        if value is not None:
            return _sign(value), key
    eps = leg.get("eps_end")
    value = _dec(eps.get("value")) if isinstance(eps, Mapping) else None
    return _sign(value), ("eps_end" if value is not None else None)


def detect_operating_income_vs_return(connection: Any, company_ref: str,
                                      products: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    models = products.get("forecast_model") or []
    if not models:
        return []
    model = max(models, key=lambda item: str(item["created_at"] or ""))
    operating = operating_income_by_period(model["record"])
    if not operating:
        return []
    out = []
    for bridge in products.get("return_bridge", ()):
        for leg in bridge["record"].get("legs") or ():
            if not isinstance(leg, Mapping) or leg.get("status") != "available":
                continue
            if not str(leg.get("leg") or "").startswith("expected"):
                continue
            window = leg.get("window") or {}
            start, end = str(window.get("start") or ""), str(window.get("end") or "")
            ends = sorted(period for period in operating if start < period <= end) if start and end else []
            if not ends:
                ends = sorted(operating)[:4]
            total = sum((operating[period] for period in ends), Decimal(0))
            model_sign = _sign(total)
            leg_sign, basis = _leg_earnings_sign(leg)
            if model_sign == 0 or leg_sign == 0 or model_sign == leg_sign:
                continue
            words = {1: "为正", -1: "为负"}
            out.append(_conflict(
                "operating_income_vs_return", company_ref, "high", [model, bridge],
                f"预测模型在 {ends[0]}–{ends[-1]} 的营业利润合计{words[model_sign]}，"
                f"回报拆解「{leg.get('label') or leg.get('leg')}」的预期盈利却{words[leg_sign]}；"
                "回报数字不能脱离模型单独阅读",
                key=leg.get("leg"), leg=leg.get("leg"), basis=basis,
                model_operating_income=format(total, "f"), periods=ends))
    return out


_CURRENCY_SUFFIX = re.compile(r"-(usd|eur|gbp|jpy|cny|rmb|hkd|chf|cad|aud|inr|krw|twd|sgd)$", re.IGNORECASE)


def _unit_norm(unit: Any, currency: Any) -> str:
    text = str(unit or "").strip().lower()
    if text in ("", "one", "1", "units", "unit") or (currency and text == str(currency).strip().lower()):
        return "one"
    return text


def detect_unit_currency(connection: Any, company_ref: str, lines: Sequence[Mapping[str, Any]],
                         products: Mapping[str, list[dict[str, Any]]],
                         sensitivities: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    model = max(products.get("forecast_model") or [], key=lambda item: str(item["created_at"] or ""),
                default=None)
    model_currency = str((model or {}).get("record", {}).get("currency") or "").upper() or None
    model_unit = (model or {}).get("record", {}).get("unit")
    by_metric: dict[str, set[tuple[str, str]]] = {}
    for line in lines:
        currency = str(line.get("currency") or "").upper() or None
        metric = str(line.get("metric") or "")
        suffix = _CURRENCY_SUFFIX.search(metric)
        expected = None if suffix is None else suffix.group(1).upper().replace("RMB", "CNY")
        version = {"version_ref": line["version_ref"], "product": "forecast_line"}
        if expected and currency and expected != currency:
            out.append(_conflict("unit_currency", company_ref, "high", [version],
                                 f"预测行指标 {metric} 按名称是 {expected}，记录的币种却是 {currency}",
                                 key="metric_currency", metric=metric))
        if model_currency and currency and currency != model_currency:
            out.append(_conflict("unit_currency", company_ref, "high", [version, model],
                                 f"预测行币种 {currency} 与预测模型币种 {model_currency} 不同",
                                 key="model_currency", metric=metric))
        by_metric.setdefault(metric, set()).add((_unit_norm(line.get("unit"), currency), currency or ""))
    for metric, kinds in sorted(by_metric.items()):
        if len(kinds) > 1:
            versions = [{"version_ref": line["version_ref"], "product": "forecast_line"}
                        for line in lines if line.get("metric") == metric]
            out.append(_conflict("unit_currency", company_ref, "medium", versions,
                                 f"同一指标 {metric} 的预测行用了不同的单位或币种："
                                 + "、".join(f"{unit}/{currency or '未注明'}" for unit, currency in sorted(kinds)),
                                 key=f"metric_units:{metric}", metric=metric))
    # The spelling the review flagged: lines in "one", the model in "usd".
    spellings = {str(line.get("unit") or "").lower() for line in lines if line.get("unit")}
    spellings |= {str(item.get("unit") or "").lower() for item in sensitivities if item.get("unit")}
    if model_unit:
        spellings.add(str(model_unit).lower())
    if len(spellings) > 1 and lines:
        versions = [{"version_ref": line["version_ref"], "product": "forecast_line"} for line in lines]
        if model:
            versions.append(model)
        out.append(_conflict("unit_currency", company_ref, "low", versions,
                             "预测行、敏感性测算与模型的金额单位写法不同（" + "、".join(sorted(spellings))
                             + "）；展示时须统一换算为同一金额单位",
                             key="unit_spelling", spellings=sorted(spellings)))
    return out


def detect_conflicts(connection: Any, company_ref: str | None = None, *,
                     today: str | None = None) -> list[dict[str, Any]]:
    """Every conflict among the current products, per company.  Read-only."""

    day = today or datetime.now(timezone.utc).date().isoformat()
    by_company = current_products(connection, company_ref)
    derived = derived_objects(connection, company_ref, today=day)
    lines: dict[str, list[dict[str, Any]]] = {}
    sensitivities: dict[str, list[dict[str, Any]]] = {}
    for item in derived:
        if item["kind"] == "forecast_line":
            lines.setdefault(_canon(item["company_ref"]), []).append(item)
        elif item["kind"] == "sensitivity_projection":
            sensitivities.setdefault(_canon(item["company_ref"]), []).append(item)
    out: list[dict[str, Any]] = []
    for company in sorted(set(by_company) | set(lines)):
        products = by_company.get(company, {})
        for detector in (detect_classification, detect_variant_counter, detect_consensus_source,
                         detect_confidence_prose, detect_operating_income_vs_return):
            try:
                out.extend(detector(connection, company, products))
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                continue
        try:
            out.extend(detect_zero_base_catalyst(connection, company, products, today=day))
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            pass
        try:
            out.extend(detect_unit_currency(connection, company, lines.get(company, []), products,
                                            sensitivities.get(company, [])))
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            pass
    if company_ref is None:
        out.extend(detect_thesis_confidence(connection))
    unique: dict[str, dict[str, Any]] = {}
    for item in out:
        unique.setdefault(item["id"], item)
    return list(unique.values())


# ---------------------------------------------------------------------------
# the authority: conflicts and aliases, append-only
# ---------------------------------------------------------------------------


def _current_refs(connection: Any, refs: Iterable[str], heads: Heads) -> bool:
    for ref in refs:
        head = heads.resolve(ref)
        if head["status"] == "superseded":
            return False
    return True


def recorded_conflicts(connection: Any, company_ref: str | None = None, *,
                       include_closed: bool = False) -> list[dict[str, Any]]:
    """Stored conflicts with their state: ``open`` unless a resolution says otherwise.

    A conflict whose versions are no longer all current reads ``superseded``
    even before the scan appends that event.
    """

    if not _table_exists(connection, "product_consistency_conflicts"):
        return []
    from .mission_company_cik import company_ref_variants

    sql = "SELECT record_json FROM product_consistency_conflicts"
    params: tuple[Any, ...] = ()
    if company_ref is not None:
        variants = company_ref_variants(_canon(company_ref)) + company_ref_variants(company_ref)
        variants = list(dict.fromkeys(variants))
        sql += f" WHERE company_ref IN ({','.join('?' * len(variants))})"
        params = tuple(variants)
    latest: dict[str, str] = {}
    if _table_exists(connection, "product_consistency_resolutions"):
        for row in connection.execute(
                "SELECT conflict_id, resolution FROM product_consistency_resolutions "
                "ORDER BY created_at, resolution_id").fetchall():
            latest[row[0]] = row[1]
    heads = Heads(connection)
    out = []
    for row in connection.execute(sql + " ORDER BY created_at", params).fetchall():
        record = _loads(row[0]) or {}
        state = latest.get(record.get("id"))
        if state in (None, "reopened"):
            state = "open" if _current_refs(connection, record.get("version_refs") or (), heads) \
                else "superseded"
        if state != "open" and not include_closed:
            continue
        out.append({**record, "state": state})
    return out


class ProductConsistencyAuthority:
    """Append-only conflicts, resolution events and company-ref aliases."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_product_consistency_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ProductConsistencyAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def _actor(actor_ref: str) -> str:
        if not re.fullmatch(r"(automation|human|core):[A-Za-z0-9._:-]{1,128}", str(actor_ref)):
            raise ProductConsistencyError("actor_ref must be automation:…, human:… or core:…")
        return str(actor_ref)

    def record_conflict(self, conflict: Mapping[str, Any], *, actor_ref: str) -> dict[str, Any]:
        from .mission_company_cik import canonical_company_ref, validate_company_ref

        actor = self._actor(actor_ref)
        company = validate_company_ref(canonical_company_ref(str(conflict["company_ref"]))
                                       if str(conflict["company_ref"]).startswith("company:")
                                       else str(conflict["company_ref"]))
        if conflict.get("conflict_kind") not in CONFLICT_KINDS:
            raise ProductConsistencyError("unknown conflict kind")
        record = {**{key: value for key, value in conflict.items() if key not in ("state",)},
                  "schema_version": SCHEMA_VERSION, "company_ref": company,
                  "actor_ref": actor, "created_at": self.clock()}
        record["content_hash"] = content_hash({k: v for k, v in record.items()
                                               if k not in ("content_hash", "created_at")})
        with self._transaction() as cur:
            seen = cur.execute("SELECT record_json FROM product_consistency_conflicts WHERE conflict_id=?",
                               (record["id"],)).fetchone()
            if seen is not None:
                return {**json.loads(seen[0]), "write_status": "duplicate"}
            cur.execute(
                "INSERT INTO product_consistency_conflicts(conflict_id,conflict_kind,company_ref,severity,"
                "rule_ref,version_refs_json,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record["id"], record["conflict_kind"], company, record["severity"], record["rule_ref"],
                 json.dumps(record["version_refs"]), canonical_json(record), record["content_hash"],
                 actor, record["created_at"]))
        return {**record, "write_status": "fresh"}

    def resolve(self, conflict_id: str, *, resolution: str, note: str, actor_ref: str,
                evidence_refs: Sequence[str] = ()) -> dict[str, Any]:
        actor = self._actor(actor_ref)
        if resolution not in ("superseded", "explained", "dismissed", "reopened"):
            raise ProductConsistencyError("resolution is superseded / explained / dismissed / reopened")
        if resolution == "explained" and not evidence_refs:
            raise ProductConsistencyError("an explanation names the evidence that resolves the conflict")
        if self.connection.execute("SELECT 1 FROM product_consistency_conflicts WHERE conflict_id=?",
                                   (conflict_id,)).fetchone() is None:
            raise ProductConsistencyError("no such conflict")
        created = self.clock()
        record = {"schema_version": SCHEMA_VERSION, "conflict_id": conflict_id, "resolution": resolution,
                  "note": str(note)[:1000], "evidence_refs": list(evidence_refs)[:50],
                  "actor_ref": actor, "created_at": created}
        record["id"] = "product-conflict-resolution:" + content_hash(
            {"conflict": conflict_id, "resolution": resolution, "at": created})[:32]
        record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO product_consistency_resolutions(resolution_id,conflict_id,resolution,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                (record["id"], conflict_id, resolution, canonical_json(record), record["content_hash"],
                 actor, created))
        return record

    def scan(self, *, actor_ref: str, today: str | None = None) -> dict[str, Any]:
        """Detect, append new conflicts, append ``superseded`` for moved-on ones."""

        actor = self._actor(actor_ref)
        found = detect_conflicts(self.connection, today=today)
        fresh = 0
        for conflict in found:
            if self.record_conflict(conflict, actor_ref=actor)["write_status"] == "fresh":
                fresh += 1
        superseded = 0
        for item in recorded_conflicts(self.connection, include_closed=True):
            if item["state"] == "superseded":
                latest = self.connection.execute(
                    "SELECT resolution FROM product_consistency_resolutions WHERE conflict_id=? "
                    "ORDER BY created_at DESC, resolution_id DESC LIMIT 1", (item["id"],)).fetchone()
                if latest is None or latest[0] == "reopened":
                    self.resolve(item["id"], resolution="superseded", actor_ref=actor,
                                 note="冲突涉及的产品版本已被新版本取代")
                    superseded += 1
        return {"detected": len(found), "fresh": fresh, "superseded": superseded}

    def record_alias(self, alias_ref: str, canonical_ref: str, *, reason: str,
                     actor_ref: str) -> dict[str, Any]:
        from .mission_company_cik import same_company_ref, validate_company_ref

        actor = self._actor(actor_ref)
        canonical = validate_company_ref(canonical_ref)
        if alias_ref == canonical:
            raise ProductConsistencyError("an alias differs from its canonical ref")
        if alias_ref.startswith("company:sec-cik:") and not same_company_ref(alias_ref, canonical):
            raise ProductConsistencyError("a CIK alias must name the same CIK")
        record = {"schema_version": SCHEMA_VERSION, "alias_ref": alias_ref, "canonical_ref": canonical,
                  "reason": str(reason)[:500], "actor_ref": actor, "created_at": self.clock()}
        record["content_hash"] = content_hash({k: v for k, v in record.items()
                                               if k not in ("content_hash", "created_at")})
        with self._transaction() as cur:
            seen = cur.execute("SELECT record_json FROM company_ref_aliases WHERE alias_ref=?",
                               (alias_ref,)).fetchone()
            if seen is not None:
                return {**json.loads(seen[0]), "write_status": "duplicate"}
            cur.execute(
                "INSERT INTO company_ref_aliases(alias_ref,canonical_ref,reason,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                (alias_ref, canonical, record["reason"], canonical_json(record), record["content_hash"],
                 actor, record["created_at"]))
        return {**record, "write_status": "fresh"}

    def record_aliases_from_scan(self, *, actor_ref: str) -> dict[str, Any]:
        """One alias row per non-canonical company ref the scan finds (``:`` spelling)."""

        from .mission_company_cik import canonical_spelling

        report = scan_noncanonical_refs(self.connection, light=True)
        written = []
        for ref in sorted(report["refs"]):
            if not ref.startswith("company:sec-cik:"):
                continue
            result = self.record_alias(ref, canonical_spelling(ref), actor_ref=actor_ref,
                                       reason="CIK 未补足 10 位的历史写法；历史记录保持原样，读取时按此映射")
            written.append({"alias_ref": ref, "write_status": result["write_status"]})
        return {"aliases": written}


def company_ref_aliases(connection: Any) -> dict[str, str]:
    """alias -> canonical, from the append-only alias table (empty without it)."""

    if not _table_exists(connection, "company_ref_aliases"):
        return {}
    return {row[0]: row[1] for row in connection.execute(
        "SELECT alias_ref, canonical_ref FROM company_ref_aliases").fetchall()}


# ---------------------------------------------------------------------------
# CIK spelling scan (read-only)
# ---------------------------------------------------------------------------

#: (table, columns) holding company refs or ids that embed them.
CIK_SCAN_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("coverage_mission_versions", ("record_json",)),
    ("forecast_model_versions", ("company_ref", "model_ref", "version_id")),
    ("model_forecast_line_versions", ("subject_ref", "line_ref")),
    ("sensitivity_projections", ("company_ref", "projection_ref")),
    ("company_dossier_versions", ("company_ref", "version_id")),
    ("deep_insight_gate_versions", ("company_ref", "version_id")),
    ("mission_deliverable_versions", ("subject_ref", "deliverable_ref")),
    ("debate_map_versions", ("subject_ref",)),
    ("company_scenario_versions", ("company_ref",)),
    ("holding_period_return_bridges", ("company_ref",)),
    ("economic_invariant_verdicts", ("company_ref", "verdict_ref")),
    ("zero_base_review_versions", ("company_ref",)),
    ("catalyst_calendar_versions", ("company_ref",)),
    ("consensus_estimate_versions", ("company_ref",)),
    ("valuation_snapshot_versions", ("company_ref",)),
    ("company_overlay_versions", ("company_ref", "overlay_ref", "record_json")),
    ("claim_versions", ("claim_json",)),
    ("research_quality_score_versions", ("subject_ref",)),
    ("event_judgements", ("company_ref",)),
)


_JSON_COLUMNS = frozenset({"record_json", "claim_json", "content_json"})


def scan_noncanonical_refs(connection: Any, *, sample: int = 3, light: bool = False) -> dict[str, Any]:
    """Every short-CIK company ref (``:`` ref or ``-`` slug) in the known tables.

    Read-only; counts rows per table and column, and keeps a few row ids.
    ``light`` reads the ref / id columns only (no JSON bodies) -- what a
    periodic alias pass needs; the full scan is the report.
    """

    from .mission_company_cik import canonical_spelling, noncanonical_refs_in_text

    found: dict[str, dict[str, Any]] = {}
    for table, columns in CIK_SCAN_COLUMNS:
        if not _table_exists(connection, table):
            continue
        present = [column for column in columns if column in _columns(connection, table)
                   and not (light and column in _JSON_COLUMNS)]
        for column in present:
            try:
                rows = connection.execute(
                    f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE '%sec-cik%'").fetchall()
            except sqlite3.Error:
                continue
            for rowid, value in rows:
                for ref in noncanonical_refs_in_text(value):
                    entry = found.setdefault(ref, {"canonical": canonical_spelling(ref), "where": {}})
                    slot = entry["where"].setdefault(f"{table}.{column}", {"rows": 0, "sample_rowids": []})
                    slot["rows"] += 1
                    if len(slot["sample_rowids"]) < sample:
                        slot["sample_rowids"].append(rowid)
    return {"refs": found, "count": len(found)}


def scan_state_files(state_dir: str | Path, *, limit: int = 2000) -> dict[str, list[str]]:
    """Short-CIK refs inside the state directory's JSON files (weekly brief overlays ...)."""

    from .mission_company_cik import noncanonical_refs_in_text

    out: dict[str, list[str]] = {}
    root = Path(state_dir)
    seen = 0
    for path in sorted(root.rglob("*.json")):
        if seen >= limit:
            break
        seen += 1
        try:
            if path.stat().st_size > 5_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        refs = noncanonical_refs_in_text(text)
        if refs:
            out[str(path.relative_to(root))] = refs
    return out


# ---------------------------------------------------------------------------
# what redraft_policy and the cockpit read
# ---------------------------------------------------------------------------

#: redraft product -> the conflict products that make it need a redraft.
CONFLICT_PRODUCTS: Mapping[str, tuple[str, ...]] = {
    "initial_screen": ("initial_screen",), "dossier": ("company_dossier",),
    "earnings_preview": ("earnings_preview",), "investment_memo": ("return_bridge", "forecast_model"),
    "event_judgement": ("event_note",), "industry_framework": (), "ask": (),
    "deep_insight_gate": ("deep_insight_gate",),
}


def redraft_signals(connection: Any, company_ref: str, product: str, *,
                    version_ref: str | None = None, today: str | None = None) -> list[dict[str, Any]]:
    """``[{"code", "detail", "refs", "fired"}]`` for ``redraft_policy``.

    ``stale_binding``: the product's own derived objects (its version, or the
    kinds :data:`REDRAFT_PRODUCT_KINDS` names) are bound to superseded
    upstreams.  ``consistency_conflict``: an open high/medium conflict names
    this product (or this exact version).
    """

    out = []
    kinds = set(REDRAFT_PRODUCT_KINDS.get(product, ()))
    stale = [item for item in evaluate_bindings(connection, company_ref, today=today)
             if item["binding_status"] == STALE
             and (item["kind"] in kinds or (version_ref and item["version_ref"] == version_ref))]
    out.append({"code": "stale_binding", "fired": bool(stale),
                "detail": (f"{len(stale)} 份派生结果基于旧版本上游：" + "；".join(
                    item["display_note"] for item in stale[:3])) if stale else "派生结果都基于当前版本",
                "refs": [item["version_ref"] for item in stale][:20], "count": len(stale)})
    names = set(CONFLICT_PRODUCTS.get(product, ()))
    conflicts = [item for item in detect_conflicts(connection, company_ref, today=today)
                 if item["severity"] in TRIGGER_SEVERITIES
                 and (names & set(item.get("products") or ())
                      or (version_ref and version_ref in (item.get("version_refs") or ())))]
    out.append({"code": "consistency_conflict", "fired": bool(conflicts),
                "detail": ("；".join(f"{item['label']}：{item['detail']}" for item in conflicts[:3])
                           if conflicts else "没有涉及这份产品的跨产物冲突"),
                "refs": [item["id"] for item in conflicts][:20], "count": len(conflicts)})
    return out


def consistency_view(connection: Any, company_ref: str, *, today: str | None = None) -> dict[str, Any]:
    """The cockpit's 「研究一致性」 block for one company (display fields only)."""

    day = today or datetime.now(timezone.utc).date().isoformat()
    bindings = evaluate_bindings(connection, company_ref, today=day)
    conflicts = detect_conflicts(connection, company_ref, today=day)
    stale = [item for item in bindings if item["binding_status"] == STALE]
    unbound = [item for item in bindings if item["binding_status"] == UNBOUND]
    entries = [{"kind": item["conflict_kind"], "label": item["label"], "severity": item["severity"],
                "severity_label": {"high": "严重", "medium": "需处理", "low": "提示"}[item["severity"]],
                "text": item["detail"], "conflict_ref": item["id"],
                "products": [PRODUCT_LABELS.get(name, name) for name in item.get("products") or ()]}
               for item in sorted(conflicts, key=lambda c: ({"high": 0, "medium": 1, "low": 2}[c["severity"]],
                                                            c["conflict_kind"]))]
    stale_entries = [{"kind": item["kind"], "label": item["label"], "status_label": STALE_LABEL,
                      "text": item["display_note"], "version_ref": item["version_ref"]}
                     for item in stale]
    by_kind: dict[str, int] = {}
    for item in stale:
        by_kind[item["label"]] = by_kind.get(item["label"], 0) + 1
    headline = []
    if entries:
        headline.append(f"{len(entries)} 处跨产品不一致")
    if stale:
        headline.append(f"{len(stale)} 份结果基于旧版本（" + "、".join(
            f"{label} {count}" for label, count in sorted(by_kind.items())) + "）")
    return {"title": "研究一致性", "as_of": day,
            "summary": "；".join(headline) if headline else "各产品之间没有发现不一致，派生结果都基于当前版本",
            "conflicts": entries, "stale": stale_entries,
            "stale_count": len(stale), "unbound_count": len(unbound),
            "conflict_count": len(entries)}


__all__ = [
    "CONFLICT_KINDS", "CURRENT", "Chain", "DERIVED_LABELS", "HISTORICAL", "Heads",
    "ProductConsistencyAuthority", "ProductConsistencyError", "RULE_REF", "SCHEMA_VERSION", "STALE",
    "STALE_LABEL", "TRIGGER_SEVERITIES", "UNBOUND", "VERSION_CHAINS", "binding_entry",
    "binding_status_index", "chain_for", "classification_in_prose", "company_ref_aliases",
    "confidence_in_prose", "consistency_view", "current_products", "derived_objects",
    "detect_conflicts", "evaluate_bindings", "operating_income_by_period", "recorded_conflicts",
    "redraft_signals", "scan_noncanonical_refs", "scan_state_files", "scenario_model_ref",
    "stale_bindings", "upstream_bindings", "version_refs_in",
]
