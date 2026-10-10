"""The owner's governed close of one document-research admission (2026-09-30).

Admissions are append-only, and until now the only owner doors re-opened a
stage (authorize-reentry, -paid, -unproved).  A hold that no retry can fix --
the owner's door already used, a prompt larger than the admitted input budget,
a reply cap exhausted at a budget below the current one, a verifier reply the
retired 0.1 contract can only refuse again -- therefore sat in
``recovery_required`` for ever: legacy about 30, ws-7d about 10.

A withdrawal is one more immutable fact about one admission:

* written only by a person (``human:*``), once per admission, never edited;
* only for a hold the lane has escalated or made terminal, and only while no
  child run and no stage or recovery WorkOrder of it is running, and no
  recovery the owner has already bought for it is still waiting to run;
* afterwards ``resolve_for_execution`` and the executor refuse the admission,
  and the lane turns its hold into a terminal ``withdrawn_by_owner`` that no
  longer waits on anybody;
* the planner may admit the same inquiry again later: the fresh admission's
  identity names the withdrawal it supersedes, so it is new paid authority
  under the current budget, not the old one re-opened.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)

SCHEMA_VERSION = "0.1"
WITHDRAWN_REASON = "withdrawn_by_owner"
WITHDRAWAL_PREFIX = "mission-document-research-withdrawal:"
MAX_REASON_CHARS = 1000
# The hold reasons a withdrawal may close besides a terminal hold: the ones
# the lane has already escalated to a person (its own automatic attempt is
# spent), plus the stopped verdicts that predate the escalation names.  A
# timed wait, or a recovery the lane is still doing by itself, is not the
# owner's to close.
ESCALATED_HOLD_REASONS: tuple[str, ...] = (
    "contract_failed_after_automatic_retry",
    "contract_failed_after_owner_authorized_retry",
    "unproved_send_failed_after_automatic_retry",
    "provider_budget_exceeded_not_retried",
    "provider_budget_exceeded_after_corrected_retry",
    "provider_budget_exceeded_correction_refused",
    "reentry_failed_after_automatic_rebind",
    "recovery_hint_unverifiable",
    "paid_send_output_contract_failed",
    "send_state_unproved",
)


class MissionDocumentWithdrawalError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_withdrawals(connection: Any) -> dict[str, dict[str, Any]]:
    """Every withdrawal by admission ref, each re-hashed on read.

    A Core from before withdrawals existed has no table and no withdrawals.
    A row whose bytes do not re-hash is not silently skipped: it raises,
    because a withdrawal is what stops paid work and must not be forgeable
    into or out of existence.
    """

    try:
        rows = connection.execute(
            "SELECT withdrawal_id, admission_ref, admission_hash, actor_ref, reason, "
            "record_json, content_hash, created_at "
            "FROM mission_document_research_withdrawals ORDER BY created_at, withdrawal_id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        (withdrawal_id, admission_ref, admission_hash, actor_ref, reason,
         record_json, digest, created_at) = tuple(row)
        try:
            wire = json.loads(record_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionDocumentWithdrawalError("stored withdrawal is invalid") from exc
        body = dict(wire) if isinstance(wire, Mapping) else {}
        asserted = body.pop("content_hash", None)
        if (canonical_json(wire) != record_json or asserted != digest
                or asserted != content_hash(body)
                or wire.get("id") != withdrawal_id
                or wire.get("admission_ref") != admission_ref
                or wire.get("admission_hash") != admission_hash
                or wire.get("actor_ref") != actor_ref
                or wire.get("reason") != reason
                or wire.get("created_at") != created_at):
            raise MissionDocumentWithdrawalError("stored withdrawal authority drifted")
        result[admission_ref] = dict(wire)
    return result


def withdrawal_for(connection: Any, admission_ref: str) -> dict[str, Any] | None:
    return read_withdrawals(connection).get(admission_ref)


def withdrawable_hold(held: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Whether the lane's hold is one the owner may close, and why not."""

    if not isinstance(held, Mapping):
        return False, "not_held"
    disposition = held.get("disposition")
    reason = str(held.get("reason") or "")
    if reason == WITHDRAWN_REASON:
        return False, "already_withdrawn"
    if disposition == "terminal_hold":
        return True, "terminal"
    if disposition == "recovery_required" and any(
            name in reason for name in ESCALATED_HOLD_REASONS):
        return True, "escalated"
    # 2026-10-09: withdrawal is always one of the owner's doors on a hold that
    # waits on a person (``available_doors``), whatever its reason -- a
    # fresh-work cap, a lane error, a reason this release does not know.  The
    # other guards (nothing running, no pending grant or bought recovery)
    # still apply.
    if disposition == "recovery_required":
        return True, "held"
    if disposition == "recovery_wait":
        return False, "lane_is_waiting_on_a_timer"
    return False, "lane_still_owns_this_hold"


