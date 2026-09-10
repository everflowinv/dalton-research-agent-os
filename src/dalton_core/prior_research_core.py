"""W3: the fund's own earlier work on a company, read as a governed source.

Some companies are not new. There is an Initial Screen from two years ago, a
memo, a folder of notes, an Excel model somebody still maintains. A new
analyst reads all of it on day one, and it saves a week. Then they write their
own Initial Screen anyway, because the material is two years old and the
questions the market is asking have moved.

This module is the reading half of that. It is a ``host_tool`` feed like S1's
sales notes and company wiki: bytes a person already put on this machine,
read-only, offline, no credential to hold. Three things make it different.

**The manifest is the contract, and it is loose about layout.** The owner puts
files under ``$DALTON_PRIOR_RESEARCH_DIR/<ticker>/`` in whatever arrangement
they already have, and writes one ``manifest.json`` per company folder naming
each file: its ``path``, its ``kind``, its ``as_of``, who wrote it, and a free
note. Subdirectories, spaces, Chinese filenames, all fine. The feed does not
walk the tree looking for documents, because a folder is not a statement of
intent and a stray download is not prior research.

**It is strict about ``as_of``.** An entry with no date, or an unparseable
one, is refused with its reason and never enumerated. Nothing here derives a
date from a filename, an mtime or a title, and that refusal is the single
most load-bearing rule in the module. Age is what separates "this is what we
think" from "this is what we thought"; a prior view whose date was guessed is
a prior view that will be read as current on the day the guess is wrong.

**Everything it yields is tier ``internal_prior``.** Not because internal work
is weak -- it is often the best thing in the building -- but because it is
*ours*, and the claim index has to be able to tell "the company filed this"
from "we concluded this in 2024". A downstream staleness rule then downgrades
it once it is older than the policy threshold. Both of those live in
``claim_index_tagging``; this module's job is to put an honest ``as_of`` on
every row so that they have something to work with.

Formats: markdown and plain text are read verbatim. PDF is rendered through
the same optional ``pypdf`` path the public-web lane uses. ``.docx`` and
``.xlsx`` are read as text through ``openpyxl`` / the packaged docx reader
when they are installed, and refused with their reason when they are not --
never guessed at. A refusal is a row in ``refused`` on the wire, so the owner
sees "this one needs a date" rather than a document quietly missing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "prior-research"
LIST_OPERATION = "list_documents"
GET_OPERATION = "get_document"
OPERATIONS = (LIST_OPERATION, GET_OPERATION)

LIST_KIND = "prior-research-list-documents"
GET_KIND = "prior-research-get-document"
KIND_BY_OPERATION = {LIST_OPERATION: LIST_KIND, GET_OPERATION: GET_KIND}
LIST_CAPABILITY_ID = "capability:dalton:connector:prior-research-list-documents"
GET_CAPABILITY_ID = "capability:dalton:connector:prior-research-get-document"
CAPABILITY_BY_OPERATION = {
    LIST_OPERATION: LIST_CAPABILITY_ID,
    GET_OPERATION: GET_CAPABILITY_ID,
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:host-feed-directory"
FILESYSTEM_READ_REF = "host-feed:prior-research-corpus"

SOURCE_REF = "source:prior-research"
DOCUMENT_REF_PREFIX = "prior-research-doc:sha256:"
WIRE_SCHEMA_VERSION = "0.1"

#: The environment variable the owner sets to declare where the corpus is.
#: Read by ``deploy/macos/install.sh``, which links it to
#: ``<state>/feeds/prior-research``; the child process is handed a
#: ``--corpus-root`` and never reads the environment itself, because the
#: host-tool runner builds its child's environment from a whitelist rather
#: than inheriting one.
ROOT_ENV_VAR = "DALTON_PRIOR_RESEARCH_DIR"

MANIFEST_NAME = "manifest.json"

#: What a prior document *is*, in the owner's words. Closed, because the
#: import paths downstream branch on it: an ``initial_screen`` becomes a
#: deliverable v0, a ``model_excel`` becomes a PriorModelVersion, and the rest
#: become Claims. A sixth word would silently be routed nowhere.
DOCUMENT_KINDS: tuple[str, ...] = (
    # A previous Initial Screen on this company, written by us.
    "initial_screen",
    # An investment memo or write-up.
    "memo",
    # Working notes: call notes, reading notes, a running file.
    "notes",
    # A maintained Excel model.
    "model_excel",
    # Prior work that is none of the above and is still worth reading.
    "other",
)

#: The evidence tier every row of this feed carries. One word, not a
#: classifier: the source is "us, earlier", whatever the document is.
EVIDENCE_TIER = "internal_prior"

#: Where the ``as_of`` came from. One value today, and it is named rather than
#: implied so that a future basis (say, a date read out of a filing the memo
#: cites) is a new word rather than a silent change of meaning.
AS_OF_BASIS = "manifest_as_of"

#: The manifest entry's fields. ``path``, ``kind`` and ``as_of`` are required;
#: ``author`` and ``source_note`` may be empty but must be present, because an
#: absent key and an empty one are different mistakes and only one of them is
#: the owner saying "I don't know".
MANIFEST_ENTRY_REQUIRED: frozenset[str] = frozenset({"path", "kind", "as_of"})
MANIFEST_ENTRY_OPTIONAL: frozenset[str] = frozenset({"author", "source_note"})

MAX_DOCUMENTS = 500
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 600_000
MAX_PDF_PAGES = 400
#: A prior model can be large; this bounds what is read as *text*, not what a
#: workbook import reads cell by cell.
MAX_SHEET_ROWS = 400
MAX_SHEET_COLUMNS = 60

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOCUMENT_ID_RE = re.compile(r"^prior-research-doc:sha256:[0-9a-f]{64}$")
#: A company folder is a directory name. Deliberately permissive -- the owner
#: files by ticker, but a folder called `ACN-accenture` should not be a
#: refusal -- and deliberately not a path: one segment, no separators.
_COMPANY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Suffix to the one word this feed calls the format. Not a media type: the
#: frozen connector template may hold no slashes at all, so the wire says
#: "markdown" where a header would say "text/markdown".
FORMAT_BY_SUFFIX: Mapping[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
}
DOC_FORMATS: tuple[str, ...] = ("markdown", "text", "pdf", "docx", "xlsx")

_TEXT_RENDERER = "text-verbatim:0.1"
_DOCX_RENDERER = "docx-wordprocessingml:0.1"
_XLSX_RENDERER = "xlsx-openpyxl"


class PriorResearchError(RuntimeError):
    """The prior-research identity, governance record, or corpus is invalid."""


class PriorResearchRefusal(PriorResearchError):
    """One manifest entry cannot be enumerated, and this says why.

    Separate from the module's other errors on purpose: a bad entry is the
    owner's next action and must not take the other nine documents in the
    folder down with it.
    """


# -- identity -----------------------------------------------------------


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise PriorResearchError(
            f"prior-research has no frozen {operation!r} operation"
        )
    return operation


def prior_research_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen operation contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise PriorResearchError(
            f"packaged prior-research template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def prior_research_source_hash() -> str:
    """Shared by both operations: the same corpus is the same source."""

    template, _ = prior_research_contract(LIST_OPERATION)
    return content_hash(dict(template["source_identity"]))


def prior_research_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to the other."""

    _, contract = prior_research_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


