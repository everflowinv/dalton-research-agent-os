"""S1: the sell-side notes a human already receives, read as files on this disk.

Three banks mail research and sales notes to the owner every trading day. A
host skill fetches them from Gmail twice a day, filters by sender domain, and
writes the **verbatim mail** -- sender, subject, date, body -- into one JSON
file per run. Dalton reads those files. It never authenticates to Gmail, never
calls a mail API, and never sees a credential: by the time these bytes exist,
the credential has already been spent by somebody else's process.

That is why the auth boundary is ``none`` and ``network`` is false. The only
permission this connector needs is to read one directory.

**The note, not the summary.** The same file also carries a model-written
digest of the mail. It is a useful thing and it is not evidence: it is a
paraphrase, and a paraphrase cannot be quoted back to its author. Reading it
is a forbidden route on the profile, not an option nobody happens to take.

**Tier.** Every note here is ``sell_side`` with a named sender -- an analyst
or a named desk at one of three banks. The tier is a fact about the source,
not about the text, so it rides on the wire rather than being guessed later
from the subject line.

Two operations, two capabilities, two approvals, exactly as Guidepoint and
AlphaEngine are split: ``list_notes`` reads an index, ``get_note`` reads a
document, and a schema hash binds one operation so neither approval widens
into the other.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "sales-notes"
LIST_OPERATION = "list_notes"
GET_OPERATION = "get_note"
OPERATIONS = (LIST_OPERATION, GET_OPERATION)

LIST_KIND = "sales-notes-list-notes"
GET_KIND = "sales-notes-get-note"
KIND_BY_OPERATION = {LIST_OPERATION: LIST_KIND, GET_OPERATION: GET_KIND}
LIST_CAPABILITY_ID = "capability:dalton:connector:sales-notes-list-notes"
GET_CAPABILITY_ID = "capability:dalton:connector:sales-notes-get-note"
CAPABILITY_BY_OPERATION = {
    LIST_OPERATION: LIST_CAPABILITY_ID,
    GET_OPERATION: GET_CAPABILITY_ID,
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:host-feed-directory"
# A logical name for the directory, never the directory. Governance records
# are owner artifacts that get copied around; a real path in one is a leak of
# this machine's shape for no gain, and the child is told the path by argv.
FILESYSTEM_READ_REF = "host-feed:market-digest-output"

SOURCE_REF = "source:sales-notes"
DOCUMENT_REF_PREFIX = "sales-note:"
EVIDENCE_TIER = "sell_side"
WIRE_SCHEMA_VERSION = "0.1"

# What a digest run writes. Checked rather than assumed: a shape change
# upstream should stop this connector, not quietly halve its enumeration.
DIGEST_FILE_RE = re.compile(r"^digest_(\d{4}-\d{2}-\d{2})_(AM|PM)\.json$")
_NOTE_ID_RE = re.compile(r"^[0-9a-z]{6,40}$")
_REQUIRED_EMAIL_FIELDS = frozenset({"id", "from", "subject", "date", "is_priority", "body"})

MAX_NOTES = 500


class SalesNotesError(RuntimeError):
    """The sales-notes identity, governance record, or feed layout is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise SalesNotesError(f"sales notes have no frozen {operation!r} operation")
    return operation


