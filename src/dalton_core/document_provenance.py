"""P10x: who wrote a document, and who it is about, kept instead of thrown away.

The AlphaEngine search response already answers both questions and the lane
reads one field out of it.  ``live_mcp_connector`` takes ``doc_id`` and drops
the rest, so by the time a document reaches the extraction queue the only
things known about it are the company whose search happened to return it and
the ``spec_ref`` of that search.  Two consequences, both visible live:

* **A five-vendor note belongs to one company.**  One held sell-side report --
  "Payments, Processors, and IT Services: Wells Weekly Payments Pulse" -- names
  Accenture, Cognizant, EPAM and Infosys in its own ``companies`` field.  It was
  found under a Cognizant query, so every Claim minted from it is Cognizant's
  and Accenture gets nothing.  Live, Accenture holds five sell-side Claims and
  Cognizant one hundred and forty-nine, and the difference is mostly which
  query returned the same broker's note.
* **Two notes from the same house look like two sources.**  Nothing records
  that ``sources: ["Wells Fargo Securities, LLC"]`` was the author, so a debate
  map counting "independent sell-side sources" counts documents, not brokers.

Both are fixed by keeping what the wire already said.  This module is the
reading half: pure functions over the raw search bytes and over the mission's
own universe.  It writes nothing and calls nothing.

The tier vocabulary is the one the DebateMap's independence ladder uses, and it
is derived from ``spec_ref`` -- the kind of search that found the document --
never asked of a model.  The ladder is deliberately about *what a document is*,
not how confident anyone feels about it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "0.1"

# The provenance ladder, best evidence first.  Same words the DebateMap uses so
# a tier travels from here to the independence count without translation.
TIER_FILING = "filing"
TIER_MANAGEMENT = "management"
TIER_SELL_SIDE = "sell_side"
TIER_EXPERT = "expert"
TIER_SALES_NOTE = "sales_note"
TIER_NEWS = "news"
TIER_CROWD = "crowd"
TIER_OTHER = "other"

TIERS: tuple[str, ...] = (
    TIER_FILING, TIER_MANAGEMENT, TIER_SELL_SIDE, TIER_EXPERT,
    TIER_SALES_NOTE, TIER_NEWS, TIER_CROWD, TIER_OTHER,
)

# How much a document of this tier is worth reading, best first.  The owner's
# order: what the company filed, then what management said, then what a broker
# who covers it wrote, then a sales note, then news.
EVIDENCE_VALUE: Mapping[str, int] = {tier: index for index, tier in enumerate(TIERS)}

# Which search found it decides what it is.  A spec with no entry is ``other``
# rather than a guess: an unknown kind should sort last, not be promoted.
TIER_BY_SPEC: Mapping[str, str] = {
    "annual-report-10k": TIER_FILING,
    "quarterly-report-10q": TIER_FILING,
    "company-press-release": TIER_FILING,
    "earnings-call-transcripts": TIER_MANAGEMENT,
    "sell-side-reports": TIER_SELL_SIDE,
    "expert-network-transcripts": TIER_EXPERT,
    "sales-notes": TIER_SALES_NOTE,
    "industry-demand": TIER_NEWS,
    "competitive-landscape": TIER_NEWS,
    "management-changes": TIER_NEWS,
}

# Legal dressing that is not part of a broker's identity.  "Wells Fargo
# Securities, LLC" and "Wells Fargo Securities LLC" are one house, and an
# independence count that treats them as two has counted the same opinion
# twice -- which is exactly the failure the count exists to prevent.
_BROKER_NOISE = re.compile(
    r"\b(?:llc|l\.l\.c|inc|incorporated|ltd|limited|plc|llp|lp|sa|nv|ag|gmbh|"
    r"co|corp|corporation|group|holdings|securities|research|capital|markets|"
    r"partners|international|global|and|&)\b",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def tier_for_spec(spec_ref: Any) -> str:
    """The provenance tier of a document found by this kind of search."""

    if not isinstance(spec_ref, str):
        return TIER_OTHER
    return TIER_BY_SPEC.get(spec_ref, TIER_OTHER)


def evidence_value(tier: Any) -> int:
    """How early this tier should be read; lower is sooner."""

    if not isinstance(tier, str):
        return EVIDENCE_VALUE[TIER_OTHER]
    return EVIDENCE_VALUE.get(tier, EVIDENCE_VALUE[TIER_OTHER])


def broker_key(name: Any) -> str:
    """A house's identity with the legal dressing removed.

    Not a display name: an equality key for "is this the same broker".  Two
    notes whose keys match are one source however differently the wire spelled
    the publisher.
    """

    if not isinstance(name, str):
        return ""
    folded = _NON_WORD.sub(" ", name.lower()).strip()
    folded = _BROKER_NOISE.sub(" ", folded)
    return " ".join(folded.split())


def broker_from_sources(sources: Any) -> str | None:
    """The publishing house named by the wire, verbatim, or None.

    The first named source is the publisher; later entries on the live wire are
    redistribution channels rather than authors.  Verbatim because a display
    name belongs to the source and only the *key* is normalised.
    """

    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, Sequence):
        return None
    for item in sources:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _search_payload(raw: Any) -> dict[str, Any] | None:
    """The AlphaEngine search body inside the MCP envelope, or None.

    The wire is a JSON-RPC result whose ``content`` carries a text part that is
    itself JSON.  Anything that does not decode to that shape is not a search
    response and yields nothing rather than an exception: this runs beside a
    lane that must not stop because one artefact is unreadable.
    """

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, Mapping):
        return None
    if "results" in raw and isinstance(raw.get("results"), list):
        return dict(raw)
    content = (raw.get("result") or {}).get("content") if isinstance(raw.get("result"), Mapping) else None
    if not isinstance(content, Sequence):
        return None
    for part in content:
        if not isinstance(part, Mapping) or part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(body, Mapping) and isinstance(body.get("results"), list):
            return dict(body)
    return None


def parse_search_metadata(raw: Any) -> dict[str, dict[str, Any]]:
    """What one AlphaEngine search said about each document it returned.

    Keyed by ``document_ref`` so a caller can join it straight onto the
    discovered-document rows.  Only fields the wire actually carried are
    present; a missing publisher is ``None``, never an invented one.
    """

    payload = _search_payload(raw)
    if payload is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in payload["results"]:
        if not isinstance(item, Mapping):
            continue
        doc_id = item.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            continue
        broker = broker_from_sources(item.get("sources"))
        companies = [c.strip() for c in (item.get("companies") or [])
                     if isinstance(c, str) and c.strip()]
        out[f"alphaengine-doc:{doc_id}"] = {
            "schema_version": SCHEMA_VERSION,
            "document_ref": f"alphaengine-doc:{doc_id}",
            "title": item.get("title") if isinstance(item.get("title"), str) else None,
            "broker": broker,
            "broker_key": broker_key(broker) or None,
            "sources": [s for s in (item.get("sources") or []) if isinstance(s, str)],
            "named_companies": companies,
            "published_at": item.get("publish_time") if isinstance(item.get("publish_time"), str) else None,
        }
    return out


def covered_subjects(named_companies: Any, subjects: Mapping[str, str]) -> list[str]:
    """Which covered subjects a document's own company list names.

    ``subjects`` maps a subject ref to the ticker or industry ref that
    ``document_subject`` knows how to recognise, so the naming rule here is the
    same one the attribution check already applies to a document's text --
    there is no second, looser definition of "names the company".

    The answer is ordered by ``subjects`` so it does not depend on how the
    broker happened to order its coverage list.
    """

    from .document_subject import document_names_subject

    if isinstance(named_companies, str):
        named_companies = [named_companies]
    if not isinstance(named_companies, Sequence):
        return []
    text = " | ".join(str(c) for c in named_companies)
    if not text.strip():
        return []
    found: list[str] = []
    for subject_ref, name_key in subjects.items():
        verdict = document_names_subject(text, name_key)
        if verdict.get("checked") and verdict.get("names_subject"):
            found.append(subject_ref)
    return found


def provenance_record(
    *,
    document_ref: str,
    source_ref: str,
    spec_ref: Any,
    metadata: Mapping[str, Any] | None = None,
    subjects: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One document's provenance, ready to persist.

    Deliberately tolerant of a document the search metadata never described:
    the tier still comes from the spec, and the record says honestly that no
    publisher and no company list were carried.  A row that admits it knows
    nothing is worth more than no row, because the backlog reader can then tell
    "not a sell-side note" from "nobody looked".
    """

    metadata = dict(metadata or {})
    tier = tier_for_spec(spec_ref)
    named = list(metadata.get("named_companies") or ())
    return {
        "schema_version": SCHEMA_VERSION,
        "document_ref": document_ref,
        "source_ref": source_ref,
        "spec_ref": spec_ref if isinstance(spec_ref, str) else None,
        "provenance_tier": tier,
        "broker": metadata.get("broker"),
        "broker_key": metadata.get("broker_key"),
        "title": metadata.get("title"),
        "named_companies": named,
        "covered_subjects": covered_subjects(named, subjects or {}),
        "published_at": metadata.get("published_at"),
        "metadata_seen": bool(metadata),
    }


