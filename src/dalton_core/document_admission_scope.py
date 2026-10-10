"""2026-09-29: what one document may put into the Ledger -- once, at most a dozen, dated by its filing.

Batch g's first hours on legacy: EPAM's 10-K (fetched from sec.gov) went in
as 79 qualitative Claims, most of them risk-factor and accounting-policy
prose ("EPAM states that its effective tax rate could be materially adversely
affected ..."), seven pairs of them the same sentence read out of two sections
of the filing (similarity 0.69-0.95), under 21 spellings of the period, one of
them wrong ("FY2026 filing (as of 2026-09-08)").  IBM's 10-K went in as 22.
And a DXC Guidepoint excerpt was read twice across a mission upgrade, the
second read admitting three more Claims on exactly the spans the first had
cited.

Every window's admission is now planned per document before anything is
checked or committed (:func:`plan_document_admissions`):

* **Once per span.**  A statement whose (document, subject, cited span) a live
  Claim already cites is that Claim: it is reported as a duplicate of it and
  never staged, checked or committed.  A document re-read under a later
  mission version cannot add a second Claim on the same bytes.
* **Once per finding.**  Within one document and one subject, two statements
  that say the same thing are one finding (:func:`same_finding`), wherever in
  the document they are cited -- a 10-K says what the company does in Item 1,
  in MD&A and again in Note 1.  The later-read or less informative one is the
  duplicate.  The rule is the support recheck's ``near_duplicate`` (cd1cae38)
  loosened for use before admission: that one decides after the fact, over
  overlapping spans only, at difflib 0.85; here the spans may be anywhere in
  the document, so the bar is the lower of two measures (see the constants),
  and the same guards apply -- the same numbers, the same negations.
* **At most ``MAX_CLAIMS_PER_DOCUMENT`` per document and subject**, counting
  the live Claims it already has.  What is over is ranked by
  :func:`information_score` and the tail is dropped (``truncated``), counted
  in the run summary and the review's rationale.

And an SEC filing's period is its own (:func:`sec_filing_period`): the fiscal
year or quarter the cover says it reports, never the day it was fetched.
"""

from __future__ import annotations

import calendar
import difflib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

RULE_REF = "document-admission-scope:v1"

#: The cap.  Live Claims per (document, subject) on legacy, 10,011 qualitative
#: Claims over 560 documents (read only, 2026-09-29): median 5, p75 12, p90 33,
#: p99 230.  Guidepoint excerpts and sales notes never exceeded 7; the tail is
#: filings and long transcripts, and the 10-K tail is what the post-deploy
#: check found to be mostly risk factors and accounting policy.  Twelve is the
#: p75: it leaves every excerpt and note whole and holds a filing to its dozen
#: most informative findings.
MAX_CLAIMS_PER_DOCUMENT = 12

#: The two measures of "the same finding" before admission, either enough.
#: Calibrated on every same-document, same-subject pair of live Claims on the
#: legacy Ledger (read only, 2026-09-29; 121,030 pairs): 1,372 meet the rule,
#: and the samples read from every bucket were restatements ("EPAM describes
#: itself as a leading global provider ..." from Item 1 and from Note 1;
#: "IBM's Bob ... is pitched as" / "is positioned as"; the lowest, 0.701,
#: "Accenture generates substantial free cash flow ..." / "... large free
#: cash flow ...") but one, a shared clause about "a changing economic
#: landscape" under two different findings at 0.703.  Below 0.60 with
#: containment under 0.80 they were different findings.  EPAM's 10-K pairs
#: score 0.80-1.0.
PRE_ADMISSION_RATIO = 0.70
PRE_ADMISSION_CONTAINMENT = 0.80
#: Containment over fewer content words than this is too coarse to call.
MIN_CONTENT_WORDS = 5

