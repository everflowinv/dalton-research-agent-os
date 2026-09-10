"""W3: read the fund's own prior work out of process, into a bounded wire.

Same order as every other connector child, and for the same reasons:
**approval first, artifact always, contract last.**

1. The governance record is checked against the identity of the exact
   operation being run before a manifest is opened. One schema hash binds one
   operation, so the ``list_documents`` approval cannot be used to read a
   document.
2. What was read is hashed into the raw spool before anything is taken out of
   it.
3. The wire is validated against the frozen output schema; anything the
   contract cannot describe is refused rather than stored.
4. ``summary.json`` is written on every path and the exit code follows it.

The one thing this child does that the wiki's does not is carry its refusals
onto the wire. A manifest entry with no date, or a file the manifest names and
the disk does not have, is a row in ``refused`` with its reason. It is on the
wire and not in a log because it is the owner's next action: "give this memo a
date" is a thirty-second fix, and a document that silently failed to enumerate
is a fix nobody knows to make.
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
    PRIOR_RESEARCH_SOURCE_REF,
    build_feed_acquisition_manifest,
)
from .prior_research_core import (
    GET_OPERATION,
    LIST_OPERATION,
    MAX_DOCUMENTS,
    OPERATIONS,
    TEMPLATE_KEY,
    WIRE_SCHEMA_VERSION,
    PriorResearchError,
    enumerate_documents,
    prior_research_identity,
    read_document,
)
from .raw_spool import RawSpool
from .store import canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
MAX_RAW_BYTES = 64 * 1024 * 1024

TARGET_REF = "host-tool:prior-research-corpus"


class PriorResearchRunError(RuntimeError):
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
        raise PriorResearchRunError("prior-research governance record is not approved")
    identity = prior_research_identity(operation)
    if governance.capability_id != identity["capability_id"]:
        raise PriorResearchRunError(
            f"governance record covers a different capability than {operation}"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise PriorResearchRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise PriorResearchRunError(
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
    raise PriorResearchRunError(f"packaged template has no {operation} output contract")


def _spool(state: Path, spool_dir: Path | None) -> RawSpool:
    root = _secure_dir(spool_dir if spool_dir is not None else state / DEFAULT_SPOOL_NAME)
    return RawSpool(str(root), max_total_bytes=1_000_000_000)


def _spool_bytes(spool: RawSpool, payload: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
    sink.write(payload)
    return sink.finalize().to_dict()


def _subject_tickers(company: str) -> list[str]:
    """The company folder as a ticker, when it is one.

    A folder called ``ACN`` is Accenture. A folder called ``acn-accenture`` is
    a folder, and this returns nothing rather than inventing a ticker from it:
    the manifest's job is to bind a document, and a wrong attribution is worse
    than an absent one.
    """

    candidate = company.strip().upper()
    if candidate and candidate.replace("-", "").isalnum() and candidate[0].isalpha():
        if candidate == company.strip():
            return [candidate]
    return []


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = (
        Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    )
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "source_ref": PRIOR_RESEARCH_SOURCE_REF,
        "operation": args.operation,
        "transport": "host-tool",
        "since": args.since,
        "until": args.until,
        "company": args.company,
        "truncated": False,
        "document_ref": args.document_id,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "manifest_ref": None,
        "manifest_hash": None,
        "document_count": 0,
        "doc_kinds": {},
        "refused": [],
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
            documents, refused, truncated = enumerate_documents(
                args.corpus_root, since=args.since, until=args.until,
                company=args.company, limit=args.limit,
            )
            artifact = _spool_bytes(spool, canonical_json(documents).encode("utf-8"))
            summary["artifact"] = artifact
            wire = {
                "schema_version": WIRE_SCHEMA_VERSION,
                "since": args.since,
                "until": args.until,
                "company": args.company,
                "documents": documents,
                "document_count": len(documents),
                "refused": refused,
                "refused_count": len(refused),
                "truncated": truncated,
                "source_record_refs": [item["document_id"] for item in documents],
                "next_cursor": (
                    documents[-1]["as_of"] if truncated and documents else None
                ),
                "provider_status": 200,
            }
            summary["truncated"] = truncated
            kinds: dict[str, int] = {}
            for item in documents:
                kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
            summary["document_count"] = len(documents)
            summary["doc_kinds"] = dict(sorted(kinds.items()))
            summary["refused"] = refused
        else:
            header, text = read_document(args.corpus_root, args.document_id)
            artifact = _spool_bytes(
                spool, canonical_json({"document": header}).encode("utf-8")
            )
            assembled = _spool_bytes(spool, text.encode("utf-8"))
            summary["artifact"] = artifact
            manifest = build_feed_acquisition_manifest(
                created_at=summary["created_at"],
                source_ref=PRIOR_RESEARCH_SOURCE_REF,
                operation=GET_OPERATION,
                document_ref=header["document_id"],
                target_ref=TARGET_REF,
                governance_ref=governance.id,
                governance_hash=governance.content_hash,
                doc_kind=header["kind"],
                evidence_tier=header["evidence_tier"],
                # The manifest's date, which is the document's date. Not the
                # day the file was read: that is today for a memo from 2024.
                doc_date=header["as_of"],
                origin_ref=f"prior-research:{header['company']}:{header['kind']}",
                subject_tickers=_subject_tickers(header["company"]),
                text=text,
                assembled_object=assembled,
                connector_invocation_ref=args.connector_invocation_ref,
                connector_invocation_hash=args.connector_invocation_hash,
            )
            _write_owner_only(summary_dir / "manifest.json", manifest)
            summary["manifest_ref"] = manifest["id"]
            summary["manifest_hash"] = manifest["content_hash"]
            summary["document_ref"] = header["document_id"]
            summary["document_count"] = 1
            summary["doc_kinds"] = {header["kind"]: 1}
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
    except (PriorResearchRunError, PriorResearchError, ConnectorGovernanceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    _write_owner_only(summary_dir / "summary.json", summary)
    return summary


# -- the two imports an owner runs by hand ------------------------------
#
# Deliberately owner-run rather than part of the tick. Reading the corpus is
# automation's job -- it is a governed source like any other -- but *promoting*
# one of those documents to version zero of a company's chain, or to a prior
# model, is a statement about which document is the fund's earlier view of this
# company. That is a judgement, and ADR-0008's split says the storage layer
# does not make it. So the lane files everything as Claims, and these two
# commands are how a person says "that one is the screen".


def import_screen(args: argparse.Namespace) -> dict[str, Any]:
    """Store one prior document as version 0 of a company's screen chain."""

    from .coverage_mission import CoverageMissionAuthority
    from .mission_deliverable import MissionDeliverableAuthority
    from .prior_screen_import import import_prior_screen
    from .research_playbook import ResearchPlaybookAuthority
    from .store import DaltonStore

    header, text = read_document(args.corpus_root, args.document_id)
    if header["kind"] != "initial_screen":
        raise PriorResearchRunError(
            f"{args.document_id} is filed as {header['kind']!r}; only a document "
            "the manifest calls an initial_screen becomes version zero"
        )
    store = DaltonStore(str(Path(args.db).expanduser().resolve()))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            raise PriorResearchRunError("this Core holds no coverage mission")
        mission = missions.mission(pointer["mission_version_id"])
        playbooks = ResearchPlaybookAuthority(store)
        playbook = playbooks.playbook(
            mission["bindings"]["playbook_version"]["ref"]
        )
        record = import_prior_screen(
            MissionDeliverableAuthority(store), mission=mission, playbook=playbook,
            company_ref=args.company_ref, document=header, text=text,
            actor_ref=args.actor_ref,
        )
    finally:
        store.close()
    return {
        "operation": "import_screen", "status": record["status"],
        "version_ref": record["id"], "version": record["version"],
        "company_ref": args.company_ref, "document_ref": header["document_id"],
        "as_of": header["as_of"],
    }


