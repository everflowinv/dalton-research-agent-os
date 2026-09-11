"""Production admission host for planner-directed original-document reads."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from .annual_report_runtime import load_annual_report_model_config
from .document_research_inventory import load_document_inventory_authority
from .mission_document_model_authority import (
    MissionDocumentModelAuthority,
    load_mission_document_model_configs,
)
from .mission_document_research import MissionDocumentResearchAuthority
from .model_router import ModelRouter
from .readonly_sqlite import connect_read_only
from .research_question_backlog import ResearchQuestionBacklog
from .research_task import inquiry_content_hash, inquiry_ref_for
from .store import canonical_json


class MissionDocumentAdmissionHostError(RuntimeError):
    pass


def _registration_index(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise MissionDocumentAdmissionHostError(
            "document registration authority map is unavailable"
        )
    by_id: dict[str, dict[str, Any]] = {}
    for digest, raw in value.items():
        if (
            not isinstance(digest, str)
            or not isinstance(raw, Mapping)
            or raw.get("content_hash") != digest
            or not isinstance(raw.get("id"), str)
            or not raw["id"]
            or raw["id"] in by_id
        ):
            raise MissionDocumentAdmissionHostError(
                "document registration authority map drifted"
            )
        by_id[raw["id"]] = dict(raw)
    return by_id


def registration_index(value: Any) -> dict[str, dict[str, Any]]:
    """Public closed re-indexing used by read-only admission previews."""

    return _registration_index(value)


@contextmanager
def open_mission_document_admission_authority(
    *,
    store: Any,
    state_dir: str | Path,
    mission: Mapping[str, Any],
    planner_scheduler_db: str | Path,
    planner_model_config_path: str | Path,
    document_config_path: str | Path | None = None,
    draft_model_config_path: str | Path | None = None,
    verifier_model_config_path: str | Path | None = None,
    clock: Any = None,
) -> Iterator[tuple[MissionDocumentResearchAuthority, dict[str, dict[str, Any]]]]:
    """Open read-only Router/Scheduler inputs and the writer-owned authority."""

    state = Path(state_dir).expanduser().resolve()
    inventory = load_document_inventory_authority(
        core=store, mission=mission, state_dir=state,
        config_path=document_config_path,
    )
    registry = inventory.get("registry")
    if registry is None:
        raise MissionDocumentAdmissionHostError(
            "document research inventory is not configured"
        )
    registrations = _registration_index(inventory.get("registration_by_hash"))
    setup_error: Exception | None = None
    try:
        planner_config = load_annual_report_model_config(
            planner_model_config_path, "research planner model configuration"
        )
    except Exception as exc:
        raise MissionDocumentAdmissionHostError(str(exc)) from exc
    model_authority = None
    document_router = None
    planner_router = None
    planner_scheduler = None
    try:
        # These are evidence readers. The admission is the sole write and uses
        # the caller-owned Core connection below.
        loaded_document_configs = load_mission_document_model_configs(
            state,
            draft_path=draft_model_config_path,
            verifier_path=verifier_model_config_path,
        )
        document_router = ModelRouter(
            loaded_document_configs[0]["model_router_db"], read_only=True,
        )
        document_configs = MissionDocumentModelAuthority(
            state_dir=state,
            router=document_router,
            draft_config_path=draft_model_config_path,
            verifier_config_path=verifier_model_config_path,
            **({} if clock is None else {"clock": clock}),
        )
        model_authority = document_configs
        planner_router = ModelRouter(planner_config["model_router_db"], read_only=True)
        planner_scheduler = connect_read_only(planner_scheduler_db)
        planner_scheduler.row_factory = sqlite3.Row
        authority = MissionDocumentResearchAuthority(
            store,
            registry=registry,
            registration_resolver=lambda ref: registrations[ref],
            model_execution_resolver=model_authority,
            planner_scheduler_connection=planner_scheduler,
            planner_router_connection=planner_router.connection,
            **({} if clock is None else {"clock": clock}),
        )
    except Exception as exc:
        setup_error = exc
    if setup_error is not None:
        for item in (planner_scheduler, planner_router, document_router):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass
        if isinstance(setup_error, MissionDocumentAdmissionHostError):
            raise setup_error
        raise MissionDocumentAdmissionHostError(str(setup_error)) from setup_error
    try:
        yield authority, registrations
    finally:
        for item in (planner_scheduler, planner_router, document_router):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass


def admit_directed_inquiry(
    *,
    authority: MissionDocumentResearchAuthority,
    registrations: Mapping[str, Mapping[str, Any]],
    backlog: ResearchQuestionBacklog,
    mission: Mapping[str, Any],
    plan: Mapping[str, Any],
    inquiry: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the exact planner question, then admit only its selected source."""

    strategy = inquiry.get("directed_document")
    if not isinstance(strategy, Mapping):
        raise MissionDocumentAdmissionHostError(
            "planner inquiry has no directed document strategy"
        )
    registration = next(
        (
            dict(item) for item in registrations.values()
            if item.get("content_hash") == strategy.get("document_version_hash")
            and item.get("document_ref") == strategy.get("document_ref")
        ),
        None,
    )
    if registration is None:
        raise MissionDocumentAdmissionHostError(
            "selected document registration is unavailable"
        )
    digest = inquiry_content_hash(inquiry)
    origin = authority.question_origin_from_plan(
        plan_ref=plan["plan_id"], inquiry_ref=inquiry_ref_for(digest),
        document_authority_ref=registration["id"],
    )
    if (origin["mission"]["id"] != mission["id"]
            or origin["mission"]["content_hash"] != mission["content_hash"]
            or origin["plan"]["content_hash"] != plan["content_hash"]
            or canonical_json(origin["inquiry"]) != canonical_json(inquiry)):
        raise MissionDocumentAdmissionHostError("caller differs from the exact planner question origin")
    # Derive the write from replayed server authority, never caller fields.
    mission, inquiry, registration = origin["mission"], origin["inquiry"], origin["registration"]
    principal = mission["autonomy"]["automation_principal"]
    question = backlog.record_question(
        mandate_version_ref=mission["bindings"]["mandate_version"]["ref"],
        company_ref=inquiry["company_ref"],
        question=inquiry["question"].strip(),
        answer_criteria=inquiry["wants"].strip(),
        source_refs=[registration["source_ref"]],
        actor_ref=principal,
        idempotency_key="mission-document:question:v2:" + digest[:32],
        mission_binding={"ref": mission["id"], "hash": mission["content_hash"]},
    )
    admitted = authority.admit_from_plan(
        plan_ref=plan["plan_id"],
        inquiry_ref=inquiry_ref_for(digest),
        question_version_ref=question["question_version_ref"],
        document_authority_ref=registration["id"],
    )
    return {
        "kind": "mission_directed_document_research",
        "inquiry_ref": inquiry_ref_for(digest),
        "company_ref": inquiry["company_ref"],
        "question_version_ref": question["question_version_ref"],
        "document_authority_ref": registration["id"],
        "admission_ref": admitted["id"],
        "status": admitted["status_marker"],
    }


__all__ = [
    "MissionDocumentAdmissionHostError",
    "admit_directed_inquiry",
    "open_mission_document_admission_authority",
    "registration_index",
]
