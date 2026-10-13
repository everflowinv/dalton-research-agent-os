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
from .scheduler import LaneChildScheduler
from .sqlite_contention import (
    LANE_LOCK_RETRY_SECONDS,
    is_sqlite_lock_error,
    sqlite_lock_failure,
)
from .store import DaltonStore, canonical_json


DEFAULT_CATALOG_NAME = "catalog-web-fetch.sqlite"
SUMMARY_SCHEMA_VERSION = "0.2"


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


def press_release_due(connection: Any, spool: Any, discovery: Mapping[str, Any], url_ref: str) -> bool:
    """Whether this fetch was an Item 2.02 8-K of a code-owned earnings-release spec."""

    from .sec_plan_specs import acquisition_item_filter

    items = acquisition_item_filter(discovery.get("spec_ref"))
    if not items or discovery.get("source_ref") != SEC_SOURCE_REF or not url_ref.startswith("sec:filing:"):
        return False
    try:
        row = connection.execute(
            "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
            (discovery["source_envelope_ref"],)).fetchone()
        envelope = json.loads(row["record_json"])
        payload = json.loads(spool.read_object(envelope["raw_response_hash"]))
        from .sec_earnings_release import earnings_release_filings

        accession = url_ref.removeprefix("sec:filing:")
        return any(release["accession"] == accession and set(items) & set(release["items"])
                   for release in earnings_release_filings(payload, limit=1000))
    except Exception:  # noqa: BLE001 - an unreadable index is "not an earnings release"
        return False


def fetch_press_release_exhibit(
    fetch: Any, spool: Any, *, primary_authority: Mapping[str, Any],
    primary_receipt: Mapping[str, Any], out: Path,
) -> dict[str, Any]:
    """Fetch Exhibit 99.1 of the 8-K just fetched; never raises.

    2026-10-09: which file is Exhibit 99.1 is read first from the filing's own
    document table (``<accession>-index.html``, one more governed call under
    the same authority), where the filer declared each document's type --
    ``EX-99.1`` or ``EX-99`` whatever the file is called.  Only when that page
    cannot be read or names nothing does the 8-K's own exhibit index decide.
    Live 10-08: Accenture's release ``q4fy26earnings8-kexhibit.htm`` is typed
    ``EX-99`` and numbered "99" in the 8-K, so the link heuristic found nothing.
    """

    from .earnings_release_intake import (
        EXHIBIT_DETECTOR_VERSION, EXHIBIT_NOT_FOUND_REASON, exhibit_name_from_filing_index,
        filing_index_name, press_release_exhibit_name,
    )
    from .sec_filings_index import build_sec_exhibit_url_authority

    result: dict[str, Any] = {"status": "failed", "failure_reason": None, "provider_calls": 0,
                              "canonical_url": None, "manifest_ref": None, "manifest_hash": None,
                              "body_sha256": None, "body_bytes": 0,
                              "detector_version": EXHIBIT_DETECTOR_VERSION, "detected_by": None}

    def _bytes(digest: Any) -> bytes:
        body = spool.read_object(digest)
        if body.startswith(b"\x1f\x8b"):
            import gzip

            body = gzip.decompress(body)
        return body

    try:
        raw = _bytes(primary_receipt["raw_response_hash"])
        name = None
        try:
            directory = str(primary_authority["canonical_url"]).rsplit("/", 2)[-2]
            accession = f"{directory[:10]}-{directory[10:12]}-{directory[12:]}"
            index_authority = build_sec_exhibit_url_authority(
                primary_authority, filing_index_name(accession))
            result["index_url"] = index_authority["canonical_url"]
            index_receipt = fetch.fetch(fetch.build_request(index_authority))
            result["provider_calls"] += int(index_receipt.get("provider_calls") or 0)
            if (index_receipt.get("outcome") == "succeeded"
                    and index_receipt.get("raw_response_hash")):
                name = exhibit_name_from_filing_index(_bytes(index_receipt["raw_response_hash"]))
                if name is not None:
                    result["detected_by"] = "filing_index"
            else:
                result["index_failure"] = _fetch_failure_reason(index_receipt)[:300]
        except Exception as exc:  # noqa: BLE001 - the 8-K's own index still decides
            result["index_failure"] = f"{type(exc).__name__}: {exc}"[:300]
        if name is None:
            name = press_release_exhibit_name(raw)
            if name is not None:
                result["detected_by"] = "primary_links"
        if name is None:
            result["failure_reason"] = EXHIBIT_NOT_FOUND_REASON
            return result
        authority = build_sec_exhibit_url_authority(primary_authority, name)
        result["canonical_url"] = authority["canonical_url"]
        receipt = fetch.fetch(fetch.build_request(authority))
        result["provider_calls"] += int(receipt.get("provider_calls") or 0)
        if receipt["outcome"] != "succeeded" or receipt["document_ref"] is None:
            result["failure_reason"] = _fetch_failure_reason(receipt)
            return result
        manifest = fetch.manifest(receipt)
        _write_owner_only(out / "exhibit-manifest.json", manifest)
        result.update({"status": "succeeded", "manifest_ref": manifest["id"],
                       "manifest_hash": manifest["content_hash"],
                       "body_sha256": manifest["body_sha256"], "body_bytes": manifest["body_bytes"]})
        return result
    except Exception as exc:  # noqa: BLE001 - the 8-K itself was fetched; say why this was not
        result["failure_reason"] = f"{type(exc).__name__}: {exc}"[:400]
        return result


