"""Whether a one-shot controlled re-entry claim ever produced a child run.

``LaneChildLauncher`` writes the one-shot marker *before* it launches the
child, which is the right order -- the marker is what stops two re-entries
racing -- but it means a re-entry refused between the claim and the launch
leaves a spent marker and no run at all.  Read as "already attempted", that
turns one refused attempt into a permanent hold.  Live on 2026-09-18, an hour
after the rebinding shipped, three admissions sat in ``recovery_required``
saying ``controlled reentry was already attempted`` having never once
re-entered: the claim had been taken by an attempt that died before the child
started.

So "claimed" and "attempted" are separated here.  A claim is only spent once
there is a launch record it can explain -- the prior ticket restarted at or
after the claim, or some ticket in the lane records having been rebound from
it.  Everything else is a claim that bought nothing, and the admission still
has its one automatic attempt.

The owner's own grant lives here too, for the same reason and in the same
place: once an attempt *has* really happened, nothing automatic may buy
another, so there has to be something a person can press.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "0.1"
GRANT_PREFIX = "controlled-reentry-grant-"
CLAIM_PREFIX = "controlled-reentry-"


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def marker(authorization: Any) -> str:
    """The launcher's own name for this authorization's one-shot marker."""

    return hashlib.sha256(str(authorization).encode("utf-8")).hexdigest()[:24]


def claim_path(launcher: Any, ticket_ref: str, authorization: Any) -> Path:
    return launcher._ticket_path(ticket_ref).with_name(
        f"{CLAIM_PREFIX}{marker(authorization)}.json")


def read_claim(launcher: Any, ticket_ref: str, authorization: Any
               ) -> dict[str, Any] | None:
    """The one-shot claim record for this authorization, if it was taken."""

    return _record(claim_path(launcher, ticket_ref, authorization))


def claim_consumed(launcher: Any, ticket_ref: str, authorization: Any) -> bool:
    """Did the claim for this authorization actually start a child?

    ``False`` for a claim that was taken and then refused before any process
    existed: that attempt bought nothing, so it may be completed rather than
    remembered forever as one that happened.
    """

    claim = read_claim(launcher, ticket_ref, authorization)
    if claim is None:
        return False
    claimed_at = _moment(claim.get("claimed_at"))
    if claimed_at is None:
        # A claim whose instant cannot be read is treated as spent.  The
        # conservative half of this question is the one that costs nothing:
        # the owner's grant is still there, and a person can still say go.
        return True
    prior = _record(launcher._ticket_path(ticket_ref))
    started = None if prior is None else _moment(prior.get("started_at"))
    if started is not None and started >= claimed_at:
        # The prior ticket itself was relaunched after the claim.
        return True
    for path in Path(launcher.tickets_dir).glob("*/ticket.json"):
        record = _record(path)
        if record is None or record.get("rebound_from_ticket_ref") != ticket_ref:
            continue
        rebound_at = _moment(record.get("started_at"))
        if rebound_at is None or rebound_at >= claimed_at:
            # A child was spawned onto the rebound identity.
            return True
    return False


def grant_path(launcher: Any, admission_ref: str) -> Path:
    digest = hashlib.sha256(str(admission_ref).encode("utf-8")).hexdigest()[:24]
    return Path(launcher.tickets_dir) / f"{GRANT_PREFIX}{digest}.json"


def write_grant(launcher: Any, admission_ref: str, *, actor_ref: str,
                granted_at: str) -> dict[str, Any]:
    """Record one owner grant of one further controlled re-entry.

    Idempotent for the same owner and instant, so a retried call does not
    stack grants; the grant itself is consumed by the next re-entry and then
    survives only as the record of who allowed it.
    """

    from .lane_child_launcher import write_owner_only

    path = grant_path(launcher, admission_ref)
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": "owner_authorized_controlled_reentry",
        "admission_ref": str(admission_ref),
        "actor_ref": str(actor_ref),
        "granted_at": str(granted_at),
        "max_reentries": 1,
    }
    existing = _record(path)
    if existing is not None and existing != body:
        raise ValueError("a different controlled re-entry grant is already pending")
    if existing is None:
        write_owner_only(path, body)
    return {"status": "granted", "grant_path": str(path), **body}


def consume_grant(launcher: Any, admission_ref: str, marker: str) -> dict[str, Any] | None:
    """Spend the owner's grant, atomically, and keep it as a used record."""

    path = grant_path(launcher, admission_ref)
    record = _record(path)
    if record is None:
        return None
    used = path.with_name(path.name[:-5] + f"-used-{marker}.json")
    try:
        os.rename(path, used)
    except OSError:
        return None
    return record


__all__ = [
    "CLAIM_PREFIX",
    "GRANT_PREFIX",
    "SCHEMA_VERSION",
    "claim_consumed",
    "claim_path",
    "consume_grant",
    "grant_path",
    "marker",
    "read_claim",
    "write_grant",
]
