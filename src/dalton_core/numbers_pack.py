"""Batch B: one numbers pack per company for the material-pack builder.

``numbers_pack(state_dir, company_ref, as_of)`` assembles, read-only and with
no model call:

* ``consensus``  -- ``consensus_table`` (corrected fiscal labels);
* ``street``     -- ``street_estimate_tables.street_table`` (broker house
  estimates per fiscal period, the street numbers brokers quote, target and
  rating changes);
* ``outlook``    -- ``earnings_release_outlook`` (bookings, book-to-bill,
  growth, guidance, each with its quoted sentence);
* ``valuation``  -- ``valuation_forward.forward_block`` (forward P/E three
  ways, EV with total debt, EV/EBITDA, stored snapshot percentiles) and the
  mission-universe ``peers`` table;
* ``market``     -- ``market_data_block`` (price, cap, 30-day ADV, 52-week,
  YTD, 1/3/6-month, relative to the mission basket);
* ``cross_checks`` -- deterministic consistency flags across the blocks (the
  vendor's FY numbers against the brokers' and the company's guidance).

It returns ``{"json": {...}, "text": "..."}``; the text is a compact Chinese
rendering of at most ``MAX_TEXT_CHARS`` characters for a drafting prompt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "numbers-pack:batch-b:0.1"
MAX_TEXT_CHARS = 6000
#: A vendor figure this far from the brokers' median for the same year is flagged.
STREET_DEVIATION_FLAG = Decimal("0.05")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _q(value: Any, quantum: str = "0.01") -> str:
    parsed = _dec(value)
    if parsed is None:
        return "—"
    return format(parsed.quantize(Decimal(quantum)), "f")


def _bn(value: Any) -> str:
    parsed = _dec(value)
    if parsed is None:
        return "—"
    return f"{format((parsed / Decimal(1_000_000_000)).quantize(Decimal('0.01')), 'f')}bn"


def _mission(connection) -> dict[str, Any] | None:
    from .financial_period import _table_exists, query

    if connection is None or not (_table_exists(connection, "coverage_mission_versions")
                                  and _table_exists(connection, "coverage_mission_pointer")):
        return None
    rows = query(connection,
                 "SELECT v.record_json AS record_json FROM coverage_mission_versions v JOIN coverage_mission_pointer p "
                 "ON p.mission_version_id=v.mission_version_id ORDER BY p.updated_at DESC LIMIT 1")
    return json.loads(rows[0]["record_json"]) if rows else None


def _fcf_bridge(connection, state_dir: Path, company_ref: str, *, as_of: str,
                market: Mapping[str, Any]) -> dict[str, Any] | None:
    """System vs company-defined vs ex-one-off FCF (``valuation_fcf_bridge``)."""

    from .earnings_release_outlook import _release_period, exhibit_bytes, intake_reads, plain_text
    from .financial_period import fiscal_year_end
    from .valuation_fcf_bridge import fcf_bridge
    from .valuation_forward import system_fcf

    if connection is None:
        return None
    try:
        fye = fiscal_year_end(connection, company_ref)
        releases = []
        for read in sorted(intake_reads(connection, company_ref), key=lambda r: str(r.get("report_date") or "")):
            if str(read.get("report_date") or "") > as_of:
                continue
            raw, _reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
            if raw is None:
                continue
            releases.append({"text": plain_text(raw), "release_period": _release_period(read),
                             "accession": read.get("accession"), "report_date": read.get("report_date")})
        return fcf_bridge(system=system_fcf(connection, company_ref, as_of=as_of), releases=releases,
                          market_cap=((market or {}).get("market_cap") or {}).get("value"),
                          fye_month=None if fye is None else fye["month"])
    except Exception as exc:  # noqa: BLE001 - one block, not the pack
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}


def cross_checks(consensus: Mapping[str, Any], street: Mapping[str, Any],
                 outlook: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Flags a PM should see before trusting a number: deterministic comparisons only."""

    flags: list[dict[str, Any]] = []
    medians = {(item["period"], item["metric"]): item for item in (street or {}).get("periods") or ()
               if item.get("median")}
    for period in (consensus or {}).get("periods") or ():
        for metric in ("eps", "revenue"):
            cell = period.get(metric) or {}
            mean = _dec(cell.get("mean"))
            if mean is None:
                continue
            if cell.get("year_ago_check") == "mismatch":
                flags.append({"kind": "vendor_year_ago_mismatch", "period": period["label"], "metric": metric,
                              "detail": f"数据商 {period['label']} {metric} 的 year_ago={cell.get('year_ago')} "
                                        "与我们记录的上年实际不符，期间标签存疑"})
            street_row = medians.get((period["label"], metric))
            if street_row:
                median = _dec(street_row["median"])
                if median:
                    gap = mean / median - 1
                    if abs(gap) >= STREET_DEVIATION_FLAG:
                        later = medians.get((f"FY{int(period['label'][2:6]) + 1}", metric))
                        hint = ""
                        if later and _dec(later["median"]) and abs(mean / _dec(later["median"]) - 1) < abs(gap):
                            hint = f"；更接近券商 FY{int(period['label'][2:6]) + 1} 中位数 {later['median']}，疑似数据商把下一年错放到这一年"
                        flags.append({"kind": "vendor_vs_street", "period": period["label"], "metric": metric,
                                      "vendor": cell.get("mean"), "street_median": street_row["median"],
                                      "houses": street_row.get("count"),
                                      "gap": format((gap * 100).quantize(Decimal("0.1")), "f"),
                                      "detail": f"数据商 {period['label']} {metric} 均值 {cell.get('mean')} 与 "
                                                f"{street_row.get('count')} 家券商中位数 {street_row['median']} 相差 "
                                                f"{format((gap * 100).quantize(Decimal('0.1')), 'f')}%{hint}"})
    for item in (outlook or {}).get("items") or ():
        if item.get("kind") != "guidance" or item.get("metric") != "eps" or item.get("unit") != "currency":
            continue
        low, high = _dec(item.get("low")), _dec(item.get("high"))
        period = next((p for p in (consensus or {}).get("periods") or () if p["label"] == item.get("period")), None)
        mean = _dec(((period or {}).get("eps") or {}).get("mean"))
        if low is None or high is None or mean is None:
            continue
        if not (low <= mean <= high):
            flags.append({"kind": "consensus_outside_guidance", "period": item.get("period"), "metric": "eps",
                          "detail": f"一致预期 {item.get('period')} EPS {_q(mean)} 在公司指引 {_q(low)}–{_q(high)} 之外"
                                    "（指引为 GAAP 口径，一致预期可能为调整后口径）"})
    return flags


