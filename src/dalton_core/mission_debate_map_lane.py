"""P12c: redraw one subject's debate map on a tick, when its evidence moved.

Queueless, like the two lanes before it, and for a sharper reason than either.
Redrawing a map is a judgement about a *state*, not a task in a backlog: what
matters is whether the evidence this subject's map was drawn from is still the
evidence we hold.  That is derived from the Ledger every tick -- the
fingerprint of the subject's canonical claim versions against the fingerprint
the current version recorded -- so there is nothing to leave stuck and no
second place where "still to do" is written down and can go stale.

The resting state is silence, and here it is the *usual* state.  A map is
redrawn when Claims arrive, which for a covered company is a few times a week;
between those the lane says "nothing moved", which is the correct answer and
costs one projection read.  A lane that redrew a map every tick would produce a
version chain in which nothing can be seen to change, which is the exact
failure ADR-0008 exists to prevent.

Subjects are taken in the mission's own universe order, industry last: the
industry map reads across companies, and drawing it before the companies whose
evidence it summarises have been redrawn puts the summary in front of the
thing summarised.  The first subject whose evidence moved wins the slot.
"""

from __future__ import annotations

from typing import Any, Callable

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

MAX_FAILURE_DETAIL_CHARS = 500
# Outcomes that say something about this moment rather than about this subject,
# so the subject is not held back for them.
TRANSIENT_STATUSES: frozenset[str] = frozenset({"busy", "model_unavailable"})
# Outcomes that mean the run worked and there was simply nothing to publish.
# Holding a subject back for these would be right for an hour and wrong forever.
SETTLED_STATUSES: frozenset[str] = frozenset({"duplicate", "gated", "dry_run"})

LAUNCHER_KWARG = "debate_map_launcher"
DEBATE_MAP_MODEL_CONFIG = "initial-screen-model-config.json"


class MissionDebateMapLaneCoordinator:
    """Launch and settle the debate-map lane."""

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
        self._open: str | None = None
        # Runs that failed, keyed by (subject, fingerprint), so a doomed
        # subject does not consume the slot every five minutes.  Held for this
        # process only: a restart is nearly always a deploy, which is the most
        # likely thing to have fixed whatever it was.
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
            # writing one still has to be attributable to the run it was
            # spawned for, or it can never be held back from being retried.
            "subject_ref": ticket.get("subject_ref"),
            "evidence_fingerprint": ticket.get("evidence_fingerprint"),
            "map_status": summary.get("map_status"),
            "version_ref": summary.get("version_ref"),
            "debates": summary.get("debates"),
            "rejected": summary.get("rejected"),
            "cost_micros": summary.get("cost_micros"),
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
        map_status = settled.get("map_status")
        failed = (
            map_status not in TRANSIENT_STATUSES
            and map_status not in SETTLED_STATUSES
            and (settled.get("status") != "succeeded"
                 or map_status in ("refused", "unverified", "verifier_rejected",
                                   "not_independent", "no_admitted_debates",
                                   "not_authorized", "failed"))
        )
        subject_ref = settled.get("subject_ref")
        fingerprint = settled.get("evidence_fingerprint")
        if failed and subject_ref and fingerprint:
            self._failed[f"{subject_ref}|{fingerprint}"] = (
                settled.get("failure_reason")
                or f"last run: {map_status or settled.get('status')}"
            )
        return settled

    # -- the tick ---------------------------------------------------------

    def _subjects(self, mission: Any) -> list[str]:
        subjects = [
            item["company_ref"] for item in (mission.get("universe") or [])
            if isinstance(item, dict) and item.get("company_ref")
        ]
        industry = mission.get("industry_ref")
        if isinstance(industry, str) and industry:
            subjects.append(industry)
        return subjects

    def _choose(self, mission: Any) -> tuple[str | None, str | None]:
        """The first subject whose evidence has moved since its current map."""

        from .debate_map import DebateMapAuthority, evidence_fingerprint
        from .debate_map_draft import subject_claim_rows

        authority = DebateMapAuthority(self.store)
        for subject_ref in self._subjects(mission):
            rows = subject_claim_rows(self.store, subject_ref)
            if not rows:
                continue
            fingerprint = evidence_fingerprint(
                row["claim_version_ref"] for row in rows
            )
            current = authority.current(subject_ref)
            if current is not None and current["evidence_fingerprint"] == fingerprint:
                continue
            return subject_ref, fingerprint
        return None, None

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            subject_ref, fingerprint = self._choose(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if subject_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every subject's map was drawn from the evidence "
                              "we currently hold"}
        held = self._failed.get(f"{subject_ref}|{fingerprint}")
        if held is not None:
            return {"status": "held", "subject_ref": subject_ref,
                    "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(
                subject_ref=subject_ref, fingerprint=fingerprint
            )
        except LaneChildConflict as exc:
            return {"status": "busy", "subject_ref": subject_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "subject_ref": subject_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "subject_ref": subject_ref,
            "evidence_fingerprint": fingerprint, "ticket_ref": ticket["id"],
            "settled": settled,
        }


# -- the lane ---------------------------------------------------------------

def dispatch(server: Any, params: Any) -> dict[str, Any]:
    """Controller tick (P12c).  One subject's debate map at a time."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no debate map lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionDebateMapLaneCoordinator(
            store=server.store, launcher=launcher, mission=mission,
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument("--debate-map-model-config")


def build_launcher(args: Any) -> Any | None:
    if args.debate_map_model_config is None:
        return None
    from pathlib import Path as _Path

    from .debate_map_launcher import DebateMapLauncher

    return DebateMapLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.debate_map_model_config,
        scheduler_db=args.scheduler,
    )


def argv_fragment(context: Any) -> list[str]:
    config = context.state / DEBATE_MAP_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--debate-map-model-config", str(config)]


LANE = register_lane(LaneSpec(
    operation="dispatch_debate_map",
    # 135: after the reading lanes that produce the Claims it argues over and
    # before nothing in particular. Wave 2's other agents took the neighbouring
    # tens; this one is deliberately not adjacent to the Claim index, because
    # a tick that indexed and then immediately argued about the same batch
    # would be arguing about a half-tagged one.
    order=135,
    driver_key="debate_map",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12c: what is contested about a company or the industry -- bull and "
         "bear with their evidence, where the market stands, where we differ, "
         "and which way it has been moving.",
))


__all__ = [
    "DEBATE_MAP_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "SETTLED_STATUSES",
    "TRANSIENT_STATUSES",
    "MissionDebateMapLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
