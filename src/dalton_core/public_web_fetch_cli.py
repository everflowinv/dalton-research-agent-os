"""Fetch one discovered public-web URL into a Core state directory (P9d-4b).

The writer launches this program (see ``public_web_fetch_launcher``) because
``ConnectorTransportExecutor`` bounds each call with a ``SIGALRM`` watchdog
that only works on a process main thread.  SQLite carries the shared state:
this process opens its own connection on the writer's ``core.sqlite``.

Before any byte is fetched the child:

1. finds the mission's ``discovered`` row for the URL ref and the discovery
   record that produced it;
2. re-derives the mission grant for ``source:web-search`` with the caller as
   requester (automation must be the mission principal under a connected
   source; a human may rehearse under ``probe_only``);
3. rebuilds the URL authority from the exact raw search artifact bound to
   that discovery's envelope -- a ref the search never cited is refused.

Modes:

* ``--fake-page-file PATH``: serve that file as the page body through an
  in-process ``PublicHttpTransport`` with a fixed resolver (no network).
* ``--allow-network``: real credential-free public HTTPS.

Outputs (``--summary-dir``, owner-only): ``summary.json`` and, on success,
``manifest.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .capability_catalog import CapabilityCatalog
from .connector import ConnectorStore
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .mission_source_discovery import SEC_SOURCE_REF, WEB_SEARCH_SOURCE_REF
from .observability import ObservabilityStore
from .public_http_transport import PublicHttpTransport
from .public_web_core_fetch import (
    DEFAULT_USER_AGENT,
    PublicWebCoreFetch,
    PublicWebCoreFetchError,
    WebFetchConnectorGovernance,
    build_web_fetch_governance_record,
    url_authority_from_discovery,
)
from .raw_spool import RawSpool
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json


DEFAULT_CATALOG_NAME = "catalog-web-fetch.sqlite"
SUMMARY_SCHEMA_VERSION = "0.1"


def secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


class _FakeResponse:
    status = 200
    reason = "OK"

    def __init__(self, body: bytes, media_type: str) -> None:
        self._body = body
        self._media_type = media_type

    def getheaders(self):
        return [("content-type", self._media_type), ("content-length", str(len(self._body)))]

    def read(self, _amount=None):
        body, self._body = self._body, b""
        return body

    def close(self):
        return None


def fake_page_transport(body: bytes, *, media_type: str = "text/html; charset=utf-8") -> PublicHttpTransport:
    """A public transport that answers every public host with one local body."""

    return PublicHttpTransport(
        resolver=lambda _host, _port: ("93.184.216.34",),
        exchange=lambda _target, _method, _headers, _body, _timeout: _FakeResponse(body, media_type),
    )


def _discovery_for_url(connection: Any, url_ref: str) -> dict[str, Any]:
    """The newest discovered-document row for ``url_ref`` under an active mission."""

    row = connection.execute(
        "SELECT d.* FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
        "WHERE d.document_ref=? AND d.source_ref IN (?,?) "
        "ORDER BY d.created_at DESC,d.record_id DESC LIMIT 1",
        # P10u: a filing is discovered by the SEC index and fetched by this
        # same lane, so the row it came from may belong to either source.
        (url_ref, WEB_SEARCH_SOURCE_REF, SEC_SOURCE_REF),
    ).fetchone()
    if row is None:
        raise PublicWebCoreFetchError("url_ref is not a discovered document of an active mission")
    discovery = connection.execute(
        "SELECT record_json FROM coverage_mission_source_discoveries WHERE record_id=?",
        (row["discovery_ref"],),
    ).fetchone()
    if discovery is None:
        raise PublicWebCoreFetchError("discovered document lacks its discovery record")
    return {"document": dict(row), "discovery": json.loads(discovery["record_json"])}


def _fetch_failure_reason(receipt: Mapping[str, Any]) -> str:
    """Why a fetch failed, in the words the adapter used.

    P9d-9: the old text was just ``fetch outcome failed``, which reads the same
    whether a host refuses every automated client or a DNS lookup blipped.  The
    ResultEnvelope already carries a closed ``{code, message, retryable}``, so
    repeat it here.  An operator reading the mission ledger can then tell a
    permanent host-level block from a transient fault without opening the ticket
    directory, and decide whether re-queueing the URL is worth a governed call.
    """

    parts = [f"fetch outcome {receipt['outcome']}"]
    error = receipt.get("error") if isinstance(receipt.get("error"), Mapping) else {}
    detail = str(error.get("message") or error.get("code") or "").strip()
    if detail:
        parts.append(detail)
    if error.get("retryable") is False:
        parts.append("not retryable")
    return "; ".join(parts)


def run_fetch(
    *,
    state_dir: Path,
    governance: WebFetchConnectorGovernance,
    url_ref: str,
    requested_by: str,
    transport: Any,
    transport_label: str,
    summary_dir: Path,
    user_agent: str = DEFAULT_USER_AGENT,
    catalog_db: Path | None = None,
    spool_dir: Path | None = None,
) -> dict[str, Any]:
    state = secure_dir(state_dir)
    out = secure_dir(summary_dir)
    spool_root = secure_dir(spool_dir if spool_dir is not None else state / "connector-spool")
    core = DaltonStore(str(state / "core.sqlite"))
    connectors = ConnectorStore(core)
    observability = ObservabilityStore(core)
    journal = RunnerJournal(core)
    scheduler = Scheduler(str(state / "scheduler.sqlite"), default_lease_seconds=30, max_lease_seconds=60)
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
        "transport": transport_label,
        "governance_ref": governance.id,
        "governance_hash": governance.content_hash,
        "governance_status": governance.status,
        "url_ref": url_ref,
        "requested_by": requested_by,
        "status": "failed",
        "failure_reason": None,
        "authorization": None,
        "discovery_ref": None,
        "canonical_url": None,
        "host": None,
        "fetch": None,
        "document_ref": None,
        "manifest_ref": None,
        "manifest_hash": None,
        "body_bytes": 0,
        "raw_media_type": None,
        "provider_calls": 0,
        "production_activated": False,
        "formal_authority_writes": 0,
    }
    try:
        located = _discovery_for_url(core.connection, url_ref)
        document, discovery = located["document"], located["discovery"]
        summary["discovery_ref"] = discovery["id"]
        missions = CoverageMissionAuthority(core)
        try:
            authorization = missions.authorize_source_discovery(
                company_ref=document["company_ref"],
                source_ref=WEB_SEARCH_SOURCE_REF,
                requested_by=requested_by,
                mission_version_ref=document["mission_version_ref"],
            )
        except CoverageMissionError as exc:
            summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
            return summary
        summary["authorization"] = authorization
        authority = url_authority_from_discovery(
            core.connection, spool, url_ref=url_ref, source_envelope_ref=discovery["source_envelope_ref"],
        )
        if authority["discovery_source_envelope_hash"] != discovery["source_envelope_hash"]:
            raise PublicWebCoreFetchError("discovery envelope hash drifted")
        summary["canonical_url"] = authority["canonical_url"]
        summary["host"] = authority["host"]
        fetch = PublicWebCoreFetch(
            store=core, connectors=connectors, observability=observability, journal=journal,
            scheduler=scheduler, catalog=catalog, spool=spool, governance=governance,
            transport=transport, user_agent=user_agent,
        )
        receipt = fetch.fetch(fetch.build_request(authority))
        summary["fetch"] = {
            key: receipt[key] for key in (
                "request_hash", "connector_profile_ref", "connector_invocation_ref",
                "connector_invocation_hash", "runner_response_ref", "outcome", "replayed",
                "source_envelope_ref", "source_envelope_hash", "raw_artifact_version_ref",
                "raw_response_hash", "source_status", "error",
            )
        }
        summary["provider_calls"] = receipt["provider_calls"]
        if receipt["outcome"] != "succeeded" or receipt["document_ref"] is None:
            summary["failure_reason"] = _fetch_failure_reason(receipt)
            return summary
        manifest = fetch.manifest(receipt)
        _write_owner_only(out / "manifest.json", manifest)
        summary.update({
            "status": "succeeded",
            "document_ref": receipt["document_ref"],
            "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"],
            "body_bytes": receipt["body_bytes"],
            "raw_media_type": receipt["raw_media_type"],
        })
        return summary
    except (PublicWebCoreFetchError, CoverageMissionError) as exc:
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
        help="build an in-memory approved fetch governance record for an isolated rehearsal",
    )
    parser.add_argument("--url-ref", required=True, help="public-web-url:sha256:<hash> from a discovery")
    parser.add_argument("--requested-by", required=True, help="human:<who> or the mission automation principal")
    parser.add_argument("--fake-page-file", type=Path, help="rehearsal only: serve this file as the page body")
    parser.add_argument("--fake-media-type", default="text/html; charset=utf-8")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
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
    if args.fake_page_file is None and not args.allow_network:
        parser.error("choose --fake-page-file or --allow-network")
    if args.fake_page_file is not None and args.allow_network:
        parser.error("--fake-page-file and --allow-network are mutually exclusive")
    if args.governance_approved_by and args.allow_network:
        parser.error(
            "networked fetch requires the committed approved governance record; "
            "--governance-approved-by is rehearsal-only"
        )
    if args.governance_approved_by is None and args.governance is None:
        parser.error("--governance is required unless --governance-approved-by is used")
    if args.governance_approved_by:
        governance = WebFetchConnectorGovernance(
            build_web_fetch_governance_record(approved_by=args.governance_approved_by, status="approved")
        )
    else:
        governance = WebFetchConnectorGovernance.load(args.governance)
    if args.fake_page_file is not None:
        transport: Any = fake_page_transport(args.fake_page_file.read_bytes(), media_type=args.fake_media_type)
        label = "fake"
    else:
        transport = PublicHttpTransport()
        label = "public-https"
    summary = run_fetch(
        state_dir=args.state_dir,
        governance=governance,
        url_ref=args.url_ref,
        requested_by=args.requested_by,
        transport=transport,
        transport_label=label,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        user_agent=args.user_agent,
        catalog_db=args.catalog_db,
        spool_dir=args.spool_dir,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