def import_model(args: argparse.Namespace) -> dict[str, Any]:
    """Store one prior workbook as the next PriorModelVersion of its chain."""

    from .prior_model_import import (
        PriorModelAuthority,
        read_workbook,
        workbook_digest,
    )
    from .store import DaltonStore

    header, _ = read_document(args.corpus_root, args.document_id)
    if header["kind"] != "model_excel":
        raise PriorResearchRunError(
            f"{args.document_id} is filed as {header['kind']!r}; only a document "
            "the manifest calls a model_excel becomes a PriorModelVersion"
        )
    root = Path(args.corpus_root).expanduser().resolve()
    workbook = root / header["company"] / header["relative_path"]
    store = DaltonStore(str(Path(args.db).expanduser().resolve()))
    try:
        record = PriorModelAuthority(store).publish(
            company_ref=args.company_ref,
            source_document_ref=header["document_id"],
            as_of=header["as_of"],
            workbook_sha256=workbook_digest(workbook),
            assumptions=read_workbook(workbook),
            actor_ref=args.actor_ref,
            note=header["source_note"],
        )
    finally:
        store.close()
    return {
        "operation": "import_model", "status": record["status"],
        "version_ref": record["id"], "version": record["version"],
        "company_ref": args.company_ref, "document_ref": header["document_id"],
        "as_of": header["as_of"], "assumption_count": record["assumption_count"],
    }


