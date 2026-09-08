"""P11v: how a figure came to be known, decided by the document it came from.

A number read out of a 10-K is the company's own published figure.  The same
number read out of an earnings call is a record that someone *said* it: it may
be rounded, approximated, corrected later in the same call, or mis-transcribed.
Both are worth keeping.  They are not worth the same, and a system that stores
them identically has thrown away the difference at exactly the moment it could
still be recorded for free.

So every figure carries a grade, and the grade is *derived from the kind of
document*, never asked of the model.  A model asked "how confident are you"
answers about its own reasoning, which is not the question; the question is
what the citation is a citation of, and the discovery plan already knows that
before anything is read.

Two grades, because the live plan has two kinds of document a figure may be
taken from at all:

``company-filed-document``
    The company published this number -- an annual or quarterly report, or an
    earnings release.  The document *is* the assertion.

``earnings-call-transcript``
    Management said this number aloud and the transcript records the saying.
    True to the call; one step from the filed statements.

A document kind with no grade is not read for figures at all, and that is the
other half of this module's job.  A sell-side note quotes numbers constantly,
and some of them are the analyst's own estimate rather than the company's
result: "we model revenue of $17.9bn" passes a digit check perfectly and is not
a fact about the company.  Sell-side research is read for the *names* of the
measures the market watches (that is ``metric_discovery_extraction``); it is
not read for values.
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA_VERSION = "0.1"

FILED = "company-filed-document"
SPOKEN = "earnings-call-transcript"

# The document kinds a figure may be taken from, and what taking one means.
# Anything absent is not read for figures; see the module docstring for why
# sell-side research in particular is absent on purpose.
GRADE_BY_SPEC: Mapping[str, str] = {
    "annual-report-10k": FILED,
    "quarterly-report-10q": FILED,
    "company-press-release": FILED,
    "earnings-call-transcripts": SPOKEN,
}

# What each grade means, in the Ledger's own ``basis`` vocabulary -- the field
# that already says how a claim came to be known (``official-filing-xbrl``,
# ``Earnings call commentary``).  A reader of one claim sees the grade without
# following a reference.
BASIS_BY_GRADE: Mapping[str, str] = {
    FILED: "company-filed-document",
    SPOKEN: "earnings-call-transcript-spoken",
}

# The sentence a claim adds about itself. Short, because it is appended to a
# statement that already says what the figure is.
QUALIFIER_BY_GRADE: Mapping[str, str] = {
    FILED: "as published by the company in this document",
    SPOKEN: "as spoken on the earnings call and recorded in this transcript; "
            "not read from a filed statement",
}

GRADES: tuple[str, ...] = (FILED, SPOKEN)

# P12h: whether the document is *known to be about the company* it was filed
# under, and how.
#
# A SEC filing is attributed by construction: its accession belongs to one
# CIK, and the lane derived the document from that company's own filing index.
# An AlphaEngine document is attributed by a free-text search -- "EPAM Systems
# EPAM earnings call transcript" -- and nothing checks the result. Live, that
# put a Haier European-business call (RMB, refrigerators) and an EOS call (AUD)
# into EPAM's queue, and the figures pass dutifully recorded "EPAM revenue =
# 14.3 billion RMB". Every digit was verified against the bytes it cited. The
# bytes were about another company.
#
# So a figure needs a grade *and* an attribution. Some document kinds are
# attributed by construction: a filing's accession belongs to one CIK, and the
# lane derived the document from that company's own index. Everything else has
# to earn it by naming the company -- see ``document_subject``.
#
# Filtering the search by company was the tempting repair and the wrong one: an
# industry report worth reading often carries no company tag, and a note
# comparing five vendors belongs to none of them. The check belongs where the
# number is taken, not where the document is found.
ATTRIBUTED_BY_SPEC: Mapping[str, str] = {
    "annual-report-10k": "sec-accession",
    "quarterly-report-10q": "sec-accession",
}


def attribution_for(spec_ref: Any) -> str | None:
    """How this document kind is bound to the company by construction, if it is."""

    if not isinstance(spec_ref, str):
        return None
    return ATTRIBUTED_BY_SPEC.get(spec_ref)


def figure_recordable(spec_ref: Any, *, document_names_subject: bool = False) -> bool:
    """Whether a figure from this document may be recorded against this company.

    Either the kind attributes it by construction, or the document itself named
    the company. A document that never names the company is not about it, and
    that is exactly the case that put a Haier earnings call into EPAM's file.
    """

    if not figure_worthy(spec_ref):
        return False
    return attribution_for(spec_ref) is not None or bool(document_names_subject)


class FigureGradeError(ValueError):
    """A figure was graded against a document kind that carries no grade."""


def grade_for(spec_ref: Any) -> str | None:
    """The grade a figure read from this kind of document carries, or None.

    None is not a failure and not "unknown": it means this kind of document is
    not a place to take a figure from, and the caller should not ask for one.
    """

    if not isinstance(spec_ref, str):
        return None
    return GRADE_BY_SPEC.get(spec_ref)


def figure_worthy(spec_ref: Any) -> bool:
    """Whether a figure may be read out of this kind of document at all."""

    return grade_for(spec_ref) is not None


def require_grade(spec_ref: Any) -> str:
    """The grade, or a refusal naming the document kind that has none."""

    grade = grade_for(spec_ref)
    if grade is None:
        raise FigureGradeError(
            f"{spec_ref!r} is not a document kind a figure may be read from; "
            f"graded kinds are {sorted(GRADE_BY_SPEC)}"
        )
    return grade


def basis_for(grade: str) -> str:
    """The Ledger ``basis`` text for a grade."""

    if grade not in BASIS_BY_GRADE:
        raise FigureGradeError(f"unknown figure grade {grade!r}")
    return BASIS_BY_GRADE[grade]


def qualify(statement: str, grade: str) -> str:
    """Add what this grade means to a figure's own statement.

    The grade is queryable elsewhere; this is so a person reading one claim is
    told, in the claim, that the number was spoken rather than filed.
    """

    if not isinstance(statement, str) or not statement.strip():
        raise FigureGradeError("a figure statement is required")
    if grade not in QUALIFIER_BY_GRADE:
        raise FigureGradeError(f"unknown figure grade {grade!r}")
    return f"{statement.rstrip().rstrip('.')}, {QUALIFIER_BY_GRADE[grade]}."


__all__ = [
    "ATTRIBUTED_BY_SPEC",
    "BASIS_BY_GRADE",
    "FILED",
    "GRADES",
    "GRADE_BY_SPEC",
    "QUALIFIER_BY_GRADE",
    "SPOKEN",
    "FigureGradeError",
    "attribution_for",
    "basis_for",
    "figure_recordable",
    "figure_worthy",
    "grade_for",
    "qualify",
    "require_grade",
]
