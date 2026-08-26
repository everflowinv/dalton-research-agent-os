"""Core-hosted SEC ``get_company_facts`` lane (S7d-1).

This is the importable, parameterised form of the Gate 1 canary
``scripts/run_sec_research_plan_canary.py``: perception snapshot -> agenda
cycle -> candidate -> decision -> backlog record/select ->
``create_company_facts_plan`` -> ``authorize_plan_by_policy`` ->
``control.start_plan`` -> executor ``run_once`` loop -> locate this plan's
staged candidate -> ``core.commit_policy_candidate`` -> policy-authorized
closure + replay check.

Differences from the canary, on purpose:

* It runs on an **existing** Core state directory.  The lane's WorkOrders
  live in ``core.sqlite`` because ``ResearchPlanExecutor`` requires the
  scheduler and the plan authority to share one Core connection; the lane
  never opens ``<state>/scheduler.sqlite``.
* It never installs or edits a Core governance policy.  If the active policy
  lacks the ``research_plan_auto_start`` / ``research_candidate_auto_commit``
  company-facts rules the lane raises :class:`LanePreconditionError` before
  any write.
* It never touches the live agenda policy pointer or mandate pointer: its
  own agenda policy / mandate are created with ``activate=False`` under fixed
  version ids and are idempotent.
* Candidate staging is a shared file (the Cockpit uses the same staging
  database), so the lane locates its candidate from the plan's own stage
  WorkOrder formal result, never from ``list_candidates()[0]``.
* Governance is injected.  The lane only relies on the small attribute set
  documented on :class:`GovernanceLike`; the committed implementation is
  provided by ``dalton_core.connector_governance`` (a parallel slice).
"""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .agenda import AgendaConflict, AgendaStore
from .authority_resolver import ConnectorAuthorityResolver
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorStore
from .connector_authority_port import ConnectorAuthorityPort
from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .observability import ObservabilityStore
from .raw_spool import RawSpool
from .research_auto_commit import (
    COMPANY_FACTS_RULE_REF as COMPANY_FACTS_AUTO_COMMIT_RULE_REF,
)
from .research_coordinator import ResearchCoordinatorStore
from .research_plan import (
    DEFAULT_REVENUE_CONCEPT_CANDIDATES,
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
    ResearchPlanAuthority,
    ResearchPlanControlPlane,
    _plan_work_orders,
)
from .research_plan_closure import ResearchPlanClosureCoordinator
from .research_plan_coordinator import ResearchPlanCoordinator
from .research_plan_executor import (
    ResearchPlanExecutor,
    re_read_stage_records,
    sec_connector_identity,
    sec_descriptor_spec,
)
from .research_question_backlog import ResearchQuestionBacklog
from .research_review import HumanReviewAuthority
from .research_verification import CandidateStagingStore
from .runner_journal import RunnerJournal
from .scheduler import Scheduler
from .sec_authority_harness import PUBLIC_PERMISSIONS, MutableClock, _PublicAuthorities
from .sec_public_adapter import SecPublicRouterAdapter
from .store import DaltonStore, content_hash

OPERATION = "get_company_facts"
LANE_KIND = "sec-company-facts-lane-v1"
LANE_SLUG = "us-it-services-sec-lane"
# v2: the binding scope is the whole coverage universe (plus any explicitly
# added issuer), never the subset selected for one run, so partial runs
# (one ticker at a time) share one immutable binding instead of conflicting.
AGENDA_BINDING_VERSION = "v2"
AGENDA_POLICY_VERSION_ID = f"agenda-policy-version:{LANE_SLUG}:{AGENDA_BINDING_VERSION}"
MANDATE_REF = f"mandate:{LANE_SLUG}"
MANDATE_VERSION_ID = f"mandate-version:{LANE_SLUG}:{AGENDA_BINDING_VERSION}"
QUESTION = "How did reported quarterly revenue change year over year?"
ANSWER_CRITERIA = "Return the exact same-filing SEC quarterly revenue comparison."
DEFAULT_USER_AGENT = "Dalton Research Agent SEC company-facts lane"
MAX_RUN_ONCE = 8
STAGE_RECORD_KINDS = ("candidate_evidence", "candidate_claim")


