"""Pure construction and validation for an empty workspace's first goal draft."""
from __future__ import annotations
from typing import Any, Mapping
from .store import content_hash
from .workspace import WorkspacePaths

SCHEMA_VERSION = "workspace-initial-goal-draft-0.1"
REQUIRED_FIELDS = ("industry", "companies", "research_questions", "deliverables", "source_plan", "method_bindings", "autonomy", "budget")
_FIELDS = {"schema_version", "workspace_id", "workspace_manifest_hash", "title", "objective", "companies", "budget", "setup_state", "required_fields", "mission_published", "research_authorized", "content_hash"}

class WorkspaceOnboardingError(ValueError): pass

def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceOnboardingError(f"{name} must be non-empty text")
    return value.strip()

def initial_goal_draft(workspace: WorkspacePaths, *, title: str, objective: str) -> dict[str, Any]:
    """Return journal-ready user text without creating research authority."""
    if workspace.manifest_path is None:
        raise WorkspaceOnboardingError("workspace manifest path is required")
    body = {"schema_version": SCHEMA_VERSION, "workspace_id": workspace.workspace_id,
            "workspace_manifest_hash": workspace.content_hash, "title": _text(title, "title"),
            "objective": _text(objective, "objective"), "companies": [], "budget": None,
            "setup_state": "setup_required", "required_fields": list(REQUIRED_FIELDS),
            "mission_published": False, "research_authorized": False}
    return {**body, "content_hash": content_hash(body)}

def validate_initial_goal_draft(value: Mapping[str, Any], workspace: WorkspacePaths) -> dict[str, Any]:
    """Validate exact workspace identity and the authority-free closed shape."""
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise WorkspaceOnboardingError("initial goal draft has an invalid closed shape")
    wire = dict(value)
    if wire["schema_version"] != SCHEMA_VERSION:
        raise WorkspaceOnboardingError("initial goal draft schema is unsupported")
    if (workspace.manifest_path is None or wire["workspace_id"] != workspace.workspace_id
            or wire["workspace_manifest_hash"] != workspace.content_hash):
        raise WorkspaceOnboardingError("initial goal draft belongs to another workspace")
    wire["title"] = _text(wire["title"], "title"); wire["objective"] = _text(wire["objective"], "objective")
    if wire["companies"] != [] or wire["budget"] is not None:
        raise WorkspaceOnboardingError("initial goal draft cannot grant company scope or budget")
    if (wire["setup_state"] != "setup_required" or wire["required_fields"] != list(REQUIRED_FIELDS)
            or wire["mission_published"] is not False or wire["research_authorized"] is not False):
        raise WorkspaceOnboardingError("initial goal draft cannot claim research activation")
    expected = wire.pop("content_hash")
    if not isinstance(expected, str) or expected != content_hash(wire):
        raise WorkspaceOnboardingError("initial goal draft content hash differs")
    wire["content_hash"] = expected
    return wire
