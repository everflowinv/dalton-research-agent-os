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
    # W4. Two kinds, not one, because the brain has to be able to ask for the
    # right one. An ``ownership_filing`` says who bought or sold the company's
    # shares (Form 4, SC 13D/G, 144, 13F); a ``buyback_disclosure`` says what
    # the company did with its own. They come from different operations, they
    # answer different questions, and a research decision that named "filing"
    # for either would have chosen nothing.
    "ownership_filing",
    "insider_trading_plan",
    "buyback_disclosure",
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
        "content_kinds": (
            "filing", "financial_statement", "buyback_disclosure",
            "insider_trading_plan",
        ),
        "evidence_tier": "primary_filing", "markets": ("US",), "generic": False,
        "note": (
            "filings index (list_filings) and XBRL company facts; the numeric "
            "authority. CAN deliver: every form a US issuer has filed, by "
            "accession and date, with the item numbers on an 8-K; the 10-Q and "
            "10-K text once a filing has been fetched, which is where the Item 2 "
            "issuer-purchases table lives (monthly shares, average price paid, "
            "remaining authorisation), plus Item 5 adopted or terminated insider "
            "trading arrangements. A plan is not an executed trade. CANNOT deliver: "
            "a daily buyback figure -- "
            "the US has no such disclosure at all. A US repurchase is visible "
            "only in a 10-Q/10-K Item 2 table, in an 8-K announcing a board "
            "authorisation, or on the earnings call, and the first is up to a "
            "quarter stale on the day it appears. Hong Kong's daily buyback "
            "return is a different connector and is being built in parallel; "
            "do not expect it here"
        ),
    }),
    # W4. Its own row rather than a footnote on ``sec``, because the four
    # ownership operations are approved one at a time and a brain deciding
    # where to look needs to see them as a thing it can ask for. Same source,
    # same politeness budget; the inventory profile is shared (see
    # SLUG_INVENTORY_ALIASES) and the operations column is narrowed to the four
    # this row actually describes.
    "sec-ownership": MappingProxyType({
        "content_kinds": ("ownership_filing",),
        "evidence_tier": "primary_filing", "markets": ("US",), "generic": False,
        "note": (
            "Form 4/3/5, SC 13D/G, Form 144 and 13F for a named issuer. CAN "
            "deliver: who bought or sold, in what role, how many shares, at what "
            "price, what they hold afterwards, and -- on forms filed under the "
            "2023 schema -- the Rule 10b5-1 checkbox. A Form 144 is a notice of a "
            "*proposed* sale and is the leading indicator of the Form 4 that "
            "follows it. CANNOT deliver: why anybody sold, whether a pre-2023 "
            "form's sale was under a plan (the element does not exist on it), or "
            "the holdings of the institutions that own this company -- a 13F in "
            "an issuer's own submissions index is one the issuer filed as a "
            "manager, not one filed about it"
        ),
    }),
    "sec-financials": MappingProxyType({
        "content_kinds": ("financial_statement",),
        "evidence_tier": "primary_filing", "markets": ("US",), "generic": False,
        "note": "statement structure, line by line, from the filed statements",
    }),
    # W4. Declared so the brain can see where a *daily* buyback figure lives
    # and that it is not here yet. The vendor's ``buybacks`` operation is the
    # mainland A-share market-wide table (eastmoney), filtered to one issuer;
    # it is not the HKEX daily repurchase return, which is a separate connector
    # another agent is building. Naming both in one place is what stops a
    # research decision reaching for the A-share table to answer a Hong Kong
    # question.
    "cn-hk-findata": MappingProxyType({
        "content_kinds": (
            "financial_statement", "filing", "buyback_disclosure",
        ),
        "evidence_tier": "primary_filing", "markets": ("CN", "HK"), "generic": False,
        "note": (
            "A-share and H-share fundamentals, shareholders, margin balance and "
            "the market-wide buyback table. CAN deliver: announced A-share "
            "repurchase programmes and what has been bought under them, with the "
            "vendor's own caliber note. CANNOT deliver: a US repurchase, and not "
            "yet the HKEX daily buyback return -- that connector is being built "
            "in parallel and this slice does not build it"
        ),
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
    # W4: the only daily buy-back disclosure in any market, plus the SFC's
    # Part XV notices and HKEXnews' announcement index. ``filing`` rather than
    # ``financial_statement``: these are ownership and treasury disclosures and
    # nothing here may be read for a statement figure (``HKEX_GRADE``).
    "hkex-filings": MappingProxyType({
        "content_kinds": ("filing",), "evidence_tier": "primary_filing",
        "markets": ("HK",), "generic": False,
        "note": "Hong Kong's next-day share buy-back tape, Disclosure of "
                "Interests, and the announcement index; the only market that "
                "discloses buy-backs daily, and the only source here for a "
                "company:hk-secucode: name",
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

#: W4: slugs in this map that are served by another slug's inventory profile.
#:
#: ``sec-ownership`` is not a second connector. Its four operations are on the
#: SEC template, under the SEC identity, against the same politeness budget;
#: what makes it a row of its own here is that it answers a different question
#: and is approved separately. So the profile is borrowed and the operation
#: list is narrowed, rather than the row reporting ``in_inventory: false`` --
#: which would be a false statement about an operation this Core holds an
#: approval for.
SLUG_INVENTORY_ALIASES: Mapping[str, str] = MappingProxyType({
    "sec-ownership": "sec",
})

#: The operations each aliased slug actually describes. Narrowed rather than
#: inherited: a map that told the brain ``sec-ownership`` could read company
#: facts would be offering a capability that approval does not carry.
SLUG_OPERATIONS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "sec-ownership": (
        "form4_transactions", "beneficial_ownership", "form144_notices",
        "form13f_holdings",
    ),
    "sec": ("list_filings", "list_official_attachments", "get_official_attachment",
            "read_item", "get_company_facts"),
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


def _completeness(
    definition: Mapping[str, Any] | None, operations: Sequence[str] | None = None
) -> str | None:
    """The weakest completeness any of a connector's operations promises.

    Weakest rather than best: a connector one of whose operations only returns
    a sample cannot be relied on for "all of them", and a map that reported the
    best ceiling would let a research plan assume enumeration it will not get.
    """

    if definition is None:
        return None
    order = ("sampled", "bounded", "enumerated")
    wanted = None if operations is None else set(operations)
    seen = [
        op.get("completeness") for op in definition["operations"]
        if wanted is None or op["name"] in wanted
    ]
    known = [value for value in seen if value in order]
    if not known:
        return None
    return min(known, key=order.index)


def _definition_for(slug: str) -> Mapping[str, Any] | None:
    """The inventory profile that serves this slug, following the aliases."""

    inventory = _inventory()
    return inventory.get(SLUG_INVENTORY_ALIASES.get(slug, slug))


def _operations_for(slug: str, definition: Mapping[str, Any] | None) -> tuple[str, ...]:
    if definition is None:
        return ()
    declared = SLUG_OPERATIONS.get(slug)
    available = tuple(op["name"] for op in definition["operations"])
    if declared is None:
        return available
    return tuple(name for name in declared if name in available)


def source_ref_for(slug: str) -> str | None:
    definition = _definition_for(slug)
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
    definition = _definition_for(slug)
    entry = CAPABILITIES[slug]
    if entry["evidence_tier"] not in EVIDENCE_TIERS:
        raise SourceCapabilityError(f"{slug}: evidence tier is not in the frozen vocabulary")
    return {
        "slug": slug,
        "source_ref": source_ref_for(slug),
        "in_inventory": definition is not None,
        "source_type": None if definition is None else definition["source_type"],
        "transport": None if definition is None else definition["transport"],
        "operations": _operations_for(slug, definition),
        "content_kinds": tuple(entry["content_kinds"]),
        "evidence_tier": entry["evidence_tier"],
        "markets": tuple(entry["markets"]),
        "generic": bool(entry["generic"]),
        "completeness_ceiling": _completeness(definition, _operations_for(slug, definition)),
        # The governed quota rows for the operations *this row* describes.
        # An aliased slug borrows its profile and must not borrow the whole
        # connector's quota table with it: ``sec-ownership`` showing
        # ``get_company_facts``'s daily ceiling would be telling the brain about
        # a budget its four operations do not spend.
        "quotas": tuple(
            tuple(sorted(row.items()))
            for row in _quotas().get(SLUG_INVENTORY_ALIASES.get(slug, slug), ())
            if not SLUG_OPERATIONS.get(slug)
            or row["operation"] in SLUG_OPERATIONS[slug]
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
    "SLUG_INVENTORY_ALIASES",
    "SLUG_OPERATIONS",
    "SOURCE_PLAN_ALIASES",
    "SourceCapabilityError",
    "build_map",
    "capability",
    "connection_status",
    "prompt_table",
    "source_ref_for",
    "sources_for",
]
