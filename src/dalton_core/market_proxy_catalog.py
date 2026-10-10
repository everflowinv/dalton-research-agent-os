"""F10: which public series stand in for the drivers a company's model declares.

The market-proxy authority (:mod:`market_proxy_claim`) has existed since W5 and
has never produced a row, because its only input was an owner-written mapping
file that both live Cores still hold empty.  Meanwhile every company model
specification on both Cores says, in its own words, "no market proxies are
supplied".  The drivers they declare -- currency translation, a European
revenue mix, financing cost, cloud and AI infrastructure demand, energy and
raw-material cost -- are exactly the ones a traded series can track.

**This module is the missing producer's rule table, and it is a table.**  A
frozen catalog maps cues in a declared driver (its ``ref`` and ``label``, never
the model's free-text ``because``) or a declared cost slot to one ETF that the
already-approved ``yfinance-daily-prices`` connector can read.  Each entry
carries its own ``proxy_gap`` -- how far the series sits from what the company
realises -- because the forecast admission rule refuses a ``market_proxy``
citation without one.

Three properties keep it cheap and stable:

* **Identity does not depend on model prose.**  ``mapping_ref`` is the rule
  and the company, and every field of the mapping comes from the catalog.  A
  respecified model that renames a driver changes nothing here, so a proxy can
  never make the specification it feeds re-run itself in a loop.
* **Sticky.**  A mapping once derived for a company stays derived (the
  authority's own rows are read back), so the set only grows, by at most one
  entry per rule.
* **Bounded.**  At most :data:`MAX_DERIVED_TICKERS` distinct series per
  mission: each is one more child of the one-at-a-time price lane, under the
  same approval and the same pacing as the covered companies.

ETF tickers only: the price authority's ticker rule admits exchange symbols,
not ``=F`` futures or ``=X`` FX pairs, and widening it would be a connector
contract change rather than a mapping.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from .store import content_hash

CATALOG_REF = "market-proxy-catalog:w5:v1"
AUTO_PREFIX = "proxy-map:auto:"
SERIES_PREFIX = "market-proxy-series:yfinance:"
MAX_DERIVED_TICKERS = 6


def _rule(rule_id: str, ticker: str, metric: str, aspect: str, gap: str, *,
          cues: Sequence[str] = (), cost_slots: Sequence[str] = (),
          driver_kinds: Sequence[str] = ()) -> Mapping[str, Any]:
    if len(gap) > 400:
        raise ValueError(f"{rule_id} proxy_gap exceeds the authority's 400 characters")
    return MappingProxyType({
        "rule_id": rule_id, "ticker": ticker, "metric_or_aspect": metric,
        "aspect": aspect, "proxy_gap": gap, "cues": tuple(cues),
        "cost_slots": tuple(cost_slots), "driver_kinds": tuple(driver_kinds),
    })


#: Ordered: a ticker named by an earlier rule wins the per-mission budget.
RULES: tuple[Mapping[str, Any], ...] = (
    _rule(
        "usd_broad", "UUP", "US dollar index (UUP) for currency translation",
        "demand_drivers",
        "UUP tracks US Dollar Index futures (about 58% EUR, 14% JPY, 12% GBP); "
        "the company's own non-USD revenue basket, translation timing and hedges "
        "differ, so it shows the direction of translation, not its size.",
        cues=(r"\bfx\b", "foreign exchange", "currency", "translation", "geographic",
              r"\bgeo\b", "汇率", "外汇")),
    _rule(
        "eur_usd", "FXE", "Euro per US dollar (FXE) for European revenue",
        "demand_drivers",
        "FXE tracks the EUR/USD spot rate through euro deposits; it excludes GBP "
        "and other European currencies and the company's hedging, so it bounds "
        "European translation rather than measuring it.",
        cues=("europe", "emea", r"\beur\b", r"\buk\b", "united kingdom", "欧洲")),
    _rule(
        "us_rates", "IEF", "7-10 year US Treasury price (IEF) for financing cost",
        "supply_and_cost",
        "IEF is a 7-10 year Treasury ETF whose price moves inversely to yields; it "
        "excludes the company's credit spread, maturity and fixed/floating mix, so "
        "it shows the base-rate direction behind financing cost only.",
        cues=("interest", "financing", r"\bdebt\b", "yield", "利息", "融资")),
    _rule(
        "semis_capex", "SMH", "Semiconductor equities (SMH) for AI and cloud infrastructure spend",
        "demand_drivers",
        "SMH is an equity index of chip makers; it prices expectations for AI and "
        "cloud hardware spending and moves with equity multiples, not with this "
        "company's capex, cloud revenue or server shipments.",
        # Not "infrastructure": an IT-services "legacy infrastructure" book
        # is outsourcing run-off, not hardware demand.
        cues=("cloud", "data center", "datacenter", "ai compute", "infra depreciation",
              "server", r"\bgpu", "云", "数据中心", "算力")),
    _rule(
        "energy_cost", "UNG", "US natural gas (UNG) for energy cost",
        "supply_and_cost",
        "UNG holds front-month Henry Hub futures and suffers roll cost; the "
        "company's delivered power or fuel price, contracts and regional mix "
        "differ, so it shows marginal energy-cost direction only.",
        cost_slots=("energy_intensity",),
        cues=("energy", "electricity", "natural gas", "fuel", "能源", "电力")),
    _rule(
        "broad_commodities", "DBC", "Broad commodity futures (DBC) for raw-material cost",
        "supply_and_cost",
        "DBC is a diversified commodity futures basket (energy-heavy); it is not "
        "this company's input mix or contract pricing, so it shows the direction of "
        "raw-material cost pressure, not the realised spread.",
        cost_slots=("raw_material_spread",),
        cues=("raw material", "feedstock", "原料")),
)
RULE_BY_ID = MappingProxyType({rule["rule_id"]: rule for rule in RULES})
CATALOG_HASH = content_hash({
    "catalog_ref": CATALOG_REF,
    "rules": [{key: (list(value) if isinstance(value, tuple) else value)
               for key, value in rule.items()} for rule in RULES],
    "max_derived_tickers": MAX_DERIVED_TICKERS,
})


def _slug(company_ref: str) -> str:
    return re.sub(r"[^a-z0-9.\-]+", "-", company_ref.lower()).strip("-")


def mapping_for(rule_id: str, company_ref: str) -> dict[str, str]:
    """The closed mapping the authority validates, entirely from the catalog."""

    rule = RULE_BY_ID[rule_id]
    return {
        "mapping_ref": f"{AUTO_PREFIX}{rule_id}:{_slug(company_ref)}",
        "source_series_company_ref": SERIES_PREFIX + rule["ticker"],
        "source_ticker": rule["ticker"],
        "target_subject_ref": company_ref,
        "metric_or_aspect": rule["metric_or_aspect"],
        "aspect": rule["aspect"],
        "proxy_gap": rule["proxy_gap"],
    }


def _texts(spec: Mapping[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """``(folded ref + label, driver kind, cost slot)`` for every declared line."""

    found = []
    for key, kind_key in (("revenue_drivers", "kind"), ("expense_lines", None)):
        for line in spec.get(key) or ():
            if not isinstance(line, Mapping):
                continue
            text = " ".join(str(line.get(field) or "") for field in ("ref", "label"))
            text = text.replace("-", " ").replace("_", " ").lower()
            found.append((text, str(line.get(kind_key)) if kind_key else None,
                          line.get("cost_driver_slot") if key == "expense_lines" else None))
    return found


def matched_rules(spec: Mapping[str, Any]) -> list[str]:
    """The catalog rules this specification's declared drivers call for."""

    lines = _texts(spec)
    matched = []
    for rule in RULES:
        hit = False
        for text, kind, slot in lines:
            if slot and slot in rule["cost_slots"]:
                hit = True
            elif rule["driver_kinds"] and kind not in rule["driver_kinds"]:
                continue
            elif any(re.search(cue, text) for cue in rule["cues"]):
                hit = True
            if hit:
                break
        if hit:
            matched.append(rule["rule_id"])
    return matched