#: The renderers this feed's identity is bound to, newest reader last. Named in
#: the adapter hash because they are what turns bytes into the text a Claim is
#: quoted from: a different PDF or spreadsheet reader produces different text
#: from the same file, which is a different capability and must not pass under
#: an approval granted for this one. The two hand-rolled readers carry their
#: own version; the two libraries are pinned by the extras that supply them.
RENDERER_IDENTITY: tuple[str, ...] = (
    _TEXT_RENDERER, _DOCX_RENDERER, "pdf-pypdf:0.1", _XLSX_RENDERER + ":0.1",
)


def prior_research_adapter_hash(operation: str) -> str:
    template, _ = prior_research_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
        "renderers": list(RENDERER_IDENTITY),
    })


def prior_research_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one prior-research operation."""

    template, contract = prior_research_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": prior_research_source_hash(),
        "schema_hash": prior_research_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": prior_research_adapter_hash(operation),
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


def prior_research_permissions() -> dict[str, Any]:
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


def prior_research_fixture_hash() -> str:
    template, _ = prior_research_contract(LIST_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_prior_research_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-10T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one prior-research operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise PriorResearchError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise PriorResearchError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(prior_research_permissions()),
        "expected_source_hash": prior_research_source_hash(),
        "expected_schema_hash": prior_research_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


# -- the manifest -------------------------------------------------------


def document_ref(company: str, relative_path: str) -> str:
    """Keyed by ``<company>/<path>``, which is what the owner filed.

    Not the body hash: a document whose text is edited in place is the same
    document with a new body, and a ref that moved would read as a second
    document rather than as a revision. The body hash travels beside it.
    """

    if not isinstance(company, str) or not company.strip():
        raise PriorResearchError("prior-research document has no company")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise PriorResearchError("prior-research document has no path")
    identity = f"{company.strip()}/{relative_path.strip()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{DOCUMENT_REF_PREFIX}{digest}"


def _as_of(raw: Any) -> str:
    """A ``YYYY-MM-DD`` date, or a refusal that names what was wrong.

    There is deliberately no fallback ladder here. The wiki feed may fall back
    from a period to a published date to a retrieval date; this one may not,
    because the retrieval date of a two-year-old memo is today and would make
    it look new.
    """

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise PriorResearchRefusal(
            "manifest entry has no as_of; a prior document with no date cannot "
            "be aged, and an un-ageable prior view gets read as a current one"
        )
    if not isinstance(raw, str):
        raise PriorResearchRefusal("manifest entry as_of must be a YYYY-MM-DD string")
    value = raw.strip()
    if _DATE_RE.fullmatch(value) is None:
        raise PriorResearchRefusal(
            f"manifest entry as_of {value!r} is not a YYYY-MM-DD date"
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PriorResearchRefusal(
            f"manifest entry as_of {value!r} is not a real date"
        ) from exc
    return value


def _entry_text(raw: Any, name: str, *, maximum: int = 2_000) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise PriorResearchRefusal(f"manifest entry {name} must be text")
    return raw.strip()[:maximum]


def normalize_manifest_entry(raw: Any) -> dict[str, Any]:
    """One manifest row, normalised, or a refusal naming what is wrong."""

    if not isinstance(raw, Mapping):
        raise PriorResearchRefusal("manifest entry must be an object")
    keys = set(raw)
    missing = MANIFEST_ENTRY_REQUIRED - keys
    if missing:
        raise PriorResearchRefusal(
            f"manifest entry is missing {sorted(missing)}"
        )
    unknown = keys - MANIFEST_ENTRY_REQUIRED - MANIFEST_ENTRY_OPTIONAL
    if unknown:
        raise PriorResearchRefusal(
            f"manifest entry has fields this feed does not know: {sorted(unknown)}"
        )
    path = _entry_text(raw["path"], "path", maximum=512)
    if not path:
        raise PriorResearchRefusal("manifest entry has an empty path")
    kind = _entry_text(raw["kind"], "kind", maximum=64)
    if kind not in DOCUMENT_KINDS:
        raise PriorResearchRefusal(
            f"manifest entry kind {kind!r} is not one of {list(DOCUMENT_KINDS)}"
        )
    return {
        "path": path,
        "kind": kind,
        "as_of": _as_of(raw["as_of"]),
        "author": _entry_text(raw.get("author"), "author", maximum=200),
        "source_note": _entry_text(raw.get("source_note"), "source_note"),
    }


def parse_manifest(raw: Any, *, company: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(entries, refusals)`` for one company folder's ``manifest.json``.

    A malformed *entry* is refused and the rest of the folder still reads. A
    malformed *manifest* is not: if the file itself will not parse, or is not
    the shape this feed knows, there is nothing to be partial about.
    """

    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PriorResearchError(
                f"{company}/{MANIFEST_NAME} is not valid JSON"
            ) from exc
    if isinstance(raw, Mapping):
        body = raw.get("documents")
        if body is None:
            raise PriorResearchError(
                f"{company}/{MANIFEST_NAME} has no documents array"
            )
    else:
        body = raw
    if not isinstance(body, Sequence) or isinstance(body, (str, bytes)):
        raise PriorResearchError(
            f"{company}/{MANIFEST_NAME} documents must be an array"
        )
    entries: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(body):
        try:
            entry = normalize_manifest_entry(item)
        except PriorResearchRefusal as exc:
            located = ""
            if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                located = item["path"].strip()[:512]
            refusals.append({
                "company": company,
                "relative_path": located or f"documents[{index}]",
                "reason": str(exc),
            })
            continue
        if entry["path"] in seen:
            refusals.append({
                "company": company,
                "relative_path": entry["path"],
                "reason": "manifest names this path twice",
            })
            continue
        seen.add(entry["path"])
        entries.append(entry)
    entries.sort(key=lambda item: (item["as_of"], item["path"]), reverse=True)
    return entries, refusals


