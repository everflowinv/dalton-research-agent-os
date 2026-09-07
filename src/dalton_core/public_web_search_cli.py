"""Run one mission-authorized Gemini web search into a Core state directory.

The writer launches this program (see ``mission_source_discovery``) because
``ConnectorTransportExecutor`` bounds each provider call with a ``SIGALRM``
watchdog that only works on a process main thread.  SQLite carries the shared
state: this process opens its own connection on the writer's ``core.sqlite``.

Before any call is spent the child re-reads the Core and re-derives the
mission grant (``CoverageMissionAuthority.authorize_source_discovery`` for
``source:web-search``); the exact authorization it gets back is what the
discovery record binds.

Modes:

* ``--fake-citations-file PATH``: a JSON array of ``{"url": ..., "title": ...}``
  citations served by an in-process stand-in for the host-owned ``web_search``
  handle.  No network; ``--governance-approved-by human:<who>`` may build an
  in-memory approved record for rehearsal.
* ``--allow-network``: call the host-owned OpenClaw web search broker over
  its owner-only Unix socket (P9d-4d).  The broker owns the provider and its
  credential; this child sends one exact query and never a key.  Without
  broker socket/key arguments the run is refused before touching Core.

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

from .capability_catalog import CapabilityCatalog
from .connector import ConnectorStore
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .mission_source_discovery import (
    DiscoveryPlanError,
    WEB_SEARCH_SOURCE_REF,
    build_discovery_parameters,
    load_discovery_plan,
)
from .observability import ObservabilityStore
from .openclaw_web_search_broker_client import WebSearchBrokerHandle
from .public_web_core_search import (
    FakeWebSearchHandle,
    PublicWebCoreSearch,
    PublicWebCoreSearchError,
    WebSearchConnectorGovernance,
    build_web_search_governance_record,
    public_web_urls_in_authority,
    web_search_spec_hash,
)
from .raw_spool import RawSpool
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json


DEFAULT_CATALOG_NAME = "catalog-gemini-web-search.sqlite"
SUMMARY_SCHEMA_VERSION = "0.1"
NETWORK_UNAVAILABLE_REASON = (
    "web search host bridge is not configured: --broker-socket and --broker-auth-key "
    "are required for a networked search"
)


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
    governance: WebSearchConnectorGovernance,
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
    # One catalog per governed capability (see alphaengine_search_cli): a
    # shared catalog file would let another capability's publish bump the
    # epoch under this descriptor.
    catalog = CapabilityCatalog(
        str(catalog_db if catalog_db is not None else state / DEFAULT_CATALOG_NAME),
        approval_resolver=governance.approval,
        policy_resolver=governance.policy,
    )
    spool = RawSpool(str(spool_root), max_total_bytes=1_000_000_000)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "source_ref": WEB_SEARCH_SOURCE_REF,
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
        "discovered_urls": [],
        "provider_calls": 0,
        "production_activated": False,
        "formal_authority_writes": 0,
    }
    try:
        if plan["source_ref"] != WEB_SEARCH_SOURCE_REF:
            summary["failure_reason"] = "discovery plan is not a web search plan"
            return summary
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
        summary["query_hash"] = web_search_spec_hash(parameters)
        search = PublicWebCoreSearch(
            store=core,
            connectors=connectors,
            observability=observability,
            journal=journal,
            scheduler=scheduler,
            catalog=catalog,
            spool=spool,
            governance=governance,
            host_handle=handle,
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
        present = public_web_urls_in_authority(core.connection, receipt["document_refs"])
        # Human-readable canonical URLs, rebuilt from the exact raw artifact;
        # reported for the owner and, since P9d-13, their hosts ride the
        # ledger row so the queue can be ordered without re-opening the bytes.
        authorities = search.url_authorities(receipt["source_envelope_ref"])
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
            document_hosts={item["url_ref"]: item["host"] for item in authorities},
        )
        summary["discovered_urls"] = [
            {"url_ref": item["url_ref"], "canonical_url": item["canonical_url"], "host": item["host"]}
            for item in authorities
        ]
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
    except (PublicWebCoreSearchError, CoverageMissionError, DiscoveryPlanError) as exc:
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
        help="build an in-memory approved web search governance record for an isolated rehearsal",
    )
    parser.add_argument("--discovery-plan", type=Path, required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--spec-ref", required=True)
    parser.add_argument("--requested-by", required=True, help="human:<who> or the mission automation principal")
    parser.add_argument("--mission-version-ref", required=True)
    parser.add_argument("--mission-version-hash", required=True)
    parser.add_argument("--as-of", help="YYYY-MM-DD; defaults to today (UTC)")
    parser.add_argument("--fake-citations-file", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--broker-socket", help="owner-only OpenClaw web search broker socket")
    parser.add_argument("--broker-auth-key", help="owner-only shared key file for that broker")
    parser.add_argument("--broker-client-id", default="client:dalton-core")
    parser.add_argument("--broker-profile-id", default="profile:web-search")
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
    if args.fake_citations_file is None and not args.allow_network:
        parser.error("choose --fake-citations-file or --allow-network")
    if args.fake_citations_file is not None and args.allow_network:
        parser.error("--fake-citations-file and --allow-network are mutually exclusive")
    if args.governance_approved_by and args.allow_network:
        parser.error(
            "networked discovery requires the committed approved governance record; "
            "--governance-approved-by is rehearsal-only"
        )
    if args.governance_approved_by is None and args.governance is None:
        parser.error("--governance is required unless --governance-approved-by is used")
    if args.governance_approved_by:
        governance = WebSearchConnectorGovernance(
            build_web_search_governance_record(approved_by=args.governance_approved_by, status="approved")
        )
    else:
        governance = WebSearchConnectorGovernance.load(args.governance)
    plan = load_discovery_plan(args.discovery_plan)
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc).date()
    summary_dir = args.summary_dir if args.summary_dir is not None else args.state_dir
    if args.allow_network:
        if not args.broker_socket or not args.broker_auth_key:
            # Fail closed before touching Core; the parent sees a fixed reason.
            summary = {
                "schema_version": SUMMARY_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "source_ref": WEB_SEARCH_SOURCE_REF,
                "transport": "openclaw-search-broker",
                "plan_ref": plan["id"],
                "plan_hash": plan["content_hash"],
                "company_ref": args.company_ref,
                "spec_ref": args.spec_ref,
                "requested_by": args.requested_by,
                "as_of": as_of.isoformat(),
                "status": "failed",
                "failure_reason": NETWORK_UNAVAILABLE_REASON,
                "discovery_ref": None,
                "new_document_count": 0,
                "provider_calls": 0,
                "formal_authority_writes": 0,
            }
            _write_owner_only(secure_dir(summary_dir) / "summary.json", summary)
            if not args.quiet:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
            return 1
        handle: Any = WebSearchBrokerHandle(
            socket_path=args.broker_socket,
            auth_key_path=args.broker_auth_key,
            client_id=args.broker_client_id,
            profile_id=args.broker_profile_id,
        )
        transport_label = "openclaw-search-broker"
    else:
        citations = json.loads(args.fake_citations_file.read_text(encoding="utf-8"))
        if not isinstance(citations, list):
            parser.error("--fake-citations-file must hold a JSON array of citations")
        handle = FakeWebSearchHandle(citations)
        transport_label = "fake"
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
        transport=transport_label,
        summary_dir=summary_dir,
        catalog_db=args.catalog_db,
        spool_dir=args.spool_dir,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
