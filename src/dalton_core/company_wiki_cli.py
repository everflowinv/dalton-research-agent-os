"""S1: read the company wiki out of process, into a bounded wire.

Same order as every other connector child, and for the same reasons:
**approval first, artifact always, contract last.**

1. The governance record is checked against the identity of the exact
   operation being run before the index is opened. One schema hash binds one
   operation, so the ``list_documents`` approval cannot be used to read a
   document.
2. What was read is hashed into the raw spool before anything is taken out of
   it.
3. The wire is validated against the frozen output schema; anything the
   contract cannot describe is refused rather than stored.
4. ``summary.json`` is written on every path and the exit code follows it.

The index is opened read-only through the non-provisioning reader: no WAL is
created, no page is written, and a wiki that is mid-write is a failed run
rather than a corrupted one.
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

from .company_wiki_core import (
    GET_OPERATION,
    LIST_OPERATION,
    MAX_DOCUMENTS,
    OPERATIONS,
    TEMPLATE_KEY,
    WIRE_SCHEMA_VERSION,
    CompanyWikiError,
    company_wiki_identity,
    enumerate_documents,
    read_document,
)
from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_inventory import load_packaged_connector_inventory
from .feed_acquisition import (
    COMPANY_WIKI_SOURCE_REF,
    build_feed_acquisition_manifest,
)
from .raw_spool import RawSpool
from .store import canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
MAX_RAW_BYTES = 64 * 1024 * 1024


class CompanyWikiRunError(RuntimeError):
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
    governance = ConnectorGovernance.load(path)
    if not governance.approved:
        raise CompanyWikiRunError("company-wiki governance record is not approved")
    identity = company_wiki_identity(operation)
    if governance.capability_id != identity["capability_id"]:
        raise CompanyWikiRunError(
            f"governance record covers a different capability than {operation}"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise CompanyWikiRunError("governance source hash differs from the packaged template")
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise CompanyWikiRunError(
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
    raise CompanyWikiRunError(f"packaged template has no {operation} output contract")


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
        "source_ref": COMPANY_WIKI_SOURCE_REF,
        "operation": args.operation,
        "transport": "host-tool",
        "since": args.since,
        "company": args.company,
        "industry": args.industry,
        "document_ref": args.document_id,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "manifest_ref": None,
        "manifest_hash": None,
        "document_count": 0,
        "doc_types": {},
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
            documents = enumerate_documents(
                args.index_db, args.corpus_root, since=args.since,
                company=args.company, industry=args.industry, limit=args.limit,
            )
            artifact = _spool_bytes(spool, canonical_json(documents).encode("utf-8"))
            summary["artifact"] = artifact
            wire = {
                "schema_version": WIRE_SCHEMA_VERSION,
                "since": args.since,
                "company": args.company,
                "industry": args.industry,
                "documents": documents,
                "document_count": len(documents),
                "source_record_refs": [item["document_id"] for item in documents],
                "next_cursor": None,
                "provider_status": 200,
            }
            kinds: dict[str, int] = {}
            for item in documents:
                kinds[item["doc_type_key"]] = kinds.get(item["doc_type_key"], 0) + 1
            summary["document_count"] = len(documents)
            summary["doc_types"] = dict(sorted(kinds.items()))
        else:
            header, text = read_document(
                args.index_db, args.corpus_root, args.document_id
            )
            artifact = _spool_bytes(
                spool, canonical_json({"document": header}).encode("utf-8")
            )
            assembled = _spool_bytes(spool, text.encode("utf-8"))
            summary["artifact"] = artifact
            manifest = build_feed_acquisition_manifest(
                created_at=summary["created_at"],
                source_ref=COMPANY_WIKI_SOURCE_REF,
                operation=GET_OPERATION,
                document_ref=header["document_id"],
                target_ref="host-tool:company-wiki-corpus",
                governance_ref=governance.id,
                governance_hash=governance.content_hash,
                doc_kind=header["doc_type_key"],
                evidence_tier=header["evidence_tier"],
                doc_date=header["doc_date"],
                origin_ref=(
                    f"company-wiki:{header['category_type']}:{header['category_name']}"
                ),
                # The corpus's own tags, not a guess from the text. An
                # industry note arrives here with an empty list and keeps it.
                subject_tickers=list(header["company_tags"]),
                text=text,
                assembled_object=assembled,
            )
            _write_owner_only(summary_dir / "manifest.json", manifest)
            summary["manifest_ref"] = manifest["id"]
            summary["manifest_hash"] = manifest["content_hash"]
            summary["document_ref"] = header["document_id"]
            summary["document_count"] = 1
            summary["doc_types"] = {header["doc_type_key"]: 1}
            wire = {
                "schema_version": WIRE_SCHEMA_VERSION,
                "document": header,
                "text": text,
                "source_record_refs": [header["document_id"]],
                "next_cursor": None,
                "provider_status": 200,
            }

        from .authority_resolver import _schema_matches

        _schema_matches(wire, _output_schema(args.operation), "output")
        summary["status"] = "succeeded"
        summary["observation"] = wire
    except (CompanyWikiRunError, CompanyWikiError, ConnectorGovernanceError) as exc:
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
    parser.add_argument("--index-db", required=True, help="the wiki's SQLite index")
    parser.add_argument("--corpus-root", required=True,
                        help="root the index's filepaths are relative to")
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--since", default=None, help="YYYY-MM-DD, list_documents only")
    parser.add_argument("--company", default=None, help="ticker as the corpus tags it")
    parser.add_argument("--industry", default=None, help="sector slug as the corpus tags it")
    parser.add_argument("--limit", type=int, default=MAX_DOCUMENTS)
    parser.add_argument("--document-id", default=None,
                        help="company-wiki-doc:sha256:<hash>, get_document only")
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
        if not args.since:
            parser.error("--since is required for list_documents")
        if args.document_id:
            parser.error("--document-id is not a list_documents argument")
    else:
        if not args.document_id:
            parser.error("--document-id is required for get_document")
        if args.since or args.company or args.industry:
            parser.error("--since, --company and --industry are not get_document arguments")
    summary = run(args)
    if summary["status"] == "succeeded" and args.emit_wire:
        print(canonical_json(summary["observation"]))
    elif not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "operation", "document_count",
            "doc_types", "manifest_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["CompanyWikiRunError", "build_parser", "main", "run"]