_NEGATIONS = frozenset({"not", "no", "never", "none", "nor", "neither", "without", "cannot"})
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
#: Words that carry no finding: articles, the reporting verbs every statement
#: opens with, and the connectives.
_STOP = frozenset(
    "the a an of to and or in on for with its it that this is are be by as from at which their "
    "has have states state says say said stated describes describe discloses disclose notes note "
    "noted reports report reported according per also".split())


def _normalized(statement: str) -> str:
    text = unicodedata.normalize("NFKC", str(statement)).casefold()
    text = re.sub("n['’]t\\b", " not", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _numbers(statement: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(statement))
    return sorted(value.replace(",", "") for value in _NUMBER_RE.findall(text))


def _within(small: list[str], large: list[str]) -> bool:
    """Every number of ``small`` is in ``large`` (as a multiset).

    "grew 5%" and "grew 7%" conflict; "restrictions from April 2024 until the
    end of 2026" and "restrictions until the end of 2026" do not -- one only
    says less (EPAM's 10-K, legacy 2026-09-29).
    """

    pool = list(large)
    for value in small:
        if value not in pool:
            return False
        pool.remove(value)
    return True


def _negations(normalized: str) -> int:
    # "whether or not" is not a negation: EPAM's "regardless of whether" and
    # "whether or not the employees are performing services" say one thing.
    text = normalized.replace("whether or not", "whether")
    return sum(word in _NEGATIONS for word in text.split())


def _content_words(normalized: str) -> set[str]:
    return {word for word in normalized.split() if word not in _STOP}


def same_finding(first: str, second: str) -> float | None:
    """How alike two statements of one document and subject are, when they are one finding.

    ``None`` when they differ: numbers that conflict (neither statement's
    numbers are all in the other's), a different count of negations, or
    neither measure at its bar.  Otherwise the higher of the
    difflib ratio of the normalised texts and the share of the shorter one's
    content words the longer one has.
    """

    numbers_a, numbers_b = _numbers(first), _numbers(second)
    if not (_within(numbers_a, numbers_b) or _within(numbers_b, numbers_a)):
        return None
    a, b = _normalized(first), _normalized(second)
    if _negations(a) != _negations(b):
        return None
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    words_a, words_b = _content_words(a), _content_words(b)
    shorter = min(len(words_a), len(words_b))
    containment = (len(words_a & words_b) / shorter) if shorter >= MIN_CONTENT_WORDS else 0.0
    if ratio >= PRE_ADMISSION_RATIO or containment >= PRE_ADMISSION_CONTAINMENT:
        return round(max(ratio, containment), 3)
    return None


# -- how much a statement tells ------------------------------------------------

#: A change, a comparison, a cause, a plan: what research is made of.
_SIGNAL_RES = (
    (re.compile(r"(?i)\b(?:increas|decreas|grew|grow|declin|rose|rise|fell|fall|slow|accelerat|"
                r"expand|contract|improv|deteriorat|widen|narrow|recover|weaken|strengthen|"
                r"doubl|halv|shrank|shrink|surpass|miss|beat|cut|rais|lower|higher|stabiliz)\w*"),
     2.0, 4.0),
    (re.compile(r"(?i)\b(?:compared (?:with|to)|versus|vs\.?|than|year[- ]over[- ]year|"
                r"sequential\w*)\b"), 1.0, 2.0),
    (re.compile(r"(?i)\b(?:due to|driven by|because|primarily|as a result|attribut\w+|offset)\b"),
     1.0, 2.0),
    (re.compile(r"(?i)\b(?:expects?|plans?|targets?|guidance|outlook|intends?|pipeline|"
                r"bookings|backlog|won|signed|launched|acquired|appointed|named)\b"), 1.0, 2.0),
    (re.compile(r"\b(?:19|20)\d{2}\b|\b[QH][1-4]\b|\bFY\s?'?\d{2}"), 0.5, 1.0),
)
#: Risk-factor hedging and accounting-policy prose: what every filing says.
_BOILERPLATE_RES = (
    (re.compile(r"(?i)\b(?:could|may|might|would)\b|\bif\b|no assurance|adversely|"
                r"subject to|there can be|uncertaint\w*|risk\w*"), -1.5, -4.5),
    (re.compile(r"(?i)\brecogni[sz]\w*|amortiz\w*|valuation allowance|fair value|"
                r"in accordance with|polic(?:y|ies)|contract assets|deferred tax|indemnif\w*|"
                r"accounted for|classified (?:in|as)|reported as|consideration|impairment|"
                r"realizab\w*|estimates? and assumptions"), -1.5, -4.5),
    (re.compile(r"(?i)describes? itself|positions? itself|a leading (?:global )?provider|"
                r"forward-looking"), -2.0, -2.0),
)


def information_score(statement: str) -> float:
    """A deterministic rank of how much a statement tells, higher first.

    Rewards a change, a comparison, a cause, a plan and a named period;
    penalises the hedging of a risk factor ("could", "may", "if",
    "adversely"), accounting-policy vocabulary and self-description.  Used
    only to order what the cap keeps; nothing is refused for its score.
    """

    text = str(statement or "")
    score = 0.0
    for pattern, each, bound in (*_SIGNAL_RES, *_BOILERPLATE_RES):
        hits = len(pattern.findall(text))
        if hits:
            total = each * hits
            score += max(total, bound) if bound < 0 else min(total, bound)
    return round(score, 2)


# -- what the document already has ----------------------------------------------

BINDING_PREFIX = "transcript-claim-citation-binding:"


def document_claims(connection: sqlite3.Connection, source_content_hashes: Iterable[str], *,
                    ledger: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
                    live_only: bool = True) -> list[dict[str, Any]]:
    """Every (live) Claim that cites one of these originals: ref, subject, statement, span.

    ``ledger`` is :func:`ledger_bindings`, read once per run; without it the
    Ledger is read here.  Keyed by the original's content hash -- the citation
    binding's ``source_content_hash`` -- which is the same bytes whichever
    review, mission version or source ref read them.
    """

    hashes = {value for value in source_content_hashes if isinstance(value, str) and value}
    if not hashes:
        return []
    table = ledger if ledger is not None else ledger_bindings(connection, live_only=live_only)
    found: list[dict[str, Any]] = []
    for digest in sorted(hashes):
        found.extend(dict(row) for row in table.get(digest, ()))
    return found


def ledger_bindings(connection: sqlite3.Connection, *, live_only: bool = True
                    ) -> dict[str, list[dict[str, Any]]]:
    """source content hash -> the Claims citing it, from one pass over the Ledger.

    One read of the supporting Evidence (the chain the recheck reads a binding
    from), meant to be done once per extraction run and kept.
    """

    from .claim_retirement import retired_claim_version_refs

    present = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not {"transcript_claim_citation_bindings", "evidence_relations", "evidence_versions",
            "claim_versions"} <= present:
        return {}  # a Core that has never bound a citation cites nothing
    bindings = {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
        "SELECT binding_id, source_content_hash, source_start, source_end "
        "FROM transcript_claim_citation_bindings").fetchall()}
    if not bindings:
        return {}
    retired = retired_claim_version_refs(connection) if live_only else set()
    found: dict[str, list[dict[str, Any]]] = {}
    for ref, evidence, claim in connection.execute(
        "SELECT r.claim_version_id, e.evidence_json, c.claim_json FROM evidence_relations r "
        "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
        "JOIN claim_versions c ON c.claim_version_id=r.claim_version_id "
        "WHERE r.relation='supports' AND instr(e.evidence_json, ?)>0", (BINDING_PREFIX,),
    ).fetchall():
        if ref in retired:
            continue
        record = json.loads(evidence)
        binding_ref = next((item.get("ref") for item in record.get("artifact_refs") or ()
                            if isinstance(item, Mapping) and item.get("ref") in bindings), None)
        if binding_ref is None:
            continue
        digest, start, end = bindings[binding_ref]
        body = json.loads(claim)
        found.setdefault(digest, []).append({
            "ref": ref, "subject_ref": body.get("subject_ref"),
            "statement": str(body.get("normalized_statement") or ""),
            "candidate_ref": body.get("candidate_origin_ref"),
            "start": start, "end": end})
    return found


