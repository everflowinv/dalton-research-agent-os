"""W4: the zero-base review on a tick -- monthly, after a print, otherwise free.

Chem's version of this was a cron entry that never fired once.  So the cadence
here is not a timer at all: every tick asks the Ledger two questions and both
are cheap.

**Is a review owed?**  A company past its Initial Screen with no review for
this month, or with an earnings calibration that no review has been written
against.  One read of this lane's own table plus one of the deliverable
chain -- and a review that exists is a review that exists, so a writer restart
on the 12th does not buy a second one.

**Has the outcome ledger moved?**  The derived check pass -- was the
``no_change`` right, did the ``revise`` go the way a later actual says -- is
recomputed in-process, read-only and with no model in it, and its digest is
compared with the one the last child wrote.  When only that has moved, the
child runs in ``checks`` mode and costs nothing, which is what keeps the
"observation → result" ledger fresh at tick rate instead of monthly.

Otherwise the lane answers ``idle`` after two reads, which is what it does on
almost every tick of almost every day.

It runs after the judgement lane (116) and the research-task lane (150),
because it reads what they wrote, and immediately before the weekly reflection
(160), because the reflection reports the outcome counts this lane keeps
fresh.  The reflection stays last in the tick: that is its own invariant and
this lane has no reason to take it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .zero_base_review import WRITE_SCOPE

LAUNCHER_KWARG = "zero_base_review_launcher"
REVIEW_MODEL_CONFIG = "zero-base-review-model-config.json"
TRACKING_POLICY = "tracking-policy.json"
MAX_FAILURE_DETAIL_CHARS = 500


def may_write_review(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission grants the scope a review is published under."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, (list, tuple)):
        return False
    return WRITE_SCOPE in set(scopes)


class MissionZeroBaseLaneCoordinator:
    """Settle the open child, then start the one this tick owes, if any."""

    def __init__(
        self,
        *,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        lane_state: Callable[[Mapping[str, Any], datetime], dict[str, Any]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.launcher = launcher
        self.mission = mission
        # (mission, now) -> {"due": [...], "checks_digest": "..."}.  Named as a
        # collaborator so the tick's decision can be tested without a Core, and
        # so the coordinator cannot reach anything that writes.
        self.lane_state = lane_state
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._open: str | None = None
        self._checked: str | None = None
        # Batches that failed, so a broken month does not consume the child
        # slot every five minutes -- and so a batch whose inputs then move is
        # tried again, because that is a different attempt.  Process-local: a
        # restart is nearly always a deploy, which is the likeliest thing to
        # have fixed it.
        self._failed: dict[tuple[str, str], str] = {}

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
        checks = summary.get("checks") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            "mode": ticket.get("mode"),
            "batch_ref": ticket.get("batch_ref"),
            "review_status": summary.get("review_status"),
            "due": summary.get("due"),
            "reviewed": summary.get("reviewed"),
            "refused": summary.get("refused"),
            "candidates": summary.get("candidates"),
            "checked": checks.get("checked"),
            "checks_fresh": checks.get("fresh"),
            "checks_digest": checks.get("digest"),
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
        mode = str(settled.get("mode") or "")
        batch = str(settled.get("batch_ref") or "")
        if settled.get("status") == "succeeded":
            # Only a run that finished is allowed to move the watermark. A
            # crashed child that had already written half the pass would
            # otherwise make the lane believe the ledger was fresh.
            digest = settled.get("checks_digest")
            if digest:
                self._checked = str(digest)
        elif mode and batch:
            self._failed[(mode, batch)] = (
                settled.get("failure_reason") or f"last run: {settled.get('status')}"
            )
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        if not may_write_review(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {WRITE_SCOPE} in autonomy.may_write; "
                    "a zero-base review is a deliverable-class artefact and is not "
                    "published without the grant"
                ),
            }
        now = self.clock()
        try:
            state = self.lane_state(mission, now)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        due = list(state.get("due") or ())
        digest = str(state.get("checks_digest") or "")
        if due:
            mode = "review"
            companies = [str(item["company_ref"]) for item in due]
            batch = "|".join(sorted(
                f"{item['trigger']}:{item['period_label']}:{item['company_ref']}"
                for item in due
            ))[:400]
        elif digest and digest != self._checked:
            mode = "checks"
            companies = []
            batch = digest
        else:
            return {"status": "idle", "settled": settled, "checks_digest": digest,
                    "reason": "每家公司这个月都已经从零重问过，判断结果台账也没有变动"}
        held = self._failed.get((mode, batch))
        if held is not None:
            return {"status": "held", "mode": mode, "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(
                mode=mode, batch_ref=batch, company_refs=companies
            )
        except LaneChildConflict as exc:
            return {"status": "busy", "mode": mode, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "mode": mode, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {"status": "launched", "mode": mode, "ticket_ref": ticket["id"],
                "due": len(due), "settled": settled}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (W4)."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no zero-base review lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .judgement_outcome import JudgementOutcomeAuthority
        from .market_price import MarketPriceSeriesAuthority
        from .zero_base_review import ZeroBaseReviewAuthority

        # Constructing them is what installs their schemas; the reads below
        # guard on the tables anyway, but a Core that has this lane installed
        # should have the tables from the first tick rather than from the first
        # child that happens to finish.
        ZeroBaseReviewAuthority(server.store)
        JudgementOutcomeAuthority(server.store)
        prices = MarketPriceSeriesAuthority(server.store)

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        def lane_state(
            mission_record: Mapping[str, Any], now: datetime
        ) -> dict[str, Any]:
            """What is owed, computed read-only through the writer's connection."""

            from .judgement_outcome import build_outcome_checks, checks_digest
            from .tracking_cadence import load_policy, screen_passed_companies
            from .tracking_lane_cli import thesis_stances
            from .zero_base_review import due_reviews

            connection = server.store.connection
            tracked = screen_passed_companies(server.coverage_mission, mission_record)
            due = due_reviews(
                connection, mission_ref=str(mission_record["mission_ref"]),
                company_refs=tracked, now=now,
            )
            policy = load_policy(getattr(launcher, "policy_path", None))
            universe = [
                member["company_ref"] for member in mission_record["universe"]
            ]
            checks = build_outcome_checks(
                connection, company_refs=tracked,
                series_by_company={ref: prices.series(ref) for ref in universe},
                thresholds=policy["abnormal_move"],
                stances=thesis_stances(server.store, policy, tracked=universe),
            )
            return {"due": due, "checks_digest": checks_digest(checks)}

        coordinator = MissionZeroBaseLaneCoordinator(
            launcher=launcher, mission=mission, lane_state=lane_state,
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--zero-base-review-model-config",
        help="the model configuration the monthly zero-base review calls with",
    )
    parser.add_argument("--zero-base-review-policy")


