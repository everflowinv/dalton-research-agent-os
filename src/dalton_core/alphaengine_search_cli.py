"""Run one mission-authorized AlphaEngine library search into a Core state directory.

The writer launches this program (see ``mission_source_discovery``) because
``ConnectorTransportExecutor`` bounds each provider call with a ``SIGALRM``
watchdog that only works on a process main thread.  SQLite carries the shared
state: this process opens its own connection on the writer's ``core.sqlite``.

Before any call is spent the child re-reads the Core and re-derives the
mission grant (``CoverageMissionAuthority.authorize_source_discovery``); the
exact authorization it gets back is what the discovery record binds.

Two modes:

* ``--fake-search-file PATH``: a JSON array of ``{"doc_id": ...}`` results
  served by an in-process stand-in for the host-owned MCP handle.  No
  network; ``--governance-approved-by human:<who>`` may build an in-memory
  approved record for rehearsal.
* ``--allow-network``: call the loopback AlphaEngine MCP endpoint.  Requires
  the committed governance record on disk to be ``approved``.

Outputs (``--summary-dir``, owner-only): ``summary.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .alphaengine_core_search import (
    AlphaEngineCoreSearch,
    AlphaEngineCoreSearchError,
    FakeSearchHandle,
    SearchConnectorGovernance,
    alphaengine_documents_in_authority,
    build_search_governance_record,
    search_spec_hash,
)
from .capability_catalog import CapabilityCatalog
from .connector import ConnectorStore
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .mission_source_discovery import (
    DiscoveryPlanError,
    build_discovery_parameters,
    load_discovery_plan,
)
from .observability import ObservabilityStore
from .openclaw_connector_bridge import LoopbackStreamableHttpMcpHandle
from .raw_spool import RawSpool
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json


DEFAULT_MCP_ENDPOINT = "http://127.0.0.1:8950/mcp"
DEFAULT_CATALOG_NAME = "catalog-alphaengine-search-library.sqlite"
SUMMARY_SCHEMA_VERSION = "0.1"


def secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def run_discovery(
    *,
    state_dir: Path,
    governance: SearchConnectorGovernance,
    plan: dict[str, Any],
    company_ref: str,
    spec_ref: str,
    requested_by: str,
    mission_version_ref: str,
    mission_version_hash: str,
    as_of: date,
    handle: Any,
    transport: str,
    summary_dir: Path,
    catalog_db: Path | None = None,
    spool_dir: Path | None = None,
) -> dict[str, Any]:
    """Authorize, search, and append the discovery record; return the summary."""

    state = secure_dir(state_dir)
    out = secure_dir(summary_dir)
    spool_root = secure_dir(spool_dir if spool_dir is not None else state / "connector-spool")
    core = DaltonStore(str(state / "core.sqlite"))
    connectors = ConnectorStore(core)
    observability = ObservabilityStore(core)
    journal = RunnerJournal(core)
    scheduler = Scheduler(
        str(state / "scheduler.sqlite"), default_lease_seconds=30, max_lease_seconds=60
    )
    # One catalog per governed capability.  ``CapabilityCatalog.prepare``
    # only admits a descriptor published at the catalog's *current* epoch and
    # every publish bumps the epoch, so two capabilities sharing one catalog
    # file invalidate each other (the live shared ``catalog.sqlite`` holds
    # SEC at epoch 2 and AlphaEngine get_document at epoch 1).  The search
    # capability therefore owns its own file and never touches the others.
    catalog = CapabilityCatalog(
        str(catalog_db if catalog_db is not None else state / DEFAULT_CATALOG_NAME),
        approval_resolver=governance.approval,
        policy_resolver=governance.policy,
    )
    spool = RawSpool(str(spool_root), max_total_bytes=1_000_000_000)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "transport": transport,
        "governance_ref": governance.id,
        "governance_hash": governance.content_hash,
        "governance_status": governance.status,
        "plan_ref": plan["id"],
        "plan_hash": plan["content_hash"],
        "company_ref": company_ref,
        "spec_ref": spec_ref,
        "requested_by": requested_by,
        "as_of": as_of.isoformat(),
        "status": "failed",
        "failure_reason": None,
        "authorization": None,
        "parameters": None,
        "query_hash": None,
        "search": None,
        "discovery_ref": None,
        "discovery_hash": None,
        "discovery_status": None,
        "document_count": 0,
        "new_document_count": 0,
        "in_authority_document_count": 0,
        "provider_calls": 0,
        "production_activated": False,
        "formal_authority_writes": 0,
    }
    try:
        missions = CoverageMissionAuthority(core)
        try:
            authorization = missions.authorize_source_discovery(
                company_ref=company_ref,
                source_ref=plan["source_ref"],
                requested_by=requested_by,
                mission_version_ref=mission_version_ref,
                mission_version_hash=mission_version_hash,
            )
        except CoverageMissionError as exc:
            summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
            return summary
        summary["authorization"] = authorization
        parameters = build_discovery_parameters(
            plan, spec_ref=spec_ref, company_ref=company_ref, as_of=as_of
        )
        summary["parameters"] = parameters
        summary["query_hash"] = search_spec_hash(parameters)
        search = AlphaEngineCoreSearch(
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
        receipt = search.search(search.build_request(parameters))
        summary["search"] = {
            key: receipt[key] for key in (
                "request_hash", "connector_profile_ref", "connector_invocation_ref",
                "connector_invocation_hash", "runner_response_ref", "outcome", "replayed",
                "source_envelope_ref", "source_envelope_hash", "raw_artifact_version_ref",
                "document_refs", "next_cursor", "source_status",
            )
        }
        summary["provider_calls"] = receipt["provider_calls"]
        if receipt["outcome"] != "succeeded" or receipt["source_envelope_ref"] is None:
            summary["failure_reason"] = f"search outcome {receipt['outcome']}"
            return summary
        present = alphaengine_documents_in_authority(core.connection, receipt["document_refs"])
        record = missions.record_source_discovery(
            authorization=authorization,
            discovery_plan_ref=plan["id"],
            discovery_plan_hash=plan["content_hash"],
            spec_ref=spec_ref,
            query_hash=summary["query_hash"],
            parameters=parameters,
            connector_invocation_ref=receipt["connector_invocation_ref"],
            connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"],
            source_envelope_hash=receipt["source_envelope_hash"],
            document_refs=receipt["document_refs"],
            in_authority_document_refs=present,
        )
        summary.update({
            "status": "succeeded",
            "discovery_ref": record["id"],
            "discovery_hash": record["content_hash"],
            "discovery_status": record["status"],
            "document_count": len(record["document_refs"]),
            "new_document_count": len(record["new_document_refs"]),
            "in_authority_document_count": len(record["in_authority_document_refs"]),
        })
        return summary
    except (AlphaEngineCoreSearchError, CoverageMissionError, DiscoveryPlanError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record the reason for the parent, then surface it
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(out / "summary.json", summary)
        catalog.close()
        scheduler.close()
        core.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--governance", type=Path)
    parser.add_argument(
        "--governance-approved-by",
        help="build an in-memory approved search governance record for an isolated rehearsal",
    )
    parser.add_argument("--discovery-plan", type=Path, required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--spec-ref", required=True)
    parser.add_argument("--requested-by", required=True, help="human:<who> or the mission automation principal")
    parser.add_argument("--mission-version-ref", required=True)
    parser.add_argument("--mission-version-hash", required=True)
    parser.add_argument("--as-of", help="YYYY-MM-DD; defaults to today (UTC)")
    parser.add_argument("--fake-search-file", type=Path)
    parser.add_argument("--mcp-endpoint", default=DEFAULT_MCP_ENDPOINT)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--mcp-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument(
        "--catalog-db", type=Path,
        help=f"capability catalog for this capability only; defaults to <state>/{DEFAULT_CATALOG_NAME}",
    )
    parser.add_argument("--spool-dir", type=Path, help="RawSpool data directory for the raw response")
    parser.add_argument("--quiet", action="store_true", help="do not print the summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.fake_search_file is None and not args.allow_network:
        parser.error("choose --fake-search-file or --allow-network")
    if args.fake_search_file is not None and args.allow_network:
        parser.error("--fake-search-file and --allow-network are mutually exclusive")
    if args.governance_approved_by and args.allow_network:
        parser.error(
            "networked discovery requires the committed approved governance record; "
            "--governance-approved-by is rehearsal-only"
        )
    if args.governance_approved_by is None and args.governance is None:
        parser.error("--governance is required unless --governance-approved-by is used")
    if args.governance_approved_by:
        governance = SearchConnectorGovernance(
            build_search_governance_record(approved_by=args.governance_approved_by, status="approved")
        )
    else:
        governance = SearchConnectorGovernance.load(args.governance)
    plan = load_discovery_plan(args.discovery_plan)
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc).date()
    if args.fake_search_file is not None:
        results = json.loads(args.fake_search_file.read_text(encoding="utf-8"))
        if not isinstance(results, list):
            parser.error("--fake-search-file must hold a JSON array of results")
        handle: Any = FakeSearchHandle(results)
        transport = "fake"
    else:
        handle = LoopbackStreamableHttpMcpHandle(
            args.mcp_endpoint,
            allowed_tools={"search_library": "search_library"},
            timeout_seconds=args.mcp_timeout_seconds,
        )
        transport = "loopback-mcp"
    summary = run_discovery(
        state_dir=args.state_dir,
        governance=governance,
        plan=plan,
        company_ref=args.company_ref,
        spec_ref=args.spec_ref,
        requested_by=args.requested_by,
        mission_version_ref=args.mission_version_ref,
        mission_version_hash=args.mission_version_hash,
        as_of=as_of,
        handle=handle,
        transport=transport,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        catalog_db=args.catalog_db,
        spool_dir=args.spool_dir,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
