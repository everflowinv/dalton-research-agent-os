"""Shared Agenda fixtures.

A cycle may only bind to a PerceptionSnapshot that exists in Core's
append-only authority, so every test that starts a cycle has to register a
real snapshot first.  Keeping one wire here stops each suite from inventing a
slightly different shape.
"""

from __future__ import annotations

from typing import Any

from dalton_core.store import content_hash


def perception_wire(
    snapshot_id: str,
    *,
    company: str = "wanhua",
    generated_at: str = "2026-08-14T10:00:00.000000+00:00",
) -> dict[str, Any]:
    wire = {
        "schema_version": "0.1",
        "snapshot_id": snapshot_id,
        "generated_at": generated_at,
        "source_kind": "legacy-coverage-sqlite-backup-v1",
        "source_snapshot_hash": "c" * 64,
        "company": {"slug": company, "name": company, "ticker": "600309.SS"},
        "catalysts": [{"event_key": "event-1", "title": "New filing"}],
        "evidence": [{"evidence_key": "evidence-1", "claim": "a claim"}],
        "filings": [{"accession_no": "0001", "form": "10-Q"}],
    }
    wire["content_hash"] = content_hash(wire)
    return wire


def register_perception(
    agenda: Any, snapshot_id: str, *, company: str = "wanhua", **kwargs: Any
) -> dict[str, Any]:
    wire = perception_wire(snapshot_id, company=company, **kwargs)
    agenda.register_perception_snapshot(
        wire, actor_ref="core", idempotency_key=f"perception:{snapshot_id}"
    )
    return wire