def build_launcher(args: Any) -> Any | None:
    config = getattr(args, "zero_base_review_model_config", None)
    if not config:
        return None
    from .zero_base_review_launcher import ZeroBaseReviewLauncher

    return ZeroBaseReviewLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        model_config=config,
        policy_path=getattr(args, "zero_base_review_policy", None),
        scheduler_db=getattr(args, "scheduler", None),
    )


def argv_fragment(context: Any) -> list[str]:
    """On a Core that has a model configuration for it, and nowhere else.

    Deliberately gated on the configuration rather than on the grant: a plist
    is rendered once at install and a grant changes when the owner publishes a
    mission version, so a lane that vanished from the plist because of a grant
    would need a reinstall to come back. The grant is checked at dispatch,
    where the answer is reported (``ungranted``) instead of being absent.
    """

    config = context.state / REVIEW_MODEL_CONFIG
    if not config.is_file():
        return []
    argv = ["--zero-base-review-model-config", str(config)]
    policy = context.state / TRACKING_POLICY
    if policy.is_file():
        argv += ["--zero-base-review-policy", str(policy)]
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_zero_base_review",
    # Between the research-task lane (150) and the weekly reflection (160).
    # After everything that writes what it reads -- the judgement ledger, the
    # deliverable chain, the price series -- and *before* the reflection,
    # because the reflection reports the outcome counts this lane keeps fresh
    # and reading them one tick stale every week is avoidable for nothing.
    # The reflection stays last, which is its own lane's documented invariant.
    order=155,
    driver_key="zero_base_review",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    # The pool is declared once, in ``budget_pools.LANE_POOLS``; no lane in
    # this registry names its own, and C2's test holds that line.
    note="W4: once a month per covered company, and after each earnings "
         "calibration, ask from zero whether we would form this view today. "
         "Proposes ThesisRevisionCandidates through ADR-0007; decides nothing. "
         "Also keeps the derived no_change/revise outcome ledger fresh, which "
         "costs no model call.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionZeroBaseLaneCoordinator",
    "REVIEW_MODEL_CONFIG",
    "TRACKING_POLICY",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "may_write_review",
]
