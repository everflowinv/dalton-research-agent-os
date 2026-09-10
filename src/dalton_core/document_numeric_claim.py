"""P11e: typed figures read out of a document, verified against its bytes.

Until now numeric authority belonged to the SEC lane alone: XBRL company facts
produced one structured number per company-quarter, and document extraction was
forbidden to write a figure at all -- "only direction and qualitative
magnitude".  That rule is why 1,250 qualitative claims carry zero numbers, and
why a company model has nothing to stand on: revenue by segment, margins,
bookings, headcount and utilisation are in the filings and the transcripts, and
none of them were being recorded as numbers.

The rule was right about the risk and wrong about the conclusion.  The risk is
a model asserting a figure that is not in the source.  The answer is not to
forbid figures, it is to refuse to take the model's word for the digits:

* the model may *locate and structure* -- which metric, which period, which
  basis, which quote;
* the digits themselves are then checked, deterministically, against the exact
  bytes of the quote it cited.

A candidate whose number cannot be found in its own citation is refused here,
before it reaches staging.  So the worst a wrong model output can do is
propose nothing, rather than assert a number the document never contained.

Nothing in this module admits a Claim.  It produces verified candidates; the
admission path stays where ADR-0003 put it.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .store import content_hash

SCHEMA_VERSION = "0.1"
CANDIDATE_KIND = "quantitative"
# Units the extractor may assert. Anything else is a refusal rather than a
# guess: a figure whose unit nobody agreed on cannot be compared or modelled.
ALLOWED_UNITS: tuple[str, ...] = (
    "percent", "currency", "count", "ratio", "days", "basis_points",
)
ALLOWED_BASES: tuple[str, ...] = (
    "management-reported", "gaap-reported", "non-gaap-reported", "calculated",
    # P11b: the figure is a broker's own estimate of the company -- a price
    # target, a forward EPS or revenue number out of a research note.  It needs
    # its own basis because the four above all describe a figure the *company*
    # arrived at, and filing a broker's target under "management-reported"
    # would be a false statement about who said it.  Nothing in this module
    # treats it differently; the separation that matters is enforced by the
    # grade (``document_figure_grade.BROKER_RESEARCH``), which is not
    # admissible as a quantitative Claim.
    "broker-estimate",
)
MAX_METRIC_CHARS = 200
MAX_PERIOD_CHARS = 200
# Scale words a document may use in place of trailing zeros.
_SCALE_WORDS: Mapping[str, Decimal] = {
    "thousand": Decimal(10) ** 3,
    "million": Decimal(10) ** 6,
    "billion": Decimal(10) ** 9,
    "trillion": Decimal(10) ** 12,
}
_NUMBER_RE = re.compile(r"[-+]?\d[\d,\s]*(?:\.\d+)?")
# P13j: a label has to name a line, and a period has to name a time.
#
# Live, a transcript sentence -- "$5 billion, about 40% of DXC, has the muscle
# to grow" -- was stored as DXC revenue with as_reported_label "$5 billion" and
# period "current". Both checks passed and both were vacuous: the label was the
# figure itself, so "the label appears in the quote" was trivially true, and
# "current" placed the number nowhere.
#
# Words that are part of writing an amount, not part of naming a line.
_AMOUNT_WORDS = frozenset({
    "usd", "eur", "gbp", "rmb", "cny", "aud", "jpy", "hkd",
    "thousand", "million", "billion", "trillion", "bn", "mn", "m", "k",
    "approximately", "about", "roughly", "around", "some", "over", "under",
    "a", "an", "the", "of", "and", "or", "to", "in", "at", "per", "cent",
    "percent", "pct", "dollars", "dollar",
})
_LETTERS_RE = re.compile(r"[a-z]+")
# A period is comparable only if it says which one: a four-digit year, or a
# quarter/half marker. "current" and "the period" name nothing.
_PERIOD_YEAR_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")
# The trailing \d{0,2} is the two-digit year filers write inline: 1H25, Q325.
# The trailing \d{0,2} is the two-digit year filers write inline (1H25, Q325);
# a bare "fy" names no year and is not a period.
_PERIOD_MARKER_RE = re.compile(r"\b(?:(?:q[1-4]|[1-4]q|h[12]|[12]h)\d{0,2}|fy\d{2})\b")
_METRIC_REF_RE = re.compile(r"metric:[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _names_a_line(label: str) -> str | bool:
    """Whether this label names a line item rather than restating the amount.

    "Net revenues" names a line; "$5 billion" is the figure wearing the label's
    clothes, and it makes the label check vacuous -- of course the amount
    appears in the quote the amount came from.
    """

    words = [w for w in _LETTERS_RE.findall(label.lower()) if w not in _AMOUNT_WORDS]
    return sum(len(w) for w in words) >= 3


def _names_a_period(period: str) -> bool:
    """Whether this period says which one.

    A figure whose period is "current" cannot join a series, cannot be compared
    with last year's, and cannot be checked by anyone later.
    """

    lowered = period.lower()
    return bool(_PERIOD_YEAR_RE.search(lowered) or _PERIOD_MARKER_RE.search(lowered))


def _fold_words(text: str) -> str:
    """Compare wording the way a reader would, not byte for byte."""

    folded = unicodedata.normalize("NFKC", text).lower()
    folded = folded.replace("-", " ").replace("\u2014", " ").replace("\u2013", " ")
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", folded)).strip()


class NumericCandidateError(ValueError):
    """The numeric candidate is malformed, or its number is not in its source."""


def _text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise NumericCandidateError(f"{name} must be text")
    stripped = value.strip()
    if not stripped or len(stripped) > maximum:
        raise NumericCandidateError(f"{name} must be 1..{maximum} characters")
    return stripped


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise NumericCandidateError(f"{name} must be a number written as a string or integer")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NumericCandidateError(f"{name} is not a decimal number") from exc
    if not parsed.is_finite():
        raise NumericCandidateError(f"{name} must be finite")
    return parsed


def validate_numeric_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape a numeric suggestion must have before verification."""

    if not isinstance(value, Mapping) or set(value) != {
        "quote_id", "metric_ref", "subject_as_named", "as_reported_label", "value",
        "unit", "currency", "period", "basis", "scale",
    }:
        raise NumericCandidateError(
            "numeric candidate must be exactly quote_id/metric_ref/subject_as_named/"
            "as_reported_label/value/unit/currency/period/basis/scale"
        )
    unit = value["unit"]
    if unit not in ALLOWED_UNITS:
        raise NumericCandidateError(f"unit must be one of {list(ALLOWED_UNITS)}")
    basis = value["basis"]
    if basis not in ALLOWED_BASES:
        raise NumericCandidateError(f"basis must be one of {list(ALLOWED_BASES)}")
    currency = value["currency"]
    if unit == "currency":
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise NumericCandidateError("a currency figure must name an ISO-4217 currency")
    elif currency is not None:
        raise NumericCandidateError("only a currency figure may name a currency")
    scale = value["scale"]
    if scale is not None and scale not in _SCALE_WORDS:
        raise NumericCandidateError(f"scale must be null or one of {sorted(_SCALE_WORDS)}")
    metric_ref = _text(value["metric_ref"], "metric_ref", maximum=120)
    if _METRIC_REF_RE.fullmatch(metric_ref) is None:
        raise NumericCandidateError("metric_ref must be the slot that was requested")
    label = _text(value["as_reported_label"], "as_reported_label", maximum=MAX_METRIC_CHARS)
    if not _names_a_line(label):
        raise NumericCandidateError(
            "as_reported_label must name the line, not restate the amount: "
            f"{label!r} carries no line name"
        )
    period = _text(value["period"], "period", maximum=MAX_PERIOD_CHARS)
    if not _names_a_period(period):
        raise NumericCandidateError(
            f"period must say which period: {period!r} places the figure nowhere"
        )
    return {
        "quote_id": _text(value["quote_id"], "quote_id", maximum=100),
        # The slot that was asked for, and what this document happens to call
        # it. Filers write "Net revenues", "Total revenue", "Revenues" for the
        # same line; the slot is what makes a series, and the label is what
        # lets a reader check the mapping instead of trusting it.
        "metric_ref": metric_ref,
        # P13c: whose figure this is, in the document's own words. A document
        # may discuss several companies -- an industry report, a note comparing
        # vendors -- and the digits being real says nothing about whose they
        # are. Recorded rather than verified against the quote: the subject is
        # often named a paragraph away from the number, and demanding it in the
        # same span would refuse most true figures.
        "subject_as_named": _text(
            value["subject_as_named"], "subject_as_named", maximum=MAX_METRIC_CHARS
        ),
        "as_reported_label": label,
        "value": str(_decimal(value["value"], "value")),
        "unit": unit,
        "currency": currency,
        "period": period,
        "basis": basis,
        "scale": scale,
    }


