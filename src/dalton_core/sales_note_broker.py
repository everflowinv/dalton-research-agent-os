"""Which house sent a sales note, read off the mail it arrived in.

Every debate on the live debate maps sat at ``source_independence`` 0/0 and so
could never leave ``candidate``: the independence ladder in ``debate_map``
counts publishers, issuers and web hosts, and a sales note reached it with
none of the three.  The note's own text is no help -- a morning digest quotes
other houses, data vendors and the companies it covers, and "which bank is
this" read out of prose would be a guess.

The mail header is not a guess.  The sales-notes connector records the sender
of every note it serves (``sales_notes_core._note_header``), and that header is
in the raw ``get_note`` response the Core already holds: the source envelope
names the raw artefact, the artefact names its content hash, and the bytes in
the spool hash to it.  The sending *domain* identifies the house exactly
(``mail.marquee.gs.com`` is Goldman Sachs, ``bofa.com`` is BofA), so the broker
is a lookup in a frozen table, never a model call and never a pattern over the
body.  A domain that is not in the table yields no broker, and the note stays
unattributed rather than being assigned to somebody.

**Where it is kept.**  The Claim and Evidence contracts are frozen, and the
feed acquisition manifest is a hashed contract too, so neither gains a field.
The broker goes where AlphaEngine's publisher already goes: one append-only
``document_provenance_records`` row per document (``extraction_backlog``), the
table the independence ladder is meant to read.  Only the facts the header
states are recorded -- no title (an email subject naming a company would make
the note look like that company's own document to the subject checks), no date
(the acquisition manifest already dates the note, and a second date would move
the extraction context), no company list.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from .document_provenance import TIER_SALES_NOTE, broker_key

SOURCE_REF = "source:sales-notes"
SPEC_REF = "sales-notes"
BROKER_BASIS = "sender_domain"
DEFAULT_LIMIT = 500

# Sending domain -> the house, as its name is spelled in AlphaEngine's
# ``sources`` field where the house appears there too ("Goldman Sachs",
# "Morgan Stanley", "J.P.Morgan"), so that ``broker_key`` folds a sales note and
# a research report from one bank to one key.  A sender matches a row when its
# domain *is* the row's domain or ends with "." + it: ``mail.marquee.gs.com`` is
# gs.com, ``globalresearch.bofa.com`` is bofa.com, ``notgs.com`` is nobody.
SENDER_DOMAIN_BROKERS: tuple[tuple[str, str], ...] = (
    ("gs.com", "Goldman Sachs"),
    ("goldmansachs.com", "Goldman Sachs"),
    ("bofa.com", "BofA Securities"),
    ("bankofamerica.com", "BofA Securities"),
    ("ml.com", "BofA Securities"),
    ("jefferies.com", "Jefferies"),
    ("morganstanley.com", "Morgan Stanley"),
    ("ms.com", "Morgan Stanley"),
    ("jpmorgan.com", "J.P.Morgan"),
    ("jpmchase.com", "J.P.Morgan"),
    ("citi.com", "Citi"),
    ("ubs.com", "UBS"),
    ("barclays.com", "Barclays"),
)


def broker_for_sender_domain(domain: Any) -> str | None:
    """The house a sending domain belongs to, or None when the table does not say."""

    if not isinstance(domain, str):
        return None
    folded = domain.strip().lower().rstrip(".")
    if not folded or "@" in folded:
        return None
    for known, broker in SENDER_DOMAIN_BROKERS:
        if folded == known or folded.endswith("." + known):
            return broker
    return None


def note_header(raw: bytes | str | None) -> dict[str, Any] | None:
    """The ``note`` header of one raw ``get_note`` response, or None."""

    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        wire = json.loads(raw)
    except (TypeError, ValueError):
        return None
    note = wire.get("note") if isinstance(wire, Mapping) else None
    if not isinstance(note, Mapping):
        return None
    if not isinstance(note.get("note_id"), str) or not isinstance(note.get("sender_domain"), str):
        return None
    return dict(note)


def sales_note_provenance(header: Mapping[str, Any]) -> dict[str, Any]:
    """The provenance row one note header supports."""

    from .document_provenance import SCHEMA_VERSION

    domain = str(header["sender_domain"]).strip().lower()
    broker = broker_for_sender_domain(domain)
    return {
        "schema_version": SCHEMA_VERSION,
        "document_ref": str(header["note_id"]),
        "source_ref": SOURCE_REF,
        "spec_ref": SPEC_REF,
        "provenance_tier": TIER_SALES_NOTE,
        "broker": broker,
        "broker_key": (broker_key(broker) or None) if broker else None,
        "broker_basis": BROKER_BASIS,
        "sender_domain": domain,
        "title": None,
        "authors": None,
        "sources": broker,
        "named_companies": [],
        "published_at": None,
        "metadata_seen": True,
    }


_ENVELOPES_SQL = (
    "SELECT e.record_json AS envelope, a.artifact_content_hash AS object_hash "
    "FROM connector_source_envelopes e "
    "JOIN observability_artifact_versions_v2 a "
    "  ON a.version_id=json_extract(e.record_json,'$.raw_artifact_version_ref') "
    "WHERE json_extract(e.record_json,'$.source')=? "
    "  AND json_extract(e.record_json,'$.operation')='get_note' "
    "  AND NOT EXISTS (SELECT 1 FROM document_provenance_records p "
    "    WHERE p.document_ref=json_extract(e.record_json,'$.source_record_refs[0]')) "
    "ORDER BY json_extract(e.record_json,'$.retrieved_at') DESC LIMIT ?"
)


def backfill_sales_note_provenance(
    connection: sqlite3.Connection, spool: Any, *, limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Record the broker of every served sales note that has no provenance row yet.

    Bounded, idempotent and best effort, like ``backfill_provenance``: a note
    already recorded is not selected again, an artefact the spool no longer
    holds is one ``unreadable`` count, and nothing here can stop the child it
    runs beside.  The bytes are re-hashed against the hash the Core recorded
    for them, and the header must name the note the envelope says it served.
    """

    from .extraction_backlog import DocumentProvenanceStore, ExtractionBacklogError

    result: dict[str, Any] = {"envelopes": 0, "recorded": 0, "duplicate": 0,
                              "unreadable": 0, "unattributed": 0, "refused": 0,
                              "brokers": {}}
    if spool is None:
        return {**result, "status": "no_spool"}
    store = DocumentProvenanceStore(connection)
    try:
        rows = connection.execute(_ENVELOPES_SQL, (SOURCE_REF, int(limit))).fetchall()
    except sqlite3.Error:
        return {**result, "status": "no_sales_note_envelopes"}
    seen: set[str] = set()
    for row in rows:
        result["envelopes"] += 1
        try:
            envelope = json.loads(row["envelope"])
        except (TypeError, ValueError):
            result["refused"] += 1
            continue
        refs = envelope.get("source_record_refs") or []
        document_ref = refs[0] if refs and isinstance(refs[0], str) else None
        if document_ref is None or document_ref in seen:
            continue
        try:
            raw = spool.read_object(row["object_hash"])
        except Exception:  # noqa: BLE001 - a pruned artefact is not a failure here
            result["unreadable"] += 1
            continue
        if hashlib.sha256(raw).hexdigest() != row["object_hash"]:
            result["unreadable"] += 1
            continue
        header = note_header(raw)
        if header is None or header["note_id"] != document_ref:
            result["refused"] += 1
            continue
        seen.add(document_ref)
        record = sales_note_provenance(header)
        if record["broker"] is None:
            result["unattributed"] += 1
        try:
            outcome = store.record(record)
        except ExtractionBacklogError:
            result["refused"] += 1
            continue
        status = outcome.get("status")
        if status == "fresh":
            result["recorded"] += 1
            if record["broker"]:
                result["brokers"][record["broker"]] = result["brokers"].get(record["broker"], 0) + 1
        elif status == "duplicate":
            result["duplicate"] += 1
    return {**result, "status": "complete"}


__all__ = [
    "BROKER_BASIS",
    "SENDER_DOMAIN_BROKERS",
    "backfill_sales_note_provenance",
    "broker_for_sender_domain",
    "note_header",
    "sales_note_provenance",
]
