"""P14a: what each connector can actually give you, in one deterministic table.

The brain keeps being asked to decide where to look, and until now it had no
way to know.  ``connector_inventory`` says a connector exists, what transport
it uses and which operations it exposes; the mission's source plan says whether
it is connected.  Neither says the thing a research decision needs: *what kind
of content comes out of it, and how much should I believe it*.  So a model
deciding "go find out whether the Q4 pipeline commentary changed" had to guess
that AlphaEngine holds sell-side reports and that Guidepoint holds expert
calls, and a guess about where evidence lives is how a research task ends up
querying the one source that could never have answered it.

This is a projection, not an authority: every field is derived from things
already frozen elsewhere (the inventory's profile definitions, the governed
quota table, the mission's source plan, the tracking policy's cadences) plus
one explicit table below that says which content kinds each slug yields.  It
is content-hashed so that a prompt built from it can name the exact table it
was built from, and it stores nothing.

The content-kind table lives here because ``connector_inventory`` is a shared
file several agents are appending to this week; the field belongs in
``PROFILE_DEFINITIONS`` and should move there once the S-line connectors have
landed (see the report).  Slugs that are not in the inventory yet -- the
sales-note, wiki, X and employee-review connectors on the S1/S3 branches --
are declared here anyway and marked ``in_inventory: false``, because a map
that only knows what is already merged cannot be used to plan for what is
arriving.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from .connector_inventory import PROFILE_DEFINITIONS
from .connector_quota_policy import governed_daily_quotas
from .research_event import EVIDENCE_TIERS
from .store import content_hash

SCHEMA_VERSION = "0.1"

# What a source can hand over.  Closed: a prompt renders these words and a
# research decision names them, so an unrecognised one is a refusal rather
# than a shrug.
CONTENT_KINDS: tuple[str, ...] = (
    "sell_side_report",
    "sell_side_comment",
    "transcript",
    "management_minutes",
    "expert_excerpt",
    "sales_note",
    "crowd_post",
    "employee_review",
    "news",
    "filing",
    "financial_statement",
    "price",
    "consensus",
    "calendar",
    "web_page",
)


class SourceCapabilityError(ValueError):
    """The map was asked about something it does not describe."""


# slug -> what it yields, how much to believe it, which markets it covers.
#
# ``generic`` marks a source that will return something about anything: web
# search and web fetch answer every question badly rather than one question
# well, so a research decision that names them has not actually chosen a
# source.  Naming that in the table is what lets the judgement prompt say
# "prefer a specific source; these two are the fallback".
CAPABILITIES: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "sec": MappingProxyType({
        "content_kinds": ("filing", "financial_statement"),
        "evidence_tier": "primary_filing", "markets": ("US",), "generic": False,
        "note": "filings index and XBRL company facts; the numeric authority",
    }),
    "sec-financials": MappingProxyType({
        "content_kinds": ("financial_statement",),
        "evidence_tier": "primary_filing", "markets": ("US",), "generic": False,
        "note": "statement structure, line by line, from the filed statements",
    }),
    "cninfo": MappingProxyType({
        "content_kinds": ("filing",), "evidence_tier": "primary_filing",
        "markets": ("CN",), "generic": False,
        "note": "mainland China announcements; no US coverage",
    }),
    "alphaengine": MappingProxyType({
        "content_kinds": (
            "sell_side_report", "sell_side_comment", "transcript",
            "management_minutes", "news",
        ),
        "evidence_tier": "sell_side", "markets": ("US", "HK", "CN"), "generic": False,
        "note": "the broker library and the call minutes; the densest single source, "
                "and the one with the hardest daily ceiling",
    }),
    "roic-transcript": MappingProxyType({
        "content_kinds": ("transcript",), "evidence_tier": "management_direct",
        "markets": ("US",), "generic": False,
        "note": "whole-site 403 as of 2026-09; declared, not usable",
    }),
    "guidepoint": MappingProxyType({
        "content_kinds": ("expert_excerpt",), "evidence_tier": "expert_network",
        "markets": ("US", "EU", "CN"), "generic": False,
        "note": "expert call library; verbatim quotation limited to 20 words by contract",
    }),
    "yfinance": MappingProxyType({
        "content_kinds": ("price", "consensus", "calendar"),
        "evidence_tier": "market_price", "markets": ("US",), "generic": False,
        "note": "daily bars, share count, market cap; analyst fields feed consensus",
    }),
    "xueqiu": MappingProxyType({
        "content_kinds": ("crowd_post", "price"), "evidence_tier": "crowd",
        "markets": ("CN", "HK", "US"), "generic": False,
        "note": "retail sentiment; never a first-hand source for a Claim",
    }),
    "x-xreach": MappingProxyType({
        "content_kinds": ("crowd_post", "news"), "evidence_tier": "crowd",
        "markets": ("US", "global"), "generic": False,
        "note": "timeline enumeration; the owner reads this for the street's mood, "
                "so it is a market-view source and not a fact source",
    }),
    "x-x-search": MappingProxyType({
        "content_kinds": ("crowd_post",), "evidence_tier": "crowd",
        "markets": ("US", "global"), "generic": False,
        "note": "declared and deliberately not adopted (survey v1.0)",
    }),
    "reddit-last30days": MappingProxyType({
        "content_kinds": ("crowd_post",), "evidence_tier": "crowd",
        "markets": ("US",), "generic": False, "note": "upstream is dead; declared only",
    }),
    "gemini-web-search": MappingProxyType({
        "content_kinds": ("news", "web_page"), "evidence_tier": "news_media",
        "markets": ("global",), "generic": True,
        "note": "ten results a call; the verification path behind a crowd post",
    }),
    "web-fetch": MappingProxyType({
        "content_kinds": ("web_page", "news"), "evidence_tier": "news_media",
        "markets": ("global",), "generic": True,
        "note": "one profile per host; retrieves what search named",
    }),
    # Not in the inventory yet: the S1 / S3 connectors.  Declared here so the
    # cadence policy and the judgement prompt can name them before they merge.
    # C1's catalyst calendar is a source in the cadence sense -- something we
    # look at on a schedule -- even though its content is derived from the
    # price and filing connectors rather than fetched from a vendor.
    "catalyst-calendar": MappingProxyType({
        "content_kinds": ("calendar",), "evidence_tier": "derived",
        "markets": ("US",), "generic": False,
        "note": "earnings, guidance, investor days and filing due dates; only a "
                "confirmed date drives a preview",
    }),
    "sales-notes": MappingProxyType({
        "content_kinds": ("sales_note", "news"), "evidence_tier": "vendor_note",
        "markets": ("US", "global"), "generic": False,
        "note": "the desk's own morning notes; the owner's first read on what the "
                "street is arguing about, ahead of the published reports",
    }),
    "company-wiki": MappingProxyType({
        "content_kinds": ("management_minutes", "expert_excerpt", "sell_side_comment"),
        "evidence_tier": "internal_wiki", "markets": ("US", "CN"), "generic": False,
        "note": "the fund's own file: management meetings, expert notes, broker notes",
    }),
    "employee-reviews": MappingProxyType({
        "content_kinds": ("employee_review",), "evidence_tier": "crowd",
        "markets": ("US",), "generic": False,
        "note": "hiring and morale as a lagging read on delivery capacity",
    }),
})

# Which mission source-plan ref each slug answers to.  The plan speaks in
# ``source:`` refs and two of them do not match the connector's own source ref
# (``source:web-search`` is served by the Gemini connector, whose source ref is
# ``source:public-web``), which is exactly the mapping DISCOVERY_SOURCES keeps
# for discovery and which nothing kept for anything else.
SOURCE_PLAN_ALIASES: Mapping[str, str] = MappingProxyType({
    "source:web-search": "source:public-web",
})

# Slugs that have no inventory profile yet.  Derived, so it cannot go stale.
def _inventory() -> dict[str, Mapping[str, Any]]:
    return {definition["slug"]: definition for definition in PROFILE_DEFINITIONS}


def _quotas() -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in governed_daily_quotas():
        grouped.setdefault(entry["connector_slug"], []).append({
            "operation": entry["operation"],
            "quota_unit": entry["quota_unit"],
            "daily_unit_limit": entry["daily_unit_limit"],
        })
    return grouped


def _completeness(definition: Mapping[str, Any] | None) -> str | None:
    """The weakest completeness any of a connector's operations promises.

    Weakest rather than best: a connector one of whose operations only returns
    a sample cannot be relied on for "all of them", and a map that reported the
    best ceiling would let a research plan assume enumeration it will not get.
    """

    if definition is None:
        return None
    order = ("sampled", "bounded", "enumerated")
    seen = [op.get("completeness") for op in definition["operations"]]
    known = [value for value in seen if value in order]
    if not known:
        return None
    return min(known, key=order.index)


def source_ref_for(slug: str) -> str | None:
    definition = _inventory().get(slug)
    if definition is not None:
        return definition["source_ref"]
    return f"source:{slug}"


def capability(slug: str) -> dict[str, Any]:
    """One source's entry, or a refusal naming the slug that was asked for."""

    if slug not in CAPABILITIES:
        raise SourceCapabilityError(
            f"{slug!r} is not a source this map describes; "
            f"known slugs: {sorted(CAPABILITIES)}"
        )
    definition = _inventory().get(slug)
    entry = CAPABILITIES[slug]
    if entry["evidence_tier"] not in EVIDENCE_TIERS:
        raise SourceCapabilityError(f"{slug}: evidence tier is not in the frozen vocabulary")
    return {
        "slug": slug,
        "source_ref": source_ref_for(slug),
        "in_inventory": definition is not None,
        "source_type": None if definition is None else definition["source_type"],
        "transport": None if definition is None else definition["transport"],
        "operations": () if definition is None else tuple(
            op["name"] for op in definition["operations"]
        ),
        "content_kinds": tuple(entry["content_kinds"]),
        "evidence_tier": entry["evidence_tier"],
        "markets": tuple(entry["markets"]),
        "generic": bool(entry["generic"]),
        "completeness_ceiling": _completeness(definition),
        "quotas": tuple(
            tuple(sorted(row.items())) for row in _quotas().get(slug, ())
        ),
        "note": entry["note"],
    }