def _normalize(text: str) -> str:
    """Fold the typography a document uses around numbers, nothing else."""

    folded = unicodedata.normalize("NFKC", text)
    # Minus signs, thousands separators and non-breaking spaces are
    # presentation; the digits are what has to match.
    for source, target in (("−", "-"), ("–", "-"), ("—", "-"),
                           (" ", " "), (" ", " "), (",", "")):
        folded = folded.replace(source, target)
    return folded


def numbers_in(text: str) -> set[Decimal]:
    """Every number a reader would see in this text."""

    found: set[Decimal] = set()
    for match in _NUMBER_RE.finditer(_normalize(text)):
        raw = match.group(0).replace(" ", "")
        if raw in {"-", "+", ""}:
            continue
        try:
            found.add(Decimal(raw))
        except InvalidOperation:
            continue
    return found


def _candidate_values(candidate: Mapping[str, Any]) -> set[Decimal]:
    """The forms of the asserted number that could legitimately appear.

    A document writes "$1.2 billion", not "1200000000", so a scaled figure has
    to match either the number as written or the number it means.  Both are the
    same assertion; refusing the written form would reject exactly the figures
    that are easiest for a human to check.
    """

    value = Decimal(candidate["value"])
    forms = {value}
    scale = candidate["scale"]
    if scale is not None:
        factor = _SCALE_WORDS[scale]
        forms.add(value * factor)
        if value % factor == 0:
            forms.add(value / factor)
    return forms