# -- the plan ----------------------------------------------------------------------

def plan_document_admissions(candidates: Sequence[Mapping[str, Any]],
                             live: Sequence[Mapping[str, Any]], *,
                             cap: int = MAX_CLAIMS_PER_DOCUMENT) -> dict[str, dict[str, Any]]:
    """key -> decision for every candidate of one document.

    ``candidates``: ``{"key", "subject_ref", "statement", "start", "end"}`` in
    reading order (window, then suggestion).  ``live``: :func:`document_claims`
    of the same document.  A decision is ``{"decision": "admit"}``, or
    ``"duplicate"`` with ``duplicate_of`` (a live Claim ref, or another
    candidate's key), ``basis`` (``same_span`` / ``same_finding``) and
    ``similarity``, or ``"truncated"`` with its ``score`` and the ``cap``.
    """

    decisions: dict[str, dict[str, Any]] = {}
    order = {candidate["key"]: index for index, candidate in enumerate(candidates)}
    scores = {candidate["key"]: information_score(candidate["statement"]) for candidate in candidates}
    # 1. Against what the Ledger already has: the same span, then the same finding.
    open_candidates = []
    for candidate in candidates:
        same = [row for row in live if row.get("subject_ref") == candidate["subject_ref"]]
        span = next((row for row in same if (row.get("start"), row.get("end"))
                     == (candidate.get("start"), candidate.get("end"))), None)
        if span is not None:
            decisions[candidate["key"]] = {"decision": "duplicate", "duplicate_of": span["ref"],
                                           "candidate_ref": span.get("candidate_ref"),
                                           "basis": "same_span", "similarity": None}
            continue
        match = None
        for row in same:
            similarity = same_finding(candidate["statement"], row.get("statement") or "")
            if similarity is not None and (match is None or similarity > match[1]):
                match = (row, similarity)
        if match is not None:
            decisions[candidate["key"]] = {"decision": "duplicate", "duplicate_of": match[0]["ref"],
                                           "candidate_ref": match[0].get("candidate_ref"),
                                           "basis": "same_finding", "similarity": match[1]}
            continue
        open_candidates.append(candidate)
    # 2. Among themselves, most informative first: a later restatement is the duplicate.
    ranked = sorted(open_candidates, key=lambda c: (-scores[c["key"]], order[c["key"]]))
    kept: list[Mapping[str, Any]] = []
    for candidate in ranked:
        match = None
        for other in kept:
            if other["subject_ref"] != candidate["subject_ref"]:
                continue
            # Two statements of one span in one pass are two findings the
            # drafter chose to make unless they say the same thing; only a
            # span the Ledger already cites is a duplicate by position.
            similarity = same_finding(candidate["statement"], other["statement"])
            if similarity is not None and (match is None or similarity > match[1]):
                match = (other, similarity)
        if match is not None:
            decisions[candidate["key"]] = {"decision": "duplicate", "duplicate_of": match[0]["key"],
                                           "basis": "same_finding", "similarity": match[1]}
            continue
        kept.append(candidate)
    # 3. The cap, per subject, counting what the Ledger already holds.
    held_live: dict[str, int] = {}
    for row in live:
        held_live[str(row.get("subject_ref"))] = held_live.get(str(row.get("subject_ref")), 0) + 1
    taken: dict[str, int] = {}
    for candidate in kept:  # already most informative first
        subject = str(candidate["subject_ref"])
        room = max(0, int(cap) - held_live.get(subject, 0))
        if taken.get(subject, 0) >= room:
            decisions[candidate["key"]] = {"decision": "truncated", "score": scores[candidate["key"]],
                                           "cap": int(cap), "already_in_ledger": held_live.get(subject, 0)}
            continue
        taken[subject] = taken.get(subject, 0) + 1
        decisions[candidate["key"]] = {"decision": "admit", "score": scores[candidate["key"]]}
    return decisions