# -- reading the corpus -------------------------------------------------


def _company_dir(root: Path, company: str) -> Path:
    if not isinstance(company, str) or _COMPANY_RE.fullmatch(company) is None:
        raise PriorResearchError(
            "a prior-research company folder is one path segment of letters, "
            "digits, dot, dash or underscore"
        )
    candidate = (root / company).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PriorResearchError("company folder escapes the corpus root") from exc
    return candidate


def _resolve(company_dir: Path, relative_path: str) -> Path:
    """The document's file, refusing anything that leaves its company folder."""

    candidate = (company_dir / relative_path).resolve()
    try:
        candidate.relative_to(company_dir)
    except ValueError as exc:
        raise PriorResearchRefusal(
            "manifest path escapes its company folder"
        ) from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise PriorResearchRefusal(
            "manifest names a file that is missing or is not a regular file"
        )
    return candidate


def format_for(relative_path: str) -> str:
    """The one-word format of a manifest path, or a refusal naming the suffix."""

    suffix = Path(relative_path).suffix.lower()
    doc_format = FORMAT_BY_SUFFIX.get(suffix)
    if doc_format is None:
        raise PriorResearchRefusal(
            f"this feed does not read {suffix or 'extensionless'} files; "
            f"it reads {sorted(FORMAT_BY_SUFFIX)}"
        )
    return doc_format


