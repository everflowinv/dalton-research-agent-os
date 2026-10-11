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
from .document_research_inventory import financial_note_targets_for_registration
from .document_research_strategy import normalize_evidence_target
from .research_planner import SCHEMA_VERSION as PLANNER_SCHEMA_VERSION, TASK_REF
from .research_question_backlog import read_exact_backlog_question_version
from .research_task import inquiry_content_hash, inquiry_ref_for
from .contracts import ResultEnvelope, WorkOrder
from .research_planner import parse_response
from .store import DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
PURPOSE = "mission_directed_document_research"
WORKFLOW_CONTRACT_REF = "workflow:mission-directed-document-research:0.1"
_SCHEMA_PATH = Path(__file__).with_name("mission_document_research_schema.sql")


# The two identity fields that carry the governance envelope rather than the
# work itself.  A mandate version or a daily ceiling that has been re-signed
# since the admission was written rolls the mission version; the admission is
# still the same piece of research.
GOVERNANCE_ENVELOPE_FIELDS = ("mandate_binding", "outer_budget")

# These limits are the maximum paid authority the admission received.  A
# refreshed model configuration may narrow them, but a routine policy roll may
# not silently turn an old admission into a larger purchase.
MODEL_BUDGET_LIMIT_FIELDS = (
    "max_attempts", "max_input_tokens", "max_output_tokens", "max_cost_usd",
    "max_seconds", "max_elapsed_seconds",
)


class MissionDocumentResearchError(RuntimeError):
    pass


class MissionDocumentResearchWithdrawn(MissionDocumentResearchError):
    """The admission was withdrawn by its owner and is closed for good."""


class MissionDocumentResearchEquivalentPending(MissionDocumentResearchError):
    """An equivalent inquiry is already in flight, held, or just answered."""

    def __init__(self, equivalent: Mapping[str, Any]) -> None:
        self.equivalent = dict(equivalent)
        super().__init__(
            "equivalent_inquiry_" + str(equivalent.get("state"))
            + ":" + str(equivalent.get("admission_ref"))
            + ("；仍缺所需原文证据，暂不重复读取" if equivalent.get("state") == "waiting_evidence" else ""))


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