def fetch_annual_report_exhibit(
    fetch: Any, spool: Any, *, primary_authority: Mapping[str, Any],
    primary_receipt: Mapping[str, Any], out: Path,
) -> dict[str, Any]:
    """Fetch the Exhibit 13 a 10-K incorporates by reference; never raises (2026-10-09).

    Decided from the 10-K's own bytes (``sec_annual_report_exhibit.detect``):
    a 10-K that incorporates no annual report, or whose exhibit index names
    no single Exhibit 13 file, costs no call and records why.  Otherwise one
    more governed call under the same authority, the manifest written beside
    the 10-K's as ``annual-report-exhibit-manifest.json``.
    """

    from .sec_annual_report_exhibit import DETECTOR_VERSION, MANIFEST_FILE, detect
    from .sec_filings_index import build_sec_exhibit_url_authority

    result: dict[str, Any] = {"status": "failed", "failure_reason": None, "provider_calls": 0,
                              "canonical_url": None, "manifest_ref": None, "manifest_hash": None,
                              "body_sha256": None, "body_bytes": 0,
                              "detector_version": DETECTOR_VERSION, "exhibit_name": None}
    try:
        body = spool.read_object(primary_receipt["raw_response_hash"])
        if body.startswith(b"\x1f\x8b"):
            import gzip

            body = gzip.decompress(body)
        directory = str(primary_authority["canonical_url"]).rsplit("/", 2)[-2]
        accession = f"{directory[:10]}-{directory[10:12]}-{directory[12:]}"
        found = detect(body, accession=accession)
        if not found["incorporated"]:
            result.update(status="not_incorporated",
                          failure_reason="the 10-K incorporates no annual report to holders")
            return result
        if found["name"] is None:
            result.update(status="not_found",
                          failure_reason="the 10-K's exhibit index names no single Exhibit 13 file")
            return result
        result["exhibit_name"] = found["name"]
        authority = build_sec_exhibit_url_authority(primary_authority, found["name"])
        result["canonical_url"] = authority["canonical_url"]
        receipt = fetch.fetch(fetch.build_request(authority))
        result["provider_calls"] += int(receipt.get("provider_calls") or 0)
        if receipt["outcome"] != "succeeded" or receipt["document_ref"] is None:
            result["failure_reason"] = _fetch_failure_reason(receipt)
            return result
        manifest = fetch.manifest(receipt)
        _write_owner_only(out / MANIFEST_FILE, manifest)
        result.update({"status": "succeeded", "manifest_ref": manifest["id"],
                       "manifest_hash": manifest["content_hash"],
                       "body_sha256": manifest["body_sha256"], "body_bytes": manifest["body_bytes"]})
        return result
    except Exception as exc:  # noqa: BLE001 - the 10-K itself was fetched; say why this was not
        result["failure_reason"] = f"{type(exc).__name__}: {exc}"[:400]
        return result


