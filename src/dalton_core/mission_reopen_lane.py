"""P14d's weekly check: has the evidence under a passed screen thickened?

One pass a week over every company whose Initial Screen has passed, and the
whole of it is counting.  There is no model call here and no network call, so
this lane runs in the controller tick rather than in a child process: a child
would buy isolation from a failure mode this lane does not have, and would cost
a ticket, a spawn and a settle for work that is five ``SELECT COUNT(*)``\\s per
company.

Weekly rather than daily because the thing it measures moves at the pace of
filings and ingest runs, and because a proposal is a demand on the owner's
attention.  Idempotent twice over: on (company, ISO week) so a busy tick does
not redo the arithmetic, and -- the one that actually matters, because the
in-memory guard dies with the process -- on (company, assessment hash) in the
authority itself, so the same evidence base proposes exactly once however many
times anything looks at it.

It proposes and stops.  ``gate_reopen`` is a human checkpoint by construction
(ADR-0008): approving is what the cockpit and the writer are for, and re-issuing
is what the Initial Screen lane does once it sees an approval it has not spent.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .coverage_mission import fold_stage_status
from .lane_registry import LaneSpec, register_lane

SCHEMA_VERSION = "0.1"
LANE_STATE_KEY = "reopen_lane"
CHECKPOINT_KIND = "gate_reopen"
MAX_FAILURE_DETAIL_CHARS = 500


def may_propose_reopen(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission carries the checkpoint a reopen would be decided at.

    There is no ``may_write`` scope for a reopen and there should not be: the
    proposal is worth nothing without somebody to decide it, so the grant that
    matters is the *checkpoint*.  A mission without it would accumulate
    proposals nobody could answer.
    """

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    checkpoints = autonomy.get("human_checkpoints")
    if isinstance(checkpoints, (str, bytes)) or not isinstance(checkpoints, (list, tuple)):
        return False
    return CHECKPOINT_KIND in checkpoints


def passed_companies(connection: Any, mission: Mapping[str, Any]) -> list[str]:
    """Every company in this mission's universe whose screen has passed.

    In the mission's own priority order, so a week that is cut short by
    anything reads the same companies first that everything else does.

    P14-S: folded, so a company whose gate was already reopened is not offered
    for reopening a second time.  ``DISTINCT ... status='gate_passed'`` could
    not see that: a superseded pass is still a row.  Since the P14d sequel the
    reopen is a marker in its own ledger, so the fold is read through
    ``folded_stage_history`` -- otherwise an approved-but-not-yet-re-issued
    gate would look passed and this lane would propose re-opening it again.
    """

    from .deliverable_reopen import folded_stage_history

    members = [member["company_ref"] for member in mission.get("universe") or ()]
    if not members:
        return []
    return [
        member for member in members
        if fold_stage_status(
            folded_stage_history(connection, company_ref=member, stage_ref="initial_screen")
        ) == "gate_passed"
    ]


