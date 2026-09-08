"""P13c: is this document actually about the company it was filed under?

The discovery search is free text -- "EPAM Systems EPAM earnings call
transcript" -- and nothing checked that a result was about EPAM.  It returned a
Haier European-business call and an EOS call, and the figures pass recorded
"EPAM revenue = 14.3 billion RMB".  Every digit was verified against the bytes
it cited; the bytes were about someone else, which no digit check can see.

The obvious repair -- filter the search by company -- is the wrong one.  An
industry report worth reading often carries no company tag at all, and a
sell-side note comparing five vendors belongs to none of them.  Filtering would
throw away exactly the documents that are hardest to find and most worth
having.

So the check moves to where the number is taken, and it has two halves that
fail differently:

* **the document must name the company.**  Deterministic, cheap, and decisive
  in the case that actually happened: the Haier transcript never says EPAM, and
  an industry report that genuinely covers EPAM does.  This is a floor, not a
  guarantee -- naming a company is not the same as a figure being that
  company's.
* **the figure must be that company's**, which only a reader can judge, so the
  extraction prompt says who the subject is by name and requires the model to
  say whose figure it reported.  Recorded rather than trusted: it is a
  judgement, and it travels with the figure so a reviewer can see it.

Neither half is sufficient alone. Together they refuse the failure that
occurred without refusing the documents the research needs.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "0.1"

# Display names for the covered tickers. Kept small and explicit: a wrong name
# here weakens a check, so it is a list somebody maintains rather than a guess
# derived from a ref.
COMPANY_NAMES: Mapping[str, tuple[str, ...]] = {
    "ACN": ("Accenture",),
    "CTSH": ("Cognizant",),
    "EPAM": ("EPAM Systems", "EPAM"),
    "IBM": ("IBM", "International Business Machines"),
    "DXC": ("DXC Technology", "DXC"),
}
# What each covered industry is called, for the same check. An industry screen
# rests on facts about the market -- how demand is moving, how the field is
# arranged -- and those belong to no company, so the industry is a subject in
# its own right and its documents have to be attributed too. A market report
# about European white goods is no more this industry's than the Haier call
# was EPAM's.
INDUSTRY_NAMES: Mapping[str, tuple[str, ...]] = {
    "industry:us-it-services": (
        "IT services", "IT service", "information technology services",
        "technology consulting", "IT consulting", "IT 服务",
    ),
}
# A ticker shorter than this is too easy to hit by accident inside ordinary
# prose, so it is not used as evidence on its own.
MIN_TICKER_CHARS = 3


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def subject_names(ticker: Any) -> tuple[str, ...]:
    """Every name this subject is called, best first, for a text check.

    Takes a ticker or an ``industry:`` ref: both are subjects a figure can
    belong to, and both have to be recognised in a document's own words.
    """

    if not isinstance(ticker, str) or not ticker.strip():
        return ()
    if ticker.startswith("industry:"):
        return tuple(INDUSTRY_NAMES.get(ticker, ()))
    key = ticker.strip().upper()
    names = tuple(COMPANY_NAMES.get(key, ()))
    if len(key) >= MIN_TICKER_CHARS and key not in {n.upper() for n in names}:
        names = names + (key,)
    return names


def subject_label(ticker: Any) -> str:
    """How to name the subject to a model. A CIK ref tells it nothing."""

    names = subject_names(ticker)
    if not names:
        return "the company under coverage"
    if len(names) == 1:
        return names[0]
    return f"{names[0]} ({', '.join(names[1:])})"


def _mentions(text: str, names: Sequence[str]) -> list[str]:
    folded = _fold(text)
    if not folded:
        return []
    found = []
    for name in names:
        needle = _fold(name)
        if not needle:
            continue
        # Word-boundary match, so "IBM" does not fire inside another token and
        # a ticker cannot be found in the middle of an unrelated word.
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", folded):
            found.append(name)
    return found


def document_names_subject(text: Any, subject: Any) -> dict[str, Any]:
    """Whether this document names the subject, and which name it used.

    The answer is a floor: a document that never names the subject is not about
    it, and a document that does may still be an industry report where only
    some figures are that company's. The second half is the model's job.
    """

    names = subject_names(subject)
    if not names:
        # Nothing to check against. Refusing here would block every company
        # nobody has named, which is a configuration gap, not evidence.
        return {"schema_version": SCHEMA_VERSION, "checked": False,
                "names_subject": False, "matched": [], "names": []}
    matched = _mentions(text if isinstance(text, str) else "", names)
    return {
        "schema_version": SCHEMA_VERSION,
        "checked": True,
        "names_subject": bool(matched),
        "matched": matched,
        "names": list(names),
    }


__all__ = [
    "COMPANY_NAMES",
    "INDUSTRY_NAMES",
    "MIN_TICKER_CHARS",
    "SCHEMA_VERSION",
    "document_names_subject",
    "subject_label",
    "subject_names",
]
