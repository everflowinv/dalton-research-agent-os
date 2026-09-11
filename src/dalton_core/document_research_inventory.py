"""Verified original-document inventory for planning and admission.

No source text is copied into the planner's state. The full registrations
remain internal execution inputs; the planner sees document identities and
availability. Existing Core rows supply company ownership, never LLM tags.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
import re
from typing import Any

from .document_research import (
    READ_OPERATION, READ_REQUEST_SCHEMA_VERSION,
    build_document_research_registry, validate_document_research_policy,
)
from .company_model_annual_projection import AnnualProjectionError, verify_statement_filing
from .document_research_strategy import (
    FINANCIAL_NOTE_TARGET_REF,
    FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
    inventory_document,
    normalize_evidence_target,
)
from .store import content_hash

CONFIG_FILENAME = "document-research-config.json"
CONFIG_SCHEMA = "document-research-config-0.1"
_SOURCES = frozenset({"source:alphaengine", "source:public-web", "source:web-search",
                      "source:sec-edgar", "source:sales-notes", "source:company-wiki",
                      "source:prior-research"})
_LIMITS = frozenset({"alphaengine_max_document_chars", "public_web_max_source_chars",
                     "public_web_max_pdf_pages", "public_web_max_decompressed_bytes"})
_DILUTED_EPS_CONCEPT = "us-gaap:EarningsPerShareDiluted"
_DILUTED_SHARES_CONCEPT = "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"


def financial_note_targets_for_registration(
    *, connection: Any, mission: Mapping[str, Any], company_ref: str,
    registration: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Derive typed note targets from one exact acquired 10-K and statement filing.

    The statement rows establish only the filing and period to investigate. They
    do not establish the diluted-EPS numerator or turn later note prose into a
    numeric fact.
    """

    authority = registration.get("source_authority", {})
    if (registration.get("source_ref") != "source:sec-edgar"
            or authority.get("kind") != "coverage-mission-acquired-document"
            or authority.get("mission_version_ref") != mission.get("id")
            or authority.get("company_ref") != company_ref):
        return []
    document_ref = registration.get("document_ref")
    prefix = "sec:filing:"
    if not isinstance(document_ref, str) or not document_ref.startswith(prefix):
        return []
    accession = document_ref[len(prefix):]
    if not accession:
        return []
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('coverage_mission_statement_filings','coverage_mission_statement_dispatches',"
        "'coverage_mission_statement_lines')"
    )}
    if tables != {
        "coverage_mission_statement_filings", "coverage_mission_statement_dispatches",
        "coverage_mission_statement_lines",
    }:
        return []
    rows = connection.execute(
        "SELECT f.*,d.mission_version_ref AS dispatch_mission_version_ref,"
        "d.mission_version_hash AS dispatch_mission_version_hash,"
        "d.company_ref AS dispatch_company_ref,d.form AS dispatch_form,"
        "d.status AS dispatch_status FROM coverage_mission_statement_filings f "
        "JOIN coverage_mission_statement_dispatches d ON d.dispatch_id=f.dispatch_id "
        "WHERE f.company_ref=? AND f.accession=? AND f.form='10-K'",
        (company_ref, accession),
    ).fetchall()
    if len(rows) != 1:
        return []
    filing = dict(rows[0])
    if (filing["dispatch_mission_version_ref"] != mission.get("id")
            or filing["dispatch_mission_version_hash"] != mission.get("content_hash")
            or filing["dispatch_company_ref"] != company_ref
            or filing["dispatch_form"] != "10-K"
            or filing["dispatch_status"] != "succeeded"):
        return []
    try:
        verify_statement_filing(connection, filing)
    except (AnnualProjectionError, KeyError, TypeError, ValueError):
        # A broken optional statement authority cannot advertise this target,
        # but it must not disable ordinary qualitative reading of the acquired
        # original.
        return []
    concepts_by_period: dict[tuple[str, str], set[str]] = {}
    for row in connection.execute(
        "SELECT concept,period_start,period_end,unit,is_breakdown,dimension_axis,"
        "dimension_member,dimension_count FROM coverage_mission_statement_lines "
        "WHERE ingest_id=? AND statement='income' AND period_start IS NOT NULL "
        "AND period_end=? AND value IS NOT NULL AND concept IN (?,?)",
        (filing["ingest_id"], filing["report_date"],
         _DILUTED_EPS_CONCEPT, _DILUTED_SHARES_CONCEPT),
    ):
        unit = row["unit"]
        if (bool(row["is_breakdown"])
                or row["dimension_axis"] is not None
                or row["dimension_member"] is not None
                or row["dimension_count"] not in (None, 0)):
            continue
        if ((row["concept"] == _DILUTED_EPS_CONCEPT
             and isinstance(unit, str)
             and re.fullmatch(r"[a-z]{3}PerShare", unit, re.IGNORECASE))
                or (row["concept"] == _DILUTED_SHARES_CONCEPT
                    and isinstance(unit, str) and unit.casefold() == "shares")):
            concepts_by_period.setdefault(
                (row["period_start"], row["period_end"]), set()).add(row["concept"])
    annual_periods = []
    for (start, end), concepts in concepts_by_period.items():
        try:
            elapsed = (date.fromisoformat(end) - date.fromisoformat(start)).days
        except (TypeError, ValueError):
            return []
        if concepts == {_DILUTED_EPS_CONCEPT, _DILUTED_SHARES_CONCEPT} \
                and 290 < elapsed <= 380:
            annual_periods.append({"period_start": start, "period_end": end})
    if len(annual_periods) != 1:
        return []
    target = {
        "schema_version": FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
        "target_ref": FINANCIAL_NOTE_TARGET_REF,
        "kind": "diluted_eps_numerator",
        "statement_ingest_ref": filing["ingest_id"],
        "statement_filing_hash": filing["content_hash"],
        "accession": filing["accession"], "form": "10-K",
        "applicability_kind": "annual", "periods": annual_periods,
    }
    return [normalize_evidence_target(target)]


