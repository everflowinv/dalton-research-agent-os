"""P12b: tag one company's untagged Claims on a tick, and go quiet when there are none.

Queueless, for the same reason the model-specification lane is: what needs
doing is a *derived* fact -- the latest version of every Claim, minus what the
index already covers -- so it is computed from the Ledger every tick rather
than written down and drained.  There is nothing to leave stuck, and no second
place where "still to do" is recorded and can go stale.

The resting state is silence.  Once the backlog is tagged the lane does nothing
until a Claim is committed or re-versioned, at which point exactly that claim
is tagged and the lane goes quiet again.  A quarter of ticks in between all say
"nothing to tag", which is the correct answer and costs one snapshot read.

Companies are taken in the mission's own universe order and the first with
pending claims wins the slot.  Deliberately not "the one with the most
pending": a company that has just been discovered would otherwise sit behind
whichever one accumulates news fastest, and the universe order is the order the
owner wrote down.
"""

from __future__ import annotations

from typing import Any, Callable

from .claim_index_tagging import pending_claims
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

MAX_FAILURE_DETAIL_CHARS = 500
# One tick looks at this many pending claims per company before deciding.  The
# child re-derives its own batch; this bound is only so that a company with a
# thousand untagged claims does not make the tick's selection read all of them.
MAX_PENDING_SCANNED = 200
# Outcomes that say something about this moment rather than about this batch,
# so the batch is not held back for them.
TRANSIENT_STATUSES: frozenset[str] = frozenset({"busy", "model_unavailable"})


class MissionClaimIndexLaneCoordinator:
    """Launch and settle the Claim-index lane."""

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
        # The batch in flight, so the next tick can settle it.  A launcher
        # holds one process, not a history, and the ticket ref is the only
        # handle on the summary.
        self._open: str | None = None
        # Batches whose run failed, keyed by (company, batch digest), so a
        # doomed batch does not consume the slot every five minutes.  Held for
        # this process only: a restart is nearly always a deploy, which is the
        # most likely thing to have fixed whatever it was.
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
            # writing one still has to be attributable to the batch it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "batch_digest": ticket.get("batch_digest"),
            "index_status": summary.get("index_status"),
            "rule_tagged": summary.get("rule_tagged"),
            "model_tagged": summary.get("model_tagged"),
            "cost_micros": summary.get("cost_micros"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        """Close out the previous tick's child, if it has finished.

        Settling happens before launching rather than after, because a child
        inspected in the same breath it was spawned is always still running.
        """

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        # ``busy`` is "the scheduler had this request in flight" and
        # ``model_unavailable`` is "no route right now". Both are true of a
        # moment, not of a batch, and holding a batch back for them would park
        # work that the very next tick could do.
        index_status = settled.get("index_status")
        failed = (
            index_status not in TRANSIENT_STATUSES
            and (settled.get("status") != "succeeded"
                 or index_status in ("refused", "failed", "not_authorized"))
        )
        company_ref = settled.get("company_ref")
        digest = settled.get("batch_digest")
        if failed and company_ref and digest:
            self._failed[f"{company_ref}|{digest}"] = (
                settled.get("failure_reason")
                or f"last run: {settled.get('index_status') or settled.get('status')}"
            )
        return settled

    # -- the tick ---------------------------------------------------------

    def _choose(self, mission: Any) -> tuple[str | None, list[str]]:
        """The first company in universe order with untagged claims."""

        snapshot = self.store.claim_index_snapshot()
        for item in mission.get("universe") or []:
            company_ref = item.get("company_ref") if isinstance(item, dict) else None
            if not company_ref:
                continue
            pending = pending_claims(
                self.store, subject_refs=[company_ref],
                limit=MAX_PENDING_SCANNED, snapshot=snapshot,
            )
            if pending:
                return company_ref, [row["claim_version_ref"] for row in pending]
        return None, []

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        try:
            company_ref, refs = self._choose(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if company_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every claim is indexed"}
        from .claim_index_launcher import batch_digest

        digest = batch_digest(company_ref, refs)
        held = self._failed.get(f"{company_ref}|{digest}")
        if held is not None:
            return {"status": "held", "company_ref": company_ref,
                    "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(
                company_ref=company_ref, claim_version_refs=refs
            )
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "batch_digest": digest, "pending": len(refs),
            "ticket_ref": ticket["id"], "settled": settled,
        }


# -- registration ------------------------------------------------------------
#
# INT1: P12b delivered the coordinator, the launcher and the CLI and wrote the
# LaneSpec out in its report without putting it anywhere, so the index had no
# tick and only ever ran by hand. This is that line.
#
# Everything below is either this lane's own decision -- what turns it on, what
# it is called in the tick summary -- or a lazy import, so that importing this
# module registers the lane without dragging in the writer.

LAUNCHER_KWARG = "claim_index_launcher"
# The child tags every claim's importance, as_of and dedupe group by rule and
# needs a model only for the aspect of qualitative prose, so the configuration
# is what turns the *lane* on rather than what makes it useful: without one the
# child would report ``gated`` on every batch it could not finish, every tick,
# forever. Same switch shape as the model-specification lane.
CLAIM_INDEX_MODEL_CONFIG = "claim-index-model-config.json"


def dispatch(server: Any, params: Any) -> dict[str, Any]:
    """Controller tick (P12b).

    One company's pending batch at a time. The lane has no queue -- what is
    untagged is derived from the Ledger every tick -- so its resting state is
    silence and there is nothing to leave stuck.

    The coordinator is cached in ``server.lane_state`` rather than rebuilt,
    because the state it holds is the point: which batch is open and which
    batches have already failed. A fresh coordinator every tick would forget
    both and re-launch a child for a batch it had just been told was doomed.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no claim index lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionClaimIndexLaneCoordinator(
            # The store, not the missions authority: what this lane needs to
            # read is the claim index snapshot.
            store=server.store,
            launcher=launcher,
            mission=mission,
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--claim-index-model-config",
        help="model configuration for tagging qualitative claims",
    )


def build_launcher(args: Any) -> Any | None:
    if args.claim_index_model_config is None:
        return None
    from pathlib import Path as _Path

    from .claim_index_launcher import ClaimIndexLauncher

    return ClaimIndexLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.claim_index_model_config,
        scheduler_db=args.scheduler,
    )


def argv_fragment(context: Any) -> list[str]:
    config = context.state / CLAIM_INDEX_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--claim-index-model-config", str(config)]


LANE = register_lane(LaneSpec(
    operation="dispatch_claim_index",
    # After the research plan (100) and before the Initial Screen (110): the
    # screen drafts from Claims, and drafting from an indexed set is what stops
    # it citing the same quarter's revenue three times in one paragraph.
    order=105,
    driver_key="claim_index",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12b: aspect, as_of, importance and duplicate-merge for every Claim, "
         "as an append-only projection that changes no Claim's bytes.",
))


__all__ = [
    "CLAIM_INDEX_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MAX_PENDING_SCANNED",
    "TRANSIENT_STATUSES",
    "MissionClaimIndexLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
