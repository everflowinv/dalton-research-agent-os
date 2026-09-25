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
SYSTEMIC_PREFIX = "controlled-reentry-systemic-"
# Child failures that are a condition of the *system*, not of this admission's
# work: the admission is bound to an authority version that has since rolled,
# so the run dies before it sends anything and no amount of owner judgement
# changes the outcome.  Live on 2026-09-18 six legacy admissions escalated to
# a person with exactly this: ``directed document research requires the active
# mission``, after a source-plan change rolled the mission version under them.
# An attempt that died this way bought nothing and asked nothing of anybody,
# so it may be completed once more after the condition is repaired -- once,
# because a condition that is still there on the second run is a real fault
# and does belong in front of a person.
#
# 2026-09-25: the same holds for a run that dies re-verifying the authority of
# work that already happened -- a completed stage, or a recovery link --
# against an envelope that has rolled since.  Live, all five owner grants
# consumed that morning bought exactly this and nothing else: ``model budget
# binding drifted`` (four) and ``historical recovery link authority drifted``
# (one), each raised before the run could send anything.  And for a finished
# staging stage renamed by a model-policy roll (``staging completion did not
# converge``): the run re-stages nothing new and calls no model.
SYSTEMIC_CHILD_FAILURES = (
    "requires the active mission",
    "is no longer executable",
    "model budget binding drifted",
    "historical recovery link authority drifted",
    "recovery mission binding drifted",
    "staging completion did not converge",
)
GRANT_REFUND_INFIX = "-refund-"


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


def _started_run(launcher: Any, ticket_ref: str, claimed_at: datetime) -> Path | None:
    """The ticket directory of the run this claim actually started, if any."""

    prior_path = Path(launcher._ticket_path(ticket_ref))
    prior = _record(prior_path)
    started = None if prior is None else _moment(prior.get("started_at"))
    if started is not None and started >= claimed_at:
        # The prior ticket itself was relaunched after the claim.
        return prior_path.parent
    for path in Path(launcher.tickets_dir).glob("*/ticket.json"):
        record = _record(path)
        if record is None or record.get("rebound_from_ticket_ref") != ticket_ref:
            continue
        rebound_at = _moment(record.get("started_at"))
        if rebound_at is None or rebound_at >= claimed_at:
            # A child was spawned onto the rebound identity.
            return path.parent
    return None


def systemic_failure(ticket_dir: Path | None) -> str | None:
    """The systemic condition this run died on, in the child's own words."""

    if ticket_dir is None:
        return None
    summary = _record(Path(ticket_dir) / "summary.json")
    if summary is None or summary.get("status") == "complete":
        return None
    error = summary.get("error")
    if not isinstance(error, str) or not error:
        return None
    return error if any(name in error for name in SYSTEMIC_CHILD_FAILURES) else None


def systemic_path(launcher: Any, ticket_ref: str, authorization: Any) -> Path:
    return launcher._ticket_path(ticket_ref).with_name(
        f"{SYSTEMIC_PREFIX}{marker(authorization)}.json")


