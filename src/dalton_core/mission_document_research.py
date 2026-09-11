"""Mission authority for one exact planner-directed read of an acquired document.

The planner chooses only a logical document/version and query.  This authority
re-reads the immutable plan and ResearchQuestionVersion, obtains the complete
registration from a server-side locator, and replays the source adapter before
recording an admission.  No caller-supplied question, source, path, ticket,
model route, or budget is accepted.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .agenda import read_exact_mandate_version
from .coverage_mission import CoverageMissionAuthority
from .document_research import (
    DocumentResearchRegistry, SEARCH_OPERATION, SEARCH_REQUEST_SCHEMA_VERSION,
    validate_registration,
)
from .research_planner import SCHEMA_VERSION as PLANNER_SCHEMA_VERSION, TASK_REF
from .research_question_backlog import read_exact_backlog_question_version
from .research_task import inquiry_content_hash, inquiry_ref_for
from .store import DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
PURPOSE = "mission_directed_document_research"
WORKFLOW_CONTRACT_REF = "workflow:mission-directed-document-research:0.1"
_SCHEMA_PATH = Path(__file__).with_name("mission_document_research_schema.sql")


class MissionDocumentResearchError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissionDocumentResearchError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise MissionDocumentResearchError(f"{name} must be lowercase SHA-256")
    return value


def _json(raw: Any, name: str) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise MissionDocumentResearchError(f"{name} is invalid") from exc
    if canonical_json(value) != raw:
        raise MissionDocumentResearchError(f"{name} is not canonical")
    return value


def _exact_plan(connection: sqlite3.Connection, plan_ref: str) -> dict[str, Any]:
    plan_ref = _text(plan_ref, "plan_ref")
    row = connection.execute(
        "SELECT * FROM coverage_mission_research_plans WHERE plan_id=?", (plan_ref,)
    ).fetchone()
    if row is None:
        raise MissionDocumentResearchError("stored planner plan is unavailable")
    if "plan_json" not in row.keys() or row["plan_json"] is None:
        raise MissionDocumentResearchError("stored planner plan lacks exact record authority")
    wire = _json(row["plan_json"], "exact planner plan")
    if not isinstance(wire, Mapping):
        raise MissionDocumentResearchError("exact planner plan is not an object")
    body = dict(wire)
    digest = body.pop("content_hash", None)
    expected_ref = "mission-research-plan:" + content_hash({
        "mission_version_ref": row["mission_version_ref"],
        "state_hash": row["state_hash"],
    })[:32]
    if (
        digest != content_hash(body)
        or row["content_hash"] != digest
        or row["plan_id"] != expected_ref
        or wire.get("schema_version") != PLANNER_SCHEMA_VERSION
        or wire.get("task_ref") != TASK_REF
        or wire.get("state_hash") != row["state_hash"]
        or wire.get("mission_version_ref") != row["mission_version_ref"]
        or wire.get("assessment") != row["assessment"]
        or canonical_json(wire.get("directives")) != row["directives_json"]
        or canonical_json(wire.get("inquiries")) != row["inquiries_json"]
        or canonical_json(wire.get("sufficiency")) != (row["sufficiency_json"] or "[]")
        or not isinstance(wire.get("inquiries"), list)
    ):
        raise MissionDocumentResearchError("stored planner plan authority drifted")
    return {**dict(wire), "plan_id": row["plan_id"]}


class MissionDocumentResearchAuthority:
    """Append and re-resolve exact directed-document admissions."""

    _authorized = authorized_flag()

    def __init__(
        self, store: DaltonStore, *, registry: DocumentResearchRegistry,
        registration_resolver: Callable[[str], Mapping[str, Any]],
        model_execution_resolver: Callable[[], tuple[Mapping[str, Any], Mapping[str, Any]]],
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not isinstance(store, DaltonStore):
            raise TypeError("store must be DaltonStore")
        if not isinstance(registry, DocumentResearchRegistry):
            raise TypeError("registry must be DocumentResearchRegistry")
        if not callable(registration_resolver) or not callable(model_execution_resolver):
            raise TypeError("authority resolvers must be callable")
        self.store = store
        self.connection = store.connection
        self.registry = registry
        self.registration_resolver = registration_resolver
        self.model_execution_resolver = model_execution_resolver
        self.clock = clock
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_document_research_authorized"
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("MissionDocumentResearchAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cursor:
                yield cursor
        finally:
            self._authorized = False

    def _mission(self, ref: str, digest: str, company_ref: str, source_ref: str) -> dict[str, Any]:
        missions = CoverageMissionAuthority(self.store)
        mission = missions.mission(ref)
        active = missions.active_mission(mission["mission_ref"])
        if active["id"] != ref or active["content_hash"] != digest:
            raise MissionDocumentResearchError("directed document research requires the active mission")
        if company_ref not in {item["company_ref"] for item in mission["universe"]}:
            raise MissionDocumentResearchError("company is outside the active mission universe")
        source = next((item for item in mission["source_plan"] if item["source_ref"] == source_ref), None)
        if source is None or source["status"] != "connected":
            raise MissionDocumentResearchError("selected document source is not connected")
        required = {"research_task", "model_run", "stage_record"}
        if not required.issubset(set(mission["autonomy"]["may_write"])):
            raise MissionDocumentResearchError("mission does not grant directed document execution")
        cur = self.connection.cursor()
        try:
            missions._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
            constitution = missions._validate_constitution_binding(
                cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
            )
            mandate = missions._validate_mandate_binding(
                cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
            )
        finally:
            cur.close()
        policy = self.store.active_policy_version().to_dict()
        bound = constitution["bindings"]["governance_policy_version"]
        if (
            constitution["bindings"]["mandate_version"] != mission["bindings"]["mandate_version"]
            or policy["id"] != bound["ref"]
            or policy["content_hash"] != bound["hash"]
        ):
            raise MissionDocumentResearchError("mission governance authority is stale")
        moment = self.clock().astimezone(timezone.utc)
        for field, end in (("effective_from", False), ("effective_until", True)):
            value = policy[field]
            if value is None:
                continue
            boundary = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if boundary.tzinfo is None or (boundary <= moment if end else boundary > moment):
                raise MissionDocumentResearchError("governance policy is outside its effective window")
        constraints = mandate["constraints"]
        if constraints.get("research_execution") is False:
            raise MissionDocumentResearchError("mandate forbids research execution")
        budget_fields = {"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h"}
        caps = []
        for name, parent in (("mandate", constraints), ("governance", policy["policy"])):
            cap = parent.get("research_budget")
            if not isinstance(cap, Mapping) or set(cap) != budget_fields:
                raise MissionDocumentResearchError(f"{name} lacks closed research budget authority")
            for key in budget_fields:
                if mission["budget"][key] > cap[key]:
                    raise MissionDocumentResearchError(f"mission exceeds {name} research budget")
            caps.append(cap)
        return {
            **mission,
            "outer_budget": {
                "mandate_ref": mandate["mandate_ref"],
                "mandate_version_ref": mandate["id"],
                "mandate_version_hash": mandate["content_hash"],
                "governance_policy_ref": policy["policy_ref"],
                "governance_policy_version_ref": policy["id"],
                "governance_policy_version_hash": policy["content_hash"],
                "max_daily_paid_calls": min(int(cap["max_daily_paid_calls"]) for cap in caps),
                "max_daily_cost_micros": int(min(
                    Decimal(str(cap["max_daily_cost_usd"])) for cap in caps
                ) * 1_000_000),
            },
        }

    def _derive(
        self, *, plan_ref: str, inquiry_ref: str, question_version_ref: str,
        document_authority_ref: str,
    ) -> dict[str, Any]:
        plan = _exact_plan(self.connection, plan_ref)
        matches = []
        for ordinal, inquiry in enumerate(plan["inquiries"]):
            try:
                digest = inquiry_content_hash(inquiry)
            except Exception as exc:
                raise MissionDocumentResearchError("stored planner inquiry is invalid") from exc
            if inquiry_ref_for(digest) == inquiry_ref:
                matches.append((ordinal, inquiry, digest))
        if len(matches) != 1:
            raise MissionDocumentResearchError("inquiry is absent or ambiguous in the exact plan")
        ordinal, inquiry, inquiry_hash = matches[0]
        strategy = inquiry.get("directed_document")
        if not isinstance(strategy, Mapping):
            raise MissionDocumentResearchError("planner inquiry has no directed document strategy")
        try:
            registration = validate_registration(self.registration_resolver(document_authority_ref))
        except Exception as exc:
            raise MissionDocumentResearchError("document registration authority is unavailable") from exc
        if (
            registration["id"] != document_authority_ref
            or registration["document_ref"] != strategy.get("document_ref")
            or registration["content_hash"] != strategy.get("document_version_hash")
        ):
            raise MissionDocumentResearchError("document registration does not match planner strategy")
        mission_ref = plan["mission_version_ref"]
        mission = CoverageMissionAuthority(self.store).mission(mission_ref)
        mission = self._mission(
            mission_ref, mission["content_hash"], inquiry["company_ref"], registration["source_ref"]
        )
        source_authority = registration["source_authority"]
        if (
            source_authority["mission_version_ref"] is not None
            and (
                source_authority["mission_version_ref"] != mission["id"]
                or source_authority["company_ref"] != inquiry["company_ref"]
            )
        ):
            raise MissionDocumentResearchError(
                "document registration belongs to another mission or company"
            )
        try:
            question = read_exact_backlog_question_version(
                self.connection.cursor(), _text(question_version_ref, "question_version_ref")
            )
        except Exception as exc:
            raise MissionDocumentResearchError(
                "ResearchQuestionVersion authority is unavailable"
            ) from exc
        mandate = read_exact_mandate_version(
            self.connection.cursor(), mission["bindings"]["mandate_version"]["ref"]
        )
        if (
            question["content_hash"] != content_hash({key: value for key, value in question.items() if key != "content_hash"})
            or question["question"] != inquiry["question"]
            or question["answer_criteria"] != inquiry["wants"]
            or question["company_ref"] != inquiry["company_ref"]
            or question["mandate_ref"] != mandate["mandate_ref"]
            or question["source_refs"] != [registration["source_ref"]]
            or question["actor_ref"] != mission["autonomy"]["automation_principal"]
        ):
            raise MissionDocumentResearchError("ResearchQuestionVersion does not match the stored inquiry")
        policy = self.registry.policy
        request = {
            "schema_version": SEARCH_REQUEST_SCHEMA_VERSION,
            "operation": SEARCH_OPERATION,
            "purpose": PURPOSE,
            "research_question": question["question"],
            "registration": registration,
            "query_terms": list(strategy["query_terms"]),
            "limits": {
                "max_results": policy["max_results"],
                "context_before_chars": policy["max_context_before_chars"],
                "context_after_chars": policy["max_context_after_chars"],
            },
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy["content_hash"],
        }
        # Immediate source/manifest/raw replay; a registration wire is evidence,
        # not caller authority merely because its hash is self-consistent.
        try:
            self.registry.search(request)
        except Exception as exc:
            raise MissionDocumentResearchError(
                "document registration source replay failed"
            ) from exc
        try:
            executions, model_authority = self.model_execution_resolver()
            executions = json.loads(canonical_json(executions))
            model_authority = json.loads(canonical_json(model_authority))
        except Exception as exc:
            raise MissionDocumentResearchError("directed document model authority is unavailable") from exc
        if set(executions) != {"draft", "verifier"} or set(model_authority) != {"draft", "verifier"}:
            raise MissionDocumentResearchError("directed document model authority has an invalid shape")
        return {
            "schema_version": SCHEMA_VERSION,
            "workflow_contract_ref": WORKFLOW_CONTRACT_REF,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": inquiry["company_ref"],
            "actor_ref": mission["autonomy"]["automation_principal"],
            "mandate_binding": dict(mission["bindings"]["mandate_version"]),
            "outer_budget": dict(mission["outer_budget"]),
            "plan_ref": plan["plan_id"], "plan_hash": plan["content_hash"],
            "inquiry_ordinal": ordinal, "inquiry_ref": inquiry_ref,
            "inquiry_hash": inquiry_hash, "planner_inquiry": inquiry,
            "question_version_ref": question["id"],
            "question_version_hash": question["content_hash"],
            "document_authority_ref": registration["id"],
            "document_authority_hash": registration["content_hash"],
            "source_ref": registration["source_ref"],
            "request": request,
            "model_execution": executions,
            "model_authority": model_authority,
        }

    def admit_from_plan(self, **kwargs: Any) -> dict[str, Any]:
        identity = self._derive(**kwargs)
        identity_hash = content_hash(identity)
        admission_id = "mission-document-research-admission:" + identity_hash[:32]
        row = self.connection.execute(
            "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
            (admission_id,),
        ).fetchone()
        if row is not None:
            return {"status_marker": "duplicate", **self._read_row(row)}
        created_at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        wire = {**identity, "id": admission_id, "status": "admitted", "created_at": created_at,
                "identity_hash": identity_hash}
        wire["content_hash"] = content_hash(wire)
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO mission_document_research_admissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (admission_id, identity_hash, wire["mission_version_ref"], wire["mission_version_hash"],
                 wire["plan_ref"], wire["plan_hash"], wire["inquiry_ref"], wire["inquiry_hash"],
                 wire["question_version_ref"], wire["question_version_hash"],
                 wire["document_authority_ref"], wire["document_authority_hash"],
                 wire["company_ref"], wire["source_ref"], wire["actor_ref"],
                 canonical_json(wire), wire["content_hash"], created_at,))
        return {"status_marker": "fresh", **wire}

    def _read_row(self, row: sqlite3.Row) -> dict[str, Any]:
        wire = _json(row["record_json"], "mission document admission")
        if not isinstance(wire, Mapping):
            raise MissionDocumentResearchError("mission document admission is not an object")
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        identity_fields = {
            "schema_version", "workflow_contract_ref", "mission_version_ref",
            "mission_version_hash", "mission_ref", "company_ref", "actor_ref",
            "mandate_binding", "outer_budget", "plan_ref", "plan_hash",
            "inquiry_ordinal", "inquiry_ref", "inquiry_hash", "planner_inquiry",
            "question_version_ref", "question_version_hash",
            "document_authority_ref", "document_authority_hash", "source_ref",
            "request", "model_execution", "model_authority",
        }
        expected_fields = identity_fields | {
            "id", "status", "created_at", "identity_hash", "content_hash",
        }
        columns = {
            "id": row["admission_id"], "identity_hash": row["identity_hash"],
            "mission_version_ref": row["mission_version_ref"],
            "mission_version_hash": row["mission_version_hash"],
            "plan_ref": row["plan_ref"], "plan_hash": row["plan_hash"],
            "inquiry_ref": row["inquiry_ref"], "inquiry_hash": row["inquiry_hash"],
            "question_version_ref": row["question_version_ref"],
            "question_version_hash": row["question_version_hash"],
            "document_authority_ref": row["document_authority_ref"],
            "document_authority_hash": row["document_authority_hash"],
            "company_ref": row["company_ref"], "source_ref": row["source_ref"],
            "actor_ref": row["actor_ref"], "created_at": row["created_at"],
        }
        if (
            set(wire) != expected_fields
            or asserted != row["content_hash"] or asserted != content_hash(body)
            or any(wire.get(key) != value for key, value in columns.items())
            or wire.get("schema_version") != SCHEMA_VERSION
            or wire.get("workflow_contract_ref") != WORKFLOW_CONTRACT_REF
            or wire.get("status") != "admitted"
        ):
            raise MissionDocumentResearchError("mission document admission authority drifted")
        identity = {key: wire[key] for key in identity_fields}
        if content_hash(identity) != row["identity_hash"]:
            raise MissionDocumentResearchError("mission document admission identity drifted")
        return dict(wire)

    def admission(self, admission_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
            (_text(admission_ref, "admission_ref"),),
        ).fetchone()
        if row is None:
            raise MissionDocumentResearchError("mission document admission is unavailable")
        return self._read_row(row)

    def resolve_for_execution(self, admission_ref: str) -> dict[str, Any]:
        wire = self.admission(admission_ref)
        identity = self._derive(
            plan_ref=wire["plan_ref"], inquiry_ref=wire["inquiry_ref"],
            question_version_ref=wire["question_version_ref"],
            document_authority_ref=wire["document_authority_ref"],
        )
        if content_hash(identity) != wire["identity_hash"]:
            raise MissionDocumentResearchError("mission document admission is no longer executable")
        return wire


__all__ = [
    "MissionDocumentResearchAuthority", "MissionDocumentResearchError", PURPOSE,
    "SCHEMA_VERSION", "WORKFLOW_CONTRACT_REF",
]
