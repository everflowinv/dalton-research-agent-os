"""W3: a prior human Excel model, read into an inert authority.

Somebody maintains a model for this company. It has assumptions in it -- a
revenue growth rate, a margin, an attrition number -- and those assumptions are
worth a great deal to a new analyst, for exactly one reason: they say *how this
team has thought about this company before*. A driver model that is about to
pick a range for FY27 organic growth is better for knowing that the last three
versions of the house view sat between 4% and 7%, and better still for knowing
which of those turned out wrong.

That is the whole use. It is not a source of numbers.

**Nothing here ever becomes a figure.** A prior model's revenue line is not a
fact about the company; it is a fact about what we assumed. So this module:

* writes its own table and never a row of ``coverage_mission_statement_lines``,
  ``model_forecast_line_versions`` or ``coverage_mission_document_figures``;
* never mints a ``kind: "actual"`` anything -- every assumption it stores is
  ``prior_human``, a word that exists in this module and in no other;
* never touches ``mission_figure_authority``: it registers no provenance mode,
  adds no grade to ``FIGURE_ADMISSIBLE_GRADES``, and passes no
  ``verified_figure`` anywhere;
* imports nothing from ``model_forecast``, ``model_forecast_driver``,
  ``forecast_reconciliation``, ``coverage_mission``, ``claim_index_figures`` or
  ``research_verification``, and is imported by none of them. The arrow is
  one-way, and a test walks the import graph to keep it that way.

**Formulas are kept verbatim as text.** Not evaluated, not translated, not
normalised. A prior model's formula is the most compressed statement of how a
person thought the business worked -- ``=B12*(1+B13)-B14`` says "revenue grows
at the rate above and we lose this much" -- and the moment it is parsed into
something this system computes with, it stops being a record of a judgement
and becomes a claim about a number.

**Values are decimal strings and there is no float in the record.** Excel hands
out binary floats; this takes ``repr()`` of them, which is the shortest string
that round-trips and is what the spreadsheet displayed. No rounding is applied,
because the quantum a prior model's ratios were kept at is itself information
and picking one here would invent precision or destroy it.

**Units are guessed, and the guess carries its basis.** A cell formatted as a
percent is a percent; a column header saying "$mn" is millions of dollars; a
bare number is a bare number and the unit is ``None`` with basis ``unknown``.
A wrong unit is worse than no unit, so the ladder is short and every rung says
which one it stood on.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.2"
LEGACY_SCHEMA_VERSION = "0.1"
TABLE = "prior_model_versions"
_SCHEMA_PATH = Path(__file__).with_name("prior_model_schema.sql")

#: The one kind an assumption imported from a prior workbook may carry. It is
#: deliberately not a word in ``model_forecast_driver.ASSUMPTION_KINDS``: that
#: vocabulary belongs to rows that are computed with, and adding a sixth word
#: there would re-hash every live ForecastModelVersion and let a prior number
#: into an arithmetic path. This one lives here and travels nowhere.
ASSUMPTION_KIND = "prior_human"

#: How a unit was arrived at, weakest last.
UNIT_BASES: tuple[str, ...] = (
    # the cell's own Excel number format said so
    "number_format",
    # the row or column label said so
    "label",
    # nothing said so
    "unknown",
)

#: The units this import will name. Short on purpose: a unit it cannot be sure
#: of is no unit, because a band read in the wrong unit is worse than a band
#: nobody could read at all.
UNITS: tuple[str, ...] = ("percent", "ratio", "currency", "count")

MAX_SHEETS = 40
MAX_ROWS_PER_SHEET = 2_000
MAX_COLUMNS_PER_SHEET = 200
# Kept as a legacy export for callers that imported the old safety ceiling.
# New reads have no silent global cell cap; explicit budgets are recorded.
MAX_ASSUMPTIONS = 5_000


@dataclass(frozen=True)
class WorkbookReadBudget:
    max_sheets: int | None = None
    max_rows_per_sheet: int | None = None
    max_columns_per_sheet: int | None = None
    max_cells: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "WorkbookReadBudget":
        raw = {} if value is None else dict(value)
        unknown = set(raw) - {"max_sheets", "max_rows_per_sheet",
                              "max_columns_per_sheet", "max_cells"}
        if unknown:
            raise PriorModelError(f"workbook read budget has unknown fields {sorted(unknown)}")
        for key, item in raw.items():
            if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 1):
                raise PriorModelError(f"workbook read budget {key} must be a positive integer or null")
        return cls(**raw)


class WorkbookReadArtifact(list[dict[str, Any]]):
    def __init__(self, cells: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]) -> None:
        super().__init__(dict(item) for item in cells)
        self.metadata = dict(metadata)
MAX_LABEL_CHARS = 200
MAX_FORMULA_CHARS = 8_192

_CELL_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_PERCENT_LABEL_RE = re.compile(r"%|percent|率|margin|growth|yoy|cagr", re.IGNORECASE)
_CURRENCY_LABEL_RE = re.compile(
    r"[$€£¥]|\busd\b|\beur\b|\bcny\b|\brmb\b|\bhkd\b|\bmn\b|\bbn\b|million|billion|"
    r"revenue|sales|ebitda|ebit|income|cost|capex|opex",
    re.IGNORECASE,
)
_COUNT_LABEL_RE = re.compile(
    r"headcount|employees|人数|count|units|clients|customers", re.IGNORECASE
)

#: A cell whose label matched nothing and whose value sits in [-1, 1] is *not*
#: called a ratio on that basis alone. A margin of 0.42 and a share count of
#: 0.42 million look identical to a range check, and guessing from magnitude is
#: how a unit becomes wrong quietly.
_RATIO_FORMAT_RE = re.compile(r"0\.0+$|#,##0\.0+$")


class PriorModelError(RuntimeError):
    """The prior model wire is malformed."""


class PriorModelConflict(RuntimeError):
    """The prior model authority disagrees with what was written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PriorModelError(f"{name} must be non-empty text")
    return value.strip()[:maximum]