def _exact_plan(
    connection: sqlite3.Connection, plan_ref: str
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    return ({**dict(wire), "plan_id": row["plan_id"]}, dict(row))


def _execution_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """The paid work identity; prose-only plan provenance cannot rekey it.

    ``supersedes_withdrawal`` exists only on an admission written after the
    owner withdrew an otherwise identical one (2026-09-30): the same inquiry
    asked again is new paid authority, so it must not alias the withdrawn
    admission's identity -- and every admission without it keeps its bytes.
    """

    identity = {
        key: value[key] for key in (
            "schema_version", "workflow_contract_ref", "mission_version_ref",
            "mission_version_hash", "mission_ref", "company_ref", "actor_ref",
            "mandate_binding", "outer_budget", "inquiry_ref", "inquiry_hash",
            "question_version_ref", "question_version_hash",
            "document_authority_ref", "document_authority_hash", "source_ref",
            "request", "model_execution", "model_authority",
        )
    }
    if "numeric_authority" in value:
        identity["numeric_authority"] = value["numeric_authority"]
    if "supersedes_withdrawal" in value:
        identity["supersedes_withdrawal"] = value["supersedes_withdrawal"]
    return identity


# How many withdrawn predecessors one fresh admission may step over.  Each
# step needs a person's withdrawal, so this is a sanity bound, not a budget.
MAX_SUPERSEDED_WITHDRAWALS = 50

_AUTO_TARGET_REVISION = object()


# Pre-flight reasons that only say the *current* ceiling does not fit a
# profile.  An admission keeps its own, smaller ceiling, so they say nothing
# about the route it will actually take.
_BUDGET_PREFLIGHT_REASONS = frozenset({
    "context_window_insufficient", "model_output_limit_insufficient",
    "profile_input_limit_exceeded", "profile_output_limit_exceeded",
    "profile_total_limit_exceeded", "profile_cost_limit_exceeded",
    "work_order_cost_budget_exceeded",
})


def _authority_apart_from_budget(proof: Mapping[str, Any]) -> str:
    """A stage's model authority with only what a ceiling raise changes removed.

    The installed configuration's hash moves with any edit to it, and the
    candidates' budget pre-flight reasons move with the ceiling.  Everything
    else -- purpose, capability, routing policy and its hash, the exact
    candidate profile versions, the budget policy ceiling -- must still match.
    """

    body = dict(proof)
    body.pop("config_hash", None)
    candidates = body.get("configured_candidate_profiles")
    if isinstance(candidates, list):
        body["configured_candidate_profiles"] = [
            {**item, "preflight_reasons": sorted(
                set(item.get("preflight_reasons") or ()) - _BUDGET_PREFLIGHT_REASONS)}
            if isinstance(item, Mapping) else item
            for item in candidates
        ]
    return canonical_json(body)


def _settlement_instants(connection: sqlite3.Connection,
                         admission_refs: list[str]) -> dict[str, str]:
    """When each admission was answered: its first outcome, refusal or verdict.

    Read from the executor's own immutable tables; a Core without one of them
    simply has no settlements of that kind.
    """

    found: dict[str, str] = {}
    wanted = set(admission_refs)
    for query in (
        "SELECT n.admission_ref, n.created_at FROM mission_document_numeric_results n JOIN scheduler_formal_results f ON f.work_order_id=n.work_order_ref WHERE f.terminal_state='succeeded' AND json_extract(n.record_json,'$.status')='completed'",
        "SELECT admission_ref, created_at FROM mission_document_research_outcomes",
        "SELECT admission_ref, created_at FROM mission_document_research_candidate_rejections",
        "SELECT admission_ref, created_at FROM mission_document_research_observations "
        "WHERE outcome IN ('query_miss','no_verified_claim')",
    ):
        try:
            rows = connection.execute(query).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                continue
            raise
        for admission_ref, created_at in (tuple(row) for row in rows):
            if admission_ref in wanted and (
                    admission_ref not in found or created_at < found[admission_ref]):
                found[admission_ref] = created_at
    return found


class MissionDocumentResearchAuthority:
    """Append and re-resolve exact directed-document admissions."""

    _authorized = authorized_flag()

    def __init__(
        self, store: DaltonStore, *, registry: DocumentResearchRegistry,
        registration_resolver: Callable[[str], Mapping[str, Any]],
        model_execution_resolver: Callable[[], tuple[Mapping[str, Any], Mapping[str, Any]]],
        planner_scheduler_connection: sqlite3.Connection,
        planner_router_connection: sqlite3.Connection,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not isinstance(store, DaltonStore):
            raise TypeError("store must be DaltonStore")
        if not isinstance(registry, DocumentResearchRegistry):
            raise TypeError("registry must be DocumentResearchRegistry")
        if not callable(registration_resolver) or not callable(model_execution_resolver):
            raise TypeError("authority resolvers must be callable")
        if not callable(getattr(planner_scheduler_connection, "execute", None)):
            raise TypeError("planner_scheduler_connection must be a SQLite authority")
        if not callable(getattr(planner_router_connection, "execute", None)):
            raise TypeError("planner_router_connection must be a SQLite authority")
        self.store = store
        self.connection = store.connection
        self.registry = registry
        self.registration_resolver = registration_resolver
        self.model_execution_resolver = model_execution_resolver
        self.planner_scheduler_connection = planner_scheduler_connection
        self.planner_router_connection = planner_router_connection
        self.clock = clock
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_document_research_authorized"
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # CoverageMissionAuthority installs and migrates its schema in its
        # constructor.  Do that once while this authority itself is being
        # initialized: constructing it from a read path would make SQLite's
        # executescript commit a caller-owned Ledger transaction.
        self._missions = CoverageMissionAuthority(self.store)

    def _planner_origin(
        self, plan: Mapping[str, Any], row: Mapping[str, Any],
        inquiry: Mapping[str, Any], mission: Mapping[str, Any],
    ) -> None:
        """Require the selected inquiry in the exact successful planner output."""

        if (
            row.get("decided_by") != mission["autonomy"]["automation_principal"]
            or not isinstance(row.get("work_order_ref"), str)
            or not row["work_order_ref"]
        ):
            raise MissionDocumentResearchError(
                "planner plan lacks automation execution authority"
            )
        scheduler = self.planner_scheduler_connection
        work_row = scheduler.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?", (row["work_order_ref"],),
        ).fetchone()
        formal = scheduler.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (row["work_order_ref"],),
        ).fetchone()
        if work_row is None or formal is None:
            raise MissionDocumentResearchError("planner Scheduler authority is unavailable")
        try:
            work_raw = json.loads(work_row["work_order_json"])
            work = WorkOrder.from_dict(work_raw).to_dict()
            envelope_raw = json.loads(formal["result_envelope_json"])
            envelope = ResultEnvelope.from_dict(envelope_raw).to_dict()
            parsed = parse_response(envelope["outputs"]["text"])
        except Exception as exc:
            raise MissionDocumentResearchError("planner Scheduler authority is invalid") from exc
        if (
            canonical_json(work) != work_row["work_order_json"]
            or content_hash(work) != work_row["work_order_hash"]
            or work["id"] != row["work_order_ref"]
            or work["metadata"].get("control_plane") != "cockpit"
            or work["metadata"].get("purpose") != "plan"
            or work["metadata"].get("mission_version_ref") != mission["id"]
            or not str(work["metadata"].get("request_id", "")).startswith(
                plan["state_hash"][:32]
            )
            or formal["terminal_state"] != "succeeded"
            or formal["result_envelope_id"] != envelope["id"]
            or formal["result_envelope_hash"] != content_hash(envelope)
            or canonical_json(envelope) != formal["result_envelope_json"]
            or envelope["work_order_ref"] != work["id"]
            or envelope["status"] != "succeeded"
            or not isinstance(envelope.get("invocation_ref"), str)
            or not envelope["invocation_ref"]
            or set(envelope["outputs"]) != {"text", "content_hash"}
        ):
            raise MissionDocumentResearchError("planner Scheduler authority drifted")
        import hashlib

        text = envelope["outputs"]["text"]
        if envelope["outputs"]["content_hash"] != hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest():
            raise MissionDocumentResearchError("planner output text binding drifted")
        raw_matches = []
        for candidate in parsed["inquiries"]:
            normalized = {
                "company_ref": candidate.get("company_ref"),
                "question": candidate["question"].strip(),
                "wants": candidate["wants"].strip(),
                "because": candidate["because"].strip(),
            }
            if "canonical_question" in candidate:
                normalized["canonical_question"] = candidate["canonical_question"]
            if candidate.get("repair_target_ref") is not None:
                normalized["repair_target_ref"] = candidate["repair_target_ref"]
            if candidate.get("directed_document") is not None:
                normalized["directed_document"] = candidate["directed_document"]
                if candidate["directed_document"].get("numeric_targets") is not None:
                    from .document_research_strategy import normalize_strategy
                    normalized["directed_document"] = normalize_strategy(candidate["directed_document"],
                        max_query_terms=self.registry.policy["max_query_terms"],
                        max_query_term_chars=self.registry.policy["max_query_term_chars"])
            projected = {
                key: value for key, value in inquiry.items()
                if key not in {"rank", "repair_target_hash"}
            }
            if canonical_json(normalized) == canonical_json(projected):
                raw_matches.append(candidate)
        if len(raw_matches) != 1:
            raise MissionDocumentResearchError(
                "selected inquiry is absent or ambiguous in planner formal output"
            )
        route_ref = envelope["metadata"].get("route_decision_ref")
        route_row = self.planner_router_connection.execute(
            "SELECT * FROM model_route_decisions WHERE decision_id=?",
            (route_ref,),
        ).fetchone()
        if route_row is None:
            raise MissionDocumentResearchError("planner route authority is unavailable")
        try:
            route = json.loads(route_row["decision_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionDocumentResearchError("planner route authority is invalid") from exc
        route_body = dict(route)
        route_hash = route_body.pop("content_hash", None)
        if (
            canonical_json(route) != route_row["decision_json"]
            or route_hash != route_row["decision_hash"]
            or route_hash != content_hash(route_body)
            or route.get("id") != route_ref
            or route.get("outcome") != "selected"
            or route.get("work_order_ref") != work["id"]
            or route.get("work_order_hash") != work_row["work_order_hash"]
            or route.get("selected_profile_version_ref")
            != envelope["metadata"].get("profile_version_ref")
            or route_row["work_order_id"] != work["id"]
            or route_row["work_order_hash"] != work_row["work_order_hash"]
        ):
            raise MissionDocumentResearchError("planner route authority drifted")

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

    def _governing_mission(self, mission: Mapping[str, Any]) -> dict[str, Any]:
        """The mission version whose governance this admission runs under.

        An admission names the mission version its plan was written against,
        and that version is immutable.  The pointer is not: every budget
        revision and every policy signing rolls the mission to a new version,
        and until 2026-09-18 that silently killed every admission older than
        the last signing -- six live legacy admissions and their whole
        automatic-retry budget died on ``requires the active mission`` without
        a single model call, for a change that had nothing to do with them.

        The admission is the thing that must not move.  So a prior version of
        the *same* mission lineage still runs, under the active version's
        governance: the active mission's universe, source plan, autonomy,
        bindings and budget are what is checked, and the refusal is kept for
        what it was always for -- a company or a source the mission no longer
        covers, or a version that is not this lineage at all.
        """

        missions = self._missions
        active = missions.active_mission(mission["mission_ref"])
        if active["id"] == mission["id"]:
            return dict(active)
        seen, cursor = set(), active
        while cursor["id"] != mission["id"]:
            prior = cursor.get("prior_version_ref")
            if not isinstance(prior, str) or prior in seen:
                raise MissionDocumentResearchError(
                    "directed document research requires the active mission")
            seen.add(prior)
            try:
                cursor = missions.mission(prior)
            except Exception as exc:  # noqa: BLE001 - a broken chain is a refusal
                raise MissionDocumentResearchError(
                    "directed document research requires the active mission") from exc
            if cursor["mission_ref"] != mission["mission_ref"]:
                raise MissionDocumentResearchError(
                    "directed document research requires the active mission")
        if cursor["content_hash"] != mission["content_hash"]:
            raise MissionDocumentResearchError(
                "directed document research requires the active mission")
        if (active["industry_ref"] != mission["industry_ref"]
                or active["autonomy"]["automation_principal"]
                != mission["autonomy"]["automation_principal"]):
            # A different industry or a different automation principal is a
            # different mandate for the work, not a rolled envelope around it.
            raise MissionDocumentResearchError(
                "directed document research requires the active mission")
        return dict(active)

    def _mission(self, ref: str, digest: str, company_ref: str, source_ref: str) -> dict[str, Any]:
        missions = self._missions
        mission = missions.mission(ref)
        if mission["content_hash"] != digest:
            raise MissionDocumentResearchError("directed document research requires the active mission")
        governing = self._governing_mission(mission)
        if company_ref not in {item["company_ref"] for item in governing["universe"]}:
            raise MissionDocumentResearchError("company is outside the active mission universe")
        source = next((item for item in governing["source_plan"] if item["source_ref"] == source_ref), None)
        if source is None or source["status"] != "connected":
            raise MissionDocumentResearchError("selected document source is not connected")
        required = {"research_task", "model_run", "stage_record"}
        if not required.issubset(set(governing["autonomy"]["may_write"])):
            raise MissionDocumentResearchError("mission does not grant directed document execution")
        cur = self.connection.cursor()
        try:
            missions._validate_playbook_binding(cur, governing["bindings"]["playbook_version"])
            constitution = missions._validate_constitution_binding(
                cur, governing["bindings"]["constitution_version"], governing["industry_ref"]
            )
            mandate = missions._validate_mandate_binding(
                cur, governing["bindings"]["mandate_version"], governing["industry_ref"]
            )
        finally:
            cur.close()
        policy = self.store.active_policy_version().to_dict()
        bound = constitution["bindings"]["governance_policy_version"]
        if (
            constitution["bindings"]["mandate_version"] != governing["bindings"]["mandate_version"]
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
        # Same closed shape as every other research-budget verifier: the
        # three legacy caps required, the two simplification fields optional,
        # nothing else.
        from .coverage_mission import RESEARCH_BUDGET_REQUIRED_FIELDS, research_budget_shape_valid
        budget_fields = set(RESEARCH_BUDGET_REQUIRED_FIELDS)
        caps = []
        for name, parent in (("mandate", constraints), ("governance", policy["policy"])):
            cap = parent.get("research_budget")
            if not research_budget_shape_valid(cap):
                raise MissionDocumentResearchError(f"{name} lacks closed research budget authority")
            for key in budget_fields:
                if governing["budget"][key] > cap[key]:
                    raise MissionDocumentResearchError(f"mission exceeds {name} research budget")
            caps.append(cap)
        return {
            # Identity stays with the version this work was admitted under --
            # the plan, the registration and the planner WorkOrder are all
            # bound to it -- while every live constraint comes from the
            # version that governs now.
            **mission,
            "universe": governing["universe"],
            "source_plan": governing["source_plan"],
            "autonomy": governing["autonomy"],
            "bindings": governing["bindings"],
            "budget": governing["budget"],
            "mission_version_provenance": {
                "admitted_mission_version_ref": mission["id"],
                "admitted_mission_version_hash": mission["content_hash"],
                "governing_mission_version_ref": governing["id"],
                "governing_mission_version_hash": governing["content_hash"],
            },
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

    def question_origin_from_plan(
        self, *, plan_ref: str, inquiry_ref: str, document_authority_ref: str,
    ) -> dict[str, Any]:
        """Validate exact planner/source/mission authority before writing a question."""
        plan, plan_row = _exact_plan(self.connection, plan_ref)
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
        if isinstance(strategy, Mapping) and strategy.get("numeric_targets") is not None:
            from .directed_numeric_research import normalize_targets
            if normalize_targets(strategy.get("numeric_targets")) != strategy.get("numeric_targets"):
                raise MissionDocumentResearchError("stored numeric target normalization differs")
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
        mission = self._missions.mission(mission_ref)
        mission = self._mission(
            mission_ref, mission["content_hash"], inquiry["company_ref"], registration["source_ref"]
        )
        self._planner_origin(plan, plan_row, inquiry, mission)
        source_authority = registration["source_authority"]
        from .document_mission_lineage import registration_in_mission
        if (
            source_authority["kind"] != "coverage-mission-acquired-document"
            or not registration_in_mission(self.connection, mission, source_authority)
            or source_authority["company_ref"] != inquiry["company_ref"]
        ):
            raise MissionDocumentResearchError(
                "document registration belongs to another mission or company"
            )
        if "canonical_question" in inquiry:
            from .canonical_document_questions import resolve_binding
            try:
                resolve_binding(self.connection, mission, company_ref=inquiry["company_ref"],
                    binding=inquiry["canonical_question"], strategy=strategy)
            except ValueError as exc:
                raise MissionDocumentResearchError(str(exc)) from exc
        evidence_target = strategy.get("evidence_target")
        if evidence_target is not None:
            try:
                evidence_target = normalize_evidence_target(evidence_target)
                targets = financial_note_targets_for_registration(
                    connection=self.connection, mission=mission,
                    company_ref=inquiry["company_ref"], registration=registration,
                )
            except Exception as exc:
                raise MissionDocumentResearchError(
                    "financial note target authority is unavailable") from exc
            if sum(target == evidence_target for target in targets) != 1:
                raise MissionDocumentResearchError(
                    "financial note target differs from current filing authority")
        return {
            "plan": plan, "plan_row": plan_row, "ordinal": ordinal,
            "inquiry": inquiry, "inquiry_hash": inquiry_hash, "strategy": strategy,
            "registration": registration, "mission": mission,
        }

    def _derive(
        self, *, plan_ref: str, inquiry_ref: str, question_version_ref: str,
        document_authority_ref: str, frozen_numeric_upgrades: list[dict[str, Any]] | None = None,
        frozen_target_revision: Any = _AUTO_TARGET_REVISION,
    ) -> dict[str, Any]:
        origin = self.question_origin_from_plan(
            plan_ref=plan_ref, inquiry_ref=inquiry_ref,
            document_authority_ref=document_authority_ref,
        )
        plan, plan_row, ordinal = origin["plan"], origin["plan_row"], origin["ordinal"]
        inquiry, inquiry_hash = origin["inquiry"], origin["inquiry_hash"]
        strategy, registration, mission = origin["strategy"], origin["registration"], origin["mission"]
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
        if "numeric_targets" in strategy:
            from .research_auto_commit import _policy_rule
            from .research_verification import MISSION_VERIFIED_FIGURE_RULE_REF
            rule = _policy_rule(self.store.active_policy())
            if MISSION_VERIFIED_FIGURE_RULE_REF not in rule["rules"]:
                raise MissionDocumentResearchError("numeric verified-figure policy authority is unavailable")
            governing = self._governing_mission(mission)
            if not {"claim", "evidence", "stage_record"}.issubset(set(governing["autonomy"]["may_write"])):
                raise MissionDocumentResearchError("numeric mission does not grant typed Figure/Claim writes")
        numeric_upgrades = []
        selected_upgrade = strategy.get("replaces_numeric_rejection")
        if selected_upgrade is not None:
            from .directed_numeric_research import numeric_rejection
            from .document_inquiry_equivalence import inquiry_signature_for, signatures_equivalent
            proof=numeric_rejection(self.connection,selected_upgrade["admission_ref"])
            if proof is None or any(proof[k]!=selected_upgrade[k] for k in selected_upgrade):
                raise MissionDocumentResearchError("numeric capability upgrade lacks exact numeric rejection")
            old=self.admission(proof["admission_ref"])
            if old["company_ref"]!=inquiry["company_ref"] or signatures_equivalent(inquiry_signature_for(inquiry["company_ref"],inquiry),inquiry_signature_for(inquiry["company_ref"],old["planner_inquiry"])) is None:
                raise MissionDocumentResearchError("numeric capability upgrade belongs to another question/company")
            numeric_upgrades.append(proof)
            # Freeze every existing equivalent capability rejection on admission;
            # replay validates these exact immutable proofs, never rescans future history.
            if frozen_numeric_upgrades is None:
                refs = [r[0] for r in self.connection.execute(
                    "SELECT admission_id FROM mission_document_research_admissions WHERE company_ref=? ORDER BY admission_id",
                    (inquiry["company_ref"],)).fetchall()]
                additional = [numeric_rejection(self.connection, ref) for ref in refs if ref != proof["admission_ref"]]
            else:
                if proof not in frozen_numeric_upgrades:
                    raise MissionDocumentResearchError("numeric selected upgrade proof is missing")
                additional = [p for p in frozen_numeric_upgrades if p != proof]
            for item in additional:
                if item is None:
                    continue
                actual = numeric_rejection(self.connection, item["admission_ref"])
                if actual != item:
                    raise MissionDocumentResearchError("numeric equivalent rejection proof drifted")
                prior = self.admission(item["admission_ref"])
                equivalent = prior["company_ref"] == inquiry["company_ref"] and signatures_equivalent(
                    inquiry_signature_for(inquiry["company_ref"], inquiry),
                    inquiry_signature_for(inquiry["company_ref"], prior["planner_inquiry"])) is not None
                if equivalent:
                    numeric_upgrades.append(item)
                elif frozen_numeric_upgrades is not None:
                    raise MissionDocumentResearchError("numeric upgrade proof belongs to another question")
            numeric_upgrades.sort(key=lambda item: item["admission_ref"])
        elif frozen_numeric_upgrades:
            raise MissionDocumentResearchError("numeric upgrades lack selected planner authority")
        numeric_resume_proof = None
        selected_resume = strategy.get("resumes_numeric_result")
        if selected_resume is not None:
            from .directed_numeric_research import pending_numeric_result
            from .document_inquiry_equivalence import inquiry_signature_for, signatures_equivalent
            prior=pending_numeric_result(self.connection,selected_resume["admission_ref"])
            if prior is None or any(prior[k]!=selected_resume[k] for k in selected_resume):
                raise MissionDocumentResearchError("numeric source resume lacks exact incomplete result")
            old=prior["parent"];old_registration=old["request"]["registration"]
            if (old["company_ref"]!=inquiry["company_ref"] or old["numeric_authority"]["targets"]!=strategy["numeric_targets"]
                or old["planner_inquiry"]["directed_document"]["strategy_version"]!=strategy["strategy_version"]
                or signatures_equivalent(inquiry_signature_for(inquiry["company_ref"],inquiry),inquiry_signature_for(inquiry["company_ref"],old["planner_inquiry"])) is None):
                raise MissionDocumentResearchError("numeric resume needs the same question and complete required targets")
            numeric_resume_proof=dict(selected_resume)
        from .numeric_target_revision import derive_revision_proof, verify_revision_proof
        if frozen_target_revision is _AUTO_TARGET_REVISION:
            target_revision = derive_revision_proof(self, mission["mission_ref"], inquiry)
        elif frozen_target_revision is None:
            # Historical admissions did not authorize a revision: replay their
            # exact identity, never retrospectively manufacture this authority.
            target_revision = None
        else:
            target_revision = verify_revision_proof(
                self, mission["mission_ref"], inquiry, frozen_target_revision)
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
            retrieval = self.registry.search(request)
            if selected_resume is not None and old_registration["normalized_text"]["text_sha256"] == registration["normalized_text"]["text_sha256"]:
                previous_retrieval = self.registry.search(old["request"])
                def passages(proof):
                    return {(m["source_start"], m["source_end"], m["excerpt_sha256"]) for m in proof["matches"]}
                if not passages(retrieval) - passages(previous_retrieval):
                    raise MissionDocumentResearchError("numeric resume needs actual new source bytes or new verified passages")
        except MissionDocumentResearchError:
            raise
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
        from .directed_numeric_executor import source_binding
        from .directed_numeric_source_capabilities import SPOKEN_CONTRACT_REF
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
            **({"numeric_authority": {
                "contract_ref": "directed-document-numeric:0.1",
                **({"answer_contract_ref": SPOKEN_CONTRACT_REF}
                   if strategy["strategy_version"] == SPOKEN_CONTRACT_REF else {}),
                "capability_upgrade_proofs":numeric_upgrades,
                "resume_proof":numeric_resume_proof,
                **({"target_revision_proof": target_revision} if target_revision else {}),
                "targets": strategy["numeric_targets"],
                "source_binding": source_binding(
                    self.connection, registration, inquiry["company_ref"],
                    strategy_version=strategy["strategy_version"]),
                "metric_definitions": [{"metric_ref":target["metric_ref"],
                                        "metric_definition":target["metric_definition"],
                                        "metric_definition_hash":target["metric_definition_hash"],
                                        "unit":target["unit"], "scope":"question"}
                                       for target in strategy["numeric_targets"]],
            }} if "numeric_targets" in strategy else {}),
        }

    def admit_from_plan(self, **kwargs: Any) -> dict[str, Any]:
        from .mission_document_research_withdrawal import read_withdrawals

        identity = self._derive(**kwargs)
        # 2026-10-06: no new admission for a company the owner paused, nor any
        # during an emergency stop (owner_levers).
        from .owner_levers import assert_admission_open

        assert_admission_open(self.connection, identity.get("company_ref"))
        withdrawals = read_withdrawals(self.connection)
        for _step in range(MAX_SUPERSEDED_WITHDRAWALS + 1):
            identity_hash = content_hash(_execution_identity(identity))
            admission_id = "mission-document-research-admission:" + identity_hash[:32]
            withdrawn = withdrawals.get(admission_id)
            if withdrawn is None:
                break
            # The owner closed this exact admission.  Asking the same inquiry
            # again is a fresh admission that names what it supersedes.
            identity = {**identity, "supersedes_withdrawal": {
                "ref": withdrawn["id"], "hash": withdrawn["content_hash"],
                "admission_ref": admission_id,
            }}
        else:
            raise MissionDocumentResearchError(
                "mission document inquiry was withdrawn too many times")
        row = self.connection.execute(
            "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
            (admission_id,),
        ).fetchone()
        if row is not None:
            stored = self._read_row(row)
            self.resolve_for_execution(stored["id"])
            return {"status_marker": "duplicate", **stored}
        # New admissions must be executable by the existing Figure period gate.
        # Historical identities/replay remain governed by their original rules.
        if identity.get("numeric_authority") is not None:
            from .directed_numeric_research import assert_executable_target_periods
            assert_executable_target_periods(identity["numeric_authority"]["targets"])
        equivalent = self.equivalent_open_admission(identity)
        if equivalent is not None:
            raise MissionDocumentResearchEquivalentPending(equivalent)
        created_at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        wire = {**identity, "id": admission_id, "status": "admitted", "created_at": created_at,
                "identity_hash": identity_hash}
        wire["content_hash"] = content_hash(wire)
        with self._transaction() as cursor:
            # BEGIN IMMEDIATE serializes this one-result revision allowance.
            # Withdrawals remain in the immutable admission table and count as
            # having consumed it; neither a new plan nor owner closure resets it.
            revision = identity.get("numeric_authority", {}).get("target_revision_proof")
            if revision is not None:
                from .numeric_target_revision import revision_consumer
                consumer = revision_consumer(self, revision, company_ref=identity["company_ref"])
                if consumer is not None:
                    raise MissionDocumentResearchEquivalentPending({
                        "admission_ref": consumer["id"], "state": "target_revision_already_admitted",
                        "rule_ref": revision["rule_ref"], "numeric_result_hash": revision["numeric_result_hash"]})
            cursor.execute(
                "INSERT INTO mission_document_research_admissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (admission_id, identity_hash, wire["mission_version_ref"], wire["mission_version_hash"],
                 wire["plan_ref"], wire["plan_hash"], wire["inquiry_ref"], wire["inquiry_hash"],
                 wire["question_version_ref"], wire["question_version_hash"],
                 wire["document_authority_ref"], wire["document_authority_hash"],
                 wire["company_ref"], wire["source_ref"], wire["actor_ref"],
                 canonical_json(wire), wire["content_hash"], created_at,))
        return {"status_marker": "fresh", **wire}

    def equivalent_open_admission(
        self, identity: Mapping[str, Any], *, exclude_inquiry_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """An earlier admission asking the same thing that is not done with yet.

        The governed rule ``EQUIVALENT_INQUIRY_RULE_REF`` (2026-09-30).  Every
        new plan rephrases its inquiries, so the exact identity never matched
        and the same question was bought again and again: legacy DXC "970 /
        9.7 亿" about 17 times, CTSH 15.8% about 9, ws-7d GOOGL RPO 15.  Two
        inquiries are equivalent when they are about the same company and ask
        the same normalized question or name the same figure (see
        ``document_inquiry_equivalence``).  An equivalent blocks a new
        admission while it is in flight or held -- the owner closes a hopeless
        one with a withdrawal -- and for ``ANSWERED_COOLDOWN`` after it was
        answered.  A withdrawn admission never blocks.
        """

        from .document_inquiry_equivalence import (
            ANSWERED_COOLDOWN, EQUIVALENT_INQUIRY_RULE_REF, inquiry_signature_for,
            signatures_equivalent,
        )
        from .mission_document_research_withdrawal import read_withdrawals

        inquiry = identity.get("planner_inquiry")
        company_ref = identity.get("company_ref")
        if (not isinstance(inquiry, Mapping) or not isinstance(inquiry.get("question"), str)
                or not isinstance(company_ref, str)):
            return None
        mine = inquiry_signature_for(company_ref, inquiry)
        # The host performs this check before recording its backlog question.
        # Its inquiry already came from the exact formal planner output.
        from .directed_numeric_research import numeric_rejection, CONTRACT_REF
        from .directed_numeric_source_capabilities import NUMERIC_STRATEGIES
        strategy = inquiry.get("directed_document") or {}
        selected = strategy.get("replaces_numeric_rejection") or {}
        selected_upgrade_valid = False
        if strategy.get("strategy_version") in NUMERIC_STRATEGIES and selected:
            proof = numeric_rejection(self.connection, selected.get("admission_ref"))
            if proof is not None and all(selected.get(k) == proof[k] for k in selected):
                parent = self.admission(proof["admission_ref"])
                selected_upgrade_valid = parent["company_ref"] == company_ref and signatures_equivalent(
                    mine, inquiry_signature_for(company_ref, parent["planner_inquiry"])) is not None
        from .numeric_target_revision import derive_revision_proof, revision_consumer
        revision = identity.get("numeric_authority", {}).get("target_revision_proof")
        if "numeric_authority" not in identity and identity.get("mission_ref"):
            revision = derive_revision_proof(self, identity["mission_ref"], inquiry)
        consumer = revision_consumer(self, revision, company_ref=company_ref) if revision else None
        withdrawn = read_withdrawals(self.connection)
        rows = self.connection.execute(
            "SELECT admission_id, record_json, created_at "
            "FROM mission_document_research_admissions WHERE company_ref=? "
            "ORDER BY created_at, admission_id", (company_ref,),
        ).fetchall()
        candidates = []
        for admission_id, record_json, created_at in (tuple(row) for row in rows):
            if admission_id in withdrawn:
                continue
            try:
                record = json.loads(record_json)
                other = record["planner_inquiry"]
                other["question"]
            except (TypeError, ValueError, KeyError, RecursionError):
                continue
            if exclude_inquiry_ref is not None and record.get("inquiry_ref") == exclude_inquiry_ref:
                continue
            matched = signatures_equivalent(mine, inquiry_signature_for(company_ref, other))
            if inquiry.get("canonical_question") is not None:
                from .canonical_document_questions import binding_for
                verified_other = self.admission(admission_id)
                if (inquiry["canonical_question"] == binding_for(verified_other)
                        and all((inquiry.get("directed_document") or {}).get(k)
                            == (other.get("directed_document") or {}).get(k)
                            for k in ("document_ref", "document_version_hash"))):
                    matched = "canonical_question"
            if matched is not None:
                from .directed_numeric_research import numeric_rejection, CONTRACT_REF
                from .directed_numeric_source_capabilities import NUMERIC_STRATEGIES
                rejection = numeric_rejection(self.connection, admission_id)
                strategy = inquiry.get("directed_document") or {}
                if rejection is not None and strategy.get("strategy_version") in NUMERIC_STRATEGIES and (
                    rejection in identity.get("numeric_authority", {}).get("capability_upgrade_proofs", [])
                    or ("numeric_authority" not in identity and selected_upgrade_valid)):
                    # Different source-bound numeric workflow, never a revival of the refused admission.
                    continue
                if strategy.get("strategy_version") in NUMERIC_STRATEGIES:
                    from .directed_numeric_research import pending_numeric_result
                    pending=pending_numeric_result(self.connection,admission_id)
                    resume=strategy.get("resumes_numeric_result") or {}
                    if pending is not None and all(resume.get(k)==pending[k] for k in ("admission_ref","admission_hash","numeric_result_hash")):
                        # _derive independently proves actual new original bytes and unchanged complete targets.
                        continue
                    if pending is not None:
                        if revision and revision["admission_ref"] == admission_id:
                            if consumer is not None:
                                if consumer["inquiry_ref"] == exclude_inquiry_ref:
                                    continue  # exact inquiry falls through to its immutable duplicate.
                                return {"admission_ref": consumer["id"],
                                        "state": "target_revision_already_admitted",
                                        "matched_on": matched, "rule_ref": revision["rule_ref"],
                                        "numeric_result_hash": revision["numeric_result_hash"]}
                            continue
                        return {"admission_ref": admission_id, "state": "waiting_evidence",
                                "matched_on": matched, "numeric_result_hash": pending["numeric_result_hash"],
                                "rule_ref": EQUIVALENT_INQUIRY_RULE_REF}
                if rejection is not None:
                    return {"admission_ref": admission_id, "state":"numeric_strategy_required",
                            "matched_on":matched,"rejection_proof":rejection,
                            "rule_ref":"directed-document-numeric:capability-upgrade:0.1"}
                candidates.append((admission_id, created_at, matched))
        if not candidates:
            return None
        settled = _settlement_instants(self.connection, [item[0] for item in candidates])
        now = self.clock().astimezone(timezone.utc)
        for admission_id, created_at, matched in candidates:
            at = settled.get(admission_id)
            if at is None:
                state = "in_flight"
            else:
                moment = datetime.fromisoformat(at)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if now - moment >= ANSWERED_COOLDOWN:
                    continue
                state = "recently_answered"
            return {"admission_ref": admission_id, "state": state,
                    "matched_on": matched, "settled_at": at,
                    "admitted_at": created_at, "rule_ref": EQUIVALENT_INQUIRY_RULE_REF}
        return None

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
        if "numeric_authority" in wire:
            identity_fields.add("numeric_authority")
        if "supersedes_withdrawal" in wire:
            identity_fields.add("supersedes_withdrawal")
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
        if content_hash(_execution_identity(identity)) != row["identity_hash"]:
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

    def active_budget_mission(self, admission_ref: str) -> dict[str, Any]:
        """Re-resolve the exact active mission used by before-send accounting."""

        wire = self.admission(admission_ref)
        return self._mission(
            wire["mission_version_ref"], wire["mission_version_hash"],
            wire["company_ref"], wire["source_ref"],
        )

    def resolve_for_execution(self, admission_ref: str) -> dict[str, Any]:
        from .mission_document_research_withdrawal import read_withdrawals

        wire = self.admission(admission_ref)
        withdrawals = read_withdrawals(self.connection)
        if wire["id"] in withdrawals:
            # 2026-09-30: the owner closed it.  Nothing may execute, recover,
            # or be charged against a withdrawn admission again.
            raise MissionDocumentResearchWithdrawn(
                "mission document admission was withdrawn by its owner")
        superseded = wire.get("supersedes_withdrawal")
        if superseded is not None:
            prior = withdrawals.get(superseded.get("admission_ref")) if isinstance(
                superseded, Mapping) else None
            if (prior is None or prior["id"] != superseded.get("ref")
                    or prior["content_hash"] != superseded.get("hash")):
                raise MissionDocumentResearchError(
                    "superseded withdrawal authority is unavailable")
        identity = self._derive(
            plan_ref=wire["plan_ref"], inquiry_ref=wire["inquiry_ref"],
            question_version_ref=wire["question_version_ref"],
            document_authority_ref=wire["document_authority_ref"],
            frozen_numeric_upgrades=wire.get("numeric_authority", {}).get("capability_upgrade_proofs"),
            frozen_target_revision=wire.get("numeric_authority", {}).get("target_revision_proof"),
        )
        stored_identity = {
            key: wire[key] for key in wire
            if key not in {"id", "status", "created_at", "identity_hash", "content_hash"}
        }
        # The governance envelope -- which mandate version and which daily
        # ceiling this run is bound by -- is re-resolved on every call against
        # the mission version that governs *now* (see ``_governing_mission``),
        # so it is allowed to differ from the one the admission was written
        # with.  It is not unchecked: ``_mission`` has just revalidated the
        # whole chain.  Everything that says *what work this is* -- the plan,
        # the inquiry, the question, the document, the company, the source,
        # the request -- must still be byte-identical, or this admission is
        # genuinely no longer the work that was admitted.
        merged = {**identity, **{
            key: wire[key] for key in GOVERNANCE_ENVELOPE_FIELDS if key in wire
        }}
        if superseded is not None:
            merged["supersedes_withdrawal"] = superseded
        # Model policy/profile authority is also an execution-time envelope.
        # The installed resolver has just revalidated the workflow capability,
        # eligible current profiles, producer/verifier family independence and
        # registered budget policy.  Preserve the admitted bytes for audit and
        # cap the refreshed execution at the authority originally admitted.
        #
        # 2026-09-30: *cap*, not refuse.  An admission may never spend more
        # than it was admitted with, but a later raise of the installed
        # ceiling (the owner taking mission_directed_document_draft from 4096
        # to 16000 output tokens) is policy for new work; it used to raise
        # "exceeds admitted budget" in every child and owner recovery door of
        # every admission still in flight.  Each limit is now the smaller of
        # the admitted and the current value, and a stage whose only change
        # is such a raise keeps its admitted execution and authority
        # byte-for-byte, so its WorkOrders are not re-derived at all.
        executions: dict[str, Any] = {}
        authorities: dict[str, Any] = {}
        for stage in ("draft", "verifier"):
            admitted = wire["model_execution"][stage]
            current = identity["model_execution"][stage]
            capped = dict(current)
            raised = False
            for field in MODEL_BUDGET_LIMIT_FIELDS:
                if Decimal(str(current[field])) > Decimal(str(admitted[field])):
                    capped[field] = admitted[field]
                    raised = True
            stage_authority = identity["model_authority"][stage]
            if (raised and canonical_json(capped) == canonical_json(admitted)
                    and _authority_apart_from_budget(stage_authority)
                    == _authority_apart_from_budget(wire["model_authority"][stage])):
                capped = admitted
                stage_authority = wire["model_authority"][stage]
            executions[stage] = capped
            authorities[stage] = stage_authority
        identity = {**identity, "model_execution": executions,
                    "model_authority": authorities}
        merged["model_execution"] = wire["model_execution"]
        merged["model_authority"] = wire["model_authority"]
        if (
            canonical_json(merged) != canonical_json(stored_identity)
            or content_hash(_execution_identity(merged)) != wire["identity_hash"]
        ):
            raise MissionDocumentResearchError("mission document admission is no longer executable")
        if (
            canonical_json(identity["model_execution"])
            == canonical_json(wire["model_execution"])
            and canonical_json(identity["model_authority"])
            == canonical_json(wire["model_authority"])
        ):
            return wire
        refresh = {
            "schema_version": SCHEMA_VERSION,
            "admission_ref": wire["id"],
            "admission_hash": wire["content_hash"],
            "admitted_model_execution_hash": content_hash(wire["model_execution"]),
            "admitted_model_authority_hash": content_hash(wire["model_authority"]),
            "execution_model_execution_hash": content_hash(identity["model_execution"]),
            "execution_model_authority_hash": content_hash(identity["model_authority"]),
            "stages": {
                stage: {
                    "schema_version": SCHEMA_VERSION,
                    "stage": stage,
                    "admission_ref": wire["id"],
                    "admission_hash": wire["content_hash"],
                    "admitted_execution": wire["model_execution"][stage],
                    "admitted_authority": wire["model_authority"][stage],
                    "execution": identity["model_execution"][stage],
                    "execution_authority": identity["model_authority"][stage],
                    "changed": (
                        canonical_json(wire["model_execution"][stage])
                        != canonical_json(identity["model_execution"][stage])
                        or canonical_json(wire["model_authority"][stage])
                        != canonical_json(identity["model_authority"][stage])
                    ),
                }
                for stage in ("draft", "verifier")
            },
        }
        for stage in ("draft", "verifier"):
            refresh["stages"][stage]["content_hash"] = content_hash(
                refresh["stages"][stage]
            )
        refresh["content_hash"] = content_hash(refresh)
        return {
            **wire,
            "admitted_model_execution": wire["model_execution"],
            "admitted_model_authority": wire["model_authority"],
            "model_execution": identity["model_execution"],
            "model_authority": identity["model_authority"],
            "model_authority_refresh": refresh,
        }


__all__ = [
    "MissionDocumentResearchAuthority", "MissionDocumentResearchEquivalentPending",
    "MissionDocumentResearchError", "MissionDocumentResearchWithdrawn", PURPOSE,
    "SCHEMA_VERSION", "WORKFLOW_CONTRACT_REF",
]
