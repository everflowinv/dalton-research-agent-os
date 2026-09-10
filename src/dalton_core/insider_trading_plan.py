"""Strict Item 5 trading-arrangement events from archived SEC 10-Q text."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from .buyback_disclosure import buyback_documents
from .store import content_hash

_DATE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}"
_NEGATIVE = re.compile(
    r"\b(?:no|none of (?:our|the company(?:'s)?))\b.{0,120}\b(?:adopted|terminated)\b",
    re.IGNORECASE,
)
_PLAN_WORDS = re.compile(r"(?:Rule\s+10b5-1|non-Rule\s+10b5-1).{0,80}trading arrangement", re.I)
_ACTION = re.compile(r"\b(adopted|terminated)\b", re.I)
_ON_PREFIX = re.compile(rf"^On\s+({_DATE}),\s+(.+?)\s+\b(adopted|terminated)\b", re.I)
_ACTION_DATE = re.compile(rf"\b(?:adopted|terminated)\b.{{0,180}}?\bon\s+({_DATE})", re.I)
_SHARES = re.compile(r"\b(?:up to|maximum of)\s+(?:an aggregate of\s+)?([\d,]+)\s+shares\b", re.I)
_EXPIRY = re.compile(
    rf"\b(?:expire\w*|expiration date(?: of)?|will terminate)\b.{{0,100}}?({_DATE})",
    re.I,
)
_MAX_DOCUMENT_CHARS = 1_000_000
_MAX_SECTION_CHARS = 200_000


class _VisibleText(HTMLParser):
    """The small HTML projection this parser needs from archived SEC pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "div", "p", "tr", "td", "th", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"div", "p", "tr", "td", "th", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _visible_text(value: str) -> str:
    if not re.search(r"<(?:html|table|div|span|td|body)\b", value, re.I):
        return value
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def _item5_text(text: str) -> str | None:
    heading = re.search(
        r"^\s*Item\s+5(?:\.|\s|:)\s*(?:Other Information)?",
        text, re.I | re.M,
    )
    if heading:
        tail = text[heading.start():]
        end = re.search(r"^\s*Item\s+6(?:\.|\s|:)\s*Exhibits?", tail, re.I | re.M)
        return tail[:end.start()] if end else tail
    # SEC Inline-XBRL fact pages are already a bounded disclosure fragment.
    if "IDEA: XBRL DOCUMENT" in text and "Insider Trading Arrangements" in text:
        return text
    return None


def _date(value: str) -> str:
    return datetime.strptime(value, "%B %d, %Y").date().isoformat()


def _resolve_person(raw: str, members: list[str]) -> str | None:
    cleaned = raw.strip(" .–—-")
    surname = cleaned.split()[-1] if cleaned else ""
    if cleaned.lower().startswith(("mr. ", "ms. ", "mrs. ", "dr. ")):
        matches = [name for name in members if name.split()[-1] == surname]
        return matches[0] if len(matches) == 1 else None
    return cleaned if len(cleaned.split()) >= 2 else None


