"""Budget-gated AlphaEngine probe executor for Tier 1 bounded loops.

The owner capped automated AlphaEngine calls at 30 per trailing 24 hours.
Every acquisition attempt through this executor first counts the Core's
AlphaEngine connector invocations over the trailing window (each invocation
is one physical call, so a two-page document counts once per page request the
runner makes) and refuses — without spending a call — when the cap would be
exceeded.  Present documents answer from authority without any network.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


SCHEMA_VERSION = "0.1"
PROBE_PERMISSION_SCOPE = "alphaengine_read"
PROBE_OPERATION = "alphaengine_get_document"
ALPHAENGINE_PROFILE_REF = "connector-profile:alphaengine-get-document:v1"
# P9d-1: search calls draw on the same owner cap as document pages.  Every
# governed AlphaEngine profile is listed here; the count is the sum.
ALPHAENGINE_PROFILE_REFS: tuple[str, ...] = (
    ALPHAENGINE_PROFILE_REF,
    "connector-profile:alphaengine-search-library:v1",
)
TRAILING_WINDOW = timedelta(hours=24)
MAX_CALLS_PER_WINDOW = 30
_DOCUMENT_REF_RE = re.compile(r"^alphaengine-doc:[0-9]+$")


class BoundedAlphaEngineProbeError(RuntimeError):
    """The probe WorkOrder does not match the AlphaEngine probe contract."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def count_recent_alphaengine_calls(
    connection: Any, *, as_of: datetime | None = None
) -> int:
    now = as_of or _utcnow()
    window_start = (now - TRAILING_WINDOW).isoformat(timespec="microseconds")
    placeholders = ",".join("?" for _ in ALPHAENGINE_PROFILE_REFS)
    row = connection.execute(
        "SELECT COUNT(*) FROM connector_invocations "
        f"WHERE connector_profile_ref IN ({placeholders}) AND created_at >= ?",
        (*ALPHAENGINE_PROFILE_REFS, window_start),
    ).fetchone()
    return int(row[0])


def document_in_authority(connection: Any, document_ref: str) -> bool:
    """True when Core holds at least one *successful* page of the document.

    A call spec alone is not enough: the acquisition registers the call spec
    before the capability lease, so an attempt refused at ``prepare`` (for
    example ``StaleCatalog``) leaves a call spec behind with no page.  The
    document counts as present only through a complete or partial
    ``SourceEnvelope`` bound to a ``get_document`` invocation for that ref.
    """

    row = connection.execute(
        "SELECT 1 FROM connector_source_envelopes e "
        "JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref "
        "JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref "
        "WHERE c.operation='get_document' AND c.record_json LIKE ? "
        "AND e.status IN ('complete','partial') LIMIT 1",
        (f'%"{document_ref}"%',),
    ).fetchone()
    return row is not None


def execute_alphaengine_probe(
    work_order: dict[str, Any],
    *,
    launcher: Any,
    connection: Any,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 150.0,
    max_pages: int = 20,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Execute one AlphaEngine document probe; returns a ResultEnvelope wire."""

    metadata = work_order.get("metadata") or {}
    if metadata.get("permission_scope") != PROBE_PERMISSION_SCOPE:
        raise BoundedAlphaEngineProbeError(
            "probe WorkOrder is outside the AlphaEngine read scope"
        )
    if metadata.get("operation") != PROBE_OPERATION:
        raise BoundedAlphaEngineProbeError("probe operation is not Tier 1 executable")
    parameters = metadata.get("parameters") or {}
    locator = parameters.get("locator", "")
    if not isinstance(locator, str) or not locator.startswith("document/"):
        raise BoundedAlphaEngineProbeError("probe locator must be document/<ref>")
    document_ref = locator.removeprefix("document/")
    if _DOCUMENT_REF_RE.fullmatch(document_ref) is None:
        raise BoundedAlphaEngineProbeError("probe document ref is invalid")

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "id": (
            "result:alphaengine-probe:"
            + __import__("dalton_core.store", fromlist=["content_hash"])
            .content_hash({"document_ref": document_ref})[:32]
        ),
        "created_at": _utcnow().isoformat(timespec="microseconds"),
        "work_order_ref": work_order.get("id"),
        "invocation_ref": f"invocation:alphaengine-probe:{document_ref.split(':')[-1]}",
        "outputs": {},
        "actual_side_effects": ["read:alphaengine-mcp"],
        "usage_refs": [],
        "artifact_refs": [],
        "error": None,
        "metadata": {"probe": "alphaengine-get-document", "calls_spent": 0},
    }
    matches = [{"source_location": f"alphaengine:{document_ref}"}]
    if document_in_authority(connection, document_ref):
        envelope["status"] = "succeeded"
        envelope["outputs"] = {"matches": matches}
        return envelope

    spent = count_recent_alphaengine_calls(connection, as_of=as_of)
    if spent >= MAX_CALLS_PER_WINDOW:
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "ALPHAENGINE_PROBE_BUDGET_EXCEEDED",
            "message": (
                f"{spent} AlphaEngine calls in the trailing 24h window; "
                f"owner cap is {MAX_CALLS_PER_WINDOW}"
            ),
        }
        return envelope

    import time as _time

    ticket = launcher.start_bounded_probe(
        document_ref=document_ref,
        caller_ref="automation:bounded-planner",
        max_pages=max_pages,
    )
    deadline = _time.monotonic() + timeout_seconds
    record = ticket
    while _time.monotonic() < deadline:
        record = launcher.status(ticket["id"])
        if record.get("status") != "running":
            break
        _time.sleep(poll_seconds)
    envelope["metadata"]["calls_spent"] = 1
    if record.get("status") != "succeeded":
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "SOURCE_UNAVAILABLE",
            "message": f"acquisition ended {record.get('status')}",
        }
        return envelope
    if not document_in_authority(connection, document_ref):
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "SOURCE_UNAVAILABLE",
            "message": "acquisition succeeded but the document is not in authority",
        }
        return envelope
    envelope["status"] = "succeeded"
    envelope["outputs"] = {"matches": matches}
    return envelope


__all__ = [
    "ALPHAENGINE_PROFILE_REF",
    "ALPHAENGINE_PROFILE_REFS",
    "BoundedAlphaEngineProbeError",
    "MAX_CALLS_PER_WINDOW",
    "TRAILING_WINDOW",
    "count_recent_alphaengine_calls",
    "execute_alphaengine_probe",
]
