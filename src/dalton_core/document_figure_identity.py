"""What one verified document figure *is*: its period as dates, its amount, its label.

2026-09-28, ws-7d.  One connector invocation read META's 2025 10-K and the
figures pass wrote its revenue down three times -- ``"2025"``, ``"full year
2025"`` and ``"Year Ended December 31, 2025"``, as ``200.97 billion`` twice and
``200966 million`` once -- and its net income twice, once labelled "net income
adjusted for certain non-cash items" because that was the sentence the number
sat in.  AMZN's revenue went in twice, once labelled "Consolidated".  Every one
became a Claim: the promoter keyed a figure on its free-text period and its own
row id, so three spellings of one year were three numbers.

This module answers the three questions that key should have asked, from Core
and nothing else (no model):

* **the period**, as ``start..end`` dates.  "Year Ended December 31, 2025"
  and "Fiscal Year Ended March 31, 2026" are fiscal years; so are "2025",
  "fiscal 2025", "FY25" and "full year 2025" in a 10-K, whose report date is
  its fiscal year end (ACN's 31 August, DXC's 31 March).  "Three months ended June 30, 2025" is a quarter.
  "X compared to Y" is X.  Anything else ("Q2", "first half", "current") is
  left as written: a guess would be worse than a duplicate;
* **the amount**, as value x scale, with the precision it was stated to, so
  ``200.97 billion`` and ``200966 million`` are one amount (the second rounds
  to the first) and ``200.97 billion`` and ``201.5 billion`` are two;
* **the label**: the filed XBRL line of the same filing, period and amount
  first (its ``label`` is what the filer called it), then the row label the
  number sits on in the document's own table, and only then the words the
  drafting model wrote.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

IDENTITY_VERSION = "document-figure-identity:v1"

SCALE_EXPONENTS: Mapping[str, int] = {
    "": 0, "one": 0, "unit": 0, "units": 0,
    "thousand": 3, "thousands": 3, "k": 3,
    "million": 6, "millions": 6, "mm": 6, "m": 6,
    "billion": 9, "billions": 9, "bn": 9, "b": 9,
    "trillion": 12, "trillions": 12,
}

_MONTHS = {name: index for index, name in enumerate((
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december"), start=1)}
_MONTH_RE = "(" + "|".join(_MONTHS) + r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?"
_DATE_RE = _MONTH_RE + r"\s+(\d{1,2}),?\s+(\d{4})"
_ISO_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:\.\.|to|through|–|—)\s*(\d{4}-\d{2}-\d{2})$")
_YEAR_ENDED = re.compile(
    r"^(?:for\s+the\s+)?(?:(?:fiscal|full)\s+)?(?:years?|twelve\s+months|12\s+months|"
    r"fiscal\s+years?)\s+end(?:ed|ing)\s+" + _DATE_RE + "$")
_QUARTER_ENDED = re.compile(
    r"^(?:for\s+the\s+)?(?:three\s+months|3\s+months|(?:fiscal\s+)?quarter)\s+end(?:ed|ing)\s+"
    + _DATE_RE + "$")
_BARE_YEAR = re.compile(
    r"^(?:(?:the\s+)?(?:full[\s-]+year|fiscal(?:\s+year)?|fy|year|calendar\s+year)\s*)?"
    r"'?(\d{4}|\d{2})$")
_COMPARISON = re.compile(r"\s+(?:vs\.?|versus|compared\s+(?:to|with)|over)\s+", re.IGNORECASE)


def _month(token: str) -> int | None:
    token = token.lower().rstrip(".")
    if token in _MONTHS:
        return _MONTHS[token]
    for name, index in _MONTHS.items():
        if name.startswith(token) and len(token) >= 3:
            return index
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        return None


def _year_back(end: date) -> date:
    """The first day of the twelve months ending on ``end``."""

    prior = _safe_date(end.year - 1, end.month, end.day) or date(end.year - 1, end.month, 28)
    return prior + timedelta(days=1)


def _month_end(day: date) -> bool:
    return (day + timedelta(days=1)).day == 1


def normalize_period(
    text: Any, *, fiscal_year_end: tuple[int, int] | None = None,
) -> tuple[str, str] | None:
    """``(start, end)`` ISO dates for a reported period, or None when unsure."""

    if not isinstance(text, str):
        return None
    spelled = " ".join(text.strip().split())
    if not spelled:
        return None
    spelled = _COMPARISON.split(spelled, maxsplit=1)[0].strip().rstrip(",.")
    lowered = spelled.lower()
    match = _ISO_RANGE.match(lowered)
    if match:
        start, end = (date.fromisoformat(value) for value in match.groups())
        return (start.isoformat(), end.isoformat()) if start <= end else None
    match = _YEAR_ENDED.match(lowered)
    if match:
        month = _month(match.group(1))
        end = None if month is None else _safe_date(int(match.group(3)), month,
                                                     int(match.group(2)))
        return None if end is None else (_year_back(end).isoformat(), end.isoformat())
    match = _QUARTER_ENDED.match(lowered)
    if match:
        month = _month(match.group(1))
        end = None if month is None else _safe_date(int(match.group(3)), month,
                                                     int(match.group(2)))
        if end is None or not _month_end(end):
            # A 13-week quarter ending on a Saturday has no calendar start.
            return None
        start_month = (end.month - 3) % 12 + 1
        start_year = end.year if start_month <= end.month else end.year - 1
        return date(start_year, start_month, 1).isoformat(), end.isoformat()
    match = _BARE_YEAR.match(lowered)
    if match and fiscal_year_end is not None:
        year = int(match.group(1))
        if year < 100:
            year += 2000
        month, day = fiscal_year_end
        if month <= 2:
            # A January/February year end (retailers) names its fiscal year
            # after the calendar year it *starts* in about as often as the one
            # it ends in.  Not guessed.
            return None
        end = _safe_date(year, month, day)
        return None if end is None else (_year_back(end).isoformat(), end.isoformat())
    return None


def amount(value: Any, scale: Any) -> tuple[Decimal, int] | None:
    """(value x scale, the power of ten it was stated to), or None."""

    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    exponent = SCALE_EXPONENTS.get(str(scale or "").strip().lower())
    if exponent is None:
        return None
    return number.scaleb(exponent), int(number.as_tuple().exponent) + exponent


def same_amount(left: tuple[Decimal, int] | None, right: tuple[Decimal, int] | None) -> bool:
    """One amount, stated to two precisions: equal at the coarser one."""

    if left is None or right is None:
        return False
    quantum = Decimal(1).scaleb(max(left[1], right[1]))
    return (left[0].quantize(quantum, rounding=ROUND_HALF_UP)
            == right[0].quantize(quantum, rounding=ROUND_HALF_UP))


def _accession(document_ref: Any) -> str | None:
    if isinstance(document_ref, str) and document_ref.startswith("sec:filing:"):
        return document_ref.removeprefix("sec:filing:")
    return None


def _table(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def fiscal_year_end(connection: Any, company_ref: str, document_ref: Any) -> tuple[int, int] | None:
    """The (month, day) the document's fiscal year ends on, when it is a 10-K.

    Only the document's own 10-K is asked: a bare "2025" inside a 10-Q or a
    press release could be a year-to-date or a calendar year, and is left as
    written rather than guessed into a fiscal year.
    """

    accession = _accession(document_ref)
    if accession is None or not _table(connection, "coverage_mission_statement_filings"):
        return None
    row = connection.execute(
        "SELECT report_date FROM coverage_mission_statement_filings "
        "WHERE accession=? AND company_ref=? AND form='10-K' LIMIT 1",
        (accession, company_ref)).fetchone()
    try:
        end = date.fromisoformat(str(row[0])) if row is not None else None
    except ValueError:
        end = None
    return None if end is None else (end.month, end.day)


def _metric_concepts(metric_ref: Any) -> list[str]:
    from .quantitative_claim_promotion import CONCEPT_METRICS

    slug = str(metric_ref or "").removeprefix("metric:").replace("-", " ").strip().lower()
    return [concept for concept, (metric, _label) in CONCEPT_METRICS.items() if metric == slug]


def filed_line(
    connection: Any, figure: Mapping[str, Any], span: tuple[str, str] | None,
) -> dict[str, Any] | None:
    """The undimensioned XBRL line of the same filing, period and amount."""

    accession = _accession(figure.get("document_ref"))
    concepts = _metric_concepts(figure.get("metric_ref"))
    stated = amount(figure.get("value"), figure.get("scale"))
    if (accession is None or span is None or not concepts or stated is None
            or not _table(connection, "coverage_mission_statement_lines")):
        return None
    rows = connection.execute(
        "SELECT l.line_id, l.concept, l.label, l.value, l.unit, l.statement, l.ordinal "
        "FROM coverage_mission_statement_lines l "
        "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
        "WHERE f.accession=? AND f.company_ref=? AND l.dimension_axis IS NULL "
        "AND l.dimension_member IS NULL AND l.period_start=? AND l.period_end=? "
        "AND l.concept IN (%s) ORDER BY l.statement='income' DESC, l.ordinal"
        % ",".join("?" * len(concepts)),
        (accession, figure.get("company_ref"), span[0], span[1], *concepts),
    ).fetchall()
    currency = str(figure.get("currency") or "").lower()
    for row in rows:
        if currency and not str(row["unit"] or "").lower().startswith(currency):
            continue
        if same_amount(amount(row["value"], "one"), stated):
            return {"line_id": row["line_id"], "concept": row["concept"],
                    "label": str(row["label"] or "").strip()}
    return None


_NUMERIC_SEGMENT = re.compile(r"^[\s$€£¥()\-–—%,.\d]*$")
# A row label that names no measure.  "Consolidated" was AMZN's revenue label
# on ws-7d; "Total" is ACN's adjusted operating margin row.  Either is where
# the number sits, and neither says what it is.
GENERIC_ROW_LABELS = frozenset({
    "total", "totals", "consolidated", "total company", "all other", "other", "as reported",
})


def _spellings(value: Any) -> list[str]:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return []
    plain = format(number, "f")
    whole, _, fraction = plain.partition(".")
    grouped = f"{int(whole):,}" if whole.lstrip("-").isdigit() else whole
    spellings = {plain, grouped + ("." + fraction if fraction else "")}
    return sorted(spellings, key=len, reverse=True)


def row_label(citation_text: Any, value: Any) -> str | None:
    """The label of the table row the number is printed on, if it is in a table.

    Tables reach the figures pass flattened, one cell per paragraph
    ("Net sales\\n\\n$\\n\\n352,828\\n\\n$\\n\\n387,497").  Walking back from the
    number over the other numeric cells of its row reaches the row's label.
    A number inside a sentence has no row label, and none is invented.
    """

    if not isinstance(citation_text, str):
        return None
    for spelling in _spellings(value):
        for match in re.finditer(r"(?<![\d.,])" + re.escape(spelling) + r"(?![\d])",
                                 citation_text):
            before = citation_text[:match.start()]
            if before and not before.endswith(("\n", "$", "(", " ")):
                continue
            segments = [item.strip() for item in re.split(r"\n+", before)]
            if segments and segments[-1] and not _NUMERIC_SEGMENT.match(segments[-1]):
                # Something other than a cell precedes it on its own line: prose.
                continue
            for segment in reversed(segments[:-1] if segments else []):
                if not segment or _NUMERIC_SEGMENT.match(segment):
                    continue
                words = segment.split()
                if (len(words) <= 8 and len(segment) <= 80
                        and not segment.endswith((".", ":", ";"))
                        and segment.lower() not in GENERIC_ROW_LABELS):
                    return segment
                break
    return None


def figure_identity(connection: Any, figure: Mapping[str, Any]) -> dict[str, Any]:
    """The period as dates, the amount, and the label a Claim should carry."""

    fye = fiscal_year_end(connection, str(figure.get("company_ref") or ""),
                          figure.get("document_ref"))
    span = normalize_period(figure.get("period"), fiscal_year_end=fye)
    stated = amount(figure.get("value"), figure.get("scale"))
    line = filed_line(connection, figure, span)
    label, source = None, None
    if line is not None and line["label"]:
        label, source = line["label"], "filed_concept"
    if label is None:
        found = row_label(figure.get("citation_text"), figure.get("value"))
        if found:
            label, source = found, "document_row"
    if label is None:
        label, source = str(figure.get("as_reported_label") or ""), "as_reported"
    return {
        "version": IDENTITY_VERSION,
        "span": span,
        "period": f"{span[0]}..{span[1]}" if span else str(figure.get("period") or ""),
        "amount": None if stated is None else format(stated[0].normalize(), "f"),
        "precision": None if stated is None else stated[1],
        "label": label,
        "label_source": source,
        "filed_line": line,
    }


def duplicate_groups(
    connection: Any, figures: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """``(duplicate figure_id -> the figure it repeats, identities)``.

    One document, one company, one metric, one period as dates, one amount:
    one number.  The one kept is the one a filed line confirms, then the most
    precisely stated, then the earliest written.  A period that did not
    normalise, or a figure without an amount, is never folded into another.
    """

    identities: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for figure in figures:
        identity = figure_identity(connection, figure)
        identities[str(figure["figure_id"])] = identity
        if identity["span"] is None or identity["amount"] is None:
            continue
        key = (figure.get("company_ref"), figure.get("document_ref"), figure.get("metric_ref"),
               identity["span"], figure.get("unit"), figure.get("currency"))
        groups.setdefault(key, []).append(figure)
    duplicates: dict[str, str] = {}
    for members in groups.values():
        def rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
            identity = identities[str(item["figure_id"])]
            return (identity["filed_line"] is None, identity["precision"],
                    str(item.get("created_at") or ""), str(item["figure_id"]))

        remaining = sorted(members, key=rank)
        while remaining:
            keeper = remaining.pop(0)
            kept = identities[str(keeper["figure_id"])]
            rest = []
            for other in remaining:
                other_identity = identities[str(other["figure_id"])]
                if same_amount((Decimal(kept["amount"]), kept["precision"]),
                               (Decimal(other_identity["amount"]), other_identity["precision"])):
                    duplicates[str(other["figure_id"])] = str(keeper["figure_id"])
                else:
                    rest.append(other)
            remaining = rest
    return duplicates, identities


__all__ = [
    "GENERIC_ROW_LABELS",
    "IDENTITY_VERSION",
    "amount",
    "duplicate_groups",
    "figure_identity",
    "filed_line",
    "fiscal_year_end",
    "normalize_period",
    "row_label",
    "same_amount",
]
