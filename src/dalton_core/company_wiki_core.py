"""S1: the company wiki -- meeting minutes, expert calls and broker notes a human wrote.

A person on this machine keeps a markdown corpus: management meeting minutes,
expert interviews, broker reports, quarterly notes, NDR write-ups, buy-side
memos. A small SQLite index beside it records, per document, which company or
sector it belongs to, what kind of document it is, and when it is from.

Dalton reads both, read-only, offline. It does not run the wiki's tagger, does
not touch its embedding index, and holds no credential -- there is none to
hold. The corpus is bytes a human already put here.

**Tier comes from the document kind, and only from it.** Minutes of a call
with management are a management statement. An expert interview is expert
testimony. A broker note is sell-side. A quarterly note or buy-side memo is
internal. Anything this feed cannot place is ``unclassified`` and stays there:
a wrong tier is worse than no tier, because the claim index reads tier as
provenance strength.

**An industry document has no company.** The wiki files sector notes under a
sector slug and gives them no ticker. This feed does not invent one. A
document that names five companies in passing is not evidence about any of
them, and forcing an attribution is how a queue fills with documents nobody
should have paid to read.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .connector_inventory import load_packaged_connector_inventory
from .readonly_sqlite import connect_read_only
from .store import content_hash

TEMPLATE_KEY = "company-wiki"
LIST_OPERATION = "list_documents"
GET_OPERATION = "get_document"
OPERATIONS = (LIST_OPERATION, GET_OPERATION)

LIST_KIND = "company-wiki-list-documents"
GET_KIND = "company-wiki-get-document"
KIND_BY_OPERATION = {LIST_OPERATION: LIST_KIND, GET_OPERATION: GET_KIND}
LIST_CAPABILITY_ID = "capability:dalton:connector:company-wiki-list-documents"
GET_CAPABILITY_ID = "capability:dalton:connector:company-wiki-get-document"
CAPABILITY_BY_OPERATION = {
    LIST_OPERATION: LIST_CAPABILITY_ID,
    GET_OPERATION: GET_CAPABILITY_ID,
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:host-feed-directory"
FILESYSTEM_READ_REF = "host-feed:company-wiki-corpus"

SOURCE_REF = "source:company-wiki"
DOCUMENT_REF_PREFIX = "company-wiki-doc:sha256:"
WIRE_SCHEMA_VERSION = "0.1"

# One window's worth, not the corpus's. The whole corpus is about a thousand
# documents; a caller whose window does not fit narrows it rather than being
# handed a silent prefix.
MAX_DOCUMENTS = 2_000
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

# The wiki's own naming: companies are upper-case tickers, sectors are
# lower-case slugs. Both live in one ``related`` array, so the case is what
# tells them apart.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9-]*$")
_SECTOR_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOCUMENT_ID_RE = re.compile(r"^company-wiki-doc:sha256:[0-9a-f]{64}$")

# Document kind to (closed key, evidence tier), first match on the wiki's own
# free-text label. Ordered: "季度研究笔记" is a quarterly note before it is a
# research note, and an expert call hosted by a broker is still expert
# testimony.
#
# The rules are deliberately narrow. Everything they do not recognise becomes
# ``other`` / ``unclassified`` rather than being rounded up to the nearest
# tier that looks plausible.
DOC_TYPE_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("专家", "expert", "fireside"), "expert_interview", "expert"),
    (("卖方", "券商", "分析师"), "broker_report", "sell_side"),
    (("earnings update", "earnings-update"), "broker_report", "sell_side"),
    (("ndr",), "ndr", "management_statement"),
    (
        ("管理层", "业绩后", "ir会议", "路演纪要"),
        "management_meeting_minutes",
        "management_statement",
    ),
    (("季度",), "quarterly_note", "internal"),
    (("买方", "memo", "投资备忘录"), "buy_side_note", "internal"),
    (("flomo", "研究笔记"), "research_note", "internal"),
)
UNKNOWN_DOC_TYPE = ("other", "unclassified")


class CompanyWikiError(RuntimeError):
    """The wiki identity, governance record, or corpus layout is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise CompanyWikiError(f"the company wiki has no frozen {operation!r} operation")
    return operation


