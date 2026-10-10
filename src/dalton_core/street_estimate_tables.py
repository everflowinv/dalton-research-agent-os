"""Batch B: the estimates tables inside broker notes, read deterministically.

``street_estimates`` holds one row per note -- the house, the rating and the
price target from page one.  The numbers a PM compares a model against are
further in: Goldman's "Summary of GS vs. Street estimates", Citi's P&L block,
UBS's highlights grid, TD's quarterly EPS table with its "Prior" and
"Consensus EPS" rows, Wells Fargo's year-by-quarter grid.  The acquired text
of those notes is a PDF-to-text rendering: one cell per line, sometimes one
row per line with ``|`` between cells, column headers before (or, for Goldman,
after) the rows.

The reader here is a column-count parser, not a layout model:

1. The text becomes a stream of cells, each with its character span.
2. **Period headers** are maximal runs of period cells (``2026E``,
   ``FY27 (E)``, ``08/27E``, ``4Q26 (E)``, ``Q1``..``Q4``/``FY``).
3. **Rows** are a label cell (EPS / revenue / their Prior, Old, New, Street,
   Consensus variants, or a quarter/"Year" sub-label under a metric section)
   followed by a run of value cells.
4. A row is admitted only when its value count equals the header's column
   count (or three times it, for a ``GS | Street | Δ`` triple, whose Δ is
   re-computed and must agree).  Anything else is skipped, never guessed.

Figures are the house's (``house``), the house's superseded ones (``house_prior``),
or the street number the house quotes (``street_per_broker``).  Actuals
(``A`` suffix, or a header without ``E`` before estimated columns) are not
estimates and are not stored.  Each figure carries its row text and span, and
its digits are re-found in that span.

Stored append-only in ``street_estimate_figures`` with a scan ledger
``street_estimate_figure_scans`` (one scan per document per parser version).
A model pass (``street_estimate_extraction``'s frozen request/prompt/verify
surface, purpose ``street_estimate``, cheap tier) is used only when the
deterministic pass finds no admissible table in a note that visibly has one,
and only when a model callable and a budget gate are injected; otherwise the
scan records ``needs_model`` and says so.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
PARSER_VERSION = "street-estimate-tables:batch-b:0.1"
EXTRACTOR_REF = "extractor:street-estimate-tables:0.1"
_SCHEMA_PATH = Path(__file__).with_name("street_estimate_tables_schema.sql")
METRICS = ("eps", "revenue")
ESTIMATE_KINDS = ("house", "house_prior", "street_per_broker")
SCAN_OUTCOMES = ("recorded", "none_found", "needs_model", "model_recorded", "refused")
#: How far (in cells) a header may sit from a row it labels.
HEADER_REACH_BEFORE = 160
HEADER_REACH_AFTER = 60
MAX_FIGURES_PER_DOCUMENT = 120
#: Δ in a GS|Street|Δ triple must agree with GS/Street-1 to within this.
DELTA_TOLERANCE = Decimal("0.006")


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------


def cells(text: str) -> list[dict[str, Any]]:
    """Non-empty cells (split on newlines and ``|``), each with its span."""

    out = []
    for match in re.finditer(r"[^\n|]+", text):
        raw = match.group(0)
        stripped = raw.strip()
        if not stripped:
            continue
        start = match.start() + (len(raw) - len(raw.lstrip()))
        out.append({"text": stripped, "start": start, "end": start + len(stripped)})
    return out


_YEAR_CELL = re.compile(r"^(?:FY\s?|F)?'?(?P<y>(?:19|20)\d{2}|\d{2})\s*(?:\(?(?P<ae>[AaEe])\)?)?$")
_MY_CELL = re.compile(r"^(?P<m>\d{1,2})/(?P<y>\d{2}|\d{4})\s*(?P<ae>[AaEe])?$")
_QY_CELL = re.compile(r"^F?(?P<q>[1-4])Q\s?'?(?P<y>\d{2}|\d{4})\s*(?:\(?(?P<ae>[AaEe])\)?)?$")
_Q_ONLY = re.compile(r"^(?:Q(?P<q>[1-4])|(?P<fy>FY|Year))$", re.I)
_VALUE = re.compile(r"(?P<sign>[+\-−])?(?P<neg>\()?(?P<cur>US\$|\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                    r"\)?\s?(?P<pct>%)?\s?(?P<ae>[AE])?(?![\w.])")
_PLACEHOLDER = re.compile(r"^(?:-|–|—|NA|N/A|NM|NE|nm|n\.a\.)$")


def period_cell(text: str) -> dict[str, Any] | None:
    """``{"form", "year", "month"|"quarter", "estimate"}`` for a header cell."""

    text = text.strip()
    found = _QY_CELL.match(text)
    if found:
        return {"form": "qy", "year": _year(found.group("y")), "quarter": int(found.group("q")),
                "estimate": (found.group("ae") or "").upper() == "E" or None}
    found = _MY_CELL.match(text)
    if found and 1 <= int(found.group("m")) <= 12:
        return {"form": "my", "year": _year(found.group("y")), "month": int(found.group("m")),
                "estimate": (found.group("ae") or "").upper() == "E"}
    found = _YEAR_CELL.match(text)
    if found:
        year = _year(found.group("y"))
        if 2000 <= year <= 2045 and (text.upper().startswith("F") or len(found.group("y")) == 4
                                     or found.group("ae")):
            return {"form": "y", "year": year,
                    "estimate": (found.group("ae") or "").upper() == "E" if found.group("ae") else None}
    found = _Q_ONLY.match(text)
    if found:
        return {"form": "q", "quarter": int(found.group("q")) if found.group("q") else None}
    return None


def _year(text: str) -> int:
    value = int(text)
    return value + 2000 if value < 100 else value


def values_in(text: str) -> list[dict[str, Any]] | None:
    """The value(s) a cell holds, or None if it is not a value cell.

    ``3.19 E 13.86 E`` is two values; ``-``/``NA`` is one null placeholder.
    """

    text = text.strip()
    year = _YEAR_CELL.match(text)
    if year and (year.group("ae") or text.upper().startswith("F")) and len(year.group("y")) in (2, 4):
        return None  # "2027E" / "FY27" is a period label, not a figure
    if _MY_CELL.match(text) or _QY_CELL.match(text):
        return None
    if _PLACEHOLDER.match(text):
        return [{"value": None, "printed": text, "actual": False, "pct": False}]
    out = []
    position = 0
    for found in _VALUE.finditer(text):
        if text[position:found.start()].strip(" ,"):
            return None
        position = found.end()
        number = found.group("num")
        try:
            value = Decimal(number.replace(",", ""))
        except InvalidOperation:
            return None
        if found.group("neg") or found.group("sign") in {"-", "−"}:
            value = -value
        out.append({"value": value, "printed": found.group(0).strip(), "digits": number,
                    "actual": found.group("ae") == "A", "estimate_mark": found.group("ae") == "E",
                    "pct": bool(found.group("pct"))})
    if not out or text[position:].strip():
        return None
    return out


_EPS_LABEL = re.compile(r"(?i)\bEPS\b|earnings per share")
_REV_LABEL = re.compile(r"(?i)^(?:total\s+)?(?:net\s+)?(?:revenues?|sales(?:\s+revenue)?)\b")
_NOT_LEVEL = re.compile(r"(?i)growth|%|y/y|yoy|\bP/E\b|/\s*revenue|EV/|margin|per employee|ratio")


def metric_of(label: str) -> str | None:
    if _NOT_LEVEL.search(label):
        return None
    if _EPS_LABEL.search(label):
        return "eps"
    if _REV_LABEL.search(label):
        return "revenue"
    return None


def kind_of(label: str) -> str:
    lowered = label.lower()
    if re.search(r"\b(prior|previous|old)\b", lowered):
        return "house_prior"
    if re.search(r"\b(consensus|street|cons\.?|factset|visible alpha)\b", lowered):
        return "street_per_broker"
    return "house"


def _scale_in(text: str) -> str | None:
    if re.search(r"(?i)\$\s?mn\b|\(\s*\$?\s?m\s*\)|US\$\s?m\b|\$\s?m\b|\(MM\)|\bmn\b|\bmillions?\b|\$M\b|\bMM\b", text):
        return "million"
    if re.search(r"(?i)\$\s?bn\b|\bbillions?\b|\(\s*\$?\s?b\s*\)|\$B\b|\bbn\b", text):
        return "billion"
    return None


#: Table boundaries: an exhibit/figure caption starts a block, a source line ends one.
_BLOCK_START = re.compile(r"(?i)^(?:exhibit|figure|table|chart)\s+\d+\b")
_BLOCK_END = re.compile(r"(?i)^source[s]?\s*:")

_SCALE = {"million": Decimal(1_000_000), "billion": Decimal(1_000_000_000), "one": Decimal(1)}


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------


def header_runs(stream: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Maximal runs (length >= 2) of period cells of one form."""

    runs: list[dict[str, Any]] = []
    index = 0
    while index < len(stream):
        first = period_cell(stream[index]["text"])
        if first is None:
            index += 1
            continue
        group = [(index, first)]
        cursor = index + 1
        while cursor < len(stream):
            nxt = period_cell(stream[cursor]["text"])
            if nxt is None or (nxt["form"] == "q") != (first["form"] == "q"):
                break
            group.append((cursor, nxt))
            cursor += 1
        if len(group) >= 2:
            runs.append({"start": group[0][0], "end": group[-1][0], "periods": [g[1] for g in group],
                         "block": stream[group[0][0]].get("block"),
                         "texts": [stream[g[0]]["text"] for g in group],
                         "transposed": first["form"] == "q"})
        index = cursor
    return runs


