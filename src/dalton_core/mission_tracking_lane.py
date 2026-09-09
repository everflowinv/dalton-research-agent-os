"""P14a: the resident lane. Every tick, every covered company, no queue.

The owner's instruction is the whole of the design: once a company's Initial
Screen has passed, it is tracked daily for as long as it is covered, whatever
else the system is doing.  So this lane has no selector, no priority order and
no way to be starved.  It launches one child that scans every tracked company,
settles it on the following tick, and does it again.

What it deliberately does *not* have is discretion.  The judgement lane may
propose that AlphaEngine be pulled every three days for a thinly covered
company; it may not propose that a company stop being tracked, and there is no
code path here that would let it.  A resident property implemented as a flag
somebody can clear is a property that gets cleared.

It costs nothing when nothing has happened: the child makes no model call and
reaches no network, and the event ledger is idempotent on what an event says,
so a scan that re-reads yesterday's documents writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "tracking_launcher"
# One run per hour of a mission version. Documents arrive at the pace the
# discovery lanes fetch them and prices settle once a day; scanning more often
# than this re-reads the same rows to write the same nothing.
WINDOW_SECONDS = 3600


class MissionTrackingLaneCoordinator:
    """Launch and settle the tracking lane. One child, no queue, no selection."""

    def __init__(
        self,
        *,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.launcher = launcher
        self.mission = mission
        if clock is None:
            from datetime import datetime, timezone

            clock = lambda: datetime.now(timezone.utc)  # noqa: E731
        self.clock = clock
        self._open: str | None = None
        self._last_window: str | None = None

    def window_ref(self, mission: Mapping[str, Any]) -> str:
        now = self.clock()
        bucket = int(now.timestamp()) // WINDOW_SECONDS
        return f"{mission['id']}:{bucket}"

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
            "window_ref": ticket.get("window_ref"),
            "tracking_status": summary.get("tracking_status"),
            "tracked_companies": summary.get("tracked_companies"),
            "events_recorded": summary.get("events_recorded"),
            "events_by_kind": summary.get("events_by_kind"),
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
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        window = self.window_ref(mission)
        if window == self._last_window:
            # The same hour of the same mission version: the previous run has
            # already asked every question this one would ask.
            return {"status": "idle", "settled": settled, "window_ref": window,
                    "reason": "this window has already been scanned"}
        try:
            ticket = self.launcher.start(window_ref=window)
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        self._last_window = window
        return {"status": "launched", "ticket_ref": ticket["id"],
                "window_ref": window, "settled": settled}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P14a)."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured", "reason": "no tracking lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionTrackingLaneCoordinator(launcher=launcher, mission=mission)
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument("--tracking-policy", type=Path,
                        help="P14a tracking baselines and abnormal-move thresholds")


def build_launcher(args: Any) -> Any | None:
    policy = getattr(args, "tracking_policy", None)
    if not policy:
        return None
    from .tracking_lane_launcher import TrackingLaneLauncher

    return TrackingLaneLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent, policy_path=policy
    )


def argv_fragment(context: Any) -> list[str]:
    # The policy file is the switch, exactly as a governance record is for the
    # connector lanes: a tracking lane with no baselines has no opinion about
    # how often to look at anything, and starting it would be worse than not
    # having it.
    policy = context.state / "tracking-policy.json"
    if not policy.is_file():
        return []
    return ["--tracking-policy", str(policy)]


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_tracking",
    order=86,
    driver_key="mission_tracking",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P14a: daily tracking for every company past its Initial Screen. Runs "
         "at 86, after the price lane (85) because an abnormal move is read "
         "off the bars that lane just published, and before the judgement "
         "lane, which reads what this one records. 87 is C1's catalyst "
         "calendar.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionTrackingLaneCoordinator",
    "WINDOW_SECONDS",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
