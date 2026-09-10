"""P11a: keep every covered company's price history current, one child a tick.

Queueless, like the specification lane and unlike the acquisition lanes. What
needs fetching is a *derived* fact -- a company whose stored series stops before
the last trading day -- so it is computed from the authority every tick rather
than written down and drained. There is nothing to leave stuck, and a company
that is up to date simply is not chosen.

The resting state is therefore almost-silence: five companies get one child
each on the first afternoon, and after that one company a tick picks up
yesterday's bar and the rest are skipped in a couple of reads. Weekends cost
nothing, because a run that adds no bar puts the company aside for a while
rather than asking Yahoo the same empty question every five minutes.

**The grant.** This lane writes a market-price authority, so the mission has to
say it may: ``market_price`` in ``autonomy.may_write``. A mission that does not
grant it gets ``ungranted`` and no child, every tick, forever -- which is the
correct behaviour, not a bug to route around. The grant is a decision the owner
makes by publishing a mission version.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_failure_ledger import lane_budget
from .lane_registry import LaneSpec, register_lane
from .market_price import provisional_bar_date

WRITE_SCOPE = "market_price"
PROXY_WRITE_SCOPES = frozenset({"claim", "evidence", "claim_index"})
# How far back a company with no stored history is fetched. Three years is the
# blueprint's own acceptance bar -- a valuation percentile computed over six
# months is a number about this year's mood, not about the company.
BACKFILL_YEARS = 3
# Yahoo's window excludes ``end``, so asking for tomorrow is how today's bar is
# included. Not a fudge: it is what the source means by the parameter.
END_LOOKAHEAD_DAYS = 1
# After a run that added no trading day, leave the company alone for this long.
#
# Two things ride on this. A weekend tick would otherwise spend a call every
# five minutes discovering that Saturday is still not a trading day. And once
# the lane re-requests a provisional bar -- one read mid-session, whose close
# is only the last trade so far -- there is always a window to ask for, so
# without a hold the afternoon would be one restatement version per tick. Six
# hours means an intraday bar is corrected a few times a day, which is what a
# valuation needs and no more.
SATISFIED_HOLD_SECONDS = 6 * 3600
MAX_FAILURE_DETAIL_CHARS = 500
# A company whose runs keep failing stops consuming the single slot. Held in
# this process only: a restart is nearly always a deploy, which is the most
# likely thing to have fixed whatever it was.
#
# P17d: this is now the *transient* budget only. A company whose runs fail
# because Yahoo is not answering is parked against ``market_data`` rather than
# counted down to a permanent hold, and it comes back when a read of that
# source works. Three strikes was never a statement about the company when the
# source was the thing that was down.
MAX_FAILURES_PER_COMPANY = 3
# The tick-summary key, and therefore the name this lane is parked under.
DRIVER_KEY = "mission_market_prices"


def _universe(mission: Mapping[str, Any]) -> list[dict[str, str]]:
    """The covered companies, in the order the mission prioritised them.

    A company with no ticker is skipped rather than guessed at: this connector
    is keyed by market symbol and there is no mapping from a CIK to one that
    does not involve asking somebody.
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


def may_write_market_price(mission: Mapping[str, Any] | None) -> bool:
    """Whether this mission granted the automation the price-authority scope."""

    if not isinstance(mission, Mapping):
        return False
    autonomy = mission.get("autonomy")
    if not isinstance(autonomy, Mapping):
        return False
    scopes = autonomy.get("may_write")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return False
    return WRITE_SCOPE in set(scopes)