def _triple_marker(stream: Sequence[Mapping[str, Any]], header: Mapping[str, Any]) -> list[str] | None:
    """``GS | Street | Δ`` (or ``GS | Old | Δ``) repeated once per header column."""

    columns = len(header["periods"])
    for begin in range(max(0, header["start"] - HEADER_REACH_BEFORE), len(stream) - 3 * columns):
        window = [stream[begin + step]["text"] for step in range(3 * columns)]
        roles = []
        for offset in range(0, 3 * columns, 3):
            a, b, c = window[offset:offset + 3]
            if c not in {"Δ", "Delta", "Diff", "% diff", "Var"}:
                roles = []
                break
            roles.append((a, b))
        if roles and all(role == roles[0] for role in roles):
            return [roles[0][0], roles[0][1]]
        if begin > header["end"] + HEADER_REACH_AFTER:
            break
    return None


def _fiscal(period: Mapping[str, Any], fye_month: int, quarter: int | None = None) -> dict[str, Any] | None:
    from .financial_period import fiscal_label, month_end

    if period["form"] == "y":
        year = period["year"]
        if quarter:
            end = month_end(year, fye_month)
            index = end.year * 12 + end.month - 1 - 3 * (4 - quarter)
            return fiscal_label(month_end(index // 12, index % 12 + 1), fye_month, "quarter")
        return fiscal_label(month_end(year, fye_month), fye_month, "fiscal_year")
    if period["form"] == "qy":
        end = month_end(period["year"], fye_month)
        index = end.year * 12 + end.month - 1 - 3 * (4 - period["quarter"])
        return fiscal_label(month_end(index // 12, index % 12 + 1), fye_month, "quarter")
    if period["form"] == "my":
        return None  # resolved per run (annual when every month is the year end)
    return None


def _run_labels(header: Mapping[str, Any], fye_month: int) -> list[dict[str, Any] | None]:
    periods = header["periods"]
    if periods[0]["form"] == "my":
        from .financial_period import fiscal_label, month_end

        months = {item["month"] for item in periods}
        annual = months == {fye_month}
        out = []
        for item in periods:
            end = month_end(item["year"], item["month"])
            out.append(fiscal_label(end, fye_month, "fiscal_year" if annual else "quarter"))
        return out
    return [_fiscal(item, fye_month) for item in periods]


def _estimated(header: Mapping[str, Any]) -> list[bool]:
    """Which columns are estimates: an E mark, or after the last unmarked column of a marked run."""

    marks = [item.get("estimate") for item in header["periods"]]
    if any(mark is True for mark in marks):
        return [mark is True for mark in marks]
    return [True] * len(marks)  # unmarked headers: estimates unless the value says A


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


def parse_tables(text: str, *, fye_month: int, house: str | None = None,
                 published_on: str | None = None) -> dict[str, Any]:
    """Every admissible estimate figure in one note's text.

    Returns ``{"figures": [...], "skipped": [...], "signals": n}`` where
    ``signals`` counts EPS/revenue label cells seen (a note with signals and no
    figures is the one a model pass is for).
    """

    stream = cells(text)
    block = 0
    for cell in stream:
        if _BLOCK_START.match(cell["text"]):
            block += 1
        cell["block"] = block
        if _BLOCK_END.match(cell["text"]):
            block += 1
    runs = header_runs(stream)
    figures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    signals = 0
    section: tuple[str, int, str | None] | None = None  # (metric, cell index, scale)
    index = 0
    while index < len(stream):
        cell = stream[index]
        label = cell["text"]
        metric = metric_of(label)
        values: list[dict[str, Any]] = []
        cursor = index + 1
        while cursor < len(stream):
            found = values_in(stream[cursor]["text"])
            if found is None:
                break
            values.extend(found)
            cursor += 1
        sub = None
        if metric is None and section and index - section[1] <= 40:
            sub_match = re.match(r"(?i)^(?:(prior|consensus)\s+)?(?:Q([1-4])|year|FY)$", label)
            if sub_match:
                sub = sub_match
                metric = section[0]
        if metric is not None:
            signals += 1
            if not values:
                if sub is None:
                    section = (metric, index, _scale_in(label))
                index += 1
                continue
            outcome = _admit_row(stream, runs, index, cursor, label, metric, values, fye_month,
                                 sub=sub, section=section)
            if isinstance(outcome, list):
                figures.extend(outcome)
            else:
                skipped.append({"label": label, "reason": outcome, "span": [cell["start"], stream[cursor - 1]["end"]]})
            if sub is None and section and metric != section[0]:
                section = None
            index = cursor
            continue
        if section and values_in(label) is None and period_cell(label) is None:
            section = None  # another row label ("EBITDA") ends the metric's section
        index += 1
    figures.extend(_transposed(stream, runs, fye_month))
    figures.extend(_prose_revisions(text, fye_month))
    for figure in figures:
        figure.setdefault("house", house)
        figure.setdefault("published_on", published_on)
    return {"figures": _dedupe(figures)[:MAX_FIGURES_PER_DOCUMENT], "skipped": skipped,
            "signals": signals}


def _nearest_header(runs: Sequence[Mapping[str, Any]], row: int, count: int,
                    allow: Callable[[Mapping[str, Any]], bool],
                    row_block: int | None = None) -> tuple[Mapping[str, Any], int] | None:
    """The nearest non-transposed header whose width fits ``count`` (1x or 3x)."""

    best = None
    for run in runs:
        if run["transposed"] or not allow(run):
            continue
        if run.get("block") is not None and run["block"] != row_block:
            continue
        width = len(run["periods"])
        factor = 1 if count == width else 3 if count == 3 * width else None
        if factor is None:
            continue
        if run["end"] < row and row - run["end"] <= HEADER_REACH_BEFORE:
            distance = row - run["end"]
        elif run["start"] > row and run["start"] - row <= HEADER_REACH_AFTER + 3 * count:
            distance = (run["start"] - row) * 2  # prefer headers above the row
        else:
            continue
        if best is None or distance < best[2]:
            best = (run, factor, distance)
    return None if best is None else (best[0], best[1])


def _admit_row(stream, runs, index, cursor, label, metric, values, fye_month, *, sub, section):
    count = len(values)
    header = _nearest_header(runs, index, count, lambda run: True, stream[index].get("block"))
    if header is None:
        return "column_count_matches_no_header"
    run, factor = header
    labels = _run_labels(run, fye_month)
    estimated = _estimated(run)
    scale = None
    if metric == "revenue":
        begin = max(0, min(index, run["start"]) - 12)
        while begin > 0 and stream[begin - 1].get("block") == stream[index].get("block") and index - begin < 200:
            begin -= 1
        context = " ".join(item["text"] for item in stream[begin:index + 1])
        scale = _scale_in(label) or (section[2] if section else None) or _scale_in(context)
        if scale is None:
            return "revenue_scale_not_printed"
    kind = kind_of(label if sub is None else (sub.group(0)))
    quarter = int(sub.group(2)) if sub is not None and sub.group(2) else None
    roles: list[str] = ["single"]
    if factor == 3:
        marker = _triple_marker(stream, run)
        if marker is None:
            return "triple_without_marker"
        roles = ["first", "second", "delta"]
    row_text = " | ".join(item["text"] for item in stream[index:cursor])
    span = [stream[index]["start"], stream[cursor - 1]["end"]]
    out = []
    for column, period in enumerate(labels):
        if period is None:
            continue
        if quarter is not None:
            period = _fiscal(run["periods"][column], fye_month, quarter) if run["periods"][column]["form"] == "y" else None
            if period is None:
                continue
        group = values[column * factor:(column + 1) * factor]
        if factor == 3:
            first, second, delta = group
            if first["value"] is None or second["value"] is None:
                continue
            if delta["value"] is not None and second["value"] != 0 and delta.get("pct"):
                implied = first["value"] / second["value"] - 1
                if abs(implied - delta["value"] / 100) > DELTA_TOLERANCE:
                    return "delta_does_not_reconcile"
            names = marker
            picks = [(first, "house_prior" if names[0].lower() == "old" else "house"),
                     (second, "house_prior" if names[1].lower() == "old" else "street_per_broker")]
        else:
            picks = [(group[0], kind)]
        for value, value_kind in picks:
            if value["value"] is None or value["actual"] or value.get("pct"):
                continue
            if not estimated[column] and not value.get("estimate_mark"):
                continue
            amount = value["value"]
            if metric == "eps" and not (Decimal("-1000") < amount < Decimal("1000")):
                continue
            if metric == "revenue":
                if amount <= 0:
                    continue
                amount = amount * _SCALE[scale]
            if value["digits"] not in row_text:
                continue
            out.append({
                "metric": metric, "estimate_kind": value_kind, "period": period["label"],
                "period_kind": period["period_kind"], "period_end": period["period_end"],
                "value": _plain(amount), "value_as_printed": value["printed"],
                "scale": scale or "one", "unit": "per_share" if metric == "eps" else "currency",
                "label_as_printed": label if sub is None else f"{section[0] if section else ''} {label}".strip(),
                "header_as_printed": run["texts"][column], "quote": row_text[:500], "span": span,
                "layout": "triple" if factor == 3 else "row",
            })
    return out or "no_estimate_cells"


def _transposed(stream, runs, fye_month) -> list[dict[str, Any]]:
    """Q1|Q2|Q3|Q4|FY headers, then ``2027E | 4.00 E | ... | 14.77 E`` rows."""

    out: list[dict[str, Any]] = []
    for run in runs:
        if not run["transposed"]:
            continue
        columns = run["periods"]
        nearby = [stream[i]["text"] for i in range(max(0, run["start"] - 3), min(len(stream), run["end"] + 3))]
        metric = next((metric_of(text) for text in nearby if metric_of(text)), None)
        if metric != "eps":
            continue
        cursor = run["end"] + 1
        rows_seen = 0
        while cursor < len(stream) and rows_seen < 6:
            if metric_of(stream[cursor]["text"]) == "eps":
                cursor += 1
                continue
            year = period_cell(stream[cursor]["text"])
            if year is None or year["form"] != "y":
                break
            values = []
            end = cursor + 1
            while end < len(stream):
                found = values_in(stream[end]["text"])
                if found is None:
                    break
                values.extend(found)
                end += 1
            if len(values) != len(columns):
                break
            row_text = " | ".join(item["text"] for item in stream[cursor:end])
            span = [stream[cursor]["start"], stream[end - 1]["end"]]
            for column, value in zip(columns, values):
                if value["value"] is None or value["actual"]:
                    continue
                if year.get("estimate") is False and not value.get("estimate_mark"):
                    continue
                period = (_fiscal(year, fye_month, column["quarter"]) if column["quarter"]
                          else _fiscal(year, fye_month))
                if period is None or value["digits"] not in row_text:
                    continue
                out.append({
                    "metric": "eps", "estimate_kind": "house", "period": period["label"],
                    "period_kind": period["period_kind"], "period_end": period["period_end"],
                    "value": _plain(value["value"]), "value_as_printed": value["printed"],
                    "scale": "one", "unit": "per_share", "label_as_printed": "EPS",
                    "header_as_printed": f"{stream[cursor]['text']} × {'FY' if not column['quarter'] else 'Q' + str(column['quarter'])}",
                    "quote": row_text[:500], "span": span, "layout": "transposed",
                })
            rows_seen += 1
            cursor = end
    return out


_REVISION = re.compile(
    r"(?i)(?:adjust|rais|lower|cut|trim|tweak|chang|revis)\w*\s+our\s+FY\s?'?(?P<y1>\d{2})(?:/(?P<y2>\d{2}))?(?:/(?P<y3>\d{2}))?"
    r"\s+EPS\s+estimates?\s+to\s+\$(?P<n1>\d+\.\d+)(?:/\$(?P<n2>\d+\.\d+))?(?:/\$(?P<n3>\d+\.\d+))?"
    r"\s+from\s+\$(?P<o1>\d+\.\d+)(?:/\$(?P<o2>\d+\.\d+))?(?:/\$(?P<o3>\d+\.\d+))?")


def _prose_revisions(text: str, fye_month: int) -> list[dict[str, Any]]:
    """"we adjust our FY27/28 EPS estimates to $14.75/$16.30 from $14.55/$15.95"."""

    out = []
    flat = text.replace("\n", " ")
    for found in _REVISION.finditer(flat):
        quote = found.group(0)
        span = [found.start(), found.end()]
        for slot in ("1", "2", "3"):
            year, new, old = found.group(f"y{slot}"), found.group(f"n{slot}"), found.group(f"o{slot}")
            if not (year and new):
                continue
            period = _fiscal({"form": "y", "year": _year(year)}, fye_month)
            for value, kind in ((new, "house"), (old, "house_prior")):
                if value:
                    out.append({"metric": "eps", "estimate_kind": kind, "period": period["label"],
                                "period_kind": "fiscal_year", "period_end": period["period_end"],
                                "value": _plain(Decimal(value)), "value_as_printed": f"${value}", "scale": "one",
                                "unit": "per_share", "label_as_printed": "EPS estimates",
                                "header_as_printed": f"FY{year}", "quote": quote[:500], "span": span,
                                "layout": "prose"})
    return out


_TARGET_CHANGE = re.compile(
    r"(?i)(?:price target|target price|\bPT\b|\bTP\b)[^.\n$]{0,30}?(?:to\s+)?\$\s?(?P<new>\d+(?:\.\d+)?)"
    r"\s*\(?\s*(?:from|prior:?|previous:?|was)\s+\$\s?(?P<old>\d+(?:\.\d+)?)\)?")
_RATING_CHANGE = re.compile(
    r"(?i)\b(?P<verb>upgrad\w*|downgrad\w*)\b[^.\n]{0,40}?\bto\s+(?P<to>buy|overweight|outperform|neutral|hold|"
    r"equal[- ]weight|market perform|sell|underweight|underperform)\b")


def rating_target_changes(text: str, *, page_chars: int = 6000) -> list[dict[str, Any]]:
    """Target and rating changes stated on the first pages, with their quotes."""

    head = text[:page_chars]
    out = []
    for found in _TARGET_CHANGE.finditer(head):
        new, old = Decimal(found.group("new")), Decimal(found.group("old"))
        if new == old or not (Decimal(1) < new < Decimal(100000)):
            continue
        out.append({"kind": "target_change", "to": found.group("new"), "from": found.group("old"),
                    "direction": "raised" if new > old else "lowered",
                    "quote": found.group(0)[:300], "span": [found.start(), found.end()]})
    for found in _RATING_CHANGE.finditer(head):
        out.append({"kind": "rating_change", "direction": "upgrade" if found.group("verb").lower().startswith("up")
                    else "downgrade", "to": found.group("to"), "quote": found.group(0)[:300],
                    "span": [found.start(), found.end()]})
    return out


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _dedupe(figures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One figure per (metric, period, kind).  Two tables printing the same
    number at different precision (73,469.9 and 73,470) agree; the more precise
    one is kept.  Two that disagree beyond rounding cancel each other."""

    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    out = []
    for figure in figures:
        key = (figure["metric"], figure["period"], figure["estimate_kind"])
        if key in seen:
            held, new = Decimal(seen[key]["value"]), Decimal(figure["value"])
            if held != new:
                close = held != 0 and abs(held - new) / abs(held) <= Decimal("0.001")
                if not close:
                    seen[key].setdefault("conflicting_values", []).append(figure["value"])
                elif len(str(figure.get("value_as_printed"))) > len(str(seen[key].get("value_as_printed"))):
                    keep = seen[key].get("conflicting_values")
                    seen[key].clear()
                    seen[key].update(dict(figure))
                    if keep:
                        seen[key]["conflicting_values"] = keep
            continue
        seen[key] = dict(figure)
        out.append(seen[key])
    # A figure two tables disagree about is not admitted.
    return [figure for figure in out if not figure.get("conflicting_values")]


# ---------------------------------------------------------------------------
# model fallback (injected, budget-gated)
# ---------------------------------------------------------------------------


def model_fallback(text: str, *, company_ref: str, document_ref: str, house: str | None,
                   subject_as_named: str, model_call: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
                   budget_ok: Callable[[str], bool]) -> dict[str, Any]:
    """The cheap-model pass, through ``street_estimate_extraction``'s frozen surface.

    ``budget_ok(purpose)`` must say yes (the existing purpose cap for
    ``street_estimate``) before any call; the response is verified whole.
    """

    from .street_estimate_extraction import (
        MODEL_PURPOSE, StreetEstimateExtractionError, build_prompt, build_request,
        verify_estimate_table,
    )

    if not budget_ok(MODEL_PURPOSE):
        return {"status": "budget_refused", "figures": []}
    quotes = []
    for start in range(0, min(len(text), 60_000), 1500):
        chunk = text[start:start + 1500]
        quotes.append({"quote_id": f"quote:{start}:{start + len(chunk)}", "raw_text": chunk})
    request = build_request({"company_ref": company_ref, "document_ref": document_ref,
                             "broker": house, "quotes": quotes})
    response = model_call(build_prompt(request), request)
    try:
        verified = verify_estimate_table(response, quotes={q["quote_id"]: q["raw_text"] for q in quotes},
                                         subject_as_named=subject_as_named)
    except StreetEstimateExtractionError as exc:
        return {"status": "refused", "reason": str(exc), "figures": []}
    return {"status": "model_recorded", "figures": verified}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


class StreetEstimateFigureStore:
    """``street_estimate_figures`` + ``street_estimate_figure_scans``, append-only."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("StreetEstimateFigureStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def scanned(self, company_ref: str, extractor_ref: str = EXTRACTOR_REF) -> set[str]:
        return {row[0] for row in self.connection.execute(
            "SELECT document_ref FROM street_estimate_figure_scans WHERE company_ref=? AND extractor_ref=?",
            (company_ref, extractor_ref))}

    def record_document(self, *, company_ref: str, document_ref: str, house: str | None,
                        published_on: str | None, figures: Sequence[Mapping[str, Any]],
                        changes: Sequence[Mapping[str, Any]] = (), outcome: str | None = None,
                        reason: str | None = None, extraction_method: str = "deterministic",
                        source_sha256: str | None = None, now: str | None = None,
                        extractor_ref: str = EXTRACTOR_REF) -> dict[str, Any]:
        if self.connection.execute(
                "SELECT 1 FROM street_estimate_figure_scans WHERE company_ref=? AND document_ref=? "
                "AND extractor_ref=?", (company_ref, document_ref, extractor_ref)).fetchone():
            return {"status": "duplicate", "recorded": 0}
        created = now or datetime.now(timezone.utc).isoformat(timespec="microseconds")
        outcome = outcome or ("recorded" if figures or changes else "none_found")
        if outcome not in SCAN_OUTCOMES:
            raise ValueError("unknown scan outcome")
        rows = []
        for figure in figures:
            body = {"schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
                    "company_ref": company_ref, "document_ref": document_ref, "house": house,
                    "published_on": published_on, "extraction_method": extraction_method,
                    "source_sha256": source_sha256, "basis": "broker-estimate",
                    **{key: value for key, value in figure.items() if key not in {"house", "published_on"}}}
            body["id"] = "street-estimate-figure:" + content_hash(
                {k: body.get(k) for k in ("company_ref", "document_ref", "parser_version", "metric",
                                           "period", "estimate_kind", "value")})[:32]
            body["created_at"] = created
            body["content_hash"] = content_hash(body)
            rows.append(body)
        change_body = [dict(item) for item in changes]
        with self.store._transaction() as cur:
            for body in rows:
                cur.execute(
                    "INSERT OR IGNORE INTO street_estimate_figures (figure_id, company_ref, document_ref, house, "
                    "published_on, period, period_kind, metric, estimate_kind, value, extraction_method, "
                    "parser_version, record_json, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (body["id"], company_ref, document_ref, house, published_on, body["period"],
                     body["period_kind"], body["metric"], body["estimate_kind"], body["value"],
                     extraction_method, PARSER_VERSION, canonical_json(body), body["content_hash"], created))
            scan = {"company_ref": company_ref, "document_ref": document_ref, "outcome": outcome,
                    "reason": reason, "figure_count": len(rows), "changes": change_body,
                    "extractor_ref": extractor_ref, "house": house, "published_on": published_on,
                    "scanned_at": created}
            cur.execute(
                "INSERT INTO street_estimate_figure_scans (scan_id, company_ref, document_ref, outcome, reason, "
                "figure_count, extractor_ref, record_json, scanned_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("street-estimate-figure-scan:" + content_hash(
                    {"c": company_ref, "d": document_ref, "e": extractor_ref})[:32],
                 company_ref, document_ref, outcome, reason, len(rows), extractor_ref,
                 canonical_json(scan), created))
        return {"status": outcome, "recorded": len(rows)}


# ---------------------------------------------------------------------------
# reading held notes (read-only) and scanning them
# ---------------------------------------------------------------------------


def held_broker_notes(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """Notes this Core already attributes to one house for this company (newest first)."""

    from .financial_period import _refs, _table_exists, query

    if not _table_exists(connection, "street_estimates"):
        return []
    refs = _refs(company_ref)
    rows = query(connection,
                 f"SELECT document_ref, broker, published_on, source_manifest_hash FROM street_estimates "
                 f"WHERE company_ref IN ({','.join('?' * len(refs))}) ORDER BY published_on DESC", refs)
    seen, out = set(), []
    for row in rows:
        if row["document_ref"] in seen:
            continue
        seen.add(row["document_ref"])
        out.append(dict(row))
    return out


def note_text(state_dir: Path | str, document_ref: str,
              manifests: Mapping[str, Path] | None = None) -> tuple[str | None, str | None, str | None]:
    """``(text, sha256, reason)`` for an acquired note, from its manifest and the spool."""

    from .acquired_sec_filings import _spool_bytes

    index = manifests if manifests is not None else manifest_index(state_dir)
    path = index.get(document_ref)
    if path is None:
        return None, None, "这份研报没有获取记录"
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, "研报获取记录读不出"
    digest = ((manifest.get("assembled_object") or {}).get("content_hash")
              or manifest.get("assembled_prefix_sha256"))
    if not isinstance(digest, str):
        return None, None, "研报获取记录里没有正文哈希"
    raw, reason = _spool_bytes(Path(state_dir), digest)
    if raw is None:
        return None, digest, reason
    return raw.decode("utf-8", errors="replace"), digest, None


def manifest_index(state_dir: Path | str) -> dict[str, Path]:
    """``document_ref -> manifest path`` over ``<state>/acquisitions/*/manifest.json``."""

    out: dict[str, Path] = {}
    root = Path(state_dir) / "acquisitions"
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/manifest.json")):
        try:
            head = path.read_text(encoding="utf-8")
        except OSError:
            continue
        found = re.search(r'"document_ref"\s*:\s*"([^"]+)"', head)
        if found:
            out.setdefault(found.group(1), path)
    return out


def scan_company(store: StreetEstimateFigureStore, state_dir: Path | str, company_ref: str, *,
                 fye_month: int, limit: int = 1,
                 model_call: Callable[..., Mapping[str, Any]] | None = None,
                 budget_ok: Callable[[str], bool] | None = None,
                 subject_as_named: str | None = None) -> list[dict[str, Any]]:
    """Scan up to ``limit`` held notes not yet read by this parser; append results."""

    done = store.scanned(company_ref)
    manifests = manifest_index(state_dir)
    results = []
    for note in held_broker_notes(store.connection, company_ref):
        if len(results) >= limit:
            break
        if note["document_ref"] in done:
            continue
        text, digest, reason = note_text(state_dir, note["document_ref"], manifests)
        if text is None:
            results.append({"document_ref": note["document_ref"], **store.record_document(
                company_ref=company_ref, document_ref=note["document_ref"], house=note["broker"],
                published_on=note["published_on"], figures=(), outcome="refused", reason=reason)})
            continue
        parsed = parse_tables(text, fye_month=fye_month, house=note["broker"],
                              published_on=note["published_on"])
        changes = rating_target_changes(text)
        outcome, method, figures = None, "deterministic", parsed["figures"]
        why = None
        if not figures and parsed["signals"]:
            if model_call is not None and budget_ok is not None:
                fallback = model_fallback(text, company_ref=company_ref, document_ref=note["document_ref"],
                                          house=note["broker"], subject_as_named=subject_as_named or "",
                                          model_call=model_call, budget_ok=budget_ok)
                outcome, why = fallback["status"], fallback.get("reason")
                if fallback["status"] == "model_recorded":
                    method = "model"
                    figures = [_from_model(item, fye_month) for item in fallback["figures"]]
                    figures = [item for item in figures if item]
                elif fallback["status"] == "budget_refused":
                    outcome, why = "needs_model", "street_estimate 预算池已用尽"
                else:
                    outcome = "refused"
            else:
                outcome, why = "needs_model", "表格列数无法确定性对齐；未配置模型读取"
        results.append({"document_ref": note["document_ref"], "figures": len(figures),
                        **store.record_document(
                            company_ref=company_ref, document_ref=note["document_ref"], house=note["broker"],
                            published_on=note["published_on"], figures=figures, changes=changes,
                            outcome=outcome, reason=why, extraction_method=method, source_sha256=digest)})
    return results


def _from_model(item: Mapping[str, Any], fye_month: int) -> dict[str, Any] | None:
    metric = "eps" if item.get("metric_ref") == "metric:eps-estimate" else (
        "revenue" if item.get("metric_ref") == "metric:revenue-estimate" else None)
    header = period_cell(str(item.get("period") or ""))
    if metric is None or header is None or header["form"] not in {"y", "qy", "my"}:
        return None
    period = _fiscal(header, fye_month)
    if period is None:
        return None
    scale = str(item.get("scale") or "one")
    value = Decimal(str(item.get("value"))) * _SCALE.get(scale, Decimal(1))
    return {"metric": metric, "estimate_kind": "house", "period": period["label"],
            "period_kind": period["period_kind"], "period_end": period["period_end"], "value": _plain(value),
            "value_as_printed": str(item.get("value")), "scale": scale,
            "unit": "per_share" if metric == "eps" else "currency",
            "label_as_printed": item.get("as_reported_label"),
            "quote": item.get("citation_text") or item.get("quote"), "quote_id": item.get("quote_id"),
            "span": None, "layout": "model"}


# ---------------------------------------------------------------------------
# the street table
# ---------------------------------------------------------------------------


def street_table_from(connection: sqlite3.Connection | None, company_ref: str, *,
                      as_of: str | None = None, window_days: int = 150,
                      figures: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Per fiscal period and metric: each house's newest estimate, and their spread.

    ``figures`` lets a read-only caller pass freshly parsed figures (for a Core
    that has not stored them yet); otherwise they are read from the table.
    """

    from .financial_period import _refs, _table_exists, query

    out: dict[str, Any] = {"company_ref": company_ref, "as_of": as_of, "window_days": window_days,
                           "periods": [], "status": "empty", "reason": None}
    rows: list[dict[str, Any]] = []
    if figures is not None:
        rows = [dict(item) for item in figures]
    elif connection is not None and _table_exists(connection, "street_estimate_figures"):
        refs = _refs(company_ref)
        rows = [json.loads(row["record_json"]) for row in query(
            connection, f"SELECT record_json FROM street_estimate_figures WHERE company_ref IN "
                        f"({','.join('?' * len(refs))})", refs)]
    targets = []
    if connection is not None and _table_exists(connection, "street_estimates"):
        refs = _refs(company_ref)
        targets = query(connection,
                        f"SELECT broker, published_on, rating, target_value FROM street_estimates WHERE company_ref IN "
                        f"({','.join('?' * len(refs))}) ORDER BY published_on DESC", refs)
    floor = None
    if as_of:
        floor = (date.fromisoformat(str(as_of)[:10]) - timedelta(days=window_days)).isoformat()
        rows = [row for row in rows if str(row.get("published_on") or "") <= str(as_of)[:10]]
        targets = [row for row in targets if str(row["published_on"]) <= str(as_of)[:10]]
    if floor:
        rows = [row for row in rows if str(row.get("published_on") or "") >= floor]
        targets = [row for row in targets if str(row["published_on"]) >= floor]
    newest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: str(r.get("published_on") or "")):
        key = (row["period"], row["metric"], row["estimate_kind"], str(row.get("house")))
        newest[key] = row
    by_period: dict[tuple[str, str], dict[str, Any]] = {}
    for (period, metric, kind, house), row in newest.items():
        entry = by_period.setdefault((period, metric), {
            "period": period, "period_kind": row.get("period_kind"), "metric": metric,
            "houses": [], "street_quoted": [], "priors": []})
        item = {"house": house, "value": row["value"], "published_on": row.get("published_on"),
                "document_ref": row.get("document_ref"), "quote": row.get("quote")}
        {"house": entry["houses"], "street_per_broker": entry["street_quoted"],
         "house_prior": entry["priors"]}[kind].append(item)
    periods = []
    for key in sorted(by_period, key=lambda k: (k[0][:6], len(k[0]), k[0], k[1])):
        entry = by_period[key]
        values = [Decimal(item["value"]) for item in entry["houses"]]
        if values:
            entry.update({"count": len(values), "median": _plain(Decimal(statistics.median(values))),
                          "mean": _plain(sum(values) / len(values)), "high": _plain(max(values)),
                          "low": _plain(min(values))})
        else:
            entry.update({"count": 0, "median": None, "mean": None, "high": None, "low": None})
        periods.append(entry)
    latest_target: dict[str, dict[str, Any]] = {}
    for row in targets:
        latest_target.setdefault(row["broker"], dict(row))
    out.update({
        "periods": periods,
        "houses": sorted({str(item["house"]) for e in periods for item in e["houses"]}),
        "targets": sorted(latest_target.values(), key=lambda r: str(r["broker"])),
        "status": "ok" if periods or latest_target else "empty",
        "reason": None if periods or latest_target else "还没有从券商研报读出盈利预测表或目标价",
    })
    return out


def parsed_unstored(connection: sqlite3.Connection, state_dir: Path | str,
                    company_ref: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Figures and changes parsed read-only from held notes, or ``(None, [])``
    when figures are already stored (then the stored ones are the answer)."""

    from .financial_period import _table_exists, fiscal_year_end

    if _table_exists(connection, "street_estimate_figures") and connection.execute(
            "SELECT 1 FROM street_estimate_figures WHERE company_ref=? LIMIT 1", (company_ref,)).fetchone():
        return None, []
    fye = fiscal_year_end(connection, company_ref)
    if fye is None:
        return None, []
    figures: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    manifests = manifest_index(state_dir)
    for note in held_broker_notes(connection, company_ref):
        text, _digest, _reason = note_text(state_dir, note["document_ref"], manifests)
        if text is None:
            continue
        parsed = parse_tables(text, fye_month=fye["month"], house=note["broker"],
                              published_on=note["published_on"])
        for item in parsed["figures"]:
            item["document_ref"] = note["document_ref"]
        figures.extend(parsed["figures"])
        for change in rating_target_changes(text):
            changes.append({**change, "house": note["broker"], "published_on": note["published_on"],
                            "document_ref": note["document_ref"]})
    return figures, changes


def street_table(state_dir: Path | str, company_ref: str, *, as_of: str | None = None,
                 window_days: int = 150, parse_unstored: bool = False,
                 core_db: Path | str | None = None) -> dict[str, Any]:
    """The street table, read-only.  ``parse_unstored`` parses held notes on the fly
    (no write) when the Core has no stored figures -- for a read-only copy."""

    from .consensus_table import open_core

    with open_core(core_db or state_dir) as connection:
        figures, changes = (parsed_unstored(connection, state_dir, company_ref)
                            if parse_unstored and connection is not None else (None, []))
        table = street_table_from(connection, company_ref, as_of=as_of, window_days=window_days,
                                  figures=figures)
        if figures is not None:
            table["stored"] = False
            table["changes"] = changes
        return table


__all__ = [
    "ESTIMATE_KINDS", "EXTRACTOR_REF", "PARSER_VERSION", "StreetEstimateFigureStore",
    "cells", "header_runs", "held_broker_notes", "manifest_index", "metric_of", "model_fallback",
    "note_text", "parse_tables", "parsed_unstored", "period_cell", "rating_target_changes", "scan_company",
    "street_table", "street_table_from", "values_in",
]