def _optional_text(value: Any, name: str, *, maximum: int = 512) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PriorModelError(f"{name} must be text")
    return value.strip()[:maximum]


def _day(value: Any, name: str) -> str:
    text = _text(value, name, maximum=10)
    if _DATE_RE.fullmatch(text) is None:
        raise PriorModelError(f"{name} must be a YYYY-MM-DD date")
    return text


def decimal_text(value: Any) -> str:
    """One number as the exact decimal string of what the sheet displayed.

    ``repr`` of a float is the shortest string that round-trips it, which is
    also what Excel showed the person who typed it. Nothing is rounded here:
    the precision a prior model kept its ratios at is itself a fact about how
    the model was built, and a quantum chosen in this module would either
    invent precision the sheet never had or throw away precision it did.
    """

    if isinstance(value, bool):
        raise PriorModelError("a boolean is not a model assumption")
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise PriorModelError("a non-finite cell is not a model assumption")
        text = repr(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise PriorModelError(f"{type(value).__name__} is not a numeric cell")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise PriorModelError(f"{text!r} is not a decimal") from exc
    if not parsed.is_finite():
        raise PriorModelError("a non-finite cell is not a model assumption")
    formatted = format(parsed, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return "0" if formatted in {"", "-0"} else formatted


def guess_unit(*, label: str, number_format: str | None) -> tuple[str | None, str]:
    """``(unit, unit_basis)`` for one cell, or ``(None, "unknown")``.

    The number format is asked first because it is the sheet's own statement
    and the label is an inference from prose. A percent-formatted cell is a
    percent whatever the row is called.
    """

    fmt = (number_format or "").strip()
    if "%" in fmt:
        return "percent", "number_format"
    if fmt and any(symbol in fmt for symbol in ("$", "€", "£", "¥", "USD", "CNY")):
        return "currency", "number_format"
    text = (label or "").strip()
    if text:
        if _PERCENT_LABEL_RE.search(text):
            return "percent", "label"
        if _COUNT_LABEL_RE.search(text):
            return "count", "label"
        if _CURRENCY_LABEL_RE.search(text):
            return "currency", "label"
    if fmt and _RATIO_FORMAT_RE.search(fmt):
        return "ratio", "number_format"
    return None, "unknown"


def normalize_label(label: Any) -> str:
    """A driver label folded for matching: case, spacing and punctuation."""

    text = str(label or "").strip().lower()
    text = re.sub(r"[^\w\s%]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


# -- reading the workbook ------------------------------------------------


def _cell_label(
    values: Sequence[Sequence[Any]], row_index: int, column_index: int
) -> str:
    """The nearest text to the left on this row, else the nearest above it.

    A model's assumption cells are labelled the way a person labels them: a
    row header on the left, or a column header at the top. This looks left
    first because a row header is the more specific of the two.
    """

    row = values[row_index]
    for back in range(column_index - 1, -1, -1):
        candidate = row[back]
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for up in range(row_index - 1, -1, -1):
        above = values[up]
        if column_index < len(above):
            candidate = above[column_index]
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def read_workbook(path: str | Path, *, budget: WorkbookReadBudget | Mapping[str, Any] | None = None
                  ) -> WorkbookReadArtifact:
    """Every numeric cell of a workbook, with its formula and its label.

    The workbook is opened twice on purpose: once with ``data_only=True`` for
    the last-computed values Excel cached, once without for the formula text.
    A cell that has a formula and no cached value is kept -- the formula is
    the part worth keeping -- with ``value`` absent.
    """

    try:
        from openpyxl import load_workbook
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the env
        raise PriorModelError(
            "importing a prior Excel model needs the optional `prior-models` "
            "extra (openpyxl); without it the workbook is refused rather than "
            "guessed at"
        ) from exc

    limits = budget if isinstance(budget, WorkbookReadBudget) else WorkbookReadBudget.from_mapping(budget)
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise PriorModelError("prior model workbook is missing")
    valued = load_workbook(filename=str(target), read_only=False, data_only=True)
    formulas = load_workbook(filename=str(target), read_only=False, data_only=False)
    try:
        assumptions: list[dict[str, Any]] = []
        sheet_metadata: list[dict[str, Any]] = []
        truncations: list[dict[str, Any]] = []
        formula_identity: list[dict[str, str]] = []
        for sheet_index, sheet in enumerate(valued.worksheets):
            if limits.max_sheets is not None and sheet_index >= limits.max_sheets:
                truncations.append({"scope": "workbook", "reason": "max_sheets",
                                    "omitted_sheet_count": len(valued.worksheets) - sheet_index})
                break
            formula_sheet = (
                formulas[sheet.title] if sheet.title in formulas.sheetnames else None
            )
            max_row = sheet.max_row if limits.max_rows_per_sheet is None else min(sheet.max_row, limits.max_rows_per_sheet)
            max_col = sheet.max_column if limits.max_columns_per_sheet is None else min(sheet.max_column, limits.max_columns_per_sheet)
            grid = [
                list(row[:max_col])
                for row in sheet.iter_rows(
                    max_row=max_row, max_col=max_col,
                    values_only=True,
                )
            ]
            formula_grid = [] if formula_sheet is None else [
                list(row[:max_col])
                for row in formula_sheet.iter_rows(
                    max_row=max_row, max_col=max_col,
                    values_only=True,
                )
            ]
            calendar_rows = _calendar_rows(grid)
            for row_index, row in enumerate(grid):
                for column_index, value in enumerate(row):
                    # Decide first, materialise second. A model sheet is mostly
                    # empty and mostly labels; asking openpyxl for a Cell object
                    # and a coordinate string per visited position built tens of
                    # thousands of objects to throw nearly all of them away.
                    raw_formula = None
                    if row_index < len(formula_grid) and column_index < len(
                        formula_grid[row_index]
                    ):
                        raw_formula = formula_grid[row_index][column_index]
                    formula = raw_formula if isinstance(raw_formula, str) and raw_formula.startswith("=") else ""
                    if len(formula) > MAX_FORMULA_CHARS:
                        raise PriorModelError(
                            f"formula {sheet.title}!R{row_index + 1}C{column_index + 1} "
                            f"exceeds {MAX_FORMULA_CHARS} characters")
                    numeric = isinstance(value, (int, float, Decimal)) and not isinstance(
                        value, bool
                    )
                    if not numeric and not formula:
                        continue
                    cell = sheet.cell(row=row_index + 1, column=column_index + 1)
                    formula_cell = formula_sheet.cell(row=row_index + 1, column=column_index + 1)
                    address = str(cell.coordinate)
                    raw_label = _cell_label(grid, row_index, column_index)
                    label = raw_label[:MAX_LABEL_CHARS]
                    if len(raw_label) > MAX_LABEL_CHARS:
                        truncations.append({"scope": "cell_projection", "sheet": sheet.title,
                                            "cell": address, "reason": "label_chars",
                                            "original_chars": len(raw_label)})
                    raw_number_format = str(cell.number_format)
                    if len(raw_number_format) > 200:
                        truncations.append({"scope": "cell_projection", "sheet": sheet.title,
                                            "cell": address, "reason": "number_format_chars",
                                            "original_chars": len(raw_number_format)})
                    unit, unit_basis = guess_unit(
                        label=label, number_format=cell.number_format
                    )
                    entry: dict[str, Any] = {
                        "sheet": str(sheet.title)[:MAX_LABEL_CHARS],
                        "cell": address,
                        "label": label,
                        "value": decimal_text(value) if numeric else None,
                        "formula": formula,
                        "unit": unit,
                        "unit_basis": unit_basis,
                        "cell_role": ("external_formula" if "_xll." in formula or "[" in formula
                                      else "cross_sheet_formula" if formula and "!" in formula
                                      else "formula" if formula else "hardcoded_input"),
                        "number_format": raw_number_format[:200],
                        "font_color": _font_color(formula_cell),
                        "period": _period_label(calendar_rows, row_index, column_index),
                        "period_status": _period_status(_period_label(calendar_rows, row_index, column_index)),
                        "period_basis": ("calendar_axis" if _period_label(
                            calendar_rows, row_index, column_index) is not None else None),
                        "source_type": "prior_human_formula" if formula else "prior_human_hardcode",
                    }
                    assumptions.append(entry)
                    if formula:
                        formula_identity.append({"sheet": sheet.title, "cell": address,
                                                 "formula": formula})
                    if limits.max_cells is not None and len(assumptions) >= limits.max_cells:
                        truncations.append({"scope": "workbook", "reason": "max_cells",
                                            "at_sheet": sheet.title, "at_cell": address})
                        break
                if truncations and truncations[-1].get("reason") == "max_cells":
                    break
            sheet_metadata.append({"name": sheet.title, "state": sheet.sheet_state,
                                   "max_row": sheet.max_row, "max_column": sheet.max_column,
                                   "imported_rows": max_row, "imported_columns": max_col})
            if sheet.max_row > max_row:
                truncations.append({"scope": "sheet", "sheet": sheet.title,
                                    "reason": "max_rows_per_sheet", "omitted_from_row": max_row + 1})
            if sheet.max_column > max_col:
                truncations.append({"scope": "sheet", "sheet": sheet.title,
                                    "reason": "max_columns_per_sheet", "omitted_from_column": max_col + 1})
            if limits.max_cells is not None and len(assumptions) >= limits.max_cells:
                break
        metadata = {"schema_version": "prior-workbook-import-0.1",
                    "complete": not truncations, "budget": limits.__dict__,
                    "sheets": sheet_metadata, "truncations": truncations,
                    "formula_map_hash": content_hash(formula_identity)}
        return WorkbookReadArtifact(assumptions, metadata)
    finally:
        valued.close()
        formulas.close()


def _font_color(cell: Any) -> str | None:
    color = getattr(getattr(cell, "font", None), "color", None)
    if color is None:
        return None
    if color.type == "rgb" and isinstance(color.rgb, str):
        return color.rgb
    if color.type == "theme" and isinstance(color.theme, int):
        return f"theme:{color.theme}"
    return None


_PERIOD_RE = re.compile(
    r"(?:(?:FY|CY)?(?:19|20|21)\d{2}|[1-4]Q(?:\d{2}|(?:19|20|21)\d{2}))[AE]?"
)


def _calendar_rows(values: Sequence[Sequence[Any]]) -> list[tuple[int, dict[int, str]]]:
    axes = []
    for index, row in enumerate(values):
        tokens = {column: str(value).strip() for column, value in enumerate(row)
                  if isinstance(value, (str, int)) and
                  _PERIOD_RE.fullmatch(str(value).strip()) is not None}
        if len(tokens) >= 2:
            axes.append((index, tokens))
    return axes


def _period_label(calendar_rows: Sequence[tuple[int, Mapping[int, str]]],
                  row_index: int, column_index: int) -> str | None:
    for header_row, tokens in reversed(calendar_rows):
        if header_row < row_index and column_index in tokens:
            return tokens[column_index]
    return None


def _period_status(period: str | None) -> str:
    # Only an explicit estimate suffix is authoritative. An older date may be
    # an actual, consensus, or historical forecast, so it remains unknown.
    return "forecast" if period and period.endswith("E") else "unknown"


def workbook_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).expanduser().resolve().read_bytes()).hexdigest()


# -- the record ---------------------------------------------------------


_ASSUMPTION_FIELDS = frozenset({
    "ref", "sheet", "cell", "label", "value", "formula", "unit", "unit_basis",
    "kind", "as_of",
})
_ASSUMPTION_FIELDS_V2 = _ASSUMPTION_FIELDS | frozenset({
    "cell_role", "number_format", "font_color", "period", "period_status",
    "period_basis", "source_type",
})
CELL_ROLES = frozenset({"external_formula", "cross_sheet_formula", "formula", "hardcoded_input"})
PERIOD_STATUSES = frozenset({"forecast", "unknown"})
SOURCE_TYPES = frozenset({"prior_human_formula", "prior_human_hardcode"})

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "model_ref", "version",
    "prior_version_ref", "company_ref", "source_document_ref", "as_of",
    "workbook_sha256", "sheets", "assumptions", "assumption_count",
    "note", "actor_ref", "content_hash",
})
_RECORD_FIELDS_V2 = _RECORD_FIELDS | frozenset({"import_metadata"})

