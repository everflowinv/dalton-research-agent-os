#!/usr/bin/env python3
"""Acquire one AlphaEngine document into an *isolated* Core connector authority.

The canary opens its own Core / Scheduler / Catalog SQLite files inside an
owner-only state directory.  It never touches the live Dalton Core, writes no
Evidence, Claim or Thesis, and prints hashes and authority refs only.

Two transports are supported:

* ``--fake-document-file PATH``: serve the file's text as exact contiguous
  pages through an in-process stand-in for the host-owned MCP handle
  (zero network, used for rehearsal);
* ``--mcp-endpoint http://127.0.0.1:8950/mcp --allow-network``: call the
  operator-installed loopback AlphaEngine MCP bridge (one document quota unit,
  one physical call per page).

The governance record must be ``approved``; the committed
``deploy/connector-governance/alphaengine-get-document-v1.json`` is a
``proposed`` record and therefore fails closed until the owner approves it.
``--governance-approved-by human:<owner>`` builds an in-memory approved record
for an isolated rehearsal and is refused together with ``--allow-network``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.alphaengine_core_acquisition import (
    AlphaEngineCoreAcquisition,
    StaticConnectorGovernance,
    build_governance_record,
    core_transcript_authority_probe,
)
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_connector_bridge import (
    HostToolInvocationResult,
    LoopbackStreamableHttpMcpHandle,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOVERNANCE = ROOT / "deploy" / "connector-governance" / "alphaengine-get-document-v1.json"
DEFAULT_MCP_ENDPOINT = "http://127.0.0.1:8950/mcp"


class _FakeDocumentHandle:
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


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--document-ref", required=True, help="alphaengine-doc:<id>")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--governance", type=Path, default=DEFAULT_GOVERNANCE)
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

    if args.governance_approved_by:
        governance = StaticConnectorGovernance(
            build_governance_record(
                approved_by=args.governance_approved_by, status="approved"
            )
        )
    else:
        governance = StaticConnectorGovernance.load(args.governance)

    state = _secure_dir(args.state_dir)
    core = DaltonStore(str(state / "core.sqlite"))
    connectors = ConnectorStore(core)
    observability = ObservabilityStore(core)
    journal = RunnerJournal(core)
    scheduler = Scheduler(
        str(state / "scheduler.sqlite"), default_lease_seconds=30, max_lease_seconds=60
    )
    catalog = CapabilityCatalog(
        str(state / "catalog.sqlite"),
        approval_resolver=governance.approval,
        policy_resolver=governance.policy,
    )
    spool = RawSpool(str(_secure_dir(state / "connector-spool")), max_total_bytes=1_000_000_000)
    if args.fake_document_file is not None:
        handle = _FakeDocumentHandle(
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
        plan = acquisition.build_plan(args.document_ref, max_pages=args.max_pages)
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
            None if args.expected_content_sha256 is None
            else assembled is not None
            and assembled["content_hash"] == args.expected_content_sha256
        )
        summary = {
            "schema_version": "0.1",
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
            "expected_content_sha256": args.expected_content_sha256,
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
        (state / "summary.json").write_text(canonical_json(summary) + "\n", encoding="utf-8")
        os.chmod(state / "summary.json", 0o600)
        (state / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        os.chmod(state / "manifest.json", 0o600)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
        return 0 if probe["ok"] and digest_match in {None, True} else 1
    finally:
        catalog.close()
        scheduler.close()
        core.close()


if __name__ == "__main__":
    sys.exit(main())