def numbers_pack(state_dir: Path | str, company_ref: str, as_of: str | None = None, *,
                 parse_unstored: bool = True, include_peers: bool = True,
                 today: str | None = None, core_db: Path | str | None = None) -> dict[str, Any]:
    """The pack: ``{"json": ..., "text": ...}``.

    ``parse_unstored`` parses held broker notes read-only when no estimate
    figures are stored yet (slower; the material-pack builder wants it, a page
    load does not).  ``core_db`` names the Core file when it is not
    ``<state_dir>/core.sqlite``.
    """

    from .consensus_table import consensus_table_from, open_core
    from .earnings_release_outlook import latest_outlook
    from .market_data_block import market_block_from
    from .street_estimate_tables import parsed_unstored, street_table_from
    from .valuation_forward import forward_block, latest_snapshot_view, our_eps, peers_table

    as_of = str(as_of or today or date.today().isoformat())[:10]
    state_dir = Path(state_dir)
    core_path = Path(core_db) if core_db is not None else state_dir / "core.sqlite"
    with open_core(core_path) as connection:
        mission = _mission(connection)
        universe = [row["company_ref"] for row in (mission or {}).get("universe") or ()]
        tickers = {row["company_ref"]: row.get("ticker") for row in (mission or {}).get("universe") or ()}
        figures, changes = (parsed_unstored(connection, state_dir, company_ref)
                            if parse_unstored and connection is not None else (None, []))
        street = street_table_from(connection, company_ref, as_of=as_of, figures=figures)
        if figures is not None:
            street["stored"] = False
            street["changes"] = changes
        consensus = consensus_table_from(connection, company_ref, as_of, state_dir=state_dir, today=as_of,
                                         street=street)
        try:
            outlook = latest_outlook(connection, company_ref, state_dir=state_dir) if connection else None
        except Exception as exc:  # noqa: BLE001 - one block, not the pack
            outlook = {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "items": []}
        if outlook and as_of and str(outlook.get("report_date") or "") > as_of:
            outlook = None
        series_cache: dict[str, Any] = {}
        market = market_block_from(connection, company_ref, as_of=as_of, basket=universe, cache=series_cache)
        try:
            from .cockpit_v2.charts_api import ChartReader

            ours = our_eps(ChartReader(core_path), company_ref)
        except Exception as exc:  # noqa: BLE001
            ours = {"status": "unavailable", "reason": f"读不出预测模型：{type(exc).__name__}"}
        valuation = forward_block(connection, company_ref, as_of=as_of, consensus=consensus, street=street,
                                  market=market, ours=ours,
                                  snapshot=latest_snapshot_view(connection, company_ref))
        valuation["fcf_bridge"] = _fcf_bridge(connection, state_dir, company_ref, as_of=as_of,
                                              market=market)
        try:
            from .market_data_block import latest_series
            from .valuation_history import long_pe

            series = series_cache.get(company_ref) or latest_series(connection, company_ref)
            bars = [bar for bar in (series or {}).get("bars") or () if str(bar["date"]) <= as_of]
            valuation["long_pe"] = long_pe(connection, state_dir, company_ref, bars=bars)
        except Exception as exc:  # noqa: BLE001 - one block, not the pack
            valuation["long_pe"] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
        peers = None
        if include_peers and universe:
            blocks = {company_ref: valuation}
            for ref in universe:
                if ref == company_ref:
                    continue
                peer_street = street_table_from(connection, ref, as_of=as_of)
                peer_consensus = consensus_table_from(connection, ref, as_of, state_dir=None, today=as_of,
                                                      street=peer_street)
                peer_market = market_block_from(connection, ref, as_of=as_of, cache=series_cache)
                blocks[ref] = forward_block(connection, ref, as_of=as_of, consensus=peer_consensus,
                                            street=peer_street, market=peer_market, ours=None,
                                            snapshot=latest_snapshot_view(connection, ref))
            peers = peers_table(blocks, tickers)
    pack = {
        "schema_version": SCHEMA_VERSION, "company_ref": company_ref, "ticker": tickers.get(company_ref),
        "as_of": as_of, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "consensus": consensus, "street": street, "outlook": outlook, "valuation": valuation,
        "peers": peers, "market": market,
        "cross_checks": cross_checks(consensus, street, outlook),
        "provenance": {
            "consensus": "consensus_estimate_versions (+ financial_period relabel)",
            "street": "street_estimate_figures" + (" / parsed read-only from held notes" if street.get("stored") is False else ""),
            "outlook": "earnings_release_outlooks / Exhibit 99.1 via earnings_release_reads",
            "valuation": "valuation_snapshot_versions + XBRL statement lines + market price series",
            "market": "market_price_series_versions",
        },
    }
    return {"json": pack, "text": render_text(pack)}