#: Fields that say when and who rather than what, so a re-import of an
#: unchanged workbook is a duplicate instead of a second version.
_BODY_EXCLUDED = frozenset({
    "schema_version", "id", "created_at", "version", "prior_version_ref",
    "actor_ref", "content_hash",
})


def assumption_ref(model_ref: str, sheet: str, cell: str) -> str:
    """One cell's identity inside one model: the sheet and the address.

    Not the label: two rows can carry the same label, and a label is the part
    of a spreadsheet most likely to be edited.
    """

    return f"prior-assumption:{content_hash({'model': model_ref, 'sheet': sheet, 'cell': cell})[:32]}"


def company_slug(company_ref: str) -> str:
    """A stable slug for a company ref, hashing the whole ref.

    The whole ref and not its last segment: live holds
    ``company:sec-cik:001688568`` beside ``company:sec-cik:0001467373``, and a
    slug that reads only the tail collides on the zero padding.
    """

    return content_hash({"company_ref": _text(company_ref, "company_ref")})[:24]


def model_ref_for(company_ref: str, source_document_ref: str) -> str:
    """One chain per company per source workbook."""

    return (
        "prior-model:"
        + content_hash({
            "company": _text(company_ref, "company_ref"),
            "document": _text(source_document_ref, "source_document_ref"),
        })[:32]
    )


