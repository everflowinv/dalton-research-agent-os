"""The fund Excel template registry and the per-company template choice.

Vision-gap #4 (2026-09-30 report, section 1.1).  Recovered design: the
2026-09-13 candidate ``8e66ee01`` (``continuous/0913-fund-style-registry``,
archived in ``dalton-archive-20260915``) kept a file registry of redacted
style contracts that a person published and then, separately, activated; the
exporter took one ``style_contract``.  Rewritten here against today's Core:

**One authority, two append-only books** (``fund_xlsx_template_registry_schema.sql``).

- ``fund_xlsx_template_versions``: every registered template, versioned per
  slug.  A version carries the redacted *style contract* (presentation only,
  produced by ``fund_xlsx_template_import`` from the owner's workbook: no cell
  value, formula, defined name or comment of the source survives), the
  *mapping* -- which sheet each model role is written to, the unit headers,
  which of the model's metric bases the valuation sheet summarises and in
  which order, and the cell each valuation-snapshot field is written to -- and
  where it came from (source workbook hash, file name, sheet binding).  The
  built-in AMZN-source template is version 1 of ``amzn-source``; it is defined
  in code and seeded into the book the first time the writer opens the
  registry, so every environment starts from the identical row.
- ``fund_xlsx_template_selections``: the owner's choices.  One receipt per
  choice of the template a company, an industry classification, or the whole
  workspace exports with (or the withdrawal of such a choice).  The choice in
  force is folded from the receipts, newest per scope wins.

Registering a template never changes what anything exports with: like the
candidate's "published_inactive", a new version is inert until a selection
names it.  Both doors are ``human:`` only (the writer binds the actor from the
authenticated principal and this module refuses anything else again).

**Select** (:func:`resolve_template`): an explicit template for this one
export, else the owner's choice for the company, else the owner's default for
the company's industry classification (read from its latest dossier, the
place the classification is versioned), else the owner's workspace default,
else the built-in.  Read-only and tolerant of a Core that has never seen the
registry (a new workspace, or a cockpit reading a Core the writer has not yet
opened the registry on): such a Core resolves to the built-in, so a new
workspace exports with the default template without any patch.

Cost-side templates (W5, ``supply_and_cost`` slots per classification) are a
forecast-driver concern and already live in ``driver_template`` on main; they
are not workbook presentation and are deliberately not stored here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fund_xlsx_template import (
    STYLE_RESOURCE_SHA256,
    load_fund_xlsx_template_style,
    style_contract_hash,
    validate_fund_xlsx_template_style,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "fund-xlsx-template-version-0.1"
SELECTION_SCHEMA_VERSION = "fund-xlsx-template-selection-0.1"
MAPPING_SCHEMA_VERSION = "fund-xlsx-template-mapping-0.1"
_SCHEMA_PATH = Path(__file__).with_name("fund_xlsx_template_registry_schema.sql")

BUILTIN_SLUG = "amzn-source"
BUILTIN_VERSION_REF = f"fund-xlsx-template-version:{BUILTIN_SLUG}:1"
BUILTIN_ACTOR = "system:builtin"
BUILTIN_CREATED_AT = "2026-09-11T00:00:00+00:00"
BUILTIN_STYLE_REF = "fund-xlsx-template-style:amzn-source:0.1"
DEFAULT_SCOPE_REF = "default"
SCOPE_KINDS: tuple[str, ...] = ("company", "industry", "default")
BASES: tuple[str, ...] = ("explicit", "company", "industry", "default", "builtin")

#: The dossier's classification vocabulary minus ``insufficient_evidence``
#: (company_dossier.INDUSTRY_CLASSIFICATIONS), with the owner's words.
INDUSTRY_LABELS: Mapping[str, str] = {
    "commodity_cycle": "商品周期型",
    "capital_cycle": "资本周期型",
    "contract_compounder": "合同型复利",
    "structural_growth": "结构性成长",
    "turnaround": "困境反转",
}
BASIS_LABELS: Mapping[str, str] = {
    "explicit": "本次导出临时指定",
    "company": "你为这家公司指定",
    "industry": "按行业分类的默认模板",
    "default": "你设定的全局默认模板",
    "builtin": "系统内置默认模板",
}

#: The valuation sheet's annual blocks: one per metric basis of the model.
#: ``title`` takes the model currency; ``fallback_ref`` is the result ref used
#: when no result carries the role.
VALUATION_BLOCKS: Mapping[str, tuple[str, str, str]] = {
    "diluted_eps": ("GAAP EPS ({currency})", "result:diluted_eps", "per_share"),
    "revenue": ("Total Revenue ({currency} MM)", "result:revenue", "amount"),
    "operating_income": ("GAAP Operating Profit ({currency} MM)",
                         "result:operating_income", "amount"),
    "net_income": ("GAAP Net Income ({currency} MM)", "result:net_income", "amount"),
}
#: The valuation snapshot's summary fields.  The mapping names the label cell;
#: the value is written one column to its right.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "price", "shares_outstanding", "market_cap", "cash", "debt", "net_cash",
    "enterprise_value",
)
DEFAULT_MAPPING: Mapping[str, Any] = {
    "schema_version": MAPPING_SCHEMA_VERSION,
    "sheet_names": {"valuation": "Valuation", "financials": "Financials", "driver": "Driver"},
    "unit_labels": {"financials": "({currency} MM)", "driver": "({currency} mm)",
                    "driver_usd": "(US$mm)"},
    "valuation_annual_blocks": ["diluted_eps", "revenue", "operating_income", "net_income"],
    "valuation_snapshot_cells": {
        "price": "A4", "shares_outstanding": "A5", "market_cap": "D2", "cash": "D3",
        "debt": "D4", "net_cash": "D5", "enterprise_value": "D6",
    },
}
RESERVED_SHEET_NAMES = frozenset({"sources", "formula map"})
MAX_REASON_CHARS = 600
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
_SNAPSHOT_CELL = re.compile(r"([A-H])([2-7])")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\']")
_HUMAN = re.compile(r"human:[A-Za-z0-9][A-Za-z0-9._/@:-]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: The exporter writes the ticker/company and the date here.
_IDENTITY_CELLS = frozenset({"A2", "B2", "A3", "B3"})


class FundXlsxTemplateRegistryError(ValueError):
    """A registry request does not satisfy the closed contract."""


class FundXlsxTemplateRegistryConflict(RuntimeError):
    """A registry request conflicts with what is already recorded."""


def _need(value: Any, reason: str) -> None:
    if not value:
        raise FundXlsxTemplateRegistryError(reason)


def _text(value: Any, name: str, *, maximum: int = 240) -> str:
    _need(isinstance(value, str) and value.strip(), f"{name} must be non-empty text")
    value = value.strip()
    _need(len(value) <= maximum, f"{name} must be at most {maximum} characters")
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def _sheet_name(value: Any, role: str) -> str:
    _need(isinstance(value, str) and value.strip() == value and value
          and len(value) <= 31 and _BAD_SHEET_CHARS.search(value) is None
          and value.lower() not in RESERVED_SHEET_NAMES,
          f"sheet name for {role} must be 1-31 characters without []:*?/\\' "
          "and not an audit sheet name")
    return value


def _unit_label(value: Any, name: str) -> str:
    _need(isinstance(value, str) and value.strip() == value and value and len(value) <= 40,
          f"unit label {name} must be 1-40 characters")
    _need(re.sub(r"\{currency\}", "", value).count("{") == 0
          and re.sub(r"\{currency\}", "", value).count("}") == 0,
          f"unit label {name} may only use the {{currency}} placeholder")
    return value


def validate_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Close a mapping; omitted keys take the built-in template's mapping."""

    source = dict(value or {})
    _need(set(source) <= set(DEFAULT_MAPPING), "mapping names an unknown key")
    _need(source.get("schema_version", MAPPING_SCHEMA_VERSION) == MAPPING_SCHEMA_VERSION,
          "mapping schema version is unsupported")
    sheets = dict(DEFAULT_MAPPING["sheet_names"])
    given = source.get("sheet_names") or {}
    _need(isinstance(given, Mapping) and set(given) <= set(sheets),
          "sheet names bind valuation, financials and driver only")
    sheets.update(given)
    sheets = {role: _sheet_name(name, role) for role, name in sheets.items()}
    _need(len({name.lower() for name in sheets.values()}) == 3,
          "the three model sheets need distinct names")
    units = dict(DEFAULT_MAPPING["unit_labels"])
    given = source.get("unit_labels") or {}
    _need(isinstance(given, Mapping) and set(given) <= set(units),
          "unit labels bind financials, driver and driver_usd only")
    units.update(given)
    units = {key: _unit_label(label, key) for key, label in units.items()}
    blocks = source.get("valuation_annual_blocks", DEFAULT_MAPPING["valuation_annual_blocks"])
    _need(isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes))
          and blocks and len(blocks) == len(set(blocks))
          and all(block in VALUATION_BLOCKS for block in blocks),
          "valuation annual blocks must be distinct metric bases from "
          + ", ".join(VALUATION_BLOCKS))
    cells = dict(DEFAULT_MAPPING["valuation_snapshot_cells"])
    given = source.get("valuation_snapshot_cells") or {}
    _need(isinstance(given, Mapping) and set(given) <= set(cells),
          "valuation snapshot cells name an unknown field")
    cells.update(given)
    labels: set[str] = set()
    values: set[str] = set()
    for field, cell in cells.items():
        match = _SNAPSHOT_CELL.fullmatch(cell) if isinstance(cell, str) else None
        _need(match is not None,
              f"valuation snapshot cell for {field} must be within A2:H7 (above the annual blocks)")
        labels.add(cell)
        values.add(f"{chr(ord(match.group(1)) + 1)}{match.group(2)}")
    _need(len(labels) == len(cells) and len(values) == len(cells) and not labels & values,
          "valuation snapshot cells overlap")
    _need(not (labels | values) & _IDENTITY_CELLS,
          "valuation snapshot cells overlap the ticker and date cells (A2:B3)")
    return {"schema_version": MAPPING_SCHEMA_VERSION, "sheet_names": sheets,
            "unit_labels": units, "valuation_annual_blocks": list(blocks),
            "valuation_snapshot_cells": cells}


