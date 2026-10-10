"""Batch C: "is there enough material to draft this product yet?"

The owner's rule (research capability diagnosis 2026-10-09, decision 4) is the
analyst's: collect a batch of material, then draft.  ``sufficiency(...)``
checks a product's required-materials checklist; every required item that is
missing opens (idempotently) a MaterialRequest (:mod:`material_requests`) so
the acquisition service has something to work on, and the product lane gets
one of three answers:

``sufficient``   everything required is here: draft now;
``insufficient`` something is missing and the wait has not expired: hold;
``timed_out``    the wait expired (``wait_until``, 24 h from the first check by
                 default) or every open request's sources are exhausted: draft
                 anyway, and every gap cites the request that tried
                 (``missing[i]["request"]``).

The initial-screen checklist: the latest reported quarter's transcript and
earnings release indexed; consensus at most 7 days old with a credible fiscal
calendar; at least 4 broker reports from the last 6 months; our model with
an EPS row (or an explicit reason it is blocked); the market block.

Read-only over Core and the evidence index; the only write is the request
ledger, and ``record=False`` makes even that a lookup.  A blank workspace
(no Core, no index) is ``insufficient`` with every item missing, never an
exception.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "material-sufficiency:0.1"
DEFAULT_WAIT_HOURS = 24
CONSENSUS_MAX_AGE_DAYS = 7
#: A "last reported period" this old means the fiscal calendar (and so the
#: consensus period labels) stopped moving -- the ACN FY26Q4/FY27Q1 bug.
CALENDAR_MAX_AGE_DAYS = 125
BROKER_WINDOW_DAYS = 183
#: How much a document must be *about* the company to count (see ``aboutness``).
OWN_DOCUMENT_ABOUTNESS = 0.7
BROKER_ABOUTNESS = 0.5

PRODUCTS: tuple[str, ...] = ("initial_screen", "dossier", "debate_map", "investment_memo", "event_judgement",
                             "earnings_preview", "industry_framework", "ask")

#: product -> required checks, each ``(check, params)``.  Data, not code.
REQUIREMENTS: Mapping[str, tuple[tuple[str, Mapping[str, Any]], ...]] = {
    "initial_screen": (
        ("latest_transcript", {}), ("latest_earnings_release", {}), ("consensus_fresh", {}),
        ("broker_reports", {"min_count": 4, "days": BROKER_WINDOW_DAYS}), ("model_eps", {}),
        ("market_block", {})),
    "investment_memo": (
        ("latest_transcript", {}), ("latest_earnings_release", {}), ("consensus_fresh", {}),
        ("broker_reports", {"min_count": 4, "days": BROKER_WINDOW_DAYS}), ("model_eps", {}),
        ("market_block", {})),
    "dossier": (
        ("latest_transcript", {}), ("latest_10k", {}), ("consensus_fresh", {}), ("model_eps", {})),
    "debate_map": (
        ("latest_transcript", {}), ("broker_reports", {"min_count": 4, "days": BROKER_WINDOW_DAYS})),
    "event_judgement": (
        ("latest_earnings_release", {}), ("consensus_fresh", {})),
    "earnings_preview": (
        ("consensus_fresh", {}), ("latest_transcript", {}), ("broker_reports", {"min_count": 2, "days": 90})),
    "industry_framework": (),
    "ask": (),
}

CHECK_LABELS: Mapping[str, str] = {
    "latest_transcript": "最近一个已报告季度的电话会纪要",
    "latest_earnings_release": "最近一个已报告季度的业绩新闻稿（8-K 附件 99.1）",
    "latest_10k": "最新年报 10-K",
    "consensus_fresh": "一致预期（≤7 天，财期标签正确）",
    "broker_reports": "近期券商研报",
    "model_eps": "我们的模型（含 EPS 行或明确的阻断原因）",
    "market_block": "行情数据块",
}
SOURCE_PRIORITY: Mapping[str, tuple[str, ...]] = {
    "latest_transcript": ("indexed_corpus", "alphaengine", "web"),
    "latest_earnings_release": ("indexed_corpus", "sec_edgar", "web"),
    "latest_10k": ("indexed_corpus", "sec_edgar"),
    "consensus_fresh": ("consensus_vendor",),
    "broker_reports": ("indexed_corpus", "alphaengine", "sales_notes"),
    "model_eps": ("forecast_model_lane",),
    "market_block": ("market_price_lane",),
}

_NAME_SUFFIX_RE = re.compile(
    r"(?i)[,\s]+(plc|inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|llc|l\.p\.|holdings?|"
    r"group|sa|ag|nv|se)\s*$")
_GENERIC_FIRST = frozenset({"international", "global", "american", "united", "first", "general", "national",
                            "the", "new", "china", "digital"})


# ---------------------------------------------------------------------------
# shared readers (also used by material_pack and redraft_policy)
# ---------------------------------------------------------------------------


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def as_of_day(as_of: Any) -> str:
    return str(as_of or today_iso())[:10]


def days_before(day: str, days: int) -> str:
    return (date.fromisoformat(day[:10]) - timedelta(days=days)).isoformat()


def core_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "core.sqlite"


def open_core_ro(state_dir: str | Path) -> sqlite3.Connection | None:
    path = core_path(state_dir)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def table_exists(connection: sqlite3.Connection | None, name: str) -> bool:
    if connection is None:
        return False
    try:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def mission(connection: sqlite3.Connection | None) -> dict[str, Any] | None:
    if not (table_exists(connection, "coverage_mission_versions")
            and table_exists(connection, "coverage_mission_pointer")):
        return None
    row = connection.execute(
        "SELECT v.record_json FROM coverage_mission_versions v JOIN coverage_mission_pointer p "
        "ON p.mission_version_id=v.mission_version_id ORDER BY p.updated_at DESC LIMIT 1").fetchone()
    return None if row is None else json.loads(row[0])


def _clean_name(name: str) -> str:
    previous = None
    name = name.strip()
    while previous != name:
        previous = name
        name = _NAME_SUFFIX_RE.sub("", name).strip(" ,.")
    if name.isupper() and len(name) > 5:
        name = name.title()
    return name


def company_identity(connection: sqlite3.Connection | None, company_ref: str,
                     state_dir: str | Path | None = None) -> dict[str, Any]:
    """Ticker, a display name and the aliases a title or passage names it by."""

    ticker = name = None
    record = mission(connection)
    for member in (record or {}).get("universe") or ():
        if member.get("company_ref") == company_ref:
            ticker = member.get("ticker")
            name = member.get("name")
    if not name and table_exists(connection, "coverage_mission_statement_filings"):
        row = connection.execute(
            "SELECT entity_name FROM coverage_mission_statement_filings WHERE company_ref=? AND entity_name IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1", (company_ref,)).fetchone()
        name = row[0] if row else None
    if not name and state_dir is not None:
        from . import evidence_index

        for doc in evidence_index.documents(state_dir, company_ref=company_ref, kinds=["sec_10k", "sec_10q"],
                                            bases=("filed",), limit=5):
            if doc.get("house"):
                name = doc["house"]
                break
    if not ticker:
        match = re.match(r"company:ticker:([a-z0-9.\-]+)$", company_ref or "", re.I)
        ticker = match.group(1).upper() if match else None
    clean = _clean_name(str(name)) if name else None
    aliases: list[str] = []
    if clean:
        aliases.append(clean)
        first = clean.split()[0]
        if len(first) >= 4 and first.lower() not in _GENERIC_FIRST and first != clean:
            aliases.append(first)
    patterns = [re.compile(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", re.I) for alias in aliases]
    if ticker:
        patterns.append(re.compile(rf"(?<![A-Za-z]){re.escape(str(ticker))}(?![A-Za-z])"))
    return {"company_ref": company_ref, "ticker": ticker, "name": clean or ticker or company_ref,
            "aliases": aliases + ([str(ticker)] if ticker else []), "_patterns": patterns}


def mentions(identity: Mapping[str, Any], text: str | None) -> bool:
    return bool(text) and any(pattern.search(text) for pattern in identity.get("_patterns") or ())


def aboutness(doc: Mapping[str, Any], identity: Mapping[str, Any]) -> float:
    """How much an indexed document is *about* the company, 0..1, deterministic.

    The mission files many documents under a company that only mention it (a
    conference call, a sector weekly); a title that names the company is the
    strongest signal, then the vendor's own company list, then the share of
    passages that name it.
    """

    if mentions(identity, doc.get("title")):
        return 1.0
    count = int(doc.get("company_passages") or 0)
    ratio = count / max(1, int(doc.get("passage_count") or 1))
    score = 0.0
    if "listed" in (doc.get("bases") or ()):
        score = 0.6
    if ratio >= 0.25 and count >= 2:
        score = max(score, 0.7)
    elif ratio >= 0.1 and count >= 2:
        score = max(score, 0.5)
    elif count:
        score = max(score, 0.3)
    elif "filed" in (doc.get("bases") or ()):
        score = max(score, 0.15)
    return score


def own_documents(state_dir: str | Path, company_ref: str, identity: Mapping[str, Any], kinds: Sequence[str], *,
                  as_of: str, days: int | None = None, threshold: float = OWN_DOCUMENT_ABOUTNESS,
                  limit: int = 400) -> list[dict[str, Any]]:
    """Indexed documents of ``kinds`` about the company, newest first, deduplicated by body."""

    from . import evidence_index

    try:
        docs = evidence_index.documents(state_dir, company_ref=company_ref, kinds=kinds, date_to=as_of,
                                        date_from=days_before(as_of, days) if days else None, limit=limit)
    except Exception:  # noqa: BLE001 - a broken index reads as no documents
        return []
    out, seen = [], set()
    for doc in docs:
        doc["aboutness"] = aboutness(doc, identity)
        if doc["aboutness"] < threshold or doc["text_sha256"] in seen:
            continue
        seen.add(doc["text_sha256"])
        out.append(doc)
    return out


def latest_reported_quarter(connection: sqlite3.Connection | None, company_ref: str, as_of: str,
                            consensus: Mapping[str, Any] | None = None) -> str | None:
    calendar = (consensus or {}).get("calendar") or {}
    if calendar.get("last_reported_period_end"):
        return str(calendar["last_reported_period_end"])[:10]
    if connection is None:
        return None
    try:
        from .financial_period import company_calendar

        found = company_calendar(connection, company_ref, as_of)
    except Exception:  # noqa: BLE001
        found = None
    return None if not found else str(found["last_reported_period_end"])[:10]


def model_version(connection: sqlite3.Connection | None, company_ref: str) -> dict[str, Any] | None:
    if not table_exists(connection, "forecast_model_versions"):
        return None
    row = connection.execute(
        "SELECT version_id, version_number, created_at FROM forecast_model_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    return None if row is None else {"version_ref": row["version_id"], "version_number": row["version_number"],
                                     "created_at": row["created_at"]}


def our_eps(state_dir: str | Path, company_ref: str) -> dict[str, Any]:
    path = core_path(state_dir)
    if not path.is_file():
        return {"status": "unavailable", "reason": "这个研究环境还没有数据库"}
    try:
        from .cockpit_v2.charts_api import ChartReader
        from .valuation_forward import our_eps as _our_eps

        return _our_eps(ChartReader(path), company_ref)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"读不出预测模型：{type(exc).__name__}"}


def facts(state_dir: str | Path, company_ref: str, as_of: str, *,
          numbers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The few numbers the checklist reads; reuses a numbers-pack json when given."""

    connection = open_core_ro(state_dir)
    try:
        identity = company_identity(connection, company_ref, state_dir)
        if numbers is not None:
            consensus = numbers.get("consensus") or {}
            market = numbers.get("market") or {}
            ours = next((row for row in (numbers.get("valuation") or {}).get("forward_pe") or ()
                         if row.get("source") == "ours"), None) or our_eps(state_dir, company_ref)
        else:
            consensus, market = {}, {}
            if connection is not None:
                try:
                    from .consensus_table import consensus_table_from

                    consensus = consensus_table_from(connection, company_ref, as_of, state_dir=state_dir,
                                                     today=as_of)
                except Exception as exc:  # noqa: BLE001
                    consensus = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
                try:
                    from .market_data_block import market_block_from

                    market = market_block_from(connection, company_ref, as_of=as_of)
                except Exception as exc:  # noqa: BLE001
                    market = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
            ours = our_eps(state_dir, company_ref)
        return {"identity": identity, "consensus": consensus, "market": market, "ours": ours,
                "model": model_version(connection, company_ref),
                "last_quarter_end": latest_reported_quarter(connection, company_ref, as_of, consensus),
                "has_core": connection is not None}
    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def _iso_week(day: str) -> str:
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def _item(key: str, *, satisfied: bool, detail: str, state: str | None = None,
          evidence: Sequence[str] = (), need: Mapping[str, Any] | None = None, period: str | None = None,
          acceptance: Mapping[str, Any] | None = None, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    label = CHECK_LABELS[key]
    if key == "broker_reports" and params:
        label = f"近 {params.get('days')} 天至少 {params.get('min_count')} 份关于本公司的券商研报"
    return {"key": key, "label": label, "satisfied": satisfied,
            "state": state or ("ok" if satisfied else "missing"), "detail": detail,
            "evidence": list(evidence)[:12], "need": dict(need or {}), "period": period,
            "acceptance": dict(acceptance or {}), "source_priority": list(SOURCE_PRIORITY.get(key, ()))}


def _check(key: str, params: Mapping[str, Any], *, state_dir: str | Path, company_ref: str, as_of: str,
           info: Mapping[str, Any]) -> dict[str, Any]:
    identity = info["identity"]
    last_end = info.get("last_quarter_end")
    quarter_period = f"quarter_ending:{last_end}" if last_end else f"latest_quarter_as_of:{as_of[:7]}"
    if key in ("latest_transcript", "latest_earnings_release"):
        kinds = ["transcript"] if key == "latest_transcript" else ["sec_exhibit"]
        docs = own_documents(state_dir, company_ref, identity, kinds, as_of=as_of, days=200)
        if last_end:
            fresh = [doc for doc in docs if str(doc.get("date") or "") > last_end]
            rule = f"{last_end} 季度结束之后发布"
        else:
            fresh = [doc for doc in docs if str(doc.get("date") or "") >= days_before(as_of, 100)]
            rule = "不知道最近已报告的季度；按 100 天内发布判断"
        need = {"kind": "document", "doc_kinds": kinds}
        acceptance = {"published_after": last_end, "about_company": True}
        if fresh:
            top = fresh[0]
            return _item(key, satisfied=True, evidence=[top["document_ref"]], period=quarter_period,
                         detail=f"已入索引：《{top.get('title') or top['document_ref']}》{top.get('date')}（{rule}）")
        latest = docs[0] if docs else None
        detail = (f"索引里最新一份是 {latest.get('date')}《{latest.get('title')}》，早于要求（{rule}）" if latest
                  else "索引里没有这家公司的这类资料")
        return _item(key, satisfied=False, detail=detail, need=need, period=quarter_period, acceptance=acceptance)
    if key == "latest_10k":
        docs = own_documents(state_dir, company_ref, identity, ["sec_10k"], as_of=as_of, days=430)
        if docs:
            return _item(key, satisfied=True, evidence=[docs[0]["document_ref"]],
                         detail=f"已入索引：{docs[0].get('date')}《{docs[0].get('title')}》")
        return _item(key, satisfied=False, detail="索引里没有近 14 个月的 10-K",
                     need={"kind": "document", "doc_kinds": ["sec_10k"]}, period=f"fiscal_year_as_of:{as_of[:4]}",
                     acceptance={"max_age_days": 430})
    if key == "consensus_fresh":
        consensus = info.get("consensus") or {}
        need = {"kind": "data", "data_item": "consensus_estimates"}
        period = f"week:{_iso_week(as_of)}"
        if consensus.get("status") != "ok":
            return _item(key, satisfied=False, need=need, period=period,
                         detail=f"没有一致预期（{consensus.get('reason') or '还没有入库'}）")
        stale = consensus.get("staleness") or {}
        age = stale.get("vendor_days_old")
        calendar = consensus.get("calendar") or {}
        last = calendar.get("last_reported_period_end")
        problems = []
        if age is None or age > CONSENSUS_MAX_AGE_DAYS:
            problems.append(f"数据商一致预期 {stale.get('vendor_as_of') or '日期未知'}，已 {age} 天")
        if not last:
            problems.append("不知道最近已报告的财期，财期标签无法核对")
        elif (date.fromisoformat(as_of) - date.fromisoformat(str(last)[:10])).days > CALENDAR_MAX_AGE_DAYS:
            problems.append(f"最近已报告财期停在 {last}，财期标签可能整体错位")
        if problems:
            return _item(key, satisfied=False, state="stale", need=need, period=period,
                         acceptance={"max_age_days": CONSENSUS_MAX_AGE_DAYS}, detail="；".join(problems))
        version = consensus.get("version") or {}
        return _item(key, satisfied=True, evidence=[str(version.get("ref") or "")],
                     detail=f"一致预期 {stale.get('vendor_as_of')}（{age} 天），已报告至 {last}"
                            + ("，财期已按业绩稿更正" if calendar.get("advanced_by_release") else ""))
    if key == "broker_reports":
        min_count, days = int(params.get("min_count", 4)), int(params.get("days", BROKER_WINDOW_DAYS))
        docs = own_documents(state_dir, company_ref, identity, ["broker_report"], as_of=as_of, days=days,
                             threshold=BROKER_ABOUTNESS)
        houses = sorted({str(doc.get("house") or "?") for doc in docs})
        detail = f"近 {days} 天关于本公司的券商研报 {len(docs)} 份（{len(houses)} 家：{'、'.join(houses[:8])}）"
        if len(docs) >= min_count:
            return _item(key, satisfied=True, evidence=[doc["document_ref"] for doc in docs], detail=detail,
                         params=params)
        return _item(key, satisfied=False, detail=detail + f"，少于 {min_count} 份", params=params,
                     need={"kind": "document", "doc_kinds": ["broker_report"]},
                     period=f"{days}d_to:{as_of[:7]}", acceptance={"min_documents": min_count, "max_age_days": days,
                                                                   "about_company": True})
    if key == "model_eps":
        ours = info.get("ours") or {}
        model = info.get("model")
        if ours.get("status") == "available":
            return _item(key, satisfied=True, evidence=[(model or {}).get("version_ref") or ""],
                         detail=f"模型 NTM EPS {ours.get('value')}（{ours.get('method')}）")
        if model is not None:
            return _item(key, satisfied=True, state="blocked", evidence=[model["version_ref"]],
                         detail=f"模型 v{model['version_number']} 没有可用的 EPS：{ours.get('reason')}（明确的阻断原因，可起草并写明）")
        return _item(key, satisfied=False, need={"kind": "model", "data_item": "forecast_model_eps"},
                     period="current", detail=f"还没有预测模型（{ours.get('reason') or '未建'}）")
    if key == "market_block":
        market = info.get("market") or {}
        if market.get("status") == "ok":
            price = market.get("price") or {}
            return _item(key, satisfied=True, detail=f"行情至 {price.get('date')}，收盘 {price.get('close')}")
        return _item(key, satisfied=False, need={"kind": "data", "data_item": "market_price_series"},
                     period=f"week:{_iso_week(as_of)}", detail=f"没有行情数据（{market.get('reason') or '未入库'}）")
    raise ValueError(f"unknown check {key}")


def _parse_ts(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def sufficiency(state_dir: str | Path, company_ref: str, product: str, as_of: str | None = None, *,
                record: bool = True, requester: Mapping[str, Any] | None = None,
                wait_hours: float = DEFAULT_WAIT_HOURS, now: str | None = None,
                numbers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The checklist for ``product`` about ``company_ref`` as of ``as_of``.

    Returns ``{"status", "drafting_allowed", "items", "missing", "wait_until",
    "first_checked_at", ...}``; each ``missing`` entry carries ``request``: the
    MaterialRequest it opened (``record=True``) or found (``record=False``,
    ``None`` when none exists yet).
    """

    from . import material_requests as mr

    if product not in REQUIREMENTS:
        raise ValueError(f"unknown product {product!r}; known: {list(REQUIREMENTS)}")
    as_of = as_of_day(as_of)
    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    info = facts(state_dir, company_ref, as_of, numbers=numbers)
    items = []
    for key, params in REQUIREMENTS[product]:
        try:
            items.append(_check(key, params, state_dir=state_dir, company_ref=company_ref, as_of=as_of, info=info))
        except Exception as exc:  # noqa: BLE001 - one broken reader is a missing item, not a crash
            items.append(_item(key, satisfied=False, detail=f"检查失败：{type(exc).__name__}: {exc}"[:300],
                               need={"kind": "data", "data_item": f"check:{key}"}, period=f"as_of:{as_of}",
                               params=params))
    who = dict(requester or {"lane": product})
    who.setdefault("lane", product)
    missing = []
    for item in items:
        if item["satisfied"]:
            continue
        found = None
        try:
            if record:
                found = mr.open_request(
                    state_dir, requester={**who, "section": who.get("section") or item["key"]},
                    subject=company_ref, need=item["need"], period=item["period"],
                    source_priority=item["source_priority"], acceptance=item["acceptance"],
                    deadline=(_parse_ts(stamp) + timedelta(hours=wait_hours)).isoformat(timespec="seconds"),
                    now=stamp)
            else:
                found = mr.find_request(state_dir, subject=company_ref, need=item["need"], period=item["period"])
        except mr.MaterialRequestError as exc:
            item["request_error"] = str(exc)
        missing.append({**item, "request": None if found is None else {
            key: found.get(key) for key in ("id", "status", "status_label", "created_at", "deadline", "attempt_count",
                                            "terminal", "write_status")}})
    first = min((entry["request"]["created_at"] for entry in missing if entry.get("request")), default=None)
    wait_until = None
    status = "sufficient"
    reason = "必需材料齐全，可以起草"
    if missing:
        base = _parse_ts(first) if first else _parse_ts(stamp)
        wait_until = (base + timedelta(hours=wait_hours)).isoformat(timespec="seconds")
        all_terminal = all(entry.get("request") and entry["request"].get("terminal") for entry in missing)
        if all_terminal:
            status, reason = "timed_out", "缺的材料已全部尝试获取并结束（来源用尽/超时），按缺口起草并在正文引用这些请求"
        elif _parse_ts(stamp) >= _parse_ts(wait_until):
            status, reason = "timed_out", f"等待已到期（{wait_until}），按缺口起草并在正文引用这些请求"
        else:
            status, reason = "insufficient", f"缺 {len(missing)} 项必需材料，等待获取至 {wait_until}"
    return {
        "schema_version": SCHEMA_VERSION, "company_ref": company_ref, "product": product, "as_of": as_of,
        "checked_at": stamp, "status": status, "drafting_allowed": status != "insufficient", "reason": reason,
        "items": items, "missing": missing, "first_checked_at": first, "wait_until": wait_until,
        "wait_hours": wait_hours, "recorded": bool(record),
        "gap_request_refs": [entry["request"]["id"] for entry in missing if entry.get("request")],
        "workspace": {"has_core": info["has_core"], "has_index": _has_index(state_dir)},
    }


def _has_index(state_dir: str | Path) -> bool:
    from . import evidence_index

    connection = evidence_index.open_index_readonly(state_dir)
    if connection is None:
        return False
    connection.close()
    return True


__all__ = [
    "CHECK_LABELS", "DEFAULT_WAIT_HOURS", "PRODUCTS", "REQUIREMENTS", "SCHEMA_VERSION", "aboutness",
    "company_identity", "facts", "latest_reported_quarter", "mentions", "model_version", "own_documents",
    "sufficiency",
]