# -- a document's admission, remembered past its review (2026-09-29) -------------------

_OUTCOME_SCHEMA = "document_admission_schema.sql"


class DocumentAdmissionOutcomes:
    """The authority over ``document_admission_outcomes``; its constructor applies the schema."""

    def __init__(self, store: Any) -> None:
        from pathlib import Path

        from .store import authorization_flag

        self.connection = store.connection
        self._flag = authorization_flag(self.connection, "dalton_document_admission_authorized")
        self.connection.executescript(
            Path(__file__).with_name(_OUTCOME_SCHEMA).read_text(encoding="utf-8"))

    def record(self, *, review: Mapping[str, Any], candidate_claim_ref: str,
               claim_version_refs: Sequence[str], admitted: int, held: int, actor_ref: str,
               now: str | None = None) -> dict[str, Any]:
        """Write down that ``review``'s document was read to the end and admitted.

        Called by the extraction child when the last window of a review has
        gone through admission, before the review is closed -- so the
        conclusion survives a close that fails (the mission moved under it).
        Idempotent per review.
        """

        from datetime import datetime, timezone

        from .store import canonical_json, content_hash

        body = {
            "schema_version": "0.1", "rule_ref": RULE_REF, "review_id": review["review_id"],
            "document_ref": review["document_ref"], "company_ref": review["company_ref"],
            "source_ref": review["source_ref"], "mission_version_ref": review["mission_version_ref"],
            "candidate_claim_ref": candidate_claim_ref,
            "claim_version_refs": sorted({str(ref) for ref in claim_version_refs if ref}),
            "admitted": int(admitted), "held": int(held), "actor_ref": actor_ref,
            "created_at": now or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        }
        body["content_hash"] = content_hash(body)
        self._flag.authorized = True
        try:
            self.connection.execute(
                "INSERT OR IGNORE INTO document_admission_outcomes(review_id,document_ref,company_ref,"
                "source_ref,mission_version_ref,candidate_claim_ref,claim_version_refs_json,admitted,"
                "held,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (body["review_id"], body["document_ref"], body["company_ref"], body["source_ref"],
                 body["mission_version_ref"], candidate_claim_ref,
                 canonical_json(body["claim_version_refs"]), body["admitted"], body["held"],
                 canonical_json(body), body["content_hash"], body["created_at"]))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self._flag.authorized = False
        return body


