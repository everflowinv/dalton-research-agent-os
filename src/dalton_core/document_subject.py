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

#: Ticker (or ``industry:`` ref) to the names a document may call it by. Built
#: per mission; see ``mission_company_names``.
NameTable = Mapping[str, Sequence[str]]

# Display names for the covered tickers. Kept small and explicit: a wrong name
# here weakens a check, so it is a list somebody maintains rather than a guess
# derived from a ref.
#
# W7: this is now a *fallback*, not the answer. It is the legacy Core's five
# issuers, and a second environment covering other companies was refused by
# every feed lane on the strength of this dict -- "MSFT has no company names to
# match a subject line against" -- which named a missing row in a shared table
# as though it were a fact about the research. The answer is a per-mission
# table (``mission_company_names.mission_name_table``) that each caller passes
# in; this dict is consulted only when no table was supplied, which is what
# keeps the legacy Core working unchanged.
#
# 2026-09-24: the hyperscalers and the Chinese names are here too, because a
# workspace's own table used to *replace* this one and so could never pick up
# a name added here; ``mission_name_table`` now unions the two, so a name added
# below reaches every environment on the next deploy.  A name somebody needs
# before a deploy goes through ``company_aliases`` (append-only, audited).
COMPANY_NAMES: Mapping[str, tuple[str, ...]] = {
    "ACN": ("Accenture", "Accenture plc", "埃森哲"),
    "CTSH": ("Cognizant", "Cognizant Technology Solutions", "高知特"),
    "EPAM": ("EPAM Systems", "EPAM"),
    "IBM": ("IBM", "International Business Machines", "国际商业机器",
            "Red Hat", "HashiCorp", "Confluent", "watsonx"),
    "DXC": ("DXC Technology", "DXC"),
    "GOOGL": ("Alphabet", "Alphabet Inc.", "Google", "谷歌", "GOOG", "Gemini", "YouTube"),
    "AMZN": ("Amazon", "Amazon.com", "亚马逊", "AWS", "Amazon Web Services"),
    "META": ("Meta Platforms", "Meta", "Facebook", "脸书", "Instagram", "WhatsApp"),
    "MSFT": ("Microsoft", "微软", "Azure", "Microsoft Azure"),
}
#: Names of a company's *products*, not of the company.  They name the subject
#: when they appear -- a note about AWS margins is Amazon's -- but they do not
#: name an *issuer*: "Azure vs AWS" in Microsoft's call title is the business
#: being discussed, not a second company presenting.  So the ambiguity check
#: that refuses a title naming two covered issuers does not count them (see
#: ``earnings_call_names_issuer``); everywhere else they are ordinary names.
#:
#: 2026-09-24b: Gemini and YouTube (Alphabet's), Red Hat, HashiCorp, Confluent
#: and watsonx (IBM's) join them.  The three acquisitions are brands for the
#: same reason AWS is: none is a covered issuer, so naming one next to a
#: covered company is that company's business, never a second presenter.
#: They are also left out of the *issuer position* of a call title, which a
#: product never holds: "Confluent Q4 2025 Earnings Call" is Confluent's own
#: call from before IBM owned it, not IBM's.  "GOOG" is not here: it is
#: Alphabet's other share class, i.e. the issuer itself.
#:
#: Executives (Jassy, Pichai, Zuckerberg, Nadella, Krishna) are deliberately
#: in neither table: a person is quoted about other companies, moves between
#: them, and would make a span about anything they said "name" the company.
BRAND_NAMES: frozenset[str] = frozenset({
    "AWS", "Amazon Web Services", "Azure", "Microsoft Azure", "Instagram", "WhatsApp",
    "Gemini", "YouTube", "Red Hat", "HashiCorp", "Confluent", "watsonx",
})
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


def subject_names(ticker: Any, names: NameTable | None = None) -> tuple[str, ...]:
    """Every name this subject is called, best first, for a text check.

    Takes a ticker or an ``industry:`` ref: both are subjects a figure can
    belong to, and both have to be recognised in a document's own words.

    ``names`` is the mission's own ticker-to-names table. It wins outright
    when it has an entry, because it was built from the mission that is
    actually running; the packaged dict answers only when it does not, which
    is the legacy Core and nothing else.
    """

    if not isinstance(ticker, str) or not ticker.strip():
        return ()
    if ticker.startswith("industry:"):
        if names is not None and ticker in names:
            return tuple(str(name) for name in names[ticker] if str(name).strip())
        return tuple(INDUSTRY_NAMES.get(ticker, ()))
    key = ticker.strip().upper()
    if names is not None and key in names:
        found = tuple(str(name) for name in names[key] if str(name).strip())
    else:
        found = tuple(COMPANY_NAMES.get(key, ()))
    if len(key) >= MIN_TICKER_CHARS and key not in {n.upper() for n in found}:
        found = found + (key,)
    return found


