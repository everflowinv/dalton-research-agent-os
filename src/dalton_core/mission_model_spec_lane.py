"""P13am: decide one company's model on a tick, and stop when there is nothing left.

The specification lane is unlike the acquisition lanes in one way that shapes
this whole module: it has no queue. What needs deciding is a *derived* fact --
a company that has filed statements and has no specification for the structure
those filings disclose -- so it is computed from the ledger every tick rather
than written down and drained. There is nothing to leave stuck.

That also means the natural resting state is silence. Five companies get five
specifications and then the lane does nothing until one of them files something
new, at which point the structure hash moves and exactly that company is
decided about again. A quarter's worth of ticks in between are all "nothing to
decide", which is the correct answer and costs a couple of database reads.

One child at a time, and the ticket is named by the company and the disclosure
together, so a tick that fires while a child is running does not start a second
one for the same judgement.
"""

from __future__ import annotations

from typing import Any, Callable

from .company_model_cli import choose_company
from .company_model_state import CompanyModelStateError, build_company_model_state
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)

MAX_FAILURE_DETAIL_CHARS = 500


class MissionModelSpecLaneCoordinator:
    """Launch and settle the company-model-specification lane."""

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.missions = missions
        self.launcher = launcher
        self.mission = mission
        # The judgement in flight, so the next tick can settle it. A launcher
        # cannot be asked "what did you last run" -- it holds one process, not
        # a history -- and the ticket ref is the only handle on the summary.
        self._open: str | None = None
        # Judgements whose run failed, keyed by (company, disclosure), so a
        # doomed company does not consume the slot every five minutes. Held for
        # this process only: a restart is nearly always a deploy, which is the
        # most likely thing to have fixed whatever it was.
        self._failed: dict[str, str] = {}

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
            # writing one still has to be attributable to the judgement it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "state_hash": ticket.get("state_hash"),
            "spec_status": summary.get("spec_status"),
            "spec_ref": summary.get("spec_ref"),
            "cost_micros": summary.get("cost_micros"),
            "revenue_drivers": summary.get("revenue_drivers"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        """Close out the previous tick's child, if it has finished.

        Settling happens here rather than after ``start`` for the obvious
        reason: a child inspected in the same breath it was spawned is always
        still running, and a lane that only ever looks at its own newborn
        never learns anything.
        """

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        failed = settled.get("status") != "succeeded" or settled.get("spec_status") in (
            "refused", "model_unavailable", "busy", "failed",
        )
        company_ref = settled.get("company_ref")
        state_hash = settled.get("state_hash")
        if failed and company_ref and state_hash:
            self._failed[f"{company_ref}|{state_hash}"] = (
                settled.get("failure_reason")
                or f"last run: {settled.get('spec_status') or settled.get('status')}"
            )
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        try:
            company_ref, _ = choose_company(self.missions, mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if company_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every company has a current specification"}
        try:
            state = build_company_model_state(self.missions, company_ref)
        except CompanyModelStateError as exc:
            return {"status": "idle", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        state_hash = state["state_hash"]
        held = self._failed.get(f"{company_ref}|{state_hash}")
        if held is not None:
            return {"status": "held", "company_ref": company_ref,
                    "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(company_ref=company_ref, state_hash=state_hash)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "state_hash": state_hash, "ticket_ref": ticket["id"],
            "settled": settled,
        }


__all__ = ["MAX_FAILURE_DETAIL_CHARS", "MissionModelSpecLaneCoordinator"]