def _docx_text(raw: bytes) -> str:
    """A ``.docx`` as its paragraphs, in order, with no styling.

    Deliberately hand-rolled over the zip rather than a dependency: a
    wordprocessingml body is paragraphs of runs of text, the reading of it is
    twenty lines, and adding a library for twenty lines would put a version
    number into this connector's adapter identity for no gain.
    """

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(_BytesReader(raw)) as archive:
            document = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise PriorResearchRefusal(
            "this .docx has no word/document.xml; it is not a Word document"
        ) from exc
    try:
        tree = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise PriorResearchRefusal("this .docx body is not parseable XML") from exc
    lines: list[str] = []
    for paragraph in tree.iter(f"{namespace}p"):
        parts = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
        lines.append("".join(parts).strip())
    text = "\n".join(lines).strip()
    if not text:
        raise PriorResearchRefusal("this .docx holds no readable text")
    return text


def _xlsx_text(path: Path) -> tuple[str, str]:
    """A workbook rendered as text, for reading -- never for numbers.

    The numbers a prior model holds go through ``prior_model_import``, cell by
    cell, with the formula kept verbatim. What comes back here is a flattened
    reading copy so that a person -- or a prompt -- can see what the model is
    about. Nothing downstream takes a figure from this string.
    """

    try:
        from openpyxl import load_workbook
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the env
        raise PriorResearchRefusal(
            "reading .xlsx needs the optional `prior-models` extra (openpyxl); "
            "install it or drop the workbook from the manifest -- this feed "
            "does not guess at a spreadsheet it cannot open"
        ) from exc
    import openpyxl

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        lines: list[str] = []
        for sheet in workbook.worksheets:
            lines.append(f"# {sheet.title}")
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if row_index > MAX_SHEET_ROWS:
                    lines.append("… (sheet truncated for reading)")
                    break
                cells = [
                    "" if value is None else str(value)
                    for value in row[:MAX_SHEET_COLUMNS]
                ]
                if any(cell.strip() for cell in cells):
                    lines.append("\t".join(cells).rstrip())
            lines.append("")
    finally:
        workbook.close()
    return "\n".join(lines).strip(), f"{_XLSX_RENDERER}-{openpyxl.__version__}:0.1"


