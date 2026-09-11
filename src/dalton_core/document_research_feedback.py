"""Feed exact directed-reading outcomes back into the next research plan.

These are execution observations, not investment evidence. In particular,
an unsuccessful query does not establish absence of information in a source.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .store import content_hash


def _read_observations(connection: Any, mission_version_ref: str):
    from .mission_document_research_executor import read_mission_document_research_observations
    return read_mission_document_research_observations(
        connection, mission_version_ref=mission_version_ref)


def read_document_research_feedback(core: Any, mission: Mapping[str, Any]) -> dict[str, Any]:
    companies = {item["company_ref"] for item in mission["universe"]}
    try:
        observations = _read_observations(core.connection, mission["id"])
        grouped = {ref: [] for ref in sorted(companies)}
        for observation in observations:
            if (observation.get("mission_version_ref") != mission["id"]
                    or observation.get("company_ref") not in companies):
                raise ValueError("directed-reading feedback escaped its mission/company")
            # Keep the validated observation identity with all attempted terms
            # and missing evidence, rather than reducing it to a success count.
            grouped[observation["company_ref"]].append(dict(observation))
        body = {"status": "available", "by_company": grouped}
    except Exception as exc:
        body = {"status": "unavailable", "by_company": {}, "reason": type(exc).__name__}
    return {**body, "content_hash": content_hash(body)}


def document_research_feedback_signature(core: Any) -> str:
    """Wake the planner even when a query wrote no new Claim or document."""
    import json
    try:
        missions = core.connection.execute(
            "SELECT v.record_json FROM coverage_mission_versions v "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=v.mission_version_id "
            "ORDER BY v.mission_version_id")
        body = [read_document_research_feedback(core, json.loads(row["record_json"]))
                for row in missions]
    except Exception as exc:
        body = {"status": "unavailable", "reason": type(exc).__name__}
    return content_hash(body)
