"""P13ag: turn a parsed filing into statement lines the contract can describe.

Two views of one filing, each holding half of what a line needs:

* the **statement** view knows what the statement *is* -- which concepts are
  lines, their level, what they roll into, which dimension axis and member a
  breakdown sits on -- and carries no unit;
* the **fact** view knows what each figure *is* -- the value exactly as filed,
  its ``unit_ref``, its period -- and carries no hierarchy.

So the statement defines the lines and the facts fill them. Not the reverse: a
fact that the statement does not present is not a line of that statement, and
emitting one would mean inventing a level and a parent for it.

Nothing here infers. A figure whose unit the filing does not state is dropped
rather than assumed to be dollars, a value that is not a number is dropped
rather than coerced, and both are counted so the drop is visible instead of
silent. The raw parser output is hashed and kept whatever this returns, so
anything dropped here is still recoverable from the artifact.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "0.1"
# The three statements a model is built on. The parser also classifies
# parentheticals, equity and disclosures; those are not these.
STATEMENT_BY_TYPE = {
    "IncomeStatement": "income",
    "BalanceSheet": "balance",
    "CashFlowStatement": "cash",
}
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)([.][0-9]+)?$")
_INSTANT_RE = re.compile(r"^instant_(\d{4}-\d{2}-\d{2})$")
_DURATION_RE = re.compile(r"^duration_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$")


def _text(value: Any) -> str | None:
    """A present, non-empty string, or None. Treats pandas NaN as absent."""

    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def fact_period(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(start, end) for a fact, or (None, None) when the filing gives neither.

    A balance-sheet fact is an instant: it has no start, and the parser leaves
    ``period_end`` empty and puts the date in ``period_key`` as
    ``instant_2026-06-30``. Reading only ``period_end`` silently dropped every
    balance-sheet line -- the statement was simply absent from the output, which
    is a worse failure than an error because nothing said so.
    """

    start, end = _text(row.get("period_start")), _text(row.get("period_end"))
    if end is not None:
        return start, end
    key = _text(row.get("period_key")) or ""
    instant = _INSTANT_RE.fullmatch(key)
    if instant is not None:
        return None, instant.group(1)
    duration = _DURATION_RE.fullmatch(key)
    if duration is not None:
        return duration.group(1), duration.group(2)
    return None, None


def canonical_ref(value: Any) -> str | None:
    """One spelling for a concept, axis or member across both views.

    The two views disagree: a concept is ``us-gaap_Revenues`` in the statement
    and ``us-gaap:Revenues`` in the facts, and a member is ``srt_AmericasMember``
    in one and ``srt:AmericasMember`` in the other. The prefix separator is the
    first underscore when there is no colon; the local name never contains one.
    """

    text = _text(value)
    if text is None or ":" in text:
        return text
    return text.replace("_", ":", 1)


