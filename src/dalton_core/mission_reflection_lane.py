"""Q2 / C4 lane: one reflection a week, on the first tick after Monday 00:00.

Every other lane on this writer is driven by work arriving -- a filing lands, a
document needs extracting, a company has no specification.  This one is driven
by a clock, and it is the only one, which is worth saying out loud because it
changes what "idle" means for it: a reflection lane that does nothing for 2,016
consecutive ticks and then does one thing is working correctly, and a
reflection lane that fires twice in a week is broken.

So the cadence is expressed twice, in two different mechanisms, on purpose:

- **the tick** holds the boundary.  ``closed_week`` names the week that ended
  at the last Monday 00:00 *local* (the owner's Monday, not UTC's), and the
  lane launches only when it has not already written that week.  The check is a
  read of its own authority rather than a timer in memory, so a writer restart
  on Monday afternoon does not produce a second reflection.
- **the authority** holds the rule.  Even if the lane fired ten times, the
  second write of a week whose inputs have not moved is a ``duplicate``.  The
  clock decides when to look; the ``inputs_hash`` decides whether there is
  anything new to say.

A week whose numbers changed after Monday -- a late-arriving cost row, a
question answered on Tuesday about Friday's work -- can be reflected on again,
and that is a new version of the same week rather than a second record.  This
is why the lane keeps looking rather than firing once and going quiet.

The child costs nothing.  There is no model in this lane at all, which is also
why it takes the tick's child slot so rarely and so briefly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildLauncher,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .research_cycle_reflection import WRITE_SCOPE, closed_week, reflection_ref_for
from .store import canonical_json

LAUNCHER_KWARG = "reflection_launcher"
TICKET_PREFIX = "research-cycle-reflection"
TICKETS_DIRNAME = "research-cycle-reflections"
MAX_FAILURE_DETAIL_CHARS = 500


def may_write_reflection(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission grants the scope a reflection is published under.

    ``deliverable``.  A reflection is a dated document about the mission,
    produced on a cadence and read by a person -- the same class of thing as an
    Initial Screen, and unlike a Claim it asserts nothing about any company.
    Inventing a scope for it would have meant asking the owner to publish a
    mission version before the lane could run once; ``deliverable`` is already
    granted on the live mission, and the reflection genuinely belongs to it.
    """

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return False
    return WRITE_SCOPE in set(scopes)


class ReflectionLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.research_cycle_reflection_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = TICKETS_DIRNAME
    CHILD_MODULE = "dalton_core.research_cycle_reflection_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        tick_summary_dir: str | Path | None = None,
        actor_ref: str = "automation:coverage-mission",
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.tick_summary_dir = (
            None if tick_summary_dir is None
            else Path(tick_summary_dir).expanduser().resolve()
        )
        self.actor_ref = actor_ref

    def _command(self, *, ticket_dir: Path, **_: Any) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE, "run",
            "--state-dir", str(self.state_dir),
            "--actor-ref", self.actor_ref,
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]
        if self.tick_summary_dir is not None:
            command += ["--tick-summary-dir", str(self.tick_summary_dir)]
        return command

    def start(self, *, iso_week: str) -> dict[str, Any]:
        """One child for one week.  The week names the ticket."""

        if not isinstance(iso_week, str) or not iso_week.strip():
            raise LaneChildRejected("a reflection run is named by its ISO week")
        digest = hashlib.sha256(
            canonical_json({"iso_week": iso_week, "state": str(self.state_dir)}).encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(digest=digest, record={"iso_week": iso_week})


class MissionReflectionLaneCoordinator:
    """Settle last week's child, then start this week's if it is owed."""

    def __init__(
        self,
        *,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        already_reflected: Callable[[str, str], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.launcher = launcher
        self.mission = mission
        self.already_reflected = already_reflected
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._open: str | None = None
        # Weeks whose run failed, so a broken week does not consume the child
        # slot every five minutes for the rest of the week.  Process-local: a
        # restart is nearly always a deploy, which is the likeliest thing to
        # have fixed it.
        self._failed: dict[str, str] = {}

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now; look again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        recorded = summary.get("recorded") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            "iso_week": ticket.get("iso_week"),
            "reflection_status": recorded.get("status"),
            "reflection_ref": recorded.get("reflection_ref"),
            "version": recorded.get("version"),
            "backlog_candidates": len(summary.get("backlog_candidates") or []),
            "policy_suggestions": len(summary.get("policy_suggestions") or []),
        }
        reason = summary.get("reason")
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
        week = settled.get("iso_week")
        if week and settled.get("status") != "succeeded":
            self._failed[str(week)] = (
                settled.get("failure_reason") or f"last run: {settled.get('status')}"
            )
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            # Last tick's child has not finished. Answering here rather than
            # letting the launcher refuse a second spawn keeps the reason in
            # the lane's own words: the week is being written, not blocked.
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        if not may_write_reflection(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {WRITE_SCOPE} in autonomy.may_write; "
                    "a weekly reflection is a deliverable-class artefact and is not "
                    "published without the grant"
                ),
            }
        week = closed_week(self.clock())
        iso_week = week["iso_week"]
        held = self._failed.get(iso_week)
        if held is not None:
            return {"status": "held", "iso_week": iso_week, "settled": settled, "reason": held}
        try:
            done = self.already_reflected(str(mission["mission_ref"]), iso_week)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if done:
            # The normal resting state, six days out of seven. Named
            # "duplicate" rather than "idle" because it is not that there was
            # nothing to do -- the week's reflection exists, and saying so is
            # what makes a restart on Wednesday visibly a no-op.
            return {
                "status": "duplicate", "iso_week": iso_week, "settled": settled,
                "reflection_ref": reflection_ref_for(str(mission["mission_ref"]), iso_week),
                "reason": f"{iso_week} 已经有一条 reflection，且它的输入没有变",
            }
        try:
            ticket = self.launcher.start(iso_week=iso_week)
        except LaneChildConflict as exc:
            return {"status": "busy", "iso_week": iso_week, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "iso_week": iso_week, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "iso_week": iso_week,
            "ticket_ref": ticket["id"], "settled": settled,
        }


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (Q2).

    One reflection per mission per ISO week, launched on the first tick after
    Monday 00:00 local.  Six days out of seven it answers ``duplicate`` after
    two reads and costs nothing.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured", "reason": "no reflection lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        def already_reflected(mission_ref: str, iso_week: str) -> bool:
            # Read straight through the writer's connection rather than opening
            # the authority: the coordinator has no business being able to
            # write, and this is the only thing it needs to know.
            row = server.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_cycle_reflection_versions'"
            ).fetchone()
            if row is None:
                return False
            found = server.store.connection.execute(
                "SELECT 1 FROM research_cycle_reflection_versions "
                "WHERE reflection_ref=? LIMIT 1",
                (reflection_ref_for(mission_ref, iso_week),),
            ).fetchone()
            return found is not None

        coordinator = MissionReflectionLaneCoordinator(
            launcher=launcher, mission=mission, already_reflected=already_reflected,
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--reflection-lane", action="store_true",
        help="run the weekly research-cycle reflection lane",
    )
    parser.add_argument(
        "--reflection-tick-summary-dir",
        help="a directory of archived controller-tick summaries, for the idle-tick metric",
    )


def build_launcher(args: Any) -> Any | None:
    if not getattr(args, "reflection_lane", False):
        return None
    return ReflectionLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        tick_summary_dir=getattr(args, "reflection_tick_summary_dir", None),
    )


def argv_fragment(context: Any) -> list[str]:
    """On wherever there is a Core to reflect on.

    Every other lane's fragment is gated on the thing that lane needs -- a
    model configuration, a connector approval. This one needs no
    configuration at all, so the thing it is gated on is the only thing it
    genuinely requires: a Core in this state directory. A state directory with
    no ``core.sqlite`` has no week to reflect on, and the argument would name a
    lane whose first tick could only report that there is nothing there.

    What the lane also needs -- a mission granting ``deliverable`` -- is
    deliberately *not* checked here. A plist is rendered once at install and a
    grant changes when the owner publishes a mission version; a lane that
    disappeared from the plist because of a grant would need a reinstall to
    come back. So the grant is checked at dispatch, where the answer is
    reported (``ungranted``) instead of being silently absent.
    """

    if not (context.state / "core.sqlite").is_file():
        return []
    argv = ["--reflection-lane"]
    archive = context.state / "tick-summaries"
    if archive.is_dir():
        argv += ["--reflection-tick-summary-dir", str(archive)]
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_reflection",
    # Last. It reads what every other lane did this week, so running it after
    # them costs one tick of freshness at worst and never reads a half-written
    # week; and being last means a slow reflection cannot delay real work.
    order=120,
    driver_key="mission_reflection",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="Q2/C4: one ResearchCycleReflection a week -- where the spend, the "
         "questions and the waiting went. Proposes candidates; decides nothing.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionReflectionLaneCoordinator",
    "ReflectionLauncher",
    "TICKET_PREFIX",
    "TICKETS_DIRNAME",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "may_write_reflection",
]
