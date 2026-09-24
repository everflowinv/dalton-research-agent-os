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
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&-]*")
_CJK_RE = re.compile(r"[㐀-鿿]")


def name_needles(names: Iterable[Any]) -> list[str]:
    """Lowercased names plus their distinctive words, generic words dropped.

    "Amazon.com" yields ``amazon.com`` and ``amazon``; "Meta Platforms"
    yields ``meta platforms`` and ``meta``; a CJK alias is kept whole.
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
        for token in _TOKEN_RE.findall(text):
            token = token.strip("-&")
            if len(token) >= MIN_TOKEN_CHARS and token not in GENERIC_NAME_TOKENS:
                found.add(token)
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
    context_before: Any = None, context_after: Any = None,
    peer_needles: Sequence[str] = (),
) -> bool:
    """The review patrol's span-level rule.

    Fires only when all of these hold: the document is not the subject's own,
    the exact span the Claim cites never names the subject, and neither does
    the Claim's own statement.  With no needles it never fires -- a company
    nobody has named is a configuration gap, not evidence.  "Names" is a
    whole-word match (:func:`text_names_word`) since v3: with the 2026-09-24
    alias table the substring test found Amazon in every "website" and
    "laws", which kept Claims the rule exists to retire.

    v3 (2026-09-24 audit, retirement only): a retirement is not reversible by
    the patrol, so it also needs the subject absent from what the span leans
    on -- an executive named in the span or statement ("Jassy said"), or an
    antecedent just before it that a statement's "management" / "the
    parties" / "he" refers to (see :func:`citation_context_names_subject`).
    ``context_before`` / ``context_after`` are the text around the span;
    without them the rule is exactly v2.
    """

    if not needles or document_is_own:
        return False
    # Whole words, and never a weak token: "aws" is not in "laws", and the
    # ``web`` that "Amazon Web Services" yields is not Amazon.  The admission
    # check keeps its lenient substring test; a retirement needs the name.
    if text_names_word(span, needles) or text_names_word(statement, needles):
        return False
    executives = executive_needles(needles)
    if text_names_word(span, executives) or text_names_word(statement, executives):
        return False
    if context_before is None and context_after is None:
        return True
    return not citation_context_names_subject(
        statement=statement, span=span, before=context_before, after=context_after,
        needles=[*needles, *executives], peer_needles=peer_needles,
    )


# -- retirement-only rules (2026-09-24 audit) --------------------------------
#
# The admission check (``span_names_subject_for_admission``) is untouched: a
# held candidate waits for a person, so it may be strict.  A retirement takes
# a Claim out of every deliverable, so the patrol keeps a Claim whenever the
# text gives a reasonable reading under which it is the subject's.  Everything
# below is used only by the retirement rule and by the re-review of past
# retirements.

#: The people whose name stands for the company in a sell-side sentence.
#: Used *only* to keep a Claim from being retired ("Jassy said ..." is an
#: Amazon statement); never as an alias at admission, where a name would let
#: any mention of a person file a Claim under a company.  Surnames appear
#: alone only when they are distinctive; common ones only with a first name.
EXECUTIVE_NAMES: Mapping[str, tuple[str, ...]] = {
    "AMZN": ("Andy Jassy", "Jassy", "Jeff Bezos", "Bezos", "Matt Garman",
             "Brian Olsavsky", "Olsavsky", "贾西", "贝索斯"),
    "GOOGL": ("Sundar Pichai", "Pichai", "Demis Hassabis", "Hassabis",
              "Thomas Kurian", "Anat Ashkenazi", "Ruth Porat", "皮查伊"),
    "META": ("Mark Zuckerberg", "Zuckerberg", "Susan Li", "Javier Olivan",
             "Andrew Bosworth", "扎克伯格"),
    "MSFT": ("Satya Nadella", "Nadella", "Amy Hood", "Mustafa Suleyman",
             "Judson Althoff", "纳德拉"),
    "IBM": ("Arvind Krishna", "Jim Kavanaugh"),
    "ACN": ("Julie Sweet", "Angie Park"),
    "CTSH": ("Ravi Kumar S", "Jatin Dalal"),
    "EPAM": ("Arkadiy Dobkin", "Dobkin", "Balazs Fejes"),
    "DXC": ("Raul Fernandez", "Rob Del Bene"),
}
#: Needles a name table produces that are ordinary words in running prose
#: ("Amazon Web Services" yields ``web``).  Harmless as a lenient keep in the
#: span test; wrong as evidence that a *neighbouring* sentence names the
#: company, so the context and density rules skip them.
WEAK_CONTEXT_NEEDLES = frozenset({"web", "red", "hat", "cloud", "one"})
#: How far around the cited span a pronoun's antecedent may be.
CONTEXT_CHARS = 500
#: How much of a document a filing's cover (XBRL header, "FORM 10-K", CIK)
#: occupies.  Wider than ``HEAD_CHARS``: an inline-XBRL rendering starts with
#: several hundred characters of taxonomy URLs before the company name.
COVER_CHARS = 3000
#: A document that is mostly about one company is that company's, whoever
#: wrote it.  All three must hold: enough mentions, dense enough, and the
#: subject's share of every covered company's mentions.  Measured against
#: ws-7d's 405 span retirements (sell-side digests name every hyperscaler a
#: dozen times) these keep an issuer's own 10-K and nothing in a digest.
DENSITY_MIN_MENTIONS = 20
DENSITY_MIN_PER_10K_CHARS = 8.0
DENSITY_MIN_SHARE = 0.8
#: Words in a statement that lean on an antecedent: "management", "the
#: parties", "he".  Without one the statement stands alone, and a company
#: named in a neighbouring line of a digest says nothing about it.
_ANAPHOR_RE = re.compile(
    r"(?i)\b(management|the company|the company's|the parties|the firm|the firm's|"
    r"the ceo|the cfo|he|she|its own|we|our)\b"
    r"|管理层|该公司|双方|他们|他|她"
)
#: A ticker-shaped word in a statement names *some* company.  Words that are
#: only ever acronyms of other things are not counted.
_TICKERLIKE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3,5}(?![A-Za-z0-9])")
_NOT_TICKERS = frozenset({
    "CEO", "CFO", "CTO", "COO", "EPS", "GDP", "YOY", "USD", "EBIT", "EBITDA",
    "CAGR", "TAM", "ROI", "ROIC", "GAAP", "SOTP", "NDX", "HBM", "DRAM", "NAND",
    "GPU", "CPU", "TPU", "ASIC", "LLM", "IPO", "ETF", "FICC", "TMT", "APAC",
    "EMEA", "BOFA", "SEC", "FTC", "DOJ", "NDRC", "CAICT", "USA", "MOC", "ATH",
    "RPO", "ARR", "OPEX", "CAPEX", "COGS", "SBC", "FCF", "GPT", "API", "SAAS",
    "PAAS", "IAAS", "DCF", "YTD", "QTD", "NYSE", "OTC", "ADR", "CIK", "XBRL",
    "FORM", "PDF", "URL", "KPI", "ESG", "THE", "AND", "FOR", "NOTE", "GIR",
})
_XBRL_COVER_RE = re.compile(r"(?<![a-z0-9])([a-z]{2,6})-(20\d{6})(?![0-9])")
_FILING_COVER_RE = re.compile(
    r"form\s+10-[kq](?![a-z])|annual report pursuant to section 1[35]"
    r"|quarterly report pursuant to section 1[35]"
)
_CIK_RE = re.compile(r"sec-cik:0*(\d+)")
_WORD_CACHE: dict[str, re.Pattern[str]] = {}


def _word_pattern(needle: str) -> re.Pattern[str]:
    pattern = _WORD_CACHE.get(needle)
    if pattern is None:
        if _CJK_RE.search(needle):
            pattern = re.compile(re.escape(needle))
        else:
            pattern = re.compile(
                r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])")
        _WORD_CACHE[needle] = pattern
    return pattern


def _cut(text: str, limit: int) -> str:
    """The first ``limit`` characters, without a word the cut split in two.

    "metals meta|data" cut at the bar must not read as naming Meta.
    """

    head = text[:limit]
    if len(text) > limit and text[limit].isalnum():
        head = re.sub(r"[A-Za-z0-9]+$", "", head)
    return head


def _usable(needles: Iterable[str]) -> list[str]:
    return [needle for needle in needles
            if isinstance(needle, str) and len(needle) >= MIN_TOKEN_CHARS
            and needle not in WEAK_CONTEXT_NEEDLES]


def text_names_word(text: Any, needles: Sequence[str]) -> bool:
    """Like :func:`text_names_any`, but a Latin needle must be a whole word.

    "aws" is not found in "laws"; a CJK needle is still a substring.
    """

    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(_word_pattern(needle).search(lowered) for needle in _usable(needles))


def _last_mention(text: str, needles: Sequence[str]) -> int:
    best = -1
    for needle in _usable(needles):
        for match in _word_pattern(needle).finditer(text):
            best = max(best, match.start())
    return best


def _first_mention(text: str, needles: Sequence[str]) -> int | None:
    best: int | None = None
    for needle in _usable(needles):
        match = _word_pattern(needle).search(text)
        if match is not None and (best is None or match.start() < best):
            best = match.start()
    return best


def executive_needles(needles: Sequence[str]) -> list[str]:
    """The executives of whichever company these needles name (by ticker)."""

    present = {str(needle).lower() for needle in needles}
    found: list[str] = []
    for ticker, names in EXECUTIVE_NAMES.items():
        if ticker.lower() in present:
            found.extend(name.lower() for name in names)
    return found


def citation_context_names_subject(
    *, statement: Any, span: Any, before: Any, after: Any,
    needles: Sequence[str], peer_needles: Sequence[str] = (),
) -> bool:
    """Whether the text around the span supplies the statement's subject.

    The audit's cases: "NDRC orders Meta to unwind ... [span:] requires the
    parties to withdraw"; "The headwinds IBM noted ... [span:] we expect
    management will take a prudent approach".  The span never repeats the
    name, the statement says "the parties" / "management", and the name is
    the nearest company named before it.

    Deliberately narrower than "the name is within 500 characters": in a
    sell-side digest every hyperscaler is within 500 characters of every
    sentence, and on ws-7d that looser reading would have put back ~100
    industry statements the audit agreed were rightly retired.  So all of:

    * the statement leans on an antecedent (``management``, ``the parties``,
      ``he`` ...);
    * neither the statement nor the span names another covered company, and
      the statement names no other ticker-shaped company;
    * the nearest covered company named in the ``CONTEXT_CHARS`` before the
      span is the subject -- or, when none is named before it, the first one
      named after it is.
    """

    if not isinstance(statement, str) or not _ANAPHOR_RE.search(statement):
        return False
    peers = _usable(peer_needles)
    if text_names_word(statement, peers) or text_names_word(span, peers):
        return False
    own = {needle.lower() for needle in needles}
    if any(match.group(0) not in _NOT_TICKERS and match.group(0).lower() not in own
           for match in _TICKERLIKE_RE.finditer(statement)):
        return False
    before_text = (before if isinstance(before, str) else "")[-CONTEXT_CHARS:].lower()
    subject_at = _last_mention(before_text, needles)
    if subject_at >= 0:
        return _last_mention(before_text, peers) < subject_at
    if _last_mention(before_text, peers) >= 0:
        return False
    after_text = (after if isinstance(after, str) else "")[:CONTEXT_CHARS].lower()
    subject_first = _first_mention(after_text, needles)
    if subject_first is None:
        return False
    peer_first = _first_mention(after_text, peers)
    return peer_first is None or subject_first < peer_first


def _mentions(lowered: str, needles: Sequence[str]) -> int:
    starts: set[int] = set()
    for needle in _usable(needles):
        starts.update(match.start() for match in _word_pattern(needle).finditer(lowered))
    return len(starts)


def own_document_evidence(
    *, title: Any = None, text: Any = None, needles: Sequence[str],
    issuer_document: bool = False, subject_ref: Any = None,
    peer_needles: Sequence[str] = (),
) -> str | None:
    """Why this document is the subject's own, for the retirement rule, or None.

    Wider than :func:`document_is_subjects` (which admission keeps using):

    * ``issuer`` / ``title`` / ``head`` -- the admission rule's three, with
      the name matched as a whole word;
    * ``xbrl_cover`` -- an inline-XBRL rendering fetched from the web starts
      ``goog-20251231``: a filer prefix that is the ticker or a prefix of it;
    * ``cik_cover`` -- the subject's own CIK (from a ``sec-cik`` ref) on the
      cover;
    * ``filing_cover`` -- "FORM 10-K" / "Annual report pursuant to Section 13"
      on the cover *and* a name of the subject there too;
    * ``density`` -- the subject is named throughout (see the thresholds).
    """

    if issuer_document:
        return "issuer"
    # Title and head as :func:`document_is_subjects` reads them, but by whole
    # word: "meta" is in "metadata" and "metals", which the head of many a
    # digest contains.
    if text_names_word(title, needles):
        return "title"
    if not isinstance(text, str) or not text:
        return None
    if text_names_word(_cut(text, HEAD_CHARS), needles):
        return "head"
    lowered = text.lower()
    cover = _cut(lowered, COVER_CHARS)
    tickers = [needle for needle in needles
               if isinstance(needle, str) and re.fullmatch(r"[a-z]{3,5}", needle)]
    for match in _XBRL_COVER_RE.finditer(cover):
        prefix = match.group(1)
        if len(prefix) >= MIN_TOKEN_CHARS and any(
                ticker == prefix or ticker.startswith(prefix) for ticker in tickers):
            return "xbrl_cover"
    cik = _CIK_RE.search(str(subject_ref or ""))
    if cik is not None and re.search(
            r"(?<![0-9])0*" + cik.group(1) + r"(?![0-9])", cover):
        return "cik_cover"
    if _FILING_COVER_RE.search(cover) and text_names_word(cover, needles):
        return "filing_cover"
    mine = _mentions(lowered, needles)
    if (mine >= DENSITY_MIN_MENTIONS
            and mine * 10_000 / max(1, len(lowered)) >= DENSITY_MIN_PER_10K_CHARS
            and mine >= DENSITY_MIN_SHARE * (mine + _mentions(lowered, peer_needles))):
        return "density"
    return None


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
    if not callable(getter):
        return []
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kwarg in FEED_SOURCE_LAUNCHER_KWARGS.values():
        try:
            launcher = getter(kwarg)
        except Exception:  # noqa: BLE001 - an absent lane adds nothing
            continue
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
    "CONTEXT_CHARS",
    "COVER_CHARS",
    "EXECUTIVE_NAMES",
    "GENERIC_NAME_TOKENS",
    "HEAD_CHARS",
    "WEAK_CONTEXT_NEEDLES",
    "citation_context_names_subject",
    "citation_names_subject",
    "document_is_subjects",
    "executive_needles",
    "mission_subject_needles",
    "own_document_evidence",
    "name_needles",
    "span_names_subject_for_admission",
    "subject_absent_from_citation",
    "text_names_any",
    "text_names_word",
    "writer_feed_plans",
]
