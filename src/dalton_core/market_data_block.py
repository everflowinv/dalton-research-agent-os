"""Batch B: the market data block -- price, size, liquidity, ranges, relative moves.

Read-only from the price-series authority's stored versions
(``market_price_series_versions``): no external call, no estimate.  Every
figure says what it was computed from; anything the stored series cannot
answer (short interest, implied volatility) is listed as unavailable with the
reason rather than left out.

* price: the newest bar's close, its date, and whether it had settled;
* market cap: close x the newest ``shares_outstanding`` observation (or the
  vendor's ``market_cap`` observation when no share count is held);
* 30-day ADV: mean daily share volume and mean daily dollar value (close x
  volume) over the last 30 bars -- only when every one of them carries volume;
* 52-week range: the high/low (intraday when held, closes otherwise) over bars
  in the 365 days to the newest bar;
* YTD, 1/3/6-month returns on close (price returns, no dividends);
* relative to the mission basket: the company's return minus the equal-weight
  mean of the other covered companies' returns over the same window.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "market-data-block:batch-b:0.1"
ADV_BARS = 30
HORIZONS = (("1m", 1), ("3m", 3), ("6m", 6))
#: Broad-market benchmarks looked for in the price authority, in order.
BENCHMARK_TICKERS = ("SPY", "IVV", "VOO", "^GSPC", "VTI", "ACWI")
UNAVAILABLE_SOURCES = {
    "short_interest": "没有接入卖空数据源（交易所/FINRA 半月数据未获取）",
    "implied_volatility": "没有接入期权数据源，无法给出隐含波动率",
}


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: Decimal | None, quantum: str | None = None) -> str | None:
    if value is None:
        return None
    if quantum:
        value = value.quantize(Decimal(quantum))
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def latest_series(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    from .financial_period import _refs, _table_exists, query

    if not _table_exists(connection, "market_price_series_versions"):
        return None
    refs = _refs(company_ref)
    rows = query(connection,
                 f"SELECT record_json FROM market_price_series_versions WHERE company_ref IN "
                 f"({','.join('?' * len(refs))}) ORDER BY version_number DESC LIMIT 1", refs)
    return json.loads(rows[0]["record_json"]) if rows else None


def _months_back(moment: date, months: int) -> date:
    index = moment.year * 12 + moment.month - 1 - months
    year, month = index // 12, index % 12 + 1
    import calendar

    return date(year, month, min(moment.day, calendar.monthrange(year, month)[1]))


def _close_on_or_before(bars: Sequence[Mapping[str, Any]], day: date) -> Mapping[str, Any] | None:
    chosen = None
    for bar in bars:
        if bar["date"] <= day.isoformat():
            chosen = bar
        else:
            break
    return chosen


def returns(bars: Sequence[Mapping[str, Any]], as_of: str | None = None) -> dict[str, Any]:
    """Close-to-close price returns for YTD and the 1/3/6-month horizons."""

    usable = [bar for bar in bars if _dec(bar.get("close")) is not None
              and (as_of is None or bar["date"] <= str(as_of)[:10])]
    if not usable:
        return {}
    last = usable[-1]
    end = date.fromisoformat(last["date"])
    out: dict[str, Any] = {}
    for key, months in HORIZONS:
        base = _close_on_or_before(usable, _months_back(end, months))
        out[key] = _ret(base, last)
    base = _close_on_or_before(usable, date(end.year - 1, 12, 31))
    out["ytd"] = _ret(base, last)
    return out


def _ret(base: Mapping[str, Any] | None, last: Mapping[str, Any]) -> dict[str, Any]:
    if base is None:
        return {"status": "unavailable", "percent": None, "since": None,
                "reason": "价格历史不够长"}
    start, finish = _dec(base["close"]), _dec(last["close"])
    if not start:
        return {"status": "unavailable", "percent": None, "since": base["date"], "reason": "起点收盘价无效"}
    return {"status": "available", "percent": _plain((finish / start - 1) * 100, "0.1"),
            "since": base["date"], "reason": None}


def market_block_from(connection: sqlite3.Connection | None, company_ref: str, *,
                      as_of: str | None = None, basket: Sequence[str] = (),
                      cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """The block for one company.  ``cache`` (a dict) shares parsed series
    across calls, so a page building every company's block parses each once."""

    def series_of(ref: str) -> dict[str, Any] | None:
        if cache is None:
            return latest_series(connection, ref)
        if ref not in cache:
            cache[ref] = latest_series(connection, ref)
        return cache[ref]

    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "company_ref": company_ref,
                           "status": "empty", "reason": None}
    if connection is None:
        out["reason"] = "这个研究环境还没有数据库"
        return out
    record = series_of(company_ref)
    if record is None:
        out["reason"] = "还没有行情数据"
        return out
    bars = [bar for bar in record.get("bars") or ()
            if as_of is None or bar["date"] <= str(as_of)[:10]]
    if not bars:
        out["reason"] = "所选日期之前没有行情"
        return out
    from .market_price import bar_is_provisional

    last = bars[-1]
    close = _dec(last["close"])
    out.update({"status": "ok", "ticker": record.get("ticker"), "currency": record.get("currency"),
                "series_version_ref": record.get("id"), "source_ref": record.get("source_ref"),
                "price": {"close": last["close"], "date": last["date"],
                          "provisional": bar_is_provisional(last)},
                "history": {"first_bar": bars[0]["date"], "bars": len(bars)}})
    observations = [row for row in record.get("observations") or ()
                    if as_of is None or str(row.get("as_of")) <= str(as_of)[:10]]
    shares = max((row for row in observations if row.get("observation") == "shares_outstanding"),
                 key=lambda row: row["as_of"], default=None)
    vendor_cap = max((row for row in observations if row.get("observation") == "market_cap"),
                     key=lambda row: row["as_of"], default=None)
    if shares is not None and close is not None:
        cap = close * Decimal(str(shares["value"]))
        out["market_cap"] = {"value": _plain(cap, "1"), "formula": "close × shares_outstanding",
                             "shares": shares["value"], "shares_as_of": shares["as_of"]}
    elif vendor_cap is not None:
        out["market_cap"] = {"value": vendor_cap["value"], "formula": "vendor market_cap observation",
                             "as_of": vendor_cap["as_of"]}
    else:
        out["market_cap"] = {"value": None, "reason": "没有股本或市值观测"}
    window = bars[-ADV_BARS:]
    volumes = [_dec(bar.get("volume")) for bar in window]
    if len(window) < ADV_BARS:
        out["adv_30d"] = {"status": "unavailable", "reason": f"只有 {len(window)} 个交易日"}
    elif any(volume is None for volume in volumes) or all(volume == 0 for volume in volumes):
        out["adv_30d"] = {"status": "unavailable",
                          "reason": "行情序列里没有成交量，无法计算日均成交（不是零成交）"}
    else:
        shares_adv = sum(volumes) / len(volumes)
        dollars = sum(_dec(bar["close"]) * _dec(bar["volume"]) for bar in window) / len(window)
        out["adv_30d"] = {"status": "available", "shares": _plain(shares_adv, "1"),
                          "value": _plain(dollars, "1"), "bars": len(window),
                          "since": window[0]["date"],
                          "formula": "mean(volume), mean(close × volume) over the last 30 bars"}
    end = date.fromisoformat(last["date"])
    year = [bar for bar in bars if bar["date"] > (end - timedelta(days=365)).isoformat()]
    highs = [(_dec(bar.get("high")) or _dec(bar["close"]), bar["date"]) for bar in year]
    lows = [(_dec(bar.get("low")) or _dec(bar["close"]), bar["date"]) for bar in year]
    high, low = max(highs), min(lows)
    out["range_52w"] = {"high": _plain(high[0], "0.01"), "high_date": high[1],
                        "low": _plain(low[0], "0.01"), "low_date": low[1],
                        "basis": "intraday high/low" if year[-1].get("high") else "closes",
                        "from_high_percent": _plain((close / high[0] - 1) * 100, "0.1") if close else None,
                        "from_low_percent": _plain((close / low[0] - 1) * 100, "0.1") if close else None}
    own = returns(bars)
    out["returns"] = own
    peers = [ref for ref in basket if ref != company_ref]
    peer_returns: dict[str, dict[str, Any]] = {}
    for ref in peers:
        series = series_of(ref)
        if series is None:
            continue
        peer_returns[ref] = returns(series.get("bars") or (), last["date"])
    relative: dict[str, Any] = {}
    for key in ("ytd", *(name for name, _ in HORIZONS)):
        mine = own.get(key) or {}
        values = [_dec(item[key]["percent"]) for item in peer_returns.values()
                  if (item.get(key) or {}).get("status") == "available"]
        if mine.get("status") != "available" or not values:
            relative[key] = {"status": "unavailable", "reason": "篮子里没有同期可比收益"}
            continue
        mean = sum(values) / len(values)
        relative[key] = {"status": "available", "basket_percent": _plain(mean, "0.1"),
                         "relative_pp": _plain(_dec(mine["percent"]) - mean, "0.1"),
                         "basket_size": len(values)}
    out["relative_to_basket"] = {"basket": peers, "method": "equal-weight mean of covered peers' price returns",
                                 "windows": relative}
    out["relative_to_benchmark"] = _benchmark(connection, own, last["date"], cache=cache)
    out["unavailable"] = dict(UNAVAILABLE_SOURCES)
    return out


