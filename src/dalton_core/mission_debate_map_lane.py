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
from .lane_failure_ledger import lane_budget
from .lane_permission_control import (
    authority_connection, current_permission, record_controlled_failure,
)

MAX_FAILURE_DETAIL_CHARS = 500
DRIVER_KEY = "mission_debate_map"
# Outcomes that say something about this moment rather than about this
# evidence, so the subject is not held back for them: the scheduler had the
# request in flight, or there was no route just then. The very next tick can
# do the work.
TRANSIENT_STATUSES: frozenset[str] = frozenset({"busy", "model_unavailable"})
# The one outcome that changes what the next tick sees. After it the stored
# version's fingerprint equals the evidence's, so the selector skips the
# subject on its own and no hold is needed.
PUBLISHED_STATUS = "fresh"

LAUNCHER_KWARG = "debate_map_launcher"
DEBATE_MAP_MODEL_CONFIG = "initial-screen-model-config.json"


def _business_key(subject_ref: str, fingerprint: str,
                  mission: dict[str, Any]) -> str:
    from .debate_map_draft import DRAFT_CONTRACT_HASH

    return (
        f"{subject_ref}|{fingerprint}|{mission['id']}|"
        f"{mission['content_hash']}|contract:{DRAFT_CONTRACT_HASH}"
    )


class MissionDebateMapLaneCoordinator:
    """Launch and settle the debate-map lane."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.mission = mission
        self._open: str | None = None
        self._open_business_key: str | None = None
        # Runs that failed, keyed by (subject, fingerprint), so a doomed
        # subject does not consume the slot every five minutes.  Held for this
        # process only: a restart is nearly always a deploy, which is the most
        # likely thing to have fixed whatever it was.
        self.budget = lane_budget(DRIVER_KEY, state_dir=failure_ledger_dir)

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
        business_key = self._open_business_key
        self._open_business_key = None
        map_status = settled.get("map_status")
        published = (
            settled.get("status") == "succeeded" and map_status == PUBLISHED_STATUS
        )
        # Hold on *every* other outcome, not only the ones that look like
        # failures. A run that reasoned over this exact evidence and published
        # nothing -- refused, rejected, duplicate, gated -- has answered the
        # question for this evidence, and asking it again next tick would let
        # one subject take the slot forever while subjects two through five
        # never get a turn. This was the shape of the bug: ``duplicate`` was
        # treated as "not a failure, so do not hold", the fingerprint never
        # moved because nothing was published, and the lane re-asked about the
        # same company every five minutes.
        #
        # The hold is keyed on (subject, fingerprint) and therefore releases
        # itself the moment one Claim arrives, which is exactly when the
        # question is worth asking again. It is process-local as well, so a
        # deploy -- the most likely thing to have fixed a ``gated`` or a
        # ``not_authorized`` -- clears it too.
        hold = not published
        subject_ref = settled.get("subject_ref")
        fingerprint = settled.get("evidence_fingerprint")
        if hold and subject_ref and fingerprint:
            key = business_key or f"{subject_ref}|{fingerprint}"
            reason = settled.get("failure_reason") or f"last run: {map_status or settled.get('status')}"
            settled["failure"] = record_controlled_failure(
                self.budget, key, self.mission() or {}, self.launcher,
                reason=reason, connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)), status=str(map_status or settled.get("status")),
            ).as_wire()
        elif subject_ref and fingerprint:
            settled["resumed"] = self.budget.clear(
                business_key or f"{subject_ref}|{fingerprint}")
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

    def _choose(self, mission: Any) -> tuple[str | None, str | None, str | None]:
        """The first subject whose evidence has moved and is not held.

        The held check belongs *inside* the loop, not after it.  A held subject
        left in front of the queue is not a subject that waits its turn -- it
        is a subject that takes the slot every tick and reports ``held``, and
        the four behind it are never looked at.  That is the same starvation
        the hold was added to cure, one step further along.

        The last held subject is returned when nothing else qualifies, so a
        tick that does nothing still says which subject it would have run and
        why it did not.
        """

        from .debate_map import DebateMapAuthority, evidence_fingerprint
        from .debate_map_draft import subject_claim_refs

        authority = DebateMapAuthority(self.store)
        blocked: tuple[str, str, str] | None = None
        for subject_ref in self._subjects(mission):
            # Refs only. The full drafting rows walk evidence per claim and
            # build a title map; doing that for five subjects on every tick is
            # work spent learning nothing on the overwhelmingly common tick
            # where the answer is "nothing moved".
            refs = subject_claim_refs(self.store, subject_ref)
            if not refs:
                continue
            fingerprint = evidence_fingerprint(refs)
            current = authority.current(subject_ref)
            if (current is not None
                    and current["evidence_fingerprint"] == fingerprint
                    and current.get("mission_version_ref") == mission.get("id")
                    and current.get("mission_version_hash") == mission.get("content_hash")):
                continue
            # A mission roll is a distinct authorized input even when its
            # claim set is byte-identical.  Keeping it in the persistent
            # signature also prevents an old terminal/permission outcome from
            # suppressing the rebind.
            business_key = _business_key(subject_ref, fingerprint, mission)

            permission = current_permission(

                self.budget, business_key, mission, self.launcher,
                connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)))

            decision = (self.budget.blocked(permission)

                        or self.budget.blocked(business_key))
            if decision is not None:
                blocked = blocked or (subject_ref, fingerprint,
                                      decision.classification.reason)
                continue
            return subject_ref, fingerprint, None
        if blocked is not None:
            return blocked
        return None, None, None

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            subject_ref, fingerprint, held = self._choose(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if subject_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every subject's map was drawn from the evidence "
                              "we currently hold"}
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
        self._open_business_key = _business_key(subject_ref, fingerprint, mission)
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
            failure_ledger_dir=getattr(server, "state_dir", None),
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


# C2's budget pools. Drawing the map is part of covering a company -- it reads
# the Claims coverage produced and says what they disagree about -- so it
# belongs in the same pool as the screen and the model specification, not in
# the event-response pool that pays for reacting to today's news. That is also
# where an unassigned lane lands by default, so the lane is correct today with
# no declaration, and ``LaneSpec.budget_pool`` is deliberately left unset:
# every registered lane currently takes its pool from ``budget_pools.LANE_POOLS``
# and C2 has a test saying so. The explicit line belongs in that table, which
# is not this slice's file; it is named in the report's integration list.
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
    "PUBLISHED_STATUS",
    "TRANSIENT_STATUSES",
    "MissionDebateMapLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
