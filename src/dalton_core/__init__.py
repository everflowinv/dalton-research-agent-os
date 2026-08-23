"""Dalton Core prototype.

This package is intentionally isolated from the live Dalton Coverage OS.
"""

__version__ = "0.1.0.dev0"

from importlib import import_module

from .process_runtime import ProcessRuntimeAdapter


_LAZY_EXPORTS = {
    "Scheduler": (".scheduler", "Scheduler"),
    "CapabilityAttestation": (".capability_attestation", "CapabilityAttestation"),
    "TrustedLaunchContext": (".capability_attestation", "TrustedLaunchContext"),
    "UntrustedSandboxReport": (".capability_attestation", "UntrustedSandboxReport"),
    "validate_sandbox_report": (".capability_attestation", "validate_sandbox_report"),
    "CapabilityCatalog": (".capability_catalog", "CapabilityCatalog"),
    "OpenClawMetadataImporter": (
        ".openclaw_metadata", "OpenClawMetadataImporter"
    ),
    "OpenClawMetadataExporter": (
        ".openclaw_metadata_exporter", "OpenClawMetadataExporter"
    ),
    "PublicHttpTransport": (".public_http_transport", "PublicHttpTransport"),
    "CredentialGrantEnvelope": (
        ".credential_authority", "CredentialGrantEnvelope"
    ),
    "CredentialAuthorityStore": (
        ".credential_authority", "CredentialAuthorityStore"
    ),
    "McpManagedRunnerAdmissionGate": (
        ".mcp_managed_runner", "McpManagedRunnerAdmissionGate"
    ),
    "ModelRouter": (".model_router", "ModelRouter"),
    "ObservabilityStore": (".observability", "ObservabilityStore"),
    "CoverageAdmissionAuthority": (
        ".coverage_admission", "CoverageAdmissionAuthority"
    ),
    "CoverageAdmissionConflict": (
        ".coverage_admission", "CoverageAdmissionConflict"
    ),
    "CoverageAdmissionError": (
        ".coverage_admission", "CoverageAdmissionError"
    ),
    "CoverageAdmissionNotFound": (
        ".coverage_admission", "CoverageAdmissionNotFound"
    ),
    "CoverageAdmissionValidationError": (
        ".coverage_admission", "CoverageAdmissionValidationError"
    ),
    "ConnectorStore": (".connector", "ConnectorStore"),
    "ConnectorAuthorityResolver": (".authority_resolver", "ConnectorAuthorityResolver"),
    "ResolvedAuthority": (".authority_resolver", "ResolvedAuthority"),
    "AuthorityResolutionError": (".authority_resolver", "AuthorityResolutionError"),
    "AuthorityResolutionConflict": (".authority_resolver", "AuthorityResolutionConflict"),
    "validate_connector_proposal_manifest": (
        ".connector", "validate_connector_proposal_manifest"
    ),
    "ConnectorRunnerAdmissionGate": (
        ".connector_runner", "ConnectorRunnerAdmissionGate"
    ),
    "StaticAdapterResolver": (".connector_runner", "StaticAdapterResolver"),
    "DashboardProjector": (".dashboard_projector", "DashboardProjector"),
    "project_dashboard": (".dashboard_projector", "project_dashboard"),
    "DashboardQueryService": (".dashboard", "DashboardQueryService"),
    "ProjectionWriter": (".dashboard", "ProjectionWriter"),
    "OpenClawModelAdapter": (".openclaw_model_adapter", "OpenClawModelAdapter"),
    "install_openclaw_catalog": (".model_deployment", "install_openclaw_catalog"),
    "migrate_legacy_workspace": (".legacy_migration", "migrate_legacy_workspace"),
    "FixtureResearchCoordinator": (
        ".research_coordinator", "FixtureResearchCoordinator"
    ),
    "RecordedShadowFixturePort": (
        ".research_coordinator", "RecordedShadowFixturePort"
    ),
    "ResearchCoordinatorStore": (
        ".research_coordinator", "ResearchCoordinatorStore"
    ),
    "build_claim_index": (".research_context", "build_claim_index"),
    "build_context_pack": (".research_context", "build_context_pack"),
    "build_agenda_context_binding": (
        ".research_context", "build_agenda_context_binding"
    ),
    "validate_agenda_context_binding": (
        ".research_context", "validate_agenda_context_binding"
    ),
    "build_agenda_context": (".agenda_context", "build_agenda_context"),
    "build_fixture_runner_request": (
        ".research_context", "build_fixture_runner_request"
    ),
    "build_reference_fixture_plan": (
        ".research_context", "build_reference_fixture_plan"
    ),
    "SecPublicHttpAdapter": (".sec_public_adapter", "SecPublicHttpAdapter"),
    "SecCompanyConceptHttpAdapter": (
        ".sec_public_adapter", "SecCompanyConceptHttpAdapter"
    ),
    "SecPublicRouterAdapter": (".sec_public_adapter", "SecPublicRouterAdapter"),
    "normalize_sec_company_concept": (
        ".sec_public_adapter", "normalize_sec_company_concept"
    ),
    "normalize_sec_submissions": (".sec_public_adapter", "normalize_sec_submissions"),
    "HumanReviewAuthority": (".research_review", "HumanReviewAuthority"),
    "ResearchReviewControlPlane": (
        ".research_review_control", "ResearchReviewControlPlane"
    ),
    "DocumentIndex": (".document_index", "DocumentIndex"),
    "DocumentIndexInput": (".document_index", "DocumentIndexInput"),
    "make_document_index_input": (".document_index", "make_document_index_input"),
    "ContextMaterializer": (".context_materializer", "ContextMaterializer"),
    "ContextMaterialization": (".context_materializer", "ContextMaterialization"),
    "ContextMaterializerConflict": (".context_materializer", "ContextMaterializerConflict"),
    "ContextMaterializerError": (".context_materializer", "ContextMaterializerError"),
    "ContextMaterializerUnsupported": (".context_materializer", "ContextMaterializerUnsupported"),
    "validate_context_materialization": (".context_materializer", "validate_context_materialization"),
    "ResearchQuestionBacklog": (".research_question_backlog", "ResearchQuestionBacklog"),
    "ResearchPlanAuthority": (".research_plan", "ResearchPlanAuthority"),
    "ResearchPlanControlPlane": (".research_plan", "ResearchPlanControlPlane"),
    "ResearchPlanCoordinator": (".research_plan_coordinator", "ResearchPlanCoordinator"),
    "ResearchPlanCoordinatorConflict": (
        ".research_plan_coordinator", "ResearchPlanCoordinatorConflict"
    ),
    "ResearchPlanCoordinatorError": (
        ".research_plan_coordinator", "ResearchPlanCoordinatorError"
    ),
    "ResearchPlanExecutor": (
        ".research_plan_executor", "ResearchPlanExecutor"
    ),
    "ResearchPlanExecutorConflict": (
        ".research_plan_executor", "ResearchPlanExecutorConflict"
    ),
    "ResearchPlanExecutorError": (
        ".research_plan_executor", "ResearchPlanExecutorError"
    ),
    "ResearchPlanClosureCoordinator": (
        ".research_plan_closure", "ResearchPlanClosureCoordinator"
    ),
    "ResearchPlanClosureConflict": (
        ".research_plan_closure", "ResearchPlanClosureConflict"
    ),
    "ResearchPlanClosureError": (
        ".research_plan_closure", "ResearchPlanClosureError"
    ),
    "ResearchPlanClosurePending": (
        ".research_plan_closure", "ResearchPlanClosurePending"
    ),
    "ThesisImpactAuthority": (".thesis_impact", "ThesisImpactAuthority"),
    "ThesisImpactConflict": (".thesis_impact", "ThesisImpactConflict"),
    "ThesisImpactIneligible": (".thesis_impact", "ThesisImpactIneligible"),
    "validate_thesis_impact_model_output": (
        ".thesis_impact", "validate_thesis_impact_model_output"
    ),
    "validate_thesis_impact_verifier_output": (
        ".thesis_impact", "validate_thesis_impact_verifier_output"
    ),
    "validate_thesis_impact_verifier_consistency": (
        ".thesis_impact", "validate_thesis_impact_verifier_consistency"
    ),
    "load_frozen_calibration_corpus": (
        ".thesis_impact_calibration", "load_frozen_calibration_corpus"
    ),
    "build_thesis_impact_calibration_prompt": (
        ".thesis_impact_calibration", "build_calibration_prompt"
    ),
    "model_visible_calibration_case": (
        ".thesis_impact_calibration", "model_visible_case"
    ),
    "score_thesis_impact_verifier_outputs": (
        ".thesis_impact_calibration", "score_verifier_outputs"
    ),
    "ResearchPlanThesisImpactCoordinator": (
        ".thesis_impact_control", "ResearchPlanThesisImpactCoordinator"
    ),
    "ResearchPlanThesisImpactConflict": (
        ".thesis_impact_control", "ResearchPlanThesisImpactConflict"
    ),
    "ResearchPlanThesisImpactError": (
        ".thesis_impact_control", "ResearchPlanThesisImpactError"
    ),
    "ResearchPlanThesisImpactPending": (
        ".thesis_impact_control", "ResearchPlanThesisImpactPending"
    ),
    "ResearchPlanThesisImpactRuntime": (
        ".thesis_impact_model_worker", "ResearchPlanThesisImpactRuntime"
    ),
    "ThesisImpactModelWorker": (
        ".thesis_impact_model_worker", "ThesisImpactModelWorker"
    ),
    "ThesisImpactModelWorkerConflict": (
        ".thesis_impact_model_worker", "ThesisImpactModelWorkerConflict"
    ),
    "ThesisImpactModelWorkerError": (
        ".thesis_impact_model_worker", "ThesisImpactModelWorkerError"
    ),
    "ThesisImpactModelWorkerRejected": (
        ".thesis_impact_model_worker", "ThesisImpactModelWorkerRejected"
    ),
    "ResearchPlanConflict": (".research_plan", "ResearchPlanConflict"),
    "ResearchPlanError": (".research_plan", "ResearchPlanError"),
    "ResearchPlanNotFound": (".research_plan", "ResearchPlanNotFound"),
    "ResearchPlanValidationError": (".research_plan", "ResearchPlanValidationError"),
    "plan_version_ref_for": (".research_plan", "plan_version_ref_for"),
    "ResearchQuestionConflict": (".research_question_backlog", "ResearchQuestionConflict"),
    "ResearchQuestionError": (".research_question_backlog", "ResearchQuestionError"),
    "ResearchQuestionNotFound": (".research_question_backlog", "ResearchQuestionNotFound"),
    "ResearchQuestionValidationError": (
        ".research_question_backlog", "ResearchQuestionValidationError"
    ),
    "question_ref_for": (".research_question_backlog", "question_ref_for"),
    "question_identity": (".research_question_backlog", "question_identity"),
}