def validate_inventory_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "purpose", "spool_dir", "enabled_sources", "policy",
        "source_reading_limits", "inventory_preview_chars",
    } or value.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError("document research configuration has an invalid shape")
    policy = validate_document_research_policy(value["policy"])
    preview = value["inventory_preview_chars"]
    if (isinstance(preview, bool) or not isinstance(preview, int)
            or not 1 <= preview <= policy["max_read_chars"]):
        raise ValueError("inventory preview must fit the explicit original read policy")
    if value["purpose"] not in policy["allowed_purposes"]:
        raise ValueError("document research purpose is not granted by policy")
    spool = value["spool_dir"]
    if not isinstance(spool, str) or not Path(spool).is_absolute():
        raise ValueError("document research spool_dir must be an explicit absolute path")
    sources = value["enabled_sources"]
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(s, str) or s not in _SOURCES for s in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("document research enabled sources are invalid")
    limits = value["source_reading_limits"]
    if (not isinstance(limits, Mapping) or set(limits) != _LIMITS
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1
                   for n in limits.values())):
        raise ValueError("document source reading limits must be explicit positive integers")
    return {**dict(value), "policy": policy, "source_reading_limits": dict(limits)}


def inventory_with_registry(*, core: Any, mission: Mapping[str, Any],
                            registry: Any, purpose: str,
                            preview_chars: int = 0) -> dict[str, Any]:
    """Verify every acquired row in the active mission without a hidden cap."""
    companies = {entry["company_ref"] for entry in mission["universe"]}
    readable: dict[str, list[dict[str, Any]]] = {ref: [] for ref in sorted(companies)}
    unavailable: dict[str, list[dict[str, Any]]] = {ref: [] for ref in sorted(companies)}
    registrations: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    rows = core.connection.execute(
        "SELECT record_id,company_ref,source_ref,document_ref,ticket_ref "
        "FROM coverage_mission_discovered_documents "
        "WHERE mission_version_ref=? AND status='acquired' "
        "ORDER BY company_ref,source_ref,document_ref,record_id", (mission["id"],))
    for row in rows:
        row = dict(row)
        company = row["company_ref"]
        if company not in companies:
            continue
        availability = registry.inspect_acquired_document(record_id=row["record_id"], purpose=purpose)
        registration = availability.get("registration")
        if availability.get("available") is not True or not isinstance(registration, Mapping):
            unavailable[company].append({
                "record_ref": row["record_id"], "document_ref": row["document_ref"],
                "source_ref": row["source_ref"], "readable": False,
                "reason": availability.get("reason") or "source_not_readable",
            })
            continue
        authority = registration.get("source_authority", {})
        if (authority.get("kind") != "coverage-mission-acquired-document"
                or authority.get("ref") != row["record_id"]
                or authority.get("mission_version_ref") != mission["id"]
                or authority.get("company_ref") != company
                or registration.get("document_ref") != row["document_ref"]
                or registration.get("source_ref") != row["source_ref"]
                or (row["ticket_ref"] is not None
                    and registration.get("acquisition_ticket_ref") != row["ticket_ref"])):
            raise ValueError("verified document registration differs from its Core ownership")
        projected = inventory_document(registration=registration, company_ref=company)
        projected.update({"doc_kind": registration["doc_kind"],
                          "doc_date": registration["doc_date"]})
        evidence_targets = financial_note_targets_for_registration(
            connection=core.connection, mission=mission, company_ref=company,
            registration=registration,
        )
        if evidence_targets:
            projected["evidence_targets"] = evidence_targets
        if preview_chars:
            end = min(preview_chars, registration["normalized_text"]["characters"])
            if end:
                preview = registry.read({
                    "schema_version": READ_REQUEST_SCHEMA_VERSION, "operation": READ_OPERATION,
                    "purpose": purpose, "research_question": "Identify this document's subject and contents.",
                    "registration": registration, "source_start": 0, "source_end": end,
                    "policy_ref": registry.policy["policy_ref"], "policy_hash": registry.policy["content_hash"],
                })
                projected.update({"original_preview": preview["text"],
                                  "preview_proof_ref": preview["id"],
                                  "preview_proof_hash": preview["content_hash"]})
        # Review disposition is a useful warning, not an original-text source
        # and not permission to mutate/reopen a human review.
        if core.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_document_reviews'").fetchone():
            review = core.connection.execute(
                "SELECT review_id,state,rationale FROM coverage_mission_document_reviews "
                "WHERE discovered_document_ref=? ORDER BY updated_at DESC,review_id DESC LIMIT 1",
                (row["record_id"],)).fetchone()
            if review is not None:
                projected["prior_review"] = dict(review)
        key = (company, projected["document_ref"], projected["document_version_hash"])
        if key not in seen:
            readable[company].append(projected)
            seen.add(key)
        registrations[registration["content_hash"]] = dict(registration)
    return {"readable_documents_by_company": readable,
            "unavailable_documents_by_company": unavailable,
            "registration_by_hash": registrations,
            "document_research_policy": dict(registry.policy)}


