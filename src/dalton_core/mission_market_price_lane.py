"""P11a: keep every covered company's price history current, one child a tick.

Queueless, like the specification lane and unlike the acquisition lanes. What
needs fetching is a *derived* fact -- a company whose stored series stops before
the last trading day -- so it is computed from the authority every tick rather
than written down and drained. There is nothing to leave stuck, and a company
that is up to date simply is not chosen.

The resting state is therefore almost-silence: five companies get one child
each on the first afternoon, and after that one company a tick picks up
yesterday's bar and the rest are skipped in a couple of reads. Weekends cost
nothing, because a run that adds no bar puts the company aside for a while
rather than asking Yahoo the same empty question every five minutes.

**The grant.** This lane writes a market-price authority, so the mission has to
say it may: ``market_price`` in ``autonomy.may_write``. A mission that does not
grant it gets ``ungranted`` and no child, every tick, forever -- which is the
correct behaviour, not a bug to route around. The grant is a decision the owner
makes by publishing a mission version.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)

WRITE_SCOPE = "market_price"
# How far back a company with no stored history is fetched. Three years is the
# blueprint's own acceptance bar -- a valuation percentile computed over six
# months is a number about this year's mood, not about the company.
BACKFILL_YEARS = 3
# Yahoo's window excludes ``end``, so asking for tomorrow is how today's bar is
# included. Not a fudge: it is what the source means by the parameter.
END_LOOKAHEAD_DAYS = 1
# After a run that found nothing new, leave the company alone for this long.
# A weekend tick would otherwise spend a call every five minutes discovering
# that Saturday is still not a trading day.
SATISFIED_HOLD_SECONDS = 6 * 3600
MAX_FAILURE_DETAIL_CHARS = 500
# A company whose runs keep failing stops consuming the single slot. Held in
# this process only: a restart is nearly always a deploy, which is the most
# likely thing to have fixed whatever it was.
MAX_FAILURES_PER_COMPANY = 3


def _universe(mission: Mapping[str, Any]) -> list[dict[str, str]]:
    """The covered companies, in the order the mission prioritised them.

    A company with no ticker is skipped rather than guessed at: this connector
    is keyed by market symbol and there is no mapping from a CIK to one that
    does not involve asking somebody.
    """

    rows: list[dict[str, str]] = []
    for item in mission.get("universe") or ():
        if not isinstance(item, Mapping):
            continue
        company_ref = item.get("company_ref")
        ticker = item.get("ticker")
        if not isinstance(company_ref, str) or not company_ref.strip():
            continue
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        rows.append({
            "company_ref": company_ref.strip(),
            "ticker": ticker.strip().upper(),
            "bootstrap_priority": str(item.get("bootstrap_priority") or "P9"),
        })
    rows.sort(key=lambda row: (row["bootstrap_priority"], row["ticker"]))
    return rows


def may_write_market_price(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission granted the automation the price-authority scope."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return False
    return WRITE_SCOPE in set(scopes)


class MissionMarketPriceLaneCoordinator:
    """Settle the previous tick's price child, then start at most one more."""

    def __init__(
        self,
        *,
        authority: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        clock: Callable[[], datetime] | None = None,
        backfill_years: int = BACKFILL_YEARS,
    ) -> None:
        self.authority = authority
        self.launcher = launcher
        self.mission = mission
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.backfill_years = int(backfill_years)
        # The fetch in flight, so the next tick can settle it. A launcher
        # cannot be asked "what did you last run" -- it holds one process, not
        # a history -- and the ticket ref is the only handle on the summary.
        self._open: str | None = None
        # Companies whose runs failed, and how many times. Held in this process
        # only, on purpose: see the module note.
        self._failures: dict[str, int] = {}
        self._failure_reason: dict[str, str] = {}
        # Companies that were up to date last time they were asked, and when.
        self._satisfied: dict[str, datetime] = {}

    # -- window ------------------------------------------------------------

    def _today(self) -> date:
        return self.clock().astimezone(timezone.utc).date()

    def window(self, company_ref: str) -> tuple[str, str] | None:
        """The days this company is missing, or None if it has them all.

        The first run reaches back three years. Every run after that starts the
        day after the last stored bar, so a lane running daily asks for one day
        and a lane that was off for a month asks for a month -- without anyone
        having to decide which.
        """

        today = self._today()
        end = today + timedelta(days=END_LOOKAHEAD_DAYS)
        latest = self.authority.latest_version(company_ref)
        if latest is None:
            try:
                start = today.replace(year=today.year - self.backfill_years)
            except ValueError:
                # 29 February exists in one year in four; the backfill does not
                # need to fail on the one tick a leap year that lands on it.
                start = today.replace(
                    year=today.year - self.backfill_years, month=2, day=28
                )
            return start.isoformat(), end.isoformat()
        last = date.fromisoformat(latest["last_bar_date"])
        start = last + timedelta(days=1)
        if start >= end:
            return None
        return start.isoformat(), end.isoformat()

    # -- settling ----------------------------------------------------------

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
            # writing one still has to be attributable to the company it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "series_status": summary.get("series_status"),
            "series_version_ref": summary.get("series_version_ref"),
            "bar_count": summary.get("bar_count"),
            "added_bar_count": summary.get("added_bar_count"),
            "restated_bar_dates": summary.get("restated_bar_dates"),
            "last_bar_date": summary.get("last_bar_date"),
            "invocation_ref": summary.get("invocation_ref"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        """Close out the previous tick's child, if it has finished.

        Settling happens here rather than after ``start`` for the obvious
        reason: a child inspected in the same breath it was spawned is always
        still running, and a lane that only ever looks at its own newborn never
        learns anything.
        """

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        company_ref = settled.get("company_ref")
        if not company_ref:
            return settled
        if settled.get("status") != "succeeded":
            self._failures[company_ref] = self._failures.get(company_ref, 0) + 1
            self._failure_reason[company_ref] = (
                settled.get("failure_reason")
                or f"last run: {settled.get('status')}"
            )
            return settled
        # A run that succeeded is a run that reached the source, whatever it
        # found: the retry budget is about companies this lane cannot serve,
        # not about quiet markets.
        self._failures.pop(company_ref, None)
        self._failure_reason.pop(company_ref, None)
        if settled.get("series_status") in {"duplicate", "empty"}:
            self._satisfied[company_ref] = self.clock()
        else:
            self._satisfied.pop(company_ref, None)
        return settled

    def _held_recently(self, company_ref: str) -> bool:
        when = self._satisfied.get(company_ref)
        if when is None:
            return False
        held = (self.clock() - when).total_seconds()
        if held >= SATISFIED_HOLD_SECONDS:
            self._satisfied.pop(company_ref, None)
            return False
        return True

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        if not may_write_market_price(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {WRITE_SCOPE} in "
                    "autonomy.may_write; publishing a price series without the "
                    "grant is not something to work around"
                ),
            }
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "a price child is still running"}
        skipped: list[dict[str, Any]] = []
        for company in _universe(mission):
            company_ref = company["company_ref"]
            if self._failures.get(company_ref, 0) >= MAX_FAILURES_PER_COMPANY:
                skipped.append({
                    "company_ref": company_ref, "reason": "held",
                    "detail": self._failure_reason.get(company_ref, "repeated failures"),
                })
                continue
            if self._held_recently(company_ref):
                skipped.append({"company_ref": company_ref, "reason": "recently_current"})
                continue
            try:
                window = self.window(company_ref)
            except Exception as exc:  # noqa: BLE001 - one company, not the tick
                skipped.append({
                    "company_ref": company_ref, "reason": "unreadable",
                    "detail": f"{type(exc).__name__}: {exc}",
                })
                continue
            if window is None:
                skipped.append({"company_ref": company_ref, "reason": "current"})
                continue
            start, end = window
            try:
                ticket = self.launcher.start(
                    company_ref=company_ref, ticker=company["ticker"],
                    start=start, end=end,
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped,
                        "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._failures[company_ref] = self._failures.get(company_ref, 0) + 1
                self._failure_reason[company_ref] = reason
                return {"status": "rejected", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped, "reason": reason}
            self._open = ticket["id"]
            return {
                "status": "launched", "company_ref": company_ref,
                "ticker": company["ticker"], "requested_start": start,
                "requested_end": end, "ticket_ref": ticket["id"],
                "settled": settled, "skipped": skipped,
            }
        return {
            "status": "idle", "settled": settled, "skipped": skipped,
            "reason": "every covered company's price history is current",
        }


__all__ = [
    "BACKFILL_YEARS",
    "END_LOOKAHEAD_DAYS",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_FAILURE_DETAIL_CHARS",
    "SATISFIED_HOLD_SECONDS",
    "WRITE_SCOPE",
    "MissionMarketPriceLaneCoordinator",
    "may_write_market_price",
]
