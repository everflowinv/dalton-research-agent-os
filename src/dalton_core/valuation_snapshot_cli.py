"""P11c-E: the child that prices one company against its own filings.

**No model call.** Every figure this writes is arithmetic over three things
that already exist in the ledger: a market-price series version, the filed
statement lines behind it, and the share count the price connector observed.
The run is therefore free, cannot be refused by the router, and produces the
same bytes twice given the same inputs.

Three decisions worth stating, because each of them is a place a valuation
lane usually goes quietly wrong.

**The fourth quarter is derived, and says so.** No filer this system covers
states a fourth quarter on its own: a 10-K states the year, and the three
10-Qs before it state the three quarters inside it. A trailing twelve months
built only from stated quarters is therefore impossible for every company in
the universe -- which is why ``valuation_snapshot_versions`` was empty. This
takes the difference the company's own cumulative figures imply, through
``company_model_series.quarterly_series``, and hands it over labelled with
both accessions so the authority can re-do the subtraction. A derived quarter
is not a guess; an *undeclared* derived quarter would be.

**A missing input is a missing metric, never a missing snapshot.** Four of the
five covered companies file no single total-debt concept, so their enterprise
value is not something this system knows. That costs them EV/EBITDA and
nothing else: the snapshot is still published, still carries P/E, P/S and free
cash flow yield, and carries a line naming what was looked for and not found.
Holding the whole snapshot back over one absent balance-sheet line would be
the same mistake in a more defensible coat.

**Stale is refused, not priced.** A price more than three trading days behind
is not a price, and the trading calendar used to measure that is the ledger's
own: the union of settled bar dates across the covered universe. No market
calendar is hard-coded here, because a hard-coded holiday list is a fact
about the world that nothing in this repository can cite.

Exit 0 when the run completed, including when it decided nothing needed doing.
``formal_authority_writes`` is 0: a snapshot is not a Claim about the world,
it is arithmetic about a price.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .company_model_series import DERIVED, REPORTED, quarterly_series
from .coverage_mission import CoverageMissionAuthority
from .market_price import MarketPriceSeriesAuthority, bar_is_provisional
from .store import DaltonStore, canonical_json, content_hash
from .valuation_snapshot import (
    COMPONENT_DERIVED,
    INPUT_COVERAGE_HELD,
    INPUT_COVERAGE_MISSING,
    NON_NEGATIVE_ROLES,
    ROLE_AGGREGATION,
    TRAILING_QUARTERS,
    ValuationSnapshotAuthority,
    ValuationSnapshotError,
)

SUMMARY_SCHEMA_VERSION = "0.1"
WRITE_SCOPE = "valuation"
FINGERPRINT_VERSION = "valuation-snapshot-fingerprint:0.1"
FUNDAMENTAL_SOURCE_REF = "source:sec-edgar"

# How far behind the covered universe's newest settled trading day this
# company's newest settled bar may be. Three, as the mandate says; measured in
# trading days the ledger can name rather than in calendar days, so a long
# weekend is not mistaken for a stale feed.
MAX_PRICE_TRADING_DAY_LAG = 3
# How many filing dates become fundamental windows. Every filing this Core
# holds for a company is nine; eight keeps the record bounded while leaving
# the percentile resting on more than one set of figures, which is the
# difference between ``price_and_filed_fundamentals`` and ``price_only``.
MAX_WINDOWS = 8
# Three years of settled bars. The percentile wants a history; the record does
# not want an unbounded one, and a bar from five years ago is being compared
# with fundamentals nobody has filed since.
MAX_PRICE_HISTORY_BARS = 756

# Which filed concept answers each role, in the order they are tried. The
# first candidate that yields a usable figure wins, which is why the list is
# ordered rather than a set: EPAM files revenue under the contract-with-
# customer concept and IBM files it under ``Revenues``, and neither is a
# fallback for a failure -- they are two filers' vocabularies.
#
# ``total_debt`` is the honest gap. ``LongTermDebtNoncurrent`` is held for four
# of the five covered companies and is *not* total debt: it excludes the
# current portion and the finance leases. Reading it as total debt would make
# every enterprise value too small with nothing on the record to say so, so it
# is not on this list and EV/EBITDA is unavailable instead.
ROLE_CANDIDATES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "net_income": (
        ("income", "us-gaap:NetIncomeLoss"),
        ("income", "us-gaap:ProfitLoss"),
    ),
    "revenue": (
        ("income", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("income", "us-gaap:Revenues"),
        ("income", "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax"),
    ),
    "operating_income": (
        ("income", "us-gaap:OperatingIncomeLoss"),
    ),
    "depreciation_amortisation": (
        ("cash", "us-gaap:DepreciationDepletionAndAmortization"),
        ("cash", "us-gaap:DepreciationAmortizationAndAccretionNet"),
        ("cash", "us-gaap:DepreciationAndAmortization"),
        ("income", "us-gaap:DepreciationAndAmortization"),
    ),
    "operating_cash_flow": (
        ("cash", "us-gaap:NetCashProvidedByUsedInOperatingActivities"),
        ("cash", "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ),
    "capital_expenditure": (
        ("cash", "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"),
        ("cash", "us-gaap:PaymentsToAcquireProductiveAssets"),
    ),
    "total_debt": (
        ("balance", "us-gaap:DebtLongtermAndShorttermCombinedAmount"),
        ("balance", "us-gaap:DebtAndCapitalLeaseObligations"),
    ),
    "cash_and_equivalents": (
        ("balance", "us-gaap:CashAndCashEquivalentsAtCarryingValue"),
    ),
}

# What an operator reading the cockpit sees instead of a role name.
ROLE_LABELS: Mapping[str, str] = {
    "net_income": "净利润（TTM）",
    "revenue": "收入（TTM）",
    "operating_income": "经营利润（TTM）",
    "depreciation_amortisation": "折旧摊销（TTM）",
    "operating_cash_flow": "经营活动现金流（TTM）",
    "capital_expenditure": "资本开支（TTM）",
    "total_debt": "总债务（时点）",
    "cash_and_equivalents": "现金及等价物（时点）",
}


# -- small helpers ---------------------------------------------------------


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _table_exists(connection: Any, name: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _plain(value: Decimal) -> str:
    """The same text the authority would store for this figure."""

    if value == 0:
        return "0"
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _next_day(value: str) -> str:
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()


def missing_write_scope(mission: Mapping[str, Any]) -> str | None:
    """Why this mission may not publish a valuation, if it may not."""

    granted = set((mission.get("autonomy") or {}).get("may_write") or [])
    if WRITE_SCOPE in granted:
        return None
    return f"这份 mission 没有授予 {WRITE_SCOPE} 写入范围"


def universe(mission: Mapping[str, Any]) -> list[str]:
    """Every company the mission admits, in mission order."""

    rows: list[tuple[str, str]] = []
    for item in mission.get("universe") or ():
        if not isinstance(item, Mapping):
            continue
        company_ref = item.get("company_ref")
        if not isinstance(company_ref, str) or not company_ref.strip():
            continue
        rows.append((str(item.get("bootstrap_priority") or "P9"),
                     company_ref.strip()))
    rows.sort()
    return [company_ref for _priority, company_ref in rows]


# -- the filed side --------------------------------------------------------


def _usable_rows(
    rows: Sequence[Mapping[str, Any]], *, as_of: str,
) -> list[dict[str, Any]]:
    """Consolidated, valued lines this company had filed by ``as_of``.

    A dimensioned line is the same figure split by segment or by equity
    component; summing one into a trailing year books the company's Americas
    revenue as its revenue. They are dropped here rather than deduplicated
    later, because the two are indistinguishable once the axis is gone.
    """

    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("filed") or "") > as_of:
            continue
        if row.get("is_breakdown") or row.get("dimension_axis") is not None:
            continue
        if row.get("value") is None:
            continue
        out.append(dict(row))
    return out


def _component_of(quarter: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """One quarter as the authority's component contract wants it."""

    accessions = list(quarter.get("source_accessions") or ())
    basis = quarter.get("basis")
    if basis == REPORTED:
        if len(accessions) != 1:
            return None, "这个季度说不清是哪一份申报里的"
        return {
            "period_start": quarter["period_start"],
            "period_end": quarter["period_end"],
            "value": quarter["value"],
            "accession": accessions[0],
        }, None
    if basis != DERIVED:
        return None, f"未知的季度来源 {basis}"
    parts = list(quarter.get("derived_from") or ())
    if len(parts) != 2:
        return None, "差额季度没有说清它是哪两个累计数之差"
    parts.sort(key=lambda item: str(item["period_end"]))
    filed = [{
        "period_start": part["period_start"],
        "period_end": part["period_end"],
        "value": part["value"],
        "accession": part["accession"],
    } for part in parts]
    if any(part["accession"] is None for part in filed):
        return None, "差额季度的某一边没有 accession"
    return {
        "period_start": quarter["period_start"],
        "period_end": quarter["period_end"],
        "value": quarter["value"],
        # The longer of the two figures is the one the quarter is measured
        # back from, so it is the accession a reader should open first.
        "accession": filed[1]["accession"],
        "basis": COMPONENT_DERIVED,
        "derived_from": filed,
    }, None


