"""Deterministic answers to "does this cited span belong to this company?".

Two places ask the same question and must not disagree about it:

* the document qualitative admission path, *before* a mission-drafted
  statement is committed under ``DOCUMENT_QUALITATIVE_RULE_REF``;
* the claim review patrol (P10b), *after* the fact, for the Claims that were
  admitted before the admission check existed.

The live defect both answer (2026-09-24 audit): the extraction prompt invited
findings about "its industry, its customers or its named competitors", and the
window's company was attached to all of them.  ws-7d holds 652 admitted Claims
whose cited span and whose own statement never name the company they are filed
under -- CoreWeave's customer concentration as an AMZN Claim, automakers'
power-generation plans as a META Claim.  The whole-document check could not see
them: a morning digest names every hyperscaler somewhere.

Matching is a lowercase substring test on purpose, the same bluntness as the
original ``claim_retirement.subject_absent_from_source``: it is lenient towards
presence ("googl" is found inside "Google", a Chinese alias inside Chinese
prose), so a check built on it errs towards keeping a Claim, never towards
retiring or holding one on a technicality of spelling.

The one exemption is a document that *is* the subject's own -- its earnings
call, its filing, a note whose title names it.  There "we", "management" and
"the company" are the subject, and a span that never repeats the name is
still about it.  Title first (the provenance row), then the document's head.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Words that name an industry or a legal form rather than a company.  A span
#: mentioning "technology" says nothing about whether it is DXC Technology's.
GENERIC_NAME_TOKENS = frozenset({
    "technology", "technologies", "systems", "system", "group", "holdings",
    "international", "business", "machines", "company", "corp", "corporation",
    "inc", "plc", "ltd", "limited", "the", "and", "platforms", "platform",
    "com", "co", "services", "solutions", "global", "class",
})
#: How much of a document stands in for its title when no title was recorded.
#: A note's subject line, a transcript's "Q2 2026 Earnings Call" header and a
#: filing's cover all sit well inside this.
HEAD_CHARS = 400
#: The shortest token accepted as a name on its own.  Tickers of three letters
#: (IBM, DXC, ACN) are real names; two letters match inside ordinary words.
MIN_TOKEN_CHARS = 3
#: The shortest word taken on its own out of a name of several distinctive
#: words.  Three- and four-letter words ("red", "hat", "web") are ordinary
#: English inside a substring test; a whole name of that length still counts.
MIN_SPLIT_TOKEN_CHARS = 5
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&-]*")
_CJK_RE = re.compile(r"[㐀-鿿]")


def name_needles(names: Iterable[Any]) -> list[str]:
    """Lowercased names plus their distinctive word, generic words dropped.

    "Amazon.com" yields ``amazon.com`` and ``amazon``; "Meta Platforms"
    yields ``meta platforms`` and ``meta``; a CJK alias is kept whole.

    A name with one distinctive word left once the legal-form and industry
    words are dropped yields that word.  A name with several yields them
    joined, and on their own only the words of ``MIN_SPLIT_TOKEN_CHARS`` or
    more (2026-09-24b): "Microsoft Copilot" still yields ``copilot``, but "Red
    Hat" is one name, not "red" and "hat" -- matched as substrings those two
    would find IBM in "reduced" and "that" -- and "Amazon Web Services" no
    longer finds Amazon in every "website".
    """

    found: set[str] = set()
    for value in names:
        if not isinstance(value, str):
            continue
        text = value.strip().lower()
        if not text:
            continue
        if _CJK_RE.search(text):
            found.add(text)
            continue
        if len(text) >= MIN_TOKEN_CHARS and text not in GENERIC_NAME_TOKENS:
            found.add(text)
        tokens = [raw.strip("-&") for raw in _TOKEN_RE.findall(text)]
        distinctive = list(dict.fromkeys(
            token for token in tokens
            if len(token) >= MIN_TOKEN_CHARS and token not in GENERIC_NAME_TOKENS))
        if len(distinctive) == 1:
            found.update(distinctive)
        elif len(distinctive) > 1:
            # "Grid Dynamics Holdings" is still found as "grid dynamics".
            kept = [token for token in tokens if token and token not in GENERIC_NAME_TOKENS]
            found.add(" ".join(kept))
            found.update(token for token in distinctive if len(token) >= MIN_SPLIT_TOKEN_CHARS)
    return sorted(found)


def text_names_any(text: Any, needles: Sequence[str]) -> bool:
    """True when any needle occurs in the text (lowercase substring)."""

    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(needle and needle in lowered for needle in needles)


def document_is_subjects(
    *, title: Any = None, text: Any = None, needles: Sequence[str],
    issuer_document: bool = False,
) -> bool:
    """Whether this document is the subject's own, so its "we" is the subject.

    ``issuer_document`` is for kinds attributed by construction (a filing
    fetched by the issuer's accession).  Otherwise the recorded title decides,
    and when there is none the document's head does.
    """

    if issuer_document:
        return True
    if text_names_any(title, needles):
        return True
    if isinstance(text, str) and text_names_any(text[:HEAD_CHARS], needles):
        return True
    return False


def citation_names_subject(
    *, span: Any, statement: Any, needles: Sequence[str],
) -> bool:
    """The cited span or the statement drawn from it names the subject."""

    return text_names_any(span, needles) or text_names_any(statement, needles)


def subject_absent_from_citation(
    *, span: Any, statement: Any, needles: Sequence[str], document_is_own: bool,
) -> bool:
    """The review patrol's span-level rule.

    Fires only when all three hold: the document is not the subject's own,
    the exact span the Claim cites never names the subject, and neither does
    the Claim's own statement.  With no needles it never fires -- a company
    nobody has named is a configuration gap, not evidence.
    """

    if not needles or document_is_own:
        return False
    return not citation_names_subject(span=span, statement=statement, needles=needles)


def span_names_subject_for_admission(
    *, span: Any, needles: Sequence[str], document_is_own: bool,
) -> bool:
    """The admission rule: the quoted span itself must name the subject.

    Stricter than the patrol on purpose.  Admission is reversible -- a held
    candidate waits in staging for a person -- while retirement is not, so
    the statement (which the model wrote, knowing the subject) does not count
    here.  An issuer's own document is exempt for the reason given above.
    """

    if document_is_own:
        return True
    if not needles:
        return True
    return text_names_any(span, needles)


def mission_subject_needles(
    universe: Sequence[Mapping[str, Any]],
    *,
    plans: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[str]]:
    """company_ref -> every name this mission knows the company by.

    The union of the aliases the repository already keeps: the universe
    member's ticker and name, the packaged ``document_subject.COMPANY_NAMES``,
    every plan's ``companies[ref].names`` (feed plans) and
    ``companies[ref].search_terms`` (discovery plans).  A union rather than a
    precedence, because each check built on it only asks whether *some* name
    is present: an extra alias can only make it keep more.
    """

    from .document_subject import COMPANY_NAMES

    table: dict[str, set[str]] = {}
    for member in universe or ():
        if not isinstance(member, Mapping):
            continue
        ref = str(member.get("company_ref") or "")
        if not ref:
            continue
        ticker = str(member.get("ticker") or "").strip()
        names: list[Any] = [ticker]
        for key in ("name", "names"):
            value = member.get(key)
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, Sequence):
                names.extend(value)
        names.extend(COMPANY_NAMES.get(ticker.upper(), ()))
        table.setdefault(ref, set()).update(name_needles(names))
    refs_by_ticker = {
        str(member.get("ticker") or "").strip().upper(): str(member.get("company_ref") or "")
        for member in universe or () if isinstance(member, Mapping)}
    for plan in plans or ():
        # The owner's alias ledger, as ``load_feed_discovery_plan`` attaches it.
        aliases = plan.get("company_aliases") if isinstance(plan, Mapping) else None
        added = aliases.get("added") if isinstance(aliases, Mapping) else None
        for ticker, extra in (added.items() if isinstance(added, Mapping) else ()):
            ref = refs_by_ticker.get(str(ticker).strip().upper())
            if ref and isinstance(extra, Sequence) and not isinstance(extra, str):
                table.setdefault(ref, set()).update(name_needles(extra))
        companies = (plan or {}).get("companies") if isinstance(plan, Mapping) else None
        if not isinstance(companies, Mapping):
            continue
        for ref, entry in companies.items():
            if not isinstance(entry, Mapping):
                continue
            names = []
            value = entry.get("names")
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, Sequence):
                names.extend(value)
            terms = entry.get("search_terms")
            if isinstance(terms, str):
                names.extend(terms.split())
            if names:
                table.setdefault(str(ref), set()).update(name_needles(names))
    return {ref: sorted(values) for ref, values in table.items() if values}


def writer_feed_plans(writer: Any) -> list[dict[str, Any]]:
    """The feed discovery plans this writer's feed lanes run on, best effort.

    Their ``companies[].names`` is where a workspace records what its companies
    are called (W7).  A plan that cannot be read adds nothing.
    """

    from .document_extraction import FEED_SOURCE_LAUNCHER_KWARGS

    getter = getattr(writer, "lane_launcher", None)
    launchers: list[Any] = [writer]
    for kwarg in FEED_SOURCE_LAUNCHER_KWARGS.values() if callable(getter) else ():
        try:
            launchers.append(getter(kwarg))
        except Exception:  # noqa: BLE001 - an absent lane adds nothing
            continue
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    # The host's own plan first (2026-09-24b): the extraction child's feed
    # readers refuse to open when a lane has no ticket directory, and the plan
    # -- the mission's names and the owner's alias ledger -- does not depend
    # on any one lane having run.
    for launcher in launchers:
        path = getattr(launcher, "feed_plan_path", None)
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))
        try:
            from .mission_feed_lane import load_feed_discovery_plan

            plans.append(load_feed_discovery_plan(path))
        except Exception:  # noqa: BLE001 - an unreadable plan adds nothing
            continue
    return plans


__all__ = [
    "GENERIC_NAME_TOKENS",
    "HEAD_CHARS",
    "citation_names_subject",
    "document_is_subjects",
    "mission_subject_needles",
    "name_needles",
    "span_names_subject_for_admission",
    "subject_absent_from_citation",
    "text_names_any",
    "writer_feed_plans",
]
