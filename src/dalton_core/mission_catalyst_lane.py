"""C1: ask each covered company's diary once a day, and say what opened.

Queueless, like the price and specification lanes: what needs asking is derived
from the calendar every tick -- a company nobody has asked today -- so there is
nothing to leave stuck and a company already current is simply not chosen.

**Once a day, and that is the whole cadence.** An earnings date is announced
once and then stays true for three months. The plan's daily-tracking table puts
"SEC 8-K / earnings calendar" at *daily, fixed*, and this lane takes that
literally: one child per company per calendar day, which is five calls a day
against a free source that never agreed to serve us. Asking more often would
buy nothing and cost the goodwill the market layer runs on.

**Two grants, and they are not the same one.** Publishing the calendar needs
``observation`` in ``autonomy.may_write``; a mission without it gets
``ungranted`` and no child, every tick. Recording what the calendar opens is a
write to P14a's event ledger and needs *its* scope, ``market_event``, which the
ledger checks for itself. A mission with the first and not the second keeps a
correct calendar and records no events, and the tick says so
(``events_ungranted``) rather than counting it as a broken run: it is a
permission the owner has not granted yet, not a lane that is failing.

**The events.** When a confirmed date enters the P14f preview window (T-30) or
its calibration window (T+0..T+2), or when any date moves, this lane records a
``calendar`` ResearchEvent. It does that through a ``record_event`` callable
handed to the coordinator, never by importing P14a's module: that authority is
another slice's to own, and a lane that reached into it would make the two
impossible to land in either order. Without one wired, the events are computed
and reported in the tick summary as ``events_unwired`` -- visible, so nobody
discovers months later that the windows were opening into nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_failure_ledger import lane_budget
from .lane_registry import LaneSpec, register_lane

WRITE_SCOPE = "observation"
# How a company's SEC issuer number is recovered. The company ref *is* the
# mapping -- ``company:sec-cik:0001467373`` -- and there is no second table to
# keep in step with it. A company whose ref is not of this shape simply gets
# the vendor half, which is what a company with no SEC filings should get.
COMPANY_REF_CIK_PREFIX = "company:sec-cik:"
MAX_FAILURE_DETAIL_CHARS = 500
# A company whose runs keep failing stops consuming the single slot. Held in
# this process only: a restart is nearly always a deploy, which is the most
# likely thing to have fixed whatever it was.
MAX_FAILURES_PER_COMPANY = 3
# The tick-summary key, and therefore the name this lane is parked under.
DRIVER_KEY = "mission_catalyst_calendar"
# How far ahead the tick summary reports, so an operator can see the strip
# without opening the authority. Six weeks covers the preview window with room
# to see what is behind it.
SUMMARY_HORIZON_DAYS = 45


def _universe(mission: Mapping[str, Any]) -> list[dict[str, str]]:
    """The covered companies, in the order the mission prioritised them.

    A company with no ticker is skipped rather than guessed at: the vendor half
    of this lane is keyed by market symbol, and there is no mapping from a CIK
    to one that does not involve asking somebody.
    """

    rows: list[dict[str, str]] = []
    for item in mission.get("universe") or ():
        if not isinstance(item, Mapping):
            continue
        company_ref = item.get("company_ref")
        ticker = item.get("ticker")
        if not isinstance(company_ref, str) or not company_ref.strip():
            continue
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        company_ref = company_ref.strip()
        rows.append({
            "company_ref": company_ref,
            "ticker": ticker.strip().upper(),
            "issuer": issuer_for(company_ref) or "",
            "bootstrap_priority": str(item.get("bootstrap_priority") or "P9"),
        })
    rows.sort(key=lambda row: (row["bootstrap_priority"], row["ticker"]))
    return rows


def issuer_for(company_ref: str) -> str | None:
    """The SEC CIK inside a company ref, or None when there is not one."""

    if not isinstance(company_ref, str):
        return None
    if not company_ref.startswith(COMPANY_REF_CIK_PREFIX):
        return None
    cik = company_ref[len(COMPANY_REF_CIK_PREFIX):].strip()
    return cik if cik.isdigit() else None


def may_write_calendar(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission granted the automation the observation scope."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return False
    return WRITE_SCOPE in set(scopes)


class MissionCatalystLaneCoordinator:
    """Settle the previous tick's calendar child, then start at most one more."""

    def __init__(
        self,
        *,
        authority: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        record_event: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.authority = authority
        self.launcher = launcher
        self.mission = mission
        # P14a's writer, handed in. See the module note: this lane never
        # imports the event authority.
        self.record_event = record_event
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._open: str | None = None
        self._open_company: str | None = None
        # The day each company was last asked about. This is the cadence.
        self._asked: dict[str, str] = {}
        # P17d: the counted budget, the parked set and the terminal set, in the
        # lane common layer. Only ``transient`` failures spend the count; a
        # source that is down parks the company instead and releases it when a
        # read of that source works.
        self.budget = lane_budget(
            DRIVER_KEY, state_dir=failure_ledger_dir, clock=self.clock,
            max_transient_failures=MAX_FAILURES_PER_COMPANY)

    # -- the day -----------------------------------------------------------

    def _today(self) -> str:
        return self.clock().astimezone(timezone.utc).date().isoformat()

    def _permission_key(self, company_ref: str) -> str:
        resolver = getattr(self.launcher, "governance_identity", None)
        try:
            identity = resolver() if resolver is not None else "legacy"
        except Exception:
            identity = "invalid"
        return f"permission|{company_ref}|governance:{identity}"

    def _retire_legacy_permission(self, company_ref: str) -> None:
        blocked = self.budget.blocked(company_ref)
        if blocked is None or "yfinance calendar governance record is not approved" not in blocked.classification.reason:
            return
        loader = getattr(self.launcher, "load_governance", None)
        if loader is not None:
            try:
                loader()
            except Exception:
                return
            self.budget.retire(company_ref, reason="approved_governance_replaced_legacy_refusal")

    def due(self, company_ref: str) -> bool:
        """Has nobody asked about this company's diary today?"""

        return self._asked.get(company_ref) != self._today()

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
            # writing one is still attributable to the company it was for.
            "company_ref": ticket.get("company_ref"),
            "governance_hash": ticket.get("governance_hash"),
            "ticker": ticket.get("ticker"),
            "calendar_status": summary.get("calendar_status"),
            "calendar_version_ref": summary.get("calendar_version_ref"),
            "entry_count": summary.get("entry_count") or 0,
            "next_catalyst_date": summary.get("next_catalyst_date"),
            "change_reason": summary.get("change_reason"),
            "moved_entry_refs": list(summary.get("moved_entry_refs") or []),
            "published_change_count": summary.get("published_change_count") or 0,
            "sec_status": summary.get("sec_status"),
            "vendor_status": summary.get("vendor_status"),
            "sec_release_count": summary.get("sec_release_count") or 0,
            "failure_reason": (
                reason[:MAX_FAILURE_DETAIL_CHARS] if isinstance(reason, str) else None
            ),
        }

    def _settle_open(self) -> dict[str, Any] | None:
        """Read the child started last tick, and emit whatever it opened."""

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open, self._open_company = None, None
        company_ref = settled.get("company_ref")
        if not company_ref:
            return settled
        status = settled.get("status")
        if status not in ("succeeded", "partial"):
            settled["failure"] = self.budget.record_settled(
                company_ref, settled).as_wire()
            # Not marked as asked: a failed run learned nothing, and holding
            # the company for a day on the strength of it would mean a
            # transient error costs a day of the calendar.
            return settled
        if status == "partial":
            # The filed half published and the vendor half did not. The day
            # counts as asked, because the calendar did learn something and
            # asking again today would not fix Yahoo; the failure counts too,
            # because a vendor that stays broken must not hide behind a
            # calendar that keeps almost working.
            settled["failure"] = self.budget.record(
                company_ref,
                reason=settled.get("failure_reason") or "the vendor half failed",
                status="partial",
            ).as_wire()
        else:
            resumed = self.budget.clear(company_ref)
            if resumed:
                settled["resumed"] = resumed
        self._asked[company_ref] = self._today()
        settled["events"] = self._emit(company_ref, settled["moved_entry_refs"])
        return settled

    # -- events ------------------------------------------------------------

    def _emit(
        self, company_ref: str, moved_entry_refs: Sequence[str]
    ) -> dict[str, Any]:
        """Record the windows this company's calendar has open today.

        There is no de-duplication here and there used to be. The event
        ledger's identity is ``(company_ref, kind, payload_hash)``, so a window
        that stays open for a month produces the same payload every morning and
        the ledger answers ``duplicate`` and writes nothing. A set held in this
        process was a second answer to a question that already had one, and it
        was the worse answer: it forgot everything on restart, so the first
        tick after a deploy re-recorded every open window.
        """

        from .catalyst_calendar import emit_calendar_events  # noqa: PLC0415

        latest = self.authority.latest_version(company_ref)
        if latest is None:
            return {"status": "no_calendar", "emitted": []}
        if self.record_event is None:
            return {
                "status": "events_unwired", "emitted": [], "recorded_count": 0,
                "reason": (
                    "no ResearchEvent writer is wired to this lane; no window "
                    "this calendar opens is being recorded"
                ),
            }
        recorded: list[dict[str, Any]] = []

        def writer(**event: Any) -> Any:
            result = self.record_event(**event)
            recorded.append(event)
            return result

        try:
            emitted = emit_calendar_events(
                company_ref=company_ref,
                entries=latest["entries"],
                record_event=writer,
                now=self.clock(),
                version_ref=latest["id"],
                moved_entry_refs=moved_entry_refs,
            )
        except Exception as exc:  # noqa: BLE001 - one company, not the tick
            reason = f"{type(exc).__name__}: {exc}"
            # A mission that has not granted the event scope is the expected
            # state of a Core the owner has not published a new mission for. It
            # is not a broken lane, it is a missing permission, and calling it
            # a failure would spend this company's retry budget on it.
            status = "events_ungranted" if "may_write" in reason or (
                "does not grant" in reason) else "failed"
            return {
                "status": status, "reason": reason,
                # What did get written before it stopped, so a reader can tell
                # a run that recorded nothing from one that recorded half.
                "emitted": [event["payload"] for event in recorded],
                "recorded_count": len(recorded),
            }
        return {
            "status": "recorded",
            "emitted": emitted,
            "recorded_count": len(recorded),
            # The resting state of an open window: asked for, already there.
            "fresh_count": sum(
                1 for item in emitted if item.get("status") == "fresh"),
            "duplicate_count": sum(
                1 for item in emitted if item.get("status") == "duplicate"),
        }

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        if not may_write_calendar(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {WRITE_SCOPE} in "
                    "autonomy.may_write; publishing a catalyst calendar "
                    "without the grant is not something to work around"
                ),
            }
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "a calendar child is still running"}
        skipped: list[dict[str, Any]] = []
        for company in _universe(mission):
            company_ref = company["company_ref"]
            self._retire_legacy_permission(company_ref)
            permission_key = self._permission_key(company_ref)
            permission_blocked = self.budget.blocked(permission_key)
            business_blocked = self.budget.blocked(company_ref)
            blocked = permission_blocked or business_blocked
            if blocked is not None:
                # P17d: three words, not one. ``held`` will change on a deploy,
                # ``parked`` when the dependency answers, ``terminal`` never.
                skipped.append({
                    "company_ref": company_ref, "reason": blocked.action,
                    "detail": self.budget.failure_reason(permission_key if permission_blocked else company_ref),
                    "failure_class": blocked.classification.failure_class,
                    "dependency": blocked.classification.dependency,
                })
                continue
            if not self.due(company_ref):
                skipped.append({"company_ref": company_ref, "reason": "asked_today"})
                continue
            try:
                ticket = self.launcher.start(
                    company_ref=company_ref, ticker=company["ticker"],
                    issuer=company["issuer"] or None, as_of=self._today(),
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped,
                        "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                reason = f"{type(exc).__name__}: {exc}"
                decision = self.budget.record(permission_key, reason=reason)
                return {"status": "not_permitted" if decision.classification.awaits_permission else "rejected", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped, "reason": reason,
                        "failure": decision.as_wire()}
            self._open = ticket["id"]
            self._open_company = company_ref
            return {
                "status": "launched", "company_ref": company_ref,
                "ticker": company["ticker"], "issuer": company["issuer"] or None,
                "as_of": self._today(), "ticket_ref": ticket["id"],
                "settled": settled, "skipped": skipped,
            }
        return {
            "status": "idle", "settled": settled, "skipped": skipped,
            "reason": "every covered company's diary has been read today",
            "upcoming": self._upcoming(),
        }

    def _upcoming(self) -> list[dict[str, Any]]:
        """The strip an operator reads out of the tick summary."""

        try:
            found = self.authority.upcoming(self.clock(), SUMMARY_HORIZON_DAYS)
        except Exception:  # noqa: BLE001 - a summary line, not the tick
            return []
        return [
            {
                "company_ref": entry["company_ref"],
                "event_kind": entry["event_kind"],
                "expected_date": entry["expected_date"],
                "date_confidence": entry["confidence"],
                "date_caveat": entry["date_caveat"],
                "disagreement": entry["disagreement"],
                "days_until": entry["days_until"],
            }
            for entry in found
        ]


# -- registration ----------------------------------------------------------

LAUNCHER_KWARG = "catalyst_calendar_launcher"
# The approved record's filename under the live state's governance directory.
# Its presence is what turns the lane on: a Core without an approved calendar
# connector runs without one rather than failing to start.
CALENDAR_GOVERNANCE = "yfinance-calendar-v1.json"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (C1).

    One company's diary at a time. The lane has no queue: what needs asking is
    derived from the day, so a company already asked today is simply not
    chosen.

    The coordinator is cached rather than rebuilt, because its held state is
    the point: which child is open, which companies were asked today, which
    failed, and which event windows have already been recorded. A fresh
    coordinator every tick would forget all four and re-record every open
    preview window on every tick of the day.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no approved catalyst-calendar connector on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .catalyst_calendar import CatalystCalendarAuthority
        from .research_event import ResearchEventAuthority, record_event

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        # P14a's ledger, on the writer's own store, bound to the mission this
        # tick is running under.
        #
        # This used to read ``record_research_event`` off the server, which no
        # writer has ever had, so the attribute was always None and the lane
        # reported ``events_unwired`` on every tick of its life -- a fallback
        # that looked like wiring. The authority is built the same way this
        # lane builds the calendar authority, which is the shape every other
        # lane here uses and the one that cannot be absent by accident.
        events = ResearchEventAuthority(server.store)

        def record(**event: Any) -> Any:
            current = mission()
            if current is None:
                raise LaneChildRejected("no mission to record an event under")
            return record_event(
                events, mission=current,
                actor_ref=current["autonomy"]["automation_principal"],
                **event,
            )

        coordinator = MissionCatalystLaneCoordinator(
            authority=CatalystCalendarAuthority(server.store),
            launcher=launcher,
            mission=mission,
            record_event=record,
            # ``getattr``: a lane exercised against a stub writer has no
            # state directory, and a lane that refuses to run without a
            # ledger would be a lane that fails closed on its bookkeeping.
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--catalyst-calendar-governance",
        help="approved yfinance-calendar governance record",
    )


def build_launcher(args: Any) -> Any | None:
    if args.catalyst_calendar_governance is None:
        return None
    from pathlib import Path as _Path

    from .catalyst_calendar_launcher import CatalystCalendarLauncher

    return CatalystCalendarLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        governance_path=args.catalyst_calendar_governance,
    )


def argv_fragment(context: Any) -> list[str]:
    governance = context.state / "connector-governance" / CALENDAR_GOVERNANCE
    if not governance.is_file():
        return []
    return ["--catalyst-calendar-governance", str(governance)]


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_catalyst_calendar",
    # After the price lane and before the model specification. It shares the
    # price lane's source and its politeness budget, and it wants the tick's
    # single child slot less than a filing does; running it next to prices
    # keeps the two Yahoo calls of a day adjacent rather than scattered.
    order=87,
    driver_key="mission_catalyst_calendar",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="C1: when each covered company will next speak, from Yahoo's calendar "
         "and the company's own Item 2.02 8-K, and the P14f windows that open.",
))


__all__ = [
    "CALENDAR_GOVERNANCE",
    "COMPANY_REF_CIK_PREFIX",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_FAILURE_DETAIL_CHARS",
    "SUMMARY_HORIZON_DAYS",
    "WRITE_SCOPE",
    "MissionCatalystLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "issuer_for",
    "may_write_calendar",
]