def trailing_year(
    rows: Sequence[Mapping[str, Any]], *, currency: str,
) -> tuple[list[dict[str, Any]] | None, Decimal | None, str | None]:
    """The last four contiguous quarters, or why there are not four."""

    series = quarterly_series(rows)
    quarters = [item for item in series["quarters"]
                if item.get("value") is not None]
    if not quarters:
        return None, None, "这个科目下没有可用的季度期间"
    quarters.sort(key=lambda item: (str(item["period_end"]),
                                    str(item["period_start"])))
    chosen = [quarters[-1]]
    for candidate in reversed(quarters[:-1]):
        if _next_day(str(candidate["period_end"])) == str(chosen[0]["period_start"]):
            chosen.insert(0, candidate)
            if len(chosen) == TRAILING_QUARTERS:
                break
    if len(chosen) < TRAILING_QUARTERS:
        return None, None, (
            f"只拼得出 {len(chosen)} 个首尾相接的季度，凑不满 "
            f"{TRAILING_QUARTERS} 个，滚动一年无法成立")
    units = {str(item.get("unit") or "").casefold() for item in chosen}
    if units != {currency.casefold()}:
        return None, None, (
            f"这四个季度的计量单位是 {sorted(units)}，与价格的 {currency} 不一致")
    components: list[dict[str, Any]] = []
    total = Decimal(0)
    for quarter in chosen:
        component, why = _component_of(quarter)
        if component is None:
            return None, None, str(why)
        amount = _decimal(component["value"])
        if amount is None:
            return None, None, "某个季度的数字读不成十进制"
        total += amount
        components.append(component)
    return components, total, None


