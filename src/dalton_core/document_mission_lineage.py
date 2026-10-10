"""Read original documents across verified versions of the same research project.

A budget or policy revision changes the execution grant, not the identity of
an already acquired original. Registrations retain their original row/hash;
current mission scope and source access still decide whether it can be read.
"""
from __future__ import annotations

from typing import Any, Mapping

RULE_REF = "document-mission-lineage:0.1"


def document_mission_versions(connection: Any, mission: Mapping[str, Any]) -> list[str]:
    """Newest first, with every inherited version verified through Core authority."""
    from .coverage_mission import CoverageMissionAuthority

    current_ref = mission["id"]
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_versions'"
    ).fetchone() is None:
        # No ancestry can be established. Preserve exact-version reads only.
        return [current_ref]
    reader = object.__new__(CoverageMissionAuthority)
    reader.connection = connection
    current = reader._mission_record(current_ref)
    if current["content_hash"] != mission.get("content_hash"):
        raise ValueError("document inventory mission hash differs from Core")
    versions, seen = [], set()
    cursor = current
    while True:
        if cursor["id"] in seen:
            raise ValueError("document inventory mission lineage contains a cycle")
        seen.add(cursor["id"])
        if cursor["mission_ref"] != current["mission_ref"]:
            raise ValueError("document inventory mission lineage crosses projects")
        if (cursor["industry_ref"] != current["industry_ref"] or
                cursor["autonomy"]["automation_principal"] != current["autonomy"]["automation_principal"]):
            break
        versions.append(cursor["id"])
        prior = cursor["prior_version_ref"]
        if prior is None:
            break
        cursor = reader._mission_record(prior)
    return versions


def registration_in_mission(connection: Any, mission: Mapping[str, Any],
                            source_authority: Mapping[str, Any]) -> bool:
    """Original ownership survives a verified project revision, never a different project."""
    ref = source_authority.get("mission_version_ref")
    if ref == mission["id"]:
        return True
    return ref in document_mission_versions(connection, mission)
