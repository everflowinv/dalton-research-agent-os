"""P13ak: run the financial-statements lane on a tick.

The connector, its governance and its child all worked by hand; nothing
dispatched them. This is the part that makes them run: for each company the
checklist covers, queue one statements run, launch one at a time, and when a
child finishes, read its observation into the ledger.

Three rules, each of which is a bug this codebase has already paid for:

* One child at a time, and a company with a dispatch in flight is not queued
  again. Queueing faster than the lane drains is what left thirty-five SEC
  dispatches open and froze that lane for a day.
* Record before settling. If the process dies between the two, the dispatch is
  still ``launched`` and the next tick records the same observation again --
  which is a no-op, because a filing is ingested once per company.
* A company whose runs keep failing stops being retried. Without a budget, a
  company the parser cannot handle consumes the single slot forever and every
  other company starves behind it.

How deep to go -- how many filings back -- is a parameter rather than a
constant on purpose. One quarter is the floor that keeps the lane moving; how
much history a company actually needs is a judgement the planner makes, and
this is where that judgement will attach.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .coverage_mission import CoverageMissionError, MAX_STATEMENT_FILINGS
from .lane_registry import LaneSpec, register_lane
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .store import content_hash

MAX_QUEUED_PER_RUN = 4
# Three failed or rejected runs is a company this lane cannot serve today.
MAX_FAILURES_PER_COMPANY = 3
# P13am: which failures are the *company's*, listed positively.
#
# The first version listed the failures that were this Core's fault and charged
# everything else to the company. That is the wrong way round, and it showed:
# an AttributeError in this codebase's own adapter -- asking a collection of
# filings for the XBRL only a single filing has -- looked exactly like a
# company the lane could not serve, and spent IBM's whole retry budget in three
# ticks on a bug that had nothing to do with IBM.
#
# The set of things that are genuinely the company's fault is short and
# knowable: it did not file, or what it filed has no XBRL. Everything else --
# a malformed EDGAR identity, an unapproved record, a crash in our own code --
# is ours, and a failure nobody can attribute is not evidence against the
# company either.
COMPANY_FAILURE_MARKERS = (
    "filing found for this company",
    "returned no filing with XBRL",
    "carries no lane ticket",
)
# Ours are held rather than charged, but not retried without end: a fault that
# survives this many attempts is not going to be fixed by another one.
MAX_ATTEMPTS_PER_COMPANY = 8
# While a configuration is broken it is broken for every company, so the lane
# holds instead of asking SEC the same doomed question once a tick.
CONFIGURATION_HOLD_SECONDS = 1800
DEFAULT_FORM = "10-Q"
DEFAULT_FORMS = ("10-K", "10-Q")
DEFAULT_FORM_LIMITS = {"10-K": 1}
DEFAULT_FILING_LIMIT = 1
MAX_FAILURE_DETAIL_CHARS = 500
STATEMENT_LANE_CONFIG = "statement-lane-config.json"
STATEMENT_LANE_CONFIG_SCHEMA = "0.1"


def load_statement_lane_config(path: Path) -> dict[str, Any]:
    """Read an owner-selected scheduling target without modifying it."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"statement lane config cannot be read: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "forms", "filing_limits", "content_hash"}:
        raise ValueError("statement lane config has an invalid closed shape")
    if value["schema_version"] != STATEMENT_LANE_CONFIG_SCHEMA:
        raise ValueError("statement lane config schema_version is unsupported")
    forms = value["forms"]
    limits = value["filing_limits"]
    if (not isinstance(forms, list) or not forms
            or any(item not in DEFAULT_FORMS for item in forms)
            or len(set(forms)) != len(forms)):
        raise ValueError("statement lane config forms must be unique 10-Q/10-K values")
    if not isinstance(limits, dict) or set(limits) != set(forms):
        raise ValueError("statement lane config needs one filing limit per selected form")
    if any(isinstance(item, bool) or not isinstance(item, int)
           or not 1 <= item <= MAX_STATEMENT_FILINGS for item in limits.values()):
        raise ValueError(f"statement lane filing limits must be 1..{MAX_STATEMENT_FILINGS}")
    body = {key: value[key] for key in ("schema_version", "forms", "filing_limits")}
    if value["content_hash"] != content_hash(body):
        raise ValueError("statement lane config content_hash is invalid")
    return body