def _benchmark(connection: sqlite3.Connection, own: Mapping[str, Any], as_of: str, *,
               cache: dict[str, Any] | None) -> dict[str, Any]:
    """Relative to SPY (or another broad index) when the price authority holds one."""

    from .financial_period import _table_exists, query

    if not _table_exists(connection, "market_price_series_versions"):
        return {"status": "unavailable", "reason": "没有价格库"}
    try:
        held = {row["ticker"]: row["company_ref"] for row in query(
            connection, "SELECT DISTINCT ticker, company_ref FROM market_price_series_versions")}
    except sqlite3.Error:
        return {"status": "unavailable", "reason": "价格库读不出代码列表"}
    ticker = next((t for t in BENCHMARK_TICKERS if t in held), None)
    if ticker is None:
        proxies = sorted(t for t, ref in held.items() if str(ref).startswith("market-proxy-series:"))
        return {"status": "unavailable",
                "reason": "价格库里没有 SPY 或其他宽基指数序列"
                          + (f"（现有市场代理：{', '.join(proxies)}，都不是宽基）" if proxies else ""),
                "wanted": list(BENCHMARK_TICKERS)}
    ref = held[ticker]
    if cache is not None and ref in cache:
        series = cache[ref]
    else:
        series = latest_series(connection, ref)
        if cache is not None:
            cache[ref] = series
    theirs = returns((series or {}).get("bars") or (), as_of)
    windows = {}
    for key in ("ytd", *(name for name, _ in HORIZONS)):
        mine, bench = own.get(key) or {}, theirs.get(key) or {}
        if mine.get("status") == "available" and bench.get("status") == "available":
            windows[key] = {"status": "available", "benchmark_percent": bench["percent"],
                            "relative_pp": _plain(_dec(mine["percent"]) - _dec(bench["percent"]), "0.1")}
        else:
            windows[key] = {"status": "unavailable", "reason": "基准同期收益不可得"}
    return {"status": "available", "ticker": ticker, "windows": windows}


__all__ = ["SCHEMA_VERSION", "BENCHMARK_TICKERS", "latest_series", "market_block_from", "returns"]