class MissionReopenLaneCoordinator:
    """Assess, propose, and say plainly why it did neither."""

    def __init__(
        self,
        *,
        mission: Callable[[], dict[str, Any] | None],
        connection: Any,
        authority: Any,
        policy: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.mission = mission
        self.connection = connection
        self.authority = authority
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._weeks: dict[str, str] = {}

    def week_ref(self, mission: Mapping[str, Any]) -> str:
        year, week, _day = self.clock().isocalendar()
        return f"{mission['id']}:{year}-W{week:02d}"

    def dispatch_once(self) -> dict[str, Any]:
        from .deliverable_reopen import DeliverableReopenError, reopen_assessment

        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no active mission",
                    "schema_version": SCHEMA_VERSION}
        if not may_propose_reopen(mission):
            return {
                "status": "ungranted", "schema_version": SCHEMA_VERSION,
                "reason": (
                    f"任务 {mission['id']} 的 autonomy.human_checkpoints 里没有 "
                    f"{CHECKPOINT_KIND}；提案没人裁决就不提（ADR-0008）"
                ),
            }
        week = self.week_ref(mission)
        companies = passed_companies(self.connection, mission)
        looked: list[dict[str, Any]] = []
        proposed: list[str] = []
        held = 0
        for company_ref in companies:
            if self._weeks.get(company_ref) == week:
                held += 1
                continue
            try:
                assessment = reopen_assessment(
                    self.connection, company_ref=company_ref, policy=self.policy
                )
            except DeliverableReopenError as exc:
                looked.append({
                    "company_ref": company_ref, "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS],
                })
                continue
            self._weeks[company_ref] = week
            entry = {
                "company_ref": company_ref,
                "status": assessment["status"],
                "flipped": list(assessment.get("flipped") or ()),
            }
            if assessment["status"] != "reopen_proposed":
                looked.append(entry)
                continue
            if self.authority.holds_assessment(
                company_ref=company_ref, assessment_hash=assessment["assessment_hash"]
            ):
                entry["status"] = "already_proposed"
                looked.append(entry)
                continue
            try:
                proposal = self.authority.propose(
                    assessment=assessment, mission=mission,
                    actor_ref=mission["autonomy"]["automation_principal"],
                )
            except DeliverableReopenError as exc:
                entry["status"] = "failed"
                entry["reason"] = f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]
                looked.append(entry)
                continue
            entry["status"] = proposal["status"]
            entry["proposal_ref"] = proposal["id"]
            if proposal["status"] == "fresh":
                proposed.append(proposal["id"])
            looked.append(entry)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "proposed" if proposed else ("held" if held and not looked else "idle"),
            "week_ref": week,
            "companies": len(companies),
            "held": held,
            "proposed": proposed,
            "looked": looked,
        }


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P14d).  Deterministic, cheap, and usually idle."""

    store = getattr(server, "store", None)
    if store is None:  # pragma: no cover - the writer always has one
        return {"status": "unconfigured", "reason": "no Core on this writer"}
    coordinator = server.lane_state.get(LANE_STATE_KEY)
    if coordinator is None:
        from .deliverable_reopen import GateReopenAuthority

        def mission() -> Any:
            pointer = store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionReopenLaneCoordinator(
            mission=mission,
            connection=store.connection,
            authority=GateReopenAuthority(store),
        )
        server.lane_state[LANE_STATE_KEY] = coordinator
    return coordinator.dispatch_once()


# ---------------------------------------------------------------------------
# the read-only CLI
# ---------------------------------------------------------------------------


def _print_assessment(assessment: Mapping[str, Any]) -> None:
    print(f"{assessment['company_ref']}  ->  {assessment['status']}")
    if assessment["status"] == "not_passed":
        print(f"   {assessment['reason']}")
        return
    print(
        f"   过闸那一版 v{assessment['passed_version_number']} "
        f"{assessment['passed_version_ref']}  发布于 {assessment['published_at']}"
    )
    for entry in assessment["diff"]:
        mark = "翻了" if entry["flipped"] else ("退了" if entry["regressed"] else "  ")
        print(
            f"   {mark} {entry['label']}: {entry['was']['mark']}({entry['was']['value']})"
            f" -> {entry['now']['mark']}({entry['now']['value']})  阈值 {entry['threshold']}"
            + (f"   # {entry['note']}" if entry["note"] else "")
        )


def main(argv: list[str] | None = None) -> int:
    """Run the assessment read-only and print the diff.  Writes nothing.

    This is the command the owner runs to answer "which of the passed screens
    would be re-proposed today, and on what", without the lane and without
    putting anything in front of themselves that they have to decide.
    """

    parser = argparse.ArgumentParser(
        description="P14d: re-run the Initial Screen evidence assessment and show the diff.",
    )
    parser.add_argument("--core", required=True, help="path to core.sqlite (opened read-only)")
    parser.add_argument("--policy", help="a JSON file of thresholds; defaults are frozen in code")
    parser.add_argument("--company", action="append", default=None,
                        help="restrict to one company_ref; repeatable")
    parser.add_argument("--json", action="store_true", help="print the assessments as JSON")
    args = parser.parse_args(argv)

    import sqlite3

    from .deliverable_reopen import load_policy, reopen_assessment

    policy = load_policy(args.policy)
    path = Path(args.core).expanduser().resolve()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if args.company:
            companies = list(args.company)
        else:
            # P14-S: folded, for the same reason ``passed_companies`` is --
            # a pass that a reopen superseded is not a pass -- and through the
            # same helper, so the CLI and the lane never disagree about which
            # gates are open.
            from .deliverable_reopen import folded_stage_history

            seen = {
                row["company_ref"] for row in connection.execute(
                    "SELECT DISTINCT company_ref FROM coverage_mission_stage_records "
                    "WHERE stage_ref='initial_screen'"
                ).fetchall()
            }
            companies = sorted(
                company for company in seen
                if fold_stage_status(
                    folded_stage_history(
                        connection, company_ref=company, stage_ref="initial_screen")
                ) == "gate_passed"
            )
        assessments = [
            reopen_assessment(connection, company_ref=company_ref, policy=policy)
            for company_ref in companies
        ]
    finally:
        connection.close()
    if args.json:
        print(json.dumps(assessments, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        for assessment in assessments:
            _print_assessment(assessment)
    return 0


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_reopen",
    # 118, between the judgement lane (116) and the S1 feed lanes (120). After
    # the judgement lane because both read the same week's arrivals and this
    # one should see the ones that lane just recorded; before the feed lanes
    # because it is cheap and deterministic and holding a fetch behind it
    # would be silly.
    order=118,
    driver_key="mission_reopen",
    handler=dispatch,
    note="P14d/ADR-0008: one evidence assessment a week per passed Initial "
         "Screen; proposes gate_reopen when an item flips 缺 to 有. No model "
         "call, no network, no child process -- so no budget pool and no "
         "launchagent fragment. Proposes; decides nothing.",
))


__all__ = [
    "CHECKPOINT_KIND",
    "LANE",
    "LANE_STATE_KEY",
    "MissionReopenLaneCoordinator",
    "SCHEMA_VERSION",
    "dispatch",
    "main",
    "may_propose_reopen",
    "passed_companies",
]


if __name__ == "__main__":
    sys.exit(main())