def latest_instant(
    rows: Sequence[Mapping[str, Any]], *, currency: str,
) -> tuple[list[dict[str, Any]] | None, Decimal | None, str | None]:
    """The newest point-in-time figure, as a single component."""

    series = quarterly_series(rows)
    instants = [item for item in series["instants"]
                if item.get("value") is not None]
    if not instants:
        return None, None, "这个科目下没有可用的时点数"
    newest = max(instants, key=lambda item: str(item["period_end"]))
    unit = str(newest.get("unit") or "").casefold()
    if unit != currency.casefold():
        return None, None, (
            f"这个时点数的计量单位是 {unit or '空'}，与价格的 {currency} 不一致")
    accessions = list(newest.get("source_accessions") or ())
    if len(accessions) != 1:
        return None, None, "这个时点数说不清是哪一份申报里的"
    amount = _decimal(newest["value"])
    if amount is None:
        return None, None, "这个时点数读不成十进制"
    return [{
        "period_start": None,
        "period_end": newest["period_end"],
        "value": newest["value"],
        "accession": accessions[0],
    }], amount, None


def build_windows(
    missions: CoverageMissionAuthority, company_ref: str, *, currency: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """One fundamental window per filing date, and what each role cost.

    ``as_of`` is the filing date rather than the period end, because that is
    the day the market could first have known the figure. Dating a window by
    the quarter it describes is how a "historical" multiple ends up built on a
    filing nobody had yet.
    """

    filings = missions.statement_filings(company_ref)
    filed_dates = sorted({str(row["filed"]) for row in filings})[-MAX_WINDOWS:]
    cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def lines(statement: str, concept: str) -> list[dict[str, Any]]:
        key = (statement, concept)
        if key not in cache:
            cache[key] = list(missions.statement_series_lines(
                company_ref, concept, statement=statement))
        return cache[key]

    windows: list[dict[str, Any]] = []
    digest: list[dict[str, Any]] = []
    notes: dict[str, str] = {}
    for as_of in filed_dates:
        roles: dict[str, Any] = {}
        totals: dict[str, str] = {}
        notes = {}
        for role, candidates in ROLE_CANDIDATES.items():
            refusals: list[str] = []
            for statement, concept in candidates:
                rows = _usable_rows(lines(statement, concept), as_of=as_of)
                if not rows:
                    refusals.append(f"{concept}：截至该申报日没有可引用的合并口径行")
                    continue
                if ROLE_AGGREGATION[role] == "trailing_sum":
                    components, total, why = trailing_year(rows, currency=currency)
                else:
                    components, total, why = latest_instant(rows, currency=currency)
                if components is None:
                    refusals.append(f"{concept}：{why}")
                    continue
                if role in NON_NEGATIVE_ROLES and total is not None and total < 0:
                    # ``capital_expenditure`` is filed as a positive outflow
                    # and subtracted by the formula. A filer that reports it
                    # already negated would double the free cash flow, and the
                    # authority refuses the whole snapshot for it -- so the
                    # role is dropped here with the reason instead.
                    refusals.append(
                        f"{concept}：合计为负 {_plain(total)}，与该科目的符号约定相反")
                    continue
                roles[role] = {
                    "concept": concept,
                    "statement": statement,
                    "unit": currency,
                    "source_ref": FUNDAMENTAL_SOURCE_REF,
                    "components": components,
                }
                totals[role] = _plain(total or Decimal(0))
                break
            else:
                notes[role] = "；".join(refusals) or "没有为这个角色配置候选科目"
        if roles:
            windows.append({"as_of": as_of, "roles": roles})
            digest.append({"as_of": as_of, "roles": dict(sorted(totals.items()))})
    return windows, digest, notes


def input_coverage(
    notes: Mapping[str, str], held: Sequence[str],
) -> list[dict[str, Any]]:
    """One line per role: found, or looked for and not found."""

    rows: list[dict[str, Any]] = []
    for role in sorted(ROLE_CANDIDATES):
        label = ROLE_LABELS.get(role, role)
        if role in held:
            rows.append({
                "input": role, "status": INPUT_COVERAGE_HELD,
                "concept": None,
                "detail": f"{label}：已从申报行取到，见 fundamental_windows",
            })
            continue
        concepts = "、".join(concept for _statement, concept in ROLE_CANDIDATES[role])
        detail = f"{label}：数据源缺失。{notes.get(role) or '没有找到可用的申报行'}"
        rows.append({
            "input": role, "status": INPUT_COVERAGE_MISSING,
            "concept": concepts, "detail": detail[:300],
        })
    return rows


# -- the priced side -------------------------------------------------------


def trading_calendar(
    prices: MarketPriceSeriesAuthority, company_refs: Sequence[str],
) -> list[str]:
    """Every settled trading day the covered universe has a bar for.

    The ledger's own calendar. A company whose feed stopped cannot tell you
    that the market was open yesterday; its four peers can, and they were
    fetched by the same lane on the same schedule.
    """

    dates: set[str] = set()
    for company_ref in company_refs:
        series = prices.series(company_ref)
        if series is None:
            continue
        for bar in series["bars"]:
            if not bar_is_provisional(bar):
                dates.add(str(bar["date"]))
    return sorted(dates)


def build_inputs(
    *,
    missions: CoverageMissionAuthority,
    prices: MarketPriceSeriesAuthority,
    company_ref: str,
    calendar: Sequence[str],
) -> dict[str, Any]:
    """Everything one snapshot needs, or the one sentence saying why not."""

    def refused(reason: str) -> dict[str, Any]:
        return {"company_ref": company_ref, "refusal": reason}

    series = prices.series(company_ref)
    if series is None:
        return refused("这家公司还没有价格序列版本")
    settled = [bar for bar in series["bars"] if not bar_is_provisional(bar)]
    if not settled:
        return refused("价格序列里没有一根已收盘的日线")
    newest = settled[-1]
    lag = sum(1 for day in calendar if day > str(newest["date"]))
    if lag > MAX_PRICE_TRADING_DAY_LAG:
        return refused(
            f"最新已收盘日线是 {newest['date']}，比覆盖范围里的最新交易日落后 "
            f"{lag} 个交易日，超过 {MAX_PRICE_TRADING_DAY_LAG} 个的新鲜度门槛")
    observation = prices.latest_observation(company_ref, "shares_outstanding")
    if observation is None:
        return refused("价格序列里没有 shares_outstanding 观测，市值无从算起")
    currency = str(series["currency"])
    price = {
        "version_ref": series["version_ref"],
        "version_hash": series["version_hash"],
        "bar_date": str(newest["date"]),
        "close": str(newest["close"]),
        "currency": currency,
        "invocation_ref": str(newest["invocation_ref"]),
        "artifact_hash": str(newest["artifact_hash"]),
    }
    shares = {
        "version_ref": observation["version_ref"],
        "version_hash": observation["version_hash"],
        "as_of": str(observation["as_of"]),
        "shares_outstanding": str(observation["value"]),
        "invocation_ref": str(observation["invocation_ref"]),
        "artifact_hash": str(observation["artifact_hash"]),
    }
    windows, digest, notes = build_windows(
        missions, company_ref, currency=currency)
    if not windows:
        return refused(
            "这家公司没有一个申报日能凑出可用的财务角色，估值快照没有分母")
    held = sorted(windows[-1]["roles"])
    coverage = input_coverage(notes, held)
    history = [{"date": str(bar["date"]), "close": str(bar["close"])}
               for bar in settled[-MAX_PRICE_HISTORY_BARS:]]
    return {
        "company_ref": company_ref,
        "refusal": None,
        "price": price,
        "shares": shares,
        "fundamental_windows": windows,
        "price_history": history,
        "input_coverage": coverage,
        "price_lag_trading_days": lag,
        "fingerprint": snapshot_fingerprint(
            price=price, shares=shares, window_digest=digest,
            coverage=coverage, bar_count=len(history)),
    }


def snapshot_fingerprint(
    *,
    price: Mapping[str, Any],
    shares: Mapping[str, Any],
    window_digest: Sequence[Mapping[str, Any]],
    coverage: Sequence[Mapping[str, Any]],
    bar_count: int,
) -> str:
    """What this snapshot would be *of*, computable from both sides.

    Deliberately not the authority's ``binding_hash``: that one is computed
    inside ``publish_snapshot`` from the normalised record, so a caller cannot
    know it without doing the publication. This is the same question asked in
    a form a held record can also answer -- which is what lets the lane stay
    silent instead of spawning a child to be told "duplicate".

    The role *totals* are in it rather than only the role names, so a
    restatement that leaves the periods alone still counts as new.
    """

    return content_hash({
        "schema_version": FINGERPRINT_VERSION,
        "price": {key: price[key] for key in
                  ("version_ref", "version_hash", "bar_date", "close")},
        "shares": {key: shares[key] for key in
                   ("version_ref", "version_hash", "as_of", "shares_outstanding")},
        "windows": [dict(row) for row in window_digest],
        "input_coverage": [dict(row) for row in coverage],
        "price_history_bars": int(bar_count),
    })


def held_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """The same fingerprint, read off a snapshot this Core already published."""

    price = snapshot.get("price") or {}
    shares = snapshot.get("shares") or {}
    digest = [
        {"as_of": window["as_of"],
         "roles": {role: detail["value"]
                   for role, detail in sorted((window.get("roles") or {}).items())}}
        for window in snapshot.get("fundamental_windows") or ()
    ]
    return snapshot_fingerprint(
        price=price, shares=shares, window_digest=digest,
        coverage=snapshot.get("input_coverage") or (),
        bar_count=int((snapshot.get("basis") or {}).get("price_history_bars") or 0),
    )


# -- what the street expects, and why it is not in the snapshot -------------


def latest_consensus_ref(connection: Any, company_ref: str) -> dict[str, Any] | None:
    """The newest consensus version for this company, for the record of the run.

    Read and reported, *not* published into the snapshot. Formula 0.2 has four
    metrics and every one of them is trailing: there is no forward multiple in
    it, so a consensus version is not an input to any figure here and binding
    one in would put a ref on the record that nothing on the record uses. A
    next-twelve-months P/E needs a new metric definition and a consensus
    binding of its own -- a formula version, not a lane change -- and inventing
    one here would be exactly the "hard-compute what the module does not
    support" this system keeps refusing.

    It is still read, because an operator asking "why is there no forward P/E"
    deserves to see that the estimates are in fact there.
    """

    if not _table_exists(connection, "consensus_estimate_versions"):
        return None
    row = connection.execute(
        "SELECT version_id, as_of, source_kind FROM consensus_estimate_versions "
        "WHERE company_ref=? ORDER BY version_number DESC LIMIT 1",
        (company_ref,),
    ).fetchone()
    if row is None:
        return None
    return {"version_ref": row["version_id"], "as_of": row["as_of"],
            "source_kind": row["source_kind"]}


# -- selection -------------------------------------------------------------


def scan(
    store: Any,
    missions: CoverageMissionAuthority,
    prices: MarketPriceSeriesAuthority,
    mission: Mapping[str, Any],
    *,
    company_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Every covered company, and what this lane would do about it now.

    All of them rather than the first, so the coordinator can step past one
    that cannot be priced. The lesson every lane here learnt the same way: one
    company with a broken input must not stand in front of the other four.
    """

    refs = universe(mission)
    if company_ref is not None:
        if company_ref not in refs:
            raise ValuationSnapshotError(
                f"{company_ref} 不在这份 mission 的 universe 里")
        refs = [company_ref]
    calendar = trading_calendar(prices, universe(mission))
    out: list[dict[str, Any]] = []
    for ref in refs:
        inputs = build_inputs(missions=missions, prices=prices,
                              company_ref=ref, calendar=calendar)
        if inputs.get("refusal"):
            out.append({"company_ref": ref, "action": "unavailable",
                        "reason": inputs["refusal"], "inputs": None})
            continue
        held = None
        if _table_exists(store.connection, "valuation_snapshot_versions"):
            try:
                from .valuation_snapshot import latest_snapshot

                held = latest_snapshot(store.connection, ref)
            except Exception:  # noqa: BLE001 - an unreadable snapshot is no snapshot
                held = None
        if held is not None and held_fingerprint(held) == inputs["fingerprint"]:
            out.append({
                "company_ref": ref, "action": "idle", "inputs": inputs,
                "reason": (
                    f"价格、股数、申报窗口与上一份快照（v{held['version']}，"
                    f"{held['as_of']}）的输入指纹相同"),
            })
            continue
        out.append({"company_ref": ref, "action": "publish",
                    "reason": None, "inputs": inputs})
    return out


def pending_companies(
    store: Any,
    missions: CoverageMissionAuthority,
    prices: MarketPriceSeriesAuthority,
    mission: Mapping[str, Any],
    *,
    company_ref: str | None = None,
) -> list[dict[str, Any]]:
    """The companies whose inputs have moved since their last snapshot."""

    return [row for row in scan(store, missions, prices, mission,
                                company_ref=company_ref)
            if row["action"] == "publish"]


# -- the run ---------------------------------------------------------------


def run_valuation_snapshot(
    *,
    state_dir: Path,
    summary_dir: Path,
    company_ref: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "publish",
        "company_ref": company_ref,
        "snapshot_status": None,
        "snapshot_ref": None,
        "snapshot_version": None,
        "fingerprint": None,
        "available_metric_count": 0,
        "missing_inputs": [],
        "window_count": 0,
        "price_version_ref": None,
        "price_bar_date": None,
        "price_lag_trading_days": None,
        "shares_version_ref": None,
        "consensus_version_ref": None,
        "skipped": [],
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        prices = MarketPriceSeriesAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "snapshot_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        try:
            rows = scan(store, missions, prices, mission, company_ref=company_ref)
        except ValuationSnapshotError as exc:
            summary.update({"status": "succeeded",
                            "snapshot_status": f"refused:{exc}"})
            return summary
        summary["skipped"] = [
            {"company_ref": row["company_ref"], "action": row["action"],
             "reason": row["reason"]}
            for row in rows if row["action"] != "publish"
        ]
        pending = [row for row in rows if row["action"] == "publish"]
        if not pending:
            summary.update({"status": "idle",
                            "snapshot_status": "nothing_to_price"})
            return summary
        chosen = pending[0]
        inputs = chosen["inputs"]
        summary.update({
            "company_ref": chosen["company_ref"],
            "fingerprint": inputs["fingerprint"],
            "window_count": len(inputs["fundamental_windows"]),
            "price_version_ref": inputs["price"]["version_ref"],
            "price_bar_date": inputs["price"]["bar_date"],
            "price_lag_trading_days": inputs["price_lag_trading_days"],
            "shares_version_ref": inputs["shares"]["version_ref"],
            "missing_inputs": [row["input"] for row in inputs["input_coverage"]
                               if row["status"] == INPUT_COVERAGE_MISSING],
        })
        consensus = latest_consensus_ref(store.connection, chosen["company_ref"])
        if consensus is not None:
            summary["consensus_version_ref"] = consensus["version_ref"]
        refusal = missing_write_scope(mission)
        if refusal is not None and not dry_run:
            summary.update({"status": "succeeded",
                            "snapshot_status": f"refused:{refusal}"})
            return summary
        if dry_run:
            summary.update({"status": "succeeded", "snapshot_status": "gated",
                            "failure_reason": refusal})
            return summary
        authority = ValuationSnapshotAuthority(store)
        try:
            published = authority.publish_snapshot(
                company_ref=chosen["company_ref"],
                price=inputs["price"],
                shares=inputs["shares"],
                fundamental_windows=inputs["fundamental_windows"],
                price_history=inputs["price_history"],
                input_coverage=inputs["input_coverage"],
                actor_ref=mission["autonomy"]["automation_principal"],
            )
        except ValuationSnapshotError as exc:
            # One company's arithmetic, not the lane's. The run succeeded and
            # the snapshot is simply not there, with the sentence that says so.
            summary.update({
                "status": "succeeded",
                "snapshot_status": f"unavailable:{type(exc).__name__}",
                "failure_reason": str(exc)[:500],
            })
            return summary
        summary.update({
            "status": "succeeded",
            "snapshot_status": ("published" if published["status"] == "fresh"
                                else published["status"]),
            "snapshot_ref": published["id"],
            "snapshot_version": published["version"],
            "available_metric_count": sum(
                1 for item in published["metrics"]
                if item["status"] == "available"),
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--company-ref",
                        help="price this company rather than the next")
    parser.add_argument("--dry-run", action="store_true",
                        help="choose and stop; no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_valuation_snapshot(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        company_ref=args.company_ref, dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "FINGERPRINT_VERSION",
    "MAX_PRICE_HISTORY_BARS",
    "MAX_PRICE_TRADING_DAY_LAG",
    "MAX_WINDOWS",
    "ROLE_CANDIDATES",
    "ROLE_LABELS",
    "WRITE_SCOPE",
    "build_inputs",
    "build_parser",
    "build_windows",
    "held_fingerprint",
    "input_coverage",
    "latest_consensus_ref",
    "latest_instant",
    "main",
    "missing_write_scope",
    "pending_companies",
    "run_valuation_snapshot",
    "scan",
    "snapshot_fingerprint",
    "trading_calendar",
    "trailing_year",
    "universe",
]