class LaneError(RuntimeError):
    """Any lane failure that is not a precondition problem."""


class LanePreconditionError(LaneError):
    """The Core / governance state does not authorize the lane; nothing was written."""


@dataclass(frozen=True)
class Issuer:
    ticker: str
    cik: str
    company_ref: str
    name: str


US_IT_SERVICES_ISSUERS: tuple[Issuer, ...] = (
    Issuer("ACN", "1467373", "company:sec-cik:0001467373", "Accenture plc"),
    Issuer("CTSH", "1058290", "company:sec-cik:0001058290", "Cognizant Technology Solutions Corp"),
    Issuer("EPAM", "1352010", "company:sec-cik:0001352010", "EPAM Systems Inc"),
    Issuer("IBM", "51143", "company:sec-cik:0000051143", "International Business Machines Corp"),
)

# Mirrors the executor's SEC rate policy (``connector-rate-policy:sec-public``):
# every call reserves ``max_response_bytes`` inside a 60-second UTC-aligned
# window whose byte limit is exactly one such reservation, so two issuers in
# the same window are rejected with ``ConnectorQuotaExceeded``.
SEC_QUOTA_WINDOW_SECONDS = 60


class GovernanceLike(Protocol):
    """The only governance surface the lane depends on."""

    id: str
    content_hash: str
    approved: bool
    policy_ref: str
    principal_ref: str
    approved_by: str
    effective_from: str
    allowed_permissions: dict[str, Any]

    def approval(self, query: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def policy(self, query: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def policy_hash(self) -> str: ...


class RehearsalGovernance(_PublicAuthorities):
    """In-memory approved governance for tests and dry runs only."""

    def __init__(self, *, approved_by: str, approved: bool = True) -> None:
        super().__init__()
        if not approved_by.startswith("human:") or len(approved_by) <= 6:
            raise LanePreconditionError("rehearsal governance approved_by must be human:<who>")
        self.approved_by = approved_by
        self.approved = approved
        self.policy_ref = "capability-policy:sec-public-research:rehearsal"
        self.principal_ref = "principal:sec-company-facts-lane"
        self.effective_from = "2026-08-15T07:00:00+00:00"
        self.allowed_permissions = copy.deepcopy(PUBLIC_PERMISSIONS)
        self.id = f"connector-governance:sec-public:rehearsal:{approved_by.split(':', 1)[1]}"
        self.content_hash = content_hash({"rehearsal": self.id, "approved": approved})

    def policy(self, query: Mapping[str, Any]) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "0.1",
            "policy_ref": query["policy_ref"],
            "effective_from": self.effective_from,
            "effective_until": None,
            "allowed_principal_refs": [self.principal_ref],
            "allowed_permissions": copy.deepcopy(self.allowed_permissions),
            "max_lease_seconds": 120,
        }
        wire["content_hash"] = content_hash(wire)
        return wire

    def policy_hash(self) -> str:
        return self.policy({"policy_ref": self.policy_ref})["content_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _agenda_policy(company_refs: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "enabled": True,
        "selected_count": 1,
        "max_model_calls_per_cycle": 1,
        "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5,
        "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000,
        "max_output_tokens": 2000,
        "feature_weights": {
            "mandate_relevance": 4,
            "catalyst_urgency": 3,
            "evidence_staleness": 2,
            "decision_impact": 4,
        },
        "trial_company_refs": list(company_refs),
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


def _integrity(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def check_core_governance_rules(core: DaltonStore) -> dict[str, Any]:
    """Raise :class:`LanePreconditionError` unless the active policy carries both lane rules."""

    try:
        active = core.active_policy()
    except Exception as exc:  # NotFound or corrupt policy
        raise LanePreconditionError(f"Core has no active governance policy: {exc}") from exc
    policy = active["policy"]
    problems = []
    start = policy.get("research_plan_auto_start")
    if not (
        isinstance(start, Mapping)
        and start.get("enabled") is True
        and start.get("rules") == [PLAN_COMPANY_FACTS_AUTO_START_RULE_REF]
    ):
        problems.append(
            "research_plan_auto_start must be {enabled: true, rules: "
            f"[{PLAN_COMPANY_FACTS_AUTO_START_RULE_REF!r}]}}"
        )
    commit = policy.get("research_candidate_auto_commit")
    if not (
        isinstance(commit, Mapping)
        and commit.get("enabled") is True
        and commit.get("rules") == [COMPANY_FACTS_AUTO_COMMIT_RULE_REF]
    ):
        problems.append(
            "research_candidate_auto_commit must be {enabled: true, rules: "
            f"[{COMPANY_FACTS_AUTO_COMMIT_RULE_REF!r}], max_records: N}}"
        )
    if problems:
        raise LanePreconditionError(
            "active Core governance policy "
            f"{active['policy_version_id']!r} does not authorize the SEC company-facts lane; "
            "the lane never installs governance policy itself. Install a new policy "
            "version through the governance CLI with: " + "; ".join(problems)
        )
    return active


class SecCompanyFactsLane:
    """Assemble the full research-plan stack on an existing Core state directory."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        staging_path: str | Path,
        governance: GovernanceLike,
        issuers: Sequence[Issuer] = US_IT_SERVICES_ISSUERS,
        catalog_db: str | Path | None = None,
        spool_dir: str | Path | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        adapter: Any | None = None,
        clock: MutableClock | None = None,
    ) -> None:
        if not getattr(governance, "approved", False):
            raise LanePreconditionError(
                f"connector governance {getattr(governance, 'id', '?')!r} is not approved; "
                "the SEC company-facts lane refuses to open Core"
            )
        if not issuers:
            raise LanePreconditionError("at least one issuer is required")
        self.governance = governance
        self.issuers = tuple(issuers)
        self.state_dir = Path(state_dir)
        self.staging_path = Path(staging_path)
        self.user_agent = user_agent
        self.clock = clock if clock is not None else MutableClock(datetime.now(timezone.utc))
        # A caller-supplied clock is a rehearsal clock: advance it instead of sleeping.
        self._realtime = clock is None
        self._opened = False

        self.core = DaltonStore(self.state_dir / "core.sqlite")
        try:
            self.observability = ObservabilityStore(self.core)
            self.agenda = AgendaStore(self.core)
            self.backlog = ResearchQuestionBacklog(self.core)
            self.plans = ResearchPlanAuthority(self.core)
            # Hard executor constraint: WorkOrders share the Core connection.
            self.scheduler = Scheduler(connection=self.core.connection)
            self.scheduler.clock = self.clock
            self.control = ResearchPlanControlPlane(
                self.plans, self.backlog, self.observability, self.scheduler
            )
            self.connectors = ConnectorStore(self.core, clock=self.clock)
            self.journal = RunnerJournal(self.core, clock=self.clock)
            spool_root = Path(spool_dir) if spool_dir is not None else self.state_dir / "connector-spool"
            self.spool = RawSpool(str(spool_root), max_total_bytes=1_000_000_000)
            self.catalog = CapabilityCatalog(
                str(catalog_db if catalog_db is not None else self.state_dir / "catalog.sqlite"),
                clock=self.clock,
                approval_resolver=governance.approval,
                policy_resolver=governance.policy,
            )
            self.connector_records = ResearchCoordinatorStore(
                str(self.state_dir / "research-coordinator.sqlite")
            )
            self.coordinator = ResearchPlanCoordinator(
                plan=self.plans,
                scheduler=self.scheduler,
                connector_records=self.connector_records,
            )
            self.staging = CandidateStagingStore(str(self.staging_path))
            self.review = HumanReviewAuthority(str(self.staging_path))
            self.resolver = ConnectorAuthorityResolver(
                core=self.core,
                connectors=self.connectors,
                observability=self.observability,
                scheduler=self.scheduler,
                coordinator=self.connector_records,
                artifact_reader=lambda artifact: self.spool.read_object(
                    artifact["artifact_content_hash"]
                ),
                runner_journal=self.journal,
            )
            self.template = load_packaged_connector_inventory()["templates"]["sec"]
            self.identity = sec_connector_identity(self.template, OPERATION)
            self.permissions = copy.deepcopy(dict(governance.allowed_permissions))
            # Wire timestamps carry microseconds; governance effective_from may not.
            self.created_at = _wire_time(datetime.fromisoformat(governance.effective_from))
            self.descriptor = self._ensure_descriptor()
            self.manifest = self._runner_manifest()
            self.adapter = (
                adapter if adapter is not None
                else SecPublicRouterAdapter(user_agent=user_agent, clock=self.clock)
            )
            binding = self.manifest["bindings"][0]
            expected_params = {
                "cik", "taxonomy", "concept_candidates", "unit", "form",
                "filed_from", "filed_to",
            }
            static_resolver = StaticAdapterResolver(
                self.manifest,
                {binding["binding_ref"]: self.adapter},
                {binding["binding_ref"]: lambda params: set(params) == expected_params},
            )
            gate = ConnectorRunnerAdmissionGate(
                scheduler=self.scheduler,
                catalog=self.catalog,
                connectors=self.connectors,
                resolver=static_resolver,
                visibility_scopes=["research"],
                clock=self.clock,
            )
            authority_port = ConnectorAuthorityPort(
                connectors=self.connectors,
                observability=self.observability,
                scheduler=self.scheduler,
            )
            self.transport = ConnectorTransportExecutor(
                gate=gate,
                journal=self.journal,
                spool=self.spool,
                authority=authority_port,
                connector_reader=self.connectors,
                clock=self.clock,
            )
            self.executor = ResearchPlanExecutor(
                plan=self.plans,
                scheduler=self.scheduler,
                connector_records=self.connector_records,
                coordinator=self.coordinator,
                connectors=self.connectors,
                catalog=self.catalog,
                transport=self.transport,
                resolver=self.resolver,
                staging=self.staging,
                clock=self.clock,
                permissions=self.permissions,
                policy_resolver=governance.policy,
                principal_ref=governance.principal_ref,
                runner_environment_hash=self.manifest["content_hash"],
                actor_ref=self.manifest["runner_actor_ref"],
                capability_policy_ref=governance.policy_ref,
            )
            self.closure = ResearchPlanClosureCoordinator(
                plan=self.plans,
                backlog=self.backlog,
                coordinator=self.coordinator,
                review=self.review,
            )
            self._opened = True
        except Exception:
            self.close()
            raise

    # ------------------------------------------------------------------ #

    def close(self) -> None:
        for name in ("review", "staging", "connector_records", "catalog"):
            obj = getattr(self, name, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.core.close()

    def __enter__(self) -> "SecCompanyFactsLane":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #

    def _ensure_descriptor(self):
        spec = sec_descriptor_spec(
            self.template,
            self.permissions,
            self.created_at,
            capability_policy_ref=self.governance.policy_ref,
            operation_name=OPERATION,
        )
        try:
            descriptor = self.catalog.describe(
                spec["id"], visibility_scopes=list(spec.get("visibility_scopes", ["research"]))
            )
        except CapabilityNotFound:
            descriptor = self.catalog.publish(spec)
        if (
            descriptor.source_hash != spec["source_hash"]
            or descriptor.schema_hash != spec["schema_hash"]
            or descriptor.eligibility.policy_ref != self.governance.policy_ref
            or descriptor.permissions.to_dict() != spec["permissions"]
        ):
            raise LanePreconditionError(
                "published SEC capability descriptor differs from the governed spec"
            )
        return descriptor

    def _runner_manifest(self) -> dict[str, Any]:
        identity = self.identity
        binding = {
            "binding_ref": "runner-binding:sec-public:v1",
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": identity["adapter_ref"],
            "adapter_hash": identity["adapter_hash"],
            "source_ref": identity["source_identity"]["source_ref"],
            "source_hash": identity["source_hash"],
            "operation": identity["operation"],
            "input_schema_ref": identity["input_schema_ref"],
            "input_schema_hash": identity["input_schema_hash"],
            "output_schema_ref": identity["output_schema_ref"],
            "output_schema_hash": identity["output_schema_hash"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": copy.deepcopy(self.permissions),
            "side_effects": ["read:public-http"],
            "rate_policy_ref": "connector-rate-policy:sec-public",
        }
        payload = {
            "schema_version": "0.1",
            "id": "runner-environment:sec-public:v1",
            "created_at": self.created_at,
            "runner_runtime_ref": "runner-runtime:sec-public:v1",
            "runner_actor_ref": "runner:research-plan-executor",
            "resolver_ref": "resolver:connector-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:sec-public:v1",
            "package_manifest_hash": "9" * 64,
            "bindings": [binding],
        }
        return validate_runner_environment_manifest(
            {**payload, "content_hash": content_hash(payload)}
        )

    # ------------------------------------------------------------------ #

    def ensure_agenda_bindings(self, *, actor_ref: str) -> dict[str, str]:
        """Create the lane's inactive agenda policy + mandate once (idempotent)."""

        if not actor_ref.startswith("human:"):
            raise LanePreconditionError("actor_ref must use the human: namespace")
        # Bind the coverage universe, not this run's subset: a later run for a
        # different ticker must replay the same idempotent binding request.
        company_refs = sorted(
            {issuer.company_ref for issuer in US_IT_SERVICES_ISSUERS}
            | {issuer.company_ref for issuer in self.issuers}
        )
        # Fixed effective window keeps the idempotent request hash stable
        # across runs; the lane only ever binds cycles to these exact versions.
        effective_from = "2026-08-01T00:00:00+00:00"
        effective_until = "2036-08-01T00:00:00+00:00"
        try:
            self._bind_agenda(company_refs, actor_ref, effective_from, effective_until)
        except AgendaConflict as exc:
            raise LanePreconditionError(
                f"lane agenda binding {AGENDA_POLICY_VERSION_ID!r} / {MANDATE_VERSION_ID!r} "
                "already exists for a different issuer universe or actor; the "
                f"{AGENDA_BINDING_VERSION} binding is immutable, pass the same "
                f"issuers/actor or bump AGENDA_BINDING_VERSION: {exc}"
            ) from exc
        return {
            "agenda_policy_version_ref": AGENDA_POLICY_VERSION_ID,
            "mandate_version_ref": MANDATE_VERSION_ID,
        }

    def _bind_agenda(
        self, company_refs: list[str], actor_ref: str, effective_from: str, effective_until: str
    ) -> None:
        policy_result = self.agenda.create_policy(
            _agenda_policy(company_refs),
            effective_from=effective_from,
            effective_until=effective_until,
            actor_ref=actor_ref,
            activate=False,
            version_id=AGENDA_POLICY_VERSION_ID,
            idempotency_key=f"agenda-policy:{LANE_SLUG}:{AGENDA_BINDING_VERSION}",
        )
        if policy_result.get("status") == "conflict":
            raise AgendaConflict("agenda policy idempotency conflict")
        mandate_result = self.agenda.create_mandate(
            MANDATE_REF,
            objective="Track reported quarterly revenue for US IT services issuers from SEC company facts",
            scope_refs=company_refs,
            constraints={"mode": "core_hosted_public_read_only_lane"},
            success_criteria={"policy_authorized_low_risk_loop": True},
            effective_from=effective_from,
            effective_until=effective_until,
            actor_ref=actor_ref,
            activate=False,
            version_id=MANDATE_VERSION_ID,
            idempotency_key=f"mandate:{LANE_SLUG}:{AGENDA_BINDING_VERSION}",
        )
        if mandate_result.get("status") == "conflict":
            raise AgendaConflict("mandate idempotency conflict")

    def _register_question(
        self, issuer: Issuer, *, filed_from: str, filed_to: str, run_key: str
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        suffix = content_hash({
            "lane": LANE_KIND, "company_ref": issuer.company_ref,
            "filed_from": filed_from, "filed_to": filed_to, "run_key": run_key,
        })[:16]
        snapshot = {
            "schema_version": "0.1",
            "snapshot_id": f"perception:{issuer.company_ref}:{filed_to}:{suffix}",
            # Deterministic so a same-parameter rerun replays the identical
            # idempotent request instead of conflicting on a new timestamp.
            "generated_at": f"{filed_to}T00:00:00.000000+00:00",
            "source_kind": LANE_KIND,
            "source_snapshot_hash": content_hash({"source": LANE_KIND, "suffix": suffix}),
            "company": {
                "slug": issuer.company_ref,
                "name": issuer.name,
                "ticker": issuer.ticker,
            },
            "catalysts": [{"event_key": "sec-company-facts", "title": "Bounded company-facts read"}],
            "evidence": [],
            "filings": [],
        }
        snapshot["content_hash"] = content_hash(snapshot)
        self.agenda.register_perception_snapshot(
            snapshot, actor_ref="core", idempotency_key=f"perception:{LANE_SLUG}:{suffix}",
        )
        cycle = self.agenda.start_cycle(
            f"agenda:{LANE_SLUG}:{suffix}",
            perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref=MANDATE_VERSION_ID,
            policy_version_ref=AGENDA_POLICY_VERSION_ID,
            company_ref=issuer.company_ref,
            actor_ref="core",
            cycle_id=f"agenda-cycle:{LANE_SLUG}:{suffix}",
            idempotency_key=f"cycle:{LANE_SLUG}:{suffix}",
        )
        self.agenda.add_candidates(
            cycle["cycle_id"],
            candidates=[{
                "candidate_id": f"candidate:{LANE_SLUG}:{suffix}",
                "company_ref": issuer.company_ref,
                "question": QUESTION,
                "answer_criteria": ANSWER_CRITERIA,
                "features": {
                    "mandate_relevance": 3, "catalyst_urgency": 2,
                    "evidence_staleness": 1, "decision_impact": 3,
                },
                "rationale": "Core-hosted SEC company-facts revenue lane",
                "source_refs": ["source:sec-edgar"],
            }],
            actor_ref="core",
            idempotency_key=f"candidates:{LANE_SLUG}:{suffix}",
        )
        decision = self.agenda.decide_cycle(
            cycle["cycle_id"],
            actor_ref="core",
            decision_id=f"decision:{LANE_SLUG}:{suffix}",
            idempotency_key=f"decision:{LANE_SLUG}:{suffix}",
        )
        record = self.backlog.record_question(
            mandate_version_ref=MANDATE_VERSION_ID,
            company_ref=issuer.company_ref,
            question=QUESTION,
            answer_criteria=ANSWER_CRITERIA,
            source_refs=["source:sec-edgar"],
            actor_ref="core",
            idempotency_key=f"question:{LANE_SLUG}:{suffix}",
        )
        self.backlog.select_question(
            question_ref=record["question_ref"],
            decision_ref=decision["id"],
            actor_ref="core",
            idempotency_key=f"select:{LANE_SLUG}:{suffix}",
        )
        return decision, record, suffix

    def _locate_candidate(self, plan_wire: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Find this plan's staged claim via its own stage WorkOrder formal result."""

        work_orders = _plan_work_orders(plan_wire)
        stage_order = work_orders[-1]
        formal = self.scheduler.formal_result(stage_order["id"])
        if formal is None or formal["terminal_state"] != "succeeded":
            raise LaneError("plan stage WorkOrder has no succeeded formal result")
        records = re_read_stage_records(formal, expected_kinds=STAGE_RECORD_KINDS)
        claim_ref = next(item["ref"] for item in records if item["kind"] == "candidate_claim")
        claim_hash = next(item["hash"] for item in records if item["kind"] == "candidate_claim")
        pair = self.review.candidate_bundle(claim_ref)
        claim, evidence = pair["claim"], pair["evidence"]
        if claim["content_hash"] != claim_hash:
            raise LaneError("staged candidate claim hash does not match the plan stage proof")
        return claim, evidence

    def run_issuer(
        self,
        issuer: Issuer,
        *,
        filed_from: str,
        filed_to: str,
        actor_ref: str,
        run_key: str,
        concept_candidates: Sequence[str] = DEFAULT_REVENUE_CONCEPT_CANDIDATES,
    ) -> dict[str, Any]:
        active = check_core_governance_rules(self.core)
        self.ensure_agenda_bindings(actor_ref=actor_ref)
        decision, record, suffix = self._register_question(
            issuer, filed_from=filed_from, filed_to=filed_to, run_key=run_key
        )
        created = self.plans.create_company_facts_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"],
            cik=issuer.cik,
            filed_from=filed_from,
            filed_to=filed_to,
            actor_ref="core:planner",
            concept_candidates=list(concept_candidates),
            idempotency_key=f"create-plan:{LANE_SLUG}:{suffix}",
        )
        plan_ref = created["plan_version_ref"]
        authorization = self.plans.authorize_plan_by_policy(
            plan_version_ref=plan_ref,
            idempotency_key=f"authorize-plan:{LANE_SLUG}:{plan_ref}",
        )
        self.control.start_plan(
            plan_version_ref=plan_ref,
            actor_ref="core:planner",
            idempotency_key=f"start-plan:{LANE_SLUG}:{plan_ref}",
        )
        plan_wire = self.plans.plan_version(plan_ref)

        outcomes = []
        for _ in range(MAX_RUN_ONCE):
            outcome = self.executor.run_once(plan_version_ref=plan_wire["id"])
            outcomes.append(outcome)
            if outcome["status"] in {"complete", "blocked"}:
                break
            if outcome["status"] not in {"admitted", "succeeded"}:
                break
        summary: dict[str, Any] = {
            "issuer": issuer.__dict__,
            "plan": {
                "ref": plan_wire["id"],
                "hash": plan_wire["content_hash"],
                "authorization_ref": authorization["authorization"]["id"],
                "governance_policy_version_ref": active["policy_version_id"],
                "parameters": plan_wire["execution_scope"]["parameters"],
            },
            "outcomes": outcomes,
        }
        if outcomes[-1]["status"] != "complete":
            summary["status"] = "blocked"
            summary["failure"] = outcomes[-1]
            return self._finish(summary)

        claim, evidence = self._locate_candidate(plan_wire)
        bundle = self.review.candidate_authority_bundle(claim["id"])
        promotion = self.core.commit_policy_candidate(
            **bundle, idempotency_key=f"policy-ledger:sec-lane:{plan_wire['id']}",
        )
        closure = self.closure.close_policy_authorized(
            plan_version_ref=plan_wire["id"], authorization=promotion["authorization"],
        )
        replay = self.closure.close_policy_authorized(
            plan_version_ref=plan_wire["id"], authorization=promotion["authorization"],
        )
        if (
            replay["status"] != "duplicate"
            or replay["answer_binding_ref"] != closure["answer_binding_ref"]
        ):
            raise LaneError("ResearchPlan closure replay did not converge")
        formal_claim = self.core.get_claim(promotion["claim_version_ref"])
        if formal_claim is None:
            raise LaneError("formal ClaimVersion is missing after policy promotion")
        material = bundle.get("material") or {}
        normalized = material.get("normalized_payload", {}) if isinstance(material, Mapping) else {}
        verifications = [
            bundle.get("source_verification"), bundle.get("numeric_verification"),
        ]
        summary.update({
            "status": "duplicate" if promotion.get("status") == "duplicate" else "committed",
            "policy_version_ref": promotion["authorization"].get("policy_version_ref"),
            "candidate": {
                "claim_ref": claim["id"],
                "claim_hash": claim["content_hash"],
                "subject_ref": claim["subject_ref"],
                "period": claim["period"],
                "normalized_statement": claim["normalized_statement"],
                "value": claim["value"],
                "unit": claim["unit"],
                "source_envelope_ref": evidence["source_envelope_ref"],
            },
            "facts": {
                key: normalized.get(key)
                for key in (
                    "concept", "latest_accession", "current", "prior", "growth_percent",
                    "unit", "filed_from", "filed_to",
                )
            },
            "verifications": [
                None if v is None else {"ref": v["id"], "kind": v["kind"], "verdict": v["verdict"]}
                for v in verifications
            ],
            "promotion": {
                "authorization_ref": promotion["authorization"]["id"],
                "evidence_version_ref": promotion["evidence_version_ref"],
                "claim_version_ref": promotion["claim_version_ref"],
                "relation_ref": promotion.get("relation_ref"),
            },
            "closure": {
                "status": closure["status"],
                "answer_binding_ref": closure["answer_binding_ref"],
                "replay_status": replay["status"],
            },
        })
        return self._finish(summary)

    def _finish(self, summary: dict[str, Any]) -> dict[str, Any]:
        core = self.core.connection
        summary["human_gate_counts"] = {
            "plan_approvals": _count(core, "research_plan_approvals"),
            "claim_reviews": _count(self.review.connection, "human_review_decisions"),
        }
        summary["model_accounting_counts"] = {
            table: _count(core, table)
            for table in (
                "model_invocations", "observability_usage_entries", "observability_cost_entries",
            )
        }
        summary["formal_ledger_counts"] = {
            table: _count(core, table)
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        summary["integrity"] = {
            "core": _integrity(core),
            "staging": _integrity(self.review.connection),
            "coordinator": _integrity(self.connector_records.connection),
        }
        return summary

    def advance_past_quota_window(self) -> None:
        """Move the lane clock into the next SEC connector quota window.

        The lane clock is frozen at construction (rehearsals inject one), so
        every issuer would otherwise reserve inside the same 60-second window
        and the second one fails with ``ConnectorQuotaExceeded``.  In real
        time the lane first sleeps until wall-clock time has crossed the
        boundary so record timestamps never run ahead of real time.
        """
        now = self.clock()
        epoch = int(now.timestamp())
        boundary = datetime.fromtimestamp(
            epoch - (epoch % SEC_QUOTA_WINDOW_SECONDS) + SEC_QUOTA_WINDOW_SECONDS,
            timezone.utc,
        )
        step = int((boundary - now).total_seconds()) + 1
        if self._realtime:
            delay = (boundary - datetime.now(timezone.utc)).total_seconds() + 1
            if delay > 0:
                time.sleep(delay)
        self.clock.advance(step)

    def run_lane(
        self,
        *,
        filed_from: str,
        filed_to: str,
        actor_ref: str,
        run_key: str,
        issuers: Sequence[Issuer] | None = None,
        concept_candidates: Sequence[str] = DEFAULT_REVENUE_CONCEPT_CANDIDATES,
    ) -> dict[str, Any]:
        results = []
        for issuer in (issuers or self.issuers):
            if results:
                self.advance_past_quota_window()
            try:
                results.append(self.run_issuer(
                    issuer, filed_from=filed_from, filed_to=filed_to,
                    actor_ref=actor_ref, run_key=run_key,
                    concept_candidates=concept_candidates,
                ))
            except LanePreconditionError:
                raise
            except Exception as exc:  # one issuer must not sink the others
                results.append({
                    "issuer": issuer.__dict__,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return {
            "schema_version": "0.1",
            "lane": LANE_KIND,
            "created_at": _wire_time(datetime.now(timezone.utc)),
            "run_key": run_key,
            "filed_from": filed_from,
            "filed_to": filed_to,
            "governance_ref": self.governance.id,
            "governance_hash": self.governance.content_hash,
            "state_dir": str(self.state_dir),
            "staging_path": str(self.staging_path),
            "issuers": results,
            "ok": all(item["status"] in {"committed", "duplicate"} for item in results),
        }


__all__ = [
    "GovernanceLike",
    "Issuer",
    "LaneError",
    "LanePreconditionError",
    "RehearsalGovernance",
    "SecCompanyFactsLane",
    "US_IT_SERVICES_ISSUERS",
    "check_core_governance_rules",
]