class MissionMarketPriceLaneCoordinator:
    """Settle the previous tick's price child, then start at most one more."""

    def __init__(
        self,
        *,
        authority: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        clock: Callable[[], datetime] | None = None,
        backfill_years: int = BACKFILL_YEARS,
        failure_ledger_dir: Any | None = None,
        proxy_mappings: Sequence[Mapping[str, Any]] = (),
        proxy_authority: Any | None = None,
    ) -> None:
        self.authority = authority
        self.launcher = launcher
        self.mission = mission
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.backfill_years = int(backfill_years)
        self.proxy_mappings = [dict(item) for item in proxy_mappings]
        self.proxy_authority = proxy_authority
        # The fetch in flight, so the next tick can settle it. A launcher
        # cannot be asked "what did you last run" -- it holds one process, not
        # a history -- and the ticket ref is the only handle on the summary.
        self._open: str | None = None
        # Companies whose runs failed, classified. The counted part is held in
        # this process only, on purpose (see the module note); the parked part
        # is appended to the lane failure ledger and replayed on construction,
        # because "Yahoo has been down since Tuesday" is not a fact a deploy
        # should erase.
        self.budget = lane_budget(
            DRIVER_KEY, state_dir=failure_ledger_dir, clock=self.clock,
            max_transient_failures=MAX_FAILURES_PER_COMPANY)
        # Companies that were up to date last time they were asked, and when.
        self._satisfied: dict[str, datetime] = {}
        # Companies whose earlier history has been asked for once and found to
        # be all there is. Held in this process only, like every other hold.
        self._backfilled: set[str] = set()
        # The window each open child was launched for, so its result can be
        # read as an answer to the question that was actually asked.
        self._open_kind: str | None = None

    # -- window ------------------------------------------------------------

    def _today(self) -> date:
        return self.clock().astimezone(timezone.utc).date()

    def _permission_key(self, company_ref: str) -> str:
        resolver = getattr(self.launcher, "governance_identity", None)
        try:
            identity = resolver() if resolver is not None else "legacy"
        except Exception:
            identity = "invalid"
        return f"permission|{company_ref}|governance:{identity}"

    def _retire_legacy_permission(self, company_ref: str) -> None:
        # blocked() consumes a dependency probe; inspecting old permission
        # history must never spend the next real transport attempt.
        refusal = next((row for row in self.budget.permission_items()
                        if row["item_key"] == company_ref), None)
        if refusal is None or "yfinance daily-prices governance record is not approved" not in refusal["reason"]:
            return
        loader = getattr(self.launcher, "load_governance", None)
        if loader is not None:
            try:
                loader()
            except Exception:
                return
            self.budget.retire(company_ref, reason="approved_governance_replaced_legacy_refusal")

    def _floor(self) -> date:
        today = self._today()
        try:
            return today.replace(year=today.year - self.backfill_years)
        except ValueError:
            # 29 February exists in one year in four; the backfill does not
            # need to fail on the one tick a leap year that lands on it.
            return today.replace(year=today.year - self.backfill_years, month=2, day=28)

    def window(self, company_ref: str) -> tuple[str, str, str] | None:
        """The days this company is missing, and which end they are missing at.

        Three cases, checked in that order.

        *No history at all*: reach back three years.

        *Missing recent days*: start the day after the last stored bar, so a
        lane running daily asks for one day and a lane that was off for a month
        asks for a month, without anyone deciding which. **Unless that last bar
        is provisional** -- read before its own trading day settled, so its
        close is whatever the last trade happened to be that afternoon. Then
        the window starts *on* it, and the next run restates it. Without this
        the authority's restatement path is unreachable through the lane and a
        mid-session print is frozen as a close forever.

        *Missing earlier days*: a series that starts inside the three-year
        floor is short at the far end -- a first run that was cut off, or a
        floor that has since moved back past what was fetched. Asked for once;
        a backward run that finds nothing settles the question for this
        process, because there is nothing behind the company's first trading
        day and asking again every six hours would never learn that.
        """

        today = self._today()
        end = today + timedelta(days=END_LOOKAHEAD_DAYS)
        latest = self.authority.latest_version(company_ref)
        if latest is None:
            return self._floor().isoformat(), end.isoformat(), "backfill"
        last = date.fromisoformat(latest["last_bar_date"])
        provisional = provisional_bar_date(latest)
        start = last if provisional == latest["last_bar_date"] else last + timedelta(days=1)
        if start < end:
            return (
                start.isoformat(), end.isoformat(),
                "restate_provisional" if provisional else "forward",
            )
        if company_ref not in self._backfilled:
            floor = self._floor()
            first = date.fromisoformat(latest["first_bar_date"])
            if floor < first:
                return floor.isoformat(), first.isoformat(), "backfill_gap"
        return None

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
            "governance_hash": ticket.get("governance_hash"),
            "series_status": summary.get("series_status"),
            "series_version_ref": summary.get("series_version_ref"),
            "bar_count": summary.get("bar_count"),
            "added_bar_count": summary.get("added_bar_count"),
            "restated_bar_dates": summary.get("restated_bar_dates"),
            "last_bar_date": summary.get("last_bar_date"),
            "provisional_bar_date": summary.get("provisional_bar_date"),
            "dropped_row_count": summary.get("dropped_row_count"),
            "invocation_ref": summary.get("invocation_ref"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        """Close out the previous tick's child, if it has finished.

        Settling happens here rather than after ``start`` for the obvious
        reason: a child inspected in the same breath it was spawned is always
        still running, and a lane that only ever looks at its own newborn never
        learns anything.
        """

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        kind, self._open, self._open_kind = self._open_kind, None, None
        settled["window_kind"] = kind
        company_ref = settled.get("company_ref")
        if not company_ref:
            return settled
        if settled.get("status") != "succeeded":
            settled["failure"] = self.budget.record_settled(
                company_ref, settled).as_wire()
            return settled
        # A run that succeeded is a run that reached the source, whatever it
        # found: the retry budget is about companies this lane cannot serve,
        # not about quiet markets. It is also the probe that resumes every
        # company parked on the same source -- P14e's resume, by dependency.
        resumed = self.budget.clear(company_ref)
        # A price read that worked is a market-data read that worked, whether
        # or not this company was the one parked on it.
        resumed += self.budget.dependency_answered("market_data")
        if resumed:
            settled["resumed"] = sorted(set(resumed))
        if kind == "backfill_gap" and not settled.get("added_bar_count"):
            # There is nothing behind this company's first trading day. Asked
            # and answered; asking again every six hours would never learn it.
            self._backfilled.add(company_ref)
        # Held on "added no trading day", not on "published nothing". A run
        # that only restated a provisional bar did publish a version, and if
        # that reopened the slot the lane would restate the same afternoon bar
        # every tick until the market closed.
        if not settled.get("added_bar_count"):
            self._satisfied[company_ref] = self.clock()
        else:
            self._satisfied.pop(company_ref, None)
        return settled

    def _held_recently(self, company_ref: str) -> bool:
        when = self._satisfied.get(company_ref)
        if when is None:
            return False
        held = (self.clock() - when).total_seconds()
        if held >= SATISFIED_HOLD_SECONDS:
            self._satisfied.pop(company_ref, None)
            return False
        return True

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        if not may_write_market_price(mission):
            return {
                "status": "ungranted", "settled": settled,
                "reason": (
                    f"this mission does not grant {WRITE_SCOPE} in "
                    "autonomy.may_write; publishing a price series without the "
                    "grant is not something to work around"
                ),
            }
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "a price child is still running"}
        proxy_results = None
        if self.proxy_mappings:
            mission = dict(mission)
            universe = [dict(item) for item in mission.get("universe") or ()]
            known = {item.get("company_ref") for item in universe}
            unknown_targets = sorted({
                mapping["target_subject_ref"] for mapping in self.proxy_mappings
                if mapping["target_subject_ref"] not in known})
            if unknown_targets:
                return {"status": "misconfigured", "settled": settled,
                        "reason": "market proxy targets are outside the mission: "
                                  + ", ".join(unknown_targets)}
            for mapping in self.proxy_mappings:
                source_ref = mapping["source_series_company_ref"]
                if source_ref not in known:
                    universe.append({
                        "company_ref": source_ref, "ticker": mapping["source_ticker"],
                        "bootstrap_priority": "P8",
                    })
                    known.add(source_ref)
            mission["universe"] = universe
        if self.proxy_authority is not None:
            scopes = set((mission.get("autonomy") or {}).get("may_write") or ())
            if self.proxy_mappings and not PROXY_WRITE_SCOPES.issubset(scopes):
                proxy_results = {
                    "status": "not_permitted", "results": [],
                    "reason": "market proxy derivation requires claim, evidence, and "
                              "claim_index write scopes",
                }
            else:
                proxy_results = self.proxy_authority.refresh_all(self.proxy_mappings)
        skipped: list[dict[str, Any]] = []
        for company in _universe(mission):
            company_ref = company["company_ref"]
            self._retire_legacy_permission(company_ref)
            permission_key = self._permission_key(company_ref)
            permission_blocked = self.budget.blocked(permission_key)
            business_blocked = self.budget.blocked(company_ref)
            blocked = permission_blocked or business_blocked
            if blocked is not None:
                # Three words, not one. ``held`` will change on a deploy,
                # ``parked`` when the source answers, ``terminal`` never --
                # and an operator who cannot tell them apart cannot act.
                skipped.append({
                    "company_ref": company_ref, "reason": blocked.action,
                    "detail": self.budget.failure_reason(permission_key if permission_blocked else company_ref),
                    "failure_class": blocked.classification.failure_class,
                    "dependency": blocked.classification.dependency,
                })
                continue
            if self._held_recently(company_ref):
                skipped.append({"company_ref": company_ref, "reason": "recently_current"})
                continue
            try:
                window = self.window(company_ref)
            except Exception as exc:  # noqa: BLE001 - one company, not the tick
                skipped.append({
                    "company_ref": company_ref, "reason": "unreadable",
                    "detail": f"{type(exc).__name__}: {exc}",
                })
                continue
            if window is None:
                skipped.append({"company_ref": company_ref, "reason": "current"})
                continue
            start, end, kind = window
            try:
                ticket = self.launcher.start(
                    company_ref=company_ref, ticker=company["ticker"],
                    start=start, end=end,
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
            self._open_kind = kind
            return {
                "status": "launched", "company_ref": company_ref,
                "ticker": company["ticker"], "requested_start": start,
                "requested_end": end, "window_kind": kind,
                "ticket_ref": ticket["id"],
                "settled": settled, "skipped": skipped,
                "market_proxies": proxy_results,
            }
        from .lane_exhaustion import exhausted_by_failures
        resting = exhausted_by_failures(
            skipped, success_reason="every covered company's price history is current")
        return {
            **resting, "settled": settled, "skipped": skipped,
            "market_proxies": proxy_results,
            "failures": self.budget.summary(),
        }


# -- registration ----------------------------------------------------------
#
# The one line the rest of the system needs. Everything below is either this
# lane's own decision (which approval turns it on, what it is called in the
# tick summary) or a lazy import, so that importing this module registers the
# lane without dragging in the writer.

LAUNCHER_KWARG = "market_price_launcher"
# The approved record's filename under the live state's governance directory.
# Its presence is what turns the lane on, exactly as the statements lane works:
# a Core without an approved price connector runs without one rather than
# failing to start.
MARKET_PRICE_GOVERNANCE = "yfinance-daily-prices-v1.json"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P11a).

    One company's price window at a time. The lane has no queue: what needs
    fetching is derived from the price authority every tick, so a company that
    is current is simply not chosen and there is nothing to leave stuck.

    The coordinator is cached rather than rebuilt, because its held state is
    the point: which child is open, which companies failed, which were current
    when last asked, and which have had their earlier history settled. A fresh
    coordinator every tick would forget all four and start a child for a
    company it had just been told to leave alone.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no approved market-price connector on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .market_price import MarketPriceSeriesAuthority
        from .market_proxy_claim import MarketProxyClaimAuthority, load_mappings

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        mappings = load_mappings(getattr(launcher, "proxy_config_path", None))
        coordinator = MissionMarketPriceLaneCoordinator(
            authority=MarketPriceSeriesAuthority(server.store),
            launcher=launcher,
            mission=mission,
            # ``getattr``: a lane exercised against a stub writer has no
            # state directory, and a lane that refuses to run without a
            # ledger would be a lane that fails closed on its bookkeeping.
            failure_ledger_dir=getattr(server, "state_dir", None),
            proxy_mappings=mappings,
            proxy_authority=MarketProxyClaimAuthority(server.store),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # Off unless an approved governance record is named, like every other
    # connector on this writer. No model configuration: this lane makes no
    # model call at all.
    parser.add_argument(
        "--market-price-governance",
        help="approved yfinance-daily-prices governance record",
    )
    parser.add_argument("--market-proxy-config",
                        help="explicit source-series to research-subject proxy mappings")


def build_launcher(args: Any) -> Any | None:
    if args.market_price_governance is None:
        return None
    from pathlib import Path as _Path

    from .market_price_launcher import MarketPriceLauncher

    return MarketPriceLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        governance_path=args.market_price_governance,
        proxy_config_path=getattr(args, "market_proxy_config", None),
    )


def argv_fragment(context: Any) -> list[str]:
    governance = context.state / "connector-governance" / MARKET_PRICE_GOVERNANCE
    if not governance.is_file():
        return []
    fragment = ["--market-price-governance", str(governance)]
    proxy_config = context.state / "market-proxy-mappings.json"
    if proxy_config.is_file():
        fragment += ["--market-proxy-config", str(proxy_config)]
    return fragment


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_market_prices",
    # After the statements lane and before the model specification: prices are
    # what the specification's valuation section will be read against, and both
    # of them want the tick's single child slot less than a filing does.
    order=85,
    driver_key="mission_market_prices",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P11a: one company's daily bars, share count and market capitalisation, "
         "every bar bound to the call that fetched it.",
))


__all__ = [
    "BACKFILL_YEARS",
    "END_LOOKAHEAD_DAYS",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_FAILURE_DETAIL_CHARS",
    "SATISFIED_HOLD_SECONDS",
    "LANE",
    "LAUNCHER_KWARG",
    "MARKET_PRICE_GOVERNANCE",
    "WRITE_SCOPE",
    "MissionMarketPriceLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "may_write_market_price",
]