def claim_consumed(launcher: Any, ticket_ref: str, authorization: Any) -> bool:
    """Did the claim for this authorization actually buy an attempt?

    ``False`` for a claim that was taken and then refused before any process
    existed: that attempt bought nothing, so it may be completed rather than
    remembered forever as one that happened.  ``False`` too -- exactly once,
    and only until ``record_systemic_completion`` writes it down -- for a run
    that started and died on a systemic condition (see
    ``SYSTEMIC_CHILD_FAILURES``): that run bought nothing either.
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
    started = _started_run(launcher, ticket_ref, claimed_at)
    if started is None:
        return False
    if (systemic_failure(started) is not None
            and not systemic_path(launcher, ticket_ref, authorization).is_file()):
        return False
    return True


def record_systemic_completion(launcher: Any, ticket_ref: str,
                               authorization: Any) -> str | None:
    """Spend the one completion a systemic failure is allowed, and say so.

    A no-op unless the claim really did start a run that died systemically,
    so the ordinary "claimed but never launched" completion stays free.
    """

    claim = read_claim(launcher, ticket_ref, authorization)
    claimed_at = None if claim is None else _moment(claim.get("claimed_at"))
    if claimed_at is None:
        return None
    reason = systemic_failure(_started_run(launcher, ticket_ref, claimed_at))
    if reason is None:
        return None
    path = systemic_path(launcher, ticket_ref, authorization)
    if path.is_file():
        return reason
    from .lane_child_launcher import write_owner_only

    write_owner_only(path, {
        "schema_version": SCHEMA_VERSION,
        "kind": "systemic_failure_completion",
        "prior_ticket_ref": str(ticket_ref),
        "marker": marker(authorization),
        "reason": reason,
        "completed_at": datetime.now(timezone.utc).isoformat(
            timespec="microseconds"),
    })
    return reason


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
    """Spend the owner's grant, atomically, and keep it as a used record.

    The used record also says when it was spent, which is what ties it to the
    one run it bought (see ``refund_systemic_grant``).
    """

    path = grant_path(launcher, admission_ref)
    record = _record(path)
    if record is None:
        return None
    used = path.with_name(path.name[:-5] + f"-used-{marker}.json")
    try:
        os.rename(path, used)
    except OSError:
        return None
    from .lane_child_launcher import write_owner_only

    try:
        write_owner_only(used, {
            **record, "consumed_at": datetime.now(timezone.utc).isoformat(
                timespec="microseconds"),
        })
    except OSError:
        pass  # the rename already spent it; the timestamp is only a hint
    return record


def _used_grants(launcher: Any, admission_ref: str) -> list[Path]:
    stem = grant_path(launcher, admission_ref).name[:-5]
    return [
        path for path in Path(launcher.tickets_dir).glob(f"{stem}-used-*.json")
        if len(path.name) == len(stem) + len("-used-") + 24 + len(".json")
    ]


def refund_systemic_grant(launcher: Any, admission_ref: str,
                          claim_ticket_ref: str) -> dict[str, Any] | None:
    """Give back the owner's last grant when all it bought was a systemic death.

    A grant is spent the moment the launcher starts the child, before the
    child can know whether the admission is executable at all.  When that
    child then dies re-verifying authority -- a condition of the system (see
    ``SYSTEMIC_CHILD_FAILURES``), raised before anything is sent -- the grant
    bought nothing and the owner's decision has not been exercised.  So the
    most recent used grant is honoured once more, exactly once: the refund is
    an exclusive marker beside it, and a second systemic death after the
    refund is a real fault that belongs in front of a person again.
    """

    used = []
    for path in _used_grants(launcher, admission_ref):
        record = _record(path)
        if record is None or record.get("admission_ref") != admission_ref:
            continue
        since = _moment(record.get("consumed_at")) or _moment(record.get("granted_at"))
        if since is not None:
            used.append((since, path, record))
    if not used:
        return None
    since, path, record = max(used, key=lambda item: item[0])
    refund = path.with_name(
        path.name[:-5].replace("-used-", GRANT_REFUND_INFIX, 1) + ".json")
    if refund.exists():
        return None
    run = _started_run(launcher, claim_ticket_ref, since)
    reason = systemic_failure(run)
    if reason is None:
        return None
    # The run must be the one this grant bought: nothing else that can buy a
    # run -- a fresh one-shot claim or a systemic completion -- may sit
    # between the grant being spent and the run starting.
    run_record = _record(Path(run) / "ticket.json") or {}
    started = _moment(run_record.get("started_at"))
    if started is None:
        return None
    for directory in {Path(run), Path(launcher._ticket_path(claim_ticket_ref)).parent}:
        for other in directory.glob(f"{CLAIM_PREFIX}*.json"):
            other_record = _record(other) or {}
            bought_at = (_moment(other_record.get("claimed_at"))
                         or _moment(other_record.get("completed_at")))
            if bought_at is not None and since < bought_at <= started:
                return None
    body = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "kind": "systemic_failure_grant_refund",
        "admission_ref": str(admission_ref),
        "used_grant": path.name,
        "claim_ticket_ref": str(claim_ticket_ref),
        "reason": reason,
        "refunded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(str(refund), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    try:
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


__all__ = [
    "CLAIM_PREFIX",
    "GRANT_PREFIX",
    "SCHEMA_VERSION",
    "SYSTEMIC_CHILD_FAILURES",
    "SYSTEMIC_PREFIX",
    "claim_consumed",
    "claim_path",
    "consume_grant",
    "grant_path",
    "marker",
    "read_claim",
    "record_systemic_completion",
    "refund_systemic_grant",
    "systemic_failure",
    "systemic_path",
    "write_grant",
]