def sales_notes_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen operation contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise SalesNotesError(
            f"packaged sales-notes template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def sales_notes_source_hash() -> str:
    """Shared by both operations: the same mail feed is the same source."""

    template, _ = sales_notes_contract(LIST_OPERATION)
    return content_hash(dict(template["source_identity"]))


def sales_notes_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to the other."""

    _, contract = sales_notes_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def sales_notes_adapter_hash(operation: str) -> str:
    template, _ = sales_notes_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
    })


def sales_notes_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one sales-notes operation."""

    template, contract = sales_notes_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": sales_notes_source_hash(),
        "schema_hash": sales_notes_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": sales_notes_adapter_hash(operation),
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


def sales_notes_permissions() -> dict[str, Any]:
    """Read one host directory, write the raw sink, reach nothing.

    No credential slot: there is nothing for a credential authority to hold.
    The mail was fetched by a host skill under the owner's own OAuth store
    before this process existed, and the bytes on disk are all Dalton gets.
    """

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [FILESYSTEM_READ_REF],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def sales_notes_fixture_hash() -> str:
    template, _ = sales_notes_contract(LIST_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_sales_notes_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one sales-notes operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise SalesNotesError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise SalesNotesError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(sales_notes_permissions()),
        "expected_source_hash": sales_notes_source_hash(),
        "expected_schema_hash": sales_notes_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


# -- reading the feed ---------------------------------------------------


def note_ref(note_id: str) -> str:
    """The document ref for one mail, keyed by the id the mail already has."""

    if not isinstance(note_id, str) or _NOTE_ID_RE.fullmatch(note_id) is None:
        raise SalesNotesError("sales note id must be 6-40 lowercase alphanumerics")
    return f"{DOCUMENT_REF_PREFIX}{note_id}"


def digest_ref_of(path: Path) -> str:
    """``market-digest:<date>:<AM|PM>`` -- which run first saw a note."""

    match = DIGEST_FILE_RE.fullmatch(path.name)
    if match is None:
        raise SalesNotesError(f"not a market-digest output file: {path.name}")
    return f"market-digest:{match.group(1)}:{match.group(2)}"


def digest_files(directory: str | Path) -> list[Path]:
    """Every digest run in the directory, oldest first.

    Ascending order is the whole dedup rule: a note carried forward into the
    next run is the same note, and the run that first published it is the one
    its provenance names.
    """

    root = Path(directory).expanduser()
    if not root.is_dir():
        raise SalesNotesError("sales-notes feed directory is missing")
    return sorted(
        (path for path in root.iterdir()
         if path.is_file() and DIGEST_FILE_RE.fullmatch(path.name)),
        key=lambda path: path.name,
    )


def _sent_at(raw_date: Any) -> str:
    """The mail's own Date header in UTC.

    Mail dates are RFC 2822 with an offset and sometimes a trailing zone
    abbreviation. They are kept as an instant, not a day, because two notes
    from the same desk on the same morning are ordered by it.
    """

    if not isinstance(raw_date, str) or not raw_date.strip():
        raise SalesNotesError("sales note has no date header")
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError) as exc:
        raise SalesNotesError("sales note date header is unparseable") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sender(raw_from: Any) -> tuple[str, str, str]:
    if not isinstance(raw_from, str) or not raw_from.strip():
        raise SalesNotesError("sales note has no sender")
    _, address = parseaddr(raw_from)
    if "@" not in address:
        raise SalesNotesError("sales note sender is not an address")
    return raw_from.strip(), address.lower(), address.rsplit("@", 1)[1].lower()


def _note_header(raw: Mapping[str, Any], *, source: Path) -> dict[str, Any]:
    missing = _REQUIRED_EMAIL_FIELDS - set(raw)
    if missing:
        raise SalesNotesError(
            f"digest email is missing {sorted(missing)}; the feed shape changed"
        )
    body = raw["body"]
    if not isinstance(body, str):
        raise SalesNotesError("digest email body must be text")
    display, address, domain = _sender(raw["from"])
    return {
        "note_id": note_ref(str(raw["id"]).strip().lower()),
        "sender": display,
        "sender_address": address,
        "sender_domain": domain,
        "subject": str(raw["subject"]),
        "sent_at": _sent_at(raw["date"]),
        "is_priority": bool(raw["is_priority"]),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_chars": len(body),
        "digest_ref": digest_ref_of(source),
        "evidence_tier": EVIDENCE_TIER,
        # Three banks, and every one of these arrives from a person or a named
        # desk. The flag is here so the claim index never has to infer it.
        "analyst_named": True,
    }


def _emails(path: Path) -> Iterator[Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SalesNotesError(f"digest run {path.name} is unreadable") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("emails"), list):
        raise SalesNotesError(f"digest run {path.name} carries no emails array")
    for item in payload["emails"]:
        if not isinstance(item, Mapping):
            raise SalesNotesError(f"digest run {path.name} has a malformed email")
        yield item


def enumerate_notes(
    directory: str | Path,
    *,
    since: str,
    sender_domain: str | None = None,
    limit: int = MAX_NOTES,
) -> list[dict[str, Any]]:
    """Every distinct note sent on or after ``since``, oldest run first.

    One pass over the runs in ascending order, first occurrence wins. That is
    what makes a second run over the same directory produce the same refs and
    the same ``digest_ref`` -- the feed grows at the end, so re-reading it is
    not re-discovering it.
    """

    if not isinstance(since, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) is None:
        raise SalesNotesError("since must be a YYYY-MM-DD date")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_NOTES:
        raise SalesNotesError(f"limit must be 1..{MAX_NOTES}")
    domain = sender_domain.lower().strip() if isinstance(sender_domain, str) else None
    seen: set[str] = set()
    notes: list[dict[str, Any]] = []
    for path in digest_files(directory):
        for raw in _emails(path):
            header = _note_header(raw, source=path)
            if header["note_id"] in seen:
                continue
            seen.add(header["note_id"])
            if header["sent_at"][:10] < since:
                continue
            if domain and header["sender_domain"] != domain:
                continue
            notes.append(header)
            if len(notes) >= limit:
                return notes
    return notes


def read_note(
    directory: str | Path, note_id: str, *, digest_ref: str | None = None
) -> tuple[dict[str, Any], str]:
    """One note's header and its verbatim body.

    The header is rebuilt from the same run ``list_notes`` would name, so the
    ``body_sha256`` an enumeration published is the hash of the bytes this
    returns.

    ``digest_ref`` is the run an enumeration said first published the note. It
    is a hint and not a key: with it this opens one file, without it it walks
    every run in the same ascending order the enumeration used, and a hint
    that turns out not to hold the note falls back to that walk rather than
    reporting a note that exists as missing. The difference matters at scale
    -- a body-attribution pass over a six-month archive is a few thousand of
    these, and a scan each would make it quadratic.
    """

    if not isinstance(note_id, str) or not note_id.startswith(DOCUMENT_REF_PREFIX):
        raise SalesNotesError(f"note id must start with {DOCUMENT_REF_PREFIX}")
    paths = digest_files(directory)
    if isinstance(digest_ref, str) and digest_ref.strip():
        hinted = [path for path in paths if digest_ref_of(path) == digest_ref.strip()]
        found = _find_note(hinted, note_id)
        if found is not None:
            return found
    found = _find_note(paths, note_id)
    if found is None:
        raise SalesNotesError("no sales note with that id is in the feed")
    return found


def _find_note(paths: Sequence[Path], note_id: str) -> tuple[dict[str, Any], str] | None:
    for path in paths:
        for raw in _emails(path):
            if f"{DOCUMENT_REF_PREFIX}{str(raw.get('id', '')).strip().lower()}" != note_id:
                continue
            return _note_header(raw, source=path), str(raw["body"])
    return None


__all__ = [
    "CAPABILITY_BY_OPERATION",
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
    "MAX_NOTES",
    "OPERATIONS",
    "SIDE_EFFECT",
    "SOURCE_REF",
    "SalesNotesError",
    "WIRE_SCHEMA_VERSION",
    "build_sales_notes_governance_record",
    "digest_files",
    "digest_ref_of",
    "enumerate_notes",
    "note_ref",
    "read_note",
    "sales_notes_adapter_hash",
    "sales_notes_contract",
    "sales_notes_fixture_hash",
    "sales_notes_identity",
    "sales_notes_permissions",
    "sales_notes_schema_hash",
    "sales_notes_source_hash",
]
