"""S1: turning two local feeds into mission discoveries and acquired documents.

The mission already has a shape for "a source was searched, here is what it
holds, acquire the ones we do not have": a source discovery record binding a
connector invocation, a row per discovered document, a bounded acquisition per
tick, and a review row when the bytes land. AlphaEngine, web search and the
SEC filings index all use it. These two feeds use the same one -- they are not
a second queue with its own rules.

**Attribution is where a local feed differs, and it differs in two ways.**

A sell-side note carries a subject line and nothing else at discovery time;
the body only exists after acquisition. So a note is attributed from what its
headers actually say. Notes about a covered company are queued for that
company. Notes about the macro -- rates, the Korea open, an index rebalance --
match nobody, and this lane does not queue them. That is the owner's rule
applied honestly: an industry document has no company tag, and inventing one
would fill a per-company queue with documents nobody should pay to read.

A wiki document is easier and stricter: a person already filed it under a
company or a sector, so the corpus's own tags are the attribution. A sector
note arrives with an empty company list and keeps it.

**What this module does not do.** It does not write the connector authority a
source discovery has to bind. ``record_source_discovery`` requires a real
``ConnectorInvocation`` and ``SourceEnvelope`` in Core, produced by the runner
for the transport in question -- and the host-tool runner does not exist yet.
So the recording step takes a receipt provider, and without one it refuses
with a reason rather than writing a discovery that binds nothing. Everything
downstream of a recorded discovery -- selection, the bounded acquisition
batch, settlement, the review row -- is here and is what the tick runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .company_wiki_core import SOURCE_REF as COMPANY_WIKI_SOURCE_REF
from .coverage_mission import DISCOVERED_DOCUMENT_STATUSES
from .document_subject import subject_names
from .sales_notes_core import (
    DOCUMENT_REF_PREFIX as SALES_NOTE_REF_PREFIX,
    SOURCE_REF as SALES_NOTES_SOURCE_REF,
)
from .company_wiki_core import DOCUMENT_REF_PREFIX as WIKI_DOC_REF_PREFIX
from .prior_research_core import (
    DOCUMENT_REF_PREFIX as PRIOR_RESEARCH_REF_PREFIX,
    SOURCE_REF as PRIOR_RESEARCH_SOURCE_REF,
)
from .lane_child_launcher import write_owner_only
from .lane_registry import LaneSpec, register_lane
from .connector import ConnectorQuotaExceeded
from .store import canonical_json, content_hash

# The two rows `coverage_mission.DISCOVERY_SOURCES` needs before either feed
# can record a discovery. Declared here so the integration is one merge of a
# table this module already owns the truth for, and so the tests can bind the
# authority to exactly what the integrator will install.
# The operation is the *document* read, not the index read, because for a
# local feed the acquisition is the discovery: a sell-side note's subject line
# does not say which company it is about, so the only way to find out is to
# read the body -- and reading the body is the acquisition. So one discovery
# record names exactly the one document that was read, and the envelope it
# binds carries exactly that document's ref. A listing-shaped envelope would
# have to claim every note it enumerated belonged to every company it was
# recorded for.
FEED_DISCOVERY_SOURCES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    SALES_NOTES_SOURCE_REF: MappingProxyType({
        "connector_source_ref": SALES_NOTES_SOURCE_REF,
        "operation": "get_note",
        "document_ref_prefix": SALES_NOTE_REF_PREFIX,
    }),
    COMPANY_WIKI_SOURCE_REF: MappingProxyType({
        "connector_source_ref": COMPANY_WIKI_SOURCE_REF,
        "operation": "get_document",
        "document_ref_prefix": WIKI_DOC_REF_PREFIX,
    }),
    # W3: the fund's own earlier work. Same rule and the same reason -- for a
    # local feed the acquisition is the discovery -- with one difference worth
    # naming: this feed *does* know which company each document belongs to
    # before it reads it, because the manifest is filed per company folder.
    # It still records the discovery on the document rather than on the
    # listing, so that a prior view's ``as_of`` is bound to the one document
    # that carries it.
    PRIOR_RESEARCH_SOURCE_REF: MappingProxyType({
        "connector_source_ref": PRIOR_RESEARCH_SOURCE_REF,
        "operation": "get_document",
        "document_ref_prefix": PRIOR_RESEARCH_REF_PREFIX,
    }),
})

SALES_NOTE_SPEC_REF = "sales-note"
WIKI_SPEC_PREFIX = "wiki-"
# W3: one spec for the whole feed, not one per kind as the wiki has. The spec
# is what carries the evidence tier, and every kind of prior document is the
# same tier -- an old screen, a memo and a working note are all "us, earlier".
# The kind still travels on the wire and decides reading order; it does not
# decide provenance strength, because there is only one provenance here.
# ``claim_index_tagging.SPEC_IMPORTANCE`` holds this exact key.
PRIOR_RESEARCH_SPEC_REF = "prior-research"
#: What an analyst opens first when a company already has a file.
PRIOR_READ_ORDER: tuple[str, ...] = (
    "initial_screen", "memo", "notes", "model_excel", "other",
)
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9-]*$")
DISCOVERY_SCOPE = "source_discovery"
# Both feeds read local files, so a tick can afford a real batch; the bound is
# here to keep one tick finite, not to ration an upstream.
ACQUISITIONS_PER_TICK = 8
ACQUISITION_WAIT_SECONDS = 30.0
TICK_BUDGET_SECONDS = 8.0

# 2026-09-16 (B1-2): enumerating and reading get separate budgets, because a
# shared one is not shared -- it is spent in order, and enumeration goes
# first.  Live, three feed lanes reported the same tick forever:
# ``enumerated: 222, launched: 0, out_of_time: true``.  Each enumeration is a
# governed child process, the lookback splits into up to nine windows, and
# nine process starts are more than eight seconds; the read budget was always
# already gone by the time there was anything to read, so the lane did the
# expensive half of its job every tick and none of the useful half.
#
# Two changes, and they only make sense together.  Reading keeps a reserve of
# the tick that enumeration may not touch -- expressed as a reserve rather
# than a share because what reading needs is an absolute number of seconds
# (one child start plus one local read), not a fraction of whatever budget
# the tick happens to have...
READ_RESERVE_SECONDS = 3.0
# ...and, when a caller wants one, at most this many windows per tick.  The
# default is ``None`` -- no count limit -- because the deadline above is the
# real bound and a count would change what a fast feed does for no reason.
# What makes the deadline safe is the cursor: a tick that runs out of
# enumeration budget resumes at the next window rather than re-enumerating
# the newest one forever, so the lookback is covered across several ticks
# instead of starving every one of them.  Newest-first ordering is unchanged;
# the cursor rotates the list, it does not reverse it.
ENUMERATIONS_PER_TICK: int | None = None
# Whatever is left, a document read gets -- but never less than this, or a
# slow enumeration silently reintroduces the starvation.  A tick that reads
# one document has made progress; a tick that reads none never will.
MIN_DOCUMENT_READ_SECONDS = 2.0

# 2026-09-16 (I1): an enumeration outlives the tick that paid for it.
#
# B1-2 split the budgets and added a cursor, and the lanes still reported
# ``enumerated: 211, launched: 0, out_of_time: true`` every five minutes.  The
# arithmetic says why: the writer gives a lane five seconds
# (``writer_server.LANE_SOFT_BUDGET_SECONDS``), one enumeration is a governed
# child process over a local corpus and costs three to four of them, and what
# is left is one document read -- if the tick does not overrun first.  So the
# lane spent five sixths of every tick re-listing a directory it had already
# listed, and the *reading* -- the only half that turns a feed into mission
# documents -- got the crumbs.
#
# The listing is the cheap thing to be wrong about.  A fortnight of sell-side
# notes does not change between two ticks five minutes apart, and when it does
# the only cost of a stale listing is that this tick reads yesterday's notes
# instead of this morning's -- the next refresh picks them up.  Whereas
# re-deriving it costs a process start every single tick, for ever.  So the
# enumeration is cached on disk, keyed by the window it covers and fingerprinted
# by what would make it wrong (the feed, the frozen plan, the window shape),
# and a tick that finds a live entry spends its whole budget reading.
#
# An hour, because that is short enough that a note filed at nine is read
# before lunch, and long enough that a five-minute controller covers the whole
# lookback out of cache between refreshes.
ENUMERATION_CACHE_TTL_SECONDS = 3600.0
# At most this many windows are kept.  The lookback is nine windows and the
# cursor rotates through them, so the bound is a guard against a lookback that
# grows, not a limit the normal case reaches.  Oldest entries are dropped
# first, because the newest window is the one a tick asks for most.
ENUMERATION_CACHE_MAX_WINDOWS = 32
# A sidecar this big is a bug, not a busy fortnight; a cache that cannot be
# trusted is dropped rather than parsed.
ENUMERATION_CACHE_MAX_BYTES = 16_000_000

# What one governed child may take before it is killed.  This used to be
# derived from the tick budget (``budget / 2`` = 4 s), which conflated two
# unrelated questions: how long a *hung* child may hang, and how much of this
# tick the lane may spend.  A local corpus read is one Python process start
# plus a directory walk -- rarely under a second, occasionally six -- so a cap
# derived from a shrinking tick budget kills healthy children, and the lane
# reports failures that are really its own scheduling.  The tick budget still
# decides whether a child is *started*; this decides when one has hung.  The
# ceiling matches the crowd lane's, which is the other host-tool lane and has
# run at 20 s since S2.
FEED_CHILD_TIMEOUT_SECONDS = 20.0
FEED_CHILD_MIN_TIMEOUT_SECONDS = 6.0


def feed_child_timeout_seconds(remaining: float | None) -> float:
    """The child cap for a tick with ``remaining`` seconds of budget left."""

    if remaining is None:
        return FEED_CHILD_TIMEOUT_SECONDS
    return min(
        FEED_CHILD_TIMEOUT_SECONDS,
        max(FEED_CHILD_MIN_TIMEOUT_SECONDS, float(remaining)),
    )
ACQUISITION_RETRY_INTERVAL = timedelta(hours=1)
# The queue reader's own maximum page. One query per status stays far
# under it; a status bucket that reaches it is reported, not truncated.
DOCUMENT_PAGE_LIMIT = 1_000
# How much of the lookback one enumeration asks for. Two weeks of this
# feed is a couple of hundred notes, comfortably inside the record
# ceiling, so a window that truncates is a busy fortnight rather than
# the normal case.
ENUMERATION_WINDOW_DAYS = 14
# A truncated window is halved; this bounds the halving so a
# pathological day cannot recurse without end.
MAX_WINDOW_SPLITS = 6
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FeedLaneError(RuntimeError):
    """The feed lane cannot run as configured."""


class FeedLaneRejected(ValueError):
    """The feed lane refused a request before doing anything."""


# -- the frozen plan ----------------------------------------------------

PLAN_SCHEMA_VERSION = "0.1"
_PLAN_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_ref", "source_refs",
    "companies", "industry_keywords", "peer_names", "lookback_days",
    "body_reads_per_tick", "content_hash",
})
MAX_BODY_READS_PER_TICK = 200
# The company-wiki host read is about 0.85 s per document on the deployed
# corpus. Fifty sequential reads exceeded the Writer's 30 s RPC boundary even
# though every child succeeded. Twelve leaves room for enumeration, recording
# and a slow local read; unread documents remain in the next enumeration.
COMPANY_WIKI_BODY_READS_PER_TICK = 12


def validate_feed_discovery_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed plan a feed lane runs under.

    A plan of its own rather than an extension of the search discovery plan:
    that one freezes a query template and a lookback per spec because a search
    costs money per query. A feed has no query -- the whole window is on disk
    -- so what it needs frozen is the opposite thing: which words make a
    document worth reading, and how many to read per tick.
    """

    if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
        raise FeedLaneRejected("feed discovery plan has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != PLAN_SCHEMA_VERSION:
        raise FeedLaneRejected("unsupported feed discovery plan schema_version")
    for name in ("id", "created_at", "mission_ref"):
        if not isinstance(wire[name], str) or not wire[name].strip():
            raise FeedLaneRejected(f"feed discovery plan {name} is required")
    sources = wire["source_refs"]
    if not isinstance(sources, list) or not sources or len(set(sources)) != len(sources) \
            or any(ref not in FEED_DISCOVERY_SOURCES for ref in sources):
        raise FeedLaneRejected("feed discovery plan names a source that is not a feed")
    companies = wire["companies"]
    if not isinstance(companies, Mapping) or not companies:
        raise FeedLaneRejected("feed discovery plan needs at least one company")
    for company_ref, entry in companies.items():
        # W7: ``names`` is optional and additive. The legacy Core's plan has
        # only ``search_terms`` and must keep validating; a plan generated from
        # a mission carries what each issuer is called, so a lane never has to
        # look the answer up in a dict shared with another environment.
        if not isinstance(entry, Mapping) or set(entry) - {"search_terms", "names"} \
                or "search_terms" not in entry \
                or not str(entry["search_terms"]).strip():
            raise FeedLaneRejected(f"feed discovery plan entry for {company_ref} is malformed")
        if "names" in entry:
            names = entry["names"]
            if not isinstance(names, list) or not names \
                    or len(set(names)) != len(names) \
                    or any(not isinstance(name, str) or not name.strip() for name in names):
                raise FeedLaneRejected(
                    f"feed discovery plan names for {company_ref} must be unique non-empty text")
    for name in ("industry_keywords", "peer_names"):
        terms = wire[name]
        if not isinstance(terms, list) or not terms or len(set(terms)) != len(terms) \
                or any(not isinstance(item, str) or not item.strip() for item in terms):
            raise FeedLaneRejected(f"feed discovery plan {name} must be unique non-empty terms")
        if terms != sorted(terms):
            raise FeedLaneRejected(f"feed discovery plan {name} must be sorted")
    lookback = wire["lookback_days"]
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 1 <= lookback <= 3650:
        raise FeedLaneRejected("feed discovery plan lookback_days must be 1..3650")
    reads = wire["body_reads_per_tick"]
    if isinstance(reads, bool) or not isinstance(reads, int) \
            or not 1 <= reads <= MAX_BODY_READS_PER_TICK:
        raise FeedLaneRejected(
            f"feed discovery plan body_reads_per_tick must be 1..{MAX_BODY_READS_PER_TICK}"
        )
    declared = wire.pop("content_hash")
    if content_hash(wire) != declared:
        raise FeedLaneRejected("feed discovery plan content_hash is invalid")
    wire["content_hash"] = declared
    return wire


def load_feed_discovery_plan(path: str | Path) -> dict[str, Any]:
    return validate_feed_discovery_plan(
        json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    )


def plan_terms(plan: Mapping[str, Any]) -> list[str]:
    """Every phrase that makes a document worth reading, industry then peers.

    The coverage companies are deliberately **not** in here: they are matched
    by ``document_subject``'s own name table, which is where "what Accenture
    is called" already lives and where a second list would go stale.
    """

    return list(plan["industry_keywords"]) + list(plan["peer_names"])


# -- discovery parameters ----------------------------------------------


def feed_discovery_parameters(
    *, terms: str, since: str, as_of: date | str
) -> dict[str, Any]:
    """The closed ``{query, date_after, date_before}`` a discovery record takes.

    Deliberately the shape the mission authority already validates for a
    windowed search rather than a fourth one: a local feed asked for "these
    words, in this window" is the same request a web search is, and a new
    parameter shape would be a new branch in a table the feed does not own.
    """

    if not isinstance(terms, str) or not terms.strip():
        raise FeedLaneRejected("discovery terms are required")
    if not isinstance(since, str) or _DATE_RE.fullmatch(since) is None:
        raise FeedLaneRejected("since must be a YYYY-MM-DD date")
    before = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
    if _DATE_RE.fullmatch(before) is None:
        raise FeedLaneRejected("as_of must be a YYYY-MM-DD date")
    if before < since:
        raise FeedLaneRejected("discovery window ends before it starts")
    return {"query": terms.strip(), "date_after": since, "date_before": before}


def feed_query_hash(source_ref: str, parameters: Mapping[str, Any]) -> str:
    """Bind the query to the feed it was asked of, the way a search spec does."""

    return content_hash({"source_ref": source_ref, "parameters": dict(parameters)})


# -- attribution --------------------------------------------------------


def _universe_terms(
    universe: Sequence[Mapping[str, Any]],
    names: Mapping[str, Sequence[str]] | None = None,
) -> list[tuple[str, str]]:
    """Company ref and ticker for each covered name, refusing an unnamed one.

    A ticker the name tables do not know still "matches" itself -- a subject
    line saying ZZZZ names ZZZZ -- and that is the trap: a subject line almost
    never says the ticker, so such a company would be silently attributed
    nothing at all for the whole run. It fails at the top instead.

    W7: what counts as "known" is this mission's table, not a dict in this
    package. ``names`` is normally ``mission_name_table(universe, plan)``; the
    packaged five answer only when no table was supplied, which is the legacy
    Core. So the fix for a refusal is a row in *this mission's* feed plan.
    """

    from .mission_company_names import (
        MissionCompanyNamesError, mission_name_table, require_named,
    )

    table = dict(names) if names is not None else mission_name_table(universe)
    try:
        require_named(
            {ticker: table.get(ticker, ()) for ticker in (
                str(item["ticker"]).strip().upper() for item in universe)},
            where="this mission's feed discovery plan (companies[].names)",
        )
    except MissionCompanyNamesError as exc:
        raise FeedLaneRejected(str(exc)) from exc
    return [(str(item["company_ref"]), str(item["ticker"]).strip())
            for item in universe]


def plan_company_names(
    universe: Sequence[Mapping[str, Any]], plan: Mapping[str, Any] | None,
) -> dict[str, tuple[str, ...]]:
    """This run's ticker-to-names table, built from its own plan."""

    from .mission_company_names import mission_name_table

    return mission_name_table(universe, plan)


def mentions_any(text: Any, phrases: Sequence[str]) -> list[str]:
    """Which of these phrases the text names, by the ledger's own matching rule.

    ``document_subject._mentions`` is reused rather than reimplemented: it is
    the NFKC fold plus word-boundary match that every subject decision in this
    system already uses, and a second copy of it would be a second answer to
    "does this document name Accenture".
    """

    from .document_subject import _mentions

    return _mentions(text if isinstance(text, str) else "", list(phrases))


def attribute_notes(
    notes: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]],
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Which covered company each note's **headers** name, if any.

    This is the cheap first pass, not the answer. Measured over the whole
    archive it finds 15 notes out of 2,923 -- sell-side subject lines are
    written for a distribution list, not for a filing index -- while 231 of
    those notes name a covered company somewhere in the body. So a header hit
    is treated as "attribute without reading" and a header miss is treated as
    "not yet known", never as "not about this company".
    """

    from .document_subject import document_names_subject

    entries = _universe_terms(universe, names)
    by_company: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for note in notes:
        subject = str(note.get("subject") or "")
        matched = False
        for company_ref, ticker in entries:
            if document_names_subject(subject, ticker, names)["names_subject"]:
                by_company.setdefault(company_ref, []).append(note["note_id"])
                matched = True
        if not matched:
            unattributed.append(note["note_id"])
    return {
        "by_company": {ref: sorted(dict.fromkeys(refs)) for ref, refs in sorted(by_company.items())},
        "unattributed": sorted(dict.fromkeys(unattributed)),
    }


def attribute_wiki_documents(
    documents: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]],
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The corpus's own company tags, intersected with the mission universe.

    No text matching. A person filed the document under a ticker or under a
    sector, and that filing is a better attribution than anything a regex over
    the prose would produce. A sector note has no company tag; it stays
    unattributed and is not queued.
    """

    ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"]) for item in universe}
    by_company: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for document in documents:
        tags = [tag for tag in document.get("company_tags", ()) if tag in ticker_to_ref]
        if not tags:
            unattributed.append(document["document_id"])
            continue
        for tag in tags:
            by_company.setdefault(ticker_to_ref[tag], []).append(document["document_id"])
    return {
        "by_company": {ref: sorted(dict.fromkeys(refs)) for ref, refs in sorted(by_company.items())},
        "unattributed": sorted(dict.fromkeys(unattributed)),
    }