def _is_configuration_failure(reason: Any) -> bool:
    """Was this failure ours rather than the company's?

    Everything that is not recognisably about the company's own filings is
    ours, including a failure with no reason recorded at all. Charging an
    unattributed failure to the company is how a bug in this codebase spends
    a company's retry budget.
    """

    if not isinstance(reason, str) or not reason.strip():
        return True
    return not any(marker in reason for marker in COMPANY_FAILURE_MARKERS)


def _failure_reason(summary: Any) -> str | None:
    """The child's own account of why it failed, if it left one."""

    if not isinstance(summary, Mapping):
        return None
    reason = summary.get("failure_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip()[:MAX_FAILURE_DETAIL_CHARS]


class MissionStatementLaneCoordinator:
    """Queue, launch and settle the statements lane, one child per tick."""

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        checklist: Callable[[], Sequence[Mapping[str, Any]]],
        form: str = DEFAULT_FORM,
        forms: Sequence[str] | None = None,
        filing_limit: int = DEFAULT_FILING_LIMIT,
        filing_limits: Mapping[str, int] | None = None,
        actor_ref: str = "automation:coverage-mission",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.missions = missions
        self.launcher = launcher
        self.checklist = checklist
        self.forms = tuple(forms) if forms is not None else (form,)
        if (not self.forms or len(set(self.forms)) != len(self.forms)
                or any(item not in ("10-Q", "10-K") for item in self.forms)):
            raise ValueError("forms must be a unique nonempty selection of 10-Q and 10-K")
        self.form = self.forms[0]
        if not 1 <= int(filing_limit) <= MAX_STATEMENT_FILINGS:
            raise ValueError(f"filing_limit must be 1..{MAX_STATEMENT_FILINGS}")
        self.filing_limit = int(filing_limit)
        self.filing_limits: dict[str, int] = {}
        for item, limit in (filing_limits or {}).items():
            if item not in self.forms:
                raise ValueError("filing limit form must be selected")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_STATEMENT_FILINGS:
                raise ValueError(f"filing limit must be 1..{MAX_STATEMENT_FILINGS}")
            self.filing_limits[item] = limit
        self.actor_ref = actor_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- configuration failures --------------------------------------------

    def _seconds_since(self, when: Any) -> float | None:
        if not isinstance(when, str) or not when:
            return None
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (self.clock() - moment).total_seconds()

    def _retry_salt(self) -> str:
        """A value that moves with the clock, not with the company.

        One retry of a broken configuration per hold window: enough to notice
        that it was fixed, not enough to keep asking SEC a doomed question.
        """

        window = int(self.clock().timestamp()) // CONFIGURATION_HOLD_SECONDS
        return f"configuration-retry:{window}"

    # -- settling ----------------------------------------------------------

    def _settle_finished(self) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        try:
            open_dispatches = self.missions.launched_statement_dispatches(limit=50)
        except Exception:  # noqa: BLE001 - never break the tick over bookkeeping
            return settled
        for dispatch in open_dispatches:
            ticket_ref = dispatch.get("ticket_ref")
            if not ticket_ref:
                settled.append(self._settle(dispatch, "failed",
                                            "dispatch carries no lane ticket"))
                continue
            try:
                ticket = self.launcher.status(ticket_ref)
            except LaneChildTicketNotFound:
                settled.append(self._settle(dispatch, "failed",
                                            "lane ticket is no longer on disk"))
                continue
            except Exception:  # noqa: BLE001 - unreadable now; try next tick
                continue
            status = ticket.get("status")
            if status == "running":
                continue
            if status != "succeeded":
                settled.append(self._settle(
                    dispatch, "failed",
                    _failure_reason(ticket.get("summary")) or f"lane run {status}"))
                continue
            summary = ticket.get("summary")
            observation = summary.get("observation") if isinstance(summary, Mapping) else None
            if not isinstance(observation, Mapping):
                settled.append(self._settle(
                    dispatch, "failed",
                    "lane run succeeded without an observation to record"))
                continue
            # Record first: a crash between here and the settle leaves the
            # dispatch launched, and recording the same filing twice is a
            # no-op by construction.
            try:
                recorded = self.missions.record_statement_observation(
                    dispatch_id=dispatch["dispatch_id"], observation=observation,
                    governance_ref=str(summary.get("governance_ref") or ""),
                    governance_hash=str(summary.get("governance_hash") or ""),
                )
            except CoverageMissionError as exc:
                settled.append(self._settle(
                    dispatch, "failed", f"{type(exc).__name__}: {exc}"))
                continue
            outcome = self._settle(dispatch, "succeeded", None)
            outcome["line_count"] = recorded["line_count"]
            outcome["accessions"] = [item["accession"] for item in recorded["filings"]]
            settled.append(outcome)
        return settled

    def _settle(self, dispatch: Mapping[str, Any], outcome: str,
                reason: str | None) -> dict[str, Any]:
        result = {
            "dispatch_ref": dispatch["dispatch_id"],
            "company_ref": dispatch["company_ref"],
            "outcome": outcome, "failure_reason": reason,
        }
        try:
            self.missions.settle_statement_dispatch(
                dispatch["dispatch_id"], outcome=outcome, failure_reason=reason)
        except CoverageMissionError as exc:
            result["outcome"] = "unsettled"
            result["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return result

    # -- depth -------------------------------------------------------------

    def _wanted_filings(self, company_ref: str, form: str) -> int:
        """How many filings of this form this company's model rests on.

        The specification says how many quarters of history it needs; this lane
        bounds that by what one child may parse in a run. A company with no
        specification yet gets the floor -- enough to decide a specification
        from, which is what unblocks the rest.
        """

        try:
            spec = self.missions.latest_company_model_spec(company_ref)
        except Exception:  # noqa: BLE001 - the floor is always safe
            return self.filing_limits.get(form, self.filing_limit)
        if not spec:
            return self.filing_limits.get(form, self.filing_limit)
        horizon = spec.get("horizon") or {}
        quarters = horizon.get("historical_quarters")
        if isinstance(quarters, bool) or not isinstance(quarters, int) or quarters < 1:
            return self.filing_limits.get(form, self.filing_limit)
        if form in self.filing_limits:
            return self.filing_limits[form]
        # A 10-K covers a year, so asking for twenty quarters of annual reports
        # would be asking for twenty years. Quarters are quarters; anything
        # else is scaled to what the form actually reports.
        if form == "10-K":
            quarters = max(1, (quarters + 3) // 4)
        return max(self.filing_limit, min(quarters, MAX_STATEMENT_FILINGS))

    # -- queueing ----------------------------------------------------------

    def _queue(self) -> list[dict[str, Any]]:
        queued: list[dict[str, Any]] = []
        try:
            companies = list(self.checklist())
        except Exception:  # noqa: BLE001
            return queued
        for company in companies:
            if len(queued) >= MAX_QUEUED_PER_RUN:
                break
            company_ref = company.get("company_ref") if isinstance(company, Mapping) else None
            ticker = company.get("ticker") if isinstance(company, Mapping) else None
            if not company_ref or not ticker:
                continue
            try:
                coverage = self.missions.statement_coverage(company_ref)
            except CoverageMissionError:
                continue
            if coverage["open_dispatches"]:
                continue
            for form in self.forms:
                outcome = self._queue_company_form(
                    company_ref=company_ref, ticker=ticker, form=form,
                    coverage=coverage,
                )
                if outcome is not None:
                    queued.append(outcome)
                    if outcome["status"] == "queued":
                        break
        return queued

    def _queue_company_form(self, *, company_ref: str, ticker: str, form: str,
                            coverage: Mapping[str, Any]) -> dict[str, Any] | None:
        failures = [item for item in (coverage.get("failures") or [])
                    if item.get("form") == form]
            # Only the failures this company is actually responsible for spend
            # its budget, and only those number its attempts.
        charged = [item for item in failures
                   if not _is_configuration_failure(item.get("reason"))]
        if len(charged) >= MAX_FAILURES_PER_COMPANY:
            return None
        if len(failures) >= MAX_ATTEMPTS_PER_COMPANY:
            return {
                "company_ref": company_ref, "status": "held", "form": form,
                "reason": f"{len(failures)} {form} runs have failed for this company; "
                          "not trying again without a change",
            }
        retry_salt = None
        if failures and _is_configuration_failure(failures[-1].get("reason")):
            held_for = self._seconds_since(failures[-1].get("at"))
            if held_for is not None and held_for < CONFIGURATION_HOLD_SECONDS:
                return {
                    "company_ref": company_ref, "status": "held", "form": form,
                    "reason": "this Core's own configuration failed the last "
                              "run; holding rather than asking SEC again",
                }
            retry_salt = self._retry_salt()
            # P13am: how much history this company needs is its own model's
            # answer, not a constant. IBM's specification asked for twenty
            # quarters to separate mainframe launch cycles from the underlying
            # business; a consultancy with a steady book needs far less. Until
            # a specification exists, one quarter is the floor that keeps the
            # lane moving and gives the model something to reason over.
        wanted = self._wanted_filings(company_ref, form)
        if coverage.get("held_by_form", {}).get(form, 0) >= wanted:
            return None
        try:
            authorization = self.missions.sec_lane_authorization_for_company(company_ref)
            dispatch = self.missions.queue_statement_dispatch(
                authorization=authorization, form=form,
                filing_limit=wanted, attempt=len(charged), retry_salt=retry_salt,
            )
        except CoverageMissionError as exc:
            return {"company_ref": company_ref, "form": form, "status": "refused",
                    "reason": f"{type(exc).__name__}: {exc}"}
        if dispatch["status_marker"] == "fresh":
            return {"company_ref": company_ref, "form": form, "status": "queued",
                    "dispatch_ref": dispatch["dispatch_id"]}
        return None

    # -- launching ---------------------------------------------------------

    def _launch_one(self) -> dict[str, Any]:
        pending = self.missions.pending_statement_dispatches(limit=1)
        if not pending:
            return {"status": "idle"}
        dispatch = pending[0]
        try:
            ticket = self.launcher.start(
                ticker=dispatch["ticker"], form=dispatch["form"],
                limit=int(dispatch["filing_limit"]),
                actor_ref=dispatch["actor_ref"],
            )
        except LaneChildConflict as exc:
            return {"status": "deferred", "dispatch_ref": dispatch["dispatch_id"],
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            reason = f"{type(exc).__name__}: {exc}"
            try:
                self.missions.reject_statement_dispatch(dispatch["dispatch_id"], reason)
            except CoverageMissionError:
                pass
            return {"status": "rejected", "dispatch_ref": dispatch["dispatch_id"],
                    "reason": reason}
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], ticket["id"])
        return {"status": "launched", "dispatch_ref": dispatch["dispatch_id"],
                "company_ref": dispatch["company_ref"], "ticket_ref": ticket["id"]}

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_finished()
        queued = self._queue()
        launched = self._launch_one()
        return {
            "settled": settled, "queued": queued,
            **{key: value for key, value in launched.items()},
            "status": launched["status"],
        }


# P13ak: SEC asks a client to say who it is and how to reach it. The
# statements lane says so in its own name rather than borrowing the
# company-facts lane's, and it carries the same contact address this Core
# already publishes on its outbound public requests -- the parser refuses an
# identity without one, which is how the first live tick failed.
STATEMENT_LANE_USER_AGENT = (
    "Dalton Research Agent OS SEC financial-statements lane everflow@lumos.space"
)
# Contracts coexist.  An already-approved v2 remains usable and emits its old
# projection; an approved v3 wins only after its exact record is selected.
STATEMENT_LANE_GOVERNANCE = "sec-financial-statements-v2.json"
STATEMENT_LANE_GOVERNANCE_CANDIDATES = (
    "sec-financial-statements-v3.json",
    STATEMENT_LANE_GOVERNANCE,
)
LAUNCHER_KWARG = "statement_lane_launcher"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P13ak).

    One financial-statements child at a time: queue the companies the
    checklist covers, launch one, and record a finished child's lines into the
    ledger.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no statements lane on this writer"}
    return MissionStatementLaneCoordinator(
        missions=server.coverage_mission,
        launcher=launcher,
        checklist=server.lane_company_checklist(),
        forms=getattr(launcher, "scheduling_forms", DEFAULT_FORMS),
        filing_limits=getattr(launcher, "scheduling_limits", None),
    ).dispatch_once()


def add_arguments(parser: Any) -> None:
    # Off unless an approved governance record is named, like every other
    # connector on this writer.
    parser.add_argument(
        "--statement-lane-governance",
        help="approved sec-financial-statements governance record",
    )
    parser.add_argument(
        "--statement-lane-fixture",
        help="rehearsal only: replay a captured parse instead of reaching SEC",
    )
    parser.add_argument("--statement-lane-user-agent", default=None)
    parser.add_argument("--statement-lane-form", action="append", choices=DEFAULT_FORMS)
    parser.add_argument("--statement-lane-filing-limit", action="append", default=[],
                        metavar="FORM=N")


def build_launcher(args: Any) -> Any | None:
    if args.statement_lane_governance is None:
        return None
    from pathlib import Path as _Path

    from .sec_financials_launcher import SecFinancialsLauncher

    mode_args = (
        ("--fixture-file", args.statement_lane_fixture)
        if args.statement_lane_fixture is not None else ("--allow-network",)
    )
    forms = tuple(getattr(args, "statement_lane_form", None) or DEFAULT_FORMS)
    limits: dict[str, int] = {
        form: limit for form, limit in DEFAULT_FORM_LIMITS.items() if form in forms
    }
    explicitly_limited: set[str] = set()
    for value in getattr(args, "statement_lane_filing_limit", ()):
        try:
            form, raw_limit = value.split("=", 1)
            limit = int(raw_limit)
        except (ValueError, AttributeError) as exc:
            raise ValueError("statement filing limit must be FORM=N") from exc
        if form not in forms or not 1 <= limit <= MAX_STATEMENT_FILINGS:
            raise ValueError("statement filing limit must select a configured form and be 1..8")
        if form in explicitly_limited:
            raise ValueError("statement filing limit form must be unique")
        limits[form] = limit
        explicitly_limited.add(form)
    return SecFinancialsLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        governance_path=args.statement_lane_governance,
        mode_args=mode_args,
        user_agent=args.statement_lane_user_agent,
        scheduling_forms=forms,
        scheduling_limits=limits,
    )