def __getattr__(name: str):
    """Load non-default control-plane APIs without bloating short workers."""

    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module = import_module(target[0], __name__)
        value = getattr(module, target[1])
        globals()[name] = value
        return value
    raise AttributeError(name)

__all__ = [
    "CapabilityAttestation",
    "CapabilityCatalog",
    "CredentialGrantEnvelope",
    "CredentialAuthorityStore",
    "FixtureResearchCoordinator",
    "ConnectorStore",
    "ConnectorAuthorityResolver",
    "ResolvedAuthority",
    "AuthorityResolutionError",
    "AuthorityResolutionConflict",
    "ConnectorRunnerAdmissionGate",
    "StaticAdapterResolver",
    "validate_connector_proposal_manifest",
    "DashboardProjector",
    "DashboardQueryService",
    "ModelRouter",
    "McpManagedRunnerAdmissionGate",
    "ObservabilityStore",
    "CoverageAdmissionAuthority",
    "CoverageAdmissionConflict",
    "CoverageAdmissionError",
    "CoverageAdmissionNotFound",
    "CoverageAdmissionValidationError",
    "OpenClawModelAdapter",
    "OpenClawMetadataImporter",
    "OpenClawMetadataExporter",
    "RecordedShadowFixturePort",
    "ResearchCoordinatorStore",
    "build_claim_index",
    "build_context_pack",
    "build_agenda_context_binding",
    "validate_agenda_context_binding",
    "build_agenda_context",
    "build_fixture_runner_request",
    "build_reference_fixture_plan",
    "SecPublicHttpAdapter",
    "SecCompanyConceptHttpAdapter",
    "SecPublicRouterAdapter",
    "normalize_sec_company_concept",
    "normalize_sec_submissions",
    "HumanReviewAuthority",
    "ResearchReviewControlPlane",
    "DocumentIndex",
    "DocumentIndexInput",
    "make_document_index_input",
    "ContextMaterializer",
    "ContextMaterialization",
    "ContextMaterializerConflict",
    "ContextMaterializerError",
    "ContextMaterializerUnsupported",
    "validate_context_materialization",
    "ResearchQuestionBacklog",
    "ResearchPlanAuthority",
    "ResearchPlanControlPlane",
    "ResearchPlanCoordinator",
    "ResearchPlanCoordinatorConflict",
    "ResearchPlanCoordinatorError",
    "ResearchPlanExecutor",
    "ResearchPlanExecutorConflict",
    "ResearchPlanExecutorError",
    "ResearchPlanClosureCoordinator",
    "ResearchPlanClosureConflict",
    "ResearchPlanClosureError",
    "ResearchPlanClosurePending",
    "ThesisImpactAuthority",
    "ThesisImpactConflict",
    "ThesisImpactIneligible",
    "validate_thesis_impact_model_output",
    "validate_thesis_impact_verifier_output",
    "validate_thesis_impact_verifier_consistency",
    "load_frozen_calibration_corpus",
    "build_thesis_impact_calibration_prompt",
    "model_visible_calibration_case",
    "score_thesis_impact_verifier_outputs",
    "ResearchPlanThesisImpactCoordinator",
    "ResearchPlanThesisImpactConflict",
    "ResearchPlanThesisImpactError",
    "ResearchPlanThesisImpactPending",
    "ResearchPlanThesisImpactRuntime",
    "ThesisImpactModelWorker",
    "ThesisImpactModelWorkerConflict",
    "ThesisImpactModelWorkerError",
    "ThesisImpactModelWorkerRejected",
    "ResearchPlanConflict",
    "ResearchPlanError",
    "ResearchPlanNotFound",
    "ResearchPlanValidationError",
    "plan_version_ref_for",
    "ResearchQuestionConflict",
    "ResearchQuestionError",
    "ResearchQuestionNotFound",
    "ResearchQuestionValidationError",
    "question_ref_for",
    "question_identity",
    "install_openclaw_catalog",
    "migrate_legacy_workspace",
    "ProcessRuntimeAdapter",
    "PublicHttpTransport",
    "ProjectionWriter",
    "Scheduler",
    "TrustedLaunchContext",
    "UntrustedSandboxReport",
    "validate_sandbox_report",
    "project_dashboard",
    "__version__",
]
