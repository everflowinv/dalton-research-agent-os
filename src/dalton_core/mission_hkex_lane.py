"""W4: read what the covered Hong Kong issuers disclosed, once a trading day.

Queueless, like the price, calendar and ownership lanes: what needs reading is
derived every tick from the day and from what this process has already read,
and there is nothing to leave stuck.

**Why this lane exists at all.** Hong Kong is the only market that makes a
listed company report its own share buy-backs the next morning, every trading
day. That is the tape the owner asked for: management selling and companies
buying, seen the day after rather than a quarter later. The rest of this
connector rides along because it is the same issuer, the same politeness budget
and the same tick.

**The cadence, and where it comes from.** The owner's existing OpenClaw
filings-alert cron runs at 20:30 Hong Kong time, after the announcement window
closes, and Monday's run covers the weekend. A Next Day Disclosure Return is
filed by 08:30 on the next business day, so one pull per trading day after that
hour catches every buy-back; a Part XV notice is due within three business days
of the event, so the Disclosure of Interests window is read with a lookback
rather than for one day. Those two facts are :data:`BUYBACK_CADENCE_NOTE` and
:data:`DEFAULT_DI_LOOKBACK_DAYS`, and the brain may widen them.

**One child per tick, and the four operations take turns.** The buy-back tape
goes first every day because it is the one thing that is stale by tomorrow.

**The universe.** ``company:hk-secucode:<code>.HK`` and nothing else. Today the
mission universe holds only ``company:sec-cik:*`` names, so on the live Core
this lane reports ``idle`` with the reason and starts nothing -- which is the
correct behaviour, not a gap: admitting a Hong Kong name to coverage is an
owner decision and this slice does not make it.

**The grants.** ``observation`` because this lane learns dated facts about
covered companies, ``market_event`` because P14a's ledger is what it writes
them into. A mission granting one and not the other gets ``ungranted`` and no
child, every tick.

**Events, never claims.** Everything this lane produces is a typed
ResearchEvent through ``record_event``, handed in rather than imported. Nothing
it reads may become a figure, a Claim or a statement line; see
``hkex_filings_core.HKEX_GRADE``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget
from .lane_permission_control import record_controlled_failure
from .store import content_hash

WRITE_SCOPES: tuple[str, ...] = ("observation", "market_event")
MAX_FAILURE_DETAIL_CHARS = 500
MAX_FAILURES_PER_COMPANY = 3
# A Part XV notice is due within three business days of the event, so a window
# that only asked about today would miss two thirds of them. A fortnight is
# generous and costs nothing: the list is one page whatever the window.
DEFAULT_DI_LOOKBACK_DAYS = 14
# The announcement index window. Long enough that a Core which was off for a
# long weekend still sees the results announcement it missed.
DEFAULT_INDEX_LOOKBACK_DAYS = 7
BUYBACK_CADENCE_NOTE = (
    "A Next Day Disclosure Return is filed by 08:30 Hong Kong time on the "
    "business day after the purchase, and the Exchange publishes its "
    "aggregation that morning. One read per trading day after that hour is "
    "enough; reading it twice buys nothing and reading it before it buys a 404."
)
# How much of the tape one process remembers, for the pace comparison and so a
# day already read is not read again. Bounded because it is in memory: a
# restart re-reads the newest few, and a duplicate event is free by
# construction -- the event key is the row's hash.
MAX_REMEMBERED_ROWS = 400
MAX_REMEMBERED_KEYS = 4000


def _today(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(timezone.utc).date().isoformat()


def _back(day: str, days: int) -> str:
    from datetime import date as _date

    return (_date.fromisoformat(day) - timedelta(days=max(0, int(days)))).isoformat()


def hk_universe(mission: Mapping[str, Any]) -> list[dict[str, str]]:
    """The covered companies that are Hong Kong listings, in mission order.

    A company whose ref is not ``company:hk-secucode:<code>.HK`` is skipped
    rather than guessed at. There is no route from a US ticker to a Hong Kong
    stock code that does not involve asking somebody, and a lane that guessed
    one would file one company's buy-backs under another's name.
    """

    from .hkex_filings_core import ticker_for_company_ref

    rows: list[dict[str, str]] = []
    for item in mission.get("universe") or ():
        if not isinstance(item, Mapping):
            continue
        company_ref = str(item.get("company_ref") or "").strip()
        ticker = ticker_for_company_ref(company_ref)
        if ticker is None:
            continue
        rows.append({
            "company_ref": company_ref,
            "hk_ticker": ticker,
            "bootstrap_priority": str(item.get("bootstrap_priority") or "P9"),
        })
    rows.sort(key=lambda row: (row["bootstrap_priority"], row["hk_ticker"]))
    return rows


def missing_scopes(mission: Mapping[str, Any] | None) -> list[str]:
    """Which of this lane's two grants the mission withheld."""

    if not isinstance(mission, Mapping):
        return list(WRITE_SCOPES)
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return list(WRITE_SCOPES)
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return list(WRITE_SCOPES)
    held = set(scopes)
    return [scope for scope in WRITE_SCOPES if scope not in held]