def annual_report_exhibit_wanted(discovery: Mapping[str, Any], url_ref: str) -> bool:
    """Whether this fetch was a 10-K of the mission's annual-report spec."""

    from .sec_annual_report_exhibit import ANNUAL_REPORT_SPEC_REF

    return (discovery.get("source_ref") == SEC_SOURCE_REF
            and discovery.get("spec_ref") == ANNUAL_REPORT_SPEC_REF
            and url_ref.startswith("sec:filing:"))


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
    lock_retry_seconds: float = LANE_LOCK_RETRY_SECONDS,
) -> dict[str, Any]:
    state = secure_dir(state_dir)
    out = secure_dir(summary_dir)
    spool_root = secure_dir(spool_dir if spool_dir is not None else state / "connector-spool")
    core = DaltonStore(str(state / "core.sqlite"))
    connectors = ConnectorStore(core)
    observability = ObservabilityStore(core)
    journal = RunnerJournal(core)
    scheduler = LaneChildScheduler(
        str(state / "scheduler.sqlite"), default_lease_seconds=30, max_lease_seconds=60,
        lock_retry_seconds=lock_retry_seconds,
    )
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
        "failure_retryable": None,
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
            error = receipt.get("error")
            if isinstance(error, Mapping) and type(error.get("retryable")) is bool:
                summary["failure_retryable"] = error["retryable"]
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
        if press_release_due(core.connection, spool, discovery, url_ref):
            # ADR-0015: an Item 2.02 8-K's press release is its Exhibit 99.1,
            # a second document in the same accession directory.  One more
            # governed call, same governance and grant; its outcome is
            # recorded beside the 8-K's and never changes the 8-K's status.
            exhibit = fetch_press_release_exhibit(
                fetch, spool, primary_authority=authority, primary_receipt=receipt, out=out)
            summary["exhibit"] = exhibit
            summary["provider_calls"] += int(exhibit.get("provider_calls") or 0)
        if annual_report_exhibit_wanted(discovery, url_ref):
            # 2026-10-09: a 10-K that incorporates its annual report by
            # reference is read together with that Exhibit 13.
            annual = fetch_annual_report_exhibit(
                fetch, spool, primary_authority=authority, primary_receipt=receipt, out=out)
            summary["annual_report_exhibit"] = annual
            summary["provider_calls"] += int(annual.get("provider_calls") or 0)
        if discovery.get('source_ref') == 'source:sec-edgar' and discovery.get('spec_ref') in {
                'foreign-disclosure-6k', 'foreign-disclosure-6k-amendment'}:
            from .sec_interim_attachments import identity, acquire, InterimAttachmentError
            try:
                interim_identity = identity(core.connection, spool, discovery['id'],
                    company_ref=document['company_ref'], parent_ref=url_ref)
                interim = acquire(fetch, spool, source_identity=interim_identity,
                    primary_receipt=receipt, out=out)
                summary['interim_attachments'] = interim
                summary['provider_calls'] += interim['provider_calls']
            except (ValueError, KeyError, TypeError) as exc:
                summary['interim_attachments'] = {'status': 'refused',
                    'failure_reason': type(exc).__name__ + ': ' + str(exc)[:400],
                    'financial_periods_inferred': False}
        return summary
    except (PublicWebCoreFetchError, CoverageMissionError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record the reason for the parent, then surface it
        if is_sqlite_lock_error(exc):
            # Contention outlasted the retries: not a verdict on the page, so
            # it is reported as such and stays retryable.
            summary.update(sqlite_lock_failure(exc))
            return summary
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(out / "summary.json", summary)
        catalog.close()
        scheduler.close()
        core.close()


def run_exhibit_redetect(
    *,
    state_dir: Path,
    governance: WebFetchConnectorGovernance,
    ticket_ref: str,
    requested_by: str,
    transport: Any,
    transport_label: str,
    summary_dir: Path,
    user_agent: str = DEFAULT_USER_AGENT,
    catalog_db: Path | None = None,
    spool_dir: Path | None = None,
    lock_retry_seconds: float = LANE_LOCK_RETRY_SECONDS,
) -> dict[str, Any]:
    """Look again for the Exhibit 99.1 of one 8-K already fetched (2026-10-09).

    Only an 8-K whose exhibit failed because no file could be identified, and
    only once per detector version (``exhibit_redetect_due``).  Everything a
    fetch is held to holds here: the mission grant is re-derived, the primary
    authority is rebuilt from the discovery envelope and must name the same
    URL, and the exhibit is fetched through the same governed path.  The
    outcome is written beside the 8-K's own ticket as ``exhibit-redetect.json``
    (with ``exhibit-manifest.json`` when it succeeded); the 8-K's summary is
    left untouched.
    """

    from .earnings_release_intake import (
        EXHIBIT_DETECTOR_VERSION, EXHIBIT_REDETECT_FILE, exhibit_redetect_due,
    )

    state = secure_dir(state_dir)
    out = secure_dir(summary_dir)
    spool_root = secure_dir(spool_dir if spool_dir is not None else state / "connector-spool")
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION, "mode": "exhibit_redetect",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "ticket_ref": ticket_ref, "requested_by": requested_by,
        "transport": transport_label, "governance_ref": governance.id,
        "status": "failed", "failure_reason": None, "provider_calls": 0,
        "detector_version": EXHIBIT_DETECTOR_VERSION, "exhibit": None,
    }
    if not isinstance(ticket_ref, str) or not ticket_ref.startswith("public-web-fetch:"):
        summary["failure_reason"] = "ticket_ref is not a public-web fetch ticket"
        _write_owner_only(out / "summary.json", summary)
        return summary
    original_dir = state / "fetches" / ticket_ref.split(":", 1)[1]
    from .sec_annual_report_exhibit import DETECTOR_VERSION as ANNUAL_DETECTOR_VERSION
    from .sec_annual_report_exhibit import RECORD_FILE as ANNUAL_RECORD_FILE
    from .sec_annual_report_exhibit import exhibit_due as annual_exhibit_due

    # 2026-10-09: the same re-detection visits a 10-K fetched before its
    # Exhibit 13 was looked for.  Which one is due decides what is fetched.
    annual = not exhibit_redetect_due(state, ticket_ref) and annual_exhibit_due(state, ticket_ref)
    if not annual and not exhibit_redetect_due(state, ticket_ref):
        summary.update(status="skipped", failure_reason="not due for exhibit re-detection")
        _write_owner_only(out / "summary.json", summary)
        return summary
    if annual:
        summary.update(mode="annual_report_exhibit", detector_version=ANNUAL_DETECTOR_VERSION)
    original = json.loads((original_dir / "summary.json").read_text(encoding="utf-8"))
    core = DaltonStore(str(state / "core.sqlite"))
    scheduler = LaneChildScheduler(
        str(state / "scheduler.sqlite"), default_lease_seconds=30, max_lease_seconds=60,
        lock_retry_seconds=lock_retry_seconds)
    catalog = CapabilityCatalog(
        str(catalog_db if catalog_db is not None else state / DEFAULT_CATALOG_NAME),
        approval_resolver=governance.approval, policy_resolver=governance.policy)
    spool = RawSpool(str(spool_root), max_total_bytes=1_000_000_000)
    try:
        url_ref = str(original.get("url_ref") or "")
        located = _discovery_for_url(core.connection, url_ref)
        document, discovery = located["document"], located["discovery"]
        try:
            CoverageMissionAuthority(core).authorize_source_discovery(
                company_ref=document["company_ref"], source_ref=WEB_SEARCH_SOURCE_REF,
                requested_by=requested_by, mission_version_ref=document["mission_version_ref"])
        except CoverageMissionError as exc:
            summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
            return summary
        authority = url_authority_from_discovery(
            core.connection, spool, url_ref=url_ref,
            source_envelope_ref=discovery["source_envelope_ref"])
        primary = original.get("fetch") or {}
        if (authority["canonical_url"] != original.get("canonical_url")
                or not isinstance(primary.get("raw_response_hash"), str)):
            raise PublicWebCoreFetchError("the filing's fetch no longer matches its discovery")
        fetch = PublicWebCoreFetch(
            store=core, connectors=ConnectorStore(core),
            observability=ObservabilityStore(core), journal=RunnerJournal(core),
            scheduler=scheduler, catalog=catalog, spool=spool, governance=governance,
            transport=transport, user_agent=user_agent)
        if annual:
            exhibit = fetch_annual_report_exhibit(
                fetch, spool, primary_authority=authority,
                primary_receipt={"raw_response_hash": primary["raw_response_hash"]},
                out=original_dir)
            _write_owner_only(original_dir / ANNUAL_RECORD_FILE, {
                "schema_version": "0.1", "ticket_ref": ticket_ref,
                "redetected_by_ticket_dir": out.name,
                "detector_version": ANNUAL_DETECTOR_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "exhibit": exhibit})
            # Not finding an Exhibit 13 is an answer, not a failure.
            summary.update(
                status=("succeeded" if exhibit.get("status") in {
                    "succeeded", "not_incorporated", "not_found"} else "failed"),
                exhibit=exhibit, provider_calls=int(exhibit.get("provider_calls") or 0),
                failure_reason=(None if exhibit.get("status") == "succeeded"
                                else exhibit.get("failure_reason")))
            return summary
        exhibit = fetch_press_release_exhibit(
            fetch, spool, primary_authority=authority,
            primary_receipt={"raw_response_hash": primary["raw_response_hash"]},
            out=original_dir)
        _write_owner_only(original_dir / EXHIBIT_REDETECT_FILE, {
            "schema_version": "0.1", "ticket_ref": ticket_ref,
            "redetected_by_ticket_dir": out.name,
            "detector_version": EXHIBIT_DETECTOR_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "exhibit": exhibit})
        summary.update(status="succeeded" if exhibit.get("status") == "succeeded" else "failed",
                       exhibit=exhibit, provider_calls=int(exhibit.get("provider_calls") or 0),
                       failure_reason=exhibit.get("failure_reason"))
        return summary
    except (PublicWebCoreFetchError, CoverageMissionError, OSError, ValueError, KeyError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"[:400]
        return summary
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
    parser.add_argument("--url-ref", help="public-web-url:sha256:<hash> from a discovery")
    parser.add_argument(
        "--exhibit-for-ticket",
        help="look again for the Exhibit 99.1 of the 8-K this fetch ticket already fetched")
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
    if (args.url_ref is None) == (args.exhibit_for_ticket is None):
        parser.error("choose exactly one of --url-ref and --exhibit-for-ticket")
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
    if args.exhibit_for_ticket is not None:
        summary = run_exhibit_redetect(
            state_dir=args.state_dir, governance=governance,
            ticket_ref=args.exhibit_for_ticket, requested_by=args.requested_by,
            transport=transport, transport_label=label,
            summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
            user_agent=args.user_agent, catalog_db=args.catalog_db, spool_dir=args.spool_dir)
        if not args.quiet:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
        return 0 if summary["status"] in {"succeeded", "skipped"} else 1
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