def _pdf_text(raw: bytes) -> str:
    try:
        import pypdf
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the env
        raise PriorResearchRefusal(
            "reading .pdf needs the optional `pdf` extra (pypdf); install it "
            "or drop the file from the manifest"
        ) from exc
    try:
        reader = pypdf.PdfReader(_BytesReader(raw))
    except Exception as exc:  # noqa: BLE001 - the reason is the point
        raise PriorResearchRefusal(f"this .pdf will not open: {exc}") from exc
    if reader.is_encrypted:
        raise PriorResearchRefusal("this .pdf is encrypted")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise PriorResearchRefusal(
            f"this .pdf has more than {MAX_PDF_PAGES} pages"
        )
    parts = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(part.strip() for part in parts).strip()
    if not text:
        raise PriorResearchRefusal(
            "this .pdf holds no extractable text; it is probably a scan"
        )
    return text


class _BytesReader:
    """A minimal seekable file over bytes, so readers need no temp file."""

    def __init__(self, raw: bytes) -> None:
        import io

        self._buffer = io.BytesIO(raw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._buffer, name)


def read_body(path: Path, *, relative_path: str) -> tuple[str, str, str, bytes]:
    """``(text, doc_format, renderer, raw_bytes)`` for one prior document."""

    doc_format = format_for(relative_path)
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise PriorResearchRefusal(
            "this document exceeds the feed size ceiling"
        )
    raw = path.read_bytes()
    if doc_format in {"markdown", "text"}:
        text, renderer = raw.decode("utf-8", errors="replace"), _TEXT_RENDERER
    elif doc_format == "pdf":
        text, renderer = _pdf_text(raw), "pdf-pypdf:0.1"
    elif doc_format == "docx":
        text, renderer = _docx_text(raw), _DOCX_RENDERER
    else:
        text, renderer = _xlsx_text(path)
    if len(text) > MAX_TEXT_CHARS:
        raise PriorResearchRefusal(
            f"this document renders to more than {MAX_TEXT_CHARS} characters"
        )
    return text, doc_format, renderer, raw


def build_header(
    entry: Mapping[str, Any],
    *,
    company: str,
    text: str,
    doc_format: str,
    renderer: str,
    raw: bytes,
) -> dict[str, Any]:
    """The wire header for one enumerated prior document."""

    return {
        "document_id": document_ref(company, entry["path"]),
        "kind": entry["kind"],
        "as_of": entry["as_of"],
        "as_of_basis": AS_OF_BASIS,
        "author": entry["author"],
        "source_note": entry["source_note"],
        "company": company,
        "relative_path": entry["path"],
        "doc_format": doc_format,
        "renderer": renderer,
        "evidence_tier": EVIDENCE_TIER,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_chars": len(text),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "file_bytes": len(raw),
    }


