"""P11c-E: one company's valuation snapshot per tick, and silence in between.

Queueless, like the price and sensitivity lanes before it: what needs doing is
derived from the ledger every tick rather than written down and drained, so
there is nothing to get stuck and a company that is already priced simply is
not chosen.

What needs doing is one thing. A company whose price version, share
observation or filed role totals have moved since its last snapshot gets one
recomputed against the current inputs. Everything else is silence -- and the
silence is cheap twice over: a tick first compares one hashed row-count
signature over the four input tables and stops there when nothing has been
written, and only when something has does it assemble the inputs and compare
fingerprints. Five companies get five snapshots on the first afternoon, and
after that one company a day picks up yesterday's close.

It runs *after* the price lane (85) and the consensus lane (89) for the
obvious reason: a snapshot is arithmetic on a price, and pricing against a bar
the tick was about to replace produces a snapshot that is stale before it is
stored. It runs beside the other two derived-deterministic lanes, 95 and 96,
which is where the tick's free work lives.

**The grant.** This lane writes a valuation authority, so the mission has to
say it may: ``valuation`` in ``autonomy.may_write``. A mission that does not
grant it gets ``ungranted`` and no child, every tick, forever -- which is the
correct behaviour, not a bug to route around.

**No model call, no network.** The child is arithmetic over versions this Core
already holds. It costs no budget, cannot be refused by the router, and
produces the same bytes twice given the same inputs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_failure_ledger import lane_budget
from .lane_registry import LaneSpec, register_lane

WRITE_SCOPE = "valuation"
DRIVER_KEY = "valuation_snapshot"
LAUNCHER_KWARG = "valuation_snapshot_launcher"
MAX_FAILURE_DETAIL_CHARS = 500

# The tables a snapshot is made of. A tick whose signature matches the last
# scan's cannot have anything new to price, and stops after these six reads.
#
# ``coverage_mission_versions`` rather than ``coverage_mission_pointer``: a
# mission is republished to move the pointer, so the versions table sees every
# change the pointer does and is append-only, which the pointer is not.
INPUT_TABLES = (
    "market_price_series_versions",
    "consensus_estimate_versions",
    "coverage_mission_statement_filings",
    "coverage_mission_statement_lines",
    "coverage_mission_versions",
    "valuation_snapshot_versions",
)
# Never assemble inputs twice in five minutes. Assembling them means reading
# five price series in full and running the quarterly projection over eight
# filing dates -- a second of writer thread on the live Core -- and the price
# lane can move the signature several times an afternoon while producing the
# same snapshot every time.
MIN_SCAN_SECONDS = 300
# Assemble them at least once an hour regardless, so a lane that somehow read
# a stale signature repairs itself within the hour rather than at the next
# deploy. This is the lane's effective interval, and it is here rather than in
# ``service.json`` because a lane is driven by the controller tick and the
# service config has no per-lane block to put it in.
SCAN_INTERVAL_SECONDS = 3600


def ledger_signature(connection: Any) -> str:
    """One hash over the newest row of everything a snapshot is made of.

    ``MAX(rowid)`` rather than ``COUNT(*)``: every table here is append-only by
    trigger, so the newest rowid answers the same question, and it answers it
    with an index seek instead of a scan. The difference is not academic -- the
    counting version cost 240ms on the live Core's 17,000 statement lines, and
    this runs in the writer thread on every five-second tick.
    """

    parts: list[str] = []
    for table in INPUT_TABLES:
        try:
            row = connection.execute(
                f"SELECT MAX(rowid) newest FROM {table}").fetchone()
            parts.append(f"{table}:{row['newest']}")
        except Exception:  # noqa: BLE001 - a Core without the table has none
            parts.append(f"{table}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def may_write_valuation(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission granted the automation the valuation scope."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if isinstance(scopes, (str, bytes)) or not isinstance(scopes, (list, tuple)):
        return False
    return WRITE_SCOPE in set(scopes)


class ValuationSnapshotLaneCoordinator:
    """Settle the previous tick's child, then start at most one more."""

    def __init__(
        self,
        *,
        store: Any,
        missions: Any,
        prices: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        clock: Callable[[], datetime] | None = None,
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.store = store
        self.missions = missions
        self.prices = prices
        self.launcher = launcher
        self.mission = mission
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._open: str | None = None
        # What the ledger looked like when the inputs were last assembled, and
        # when that was. Held in this process only, like every other hold in
        # these lanes: a restart is nearly always a deploy.
        self._scanned_signature: str | None = None
        self._scanned_at: datetime | None = None
        # Runs that failed, keyed by company and fingerprint, so a company
        # whose snapshot cannot be built does not consume the slot every tick.
        self.budget = lane_budget(DRIVER_KEY, state_dir=failure_ledger_dir,
                                  clock=self.clock)

    # -- settling ----------------------------------------------------------

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
            "company_ref": ticket.get("company_ref"),
            "fingerprint": ticket.get("fingerprint"),
            "snapshot_status": summary.get("snapshot_status"),
            "snapshot_ref": summary.get("snapshot_ref"),
            "snapshot_version": summary.get("snapshot_version"),
            "available_metric_count": summary.get("available_metric_count"),
            "missing_inputs": summary.get("missing_inputs"),
            "price_bar_date": summary.get("price_bar_date"),
            "consensus_version_ref": summary.get("consensus_version_ref"),
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
        company_ref = settled.get("company_ref")
        fingerprint = settled.get("fingerprint")
        if not company_ref or not fingerprint:
            return settled
        key = f"{company_ref}|{fingerprint}"
        status = str(settled.get("snapshot_status") or "")
        # ``refused:`` is a grant or a universe answer and ``unavailable:`` is
        # the authority declining the arithmetic. Both are runs that succeeded
        # and published nothing, and retrying the identical fingerprint every
        # tick would decline the identical way until an input moves.
        failed = settled.get("status") != "succeeded" or status.startswith(
            ("refused:", "unavailable:"))
        if failed:
            settled["failure"] = self.budget.record(
                key,
                reason=str(settled.get("failure_reason")
                           or f"上一次运行：{status or settled.get('status')}")[
                               :MAX_FAILURE_DETAIL_CHARS],
            ).as_wire()
        else:
            resumed = self.budget.clear(key)
            if resumed:
                settled["resumed"] = resumed
        return settled

    # -- the tick ----------------------------------------------------------

    def _should_scan(self, signature: str) -> bool:
        if self._scanned_at is None:
            return True
        elapsed = (self.clock() - self._scanned_at).total_seconds()
        if elapsed >= SCAN_INTERVAL_SECONDS:
            return True
        if elapsed < MIN_SCAN_SECONDS:
            return False
        return signature != self._scanned_signature

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "上一个估值快照子进程还在运行"}
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "settled": settled,
                    "reason": "这台 Core 上还没有 coverage mission"}
        if not may_write_valuation(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"这份 mission 的 autonomy.may_write 没有 {WRITE_SCOPE}；"
                    "没有授权就发布公司值多少钱，不是可以绕过去的事"
                ),
            }
        signature = ledger_signature(self.store.connection)
        if not self._should_scan(signature):
            return {"status": "idle", "settled": settled,
                    "reason": "三个输入权威自上次组装以来没有新版本"}
        from .valuation_snapshot_cli import scan

        try:
            rows = scan(self.store, self.missions, self.prices, mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._scanned_signature = signature
        self._scanned_at = self.clock()
        skipped = [{"company_ref": row["company_ref"], "reason": row["action"],
                    "detail": row["reason"]}
                   for row in rows if row["action"] != "publish"]
        pending = [row for row in rows if row["action"] == "publish"]
        held: dict[str, str] = {}
        chosen = None
        for row in pending:
            key = f"{row['company_ref']}|{row['inputs']['fingerprint']}"
            decision = self.budget.blocked(key)
            if decision is not None:
                held[row["company_ref"]] = self.budget.failure_reason(key) or (
                    decision.classification.reason)
                continue
            chosen = row
            break
        if chosen is None:
            if held:
                return {"status": "held", "settled": settled, "held": held,
                        "skipped": skipped,
                        "reason": "；".join(
                            f"{ref}：{why}" for ref, why in held.items())}
            return {"status": "idle", "settled": settled, "skipped": skipped,
                    "reason": "每家公司的估值快照都与当前输入一致"}
        company_ref = chosen["company_ref"]
        fingerprint = chosen["inputs"]["fingerprint"]
        try:
            ticket = self.launcher.start(company_ref=company_ref,
                                         fingerprint=fingerprint)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref,
                    "settled": settled, "skipped": skipped,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "skipped": skipped,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "fingerprint": fingerprint, "ticket_ref": ticket["id"],
            "missing_inputs": [row["input"]
                               for row in chosen["inputs"]["input_coverage"]
                               if row["status"] == "missing"],
            "held": held, "skipped": skipped, "settled": settled,
        }


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P11c-E).

    One company's valuation snapshot at a time, and only when one of its
    inputs has moved. It has no queue, no model call and no network, so with
    nothing to do it costs one COUNT query.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "这台 writer 上没有安装估值快照车道"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .market_price import MarketPriceSeriesAuthority

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(
                        pointer["mission_version_id"]))

        coordinator = ValuationSnapshotLaneCoordinator(
            store=server.store,
            missions=server.coverage_mission,
            # Built here rather than passed in: constructing an authority is
            # what installs its schema, and the writer has no other reason to
            # know this lane reads prices.
            prices=MarketPriceSeriesAuthority(server.store),
            launcher=launcher,
            mission=mission,
            # ``getattr``: a lane exercised against a stub writer has no state
            # directory, and a lane that refused to run without a ledger would
            # be a lane failing closed on its own bookkeeping.
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # A flag rather than a path: this child makes no model call, reaches no
    # network and reads no configuration, so there is nothing to point it at.
    parser.add_argument("--valuation-snapshot-lane", action="store_true")


def build_launcher(args: Any) -> Any | None:
    if not getattr(args, "valuation_snapshot_lane", False):
        return None
    from pathlib import Path as _Path

    from .valuation_snapshot_launcher import ValuationSnapshotLauncher

    return ValuationSnapshotLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent)


def argv_fragment(context: Any) -> list[str]:
    # Every lane's fragment is gated on the thing that lane needs being on
    # disk. This one needs no connector, no model and no configuration, so it
    # is gated on the one thing it genuinely cannot work without: a Core.
    if not (context.state / "core.sqlite").is_file():
        return []
    return ["--valuation-snapshot-lane"]


LANE = register_lane(LaneSpec(
    operation="dispatch_valuation_snapshot",
    # 95 is the driver model, 96 the sensitivity table it projects; 97 is what
    # the shares actually trade at against the filings, which wants the same
    # free-work slot and none of the tick's children.
    order=97,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P11c-E: what each covered company trades at, computed from its own "
         "price series and its own filings, with the fourth quarter derived "
         "and labelled and every missing input named on the record.",
))


__all__ = [
    "DRIVER_KEY",
    "INPUT_TABLES",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MIN_SCAN_SECONDS",
    "SCAN_INTERVAL_SECONDS",
    "WRITE_SCOPE",
    "ValuationSnapshotLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "ledger_signature",
    "may_write_valuation",
]