def build_import_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Promote one read prior document to a versioned output."
    )
    parser.add_argument("command", choices=("import-screen", "import-model"))
    parser.add_argument("--db", required=True, help="the Core database")
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument(
        "--actor-ref", default="human:coverage-owner",
        help="who is saying this is the fund's earlier view of this company",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved record for the chosen operation")
    parser.add_argument("--corpus-root", required=True,
                        help="the declared prior-research directory "
                             "(DALTON_PRIOR_RESEARCH_DIR), one folder per company")
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--since", default=None, help="YYYY-MM-DD, list_documents only")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD, list_documents only")
    parser.add_argument("--company", default=None,
                        help="one company folder; omit to read every manifest")
    parser.add_argument("--limit", type=int, default=MAX_DOCUMENTS)
    parser.add_argument("--document-id", default=None,
                        help="prior-research-doc:sha256:<hash>, get_document only")
    parser.add_argument("--connector-invocation-ref", default=None,
                        help="the invocation the host-tool runner registered")
    parser.add_argument("--connector-invocation-hash", default=None)
    parser.add_argument("--spool-dir", type=Path, default=None)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--quiet", action="store_true")
    # The host-tool runner treats this child's stdout as the raw response:
    # exactly the closed observation wire and nothing else, so the bytes it
    # hashes into the spool are the bytes it validates.
    parser.add_argument("--emit-wire", action="store_true",
                        help="print the closed observation wire on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in ("import-screen", "import-model"):
        args = build_import_parser().parse_args(raw)
        try:
            outcome = (import_screen if args.command == "import-screen"
                       else import_model)(args)
        except (PriorResearchRunError, PriorResearchError) as exc:
            print(json.dumps({"status": "failed",
                              "failure_reason": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False, indent=1))
            return 1
        print(json.dumps(outcome, ensure_ascii=False, indent=1))
        return 0
    parser = build_parser()
    args = parser.parse_args(raw)
    if args.operation == LIST_OPERATION:
        if not args.since or not args.until:
            parser.error("--since and --until are required for list_documents")
        if args.document_id:
            parser.error("--document-id is not a list_documents argument")
    else:
        if not args.document_id:
            parser.error("--document-id is required for get_document")
        if args.since or args.until or args.company:
            parser.error(
                "--since, --until and --company are not get_document arguments"
            )
    summary = run(args)
    if summary["status"] == "succeeded" and args.emit_wire:
        print(canonical_json(summary["observation"]))
    elif not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "operation", "document_count",
            "doc_kinds", "refused", "manifest_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "PriorResearchRunError",
    "build_import_parser",
    "build_parser",
    "import_model",
    "import_screen",
    "main",
    "run",
]