def company_folders(corpus_root: str | Path) -> list[str]:
    """Every company folder that carries a manifest, in name order."""

    root = Path(corpus_root).expanduser().resolve()
    if not root.is_dir():
        raise PriorResearchError("prior-research corpus root is missing")
    names: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        # A symlinked company folder is skipped, not followed: the corpus root
        # is itself a link the installer made, and following a second one is
        # how a read escapes the directory the owner declared.
        if child.is_symlink() or not child.is_dir():
            continue
        if _COMPANY_RE.fullmatch(child.name) is None:
            continue
        if (child / MANIFEST_NAME).is_file():
            names.append(child.name)
    return names


def manifest_for(corpus_root: str | Path, company: str) -> tuple[list[dict], list[dict]]:
    """``(entries, refusals)`` for one company's manifest on disk."""

    root = Path(corpus_root).expanduser().resolve()
    if not root.is_dir():
        raise PriorResearchError("prior-research corpus root is missing")
    manifest = _company_dir(root, company) / MANIFEST_NAME
    if not manifest.is_file():
        raise PriorResearchError(f"{company} has no {MANIFEST_NAME}")
    return parse_manifest(manifest.read_text(encoding="utf-8"), company=company)


def enumerate_documents(
    corpus_root: str | Path,
    *,
    since: str,
    until: str,
    company: str | None = None,
    limit: int = MAX_DOCUMENTS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Headers for prior documents dated within ``[since, until]``, newest first.

    Returns ``(documents, refused, truncated)``. Selection is decided from the
    manifests alone -- cheap, metadata only -- and the files are opened for the
    selected rows, because the body hash has to come from the file: a manifest
    is what the owner wrote down and the file is the document.
    """

    for name, value in (("since", since), ("until", until)):
        if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
            raise PriorResearchError(f"{name} must be a YYYY-MM-DD date")
    if until < since:
        raise PriorResearchError("the enumeration window ends before it starts")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_DOCUMENTS:
        raise PriorResearchError(f"limit must be 1..{MAX_DOCUMENTS}")
    root = Path(corpus_root).expanduser().resolve()
    companies = [company] if company is not None else company_folders(root)
    matching: list[tuple[str, dict[str, Any]]] = []
    refused: list[dict[str, Any]] = []
    for name in companies:
        try:
            entries, folder_refusals = manifest_for(root, name)
        except PriorResearchError as exc:
            refused.append({
                "company": str(name), "relative_path": MANIFEST_NAME,
                "reason": str(exc),
            })
            continue
        refused.extend(folder_refusals)
        for entry in entries:
            if since <= entry["as_of"] <= until:
                matching.append((name, entry))
    matching.sort(key=lambda pair: (pair[1]["as_of"], pair[0], pair[1]["path"]),
                  reverse=True)
    documents: list[dict[str, Any]] = []
    for name, entry in matching[:limit]:
        try:
            path = _resolve(_company_dir(root, name), entry["path"])
            text, doc_format, renderer, raw = read_body(
                path, relative_path=entry["path"]
            )
        except PriorResearchRefusal as exc:
            refused.append({
                "company": name, "relative_path": entry["path"], "reason": str(exc),
            })
            continue
        documents.append(build_header(
            entry, company=name, text=text, doc_format=doc_format,
            renderer=renderer, raw=raw,
        ))
    refused.sort(key=lambda item: (item["company"], item["relative_path"]))
    return documents, refused, len(matching) > limit


def read_document(
    corpus_root: str | Path, document_id: str
) -> tuple[dict[str, Any], str]:
    """One prior document's header and its rendered text."""

    if not isinstance(document_id, str) or _DOCUMENT_ID_RE.fullmatch(document_id) is None:
        raise PriorResearchError(f"document id must be {DOCUMENT_REF_PREFIX}<sha256>")
    root = Path(corpus_root).expanduser().resolve()
    for name in company_folders(root):
        entries, _ = manifest_for(root, name)
        for entry in entries:
            if document_ref(name, entry["path"]) != document_id:
                continue
            path = _resolve(_company_dir(root, name), entry["path"])
            text, doc_format, renderer, raw = read_body(
                path, relative_path=entry["path"]
            )
            header = build_header(
                entry, company=name, text=text, doc_format=doc_format,
                renderer=renderer, raw=raw,
            )
            return header, text
    raise PriorResearchError("no prior-research document with that id is in the corpus")


def read_document_artifact(
    corpus_root: str | Path, document_id: str
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Read one document plus a loss-explicit, inert Office structure manifest.

    The governed connector wire remains byte-compatible. Import and audit tools
    that need structure call this additive path; embedded objects are hashed but
    never opened or executed.
    """

    header, text = read_document(corpus_root, document_id)
    root = Path(corpus_root).expanduser().resolve()
    path = _resolve(_company_dir(root, header["company"]), header["relative_path"])
    if header["doc_format"] == "docx":
        from .prior_import_assets import docx_artifact_manifest
        artifact = docx_artifact_manifest(path.read_bytes())
        artifact["text_projection"] = {
            "complete": True, "preserves": ["ordered_paragraph_and_table_text"],
            "omits": ["layout", "styles", "rendered_chart_text"],
        }
    elif header["doc_format"] == "xlsx":
        from .prior_import_assets import xlsx_artifact_manifest
        artifact = xlsx_artifact_manifest(path)
        truncated_sheets = [
            sheet["name"] for sheet in artifact["sheets"]
            if sheet["max_row"] > MAX_SHEET_ROWS or sheet["max_column"] > MAX_SHEET_COLUMNS
        ]
        artifact["text_projection"] = {
            "complete": not truncated_sheets,
            "row_limit": MAX_SHEET_ROWS, "column_limit": MAX_SHEET_COLUMNS,
            "truncated_sheets": truncated_sheets,
            "preserves": ["cached_values", "sheet_order"],
            "omits": ["formulas", "styles", "charts", "external_link_targets"],
        }
    else:
        artifact = {"schema_version": "prior-office-artifact-0.1",
                    "format": header["doc_format"],
                    "text_projection": {"complete": True, "omits": []}}
    artifact["file_sha256"] = header["file_sha256"]
    return header, text, artifact


def age_in_months(as_of: str, *, now: date) -> int:
    """Whole months between a prior document's date and ``now``.

    Months rather than days because that is how the owner talks about it --
    「上一版（内部，2024-03）」 -- and because a prior view's age is a coarse
    quantity: the difference between 181 and 184 days is nothing, and the
    difference between four months and fourteen is the whole judgement.
    """

    then = date.fromisoformat(as_of)
    months = (now.year - then.year) * 12 + (now.month - then.month)
    if now.day < then.day:
        months -= 1
    return max(0, months)


__all__ = [
    "AS_OF_BASIS",
    "CAPABILITY_BY_OPERATION",
    "DOCUMENT_KINDS",
    "DOCUMENT_REF_PREFIX",
    "EVIDENCE_TIER",
    "FILESYSTEM_READ_REF",
    "GET_CAPABILITY_ID",
    "GET_KIND",
    "GET_OPERATION",
    "KIND_BY_OPERATION",
    "LIST_CAPABILITY_ID",
    "LIST_KIND",
    "LIST_OPERATION",
    "MANIFEST_NAME",
    "MAX_DOCUMENTS",
    "DOC_FORMATS",
    "FORMAT_BY_SUFFIX",
    "OPERATIONS",
    "RENDERER_IDENTITY",
    "ROOT_ENV_VAR",
    "SIDE_EFFECT",
    "SOURCE_REF",
    "TEMPLATE_KEY",
    "WIRE_SCHEMA_VERSION",
    "PriorResearchError",
    "PriorResearchRefusal",
    "age_in_months",
    "build_header",
    "build_prior_research_governance_record",
    "company_folders",
    "document_ref",
    "enumerate_documents",
    "manifest_for",
    "format_for",
    "normalize_manifest_entry",
    "parse_manifest",
    "prior_research_adapter_hash",
    "prior_research_contract",
    "prior_research_fixture_hash",
    "prior_research_identity",
    "prior_research_permissions",
    "prior_research_schema_hash",
    "prior_research_source_hash",
    "read_body",
    "read_document",
    "read_document_artifact",
]