def running_work_orders(connection: Any, admission_ref: str,
                        *, now: datetime | None = None) -> list[str]:
    """Stage or recovery WorkOrders of this admission that hold a live lease.

    Every stage, recovery and rebound Work carries the admission ref in its
    metadata.  Its current state is the latest attempt event; ``leased`` with
    an unexpired lease is running.  Read-only, and fail-closed: an unreadable
    Scheduler is reported as running rather than as idle.
    """

    moment = (now or _now()).astimezone(timezone.utc)
    try:
        rows = connection.execute(
            "SELECT w.work_order_id AS work_order_id, "
            "(SELECT e.state FROM scheduler_attempt_events e "
            " WHERE e.work_order_id=w.work_order_id ORDER BY e.event_seq DESC LIMIT 1) "
            "AS state, "
            "(SELECT l.expires_at FROM scheduler_attempt_events e "
            " JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
            " WHERE e.work_order_id=w.work_order_id ORDER BY e.event_seq DESC LIMIT 1) "
            "AS expires_at "
            "FROM scheduler_work_orders w "
            "WHERE json_extract(w.work_order_json,"
            "'$.metadata.mission_document_research_admission_ref')=?",
            (admission_ref,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        return ["scheduler:unreadable"]
    running = []
    for work_order_id, state, expires_at in (tuple(row) for row in rows):
        if state != "leased":
            continue
        try:
            expiry = datetime.fromisoformat(str(expires_at))
        except ValueError:
            running.append(work_order_id)
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry > moment:
            running.append(work_order_id)
    return running


# 2026-09-30.  The owner doors that buy one fresh stage WorkOrder.  Each writes
# one row in the controlled-recovery authorization ledger; the lane dispatches
# the WorkOrder it grants on a later tick.  Until that WorkOrder has a formal
# result the owner's decision has not been exercised, and the lane's hold
# ledger still shows the failure the owner just paid to retry (live legacy
# 3013db85: authorized 13:35:04Z, recovery WorkOrder ``ready``, hold still
# ``contract_failed_after_automatic_retry``).  Withdrawing then would throw
# away a just-bought, not-yet-run recovery.
OWNER_RECOVERY_PREFIXES: tuple[str, ...] = (
    "mission-document-paid-recovery-authorization:",
    "mission-document-unproved-send-recovery-authorization:",
    "mission-document-nosend-recovery-authorization:",
)
# What an owner recovery has come to, from the Core alone.
RECOVERY_NOT_DISPATCHED = "not_dispatched"   # authorized, no recovery link yet
RECOVERY_PENDING = "pending"                 # dispatched, no terminal result yet
RECOVERY_FAILED = "failed"                   # ran, and its formal result failed
RECOVERY_SUCCEEDED = "succeeded"             # ran, and its formal result succeeded
RECOVERY_UNREADABLE = "unreadable"           # cannot tell: treated as pending
SETTLED_RECOVERY_STATES = frozenset({RECOVERY_FAILED, RECOVERY_SUCCEEDED})


def owner_recoveries(connection: Any, admission_ref: str) -> list[dict[str, Any]]:
    """Every owner-authorized recovery of this admission and what became of it.

    Read-only.  The recovery an authorization grants is found through the
    recovery link written for the same failed WorkOrder; the WorkOrder that
    actually runs is that link's recovery Work or, after a model-authority
    epoch change, a rebound Work of it.  The recovery counts as run only when
    one of those has a formal (terminal) Scheduler result.  Anything that
    cannot be read is reported ``unreadable``, which callers treat as still
    pending: the doubt keeps the owner's paid decision alive.
    """

    try:
        rows = connection.execute(
            "SELECT authorization_id, stage_ordinal, failed_work_order_ref, created_at "
            "FROM mission_document_research_controlled_recovery_authorizations "
            "WHERE admission_ref=? ORDER BY created_at, authorization_id",
            (admission_ref,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        return [{"authorization_ref": None, "admission_ref": admission_ref,
                 "stage_ordinal": None, "authorized_at": None,
                 "state": RECOVERY_UNREADABLE, "work_order_refs": []}]
    found: list[dict[str, Any]] = []
    for authorization_id, stage_ordinal, failed_ref, created_at in (
            tuple(row) for row in rows):
        if not str(authorization_id).startswith(OWNER_RECOVERY_PREFIXES):
            continue
        entry: dict[str, Any] = {
            "authorization_ref": authorization_id, "admission_ref": admission_ref,
            "stage_ordinal": stage_ordinal, "authorized_at": created_at,
            "failed_work_order_ref": failed_ref, "work_order_refs": [],
        }
        try:
            links = connection.execute(
                "SELECT recovery_link_id, recovery_work_order_ref "
                "FROM mission_document_research_recovery_links "
                "WHERE admission_ref=? AND stage_ordinal=? AND failed_work_order_ref=?",
                (admission_ref, stage_ordinal, failed_ref),
            ).fetchall()
            granted: list[str] = []
            for link_id, recovery_ref in (tuple(row) for row in links):
                granted.append(str(recovery_ref))
                granted.extend(str(row[0]) for row in connection.execute(
                    "SELECT rebound_work_order_ref FROM "
                    "mission_document_research_model_authority_epoch_rebinds "
                    "WHERE recovery_link_ref=? ORDER BY created_at", (link_id,),
                ).fetchall())
            entry["work_order_refs"] = granted
            if not granted:
                entry["state"] = RECOVERY_NOT_DISPATCHED
            else:
                terminal = {str(row[0]) for row in connection.execute(
                    "SELECT terminal_state FROM scheduler_formal_results "
                    "WHERE work_order_id IN (%s)" % ",".join("?" * len(granted)),
                    granted,
                ).fetchall()}
                if RECOVERY_SUCCEEDED in terminal:
                    entry["state"] = RECOVERY_SUCCEEDED
                elif RECOVERY_FAILED in terminal:
                    entry["state"] = RECOVERY_FAILED
                else:
                    entry["state"] = RECOVERY_PENDING
        except sqlite3.Error:
            entry["state"] = RECOVERY_UNREADABLE
        found.append(entry)
    return found


def pending_owner_recoveries(connection: Any, admission_ref: str) -> list[dict[str, Any]]:
    """The owner recoveries of this admission that have not run to a result."""

    return [item for item in owner_recoveries(connection, admission_ref)
            if item["state"] not in SETTLED_RECOVERY_STATES]


def pending_reentry_grant(tickets_dir: Any, admission_ref: str) -> dict[str, Any] | None:
    """The owner's re-entry grant for this admission, if the lane has not spent it.

    The re-entry door writes a grant file that the next re-entry renames to a
    ``-used-`` record; while it is still there the owner's decision is waiting
    on the lane, exactly like an owner authorization not yet dispatched.
    """

    from pathlib import Path
    from types import SimpleNamespace

    from .lane_reentry_claim import grant_path

    path = grant_path(SimpleNamespace(tickets_dir=Path(tickets_dir)), admission_ref)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"grant_path": str(path), "granted_at": None}
    if isinstance(record, Mapping) and record.get("admission_ref") not in (
            None, admission_ref):
        return None
    return {"grant_path": str(path),
            "granted_at": record.get("granted_at") if isinstance(record, Mapping) else None}


class MissionDocumentWithdrawalAuthority:
    """Append one owner withdrawal of one exact admission."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock=_now) -> None:
        if not isinstance(store, DaltonStore):
            raise TypeError("store must be DaltonStore")
        from pathlib import Path

        self.store = store
        self.connection = store.connection
        self.clock = clock
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_document_withdrawal_authorized")
        # The admissions schema file owns this table too; applying it is a
        # no-op on a Core that has it and creates only IF NOT EXISTS objects.
        self.connection.executescript(
            (Path(__file__).with_name("mission_document_research_schema.sql"))
            .read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("MissionDocumentWithdrawalAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cursor:
                yield cursor
        finally:
            self._authorized = False

    def _admission(self, admission_ref: str) -> dict[str, Any]:
        """The stored admission, re-hashed -- deliberately not re-resolved.

        A withdrawal is how an admission that can no longer be resolved (its
        plan drifted, its re-entry cannot be repaired) is closed, so it must
        not depend on resolving it.  It only has to be exactly that admission.
        """

        row = self.connection.execute(
            "SELECT admission_id, record_json, content_hash "
            "FROM mission_document_research_admissions WHERE admission_id=?",
            (admission_ref,),
        ).fetchone()
        if row is None:
            raise MissionDocumentWithdrawalError("mission document admission is unavailable")
        admission_id, record_json, digest = tuple(row)
        try:
            wire = json.loads(record_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionDocumentWithdrawalError("mission document admission is invalid") from exc
        body = dict(wire) if isinstance(wire, Mapping) else {}
        asserted = body.pop("content_hash", None)
        if (canonical_json(wire) != record_json or asserted != digest
                or asserted != content_hash(body) or wire.get("id") != admission_id):
            raise MissionDocumentWithdrawalError("mission document admission authority drifted")
        return dict(wire)

    def withdraw(self, admission_ref: Any, *, actor_ref: Any, reason: Any,
                 hold: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(actor_ref, str) or not actor_ref.startswith("human:") \
                or len(actor_ref) <= len("human:"):
            raise MissionDocumentWithdrawalError("a withdrawal needs a human actor")
        return self._write_withdrawal(admission_ref, actor_ref=actor_ref, reason=reason, hold=hold)

    def withdraw_expired(self, admission_ref: str, *, hold=None):
        """Close an exact, expired recovery verdict, without buying work."""
        from .mission_document_research_executor import read_mission_document_research_observations
        now = self.clock().astimezone(timezone.utc)
        self._admission(admission_ref)
        if running_work_orders(self.connection, admission_ref, now=now) or pending_owner_recoveries(self.connection, admission_ref):
            raise MissionDocumentWithdrawalError("expired admission still has running or owner-authorized work")
        observations = [row for row in read_mission_document_research_observations(self.connection)
            if row["admission_ref"] == admission_ref and row.get("outcome") == "recovery_required"
            and row.get("recovery", {}).get("reason") == "fresh_work_recovery_deadline_exceeded"
            and row.get("recovery", {}).get("status") == "stopped"]
        expired = []
        for row in observations:
            try:
                deadline = datetime.fromisoformat(row["recovery"]["deadline"])
            except (KeyError, TypeError, ValueError):
                continue
            if deadline.tzinfo is not None and deadline <= now:
                expired.append(row)
        if not expired:
            raise MissionDocumentWithdrawalError("no exact expired recovery verdict")
        proof = expired[-1]
        # UTC budget refusals have a bounded, separately validated reset door.
        if (proof["recovery"].get("proof") or {}).get("classification") == "atomic_day_budget_refusal":
            raise MissionDocumentWithdrawalError("daily-budget reset remains authoritative")
        return self._write_withdrawal(admission_ref,
            actor_ref="automation:document-research-deadline",
            reason="研究恢复期限已过，自动结束本次研究；新的材料可重新安排研究。",
            hold=hold, expiry_proof={"observation_ref": proof["id"], "observation_hash": proof["content_hash"],
                                    "deadline": proof["recovery"]["deadline"]})

    def _write_withdrawal(self, admission_ref, *, actor_ref, reason, hold=None, expiry_proof=None):
        if not isinstance(reason, str) or not reason.strip():
            raise MissionDocumentWithdrawalError("a withdrawal needs a reason")
        reason = reason.strip()
        if len(reason) > MAX_REASON_CHARS:
            raise MissionDocumentWithdrawalError("withdrawal reason is too long")
        if (not isinstance(admission_ref, str)
                or not admission_ref.startswith("mission-document-research-admission:")):
            raise MissionDocumentWithdrawalError(
                "admission_ref is not a mission document research admission")
        admission = self._admission(admission_ref)
        existing = withdrawal_for(self.connection, admission_ref)
        if existing is not None:
            # A retried call after a lost reply sees its own row again.
            return {"status": "already_withdrawn", **existing}
        created_at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        body = {
            "schema_version": SCHEMA_VERSION,
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "actor_ref": actor_ref,
            "reason": reason,
            "hold": (None if hold is None else {
                "disposition": hold.get("disposition"),
                "reason": hold.get("reason"),
                "ticket_ref": hold.get("ticket_ref"),
            }),
            "created_at": created_at,
        }
        if expiry_proof is not None:
            body["expiry_proof"] = expiry_proof
        wire = {**body, "id": WITHDRAWAL_PREFIX + content_hash(body)[:32]}
        wire["content_hash"] = content_hash(wire)
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO mission_document_research_withdrawals VALUES(?,?,?,?,?,?,?,?)",
                (wire["id"], wire["admission_ref"], wire["admission_hash"],
                 wire["actor_ref"], wire["reason"], canonical_json(wire),
                 wire["content_hash"], wire["created_at"]))
        return {"status": "withdrawn", **wire}


__all__ = [
    "ESCALATED_HOLD_REASONS", "MissionDocumentWithdrawalAuthority",
    "MissionDocumentWithdrawalError", "OWNER_RECOVERY_PREFIXES",
    "SETTLED_RECOVERY_STATES", "WITHDRAWN_REASON", "owner_recoveries",
    "pending_owner_recoveries", "pending_reentry_grant", "read_withdrawals",
    "running_work_orders", "withdrawable_hold", "withdrawal_for",
]
