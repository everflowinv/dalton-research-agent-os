"""When two directed-document inquiries ask the same thing (2026-09-30).

The planner rephrases every inquiry on every plan, and a new mission version
or plan gives each rephrasing a new inquiry and question identity.  Keyed on
exact identity, the admission gate therefore bought the same question again
and again: legacy DXC "970 / 9.7 亿" about 17 times, CTSH 15.8% about 9, IBM
33.8 and CTSH 5000 three times each, ws-7d Google Cloud RPO 15 times.

The rule (``EQUIVALENT_INQUIRY_RULE_REF``) is deliberately small and fully
deterministic, so it can be tested and explained:

* the subject is the company (``company_ref``) and, when the plan typed one,
  the exact evidence target (a financial-note target reads one note, not the
  document); inquiries about different subjects are never equivalent;
* the normalized question is the question with Unicode compatibility folding,
  case folding, and every character that is not a letter or digit removed --
  equal normalized questions are equivalent;
* the figures a question names are compared by their significant digits, so
  "970", "$970 million", "9.7 亿美元" and "$0.97 billion" are one figure
  ("97"), and "15.8%" is "158".  Two questions that both name figures and
  share one are equivalent.  Not figures: four-digit years, form names such as
  10-K / 20-F, period labels such as FY2025 or Q3, accession and CIK-like
  identifiers (seven or more digits), and bare counts below ten ("5 份申报").

Replayed offline over the live admission history (2026-09-30, read-only), it
would have refused 86 of legacy's 147 admissions (34 while an equivalent was
in flight or held, 52 within three days of one being answered) and 16 of
ws-7d's 61; every refusal names the earlier admission it waits for.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from .store import content_hash

EQUIVALENT_INQUIRY_RULE_REF = "mission-document-research:equivalent-inquiry:v1"
# How long an answered inquiry keeps an equivalent from being bought again.
# An answer is new evidence the plan will see on its next pass; three days is
# several plans, and after it the same figure may honestly be asked again of
# a newer document.
ANSWERED_COOLDOWN = timedelta(hours=72)

_ACCESSION = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_FORM = re.compile(r"(?<![\w.])\d{1,2}-[A-Za-z]{1,2}\b")
_PERIOD = re.compile(r"(?i)\b(?:fy|cy|q[1-4]|h[12])\s*'?\d{2,4}\b")
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class InquirySignature:
    company_ref: str
    text: str
    figures: frozenset[str]
    target: str | None = None


#: 2026-10-09: Chinese structural particles that never change what is asked.
#: Live ws-7d, two MSFT admissions four hours apart asked 「MSFT FY2025 末融资
#: 租赁负债有多少？…」 and 「MSFT FY2025 末的融资租赁负债有多少？…」; the one
#: 的 made them different questions and both were bought.  Only particles with
#: no lexical use of their own are dropped (not 地/得, which are also nouns
#: and verbs: 地区, 获得).
_FILLER_PARTICLES = frozenset("的之了")


def normalize_question(question: str) -> str:
    folded = unicodedata.normalize("NFKC", question).casefold()
    return "".join(ch for ch in folded
                   if unicodedata.category(ch)[0] in "LN" and ch not in _FILLER_PARTICLES)


def question_figures(question: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", question)
    for pattern in (_ACCESSION, _FORM, _PERIOD):
        text = pattern.sub(" ", text)
    figures = set()
    for match in _NUMBER.finditer(text):
        whole = match.group(1).replace(",", "")
        fraction = match.group(2) or ""
        if not fraction:
            if len(whole) == 4 and whole[:2] in {"19", "20"}:
                continue            # a year
            if int(whole) < 10:
                continue            # a count or an ordinal
            if len(whole) >= 7:
                continue            # an accession, CIK or other identifier
        digits = (whole + fraction).lstrip("0").rstrip("0")
        if digits:
            figures.add(digits)
    return frozenset(figures)


# -- figures in running text (2026-09-30, self-contained claims) ---------------
#
# ``question_figures`` above is the equivalence rule's own reading and stays
# exactly as it is (``EQUIVALENT_INQUIRY_RULE_REF`` v1).  It bounds a number by
# ``\w``, and in Chinese every ideograph is ``\w``: "利润率15.8%" names no
# figure to it.  A drafted statement is running prose, often Chinese, so the
# self-contained rule reads figures with the same significant-digit identity
# and the same exclusions but bounds them by ASCII letters and digits only.
_TEXT_ACCESSION = re.compile(r"(?<![0-9])\d{10}-\d{2}-\d{6}(?![0-9])")
_TEXT_FORM = re.compile(r"(?<![A-Za-z0-9.])\d{1,2}-[A-Za-z]{1,2}(?![A-Za-z])")
_TEXT_PERIOD = re.compile(
    r"(?i)(?<![A-Za-z])(?:fy|cy|q[1-4]|h[12])\s*'?\d{2,4}(?![0-9])")
_TEXT_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![0-9])")
# One written figure with its sign: "$970", "US$ 9.7", "15.8%", "15.8 %".
_TEXT_FIGURE_TOKEN = re.compile(
    r"(?:US\$|[$¥€£])?\s?(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![0-9])"
    r"(?:\s?%)?")


def _significant(whole: str, fraction: str) -> str | None:
    whole = whole.replace(",", "")
    if not fraction:
        if len(whole) == 4 and whole[:2] in {"19", "20"}:
            return None             # a year
        if int(whole) < 10:
            return None             # a count or an ordinal
        if len(whole) >= 7:
            return None             # an accession, CIK or other identifier
    return (whole + fraction).lstrip("0").rstrip("0") or None


def _masked(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for pattern in (_TEXT_ACCESSION, _TEXT_FORM, _TEXT_PERIOD):
        text = pattern.sub(lambda match: " " * len(match.group(0)), text)
    return text


def text_figures(text: str) -> frozenset[str]:
    """The figures running text names, as significant digits ("15.8%" -> "158")."""

    found = set()
    for match in _TEXT_NUMBER.finditer(_masked(text)):
        digits = _significant(match.group(1), match.group(2) or "")
        if digits:
            found.add(digits)
    return frozenset(found)


def figure_spellings(text: str) -> tuple[str, ...]:
    """The figures as the text writes them ("15.8%", "$970"), in order, once each."""

    normalized = unicodedata.normalize("NFKC", text)
    masked = _masked(text)
    spellings: list[str] = []
    for match in _TEXT_FIGURE_TOKEN.finditer(masked):
        if _significant(match.group(1), match.group(2) or ""):
            spelling = normalized[match.start():match.end()].strip()
            if spelling not in spellings:
                spellings.append(spelling)
    return tuple(spellings)


def strip_figures(text: str, figures: frozenset[str] | set[str]) -> str:
    """``text`` with every written figure whose significant digits are in ``figures`` blanked.

    The sign travels with the figure ("$970", "15.8%"), so what is left carries
    no trace of it; every other number is left exactly where it was.
    """

    if not figures:
        return text
    normalized = unicodedata.normalize("NFKC", text)
    masked = _masked(text)
    pieces: list[str] = []
    last = 0
    for match in _TEXT_FIGURE_TOKEN.finditer(masked):
        if _significant(match.group(1), match.group(2) or "") in figures:
            pieces.append(normalized[last:match.start()])
            pieces.append(" ")
            last = match.end()
    pieces.append(normalized[last:])
    return "".join(pieces)


def inquiry_signature(company_ref: str, question: str,
                      evidence_target: Any = None) -> InquirySignature:
    return InquirySignature(company_ref=company_ref,
                            text=normalize_question(question),
                            figures=question_figures(question),
                            target=None if evidence_target is None
                            else content_hash(evidence_target))


def inquiry_signature_for(company_ref: str, inquiry: Mapping[str, Any]) -> InquirySignature:
    strategy = inquiry.get("directed_document")
    target = strategy.get("evidence_target") if isinstance(strategy, Mapping) else None
    return inquiry_signature(company_ref, str(inquiry.get("question") or ""), target)


def signatures_equivalent(a: InquirySignature, b: InquirySignature) -> str | None:
    """What makes the two equivalent -- ``"question"`` or ``"figure:<digits>"`` -- or None."""

    if a.company_ref != b.company_ref or a.target != b.target:
        return None
    if a.text and a.text == b.text:
        return "question"
    shared = a.figures & b.figures
    if shared:
        return "figure:" + ",".join(sorted(shared))
    return None


__all__ = [
    "ANSWERED_COOLDOWN", "EQUIVALENT_INQUIRY_RULE_REF", "InquirySignature",
    "figure_spellings", "inquiry_signature", "inquiry_signature_for", "normalize_question",
    "question_figures", "signatures_equivalent", "strip_figures", "text_figures",
]