def load_document_inventory(*, core: Any, mission: Mapping[str, Any],
                            state_dir: Path, config_path: Path | None = None) -> dict[str, Any]:
    result = load_document_inventory_authority(
        core=core, mission=mission, state_dir=state_dir, config_path=config_path)
    return {key: value for key, value in result.items() if key != "registry"}


def load_document_inventory_authority(*, core: Any, mission: Mapping[str, Any],
                                      state_dir: Path, config_path: Path | None = None) -> dict[str, Any]:
    """Compose only read capabilities; absent configuration grants no reads."""
    from .alphaengine_acquisition_launcher import ReadOnlyAlphaEngineManifestReader
    from .connector_authority_port import ReadOnlyConnectorReceiptReader
    from .feed_launcher import ReadOnlyFeedManifestReader
    from .public_web_fetch_launcher import ReadOnlyPublicWebFetchManifestReader
    from .raw_spool import RawSpoolReader

    config_path = config_path or state_dir / CONFIG_FILENAME
    if not config_path.exists():
        return {"readable_documents_by_company": {}, "unavailable_documents_by_company": {},
                "registration_by_hash": {}, "document_research_policy": None,
                "status": "unconfigured", "registry": None}
    if config_path.is_symlink():
        raise ValueError("document research configuration cannot be a symlink")
    config = validate_inventory_config(json.loads(config_path.read_text(encoding="utf-8")))
    sources = config["enabled_sources"]
    # Missing lane directories are reported per document by the registry;
    # reading this inventory must not install acquisition lanes as a side effect.
    missing = []

    def existing_reader(source: str, factory: Any) -> Any | None:
        try:
            return factory()
        except (ValueError, RuntimeError, OSError) as exc:
            missing.append({"source_ref": source, "reason": type(exc).__name__})
            return None

    feeds = {}
    for source in ("source:sales-notes", "source:company-wiki", "source:prior-research"):
        if source in sources:
            reader = existing_reader(source, lambda s=source: ReadOnlyFeedManifestReader(
                state_dir=state_dir, source_ref=s))
            if reader is not None:
                feeds[source] = reader
    alpha = (existing_reader("source:alphaengine", lambda: ReadOnlyAlphaEngineManifestReader(
        state_dir=state_dir)) if "source:alphaengine" in sources else None)
    web_sources = [s for s in sources if s in {"source:public-web", "source:web-search", "source:sec-edgar"}]
    web = (existing_reader("source:public-web", lambda: ReadOnlyPublicWebFetchManifestReader(
        state_dir=state_dir)) if web_sources else None)
    registry = build_document_research_registry(
        core=core, state_dir=state_dir, spool=RawSpoolReader(config["spool_dir"]),
        receipt_reader=ReadOnlyConnectorReceiptReader(core.connection), policy=config["policy"],
        feed_launchers=feeds, alphaengine_launcher=alpha, public_web_launcher=web,
        public_web_source_refs=web_sources if web is not None else [],
        source_reading_limits=config["source_reading_limits"],
    )
    result = inventory_with_registry(core=core, mission=mission, registry=registry,
                                     purpose=config["purpose"], preview_chars=config["inventory_preview_chars"])
    return {**result, "status": "configured", "config_hash": content_hash(config),
            "unavailable_sources": missing, "registry": registry}