def subject_label(ticker: Any, names: NameTable | None = None) -> str:
    """How to name the subject to a model. A CIK ref tells it nothing."""

    names = subject_names(ticker, names)
    if not names:
        return "the company under coverage"
    if len(names) == 1:
        return names[0]
    return f"{names[0]} ({', '.join(names[1:])})"


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _fold_cjk(text: str) -> str:
    """NFKC, lowercased, all whitespace removed: how a CJK name is compared."""

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)).lower())


def _mentions(text: str, names: Sequence[str]) -> list[str]:
    folded = _fold(text)
    cjk_text: str | None = None
    found = []
    for name in names:
        if _CJK_RE.search(str(name)):
            # ``_fold`` keeps [a-z0-9] only, so "谷歌" folded to nothing and was
            # silently skipped, and "IT 服务" folded to "it" and matched every
            # English "it".  A CJK name has no word boundaries to respect: it
            # is a substring test on the NFKC text with whitespace removed.
            if cjk_text is None:
                cjk_text = _fold_cjk(text)
            if cjk_text and _fold_cjk(name) in cjk_text:
                found.append(name)
            continue
        if not folded:
            continue
        needle = _fold(name)
        if not needle:
            continue
        # Word-boundary match, so "IBM" does not fire inside another token and
        # a ticker cannot be found in the middle of an unrelated word.
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", folded):
            found.append(name)
    return found


def document_names_subject(text: Any, subject: Any,
                           names: NameTable | None = None) -> dict[str, Any]:
    """Whether this document names the subject, and which name it used.

    The answer is a floor: a document that never names the subject is not about
    it, and a document that does may still be an industry report where only
    some figures are that company's. The second half is the model's job.
    """

    names = subject_names(subject, names)
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


def earnings_call_names_issuer(title: Any, subject: Any,
                               names: NameTable | None = None) -> dict[str, Any]:
    """Whether a transcript title names the subject in the issuer position."""
    table = names
    names = subject_names(subject, table)
    if not names or not isinstance(title, str):
        return {"checked": bool(names), "names_issuer": False, "matched": []}
    folded = _fold(title)
    quarter = re.search(
        r"(?:q[1-4]\s+(?:fy\s*)?20\d{2}|(?:fy\s*)?20\d{2}\s*q[1-4]|"
        r"[1-4]q\s+(?:fy\s*)?20\d{2})",
        folded,
    )
    if quarter is None:
        return {"checked": True, "names_issuer": False, "matched": []}
    call = re.search(
        r"(?:earnings\s+(?:conference\s+)?call|conference\s+call|post\s+call|investor\s+call)",
        folded[quarter.end():],
    )
    issuer_zone = folded[:quarter.start()]
    if call is not None:
        issuer_zone += " " + folded[quarter.end():quarter.end() + call.end()]
    brands = {brand.casefold() for brand in BRAND_NAMES}
    matched = _mentions(issuer_zone, [name for name in names if name.casefold() not in brands])
    # The other covered issuers, so a title naming two of them is refused as
    # ambiguous. Read from the mission's table when there is one: on a
    # workspace the packaged five are not the covered set and would make a
    # "Cognizant" in an Amazon title invisible.
    # Product names (``BRAND_NAMES``) are left out: a competitor's cloud named
    # in a call title is the market being discussed, not a second issuer.
    source = table if table is not None else COMPANY_NAMES
    other_names = tuple(
        str(name) for key, aliases in source.items()
        if not str(key).startswith("industry:")
        for name in aliases
        if str(name) not in names and str(name).casefold() not in brands
    )
    ambiguous = _mentions(issuer_zone, other_names)
    return {"checked": True, "names_issuer": bool(matched) and not ambiguous,
            "matched": matched, "ambiguous_with": ambiguous}


__all__ = [
    "BRAND_NAMES",
    "COMPANY_NAMES",
    "NameTable",
    "INDUSTRY_NAMES",
    "MIN_TICKER_CHARS",
    "SCHEMA_VERSION",
    "document_names_subject",
    "earnings_call_names_issuer",
    "subject_label",
    "subject_names",
]