def company_wiki_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen operation contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise CompanyWikiError(
            f"packaged company-wiki template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def company_wiki_source_hash() -> str:
    """Shared by both operations: the same corpus is the same source."""

    template, _ = company_wiki_contract(LIST_OPERATION)
    return content_hash(dict(template["source_identity"]))


def company_wiki_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to the other."""

    _, contract = company_wiki_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def company_wiki_adapter_hash(operation: str) -> str:
    template, _ = company_wiki_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
    })


def company_wiki_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one company-wiki operation."""

    template, contract = company_wiki_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": company_wiki_source_hash(),
        "schema_hash": company_wiki_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": company_wiki_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def company_wiki_permissions() -> dict[str, Any]:
    """Read one host directory, write the raw sink, reach nothing."""

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [FILESYSTEM_READ_REF],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def company_wiki_fixture_hash() -> str:
    template, _ = company_wiki_contract(LIST_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_company_wiki_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one company-wiki operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise CompanyWikiError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise CompanyWikiError("approved_by must be a human: principal")
    kind = KIND_BY_OPERATION[operation]
    capability_id = CAPABILITY_BY_OPERATION[operation]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{kind}:v{version}",
        "status": status,
        "capability_id": capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{kind}:v{version}",
        "approval_ref": f"approval:connector-governance:{kind}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{kind}:v{version}",
        "registry_revision_ref": f"{capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{kind}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": copy.deepcopy(company_wiki_permissions()),
        "expected_source_hash": company_wiki_source_hash(),
        "expected_schema_hash": company_wiki_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


# -- reading the corpus -------------------------------------------------


def classify_doc_type(raw: Any) -> tuple[str, str]:
    """(closed key, evidence tier) for one of the wiki's own document labels."""

    text = str(raw or "").strip().lower()
    for needles, key, tier in DOC_TYPE_RULES:
        if any(needle in text for needle in needles):
            return key, tier
    return UNKNOWN_DOC_TYPE


def document_ref(filepath: str) -> str:
    """Keyed by the corpus-relative path, which is the wiki's own identity.

    Not the row id: the index is rebuilt from the corpus, so a row id is a
    property of the last rebuild rather than of the document.
    """

    if not isinstance(filepath, str) or not filepath.strip():
        raise CompanyWikiError("wiki document has no filepath")
    digest = hashlib.sha256(filepath.strip().encode("utf-8")).hexdigest()
    return f"{DOCUMENT_REF_PREFIX}{digest}"


def _json_array(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _resolve(corpus_root: Path, filepath: str) -> Path:
    """The document's file, refusing anything that leaves the corpus."""

    candidate = (corpus_root / filepath).resolve()
    try:
        candidate.relative_to(corpus_root)
    except ValueError as exc:
        raise CompanyWikiError("wiki filepath escapes the corpus root") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise CompanyWikiError("wiki document file is missing or not a regular file")
    return candidate


def _read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise CompanyWikiError("wiki document exceeds the feed size ceiling")
    return path.read_text(encoding="utf-8", errors="replace")


def _header(row: Mapping[str, Any], *, text: str) -> dict[str, Any]:
    category_type = str(row["category_type"] or "").strip()
    if category_type not in {"company", "sector"}:
        raise CompanyWikiError("wiki row has an unknown category type")
    category_name = str(row["category_name"] or "").strip()
    related = _json_array(row["related"])
    companies = [item for item in related if _TICKER_RE.fullmatch(item)]
    sectors = [item for item in related if _SECTOR_RE.fullmatch(item)]
    if category_type == "company" and category_name and category_name not in companies:
        companies.insert(0, category_name)
    if category_type == "sector" and category_name and category_name not in sectors:
        sectors.insert(0, category_name)
    doc_date = str(row["date"] or "").strip()
    if _DATE_RE.fullmatch(doc_date) is None:
        raise CompanyWikiError("wiki row has no usable date")
    key, tier = classify_doc_type(row["content_type"])
    return {
        "document_id": document_ref(str(row["filepath"])),
        "doc_type": str(row["content_type"] or "").strip() or "unknown",
        "doc_type_key": key,
        "evidence_tier": tier,
        "doc_date": doc_date,
        "category_type": category_type,
        "category_name": category_name,
        # A sector note keeps an empty company list. That is the answer, not
        # a gap.
        "company_tags": sorted(dict.fromkeys(companies)),
        "sector_tags": sorted(dict.fromkeys(sectors)),
        "topic_tags": sorted(dict.fromkeys(_json_array(row["tags"]))),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_chars": len(text),
    }


_ROW_COLUMNS = (
    "category_type", "category_name", "content_type", "date", "filename",
    "filepath", "tags", "related",
)


def _rows(index_db: str | Path) -> list[dict[str, Any]]:
    connection = connect_read_only(index_db)
    try:
        columns = {
            item[1] for item in connection.execute("PRAGMA table_info(documents)")
        }
        missing = set(_ROW_COLUMNS) - columns
        if missing:
            raise CompanyWikiError(
                f"wiki index is missing {sorted(missing)}; the corpus shape changed"
            )
        cursor = connection.execute(
            "SELECT category_type,category_name,content_type,date,filename,filepath,"
            "tags,related FROM documents ORDER BY date DESC, filepath ASC"
        )
        return [dict(zip(_ROW_COLUMNS, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def _matches(row: Mapping[str, Any], *, company: str | None, industry: str | None) -> bool:
    if company is None and industry is None:
        return True
    related = _json_array(row["related"]) + _json_array(row["tags"])
    name = str(row["category_name"] or "").strip()
    if company is not None:
        if str(row["category_type"]) == "company" and name == company:
            return True
        if company in related:
            return True
    if industry is not None:
        if str(row["category_type"]) == "sector" and name == industry:
            return True
        if industry in related:
            return True
    return False


def enumerate_documents(
    index_db: str | Path,
    corpus_root: str | Path,
    *,
    since: str,
    until: str,
    company: str | None = None,
    industry: str | None = None,
    limit: int = MAX_DOCUMENTS,
) -> tuple[list[dict[str, Any]], bool]:
    """Headers for documents dated within ``[since, until]``, newest first.

    Returns ``(documents, truncated)``. Matching is decided from the index --
    metadata only, cheap -- and the files are opened for the selected rows
    alone, because the body hash has to come from the file: the index is a
    rebuildable projection and the file is the document.

    ``truncated`` is true when the window holds more than ``limit``. It is
    counted before the files are opened, so admitting the truncation costs
    nothing, and the caller narrows the window rather than receiving a prefix
    that calls itself an enumeration.
    """

    for name, value in (("since", since), ("until", until)):
        if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
            raise CompanyWikiError(f"{name} must be a YYYY-MM-DD date")
    if until < since:
        raise CompanyWikiError("the enumeration window ends before it starts")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_DOCUMENTS:
        raise CompanyWikiError(f"limit must be 1..{MAX_DOCUMENTS}")
    root = Path(corpus_root).expanduser().resolve()
    if not root.is_dir():
        raise CompanyWikiError("wiki corpus root is missing")
    matching = [
        row for row in _rows(index_db)
        if _DATE_RE.fullmatch(str(row["date"] or "").strip()) is not None
        and since <= str(row["date"]).strip() <= until
        and _matches(row, company=company, industry=industry)
    ]
    selected = [
        _header(row, text=_read_text(_resolve(root, str(row["filepath"]))))
        for row in matching[:limit]
    ]
    return selected, len(matching) > limit


def read_document(
    index_db: str | Path, corpus_root: str | Path, document_id: str
) -> tuple[dict[str, Any], str]:
    """One document's header and its verbatim markdown, frontmatter included.

    The frontmatter is part of what a person wrote and carries the tags, so it
    is kept: the hash a listing published is the hash of the whole file.
    """

    if not isinstance(document_id, str) or _DOCUMENT_ID_RE.fullmatch(document_id) is None:
        raise CompanyWikiError(f"document id must be {DOCUMENT_REF_PREFIX}<sha256>")
    root = Path(corpus_root).expanduser().resolve()
    if not root.is_dir():
        raise CompanyWikiError("wiki corpus root is missing")
    for row in _rows(index_db):
        if document_ref(str(row["filepath"])) != document_id:
            continue
        text = _read_text(_resolve(root, str(row["filepath"])))
        return _header(row, text=text), text
    raise CompanyWikiError("no wiki document with that id is in the corpus")


def wiki_document_tickers(headers: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every ticker the corpus itself tags, across a listing."""

    tickers: dict[str, None] = {}
    for header in headers:
        for ticker in header.get("company_tags", ()):
            tickers.setdefault(ticker, None)
    return sorted(tickers)


__all__ = [
    "CAPABILITY_BY_OPERATION",
    "DOCUMENT_REF_PREFIX",
    "DOC_TYPE_RULES",
    "FILESYSTEM_READ_REF",
    "GET_CAPABILITY_ID",
    "GET_KIND",
    "GET_OPERATION",
    "KIND_BY_OPERATION",
    "LIST_CAPABILITY_ID",
    "LIST_KIND",
    "LIST_OPERATION",
    "MAX_DOCUMENTS",
    "OPERATIONS",
    "SIDE_EFFECT",
    "SOURCE_REF",
    "UNKNOWN_DOC_TYPE",
    "WIRE_SCHEMA_VERSION",
    "CompanyWikiError",
    "build_company_wiki_governance_record",
    "classify_doc_type",
    "company_wiki_adapter_hash",
    "company_wiki_contract",
    "company_wiki_fixture_hash",
    "company_wiki_identity",
    "company_wiki_permissions",
    "company_wiki_schema_hash",
    "company_wiki_source_hash",
    "document_ref",
    "enumerate_documents",
    "read_document",
    "wiki_document_tickers",
]