def normalize_assumption(raw: Any, *, model_ref: str, as_of: str, index: int,
                         extended: bool = False) -> dict[str, Any]:
    name = f"assumptions[{index}]"
    if not isinstance(raw, Mapping):
        raise PriorModelError(f"{name} must be an object")
    allowed = _ASSUMPTION_FIELDS_V2 if extended else _ASSUMPTION_FIELDS
    unknown = set(raw) - allowed
    if unknown:
        raise PriorModelError(f"{name} has unknown fields {sorted(unknown)}")
    sheet = _text(raw.get("sheet"), f"{name}.sheet", maximum=MAX_LABEL_CHARS)
    cell = _text(raw.get("cell"), f"{name}.cell", maximum=16).upper()
    if _CELL_RE.fullmatch(cell) is None:
        raise PriorModelError(f"{name}.cell must be an A1-style address")
    value = raw.get("value")
    formula = _optional_text(raw.get("formula"), f"{name}.formula", maximum=MAX_FORMULA_CHARS)
    if value is None and not formula:
        raise PriorModelError(f"{name} has neither a value nor a formula")
    unit = raw.get("unit")
    if unit is not None and unit not in UNITS:
        raise PriorModelError(f"{name}.unit is not one of {list(UNITS)}")
    unit_basis = raw.get("unit_basis", "unknown")
    if unit_basis not in UNIT_BASES:
        raise PriorModelError(f"{name}.unit_basis is not one of {list(UNIT_BASES)}")
    if unit is None and unit_basis != "unknown":
        raise PriorModelError(f"{name} has no unit but claims a basis for one")
    kind = raw.get("kind", ASSUMPTION_KIND)
    if kind != ASSUMPTION_KIND:
        raise PriorModelError(
            f"{name}.kind must be {ASSUMPTION_KIND!r}; a prior workbook holds "
            "nothing else, and an imported cell is never an estimate or an actual"
        )
    result = {
        "ref": assumption_ref(model_ref, sheet, cell),
        "sheet": sheet,
        "cell": cell,
        "label": _optional_text(raw.get("label"), f"{name}.label", maximum=MAX_LABEL_CHARS),
        "value": None if value is None else decimal_text(value),
        # Verbatim. Never parsed, never evaluated, never rewritten.
        "formula": formula,
        "unit": unit,
        "unit_basis": unit_basis,
        "kind": ASSUMPTION_KIND,
        "as_of": as_of,
    }
    if extended:
        for field in ("cell_role", "number_format", "font_color", "period",
                      "period_status", "period_basis", "source_type"):
            value = raw.get(field)
            if value is not None and not isinstance(value, str):
                raise PriorModelError(f"{name}.{field} must be text or null")
            result[field] = value
        if result["cell_role"] not in CELL_ROLES:
            raise PriorModelError(f"{name}.cell_role is not recognized")
        if result["period_status"] not in PERIOD_STATUSES:
            raise PriorModelError(f"{name}.period_status is not recognized")
        if result["period_basis"] not in {None, "calendar_axis"}:
            raise PriorModelError(f"{name}.period_basis is not recognized")
        if result["source_type"] not in SOURCE_TYPES:
            raise PriorModelError(f"{name}.source_type is not recognized")
    return result