def triage_notes(
    notes: Sequence[Mapping[str, Any]],
    universe: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Split an enumeration into "already attributed" and "worth reading".

    The analyst rule this implements: read any note that mentions a covered
    company **or** the industry. The header is checked first because it is
    free, and everything else goes into a queue ordered by how likely it is to
    repay a read -- header names a company, then header names an industry
    term, then the rest newest first.

    Nothing is dropped here. A header that says nothing is not evidence that
    the body says nothing, and the body is a local file: the drop decision
    belongs after the read, where it can carry a reason.
    """

    names = names if names is not None else plan_company_names(universe, plan)
    entries = _universe_terms(universe, names)
    terms = plan_terms(plan)
    company: dict[str, list[str]] = {}
    header_company: list[str] = []
    header_industry: list[str] = []
    rest: list[str] = []
    for note in notes:
        header_text = f"{note.get('subject') or ''} {note.get('sender') or ''}"
        hits = [ref for ref, ticker in entries
                if mentions_any(header_text, subject_names(ticker, names))]
        if hits:
            for ref in hits:
                company.setdefault(ref, []).append(note["note_id"])
            header_company.append(note["note_id"])
        elif mentions_any(header_text, terms):
            header_industry.append(note["note_id"])
        else:
            rest.append(note["note_id"])
    return {
        "header_company": {ref: sorted(dict.fromkeys(refs))
                           for ref, refs in sorted(company.items())},
        # Every note still needs its body read: a header hit says which
        # company it is about, not what it says, and the review path can only
        # read bytes that were acquired.
        "read_queue": header_company + header_industry + list(reversed(rest)),
        "header_industry_count": len(header_industry),
    }


def attribute_body(
    text: Any,
    universe: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    *,
    header_companies: Sequence[str] = (),
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The decision a body read is for: company, industry, or dropped.

    ``header_companies`` are refs the header already established; the body can
    only add to them. A document that names no covered company but does name
    the industry is kept as industry-level -- an analyst reads the sector note
    too -- and one that names neither is dropped **with a reason**, which is
    the difference between "we looked" and "we never saw it".
    """

    names = names if names is not None else plan_company_names(universe, plan)
    entries = _universe_terms(universe, names)
    companies = list(dict.fromkeys(header_companies))
    for ref, ticker in entries:
        if ref not in companies and mentions_any(text, subject_names(ticker, names)):
            companies.append(ref)
    terms = mentions_any(text, plan_terms(plan))
    if companies:
        return {"outcome": "company", "company_refs": sorted(companies),
                "industry_terms": terms, "reason": None}
    if terms:
        return {"outcome": "industry", "company_refs": [], "industry_terms": terms,
                "reason": None}
    return {"outcome": "dropped", "company_refs": [], "industry_terms": [],
            "reason": "names no coverage company and no industry term"}


def triage_wiki_documents(
    documents: Sequence[Mapping[str, Any]],
    universe: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The same split for the wiki, decided by the corpus's own filing.

    Company attribution here does **not** get a body pass. A person already
    filed the document under a ticker or a sector, and that filing is a better
    answer than a regex over the prose -- an expert call about the sector that
    mentions Accenture twice is still a sector document. The body read is
    still what decides industry-level versus dropped, because a thinly tagged
    note may only say what it is about in its text.
    """

    _universe_terms(universe,
                    names if names is not None else plan_company_names(universe, plan))
    ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"])
                     for item in universe}
    terms = plan_terms(plan)
    company: dict[str, list[str]] = {}
    tagged: list[str] = []
    industry: list[str] = []
    other: list[str] = []
    for document in documents:
        hits = [tag for tag in document.get("company_tags", ()) if tag in ticker_to_ref]
        if hits:
            for tag in hits:
                company.setdefault(ticker_to_ref[tag], []).append(document["document_id"])
            tagged.append(document["document_id"])
            continue
        tag_text = " ".join(
            list(document.get("sector_tags", ())) + list(document.get("topic_tags", ()))
            + [str(document.get("category_name") or ""), str(document.get("doc_type") or "")]
        )
        # Nothing is dropped here either -- the tags are thin and a document
        # may only say what it is about in its text -- but a document whose
        # tags already name the industry is read before one whose tags say
        # nothing, because a bounded batch should spend itself on the likely
        # half first.
        (industry if mentions_any(tag_text, terms) else other).append(
            document["document_id"]
        )
    return {
        "header_company": {ref: sorted(dict.fromkeys(refs))
                           for ref, refs in sorted(company.items())},
        "read_queue": tagged + industry + other,
        "header_industry_count": len(industry),
    }


def prior_research_ticker(document: Mapping[str, Any]) -> str:
    """The ticker a prior document is filed under, from its company folder.

    The manifest is filed per company, so the attribution is the owner's own
    filing rather than a regex over the prose -- the same principle the wiki
    follows, and stronger here because the folder is the only thing the owner
    had to decide. A folder that is not ticker-shaped (``acn-accenture``)
    yields nothing rather than a guess.
    """

    candidate = str(document.get("company") or "").strip()
    return candidate if _TICKER_RE.fullmatch(candidate) else ""


def attribute_prior_documents(
    documents: Sequence[Mapping[str, Any]], universe: Sequence[Mapping[str, Any]],
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The company folder, intersected with the mission universe.

    One document belongs to at most one company, unlike a wiki note that can
    carry five tags: the owner put the file in one folder, and a prior view
    written about Accenture is not also a prior view about Cognizant.
    """

    ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"])
                     for item in universe}
    by_company: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for document in documents:
        ticker = prior_research_ticker(document)
        if ticker not in ticker_to_ref:
            unattributed.append(document["document_id"])
            continue
        by_company.setdefault(ticker_to_ref[ticker], []).append(document["document_id"])
    return {
        "by_company": {ref: sorted(dict.fromkeys(refs))
                       for ref, refs in sorted(by_company.items())},
        "unattributed": sorted(dict.fromkeys(unattributed)),
    }


def triage_prior_documents(
    documents: Sequence[Mapping[str, Any]],
    universe: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    names: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Split an enumeration into "already attributed" and "worth reading".

    Ordered by what the analyst would read first: an earlier Initial Screen
    before a memo before loose notes, and the newest of each before the older
    ones. The enumeration already arrives newest-first, so this only has to
    keep that order within each kind.
    """

    _universe_terms(universe,
                    names if names is not None else plan_company_names(universe, plan))
    ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"])
                     for item in universe}
    company: dict[str, list[str]] = {}
    tagged: list[str] = []
    rest: dict[str, list[str]] = {kind: [] for kind in PRIOR_READ_ORDER}
    for document in documents:
        ticker = prior_research_ticker(document)
        if ticker in ticker_to_ref:
            company.setdefault(ticker_to_ref[ticker], []).append(document["document_id"])
            tagged.append(document["document_id"])
            continue
        kind = str(document.get("kind") or "other")
        rest.setdefault(kind if kind in rest else "other", []).append(
            document["document_id"]
        )
    queue = tagged + [ref for kind in PRIOR_READ_ORDER for ref in rest[kind]]
    return {
        "header_company": {ref: sorted(dict.fromkeys(refs))
                           for ref, refs in sorted(company.items())},
        "read_queue": queue,
        "header_industry_count": 0,
    }


def wiki_spec_ref(doc_type_key: str) -> str:
    """One spec per document kind, because the kind is what sets the tier.

    Kept out of ``document_figure_grade.GRADE_BY_SPEC`` on purpose: none of
    these are a filed statement or a transcript, so no figure should ever be
    read out of one. A spec with no grade is read for prose and nothing else,
    which is exactly right for an expert call or a broker note.
    """

    key = str(doc_type_key or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        raise FeedLaneRejected("wiki document kind is not a usable spec key")
    return WIKI_SPEC_PREFIX + key.replace("_", "-")


def _merge_reads(source_ref: str, reads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One tick's reads across several windows, as one answer."""

    merged: dict[str, Any] = {
        "source_ref": source_ref, "read": 0, "already_held": 0, "outcomes": [],
        "company": 0, "industry": 0, "dropped": 0, "failed": 0,
    }
    for item in reads:
        for key in ("read", "already_held", "company", "industry", "dropped", "failed"):
            merged[key] += item[key]
        merged["outcomes"].extend(item["outcomes"])
    return merged


# -- the enumeration cache ----------------------------------------------


ENUMERATION_CACHE_SCHEMA_VERSION = "0.1"


class FeedEnumerationCache:
    """One feed's window listings, on disk, surviving ticks and restarts.

    The sidecar lives beside the launcher's tickets because that is already
    this lane's owner-only durable state: same directory, same 0700, same
    ``write_owner_only`` used for the ticket files themselves.

    Every failure mode here is a miss, never an exception.  A cache is an
    optimisation; a lane that cannot read its cache must still enumerate and
    read, and a lane that crashes because a JSON file was truncated by a power
    cut has turned an optimisation into an outage.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float = ENUMERATION_CACHE_TTL_SECONDS,
        max_windows: int = ENUMERATION_CACHE_MAX_WINDOWS,
        max_bytes: int = ENUMERATION_CACHE_MAX_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_windows = max(1, int(max_windows))
        self.max_bytes = max(1, int(max_bytes))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, dict[str, Any]] | None = None

    # -- construction ---------------------------------------------------

    @classmethod
    def for_launcher(cls, launcher: Any, **kwargs: Any) -> "FeedEnumerationCache | None":
        """The cache for this launcher, or ``None`` if it has no state.

        ``None`` rather than a refusal: a coordinator built in a test around a
        stub launcher has no ticket directory, and the lane's job does not
        depend on having somewhere to cache.
        """

        path = getattr(launcher, "enumeration_cache_path", None)
        if path is None:
            return None
        return cls(path, **kwargs)

    # -- the file -------------------------------------------------------

    @staticmethod
    def _window_key(since: str, until: str) -> str:
        return f"{since}|{until}"

    def _read(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        self._entries = {}
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return self._entries
        try:
            with os.fdopen(descriptor, "rb") as handle:
                raw = handle.read(self.max_bytes + 1)
        except OSError:
            return self._entries
        if len(raw) > self.max_bytes:
            return self._entries
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._entries
        if (not isinstance(document, Mapping)
                or document.get("schema_version") != ENUMERATION_CACHE_SCHEMA_VERSION):
            return self._entries
        windows = document.get("windows")
        if not isinstance(windows, Mapping):
            return self._entries
        for key, entry in windows.items():
            if isinstance(key, str) and self._usable(entry):
                self._entries[key] = dict(entry)
        return self._entries

    @staticmethod
    def _usable(entry: Any) -> bool:
        return (
            isinstance(entry, Mapping)
            and isinstance(entry.get("since"), str)
            and isinstance(entry.get("until"), str)
            and isinstance(entry.get("fingerprint"), str)
            and isinstance(entry.get("cached_at"), str)
            and isinstance(entry.get("observations"), list)
        )

    def _write(self) -> None:
        entries = self._read()
        # Newest first, then truncated: the cursor asks for every window in
        # turn, but a lookback that grew must not grow the sidecar with it.
        ordered = sorted(
            entries.items(), key=lambda item: item[1]["cached_at"], reverse=True
        )[: self.max_windows]
        self._entries = dict(ordered)
        document = {
            "schema_version": ENUMERATION_CACHE_SCHEMA_VERSION,
            "windows": self._entries,
        }
        try:
            write_owner_only(self.path, document)
        except OSError:
            # A sidecar that cannot be written is a slow lane, not a failed
            # one. Keep what was learned in memory for this process.
            pass

    # -- the cache ------------------------------------------------------

    def _age_seconds(self, entry: Mapping[str, Any]) -> float | None:
        try:
            cached_at = datetime.fromisoformat(entry["cached_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        return (self.clock() - cached_at).total_seconds()

    def has(self, *, since: str, until: str, fingerprint: str) -> bool:
        """Whether this window is live in the cache, without materialising it.

        A tick plans its whole order before it reads anything, over a lookback
        that is twenty-nine windows wide live.  Planning must not cost a copy
        of every listing it is deciding between.
        """

        entry = self._read().get(self._window_key(since, until))
        if entry is None or entry.get("fingerprint") != fingerprint:
            return False
        age = self._age_seconds(entry)
        return age is not None and 0 <= age <= self.ttl_seconds

    def get(
        self, *, since: str, until: str, fingerprint: str
    ) -> list[dict[str, Any]] | None:
        """This window's listing, if one was taken recently for this shape."""

        entry = self._read().get(self._window_key(since, until))
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            return None
        age = self._age_seconds(entry)
        if age is None or age < 0 or age > self.ttl_seconds:
            return None
        return [dict(item) for item in entry["observations"]]

    def put(
        self, *, since: str, until: str, fingerprint: str,
        observations: Sequence[Mapping[str, Any]],
    ) -> None:
        """Keep one window's listing for the ticks that follow this one."""

        entries = self._read()
        entries[self._window_key(since, until)] = {
            "since": since,
            "until": until,
            "fingerprint": fingerprint,
            "cached_at": self.clock().astimezone(timezone.utc).isoformat(
                timespec="microseconds"
            ),
            "observations": [dict(item) for item in observations],
        }
        self._write()

    def forget(self, *, since: str, until: str) -> None:
        """Drop one window, so the next tick re-enumerates it."""

        if self._read().pop(self._window_key(since, until), None) is not None:
            self._write()


# -- the lane -----------------------------------------------------------


class FeedDiscoveryCoordinator:
    """One feed, one mission: enumerate, read a bounded batch, record, review.

    ``missions`` is a ``CoverageMissionAuthority``, ``launcher`` one of the
    feed launchers, ``plan`` the frozen feed discovery plan, and ``runner`` a
    :class:`~dalton_core.host_tool_runner.HostToolRunner` per operation. A
    coordinator without a runner still settles a queue somebody else filled;
    it cannot discover, and says so rather than writing a discovery bound to
    nothing.
    """

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        source_ref: str,
        plan: Mapping[str, Any],
        runner: Any = None,
        enumerator: Any = None,
        automation_principal: str = "automation:coverage-mission",
        clock: Callable[[], datetime] | None = None,
        acquisitions_per_tick: int = ACQUISITIONS_PER_TICK,
        acquisition_wait_seconds: float = ACQUISITION_WAIT_SECONDS,
        tick_budget_seconds: float = TICK_BUDGET_SECONDS,
        body_reads_per_tick: int | None = None,
        body_read_cursor_ref: str | None = None,
        enumerations_per_tick: int | None = ENUMERATIONS_PER_TICK,
        enumeration_cursor_ref: str | None = None,
        enumeration_cache: Any = None,
    ) -> None:
        if source_ref not in FEED_DISCOVERY_SOURCES:
            raise FeedLaneRejected(f"{source_ref} is not a feed discovery source")
        self.missions = missions
        self.launcher = launcher
        self.source_ref = source_ref
        self.plan = validate_feed_discovery_plan(plan)
        self.companies = dict(self.plan["companies"])
        # Two runners, because two operations: the index read and the document
        # read are separate capabilities with separate approvals, so they are
        # separate profiles and cannot share one.
        self.runner = runner
        self.enumerator = enumerator
        self.automation_principal = automation_principal
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.acquisitions_per_tick = max(1, int(acquisitions_per_tick))
        self.acquisition_wait_seconds = float(acquisition_wait_seconds)
        self.tick_budget_seconds = max(0.0, float(tick_budget_seconds))
        self.body_reads_per_tick = int(
            self.plan["body_reads_per_tick"] if body_reads_per_tick is None
            else body_reads_per_tick
        )
        self.body_read_cursor_ref = body_read_cursor_ref
        self.enumerations_per_tick = (
            None if enumerations_per_tick is None
            else max(1, int(enumerations_per_tick))
        )
        self.enumeration_cursor_ref = enumeration_cursor_ref
        # Optional on purpose: without one the lane behaves exactly as it did
        # before the cache existed -- it enumerates every window it reaches.
        self.enumeration_cache = enumeration_cache

    def enumeration_order(
        self, *, since: str | None = None
    ) -> list[tuple[str, str]]:
        """This tick's windows, newest first, resumed where the last stopped.

        ``windows()`` is unchanged and still returns the lookback newest
        first.  This rotates that list so a tick that may enumerate one window
        does not enumerate the newest one every time and leave the other eight
        permanently unvisited.  An unknown cursor -- the first tick after a
        restart, or a lookback that moved past it -- starts at the newest,
        which is the behaviour this lane had before the rotation existed.
        """

        spans = self.windows(since=since)
        if not spans or self.enumeration_cursor_ref is None:
            return spans
        for index, (_, window_until) in enumerate(spans):
            if window_until == self.enumeration_cursor_ref:
                pivot = (index + 1) % len(spans)
                return spans[pivot:] + spans[:pivot]
        return spans

    def tick_windows(
        self, *, since: str | None = None
    ) -> list[tuple[str, str, bool]]:
        """This tick's windows: the ones already listed first, then the rest.

        This ordering is what makes the cache reachable at all, and the live
        numbers are why.  The deployed lookback is four hundred days, which is
        twenty-nine windows; the cursor visits one per tick and the controller
        ticks every five minutes, so a window comes round again after two and
        a half hours.  A listing kept for one hour would therefore *never* be
        found -- every tick would arrive at a window whose entry had expired
        forty minutes earlier and buy it again, which is exactly the behaviour
        this work package exists to end.

        So reading comes before listing.  A window whose listing is already on
        disk is free to read from, and reading is the half of this lane that
        produces documents; buying a new listing while there is still unread
        material in one already paid for is spending a quarter of the tick to
        find work the lane already has.  A new listing is bought when reading
        finishes early -- the enumeration reserve is what "early" means -- and
        the cursor still rotates over those purchases, so the whole lookback
        is covered, just in longer stretches per window.

        The flag is whether the window was live in the cache when the tick
        planned its order.  It is re-checked before use: planning is cheap and
        deliberately does not materialise twenty-nine listings.
        """

        kept: list[tuple[str, str, bool]] = []
        fresh: list[tuple[str, str, bool]] = []
        for window_since, window_until in self.enumeration_order(since=since):
            target = (
                kept
                if self.has_enumeration(since=window_since, until=window_until)
                else fresh
            )
            target.append((window_since, window_until, target is kept))
        return kept + fresh

    # -- windows --------------------------------------------------------

    def window_start(self) -> str:
        """The oldest day this lane will look at, from the plan's lookback."""

        as_of = self.clock().date()
        return (as_of - timedelta(days=int(self.plan["lookback_days"]))).isoformat()

    def windows(self, *, since: str | None = None) -> list[tuple[str, str]]:
        """The lookback split into bounded windows, **newest first**.

        Newest first because that is the order the work is worth doing in: a
        tick that spends its read budget on notes from five months ago and
        never reaches this morning's is worse than useless -- it looks busy.
        Bounded because one enumeration carries one bounded response, and a
        window that does not fit comes back truncated rather than as a silent
        prefix.
        """

        oldest = date.fromisoformat(since or self.window_start())
        newest = self.clock().date()
        if newest < oldest:
            return []
        step = timedelta(days=ENUMERATION_WINDOW_DAYS)
        spans: list[tuple[str, str]] = []
        end = newest
        while end >= oldest:
            begin = max(oldest, end - step + timedelta(days=1))
            spans.append((begin.isoformat(), end.isoformat()))
            if begin == oldest:
                break
            end = begin - timedelta(days=1)
        return spans

    def enumerate_via_runner(self, *, since: str, until: str) -> dict[str, Any]:
        """One window's index read, through the connector runner.

        An enumeration is a governed call like any other: it reads a source,
        it leaves an invocation behind, and its raw bytes go into the spool.
        It does not become a discovery record -- a listing is not a document
        -- but it is not an unrecorded read either.
        """

        if self.enumerator is None:
            raise FeedLaneError(
                f"{self.source_ref} has no enumeration runner; the index "
                "operation is not installed on this coordinator"
            )
        parameters = self.enumeration_parameters(since=since, until=until)
        receipt = self.enumerator.run(
            parameters=parameters,
            work_ref=f"work:{self.source_ref}:enumerate:{since}:{until}",
        )
        return dict(receipt.observation)

    def enumerate_window(
        self, *, since: str, until: str, depth: int = 0,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        """Every observation needed to cover one window without truncation.

        A truncated listing is split in half and both halves are asked for.
        The alternative -- accepting the prefix -- would leave the older half
        of every busy window permanently unread while the envelope claimed the
        window was enumerated. Splitting bottoms out at a single day, which no
        real day of this feed exceeds; a single day that still truncates is
        returned as it is, honestly marked ``partial``, because there is no
        smaller window to ask for.
        """

        if deadline is not None and time.monotonic() >= deadline:
            return []
        observation = self.enumerate_via_runner(since=since, until=until)
        if observation.get("next_cursor") is None:
            return [observation]
        begin, end = date.fromisoformat(since), date.fromisoformat(until)
        if begin >= end or depth >= MAX_WINDOW_SPLITS:
            return [observation]
        middle = begin + (end - begin) // 2
        return (
            self.enumerate_window(
                since=(middle + timedelta(days=1)).isoformat(), until=until,
                depth=depth + 1, deadline=deadline,
            )
            + self.enumerate_window(
                since=since, until=middle.isoformat(), depth=depth + 1,
                deadline=deadline,
            )
        )

    def enumeration_parameters(self, *, since: str, until: str) -> dict[str, Any]:
        """Both feeds take the same bounded window; neither takes a query."""

        return {"since": since, "until": until}

    def enumeration_fingerprint(self, *, since: str, until: str) -> str:
        """What would have to change before a kept listing is the wrong answer.

        Not the contents of the listing -- those cannot be checked without
        taking it again, which is the cost the cache exists to avoid.  This is
        the *shape* of the question: which feed, under which frozen plan, over
        which window, split how.  A plan the owner re-signs, a lookback that
        moved, a window size that changed: each one makes every kept listing
        an answer to a question nobody asked, and each one changes this.
        """

        return content_hash({
            "source_ref": self.source_ref,
            "plan_hash": self.plan.get("content_hash"),
            "parameters": self.enumeration_parameters(since=since, until=until),
            "window_days": ENUMERATION_WINDOW_DAYS,
        })

    def cached_enumeration(
        self, *, since: str, until: str
    ) -> list[dict[str, Any]] | None:
        """A live listing for this window, or ``None`` to go and take one."""

        if self.enumeration_cache is None:
            return None
        try:
            return self.enumeration_cache.get(
                since=since, until=until,
                fingerprint=self.enumeration_fingerprint(since=since, until=until),
            )
        except Exception:  # noqa: BLE001 - a cache miss is never a lane failure
            return None

    def has_enumeration(self, *, since: str, until: str) -> bool:
        """Whether this window can be read from this tick without a child."""

        if self.enumeration_cache is None:
            return False
        try:
            return bool(self.enumeration_cache.has(
                since=since, until=until,
                fingerprint=self.enumeration_fingerprint(since=since, until=until),
            ))
        except Exception:  # noqa: BLE001 - a cache miss is never a lane failure
            return False

    def cache_enumeration(
        self, *, since: str, until: str,
        observations: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Keep this window's listing. ``True`` when the next tick will find it."""

        if self.enumeration_cache is None or not observations:
            return False
        try:
            self.enumeration_cache.put(
                since=since, until=until,
                fingerprint=self.enumeration_fingerprint(since=since, until=until),
                observations=observations,
            )
        except Exception:  # noqa: BLE001 - see above
            return False
        return True

    def company_names(
        self, universe: Sequence[Mapping[str, Any]]
    ) -> dict[str, tuple[str, ...]]:
        """This run's ticker-to-names table, from this mission's own plan.

        Built here rather than inside each attribution call so that one tick
        answers "what is MSFT called" once, and so that every decision in that
        tick answers it the same way.
        """

        return plan_company_names(universe, self.plan)

    def triage(
        self, observation: Mapping[str, Any], universe: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        names = self.company_names(universe)
        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return triage_notes(observation["notes"], universe, self.plan, names)
        if self.source_ref == PRIOR_RESEARCH_SOURCE_REF:
            return triage_prior_documents(observation["documents"], universe,
                                          self.plan, names)
        return triage_wiki_documents(observation["documents"], universe,
                                     self.plan, names)

    def headers_by_document(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return {note["note_id"]: note for note in observation["notes"]}
        return {item["document_id"]: item for item in observation["documents"]}

    # -- grants ---------------------------------------------------------

    def authorize(self, *, company_ref: str, requested_by: str | None = None) -> dict[str, Any]:
        """The mission's own grant check, unchanged.

        This is the check that refuses a feed the mission has not marked
        ``connected`` and an automation the mission has not granted both
        ``source_discovery`` and ``observation``. It is not re-implemented
        here; it is called.
        """

        return self.missions.authorize_source_discovery(
            company_ref=company_ref,
            source_ref=self.source_ref,
            requested_by=requested_by or self.automation_principal,
        )

    # -- enumeration ----------------------------------------------------

    def enumerate(self, *, caller_ref: str | None = None, **parameters: Any) -> dict[str, Any]:
        """Run the governed list child once and return its observation.

        The enumeration goes through the child, not through an in-process
        read, because that is where the governance record is checked and the
        raw bytes are spooled. A lane that read the directory itself would be
        a second, ungoverned way to reach the same files.
        """

        ticket = self.launcher.start_enumeration(
            caller_ref=caller_ref or self.automation_principal, **parameters
        )
        self.launcher.wait(timeout=self.acquisition_wait_seconds)
        settled = self.launcher.status(ticket["id"])
        summary = settled.get("summary")
        if settled["status"] != "succeeded" or not isinstance(summary, Mapping):
            reason = (summary or {}).get("failure_reason") if isinstance(summary, Mapping) else None
            raise FeedLaneError(
                f"{self.source_ref} enumeration {settled['status']}: {reason or 'no summary'}"
            )
        return dict(summary["observation"])

    def attribute(
        self, observation: Mapping[str, Any], universe: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        names = self.company_names(universe)
        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return attribute_notes(observation["notes"], universe, names)
        if self.source_ref == PRIOR_RESEARCH_SOURCE_REF:
            return attribute_prior_documents(observation["documents"], universe, names)
        return attribute_wiki_documents(observation["documents"], universe, names)

    def spec_refs(self, observation: Mapping[str, Any]) -> dict[str, str]:
        """Document ref to spec ref, which is what carries the evidence tier."""

        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return {note["note_id"]: SALES_NOTE_SPEC_REF for note in observation["notes"]}
        if self.source_ref == PRIOR_RESEARCH_SOURCE_REF:
            return {
                document["document_id"]: PRIOR_RESEARCH_SPEC_REF
                for document in observation["documents"]
            }
        return {
            document["document_id"]: wiki_spec_ref(document["doc_type_key"])
            for document in observation["documents"]
        }

    # -- recording ------------------------------------------------------

    def record_document(
        self,
        *,
        company_ref: str,
        document_ref: str,
        spec_ref: str,
        receipt: Mapping[str, Any],
        parameters: Mapping[str, Any],
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """One discovery record for one document read for one company.

        The envelope the receipt names carries exactly this document, which is
        what ``record_source_discovery`` checks; a document already in the
        mission's queue is partitioned out as in-authority rather than queued
        again, so a second pass over a feed that grew by one note discovers
        one note.
        """

        authorization = self.authorize(company_ref=company_ref, requested_by=requested_by)
        known = self._documents_for(
            authorization["mission_version_ref"], company_ref=company_ref
        )
        return self.missions.record_source_discovery(
            authorization=authorization,
            discovery_plan_ref=self.plan["id"],
            discovery_plan_hash=self.plan["content_hash"],
            spec_ref=spec_ref,
            query_hash=feed_query_hash(self.source_ref, parameters),
            parameters=dict(parameters),
            connector_invocation_ref=receipt["connector_invocation_ref"],
            connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"],
            source_envelope_hash=receipt["source_envelope_hash"],
            document_refs=[document_ref],
            in_authority_document_refs=[document_ref] if document_ref in known else [],
        )

    def resolve_documents(
        self,
        *,
        queue: Sequence[str],
        universe: Sequence[Mapping[str, Any]],
        headers: Mapping[str, Mapping[str, Any]],
        header_company: Mapping[str, Sequence[str]],
        since: str,
        known: Sequence[str] = (),
        limit: int | None = None,
        requested_by: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Read a bounded batch of documents and decide what each one is.

        This is the tick's real work and the order matters: the document is
        read once, by the connector runner, and that single read produces the
        raw response the envelope binds, the manifest the review path will
        re-verify, and the text the attribution is decided from. Reading it
        twice -- once to decide and once to acquire -- would be two
        acquisitions of the same bytes with two different hashes to explain.
        """

        if self.runner is None:
            raise FeedLaneError(
                "reading a feed document needs the host-tool runner; this "
                "coordinator was built without one"
            )
        header_by_document: dict[str, list[str]] = {}
        for company_ref, refs in header_company.items():
            for ref in refs:
                header_by_document.setdefault(ref, []).append(company_ref)
        bound = self.body_reads_per_tick if limit is None else int(limit)
        # A document the mission already holds is not read again. That is the
        # duplicate suppression that matters: the feed grows at the end, so a
        # second tick over the same window should cost one read per new note,
        # not one per note. Documents that were read and *not* queued -- the
        # industry-level ones and the dropped ones -- are re-read, because the
        # ledger has no row saying "looked, kept nothing"; the per-tick bound
        # is what keeps that finite.
        held = set(known)
        pending = [ref for ref in queue if ref not in held]
        # Industry/dropped documents have no per-company discovery row by
        # design, so they remain pending. Resume after the last attempted ref
        # instead of letting the same small prefix starve the rest forever.
        # This is only scheduling state; it does not claim the document was
        # admitted to the mission and a process restart may safely re-read it.
        if self.body_read_cursor_ref in pending:
            pivot = pending.index(self.body_read_cursor_ref) + 1
            pending = pending[pivot:] + pending[:pivot]
        outcomes: list[dict[str, Any]] = []
        spent = 0.0
        for document_ref in pending[: max(0, bound)]:
            if deadline is not None:
                # I2: a read that cannot finish inside the budget is not
                # started.  The writer's lane budget is soft -- an overrun is
                # reported, not interrupted -- so a lane that starts a
                # nine-tenths-of-a-second child a tenth of a second before its
                # deadline is reported ``busy`` every single tick, which is
                # how an owner reads a stalled lane.  Live, that was every
                # tick of both feeds: five reads, the last one crossing the
                # line, ``over_budget_seconds: 5.65`` against a budget of 5.
                #
                # The estimate is this tick's own measured cost rather than a
                # constant, because the two feeds and the two corpora differ
                # by more than any constant would survive.  The first read is
                # always allowed: a tick that reads one document has made
                # progress, and there is nothing to estimate from yet.
                if not outcomes:
                    if time.monotonic() >= deadline:
                        break
                elif time.monotonic() + spent / len(outcomes) > deadline:
                    break
            mark = time.monotonic()
            outcome = self._resolve_one(
                document_ref=document_ref,
                universe=universe,
                header=headers.get(document_ref) or {},
                header_companies=header_by_document.get(document_ref, ()),
                since=since,
                requested_by=requested_by,
            )
            spent += time.monotonic() - mark
            outcomes.append(outcome)
            # A connector quota is shared by every remaining document. Once
            # the connector proves it exhausted, another 49 calls can only
            # create 49 identical failure receipts. Keep the remaining refs
            # pending for the next quota window.
            if outcome.get("reason_code") == "connector_quota_exhausted":
                break
            self.body_read_cursor_ref = document_ref
        return {
            "source_ref": self.source_ref,
            "read": len(outcomes),
            "already_held": len(queue) - len(pending),
            "outcomes": outcomes,
            "company": sum(1 for item in outcomes if item["outcome"] == "company"),
            "industry": sum(1 for item in outcomes if item["outcome"] == "industry"),
            "dropped": sum(1 for item in outcomes if item["outcome"] == "dropped"),
            "failed": sum(1 for item in outcomes if item["outcome"] == "failed"),
        }

    def _resolve_one(
        self,
        *,
        document_ref: str,
        universe: Sequence[Mapping[str, Any]],
        header: Mapping[str, Any],
        header_companies: Sequence[str],
        since: str,
        requested_by: str | None,
    ) -> dict[str, Any]:
        digest = content_hash({
            "source_ref": self.source_ref, "document_ref": document_ref,
        })[:24]
        record = {
            "source_ref": self.source_ref,
            "operation": self.launcher.GET_OPERATION,
            "document_ref": document_ref,
            "actor_ref": self.automation_principal,
        }
        ticket_id, ticket_dir = self.launcher.prepare_run(digest=digest, record=record)
        parameters = self.document_parameters(document_ref, header)
        try:
            receipt = self.runner.run(
                parameters=parameters,
                work_ref=f"work:{self.source_ref}:{digest}",
                output_dir=ticket_dir,
            )
        except ConnectorQuotaExceeded as exc:
            self.launcher.settle_run(
                ticket_id, status="failed", exit_code=1,
                failure_reason=f"{type(exc).__name__}: {exc}"[:500],
            )
            return {"document_ref": document_ref, "outcome": "failed",
                    "reason_code": "connector_quota_exhausted",
                    "reason": f"{type(exc).__name__}: {exc}"[:500]}
        except Exception as exc:  # noqa: BLE001 - one document, reported not raised
            self.launcher.settle_run(
                ticket_id, status="failed", exit_code=1,
                failure_reason=f"{type(exc).__name__}: {exc}"[:500],
            )
            return {"document_ref": document_ref, "outcome": "failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:500]}
        self.launcher.settle_run(ticket_id, status="succeeded", exit_code=0)
        decision = self.decide(receipt.observation, universe,
                               header_companies=header_companies)
        result = {
            "document_ref": document_ref,
            "ticket_ref": ticket_id,
            "outcome": decision["outcome"],
            "company_refs": decision["company_refs"],
            "industry_terms": decision["industry_terms"],
            "reason": decision["reason"],
            "records": [],
        }
        if decision["outcome"] != "company":
            # Kept visible in the tick rather than queued: the mission ledger
            # has no row for "about the industry, about no company", and
            # inventing a company to hold it is the one thing the owner said
            # not to do.
            return result
        spec_ref = self.document_spec_ref(receipt.observation)
        for company_ref in decision["company_refs"]:
            recorded = self.record_document(
                company_ref=company_ref, document_ref=document_ref,
                spec_ref=spec_ref, receipt=receipt.to_dict(),
                # The discovery's parameters are the window this lane looked
                # at for this company, not the argv that read one file. What
                # read the file is bound by the call spec inside the
                # invocation the envelope already names, and describing the
                # window is what makes two discoveries comparable.
                parameters=self.company_window(company_ref, since=since),
                requested_by=requested_by,
            )
            # The row the queue keyed by this document, which is not the
            # discovery record's own id: one discovery names one document,
            # but the queue row is keyed by (mission version, document) so
            # that two companies' discoveries of the same note are one row.
            row = self._queued_row(
                recorded["mission_version_ref"], document_ref, company_ref
            )
            if row is None:
                raise FeedLaneError(
                    "the discovery was recorded but its document is not in the queue"
                )
            if row["status"] == "discovered":
                self.missions.mark_discovered_document_launched(row["record_id"], ticket_id)
                self.missions.settle_discovered_document(row["record_id"], status="acquired")
            self.missions.register_document_review(
                row["record_id"], requested_by=self.automation_principal
            )
            result["records"].append(row["record_id"])
        return result

    def documents_in_authority(self) -> set[str]:
        """Every document of this feed the mission already holds.

        Paged, because the reader caps a page and a mission that has been
        running for a while holds more documents than one page. An unpaged
        read here would silently stop reporting held documents past the cap
        and the lane would re-read them for ever.
        """

        version = self.missions.active_mission(self.plan["mission_ref"])
        held: set[str] = set()
        # One bucket per (company, status). The reader has no cursor, so the
        # only way to stay under its page cap is to ask narrower questions,
        # and the plan already knows which companies this lane covers.
        for company_ref in self.companies:
            held |= self._documents_for(version["id"], company_ref=company_ref)
        return held

    def _documents_for(
        self, mission_version_ref: str, *, company_ref: str | None = None
    ) -> set[str]:
        """Every discovered-document ref of this feed, one query per status.

        The reader caps a page at a thousand rows and offers no cursor, so a
        single call is a page and not an answer. Splitting by status -- and by
        company where the caller knows it -- keeps every query far under that
        cap for a mission of any plausible size, and a bucket that reaches it
        raises rather than quietly reporting fewer held documents than there
        are: under-reporting here means re-reading documents for ever.
        """

        held: set[str] = set()
        for status in DISCOVERED_DOCUMENT_STATUSES:
            page = self.missions.discovered_documents(
                mission_version_ref, company_ref=company_ref, status=status,
                limit=DOCUMENT_PAGE_LIMIT,
            )
            if len(page) == DOCUMENT_PAGE_LIMIT:
                raise FeedLaneError(
                    f"{status} documents for this mission fill a whole page; "
                    "the queue reader has no cursor, so this lane cannot see "
                    "past it -- narrow the mission or add paging to the reader"
                )
            held.update(
                row["document_ref"] for row in page
                if row["source_ref"] == self.source_ref
            )
        return held

    def _queued_row(
        self, mission_version_ref: str, document_ref: str, company_ref: str
    ) -> Any:
        """The queue row for one document, narrowed rather than scanned.

        Reading one capped page and giving up is a bug that only appears once
        a mission holds more documents than a page -- and it appears at the
        worst possible moment, after ``record_source_discovery`` has already
        committed, leaving the row stuck at ``discovered`` and the tick
        failing on every pass. Filtering by company and by status keeps every
        query small and covers every row.
        """

        for status in DISCOVERED_DOCUMENT_STATUSES:
            for row in self.missions.discovered_documents(
                mission_version_ref, company_ref=company_ref, status=status,
                limit=DOCUMENT_PAGE_LIMIT,
            ):
                if row["document_ref"] == document_ref:
                    return row
        return None

    # -- per-source shapes ----------------------------------------------

    def document_parameters(
        self, document_ref: str, header: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.source_ref == SALES_NOTES_SOURCE_REF:
            parameters: dict[str, Any] = {"note_id": document_ref}
            hint = header.get("digest_ref")
            if isinstance(hint, str) and hint:
                parameters["digest_ref"] = hint
            return parameters
        return {"document_id": document_ref}

    def company_window(self, company_ref: str, *, since: str) -> dict[str, Any]:
        entry = self.companies.get(company_ref)
        if entry is None:
            raise FeedLaneRejected(
                f"{company_ref} is covered by the mission but not by the feed plan"
            )
        return feed_discovery_parameters(
            terms=entry["search_terms"], since=since, as_of=self.clock().date()
        )

    def document_spec_ref(self, observation: Mapping[str, Any]) -> str:
        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return SALES_NOTE_SPEC_REF
        if self.source_ref == PRIOR_RESEARCH_SOURCE_REF:
            return PRIOR_RESEARCH_SPEC_REF
        return wiki_spec_ref(observation["document"]["doc_type_key"])

    def decide(
        self,
        observation: Mapping[str, Any],
        universe: Sequence[Mapping[str, Any]],
        *,
        header_companies: Sequence[str] = (),
    ) -> dict[str, Any]:
        """What one read document is: a company's, the industry's, or neither."""

        if self.source_ref == SALES_NOTES_SOURCE_REF:
            return attribute_body(
                observation["body"], universe, self.plan,
                header_companies=header_companies,
                names=self.company_names(universe),
            )
        if self.source_ref == PRIOR_RESEARCH_SOURCE_REF:
            # The company folder is the attribution and there is no second
            # candidate: the owner put this file in one company's folder. A
            # document under a folder outside the universe is not read for
            # industry terms either -- prior work about a company we do not
            # cover is prior work about a company we do not cover.
            ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"])
                             for item in universe}
            ticker = prior_research_ticker(observation["document"])
            if ticker in ticker_to_ref:
                return {"outcome": "company", "company_refs": [ticker_to_ref[ticker]],
                        "industry_terms": [], "reason": None}
            terms = mentions_any(observation["text"], plan_terms(self.plan))
            if terms:
                return {"outcome": "industry", "company_refs": [],
                        "industry_terms": terms, "reason": None}
            return {"outcome": "dropped", "company_refs": [], "industry_terms": [],
                    "reason": "filed under a folder outside the universe and names "
                              "no industry term"}
        # The wiki's company attribution is the corpus's own filing, not a
        # regex over the prose: a sector note that mentions Accenture twice is
        # still a sector note. Only the industry-or-dropped half is decided
        # from the text.
        document = observation["document"]
        ticker_to_ref = {str(item["ticker"]).strip(): str(item["company_ref"])
                         for item in universe}
        tagged = [ticker_to_ref[tag] for tag in document["company_tags"]
                  if tag in ticker_to_ref]
        if tagged:
            return {"outcome": "company", "company_refs": sorted(set(tagged)),
                    "industry_terms": [], "reason": None}
        terms = mentions_any(observation["text"], plan_terms(self.plan))
        if terms:
            return {"outcome": "industry", "company_refs": [],
                    "industry_terms": terms, "reason": None}
        return {"outcome": "dropped", "company_refs": [], "industry_terms": [],
                "reason": "carries no coverage tag and names no industry term"}

    # -- acquisition ----------------------------------------------------

    def settle_documents(self) -> list[dict[str, Any]]:
        """Turn finished acquisition children into acquired rows and reviews."""

        settled: list[dict[str, Any]] = []
        for row in self.missions.launched_discovered_documents(
            source_ref=self.source_ref, limit=self.acquisitions_per_tick
        ):
            ticket_ref = row["ticket_ref"]
            if not ticket_ref:
                continue
            status = self.launcher.status(ticket_ref)
            if status["status"] == "running":
                continue
            if status["status"] != "succeeded":
                settled.append(self.missions.settle_discovered_document(
                    row["record_id"], status="acquisition_failed",
                    reason=f"child {status['status']}"[:500],
                ))
                continue
            try:
                self.launcher.read_completed_manifest(ticket_ref, row["document_ref"])
            except Exception as exc:  # noqa: BLE001 - a disagreement is a failure
                settled.append(self.missions.settle_discovered_document(
                    row["record_id"], status="acquisition_failed",
                    reason=f"{type(exc).__name__}: {exc}"[:500],
                ))
                continue
            settled.append(self.missions.settle_discovered_document(
                row["record_id"], status="acquired",
            ))
            self.missions.register_document_review(
                row["record_id"], requested_by=self.automation_principal
            )
        return settled

    def launch_acquisition(self) -> dict[str, Any]:
        """Start at most one acquisition child."""

        busy = self.missions.launched_discovered_documents(
            source_ref=self.source_ref, limit=1
        )
        if busy:
            return {"status": "busy"}
        row = self.missions.next_discovered_document(source_ref=self.source_ref)
        if row is None:
            return {"status": "idle"}
        ticket = self.launcher.start_bounded_probe(
            document_ref=row["document_ref"], caller_ref=self.automation_principal
        )
        self.missions.mark_discovered_document_launched(row["record_id"], ticket["id"])
        return {"status": "launched", "ticket_ref": ticket["id"],
                "document_ref": row["document_ref"]}

    def dispatch_once(self, *, universe: Sequence[Mapping[str, Any]] | None = None,
                      since: str | None = None) -> dict[str, Any]:
        """One tick: settle what is outstanding, then walk windows newest first.

        Settling first is not cosmetic. A tick that launched before settling
        would find the single slot occupied by its own previous child and
        report ``busy`` forever.

        The windows are walked newest first and the read budget is spent as it
        goes, so the newest unread material is always what a tick reads. Older
        windows are still enumerated when the budget runs out -- enumerating is
        one cheap local read and the counts are worth having -- but nothing is
        read from them, and the next tick, with those documents now held,
        reaches further back. That is how the whole lookback gets covered
        without any tick being unbounded.

        WP-I put one thing in front of that order: a window whose listing is
        already on disk is walked before one that would have to be bought.
        Enumerating is not "one cheap local read" in production -- it is a
        governed child process, about a quarter of the five seconds the writer
        gives this lane -- and buying a listing while an already-paid-for one
        still holds unread notes spends that quarter finding work the lane
        already had.  So reading comes first and a new listing is bought when
        reading finishes early.  ``tick_windows`` is that order;
        ``FeedEnumerationCache`` is what makes the listing outlive its tick.

        Without a runner the tick still does the queue half -- acquiring rows
        some other path discovered -- and says so, rather than failing a
        controller tick because one lane is not fully installed.
        """

        results: dict[str, Any] = {
            "source_ref": self.source_ref, "settled": [], "launched": [],
            "read": None, "status": "idle",
        }
        started = time.monotonic()
        deadline = started + self.tick_budget_seconds
        # The enumeration deadline is the one that moved.  Reading keeps the
        # whole tick, and is additionally guaranteed a floor, so a slow
        # enumeration costs this tick a window rather than every document.
        enumeration_deadline = max(
            started + 0.5, deadline - READ_RESERVE_SECONDS)
        results["settled"].extend(self.settle_documents())
        if self.runner is not None and universe:
            held = self.documents_in_authority()
            budget = self.body_reads_per_tick
            enumerated = 0
            windows = 0
            partial_windows = 0
            enumerations = 0
            cache_hits = 0
            enumeration_seconds = 0.0
            read_seconds = 0.0
            deferred = False
            reads: list[dict[str, Any]] = []
            for window_since, window_until, kept in self.tick_windows(since=since):
                if budget <= 0:
                    break
                if time.monotonic() >= deadline:
                    results["out_of_time"] = True
                    break
                # I1: the listing first, from disk if a recent tick already
                # paid for it.  A hit costs a file read and no child process,
                # which is the whole point: the tick's budget then belongs to
                # reading, which is the half that produces documents.
                observations = (
                    self.cached_enumeration(since=window_since, until=window_until)
                    if kept else None
                )
                repeatable = observations is not None
                if observations is None:
                    if ((self.enumerations_per_tick is not None
                            and enumerations >= self.enumerations_per_tick)
                            or time.monotonic() >= enumeration_deadline):
                        results["out_of_time"] = time.monotonic() >= deadline
                        break
                    enumerations += 1
                    # Advance before the read, not after: a tick that spends
                    # its whole read budget on this window must still start at
                    # the next one, or the lane re-enumerates the same
                    # fortnight forever and never reaches the rest of the
                    # lookback.
                    self.enumeration_cursor_ref = window_until
                    mark = time.monotonic()
                    observations = self.enumerate_window(
                        since=window_since, until=window_until,
                        deadline=enumeration_deadline,
                    )
                    enumeration_seconds += time.monotonic() - mark
                    repeatable = self.cache_enumeration(
                        since=window_since, until=window_until,
                        observations=observations,
                    )
                else:
                    cache_hits += 1
                    self.enumeration_cursor_ref = window_until
                for observation in observations:
                    windows += 1
                    if observation.get("next_cursor") is not None:
                        partial_windows += 1
                    headers = self.headers_by_document(observation)
                    enumerated += len(headers)
                    if budget <= 0:
                        continue
                    # I2: a read that cannot start inside the budget the
                    # writer gave this lane.  There are two honest answers and
                    # which one is right depends on whether this tick's work
                    # survives it.  If the listing is now on disk, stopping
                    # costs nothing -- the next tick starts with a cache hit,
                    # spends the whole budget reading, and launches.  If it is
                    # not, stopping would starve the lane exactly as before,
                    # so the read keeps its floor and the tick overruns.
                    if time.monotonic() + MIN_DOCUMENT_READ_SECONDS > deadline:
                        results["out_of_time"] = True
                        if repeatable and enumerations:
                            # ``enumerations`` is the guarantee.  Deferring is
                            # only ever right when this tick is the one that
                            # paid for the listing, so the next tick is
                            # cheaper by exactly what this one spent.  A tick
                            # whose windows all came from cache has no cheaper
                            # successor to hand the work to, so it reads --
                            # otherwise a writer budget too small for one read
                            # would defer for ever and launch nothing.
                            deferred = True
                            break
                        read_deadline = (
                            time.monotonic() + MIN_DOCUMENT_READ_SECONDS
                        )
                    else:
                        read_deadline = deadline
                    triage = self.triage(observation, universe)
                    mark = time.monotonic()
                    read = self.resolve_documents(
                        queue=triage["read_queue"], universe=universe,
                        headers=headers, header_company=triage["header_company"],
                        since=window_since, known=held, limit=budget,
                        deadline=read_deadline,
                    )
                    read_seconds += time.monotonic() - mark
                    reads.append(read)
                    budget -= read["read"]
                    held.update(
                        item["document_ref"] for item in read["outcomes"]
                        if item["outcome"] == "company"
                    )
                if deferred:
                    break
            results["read"] = _merge_reads(self.source_ref, reads)
            # Every document this tick read is a governed child this tick
            # started, so it belongs in the same count as an acquisition
            # launch.  It was missing, and the ledger consequently reported
            # ``launched: 0`` on ticks that had read and recorded documents --
            # the one number an owner watches to see whether the lane works.
            results["launched"].extend(
                {"status": "launched", "ticket_ref": outcome["ticket_ref"],
                 "document_ref": outcome["document_ref"]}
                for read in reads for outcome in read["outcomes"]
                if outcome.get("ticket_ref")
            )
            results["enumerated"] = enumerated
            results["windows"] = windows
            results["partial_windows"] = partial_windows
            results["enumerations"] = enumerations
            results["cache_hits"] = cache_hits
            results["enumeration_seconds"] = round(enumeration_seconds, 2)
            results["read_seconds"] = round(read_seconds, 2)
            results["read_deferred"] = deferred
        else:
            for _ in range(self.acquisitions_per_tick):
                if time.monotonic() >= deadline:
                    results["out_of_time"] = True
                    break
                outcome = self.launch_acquisition()
                if outcome["status"] != "launched":
                    break
                results["launched"].append(outcome)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    results["out_of_time"] = True
                    break
                try:
                    self.launcher.wait(
                        timeout=min(self.acquisition_wait_seconds, remaining)
                    )
                except subprocess.TimeoutExpired:
                    # The child and its durable ticket remain open.  The next
                    # tick settles them before launching more work; waiting
                    # past this operation's budget would monopolize the
                    # writer's single store executor and starve every lane.
                    results["out_of_time"] = True
                    break
                results["settled"].extend(self.settle_documents())
        if results["launched"] or (results["read"] and results["read"]["read"]):
            results["status"] = "dispatched"
        elif results["settled"]:
            results["status"] = "settled"
        return results


# -- host-tool runners --------------------------------------------------

FEED_IDENTITY = {
    SALES_NOTES_SOURCE_REF: ("sales-notes", "sales_notes_core", "sales_notes_identity"),
    COMPANY_WIKI_SOURCE_REF: ("company-wiki", "company_wiki_core", "company_wiki_identity"),
    PRIOR_RESEARCH_SOURCE_REF: (
        "prior-research", "prior_research_core", "prior_research_identity"
    ),
}


def feed_identity(source_ref: str, operation: str) -> dict[str, Any]:
    """The frozen identity of one operation of one feed."""

    import importlib

    try:
        _, module_name, function_name = FEED_IDENTITY[source_ref]
    except KeyError as exc:
        raise FeedLaneRejected(f"{source_ref} is not a feed") from exc
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, function_name)(operation)


def build_feed_runner(
    *,
    launcher: Any,
    operation: str,
    governance: Any,
    store: Any,
    connectors: Any,
    observability: Any,
    spool: Any,
    source_ref: str,
    actor_ref: str = "automation:coverage-mission",
    clock: Callable[[], datetime] | None = None,
    timeout_seconds: float | None = None,
) -> Any:
    """A host-tool runner bound to one feed operation and its child command.

    The runner stays generic: everything feed-specific -- which template, which
    identity, which argv -- is supplied here, which is exactly what another
    host-tool connector has to supply to reuse it.
    """

    from .host_tool_runner import HostToolRunner

    template_key = FEED_IDENTITY[source_ref][0]

    def command(parameters: Mapping[str, Any], output_dir: Path,
                context: Mapping[str, str]) -> list[str]:
        return launcher.child_command(
            operation=operation, output_dir=output_dir, context=context,
            **dict(parameters)
        )

    kwargs = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    return HostToolRunner(
        store=store, connectors=connectors, observability=observability, spool=spool,
        template_key=template_key,
        identity=feed_identity(source_ref, operation),
        governance=governance,
        command=command,
        connector_slug=template_key,
        actor_ref=actor_ref,
        clock=clock,
        **kwargs,
    )


# -- lane registration --------------------------------------------------

SALES_NOTES_LAUNCHER_KWARG = "sales_notes_feed_launcher"
COMPANY_WIKI_LAUNCHER_KWARG = "company_wiki_feed_launcher"
SALES_NOTES_GOVERNANCE = ("sales-notes-list-notes-v1.json", "sales-notes-get-note-v1.json")
COMPANY_WIKI_GOVERNANCE = (
    "company-wiki-list-documents-v1.json", "company-wiki-get-document-v1.json",
)


def _mission_universe(server: Any) -> list[dict[str, Any]]:
    """The covered companies, from the mission the writer is running.

    Read off the mission pointer rather than by name: this lane used to ask
    for ``coverage-mission:us-it-services`` literally, which the legacy
    environment has and every workspace does not, so in a new environment all
    three feed lanes answered "requested object was not found" every tick
    (Hyperscaler, live 2026-09-18) even after their inputs were installed.
    """

    from .coverage_mission import CoverageMissionNotFound

    pointer = server.store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer "
        "ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if pointer is None:
        raise CoverageMissionNotFound("this Core has no active coverage mission")
    version = server.coverage_mission.mission(pointer["mission_version_id"])
    return [dict(item) for item in version["universe"]]


def _dispatch(server: Any, source_ref: str, launcher_kwarg: str) -> dict[str, Any]:
    launcher = server.lane_launcher(launcher_kwarg)
    if launcher is None:
        return {"status": "unconfigured", "reason": f"no {source_ref} lane on this writer"}
    # Preflight the one thing this lane cannot install for itself. Until the
    # mission authority knows the feed is a discovery source, every read this
    # tick performs is quota spent on documents that will be refused at
    # ``record_source_discovery`` -- so it is checked before the first child
    # starts, not after fifty of them have run.
    from .coverage_mission import DISCOVERY_SOURCES

    if source_ref not in DISCOVERY_SOURCES:
        return {
            "status": "unconfigured",
            "reason": (
                f"{source_ref} is not registered in coverage_mission."
                "DISCOVERY_SOURCES; the mission authority cannot bind a "
                "discovery to it yet"
            ),
        }
    plan_path = getattr(launcher, "feed_plan_path", None)
    if plan_path is None:
        return {"status": "unconfigured", "reason": "no feed discovery plan"}
    spool = getattr(server, "_transcript_spool", None)
    connectors = getattr(server, "_connectors", None)
    if spool is None or connectors is None:
        return {"status": "unconfigured",
                "reason": "this writer has no connector spool for feed acquisition"}
    plan = load_feed_discovery_plan(plan_path)
    from .writer_server import lane_budget_remaining

    # B1-2: the lane's own budget, but never more than the writer is willing
    # to have the single store thread held for.  Outside the writer -- a test,
    # a CLI -- there is no writer budget and the lane keeps its own.
    tick_budget_seconds = float(
        getattr(launcher, "feed_tick_budget_seconds", TICK_BUDGET_SECONDS)
    )
    writer_budget = lane_budget_remaining(server)
    if writer_budget is not None:
        # I2: the writer's number is the real one.  The lane's own eight
        # seconds is a bound on a CLI run; inside the writer the budget is
        # whatever is left of the share of the single store thread this tick
        # was given, and a lane that plans against eight when it has five
        # plans to overrun.  The floor is half a second so that a lane called
        # with a spent budget still settles what is outstanding.
        tick_budget_seconds = max(0.5, min(tick_budget_seconds, writer_budget))
    from .feed_launcher import FeedLaunchRejected

    operations = (
        ("enumerator", launcher.LIST_OPERATION), ("runner", launcher.GET_OPERATION),
    )
    governance = {}
    for name, operation in operations:
        try:
            governance[name] = launcher.load_governance(operation)
        except FeedLaunchRejected as exc:
            # A known authority refusal is a lane hold, not an unmapped Writer
            # failure. Check BOTH operations before constructing either runner;
            # reload on the next tick so a real approval can unblock the lane.
            return {
                "status": "blocked", "reason_code": "connector_governance_rejected",
                "source_ref": source_ref, "operation": operation, "reason": str(exc),
            }
    runners = {
        name: build_feed_runner(
            launcher=launcher, operation=operation,
            governance=governance[name],
            store=server.store, connectors=connectors,
            observability=server.observability, spool=spool, source_ref=source_ref,
            timeout_seconds=feed_child_timeout_seconds(tick_budget_seconds),
        )
        for name, operation in operations
    }
    coordinator = FeedDiscoveryCoordinator(
        missions=server.coverage_mission, launcher=launcher, source_ref=source_ref,
        plan=plan,
        body_reads_per_tick=_feed_body_read_limit(source_ref, plan),
        body_read_cursor_ref=getattr(launcher, "_body_read_cursor_ref", None),
        tick_budget_seconds=tick_budget_seconds,
        enumeration_cursor_ref=getattr(launcher, "_enumeration_cursor_ref", None),
        # I1: on disk rather than on the launcher object, because the cursors
        # above are in-process only and a writer restart throws them away.  A
        # restart that also threw away the listings would put the lane back
        # into the state this work package exists to end: every tick spending
        # its budget re-deriving what the last one already knew.
        enumeration_cache=FeedEnumerationCache.for_launcher(launcher),
        **runners,
    )
    try:
        result = coordinator.dispatch_once(universe=_mission_universe(server))
    except ConnectorQuotaExceeded as exc:
        # 2026-09-15: a spent daily quota is a lane state the owner can read
        # and wait out, not an unmapped writer failure. Live, the
        # prior-research enumerator burned its 200-a-day listing a local
        # corpus once a tick and the cockpit could only say RemoteError.
        return {
            "status": "unavailable", "reason_code": "connector_quota_exhausted",
            "source_ref": source_ref, "reason": str(exc),
        }
    launcher._body_read_cursor_ref = coordinator.body_read_cursor_ref
    launcher._enumeration_cursor_ref = coordinator.enumeration_cursor_ref
    return result


def _feed_body_read_limit(
    source_ref: str, plan: Mapping[str, Any]
) -> int | None:
    """Return a transport bound without changing the signed discovery plan."""

    if "body_reads_per_tick" not in plan:
        return None
    if source_ref == COMPANY_WIKI_SOURCE_REF:
        return min(plan["body_reads_per_tick"], COMPANY_WIKI_BODY_READS_PER_TICK)
    return None


def dispatch_sales_notes(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick: read the sell-side note feed and queue what it holds."""

    return _dispatch(server, SALES_NOTES_SOURCE_REF, SALES_NOTES_LAUNCHER_KWARG)


def dispatch_company_wiki(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick: read the human wiki and queue what it holds."""

    return _dispatch(server, COMPANY_WIKI_SOURCE_REF, COMPANY_WIKI_LAUNCHER_KWARG)


def add_sales_notes_arguments(parser: Any) -> None:
    parser.add_argument("--sales-notes-digest-dir")
    parser.add_argument("--sales-notes-governance-list")
    parser.add_argument("--sales-notes-governance-get")
    parser.add_argument("--feed-discovery-plan")
    parser.add_argument(
        "--feed-tick-budget-seconds", type=float, default=TICK_BUDGET_SECONDS,
    )


def add_company_wiki_arguments(parser: Any) -> None:
    parser.add_argument("--company-wiki-index-db")
    parser.add_argument("--company-wiki-corpus-root")
    parser.add_argument("--company-wiki-governance-list")
    parser.add_argument("--company-wiki-governance-get")


def _build(launcher_class: Any, *, plan: Any, spool_dir: Any, state_dir: Any,
           governance_paths: Mapping[str, Any],
           feed_tick_budget_seconds: float = TICK_BUDGET_SECONDS,
           **feed_args: Any) -> Any | None:
    if plan is None or any(value is None for value in governance_paths.values()) \
            or any(value is None for value in feed_args.values()):
        return None
    launcher = launcher_class(
        state_dir=state_dir, governance_paths=dict(governance_paths),
        spool_dir=spool_dir, **feed_args,
    )
    # The plan travels with the launcher because the launcher is what the
    # writer stores; a lane handler has no other way back to its arguments.
    launcher.feed_plan_path = plan
    launcher.feed_tick_budget_seconds = max(0.1, float(feed_tick_budget_seconds))
    return launcher


def build_sales_notes_launcher(args: Any) -> Any | None:
    from pathlib import Path as _Path

    from .feed_launcher import SalesNotesFeedLauncher

    state = _Path(args.db).expanduser().resolve().parent
    return _build(
        SalesNotesFeedLauncher, plan=args.feed_discovery_plan,
        spool_dir=getattr(args, "transcript_spool", None), state_dir=state,
        governance_paths={
            "list_notes": args.sales_notes_governance_list,
            "get_note": args.sales_notes_governance_get,
        },
        feed_tick_budget_seconds=args.feed_tick_budget_seconds,
        digest_dir=args.sales_notes_digest_dir,
    )


def build_company_wiki_launcher(args: Any) -> Any | None:
    from pathlib import Path as _Path

    from .feed_launcher import CompanyWikiFeedLauncher

    state = _Path(args.db).expanduser().resolve().parent
    return _build(
        CompanyWikiFeedLauncher, plan=getattr(args, "feed_discovery_plan", None),
        spool_dir=getattr(args, "transcript_spool", None), state_dir=state,
        governance_paths={
            "list_documents": args.company_wiki_governance_list,
            "get_document": args.company_wiki_governance_get,
        },
        feed_tick_budget_seconds=getattr(
            args, "feed_tick_budget_seconds", TICK_BUDGET_SECONDS
        ),
        index_db=args.company_wiki_index_db, corpus_root=args.company_wiki_corpus_root,
    )


#: W7: the plan a workspace generated from its own mission universe. Looked for
#: before the packaged ``p9-us-it-services-feeds-v2.json`` because an
#: environment covering another industry must not run on that industry's plan --
#: and a file named after US IT services sitting in a hyperscaler workspace
#: would be a lie on disk even if its contents were right.
MISSION_FEED_PLAN_NAME = "mission-feeds-v1.json"


def resolve_feed_plan(state: Path, plan_name: str) -> Path:
    """The feed plan this state directory runs on, mission-generated first."""

    mission_plan = state / "feed-plans" / MISSION_FEED_PLAN_NAME
    return mission_plan if mission_plan.is_file() else state / "feed-plans" / plan_name


def _feed_argv(context: Any, *, plan_name: str, governance: Sequence[str],
               flags: Sequence[tuple[str, Path]]) -> list[str]:
    """Off unless every file this lane needs is already on this machine.

    A feed lane is enabled by the presence of its approved records, its plan
    and its source directory, the same way every connector lane here is. A
    Core without the OpenClaw workspace simply has no feed lane.
    """

    plan = resolve_feed_plan(context.state, plan_name)
    records = [context.state / "connector-governance" / name for name in governance]
    if not plan.is_file() or any(not path.is_file() for path in records):
        return []
    if any(not path.exists() for _, path in flags):
        return []
    argv = [
        "--feed-discovery-plan", str(plan),
        "--feed-tick-budget-seconds", str(TICK_BUDGET_SECONDS),
    ]
    for flag, path in flags:
        argv += [flag, str(path)]
    return argv


def sales_notes_argv(context: Any) -> list[str]:
    digests = context.state / "feeds" / "market-digest-output"
    return _feed_argv(
        context, plan_name="p9-us-it-services-feeds-v2.json",
        governance=SALES_NOTES_GOVERNANCE,
        flags=[
            ("--sales-notes-digest-dir", digests),
            ("--sales-notes-governance-list",
             context.state / "connector-governance" / SALES_NOTES_GOVERNANCE[0]),
            ("--sales-notes-governance-get",
             context.state / "connector-governance" / SALES_NOTES_GOVERNANCE[1]),
        ],
    )


def company_wiki_argv(context: Any) -> list[str]:
    corpus = context.state / "feeds" / "company-wiki"
    native_index = corpus / "wiki" / "vectors.db"
    legacy_index = corpus / "wiki-index.sqlite"
    index = native_index if native_index.exists() else legacy_index
    argv = _feed_argv(
        context, plan_name="p9-us-it-services-feeds-v2.json",
        governance=COMPANY_WIKI_GOVERNANCE,
        flags=[
            ("--company-wiki-index-db", index),
            ("--company-wiki-corpus-root", corpus),
            ("--company-wiki-governance-list",
             context.state / "connector-governance" / COMPANY_WIKI_GOVERNANCE[0]),
            ("--company-wiki-governance-get",
             context.state / "connector-governance" / COMPANY_WIKI_GOVERNANCE[1]),
        ],
    )
    # The plan flag is kept. ``lane_registry.lane_argv`` writes an identical
    # pair once, so this fragment no longer has to assume another feed lane
    # ran before it -- an assumption that was wrong the moment a lane was
    # registered ahead of this one.
    return argv


SALES_NOTES_LANE = register_lane(LaneSpec(
    operation="dispatch_sales_notes_feed",
    order=120,
    driver_key="sales_notes_feed",
    handler=dispatch_sales_notes,
    init_kwarg=SALES_NOTES_LAUNCHER_KWARG,
    argparse=add_sales_notes_arguments,
    launcher_factory=build_sales_notes_launcher,
    argv_fragment=sales_notes_argv,
    note="S1: named sell-side notes a host skill already wrote to this disk.",
))

COMPANY_WIKI_LANE = register_lane(LaneSpec(
    operation="dispatch_company_wiki_feed",
    order=130,
    driver_key="company_wiki_feed",
    handler=dispatch_company_wiki,
    init_kwarg=COMPANY_WIKI_LAUNCHER_KWARG,
    argparse=add_company_wiki_arguments,
    launcher_factory=build_company_wiki_launcher,
    argv_fragment=company_wiki_argv,
    note="S1: management minutes, expert calls and broker notes a person wrote.",
))


__all__ = [
    "ACQUISITIONS_PER_TICK",
    "ACQUISITION_RETRY_INTERVAL",
    "ACQUISITION_WAIT_SECONDS",
    "COMPANY_WIKI_GOVERNANCE",
    "COMPANY_WIKI_LANE",
    "COMPANY_WIKI_LAUNCHER_KWARG",
    "COMPANY_WIKI_SOURCE_REF",
    "DISCOVERY_SCOPE",
    "DOCUMENT_PAGE_LIMIT",
    "ENUMERATION_WINDOW_DAYS",
    "FEED_DISCOVERY_SOURCES",
    "PRIOR_RESEARCH_REF_PREFIX",
    "PRIOR_RESEARCH_SOURCE_REF",
    "MAX_WINDOW_SPLITS",
    "MISSION_FEED_PLAN_NAME",
    "FEED_IDENTITY",
    "MAX_BODY_READS_PER_TICK",
    "PLAN_SCHEMA_VERSION",
    "SALES_NOTES_GOVERNANCE",
    "SALES_NOTES_LANE",
    "SALES_NOTES_LAUNCHER_KWARG",
    "PRIOR_READ_ORDER",
    "PRIOR_RESEARCH_SPEC_REF",
    "SALES_NOTES_SOURCE_REF",
    "SALES_NOTE_SPEC_REF",
    "FeedDiscoveryCoordinator",
    "FeedLaneError",
    "FeedLaneRejected",
    "add_company_wiki_arguments",
    "add_sales_notes_arguments",
    "attribute_body",
    "attribute_notes",
    "attribute_wiki_documents",
    "build_company_wiki_launcher",
    "build_feed_runner",
    "build_sales_notes_launcher",
    "company_wiki_argv",
    "dispatch_company_wiki",
    "dispatch_sales_notes",
    "feed_discovery_parameters",
    "resolve_feed_plan",
    "feed_identity",
    "feed_query_hash",
    "load_feed_discovery_plan",
    "mentions_any",
    "plan_company_names",
    "plan_terms",
    "sales_notes_argv",
    "triage_notes",
    "triage_wiki_documents",
    "validate_feed_discovery_plan",
    "attribute_prior_documents",
    "prior_research_ticker",
    "triage_prior_documents",
    "wiki_spec_ref",
]