def document_inventory_signature(core: Any, state_dir: Path) -> str:
    """Wake planning on a changed exact ticket even when row counts match."""
    path = state_dir / CONFIG_FILENAME
    config = path.read_bytes().hex() if path.is_file() and not path.is_symlink() else None
    rows = core.connection.execute(
        "SELECT d.record_id,d.mission_version_ref,d.company_ref,d.source_ref,d.document_ref,"
        "d.status,d.ticket_ref FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
        "ORDER BY d.record_id")
    tables = {row[0] for row in core.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('coverage_mission_statement_filings','coverage_mission_statement_dispatches')"
    )}
    statement_filings = []
    if tables == {"coverage_mission_statement_filings", "coverage_mission_statement_dispatches"}:
        statement_filings = [dict(row) for row in core.connection.execute(
            "SELECT f.ingest_id,f.company_ref,f.accession,f.form,f.report_date,f.content_hash,"
            "d.mission_version_ref,d.mission_version_hash,d.status FROM "
            "coverage_mission_statement_filings f JOIN coverage_mission_statement_dispatches d "
            "ON d.dispatch_id=f.dispatch_id JOIN coverage_mission_pointer p "
            "ON p.mission_version_id=d.mission_version_ref ORDER BY f.ingest_id"
        )]
    return content_hash({"config": config, "acquisitions": [dict(row) for row in rows],
                         "statement_filings": statement_filings})