def admission_outcome(connection: Any, *, document_ref: Any, company_ref: Any,
                      exclude_review_id: Any = None) -> dict[str, Any] | None:
    """The earliest recorded admission of this document for this company, or None.

    ``exclude_review_id``: the review asking, so that a review never carries
    its own conclusion.  A Core without the table (nothing recorded yet)
    answers None.
    """

    try:
        row = connection.execute(
            "SELECT record_json FROM document_admission_outcomes WHERE document_ref=? AND company_ref=? "
            "AND review_id<>? ORDER BY created_at, review_id LIMIT 1",
            (document_ref, company_ref, exclude_review_id or ""),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else json.loads(row[0])


def carried_conclusion_rationale(outcome: Mapping[str, Any]) -> str:
    """The rationale of a review closed on an earlier read's conclusion."""

    return (f"{RULE_REF}: this document was read to the end and admitted under "
            f"{outcome['mission_version_ref']} (review {outcome['review_id']}: "
            f"{outcome['admitted']} Claim(s) admitted, {outcome['held']} held for human review); "
            "that conclusion is carried, and the document is not read again")


# -- an SEC filing's own period ------------------------------------------------------

_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
_COVER_RE = re.compile(
    r"(?i)for\s+the\s+(fiscal\s+year|quarterly\s+period|transition\s+period)\s+ended\s+"
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,\s*((?:19|20)\d{2})")
_FORM_RE = re.compile(r"(?i)\bFORM\s+(10-K|10-Q|20-F|40-F)(?:/A)?\b")
_EDGAR_URL_RE = re.compile(
    r"(?i)^https?://(?:www\.)?sec\.gov/Archives/edgar/data/\d+/(\d{18})/[^/]*?(?:-(\d{8}))?\.html?$")
#: How far into a filing's text the cover page is looked for.  An inline-XBRL
#: filing renders its hidden header first -- EPAM's 10-K cover starts at
#: character 44,922.
COVER_CHARS = 120_000
#: The latest a periodic report is due after its period ends (Exchange Act
#: rules 13a-1/13a-13 for a non-accelerated filer): the last day its author can
#: have known anything, for the temporal check.
FILING_DEADLINE_DAYS = {"10-K": 90, "20-F": 120, "40-F": 90, "10-Q": 45}


def is_sec_filing(source_ref: Any, url: Any) -> bool:
    return source_ref == "source:sec-edgar" or (
        isinstance(url, str) and _EDGAR_URL_RE.match(url) is not None)


def sec_filing_period(text: Any, url: Any = None) -> dict[str, Any] | None:
    """What period an SEC periodic report covers, from its cover page, or None.

    ``{"form", "period_end", "covers", "label", "known_by", "basis"}``.  The cover's
    "For the fiscal year ended December 31, 2025" (or "quarterly period") is
    the authority; failing it, an EDGAR document name's ``-YYYYMMDD`` suffix,
    which is the period of report by EDGAR's inline-XBRL naming.  The label is
    what every statement of the filing is filed under: ``FY2025 (fiscal year
    ended 2025-12-31)`` or ``quarter ended 2025-09-30``.
    """

    head = text[:COVER_CHARS] if isinstance(text, str) else ""
    form_match = _FORM_RE.search(head)
    form = form_match.group(1).upper() if form_match else None
    period_end = kind = None
    cover = _COVER_RE.search(head)
    basis = None
    if cover is not None:
        month = _MONTHS.get(cover.group(2).lower().rstrip("."))
        if month is not None:
            try:
                period_end = date(int(cover.group(4)), month, int(cover.group(3)))
                kind = cover.group(1).lower().split()[0]
                basis = "cover_page"
            except ValueError:
                period_end = None
    if period_end is None and isinstance(url, str):
        match = _EDGAR_URL_RE.match(url)
        if match is not None and match.group(2):
            try:
                period_end = date(int(match.group(2)[:4]), int(match.group(2)[4:6]),
                                  int(match.group(2)[6:]))
                basis = "edgar_document_name"
            except ValueError:
                period_end = None
    if period_end is None:
        return None
    if kind is None:
        kind = "quarterly" if form == "10-Q" else "fiscal"
    if form is None:
        form = "10-Q" if kind == "quarterly" else "10-K"
    if kind == "quarterly":
        covers = label = f"quarter ended {period_end.isoformat()}"
    elif kind == "transition":
        covers = label = f"transition period ended {period_end.isoformat()}"
    else:
        # A fiscal year is named for the calendar year it ends in (EPAM's
        # ends 2025-12-31: FY2025; one ending 2026-03-31 is FY2026).
        covers = f"fiscal year ended {period_end.isoformat()}"
        label = f"FY{period_end.year} ({covers})"
    known_by = period_end + timedelta(days=FILING_DEADLINE_DAYS.get(form, 90))
    return {"form": form, "period_end": period_end.isoformat(), "covers": covers,
            "label": label, "known_by": known_by.isoformat(), "basis": basis}


__all__ = [
    "MAX_CLAIMS_PER_DOCUMENT",
    "DocumentAdmissionOutcomes",
    "admission_outcome",
    "carried_conclusion_rationale",
    "PRE_ADMISSION_CONTAINMENT",
    "PRE_ADMISSION_RATIO",
    "RULE_REF",
    "document_claims",
    "information_score",
    "is_sec_filing",
    "ledger_bindings",
    "plan_document_admissions",
    "same_finding",
    "sec_filing_period",
]
