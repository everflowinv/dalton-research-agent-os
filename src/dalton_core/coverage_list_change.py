"""Changing the coverage list from the cockpit (vision-gap #12, ``scope_expansion``).

The owner adds or removes a company on the page.  This module turns that into
three things the owner can read before confirming, and then into a new
CoverageMission version plus the discovery plans that follow from it:

1. **who the company is** -- the SEC issuer (CIK, ticker, registered name)
   through the same resolver the first-mission flow uses
   (``workspace_mission_setup.resolve_sec_ticker``; injectable so tests and a
   draft that must not touch the network can pass their own);
2. **which discovery plans change** -- for each plan the writer has selected
   (SEC filings, AlphaEngine, web search) the companies that enter or leave;
3. **what it does to the budget** -- the caps do not change with the list
   (they are the mission's, and changing them stays the budget page's job), so
   the impact is the share of each cap per company and the searches a day the
   new company adds.

**Only company identity goes into a plan.**  A new plan version is the
current plan with its ``companies`` map changed: name, ticker and aliases (or
CIK, or search terms, whatever shape that plan's companies already have).  The
specs -- the query templates -- are copied byte for byte from the plan in
force; this module never writes a query form (the concurrent
``fix/code-owned-query-forms`` work owns those).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "coverage-list-change-0.1"
COVERAGE_TIERS = ("A", "B", "C")
DEFAULT_TIER = "B"
DEFAULT_PRIORITY = "P1"
MAX_CHANGES = 10
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_COVERAGE_SUFFIX = re.compile(r":coverage-v[0-9]+$")
COVERAGE_SELECTOR_MARK = ":coverage-change:"


class CoverageListChangeError(ValueError):
    """The requested change cannot be drafted or published as asked."""


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------


def normalize_request(add: Any, remove: Any) -> tuple[list[dict[str, str]], list[str]]:
    """``add``: tickers (or ``{"ticker", "coverage_tier"}``); ``remove``: company refs."""

    additions: list[dict[str, str]] = []
    for item in add or ():
        if isinstance(item, str):
            item = {"ticker": item}
        if not isinstance(item, Mapping):
            raise CoverageListChangeError("每个新增公司必须是股票代码")
        ticker = str(item.get("ticker") or "").strip().upper().lstrip("$")
        if not _TICKER_RE.fullmatch(ticker):
            raise CoverageListChangeError(f"「{ticker or '空'}」不是有效的美股代码")
        tier = str(item.get("coverage_tier") or DEFAULT_TIER).upper()
        if tier not in COVERAGE_TIERS:
            raise CoverageListChangeError("覆盖等级只能是 A、B 或 C")
        if any(entry["ticker"] == ticker for entry in additions):
            continue
        additions.append({"ticker": ticker, "coverage_tier": tier})
    removals: list[str] = []
    for ref in remove or ():
        if not isinstance(ref, str) or not ref.startswith("company:"):
            raise CoverageListChangeError("要移除的公司必须是当前覆盖名单里的公司")
        if ref not in removals:
            removals.append(ref)
    if not additions and not removals:
        raise CoverageListChangeError("请至少新增或移除一家公司")
    if len(additions) + len(removals) > MAX_CHANGES:
        raise CoverageListChangeError(f"一次最多调整 {MAX_CHANGES} 家公司")
    return additions, removals


def ref_style(universe: Sequence[Mapping[str, Any]]) -> str:
    """``sec-cik`` when the mission names companies by CIK (legacy), else ``ticker``."""

    refs = [str(member.get("company_ref") or "") for member in universe]
    if refs and all(ref.startswith("company:sec-cik:") for ref in refs):
        return "sec-cik"
    return "ticker"


def company_ref_for(ticker: str, cik: str | None, style: str) -> str | None:
    if style == "sec-cik":
        if not cik:
            return None
        from .mission_company_cik import sec_cik_company_ref

        return sec_cik_company_ref(cik)
    return f"company:ticker:{ticker.lower()}"


def resolve_additions(
    mission: Mapping[str, Any], additions: Sequence[Mapping[str, str]],
    resolver: Callable[[str], Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Each requested ticker with its SEC issuer, or the reason it has none."""

    universe = list(mission["universe"])
    style = ref_style(universe)
    by_ticker = {str(member.get("ticker") or "").upper(): member for member in universe}
    # Each lookup is a bounded subprocess (15 s); asked together rather than
    # one after another, so a draft of several tickers holds the writer for
    # one lookup's time, not the sum.
    answers: dict[str, Any] = {}
    pending = [item["ticker"] for item in additions if item["ticker"] not in by_ticker]
    if resolver is not None and pending:
        from concurrent.futures import ThreadPoolExecutor

        def ask(ticker: str) -> Any:
            try:
                return dict(resolver(ticker))
            except Exception as exc:  # noqa: BLE001 - the draft says why
                return exc

        with ThreadPoolExecutor(max_workers=min(len(pending), MAX_CHANGES)) as pool:
            answers = dict(zip(pending, pool.map(ask, pending)))
    resolved: list[dict[str, Any]] = []
    for item in additions:
        ticker = item["ticker"]
        entry: dict[str, Any] = {
            "ticker": ticker, "coverage_tier": item["coverage_tier"],
            "bootstrap_priority": DEFAULT_PRIORITY,
            "cik": None, "name": None, "resolution": "unresolved", "error": None,
        }
        if ticker in by_ticker:
            entry.update({"resolution": "already_covered",
                          "company_ref": by_ticker[ticker]["company_ref"],
                          "error": "已在覆盖名单中"})
            resolved.append(entry)
            continue
        if resolver is not None:
            try:
                issuer = answers.get(ticker)
                if isinstance(issuer, BaseException):
                    raise issuer
                if not isinstance(issuer, Mapping):
                    raise ValueError("no answer")
                cik = str(issuer["cik"]).zfill(10)
                if not re.fullmatch(r"[0-9]{10}", cik):
                    raise ValueError("invalid CIK")
                entry.update({"cik": cik, "name": str(issuer.get("name") or "").strip() or None,
                              "resolution": "resolved"})
            except Exception as exc:  # noqa: BLE001 - the draft says why, the owner decides
                entry["error"] = f"SEC 未能解析 {ticker}：{str(exc)[:160]}"
        else:
            entry["error"] = "本环境没有配置 SEC 发行人解析"
        entry["company_ref"] = company_ref_for(ticker, entry["cik"], style)
        if entry["company_ref"] is None:
            entry["error"] = entry["error"] or "本环境按 SEC CIK 命名公司，必须先解析出 CIK"
        resolved.append(entry)
    return resolved


