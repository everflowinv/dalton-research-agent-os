"""Acquire one AlphaEngine document into a Core state directory.

This is the importable form of ``scripts/run_isolated_alphaengine_core_acquisition_canary.py``.
It runs in its own process on purpose: ``ConnectorTransportExecutor`` bounds
every provider call with a ``SIGALRM`` watchdog that only works on a process
main thread, so the writer service launches this program instead of running
the acquisition on one of its worker threads.  SQLite carries the shared
state -- the writer keeps its own connection open and this process opens a
second one on the same ``core.sqlite`` with the usual busy timeout.

Two modes:

* ``--fake-document-file PATH``: page through an in-process stand-in for the
  host-owned MCP handle.  No network, no governance approval needed when
  ``--governance-approved-by human:<who>`` builds an in-memory approved record.
* ``--allow-network``: call the loopback AlphaEngine MCP endpoint.  Requires
  the committed governance record to be ``approved``; the in-memory rehearsal
  record is refused.

Outputs (``--summary-dir``, owner-only): ``summary.json`` and ``manifest.json``.
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

from .alphaengine_core_acquisition import (
    AlphaEngineCoreAcquisition,
    StaticConnectorGovernance,
    build_governance_record,
    core_transcript_authority_probe,
)
from .capability_catalog import CapabilityCatalog
from .connector import ConnectorStore
from .observability import ObservabilityStore
from .openclaw_connector_bridge import (
    HostToolInvocationResult,
    LoopbackStreamableHttpMcpHandle,
)
from .raw_spool import RawSpool
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json


DEFAULT_MCP_ENDPOINT = "http://127.0.0.1:8950/mcp"
SUMMARY_SCHEMA_VERSION = "0.1"


class FakeDocumentHandle:
    """Host-owned loopback stand-in that serves exact contiguous pages."""

    def __init__(self, text: str, page_chars: int) -> None:
        self.text = text
        self.page_chars = page_chars
        self.calls = 0

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        del call_ref, deadline_at, max_response_bytes
        if tool_name != "get_document":
            raise RuntimeError("fake handle serves get_document only")
        self.calls += 1
        offset = int(arguments["offset"])
        limit = min(self.page_chars, int(arguments["max_chars"]))
        page = self.text[offset:offset + limit]
        next_offset = offset + len(page)
        complete = next_offset >= len(self.text)
        payload = {
            "metadata": {"doc_id": arguments["doc_id"], "title": "fake document"},
            "content_chars": len(self.text),
            "content_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "offset": offset,
            "returned_chars": len(page),
            "text": page,
            "next_offset": None if complete else next_offset,
            "complete": complete,
        }
        result = {"content": [{"type": "text", "text": canonical_json(payload)}]}
        request_id = f"provider-request:fake:{self.calls}"
        raw = canonical_json(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        ).encode("utf-8")
        return HostToolInvocationResult(request_id=request_id, raw_response=raw, result=result)


def secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def run_acquisition(
    *,
    document_ref: str,
    state_dir: Path,
    governance: StaticConnectorGovernance,
    handle: Any,
    transport: str,
    summary_dir: Path,
    max_pages: int = 20,
    expected_content_sha256: str | None = None,
    catalog_db: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one governed acquisition and write ``summary.json`` / ``manifest.json``.

    Returns ``(summary, manifest)``.  The caller decides the exit status from
    ``summary["transcript_authority_probe"]["ok"]`` and
    ``summary["expected_digest_match"]``.
    """

    state = secure_dir(state_dir)
    out = secure_dir(summary_dir)
    core = DaltonStore(str(state / "core.sqlite"))
    connectors = ConnectorStore(core)
    observability = ObservabilityStore(core)
    journal = RunnerJournal(core)
    scheduler = Scheduler(
        str(state / "scheduler.sqlite"), default_lease_seconds=30, max_lease_seconds=60
    )
    catalog = CapabilityCatalog(
        str(catalog_db if catalog_db is not None else state / "catalog.sqlite"),
        approval_resolver=governance.approval,
        policy_resolver=governance.policy,
    )
    spool = RawSpool(str(secure_dir(state / "connector-spool")), max_total_bytes=1_000_000_000)
    acquisition = AlphaEngineCoreAcquisition(
        store=core,
        connectors=connectors,
        observability=observability,
        journal=journal,
        scheduler=scheduler,
        catalog=catalog,
        spool=spool,
        governance=governance,
        mcp_handle=handle,
    )
    try:
        plan = acquisition.build_plan(document_ref, max_pages=max_pages)
        result = acquisition.acquire(plan)
        manifest = result["manifest"]
        probe = core_transcript_authority_probe(core, manifest)
        counts = {
            table: int(core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "connector_source_envelopes", "connector_invocations",
                "connector_physical_attempts", "observability_artifact_versions_v2",
                "evidence_versions", "claim_versions", "thesis_versions",
            )
        }
        assembled = manifest["assembled_object"]
        digest_match = (
            None if expected_content_sha256 is None
            else assembled is not None
            and assembled["content_hash"] == expected_content_sha256
        )
        summary = {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "transport": transport,
            "governance_ref": governance.id,
            "governance_hash": governance.content_hash,
            "governance_status": governance.status,
            "plan_ref": plan["id"],
            "plan_hash": plan["content_hash"],
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "manifest_status": manifest["status"],
            "document_ref": manifest["document_ref"],
            "content_chars": manifest["content_chars"],
            "assembled_content_sha256": None if assembled is None else assembled["content_hash"],
            "expected_content_sha256": expected_content_sha256,
            "expected_digest_match": digest_match,
            "page_count": len(manifest["pages"]),
            "physical_calls": manifest["physical_calls"],
            "document_quota_units": manifest["document_quota_units"],
            "provider_calls": result["provider_calls"],
            "replayed_pages": result["replayed_pages"],
            "core_counts": counts,
            "transcript_authority_probe": probe,
            "production_activated": False,
            "formal_authority_writes": 0,
        }
        _write_owner_only(out / "summary.json", summary)
        _write_owner_only(out / "manifest.json", manifest)
        return summary, manifest
    finally:
        catalog.close()
        scheduler.close()
        core.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--document-ref", required=True, help="alphaengine-doc:<id>")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--governance", type=Path)
    parser.add_argument(
        "--governance-approved-by",
        help="build an in-memory approved governance record for an isolated rehearsal",
    )
    parser.add_argument("--fake-document-file", type=Path)
    parser.add_argument("--fake-page-chars", type=int, default=30_000)
    parser.add_argument("--mcp-endpoint", default=DEFAULT_MCP_ENDPOINT)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--mcp-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--expected-content-sha256")
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--catalog-db", type=Path, help="defaults to <state>/catalog.sqlite")
    parser.add_argument("--quiet", action="store_true", help="do not print the summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.fake_document_file is None and not args.allow_network:
        parser.error("choose --fake-document-file or --allow-network")
    if args.fake_document_file is not None and args.allow_network:
        parser.error("--fake-document-file and --allow-network are mutually exclusive")
    if args.governance_approved_by and args.allow_network:
        parser.error(
            "networked acquisition requires the committed approved governance record; "
            "--governance-approved-by is rehearsal-only"
        )
    if args.governance_approved_by is None and args.governance is None:
        parser.error("--governance is required unless --governance-approved-by is used")

    if args.governance_approved_by:
        governance = StaticConnectorGovernance(
            build_governance_record(
                approved_by=args.governance_approved_by, status="approved"
            )
        )
    else:
        governance = StaticConnectorGovernance.load(args.governance)

    if args.fake_document_file is not None:
        handle: Any = FakeDocumentHandle(
            args.fake_document_file.read_text(encoding="utf-8"), args.fake_page_chars
        )
        transport = "fake"
    else:
        handle = LoopbackStreamableHttpMcpHandle(
            args.mcp_endpoint,
            allowed_tools={"get_document": "get_document"},
            timeout_seconds=args.mcp_timeout_seconds,
        )
        transport = "loopback-mcp"

    summary, _manifest = run_acquisition(
        document_ref=args.document_ref,
        state_dir=args.state_dir,
        governance=governance,
        handle=handle,
        transport=transport,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        max_pages=args.max_pages,
        expected_content_sha256=args.expected_content_sha256,
        catalog_db=args.catalog_db,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    probe_ok = bool(summary["transcript_authority_probe"].get("ok"))
    return 0 if probe_ok and summary["expected_digest_match"] in {None, True} else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
