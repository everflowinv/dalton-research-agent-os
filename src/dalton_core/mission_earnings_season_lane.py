"""P14f: the earnings-season lane on a tick -- write, or stay quiet and cheap.

Queueless like the lanes it follows.  What needs doing is derived from three
ledgers each tick: the calendar (or the events it emitted) says which
occurrence has a window open, and the deliverable ledger says whether that
occurrence has already been written about.  Five covered companies with
nothing reporting produce a couple of queries and no child.

It runs after the judgement lane (116).  The order matters in one direction
only: a calibration leaves an event behind for the judgement lane to decide
whether the forward view moves, and a judgement lane that ran first would find
it on the next tick rather than this one.  That is the right way round -- the
calibration is written the day the company reports, and the decision about
next year can wait a tick.

The batch ref is the occurrence and the window, so a tick that fires twice in
an hour names the same ticket instead of paying twice for the same preview.
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
from .cockpit_model import verifier_provider_contract_fingerprint

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "earnings_season_launcher"
WRITER_MODEL_CONFIG = "earnings-season-model-config.json"
VERIFIER_MODEL_CONFIG = "earnings-season-verifier-model-config.json"
TRACKING_POLICY = "tracking-policy.json"


class MissionEarningsSeasonLaneCoordinator:
    """Launch and settle the earnings-season lane."""

    def __init__(
        self,
        *,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        pending: Callable[[Mapping[str, Any]], str | None],
    ) -> None:
        self.launcher = launcher
        self.mission = mission
        self.pending = pending
        self._open: str | None = None
        self._last_batch: str | None = None

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
            "batch_ref": ticket.get("batch_ref"),
            "season_status": summary.get("season_status"),
            "previews": summary.get("previews"),
            "calibrations": summary.get("calibrations"),
            "refused": summary.get("refused"),
            "candidates": summary.get("candidates"),
            "forecast_proposals": summary.get("forecast_proposals"),
            "pool": summary.get("pool"),
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
        try:
            newest = self.pending(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if newest is None:
            return {"status": "idle", "settled": settled,
                    "reason": "no covered company has an unwritten preview or "
                              "calibration window open"}
        contract = verifier_provider_contract_fingerprint(
            "earnings_preview_verifier", "earnings_calibration_verifier")
        batch = f"{mission['id']}:{newest}:{contract}"
        if batch == self._last_batch:
            return {"status": "idle", "settled": settled, "batch_ref": batch,
                    "reason": "this batch has already been dispatched"}
        try:
            ticket = self.launcher.start(batch_ref=batch)
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        self._last_batch = batch
        return {"status": "launched", "ticket_ref": ticket["id"],
                "batch_ref": batch, "settled": settled}


def due_occurrences(
    store: Any, missions: Any, mission: Mapping[str, Any], *, now: Any = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Every screened company's open, unwritten window.

    Reads the event ledger, and reads C1's calendar as well.  Both, every
    time: the ledger is where a calendar window is supposed to arrive, and the
    calendar is what answers on a Core where the catalyst lane has not run yet.
    The fallback is a pure read -- C1's own emitter with a collector in place
    of the writer -- so nothing is written to find out what is due.
    """

    from .earnings_season import occurrences_from_calendar, open_occurrences
    from .research_event import ResearchEventAuthority
    from .tracking_cadence import screen_passed_companies

    companies = screen_passed_companies(missions, mission)
    if not companies:
        return []
    events = ResearchEventAuthority(store)
    found = open_occurrences(
        store.connection, events, company_refs=companies, now=now, limit=limit,
    )
    # Every company goes through the fallback too, not only the ones the
    # ledger said nothing about.  Skipping a company because the ledger already
    # named *a* window for it would let a month-old preview hide the
    # calibration of a call that happened yesterday -- the two-day window would
    # close while the lane reported itself busy with the preview.  What
    # de-duplicates is the occurrence and the window, which is the thing the
    # two readers actually agree on.
    seen = {(row["occurrence_ref"], row["window"]) for row in found}
    try:
        from .catalyst_calendar import CatalystCalendarAuthority

        calendar = CatalystCalendarAuthority(store)
    except Exception:  # noqa: BLE001 - no calendar on this Core
        return found[:limit]
    for row in occurrences_from_calendar(
        store.connection, calendar, company_refs=companies, now=now, limit=limit,
    ):
        if (row["occurrence_ref"], row["window"]) in seen:
            continue
        seen.add((row["occurrence_ref"], row["window"]))
        found.append(row)
    return found[:limit]


def newest_due(store: Any, missions: Any, mission: Mapping[str, Any]) -> str | None:
    """What to name this run, or nothing to do.

    The soonest window rather than the newest event: a company reporting
    tomorrow is more urgent than one reporting in a month, and a calibration
    -- which has two days to live -- sorts ahead of both.
    """

    due = due_occurrences(store, missions, mission)
    if not due:
        return None
    due.sort(key=lambda row: (row["window"] != "calibration", row["expected_date"]))
    first = due[0]
    return f"{first['occurrence_ref']}:{first['window']}"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P14f)."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured", "reason": "no earnings-season lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .mission_deliverable import MissionDeliverableAuthority
        from .research_event import ResearchEventAuthority

        # Constructing the two authorities is what installs their schemas and,
        # for the deliverable authority, what widens the kind CHECK on a Core
        # built before these two kinds existed.  The tick query below reads
        # both tables.
        ResearchEventAuthority(server.store)
        MissionDeliverableAuthority(server.store)

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionEarningsSeasonLaneCoordinator(
            launcher=launcher, mission=mission,
            pending=lambda active: newest_due(
                server.store, server.coverage_mission, active
            ),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument("--earnings-season-model-config")
    parser.add_argument("--earnings-season-verifier-model-config")
    parser.add_argument("--earnings-season-policy")


def build_launcher(args: Any) -> Any | None:
    writer = getattr(args, "earnings_season_model_config", None)
    verifier = getattr(args, "earnings_season_verifier_model_config", None)
    if not writer or not verifier:
        return None
    from .earnings_season_launcher import EarningsSeasonLauncher

    return EarningsSeasonLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        writer_model_config=writer,
        verifier_model_config=verifier,
        policy_path=getattr(args, "earnings_season_policy", None),
        scheduler_db=getattr(args, "scheduler", None),
    )


def argv_fragment(context: Any) -> list[str]:
    writer = context.state / WRITER_MODEL_CONFIG
    verifier = context.state / VERIFIER_MODEL_CONFIG
    # Both or neither, for P14a's reason: a calibration proposes a thesis
    # revision, and one written with no independent check would carry a
    # verifier field that exists and means nothing.
    if not (writer.is_file() and verifier.is_file()):
        return []
    argv = [
        "--earnings-season-model-config", str(writer),
        "--earnings-season-verifier-model-config", str(verifier),
    ]
    policy = context.state / TRACKING_POLICY
    if policy.is_file():
        argv += ["--earnings-season-policy", str(policy)]
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_earnings_season",
    order=117,
    driver_key="earnings_season",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P14f: a preview a month before a covered company reports and a "
         "calibration within two days of the print. Runs after the judgement "
         "lane (116) because the calibration leaves an event for it to decide "
         "on, and that decision can wait a tick.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionEarningsSeasonLaneCoordinator",
    "TRACKING_POLICY",
    "VERIFIER_MODEL_CONFIG",
    "WRITER_MODEL_CONFIG",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "due_occurrences",
    "newest_due",
]
