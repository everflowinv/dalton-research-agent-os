"""Verified original-document inventory for planning and admission.

No source text is copied into the planner's state. The full registrations
remain internal execution inputs; the planner sees document identities and
availability. Existing Core rows supply company ownership, never LLM tags.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .document_research import (
    build_document_research_registry, validate_document_research_policy,
)
from .document_research_strategy import inventory_document
from .store import content_hash

CONFIG_FILENAME = "document-research-config.json"
CONFIG_SCHEMA = "document-research-config-0.1"
_SOURCES = frozenset({"source:alphaengine", "source:public-web", "source:web-search",
                      "source:sec-edgar", "source:sales-notes", "source:company-wiki"})
_LIMITS = frozenset({"alphaengine_max_document_chars", "public_web_max_source_chars",
                     "public_web_max_pdf_pages", "public_web_max_decompressed_bytes"})


def validate_inventory_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "purpose", "spool_dir", "enabled_sources", "policy",
        "source_reading_limits",
    } or value.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError("document research configuration has an invalid shape")
    policy = validate_document_research_policy(value["policy"])
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
                            registry: Any, purpose: str) -> dict[str, Any]:
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
    for source in ("source:sales-notes", "source:company-wiki"):
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
                                     purpose=config["purpose"])
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
    return content_hash({"config": config, "acquisitions": [dict(row) for row in rows]})