def build_record(
    *,
    company_ref: str,
    source_document_ref: str,
    as_of: str,
    workbook_sha256: str,
    assumptions: Sequence[Mapping[str, Any]],
    import_metadata: Mapping[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """The un-versioned body of one PriorModelVersion."""

    company_ref = _text(company_ref, "company_ref")
    source_document_ref = _text(source_document_ref, "source_document_ref")
    as_of = _day(as_of, "as_of")
    if not isinstance(workbook_sha256, str) or _HASH_RE.fullmatch(workbook_sha256) is None:
        raise PriorModelError("workbook_sha256 must be SHA-256 hex")
    model_ref = model_ref_for(company_ref, source_document_ref)
    extended = import_metadata is not None
    rows = [
        normalize_assumption(item, model_ref=model_ref, as_of=as_of, index=index,
                             extended=extended)
        for index, item in enumerate(assumptions)
    ]
    if not rows:
        raise PriorModelError(
            "a prior model with no assumption cells is not a model; the "
            "workbook read as empty and that is a refusal, not a version"
        )
    seen = {row["ref"] for row in rows}
    if len(seen) != len(rows):
        raise PriorModelError("two assumptions name the same sheet and cell")
    sheets = sorted({row["sheet"] for row in rows})
    result = {
        "model_ref": model_ref,
        "company_ref": company_ref,
        "source_document_ref": source_document_ref,
        "as_of": as_of,
        "workbook_sha256": workbook_sha256,
        "sheets": sheets,
        "assumptions": rows,
        "assumption_count": len(rows),
        "note": _optional_text(note, "note", maximum=2_000),
    }
    if import_metadata is not None:
        metadata = dict(import_metadata)
        if metadata.get("schema_version") != "prior-workbook-import-0.1":
            raise PriorModelError("unsupported workbook import metadata schema")
        if not isinstance(metadata.get("complete"), bool):
            raise PriorModelError("workbook import metadata complete must be boolean")
        truncations = metadata.get("truncations")
        if not isinstance(truncations, list) or metadata["complete"] == bool(truncations):
            raise PriorModelError("workbook import completeness disagrees with truncations")
        sheet_rows = metadata.get("sheets")
        if not isinstance(sheet_rows, list) or len({row.get("name") for row in sheet_rows
                                                   if isinstance(row, Mapping)}) != len(sheet_rows):
            raise PriorModelError("workbook import sheets must have unique names")
        if {row["sheet"] for row in rows} - {row.get("name") for row in sheet_rows}:
            raise PriorModelError("workbook import metadata omits an imported sheet")
        formula_identity = [{"sheet": row["sheet"], "cell": row["cell"],
                             "formula": row["formula"]} for row in rows if row["formula"]]
        if metadata.get("formula_map_hash") != content_hash(formula_identity):
            raise PriorModelError("workbook import formula map hash disagrees with cells")
        result["import_metadata"] = metadata
    return result


def validate_record(value: Mapping[str, Any]) -> dict[str, Any]:
    version_name = value.get("schema_version") if isinstance(value, Mapping) else None
    fields = _RECORD_FIELDS if version_name == LEGACY_SCHEMA_VERSION else _RECORD_FIELDS_V2
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PriorModelError(
            "PriorModelVersion has an invalid closed shape; "
            f"missing={sorted(fields - set(value))}, "
            f"unknown={sorted(set(value) - fields)}"
        )
    if value["schema_version"] not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise PriorModelError("unsupported PriorModelVersion schema_version")
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PriorModelError("PriorModelVersion version must be a positive integer")
    model_ref = _text(value["model_ref"], "model_ref")
    as_of = _day(value["as_of"], "as_of")
    rows = value["assumptions"]
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise PriorModelError("PriorModelVersion assumptions must be a non-empty array")
    for index, item in enumerate(rows):
        normalize_assumption(item, model_ref=model_ref, as_of=as_of, index=index,
                             extended=value["schema_version"] == SCHEMA_VERSION)
    if value["assumption_count"] != len(rows):
        raise PriorModelError("PriorModelVersion assumption_count disagrees with the rows")
    return dict(value)


def body_hash(record: Mapping[str, Any]) -> str:
    return content_hash({
        key: item for key, item in record.items() if key not in _BODY_EXCLUDED
    })


# -- the authority ------------------------------------------------------


class PriorModelAuthority:
    """Append-only PriorModelVersion chains over one Core.

    Read-only from every other layer's point of view: it publishes, it reads
    back, and it exposes bands. It has no ``revise`` and needs none -- a prior
    model is not revised, it is superseded by a later workbook, which is a new
    version of the same chain with its own ``as_of``.
    """

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("PriorModelAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_prior_model_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("PriorModelAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads ----------------------------------------------------------

    def model(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE version_id=?",
            (_text(version_id, "version_id"),),
        ).fetchone()
        if row is None:
            raise PriorModelConflict(f"no PriorModelVersion {version_id}")
        return _decode(row)

    def versions(self, model_ref: str) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE model_ref=? ORDER BY version_number",
            (_text(model_ref, "model_ref"),),
        )
        return [_decode(row) for row in cursor.fetchall()]

    def current(self, model_ref: str) -> dict[str, Any] | None:
        chain = self.versions(model_ref)
        return chain[-1] if chain else None

    def company_models(self, company_ref: str) -> list[dict[str, Any]]:
        """The head of every prior-model chain this company has, oldest first."""

        refs = [
            row["model_ref"]
            for row in self.connection.execute(
                f"SELECT DISTINCT model_ref FROM {TABLE} WHERE company_ref=? "
                "ORDER BY model_ref",
                (_text(company_ref, "company_ref"),),
            ).fetchall()
        ]
        heads = [self.current(ref) for ref in refs]
        found = [head for head in heads if head is not None]
        return sorted(found, key=lambda item: (item["as_of"], item["model_ref"]))

    # -- write ----------------------------------------------------------

    def publish(
        self,
        *,
        company_ref: str,
        source_document_ref: str,
        as_of: str,
        workbook_sha256: str,
        assumptions: Sequence[Mapping[str, Any]],
        actor_ref: str,
        note: str = "",
        import_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store one workbook as the next version of its chain.

        A re-import of an unchanged workbook is a ``duplicate`` and writes
        nothing: the body hash covers the cells, the source and the date, and
        a version that cannot name what changed is a rewrite of an unchanged
        world wearing a version number (ADR-0008).
        """

        actor_ref = _text(actor_ref, "actor_ref")
        if not actor_ref.startswith(("human:", "automation:")):
            raise PriorModelError("actor_ref must use a principal namespace")
        if import_metadata is None and isinstance(assumptions, WorkbookReadArtifact):
            import_metadata = assumptions.metadata
        body = build_record(
            company_ref=company_ref,
            source_document_ref=source_document_ref,
            as_of=as_of,
            workbook_sha256=workbook_sha256,
            assumptions=assumptions,
            note=note,
            import_metadata=import_metadata,
        )
        digest = body_hash(body)
        with self._transaction() as cur:
            head = cur.execute(
                f"SELECT version_id, version_number, body_hash, content_hash "
                f"FROM {TABLE} WHERE model_ref=? ORDER BY version_number DESC LIMIT 1",
                (body["model_ref"],),
            ).fetchone()
            if head is not None and head["body_hash"] == digest:
                return {**self.model(head["version_id"]), "status": "duplicate"}
            version = 1 if head is None else int(head["version_number"]) + 1
            record = {
                "schema_version": (SCHEMA_VERSION if import_metadata is not None
                                   else LEGACY_SCHEMA_VERSION),
                "id": (
                    "prior-model-version:"
                    + content_hash({"ref": body["model_ref"], "version": version})[:32]
                ),
                "created_at": _now(),
                "version": version,
                "prior_version_ref": None if head is None else head["version_id"],
                "actor_ref": actor_ref,
                **body,
            }
            record["content_hash"] = content_hash(record)
            validate_record(record)
            cur.execute(
                f"INSERT INTO {TABLE} (version_id, model_ref, version_number, "
                "prior_version_id, company_ref, source_document_ref, as_of, "
                "workbook_sha256, assumption_count, body_hash, record_json, "
                "content_hash, actor_ref, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record["model_ref"], version,
                    record["prior_version_ref"], record["company_ref"],
                    record["source_document_ref"], record["as_of"],
                    record["workbook_sha256"], record["assumption_count"],
                    digest, canonical_json(record), record["content_hash"],
                    actor_ref, record["created_at"],
                ),
            )
        # Read back through the same path a reader would take, so a record
        # that cannot be read is a failed write rather than a stored surprise.
        stored = self.model(record["id"])
        if stored["content_hash"] != record["content_hash"]:
            raise PriorModelConflict("prior model did not read back as written")
        return {**stored, "status": "fresh"}


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    import json

    wire = json.loads(row["record_json"])
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise PriorModelConflict("PriorModelVersion record_json is not canonical")
    body = {key: item for key, item in wire.items() if key != "content_hash"}
    if wire["content_hash"] != content_hash(body) or wire["content_hash"] != row["content_hash"]:
        raise PriorModelConflict("PriorModelVersion content hash drifted")
    validate_record(wire)
    for column, key in (
        ("model_ref", "model_ref"), ("company_ref", "company_ref"),
        ("as_of", "as_of"), ("source_document_ref", "source_document_ref"),
    ):
        if row[column] != wire[key]:
            raise PriorModelConflict("prior model authority drifted")
    return wire


# -- the read M2 / M3 actually want --------------------------------------


def prior_assumption_bands(
    connection: sqlite3.Connection,
    company_ref: str,
    driver_label: str,
    *,
    unit: str | None = None,
) -> dict[str, Any]:
    """How this team has assumed this driver before, as a range with its rows.

    Matching is by name, through a short ladder that says which rung it stood
    on -- ``exact``, ``contains`` or nothing. A prior model's labels
    ("Consulting revenue growth") will not equal a filer's XBRL labels
    ("Revenues"), so an empty band is the common case and is returned as an
    empty band with a reason rather than as a silent zero.

    ``low``/``high`` are decimal *strings*, like everything else here. There is
    no mean and no median: a band over four hand-typed assumptions from three
    different years is not a distribution, and giving it a central tendency
    would dress up four numbers as a statistic.

    Units are the other refusal. A band mixing a percent cell and a currency
    cell is unusable, so when the rows disagree the band is empty and says so;
    pass ``unit=`` to ask for one.
    """

    wanted = normalize_label(driver_label)
    if not wanted:
        raise PriorModelError("driver_label must be non-empty text")
    rows: list[dict[str, Any]] = []
    cursor = connection.execute(
        f"SELECT record_json, version_number, model_ref FROM {TABLE} "
        "WHERE company_ref=? ORDER BY model_ref, version_number",
        (_text(company_ref, "company_ref"),),
    )
    import json

    heads: dict[str, Any] = {}
    for row in cursor.fetchall():
        heads[row["model_ref"]] = json.loads(row["record_json"])
    for record in heads.values():
        for item in record["assumptions"]:
            if item["value"] is None:
                continue
            folded = normalize_label(item["label"])
            if not folded:
                continue
            if folded == wanted:
                matched_on = "exact"
            elif wanted in folded or folded in wanted:
                matched_on = "contains"
            else:
                continue
            if unit is not None and item["unit"] != unit:
                continue
            rows.append({
                "model_ref": record["model_ref"],
                "version_ref": record["id"],
                "as_of": item["as_of"],
                "sheet": item["sheet"],
                "cell": item["cell"],
                "label": item["label"],
                "value": item["value"],
                "formula": item["formula"],
                "unit": item["unit"],
                "unit_basis": item["unit_basis"],
                "kind": item["kind"],
                "matched_on": matched_on,
            })
    if not rows:
        return {
            "company_ref": company_ref, "driver_label": driver_label,
            "matched_on": None, "unit": unit, "count": 0, "rows": [],
            "low": None, "high": None, "as_of_from": None, "as_of_to": None,
            "reason": "no prior assumption in this company's models carries that label",
        }
    units = {row["unit"] for row in rows}
    if len(units) > 1:
        return {
            "company_ref": company_ref, "driver_label": driver_label,
            "matched_on": None, "unit": None, "count": len(rows), "rows": rows,
            "low": None, "high": None, "as_of_from": None, "as_of_to": None,
            "reason": (
                "the matching cells disagree about their unit "
                f"({sorted(str(item) for item in units)}); a band across units "
                "is not a band -- ask for one with unit="
            ),
        }
    values = sorted(Decimal(row["value"]) for row in rows)
    rows.sort(key=lambda item: (item["as_of"], item["model_ref"], item["cell"]))
    return {
        "company_ref": company_ref,
        "driver_label": driver_label,
        "matched_on": "exact" if all(row["matched_on"] == "exact" for row in rows)
                      else "contains",
        "unit": rows[0]["unit"],
        "count": len(rows),
        "rows": rows,
        "low": format(values[0], "f"),
        "high": format(values[-1], "f"),
        "as_of_from": rows[0]["as_of"],
        "as_of_to": rows[-1]["as_of"],
        "reason": None,
    }


__all__ = [
    "ASSUMPTION_KIND",
    "MAX_ASSUMPTIONS",
    "SCHEMA_VERSION",
    "UNITS",
    "UNIT_BASES",
    "PriorModelAuthority",
    "PriorModelConflict",
    "PriorModelError",
    "WorkbookReadArtifact",
    "WorkbookReadBudget",
    "assumption_ref",
    "body_hash",
    "build_record",
    "company_slug",
    "decimal_text",
    "guess_unit",
    "model_ref_for",
    "normalize_assumption",
    "normalize_label",
    "prior_assumption_bands",
    "read_workbook",
    "validate_record",
    "workbook_digest",
]
