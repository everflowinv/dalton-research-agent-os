"""S1: read the sell-side note feed out of process, into a bounded wire.

Runs as a child for the reason every lane child does: the writer's store
thread cannot be held while a few hundred megabytes of JSON are parsed off
disk, and a feed whose shape changed upstream should fail one child rather
than a request.

Order, and it is the whole design: **approval first, artifact always, contract
last.**

1. The governance record is loaded and checked against the identity it claims
   to cover *before* a file is opened. A record that is merely present is not
   authority; a record whose frozen hashes no longer describe the packaged
   contract is refused here rather than after the read.
2. Whatever was read is canonicalised, hashed and written into the raw spool
   **before** anything is taken out of it. If the wire turns out to violate
   the frozen contract, the bytes are still on disk and recoverable.
3. Only then is the wire built and validated against the frozen output schema.
   An observation the contract cannot describe is refused, not stored and
   explained afterwards.
4. ``summary.json`` is written on every path, success or failure, and the exit
   code is derived from its status. A child that dies without a summary is
   orphaned, which the launcher already knows how to say.

No network. No Gmail. One directory, read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_inventory import load_packaged_connector_inventory
from .feed_acquisition import (
    SALES_NOTES_SOURCE_REF,
    build_feed_acquisition_manifest,
)
from .raw_spool import RawSpool
from .sales_notes_core import (
    GET_OPERATION,
    LIST_OPERATION,
    MAX_NOTES,
    OPERATIONS,
    SalesNotesError,
    TEMPLATE_KEY,
    WIRE_SCHEMA_VERSION,
    enumerate_notes,
    read_note,
    sales_notes_identity,
)
from .store import canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# One enumeration of a six-month window is a few megabytes of headers; one
# note is tens of kilobytes. This is a ceiling, not an expectation.
MAX_RAW_BYTES = 64 * 1024 * 1024
DOC_KIND = "sell_side_note"


class SalesNotesRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_governance(path: Path, operation: str) -> ConnectorGovernance:
    """The approved record for this exact operation, or nothing happens.

    The constructor proves the record is well formed. It does not prove the
    record still describes the packaged contract, and it cannot prove the
    record covers *this* operation rather than the other one on the same
    template -- a schema hash binds one operation, which is exactly what makes
    that check possible here.
    """

    governance = ConnectorGovernance.load(path)
    if not governance.approved:
        raise SalesNotesRunError("sales-notes governance record is not approved")
    identity = sales_notes_identity(operation)
    if governance.capability_id != identity["capability_id"]:
        raise SalesNotesRunError(
            f"governance record covers a different capability than {operation}"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise SalesNotesRunError("governance source hash differs from the packaged template")
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise SalesNotesRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def _output_schema(operation: str) -> dict[str, Any]:
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    ref = f"schema:connector-inventory:{TEMPLATE_KEY}:{operation}:output:0.1"
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise SalesNotesRunError(f"packaged template has no {operation} output contract")


def _spool(state: Path, spool_dir: Path | None) -> RawSpool:
    root = _secure_dir(spool_dir if spool_dir is not None else state / DEFAULT_SPOOL_NAME)
    return RawSpool(str(root), max_total_bytes=1_000_000_000)


def _spool_bytes(spool: RawSpool, payload: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
    sink.write(payload)
    return sink.finalize().to_dict()


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = (
        Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    )
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "source_ref": SALES_NOTES_SOURCE_REF,
        "operation": args.operation,
        "transport": "host-tool",
        "since": args.since,
        "until": args.until,
        "sender_domain": args.sender_domain,
        "truncated": False,
        "digest_ref": args.digest_ref,
        "document_ref": args.note_id,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "manifest_ref": None,
        "manifest_hash": None,
        "note_count": 0,
        "senders": {},
        "observation": None,
    }
    try:
        governance = _load_governance(
            Path(args.governance).expanduser().resolve(), args.operation
        )
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash
        spool = _spool(state, args.spool_dir)

        if args.operation == LIST_OPERATION:
            notes, truncated = enumerate_notes(
                args.digest_dir, since=args.since, until=args.until,
                sender_domain=args.sender_domain, limit=args.limit,
            )
            # The artifact is the enumeration, kept whole and hashed before
            # anything is read out of it.
            artifact = _spool_bytes(spool, canonical_json(notes).encode("utf-8"))
            summary["artifact"] = artifact
            # A truncated listing says so, and says where it stopped: the day
            # of the oldest note it returned. Everything before that day is
            # still unread, and the caller narrows the window and asks again.
            # A null cursor here is a claim that the window is complete.
            wire = {
                "schema_version": WIRE_SCHEMA_VERSION,
                "since": args.since,
                "until": args.until,
                "sender_domain": args.sender_domain,
                "notes": notes,
                "note_count": len(notes),
                "truncated": truncated,
                "source_record_refs": [note["note_id"] for note in notes],
                "next_cursor": notes[-1]["sent_at"][:10] if truncated and notes else None,
                "provider_status": 200,
            }
            summary["truncated"] = truncated
            senders: dict[str, int] = {}
            for note in notes:
                senders[note["sender_domain"]] = senders.get(note["sender_domain"], 0) + 1
            summary["note_count"] = len(notes)
            summary["senders"] = dict(sorted(senders.items()))
        else:
            header, body = read_note(
                args.digest_dir, args.note_id, digest_ref=args.digest_ref
            )
            # Two objects: the note as the source record saw it, and the body
            # bytes the review path will re-read and re-hash. The second is
            # the document; the first is how it arrived.
            artifact = _spool_bytes(
                spool, canonical_json({"note": header}).encode("utf-8")
            )
            assembled = _spool_bytes(spool, body.encode("utf-8"))
            summary["artifact"] = artifact
            manifest = build_feed_acquisition_manifest(
                created_at=summary["created_at"],
                source_ref=SALES_NOTES_SOURCE_REF,
                operation=GET_OPERATION,
                document_ref=header["note_id"],
                target_ref="host-tool:market-digest-output",
                governance_ref=governance.id,
                governance_hash=governance.content_hash,
                doc_kind=DOC_KIND,
                evidence_tier=header["evidence_tier"],
                doc_date=header["sent_at"][:10],
                origin_ref=header["digest_ref"],
                subject_tickers=list(args.ticker or []),
                text=body,
                assembled_object=assembled,
                # The runner registered this invocation before it started this
                # process, so the manifest names it from the start rather than
                # being stamped afterwards -- one write, one hash, one record.
                connector_invocation_ref=args.connector_invocation_ref,
                connector_invocation_hash=args.connector_invocation_hash,
            )
            _write_owner_only(summary_dir / "manifest.json", manifest)
            summary["manifest_ref"] = manifest["id"]
            summary["manifest_hash"] = manifest["content_hash"]
            summary["document_ref"] = header["note_id"]
            summary["note_count"] = 1
            summary["senders"] = {header["sender_domain"]: 1}
            wire = {
                "schema_version": WIRE_SCHEMA_VERSION,
                "note": header,
                "body": body,
                "source_record_refs": [header["note_id"]],
                "next_cursor": None,
                "provider_status": 200,
            }

        # Validate before recording: an observation the frozen contract cannot
        # describe is refused, not stored and explained later.
        from .authority_resolver import _schema_matches

        _schema_matches(wire, _output_schema(args.operation), "output")
        summary["status"] = "succeeded"
        summary["observation"] = wire
    except (SalesNotesRunError, SalesNotesError, ConnectorGovernanceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    _write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved record for the chosen operation")
    parser.add_argument("--digest-dir", required=True,
                        help="the host skill's output directory of digest runs")
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--since", default=None, help="YYYY-MM-DD, list_notes only")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD, list_notes only")
    parser.add_argument("--sender-domain", default=None)
    parser.add_argument("--limit", type=int, default=MAX_NOTES)
    parser.add_argument("--note-id", default=None, help="sales-note:<id>, get_note only")
    parser.add_argument("--digest-ref", default=None,
                        help="locator hint: the run that first published the note")
    parser.add_argument("--ticker", action="append", default=None,
                        help="company this note was queued for; repeatable, may be empty")
    parser.add_argument("--connector-invocation-ref", default=None,
                        help="the invocation the host-tool runner registered")
    parser.add_argument("--connector-invocation-hash", default=None)
    parser.add_argument("--spool-dir", type=Path, default=None)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--quiet", action="store_true")
    # The host-tool runner treats this child's stdout as the raw
    # response: exactly the closed observation wire and nothing else,
    # so the bytes it hashes into the spool are the bytes it validates.
    parser.add_argument("--emit-wire", action="store_true",
                        help="print the closed observation wire on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.operation == LIST_OPERATION:
        if not args.since or not args.until:
            parser.error("--since and --until are required for list_notes")
        if args.note_id or args.digest_ref:
            parser.error("--note-id and --digest-ref are not list_notes arguments")
    else:
        if not args.note_id:
            parser.error("--note-id is required for get_note")
        if args.since or args.until or args.sender_domain:
            parser.error(
                "--since, --until and --sender-domain are not get_note arguments"
            )
    summary = run(args)
    if summary["status"] == "succeeded" and args.emit_wire:
        print(canonical_json(summary["observation"]))
    elif not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "operation", "note_count", "senders",
            "manifest_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["DOC_KIND", "SalesNotesRunError", "build_parser", "main", "run"]
