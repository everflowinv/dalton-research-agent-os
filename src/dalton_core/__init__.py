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
    "ModelRouter": (".model_router", "ModelRouter"),
    "ObservabilityStore": (".observability", "ObservabilityStore"),
    "ConnectorStore": (".connector", "ConnectorStore"),
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
    "ConnectorStore",
    "ConnectorRunnerAdmissionGate",
    "StaticAdapterResolver",
    "validate_connector_proposal_manifest",
    "DashboardProjector",
    "DashboardQueryService",
    "ModelRouter",
    "ObservabilityStore",
    "OpenClawModelAdapter",
    "OpenClawMetadataImporter",
    "OpenClawMetadataExporter",
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