def new_universe(
    mission: Mapping[str, Any], additions: Sequence[Mapping[str, Any]], removals: Sequence[str],
) -> list[dict[str, Any]]:
    current = [dict(member) for member in mission["universe"]]
    refs = {member["company_ref"] for member in current}
    unknown = [ref for ref in removals if ref not in refs]
    if unknown:
        raise CoverageListChangeError("要移除的公司不在当前覆盖名单中")
    kept = [member for member in current if member["company_ref"] not in set(removals)]
    for entry in additions:
        if entry.get("resolution") == "already_covered" or not entry.get("company_ref"):
            continue
        if entry["company_ref"] in {member["company_ref"] for member in kept}:
            continue
        kept.append({
            "company_ref": entry["company_ref"], "ticker": entry["ticker"],
            "coverage_tier": entry["coverage_tier"],
            "bootstrap_priority": entry.get("bootstrap_priority") or DEFAULT_PRIORITY,
        })
    if not kept:
        raise CoverageListChangeError("覆盖名单不能为空：至少保留一家公司")
    if [m["company_ref"] for m in kept] == [m["company_ref"] for m in current]:
        raise CoverageListChangeError("这次调整没有改变覆盖名单")
    return kept


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------


def _company_entry(plan: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The new company's entry in this plan's own company shape, or ``None``."""

    from .mission_source_discovery import SEC_SOURCE_REF, WEB_SEARCH_SOURCE_REF
    from .workspace_mission_setup import issuer_short_name

    ticker = entry["ticker"]
    registered = [entry["name"]] if entry.get("name") else []
    short = issuer_short_name(ticker, registered)
    sample = next(iter(plan["companies"].values()), {})
    keys = set(sample) if isinstance(sample, Mapping) else set()
    if plan["source_ref"] == SEC_SOURCE_REF:
        return {"cik": entry["cik"]} if entry.get("cik") else None
    if keys == {"name", "ticker", "aliases"}:
        return {"name": short, "ticker": ticker, "aliases": [short]}
    if plan["source_ref"] == WEB_SEARCH_SOURCE_REF:
        return {"search_terms": ticker}
    return {"search_terms": short}


def plan_after(
    plan: Mapping[str, Any], additions: Sequence[Mapping[str, Any]], removals: Sequence[str],
    *, mission_version: int, created_at: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The next version of one plan, and what changed in it.

    ``None`` when the plan does not change, or when the change would leave it
    with no company (a plan must name one; the source then simply has nothing
    to search until a company is added again -- reported, not hidden).
    """

    from .mission_source_discovery import validate_discovery_plan

    companies = {ref: dict(value) for ref, value in plan["companies"].items()}
    added, skipped, removed = [], [], []
    for ref in removals:
        if ref in companies:
            companies.pop(ref)
            removed.append(ref)
    for entry in additions:
        if entry.get("resolution") == "already_covered" or not entry.get("company_ref"):
            continue
        ref = entry["company_ref"]
        if ref in companies:
            continue
        company = _company_entry(plan, entry)
        if company is None:
            skipped.append({"company_ref": ref, "ticker": entry["ticker"],
                            "why": "没有 SEC CIK，SEC 申报检索不会包含这家公司"})
            continue
        companies[ref] = company
        added.append(ref)
    change = {"source_ref": plan["source_ref"], "plan_ref": plan["id"],
              "added": added, "removed": removed, "skipped": skipped,
              "searches_per_company_per_day": searches_per_company_per_day(plan)}
    if not added and not removed:
        return None, change
    if not companies:
        change["empty"] = True
        return None, change
    body = {key: json.loads(canonical_json(value)) for key, value in plan.items()
            if key != "content_hash"}
    body["companies"] = companies
    body["id"] = _COVERAGE_SUFFIX.sub("", str(plan["id"])) + f":coverage-v{int(mission_version)}"
    body["created_at"] = created_at
    nxt = validate_discovery_plan({**body, "content_hash": content_hash(body)})
    change["next_plan_ref"] = nxt["id"]
    return nxt, change


def searches_per_company_per_day(plan: Mapping[str, Any]) -> float:
    total = 0.0
    for spec in plan.get("specs") or ():
        days = spec.get("rediscovery_interval_days") if isinstance(spec, Mapping) else None
        if isinstance(days, (int, float)) and days > 0:
            total += 1.0 / float(days)
    return round(total, 2)


def budget_impact(
    mission: Mapping[str, Any], before: int, after: int, changes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The caps stay; what changes is each company's share and the searches a day."""

    from .budget_pools import pool_caps

    budget = mission["budget"]
    cost = float(budget["max_daily_cost_usd"])
    try:
        coverage_pool = pool_caps(budget)["caps_micros"]["coverage"] / 1_000_000
    except Exception:  # noqa: BLE001 - a pool split it cannot read is shown as the whole cap
        coverage_pool = cost
    alpha = int(budget.get("max_alphaengine_calls_24h") or 0)

    def share(value: float, n: int) -> float | None:
        return None if n <= 0 else round(value / n, 2)

    delta = 0.0
    for change in changes:
        per = float(change.get("searches_per_company_per_day") or 0.0)
        delta += per * (len(change.get("added") or ()) - len(change.get("removed") or ()))
    return {
        "caps_unchanged": True,
        "max_daily_cost_usd": cost,
        "max_daily_paid_calls": int(budget["max_daily_paid_calls"]),
        "max_alphaengine_calls_24h": alpha,
        "companies_before": before, "companies_after": after,
        "coverage_pool_usd_per_company": {"before": share(coverage_pool, before),
                                          "after": share(coverage_pool, after)},
        "alphaengine_calls_per_company": {"before": share(alpha, before),
                                          "after": share(alpha, after)},
        "searches_per_day_delta": round(delta, 2),
        "note": ("预算上限不随名单变化；新增公司会和现有公司分享同一份每日额度。"
                 "如需提高每日预算上限，请联系管理员。"),
    }


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise



def selector_for(source_ref: str) -> tuple[str, str]:
    """(selector file name, selector schema) for a discovery source."""

    from .macos_launchagent import (
        ALPHAENGINE_PLAN_SELECTOR, SEC_PLAN_SELECTOR, WEB_PLAN_SELECTOR,
    )
    from .mission_source_discovery import (
        ALPHAENGINE_SOURCE_REF, SEC_SOURCE_REF, WEB_SEARCH_SOURCE_REF,
    )

    return {
        SEC_SOURCE_REF: (SEC_PLAN_SELECTOR, "sec-discovery-plan-selection-0.1"),
        ALPHAENGINE_SOURCE_REF: (ALPHAENGINE_PLAN_SELECTOR, "alphaengine-discovery-plan-selection-0.1"),
        WEB_SEARCH_SOURCE_REF: (WEB_PLAN_SELECTOR, "web-discovery-plan-selection-0.1"),
    }[source_ref]


def write_plan(
    state_dir: str | Path, plan: Mapping[str, Any], *, mission_version: int,
) -> dict[str, str]:
    """Write the plan beside the others and point its source's selector at it.

    The old plan file is left where it is (a plan version is never edited), and
    the selector id carries ``:coverage-change:`` so a writer that is not
    workspace-bound knows on restart that this selection is the owner's.
    """

    directory = Path(state_dir) / "discovery-plans"
    source = plan["source_ref"].split(":", 1)[-1]
    filename = f"{source}-coverage-v{int(mission_version)}.json"
    _atomic_json(directory / filename, plan)
    selector_name, selector_schema = selector_for(plan["source_ref"])
    body = {
        "schema_version": selector_schema,
        "id": f"discovery-plan-selection{COVERAGE_SELECTOR_MARK}{source}:{int(mission_version)}",
        "status": "approved", "source_ref": plan["source_ref"],
        "plan_ref": plan["id"], "plan_hash": plan["content_hash"], "plan_path": filename,
    }
    _atomic_json(directory / selector_name, {**body, "content_hash": content_hash(body)})
    return {"plan_path": str(directory / filename), "selector": str(directory / selector_name),
            "plan_ref": plan["id"]}


def coverage_selected_plan(state_dir: str | Path, source_ref: str) -> Path | None:
    """The plan an owner coverage change selected for this source, or ``None``.

    Only a selector this module wrote counts, so a legacy installation whose
    older selectors were never read at start keeps reading its configured
    plan until the owner actually changes the list.
    """

    from .macos_launchagent import _selected_discovery_plan

    selector_name, selector_schema = selector_for(source_ref)
    path = Path(state_dir) / "discovery-plans" / selector_name
    try:
        selector = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(selector, Mapping) or COVERAGE_SELECTOR_MARK not in str(selector.get("id") or ""):
        return None
    try:
        selected = _selected_discovery_plan(
            Path(state_dir), selector_filename=selector_name,
            default_filename=str(selector.get("plan_path") or ""), source_ref=source_ref,
            selector_schema=selector_schema, label=source_ref)
    except (OSError, ValueError):
        return None
    return selected if selected.is_file() else None


def now_text(clock: Callable[[], datetime] | None = None) -> str:
    return (clock() if clock else datetime.now(timezone.utc)).astimezone(
        timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "COVERAGE_SELECTOR_MARK", "CoverageListChangeError", "SCHEMA_VERSION",
    "budget_impact", "company_ref_for", "coverage_selected_plan", "new_universe",
    "normalize_request", "plan_after", "ref_style", "resolve_additions",
    "searches_per_company_per_day", "selector_for", "write_plan",
]