class MissionHkexLaneCoordinator:
    """Settle the previous tick's child, record what it found, start one more."""

    def __init__(
        self,
        *,
        state_dir: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        record_event: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        di_lookback_days: int = DEFAULT_DI_LOOKBACK_DAYS,
        index_lookback_days: int = DEFAULT_INDEX_LOOKBACK_DAYS,
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.launcher = launcher
        self.mission = mission
        # P14a's writer, handed in. This lane never imports the event
        # authority: a lane that reached into it would make the two impossible
        # to land in either order.
        self.record_event = record_event
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.di_lookback_days = int(di_lookback_days)
        self.index_lookback_days = int(index_lookback_days)
        self._open: str | None = None
        self._open_item: str | None = None
        self._done: dict[tuple[str, str, str], None] = {}
        self.failure_budget = lane_budget(
            "hkex_filings", state_dir=failure_ledger_dir or state_dir,
            clock=self.clock, max_transient_failures=MAX_FAILURES_PER_COMPANY)
        self._emitted: set[str] = set()
        # (company_ref, operation) -> the rows already read, newest last. This
        # is what makes the derived context possible at all: the pace of a
        # buy-back and the trailing ninety days of a director's dealing are
        # both comparisons against what came before, and nothing else in this
        # process holds it.
        self.history: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # -- what is due -------------------------------------------------------

    def due(self, company: Mapping[str, str], today: str) -> dict[str, Any] | None:
        """The next read for one company, or None when it is current for today.

        Order is fixed rather than round-robin. The buy-back tape is the only
        one of the four that is stale tomorrow -- the report for a printed day
        stays where it is, but the *decision* it should inform does not -- so
        it is read first every day and the others fill the ticks after it.
        """

        from .hkex_filings_core import (
            ANNOUNCEMENTS_INDEX_OPERATION,
            DISCLOSURE_OF_INTERESTS_OPERATION,
            MONTHLY_RETURNS_OPERATION,
            NEXT_DAY_DISCLOSURE_OPERATION,
            TIER_ONE_ALL,
        )

        approved = frozenset(self.launcher.approved_operations())
        plans = (
            (NEXT_DAY_DISCLOSURE_OPERATION, {"as_of": today}),
            (ANNOUNCEMENTS_INDEX_OPERATION, {
                "since": _back(today, self.index_lookback_days), "until": today,
                "headline_category": TIER_ONE_ALL,
            }),
            (DISCLOSURE_OF_INTERESTS_OPERATION, {
                "since": _back(today, self.di_lookback_days), "until": today,
            }),
            (MONTHLY_RETURNS_OPERATION, {
                "since": _back(today, 45), "until": today,
            }),
        )
        for operation, parameters in plans:
            if operation not in approved:
                continue
            key = (company["company_ref"], operation, today)
            if key in self._done:
                continue
            return {"operation": operation, "parameters": parameters, "key": key}
        return None

    def _remember(self, key: tuple[str, str, str]) -> None:
        self._done[key] = None
        while len(self._done) > MAX_REMEMBERED_KEYS:
            self._done.pop(next(iter(self._done)))

    def _extend_history(
        self, company_ref: str, operation: str, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        held = self.history.setdefault((company_ref, operation), [])
        known = {row.get("record_hash") for row in held}
        for row in rows:
            if row.get("record_hash") in known:
                continue
            held.append(dict(row))
        del held[:-MAX_REMEMBERED_ROWS]

    # -- settling ----------------------------------------------------------

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now, try again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        reason = summary.get("failure_reason")
        return {
            "ticket_ref": ticket_ref,
            "status": ticket.get("status"),
            # From the ticket, not the summary: a child that died before
            # writing one is still attributable to what it was reading.
            "company_ref": ticket.get("company_ref"),
            "operation": ticket.get("operation"),
            "hk_ticker": ticket.get("hk_ticker"),
            "as_of": ticket.get("as_of"),
            "since": ticket.get("since"),
            "until": ticket.get("until"),
            "parsed_row_count": summary.get("parsed_row_count") or 0,
            "universe_row_count": summary.get("universe_row_count"),
            "record_count": summary.get("record_count"),
            "event_count": summary.get("event_count") or 0,
            "events_over_cap": summary.get("events_over_cap") or 0,
            "invocation_ref": summary.get("invocation_ref"),
            "caliber_notes": list(summary.get("caliber_notes") or []),
            "events": list(summary.get("events") or []),
            "rows": list(((summary.get("wire") or {}).get("rows")) or []),
            "failure_reason": (
                reason[:MAX_FAILURE_DETAIL_CHARS] if isinstance(reason, str) else None
            ),
        }

    def _settle_open(self, today: str) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        item_key, self._open_item = self._open_item, None
        company_ref = settled.get("company_ref")
        operation = settled.get("operation")
        if not company_ref or not operation:
            return settled
        if settled.get("status") != "succeeded":
            item_key = item_key or self._settled_key(settled)
            settled["failure"] = self.failure_budget.record_settled(item_key, settled).as_wire()
            # Not marked done: a failed run learned nothing, and marking it
            # would lose the day permanently on a transient error.
            return settled
        if item_key:
            settled["resumed"] = self.failure_budget.clear(item_key)
        self._remember((company_ref, operation, today))
        self._extend_history(company_ref, operation, settled["rows"])
        settled["emission"] = self._emit(company_ref, settled["events"])
        return settled

    def _governance_digest(self, operation: str) -> str:
        resolver = getattr(self.launcher, "governance_path", None)
        if resolver is None:
            return content_hash({"operation": operation,
                                 "approved": list(self.launcher.approved_operations())})
        path = resolver(operation)
        try:
            return content_hash({"path": str(path), "bytes": path.read_text("utf-8")})
        except Exception:  # noqa: BLE001
            return content_hash({"path": str(path), "state": "unreadable"})

    def _item_key(self, company_ref: str, operation: str,
                  parameters: Mapping[str, Any]) -> str:
        day = parameters.get("as_of") or parameters.get("until") or _today(self.clock)
        return f"{company_ref}|{operation}|input:" + content_hash({
            "company_ref": company_ref, "operation": operation, "day": day,
            "parameters": dict(parameters),
            "governance": self._governance_digest(operation),
        })[:24]

    def _settled_key(self, settled: Mapping[str, Any]) -> str:
        parameters = {key: settled.get(key) for key in
                      ("as_of", "since", "until") if settled.get(key)}
        return self._item_key(str(settled["company_ref"]),
                              str(settled["operation"]), parameters)

    def _retire_old_inputs(self, company_ref: str, operation: str, current: str) -> None:
        prefix = f"{company_ref}|{operation}|input:"
        for row in (self.failure_budget.parked_items()
                    + self.failure_budget.terminal_items()
                    + self.failure_budget.permission_items()):
            if row["item_key"].startswith(prefix) and not row["item_key"].startswith(current):
                self.failure_budget.retire(row["item_key"])

    # -- events ------------------------------------------------------------

    def _emit(
        self, company_ref: str, events: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Record what the child parsed, one event at a time.

        Marked emitted the instant the writer accepts each one, not after the
        batch: a report with twelve rows whose seventh raised would otherwise
        re-emit the first six on the next tick.
        """

        recorded: list[dict[str, Any]] = []
        for event in events:
            payload = event.get("payload") or {}
            key = payload.get("event_key")
            if key and key in self._emitted:
                continue
            if self.record_event is None:
                recorded.append(dict(payload))
                continue
            try:
                self.record_event(
                    company_ref=event.get("company_ref") or company_ref,
                    kind=event["kind"],
                    occurred_at=event["occurred_at"],
                    source_refs=list(event.get("source_refs") or []),
                    payload=payload,
                )
            except Exception as exc:  # noqa: BLE001 - one event, not the tick
                return {
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "recorded_count": len(recorded),
                    "emitted": recorded,
                }
            if key:
                self._emitted.add(key)
            recorded.append(dict(payload))
        return {
            "status": "recorded" if self.record_event is not None else "events_unwired",
            "reason": (
                None if self.record_event is not None else
                "no ResearchEvent writer is wired to this lane; the disclosures "
                "below were parsed and nothing recorded them"
            ),
            "recorded_count": len(recorded),
            "emitted": recorded,
        }

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        today = _today(self.clock)
        settled = self._settle_open(today)
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        withheld = missing_scopes(mission)
        if withheld:
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {', '.join(withheld)} in "
                    "autonomy.may_write; reading Hong Kong disclosure into the "
                    "event ledger without both grants is not something to work "
                    "around"
                ),
            }
        if not self.launcher.approved_operations():
            return {
                "status": "unconfigured", "settled": settled,
                "reason": (
                    "this writer holds no approved hkex-filings record; the four "
                    "operations are approved one at a time"
                ),
            }
        universe = hk_universe(mission)
        if not universe:
            return {
                "status": "idle", "settled": settled,
                "reason": (
                    "this mission covers no Hong Kong listing; the universe is "
                    "company:sec-cik: names and admitting a "
                    "company:hk-secucode: one is an owner decision"
                ),
            }
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "a Hong Kong disclosure child is still running"}
        skipped: list[dict[str, Any]] = []
        for company in universe:
            company_ref = company["company_ref"]
            plan = self.due(company, today)
            while plan is not None:
                item_key = self._item_key(
                    company_ref, plan["operation"], plan["parameters"])
                self._retire_old_inputs(company_ref, plan["operation"], item_key)
                blocked = self.failure_budget.blocked(item_key)
                if blocked is None:
                    break
                skipped.append({
                    "company_ref": company_ref, "reason": blocked.action,
                    "operation": plan["operation"],
                    "detail": blocked.classification.reason,
                })
                if blocked.action != "terminal":
                    plan = None
                    break
                self._remember(plan["key"])
                plan = self.due(company, today)
            if plan is None:
                if not any(row.get("company_ref") == company_ref for row in skipped):
                    skipped.append({"company_ref": company_ref, "reason": "current_today"})
                continue
            try:
                ticket = self.launcher.start(
                    operation=plan["operation"],
                    hk_ticker=company["hk_ticker"],
                    company_ref=company_ref,
                    **plan["parameters"],
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped,
                        "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                reason = f"{type(exc).__name__}: {exc}"
                decision = record_controlled_failure(
                    self.failure_budget, item_key, mission, self.launcher,
                    reason=reason, status="rejected")
                return {"status": "rejected", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped, "reason": reason,
                        "failure": decision.as_wire()}
            self._open = ticket["id"]
            self._open_item = item_key
            return {
                "status": "launched", "company_ref": company_ref,
                "operation": plan["operation"], "hk_ticker": company["hk_ticker"],
                "parameters": dict(plan["parameters"]),
                "ticket_ref": ticket["id"], "settled": settled, "skipped": skipped,
            }
        return {
            "status": "idle", "settled": settled, "skipped": skipped,
            "reason": "every covered Hong Kong issuer has been read today",
        }


# -- registration ----------------------------------------------------------

LAUNCHER_KWARG = "hkex_filings_launcher"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (W4).

    The coordinator is cached rather than rebuilt, because its held state is
    the point: which child is open, what has been read today, which companies
    are failing, which events have gone out -- and the row history the derived
    context is computed against. A fresh coordinator every tick would forget
    all five and re-read the same day for the life of the process.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no approved hkex-filings connector on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionHkexLaneCoordinator(
            state_dir=launcher.state_dir,
            launcher=launcher,
            mission=mission,
            record_event=getattr(server, "record_research_event", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--hkex-filings-governance-dir",
        help="directory holding the approved hkex-filings records",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "hkex_filings_governance_dir", None) is None:
        return None
    from pathlib import Path as _Path

    from .hkex_filings_launcher import HkexFilingsLauncher

    return HkexFilingsLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        governance_dir=_Path(args.hkex_filings_governance_dir).expanduser().resolve(),
    )


def argv_fragment(context: Any) -> list[str]:
    governance = context.state / "connector-governance"
    if not any((governance / name).is_file() for name in _governance_filenames()):
        return []
    return ["--hkex-filings-governance-dir", str(governance)]


def _governance_filenames() -> tuple[str, ...]:
    from .hkex_filings_launcher import GOVERNANCE_FILENAME_BY_OPERATION

    return tuple(GOVERNANCE_FILENAME_BY_OPERATION.values())


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_hkex_filings",
    # At the head of the daily tracking block (84-89: prices, tracking,
    # calendar, ownership, consensus). Not after the SEC ownership lane, which
    # would have been the obvious place: 89 is taken by consensus and lane
    # order is explicit rather than negotiated. First in the block rather than
    # last, because the buy-back tape is the only read here that is stale by
    # tomorrow -- the report for a printed day stays where it is, but the
    # decision it should inform does not.
    order=84,
    driver_key="mission_hkex_filings",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="W4: Hong Kong's daily share buy-back tape, the SFC's Disclosure of "
         "Interests notices, and the announcement index for the covered "
         "Hong Kong issuers.",
))


__all__ = [
    "BUYBACK_CADENCE_NOTE",
    "DEFAULT_DI_LOOKBACK_DAYS",
    "DEFAULT_INDEX_LOOKBACK_DAYS",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_FAILURE_DETAIL_CHARS",
    "MAX_REMEMBERED_KEYS",
    "MAX_REMEMBERED_ROWS",
    "WRITE_SCOPES",
    "MissionHkexLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "hk_universe",
    "missing_scopes",
]