# Which searches look for facts that belong to the market rather than to a
# company.  P13f already says so on the checklist side -- the industry has base
# items of its own, counted from these same documents -- but the extraction
# path never learned it, so a market-sizing report found by an Accenture query
# minted Accenture Claims and the industry subject holds none at all.
INDUSTRY_SPEC_REFS: frozenset[str] = frozenset({"industry-demand", "competitive-landscape"})


def subjects_for_statement(
    statement: Any,
    *,
    company_ref: str,
    company_names_key: Any,
    industry_ref: Any = None,
    extra_subjects: Mapping[str, str] | None = None,
    industry_document: bool = False,
) -> dict[str, Any]:
    """Who one drafted statement is about, in the order it should be admitted.

    The rule is the one the attribution check already uses, applied one level
    finer.  A document is attributed by naming the company; a *statement* is
    attributed by naming it too, and the extraction prompt has always invited
    statements about "its industry, its customers or its named competitors",
    so statements about somebody else were already being drafted -- they were
    simply all filed under whichever company's search found the document.

    Three outcomes, in order:

    * the statement names covered subjects -> those subjects, the review's own
      company first when it is among them;
    * it names none, and this is an industry document that names the industry
      -> the industry, which is where a fact about the market belongs;
    * otherwise -> the review's own company, exactly as before.  The fallback
      is what keeps this additive: every Claim minted today is still minted.
    """

    from .document_subject import document_names_subject

    text = statement if isinstance(statement, str) else ""
    named: list[str] = []
    verdict = document_names_subject(text, company_names_key)
    if verdict.get("checked") and verdict.get("names_subject"):
        named.append(company_ref)
    for subject_ref, name_key in (extra_subjects or {}).items():
        if subject_ref == company_ref or subject_ref in named:
            continue
        other = document_names_subject(text, name_key)
        if other.get("checked") and other.get("names_subject"):
            named.append(subject_ref)
    if named:
        return {"subjects": named, "basis": "statement_names_subject"}
    if industry_document and isinstance(industry_ref, str) and industry_ref:
        industry = document_names_subject(text, industry_ref)
        if industry.get("checked") and industry.get("names_subject"):
            return {"subjects": [industry_ref], "basis": "statement_names_industry"}
    return {"subjects": [company_ref], "basis": "review_company"}


def independent_brokers(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Distinct publishing houses among these documents, best-known name each.

    The DebateMap's ladder asks "how many independent sources say this", and
    the honest answer counts houses, not documents.  A record with no publisher
    contributes nothing rather than an anonymous unit: an unattributed note
    cannot be shown to be independent of anything.
    """

    seen: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        key = record.get("broker_key")
        name = record.get("broker")
        if not isinstance(key, str) or not key or not isinstance(name, str) or not name:
            continue
        seen.setdefault(key, name)
    return [seen[key] for key in sorted(seen)]


__all__ = [
    "EVIDENCE_VALUE",
    "INDUSTRY_SPEC_REFS",
    "SCHEMA_VERSION",
    "TIERS",
    "TIER_BY_SPEC",
    "TIER_CROWD",
    "TIER_EXPERT",
    "TIER_FILING",
    "TIER_MANAGEMENT",
    "TIER_NEWS",
    "TIER_OTHER",
    "TIER_SALES_NOTE",
    "TIER_SELL_SIDE",
    "broker_from_sources",
    "broker_key",
    "covered_subjects",
    "evidence_value",
    "independent_brokers",
    "parse_search_metadata",
    "provenance_record",
    "subjects_for_statement",
    "tier_for_spec",
]