def latest_model_specs(connection: Any, company_refs: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Each company's newest accepted model specification, as declared lines."""

    refs = [ref for ref in company_refs if isinstance(ref, str)]
    if not refs:
        return {}
    try:
        rows = connection.execute(
            "SELECT company_ref, revenue_drivers_json, expense_lines_json "
            "FROM coverage_mission_company_model_specs WHERE company_ref IN ("
            + ",".join("?" * len(refs)) + ") ORDER BY created_at, rowid", refs).fetchall()
    except Exception:  # noqa: BLE001 - a Core with no specifications has none
        return {}
    specs: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            specs[row["company_ref"]] = {
                "revenue_drivers": json.loads(row["revenue_drivers_json"] or "[]"),
                "expense_lines": json.loads(row["expense_lines_json"] or "[]"),
            }
        except (TypeError, ValueError):
            continue
    return specs


def recorded_auto_mappings(connection: Any) -> list[tuple[str, str]]:
    """``(rule_id, company_ref)`` for every auto mapping the authority already holds."""

    try:
        rows = connection.execute(
            "SELECT DISTINCT mapping_ref, target_subject_ref FROM market_proxy_claim_versions "
            "WHERE mapping_ref LIKE ?", (AUTO_PREFIX + "%",)).fetchall()
    except Exception:  # noqa: BLE001 - no authority table yet: nothing recorded
        return []
    found = []
    for row in rows:
        rule_id = str(row["mapping_ref"])[len(AUTO_PREFIX):].split(":", 1)[0]
        if rule_id in RULE_BY_ID and mapping_for(rule_id, row["target_subject_ref"])["mapping_ref"] == row["mapping_ref"]:
            found.append((rule_id, row["target_subject_ref"]))
    return found


def derive_mappings(connection: Any, company_refs: Sequence[str]) -> list[dict[str, str]]:
    """Catalog mappings for the covered companies' declared drivers, sticky and bounded."""

    universe = [ref for ref in company_refs if isinstance(ref, str)]
    wanted: list[tuple[str, str]] = []
    for rule_id, company_ref in recorded_auto_mappings(connection):
        if company_ref in universe and (rule_id, company_ref) not in wanted:
            wanted.append((rule_id, company_ref))
    specs = latest_model_specs(connection, universe)
    for company_ref in universe:
        spec = specs.get(company_ref)
        if spec is None:
            continue
        for rule_id in matched_rules(spec):
            if (rule_id, company_ref) not in wanted:
                wanted.append((rule_id, company_ref))
    order = {rule["rule_id"]: index for index, rule in enumerate(RULES)}
    wanted.sort(key=lambda item: (order[item[0]], universe.index(item[1])))
    tickers: list[str] = []
    mappings = []
    for rule_id, company_ref in wanted:
        ticker = RULE_BY_ID[rule_id]["ticker"]
        if ticker not in tickers:
            if len(tickers) >= MAX_DERIVED_TICKERS:
                continue
            tickers.append(ticker)
        mappings.append(mapping_for(rule_id, company_ref))
    return mappings


def merge_mappings(explicit: Sequence[Mapping[str, Any]],
                   derived: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Explicit owner mappings first; a derived one with the same ref yields to it."""

    merged = [dict(item) for item in explicit]
    refs = {item["mapping_ref"] for item in merged}
    tickers = {item["source_series_company_ref"]: item["source_ticker"] for item in merged}
    for item in derived:
        if item["mapping_ref"] in refs:
            continue
        if tickers.get(item["source_series_company_ref"], item["source_ticker"]) != item["source_ticker"]:
            continue
        merged.append(dict(item))
        refs.add(item["mapping_ref"])
    return merged


__all__ = [
    "AUTO_PREFIX", "CATALOG_HASH", "CATALOG_REF", "MAX_DERIVED_TICKERS", "RULES",
    "RULE_BY_ID", "SERIES_PREFIX", "derive_mappings", "latest_model_specs",
    "mapping_for", "matched_rules", "merge_mappings", "recorded_auto_mappings",
]
