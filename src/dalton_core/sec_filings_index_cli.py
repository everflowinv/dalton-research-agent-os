"""P10s: one governed SEC filings-index discovery, out of process.

Mirrors ``public_web_search_cli``: authorize the company against the mission,
read the index under the owner-signed capability, and append the discovery
record naming each filing.  The filings themselves are not fetched here -- the
receipt hands back ``public-web-url`` refs and the ordinary acquisition lane
does the retrieving, the same way a web search hands over the pages it ranked.

Out of process for the same reason every other lane is: the writer's tick must
not be able to block on a source that is slow, and a child that hangs is
visible and killable in a way an in-process call is not.
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
from .connector_governance import ConnectorGovernance, load_connector_governance
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .mission_source_discovery import (
    SEC_SOURCE_REF,
    build_discovery_parameters,
    discovery_query_hash,
    load_discovery_plan,
)
from .observability import ObservabilityStore
from .bounded_alphaengine_probe import document_in_authority
from .raw_spool import RawSpool
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .sec_filings_index import CAPABILITY_ID
from .sec_filings_index_core import DEFAULT_USER_AGENT, SecFilingsIndexCore
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_CATALOG_NAME = "catalog-sec-filings-index.sqlite"


def secure_dir(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    return resolved


def _write_owner_only(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)


def run_discovery(
    *,
    state_dir: Path,
    governance: ConnectorGovernance,
    plan: dict[str, Any],
    company_ref: str,
    spec_ref: str,
    requested_by: str,
    mission_version_ref: str,
    mission_version_hash: str,
    as_of: date,
    summary_dir: Path,
    user_agent: str = DEFAULT_USER_AGENT,
    catalog_db: Path | None = None,
    spool_dir: Path | None = None,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Authorize, read the index, and append the discovery record."""

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
    # One catalog file per governed capability: a shared one would let another
    # capability's publish bump the epoch under this descriptor.
    catalog = CapabilityCatalog(
        str(catalog_db if catalog_db is not None else state / DEFAULT_CATALOG_NAME),
        approval_resolver=governance.approval,
        policy_resolver=governance.policy,
    )
    spool = RawSpool(str(spool_root), max_total_bytes=1_000_000_000)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "source_ref": SEC_SOURCE_REF,
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
        "index": None,
        "discovery_ref": None,
        "discovery_hash": None,
        "discovery_status": None,
        "document_count": 0,
        "new_document_count": 0,
        "in_authority_document_count": 0,
        "filings": [],
        "provider_calls": 0,
    }
    try:
        if plan["source_ref"] != SEC_SOURCE_REF:
            summary["failure_reason"] = "discovery plan is not a SEC filings-index plan"
            return summary
        if governance.capability_id != CAPABILITY_ID:
            summary["failure_reason"] = "governance record is not the filings-index capability"
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
        summary["query_hash"] = discovery_query_hash(plan, parameters)

        index = SecFilingsIndexCore(
            store=core, connectors=connectors, observability=observability,
            journal=journal, scheduler=scheduler, catalog=catalog, spool=spool,
            governance=governance, user_agent=user_agent, adapter=adapter,
        )
        receipt = index.list_filings(index.build_request(parameters))
        summary["index"] = {
            key: receipt[key] for key in (
                "request_hash", "connector_profile_ref", "connector_invocation_ref",
                "connector_invocation_hash", "runner_response_ref", "outcome", "replayed",
                "source_envelope_ref", "source_envelope_hash", "raw_artifact_version_ref",
                "source_record_refs", "document_refs",
            )
        }
        summary["provider_calls"] = receipt["provider_calls"]
        if receipt["outcome"] != "succeeded" or receipt["source_envelope_ref"] is None:
            summary["failure_reason"] = f"index outcome {receipt['outcome']}"
            return summary

        filings = receipt["filings"]
        summary["filings"] = [
            {k: item[k] for k in
             ("record_ref", "accession", "form", "filing_date", "canonical_url", "url_ref")}
            for item in filings
        ]
        # The queue is keyed by the filing, not by where it happens to live:
        # record_source_discovery binds document_refs to the envelope's own
        # records. The URL rides in the summary for the acquisition step, which
        # rebuilds it from these same bytes.
        document_refs = receipt["source_record_refs"]
        hosts = {item["record_ref"]: item["host"] for item in filings}
        present = [
            ref for ref in document_refs
            if document_in_authority(core.connection, ref)
        ]
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
            document_refs=document_refs,
            in_authority_document_refs=present,
            document_hosts=hosts,
        )
        summary["discovery_ref"] = record["id"]
        summary["discovery_hash"] = record["content_hash"]
        summary["discovery_status"] = record.get("status")
        summary["document_count"] = len(document_refs)
        summary["in_authority_document_count"] = len(present)
        summary["new_document_count"] = len(document_refs) - len(present)
        summary["status"] = "succeeded"
        summary["failure_reason"] = None
        return summary
    except Exception as exc:  # the summary is the child's only durable report
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    finally:
        _write_owner_only(out / "summary.json", summary)
        catalog.close()
        scheduler.close()
        core.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one governed SEC filings-index discovery"
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--governance", type=Path, required=True)
    parser.add_argument("--discovery-plan", type=Path, required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--spec-ref", required=True)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--mission-version-ref", required=True)
    parser.add_argument("--mission-version-hash", required=True)
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    parser.add_argument("--summary-dir", type=Path, required=True)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--catalog-db", type=Path, default=None)
    parser.add_argument("--spool-dir", type=Path, default=None)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_network:
        print("networked index reads require --allow-network", file=sys.stderr)
        return 1
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print("--as-of must be YYYY-MM-DD", file=sys.stderr)
        return 1
    governance = load_connector_governance(args.governance)
    plan = load_discovery_plan(args.discovery_plan)
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
        summary_dir=args.summary_dir,
        user_agent=args.user_agent,
        catalog_db=args.catalog_db,
        spool_dir=args.spool_dir,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
