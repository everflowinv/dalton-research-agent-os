"""Owner-only local writer service for Dalton Core.

The service is intentionally boring: one process owns the ``DaltonStore``
connection and clients can only invoke an explicit operation allowlist over a
local Unix stream.  It is a file/authority boundary, not a hostile same-UID
sandbox.  A process which can read this process's token/config or open the DB
file can still defeat it; production deployment must use separate OS users or
a storage service with real identities for that threat model.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import stat
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .alphaengine_core_search import search_spec_hash
from .mission_source_discovery import (
    AlphaEngineSearchLauncher,
    DiscoveryLaunchConflict,
    DiscoveryLaunchError,
    DiscoveryLaunchRejected,
    DiscoveryPlanError,
    DiscoveryTicketNotFound,
    MissionSourceDiscoveryCoordinator,
    build_discovery_parameters,
    load_discovery_plan,
)
from .alphaengine_acquisition_launcher import (
    AcquisitionLaunchConflict,
    AcquisitionLaunchError,
    AcquisitionLaunchRejected,
    AcquisitionTicketNotFound,
    AlphaEngineAcquisitionLauncher,
)
from .sec_lane_launcher import (
    LaneLaunchConflict,
    LaneLaunchError,
    LaneLaunchRejected,
    LaneTicketNotFound,
    SecLaneLauncher,
)
from .research_review import (
    HumanReviewAuthority,
    ResearchReviewConflict,
    ResearchReviewError,
    ResearchReviewRejected,
)
from .research_verification import (
    CandidateStagingStore,
    ResearchVerificationConflict,
    ResearchVerificationError,
    VerificationRejected,
)
from .transcript_candidate_staging import stage_transcript_qualitative_candidate
from .agenda import (
    AgendaConflict,
    AgendaError,
    AgendaNotFound,
    AgendaStore,
    AgendaValidationError,
)
from .agenda_context import build_agenda_context
from .answer_routing import (
    AnswerRefreshControlPlane,
    AnswerRoutingAuthority,
    AnswerRoutingConflict,
    AnswerRoutingError,
    AnswerRoutingNotFound,
    AnswerRoutingValidationError,
)
from .perception import PerceptionError
from .context_materializer import (
    ContextMaterializerConflict,
    ContextMaterializerError,
    ContextMaterializerUnsupported,
)
from .coverage_admission import (
    CoverageAdmissionAuthority,
    CoverageAdmissionConflict,
    CoverageAdmissionError,
    CoverageAdmissionNotFound,
    CoverageAdmissionValidationError,
)
from .bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
    BoundedPlannerConflict,
    BoundedPlannerError,
    BoundedPlannerNotFound,
    BoundedPlannerPending,
    BoundedPlannerValidationError,
)
from .capability_registry import (
    CapabilityConflict,
    CapabilityNotFound,
    CapabilityRegistry,
    CapabilityRegistryError,
    EvaluationRejected,
    PermissionEscalation,
    PromotionRejected,
)
from .errors import (
    BadVerdict,
    DaltonStoreError,
    GateRejected,
    IdempotencyConflict,
    IndependenceViolation,
    InvocationConflict,
    NotFound,
    ValidationError,
    VerificationRequired,
)
from .store import DaltonStore
from .model_input import (
    ModelInputConflict,
    ModelInputLedger,
    ModelInputLedgerError,
    ModelInputNotFound,
    ModelInputValidationError,
)
from .industry_research import (
    IndustryResearchAuthority,
    IndustryResearchConflict,
    IndustryResearchError,
    IndustryResearchNotFound,
    IndustryResearchValidationError,
)
from .weekly_brief import (
    WeeklyBriefAuthority,
    WeeklyBriefConflict,
    WeeklyBriefError,
    WeeklyBriefNotFound,
    WeeklyBriefValidationError,
)
from .weekly_brief_coordinator import (
    WeeklyBriefCoordinatorError,
    run_weekly_brief_cycle,
)
from .research_constitution import (
    ResearchConstitutionAuthority,
    ResearchConstitutionConflict,
    ResearchConstitutionError,
    ResearchConstitutionNotFound,
    ResearchConstitutionValidationError,
)
from .research_playbook import (
    ResearchPlaybookAuthority,
    ResearchPlaybookConflict,
    ResearchPlaybookError,
    ResearchPlaybookNotFound,
    ResearchPlaybookValidationError,
)
from .coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionError,
    CoverageMissionNotFound,
    CoverageMissionValidationError,
)
from .model_forecast import (
    ModelForecastAuthority,
    ModelForecastConflict,
    ModelForecastError,
    ModelForecastNotFound,
    ModelForecastValidationError,
    extend_growth,
)
from .forecast_reconciliation import (
    ForecastReconciliationAuthority,
    ForecastReconciliationConflict,
    ForecastReconciliationError,
    ForecastReconciliationNotFound,
    ForecastReconciliationValidationError,
)
from .bounded_alphaengine_probe import (
    BoundedAlphaEngineProbeError,
    execute_alphaengine_probe,
)
from .company_research_view import (
    CompanyResearchViewError,
    CompanyResearchViewValidationError,
    build_company_research_view,
    query_company_research,
)
from .llm_research_planner_worker import LLMResearchPlannerModelWorker
from .llm_research_planner import (
    LLMResearchPlannerCoordinator,
    LLMResearchPlannerError,
    LLMResearchPlannerPending,
    LLMResearchPlannerRejected,
    LLMResearchPlannerValidationError,
)
from .research_doctrine import (
    ResearchDoctrineAuthority,
    ResearchDoctrineConflict,
    ResearchDoctrineError,
    ResearchDoctrineNotFound,
    ResearchDoctrineValidationError,
)
from .alphaengine_document_acquisition import (
    validate_alphaengine_document_acquisition_manifest,
)
from .raw_spool import RawSpool
from .connector import ConnectorStore
from .transcript_correction import (
    TranscriptCorrectionAuthority,
    TranscriptCorrectionConflict,
    TranscriptCorrectionError,
    TranscriptCorrectionNotFound,
    TranscriptCorrectionValidationError,
)
from .observability import (
    ObservabilityConflict,
    ObservabilityError,
    ObservabilityNotFound,
    ObservabilityStore,
    ObservabilityValidationError,
)
from .research_plan import ResearchPlanAuthority, ResearchPlanConflict, ResearchPlanNotFound
from .research_question_backlog import (
    ResearchQuestionBacklog,
    ResearchQuestionConflict,
    ResearchQuestionError,
    ResearchQuestionNotFound,
    ResearchQuestionValidationError,
)
from .intent_dispatch import (
    IntentDispatchConflict,
    IntentDispatchError,
    IntentDispatchNotFound,
    IntentDispatchValidationError,
    IntentWriterAuthority,
)
from .scheduler import Scheduler
from .thesis_impact import (
    ThesisImpactAuthority,
    ThesisImpactConflict,
    ThesisImpactNotFound,
    ThesisImpactValidationError,
)
from .thesis_impact_control import (
    ResearchPlanThesisImpactConflict,
    ResearchPlanThesisImpactCoordinator,
    ResearchPlanThesisImpactPending,
)
from .writer_protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_frame,
    encode_frame,
    error_frame,
    parse_request,
    success_frame,
)


class WriterServerError(RuntimeError):
    pass


_HUMAN_ACTOR_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_AUTOMATION_ACTOR_RE = re.compile(r"^automation:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")


def _validate_actor_ref(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise WriterServerError("actor_ref is invalid")
    if value.startswith("human:") and _HUMAN_ACTOR_RE.fullmatch(value) is None:
        raise WriterServerError("actor_ref is invalid")
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: str
    token: str
    operations: frozenset[str]
    # These are subject constraints, not claims supplied by the runtime.  The
    # server resolves the referenced invocation from its own store before
    # allowing a worker/verifier operation.
    allowed_invocation_refs: frozenset[str] = field(default_factory=frozenset)
    work_order_refs: frozenset[str] = field(default_factory=frozenset)
    unrestricted: bool = False
    actor_ref: str | None = None

    def __post_init__(self) -> None:
        _validate_actor_ref(self.actor_ref or self.principal_id)

    @property
    def is_unrestricted(self) -> bool:
        # ``core`` is the canonical bootstrap principal.  The explicit flag
        # is persisted in config; the name fallback keeps the small Python
        # bootstrap API backwards compatible while config remains strict.
        return self.unrestricted or self.principal_id == "core"

    @property
    def resolved_actor_ref(self) -> str:
        return _validate_actor_ref(self.actor_ref or self.principal_id)


MAX_CONNECTIONS = 8
CONNECTION_IDLE_TIMEOUT = 1.0
STORE_REQUEST_TIMEOUT = 30.0


WORKER_OPERATIONS = frozenset({"stage_change", "propose_model_input"})
VERIFIER_OPERATIONS = frozenset({"verify_change"})
RESEARCHER_OPERATIONS = frozenset({"register_evidence", "register_claim", "relate_evidence"})
ADJUDICATOR_OPERATIONS = frozenset({"adjudicate_claim"})
CAPABILITY_BUILDER_OPERATIONS = frozenset({"submit_capability_proposal"})
CAPABILITY_EVALUATOR_OPERATIONS = frozenset({"record_capability_evaluation"})
HUMAN_GOVERNANCE_OPERATIONS = frozenset({
    "create_policy", "decide_capability_promotion", "rollback_capability",
    "create_agenda_policy", "create_mandate", "create_priority_override",
    "set_agenda_pause", "record_agenda_feedback",
    "register_driver_pack", "propose_thesis_admission",
    "decide_thesis_admission",
    "publish_research_constitution", "get_research_constitution",
    "get_active_research_constitution", "research_constitution_report",
    "decide_model_input",
    "register_industry_evidence_pack", "register_company_overlay",
    "publish_weekly_brief", "record_weekly_brief_delivery",
    "record_weekly_brief_feedback",
    "publish_transcript_correction_set", "bind_transcript_claim_citation",
    "admit_intent_question", "issue_intent_directive",
    "publish_answer_sufficiency_policy",
    "dispatch_answer_refresh",
    "acquire_alphaengine_document", "alphaengine_acquisition_status",
    "stage_transcript_candidate", "transcript_candidate_status",
    "run_sec_company_facts_lane", "sec_lane_status",
    "company_research_view", "company_research_query",
    "record_backlog_question", "publish_probe_template",
    "create_bounded_planner_loop", "bounded_probe_template",
    "bounded_planner_loop", "publish_doctrine_pack", "get_doctrine_pack",
    "publish_forecast_line", "get_forecast_line", "extend_growth_forecast",
    "publish_research_playbook", "get_research_playbook",
    "get_active_research_playbook", "research_playbook_report",
    "create_coverage_mission", "get_coverage_mission",
    "get_active_coverage_mission", "record_mission_stage",
    "coverage_mission_progress", "coverage_mission_stage_records",
    "reconcile_forecasts", "forecast_reconciliations",
    "get_forecast_reconciliation", "decide_forecast_overturn",
    "run_mission_source_discovery", "mission_source_discovery_status",
    "mission_source_discoveries", "mission_discovered_documents",
    "mission_document_reviews", "resolve_mission_document_review",
})
# Mission stage bookkeeping is human-governed but must also be reachable by
# the mission's declared ``automation:`` principal; the CoverageMission
# authority decides which stages automation may pass (never a human
# checkpoint) and rejects principals other than the one the mission names.
MISSION_AUTOMATION_OPERATIONS = frozenset({
    "record_mission_stage", "coverage_mission_progress",
    "coverage_mission_stage_records", "get_active_coverage_mission",
    "get_coverage_mission",
    # P9d-1: automation may read what it discovered; launching stays human/core.
    "mission_source_discoveries", "mission_discovered_documents",
    # P9d-2: automation may read its extraction queue; resolving is human-only.
    "mission_document_reviews",
})
# P9c: the controller tick (core principal) reconciles pending forecast /
# actual pairs and reads them; the authority itself only writes under the
# mission grant, and the overturn decision stays human-only.
CORE_RECONCILIATION_OPERATIONS = frozenset({
    "reconcile_forecasts", "forecast_reconciliations", "get_forecast_reconciliation",
})
# P9d-1: the controller tick (core principal) advances mission source
# discovery and reads its ledgers; launching a discovery on request stays a
# human governance op.
CORE_DISCOVERY_OPERATIONS = frozenset({
    "dispatch_mission_source_discovery", "mission_source_discovery_status",
    "mission_source_discoveries", "mission_discovered_documents",
})
WEEKLY_BRIEF_READ_OPERATIONS = frozenset({
    "get_weekly_brief_issue", "render_weekly_brief_markdown",
    "weekly_brief_feedback", "weekly_brief_integrity_report",
})
WEEKLY_BRIEF_DASHBOARD_OPERATIONS = WEEKLY_BRIEF_READ_OPERATIONS | frozenset({
    "record_weekly_brief_feedback",
})
FEEDBACK_BRIDGE_OPERATIONS = frozenset({
    "list_agenda_feedback_targets", "record_agenda_feedback",
})
INTENT_CONTEXT_OPERATIONS = frozenset({"intent_context_bindings"})
ANSWER_ROUTING_OPERATIONS = frozenset({"answer_subjects", "route_answer"})
DASHBOARD_CONTROL_OPERATIONS = (
    FEEDBACK_BRIDGE_OPERATIONS
    | INTENT_CONTEXT_OPERATIONS
    | ANSWER_ROUTING_OPERATIONS
    | WEEKLY_BRIEF_DASHBOARD_OPERATIONS
)
RESEARCH_REVIEW_CONTROL_OPERATIONS = frozenset({
    "commit_reviewed_candidate", "transcript_correction_review_state",
    "candidate_promotions",
})
THESIS_IMPACT_OPERATIONS = frozenset({
    "thesis_impact_targets",
    "thesis_impact_start",
    "thesis_impact_advance_assessment",
    "thesis_impact_advance_verification",
    "thesis_impact_assessment",
    "thesis_impact_invocation",
    "thesis_impact_find_invocation",
    "get_version",
    "register_invocation",
    "record_usage",
    "create_price_rate_version",
    "record_cost",
})
SCOPED_FEEDBACK_PRINCIPALS = {
    "feedback-bridge": ("bridge:openclaw-discord",),
    "dashboard-control": ("bridge:tailscale-dashboard",),
    "agenda-timeout": ("automation:agenda-timeout",),
}
SCOPED_FEEDBACK_OPERATION_SETS = {
    "feedback-bridge": FEEDBACK_BRIDGE_OPERATIONS,
    "dashboard-control": DASHBOARD_CONTROL_OPERATIONS,
    "agenda-timeout": FEEDBACK_BRIDGE_OPERATIONS,
}
SCOPED_REVIEW_PRINCIPALS = {
    "research-review-control": "bridge:tailscale-review",
}
CORE_OPERATIONS = frozenset({
    "register_invocation", "stage_change", "verify_change", "commit",
    "commit_reviewed_candidate", "candidate_promotions",
    "current_pointer", "get_version", "list_events", "active_policy",
    "register_evidence", "register_claim", "relate_evidence", "adjudicate_claim",
    "submit_capability_proposal", "record_capability_evaluation",
    "active_capability", "get_capability_version", "get_capability_evaluation",
    "get_capability_decision", "capability_pointer_history",
    "agenda_control_state", "active_agenda_policy", "active_mandates",
    "agenda_budget_status", "register_perception_snapshot",
    "materialize_agenda_context", "get_agenda_mandate_version",
    "get_agenda_policy_version", "get_perception_snapshot",
    "active_priority_overrides", "start_agenda_cycle", "add_agenda_candidates",
    "decide_agenda_cycle", "fail_agenda_cycle", "agenda_cycle", "agenda_cycle_by_key",
    "pending_agenda_outbox", "claim_agenda_outbox", "record_agenda_delivery",
    "create_workflow_version", "link_work_order", "record_usage",
    "create_price_rate_version", "record_cost",
    "register_driver_pack", "get_driver_pack",
    "propose_thesis_admission", "get_thesis_admission_candidate",
    "decide_thesis_admission", "get_thesis_admission_decision",
    "publish_research_constitution", "get_research_constitution",
    "get_active_research_constitution", "research_constitution_report",
    "company_research_view", "company_research_query",
    "record_backlog_question", "publish_probe_template",
    "create_bounded_planner_loop", "bounded_probe_template",
    "bounded_planner_loop", "publish_doctrine_pack", "get_doctrine_pack",
    "publish_forecast_line", "get_forecast_line", "extend_growth_forecast",
    "publish_research_playbook", "get_research_playbook",
    "get_active_research_playbook", "research_playbook_report",
    "create_coverage_mission", "get_coverage_mission",
    "get_active_coverage_mission", "record_mission_stage",
    "coverage_mission_progress", "coverage_mission_stage_records",
    "propose_model_input", "get_model_input_candidate",
    "get_model_input_decision", "get_model_input_version", "current_model_input",
    "decide_model_input", "record_model_run", "record_model_reconciliation",
    "get_model_reconciliations", "model_input_integrity_report",
    "register_industry_evidence_pack", "get_industry_evidence_pack",
    "register_company_overlay", "get_company_overlay",
    "industry_brief_snapshot", "render_industry_brief_markdown",
    "industry_research_integrity_report",
    "publish_weekly_brief", "get_weekly_brief_issue",
    "render_weekly_brief_markdown", "record_weekly_brief_delivery",
    "run_weekly_brief_cycle", "record_scheduled_weekly_brief_delivery",
    "bounded_planner_propose_next", "bounded_planner_admit_proposal",
    "bounded_planner_record_outcome", "bounded_planner_record_observation",
    "dispatch_coverage_mission_sec_lane",
    "reconcile_forecasts", "forecast_reconciliations", "get_forecast_reconciliation",
    "dispatch_mission_source_discovery", "mission_source_discovery_status",
    "mission_source_discoveries", "mission_discovered_documents",
    "mission_document_reviews",
    "bounded_planner_active_loops", "materialize_bounded_planner_context",
    "bounded_planner_propose_next_with_context", "llm_planner_prepare",
    "llm_planner_advance", "llm_planner_execute", "bounded_alphaengine_probe",
    "record_weekly_brief_feedback", "weekly_brief_feedback",
    "weekly_brief_integrity_report",
    "intent_context_bindings", "admit_intent_question", "issue_intent_directive",
    "publish_answer_sufficiency_policy", "answer_subjects", "route_answer",
    "dispatch_answer_refresh",
})


# Explicit operation parameter contracts.  The server must reject unknown
# fields before they reach a method accepting **kwargs.
OPERATION_FIELDS: dict[str, frozenset[str]] = {
    "register_invocation": frozenset({"invocation"}),
    "stage_change": frozenset({"change", "change_id", "thesis_id", "content", "payload", "producer_invocation", "producer_invocation_id", "actor_id"}),
    "verify_change": frozenset({"change_id", "verification", "verification_id", "verifier_invocation", "verifier_invocation_id", "verdict", "findings", "actor_id"}),
    "commit": frozenset({"change_id", "verification_id", "idempotency_key", "request", "actor_id", "request_hash"}),
    "commit_reviewed_candidate": frozenset({"decision", "evidence", "claim", "idempotency_key"}),
    "publish_transcript_correction_set": frozenset({
        "correction_set_ref", "source_manifest", "review_scope", "corrections",
        "prior_version_ref", "actor_ref",
    }),
    "bind_transcript_claim_citation": frozenset({
        "correction_set_version_ref", "correction_set_version_hash",
        "source_manifest", "source_start", "source_end",
    }),
    "transcript_correction_review_state": frozenset({
        "source_manifest", "correction_set_ref", "source_start", "source_end",
    }),
    "candidate_promotions": frozenset({"candidate_claim_refs"}),
    "acquire_alphaengine_document": frozenset({
        "document_ref", "expected_content_sha256", "max_pages", "actor_ref",
    }),
    "alphaengine_acquisition_status": frozenset({"ticket_ref"}),
    "stage_transcript_candidate": frozenset({
        "correction_set_ref", "citation_ref", "subject_ref", "metric_or_aspect",
        "period", "basis", "normalized_statement", "idempotency_key", "actor_ref",
    }),
    "transcript_candidate_status": frozenset({"candidate_claim_ref"}),
    "run_sec_company_facts_lane": frozenset({
        "issuers", "filed_from", "filed_to", "actor_ref", "form",
    }),
    "sec_lane_status": frozenset({"ticket_ref"}),
    "create_policy": frozenset({"policy", "policy_version_id", "version_number", "activate", "policy_ref", "effective_from", "effective_until", "actor_ref", "prior_version_ref", "change_reason", "content_hash_value"}),
    "current_pointer": frozenset({"thesis_id"}),
    "get_version": frozenset({"version_id"}),
    "list_events": frozenset({"aggregate_id"}),
    "active_policy": frozenset(),
    "register_evidence": frozenset({"evidence", "evidence_ref", "evidence_id", "evidence_version_id", "actor_ref"}),
    "register_claim": frozenset({"claim", "claim_ref", "claim_id", "claim_version_id", "producer_invocation_refs", "actor_ref"}),
    "relate_evidence": frozenset({"relation", "relation_id", "idempotency_key", "actor_ref"}),
    "adjudicate_claim": frozenset({"adjudication", "adjudication_version_id", "adjudicator_invocation_ref", "subject_invocation_refs", "actor_ref"}),
    "submit_capability_proposal": frozenset({"proposal", "capability_ref", "version_number", "artifact_hash", "builder_invocation_ref", "idempotency_key", "actor_ref"}),
    "record_capability_evaluation": frozenset({"proposal_ref", "evaluation_id", "fixtures", "baseline", "results", "environment_hash", "evaluator_invocation_ref", "proposal_hash", "idempotency_key", "actor_ref"}),
    "decide_capability_promotion": frozenset({"proposal_ref", "decision", "evaluation_id", "decision_id", "requested_permissions", "rationale", "rollback_to_revision_ref", "idempotency_key", "actor_ref"}),
    "rollback_capability": frozenset({"capability_ref", "target_revision_ref", "reason", "decision_id", "idempotency_key", "actor_ref"}),
    "active_capability": frozenset({"capability_ref"}),
    "get_capability_version": frozenset({"revision_ref"}),
    "get_capability_evaluation": frozenset({"evaluation_id"}),
    "get_capability_decision": frozenset({"decision_id"}),
    "capability_pointer_history": frozenset({"capability_ref"}),
    "create_agenda_policy": frozenset({"policy", "effective_from", "effective_until", "activate", "version_id", "idempotency_key", "actor_ref"}),
    "active_agenda_policy": frozenset({"at"}),
    "agenda_budget_status": frozenset({"daily_since", "monthly_since"}),
    "create_mandate": frozenset({"mandate_ref", "objective", "scope_refs", "constraints", "success_criteria", "effective_from", "effective_until", "activate", "version_id", "idempotency_key", "actor_ref"}),
    "active_mandates": frozenset({"at"}),
    "create_priority_override": frozenset({"override_ref", "scope_refs", "weight_deltas", "rationale", "effective_from", "effective_until", "revoked", "version_id", "idempotency_key", "actor_ref"}),
    "active_priority_overrides": frozenset({"scope_ref", "at"}),
    "set_agenda_pause": frozenset({"paused", "reason", "version_id", "idempotency_key", "actor_ref"}),
    "agenda_control_state": frozenset(),
    "register_perception_snapshot": frozenset({"snapshot", "idempotency_key", "actor_ref"}),
    "materialize_agenda_context": frozenset({"cycle_id", "max_tokens", "max_bytes"}),
    "get_agenda_mandate_version": frozenset({"version_id"}),
    "get_agenda_policy_version": frozenset({"version_id"}),
    "get_perception_snapshot": frozenset({"snapshot_id"}),
    "start_agenda_cycle": frozenset({"cycle_key", "perception_snapshot_ref", "perception_snapshot_hash", "mandate_version_ref", "policy_version_ref", "company_ref", "cycle_id", "idempotency_key", "actor_ref"}),
    "add_agenda_candidates": frozenset({"cycle_id", "candidates", "idempotency_key", "actor_ref"}),
    "decide_agenda_cycle": frozenset({"cycle_id", "decision_id", "idempotency_key", "actor_ref"}),
    "fail_agenda_cycle": frozenset({"cycle_id", "reason", "metadata", "actor_ref"}),
    "agenda_cycle": frozenset({"cycle_id"}),
    "agenda_cycle_by_key": frozenset({"cycle_key"}),
    "pending_agenda_outbox": frozenset({"limit"}),
    "claim_agenda_outbox": frozenset({"endpoint_ref", "now", "claim_ttl_seconds", "max_attempts", "limit", "idempotency_key", "actor_ref"}),
    "record_agenda_delivery": frozenset({"message_id", "state", "delivery_attempt_id", "delivery_receipt_id", "error_code", "retry_after", "idempotency_key", "actor_ref"}),
    "list_agenda_feedback_targets": frozenset({"endpoint_ref", "limit"}),
    "record_agenda_feedback": frozenset({"decision_id", "verdict", "notes", "feedback_id", "idempotency_key", "subject_ref", "prior_feedback_ref", "source", "source_event_ref", "actor_ref"}),
    "intent_context_bindings": frozenset(),
    "admit_intent_question": frozenset({
        "subject_binding", "question", "answer_criteria",
        "candidate_version_ref", "candidate_version_hash",
        "confirmation_ref", "confirmation_hash", "idempotency_key", "actor_ref",
    }),
    "issue_intent_directive": frozenset({
        "loop_binding", "target_coverage_item_binding", "verbatim_text",
        "control_effect", "candidate_version_ref", "candidate_version_hash",
        "confirmation_ref", "confirmation_hash", "actor_ref",
    }),
    "publish_answer_sufficiency_policy": frozenset({
        "policy_ref", "mandate_version_ref", "mandate_version_hash",
        "thresholds", "refresh_route", "adhoc_research_route",
        "effective_from", "effective_until", "actor_ref", "version_id",
        "prior_version_ref", "idempotency_key",
    }),
    "answer_subjects": frozenset({"as_of"}),
    "route_answer": frozenset({"subject_binding", "question", "as_of"}),
    "dispatch_answer_refresh": frozenset({
        "subject_binding", "question", "route_decision_ref",
        "route_decision_hash", "route_as_of", "actor_ref",
    }),
    "create_workflow_version": frozenset({"workflow_ref", "title", "objective", "scope_refs", "root_work_order_refs", "governance_policy_ref", "prior_version_ref", "version_id", "idempotency_key", "actor_ref"}),
    "link_work_order": frozenset({"workflow_ref", "parent_work_order_ref", "child_work_order_ref", "relation", "sequence", "actor_ref", "link_id", "idempotency_key"}),
    "record_usage": frozenset({"invocation_ref", "entry_id", "occurred_at", "metering_source", "measurement_status", "raw_usage", "workflow_ref", "provider_usage_ref", "correction_of_ref", "actor_ref", "idempotency_key", "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens", "requests", "duration_ms", "input_bytes", "output_bytes"}),
    "create_price_rate_version": frozenset({"price_rate_ref", "provider", "model", "charge_type", "unit_quantity", "unit_price_micros", "currency", "effective_from", "effective_until", "source_ref", "prior_version_ref", "version_id", "idempotency_key", "actor_ref"}),
    "record_cost": frozenset({"usage_entry_ref", "price_rate_refs", "amount_micros", "currency", "cost_status", "calculation_ref", "correction_of_ref", "cost_entry_id", "idempotency_key", "actor_ref"}),
    "register_driver_pack": frozenset({"driver_pack_ref", "industry_ref", "title", "drivers", "metric_specs", "thesis_templates", "actor_ref", "version_id", "prior_version_ref", "idempotency_key"}),
    "get_driver_pack": frozenset({"version_id"}),
    "propose_thesis_admission": frozenset({"candidate_id", "thesis_ref", "company_ref", "industry_ref", "template_ref", "driver_refs", "mandate_version_ref", "mandate_version_hash", "driver_pack_version_ref", "driver_pack_version_hash", "content", "actor_ref", "idempotency_key"}),
    "get_thesis_admission_candidate": frozenset({"candidate_id"}),
    "decide_thesis_admission": frozenset({"candidate_id", "candidate_hash", "verdict", "rationale", "decision_id", "actor_ref", "idempotency_key"}),
    "get_thesis_admission_decision": frozenset({"decision_id"}),
    "publish_research_constitution": frozenset({"constitution_ref", "industry_ref", "title", "bindings", "method", "actor_ref", "version_id", "prior_version_ref", "idempotency_key"}),
    "get_research_constitution": frozenset({"version_id"}),
    "get_active_research_constitution": frozenset({"constitution_ref"}),
    "research_constitution_report": frozenset(),
    "company_research_view": frozenset({"company_ref"}),
    "company_research_query": frozenset({"company_ref", "aspect", "period", "status", "limit"}),
    "record_backlog_question": frozenset({"mandate_version_ref", "company_ref", "question", "answer_criteria", "source_refs", "actor_ref", "idempotency_key"}),
    "publish_probe_template": frozenset({"template_ref", "capability_ref", "operation", "runtime_profile_ref", "parameter_contract", "output_contract_ref", "verifier_ref", "permission_scope", "declared_side_effects", "cost", "actor_ref", "prior_version_ref"}),
    "create_bounded_planner_loop": frozenset({"loop_ref", "question_version_ref", "template_bindings", "required_coverage_items", "budget", "actor_ref", "prior_version_ref"}),
    "bounded_probe_template": frozenset({"version_ref"}),
    "bounded_planner_loop": frozenset({"version_ref"}),
    "publish_doctrine_pack": frozenset({"doctrine_pack_ref", "title", "default_lens_ref", "lenses", "actor_ref", "prior_version_ref"}),
    "get_doctrine_pack": frozenset({"version_ref"}),
    "publish_research_playbook": frozenset({"playbook_ref", "title", "provenance", "stages", "key_questions", "deliverable_templates", "decision_vocabulary", "analyst_levels", "tracker_classes", "risk_reward_standards", "model_discipline", "evidence_discipline", "actor_ref", "version_id", "prior_version_ref", "idempotency_key"}),
    "get_research_playbook": frozenset({"version_id"}),
    "get_active_research_playbook": frozenset({"playbook_ref"}),
    "research_playbook_report": frozenset(),
    "create_coverage_mission": frozenset({"mission_ref", "title", "objective", "industry_ref", "universe", "research_questions", "deliverables", "source_plan", "bindings", "autonomy", "budget", "actor_ref", "version_id", "prior_version_ref", "idempotency_key"}),
    "get_coverage_mission": frozenset({"version_id"}),
    "get_active_coverage_mission": frozenset({"mission_ref"}),
    "record_mission_stage": frozenset({"mission_version_ref", "mission_version_hash", "company_ref", "stage_ref", "status", "evidence_refs", "rationale", "actor_ref", "idempotency_key"}),
    "coverage_mission_progress": frozenset({"mission_ref"}),
    "coverage_mission_stage_records": frozenset({"mission_version_ref", "company_ref"}),
    "bounded_planner_propose_next": frozenset({"loop_version_ref"}),
    "bounded_planner_admit_proposal": frozenset({"proposal_ref"}),
    "bounded_planner_record_outcome": frozenset({"round_ref"}),
    "bounded_planner_record_observation": frozenset({"round_ref", "mandate_version_ref"}),
    "dispatch_coverage_mission_sec_lane": frozenset(),
    "reconcile_forecasts": frozenset({"requested_by", "company_ref", "claim_version_ref"}),
    "dispatch_mission_source_discovery": frozenset(),
    "run_mission_source_discovery": frozenset({"requested_by", "company_ref", "spec_ref", "as_of"}),
    "mission_source_discovery_status": frozenset({"ticket_ref"}),
    "mission_source_discoveries": frozenset({"mission_version_ref", "company_ref", "spec_ref", "limit"}),
    "mission_discovered_documents": frozenset({"mission_version_ref", "company_ref", "status", "limit"}),
    "mission_document_reviews": frozenset({"mission_version_ref", "company_ref", "state", "limit"}),
    "resolve_mission_document_review": frozenset({"review_id", "resolution", "candidate_claim_version_ref", "rationale", "actor_ref"}),

    "forecast_reconciliations": frozenset({
        "company_ref", "claim_version_ref", "forecast_line_ref", "created_from", "created_to",
    }),
    "get_forecast_reconciliation": frozenset({"reconciliation_ref"}),
    "decide_forecast_overturn": frozenset({
        "reconciliation_ref", "reconciliation_hash", "decision", "rationale", "actor_ref",
        "idempotency_key",
    }),
    "bounded_planner_active_loops": frozenset(),
    "materialize_bounded_planner_context": frozenset({"loop_version_ref", "doctrine_pack_version_ref", "doctrine_pack_version_hash", "as_of"}),
    "bounded_planner_propose_next_with_context": frozenset({"planner_context_pack_ref"}),
    "llm_planner_prepare": frozenset({"context_pack_ref", "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_seconds"}),
    "llm_planner_advance": frozenset({"context_pack_ref", "work_order"}),
    "bounded_alphaengine_probe": frozenset({"work_order"}),
    "publish_forecast_line": frozenset({"line_ref", "subject_ref", "metric_or_aspect", "period", "unit", "currency", "value", "value_kind", "scenario_version_ref", "scenario_version_hash", "actor_ref", "rationale", "version_id", "prior_version_ref", "idempotency_key"}),
    "get_forecast_line": frozenset({"version_ref"}),
    "extend_growth_forecast": frozenset({"base_input_version_ref", "growth_input_version_ref", "periods", "line_ref_prefix", "model_run_ref", "idempotency_key"}),
    "llm_planner_execute": frozenset({"context_pack_ref", "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_seconds"}),
    "propose_model_input": frozenset({
        "candidate_id", "input_kind", "model_input_ref", "prior_version_ref",
        "payload", "proposed_by", "idempotency_key",
    }),
    "get_model_input_candidate": frozenset({"candidate_id"}),
    "get_model_input_decision": frozenset({"decision_id"}),
    "get_model_input_version": frozenset({"version_id"}),
    "current_model_input": frozenset({"model_input_ref"}),
    "decide_model_input": frozenset({
        "decision_id", "candidate_id", "candidate_hash", "verdict", "rationale",
        "findings", "reviewer_ref", "version_id", "idempotency_key",
    }),
    "record_model_run": frozenset({
        "version_id", "model_run_ref", "prior_version_ref", "scenario_version_ref",
        "scenario_version_hash", "input_bindings", "formula_version_ref",
        "formula_version_hash", "status", "outputs", "errors", "started_at",
        "completed_at", "actor_ref", "idempotency_key",
    }),
    "record_model_reconciliation": frozenset({
        "reconciliation_id", "model_run_version_ref", "model_run_version_hash",
        "checks", "actor_ref", "idempotency_key",
    }),
    "get_model_reconciliations": frozenset({"model_run_version_ref"}),
    "model_input_integrity_report": frozenset(),
    "register_industry_evidence_pack": frozenset({
        "evidence_pack_ref", "industry_ref", "title", "as_of", "boundary",
        "coverage_universe", "driver_pack_version_ref", "driver_pack_version_hash",
        "evidence_bindings", "debates", "source_plan", "report_contract",
        "actor_ref", "version_id", "prior_version_ref", "idempotency_key",
    }),
    "get_industry_evidence_pack": frozenset({"version_id"}),
    "register_company_overlay": frozenset({
        "overlay_ref", "company_ref", "industry_ref", "title", "as_of", "role",
        "evidence_pack_version_ref", "evidence_pack_version_hash", "driver_views",
        "key_differences", "open_questions", "falsifier_refs", "thesis_candidate_refs",
        "actor_ref", "version_id", "prior_version_ref", "idempotency_key",
    }),
    "get_company_overlay": frozenset({"version_id"}),
    "industry_brief_snapshot": frozenset({
        "evidence_pack_version_id", "company_overlay_version_ids",
    }),
    "render_industry_brief_markdown": frozenset({
        "evidence_pack_version_id", "company_overlay_version_ids",
    }),
    "industry_research_integrity_report": frozenset(),
    "publish_weekly_brief": frozenset({
        "brief_ref", "period_start", "period_end", "evidence_pack_version_id",
        "company_overlay_version_ids", "company_thesis_refs", "actor_ref",
        "version_id", "prior_version_ref", "idempotency_key",
    }),
    "get_weekly_brief_issue": frozenset({"version_id"}),
    "render_weekly_brief_markdown": frozenset({"version_id"}),
    "record_weekly_brief_delivery": frozenset({
        "issue_version_ref", "issue_version_hash", "destination_ref",
        "external_message_ref", "artifact_sha256", "delivered_at",
        "delivery_id", "actor_ref", "idempotency_key",
    }),
    "run_weekly_brief_cycle": frozenset({"plan", "as_of", "actor_ref"}),
    "record_scheduled_weekly_brief_delivery": frozenset({
        "cycle_id", "issue_version_ref", "issue_version_hash",
        "destination_ref", "external_message_ref", "artifact_sha256",
        "delivered_at", "delivery_id", "actor_ref", "idempotency_key",
    }),
    "record_weekly_brief_feedback": frozenset({
        "issue_version_ref", "issue_version_hash", "verdict", "target_kind",
        "target_ref", "notes", "feedback_id", "prior_feedback_ref",
        "subject_ref", "actor_ref", "idempotency_key",
    }),
    "weekly_brief_feedback": frozenset({"issue_version_ref"}),
    "weekly_brief_integrity_report": frozenset(),
    "thesis_impact_targets": frozenset({"company_thesis_refs", "limit"}),
    "thesis_impact_start": frozenset({"plan_version_ref", "thesis_ref"}),
    "thesis_impact_advance_assessment": frozenset({"plan_version_ref", "thesis_ref"}),
    "thesis_impact_advance_verification": frozenset({"plan_version_ref", "thesis_ref", "assessment_ref"}),
    "thesis_impact_assessment": frozenset({"assessment_ref"}),
    "thesis_impact_invocation": frozenset({"invocation_ref"}),
    "thesis_impact_find_invocation": frozenset({"invocation_ref"}),
}


OPERATION_ACTOR_FIELDS: dict[str, str] = {
    "stage_change": "actor_id",
    "verify_change": "actor_id",
    "commit": "actor_id",
    "create_policy": "actor_ref",
    "register_evidence": "actor_ref",
    "register_claim": "actor_ref",
    "relate_evidence": "actor_ref",
    "adjudicate_claim": "actor_ref",
    "submit_capability_proposal": "actor_ref",
    "record_capability_evaluation": "actor_ref",
    "decide_capability_promotion": "actor_ref",
    "rollback_capability": "actor_ref",
    "create_agenda_policy": "actor_ref",
    "create_mandate": "actor_ref",
    "create_priority_override": "actor_ref",
    "set_agenda_pause": "actor_ref",
    "register_perception_snapshot": "actor_ref",
    "start_agenda_cycle": "actor_ref",
    "add_agenda_candidates": "actor_ref",
    "decide_agenda_cycle": "actor_ref",
    "fail_agenda_cycle": "actor_ref",
    "claim_agenda_outbox": "actor_ref",
    "record_agenda_delivery": "actor_ref",
    "record_agenda_feedback": "actor_ref",
    "admit_intent_question": "actor_ref",
    "issue_intent_directive": "actor_ref",
    "publish_answer_sufficiency_policy": "actor_ref",
    "dispatch_answer_refresh": "actor_ref",
    "create_workflow_version": "actor_ref",
    "link_work_order": "actor_ref",
    "record_usage": "actor_ref",
    "create_price_rate_version": "actor_ref",
    "record_cost": "actor_ref",
    "register_driver_pack": "actor_ref",
    "propose_thesis_admission": "actor_ref",
    "decide_thesis_admission": "actor_ref",
    "publish_research_constitution": "actor_ref",
    "record_backlog_question": "actor_ref",
    "publish_doctrine_pack": "actor_ref",
    "publish_research_playbook": "actor_ref",
    "create_coverage_mission": "actor_ref",
    "resolve_mission_document_review": "actor_ref",
    "record_mission_stage": "actor_ref",
    "publish_forecast_line": "actor_ref",
    "publish_probe_template": "actor_ref",
    "create_bounded_planner_loop": "actor_ref",
    "propose_model_input": "proposed_by",
    "decide_model_input": "reviewer_ref",
    "record_model_run": "actor_ref",
    "record_model_reconciliation": "actor_ref",
    "register_industry_evidence_pack": "actor_ref",
    "register_company_overlay": "actor_ref",
    "publish_weekly_brief": "actor_ref",
    "record_weekly_brief_delivery": "actor_ref",
    "run_weekly_brief_cycle": "actor_ref",
    "record_scheduled_weekly_brief_delivery": "actor_ref",
    "record_weekly_brief_feedback": "actor_ref",
    "publish_transcript_correction_set": "actor_ref",
    "acquire_alphaengine_document": "actor_ref",
    "stage_transcript_candidate": "actor_ref",
    "run_sec_company_facts_lane": "actor_ref",
    "reconcile_forecasts": "requested_by",
    "decide_forecast_overturn": "actor_ref",
    "run_mission_source_discovery": "requested_by",
}


def _require_owner_only(path: Path, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise WriterServerError(f"{label} is unavailable") from exc
    if mode & 0o077:
        raise WriterServerError(f"{label} must be owner-only")


def load_principals(
    path: str | Path,
    *,
    allow_managed_operation_subset: bool = False,
) -> dict[str, Principal]:
    """Load an owner-only token config without retaining unrelated fields.

    Bootstrap may opt into accepting an older, non-empty operation subset for
    managed principals so it can atomically migrate the file to the current
    exact set.  Runtime callers remain exact and fail closed.
    """
    config_path = Path(path)
    _require_owner_only(config_path, "token config")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WriterServerError("token config is invalid") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "principals"} or raw.get("schema_version") != PROTOCOL_VERSION:
        raise WriterServerError("token config is invalid")
    entries = raw.get("principals")
    if not isinstance(entries, list) or not entries:
        raise WriterServerError("token config is invalid")
    result: dict[str, Principal] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"principal_id", "token", "operations", "allowed_invocation_refs", "work_order_refs", "unrestricted", "actor_ref"}:
            raise WriterServerError("token config is invalid")
        principal_id = entry.get("principal_id")
        token = entry.get("token")
        operations = entry.get("operations")
        allowed_invocation_refs = entry.get("allowed_invocation_refs")
        work_order_refs = entry.get("work_order_refs")
        unrestricted = entry.get("unrestricted")
        actor_ref = entry.get("actor_ref")
        if not isinstance(principal_id, str) or not principal_id or not isinstance(token, str) or not token:
            raise WriterServerError("token config is invalid")
        if not isinstance(operations, list) or not operations or any(not isinstance(x, str) or x not in OPERATION_FIELDS for x in operations):
            raise WriterServerError("token config is invalid")
        if not isinstance(allowed_invocation_refs, list) or any(not isinstance(x, str) or not x for x in allowed_invocation_refs):
            raise WriterServerError("token config is invalid")
        if not isinstance(work_order_refs, list) or any(not isinstance(x, str) or not x for x in work_order_refs):
            raise WriterServerError("token config is invalid")
        if not isinstance(unrestricted, bool) or (unrestricted and principal_id != "core"):
            raise WriterServerError("token config is invalid")
        try:
            _validate_actor_ref(actor_ref or principal_id)
        except WriterServerError:
            raise WriterServerError("token config is invalid") from None
        if principal_id == "worker" and not set(operations) <= WORKER_OPERATIONS:
            raise WriterServerError("token config is invalid")
        if principal_id == "verifier" and not set(operations) <= VERIFIER_OPERATIONS:
            raise WriterServerError("token config is invalid")
        scoped_actor = SCOPED_FEEDBACK_PRINCIPALS.get(principal_id)
        scoped_operations = SCOPED_FEEDBACK_OPERATION_SETS.get(principal_id)
        scoped_operations_match = (
            set(operations) <= scoped_operations
            if allow_managed_operation_subset and scoped_operations is not None
            else set(operations) == scoped_operations
        )
        if scoped_actor is not None and (
            not scoped_operations_match
            or actor_ref not in scoped_actor
        ):
            raise WriterServerError("token config is invalid")
        review_actor = SCOPED_REVIEW_PRINCIPALS.get(principal_id)
        review_operations_match = (
            set(operations) <= RESEARCH_REVIEW_CONTROL_OPERATIONS
            if allow_managed_operation_subset
            else set(operations) == RESEARCH_REVIEW_CONTROL_OPERATIONS
        )
        if review_actor is not None and (
            not review_operations_match
            or actor_ref != review_actor
        ):
            raise WriterServerError("token config is invalid")
        thesis_impact_operations_match = (
            set(operations) <= THESIS_IMPACT_OPERATIONS
            if allow_managed_operation_subset
            else set(operations) == THESIS_IMPACT_OPERATIONS
        )
        if principal_id == "thesis-impact" and (
            not thesis_impact_operations_match
            or actor_ref != "system:thesis-impact-model-worker"
        ):
            raise WriterServerError("token config is invalid")
        if principal_id in result:
            raise WriterServerError("token config is invalid")
        result[principal_id] = Principal(
            principal_id, token, frozenset(operations), frozenset(allowed_invocation_refs),
            frozenset(work_order_refs), unrestricted, actor_ref,
        )
    return result


def replace_token_config(path: str | Path, principals: list[Principal], *, require_absent: bool = False) -> None:
    """Atomically replace an owner-only principal file without exposing tokens."""
    config_path = Path(path)
    if require_absent and config_path.exists():
        raise WriterServerError("token config already exists")
    for principal in principals:
        _validate_actor_ref(principal.resolved_actor_ref)
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = json.dumps({"schema_version": PROTOCOL_VERSION, "principals": [
        {
            "principal_id": p.principal_id,
            "token": p.token,
            "operations": sorted(p.operations),
            "allowed_invocation_refs": sorted(p.allowed_invocation_refs),
            "work_order_refs": sorted(p.work_order_refs),
            "unrestricted": p.is_unrestricted,
            "actor_ref": p.actor_ref,
        } for p in principals
    ]}, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
        os.chmod(config_path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_token_config(path: str | Path, principals: list[Principal]) -> None:
    """Create a token config with owner-only permissions (test/bootstrap use)."""
    replace_token_config(path, principals, require_absent=True)


class WriterServer:
    """Serve a capability-scoped, local-only Dalton write API."""

    def __init__(
        self,
        db_path: str | Path,
        socket_path: str | Path,
        principals: Mapping[str, Principal],
        *,
        token_config_path: str | Path | None = None,
        scheduler_path: str | Path | None = None,
        transcript_spool_dir: str | Path | None = None,
        acquisition_launcher: AlphaEngineAcquisitionLauncher | None = None,
        candidate_staging_path: str | Path | None = None,
        sec_lane_launcher: SecLaneLauncher | None = None,
        planner_model_config: Mapping[str, Any] | None = None,
        search_launcher: AlphaEngineSearchLauncher | None = None,
        discovery_plan_path: str | Path | None = None,
    ):
        if not principals:
            raise WriterServerError("at least one principal is required")
        self._acquisition_launcher = acquisition_launcher
        # P9d-1: out-of-process AlphaEngine search discovery; the plan is the
        # human-authored, hash-bound list of queries the mission may run.
        self._search_launcher = search_launcher
        self._discovery_plan_path = (
            None if discovery_plan_path is None
            else str(Path(discovery_plan_path).expanduser().resolve())
        )
        self._source_discovery: MissionSourceDiscoveryCoordinator | None = None
        self._discovery_plan_error: str | None = None
        # S7d: out-of-process SEC company-facts lane runs (human-only ops).
        self._sec_lane_launcher = sec_lane_launcher
        # The same owner-only CandidateStaging file the Cockpit review plane
        # opens as ``research_review.candidate_staging_path``; the writer
        # stages transcript candidates into it and reads status back from it.
        self._candidate_staging_path = (
            None if candidate_staging_path is None
            else str(Path(candidate_staging_path).expanduser().resolve())
        )
        self._candidate_staging: CandidateStagingStore | None = None
        self._candidate_review: HumanReviewAuthority | None = None
        self.db_path = str(db_path)  # retained only by the owner process
        self.socket_path = str(socket_path)
        if len(os.fsencode(self.socket_path)) >= 104:
            raise WriterServerError("socket path is too long")
        self.principals = dict(principals)
        self._by_token = {p.token: p for p in self.principals.values()}
        if len(self._by_token) != len(self.principals):
            raise WriterServerError("principal tokens must be unique")
        self._store: DaltonStore | None = None
        self._registry: CapabilityRegistry | None = None
        self._agenda: AgendaStore | None = None
        self._observability: ObservabilityStore | None = None
        self._connectors: ConnectorStore | None = None
        self._coverage_admission: CoverageAdmissionAuthority | None = None
        self._model_input: ModelInputLedger | None = None
        self._industry_research: IndustryResearchAuthority | None = None
        self._weekly_brief: WeeklyBriefAuthority | None = None
        self._research_doctrine: ResearchDoctrineAuthority | None = None
        self._model_forecast: ModelForecastAuthority | None = None
        self._forecast_reconciliation: ForecastReconciliationAuthority | None = None
        self._research_constitution: ResearchConstitutionAuthority | None = None
        self._research_playbook: ResearchPlaybookAuthority | None = None
        self._coverage_mission: CoverageMissionAuthority | None = None
        self._transcript_spool_dir = (
            None if transcript_spool_dir is None
            else str(Path(transcript_spool_dir).expanduser().resolve())
        )
        self._transcript_spool: RawSpool | None = None
        self._scheduler_path = None if scheduler_path is None else str(scheduler_path)
        self._scheduler: Scheduler | None = None
        self._research_plan: ResearchPlanAuthority | None = None
        self._backlog: ResearchQuestionBacklog | None = None
        self._bounded_planner: BoundedPlannerAuthority | None = None
        self._llm_planner_coordinator_instance: LLMResearchPlannerCoordinator | None = None
        self._planner_model_config: dict[str, Any] | None = planner_model_config
        self._bounded_control: BoundedPlannerControlPlane | None = None
        self._intent_writer: IntentWriterAuthority | None = None
        self._answer_routing: AnswerRoutingAuthority | None = None
        self._answer_refresh: AnswerRefreshControlPlane | None = None
        self._thesis_impact: ThesisImpactAuthority | None = None
        self._thesis_impact_control: ResearchPlanThesisImpactCoordinator | None = None
        self._token_config_path = None if token_config_path is None else Path(token_config_path)
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._connection_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._store_executor: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def store(self) -> DaltonStore:
        if self._store is None:
            raise WriterServerError("writer server is not started")
        return self._store

    @property
    def registry(self) -> CapabilityRegistry:
        if self._registry is None:
            raise WriterServerError("writer server is not started")
        return self._registry

    @property
    def agenda(self) -> AgendaStore:
        if self._agenda is None:
            self._agenda = AgendaStore(self.store)
        return self._agenda

    @property
    def observability(self) -> ObservabilityStore:
        if self._observability is None:
            self._observability = ObservabilityStore(self.store)
        return self._observability

    @property
    def coverage_admission(self) -> CoverageAdmissionAuthority:
        if self._coverage_admission is None:
            raise WriterServerError("coverage-admission authority is unavailable")
        return self._coverage_admission

    @property
    def model_input(self) -> ModelInputLedger:
        if self._model_input is None:
            raise WriterServerError("model-input authority is unavailable")
        return self._model_input

    @property
    def industry_research(self) -> IndustryResearchAuthority:
        if self._industry_research is None:
            raise WriterServerError("industry-research authority is unavailable")
        return self._industry_research

    @property
    def weekly_brief(self) -> WeeklyBriefAuthority:
        if self._weekly_brief is None:
            raise WriterServerError("weekly-brief authority is unavailable")
        return self._weekly_brief

    @property
    def research_constitution(self) -> ResearchConstitutionAuthority:
        if self._research_constitution is None:
            raise WriterServerError("research-constitution authority is unavailable")
        return self._research_constitution

    @property
    def research_playbook(self) -> ResearchPlaybookAuthority:
        if self._research_playbook is None:
            raise WriterServerError("research-playbook authority is unavailable")
        return self._research_playbook

    @property
    def coverage_mission(self) -> CoverageMissionAuthority:
        if self._coverage_mission is None:
            raise WriterServerError("coverage-mission authority is unavailable")
        return self._coverage_mission

    @property
    def backlog(self) -> ResearchQuestionBacklog:
        if self._backlog is None:
            raise WriterServerError("research-question backlog is unavailable")
        return self._backlog

    @property
    def bounded_planner(self) -> BoundedPlannerAuthority:
        if self._bounded_planner is None:
            raise WriterServerError("bounded-planner authority is unavailable")
        return self._bounded_planner

    def _transcript_support_authority(self, authority_ref: str) -> dict[str, Any]:
        evidence = self.store.connection.execute(
            "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?",
            (authority_ref,),
        ).fetchone()
        if evidence is not None:
            return json.loads(evidence["evidence_json"])
        try:
            return self.observability.get_artifact_version_v2(authority_ref)
        except ObservabilityNotFound:
            pass
        source = self.store.connection.execute(
            "SELECT record_json FROM connector_source_envelopes "
            "WHERE source_envelope_id=?",
            (authority_ref,),
        ).fetchone()
        if source is not None:
            return json.loads(source["record_json"])
        raise TranscriptCorrectionNotFound(authority_ref)

    def _transcript_corrections(
        self, source_manifest: Mapping[str, Any]
    ) -> tuple[TranscriptCorrectionAuthority, dict[str, Any]]:
        if self._transcript_spool is None:
            raise WriterServerError("transcript correction spool is unavailable")
        manifest = validate_alphaengine_document_acquisition_manifest(
            source_manifest
        )
        authority = TranscriptCorrectionAuthority(
            self.store,
            spool=self._transcript_spool,
            manifest_resolver=(
                lambda ref: manifest if ref == manifest["id"] else None
            ),
            evidence_resolver=self._transcript_support_authority,
        )
        return authority, manifest

    @property
    def thesis_impact_control(self) -> ResearchPlanThesisImpactCoordinator:
        if self._thesis_impact_control is None:
            raise WriterServerError("thesis-impact control plane is unavailable")
        return self._thesis_impact_control

    @property
    def thesis_impact(self) -> ThesisImpactAuthority:
        if self._thesis_impact is None:
            raise WriterServerError("thesis-impact authority is unavailable")
        return self._thesis_impact

    @property
    def intent_writer(self) -> IntentWriterAuthority:
        if self._intent_writer is None:
            raise WriterServerError("intent writer authority is unavailable")
        return self._intent_writer

    @property
    def answer_routing(self) -> AnswerRoutingAuthority:
        if self._answer_routing is None:
            raise WriterServerError("answer-routing authority is unavailable")
        return self._answer_routing

    @property
    def answer_refresh(self) -> AnswerRefreshControlPlane:
        if self._answer_refresh is None:
            raise WriterServerError("answer-refresh control plane is unavailable")
        return self._answer_refresh

    def start(self) -> None:
        if self._listener is not None:
            raise WriterServerError("writer server is already started")
        socket_path = Path(self.socket_path)
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise WriterServerError("socket path is not a socket")
            socket_path.unlink()
        self._store_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dalton-store")
        self._connection_executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONNECTIONS, thread_name_prefix="dalton-rpc")
        try:
            self._store_executor.submit(self._open_store).result(timeout=STORE_REQUEST_TIMEOUT)
        except BaseException:
            self._connection_executor.shutdown(wait=True, cancel_futures=True)
            self._connection_executor = None
            self._store_executor.shutdown(wait=True, cancel_futures=True)
            self._store_executor = None
            raise
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self.socket_path)
            os.chmod(self.socket_path, 0o600)
            listener.listen(MAX_CONNECTIONS)
            listener.settimeout(0.25)
        except BaseException:
            listener.close()
            self._close_executors()
            raise
        self._listener = listener

    def _open_store(self) -> None:
        self._store = DaltonStore(self.db_path)
        self._registry = CapabilityRegistry(self._store)
        self._observability = ObservabilityStore(self._store)
        # Open the connector authority schema on the same Core so that
        # candidate promotion can verify SourceEnvelope / ArtifactVersion
        # provenance instead of failing on a missing table.
        self._connectors = ConnectorStore(self._store)
        self._agenda = AgendaStore(self._store)
        self._coverage_admission = CoverageAdmissionAuthority(self._store)
        self._model_input = ModelInputLedger(self._store)
        self._industry_research = IndustryResearchAuthority(self._store)
        self._weekly_brief = WeeklyBriefAuthority(
            self._store, self._industry_research
        )
        # The doctrine authority opens its append-only schema so a
        # constitution can bind a doctrine pack; nothing here admits
        # doctrine or context on its own.
        self._research_doctrine = ResearchDoctrineAuthority(self._store)
        self._model_forecast = ModelForecastAuthority(self._store)
        self._forecast_reconciliation = ForecastReconciliationAuthority(self._store)
        self._research_constitution = ResearchConstitutionAuthority(self._store)
        # Playbook (research method) and CoverageMission (task layer) are
        # human-only, append-only authorities; opening them only creates
        # their schemas.
        self._research_playbook = ResearchPlaybookAuthority(self._store)
        self._coverage_mission = CoverageMissionAuthority(self._store)
        if self._discovery_plan_path is not None:
            # An unusable plan must not keep the writer (and every other lane)
            # from starting; the discovery op reports the reason instead.
            try:
                plan = load_discovery_plan(self._discovery_plan_path)
            except (OSError, ValueError) as exc:
                self._discovery_plan_error = f"discovery plan is unusable: {exc}"
            else:
                self._discovery_plan_error = None
                self._source_discovery = MissionSourceDiscoveryCoordinator(
                    store=self._store,
                    missions=self._coverage_mission,
                    plan=plan,
                    search_launcher=self._search_launcher,
                    acquisition_launcher=self._acquisition_launcher,
                )
        self._backlog = ResearchQuestionBacklog(self._store)
        self._bounded_planner = BoundedPlannerAuthority(self._store)
        self._intent_writer = IntentWriterAuthority(
            self._agenda, self._backlog, self._bounded_planner
        )
        self._answer_routing = AnswerRoutingAuthority(
            self._store,
            self._agenda,
            self._backlog,
            self._bounded_planner,
            self._industry_research,
        )
        if self._transcript_spool_dir is not None:
            self._transcript_spool = RawSpool(
                self._transcript_spool_dir, max_total_bytes=1_000_000_000
            )
        if self._candidate_staging_path is not None:
            self._candidate_staging = CandidateStagingStore(self._candidate_staging_path)
            self._candidate_review = HumanReviewAuthority(self._candidate_staging_path)
        if self._scheduler_path is not None:
            self._scheduler = Scheduler(self._scheduler_path)
            self._bounded_control = BoundedPlannerControlPlane(
                self._bounded_planner,
                self._observability,
                self._scheduler,
            )
            self._answer_refresh = AnswerRefreshControlPlane(
                self._answer_routing,
                self._bounded_control,
            )
            self._research_plan = ResearchPlanAuthority(self._store)
            self._thesis_impact = ThesisImpactAuthority(
                self._store, self._scheduler
            )
            self._thesis_impact_control = ResearchPlanThesisImpactCoordinator(
                plan=self._research_plan,
                backlog=self._backlog,
                scheduler=self._scheduler,
                impact=self._thesis_impact,
            )

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        listener = self._listener
        while not self._stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            conn.settimeout(CONNECTION_IDLE_TIMEOUT)
            if not self._connection_slots.acquire(blocking=False):
                conn.close()
                continue
            executor = self._connection_executor
            if executor is None:
                conn.close()
                self._connection_slots.release()
                break
            try:
                executor.submit(self._serve_connection_guarded, conn)
            except RuntimeError:
                conn.close()
                self._connection_slots.release()
                if not self._stop.is_set():
                    raise

    def _serve_connection_guarded(self, conn: socket.socket) -> None:
        try:
            with conn:
                self._serve_connection(conn)
        finally:
            self._connection_slots.release()

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._close_executors()
        try:
            path = Path(self.socket_path)
            if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
                path.unlink()
        except OSError:
            pass

    def _close_executors(self) -> None:
        connection_executor, self._connection_executor = self._connection_executor, None
        if connection_executor is not None:
            connection_executor.shutdown(wait=True, cancel_futures=True)
        store_executor, self._store_executor = self._store_executor, None
        if store_executor is not None:
            try:
                store_executor.submit(self._close_store).result(timeout=STORE_REQUEST_TIMEOUT)
            except (RuntimeError, concurrent.futures.TimeoutError):
                pass
            store_executor.shutdown(wait=True, cancel_futures=True)

    def _close_store(self) -> None:
        if self._acquisition_launcher is not None:
            self._acquisition_launcher.close()
        if self._sec_lane_launcher is not None:
            self._sec_lane_launcher.close()
        if self._candidate_review is not None:
            self._candidate_review.close()
            self._candidate_review = None
        if self._candidate_staging is not None:
            self._candidate_staging.close()
            self._candidate_staging = None
        if self._scheduler is not None:
            self._scheduler.close()
            self._scheduler = None
        self._research_plan = None
        self._backlog = None
        self._bounded_planner = None
        self._llm_planner_coordinator_instance = None
        self._bounded_control = None
        self._intent_writer = None
        self._answer_routing = None
        self._answer_refresh = None
        self._thesis_impact = None
        self._thesis_impact_control = None
        self._weekly_brief = None
        self._research_doctrine = None
        self._model_forecast = None
        self._forecast_reconciliation = None
        self._research_constitution = None
        if self._store is not None:
            self._store.close()
            self._store = None
        self._registry = None
        self._agenda = None
        self._observability = None
        self._connectors = None
        self._coverage_admission = None
        self._model_input = None
        self._industry_research = None
        self._transcript_spool = None

    def _serve_connection(self, conn: socket.socket) -> None:
        reader = conn.makefile("rb")
        try:
            while not self._stop.is_set():
                line = reader.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                request_id = "unknown"
                try:
                    raw = decode_frame(line)
                    candidate = raw.get("request_id")
                    if isinstance(candidate, str) and candidate:
                        request_id = candidate
                    request = parse_request(raw)
                    executor = self._store_executor
                    if executor is None:
                        raise WriterServerError("writer server is stopping")
                    result = executor.submit(self._handle, request).result(timeout=STORE_REQUEST_TIMEOUT)
                    conn.sendall(success_frame(request.request_id, result))
                except ProtocolError:
                    conn.sendall(error_frame(request_id, "protocol_error", "malformed request"))
                except PermissionError:
                    conn.sendall(error_frame(request_id, "forbidden", "operation is not permitted"))
                except Exception as exc:  # all exceptions are intentionally sanitized
                    conn.sendall(error_frame(request_id, self._error_code(exc), self._error_message(exc)))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            reader.close()

    def _principal(self, token: str) -> Principal:
        if self._token_config_path is not None:
            refreshed = load_principals(self._token_config_path)
            self.principals = refreshed
            self._by_token = {principal.token: principal for principal in refreshed.values()}
        for known, principal in self._by_token.items():
            if hmac.compare_digest(known, token):
                return principal
        raise PermissionError("invalid token")

    def _handle(self, request: Any) -> Any:
        principal = self._principal(request.auth_token)
        operation = request.operation
        if operation not in OPERATION_FIELDS:
            raise PermissionError("unknown operation")
        if operation not in principal.operations:
            raise PermissionError("operation is not permitted")
        unknown = set(request.params) - OPERATION_FIELDS[operation]
        if unknown:
            raise ProtocolError("unknown operation parameter")
        params = self._authorized_params(principal, operation, request.params)
        method = getattr(self, f"_op_{operation}")
        return method(params)

    def _authorized_params(self, principal: Principal, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Bind actor and invocation provenance to the authenticated principal."""
        result = dict(params)
        actor_field = OPERATION_ACTOR_FIELDS.get(operation)
        if actor_field is not None:
            actor = principal.resolved_actor_ref
            supplied_actor = result.get(actor_field)
            if supplied_actor is not None and supplied_actor != actor:
                raise PermissionError("request actor does not match principal")
            result[actor_field] = actor
        is_scoped_feedback = (
            principal.principal_id in SCOPED_FEEDBACK_PRINCIPALS
            and operation == "record_agenda_feedback"
            and principal.operations
            == SCOPED_FEEDBACK_OPERATION_SETS[principal.principal_id]
        )
        is_scoped_weekly_feedback = (
            principal.principal_id == "dashboard-control"
            and operation == "record_weekly_brief_feedback"
            and principal.operations
            == SCOPED_FEEDBACK_OPERATION_SETS["dashboard-control"]
        )
        is_mission_automation = (
            operation in MISSION_AUTOMATION_OPERATIONS
            and _AUTOMATION_ACTOR_RE.fullmatch(principal.resolved_actor_ref) is not None
        )
        is_core_reconciliation = (
            operation in CORE_RECONCILIATION_OPERATIONS and principal.is_unrestricted
        ) or (
            operation in CORE_DISCOVERY_OPERATIONS and principal.is_unrestricted
        )
        if operation in HUMAN_GOVERNANCE_OPERATIONS and _HUMAN_ACTOR_RE.fullmatch(
            principal.resolved_actor_ref
        ) is None:
            if not (
                (operation == "record_agenda_feedback" and is_scoped_feedback)
                or (
                    operation == "record_weekly_brief_feedback"
                    and is_scoped_weekly_feedback
                )
                or is_mission_automation
                or is_core_reconciliation
            ):
                raise PermissionError("governance changes require an authenticated human principal")
        if operation == "record_agenda_feedback" and is_scoped_feedback:
            subject_ref = result.get("subject_ref")
            source_event_ref = result.get("source_event_ref")
            if principal.principal_id == "feedback-bridge":
                valid = (
                    isinstance(subject_ref, str) and subject_ref.startswith("human:discord-")
                    and result.get("source") == "openclaw_discord_reaction"
                    and isinstance(source_event_ref, str)
                    and source_event_ref.startswith("discord-reaction:")
                )
            elif principal.principal_id == "dashboard-control":
                valid = (
                    isinstance(subject_ref, str) and subject_ref.startswith("human:tailscale-")
                    and result.get("source") == "tailscale_dashboard"
                    and isinstance(source_event_ref, str)
                    and source_event_ref.startswith("dashboard-feedback:")
                )
            else:
                valid = (
                    subject_ref == "automation:timeout"
                    and result.get("source") == "auto_accept_timeout"
                    and result.get("verdict") == "agree"
                    and isinstance(source_event_ref, str)
                    and source_event_ref.startswith("agenda-timeout:")
                )
            if not valid:
                raise PermissionError("scoped feedback provenance is invalid")
        if operation == "record_weekly_brief_feedback" and is_scoped_weekly_feedback:
            subject_ref = result.get("subject_ref")
            if not (
                isinstance(subject_ref, str)
                and subject_ref.startswith("human:tailscale-")
            ):
                raise PermissionError(
                    "scoped weekly brief feedback provenance is invalid"
                )
        if operation == "commit_reviewed_candidate" and not principal.is_unrestricted:
            # The scoped review principal may hold a subset of the review
            # control operations (a live token file issued before a read-only
            # op was added still qualifies); it may never hold any other op.
            if (
                principal.principal_id not in SCOPED_REVIEW_PRINCIPALS
                or not frozenset(principal.operations) <= RESEARCH_REVIEW_CONTROL_OPERATIONS
                or "commit_reviewed_candidate" not in principal.operations
            ):
                raise PermissionError("review promotion requires the scoped review control principal")
            decision = result.get("decision")
            if not isinstance(decision, Mapping):
                raise PermissionError("review decision is required")
            reviewer_ref = decision.get("reviewer_ref")
            source_event_ref = decision.get("source_event_ref")
            if (
                not isinstance(reviewer_ref, str)
                or _HUMAN_ACTOR_RE.fullmatch(reviewer_ref) is None
                or not reviewer_ref.startswith("human:tailscale-")
                or decision.get("authorization") != "explicit_human_review"
                or decision.get("source") != "tailscale_review"
                or not isinstance(source_event_ref, str)
                or not source_event_ref.startswith("research-review:")
                or decision.get("verdict") != "accept"
            ):
                raise PermissionError("review promotion provenance is invalid")
        if operation in {"stage_change", "verify_change"}:
            self._authorize_invocation_subject(principal, operation, result)
        elif operation == "register_claim":
            refs = result.pop("producer_invocation_refs", None)
            claim = dict(result.get("claim") or {})
            embedded = claim.get("producer_invocation_refs")
            if refs is None:
                refs = embedded
            elif embedded is not None and list(embedded) != list(refs):
                raise PermissionError("claim producer provenance is inconsistent")
            if not isinstance(refs, list) or not refs:
                raise PermissionError("claim producer invocation references are required")
            self._authorize_invocation_refs(principal, refs)
            claim["producer_invocation_refs"] = list(refs)
            if "actor_ref" in claim and claim["actor_ref"] != principal.resolved_actor_ref:
                raise PermissionError("claim actor does not match principal")
            claim["actor_ref"] = principal.resolved_actor_ref
            top_claim_ref = result.get("claim_ref") or result.get("claim_id")
            embedded_claim_ref = claim.get("claim_ref")
            if top_claim_ref is not None and embedded_claim_ref is not None and top_claim_ref != embedded_claim_ref:
                raise PermissionError("claim stable reference is inconsistent")
            self._authorize_version_owner(
                principal, "claim_versions", "claim_ref",
                top_claim_ref or embedded_claim_ref,
                "claim_json",
            )
            result["claim"] = claim
        elif operation == "adjudicate_claim":
            ref = result.pop("adjudicator_invocation_ref", None)
            adjudication = dict(result.get("adjudication") or {})
            embedded = adjudication.get("adjudicator_invocation_ref")
            if ref is None:
                ref = embedded
            elif embedded is not None and embedded != ref:
                raise PermissionError("adjudicator provenance is inconsistent")
            self._authorize_invocation_refs(principal, [ref])
            adjudication["adjudicator_invocation_ref"] = ref
            result["adjudication"] = adjudication
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation == "submit_capability_proposal":
            ref = result.get("builder_invocation_ref")
            proposal = dict(result.get("proposal") or {})
            participants = dict(proposal.get("participants") or {})
            embedded = participants.get("builder_invocation_ref") or participants.get("builder_invocation_id") or participants.get("builder")
            if ref is None:
                ref = embedded
            elif embedded is not None and embedded != ref:
                raise PermissionError("builder provenance is inconsistent")
            self._authorize_invocation_refs(principal, [ref])
            if participants.get("actor_ref") not in {None, principal.resolved_actor_ref}:
                raise PermissionError("proposal actor does not match principal")
            participants["builder_invocation_ref"] = ref
            participants["actor_ref"] = principal.resolved_actor_ref
            proposal["participants"] = participants
            result["proposal"] = proposal
            result["builder_invocation_ref"] = ref
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation == "record_capability_evaluation":
            ref = result.pop("evaluator_invocation_ref", None)
            self._authorize_invocation_refs(principal, [ref])
            result["evaluator_invocation"] = ref
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation in {"decide_capability_promotion", "rollback_capability"}:
            if _HUMAN_ACTOR_RE.fullmatch(principal.resolved_actor_ref) is None:
                raise PermissionError("capability governance requires an authenticated human principal")
            result["actor_ref"] = principal.resolved_actor_ref
        elif operation in {"register_evidence", "relate_evidence"}:
            payload_name = "evidence" if operation == "register_evidence" else "relation"
            payload = dict(result.get(payload_name) or {})
            if payload.get("actor_ref") not in {None, principal.resolved_actor_ref}:
                raise PermissionError("record actor does not match principal")
            payload["actor_ref"] = principal.resolved_actor_ref
            result[payload_name] = payload
            result["actor_ref"] = principal.resolved_actor_ref
            if operation == "register_evidence":
                top_evidence_ref = result.get("evidence_ref") or result.get("evidence_id")
                embedded_evidence_ref = payload.get("evidence_ref")
                if top_evidence_ref is not None and embedded_evidence_ref is not None and top_evidence_ref != embedded_evidence_ref:
                    raise PermissionError("evidence stable reference is inconsistent")
                self._authorize_version_owner(
                    principal, "evidence_versions", "evidence_ref",
                    top_evidence_ref or embedded_evidence_ref,
                    "evidence_json",
                )
            else:
                if result.get("relation_id") is None and payload.get("id") is None:
                    identity = {
                        "evidence_version_ref": payload.get("evidence_version_ref"),
                        "claim_version_ref": payload.get("claim_version_ref"),
                        "relation": payload.get("relation"),
                    }
                    if any(not isinstance(value, str) or not value for value in identity.values()):
                        raise PermissionError("relation identity is incomplete")
                    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    result["relation_id"] = f"relation:{hashlib.sha256(encoded).hexdigest()}"
                if result.get("idempotency_key") is None:
                    relation_ref = result.get("relation_id") or payload.get("id")
                    result["idempotency_key"] = f"relation-write:{relation_ref}"
                self._authorize_relation_target(principal, payload)
        return result

    def _authorize_invocation_subject(self, principal: Principal, operation: str, params: Mapping[str, Any]) -> None:
        """Bind worker/verifier requests to core-created immutable subjects.

        ``DaltonStore`` deliberately accepts a complete invocation mapping for
        its trusted in-process API.  The RPC boundary must be stricter: a
        runtime cannot use that convenience to self-assert a model family or
        actor and then ask the commit gate to call it independent.
        """
        if principal.is_unrestricted:
            return
        if operation == "stage_change":
            ref_key = "producer_invocation_id"
            inline_key = "producer_invocation"
        else:
            ref_key = "verifier_invocation_id"
            inline_key = "verifier_invocation"
        ref = params.get(ref_key)
        if not isinstance(ref, str) or not ref:
            raise PermissionError("a core-registered invocation reference is required")
        if inline_key in params and params.get(inline_key) is not None:
            raise PermissionError("inline invocation is not permitted")
        # A nested change/verification mapping is another common way to hide
        # an inline self-asserted invocation.  Reject it explicitly.
        nested_key = "change" if operation == "stage_change" else "verification"
        nested = params.get(nested_key)
        if isinstance(nested, Mapping) and (inline_key in nested or "invocation" in nested):
            raise PermissionError("inline invocation is not permitted")
        self._authorize_invocation_refs(principal, [ref])

    def _authorize_invocation_refs(self, principal: Principal, refs: Any) -> None:
        if principal.is_unrestricted:
            return
        if not isinstance(refs, (list, tuple)) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
            raise PermissionError("core-registered invocation references are required")
        for ref in refs:
            if ref not in principal.allowed_invocation_refs:
                raise PermissionError("invocation reference is not assigned to this principal")
            row = self.store.conn.execute(
                "SELECT work_order_ref FROM model_invocations WHERE invocation_id=?", (ref,)
            ).fetchone()
            if row is None:
                raise PermissionError("invocation reference is not registered")
            if principal.work_order_refs and row[0] not in principal.work_order_refs:
                raise PermissionError("work order is not assigned to this principal")

    def _authorize_version_owner(
        self, principal: Principal, table: str, ref_column: str, stable_ref: Any, json_column: str,
    ) -> None:
        """Prevent a scoped runtime from appending to another actor's stable ID."""
        if principal.is_unrestricted:
            return
        if not isinstance(stable_ref, str) or not stable_ref:
            raise PermissionError("stable record reference is required")
        row = self.store.conn.execute(
            f"SELECT {json_column} FROM {table} WHERE {ref_column}=? ORDER BY version_number DESC LIMIT 1",
            (stable_ref,),
        ).fetchone()
        if row is None:
            return
        try:
            actor_ref = json.loads(row[0]).get("actor_ref")
        except (TypeError, json.JSONDecodeError):
            raise PermissionError("existing record provenance is invalid") from None
        if actor_ref != principal.resolved_actor_ref:
            raise PermissionError("stable record belongs to another actor")

    def _authorize_relation_target(self, principal: Principal, relation: Mapping[str, Any]) -> None:
        """A researcher may reuse shared evidence but may only mutate its assigned claim graph."""
        if principal.is_unrestricted:
            return
        claim_version_ref = relation.get("claim_version_ref") or relation.get("claim_version_id")
        if not isinstance(claim_version_ref, str) or not claim_version_ref:
            raise PermissionError("claim version reference is required")
        row = self.store.conn.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (claim_version_ref,)
        ).fetchone()
        if row is None:
            raise PermissionError("claim version is not registered")
        try:
            claim = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            raise PermissionError("claim provenance is invalid") from None
        if claim.get("actor_ref") != principal.resolved_actor_ref:
            raise PermissionError("claim belongs to another actor")
        producer_refs = claim.get("producer_invocation_refs")
        self._authorize_invocation_refs(principal, producer_refs)

    def _op_register_invocation(self, p: Mapping[str, Any]) -> Any:
        return self.store.register_invocation(p.get("invocation"))

    def _op_stage_change(self, p: Mapping[str, Any]) -> Any:
        return self.store.stage_change(**dict(p))

    def _op_verify_change(self, p: Mapping[str, Any]) -> Any:
        return self.store.verify_change(**dict(p))

    def _op_commit(self, p: Mapping[str, Any]) -> Any:
        return self.store.commit(**dict(p))

    def _op_commit_reviewed_candidate(self, p: Mapping[str, Any]) -> Any:
        return self.store.commit_reviewed_candidate(**dict(p))

    def _op_create_policy(self, p: Mapping[str, Any]) -> Any:
        return self.store.create_policy(**dict(p))

    def _op_current_pointer(self, p: Mapping[str, Any]) -> Any:
        return self.store.current_pointer(**dict(p))

    def _op_get_version(self, p: Mapping[str, Any]) -> Any:
        return self.store.get_version(**dict(p))

    def _op_list_events(self, p: Mapping[str, Any]) -> Any:
        return self.store.list_events(**dict(p))

    def _op_active_policy(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("active_policy takes no parameters")
        return self.store.active_policy()

    def _op_register_evidence(self, p: Mapping[str, Any]) -> Any:
        return self.store.register_evidence(**dict(p))

    def _op_register_claim(self, p: Mapping[str, Any]) -> Any:
        return self.store.register_claim(**dict(p))

    def _op_relate_evidence(self, p: Mapping[str, Any]) -> Any:
        return self.store.relate_evidence(**dict(p))

    def _op_adjudicate_claim(self, p: Mapping[str, Any]) -> Any:
        return self.store.adjudicate_claim(**dict(p))

    def _op_submit_capability_proposal(self, p: Mapping[str, Any]) -> Any:
        return self.registry.submit_proposal(**dict(p))

    def _op_record_capability_evaluation(self, p: Mapping[str, Any]) -> Any:
        return self.registry.record_evaluation(**dict(p))

    def _op_decide_capability_promotion(self, p: Mapping[str, Any]) -> Any:
        return self.registry.decide_promotion(**dict(p))

    def _op_rollback_capability(self, p: Mapping[str, Any]) -> Any:
        return self.registry.rollback(**dict(p))

    def _op_active_capability(self, p: Mapping[str, Any]) -> Any:
        return self.registry.active_pointer(**dict(p))

    def _op_get_capability_version(self, p: Mapping[str, Any]) -> Any:
        return self.registry.get_version(**dict(p))

    def _op_get_capability_evaluation(self, p: Mapping[str, Any]) -> Any:
        return self.registry.get_evaluation(**dict(p))

    def _op_get_capability_decision(self, p: Mapping[str, Any]) -> Any:
        return self.registry.get_decision(**dict(p))

    def _op_capability_pointer_history(self, p: Mapping[str, Any]) -> Any:
        return self.registry.pointer_history(**dict(p))

    def _op_create_agenda_policy(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.create_policy(**dict(p))

    def _op_active_agenda_policy(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.active_policy(**dict(p))

    def _op_agenda_budget_status(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.budget_status(**dict(p))

    def _op_create_mandate(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.create_mandate(**dict(p))

    def _op_active_mandates(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.active_mandates(**dict(p))

    def _op_create_priority_override(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.create_priority_override(**dict(p))

    def _op_active_priority_overrides(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.active_priority_overrides(**dict(p))

    def _op_set_agenda_pause(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.set_pause(**dict(p))

    def _op_agenda_control_state(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("agenda_control_state takes no parameters")
        return self.agenda.control_state()

    def _op_register_perception_snapshot(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        snapshot = values.pop("snapshot")
        return self.agenda.register_perception_snapshot(snapshot, **values)

    def _op_materialize_agenda_context(self, p: Mapping[str, Any]) -> Any:
        # Materialization runs inside the writer service on purpose: the
        # caller has no database path, no raw spool, and no way to substitute
        # a body for the mandate or the perception snapshot it names.
        return build_agenda_context(
            self.store, self.observability, **dict(p)
        )

    def _op_get_agenda_mandate_version(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.mandate_version(**dict(p))

    def _op_get_agenda_policy_version(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.policy_version(**dict(p))

    def _op_get_perception_snapshot(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.perception_snapshot(**dict(p))

    def _op_start_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.start_cycle(**dict(p))

    def _op_add_agenda_candidates(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.add_candidates(**dict(p))

    def _op_decide_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.decide_cycle(**dict(p))

    def _op_fail_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.fail_cycle(**dict(p))

    def _op_agenda_cycle(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.cycle(**dict(p))

    def _op_agenda_cycle_by_key(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.cycle_by_key(**dict(p))

    def _op_pending_agenda_outbox(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.pending_outbox(**dict(p))

    def _op_claim_agenda_outbox(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.claim_outbox(**dict(p))

    def _op_record_agenda_delivery(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.record_delivery(**dict(p))

    def _op_list_agenda_feedback_targets(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.feedback_targets(**dict(p))

    def _op_record_agenda_feedback(self, p: Mapping[str, Any]) -> Any:
        return self.agenda.record_feedback(**dict(p))

    def _op_intent_context_bindings(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("intent_context_bindings takes no parameters")
        return self.intent_writer.context_bindings()

    def _op_admit_intent_question(self, p: Mapping[str, Any]) -> Any:
        return self.intent_writer.admit_question(**dict(p))

    def _op_issue_intent_directive(self, p: Mapping[str, Any]) -> Any:
        return self.intent_writer.issue_directive(**dict(p))

    def _op_publish_answer_sufficiency_policy(
        self, p: Mapping[str, Any]
    ) -> Any:
        return self.answer_routing.publish_policy(**dict(p))

    def _op_answer_subjects(self, p: Mapping[str, Any]) -> Any:
        return self.answer_routing.subjects(**dict(p))

    def _op_route_answer(self, p: Mapping[str, Any]) -> Any:
        return self.answer_routing.route(**dict(p))

    def _op_dispatch_answer_refresh(self, p: Mapping[str, Any]) -> Any:
        return self.answer_refresh.dispatch(**dict(p))

    def _op_create_workflow_version(self, p: Mapping[str, Any]) -> Any:
        return self.observability.create_workflow_version(**dict(p))

    def _op_link_work_order(self, p: Mapping[str, Any]) -> Any:
        return self.observability.link_work_order(**dict(p))

    def _op_record_usage(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        invocation_ref = values.pop("invocation_ref")
        return self.observability.record_usage(invocation_ref, **values)

    def _op_create_price_rate_version(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        price_rate_ref = values.pop("price_rate_ref")
        return self.observability.create_price_rate_version(price_rate_ref, **values)

    def _op_record_cost(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        usage_entry_ref = values.pop("usage_entry_ref")
        return self.observability.record_cost(usage_entry_ref, **values)

    def _op_register_driver_pack(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        driver_pack_ref = values.pop("driver_pack_ref")
        return self.coverage_admission.register_driver_pack(driver_pack_ref, **values)

    def _op_get_driver_pack(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_admission.driver_pack(**dict(p))

    def _op_propose_thesis_admission(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_admission.propose_thesis_admission(**dict(p))

    def _op_get_thesis_admission_candidate(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_admission.candidate(**dict(p))

    def _op_decide_thesis_admission(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_admission.decide_thesis_admission(**dict(p))

    def _op_get_thesis_admission_decision(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_admission.decision(**dict(p))

    def _op_publish_research_constitution(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        constitution_ref = values.pop("constitution_ref")
        return self.research_constitution.publish_constitution(constitution_ref, **values)

    def _op_get_research_constitution(self, p: Mapping[str, Any]) -> Any:
        return self.research_constitution.constitution(**dict(p))

    def _op_get_active_research_constitution(self, p: Mapping[str, Any]) -> Any:
        return self.research_constitution.active_constitution(**dict(p))

    def _op_research_constitution_report(self, p: Mapping[str, Any]) -> Any:
        return self.research_constitution.constitution_report()

    def _op_publish_research_playbook(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        playbook_ref = values.pop("playbook_ref")
        return self.research_playbook.publish_playbook(playbook_ref, **values)

    def _op_get_research_playbook(self, p: Mapping[str, Any]) -> Any:
        return self.research_playbook.playbook(**dict(p))

    def _op_get_active_research_playbook(self, p: Mapping[str, Any]) -> Any:
        return self.research_playbook.active_playbook(**dict(p))

    def _op_research_playbook_report(self, p: Mapping[str, Any]) -> Any:
        return self.research_playbook.playbook_report()

    def _op_create_coverage_mission(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        mission_ref = values.pop("mission_ref")
        return self.coverage_mission.create_mission(mission_ref, **values)

    def _op_get_coverage_mission(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_mission.mission(**dict(p))

    def _op_get_active_coverage_mission(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_mission.active_mission(**dict(p))

    def _op_record_mission_stage(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_mission.record_stage(**dict(p))

    def _op_coverage_mission_progress(self, p: Mapping[str, Any]) -> Any:
        return self.coverage_mission.mission_progress(**dict(p))

    def _op_coverage_mission_stage_records(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        values.setdefault("company_ref", None)
        return {
            "projection_kind": "coverage_mission_stage_records",
            "records": self.coverage_mission.stage_records(**values),
        }

    def _op_company_research_view(self, p: Mapping[str, Any]) -> Any:
        return build_company_research_view(self.store, dict(p)["company_ref"])

    def _op_company_research_query(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        values.setdefault("limit", 100)
        return {
            "projection_kind": "company_research_query",
            "claims": query_company_research(self.store, **values),
        }

    def _op_publish_doctrine_pack(self, p: Mapping[str, Any]) -> Any:
        if self._research_doctrine is None:
            raise WriterServerError("research-doctrine authority is unavailable")
        values = dict(p)
        doctrine_pack_ref = values.pop("doctrine_pack_ref")
        return self._research_doctrine.publish_pack(doctrine_pack_ref, **values)

    def _op_get_doctrine_pack(self, p: Mapping[str, Any]) -> Any:
        if self._research_doctrine is None:
            raise WriterServerError("research-doctrine authority is unavailable")
        return self._research_doctrine.pack(dict(p)["version_ref"])

    def _op_record_backlog_question(self, p: Mapping[str, Any]) -> Any:
        return self.backlog.record_question(**dict(p))

    def _op_publish_probe_template(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        template_ref = values.pop("template_ref")
        return self.bounded_planner.publish_probe_template(template_ref, **values)

    def _op_create_bounded_planner_loop(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        loop_ref = values.pop("loop_ref")
        return self.bounded_planner.create_loop(loop_ref, **values)

    def _op_bounded_probe_template(self, p: Mapping[str, Any]) -> Any:
        return self.bounded_planner.probe_template(dict(p)["version_ref"])

    def _op_bounded_planner_loop(self, p: Mapping[str, Any]) -> Any:
        return self.bounded_planner.loop(dict(p)["version_ref"])

    def _op_materialize_bounded_planner_context(self, p: Mapping[str, Any]) -> Any:
        if self._research_doctrine is None or self._bounded_planner is None:
            raise WriterServerError("doctrine or bounded-planner authority is unavailable")
        values = dict(p)
        loop_version_ref = values.pop("loop_version_ref")
        return self._research_doctrine.materialize_planner_context(
            self._bounded_planner, loop_version_ref, **values
        )

    def _llm_planner_coordinator(self) -> LLMResearchPlannerCoordinator:
        if self._bounded_planner is None or self._scheduler is None:
            raise WriterServerError("bounded-planner control plane is unavailable")
        if self._llm_planner_coordinator_instance is None:
            self._llm_planner_coordinator_instance = LLMResearchPlannerCoordinator(
                self._bounded_planner, self._scheduler
            )
        return self._llm_planner_coordinator_instance

    def _op_llm_planner_prepare(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        context_pack_ref = values.pop("context_pack_ref")
        budget = {key: value for key, value in values.items() if value is not None}
        return self._llm_planner_coordinator().prepare(context_pack_ref, **budget)

    def _op_llm_planner_advance(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        return self._llm_planner_coordinator().advance(
            values["context_pack_ref"], values["work_order"]
        )

    def _op_llm_planner_execute(self, p: Mapping[str, Any]) -> Any:
        # Runs inside the writer: the planner model worker writes model
        # accounting into this Core, which the driver process must not open.
        if self._planner_model_config is None:
            raise WriterServerError("planner model execution is not configured")
        config = self._planner_model_config
        values = dict(p)
        context_pack_ref = values.pop("context_pack_ref")
        budget = {key: value for key, value in values.items() if value is not None}
        coordinator = self._llm_planner_coordinator()
        prepared = coordinator.prepare(context_pack_ref, **budget)
        if prepared.get("status") != "model_work_ready":
            return prepared
        work_order = prepared["work_order"]
        from .model_router import ModelRouter
        from .openclaw_model_adapter import OpenClawModelAdapter

        with ModelRouter(config["model_router_db"]) as router:
            adapter = OpenClawModelAdapter(
                str(config["broker_socket"]),
                route_resolver=router.get_decision,
                auth_client_id=config["broker_client_id"],
                auth_key_provider=lambda: Path(
                    config["broker_auth_key"]
                ).read_bytes().strip(),
                expected_agent_id=config["expected_agent_id"],
                timeout_seconds=120.0,
            )
            worker = LLMResearchPlannerModelWorker(
                scheduler=self._scheduler,
                router=router,
                adapter=adapter,
                store=self.store,
                observability=self.observability,
                routing_policy_ref=config["routing_policy_ref"],
                credential_slot_refs=config["credential_slot_refs"],
            )
            run = worker.run_once(work_order)
        if run.get("status") != "succeeded":
            return {"status": f"model_{run.get('status')}", "work_order_ref": work_order["id"]}
        return coordinator.advance(context_pack_ref, work_order)

    @property
    def model_forecast(self) -> ModelForecastAuthority:
        if self._model_forecast is None:
            raise WriterServerError("model-forecast authority is unavailable")
        return self._model_forecast

    def _op_publish_forecast_line(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        line_ref = values.pop("line_ref")
        return self.model_forecast.publish_line(line_ref, **values)

    def _op_get_forecast_line(self, p: Mapping[str, Any]) -> Any:
        return self.model_forecast.line(dict(p)["version_ref"])

    def _op_extend_growth_forecast(self, p: Mapping[str, Any]) -> Any:
        return extend_growth(self.model_input, self.model_forecast, **dict(p))

    @property
    def forecast_reconciliation(self) -> ForecastReconciliationAuthority:
        if self._forecast_reconciliation is None:
            raise WriterServerError("forecast-reconciliation authority is unavailable")
        return self._forecast_reconciliation

    def _op_reconcile_forecasts(self, p: Mapping[str, Any]) -> Any:
        # P9c.  A human principal reconciles on their own request without a
        # mission.  The core principal (controller tick) and mission automation
        # reconcile only under the sole active CoverageMission that grants the
        # forecast_reconciliation scope; unauthorized pairs are reported as
        # skipped, never written.
        values = dict(p)
        requested_by = values.pop("requested_by")
        resolver = None
        if not requested_by.startswith("human:"):
            actor = None if requested_by == "core" else requested_by

            def resolver(company_ref: str, _actor=actor) -> dict[str, Any]:
                return self.coverage_mission.authorize_forecast_reconciliation(
                    company_ref=company_ref, actor_ref=_actor,
                )
            # The record names the mission principal, resolved per company.
            requested_by = "automation:coverage-mission" if actor is None else actor
        result = self.forecast_reconciliation.reconcile_pending(
            requested_by=requested_by,
            mission_resolver=resolver,
            company_ref=values.get("company_ref"),
            claim_version_ref=values.get("claim_version_ref"),
        )
        return {
            "status": result["status"],
            "created": [
                {
                    "ref": item["id"], "hash": item["content_hash"],
                    "subject_ref": item["subject_ref"], "metric_ref": item["metric_ref"],
                    "period": item["period"], "forecast_value": item["forecast_value"],
                    "actual_value": item["actual_value"], "unit": item["unit"],
                    "currency": item["currency"],
                    "deviation_percent": item["deviation_percent"],
                    "direction": item["direction"], "band": item["band"],
                    "human_checkpoint": item["human_checkpoint"],
                    "mission_binding": item["mission_binding"],
                    "requested_by": item["requested_by"], "status": item["status"],
                }
                for item in result["created"]
            ],
            "skipped": result["skipped"],
        }

    def _op_forecast_reconciliations(self, p: Mapping[str, Any]) -> Any:
        return {
            "projection_kind": "forecast_reconciliations",
            "reconciliations": self.forecast_reconciliation.reconciliations(**dict(p)),
        }

    def _op_get_forecast_reconciliation(self, p: Mapping[str, Any]) -> Any:
        return self.forecast_reconciliation.reconciliation(dict(p)["reconciliation_ref"])

    def _op_decide_forecast_overturn(self, p: Mapping[str, Any]) -> Any:
        return self.forecast_reconciliation.decide_overturn(**dict(p))

    # -- P9d-1: mission source discovery ------------------------------------
    @property
    def source_discovery(self) -> MissionSourceDiscoveryCoordinator:
        if self._source_discovery is None:
            raise DiscoveryLaunchRejected(
                self._discovery_plan_error
                or "mission source discovery is not configured on this writer (no discovery plan)"
            )
        return self._source_discovery

    @property
    def search_launcher(self) -> AlphaEngineSearchLauncher:
        if self._search_launcher is None:
            raise DiscoveryLaunchRejected(
                "AlphaEngine search launcher is not configured on this writer"
            )
        return self._search_launcher

    def _op_dispatch_mission_source_discovery(self, p: Mapping[str, Any]) -> Any:
        # Controller tick.  Unconfigured writers answer truthfully instead of
        # raising so the driver's tick summary shows the reason.
        if self._source_discovery is None:
            return {
                "status": "unconfigured",
                "reason": self._discovery_plan_error or "no discovery plan on this writer",
            }
        return self.source_discovery.dispatch_once()

    def _op_run_mission_source_discovery(self, p: Mapping[str, Any]) -> Any:
        # Human-requested discovery: the mission grant is resolved here with the
        # human as requester (a probe_only source is allowed for rehearsal),
        # and the child re-derives the same grant before spending a call.
        values = dict(p)
        plan = self.source_discovery.plan
        authorization = self.coverage_mission.authorize_source_discovery(
            company_ref=values["company_ref"],
            source_ref=plan["source_ref"],
            requested_by=values["requested_by"],
        )
        as_of = values.get("as_of")
        as_of_date = None if as_of is None else date.fromisoformat(as_of)
        ticket = self.search_launcher.start(
            authorization=authorization, spec_ref=values["spec_ref"], as_of=as_of_date,
        )
        parameters = build_discovery_parameters(
            plan, spec_ref=values["spec_ref"], company_ref=values["company_ref"],
            as_of=as_of_date or datetime.now(timezone.utc).date(),
        )
        dispatch = self.coverage_mission.record_discovery_dispatch(
            authorization=authorization,
            discovery_plan_ref=plan["id"],
            discovery_plan_hash=plan["content_hash"],
            spec_ref=values["spec_ref"],
            query_hash=search_spec_hash(parameters),
            ticket_ref=ticket["id"],
        )
        return {**ticket, "dispatch_ref": dispatch["dispatch_id"], "parameters": parameters}

    def _op_mission_source_discovery_status(self, p: Mapping[str, Any]) -> Any:
        return self.search_launcher.status(dict(p)["ticket_ref"])

    def _resolve_mission_version_ref(self, values: dict[str, Any]) -> str:
        ref = values.pop("mission_version_ref", None)
        if ref is not None:
            return ref
        if self._source_discovery is None:
            raise DiscoveryLaunchRejected("mission_version_ref is required without a discovery plan")
        return self.coverage_mission.active_mission(self._source_discovery.plan["mission_ref"])["id"]

    def _op_mission_source_discoveries(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        mission_version_ref = self._resolve_mission_version_ref(values)
        return {
            "projection_kind": "mission_source_discoveries",
            "mission_version_ref": mission_version_ref,
            "discoveries": self.coverage_mission.source_discoveries(mission_version_ref, **values),
            "dispatches": self.coverage_mission.discovery_dispatches(
                mission_version_ref, company_ref=values.get("company_ref"),
                spec_ref=values.get("spec_ref"), limit=values.get("limit", 100),
            ),
        }

    def _op_mission_discovered_documents(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        mission_version_ref = self._resolve_mission_version_ref(values)
        return {
            "projection_kind": "mission_discovered_documents",
            "mission_version_ref": mission_version_ref,
            "documents": self.coverage_mission.discovered_documents(mission_version_ref, **values),
        }

    def _op_mission_document_reviews(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        values.setdefault("state", None)
        values.setdefault("company_ref", None)
        mission_version_ref = self._resolve_mission_version_ref(values)
        return {
            "projection_kind": "mission_document_reviews",
            "mission_version_ref": mission_version_ref,
            "reviews": self.coverage_mission.document_reviews(mission_version_ref, **values),
        }

    def _op_resolve_mission_document_review(self, p: Mapping[str, Any]) -> Any:
        # Human-only close of an extraction review.  The staged candidate is
        # verified against the CandidateStaging authority (a separate database
        # shared with the Cockpit review plane) before the mission ledger
        # records the resolution.
        values = dict(p)
        if values["resolution"] == "extraction_staged":
            ref = values["candidate_claim_version_ref"]
            if self.candidate_staging is None:
                raise WriterServerError("candidate staging is not configured")
            try:
                status = self.candidate_review.candidate_status(ref)
            except ResearchReviewRejected as exc:
                raise NotFound(str(exc)) from exc
            if status.get("review_state") not in ("staged", "committed"):
                raise WriterServerError(
                    "candidate claim version is not in a staged review state"
                )
        return self.coverage_mission.resolve_document_review(
            values["review_id"],
            resolution=values["resolution"],
            actor_ref=values["actor_ref"],
            candidate_claim_version_ref=values.get("candidate_claim_version_ref"),
            rationale=values.get("rationale"),
        )

    def _op_bounded_alphaengine_probe(self, p: Mapping[str, Any]) -> Any:
        # Executes in the writer: the acquisition subprocess writes Core
        # connector authority, which the driver process must not open, and
        # the budget gate counts Core invocations before any call is spent.
        if self._acquisition_launcher is None:
            raise WriterServerError("alphaengine acquisition is not configured")
        return execute_alphaengine_probe(
            dict(p)["work_order"],
            launcher=self._acquisition_launcher,
            connection=self.store.connection,
        )

    def _op_bounded_planner_propose_next_with_context(self, p: Mapping[str, Any]) -> Any:
        return self.bounded_planner.propose_next_with_context(
            dict(p)["planner_context_pack_ref"]
        )

    def _op_bounded_planner_propose_next(self, p: Mapping[str, Any]) -> Any:
        return self.bounded_planner.propose_next_capital_lease(
            dict(p)["loop_version_ref"]
        )

    def _op_bounded_planner_admit_proposal(self, p: Mapping[str, Any]) -> Any:
        if self._bounded_control is None:
            raise WriterServerError("bounded-planner control plane is unavailable")
        return self._bounded_control.admit_proposal(dict(p)["proposal_ref"])

    def _op_bounded_planner_record_outcome(self, p: Mapping[str, Any]) -> Any:
        if self._bounded_control is None:
            raise WriterServerError("bounded-planner control plane is unavailable")
        return self._bounded_control.record_outcome(dict(p)["round_ref"])

    def _op_bounded_planner_record_observation(self, p: Mapping[str, Any]) -> Any:
        if self._bounded_control is None:
            raise WriterServerError("bounded-planner control plane is unavailable")
        values = dict(p)
        observation = self._bounded_control.record_observation_followup(
            values["round_ref"],
            mandate_version_ref=values["mandate_version_ref"],
        )
        if observation.get("status") != "recorded":
            return observation
        try:
            authorization = self.coverage_mission.sec_lane_authorization_for_company(
                observation["company_ref"]
            )
        except CoverageMissionError as exc:
            return {
                **observation,
                "lane_status": "not_authorized",
                "lane_reason": f"{type(exc).__name__}: {exc}",
            }
        location = observation["source_location"]
        prefix = "sec:accession:"
        raw_accession = location.removeprefix(prefix) if location.startswith(prefix) else ""
        if re.fullmatch(r"[0-9]{18}", raw_accession):
            accession = (
                f"{raw_accession[:10]}-{raw_accession[10:12]}-{raw_accession[12:]}"
            )
        elif re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", raw_accession):
            accession = raw_accession
        else:
            return {
                **observation,
                "lane_status": "not_authorized",
                "lane_reason": "observed source location is not an exact SEC accession",
            }
        queued = self.coverage_mission.queue_sec_dispatch(
            authorization=authorization,
            form=observation["form"],
            filed_from=observation["filed_from"],
            filed_to=observation["filed_to"],
            expected_accession=accession,
            observation_ref=observation["outcome_ref"],
        )
        dispatched = self._dispatch_one_coverage_mission_sec_lane()
        result = {
            **observation,
            "mission_version_ref": authorization["mission_version_ref"],
            "mission_version_hash": authorization["mission_version_hash"],
            "lane_status": dispatched["status"],
            "lane_dispatch_ref": queued["dispatch_id"],
            "expected_accession": accession,
            "paid_calls_reserved": authorization["paid_calls_reserved"],
            "cost_usd_reserved": authorization["cost_usd_reserved"],
        }
        if dispatched.get("ticket_ref") is not None:
            result["lane_ticket_ref"] = dispatched["ticket_ref"]
        return result

    def _dispatch_one_coverage_mission_sec_lane(self) -> dict[str, Any]:
        pending = self.coverage_mission.pending_sec_dispatches(limit=1)
        if not pending:
            return {"status": "idle"}
        dispatch = pending[0]
        authorization = dispatch["authorization"]
        try:
            exact = self.coverage_mission.authorize_sec_lane(
                company_ref=dispatch["company_ref"],
                ticker=dispatch["ticker"],
                actor_ref=dispatch["actor_ref"],
                mission_version_ref=dispatch["mission_version_ref"],
                mission_version_hash=dispatch["mission_version_hash"],
            )
            if exact != authorization:
                raise CoverageMissionConflict("queued SEC authorization drifted")
        except CoverageMissionError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.coverage_mission.mark_sec_dispatch_rejected(
                dispatch["dispatch_id"], reason
            )
            return {
                "status": "rejected", "dispatch_ref": dispatch["dispatch_id"],
                "reason": reason,
            }
        try:
            ticket = self.sec_lane_launcher.start(
                issuers=[dispatch["ticker"]],
                filed_from=dispatch["filed_from"],
                filed_to=dispatch["filed_to"],
                actor_ref=dispatch["actor_ref"],
                form=dispatch["form"],
                expected_accession=dispatch["expected_accession"],
                mission_context=exact,
            )
        except LaneLaunchConflict as exc:
            return {
                "status": "deferred", "dispatch_ref": dispatch["dispatch_id"],
                "reason": f"{type(exc).__name__}: {exc}",
            }
        except LaneLaunchRejected as exc:
            return {
                "status": "rejected", "dispatch_ref": dispatch["dispatch_id"],
                "reason": f"{type(exc).__name__}: {exc}",
            }
        self.coverage_mission.mark_sec_dispatch_launched(
            dispatch["dispatch_id"], ticket["id"]
        )
        return {
            "status": "launched", "dispatch_ref": dispatch["dispatch_id"],
            "ticket_ref": ticket["id"],
        }

    def _op_dispatch_coverage_mission_sec_lane(self, p: Mapping[str, Any]) -> Any:
        return self._dispatch_one_coverage_mission_sec_lane()

    def _op_bounded_planner_active_loops(self, p: Mapping[str, Any]) -> Any:
        return {
            "projection_kind": "bounded_planner_active_loops",
            "loops": [
                {"loop_version_ref": loop["id"], "loop_ref": loop["loop_ref"]}
                for loop in self.bounded_planner.active_loops()
            ],
        }

    def _op_propose_model_input(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.propose_input(**dict(p))

    def _op_get_model_input_candidate(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.candidate(**dict(p))

    def _op_get_model_input_decision(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.decision(**dict(p))

    def _op_get_model_input_version(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.input_version(**dict(p))

    def _op_current_model_input(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.current_input(**dict(p))

    def _op_decide_model_input(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.decide_input(**dict(p))

    def _op_record_model_run(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.record_model_run(**dict(p))

    def _op_record_model_reconciliation(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.record_reconciliation(**dict(p))

    def _op_get_model_reconciliations(self, p: Mapping[str, Any]) -> Any:
        return self.model_input.reconciliations(**dict(p))

    def _op_model_input_integrity_report(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("model_input_integrity_report takes no parameters")
        return self.model_input.integrity_report()

    @property
    def acquisition_launcher(self) -> AlphaEngineAcquisitionLauncher:
        if self._acquisition_launcher is None:
            raise AcquisitionLaunchRejected(
                "AlphaEngine acquisition launcher is not configured on this writer"
            )
        return self._acquisition_launcher

    def _op_acquire_alphaengine_document(self, p: Mapping[str, Any]) -> Any:
        # Runs out of process; see alphaengine_acquisition_launcher.  The
        # store thread only validates, checks governance and spawns.
        values = dict(p)
        return self.acquisition_launcher.start(
            document_ref=values["document_ref"],
            actor_ref=values["actor_ref"],
            expected_content_sha256=values.get("expected_content_sha256"),
            max_pages=values.get("max_pages", 20),
        )

    def _op_alphaengine_acquisition_status(self, p: Mapping[str, Any]) -> Any:
        return self.acquisition_launcher.status(dict(p)["ticket_ref"])

    @property
    def sec_lane_launcher(self) -> SecLaneLauncher:
        if self._sec_lane_launcher is None:
            raise LaneLaunchRejected(
                "SEC company-facts lane launcher is not configured on this writer"
            )
        return self._sec_lane_launcher

    def _op_run_sec_company_facts_lane(self, p: Mapping[str, Any]) -> Any:
        # Runs out of process; see sec_lane_launcher.  The store thread only
        # validates, checks the SEC governance record and spawns.  The lane's
        # Core writes (plans, candidates, policy-committed Claims) happen in
        # the child under the active governance policy, never here.
        values = dict(p)
        return self.sec_lane_launcher.start(
            issuers=values["issuers"],
            filed_from=values["filed_from"],
            filed_to=values["filed_to"],
            actor_ref=values["actor_ref"],
            form=values.get("form", "10-Q"),
        )

    def _op_sec_lane_status(self, p: Mapping[str, Any]) -> Any:
        return self.sec_lane_launcher.status(dict(p)["ticket_ref"])

    @property
    def candidate_staging(self) -> CandidateStagingStore:
        if self._candidate_staging is None:
            raise VerificationRejected(
                "candidate staging is not configured on this writer"
            )
        return self._candidate_staging

    @property
    def candidate_review(self) -> HumanReviewAuthority:
        if self._candidate_review is None:
            raise VerificationRejected(
                "candidate staging is not configured on this writer"
            )
        return self._candidate_review

    def _read_transcript_artifact(self, artifact: Mapping[str, Any]) -> bytes:
        if self._transcript_spool is None:
            raise VerificationRejected("transcript spool is not configured on this writer")
        return self._transcript_spool.read_object(artifact["artifact_content_hash"])

    def _op_stage_transcript_candidate(self, p: Mapping[str, Any]) -> Any:
        # Human-only (HUMAN_GOVERNANCE_OPERATIONS).  Reads the Core-held
        # AlphaEngine authority and writes only CandidateStaging; the
        # verification mode is fixed to ``transcript_core_authority`` by the
        # S7b entry point and no policy path is touched.  ADR-0003 option B.
        values = dict(p)
        staging = self.candidate_staging
        artifact_reader = (
            None if self._transcript_spool is None else self._read_transcript_artifact
        )
        return stage_transcript_qualitative_candidate(
            self.store,
            staging,
            correction_set_ref=values["correction_set_ref"],
            citation_ref=values["citation_ref"],
            subject_ref=values["subject_ref"],
            metric_or_aspect=values["metric_or_aspect"],
            period=values["period"],
            basis=values["basis"],
            normalized_statement=values["normalized_statement"],
            actor_ref=values["actor_ref"],
            idempotency_key=values["idempotency_key"],
            artifact_reader=artifact_reader,
        )

    def _op_transcript_candidate_status(self, p: Mapping[str, Any]) -> Any:
        ref = dict(p)["candidate_claim_ref"]
        try:
            return self.candidate_review.candidate_status(ref)
        except ResearchReviewRejected as exc:
            raise NotFound(str(exc)) from exc

    def _op_publish_transcript_correction_set(
        self, p: Mapping[str, Any]
    ) -> Any:
        values = dict(p)
        source_manifest = values.pop("source_manifest")
        authority, manifest = self._transcript_corrections(source_manifest)
        correction_set_ref = values.pop("correction_set_ref")
        return authority.publish(
            correction_set_ref,
            source_manifest_ref=manifest["id"],
            source_manifest_hash=manifest["content_hash"],
            source_content_hash=manifest["assembled_object"]["content_hash"],
            **values,
        )

    def _op_bind_transcript_claim_citation(
        self, p: Mapping[str, Any]
    ) -> Any:
        values = dict(p)
        source_manifest = values.pop("source_manifest")
        authority, _ = self._transcript_corrections(source_manifest)
        version_ref = values.pop("correction_set_version_ref")
        version_hash = values.pop("correction_set_version_hash")
        return authority.bind_claim_citation(
            version_ref, version_hash, **values
        )

    def _op_candidate_promotions(self, p: Mapping[str, Any]) -> Any:
        return self.store.candidate_promotions(**dict(p))

    def _op_transcript_correction_review_state(
        self, p: Mapping[str, Any]
    ) -> Any:
        values = dict(p)
        source_manifest = values.pop("source_manifest")
        authority, _ = self._transcript_corrections(source_manifest)
        correction_set_ref = values.pop("correction_set_ref")
        return authority.review_state(correction_set_ref, **values)

    def _op_register_industry_evidence_pack(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        evidence_pack_ref = values.pop("evidence_pack_ref")
        return self.industry_research.register_evidence_pack(evidence_pack_ref, **values)

    def _op_get_industry_evidence_pack(self, p: Mapping[str, Any]) -> Any:
        return self.industry_research.evidence_pack(**dict(p))

    def _op_register_company_overlay(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        overlay_ref = values.pop("overlay_ref")
        return self.industry_research.register_company_overlay(overlay_ref, **values)

    def _op_get_company_overlay(self, p: Mapping[str, Any]) -> Any:
        return self.industry_research.company_overlay(**dict(p))

    def _op_industry_brief_snapshot(self, p: Mapping[str, Any]) -> Any:
        return self.industry_research.industry_brief_snapshot(**dict(p))

    def _op_render_industry_brief_markdown(self, p: Mapping[str, Any]) -> Any:
        return self.industry_research.render_industry_brief_markdown(**dict(p))

    def _op_industry_research_integrity_report(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("industry_research_integrity_report takes no parameters")
        return self.industry_research.integrity_report()

    def _op_publish_weekly_brief(self, p: Mapping[str, Any]) -> Any:
        values = dict(p)
        brief_ref = values.pop("brief_ref")
        return self.weekly_brief.publish_issue(brief_ref, **values)

    def _op_get_weekly_brief_issue(self, p: Mapping[str, Any]) -> Any:
        return self.weekly_brief.issue(**dict(p))

    def _op_render_weekly_brief_markdown(self, p: Mapping[str, Any]) -> Any:
        return self.weekly_brief.render_markdown(**dict(p))

    def _op_record_weekly_brief_delivery(self, p: Mapping[str, Any]) -> Any:
        return self.weekly_brief.record_delivery(**dict(p))

    def _op_run_weekly_brief_cycle(self, p: Mapping[str, Any]) -> Any:
        return run_weekly_brief_cycle(
            self.store, self.weekly_brief, self.agenda, **dict(p)
        )

    def _op_record_scheduled_weekly_brief_delivery(
        self, p: Mapping[str, Any]
    ) -> Any:
        return self.weekly_brief.record_scheduled_delivery(**dict(p))

    def _op_record_weekly_brief_feedback(self, p: Mapping[str, Any]) -> Any:
        return self.weekly_brief.record_feedback(**dict(p))

    def _op_weekly_brief_feedback(self, p: Mapping[str, Any]) -> Any:
        return self.weekly_brief.feedback(**dict(p))

    def _op_weekly_brief_integrity_report(self, p: Mapping[str, Any]) -> Any:
        if p:
            raise ProtocolError("weekly_brief_integrity_report takes no parameters")
        return self.weekly_brief.integrity_report()

    def _op_thesis_impact_targets(self, p: Mapping[str, Any]) -> Any:
        if self._research_plan is None or self._backlog is None:
            raise WriterServerError("thesis-impact control plane is unavailable")
        mapping = p.get("company_thesis_refs")
        limit = p.get("limit", 100)
        if (
            not isinstance(mapping, Mapping)
            or any(
                not isinstance(company, str)
                or not company
                or not isinstance(thesis, str)
                or not thesis
                for company, thesis in mapping.items()
            )
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise WriterServerError("thesis-impact target request is invalid")
        targets: list[dict[str, Any]] = []
        for view in self._research_plan.plans():
            if view["state"] != "started" or view["start_binding"] is None:
                continue
            plan = view["plan_version"]
            question = self._backlog.question(plan["question_ref"])
            thesis_ref = mapping.get(question["head"]["company_ref"])
            if question["state"] != "answered" or thesis_ref is None:
                continue
            targets.append({
                "plan_version_ref": plan["id"],
                "plan_version_hash": plan["content_hash"],
                "company_ref": question["head"]["company_ref"],
                "thesis_ref": thesis_ref,
            })
            if len(targets) >= limit:
                break
        return targets

    def _op_thesis_impact_start(self, p: Mapping[str, Any]) -> Any:
        return self.thesis_impact_control.start_from_closed_plan(**dict(p))

    def _op_thesis_impact_advance_assessment(self, p: Mapping[str, Any]) -> Any:
        return self.thesis_impact_control.advance_assessment(**dict(p))

    def _op_thesis_impact_advance_verification(self, p: Mapping[str, Any]) -> Any:
        return self.thesis_impact_control.advance_verification(**dict(p))

    def _op_thesis_impact_assessment(self, p: Mapping[str, Any]) -> Any:
        return self.thesis_impact.assessment(**dict(p))

    def _op_thesis_impact_invocation(self, p: Mapping[str, Any]) -> Any:
        return self.thesis_impact.invocation(**dict(p))

    def _op_thesis_impact_find_invocation(self, p: Mapping[str, Any]) -> Any:
        return self.thesis_impact.find_invocation(**dict(p))

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "forbidden"
        if isinstance(exc, ProtocolError):
            return "protocol_error"
        if isinstance(exc, (ValidationError, BadVerdict, VerificationRequired, IndependenceViolation, GateRejected, AgendaValidationError, ObservabilityValidationError, ThesisImpactValidationError, ResearchPlanThesisImpactPending, CoverageAdmissionValidationError, ModelInputValidationError, IndustryResearchValidationError, WeeklyBriefValidationError, WeeklyBriefCoordinatorError, TranscriptCorrectionValidationError, BoundedPlannerValidationError, BoundedPlannerPending, ResearchQuestionValidationError, IntentDispatchValidationError, AnswerRoutingValidationError, ResearchConstitutionValidationError, ResearchPlaybookValidationError, CoverageMissionValidationError, CompanyResearchViewValidationError, ResearchDoctrineValidationError, LLMResearchPlannerValidationError, BoundedAlphaEngineProbeError, ModelForecastValidationError, ForecastReconciliationValidationError)):
            return "rejected"
        if isinstance(exc, (NotFound, AgendaNotFound, ObservabilityNotFound, ThesisImpactNotFound, ResearchPlanNotFound, CoverageAdmissionNotFound, ModelInputNotFound, IndustryResearchNotFound, WeeklyBriefNotFound, TranscriptCorrectionNotFound, BoundedPlannerNotFound, ResearchQuestionNotFound, IntentDispatchNotFound, AnswerRoutingNotFound, ResearchConstitutionNotFound, ResearchPlaybookNotFound, CoverageMissionNotFound, ResearchDoctrineNotFound, LLMResearchPlannerPending, ModelForecastNotFound, ForecastReconciliationNotFound)):
            return "not_found"
        if isinstance(exc, (IdempotencyConflict, InvocationConflict, AgendaConflict, ObservabilityConflict, ContextMaterializerConflict, ThesisImpactConflict, ResearchPlanConflict, ResearchPlanThesisImpactConflict, CoverageAdmissionConflict, ModelInputConflict, IndustryResearchConflict, WeeklyBriefConflict, TranscriptCorrectionConflict, BoundedPlannerConflict, ResearchQuestionConflict, IntentDispatchConflict, AnswerRoutingConflict, ResearchConstitutionConflict, ResearchPlaybookConflict, CoverageMissionConflict, ResearchDoctrineConflict, LLMResearchPlannerRejected, ModelForecastConflict, ForecastReconciliationConflict)):
            return "conflict"
        if isinstance(exc, (ContextMaterializerUnsupported, ContextMaterializerError, PerceptionError)):
            return "rejected"
        if isinstance(exc, (AcquisitionLaunchRejected, LaneLaunchRejected, DiscoveryLaunchRejected, DiscoveryPlanError)):
            return "rejected"
        if isinstance(exc, (AcquisitionLaunchConflict, LaneLaunchConflict, DiscoveryLaunchConflict)):
            return "conflict"
        if isinstance(exc, DiscoveryTicketNotFound):
            return "not_found"
        if isinstance(exc, DiscoveryLaunchError):
            return "store_error"
        if isinstance(exc, (ResearchVerificationConflict, ResearchReviewConflict)):
            return "conflict"
        if isinstance(exc, (ResearchVerificationError, ResearchReviewError)):
            return "rejected"
        if isinstance(exc, (AcquisitionTicketNotFound, LaneTicketNotFound)):
            return "not_found"
        if isinstance(exc, (AcquisitionLaunchError, LaneLaunchError)):
            return "store_error"
        if isinstance(exc, (CapabilityConflict,)):
            return "conflict"
        if isinstance(exc, (CapabilityNotFound,)):
            return "not_found"
        if isinstance(exc, (EvaluationRejected, PromotionRejected, PermissionEscalation)):
            return "rejected"
        if isinstance(exc, CapabilityRegistryError):
            return "store_error"
        if isinstance(exc, (DaltonStoreError, AgendaError, ObservabilityError, CoverageAdmissionError, ModelInputLedgerError, IndustryResearchError, WeeklyBriefError, TranscriptCorrectionError, BoundedPlannerError, ResearchQuestionError, IntentDispatchError, AnswerRoutingError, ResearchConstitutionError, ResearchPlaybookError, CoverageMissionError, CompanyResearchViewError, ResearchDoctrineError, LLMResearchPlannerError, ModelForecastError, ForecastReconciliationError)):
            return "store_error"
        return "internal_error"

    @staticmethod
    def _error_message(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "operation is not permitted"
        if isinstance(exc, ProtocolError):
            return "malformed request"
        if isinstance(exc, (ValidationError, BadVerdict, VerificationRequired, IndependenceViolation, GateRejected, AgendaValidationError, ObservabilityValidationError, ThesisImpactValidationError, ResearchPlanThesisImpactPending, CoverageAdmissionValidationError, ModelInputValidationError, IndustryResearchValidationError, WeeklyBriefValidationError, WeeklyBriefCoordinatorError, TranscriptCorrectionValidationError, BoundedPlannerValidationError, BoundedPlannerPending, ResearchQuestionValidationError, IntentDispatchValidationError, AnswerRoutingValidationError, ResearchConstitutionValidationError, ResearchPlaybookValidationError, CoverageMissionValidationError, CompanyResearchViewValidationError, ResearchDoctrineValidationError, LLMResearchPlannerValidationError, BoundedAlphaEngineProbeError, ModelForecastValidationError, ForecastReconciliationValidationError)):
            return "request rejected by contract or gate"
        if isinstance(exc, (NotFound, AgendaNotFound, ObservabilityNotFound, ThesisImpactNotFound, ResearchPlanNotFound, CoverageAdmissionNotFound, ModelInputNotFound, IndustryResearchNotFound, WeeklyBriefNotFound, TranscriptCorrectionNotFound, BoundedPlannerNotFound, ResearchQuestionNotFound, IntentDispatchNotFound, AnswerRoutingNotFound, ResearchConstitutionNotFound, ResearchPlaybookNotFound, CoverageMissionNotFound, ResearchDoctrineNotFound, LLMResearchPlannerPending, ModelForecastNotFound, ForecastReconciliationNotFound)):
            return "requested object was not found"
        if isinstance(exc, (IdempotencyConflict, InvocationConflict, AgendaConflict, ObservabilityConflict, ContextMaterializerConflict, ThesisImpactConflict, ResearchPlanConflict, ResearchPlanThesisImpactConflict, CoverageAdmissionConflict, ModelInputConflict, IndustryResearchConflict, WeeklyBriefConflict, TranscriptCorrectionConflict, BoundedPlannerConflict, ResearchQuestionConflict, IntentDispatchConflict, AnswerRoutingConflict, ResearchConstitutionConflict, ResearchPlaybookConflict, CoverageMissionConflict, ResearchDoctrineConflict, LLMResearchPlannerRejected, ModelForecastConflict, ForecastReconciliationConflict)):
            return "request conflicts with existing immutable data"
        if isinstance(exc, (ContextMaterializerError, PerceptionError)):
            return "request rejected by contract or gate"
        if isinstance(exc, (ResearchVerificationConflict, ResearchReviewConflict)):
            return "request conflicts with existing immutable data"
        if isinstance(exc, (ResearchVerificationError, ResearchReviewError)):
            return "request rejected by contract or gate"
        if isinstance(exc, CapabilityConflict):
            return "request conflicts with existing immutable capability data"
        if isinstance(exc, CapabilityNotFound):
            return "requested capability object was not found"
        if isinstance(exc, (EvaluationRejected, PromotionRejected, PermissionEscalation)):
            return "request rejected by capability governance"
        return "writer service failed to complete the request"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dalton Core owner-only writer service")
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--token-config", required=True)
    parser.add_argument("--scheduler")
    parser.add_argument("--transcript-spool-dir")
    parser.add_argument(
        "--candidate-staging",
        help="owner-only CandidateStaging sqlite shared with the Cockpit review plane "
             "(research_review.candidate_staging_path); enables stage_transcript_candidate",
    )
    parser.add_argument(
        "--connector-governance",
        help="approved AlphaEngine connector governance record; enables acquisition launches",
    )
    parser.add_argument(
        "--alphaengine-mcp-endpoint", default="http://127.0.0.1:8950/mcp",
        help="loopback AlphaEngine MCP endpoint used by networked acquisitions",
    )
    parser.add_argument(
        "--acquisition-rehearsal-document",
        help="rehearsal only: page a local file instead of the network (tests)",
    )
    parser.add_argument(
        "--acquisition-rehearsal-approved-by",
        help="rehearsal only: in-memory approved governance principal (tests)",
    )
    parser.add_argument(
        "--sec-lane-governance",
        help="approved SEC company-facts connector governance record; enables "
             "run_sec_company_facts_lane (requires --candidate-staging)",
    )
    parser.add_argument(
        "--sec-lane-user-agent",
        help="operator-visible SEC User-Agent passed to lane runs",
    )
    parser.add_argument(
        "--sec-lane-rehearsal-fixture",
        help="rehearsal only: company-facts fixture file served instead of data.sec.gov (tests)",
    )
    parser.add_argument(
        "--alphaengine-search-governance",
        help="approved AlphaEngine search_library governance record; enables mission "
             "source discovery launches (requires --alphaengine-discovery-plan)",
    )
    parser.add_argument(
        "--alphaengine-discovery-plan",
        help="hash-bound discovery plan manifest (deploy/phase9/*discovery-plan*.json); "
             "enables the mission source discovery coordinator",
    )
    parser.add_argument(
        "--search-rehearsal-results",
        help="rehearsal only: JSON array of search results served instead of the network (tests)",
    )
    parser.add_argument(
        "--search-rehearsal-approved-by",
        help="rehearsal only: in-memory approved search governance principal (tests)",
    )
    parser.add_argument("--planner-routing-policy")
    parser.add_argument("--planner-credential-slots")
    parser.add_argument("--planner-model-router-db")
    parser.add_argument("--planner-broker-socket")
    parser.add_argument("--planner-broker-auth-key")
    parser.add_argument("--planner-broker-client-id", default="client:dalton-core")
    parser.add_argument("--planner-expected-agent-id", default="chem")
    parser.add_argument(
        "--sec-lane-rehearsal-approved-by",
        help="rehearsal only: in-memory approved SEC governance principal (tests)",
    )
    args = parser.parse_args(argv)
    try:
        principals = load_principals(args.token_config)
        launcher = None
        sec_lane_launcher = None
        if args.sec_lane_governance is not None:
            if args.candidate_staging is None:
                raise WriterServerError(
                    "--sec-lane-governance requires --candidate-staging"
                )
            if args.sec_lane_rehearsal_fixture is not None:
                lane_mode_args: tuple[str, ...] = (
                    "--fixture-company-facts", args.sec_lane_rehearsal_fixture,
                )
                if args.sec_lane_rehearsal_approved_by is not None:
                    lane_mode_args += (
                        "--rehearsal-approved-by", args.sec_lane_rehearsal_approved_by,
                    )
            else:
                lane_mode_args = ("--allow-network",)
            sec_lane_launcher = SecLaneLauncher(
                state_dir=Path(args.db).expanduser().resolve().parent,
                governance_path=args.sec_lane_governance,
                staging_path=args.candidate_staging,
                mode_args=lane_mode_args,
                user_agent=args.sec_lane_user_agent,
            )
        if args.connector_governance is not None:
            if args.acquisition_rehearsal_document is not None:
                mode_args: tuple[str, ...] = (
                    "--fake-document-file", args.acquisition_rehearsal_document,
                )
                if args.acquisition_rehearsal_approved_by is not None:
                    mode_args += (
                        "--governance-approved-by", args.acquisition_rehearsal_approved_by,
                    )
            else:
                mode_args = ("--allow-network",)
            launcher = AlphaEngineAcquisitionLauncher(
                state_dir=Path(args.db).expanduser().resolve().parent,
                governance_path=args.connector_governance,
                mode_args=mode_args,
                mcp_endpoint=args.alphaengine_mcp_endpoint,
                # Same spool the writer reads through for
                # stage_transcript_candidate, so the acquired page bytes are
                # verifiable in place.
                spool_dir=args.transcript_spool_dir,
            )
        search_launcher = None
        if args.alphaengine_search_governance is not None:
            if args.alphaengine_discovery_plan is None:
                raise WriterServerError(
                    "--alphaengine-search-governance requires --alphaengine-discovery-plan"
                )
            if args.search_rehearsal_results is not None:
                search_mode_args: tuple[str, ...] = (
                    "--fake-search-file", args.search_rehearsal_results,
                )
                if args.search_rehearsal_approved_by is not None:
                    search_mode_args += (
                        "--governance-approved-by", args.search_rehearsal_approved_by,
                    )
            else:
                search_mode_args = ("--allow-network",)
            search_launcher = AlphaEngineSearchLauncher(
                state_dir=Path(args.db).expanduser().resolve().parent,
                governance_path=args.alphaengine_search_governance,
                plan_path=args.alphaengine_discovery_plan,
                mode_args=search_mode_args,
                mcp_endpoint=args.alphaengine_mcp_endpoint,
                spool_dir=args.transcript_spool_dir,
            )
        planner_model_config = None
        if args.planner_routing_policy is not None:
            slots = (args.planner_credential_slots or "").split(",")
            slots = tuple(item.strip() for item in slots if item.strip())
            if not slots or args.planner_model_router_db is None \
                    or args.planner_broker_socket is None \
                    or args.planner_broker_auth_key is None:
                raise WriterServerError(
                    "--planner-routing-policy requires credential slots, "
                    "router db and broker socket/auth key"
                )
            planner_model_config = {
                "routing_policy_ref": args.planner_routing_policy,
                "credential_slot_refs": slots,
                "model_router_db": args.planner_model_router_db,
                "broker_socket": args.planner_broker_socket,
                "broker_auth_key": args.planner_broker_auth_key,
                "broker_client_id": args.planner_broker_client_id,
                "expected_agent_id": args.planner_expected_agent_id,
            }
        server = WriterServer(
            args.db,
            args.socket,
            principals,
            token_config_path=args.token_config,
            scheduler_path=args.scheduler,
            transcript_spool_dir=args.transcript_spool_dir,
            acquisition_launcher=launcher,
            candidate_staging_path=args.candidate_staging,
            sec_lane_launcher=sec_lane_launcher,
            planner_model_config=planner_model_config,
            search_launcher=search_launcher,
            discovery_plan_path=args.alphaengine_discovery_plan,
        )
        server.start()
        def stop(_signum: int, _frame: Any) -> None:
            server.stop()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()
        server.stop()
        return 0
    except WriterServerError:
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())