def _industries(value: Any) -> list[str]:
    values = list(value or [])
    _need(all(item in INDUSTRY_LABELS for item in values) and len(values) == len(set(values)),
          "industries must be distinct classifications from " + ", ".join(INDUSTRY_LABELS))
    return sorted(values)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def builtin_version() -> dict[str, Any]:
    """Version 1 of ``amzn-source``: the packaged, hash-pinned contract.

    ``style_contract`` is ``None``: the exporter loads the packaged resource
    and binds it by its byte hash, exactly as before the registry existed, so
    the built-in export is unchanged byte for byte in every cell it writes.
    """

    style = load_fund_xlsx_template_style()
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": BUILTIN_VERSION_REF,
        "template_slug": BUILTIN_SLUG,
        "version": 1,
        "prior_version_ref": None,
        "display_name": "参考基金模板（AMZN 样本）",
        "description": ("owner 2026-09-10 提供的 AMZN 基金模型样本中提取的版式："
                        "Valuation / Financials / Driver 三张表、Arial 8 号字、"
                        "蓝色输入、绿色跨表公式、黄色假设框。"),
        "industries": [],
        "origin": "builtin",
        "source": {"kind": "builtin", "resource": "fund_xlsx_template_style.json",
                   "resource_sha256": STYLE_RESOURCE_SHA256,
                   "workbook_sha256": style["source_proof"]["workbook_sha256"],
                   "filename": None, "sheet_bindings": None, "candidate_hash": None},
        "style_ref": style["template_ref"],
        "style_hash": STYLE_RESOURCE_SHA256,
        "style_contract": None,
        "mapping": validate_mapping(None),
        "actor_ref": BUILTIN_ACTOR,
        "reason": "built-in default template, seeded by code on first use",
        "created_at": BUILTIN_CREATED_AT,
    }
    record["content_hash"] = content_hash(record)
    return record


