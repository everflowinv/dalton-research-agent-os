"""S5: read the ownership filings each covered company gathered, once a day.

Queueless, like the price, calendar and specification lanes: what needs reading
is derived every tick from the filings index already on this disk, and a filing
this Core has already read is simply not chosen. There is nothing to leave
stuck.

**Where the work comes from, and why it costs nothing to find.** C1 established
that the governed ``list_filings`` call returns the issuer's *whole*
``filings.recent`` block and that the raw body is spooled and hashed. So every
Form 4, 13D/G and 144 these companies have filed is already here, unparsed, and
choosing what to read next is a read of local bytes with no SEC call in it.
Only fetching a chosen filing's primary document costs a call, and that call is
one of the four governed operations.

**One filing per tick.** A Form 4 is a small document and a company files one
or two a day; live, the five covered issuers filed 234, 172, 43, 87 and 43 of
them in the last twelve months, which is about two a day across the whole
universe. One child per tick clears that with room to spare and keeps this lane
from ever being the reason SEC starts refusing.

**The grants.** Two, and they are different permissions. ``observation``
because this lane learns dated facts about covered companies;
``market_event`` because P14a's ledger is what it writes them into. A mission
granting one and not the other gets ``ungranted`` and no child, every tick,
which is correct rather than something to route around.

**The IR watcher rides along.** Once a day, after the filings, the lane asks
the local changedetection.io what moved on the declared IR pages. It is in this
lane rather than its own because it is the same job -- what happened to this
company today that nobody filed a statement about -- and because a lane whose
launcher is absent on almost every Core should not be a second entry in the
tick summary that says ``unconfigured`` forever.

**Events, never claims.** Everything this lane produces is a typed
ResearchEvent through P14a's ``record_event``, handed in rather than imported.
Nothing it reads may become a figure, a Claim or a statement line; see
``sec_ownership_core.OWNERSHIP_GRADE``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

# Both are required. The first authorises learning a dated fact about a covered
# company; the second authorises writing into the event ledger. They are not
# the same permission and a mission that grants one is not granting the other.
WRITE_SCOPES: tuple[str, ...] = ("observation", "market_event")
COMPANY_REF_CIK_PREFIX = "company:sec-cik:"
MAX_FAILURE_DETAIL_CHARS = 500
MAX_FAILURES_PER_COMPANY = 3
# How far back a first run reaches. A quarter: enough that a Core turned on
# today sees the current quarter's insider activity, short enough that it does
# not spend a week reading three years of Form 4s nobody asked about.
DEFAULT_LOOKBACK_DAYS = 90
# The most filings one company contributes to the queue in one tick. The queue
# is rebuilt from local bytes every tick, so this bounds the work of *choosing*
# and nothing else.
MAX_CANDIDATES_PER_COMPANY = 25
# How many accessions this process remembers having read. Bounded because the
# set is in memory: a restart re-reads the newest few, which costs a handful of
# duplicate events -- and a duplicate event is free, by construction.
MAX_REMEMBERED_ACCESSIONS = 4000


def issuer_for(company_ref: str) -> str | None:
    """The SEC CIK inside a company ref, or None when there is not one."""

    if not isinstance(company_ref, str):
        return None
    if not company_ref.startswith(COMPANY_REF_CIK_PREFIX):
        return None
    cik = company_ref[len(COMPANY_REF_CIK_PREFIX):].strip()
    return cik if cik.isdigit() else None


def _universe(mission: Mapping[str, Any]) -> list[dict[str, str]]:
    """The covered companies with a CIK, in the order the mission prioritised.

    A company with no CIK is skipped rather than guessed at: everything this
    lane reads is keyed by one, and there is no route from a ticker to a CIK
    that does not involve asking somebody.
    """

    rows: list[dict[str, str]] = []
    for item in mission.get("universe") or ():
        if not isinstance(item, Mapping):
            continue
        company_ref = item.get("company_ref")
        if not isinstance(company_ref, str) or not company_ref.strip():
            continue
        company_ref = company_ref.strip()
        issuer = issuer_for(company_ref)
        if issuer is None:
            continue
        rows.append({
            "company_ref": company_ref,
            "ticker": str(item.get("ticker") or "").strip().upper(),
            "issuer": issuer,
            "bootstrap_priority": str(item.get("bootstrap_priority") or "P9"),
        })
    rows.sort(key=lambda row: (row["bootstrap_priority"], row["ticker"]))
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


class MissionOwnershipLaneCoordinator:
    """Settle the previous tick's child, record what it found, start one more."""

    def __init__(
        self,
        *,
        connection: Any,
        state_dir: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        record_event: Callable[..., Any] | None = None,
        ir_watch: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        self.connection = connection
        self.state_dir = state_dir
        self.launcher = launcher
        self.mission = mission
        # P14a's writer, handed in. This lane never imports the event
        # authority: that slice is another agent's to own and a lane that
        # reached into it would make the two impossible to land in either
        # order.
        self.record_event = record_event
        # S5's own IR watcher, handed in for the same reason: it needs the
        # writer's connector store and spool, which a lane module has no
        # business constructing.
        self.ir_watch = ir_watch
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lookback_days = int(lookback_days)
        self._open: str | None = None
        self._read: dict[str, None] = {}
        self._failures: dict[str, int] = {}
        self._failure_reason: dict[str, str] = {}
        self._emitted: set[str] = set()
        self._ir_watched_on: str | None = None

    # -- the day -----------------------------------------------------------

    def _today(self) -> str:
        return self.clock().astimezone(timezone.utc).date().isoformat()

    def _remember(self, accession: str) -> None:
        self._read[accession] = None
        while len(self._read) > MAX_REMEMBERED_ACCESSIONS:
            self._read.pop(next(iter(self._read)))

    # -- what is due -------------------------------------------------------

    def candidates(self, company: Mapping[str, str]) -> dict[str, Any]:
        """The filings this company has that this Core has not read.

        Zero network calls: the answer comes out of the spooled ``list_filings``
        body the discovery lane already wrote. A company whose discovery run has
        not happened yet gets a reason, not an exception.
        """

        from .sec_ownership_core import ownership_filings_for_issuer

        approved = frozenset(self.launcher.approved_operations())
        found = ownership_filings_for_issuer(
            self.connection, self.state_dir,
            issuer=company["issuer"], today=self._today(),
            lookback_days=self.lookback_days,
            limit=MAX_CANDIDATES_PER_COMPANY * 4,
        )
        if found["status"] != "read":
            return {"status": found["status"], "reason": found.get("reason"),
                    "filings": [], "unapproved": 0}
        unapproved = 0
        due: list[dict[str, Any]] = []
        for row in found["filings"]:
            if row["operation"] is None:
                continue
            if row["operation"] not in approved:
                unapproved += 1
                continue
            if row["accession"] in self._read:
                continue
            due.append(row)
        return {
            "status": "read", "reason": None,
            "filings": due[:MAX_CANDIDATES_PER_COMPANY],
            "unapproved": unapproved,
            "artifact_hash": found.get("artifact_hash"),
            "invocation_ref": found.get("invocation_ref"),
        }

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
            "accession": ticket.get("accession"),
            "form_type": ticket.get("form_type"),
            "parsed_row_count": summary.get("parsed_row_count") or 0,
            "event_count": summary.get("event_count") or 0,
            "events_over_cap": summary.get("events_over_cap") or 0,
            "comparison_status": summary.get("comparison_status"),
            "prior_quarter": summary.get("prior_quarter"),
            "value_unit": summary.get("value_unit"),
            "value_unit_basis": summary.get("value_unit_basis"),
            "invocation_ref": summary.get("invocation_ref"),
            "events": list(summary.get("events") or []),
            "failure_reason": (
                reason[:MAX_FAILURE_DETAIL_CHARS] if isinstance(reason, str) else None
            ),
        }

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        company_ref = settled.get("company_ref")
        if not company_ref:
            return settled
        if settled.get("status") != "succeeded":
            self._failures[company_ref] = self._failures.get(company_ref, 0) + 1
            self._failure_reason[company_ref] = (
                settled.get("failure_reason") or f"last run: {settled.get('status')}"
            )
            # Not marked as read: a failed run learned nothing, and marking the
            # accession would lose the filing permanently on a transient error.
            return settled
        self._failures.pop(company_ref, None)
        self._failure_reason.pop(company_ref, None)
        if settled.get("accession"):
            self._remember(settled["accession"])
        settled["emission"] = self._emit(company_ref, settled["events"])
        return settled

    # -- events ------------------------------------------------------------

    def _emit(
        self, company_ref: str, events: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Record what the child parsed, one event at a time.

        Marked emitted the instant the writer accepts each one, not after the
        batch: a filing with twelve transactions whose seventh raised would
        otherwise re-emit the first six on the next tick.
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
                    company_ref=company_ref,
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
                "no ResearchEvent writer is wired to this lane; the filings "
                "below were parsed and nothing recorded them"
            ),
            "recorded_count": len(recorded),
            "emitted": recorded,
        }

    # -- the IR pages ------------------------------------------------------

    def _watch_ir_pages(self, mission: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the local watcher what moved, once a day, and record it."""

        if self.ir_watch is None:
            return {"status": "unconfigured",
                    "reason": "no changedetection watcher on this writer"}
        if self._ir_watched_on == self._today():
            return {"status": "watched_today"}
        try:
            found = self.ir_watch()
        except Exception as exc:  # noqa: BLE001 - one source, not the tick
            return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
        self._ir_watched_on = self._today()
        recorded: list[dict[str, Any]] = []
        for change in found.get("changes") or []:
            key = change["payload"].get("event_key")
            if key and key in self._emitted:
                continue
            if self.record_event is not None:
                try:
                    self.record_event(
                        company_ref=change["company_ref"],
                        kind="ir_page_change",
                        occurred_at=change["occurred_at"],
                        source_refs=list(change.get("source_refs") or []),
                        payload=change["payload"],
                    )
                except Exception as exc:  # noqa: BLE001
                    return {
                        "status": "failed", "reason": f"{type(exc).__name__}: {exc}",
                        "recorded_count": len(recorded),
                        "undeclared_count": found.get("undeclared_count", 0),
                    }
                if key:
                    self._emitted.add(key)
            recorded.append(change["payload"])
        return {
            "status": found.get("status", "watched"),
            "reason": found.get("reason"),
            "watch_count": found.get("watch_count", 0),
            # An operator wants to know the shared tool is watching pages this
            # connector will not read. Counted here rather than logged, because
            # a count in the tick summary is a thing somebody sees.
            "undeclared_count": found.get("undeclared_count", 0),
            "recorded_count": len(recorded),
            "changes": recorded,
        }

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
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
                    "autonomy.may_write; reading ownership filings into the "
                    "event ledger without both grants is not something to "
                    "work around"
                ),
            }
        if not self.launcher.approved_operations():
            return {
                "status": "unconfigured", "settled": settled,
                "reason": (
                    "this writer holds no approved sec ownership record; the "
                    "four operations are approved one at a time"
                ),
            }
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "an ownership child is still running"}
        # Imported here rather than at module scope: a lane module is imported
        # while the registry is loading, and its expensive imports stay inside
        # the handler for the reason ``lane_registry`` spells out.
        from .sec_ownership_core import FORM13F_OPERATION

        skipped: list[dict[str, Any]] = []
        for company in _universe(mission):
            company_ref = company["company_ref"]
            if self._failures.get(company_ref, 0) >= MAX_FAILURES_PER_COMPANY:
                skipped.append({
                    "company_ref": company_ref, "reason": "held",
                    "detail": self._failure_reason.get(company_ref, "repeated failures"),
                })
                continue
            found = self.candidates(company)
            if found["status"] != "read":
                skipped.append({
                    "company_ref": company_ref, "reason": found["status"],
                    "detail": found.get("reason"),
                })
                continue
            if not found["filings"]:
                skipped.append({
                    "company_ref": company_ref, "reason": "nothing_new",
                    "unapproved": found["unapproved"],
                })
                continue
            filing = found["filings"][0]
            # A 13F in a covered company's *own* submissions index is one the
            # company filed as a manager -- Accenture files seven of them --
            # so the filing manager is that company. Reading the 13Fs of the
            # institutions that hold it is a different question and needs a
            # declared holder list, which nobody has written yet; see the S5
            # report's open questions.
            holder = (
                company["issuer"] if filing["operation"] == FORM13F_OPERATION else None
            )
            try:
                ticket = self.launcher.start(
                    operation=filing["operation"],
                    company_ref=company_ref,
                    accession=filing["accession"],
                    form_type=filing["form"],
                    issuer=None if holder else company["issuer"],
                    holder_cik=holder,
                    filed_at=filing["filing_date"],
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped,
                        "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._failures[company_ref] = self._failures.get(company_ref, 0) + 1
                self._failure_reason[company_ref] = reason
                return {"status": "rejected", "company_ref": company_ref,
                        "settled": settled, "skipped": skipped, "reason": reason}
            self._open = ticket["id"]
            return {
                "status": "launched", "company_ref": company_ref,
                "operation": filing["operation"], "accession": filing["accession"],
                "form_type": filing["form"], "filed_at": filing["filing_date"],
                "ticket_ref": ticket["id"], "settled": settled, "skipped": skipped,
                "pending_count": len(found["filings"]),
            }
        return {
            "status": "idle", "settled": settled, "skipped": skipped,
            "reason": "every ownership filing in the window has been read",
            "ir_pages": self._watch_ir_pages(mission),
        }


# -- registration ----------------------------------------------------------

LAUNCHER_KWARG = "sec_ownership_launcher"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (S5).

    The coordinator is cached rather than rebuilt, because its held state is
    the point: which child is open, which accessions have been read, which
    companies are failing, and which events have already gone out. A fresh
    coordinator every tick would forget all four and re-read the same filing
    for the life of the process.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no approved sec ownership connector on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionOwnershipLaneCoordinator(
            connection=server.store.connection,
            state_dir=launcher.state_dir,
            launcher=launcher,
            mission=mission,
            record_event=getattr(server, "record_research_event", None),
            ir_watch=build_ir_watch(server, launcher),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def build_ir_watch(server: Any, launcher: Any) -> Callable[[], dict[str, Any]] | None:
    """A callable that runs one IR-watch sweep, or None when it cannot.

    Built here rather than inside the coordinator because it needs the
    writer's connector store, observability and spool -- the three things a
    lane module must not construct for itself -- and because a Core without an
    approved watcher should get ``None`` and a tick summary that says so.
    """

    declaration = getattr(launcher, "ir_declaration_path", None)
    governance = getattr(launcher, "ir_governance_dir", None)
    if declaration is None or governance is None:
        return None
    connectors = getattr(server, "_connectors", None)
    spool = getattr(server, "_transcript_spool", None)
    if connectors is None or spool is None:
        return None

    def watch() -> dict[str, Any]:
        from .ir_page_watch_core import sweep_ir_pages

        return sweep_ir_pages(
            store=server.store, connectors=connectors,
            observability=server.observability, spool=spool,
            declaration_path=declaration, governance_dir=governance,
            state_dir=launcher.state_dir,
        )

    return watch


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--sec-ownership-governance-dir",
        help="directory holding the approved sec ownership records",
    )
    parser.add_argument(
        "--ir-page-declaration",
        help="the declared investor-relations pages, per company",
    )


def build_launcher(args: Any) -> Any | None:
    if args.sec_ownership_governance_dir is None:
        return None
    from pathlib import Path as _Path

    from .sec_ownership_launcher import SecOwnershipLauncher

    governance = _Path(args.sec_ownership_governance_dir).expanduser().resolve()
    launcher = SecOwnershipLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        governance_dir=governance,
    )
    declaration = getattr(args, "ir_page_declaration", None)
    launcher.ir_declaration_path = (
        None if declaration is None else _Path(declaration).expanduser().resolve()
    )
    # The watcher's two approvals live beside the ownership ones. Absent means
    # the watcher is off, which is what most Cores are.
    launcher.ir_governance_dir = governance if launcher.ir_declaration_path else None
    return launcher


def argv_fragment(context: Any) -> list[str]:
    governance = context.state / "connector-governance"
    if not any(
        (governance / name).is_file()
        for name in _governance_filenames()
    ):
        return []
    argv = ["--sec-ownership-governance-dir", str(governance)]
    declaration = context.state / "ir-pages.json"
    if declaration.is_file():
        argv.extend(["--ir-page-declaration", str(declaration)])
    return argv


def _governance_filenames() -> tuple[str, ...]:
    from .sec_ownership_launcher import GOVERNANCE_FILENAME_BY_OPERATION

    return tuple(GOVERNANCE_FILENAME_BY_OPERATION.values())


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_ownership",
    # After the calendar and before the model specification. It is a filings
    # lane and it wants the tick's single child slot no more urgently than the
    # calendar does; running the three tracking lanes adjacent keeps a day's
    # tracking reads together rather than scattered through the tick.
    order=88,
    driver_key="mission_ownership",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="S5: Form 4, SC 13D/G, 144 and 13F for the covered companies, plus "
         "what moved on their investor-relations pages.",
))


__all__ = [
    "COMPANY_REF_CIK_PREFIX",
    "DEFAULT_LOOKBACK_DAYS",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_CANDIDATES_PER_COMPANY",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_FAILURE_DETAIL_CHARS",
    "MAX_REMEMBERED_ACCESSIONS",
    "WRITE_SCOPES",
    "MissionOwnershipLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_ir_watch",
    "build_launcher",
    "dispatch",
    "issuer_for",
    "missing_scopes",
]
