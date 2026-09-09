"""P13ak: run the financial-statements lane on a tick.

The connector, its governance and its child all worked by hand; nothing
dispatched them. This is the part that makes them run: for each company the
checklist covers, queue one statements run, launch one at a time, and when a
child finishes, read its observation into the ledger.

Three rules, each of which is a bug this codebase has already paid for:

* One child at a time, and a company with a dispatch in flight is not queued
  again. Queueing faster than the lane drains is what left thirty-five SEC
  dispatches open and froze that lane for a day.
* Record before settling. If the process dies between the two, the dispatch is
  still ``launched`` and the next tick records the same observation again --
  which is a no-op, because a filing is ingested once per company.
* A company whose runs keep failing stops being retried. Without a budget, a
  company the parser cannot handle consumes the single slot forever and every
  other company starves behind it.

How deep to go -- how many filings back -- is a parameter rather than a
constant on purpose. One quarter is the floor that keeps the lane moving; how
much history a company actually needs is a judgement the planner makes, and
this is where that judgement will attach.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .coverage_mission import CoverageMissionError, MAX_STATEMENT_FILINGS
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)

MAX_QUEUED_PER_RUN = 4
# Three failed or rejected runs is a company this lane cannot serve today.
MAX_FAILURES_PER_COMPANY = 3
# A run that never reached SEC failed because of how this Core is set up, not
# because of the company. The first live tick proved why this distinction is
# needed: every child died on a malformed EDGAR identity, and counted the way
# an ordinary failure counts, three ticks would have exhausted all five
# companies and left the lane permanently idle once the identity was fixed.
CONFIGURATION_FAILURE_MARKERS = (
    "SECIdentityError",
    "governance record is not approved",
    "governance source hash differs",
    "governance schema hash differs",
    "governance record covers a different capability",
    "parser is not installed",
    "LaneChildRejected",
)
# While a configuration is broken it is broken for every company, so the lane
# holds instead of asking SEC the same doomed question once a tick.
CONFIGURATION_HOLD_SECONDS = 1800
DEFAULT_FORM = "10-Q"
DEFAULT_FILING_LIMIT = 1
MAX_FAILURE_DETAIL_CHARS = 500


def _is_configuration_failure(reason: Any) -> bool:
    """Did this run fail before it ever reached the source?"""

    if not isinstance(reason, str):
        return False
    return any(marker in reason for marker in CONFIGURATION_FAILURE_MARKERS)


def _failure_reason(summary: Any) -> str | None:
    """The child's own account of why it failed, if it left one."""

    if not isinstance(summary, Mapping):
        return None
    reason = summary.get("failure_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip()[:MAX_FAILURE_DETAIL_CHARS]


class MissionStatementLaneCoordinator:
    """Queue, launch and settle the statements lane, one child per tick."""

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        checklist: Callable[[], Sequence[Mapping[str, Any]]],
        form: str = DEFAULT_FORM,
        filing_limit: int = DEFAULT_FILING_LIMIT,
        actor_ref: str = "automation:coverage-mission",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.missions = missions
        self.launcher = launcher
        self.checklist = checklist
        self.form = form
        if not 1 <= int(filing_limit) <= MAX_STATEMENT_FILINGS:
            raise ValueError(f"filing_limit must be 1..{MAX_STATEMENT_FILINGS}")
        self.filing_limit = int(filing_limit)
        self.actor_ref = actor_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- configuration failures --------------------------------------------

    def _seconds_since(self, when: Any) -> float | None:
        if not isinstance(when, str) or not when:
            return None
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (self.clock() - moment).total_seconds()

    def _retry_salt(self) -> str:
        """A value that moves with the clock, not with the company.

        One retry of a broken configuration per hold window: enough to notice
        that it was fixed, not enough to keep asking SEC a doomed question.
        """

        window = int(self.clock().timestamp()) // CONFIGURATION_HOLD_SECONDS
        return f"configuration-retry:{window}"

    # -- settling ----------------------------------------------------------

    def _settle_finished(self) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        try:
            open_dispatches = self.missions.launched_statement_dispatches(limit=50)
        except Exception:  # noqa: BLE001 - never break the tick over bookkeeping
            return settled
        for dispatch in open_dispatches:
            ticket_ref = dispatch.get("ticket_ref")
            if not ticket_ref:
                settled.append(self._settle(dispatch, "failed",
                                            "dispatch carries no lane ticket"))
                continue
            try:
                ticket = self.launcher.status(ticket_ref)
            except LaneChildTicketNotFound:
                settled.append(self._settle(dispatch, "failed",
                                            "lane ticket is no longer on disk"))
                continue
            except Exception:  # noqa: BLE001 - unreadable now; try next tick
                continue
            status = ticket.get("status")
            if status == "running":
                continue
            if status != "succeeded":
                settled.append(self._settle(
                    dispatch, "failed",
                    _failure_reason(ticket.get("summary")) or f"lane run {status}"))
                continue
            summary = ticket.get("summary")
            observation = summary.get("observation") if isinstance(summary, Mapping) else None
            if not isinstance(observation, Mapping):
                settled.append(self._settle(
                    dispatch, "failed",
                    "lane run succeeded without an observation to record"))
                continue
            # Record first: a crash between here and the settle leaves the
            # dispatch launched, and recording the same filing twice is a
            # no-op by construction.
            try:
                recorded = self.missions.record_statement_observation(
                    dispatch_id=dispatch["dispatch_id"], observation=observation,
                    governance_ref=str(summary.get("governance_ref") or ""),
                    governance_hash=str(summary.get("governance_hash") or ""),
                )
            except CoverageMissionError as exc:
                settled.append(self._settle(
                    dispatch, "failed", f"{type(exc).__name__}: {exc}"))
                continue
            outcome = self._settle(dispatch, "succeeded", None)
            outcome["line_count"] = recorded["line_count"]
            outcome["accessions"] = [item["accession"] for item in recorded["filings"]]
            settled.append(outcome)
        return settled

    def _settle(self, dispatch: Mapping[str, Any], outcome: str,
                reason: str | None) -> dict[str, Any]:
        result = {
            "dispatch_ref": dispatch["dispatch_id"],
            "company_ref": dispatch["company_ref"],
            "outcome": outcome, "failure_reason": reason,
        }
        try:
            self.missions.settle_statement_dispatch(
                dispatch["dispatch_id"], outcome=outcome, failure_reason=reason)
        except CoverageMissionError as exc:
            result["outcome"] = "unsettled"
            result["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return result

    # -- queueing ----------------------------------------------------------

    def _queue(self) -> list[dict[str, Any]]:
        queued: list[dict[str, Any]] = []
        try:
            companies = list(self.checklist())
        except Exception:  # noqa: BLE001
            return queued
        for company in companies:
            if len(queued) >= MAX_QUEUED_PER_RUN:
                break
            company_ref = company.get("company_ref") if isinstance(company, Mapping) else None
            ticker = company.get("ticker") if isinstance(company, Mapping) else None
            if not company_ref or not ticker:
                continue
            try:
                coverage = self.missions.statement_coverage(company_ref)
            except CoverageMissionError:
                continue
            if coverage["open_dispatches"]:
                continue
            failures = coverage.get("failures") or []
            # Only the failures this company is actually responsible for spend
            # its budget, and only those number its attempts.
            charged = [item for item in failures
                       if not _is_configuration_failure(item.get("reason"))]
            if len(charged) >= MAX_FAILURES_PER_COMPANY:
                continue
            retry_salt = None
            if failures and _is_configuration_failure(failures[-1].get("reason")):
                held_for = self._seconds_since(failures[-1].get("at"))
                if held_for is not None and held_for < CONFIGURATION_HOLD_SECONDS:
                    queued.append({
                        "company_ref": company_ref, "status": "held",
                        "reason": "this Core's own configuration failed the last "
                                  "run; holding rather than asking SEC again",
                    })
                    continue
                retry_salt = self._retry_salt()
            # Whatever this company already has of this form is enough for now.
            # Depth beyond the newest quarter is the planner's call, not a
            # default this lane takes on its own.
            if coverage["accessions"] and self.form in coverage["forms"]:
                continue
            try:
                authorization = self.missions.sec_lane_authorization_for_company(company_ref)
                dispatch = self.missions.queue_statement_dispatch(
                    authorization=authorization, form=self.form,
                    filing_limit=self.filing_limit, attempt=len(charged),
                    retry_salt=retry_salt,
                )
            except CoverageMissionError as exc:
                queued.append({"company_ref": company_ref, "status": "refused",
                               "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if dispatch["status_marker"] == "fresh":
                queued.append({"company_ref": company_ref, "status": "queued",
                               "dispatch_ref": dispatch["dispatch_id"]})
        return queued

    # -- launching ---------------------------------------------------------

    def _launch_one(self) -> dict[str, Any]:
        pending = self.missions.pending_statement_dispatches(limit=1)
        if not pending:
            return {"status": "idle"}
        dispatch = pending[0]
        try:
            ticket = self.launcher.start(
                ticker=dispatch["ticker"], form=dispatch["form"],
                limit=int(dispatch["filing_limit"]),
                actor_ref=dispatch["actor_ref"],
            )
        except LaneChildConflict as exc:
            return {"status": "deferred", "dispatch_ref": dispatch["dispatch_id"],
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            reason = f"{type(exc).__name__}: {exc}"
            try:
                self.missions.reject_statement_dispatch(dispatch["dispatch_id"], reason)
            except CoverageMissionError:
                pass
            return {"status": "rejected", "dispatch_ref": dispatch["dispatch_id"],
                    "reason": reason}
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], ticket["id"])
        return {"status": "launched", "dispatch_ref": dispatch["dispatch_id"],
                "company_ref": dispatch["company_ref"], "ticket_ref": ticket["id"]}

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_finished()
        queued = self._queue()
        launched = self._launch_one()
        return {
            "settled": settled, "queued": queued,
            **{key: value for key, value in launched.items()},
            "status": launched["status"],
        }


__all__ = [
    "DEFAULT_FILING_LIMIT",
    "DEFAULT_FORM",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_QUEUED_PER_RUN",
    "MissionStatementLaneCoordinator",
]
