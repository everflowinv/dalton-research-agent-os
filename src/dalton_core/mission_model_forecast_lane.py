"""P13-M2: build one company's driver model on a tick, and stop when nothing has moved.

Queueless, like the specification lane and for the same reason: what needs
doing is *derived* from the ledger every tick rather than written down and
drained. There is nothing to get stuck.

Two things need doing, and only two. A company with a specification and no
driver model gets its first one. A company whose model estimated a quarter the
filings now cover gets those estimates answered, with the future left alone.
Nothing here re-forecasts because a document arrived: what a filing or an event
*means* for the quarters ahead is a judgement, it carries a decision word, and
it reaches the model as an explicit revision from whoever made it.

Silence is therefore the resting state and it is cheap. Five companies get five
models and then this lane does nothing until one of them reports.

One child at a time, settled on the following tick. A child inspected in the
same breath it was spawned is always still running, and a lane that only ever
looks at its own newborn never learns anything.
"""

from __future__ import annotations

from typing import Any, Callable

from .company_model_forecast import model_digest
from .company_model_forecast_cli import pending_companies
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)

MAX_FAILURE_DETAIL_CHARS = 500


class MissionModelForecastLaneCoordinator:
    """Launch and settle the driver-model lane."""

    def __init__(
        self,
        *,
        missions: Any,
        models: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.missions = missions
        self.models = models
        self.launcher = launcher
        self.mission = mission
        self._open: str | None = None
        # Runs that failed, keyed by (company, digest), so a company whose
        # model cannot be built does not consume the slot every tick. Held for
        # this process only: a restart is nearly always a deploy, which is the
        # most likely thing to have fixed it.
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
            # writing one still has to be attributable to the run it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "model_digest": ticket.get("model_digest"),
            "forecast_status": summary.get("forecast_status"),
            "model_version_ref": summary.get("model_version_ref"),
            "forecast_lines_written": summary.get("forecast_lines_written"),
            "results_computed": summary.get("results_computed"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        status = str(settled.get("forecast_status") or "")
        failed = settled.get("status") != "succeeded" or status.startswith("refused:")
        company_ref = settled.get("company_ref")
        digest = settled.get("model_digest")
        if failed and company_ref and digest:
            self._failed[f"{company_ref}|{digest}"] = (
                settled.get("failure_reason")
                or f"last run: {status or settled.get('status')}"
            )
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            pending = pending_companies(self.missions, self.models, mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if not pending:
            return {"status": "idle", "settled": settled,
                    "reason": "every company has a current driver model"}
        # The digest comes from the table the chooser already built, for the
        # reason the specification lane found the hard way: two projections of
        # the same company hash differently if they were built with different
        # arguments, and a selector that disagrees with the run about the hash
        # names a different ticket every time.
        #
        # And a company being held is skipped rather than reported, because one
        # company whose model cannot be built must not stand in front of the
        # other four. IBM's specification binds no filed revenue concept at
        # all; without this the lane would hand IBM back every tick forever.
        held: dict[str, str] = {}
        company_ref = spec = table = digest = None
        for candidate, candidate_spec, candidate_table in pending:
            candidate_digest = model_digest(candidate_spec, candidate_table)
            reason = self._failed.get(f"{candidate}|{candidate_digest}")
            if reason is not None:
                held[candidate] = reason
                continue
            company_ref, spec, table = candidate, candidate_spec, candidate_table
            digest = candidate_digest
            break
        if company_ref is None:
            return {"status": "held", "settled": settled, "held": held,
                    "reason": "; ".join(f"{ref}: {why}" for ref, why in held.items())}
        try:
            ticket = self.launcher.start(company_ref=company_ref, model_digest=digest)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "model_digest": digest, "ticket_ref": ticket["id"],
            "held": held, "settled": settled,
        }


__all__ = ["MAX_FAILURE_DETAIL_CHARS", "MissionModelForecastLaneCoordinator"]
