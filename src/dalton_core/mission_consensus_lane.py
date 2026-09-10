"""P11b: keep every covered company's street expectations current, daily.

Two routes to the same question, and the lane runs both in one tick because
they cost different things.

*The vendor observation* reaches the network and is a child, like every other
connector call on this writer: approval first, artifact always, contract last.
Once a day per company. The street's published expectation of a fiscal year
does not move hourly, and a source that never agreed to serve us is asked
politely.

*The report scan* reaches nothing. The broker notes are already in this ledger,
already acquired under governance, already hashed; reading page one of one of
them is a handful of receipt checks and one spool read. So it runs in the tick
rather than in a child -- there is no network to wait for, no library to import
and no three-year backfill to outlive a request timeout, and a child would buy
nothing but a second process. One document a tick, so the writer thread is
never held for longer than one page-one read.

**The grant.** This lane writes a consensus authority, so the mission has to
say it may: ``consensus_estimate`` in ``autonomy.may_write``. A mission that
does not grant it gets ``ungranted`` and no child and no scan, every tick,
forever -- which is the correct behaviour and not a bug to route around.

**The fiscal calendar is a precondition, not a detail.** A company whose 10-K
this system has not ingested cannot have Yahoo's ``0q`` placed on its calendar,
so it is skipped with that reason rather than published against a guess. Live,
every covered company has filings; the skip exists for the sixth company
somebody adds on a Tuesday.
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

WRITE_SCOPE = "consensus_estimate"
# How long a company's vendor observation stays fresh. Twenty hours rather than
# twenty-four so a daily rhythm does not drift an hour later every day until it
# lands in the middle of the night and stays there.
REFRESH_SECONDS = 20 * 3600
MAX_FAILURE_DETAIL_CHARS = 500
# A company whose runs keep failing stops consuming the single slot. Held in
# this process only: a restart is nearly always a deploy, which is the most
# likely thing to have fixed whatever it was.
MAX_FAILURES_PER_COMPANY = 3
# How many documents one tick reads. One: the writer thread is doing this
# in-process, and a lane that reads twenty notes in a tick is a lane that
# occasionally stops answering.
DOCUMENTS_PER_TICK = 1


def _universe(mission: Mapping[str, Any]) -> list[dict[str, str]]:
    """The covered companies, in the order the mission prioritised them.

    A company with no ticker is skipped rather than guessed at: the vendor
    connector is keyed by market symbol, and there is no mapping from a CIK to
    one that does not involve asking somebody.
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
        rows.append({
            "company_ref": company_ref.strip(),
            "ticker": ticker.strip().upper(),
            "bootstrap_priority": str(item.get("bootstrap_priority") or "P9"),
        })
    rows.sort(key=lambda row: (row["bootstrap_priority"], row["ticker"]))
    return rows