def _fact_key(row: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    return (
        canonical_ref(row.get("concept")),
        canonical_ref(row.get("dimension")),
        canonical_ref(row.get("member")),
    )


def _structure_key(row: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    return (
        canonical_ref(row.get("concept")),
        canonical_ref(row.get("dimension_axis")),
        canonical_ref(row.get("dimension_member")),
    )


def fact_dimension_count(row: Mapping[str, Any]) -> int | None:
    """Count dimensions only when the parser supplied its complete axis set.

    ``with_dimensions()`` emits one ``dim_*`` column per context axis.  The
    older projected ``dimension``/``member`` pair names one axis but cannot
    prove there was no second axis, so it deliberately remains unknown.
    """

    dimension_columns = [key for key in row if str(key).startswith("dim_")]
    if not dimension_columns:
        return None
    return sum(_text(row.get(key)) is not None for key in dimension_columns)


def _level(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    return level if level >= 0 else None


def normalise_statement(
    *,
    statement: str,
    structure: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Lines for one statement, plus what was dropped and why."""

    if statement not in set(STATEMENT_BY_TYPE.values()):
        raise ValueError(f"unknown statement {statement!r}")
    by_key: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in facts:
        by_key.setdefault(_fact_key(row), []).append(row)

    lines: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for row in structure:
        # A header ("Operating expenses:") is a row of the statement with no
        # figure of its own. It is not a line, and giving it a null value would
        # put a fact-shaped thing where there is no fact.
        if bool(row.get("abstract")):
            continue
        concept = canonical_ref(row.get("concept"))
        level = _level(row.get("level"))
        if concept is None or level is None:
            drop("structure row has no concept or level")
            continue
        matches = by_key.get(_structure_key(row))
        if not matches:
            drop("statement line has no fact in this filing")
            continue
        for fact in matches:
            period_start, period_end = fact_period(fact)
            unit = _text(fact.get("unit_ref"))
            raw = _text(fact.get("value"))
            if period_end is None:
                drop("fact has no period")
                continue
            if unit is None:
                # The filing did not say what this is measured in. Assuming
                # dollars is exactly the inference this system refuses.
                drop("fact has no unit")
                continue
            if raw is None or _DECIMAL_RE.fullmatch(raw) is None:
                # Text, a footnote marker, or a formatted number: not a figure
                # as filed, and not something to coerce into one.
                drop("fact value is not a plain decimal")
                continue
            axis = canonical_ref(row.get("dimension_axis"))
            lines.append({
                "statement": statement,
                "concept": concept,
                "label": _text(row.get("label")) or _text(fact.get("label")) or concept,
                "level": level,
                "parent_concept": canonical_ref(row.get("parent_concept")),
                # P13an: a line reported along a dimension is a breakdown,
                # whatever the parser's own flag says. Live, EPAM's revenue
                # split by timing of transfer came back with the axis set and
                # the flag clear, so the reported total and its two components
                # were indistinguishable -- three rows for one quarter, and any
                # series built from them silently adds a total to its parts.
                "is_breakdown": bool(row.get("is_breakdown")) or axis is not None,
                "dimension_axis": axis,
                "dimension_member": canonical_ref(row.get("dimension_member")),
                "dimension_count": fact_dimension_count(fact),
                # A quarter and a year to date share an end date; only the
                # start tells them apart, and a balance-sheet instant has none.
                "period_start": period_start,
                "period_end": period_end,
                # The value exactly as filed. numeric_value is the parser's
                # float of the same thing and is not what the company reported.
                "value": raw,
                "unit": unit,
                "balance": _text(row.get("balance")) or _text(fact.get("balance")),
            })
    # The fact view repeats a fact once per presentation it appears in, so the
    # same filed figure arrives several times. Identical lines are one line.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for line in lines:
        key = (line["concept"], line["dimension_axis"], line["dimension_member"],
               line["period_start"], line["period_end"], line["value"], line["unit"])
        if key in unique:
            drop("duplicate presentation of one filed figure")
            continue
        unique[key] = line
    ordered = sorted(unique.values(), key=lambda item: (
        item["period_end"], item["period_start"] or "", item["level"],
        item["concept"], item["dimension_axis"] or "", item["dimension_member"] or ""))
    return {"lines": ordered, "dropped": dropped}


def normalise_filing(
    *,
    accession: str,
    form: str,
    filed: str,
    report_date: str,
    statements: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    """One filing's lines across the statements it reports.

    ``statements`` maps our statement name to ``{"structure": rows,
    "facts": rows}``; a statement the filing does not carry is simply absent.
    """

    lines: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for statement in sorted(statements):
        result = normalise_statement(
            statement=statement,
            structure=statements[statement].get("structure") or (),
            facts=statements[statement].get("facts") or (),
        )
        lines.extend(result["lines"])
        for reason, count in result["dropped"].items():
            dropped[reason] = dropped.get(reason, 0) + count
    return {
        "accession": accession,
        "form": form,
        "filed": filed,
        "report_date": report_date,
        "lines": lines,
        "dropped": dropped,
    }


def build_wire(
    *,
    cik: str,
    entity_name: str,
    filings: Sequence[Mapping[str, Any]],
    source_record_refs: Sequence[str],
    provider_status: int = 200,
) -> dict[str, Any]:
    """The closed observation the frozen output contract describes.

    ``dropped`` never reaches the wire: the contract is the statement, and what
    the adapter could not use belongs in the run summary beside it, where a
    reader looking for "why is this line missing" will actually look.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "cik": str(cik).zfill(10),
        "entity_name": entity_name,
        "filings": [
            {
                "accession": filing["accession"],
                "form": filing["form"],
                "filed": filing["filed"],
                "report_date": filing["report_date"],
                "lines": list(filing["lines"]),
            }
            for filing in filings
        ],
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": int(provider_status),
    }


__all__ = [
    "SCHEMA_VERSION",
    "STATEMENT_BY_TYPE",
    "build_wire",
    "canonical_ref",
    "normalise_filing",
    "normalise_statement",
]
