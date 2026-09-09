"""P10x: what is still waiting to be read for a company, and what reading it buys.

The cockpit can say how many documents were found and how many Claims exist.
It cannot say the thing an analyst would ask first -- *if this lane keeps
running, what does this company get, and what does it cost* -- and neither can
P14a's SourceCapabilityMap, which is why "ACN needs two independent sell-side
sources" was diagnosed live as "885 documents are not being extracted" when the
truth was that Accenture holds twelve sell-side documents in total and three of
them were ever acquired.

So the projection is deliberately arithmetic over the ledger's own history
rather than a table of constants: how many windows a document of this tier has
actually taken here, and how many Claims one has actually yielded.  An install
with no history gets a documented floor and says so, because a projection that
cannot distinguish "we measured this" from "we assumed this" is how the last
wrong number was believed.

Nothing here writes to any authority except the append-only provenance table
this module owns, and that table only records what the search wire already
said.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .document_provenance import (
    SCHEMA_VERSION as PROVENANCE_SCHEMA_VERSION,
    TIERS,
    independent_brokers,
    provenance_record,
    tier_for_spec,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("extraction_backlog_schema.sql")

# What one review actually cost and yielded, when nothing has been read yet.
# Measured from the live ledger on 2026-09-09 (1,288 reviews across thirteen
# mission versions) and used only as a floor: a tier with its own history uses
# that instead, and the answer says which it used.
DEFAULT_WINDOWS_PER_DOCUMENT = 3
DEFAULT_CLAIMS_PER_DOCUMENT = Decimal("2.5")

# The two sentences the extraction child writes when it closes a review. They
# are generated in this repository, so parsing them is reading our own record,
# not scraping someone else's prose.
_ADMITTED_RE = re.compile(r"(\d+) qualitative claim\(s\) admitted")
_WINDOWS_RE = re.compile(r"in (\d+) window\(s\)")


def apply_schema(connection: sqlite3.Connection) -> None:
    """Create the provenance table if it is not already there."""

    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ExtractionBacklogError(RuntimeError):
    """A backlog refusal; the message is safe to show."""


class DocumentProvenanceStore:
    """Append-only record of what a search said about each document it returned."""

    def __init__(self, connection: sqlite3.Connection, *, clock: Any = None) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.clock = clock or _now
        apply_schema(self.connection)

    def record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one document's provenance; a replay is a duplicate, not a write.

        A second call carrying *different* facts about the same pinned bytes is
        a conflict rather than an overwrite: the document cannot have changed
        publisher, so one of the two readings is wrong and losing the first one
        would hide that.
        """

        wire = dict(record)
        for field in ("document_ref", "source_ref", "provenance_tier"):
            if not isinstance(wire.get(field), str) or not wire[field]:
                raise ExtractionBacklogError(f"provenance record requires {field}")
        if wire["provenance_tier"] not in TIERS:
            raise ExtractionBacklogError("provenance tier is not in the vocabulary")
        wire.setdefault("schema_version", PROVENANCE_SCHEMA_VERSION)
        wire["named_companies"] = list(wire.get("named_companies") or ())
        wire["covered_subjects"] = list(wire.get("covered_subjects") or ())
        wire["metadata_seen"] = bool(wire.get("metadata_seen"))
        digest = content_hash({k: v for k, v in wire.items() if k != "content_hash"})
        existing = self.get(wire["document_ref"])
        if existing is not None:
            if existing["content_hash"] != digest:
                raise ExtractionBacklogError(
                    "document provenance was recorded with different facts")
            return {"status": "duplicate", **existing}
        self.connection.execute(
            "INSERT INTO document_provenance_records("
            "document_ref,source_ref,spec_ref,provenance_tier,broker,broker_key,title,"
            "named_companies_json,covered_subjects_json,published_at,metadata_seen,"
            "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wire["document_ref"], wire["source_ref"], wire.get("spec_ref"),
                wire["provenance_tier"], wire.get("broker"), wire.get("broker_key"),
                wire.get("title"), canonical_json(wire["named_companies"]),
                canonical_json(wire["covered_subjects"]), wire.get("published_at"),
                1 if wire["metadata_seen"] else 0, canonical_json(wire), digest,
                self.clock(),
            ),
        )
        self.connection.commit()
        return {"status": "fresh", **dict(wire), "content_hash": digest}

    def get(self, document_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM document_provenance_records WHERE document_ref=?",
            (document_ref,),
        ).fetchone()
        if row is None:
            return None
        return {**json.loads(row["record_json"]), "content_hash": row["content_hash"]}

    def record_search(
        self,
        *,
        raw: Any,
        source_ref: str,
        spec_by_document: Mapping[str, str],
        subjects: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """Record every document one AlphaEngine search described.

        Best effort by construction: an unreadable artefact yields nothing and
        a document already recorded is left alone, so this can be called on
        every pass over the queue without becoming a write each time.
        """

        from .document_provenance import parse_search_metadata

        results = []
        for document_ref, metadata in parse_search_metadata(raw).items():
            try:
                results.append(self.record(provenance_record(
                    document_ref=document_ref, source_ref=source_ref,
                    spec_ref=spec_by_document.get(document_ref),
                    metadata=metadata, subjects=subjects,
                )))
            except ExtractionBacklogError as exc:
                results.append({"status": "refused", "document_ref": document_ref,
                                "reason": str(exc)})
        return results

    def subjects_for(self, document_ref: str) -> list[str]:
        """The covered subjects this document names, by its own company list.

        Empty when nothing was recorded, which the admission path reads as "no
        extra subjects" -- so a missing row leaves today's behaviour exactly as
        it was rather than guessing at a wider attribution.
        """

        record = self.get(document_ref)
        if record is None:
            return []
        return list(record.get("covered_subjects") or ())


def spec_refs_by_document(connection: sqlite3.Connection) -> dict[str, str]:
    """document_ref -> the discovery spec that found it, across every mission version.

    ``CoverageMissionAuthority.document_spec_refs`` answers for one version, and
    live the current version holds 48 of 454 acquired documents: the rest were
    registered under versions 2 through 12 and are invisible to a
    version-scoped query.  A document's *kind* does not change when the mission
    is re-versioned, so this reads them all.
    """

    rows = connection.execute(
        "SELECT d.document_ref AS document_ref, s.spec_ref AS spec_ref "
        "FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref"
    ).fetchall()
    return {row["document_ref"]: row["spec_ref"] for row in rows}


def backfill_provenance(
    connection: sqlite3.Connection,
    spool: Any,
    *,
    subjects: Mapping[str, str],
    source_ref: str = "source:alphaengine",
    limit: int = 200,
) -> dict[str, Any]:
    """Read what past searches said, for the documents they returned.

    Best effort by construction and bounded by ``limit``: this runs beside the
    extraction child, and an unreadable spool object or a search whose artefact
    has been pruned must cost one skipped envelope, not the run.  Idempotent --
    a document already recorded is a duplicate and writes nothing.
    """

    store = DocumentProvenanceStore(connection)
    specs = spec_refs_by_document(connection)
    result = {"envelopes": 0, "recorded": 0, "duplicate": 0, "unreadable": 0}
    try:
        rows = connection.execute(
            "SELECT e.record_json AS envelope, a.artifact_content_hash AS object_hash "
            "FROM connector_source_envelopes e "
            "JOIN observability_artifact_versions_v2 a "
            "  ON a.version_id=json_extract(e.record_json,'$.raw_artifact_version_ref') "
            "WHERE json_extract(e.record_json,'$.source')=? "
            "  AND json_extract(e.record_json,'$.operation')='search_library' "
            "ORDER BY json_extract(e.record_json,'$.retrieved_at') DESC LIMIT ?",
            (source_ref, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return {**result, "status": "no_search_envelopes"}
    for row in rows:
        result["envelopes"] += 1
        try:
            raw = spool.read_object(row["object_hash"])
        except Exception:  # noqa: BLE001 - a pruned object is not a failure here
            result["unreadable"] += 1
            continue
        for outcome in store.record_search(
            raw=raw, source_ref=source_ref, spec_by_document=specs, subjects=subjects,
        ):
            if outcome.get("status") == "fresh":
                result["recorded"] += 1
            elif outcome.get("status") == "duplicate":
                result["duplicate"] += 1
    return {**result, "status": "complete"}


def observed_yield(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """What a document of each tier has actually taken and yielded here.

    Reads the review ledger's own closing rationales: an admission names how
    many Claims it admitted, a dismissal names how many windows it read for
    nothing.  Both sentences are written by this repository's extraction child.
    """

    rows = connection.execute(
        "SELECT r.document_ref, r.state, r.rationale, s.spec_ref "
        "FROM coverage_mission_document_reviews r "
        "JOIN coverage_mission_discovered_documents d ON d.record_id=r.discovered_document_ref "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        "WHERE r.state IN ('extraction_staged','dismissed')"
    ).fetchall()
    # One document read under several mission versions is one document, and the
    # reading that counts is the best it ever got: carry-forward re-registers a
    # document under each new version, so the same note can carry a dismissal
    # from version 9 and an admission from version 12.  Taking the first row
    # seen would have scored a sell-side note at one Claim per document when
    # the mission-wide count says three and a half.
    best: dict[str, tuple[str, int]] = {}
    for row in rows:
        tier = tier_for_spec(row["spec_ref"])
        rationale = row["rationale"] or ""
        admitted = _ADMITTED_RE.search(rationale)
        count = int(admitted.group(1)) if admitted else 0
        previous = best.get(row["document_ref"])
        if previous is None or count > previous[1]:
            best[row["document_ref"]] = (tier, count)
    claims: dict[str, int] = {}
    read: dict[str, int] = {}
    for tier, count in best.values():
        read[tier] = read.get(tier, 0) + 1
        claims[tier] = claims.get(tier, 0) + count
    result: dict[str, dict[str, Any]] = {}
    for tier in TIERS:
        documents = read.get(tier, 0)
        if documents:
            per_claim = (Decimal(claims.get(tier, 0)) / Decimal(documents)).quantize(Decimal("0.01"))
            result[tier] = {"basis": "observed", "documents_read": documents,
                            "claims_per_document": per_claim,
                            "windows_per_document": DEFAULT_WINDOWS_PER_DOCUMENT}
        else:
            result[tier] = {"basis": "default", "documents_read": 0,
                            "claims_per_document": DEFAULT_CLAIMS_PER_DOCUMENT,
                            "windows_per_document": DEFAULT_WINDOWS_PER_DOCUMENT}
    return result


def extraction_backlog(
    connection: sqlite3.Connection,
    company_ref: str,
    *,
    mission_version_ref: str | None = None,
    reservation_micros: int | None = None,
    yields: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Queued documents for one subject, by tier, with what reading them buys.

    "Queued" is honest about the three ways a document can be waiting, because
    they have different owners and only one of them is this lane's:

    * ``awaiting_extraction`` -- acquired, review open.  The lane's own work.
    * ``acquired_unqueued`` -- acquired under an older mission version and
      never re-registered under the current one.  Invisible to the lane today.
    * ``discovered`` -- found but never fetched.  Bounded by the source's own
      call cap, which is the owner's number, not the lane's.

    Cost is an upper bound: the rate-card worst case for every window, which is
    what a reservation must assume even though settlement is far smaller.
    """

    if not isinstance(company_ref, str) or not company_ref:
        raise ExtractionBacklogError("a subject ref is required")
    if mission_version_ref is None:
        row = connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        mission_version_ref = None if row is None else row[0]
    measured = dict(yields or observed_yield(connection))
    rows = connection.execute(
        "SELECT d.document_ref, s.spec_ref, d.status, "
        "(SELECT COUNT(*) FROM coverage_mission_document_reviews r "
        "  WHERE r.document_ref=d.document_ref AND r.mission_version_ref=? "
        "    AND r.state='awaiting_human_extraction') AS open_here, "
        "(SELECT COUNT(*) FROM coverage_mission_document_reviews r2 "
        "  WHERE r2.document_ref=d.document_ref "
        "    AND r2.state IN ('extraction_staged','dismissed')) AS closed_anywhere "
        "FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        "WHERE d.company_ref=?",
        (mission_version_ref or "", company_ref),
    ).fetchall()

    # A document appears once per mission version; the best status it ever
    # reached is the one that matters.
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = best.setdefault(row["document_ref"], {
            "spec_ref": row["spec_ref"], "acquired": False,
            "open_here": False, "closed": False,
        })
        entry["acquired"] = entry["acquired"] or row["status"] == "acquired"
        entry["open_here"] = entry["open_here"] or bool(row["open_here"])
        entry["closed"] = entry["closed"] or bool(row["closed_anywhere"])

    buckets: dict[str, dict[str, Any]] = {}
    for document_ref, entry in best.items():
        tier = tier_for_spec(entry["spec_ref"])
        bucket = buckets.setdefault(tier, {
            "tier": tier, "spec_refs": set(), "awaiting_extraction": 0,
            "acquired_unqueued": 0, "discovered": 0, "already_read": 0,
        })
        if entry["spec_ref"]:
            bucket["spec_refs"].add(entry["spec_ref"])
        if entry["closed"]:
            bucket["already_read"] += 1
        elif entry["open_here"]:
            bucket["awaiting_extraction"] += 1
        elif entry["acquired"]:
            bucket["acquired_unqueued"] += 1
        else:
            bucket["discovered"] += 1

    per_window = Decimal(reservation_micros or 0) / Decimal(1_000_000)
    tiers: list[dict[str, Any]] = []
    total_docs = total_claims = 0
    total_cost = Decimal(0)
    for tier in TIERS:
        bucket = buckets.get(tier)
        if bucket is None:
            continue
        queued = bucket["awaiting_extraction"] + bucket["acquired_unqueued"] + bucket["discovered"]
        measure = measured.get(tier) or {}
        per_doc_claims = Decimal(str(measure.get("claims_per_document", DEFAULT_CLAIMS_PER_DOCUMENT)))
        per_doc_windows = int(measure.get("windows_per_document", DEFAULT_WINDOWS_PER_DOCUMENT))
        windows = queued * per_doc_windows
        expected_claims = int((Decimal(queued) * per_doc_claims).to_integral_value())
        cost = (Decimal(windows) * per_window).quantize(Decimal("0.000001"))
        total_docs += queued
        total_claims += expected_claims
        total_cost += cost
        tiers.append({
            "tier": tier,
            "spec_refs": sorted(bucket["spec_refs"]),
            "awaiting_extraction": bucket["awaiting_extraction"],
            "acquired_unqueued": bucket["acquired_unqueued"],
            "discovered": bucket["discovered"],
            "already_read": bucket["already_read"],
            "queued_documents": queued,
            "expected_windows": windows,
            "expected_claims": expected_claims,
            "expected_cost_usd": str(cost),
            "yield_basis": measure.get("basis", "default"),
        })

    provenance = connection.execute(
        "SELECT p.record_json FROM document_provenance_records p "
        "JOIN coverage_mission_discovered_documents d ON d.document_ref=p.document_ref "
        "WHERE d.company_ref=? GROUP BY p.document_ref", (company_ref,),
    ).fetchall() if _has_provenance(connection) else []
    records = [json.loads(row["record_json"]) for row in provenance]

    return {
        "projection_kind": "extraction_backlog",
        "schema_version": SCHEMA_VERSION,
        "company_ref": company_ref,
        "mission_version_ref": mission_version_ref,
        "as_of": _now(),
        "tiers": tiers,
        "totals": {
            "queued_documents": total_docs,
            "expected_claims": total_claims,
            "expected_cost_usd": str(total_cost.quantize(Decimal("0.000001"))),
            "reservation_micros_per_window": int(reservation_micros or 0),
        },
        "independent_brokers": independent_brokers(records),
        "documents_with_provenance": len(records),
    }


def _has_provenance(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_provenance_records'"
    ).fetchone() is not None


__all__ = [
    "DEFAULT_CLAIMS_PER_DOCUMENT",
    "DEFAULT_WINDOWS_PER_DOCUMENT",
    "DocumentProvenanceStore",
    "ExtractionBacklogError",
    "SCHEMA_VERSION",
    "apply_schema",
    "backfill_provenance",
    "extraction_backlog",
    "observed_yield",
    "spec_refs_by_document",
]