def may_write_consensus(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission granted the automation the consensus scope."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return False
    return WRITE_SCOPE in set(scopes)


class MissionConsensusLaneCoordinator:
    """Settle the previous tick's vendor child, start at most one more, read one note."""

    def __init__(
        self,
        *,
        authority: Any,
        street_store: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        fiscal_calendar_for: Callable[[str], Mapping[str, str] | None],
        next_context: Callable[[str, set[str]], Mapping[str, Any] | None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.authority = authority
        self.street_store = street_store
        self.launcher = launcher
        self.mission = mission
        self.fiscal_calendar_for = fiscal_calendar_for
        self.next_context = next_context
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # The fetch in flight, so the next tick can settle it.
        self._open: str | None = None
        self._failures: dict[str, int] = {}
        self._failure_reason: dict[str, str] = {}
        # Companies whose vendor observation was refreshed, and when.
        self._refreshed: dict[str, datetime] = {}
        # Companies whose notes have all been read, so the scan stops asking.
        self._scanned_out: set[str] = set()

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
            # writing one still has to be attributable to the company it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "consensus_status": summary.get("consensus_status"),
            "consensus_version_ref": summary.get("consensus_version_ref"),
            "as_of": summary.get("as_of"),
            "changed_fields": summary.get("changed_fields"),
            "mapped_periods": summary.get("mapped_periods"),
            "invocation_ref": summary.get("invocation_ref"),
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
        if not company_ref:
            return settled
        if settled.get("status") != "succeeded":
            self._failures[company_ref] = self._failures.get(company_ref, 0) + 1
            self._failure_reason[company_ref] = (
                settled.get("failure_reason") or f"last run: {settled.get('status')}"
            )
            return settled
        # A run that succeeded is a run that reached the source, whatever it
        # found. ``duplicate`` is the common and correct answer: the street
        # said today what it said yesterday.
        self._failures.pop(company_ref, None)
        self._failure_reason.pop(company_ref, None)
        self._refreshed[company_ref] = self.clock()
        return settled

    def _fresh(self, company_ref: str) -> bool:
        when = self._refreshed.get(company_ref)
        if when is None:
            return False
        if (self.clock() - when).total_seconds() >= REFRESH_SECONDS:
            self._refreshed.pop(company_ref, None)
            return False
        return True

    # -- the two routes ----------------------------------------------------

    def _launch(self, universe: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        skipped: list[dict[str, Any]] = []
        day = self.clock().astimezone(timezone.utc).date().isoformat()
        for company in universe:
            company_ref = company["company_ref"]
            if self._failures.get(company_ref, 0) >= MAX_FAILURES_PER_COMPANY:
                skipped.append({
                    "company_ref": company_ref, "reason": "held",
                    "detail": self._failure_reason.get(company_ref, "repeated failures"),
                })
                continue
            if self._fresh(company_ref):
                skipped.append({"company_ref": company_ref, "reason": "recently_refreshed"})
                continue
            try:
                calendar = self.fiscal_calendar_for(company_ref)
            except Exception as exc:  # noqa: BLE001 - one company, not the tick
                skipped.append({
                    "company_ref": company_ref, "reason": "unreadable",
                    "detail": f"{type(exc).__name__}: {exc}",
                })
                continue
            if not calendar:
                # Yahoo's 0q/+1q/0y/+1y are relative to a calendar it does not
                # publish. Without this company's own filings there is nothing
                # to place them on, and a guess would be filed as a fact.
                skipped.append({
                    "company_ref": company_ref, "reason": "fiscal_calendar_unknown",
                    "detail": "no 10-K report date is held for this company, so "
                              "the vendor's relative periods cannot be placed",
                })
                continue
            try:
                ticket = self.launcher.start(
                    company_ref=company_ref, ticker=company["ticker"],
                    fiscal_year_end=calendar["fiscal_year_end"],
                    last_reported_period_end=calendar["last_reported_period_end"],
                    day=day,
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "company_ref": company_ref,
                        "skipped": skipped, "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._failures[company_ref] = self._failures.get(company_ref, 0) + 1
                self._failure_reason[company_ref] = reason
                return {"status": "rejected", "company_ref": company_ref,
                        "skipped": skipped, "reason": reason}
            self._open = ticket["id"]
            return {
                "status": "launched", "company_ref": company_ref,
                "ticker": company["ticker"], "ticket_ref": ticket["id"],
                "fiscal_year_end": calendar["fiscal_year_end"],
                "last_reported_period_end": calendar["last_reported_period_end"],
                "skipped": skipped,
            }
        return {"status": "idle", "skipped": skipped,
                "reason": "every covered company's vendor consensus is current"}

    def _scan(self, universe: Sequence[Mapping[str, str]]) -> dict[str, Any] | None:
        """Read one unscanned broker note, and say what came of it."""

        from .street_estimate import report_consensus
        from .street_estimate_extraction import extract

        for company in universe:
            company_ref = company["company_ref"]
            if company_ref in self._scanned_out:
                continue
            scanned = self.street_store.scanned_document_refs(company_ref)
            try:
                context = self.next_context(company_ref, scanned)
            except Exception as exc:  # noqa: BLE001 - one company, not the tick
                return {"company_ref": company_ref, "outcome": "unreadable",
                        "reason": f"{type(exc).__name__}: {exc}"}
            if context is None:
                # Every note this system holds for this company has been read.
                # Held in this process only, like every other hold here; a new
                # acquisition arrives with a restart or with the next hour.
                self._scanned_out.add(company_ref)
                continue
            result = extract(context)
            document_ref = str(context["document_ref"])
            if result["refusal"] is not None:
                self.street_store.record_scan(
                    company_ref=company_ref, document_ref=document_ref,
                    outcome="refused", reason=result["refusal"],
                    extraction_method=result["extraction_method"],
                )
                return {"company_ref": company_ref, "document_ref": document_ref,
                        "outcome": "refused", "reason": result["refusal"]}
            recorded = self.street_store.record(result["estimate"])
            self.street_store.record_scan(
                company_ref=company_ref, document_ref=document_ref,
                outcome="recorded", estimate_id=recorded["id"],
                extraction_method=result["extraction_method"],
            )
            scan = {
                "company_ref": company_ref, "document_ref": document_ref,
                "outcome": "recorded", "broker": recorded["broker"],
                "estimate_id": recorded["id"],
                "estimate_status": recorded["status"],
                "report_consensus": None,
            }
            # One more note may be what turns one house's opinion into a range.
            block = report_consensus(
                self.street_store.estimates(company_ref),
                as_of=self.clock().astimezone(timezone.utc).date().isoformat(),
            )
            if block is not None:
                try:
                    published = self.authority.publish_report_consensus(
                        company_ref=company_ref, report_consensus=block
                    )
                    scan["report_consensus"] = {
                        "status": published["status"],
                        "broker_count": block["broker_count"],
                        "brokers": block["brokers"],
                        "version_ref": published["id"],
                    }
                except Exception as exc:  # noqa: BLE001 - the note is still held
                    # The most common reason is the honest one: this company
                    # has no vendor observation yet, so there is no chain to
                    # attach a range to. The estimate itself is already stored.
                    scan["report_consensus"] = {
                        "status": "not_attached",
                        "reason": f"{type(exc).__name__}: {exc}"[
                            :MAX_FAILURE_DETAIL_CHARS],
                    }
            return scan
        return None

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        if not may_write_consensus(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {WRITE_SCOPE} in "
                    "autonomy.may_write; publishing what the street expects "
                    "without the grant is not something to work around"
                ),
            }
        universe = _universe(mission)
        if self._open is not None:
            fetch: dict[str, Any] = {
                "status": "busy", "reason": "a consensus child is still running",
                "skipped": [],
            }
        else:
            fetch = self._launch(universe)
        scan = self._scan(universe)
        status = fetch["status"]
        if status in {"idle", "busy"} and scan is not None:
            status = "scanned"
        return {
            "status": status, "settled": settled, "fetch": fetch, "scan": scan,
            "skipped": fetch.get("skipped", []),
            "reason": fetch.get("reason"),
        }


# -- registration ----------------------------------------------------------

LAUNCHER_KWARG = "consensus_estimate_launcher"
# The approved record's filename under the live state's governance directory.
# Its presence is what turns the lane on, exactly as the price lane works: a
# Core without an approved consensus connector runs without one rather than
# failing to start.
CONSENSUS_GOVERNANCE = "yfinance-analyst-estimates-v1.json"
SELL_SIDE_SOURCE_REF = "source:alphaengine"


def _fiscal_calendar_reader(server: Any) -> Callable[[str], dict[str, str] | None]:
    """The company's own filings, read for the two dates the mapping needs.

    Not a table of fiscal year ends. There is no citable fiscal calendar in
    this repository and adding one would be a second, unciteable copy of
    something the filings already say -- ``claim_index_tagging`` refuses the
    same temptation for the same reason. The newest 10-K's report date *is* the
    fiscal year end, and the newest filing of either form is the last period
    actually reported.
    """

    def read(company_ref: str) -> dict[str, str] | None:
        connection = server.store.connection
        annual = connection.execute(
            "SELECT report_date FROM coverage_mission_statement_filings "
            "WHERE company_ref=? AND form='10-K' ORDER BY report_date DESC LIMIT 1",
            (company_ref,),
        ).fetchone()
        newest = connection.execute(
            "SELECT report_date FROM coverage_mission_statement_filings "
            "WHERE company_ref=? ORDER BY report_date DESC LIMIT 1",
            (company_ref,),
        ).fetchone()
        if annual is None or newest is None:
            return None
        return {
            "fiscal_year_end": str(annual["report_date"])[5:10],
            "last_reported_period_end": str(newest["report_date"]),
        }

    return read


def _context_reader(server: Any) -> Callable[[str, set[str]], dict[str, Any] | None]:
    """The next unread broker note for one company, as an extraction context.

    Reads through the same service every other document pass uses, so the
    receipts are re-verified and the quotes are the exact bytes the acquisition
    recorded. ``require_open=False`` because a note that has already been read
    for prose is exactly the note whose price target is still unread -- the
    same reason the metric-discovery pass reads closed reviews.
    """

    def read(company_ref: str, scanned: set[str]) -> dict[str, Any] | None:
        from .document_extraction import DocumentExtractionService
        from .store import content_hash
        from .street_estimate import SELL_SIDE_SPEC_REFS

        pointer = server.store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            return None
        mission_ref = pointer["mission_version_id"]
        mission = server.coverage_mission.mission(mission_ref)
        specs = server.coverage_mission.document_spec_refs(mission_ref)
        member = next(
            (row for row in mission["universe"]
             if row.get("company_ref") == company_ref), {}
        )
        names = [name for name in (member.get("name"), member.get("ticker")) if name]
        service = DocumentExtractionService(server)
        for review in server.coverage_mission.document_reviews(
            mission_ref, company_ref=company_ref, limit=500
        ):
            document_ref = review["document_ref"]
            if document_ref in scanned:
                continue
            if review["source_ref"] != SELL_SIDE_SOURCE_REF:
                continue
            spec_ref = specs.get(document_ref)
            if spec_ref not in SELL_SIDE_SPEC_REFS:
                continue
            context = service.context(
                review["review_id"], content_hash(review), 0,
                "automation:coverage-mission", require_open=False,
            )
            return {
                "company_ref": company_ref,
                "document_ref": document_ref,
                "spec_ref": spec_ref,
                "source_manifest_hash": context["source_manifest_hash"],
                # The acquisition recorded when the note was published; the
                # review's own creation date is when this system saw it, which
                # is a different fact and not the one a target price is dated
                # by. Falling back to it is better than refusing the note.
                "published_on": str(review["created_at"])[:10],
                "subject_names": names,
                "sources": [],
                "analysts": [],
                "document_companies": [],
                "quotes": context["quotes"],
            }
        return None

    return read


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P11b).

    One vendor child at a time and one broker note a tick. The coordinator is
    cached rather than rebuilt, because its held state is the point: which
    child is open, which companies failed, which were refreshed today and which
    have had every note they hold already read. A fresh coordinator every tick
    would forget all four and start a child for a company it had just been told
    to leave alone.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no approved consensus connector on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .consensus_estimate import ConsensusEstimateAuthority
        from .street_estimate import StreetEstimateStore

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionConsensusLaneCoordinator(
            authority=ConsensusEstimateAuthority(server.store),
            street_store=StreetEstimateStore(server.store),
            launcher=launcher,
            mission=mission,
            fiscal_calendar_for=_fiscal_calendar_reader(server),
            next_context=_context_reader(server),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # Off unless an approved governance record is named, like every other
    # connector on this writer. No model configuration: the extraction that
    # ships in v1.0 is deterministic and makes no model call at all.
    parser.add_argument(
        "--consensus-governance",
        help="approved yfinance-analyst-estimates governance record",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "consensus_governance", None) is None:
        return None
    from pathlib import Path as _Path

    from .consensus_estimate_launcher import ConsensusEstimateLauncher

    return ConsensusEstimateLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        governance_path=args.consensus_governance,
    )


def argv_fragment(context: Any) -> list[str]:
    governance = context.state / "connector-governance" / CONSENSUS_GOVERNANCE
    if not governance.is_file():
        return []
    return ["--consensus-governance", str(governance)]


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_consensus",
    # After the price lane and its neighbours, before the model specification:
    # what the street expects is read against what the shares trade at, and
    # both of them want the tick's single child slot less than a filing does.
    # 88 is free; 85-87 are prices, tracking and the catalyst calendar.
    order=88,
    driver_key="mission_consensus",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P11b: what the street expects, from the vendor daily and from the "
         "broker notes already in the ledger.",
))


__all__ = [
    "CONSENSUS_GOVERNANCE",
    "DOCUMENTS_PER_TICK",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_FAILURE_DETAIL_CHARS",
    "REFRESH_SECONDS",
    "SELL_SIDE_SOURCE_REF",
    "WRITE_SCOPE",
    "MissionConsensusLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "may_write_consensus",
]