def verify_numeric_candidate(
    candidate: Mapping[str, Any], quotes: Mapping[str, str]
) -> dict[str, Any]:
    """Refuse any candidate whose number is not in the bytes it cited.

    ``quotes`` maps quote_id to that quote's exact raw text.  The check is
    deliberately about the digits alone: whether the figure means what the
    model says it means is a human's judgement, but whether the document
    contains it at all is not a matter of opinion.
    """

    wire = validate_numeric_candidate(candidate)
    quote = quotes.get(wire["quote_id"])
    if not isinstance(quote, str) or not quote:
        raise NumericCandidateError("numeric candidate cites a quote that was not supplied")
    present = numbers_in(quote)
    if not _candidate_values(wire) & present:
        raise NumericCandidateError(
            "numeric candidate asserts a value its citation does not contain"
        )
    # The wording is checked the same way the digits are: a model may report
    # what this filer calls the line, never invent that it called it that.
    if _fold_words(wire["as_reported_label"]) not in _fold_words(quote):
        raise NumericCandidateError(
            "numeric candidate reports a label its citation does not contain"
        )
    verified = {
        **wire,
        "schema_version": SCHEMA_VERSION,
        "claim_kind": CANDIDATE_KIND,
        "citation_text": quote,
        "citation_hash": content_hash({"quote_id": wire["quote_id"], "raw_text": quote}),
    }
    verified["content_hash"] = content_hash(verified)
    return verified


def verify_numeric_candidates(
    candidates: Any, quotes: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a model's numeric suggestions into verified ones and refusals.

    One bad suggestion does not discard the window: a model that gets three
    figures right and invents a fourth should give up the fourth, not the
    three.  Refusals are returned rather than dropped so the reason is visible
    instead of the figure silently never appearing.
    """

    if not isinstance(candidates, list):
        raise NumericCandidateError("numeric candidates must be a list")
    verified: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for item in candidates:
        try:
            verified.append(verify_numeric_candidate(item, quotes))
        except NumericCandidateError as exc:
            refused.append({
                "reason": str(exc),
                "quote_id": item.get("quote_id") if isinstance(item, Mapping) else None,
                "metric_ref": item.get("metric_ref") if isinstance(item, Mapping) else None,
            })
    return verified, refused


__all__ = [
    "ALLOWED_BASES",
    "ALLOWED_UNITS",
    "CANDIDATE_KIND",
    "NumericCandidateError",
    "numbers_in",
    "validate_numeric_candidate",
    "verify_numeric_candidate",
    "verify_numeric_candidates",
]