def render_text(pack: Mapping[str, Any], limit: int = MAX_TEXT_CHARS) -> str:
    """A compact Chinese rendering, every number with its source tag, ≤ ``limit`` chars."""

    lines: list[str] = []
    ticker = pack.get("ticker") or pack.get("company_ref")
    lines.append(f"【数字包】{ticker} 截至 {pack.get('as_of')}")
    market = pack.get("market") or {}
    if market.get("status") == "ok":
        price = market["price"]
        cap = (market.get("market_cap") or {}).get("value")
        adv = market.get("adv_30d") or {}
        rng = market.get("range_52w") or {}
        rets = market.get("returns") or {}
        rel = ((market.get("relative_to_basket") or {}).get("windows") or {})
        lines.append(
            f"行情：收盘 {price['close']}（{price['date']}{'，盘中' if price.get('provisional') else ''}）；市值 {_bn(cap)}；"
            + (f"30日ADV {_q(_dec(adv.get('value')) / 1_000_000 if adv.get('value') else None, '0.1')}m美元/"
               f"{_q(_dec(adv.get('shares')) / 1_000_000 if adv.get('shares') else None, '0.01')}m股；"
               if adv.get("status") == "available" else f"ADV不可得（{adv.get('reason')}）；")
            + f"52周 {rng.get('low')}–{rng.get('high')}（距高点 {rng.get('from_high_percent')}%）")
        parts = []
        for key, name in (("ytd", "YTD"), ("1m", "1月"), ("3m", "3月"), ("6m", "6月")):
            r = rets.get(key) or {}
            rr = rel.get(key) or {}
            if r.get("status") == "available":
                parts.append(f"{name} {r['percent']}%" + (f"（相对篮子 {rr['relative_pp']}pp）"
                                                          if rr.get("status") == "available" else ""))
        bench = market.get("relative_to_benchmark") or {}
        bench_text = (f"相对 {bench['ticker']} YTD {((bench.get('windows') or {}).get('ytd') or {}).get('relative_pp')}pp"
                      if bench.get("status") == "available" else f"宽基基准不可得（{bench.get('reason')}）")
        lines.append("涨跌：" + "；".join(parts) + f"。{bench_text}。卖空/隐含波动率：无数据源")
    consensus = pack.get("consensus") or {}
    if consensus.get("status") == "ok":
        cal = consensus.get("calendar") or {}
        relabel = consensus.get("relabel") or {}
        lines.append(f"一致预期（Yahoo，{(consensus.get('version') or {}).get('as_of')}；财年止 {cal.get('fiscal_year_end')}，"
                     f"已报告至 {cal.get('last_reported_period_end')}"
                     + ("，由8-K推进" if cal.get("advanced_by_release") else "")
                     + (f"；标签更正 {', '.join(relabel.get('changed_labels') or [])}" if relabel.get("changed_labels") else "")
                     + "）：")
        for period in consensus.get("periods") or ():
            eps, rev = period.get("eps") or {}, period.get("revenue") or {}
            if period.get("status") != "available":
                lines.append(f"  {period['slot']} {period['label']}：无（{period.get('reason')}）")
                continue
            lines.append(f"  {period['slot']} {period['label']}：EPS {_q(eps.get('mean'))}（{_q(eps.get('low'))}–{_q(eps.get('high'))}，"
                         f"{eps.get('count')}家）；收入 {_bn(rev.get('mean'))}（{_bn(rev.get('low'))}–{_bn(rev.get('high'))}，{rev.get('count')}家）")
        for quarter in consensus.get("quarters") or ():
            eps, rev = quarter.get("eps") or {}, quarter.get("revenue") or {}
            lines.append(f"  {quarter['label']}：EPS {_q(eps.get('mean'))}（{eps.get('count')}家）；收入 {_bn(rev.get('mean'))}")
        target = consensus.get("target") or {}
        ratings = consensus.get("ratings") or {}
        report = consensus.get("report_consensus") or {}
        lines.append(f"  目标价 均值 {_q(target.get('mean'))}/中位 {target.get('median')}/高 {target.get('high')}/低 {target.get('low')}（{target.get('count')}家）；"
                     f"评级 强买{ratings.get('strong_buy')}/买{ratings.get('buy')}/持有{ratings.get('hold')}/卖{ratings.get('sell')}"
                     + (f"；券商研报目标价区间 {report.get('low')}–{report.get('high')}（{report.get('broker_count')}家，{report.get('as_of')}）" if report else ""))
        if consensus.get("pre_report"):
            lines.append("  已公布期间的会前一致预期：" + "；".join(
                f"{item['label']} {item['metric']} {_q(item['mean']) if item['metric'] == 'eps' else _bn(item['mean'])}"
                for item in consensus["pre_report"]))
    street = pack.get("street") or {}
    if street.get("periods"):
        lines.append(f"券商研报预测（{', '.join(street.get('houses') or [])}）：")
        for item in street["periods"]:
            if item["period_kind"] != "fiscal_year" or not item.get("count"):
                continue
            fmt = _q if item["metric"] == "eps" else _bn
            houses = "，".join(f"{h['house']} {fmt(h['value'])}" for h in item["houses"])
            quoted = "，".join(f"{h['house']}引 {fmt(h['value'])}" for h in item.get("street_quoted") or ())
            lines.append(f"  {item['period']} {item['metric']}：中位 {fmt(item['median'])}（{houses}）" + (f"；{quoted}" if quoted else ""))
        changes = street.get("changes") or []
        if changes:
            lines.append("  评级/目标价变动：" + "；".join(
                f"{c.get('house')} {c.get('published_on')} {c['kind']} {c.get('from') or ''}→{c.get('to')}" for c in changes[:6]))
    outlook = pack.get("outlook") or {}
    if outlook.get("items"):
        lines.append(f"业绩稿（{outlook.get('release_period')}，{outlook.get('report_date')}，8-K {outlook.get('accession')}）：")
        picks = []
        for item in outlook["items"]:
            if item["kind"] in {"bookings", "book_to_bill", "growth", "guidance", "guidance_vs_actual"} and ":" not in item["metric"]:
                if item.get("low") is not None:
                    value = f"{item['low']}–{item['high']}"
                else:
                    value = str(item.get("value"))
                unit = {"percent": "%", "billion": "bn", "million": "m"}.get(item.get("unit") if item.get("unit") == "percent" else item.get("scale"), "")
                extra = f"（{item['position']}）" if item.get("position") else (f"（{item['qualifier']}）" if item.get("qualifier") else "")
                picks.append(f"{item.get('period')} {item['metric']} {value}{unit}{extra}")
        lines.append("  " + "；".join(picks))
    valuation = pack.get("valuation") or {}
    if valuation:
        fwd = "；".join(
            f"{row['label']} NTM EPS {row.get('value') or '—'} → {row.get('forward_pe') or '—'}x"
            + (f"（{row.get('method')}）" if row.get("method") else f"（{row.get('reason')}）")
            for row in valuation.get("forward_pe") or ())
        lines.append(f"估值：前瞻P/E — {fwd}")
        debt = valuation.get("total_debt") or {}
        ev = valuation.get("enterprise_value") or {}
        ebitda = valuation.get("ebitda_ttm") or {}
        lines.append(f"  EV {_bn(ev.get('value'))}（总债务 {_bn(debt.get('value'))} {debt.get('basis') or debt.get('reason')}，"
                     f"现金 {_bn((valuation.get('cash') or {}).get('value'))}）；EBITDA TTM {_bn(ebitda.get('value'))}"
                     f"（至 {ebitda.get('through')}）；EV/EBITDA {valuation.get('ev_to_ebitda') or '—'}x；EV/Sales {valuation.get('ev_to_sales') or '—'}x")
        bridge = valuation.get("fcf_bridge") or {}
        if bridge.get("columns"):
            lines.append("  FCF 口径桥：" + "；".join(
                f"{col['label'].split('（')[0]} {_bn(col.get('value'))}"
                + (f"（至 {col['through']}）" if col.get("through") else "")
                + (f"（收益率 {col['yield_percent']}%）" if col.get("yield_percent") else f"（{col.get('reason') or col.get('note') or '—'}）")
                for col in bridge["columns"]))
        snapshot = valuation.get("snapshot") or {}
        snap = snapshot.get("metrics") or []
        if snap:
            lines.append(f"  历史分位（P11c 快照，{snapshot.get('fundamental_windows') or '—'} 个财务窗口，"
                         f"股数口径 {snapshot.get('shares_basis') or '—'}）：" + "；".join(
                f"{m['metric']} {m.get('value') or '—'}（第{m.get('percentile') or '—'}百分位，"
                f"{(m.get('reading') or {}).get('label') or '—'}：{(m.get('reading') or {}).get('detail') or ''}）"
                for m in snap if m.get("status") == "available"))
        long_pe = valuation.get("long_pe") or {}
        if long_pe.get("status") == "available":
            reading = long_pe.get("reading") or {}
            lines.append(f"  长样本 P/E（GAAP 稀释 EPS，XBRL company-facts）：当前 {long_pe['current']}x，"
                         f"第{long_pe['percentile']}百分位（{reading.get('label')}：{reading.get('detail')}），"
                         f"中位 {long_pe.get('median')}x，{long_pe.get('first_date')}–{long_pe.get('last_date')}")
    peers = pack.get("peers") or {}
    if peers.get("rows"):
        lines.append("同业（任务篮子）：" + "；".join(
            f"{row.get('ticker')} P/E {row.get('trailing_pe') and _q(row['trailing_pe'], '0.1') or '—'}"
            f"/前瞻 {row.get('forward_pe_consensus') or '—'}/EV/EBITDA {row.get('ev_to_ebitda') or '—'}"
            for row in peers["rows"]) + f"；中位 前瞻P/E {peers['median'].get('forward_pe_consensus') or '—'}")
    for flag in (pack.get("cross_checks") or [])[:6]:
        lines.append(f"⚠ {flag['detail']}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit - 20].rsplit("\n", 1)[0] + "\n…（已截断）"
    return text


def display_block(connection, company_ref: str, *, as_of: str,
                  basket: Sequence[str] = (), cache: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The cockpit's compact 数字 block: stored records only, no spool reads, no peers.

    None when the company has neither consensus nor prices.
    """

    from .consensus_table import consensus_table_from
    from .earnings_release_outlook import latest_outlook
    from .market_data_block import market_block_from
    from .street_estimate_tables import street_table_from
    from .valuation_forward import forward_block

    street = street_table_from(connection, company_ref, as_of=as_of)
    consensus = consensus_table_from(connection, company_ref, as_of, state_dir=None, today=as_of,
                                     street=street)
    market = market_block_from(connection, company_ref, as_of=as_of, basket=basket, cache=cache)
    if consensus.get("status") != "ok" and market.get("status") != "ok":
        return None
    outlook = latest_outlook(connection, company_ref, state_dir=None)
    valuation = forward_block(connection, company_ref, as_of=as_of, consensus=consensus, street=street,
                              market=market, ours=None)
    first = next((p for p in consensus.get("periods") or () if p.get("status") == "available"), None)
    forward = {row["source"]: row for row in valuation.get("forward_pe") or ()}
    rets = market.get("returns") or {}
    rel = ((market.get("relative_to_basket") or {}).get("windows") or {})
    return {
        "as_of": as_of,
        "consensus": None if consensus.get("status") != "ok" else {
            "fiscal_year": None if first is None else first["label"],
            "eps_mean": None if first is None else (first.get("eps") or {}).get("mean"),
            "eps_count": None if first is None else (first.get("eps") or {}).get("count"),
            "revenue_mean": None if first is None else (first.get("revenue") or {}).get("mean"),
            "target_mean": (consensus.get("target") or {}).get("mean"),
            "target_high": (consensus.get("target") or {}).get("high"),
            "target_low": (consensus.get("target") or {}).get("low"),
            "ratings": consensus.get("ratings"),
            "vendor_as_of": (consensus.get("staleness") or {}).get("vendor_as_of"),
            "stale": (consensus.get("staleness") or {}).get("stale"),
            "last_reported_period_end": (consensus.get("calendar") or {}).get("last_reported_period_end"),
            "relabelled": (consensus.get("relabel") or {}).get("changed_labels") or [],
            "label_note": ("财期已按 8-K 业绩稿更正" if (consensus.get("calendar") or {}).get("advanced_by_release")
                           else None),
        },
        "street_houses": street.get("houses") or [],
        "valuation": {
            "forward_pe_consensus": (forward.get("consensus_vendor") or {}).get("forward_pe"),
            "forward_pe_street": (forward.get("street_median") or {}).get("forward_pe"),
            "enterprise_value": (valuation.get("enterprise_value") or {}).get("value"),
            "ev_to_ebitda": valuation.get("ev_to_ebitda"),
            "total_debt_basis": (valuation.get("total_debt") or {}).get("basis"),
        },
        "market": None if market.get("status") != "ok" else {
            "adv_30d_value": (market.get("adv_30d") or {}).get("value"),
            "adv_note": (market.get("adv_30d") or {}).get("reason"),
            "range_52w": market.get("range_52w"),
            "ytd_percent": (rets.get("ytd") or {}).get("percent"),
            "ytd_relative_pp": (rel.get("ytd") or {}).get("relative_pp"),
        },
        "guidance": [
            {"period": item.get("period"), "metric": item.get("metric"), "low": item.get("low"),
             "high": item.get("high"), "value": item.get("value"), "unit": item.get("unit"),
             "scale": item.get("scale")}
            for item in (outlook or {}).get("items") or () if item.get("kind") == "guidance"][:8],
        "flags": [flag["detail"] for flag in cross_checks(consensus, street, outlook)][:4],
    }


__all__ = ["MAX_TEXT_CHARS", "SCHEMA_VERSION", "cross_checks", "display_block", "numbers_pack",
           "render_text"]
