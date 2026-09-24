"""W7: what this mission's companies are called, decided by this mission.

``document_subject.COMPANY_NAMES`` is five rows -- ACN, CTSH, EPAM, IBM, DXC --
and until now it was the only answer to "which words in a document mean this
company".  That was tolerable while one environment covered one industry.  With
a second environment it became a bug with a misleading error message: a
hyperscaler workspace's feed lanes refused every tick with ``MSFT has no
company names to match a subject line against``, which sounds like a fact about
the research and is in fact a missing row in a table the mission has no way to
edit.

The subject of a document belongs to the mission that is reading it, so the
table belongs to the mission too.  Three places can supply a name, in this
order, and the order is the order of authority:

1. **the mission's own feed discovery plan.**  Its ``companies[ref].names``
   carries what each covered issuer is called.  It is written once when the
   mission is published and it travels with the plan, so a lane reading a
   document never has to reach for a shared dict at all.
2. **the mission universe member**, when it carries a ``name``.  Note that
   ``coverage_mission`` validates universe members as a closed shape
   (``company_ref/ticker/coverage_tier/bootstrap_priority``), so a published
   mission cannot carry one today; the branch stays for records built in
   memory, and the owner's way to name a company is (4).
3. **the packaged fallbacks.**  ``COMPANY_NAMES`` for the legacy Core, and the
   SEC company resolver for everything else -- the resolver is already called
   once per company when a first mission is published, and it returns the
   issuer's registered name alongside its CIK.  That name was being thrown
   away; now it is kept.

A ticker with no name anywhere still fails, and deliberately.  A subject line
almost never says the ticker, so a company known only as "MSFT" would be
silently attributed nothing for the whole run -- the failure this refusal
exists to make visible.  What changed is where the fix goes: the mission's feed
plan, which the owner's own workspace owns, rather than a dict in this package.

2026-09-24: sources (1)-(3) no longer *replace* the packaged names, they come
first and the packaged ``COMPANY_NAMES`` are added after them.  A workspace
whose plan said "Amazon.com" could otherwise never learn "AWS" or "亚马逊"
from a deploy, and there was no sanctioned way to edit its plan.  And a fourth
source closes the loop:

4. **the owner's alias ledger** (``company_aliases``): append-only, one
   audited revision per change, kept in the environment's own state
   directory.  ``load_feed_discovery_plan`` attaches its current overlay to
   the plan as ``company_aliases``; names it adds are appended here, names it
   retires are removed, including packaged ones.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .document_subject import COMPANY_NAMES, MIN_TICKER_CHARS


class MissionCompanyNamesError(RuntimeError):
    """A company in this mission has no name a document could call it by."""


def _clean(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        return ()
    seen: dict[str, None] = {}
    for value in values:
        text = str(value).strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def names_from_plan(plan: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """Company ref to names, as the feed discovery plan carries them.

    Keyed by ``company_ref`` because that is what the plan is keyed by; the
    caller joins it to tickers through the universe, which is the one place
    that mapping is authoritative.
    """

    if not isinstance(plan, Mapping):
        return {}
    companies = plan.get("companies")
    if not isinstance(companies, Mapping):
        return {}
    table: dict[str, tuple[str, ...]] = {}
    for company_ref, entry in companies.items():
        if not isinstance(entry, Mapping):
            continue
        names = _clean(entry.get("names"))
        if names:
            table[str(company_ref)] = names
    return table


def mission_name_table(
    universe: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any] | None = None,
    *,
    extra: Mapping[str, Sequence[str]] | None = None,
    union_packaged: bool = True,
) -> dict[str, tuple[str, ...]]:
    """The ticker-to-names table one mission's lanes run on.

    Returns every covered ticker, including the ones whose only name is the
    ticker itself: a caller that wants to refuse those asks
    :func:`unnamed_tickers`, so the refusal is one explicit decision rather
    than an empty dict entry nobody notices.

    ``union_packaged=False`` is the pre-2026-09-24 precedence (packaged names
    only when the mission supplied none).  Only the plan *generator* wants it,
    so a generated plan records what the owner said rather than a copy of the
    package, which every lane adds at run time anyway.
    """

    by_ref = names_from_plan(plan)
    overlay = alias_overlay(plan)
    table: dict[str, tuple[str, ...]] = {}
    for item in universe or ():
        if not isinstance(item, Mapping):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        company_ref = str(item.get("company_ref") or "")
        owned = (_clean(item.get("name") or item.get("names"))
                 or by_ref.get(company_ref, ())
                 or _clean((extra or {}).get(ticker)))
        retired = {name.casefold() for name in overlay["retired"].get(ticker, ())}
        packaged = COMPANY_NAMES.get(ticker, ()) if union_packaged or not owned else ()
        names = _union(owned, packaged, overlay["added"].get(ticker, ()))
        names = tuple(name for name in names if name.casefold() not in retired
                      or name.upper() == ticker)
        # The ticker last, however it was spelled, so a label leads with a name.
        others = tuple(name for name in names if name.upper() != ticker)
        if len(others) != len(names) or len(ticker) >= MIN_TICKER_CHARS:
            names = others + (ticker,)
        table[ticker] = names
    return table


def _union(*groups: Sequence[str]) -> tuple[str, ...]:
    """Names in first-seen order, one per spelling regardless of case."""

    seen: dict[str, str] = {}
    for group in groups:
        for name in _clean(group):
            seen.setdefault(name.casefold(), name)
    return tuple(seen.values())


def alias_overlay(plan: Mapping[str, Any] | None) -> dict[str, dict[str, tuple[str, ...]]]:
    """The owner's alias ledger as the plan carries it: ``{added, retired}`` by ticker."""

    empty: dict[str, dict[str, tuple[str, ...]]] = {"added": {}, "retired": {}}
    value = plan.get("company_aliases") if isinstance(plan, Mapping) else None
    if not isinstance(value, Mapping):
        return empty
    out: dict[str, dict[str, tuple[str, ...]]] = {"added": {}, "retired": {}}
    for key in ("added", "retired"):
        entries = value.get(key)
        if isinstance(entries, Mapping):
            for ticker, names in entries.items():
                cleaned = _clean(names)
                if cleaned:
                    out[key][str(ticker).strip().upper()] = cleaned
    return out


