"""P12b: tag one company's untagged Claims on a tick, and go quiet when there are none.

Queueless, for the same reason the model-specification lane is: what needs
doing is a *derived* fact -- the latest version of every Claim, minus what the
index already covers -- so it is computed from the Ledger every tick rather
than written down and drained.  There is nothing to leave stuck, and no second
place where "still to do" is recorded and can go stale.

The resting state is silence.  Once the backlog is tagged the lane does nothing
until a Claim is committed or re-versioned, at which point exactly that claim
is tagged and the lane goes quiet again.  A quarter of ticks in between all say
"nothing to tag", which is the correct answer and costs one snapshot read.

Companies are taken in the mission's own universe order and the first with
pending claims wins the slot.  Deliberately not "the one with the most
pending": a company that has just been discovered would otherwise sit behind
whichever one accumulates news fastest, and the universe order is the order the
owner wrote down.
"""

from __future__ import annotations

from typing import Any, Callable

from .claim_index_tagging import pending_claims
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)

MAX_FAILURE_DETAIL_CHARS = 500
# One tick looks at this many pending claims per company before deciding.  The
# child re-derives its own batch; this bound is only so that a company with a
# thousand untagged claims does not make the tick's selection read all of them.
MAX_PENDING_SCANNED = 200
# Outcomes that say something about this moment rather than about this batch,
# so the batch is not held back for them.
TRANSIENT_STATUSES: frozenset[str] = frozenset({"busy", "model_unavailable"})


class MissionClaimIndexLaneCoordinator:
    """Launch and settle the Claim-index lane."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.mission = mission
        # The batch in flight, so the next tick can settle it.  A launcher
        # holds one process, not a history, and the ticket ref is the only
        # handle on the summary.
        self._open: str | None = None
        # Batches whose run failed, keyed by (company, batch digest), so a
        # doomed batch does not consume the slot every five minutes.  Held for
        # this process only: a restart is nearly always a deploy, which is the
        # most likely thing to have fixed whatever it was.
        self._failed: dict[str, str] = {}

    # -- settling ---------------------------------------------------------

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now; try again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            # From the ticket, not the summary: a child that died before
            # writing one still has to be attributable to the batch it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "batch_digest": ticket.get("batch_digest"),
            "index_status": summary.get("index_status"),
            "rule_tagged": summary.get("rule_tagged"),
            "model_tagged": summary.get("model_tagged"),
            "cost_micros": summary.get("cost_micros"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        """Close out the previous tick's child, if it has finished.

        Settling happens before launching rather than after, because a child
        inspected in the same breath it was spawned is always still running.
        """

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        # ``busy`` is "the scheduler had this request in flight" and
        # ``model_unavailable`` is "no route right now". Both are true of a
        # moment, not of a batch, and holding a batch back for them would park
        # work that the very next tick could do.
        index_status = settled.get("index_status")
        failed = (
            index_status not in TRANSIENT_STATUSES
            and (settled.get("status") != "succeeded"
                 or index_status in ("refused", "failed", "not_authorized"))
        )
        company_ref = settled.get("company_ref")
        digest = settled.get("batch_digest")
        if failed and company_ref and digest:
            self._failed[f"{company_ref}|{digest}"] = (
                settled.get("failure_reason")
                or f"last run: {settled.get('index_status') or settled.get('status')}"
            )
        return settled

    # -- the tick ---------------------------------------------------------

    def _choose(self, mission: Any) -> tuple[str | None, list[str]]:
        """The first company in universe order with untagged claims."""

        snapshot = self.store.claim_index_snapshot()
        for item in mission.get("universe") or []:
            company_ref = item.get("company_ref") if isinstance(item, dict) else None
            if not company_ref:
                continue
            pending = pending_claims(
                self.store, subject_refs=[company_ref],
                limit=MAX_PENDING_SCANNED, snapshot=snapshot,
            )
            if pending:
                return company_ref, [row["claim_version_ref"] for row in pending]
        return None, []

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        try:
            company_ref, refs = self._choose(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if company_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every claim is indexed"}
        from .claim_index_launcher import batch_digest

        digest = batch_digest(company_ref, refs)
        held = self._failed.get(f"{company_ref}|{digest}")
        if held is not None:
            return {"status": "held", "company_ref": company_ref,
                    "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(
                company_ref=company_ref, claim_version_refs=refs
            )
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "batch_digest": digest, "pending": len(refs),
            "ticket_ref": ticket["id"], "settled": settled,
        }


__all__ = [
    "MAX_FAILURE_DETAIL_CHARS",
    "MAX_PENDING_SCANNED",
    "TRANSIENT_STATUSES",
    "MissionClaimIndexLaneCoordinator",
]