def argv_fragment(context: Any) -> list[str]:
    # Independent of the Cockpit staging file -- this lane writes into the
    # mission ledger, not the Cockpit inbox -- so it is enabled by its own
    # approved record being present, and stays off on a Core without one.
    from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
    from .sec_financials_core import sec_financials_identity

    governance = None
    for version, filename in ((3, STATEMENT_LANE_GOVERNANCE_CANDIDATES[0]),
                              (2, STATEMENT_LANE_GOVERNANCE_CANDIDATES[1])):
        candidate = context.state / "connector-governance" / filename
        if not candidate.is_file():
            continue
        try:
            record = ConnectorGovernance.load(candidate)
        except ConnectorGovernanceError:
            continue
        identity = sec_financials_identity(version=version)
        if (record.approved and record.capability_id == identity["capability_id"]
                and record.wire["expected_source_hash"] == identity["source_hash"]
                and record.wire["expected_schema_hash"] == identity["schema_hash"]):
            governance = candidate
            break
    if governance is None:
        return []
    config_path = context.state / STATEMENT_LANE_CONFIG
    if config_path.exists():
        config = load_statement_lane_config(config_path)
        forms = tuple(config["forms"])
        limits = dict(config["filing_limits"])
    else:
        forms = DEFAULT_FORMS
        limits = DEFAULT_FORM_LIMITS
    argv = [
        "--statement-lane-governance", str(governance),
        "--statement-lane-user-agent", STATEMENT_LANE_USER_AGENT,
    ]
    for form in forms:
        argv += ["--statement-lane-form", form]
    for form in forms:
        if form in limits:
            argv += ["--statement-lane-filing-limit", f"{form}={limits[form]}"]
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_statements",
    order=80,
    driver_key="mission_statements",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P13ak: one company's quarterly income, balance and cash statements "
         "as filed, structure and all.",
))


__all__ = [
    "COMPANY_FAILURE_MARKERS",
    "DEFAULT_FILING_LIMIT",
    "DEFAULT_FORM",
    "DEFAULT_FORMS",
    "DEFAULT_FORM_LIMITS",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_ATTEMPTS_PER_COMPANY",
    "MAX_FAILURES_PER_COMPANY",
    "MAX_QUEUED_PER_RUN",
    "STATEMENT_LANE_GOVERNANCE",
    "STATEMENT_LANE_CONFIG",
    "STATEMENT_LANE_CONFIG_SCHEMA",
    "STATEMENT_LANE_GOVERNANCE_CANDIDATES",
    "STATEMENT_LANE_USER_AGENT",
    "MissionStatementLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "load_statement_lane_config",
    "build_launcher",
    "dispatch",
]