def unnamed_tickers(table: Mapping[str, Sequence[str]]) -> list[str]:
    """Covered tickers whose only name is the ticker.

    These are the ones a document will never name: a sell-side subject line
    says "Microsoft", not "MSFT". A lane that ran on them would attribute
    nothing all week and report no fault, which is the one outcome worth
    refusing over.
    """

    unnamed: list[str] = []
    for ticker, names in table.items():
        if str(ticker).startswith("industry:"):
            continue
        others = [str(name) for name in names
                  if str(name).strip().upper() != str(ticker).strip().upper()]
        if not others:
            unnamed.append(str(ticker))
    return sorted(unnamed)


def resolve_universe_names(
    universe: Sequence[Mapping[str, Any]],
    *,
    resolve: Any = None,
) -> dict[str, tuple[str, ...]]:
    """Ask the issuer registry for the names this mission did not supply.

    ``resolve`` is ``ticker -> {"name": ...}``; the SEC company resolver
    already has that shape and is already called once per company when a first
    mission is published.  A lookup that fails is not an error here: it means
    this company's name has to be written into the feed plan by hand, and the
    audit says so rather than this function raising in the middle of a publish.
    """

    if resolve is None:
        return {}
    found: dict[str, tuple[str, ...]] = {}
    for ticker in unnamed_tickers(mission_name_table(universe, union_packaged=False)):
        try:
            answer = resolve(ticker)
        except Exception:  # noqa: BLE001 - a missing name is reported, not raised
            continue
        name = ""
        if isinstance(answer, Mapping):
            name = str(answer.get("name") or "").strip()
        elif isinstance(answer, str):
            name = answer.strip()
        if name:
            found[ticker] = (name,)
    return found


def require_named(table: Mapping[str, Sequence[str]], *, where: str) -> None:
    """Refuse a run whose companies no document could name, saying where to fix it."""

    missing = unnamed_tickers(table)
    if missing:
        raise MissionCompanyNamesError(
            ", ".join(missing)
            + " has no company names to match a subject line against; add them to "
            + where
        )


__all__ = [
    "MissionCompanyNamesError",
    "alias_overlay",
    "mission_name_table",
    "names_from_plan",
    "require_named",
    "resolve_universe_names",
    "unnamed_tickers",
]