def _verify_version(record: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(record)
    asserted = wire.pop("content_hash", None)
    if asserted != content_hash(wire):
        raise FundXlsxTemplateRegistryConflict("fund template version record drifted")
    if wire["style_contract"] is None:
        if wire["id"] != BUILTIN_VERSION_REF or wire["style_hash"] != STYLE_RESOURCE_SHA256:
            raise FundXlsxTemplateRegistryConflict("only the built-in template may omit its style")
    elif style_contract_hash(wire["style_contract"]) != wire["style_hash"]:
        raise FundXlsxTemplateRegistryConflict("fund template style bytes drifted")
    validate_mapping(wire["mapping"])
    return {**wire, "content_hash": asserted}


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _decode(row: Any) -> dict[str, Any]:
    raw = row["record_json"] if isinstance(row, sqlite3.Row) else row[0]
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise FundXlsxTemplateRegistryConflict("fund template record is not an object")
    return value


def template_versions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every version, the built-in first; read-only and table-tolerant."""

    builtin = builtin_version()
    out: dict[str, dict[str, Any]] = {builtin["id"]: builtin}
    if _table_exists(connection, "fund_xlsx_template_versions"):
        rows = connection.execute(
            "SELECT record_json FROM fund_xlsx_template_versions "
            "ORDER BY template_slug, version_number").fetchall()
        for row in rows:
            record = _verify_version(_decode(row))
            if record["id"] == BUILTIN_VERSION_REF and record != builtin:
                raise FundXlsxTemplateRegistryConflict(
                    "the seeded built-in template differs from the code's")
            out[record["id"]] = record
    return list(out.values())


def template_version(connection: sqlite3.Connection, version_ref: str) -> dict[str, Any]:
    for record in template_versions(connection):
        if record["id"] == version_ref:
            return record
    raise FundXlsxTemplateRegistryError("fund template version is not registered")


def selection_receipts(connection: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    """Newest first.  A Core without the registry answers an empty list."""

    if not _table_exists(connection, "fund_xlsx_template_selections"):
        return []
    rows = connection.execute(
        "SELECT record_json FROM fund_xlsx_template_selections ORDER BY sequence DESC LIMIT ?",
        (max(1, min(int(limit), 1000)),)).fetchall()
    return [_decode(row) for row in rows]


def selection_state(connection: sqlite3.Connection) -> dict[str, Any]:
    """The owner's choices in force, folded from the receipts."""

    state: dict[str, Any] = {"default": None, "industries": {}, "companies": {},
                             "receipt_count": 0}
    if not _table_exists(connection, "fund_xlsx_template_selections"):
        return state
    rows = connection.execute(
        "SELECT record_json FROM fund_xlsx_template_selections ORDER BY sequence ASC").fetchall()
    state["receipt_count"] = len(rows)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        wire = _decode(row)
        latest[(wire["scope_kind"], wire["scope_ref"])] = wire
    for (kind, ref), wire in sorted(latest.items()):
        if wire["action"] != "select":
            continue
        if kind == "default":
            state["default"] = wire
        elif kind == "industry":
            state["industries"][ref] = wire
        else:
            state["companies"][ref] = wire
    return state


def company_classification(connection: sqlite3.Connection, company_ref: str) -> str | None:
    """The company's industry classification from its latest dossier, or None.

    Only the classification word is read (it chooses a presentation default,
    nothing numeric); ``insufficient_evidence`` and an undrafted block read as
    no classification.
    """

    if not _table_exists(connection, "company_dossier_versions"):
        return None
    try:
        row = connection.execute(
            "SELECT record_json FROM company_dossier_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        record = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    word = ((record.get("industry_classification") or {}).get("classification")
            if isinstance(record, dict) else None)
    return word if word in INDUSTRY_LABELS else None


_UNSET: Any = object()


def resolve_template(
    connection: sqlite3.Connection, company_ref: str, *,
    classification: Any = _UNSET, explicit_version_ref: str | None = None,
) -> dict[str, Any]:
    """Choose the template this company exports with, and say why.

    Explicit (this export only) > the owner's company choice > the owner's
    industry default for the company's classification > the owner's workspace
    default > the built-in.
    """

    versions = {record["id"]: record for record in template_versions(connection)}
    if classification is _UNSET:
        classification = company_classification(connection, company_ref)
    state = selection_state(connection)

    def chosen(record: Mapping[str, Any] | None, basis: str, scope_ref: str | None,
               receipt: Mapping[str, Any] | None) -> dict[str, Any]:
        template = versions.get(str((record or {}).get("id")))
        if template is None:
            raise FundXlsxTemplateRegistryConflict("selected fund template version is missing")
        return {"template": template, "basis": basis, "basis_label": BASIS_LABELS[basis],
                "scope_ref": scope_ref, "classification": classification,
                "receipt_ref": None if receipt is None else receipt.get("id")}

    if explicit_version_ref is not None:
        if explicit_version_ref not in versions:
            raise FundXlsxTemplateRegistryError("fund template version is not registered")
        return chosen(versions[explicit_version_ref], "explicit", None, None)
    receipt = state["companies"].get(company_ref)
    if receipt is not None:
        return chosen({"id": receipt["template_version_ref"]}, "company", company_ref, receipt)
    if classification is not None and classification in state["industries"]:
        receipt = state["industries"][classification]
        return chosen({"id": receipt["template_version_ref"]}, "industry", classification, receipt)
    if state["default"] is not None:
        receipt = state["default"]
        return chosen({"id": receipt["template_version_ref"]}, "default", DEFAULT_SCOPE_REF, receipt)
    return chosen({"id": BUILTIN_VERSION_REF}, "builtin", None, None)


def export_binding(resolved: Mapping[str, Any]) -> dict[str, Any]:
    """What an export needs from a resolution, and what its manifest records."""

    template = resolved["template"]
    return {
        "version_ref": template["id"], "version_hash": template["content_hash"],
        "display_name": template["display_name"], "style_ref": template["style_ref"],
        "style_hash": template["style_hash"], "style_contract": template["style_contract"],
        "mapping": template["mapping"], "basis": resolved["basis"],
        "scope_ref": resolved.get("scope_ref"), "receipt_ref": resolved.get("receipt_ref"),
    }


# ---------------------------------------------------------------------------
# candidate
# ---------------------------------------------------------------------------


def _import_canonical(value: Any) -> str:
    """The importer's own hash (``fund_xlsx_template_import._canonical``)."""

    import hashlib

    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(wire.encode()).hexdigest()


def validate_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The importer's inert, redacted candidate, checked closed (as in 8e66ee01)."""

    from .fund_xlsx_template_import import CANDIDATE_SCHEMA_VERSION

    _need(isinstance(candidate, Mapping), "template candidate must be an object")
    wire = json.loads(json.dumps(dict(candidate)))
    asserted = wire.get("content_hash")
    _need(set(wire) == {"schema_version", "status", "activation_authorized",
                        "template_slug", "source", "style_contract",
                        "redaction", "content_hash"}
          and wire.get("schema_version") == CANDIDATE_SCHEMA_VERSION
          and wire.get("status") == "candidate_pending_owner_review"
          and wire.get("activation_authorized") is False
          and asserted == _import_canonical(
              {key: value for key, value in wire.items() if key != "content_hash"}),
          "template candidate is invalid or not inert")
    _need(wire["redaction"] == {"cell_values": "omitted", "formulas": "omitted",
                                "defined_names": "omitted", "comments": "omitted",
                                "hyperlinks": "omitted", "external_links": "rejected"},
          "template candidate was not redacted")
    slug = wire.get("template_slug")
    _need(isinstance(slug, str) and _SLUG.fullmatch(slug) is not None,
          "template slug must be lowercase letters, digits and hyphens")
    style = validate_fund_xlsx_template_style(wire["style_contract"])
    _need(style["template_ref"] == f"fund-xlsx-template-style:{slug}:candidate-0.1",
          "candidate style identity differs from its slug")
    source = wire.get("source")
    _need(isinstance(source, Mapping)
          and isinstance(source.get("workbook_sha256"), str)
          and _HEX64.fullmatch(source["workbook_sha256"]) is not None
          and source["workbook_sha256"] == style["source_proof"]["workbook_sha256"],
          "candidate source binding differs")
    return wire


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


class FundXlsxTemplateRegistry:
    """The append-only template book and selection receipts.  Writes only
    through a DaltonStore; seeds the built-in template on construction."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("FundXlsxTemplateRegistry requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.seed_builtin()

    def seed_builtin(self) -> dict[str, Any]:
        record = builtin_version()
        row = self.connection.execute(
            "SELECT record_json FROM fund_xlsx_template_versions WHERE version_id=?",
            (BUILTIN_VERSION_REF,)).fetchone()
        if row is not None:
            if _decode(row) != record:
                raise FundXlsxTemplateRegistryConflict(
                    "the seeded built-in template differs from the code's")
            return record
        with self.store._transaction() as cur:
            if cur.execute("SELECT 1 FROM fund_xlsx_template_versions WHERE version_id=?",
                           (BUILTIN_VERSION_REF,)).fetchone() is None:
                self._insert_version(cur, record, idempotency_key=None)
        return record

    @staticmethod
    def _insert_version(cur: sqlite3.Cursor, record: Mapping[str, Any],
                        *, idempotency_key: str | None) -> None:
        cur.execute(
            "INSERT INTO fund_xlsx_template_versions(version_id,template_slug,version_number,"
            "prior_version_id,style_ref,style_hash,origin,actor_ref,idempotency_key,record_json,"
            "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["id"], record["template_slug"], record["version"],
             record["prior_version_ref"], record["style_ref"], record["style_hash"],
             record["origin"], record["actor_ref"], idempotency_key, canonical_json(record),
             record["content_hash"], record["created_at"]))

    def versions(self) -> list[dict[str, Any]]:
        return template_versions(self.connection)

    def state(self) -> dict[str, Any]:
        return selection_state(self.connection)

    def register(
        self, *, candidate: Mapping[str, Any], display_name: str, reason: str,
        actor_ref: str, industries: Sequence[str] = (),
        mapping: Mapping[str, Any] | None = None, description: str | None = None,
        source_filename: str | None = None,
        sheet_bindings: Mapping[str, str] | None = None,
        idempotency_key: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append the next version of the candidate's slug.  Inert until selected."""

        _need(isinstance(actor_ref, str) and _HUMAN.fullmatch(actor_ref) is not None,
              "only a person may register a fund template")
        wire = validate_candidate(candidate)
        slug = wire["template_slug"]
        display_name = _text(display_name, "display_name", maximum=80)
        reason = _text(reason, "reason", maximum=MAX_REASON_CHARS)
        description = (None if description is None or not str(description).strip()
                       else _text(description, "description", maximum=600))
        industries = _industries(industries)
        closed_mapping = validate_mapping(mapping)
        filename = None
        if source_filename is not None and str(source_filename).strip():
            filename = Path(str(source_filename)).name[:160]
        bindings = None
        if sheet_bindings is not None:
            _need(isinstance(sheet_bindings, Mapping)
                  and set(sheet_bindings) == {"valuation", "financials", "driver"}
                  and all(isinstance(v, str) and v and len(v) <= 31
                          for v in sheet_bindings.values()),
                  "sheet bindings must name the source's three model sheets")
            bindings = {key: sheet_bindings[key] for key in sorted(sheet_bindings)}
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key", maximum=200)
        request = {"candidate_hash": wire["content_hash"], "display_name": display_name,
                   "reason": reason, "description": description, "industries": industries,
                   "mapping": closed_mapping, "filename": filename, "bindings": bindings,
                   "actor_ref": actor_ref}
        moment = (now or _now()).astimezone(timezone.utc)
        with self.store._transaction() as cur:
            if idempotency_key is not None:
                row = cur.execute(
                    "SELECT record_json FROM fund_xlsx_template_versions WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if row is not None:
                    existing = _decode(row)
                    if existing.get("request_hash") != content_hash(request):
                        raise FundXlsxTemplateRegistryConflict(
                            "idempotency key was already used for a different template")
                    return {**existing, "status": "replayed"}
            prior = cur.execute(
                "SELECT version_id, version_number FROM fund_xlsx_template_versions "
                "WHERE template_slug=? ORDER BY version_number DESC LIMIT 1", (slug,)).fetchone()
            version = 1 if prior is None else int(prior[1]) + 1
            style = dict(wire["style_contract"])
            style["template_ref"] = f"fund-xlsx-template-style:{slug}:{version}"
            style = validate_fund_xlsx_template_style(style)
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": f"fund-xlsx-template-version:{slug}:{version}",
                "template_slug": slug, "version": version,
                "prior_version_ref": None if prior is None else prior[0],
                "display_name": display_name, "description": description,
                "industries": industries, "origin": "owner_upload",
                "source": {"kind": "owner_upload", "resource": None, "resource_sha256": None,
                           "workbook_sha256": wire["source"]["workbook_sha256"],
                           "filename": filename, "sheet_bindings": bindings,
                           "candidate_hash": wire["content_hash"]},
                "style_ref": style["template_ref"], "style_hash": style_contract_hash(style),
                "style_contract": style, "mapping": closed_mapping,
                "actor_ref": actor_ref, "reason": reason,
                "created_at": moment.isoformat(timespec="microseconds"),
                "request_hash": content_hash(request),
            }
            record["content_hash"] = content_hash(record)
            self._insert_version(cur, record, idempotency_key=idempotency_key)
        return {**record, "status": "registered"}

    def choose(
        self, *, scope_kind: str, scope_ref: str, reason: str, actor_ref: str,
        template_version_ref: str | None = None, template_version_hash: str | None = None,
        idempotency_key: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Select a template for a scope (or clear the scope's choice with no ref)."""

        _need(isinstance(actor_ref, str) and _HUMAN.fullmatch(actor_ref) is not None,
              "only a person may choose a fund template")
        _need(scope_kind in SCOPE_KINDS, f"scope_kind must be one of {list(SCOPE_KINDS)}")
        scope_ref = _text(scope_ref, "scope_ref", maximum=240)
        if scope_kind == "default":
            _need(scope_ref == DEFAULT_SCOPE_REF, "the workspace default is spelled 'default'")
        elif scope_kind == "industry":
            _need(scope_ref in INDUSTRY_LABELS,
                  "industry must be one of " + ", ".join(INDUSTRY_LABELS))
        reason = _text(reason, "reason", maximum=MAX_REASON_CHARS)
        action = "clear" if template_version_ref is None else "select"
        if action == "select":
            template = template_version(self.connection, _text(
                template_version_ref, "template_version_ref", maximum=240))
            _need(template_version_hash == template["content_hash"],
                  "the template changed since the page was read; reload and choose again")
            if template["id"] == BUILTIN_VERSION_REF:
                # A selection names a row; make sure the built-in row exists.
                self.seed_builtin()
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key", maximum=200)
        request = {"scope_kind": scope_kind, "scope_ref": scope_ref, "action": action,
                   "template_version_ref": template_version_ref,
                   "template_version_hash": template_version_hash if action == "select" else None,
                   "reason": reason, "actor_ref": actor_ref}
        moment = (now or _now()).astimezone(timezone.utc)
        with self.store._transaction() as cur:
            if idempotency_key is not None:
                row = cur.execute(
                    "SELECT record_json FROM fund_xlsx_template_selections WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if row is not None:
                    existing = _decode(row)
                    if existing.get("request_hash") != content_hash(request):
                        raise FundXlsxTemplateRegistryConflict(
                            "idempotency key was already used for a different choice")
                    return {**existing, "status": "replayed"}
            last = cur.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM fund_xlsx_template_selections").fetchone()[0]
            prior = cur.execute(
                "SELECT receipt_id, record_json FROM fund_xlsx_template_selections "
                "WHERE scope_kind=? AND scope_ref=? ORDER BY sequence DESC LIMIT 1",
                (scope_kind, scope_ref)).fetchone()
            outcome = "applied"
            if action == "clear" and (prior is None or json.loads(prior[1])["action"] == "clear"):
                outcome = "was_not_selected"
            elif (action == "select" and prior is not None
                  and json.loads(prior[1]).get("template_version_ref") == template_version_ref
                  and json.loads(prior[1])["action"] == "select"):
                outcome = "unchanged"
            sequence = int(last) + 1
            created_at = moment.isoformat(timespec="microseconds")
            record = {
                "schema_version": SELECTION_SCHEMA_VERSION,
                "id": "fund-xlsx-template-selection:" + content_hash(
                    {"sequence": sequence, "request": request, "at": created_at})[:32],
                "sequence": sequence,
                "prior_receipt_ref": None if prior is None else prior[0],
                **request, "outcome": outcome,
                "request_hash": content_hash(request), "created_at": created_at,
            }
            record["content_hash"] = content_hash(record)
            cur.execute(
                "INSERT INTO fund_xlsx_template_selections(receipt_id,sequence,scope_kind,scope_ref,"
                "action,template_version_id,prior_receipt_id,reason,actor_ref,idempotency_key,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], sequence, scope_kind, scope_ref, action, template_version_ref,
                 record["prior_receipt_ref"], reason, actor_ref, idempotency_key,
                 canonical_json(record), record["content_hash"], created_at))
        return {**record, "status": "recorded"}


__all__ = [
    "BASIS_LABELS", "BUILTIN_SLUG", "BUILTIN_VERSION_REF", "DEFAULT_MAPPING",
    "DEFAULT_SCOPE_REF", "FundXlsxTemplateRegistry", "FundXlsxTemplateRegistryConflict",
    "FundXlsxTemplateRegistryError", "INDUSTRY_LABELS", "SCOPE_KINDS", "SNAPSHOT_FIELDS",
    "VALUATION_BLOCKS", "builtin_version", "company_classification", "export_binding",
    "resolve_template", "selection_receipts", "selection_state", "template_version",
    "template_versions", "validate_candidate", "validate_mapping",
]