def sources_for(content_kind: str, *, connected_only: Sequence[str] | None = None) -> list[str]:
    """Every slug that yields this content kind, specific sources first.

    Ordering matters to the caller that uses it: a research decision that has
    to pick one source should be handed the specific ones before the two that
    answer everything, because a generic source is what you use when nothing
    specific holds the answer, not what you reach for first.
    """

    if content_kind not in CONTENT_KINDS:
        raise SourceCapabilityError(
            f"{content_kind!r} is not a content kind; known: {list(CONTENT_KINDS)}"
        )
    allowed = None if connected_only is None else set(connected_only)
    matches = [
        slug for slug, entry in CAPABILITIES.items()
        if content_kind in entry["content_kinds"] and (allowed is None or slug in allowed)
    ]
    return sorted(matches, key=lambda slug: (CAPABILITIES[slug]["generic"], slug))


def connection_status(mission: Mapping[str, Any] | None) -> dict[str, str]:
    """slug -> the mission's own word for whether it is connected.

    A slug the mission's source plan does not mention is ``undeclared``, which
    is a different thing from ``not_connected``: the first says the mission
    never asked for it, the second says it asked and it is not there.
    """

    if mission is None:
        return {}
    by_source: dict[str, str] = {}
    for entry in mission.get("source_plan", ()):
        ref = entry.get("source_ref")
        if isinstance(ref, str):
            by_source[SOURCE_PLAN_ALIASES.get(ref, ref)] = str(entry.get("status") or "unknown")
            by_source.setdefault(ref, str(entry.get("status") or "unknown"))
    result: dict[str, str] = {}
    for slug in CAPABILITIES:
        ref = source_ref_for(slug)
        result[slug] = by_source.get(ref, "undeclared")
    return result