def trading_arrangement_events(
    text: str, *, company_ref: str, accession: str, filing_date: str,
    issuer_name: str | None, document_ref: str, artifact_hash: str,
) -> dict[str, Any]:
    """Return located plan actions; never infer an action from generic prose."""

    if not all((company_ref, accession, filing_date, issuer_name,
                document_ref, artifact_hash)):
        return {"status": "refused", "reason": "the filing lacks accession, issuer, filing date, document ref, or artifact hash", "events": []}
    raw = text or ""
    if len(raw) > _MAX_DOCUMENT_CHARS:
        return {"status": "refused", "reason": "the archived filing exceeds the bounded Item 5 parser input", "events": []}
    section = _item5_text(_visible_text(raw))
    if section is None:
        return {"status": "absent", "reason": "no bounded Item 5 or SEC insider-trading-arrangements fact was found", "events": []}
    if len(section) > _MAX_SECTION_CHARS:
        return {"status": "refused", "reason": "the located Item 5 disclosure exceeds the parser section bound", "events": []}
    # Inline-XBRL commonly puts several ``On <date>`` disclosures in one table
    # cell.  The visible-text projection retains a line break between its divs,
    # so make those explicit action boundaries before paragraph parsing.
    section = re.sub(rf"\n(?=On\s+{_DATE},)", "\n\n", section, flags=re.I)
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n", section)]
    paragraphs = [part.lstrip("• \t") for part in paragraphs if part]
    relevant = [part for part in paragraphs if _PLAN_WORDS.search(part)]
    if relevant and all(_NEGATIVE.search(part) for part in relevant):
        return {"status": "empty", "reason": "the filing explicitly reports no adopted or terminated trading arrangements", "events": []}
    members = []
    for part in paragraphs:
        for name in re.findall(r"([A-Z][A-Za-z.' -]+?)\s*\[Member\]", part):
            if name not in members:
                members.append(name.strip())
    events = []
    refused = []
    titles: dict[str, str] = {}
    for paragraph in relevant:
        if _NEGATIVE.search(paragraph):
            continue
        if not re.search(_DATE, paragraph, re.I):
            # XBRL labels and generic introductory sentences are not plan rows.
            continue
        action_match = _ACTION.search(paragraph)
        if action_match is None:
            continue
        action = action_match.group(1).lower()
        prefix = _ON_PREFIX.search(paragraph)
        if prefix:
            action_date = _date(prefix.group(1))
            identity = prefix.group(2).strip(" ,")
            candidates = [name for name in members
                          if name in identity or name.split()[-1] in identity]
            person = candidates[0] if len(set(candidates)) == 1 else None
            title = None
            if person and person in identity:
                title = identity.split(person, 1)[1].strip(" ,") or None
        else:
            dated = _ACTION_DATE.search(paragraph)
            action_date = _date(dated.group(1)) if dated else None
            candidates = [name for name in members if paragraph.startswith(name)]
            person = candidates[0] if len(candidates) == 1 else None
            title_match = re.search(r",\s+(?:a|an|the)\s+([^,]+?),\s+(?:adopted|terminated)\b", paragraph, re.I)
            title = title_match.group(1) if title_match else None
        if not person or not action_date:
            refused.append(paragraph)
            continue
        plan_type = ("non_rule_10b5_1" if re.search(r"non-Rule\s+10b5-1", paragraph, re.I)
                     else "rule_10b5_1")
        direction = ("purchase" if re.search(r"\b(?:purchase|buy)\w*\b", paragraph, re.I)
                     else "sale" if re.search(r"\b(?:sale|sell|sold)\w*\b", paragraph, re.I)
                     else "unknown")
        shares = _SHARES.search(paragraph)
        # A colon announces an enumerated set of terms in the next blocks.
        # Its first ``up to N shares`` is one leg, not the aggregate plan size.
        # Leaving the aggregate unknown is preferable to recording that leg as
        # the whole arrangement.
        aggregate_shares = (None if paragraph.rstrip().endswith(":") else
                            shares.group(1).replace(",", "") if shares else None)
        expiry = _EXPIRY.search(paragraph)
        if title:
            titles[person] = title
        else:
            title = titles.get(person)
        excerpt_hash = hashlib.sha256(paragraph.encode("utf-8")).hexdigest()
        payload = {
            "accession": accession, "form": "10-Q", "filing_date": filing_date,
            "issuer_name": issuer_name,
            "person_name": person, "person_title": title,
            "action": action, "action_date": action_date, "plan_type": plan_type,
            "securities_direction": direction,
            "aggregate_shares": aggregate_shares,
            "expiration_date": _date(expiry.group(1)) if expiry else None,
            "material_terms_excerpt": paragraph, "excerpt_hash": excerpt_hash,
            "document_ref": document_ref, "artifact_hash": artifact_hash,
        }
        payload["event_key"] = content_hash({
            key: payload[key] for key in (
                "accession", "person_name", "action", "action_date",
                "plan_type", "excerpt_hash")
        })
        events.append({
            "company_ref": company_ref, "kind": "insider_trading_plan",
            "evidence_tier": "primary_filing",
            "occurred_at": f"{action_date}T00:00:00+00:00",
            "source_refs": [document_ref, f"sec:filing:{accession}"],
            "payload": payload,
        })
    if refused:
        return {"status": "refused", "reason": "trading-arrangement prose was found but person/action/date could not all be located", "events": events}
    if events:
        return {"status": "read", "reason": None, "events": events}
    return {"status": "absent", "reason": "no Item 5 trading-arrangement action was found", "events": []}


def trading_plan_event_candidates(connection: Any, *, company_ref: str,
                                  limit: int = 40) -> dict[str, Any]:
    events, reports = [], []
    for document in buyback_documents(connection, company_ref=company_ref, limit=limit):
        if document["form"] != "10-Q":
            continue
        if not document["accession"]:
            reports.append({"status": "refused", "reason": "10-Q has no attributable accession", "document_ref": document["artifact_version_ref"]})
            continue
        result = trading_arrangement_events(
            document["text"] or "", company_ref=company_ref,
            accession=document["accession"], filing_date=document["document_date"],
            issuer_name=document["title"], document_ref=document["artifact_version_ref"],
            artifact_hash=document["artifact_hash"],
        )
        events.extend(result["events"])
        reports.append({"accession": document["accession"], "document_ref": document["artifact_version_ref"], "status": result["status"], "reason": result["reason"]})
    return {"events": events, "reports": reports}
