"""Strict Item 408(a) trading-arrangement events from archived SEC filings.

A 10-Q discloses directors' and officers' Rule 10b5-1 plan adoptions and
terminations in Part II Item 5; a 10-K discloses the fourth quarter's in Item
9B.  Since 2023 both are also tagged in inline XBRL under the SEC's ``ecd``
taxonomy -- one ``ecd:IndividualAxis`` member per person, with the name,
title, action flag, action date, expiry and share amount as separate facts.

Two readers, tried in that order, both deterministic:

* :func:`inline_xbrl_trading_arrangements` reads the tagged facts.  A person is
  the filer's own ``IndividualAxis`` member, so two people's dates can never be
  crossed, and an action is a flag the filer set ``fixed-true``.
* the bounded section parser reads the prose or the SEC fact page when there is
  no tagging, exactly as before.

Neither infers anything the filing does not say: an unknown direction is
``unknown`` and an aggregate split across share classes is left null.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Mapping

from .buyback_disclosure import buyback_documents
from .store import content_hash

_DATE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}"
_NEGATIVE = re.compile(
    r"\b(?:no|none of (?:our|the company(?:'s)?))\b.{0,120}\b(?:adopted|terminated)\b",
    re.IGNORECASE,
)
_PLAN_WORDS = re.compile(
    r"(?:(?:Rule\s+10b5-1|non-Rule\s+10b5-1).{0,80}trading (?:arrangement|plan)"
    r"|trading (?:arrangement|plan).{0,80}Rule\s+10b5-1)", re.I)
_NAME = re.compile(r"^[A-Z][A-Za-z'\-]+\.?(?:\s+[A-Z][A-Za-z'\-]*\.?){1,4}$")
_ACTION = re.compile(r"\b(adopted|terminated)\b", re.I)
_ON_PREFIX = re.compile(rf"^On\s+({_DATE})\s*,\s+(.+?)\s+\b(adopted|terminated)\b", re.I)
_ACTION_DATE = re.compile(rf"\b(?:adopted|terminated)\b.{{0,180}}?\bon\s+({_DATE})", re.I)
_SHARES = re.compile(r"\b(?:up to|maximum of)\s+(?:an aggregate of\s+)?([\d,]+)\s+shares\b", re.I)
_EXPIRY = re.compile(
    rf"\b(?:expire\w*|expiration date(?: of)?|will terminate)\b.{{0,100}}?({_DATE})",
    re.I,
)
_MAX_DOCUMENT_CHARS = 1_000_000
#: An archived inline-XBRL filing is markup, not prose: a Microsoft 10-K is
#: 8.6 MB of it. The tagged facts and the section are bounded separately, so
#: markup is admitted up to this size and plain text keeps the old bound.
_MAX_MARKUP_CHARS = 16_000_000
_MAX_SECTION_CHARS = 200_000
_MAX_EXCERPT_CHARS = 600
#: The forms whose trading-arrangement disclosure this module reads, and the
#: item it sits under in each.
PLAN_FORMS: tuple[str, ...] = ("10-Q", "10-K", "10-Q/A", "10-K/A")


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


_SECTION_BOUNDS = {
    "10-Q": (r"Item\s*5\b", r"Item\s*6\b"),
    "10-K": (r"Item\s*9B\b", r"Item\s*9C\b|Item\s*10\b|PART\s+III\b"),
}


def _plan_section(text: str, form: str) -> str | None:
    """The disclosure section of this form that talks about trading plans.

    A filing names each item twice -- once in its table of contents -- so
    every heading is a candidate and the one whose body mentions a trading
    arrangement is taken.  None mentioning one means the section is the last
    heading's body, which is where the filer's "None" sits.
    """

    if form.startswith("10-Q"):
        legacy = _item5_text(text)
        if legacy is not None and _PLAN_WORDS.search(" ".join(legacy.split())):
            return legacy
    start, end = _SECTION_BOUNDS["10-K" if form.startswith("10-K") else "10-Q"]
    sections = []
    for heading in re.finditer(start, text, re.I):
        tail = text[heading.start():heading.start() + _MAX_SECTION_CHARS + 1]
        stop = re.search(end, tail[8:], re.I)
        sections.append(tail[:stop.start() + 8] if stop else tail)
    for section in sections:
        if re.search(r"10b5-1|trading arrangement", section, re.I):
            return section
    if sections:
        return sections[-1]
    return _item5_text(text) if form.startswith("10-Q") else None


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


def _is_markup(value: str) -> bool:
    return re.search(r"<(?:html|table|div|span|td|body)\b", value[:200_000], re.I) is not None


def trading_arrangement_events(
    text: str, *, company_ref: str, accession: str, filing_date: str,
    issuer_name: str | None, document_ref: str, artifact_hash: str,
    form: str = "10-Q",
) -> dict[str, Any]:
    """Return located plan actions; never infer an action from generic prose."""

    if not all((company_ref, accession, filing_date, issuer_name,
                document_ref, artifact_hash)):
        return {"status": "refused", "reason": "the filing lacks accession, issuer, filing date, document ref, or artifact hash", "events": []}
    form = (form or "10-Q").upper()
    if form not in PLAN_FORMS:
        return {"status": "refused", "reason": f"{form} 没有 Item 408(a) 交易计划披露段", "events": []}
    raw = text or ""
    markup = _is_markup(raw)
    if len(raw) > (_MAX_MARKUP_CHARS if markup else _MAX_DOCUMENT_CHARS):
        return {"status": "refused", "reason": "the archived filing exceeds the bounded Item 5 parser input", "events": []}
    identity = {"company_ref": company_ref, "accession": accession, "form": form,
                "filing_date": filing_date, "issuer_name": issuer_name,
                "document_ref": document_ref, "artifact_hash": artifact_hash}
    if markup and "ecd:" in raw:
        tagged = inline_xbrl_trading_arrangements(raw, **identity)
        if tagged["status"] != "absent":
            return tagged
    section = _plan_section(_visible_text(raw), form)
    if section is None:
        return {"status": "absent", "reason": "no bounded Item 5 / Item 9B or SEC insider-trading-arrangements fact was found", "events": []}
    if len(section) > _MAX_SECTION_CHARS:
        return {"status": "refused", "reason": "the located Item 5 disclosure exceeds the parser section bound", "events": []}
    # Inline-XBRL commonly puts several ``On <date>`` disclosures in one table
    # cell.  The visible-text projection retains a line break between its divs,
    # so make those explicit action boundaries before paragraph parsing; a
    # filer that writes them as consecutive sentences gets the same boundary.
    section = re.sub(rf"\n(?=On\s+{_DATE}\s*,)", "\n\n", section, flags=re.I)
    section = re.sub(rf"(?<=\.)[ \t]+(?=On\s+{_DATE}\s*,)", "\n\n", section, flags=re.I)
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
            elif not members:
                # A main document has no XBRL member labels; the filer's own
                # "On <date>, <Name>, <Title>, adopted" order is the binding,
                # and a first segment that is not a name is refused below.
                segments = [part.strip() for part in identity.split(",") if part.strip()]
                if segments and _NAME.match(segments[0]):
                    person = segments[0]
                    title = ", ".join(segments[1:]) or None
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
        paragraph = paragraph[:_MAX_EXCERPT_CHARS].strip()
        excerpt_hash = hashlib.sha256(paragraph.encode("utf-8")).hexdigest()
        payload = {
            "accession": accession, "form": form, "filing_date": filing_date,
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


# -- inline XBRL ------------------------------------------------------------

_IX_FACT_TAGS = frozenset({"ix:nonnumeric", "ix:nonfraction"})
_BLOCK_TAGS = frozenset({"br", "p", "div", "td", "th", "tr", "li"})
#: Flag -> (action, plan type). A flag counts only when the filer set it
#: ``ixt:fixed-true`` (or tagged a visible word without a fixed format).
_ACTION_FLAGS = {
    "Rule10b51ArrAdoptedFlag": ("adopted", "rule_10b5_1"),
    "NonRule10b51ArrAdoptedFlag": ("adopted", "non_rule_10b5_1"),
    "Rule10b51ArrTrmntdFlag": ("terminated", "rule_10b5_1"),
    "NonRule10b51ArrTrmntdFlag": ("terminated", "non_rule_10b5_1"),
}
_INDIVIDUAL_AXIS = "ecd:IndividualAxis"
_ARRANGEMENT_AXIS = "ecd:TradingArrAxis"


class _InlineFacts(HTMLParser):
    """``ecd:`` facts and the explicit members of every context, nothing else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.facts: list[dict[str, Any]] = []
        self.contexts: dict[str, dict[str, str]] = {}
        self._open: list[dict[str, Any] | None] = []
        self._context: str | None = None
        self._dimension: str | None = None
        self._member: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag in _IX_FACT_TAGS:
            name = values.get("name", "")
            fact = None
            if name.startswith("ecd:") and len(self.facts) < 2000:
                fact = {"name": name[4:], "context": values.get("contextref", ""),
                        "format": values.get("format", ""), "parts": []}
                self.facts.append(fact)
            self._open.append(fact)
        elif tag == "xbrli:context":
            self._context = values.get("id", "")
            self.contexts.setdefault(self._context, {})
        elif tag == "xbrldi:explicitmember" and self._context is not None:
            self._dimension, self._member = values.get("dimension", ""), []
        elif tag in _BLOCK_TAGS:
            for fact in self._open:
                if fact is not None:
                    fact["parts"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _IX_FACT_TAGS and self._open:
            self._open.pop()
        elif tag == "xbrli:context":
            self._context = None
        elif tag == "xbrldi:explicitmember" and self._member is not None:
            if self._context is not None and self._dimension:
                self.contexts[self._context][self._dimension] = "".join(self._member).strip()
            self._dimension, self._member = None, None

    def handle_data(self, data: str) -> None:
        if self._member is not None:
            self._member.append(data)
        for fact in self._open:
            if fact is not None and sum(len(part) for part in fact["parts"]) < 20_000:
                fact["parts"].append(data)


def _fact_text(fact: Mapping[str, Any]) -> str:
    return " ".join("".join(fact["parts"]).split())


def _flag_set(fact: Mapping[str, Any]) -> bool:
    form = str(fact.get("format") or "").lower()
    if "fixed-false" in form:
        return False
    if "fixed-true" in form:
        return True
    return bool(_fact_text(fact)) and _fact_text(fact).lower() not in {"false", "no", "none"}


def _tagged_date(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.replace(" ,", ",").split())
    match = re.search(_DATE, text)
    if match:
        return _date(match.group(0))
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    return None


def inline_xbrl_trading_arrangements(
    html: str, *, company_ref: str, accession: str, form: str, filing_date: str,
    issuer_name: str | None, document_ref: str, artifact_hash: str,
) -> dict[str, Any]:
    """Plan actions from the filer's own ``ecd`` inline-XBRL tagging.

    ``absent`` when the filing carries no trading-arrangement tagging at all,
    so the caller falls back to the section parser; ``empty`` when the filer
    tagged only entity-level flags, all false (nothing adopted or terminated).
    """

    parser = _InlineFacts()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed markup is a refusal
        return {"status": "refused", "reason": f"内联 XBRL 无法解析：{exc}", "events": []}
    facts = [fact for fact in parser.facts
             if fact["name"] in _ACTION_FLAGS or fact["name"].startswith(("TrdArr", "MtrlTermsOfTrdArr"))]
    if not facts:
        return {"status": "absent", "reason": "没有 ecd 交易计划标签", "events": []}

    def members(context: str) -> dict[str, str]:
        return parser.contexts.get(context, {})

    people = [fact for fact in facts if fact["name"] == "TrdArrIndName"]
    if not people:
        flags = [fact for fact in facts if fact["name"] in _ACTION_FLAGS]
        if flags and not any(_flag_set(fact) for fact in flags):
            return {"status": "empty", "reason": "申报人标注本期没有董事或高管采用或终止交易计划", "events": []}
        return {"status": "absent", "reason": "ecd 标签没有指明具体人员", "events": []}
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    for fact in people:
        context = members(fact["context"])
        individual = context.get(_INDIVIDUAL_AXIS) or f"context:{fact['context']}"
        groups.setdefault((individual, context.get(_ARRANGEMENT_AXIS)), {"facts": []})
    for fact in facts:
        context = members(fact["context"])
        individual = context.get(_INDIVIDUAL_AXIS) or f"context:{fact['context']}"
        arrangement = context.get(_ARRANGEMENT_AXIS)
        if (individual, arrangement) in groups:
            groups[(individual, arrangement)]["facts"].append(fact)
        elif (individual, None) in groups:
            # An amount tagged per share class belongs to that person's plan.
            groups[(individual, None)]["facts"].append(fact)
    events: list[dict[str, Any]] = []
    refused: list[str] = []
    for (individual, arrangement), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        by_name: dict[str, list[str]] = {}
        flags: list[tuple[str, str]] = []
        for fact in group["facts"]:
            if fact["name"] in _ACTION_FLAGS:
                if _flag_set(fact):
                    flags.append(_ACTION_FLAGS[fact["name"]])
                continue
            text = _fact_text(fact)
            if text:
                by_name.setdefault(fact["name"], []).append(text)
        person = (by_name.get("TrdArrIndName") or [None])[0]
        title = (by_name.get("TrdArrIndTitle") or [None])[0]
        if not person or not _NAME.match(person.replace(",", "")):
            refused.append(individual)
            continue
        amounts = sorted({value.replace(",", "") for value in by_name.get("TrdArrSecuritiesAggAvailAmt", [])
                          if re.fullmatch(r"[\d,]+", value)})
        terms = (by_name.get("MtrlTermsOfTrdArrTextBlock") or [""])[0]
        excerpt = terms or " | ".join(
            value for key in ("TrdArrIndName", "TrdArrIndTitle", "TrdArrAdoptionDate",
                              "TrdArrTerminationDate", "TrdArrExpirationDate",
                              "TrdArrSecuritiesAggAvailAmt")
            for value in by_name.get(key, []))
        excerpt = excerpt[:_MAX_EXCERPT_CHARS].strip()
        direction = ("purchase" if re.search(r"\b(?:purchase|buy)\w*\b", terms, re.I)
                     else "sale" if re.search(r"\b(?:sale|sell|sold)\w*\b", terms, re.I)
                     else "unknown")
        expiry = _tagged_date((by_name.get("TrdArrExpirationDate") or [None])[0])
        for action, plan_type in sorted(set(flags)):
            source = "TrdArrAdoptionDate" if action == "adopted" else "TrdArrTerminationDate"
            action_date = _tagged_date((by_name.get(source) or [None])[0])
            if action_date is None:
                refused.append(f"{individual}:{action}")
                continue
            payload = {
                "accession": accession, "form": form, "filing_date": filing_date,
                "issuer_name": issuer_name, "person_name": person, "person_title": title,
                "action": action, "action_date": action_date, "plan_type": plan_type,
                "securities_direction": direction,
                # Two amounts are two share classes; their sum is not a filed figure.
                "aggregate_shares": amounts[0] if len(amounts) == 1 else None,
                "expiration_date": expiry if action == "adopted" else None,
                "material_terms_excerpt": excerpt or person,
                "excerpt_hash": hashlib.sha256((excerpt or person).encode("utf-8")).hexdigest(),
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
    if refused and not events:
        return {"status": "refused", "reason": "已标注的交易计划缺少人员姓名或动作日期，不予记录", "events": []}
    if events:
        return {"status": "read", "reason": None if not refused else "部分已标注的交易计划缺少姓名或日期，已跳过", "events": events}
    return {"status": "empty", "reason": "已标注人员均未勾选采用或终止标记", "events": []}


def trading_plan_event_candidates(connection: Any, *, company_ref: str,
                                  limit: int = 40, state_dir: Any = None,
                                  cache: dict[Any, Any] | None = None) -> dict[str, Any]:
    events, reports = [], []
    for document in buyback_documents(connection, company_ref=company_ref, limit=limit,
                                      state_dir=state_dir, cache=cache):
        if document["form"] not in PLAN_FORMS:
            continue
        if not document["accession"]:
            reports.append({"status": "refused", "reason": f"{document['form']} 无法确定申报编号", "document_ref": document["artifact_version_ref"]})
            continue
        result = trading_arrangement_events(
            document["text"] or "", company_ref=company_ref,
            accession=document["accession"], filing_date=document["document_date"],
            issuer_name=document.get("issuer_name") or document["title"],
            document_ref=document["artifact_version_ref"],
            artifact_hash=document["artifact_hash"], form=document["form"],
        )
        events.extend(result["events"])
        reports.append({"accession": document["accession"], "document_ref": document["artifact_version_ref"], "status": result["status"], "reason": result["reason"]})
    return {"events": events, "reports": reports}
