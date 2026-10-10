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
SEARCH_PROFILE_REF = "connector-profile:alphaengine-search-library:v1"
TRAILING_WINDOW = timedelta(hours=24)
MAX_CALLS_PER_WINDOW = 30
#: Who the bounded planner's probes are launched as.  The acquisition ticket
#: records it; the Core's connector receipts do not, so the probe's own cap is
#: counted through the tickets (``count_recent_probe_calls``).
PROBE_CALLER_REF = "automation:bounded-planner"
_DOCUMENT_REF_RE = re.compile(r"^alphaengine-doc:[0-9]+$")


class BoundedAlphaEngineProbeError(RuntimeError):
    """The probe WorkOrder does not match the AlphaEngine probe contract."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def count_recent_alphaengine_calls(
    connection: Any, *, as_of: datetime | None = None,
    profile_refs: tuple[str, ...] = ALPHAENGINE_PROFILE_REFS,
) -> int:
    """Trailing-window AlphaEngine calls, all profiles unless narrowed."""

    now = as_of or _utcnow()
    window_start = (now - TRAILING_WINDOW).isoformat(timespec="microseconds")
    now_text = now.isoformat(timespec="microseconds")
    profile_refs = tuple(profile_refs)
    placeholders = ",".join("?" for _ in profile_refs)
    row = connection.execute(
        "SELECT ("
        "SELECT COUNT(*) FROM connector_physical_attempts a "
        "JOIN connector_invocations i ON i.connector_invocation_id=a.connector_invocation_ref "
        f"WHERE i.connector_profile_ref IN ({placeholders}) AND a.started_at >= ?"
        ") + ("
        "SELECT COUNT(*) FROM connector_quota_reservations r "
        "JOIN connector_invocations i ON i.connector_invocation_id=r.connector_invocation_ref "
        "LEFT JOIN connector_physical_attempts a ON a.reservation_ref=r.reservation_id "
        f"WHERE i.connector_profile_ref IN ({placeholders}) AND r.created_at >= ? "
        "AND a.physical_attempt_id IS NULL AND r.expires_at > ? AND r.window_ends_at > ? "
        "AND NOT EXISTS (SELECT 1 FROM connector_quota_settlements s "
        "WHERE s.reservation_ref=r.reservation_id)"
        ")",
        (*profile_refs, window_start,
         *profile_refs, window_start, now_text, now_text),
    ).fetchone()
    return int(row[0])


def recent_probe_documents(launcher: Any, *, as_of: datetime | None = None) -> set[str]:
    """Documents the bounded planner's probes launched in the trailing window."""

    tickets_dir = getattr(launcher, "tickets_dir", None)
    if tickets_dir is None:
        return set()
    from pathlib import Path
    import json

    now = as_of or _utcnow()
    floor = now - TRAILING_WINDOW
    documents: set[str] = set()
    for path in Path(tickets_dir).glob("*/ticket.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(
                str(record.get("started_at")).replace("Z", "+00:00"))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if (record.get("actor_ref") == PROBE_CALLER_REF and started >= floor
                and isinstance(record.get("document_ref"), str)):
            documents.add(record["document_ref"])
    return documents


def count_recent_probe_calls(
    connection: Any, launcher: Any, *, as_of: datetime | None = None,
) -> int:
    """Trailing-window AlphaEngine calls made by the bounded planner's probes.

    2026-10-07 (batch r): the probe cap used to be compared with *every*
    AlphaEngine call, so once discovery had run its searches and acquisitions
    the probe was blocked although it had made none.  Only the calls of the
    documents a probe launched count here; a probe still in flight counts as
    one call so a burst cannot overshoot.
    """

    documents = recent_probe_documents(launcher, as_of=as_of)
    if not documents:
        return 0
    now = as_of or _utcnow()
    window_start = (now - TRAILING_WINDOW).isoformat(timespec="microseconds")
    marks = ",".join("?" for _ in documents)
    row = connection.execute(
        "SELECT COUNT(*) FROM connector_physical_attempts a "
        "JOIN connector_invocations i ON i.connector_invocation_id=a.connector_invocation_ref "
        "JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref "
        "WHERE i.connector_profile_ref=? AND a.started_at >= ? "
        f"AND json_extract(c.record_json,'$.parameters.document_ref') IN ({marks})",
        (ALPHAENGINE_PROFILE_REF, window_start, *sorted(documents)),
    ).fetchone()
    return max(int(row[0]), len(documents))


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
        "WHERE c.operation='get_document' "
        "AND json_extract(c.record_json,'$.parameters.document_ref')=? "
        "AND e.status IN ('complete','partial') LIMIT 1",
        (document_ref,),
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
    max_calls_per_window: int = MAX_CALLS_PER_WINDOW,
    as_of: datetime | None = None,
    max_probe_calls_per_window: int | None = None,
) -> dict[str, Any]:
    """Execute one AlphaEngine document probe; returns a ResultEnvelope wire.

    ``max_calls_per_window`` bounds every AlphaEngine call (the mission's
    window, which searches and acquisitions share); ``max_probe_calls_per_window``
    bounds only the calls the probes themselves made.
    """

    for name, value in (("max_calls_per_window", max_calls_per_window),
                        ("max_probe_calls_per_window", max_probe_calls_per_window)):
        if value is None and name == "max_probe_calls_per_window":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BoundedAlphaEngineProbeError(
                f"{name} must be a non-negative integer"
            )

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
    if spent >= max_calls_per_window:
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "ALPHAENGINE_PROBE_BUDGET_EXCEEDED",
            "message": (
                f"{spent} AlphaEngine calls in the trailing 24h window; "
                f"the mission's AlphaEngine cap is {max_calls_per_window}"
            ),
        }
        return envelope
    if max_probe_calls_per_window is not None:
        probes = count_recent_probe_calls(connection, launcher, as_of=as_of)
        if probes >= max_probe_calls_per_window:
            envelope["status"] = "failed"
            envelope["error"] = {
                "code": "ALPHAENGINE_PROBE_BUDGET_EXCEEDED",
                "message": (
                    f"{probes} probe calls in the trailing 24h window; "
                    f"configured probe cap is {max_probe_calls_per_window}"
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
    "PROBE_CALLER_REF",
    "SEARCH_PROFILE_REF",
    "TRAILING_WINDOW",
    "count_recent_alphaengine_calls",
    "count_recent_probe_calls",
    "recent_probe_documents",
    "execute_alphaengine_probe",
]