def build_map(
    *,
    mission: Mapping[str, Any] | None = None,
    cadences: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The whole table, content-hashed, deterministic in its inputs."""

    status = connection_status(mission)
    baseline = dict(cadences or {})
    sources = []
    for slug in sorted(CAPABILITIES):
        entry = capability(slug)
        cadence = baseline.get(slug)
        sources.append({
            **entry,
            "connection_status": status.get(slug, "undeclared"),
            "cadence_baseline_seconds": None if cadence is None
            else int(cadence["interval_seconds"]),
            "cadence_adjustable": None if cadence is None else bool(cadence["adjustable"]),
        })
    projection = {
        "projection_kind": "source_capability_map",
        "schema_version": SCHEMA_VERSION,
        "content_kinds": list(CONTENT_KINDS),
        "mission_version_ref": None if mission is None else mission["id"],
        "sources": sources,
    }
    projection["content_hash"] = content_hash(projection)
    return projection


def prompt_table(
    projection: Mapping[str, Any], *, connected_only: bool = True, limit: int = 12
) -> str:
    """The compact "where to get what" block the judgement prompt carries.

    Bounded and one line per source: a research decision has to *name* a
    source kind, and the only thing that makes that possible in one bounded
    call is a table small enough to be read in the prompt it is decided in.
    """

    lines = ["source | content it yields | tier | status"]
    rows = [
        row for row in projection["sources"]
        if not connected_only or row["connection_status"] == "connected"
    ]
    if not rows:
        rows = list(projection["sources"])
    for row in rows[:limit]:
        kinds = ",".join(row["content_kinds"][:4])
        generic = " (generic)" if row["generic"] else ""
        lines.append(
            f"{row['slug']}{generic} | {kinds} | {row['evidence_tier']} | "
            f"{row['connection_status']}"
        )
    return "\n".join(lines)


__all__ = [
    "CAPABILITIES",
    "CONTENT_KINDS",
    "SCHEMA_VERSION",
    "SOURCE_PLAN_ALIASES",
    "SourceCapabilityError",
    "build_map",
    "capability",
    "connection_status",
    "prompt_table",
    "source_ref_for",
    "sources_for",
]
