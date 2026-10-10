"""F14 tail: the lanes with no failure budget of their own, written down anyway.

Twenty-three lanes carry a :class:`~dalton_core.lane_failure_class.LaneFailureBudget`
and append their parks, terminal verdicts and permission refusals to the lane
failure ledger themselves.  The other twenty-two never did: some because their
retry bookkeeping is durable in Core already (the statements lane counts failed
dispatches per company in the mission tables), some because they are
deterministic and rarely fail, some because nobody got to them.  The cockpit's
failure panel is a fold over the ledger, so a failure in any of those lanes was
simply not on it -- the panel said "nothing waiting" while the statements lane
was holding IBM.

This module closes that without touching how those lanes decide what to run.
The writer hands every lane result to :meth:`LaneOutcomeObserver.observe`; for a
lane whose registry entry names no ``failure_ledger`` the observer reads what
the lane already said -- its top-level ``status``/``reason``, the ``settled``
child outcomes, and the per-source sub-results a coordinator nests -- and
charges a budget named for the lane's ``driver_key``.  It is a **recorder**,
not a gate: nothing here is consulted before a lane runs, so a lane's own
admission rules are exactly what they were.

What it deliberately does not record (``resume-failure-ledger-next`` §Boundary):
quiet statuses -- ``idle``, ``duplicate``, ``waiting``, ``unconfigured``,
``ungranted`` and friends -- are a scheduler that is current, not failed work.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# The status words that mean "this attempt failed".  Everything else is either
# progress (``launched``, ``recorded``) or a state description, and neither
# belongs in a failure ledger.
#
# Not ``held``.  Live, ``held`` is what a lane says when it is *quiet* on
# purpose -- "研究状态没有变化，没有必要重新决策" (research_plan), "上一轮没有可写的内容"
# (initial_screen), "两次起草之间的最短间隔未到" (scenario_proposal) -- and what the
# writer's own short-circuit says for a governance, quota or configuration
# refusal, which the lanes panel already shows.  Thousands of those a day are a
# scheduler that is current, not a backlog.
FAILURE_STATUSES: frozenset[str] = frozenset({
    "failed", "rejected", "refused", "error", "acquisition_failed",
    "unavailable", "recovery_required", "terminal", "orphaned",
})
# Still working: neither a failure nor evidence that an earlier failure is over.
IN_PROGRESS_STATUSES: frozenset[str] = frozenset({"busy", "running"})
# Child outcomes that prove the item went through.
SUCCESS_OUTCOMES: frozenset[str] = frozenset({
    "succeeded", "success", "completed", "recorded", "committed", "acquired",
    "advanced", "gate_passed", "dispatched", "published",
})
# A failure is blocking once the budget has said so; the same sentence on the
# next tick is the same failure, not a new one.
_BLOCKING = frozenset({"parked", "terminal", "not_permitted", "held"})

LANE_ITEM = "lane"
#: How often the writer releases parked items whose dependency recovered or
#: which expired (``lane_failure_ledger.release_stale_parks``).
SWEEP_INTERVAL_SECONDS = 3600.0


def _status_word(value: Any) -> str:
    text = str(value or "").strip().lower()
    # ``unavailable:RemoteError`` and ``failed:timeout`` are the same word with
    # a detail attached; the detail stays in the reason.
    return text.split(":", 1)[0]


def _reason(record: Mapping[str, Any]) -> str:
    for key in ("failure_reason", "reason", "error", "detail"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    status = record.get("status") or record.get("outcome")
    return f"last run: {status}"


def _item_key(record: Mapping[str, Any], fallback: str) -> str:
    """Company first, because that is what the cockpit labels an item by."""

    company = record.get("company_ref")
    if isinstance(company, str) and company.startswith("company:"):
        form = record.get("form")
        return company if not isinstance(form, str) or not form else f"{company}|{form}"
    for key in ("subject_ref", "item_key", "source_ref"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


class LaneOutcomeObserver:
    """One writer's recorder for the lanes that do not record themselves."""

    def __init__(
        self, state_dir: str | Path | None, *,
        clock: Callable[[], datetime] | None = None,
        budget_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.state_dir = None if state_dir is None else Path(state_dir)
        self.clock = clock
        self._budget_factory = budget_factory
        self._budgets: dict[str, Any] = {}
        # (lane, item) -> (decision action, failure class, dependency) last
        # written.  Not the reason: a lane that holds says so every tick, often
        # with a countdown in the sentence, and a countdown is not a new failure.
        self._last: dict[tuple[str, str], tuple[str, str, str]] = {}
        # 2026-10-09: when the parked-item sweep last ran (monotonic seconds).
        self._swept_at: float | None = None
        self.sweep_interval_seconds = SWEEP_INTERVAL_SECONDS

    # -- which lanes ---------------------------------------------------------

    @staticmethod
    def observed_lane(operation: str) -> str | None:
        """The ledger name to record ``operation`` under, or ``None``.

        ``None`` for an operation that is not a driven lane and for a lane
        whose own budget already writes the ledger -- recording those twice
        would count every failure twice.
        """

        from .lane_registry import lane_for_operation

        spec = lane_for_operation(operation)
        if spec is None or spec.driver_key is None or spec.failure_ledger:
            return None
        return str(spec.driver_key)

    def budget(self, lane: str) -> Any:
        found = self._budgets.get(lane)
        if found is None:
            if self._budget_factory is not None:
                found = self._budget_factory(lane)
            else:
                from .lane_failure_ledger import lane_budget

                found = lane_budget(lane, state_dir=self.state_dir, clock=self.clock)
            self._budgets[lane] = found
        return found

    # -- the reading ---------------------------------------------------------

    def observe(self, operation: str, result: Any) -> dict[str, Any] | None:
        """Record what this lane result says failed; clear what went through.

        Never raises: the writer calls this after a lane has already done its
        work, and bookkeeping about that work must not become its failure.
        """

        self.sweep()
        lane: str | None = None
        try:
            lane = self.observed_lane(operation)
            if lane is None or not isinstance(result, Mapping):
                return None
            return self._observe(lane, result)
        except Exception as exc:  # noqa: BLE001 - a bookkeeper never fails a tick
            return {"lane": lane, "status": f"unrecorded:{type(exc).__name__}"}

    def sweep(self, *, force: bool = False) -> dict[str, Any] | None:
        """Release parked items whose dependency recovered or that expired.

        At most once per ``sweep_interval_seconds``; appends ``resumed``
        events with an auto-release rule (the ledger stays append-only).
        Never raises, like :meth:`observe`.
        """

        import time

        if self.state_dir is None:
            return None
        moment = time.monotonic()
        if (not force and self._swept_at is not None
                and moment - self._swept_at < self.sweep_interval_seconds):
            return None
        self._swept_at = moment
        try:
            from .lane_failure_ledger import release_stale_parks

            kwargs = {} if self.clock is None else {"now": self.clock()}
            released = release_stale_parks(self.state_dir, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a bookkeeper never fails a tick
            return {"status": f"unswept:{type(exc).__name__}"}
        # The budgets cached here replayed the ledger when they were built;
        # drop them so the next one replays the release too.
        if released:
            for lane in {row["lane"] for row in released}:
                self._budgets.pop(lane, None)
                for key in [key for key in self._last if key[0] == lane]:
                    self._last.pop(key, None)
        return {"status": "swept", "released": len(released)}

    def _observe(self, lane: str, result: Mapping[str, Any]) -> dict[str, Any]:
        recorded: list[str] = []
        cleared: list[str] = []
        self._one(lane, LANE_ITEM, result, recorded, cleared)
        # Coordinators that drive several sources nest one result per source
        # (source discovery: AlphaEngine at the top, ``web_search`` and
        # ``sec_filings_index`` beside it).  One level only: deeper mappings
        # are details of a result, not results.
        for key, value in result.items():
            if isinstance(value, Mapping) and "status" in value:
                self._one(lane, f"part:{key}", value, recorded, cleared)
        for child in self._settled(result):
            outcome = _status_word(child.get("outcome") or child.get("status"))
            item = _item_key(child, fallback=f"{LANE_ITEM}:child")
            if outcome in FAILURE_STATUSES:
                self._fail(lane, item, child, outcome, recorded)
            elif outcome in SUCCESS_OUTCOMES:
                self._clear(lane, item, cleared)
        return {
            "lane": lane, "recorded": recorded, "cleared": cleared,
            "ledger": self.budget(lane).ledger_status,
        }

    @staticmethod
    def _settled(result: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        settled = result.get("settled")
        if isinstance(settled, Mapping):
            settled = settled.get("settled") or settled.get("items") or []
        if not isinstance(settled, (list, tuple)):
            return ()
        return [row for row in settled if isinstance(row, Mapping)]

    def _one(self, lane: str, item: str, record: Mapping[str, Any],
             recorded: list[str], cleared: list[str]) -> None:
        status = _status_word(record.get("status"))
        if status in FAILURE_STATUSES:
            self._fail(lane, item, record, status, recorded)
        elif status and status not in IN_PROGRESS_STATUSES:
            self._clear(lane, item, cleared)

    def _fail(self, lane: str, item: str, record: Mapping[str, Any], status: str,
              recorded: list[str]) -> None:
        from .lane_failure_class import classify

        budget = self.budget(lane)
        reason = _reason(record)
        found = classify(reason, status=record.get("status") or record.get("outcome"),
                         lane=lane)
        key = (lane, item)
        previous = self._last.get(key)
        dependency = str(found.dependency or "")
        if (previous is not None and previous[0] in _BLOCKING
                and previous[1:] == (found.failure_class, dependency)):
            return
        decision = budget.record(item, classification=found)
        self._last[key] = (decision.action, found.failure_class, dependency)
        recorded.append(f"{item}:{decision.action}")

    def _clear(self, lane: str, item: str, cleared: list[str]) -> None:
        key = (lane, item)
        budget = self.budget(lane)
        known = key in self._last or budget.attempts(item) or any(
            row.get("item_key") == item for row in budget.blocked_items())
        if not known:
            return
        self._last.pop(key, None)
        budget.clear(item)
        # A terminal verdict is a statement about content, and only this lane
        # knows when its content changed.  It just went through, so it did.
        if any(row.get("item_key") == item for row in budget.terminal_items()):
            budget.retire(item, reason="a later run of this item succeeded")
        cleared.append(item)


__all__ = [
    "FAILURE_STATUSES",
    "IN_PROGRESS_STATUSES",
    "LANE_ITEM",
    "SUCCESS_OUTCOMES",
    "SWEEP_INTERVAL_SECONDS",
    "LaneOutcomeObserver",
]
