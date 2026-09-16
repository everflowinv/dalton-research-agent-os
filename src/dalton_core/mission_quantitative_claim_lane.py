"""C2-4: promote the numbers this install already holds, once a tick.

Queueless, like the claim-index lane: what still needs promoting is *derived*
-- every filed statement line and verified figure, minus what
``quantitative_claim_promotions`` already covers -- so it is computed each tick
rather than written down somewhere that can go stale.  The resting state is
silence: once the backlog is promoted the lane does nothing until a new filing
lands or the figures pass verifies another number.

The lane spends no money.  It calls no model, opens no broker socket and holds
no budget: it re-reads filed rows and writes down arithmetic.  That is why it
has no failure budget and no permission control, and why it is safe to let it
run on every tick.
"""

from __future__ import annotations

from typing import Any

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

LAUNCHER_KWARG = "quantitative_claim_promotion_launcher"
DRIVER_KEY = "quantitative_claim_promotion"
MAX_FAILURE_DETAIL_CHARS = 500


class QuantitativeClaimPromotionCoordinator:
    """Launch and settle the deterministic promoter."""

    def __init__(self, *, launcher: Any, limit: int | None = None) -> None:
        self.launcher = launcher
        self.limit = limit
        self._ticket: str | None = None

    def dispatch_once(self) -> dict[str, Any]:
        if self._ticket is not None:
            try:
                ticket = self.launcher.status(self._ticket)
            except LaneChildTicketNotFound:
                ticket = None
            if ticket is not None:
                if ticket["status"] == "running":
                    return {"status": "busy", "ticket_ref": ticket["id"]}
                self._ticket = None
                summary = ticket.get("summary") or {}
                settled = {
                    "ticket_ref": ticket["id"],
                    "child_status": summary.get("status") or ticket["status"],
                    "promoted": summary.get("promoted"),
                    "duplicates": summary.get("duplicates"),
                    "held_totals": summary.get("held_totals"),
                }
                reason = summary.get("failure_reason")
                if summary.get("status") == "held":
                    # The mission has not granted what this lane writes with.
                    # A held lane says exactly what the owner would have to
                    # grant, because nothing else in the system will.
                    return {"status": "ungranted", "reason": str(reason or "")[:MAX_FAILURE_DETAIL_CHARS],
                            "last": settled}
                if summary.get("status") not in ("succeeded", "idle"):
                    return {"status": "failed",
                            "reason": str(reason or "")[:MAX_FAILURE_DETAIL_CHARS],
                            "last": settled}
                return {"status": "idle", "last": settled}
        try:
            ticket = self.launcher.start(limit=self.limit)
        except LaneChildConflict as exc:
            return {"status": "busy", "reason": str(exc)[:MAX_FAILURE_DETAIL_CHARS]}
        except (LaneChildRejected, ValueError) as exc:
            return {"status": "rejected", "reason": str(exc)[:MAX_FAILURE_DETAIL_CHARS]}
        self._ticket = ticket["id"]
        return {"status": "launched", "ticket_ref": ticket["id"]}


def dispatch(server: Any, params: Any) -> dict[str, Any]:
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no quantitative claim promotion lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = QuantitativeClaimPromotionCoordinator(launcher=launcher)
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--quantitative-claim-promotion",
        action="store_true",
        help="promote filed statement lines and verified figures into quantitative Claims",
    )
    parser.add_argument(
        "--quantitative-claim-promotion-limit", type=int, default=200,
        help="numbers examined per run (1..5000)",
    )


def build_launcher(args: Any) -> Any | None:
    if not getattr(args, "quantitative_claim_promotion", False):
        return None
    from pathlib import Path as _Path

    from .quantitative_claim_promotion_launcher import QuantitativeClaimPromotionLauncher

    return QuantitativeClaimPromotionLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        candidate_staging=getattr(args, "candidate_staging", None),
        limit=int(getattr(args, "quantitative_claim_promotion_limit", 200) or 200),
    )


def argv_fragment(context: Any) -> list[str]:
    """On when the install holds filed statements to promote.

    The switch is the SEC statements lane's own output directory rather than a
    model configuration, because this lane needs no model -- what it needs is
    something to read.
    """

    if not (context.state / "core.sqlite").is_file():
        return []
    return ["--quantitative-claim-promotion"]


LANE = register_lane(LaneSpec(
    operation="dispatch_quantitative_claim_promotion",
    # Before the claim index (105): the index tags Claims, and a Claim has to
    # exist before it can be tagged.
    order=99,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="C2-4: filed statement lines and verified figures become quantitative "
         "Claims deterministically, each anchored to the row it came from.",
))


__all__ = [
    "DRIVER_KEY",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "QuantitativeClaimPromotionCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
