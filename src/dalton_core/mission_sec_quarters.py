"""P10d: fill "past four quarters of financials" from authority already held.

The Initial Screen's first gate question asks for four quarters of filings.
Live it had one per company: the SEC lane only ever ran from a bounded-planner
observation, and no loop is active, so nothing queued it.

The lane refuses an automation run that does not bind an observed accession —
correctly: automation should fetch the filing it saw, not sweep a window.  But
the accessions are already in authority.  Every SEC company-facts payload the
lane fetched is a hash-addressed artifact in the spool, and every fact row in
it carries the accession and filing date of the filing it came from.  So the
observation exists; nothing new needs to be fetched.

This coordinator reads that payload back through its own artifact hash, lists
the quarterly filings the company has, drops the periods the Ledger already
holds a Claim for, and queues the rest as SEC dispatches bound to their exact
accessions.  The existing chain does the rest: dispatch → lane → verifier →
candidate → policy commit → a quantitative Claim the Initial Screen may cite.

No new connector call, no new governance, no widened grant.

2026-09-24: "four quarters held" is not "the newest quarter held".  The gate
used to be the checklist's count of *any* quantitative Claim period, and once
the statement-line promoter admitted thousands of numbers every company had far
more than four, so the coordinator went idle for good -- CTSH's 2026-06-30 10-Q
(0001058290-26-000031, filed 2026-07-29) was observed and never dispatched,
and the weekly report repeated itself.  At the same time the newest SEC
artifact behind a company's Claims had become an EDGAR *submissions* payload
rather than company facts, so even an open gate would have found no 10-Q in
it.  The coordinator now asks the question the weekly report depends on --
does each of the newest four 10-Q quarters carry this lane's own
``quarterly_revenue_yoy_growth`` Claim -- and reads the filings it may bind to
from every observation authority already holds: company facts, submissions,
and the statement lane's ingested filings.  The statement lane is the one that
goes looking for new filings, so a new 10-Q it ingests is queued here next.

2026-09-26: two failures that say nothing about the filing stopped spending
its budget.  ws-7d's active policy lacked ``research_plan_auto_start``, so
every run refused at the lane's governance precondition -- and the coordinator
kept queuing, five minutes apart, until the 47 attempts just given back were
gone again.  The coordinator now asks that precondition itself and holds
before queuing anything, and a run that died on it (or was rejected because
the mission moved under a queued dispatch) is not an attempt.  Separately,
SEC company facts lags the filing index: CTSH's 10-Q was listed and not yet
in company facts, so each run answered "no 10-Q accession in the filing
window".  That is the source being late, not the filing being bad: it is not
counted either, and the filing is retried on a backoff instead of at once.

2026-09-28: a fiscal year's fourth quarter is never in a 10-Q.  ws-7d signed
the annual rule (``research-auto-commit:sec-public-company-facts-growth-annual:v1``)
and the lane still sat at ``idle skipped 4``: every read here was
``form='10-Q'``, so "the newest four quarters" skipped the quarter a 10-K
reports and its absence was never noticed.  The newest four are now read from
10-Qs *and* 10-Ks -- the 10-K standing for the quarter that ends on its fiscal
year end, which is its report date whatever the calendar (MSFT's June, ACN's
August, DXC's March) -- and a missing fourth quarter is queued as a 10-K
dispatch, which the lane runs under ``COMPANY_FACTS_RULE_REFS["10-K"]``.

The annual rule is the same-accession quarterly pair, nothing more: a 10-K
that reports only fiscal-year totals has no quarter to compare, and the
FY - 9M derivation was not a rule (it is now, below).  When company facts show
that a 10-K carries no fourth-quarter row, it is not queued (it could only
fail); when nothing held says either way it is queued once, and a run that
proves the 10-K annual-only ends its chase.  A 10-K the bounded-planner path
already queued is the company's open dispatch, and one it already answered is
a held period, so neither path runs a filing the other has.

2026-09-28 (later): FY - 9M is a rule now
(``research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1``,
``sec_fy_minus_9m``).  A 10-K shown to report only its fiscal year is no
longer a dead end: when the active policy lists that rule and Core holds the
10-K's filed statement rows and the same fiscal year's first three quarters,
the fourth quarter is derived here -- fiscal year less nine months, for this
year and the prior one -- staged, and offered to the Ledger, which admits it
only after rebuilding every byte from the same rows.  No connector call: the
rows are already in Core.  Anything the rule refuses (a concept change, a
restatement, fiscal boundaries that do not meet, a quarter not held, filed
precision too coarse) is reported with its reason and nothing is queued, as
before.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .lane_registry import LaneSpec, register_lane
from .raw_spool import RawSpoolError, RawSpoolReader

REQUIRED_QUARTERS = 4
MAX_QUEUED_PER_RUN = 4
# A 10-Q's quarterly row spans one quarter; the same filing also carries
# year-to-date rows, which are not a quarter and which the lane refuses.
MIN_QUARTER_DAYS = 80
MAX_QUARTER_DAYS = 100
FILING_WINDOW_DAYS = 2
MAX_ATTEMPTS_PER_FILING = 3
# How far back a "past four quarters" search may reach.  Six leaves room for a
# filing whose comparative the lane cannot resolve without wandering into old
# filings that use different concepts.
RECENT_FILINGS = 6
# The one metric this lane's SEC chain produces.  Held periods are read for it
# alone: a promoted revenue line for the same quarter is not the growth Claim
# the weekly report reads.
YOY_METRIC = "quarterly_revenue_yoy_growth"
# How many distinct SEC artifacts one company's pass may open looking for a
# company-facts and a submissions payload.  They are megabytes each.
MAX_ARTIFACTS_READ = 6
SPOOL_ROOTS = ("transcript-spool", "connector-spool", "raw-spool")
# 2026-09-26: failures that prove nothing about the filing they were spent on.
# Governance: the lane refused before it opened anything, and would refuse any
# filing equally until a policy is installed.
GOVERNANCE_FAILURE_MARKERS = (
    "does not authorize the SEC company-facts lane",
    "Core has no active governance policy",
)
# The mission moved while a dispatch sat in the queue; the drain rejects it
# for binding the old version.  A mission publish is an owner act (a policy
# cascade), not a verdict on the filing.
MISSION_DRIFT_MARKERS = (
    "SEC automation must bind the active mission version",
    "SEC automation mission hash binding failed",
    "queued SEC authorization drifted",
)
# Company facts has not caught up with the filing index yet
# (``sec_public_adapter.latest_accession``).  Retried, later, on a backoff.
SOURCE_LAG_PATTERN = re.compile(
    r"SEC company facts has no 10-[QK] accession in the filing window")
SOURCE_LAG_RETRY_BASE = timedelta(days=1)
SOURCE_LAG_RETRY_MAX = timedelta(days=7)
# 2026-09-27, ws-7d: GOOGL 0001652044-25-000062 failed with "connector
# transport exceeded the authority deadline" and was counted, two of its three
# attempts gone on the network rather than the filing.  The connector
# executor's two deadline messages (``connector_transport_executor``), and its
# ``deadline_exceeded`` code where a message does not say so itself.
TRANSPORT_TIMEOUT_PATTERN = re.compile(
    r"connector transport (?:exceeded|completed after) the authority deadline"
    r"|\bdeadline_exceeded\b")
# Half an hour, doubling, at most six: long enough for a congested link or
# a slow SEC edge to clear (the tick is five minutes, and retrying into the
# same congestion is how a timeout repeats), short enough that a filing is
# not a day late for a blip.  A persistent outage then costs at most four
# public reads a day, none of them counted.
TRANSPORT_RETRY_BASE = timedelta(minutes=30)
TRANSPORT_RETRY_MAX = timedelta(hours=6)
# 2026-09-28: the lane's answer for a 10-K that reports only fiscal-year
# totals (``sec_public_adapter.normalize_sec_company_facts``).  The filing
# will never carry a fourth-quarter pair, so one such run ends its chase.
ANNUAL_ONLY_PATTERN = re.compile(
    r"no allowlisted revenue concept resolves on the latest 10-K accession"
    r"|lacks a same-filing prior-year quarterly comparison")
QUARTERLY_FORM = "10-Q"
ANNUAL_FORM = "10-K"
# A fiscal year, as company facts spans one (364..371 days, 52/53-week years).
MIN_YEAR_DAYS = 350
EXCUSED_GOVERNANCE = "governance"
EXCUSED_SOURCE_LAG = "source_lag"
EXCUSED_TRANSPORT = "transport_timeout"


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def quarterly_filings(
    payload: Mapping[str, Any], *, limit: int = RECENT_FILINGS
) -> list[dict[str, Any]]:
    """The most recent quarters in a company-facts payload: 10-Qs and 10-Ks.

    Company facts carry every filing the issuer ever made.  The Playbook asks
    for the *past* four quarters, so only the newest few are candidates; live,
    without this bound the lane walked back into 2023 filings whose revenue
    concepts no longer resolve.

    A 10-K stands for the quarter ending on its fiscal year end (its newest
    fiscal-year row), whatever month that is.  ``fourth_quarter`` says whether
    the 10-K reports what the annual rule compares: a revenue concept the lane
    reads, for that quarter and for the same quarter a year earlier, both as
    quarters and both in this accession.  Any other quarterly row a 10-K
    carries (live: AMZN's severance, META's dividends, MSFT's dividends per
    share) is not revenue and answers nothing.  When the pair is absent the
    entry names no start: there is no quarterly revenue row to name one from.
    """

    from .research_plan import DEFAULT_REVENUE_CONCEPT_CANDIDATES

    facts = (payload.get("facts") or {}).get("us-gaap") or {}
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    # accession -> filed, fiscal year end, revenue quarters {end: start}
    annual: dict[str, dict[str, Any]] = {}
    for name, concept in facts.items():
        revenue = name in DEFAULT_REVENUE_CONCEPT_CANDIDATES
        units = (concept or {}).get("units") or {}
        for rows in units.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping) or row.get("form") not in (
                        QUARTERLY_FORM, ANNUAL_FORM):
                    continue
                start, end = _parse_date(row.get("start")), _parse_date(row.get("end"))
                filed, accession = _parse_date(row.get("filed")), row.get("accn")
                if start is None or end is None or filed is None or not isinstance(accession, str):
                    continue
                span = (end - start).days
                quarter = MIN_QUARTER_DAYS <= span <= MAX_QUARTER_DAYS
                if row["form"] == ANNUAL_FORM:
                    held = annual.setdefault(accession, {
                        "filed": filed, "year_end": None, "quarters": {}})
                    if span >= MIN_YEAR_DAYS:
                        if held["year_end"] is None or end > held["year_end"]:
                            held["year_end"] = end
                    elif quarter and revenue:
                        held["quarters"].setdefault(end, start)
                    continue
                if not quarter:
                    continue
                key = (start.isoformat(), end.isoformat())
                if key in seen:
                    continue
                seen[key] = {
                    "period": f"{start.isoformat()}..{end.isoformat()}",
                    "start": start.isoformat(), "end": end.isoformat(),
                    "accession": accession, "filed": filed.isoformat(),
                    "form": QUARTERLY_FORM,
                }
    # The quarter a 10-Q is *for* is its newest; its older quarterly rows are
    # comparatives.  Live, ACN's Q1 10-Q (0001467373-25-000222) carries a
    # 2025-06-01..2025-08-31 row, and standing in for the fiscal fourth
    # quarter it hid the 10-K that reports it -- a filing already run for its
    # own quarter, which could never answer that one.
    own_end: dict[str, str] = {}
    for item in seen.values():
        if item["end"] > own_end.get(item["accession"], ""):
            own_end[item["accession"]] = item["end"]
    by_end = {item["end"]: key for key, item in seen.items()}
    for accession, held in annual.items():
        year_end = held["year_end"]
        if year_end is None:
            continue
        standing = by_end.get(year_end.isoformat())
        if standing is not None:
            if own_end.get(seen[standing]["accession"]) == year_end.isoformat():
                continue
            del seen[standing]
            by_end.pop(year_end.isoformat())
        start = held["quarters"].get(year_end)
        # The comparative: the same quarter a year earlier, in this filing.
        paired = start is not None and any(
            350 <= (year_end - end).days <= 380
            for end in held["quarters"] if end != year_end)
        entry = {
            "period": f"{start.isoformat()}..{year_end.isoformat()}" if paired else None,
            "start": start.isoformat() if paired else None,
            "end": year_end.isoformat(), "accession": accession,
            "filed": held["filed"].isoformat(), "form": ANNUAL_FORM,
            "fourth_quarter": paired,
        }
        key = (ANNUAL_FORM, year_end.isoformat())
        current = seen.get(key)
        # Two 10-Ks for one year end (an amendment): the later filing speaks.
        if current is None or entry["filed"] > current["filed"]:
            seen[key] = entry
    ordered = sorted(seen.values(), key=lambda item: item["end"], reverse=True)
    return ordered[: max(1, int(limit))]


def submissions_filings(
    payload: Mapping[str, Any], *, limit: int = RECENT_FILINGS
) -> list[dict[str, Any]]:
    """The most recent 10-Qs and 10-Ks listed in an EDGAR submissions payload.

    A submissions payload names each filing's accession, filing date and the
    period it reports, but not the period's start; the quarter is identified by
    its end, which is what a held Claim period is matched on.  A 10-K's report
    date is its fiscal year end, which is where its fourth quarter ends;
    whether it reports that quarter as a quarter is not something the index
    says (``fourth_quarter`` is None).
    """

    recent = ((payload.get("filings") or {}).get("recent")) if isinstance(payload, Mapping) else None
    if not isinstance(recent, Mapping):
        return []
    columns = [recent.get(name) for name in ("form", "accessionNumber", "filingDate", "reportDate")]
    if not all(isinstance(column, list) for column in columns):
        return []
    seen: dict[str, dict[str, Any]] = {}
    for form, accession, filed, report in zip(*columns):
        if form not in (QUARTERLY_FORM, ANNUAL_FORM) or not isinstance(accession, str):
            continue
        filed_day, end = _parse_date(filed), _parse_date(report)
        if filed_day is None or end is None or end.isoformat() in seen:
            continue
        seen[end.isoformat()] = {
            "period": None, "start": None, "end": end.isoformat(),
            "accession": accession, "filed": filed_day.isoformat(), "form": form,
        }
        if form == ANNUAL_FORM:
            seen[end.isoformat()]["fourth_quarter"] = None
    ordered = sorted(seen.values(), key=lambda item: item["end"], reverse=True)
    return ordered[: max(1, int(limit))]


def classify_failure(reason: Any) -> str | None:
    """``governance`` / ``source_lag`` / ``transport_timeout`` for a failure
    that is not the filing's."""

    if not isinstance(reason, str) or not reason:
        return None
    if any(marker in reason for marker in GOVERNANCE_FAILURE_MARKERS + MISSION_DRIFT_MARKERS):
        return EXCUSED_GOVERNANCE
    if SOURCE_LAG_PATTERN.search(reason):
        return EXCUSED_SOURCE_LAG
    if TRANSPORT_TIMEOUT_PATTERN.search(reason):
        return EXCUSED_TRANSPORT
    return None


def run_failure_text(connection: Any, state_dir: Path | None, ticket_ref: Any) -> str | None:
    """What a settled run said went wrong, read from what it left behind.

    The settlement's ``failure_reason`` comes from the run summary's issuer
    ``error``; two failures never reach it.  A governance precondition refuses
    before any summary is written, so its only trace is ``run.log``.  A plan
    that ran and failed at the connector leaves a summary whose issuer carries
    a ``failure`` naming the result envelope, and the reason is in that
    envelope in the Core.  Both are read here rather than guessed.
    """

    if state_dir is None or not isinstance(ticket_ref, str) or ":" not in ticket_ref:
        return None
    directory = Path(state_dir) / "sec-lane-runs" / ticket_ref.split(":", 1)[1]
    try:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, Mapping):
        envelopes: list[str] = []
        for issuer in summary.get("issuers") or ():
            if not isinstance(issuer, Mapping):
                continue
            if isinstance(issuer.get("error"), str) and issuer["error"].strip():
                return issuer["error"].strip()
            failure = issuer.get("failure")
            if isinstance(failure, Mapping) and isinstance(failure.get("result_envelope_ref"), str):
                envelopes.append(failure["result_envelope_ref"])
        for ref in envelopes:
            try:
                row = connection.execute(
                    "SELECT result_envelope_json FROM scheduler_result_envelopes "
                    "WHERE result_envelope_id=? LIMIT 1", (ref,),
                ).fetchone()
                error = (json.loads(row["result_envelope_json"]).get("error") or {}) if row else {}
            except Exception:  # noqa: BLE001 - an unreadable envelope names nothing
                continue
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                code = error.get("code")
                if (isinstance(code, str) and code == "deadline_exceeded"
                        and code not in error["message"]):
                    # The code is the classification; not every deadline
                    # message spells it ("recorded source page timed out").
                    return f"{error['message']} [{code}]"
                return error["message"]
        if isinstance(summary.get("error"), str):
            return summary["error"]
        return None
    try:
        log = (directory / "run.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in log.splitlines():
        if line.strip().startswith("lane precondition failed:"):
            return line.strip()
    return None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def source_lag_retry_at(failed_at: Sequence[datetime]) -> datetime | None:
    """When a filing company facts has not caught up with may be tried again.

    A day after the first such failure, doubling to at most a week: company
    facts usually catches up within days, and one public read a week for a
    filing it never picks up costs nothing worth stopping for.
    """

    return _backoff(failed_at, SOURCE_LAG_RETRY_BASE, SOURCE_LAG_RETRY_MAX)


def transport_retry_at(failed_at: Sequence[datetime]) -> datetime | None:
    """When a filing whose connector timed out may be tried again.

    Not an attempt (``attempt_ledger``), so without a backoff the next tick
    would re-queue it into the same congestion; see ``TRANSPORT_RETRY_BASE``.
    """

    return _backoff(failed_at, TRANSPORT_RETRY_BASE, TRANSPORT_RETRY_MAX)


def _backoff(failed_at: Sequence[datetime], base: timedelta,
             ceiling: timedelta) -> datetime | None:
    if not failed_at:
        return None
    delay = min(base * (2 ** min(len(failed_at) - 1, 16)), ceiling)
    return max(failed_at) + delay


def attempt_ledger(connection: Any, state_dir: Path | None = None) -> dict[str, Any]:
    """The retry budget per accession, and the failures it does not count.

    ``counted``: dispatches that still count against ``MAX_ATTEMPTS_PER_FILING``
    -- every dispatch, less the voided ones and the ones whose failure was
    governance, source lag or a connector transport timeout.  ``source_lag``
    and ``transport``: when each accession's failures of that kind happened,
    for their backoffs.  ``excused``: how many were forgiven, by kind, so the
    report can say so.  ``annual_only``: 10-K accessions a run has shown to
    carry no fourth-quarter pair (2026-09-28); they are not chased again.
    """

    from .coverage_mission import SEC_RUN_SUCCEEDED

    try:
        rows = connection.execute(
            "SELECT d.expected_accession AS expected_accession, COUNT(*) AS n "
            "FROM coverage_mission_sec_dispatches d "
            "LEFT JOIN coverage_mission_sec_dispatch_attempt_voids v "
            "ON v.dispatch_id=d.dispatch_id "
            "WHERE v.dispatch_id IS NULL GROUP BY d.expected_accession"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {"counted": {}, "source_lag": {}, "transport": {}, "excused": {},
                "annual_only": set()}
    counted = {row["expected_accession"]: int(row["n"])
               for row in rows if row["expected_accession"]}
    try:
        failed = connection.execute(
            "SELECT d.dispatch_id AS dispatch_id, d.expected_accession AS expected_accession, "
            "d.status AS status, d.ticket_ref AS ticket_ref, d.form AS form, "
            "d.failure_reason AS dispatch_reason, d.updated_at AS updated_at, "
            "s.failure_reason AS settled_reason, s.settled_at AS settled_at "
            "FROM coverage_mission_sec_dispatches d "
            "LEFT JOIN coverage_mission_sec_dispatch_settlements s "
            "ON s.dispatch_id=d.dispatch_id "
            "LEFT JOIN coverage_mission_sec_dispatch_attempt_voids v "
            "ON v.dispatch_id=d.dispatch_id "
            "WHERE v.dispatch_id IS NULL AND (d.status='rejected' "
            "OR (s.dispatch_id IS NOT NULL AND s.detail IS NOT ?))",
            (SEC_RUN_SUCCEEDED,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - an older Core has no settlement journal
        failed = []
    lag: dict[str, list[datetime]] = {}
    transport: dict[str, list[datetime]] = {}
    excused: dict[str, dict[str, int]] = {}
    annual_only: set[str] = set()
    for row in failed:
        accession = row["expected_accession"]
        if not accession:
            continue
        if row["status"] == "rejected":
            # Only a rejection for the mission moving is excused; any other
            # refusal is left counting, as before.
            kind = classify_failure(row["dispatch_reason"])
            if kind != EXCUSED_GOVERNANCE:
                continue
        else:
            reason = row["settled_reason"] or run_failure_text(
                connection, state_dir, row["ticket_ref"])
            kind = classify_failure(reason)
            if (kind is None and _column(row, "form") == ANNUAL_FORM
                    and isinstance(reason, str) and ANNUAL_ONLY_PATTERN.search(reason)):
                annual_only.add(accession)
        if kind is None:
            continue
        counted[accession] = max(0, counted.get(accession, 0) - 1)
        bucket = excused.setdefault(accession, {})
        bucket[kind] = bucket.get(kind, 0) + 1
        if kind in (EXCUSED_SOURCE_LAG, EXCUSED_TRANSPORT):
            when = _instant(row["settled_at"]) or _instant(row["updated_at"])
            if when is not None:
                (lag if kind == EXCUSED_SOURCE_LAG else transport).setdefault(
                    accession, []).append(when)
    return {"counted": counted, "source_lag": lag, "transport": transport,
            "excused": excused, "annual_only": annual_only}


def _column(row: Any, name: str, default: Any = None) -> Any:
    """``row[name]`` for a sqlite3.Row or a mapping that may lack the column."""

    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def accession_in_hand(connection: Any, accession: str) -> dict[str, Any] | None:
    """A dispatch of this accession that is still open or already succeeded.

    2026-09-28: two paths queue SEC dispatches -- this coordinator and the
    bounded-planner observation follow-up (``writer_server``) -- and a
    dispatch's identity includes its observation and window, so the same
    accession queued by both is two dispatches, two plans and, if both run,
    two Claims for one quarter.  The coordinator already stands aside while a
    company has an open dispatch and once the period is held; this is the
    same question asked from the other side, by accession.
    """

    from .coverage_mission import SEC_RUN_SUCCEEDED

    try:
        row = connection.execute(
            "SELECT d.dispatch_id AS dispatch_id, d.form AS form, d.status AS status, "
            "s.detail AS detail FROM coverage_mission_sec_dispatches d "
            "LEFT JOIN coverage_mission_sec_dispatch_settlements s "
            "ON s.dispatch_id=d.dispatch_id "
            "WHERE d.expected_accession=? AND ("
            "(d.status IN ('pending','launched') AND s.dispatch_id IS NULL) "
            "OR s.detail=?) ORDER BY d.created_at DESC LIMIT 1",
            (accession, SEC_RUN_SUCCEEDED),
        ).fetchone()
    except Exception:  # noqa: BLE001 - an older Core has no dispatch journal
        return None
    if row is None:
        return None
    return {"dispatch_id": row["dispatch_id"], "form": row["form"],
            "state": "succeeded" if row["detail"] == SEC_RUN_SUCCEEDED else "open"}


def read_artifact(state_dir: Path, content_sha256: str) -> Mapping[str, Any] | None:
    """The exact artifact bytes, verified against the hash authority recorded."""

    for name in SPOOL_ROOTS:
        try:
            reader = RawSpoolReader(state_dir / name)
        except RawSpoolError:
            continue
        if not reader.object_exists(content_sha256):
            continue
        try:
            raw = reader.read_object(content_sha256)
        except RawSpoolError:
            return None
        if hashlib.sha256(raw).hexdigest() != content_sha256:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, Mapping) else None
    return None


# digest -> ("facts" | "submissions" | None, filings).  An artifact is
# content-addressed and verified against its hash, so what it lists never
# changes; since 10-Ks joined the newest four, a company whose fourth quarter
# no rule can answer is re-examined every tick, and re-parsing megabytes of
# company facts to learn the same thing each time is waste.
_ARTIFACT_VIEWS: dict[str, tuple[str | None, list[dict[str, Any]]]] = {}
_ARTIFACT_VIEWS_MAX = 64


def _artifact_view(state_dir: Path, digest: str) -> tuple[str | None, list[dict[str, Any]]]:
    held = _ARTIFACT_VIEWS.get(digest)
    if held is not None:
        return held
    payload = read_artifact(state_dir, digest)
    if payload is None:
        # Not cached: a missing or corrupt object may be restored.
        return None, []
    view: tuple[str | None, list[dict[str, Any]]] = (None, [])
    held_facts = payload.get("facts")
    if isinstance(held_facts, Mapping) and isinstance(held_facts.get("us-gaap"), Mapping):
        view = ("facts", quarterly_filings(payload))
    elif isinstance(payload.get("filings"), Mapping):
        view = ("submissions", submissions_filings(payload))
    if len(_ARTIFACT_VIEWS) >= _ARTIFACT_VIEWS_MAX:
        _ARTIFACT_VIEWS.pop(next(iter(_ARTIFACT_VIEWS)))
    _ARTIFACT_VIEWS[digest] = view
    return view


class MissionSecQuartersCoordinator:
    """Queue the SEC dispatches a company still needs, one company per tick."""

    def __init__(
        self,
        *,
        store: Any,
        missions: Any,
        state_dir: str | Path,
        checklist: Callable[[], Sequence[Mapping[str, Any]]],
        clock: Callable[[], datetime] | None = None,
        governance_check: Callable[[], Any] | None = None,
        staging: Any | None = None,
    ) -> None:
        self.store = store
        # The shared candidate staging store (the writer's
        # ``research_review.candidate_staging_path``).  Only the FY - 9M path
        # uses it; without one a derivable fourth quarter is reported, not
        # staged.
        self.staging = staging
        self.connection = store.connection
        self.missions = missions
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.checklist = checklist
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.governance_check = governance_check or self._core_governance

    def _core_governance(self) -> Any:
        from .sec_company_facts_lane import check_core_governance_rules

        return check_core_governance_rules(self.store)

    def _governance_hold(self) -> str | None:
        """Why the lane would refuse any run right now, or None.

        2026-09-26: the lane checks the active policy for its two rules before
        it does anything, and refuses every run while they are missing.  The
        coordinator used to queue anyway, and each refused run was an attempt;
        on ws-7d that spent the retry budget of every filing every company
        needed, five minutes at a time.  Asking first costs one policy read.
        Anything the check cannot answer holds too: queueing on a guess is
        how the budget went.
        """

        try:
            self.governance_check()
        except Exception as exc:  # noqa: BLE001 - LanePreconditionError or unreadable
            return f"{type(exc).__name__}: {exc}"
        return None

    # -- authority reads -----------------------------------------------------

    def _facts_artifact(self, company_ref: str) -> str | None:
        """The newest SEC artifact hash behind this company's Claims, of any kind."""

        return next(iter(self._sec_artifacts(company_ref)), None)

    def _sec_artifacts(self, company_ref: str):
        """SEC artifact hashes behind this company's Claims, newest first, once each."""

        seen: set[str] = set()
        rows = self.connection.execute(
            "SELECT e.evidence_json AS evidence_json, e.created_at AS created_at "
            "FROM evidence_relations r "
            "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
            "JOIN claim_versions c ON c.claim_version_id=r.claim_version_id "
            "WHERE json_extract(c.claim_json,'$.subject_ref')=? "
            "AND json_extract(c.claim_json,'$.value') IS NOT NULL "
            "ORDER BY e.created_at DESC", (company_ref,),
        ).fetchall()
        for row in rows:
            evidence = json.loads(row["evidence_json"])
            if evidence.get("source_ref") != "source:sec-edgar":
                continue
            for item in evidence.get("artifact_refs") or ():
                ref = item.get("ref") if isinstance(item, Mapping) else None
                if not isinstance(ref, str) or not ref.startswith("artifact-version:"):
                    continue
                artifact = self.connection.execute(
                    "SELECT artifact_content_hash FROM observability_artifact_versions_v2 "
                    "WHERE version_id=?", (ref,),
                ).fetchone()
                if artifact is not None and artifact["artifact_content_hash"] not in seen:
                    seen.add(artifact["artifact_content_hash"])
                    yield artifact["artifact_content_hash"]

    def _artifact_filings(self, company_ref: str) -> tuple[list[dict[str, Any]], bool]:
        """10-Qs and 10-Ks named by the newest company-facts and submissions payloads held.

        The second value says whether any SEC artifact was held at all.  The
        newest artifact is not necessarily company facts -- live it was a
        submissions payload -- so each is opened and recognised by its shape.
        """

        facts: list[dict[str, Any]] | None = None
        listed: list[dict[str, Any]] | None = None
        any_held = False
        for count, digest in enumerate(self._sec_artifacts(company_ref)):
            any_held = True
            if count >= MAX_ARTIFACTS_READ or (facts is not None and listed is not None):
                break
            kind, items = _artifact_view(self.state_dir, digest)
            if kind == "facts" and facts is None:
                facts = [{**item, "observation_ref": f"sec-company-facts-artifact:{digest}"}
                         for item in items]
            elif kind == "submissions" and listed is None:
                listed = [{**item, "observation_ref": f"sec-submissions-artifact:{digest}"}
                          for item in items]
        return [*(facts or []), *(listed or [])], any_held

    def _statement_filings(self, company_ref: str) -> list[dict[str, Any]]:
        """10-Qs and 10-Ks the statement lane has ingested for this company.

        Each is an accession SEC served for this company, recorded under the
        mission's own governance: an observation, and the only one that is
        refreshed on a schedule as new filings land.
        """

        try:
            rows = self.connection.execute(
                "SELECT ingest_id, accession, form, filed, report_date "
                "FROM coverage_mission_statement_filings "
                "WHERE company_ref=? AND form IN ('10-Q','10-K')", (company_ref,),
            ).fetchall()
        except Exception:  # noqa: BLE001 - an older Core has no statement lane
            return []
        result: list[dict[str, Any]] = []
        for row in rows:
            filed, end = _parse_date(row["filed"]), _parse_date(row["report_date"])
            if filed is None or end is None or not isinstance(row["accession"], str):
                continue
            form = _column(row, "form") or QUARTERLY_FORM
            item = {
                "period": None, "start": None, "end": end.isoformat(),
                "accession": row["accession"], "filed": filed.isoformat(),
                "observation_ref": f"statement-ingest:{row['ingest_id']}",
                "form": form,
            }
            if form == ANNUAL_FORM:
                # The statement lane reads the 10-K's statements, not its
                # quarterly note; whether a Q4 pair is in company facts is
                # the company-facts artifact's to say.
                item["fourth_quarter"] = None
            result.append(item)
        return result

    @staticmethod
    def _recent_quarters(filings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """One filing per quarter end, newest first, preferring the best-known one.

        A filing that names its start beats one that does not; after that, a
        10-K whose company facts say whether it carries a fourth-quarter row
        beats one only the index or the statement lane listed.
        """

        def known(item: Mapping[str, Any]) -> tuple[bool, bool]:
            return (item.get("start") is not None,
                    item.get("form") != ANNUAL_FORM or item.get("fourth_quarter") is not None)

        by_end: dict[str, dict[str, Any]] = {}
        for filing in filings:
            current = by_end.get(filing["end"])
            if current is None or known(filing) > known(current):
                by_end[filing["end"]] = dict(filing)
        ordered = sorted(by_end.values(), key=lambda item: item["end"], reverse=True)
        return ordered[:RECENT_FILINGS]

    def _held_periods(self, company_ref: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT claim_version_id AS id, json_extract(claim_json,'$.period') AS period "
            "FROM claim_versions WHERE json_extract(claim_json,'$.subject_ref')=? "
            "AND json_extract(claim_json,'$.metric_or_aspect')=? "
            "AND json_extract(claim_json,'$.value') IS NOT NULL", (company_ref, YOY_METRIC),
        ).fetchall()
        from .claim_retirement import retired_claim_version_refs

        try:
            # Retired less reinstated (2026-09-24).
            retired = retired_claim_version_refs(self.connection)
        except Exception:  # noqa: BLE001 - an older Core has no retirements
            retired = set()
        return {row["period"] for row in rows if row["id"] not in retired and row["period"]}

    def _open_dispatches(self, company_ref: str) -> int:
        """Filings already queued for this company and not yet finished.

        One at a time: the tick drains one dispatch and the coordinator would
        otherwise queue three more, so the queue grows faster than the lane can
        run it.
        """

        try:
            row = self.connection.execute(
                # P12b: "not yet finished" has to mean it. 'launched' was
                # terminal in practice -- nothing ever moved a dispatch out of
                # it -- so after the first batch every company looked
                # permanently busy and no new quarter was ever dispatched. The
                # settlement journal is what says a run is over.
                "SELECT COUNT(*) AS n FROM coverage_mission_sec_dispatches d "
                "LEFT JOIN coverage_mission_sec_dispatch_settlements s "
                "ON s.dispatch_id=d.dispatch_id "
                "WHERE d.company_ref=? AND d.status IN ('pending','launched') "
                "AND s.dispatch_id IS NULL", (company_ref,),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return 0
        return int(row["n"]) if row else 0

    def _dispatch_attempts(self) -> dict[str, int]:
        """How many times each accession has been queued already.

        A plan that terminated is terminal by design: the lane keys it by its
        parameters, so re-queuing the identical window replays the failure.
        Live, three windows queued while the capability descriptor was stale
        are permanently dead that way.  Each retry therefore widens the filing
        window by a day, which is a different plan, and stops after three.

        P13z: that reasoning holds only while the failure is a property of the
        filing.  A connector-profile conflict killed every SEC run for a day
        and would have killed any window equally; it spent all three attempts
        on every filing five companies still needed and told nobody anything
        about those filings.  A voided attempt is one somebody has recorded as
        proving nothing, and it does not count against the budget.

        2026-09-26: nor do the two failures that are never the filing's -- a
        governance precondition (or a mission publish) refusing the run, and
        company facts not having caught up with the filing yet.  Those are
        recognised from what the run left behind, see ``attempt_ledger``.
        2026-09-27: nor a connector transport timeout, which is the network's
        and is retried on its own backoff (``transport_retry_at``).
        """

        return attempt_ledger(self.connection, getattr(self, "state_dir", None))["counted"]

    def _attempt_ledger(self) -> dict[str, Any]:
        return attempt_ledger(self.connection, getattr(self, "state_dir", None))

    def _dispatch_windows_used(self) -> dict[str, int]:
        """How many windows each accession has already been queued under.

        P13z: the attempt count was doing two jobs -- the retry budget, and the
        salt that widens the filing window by a day so a retry is a *different*
        dispatch. Voiding an attempt is meant to give back only the first, but
        it gave back both: the coordinator recomputed a window it had already
        used, the queue call replayed that settled dispatch instead of writing
        a new one, and nothing was ever left pending for the lane to run. The
        ledger showed "queued" while the lane sat idle.

        A voided attempt still consumed its window, so this counts every
        dispatch ever made. The budget forgives; the calendar does not.
        """

        try:
            rows = self.connection.execute(
                "SELECT expected_accession, COUNT(*) AS n "
                "FROM coverage_mission_sec_dispatches GROUP BY expected_accession"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return {}
        return {row["expected_accession"]: int(row["n"]) for row in rows if row["expected_accession"]}

    # -- the pass ------------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        try:
            companies = list(self.checklist())
        except Exception as exc:  # noqa: BLE001 - report, never crash the tick
            return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
        hold = self._governance_hold()
        if hold is not None:
            # Nothing is queued, so nothing is spent: the next tick asks again
            # and queues as soon as the policy authorizes the lane.
            return {"status": "held", "reason": "lane precondition: " + hold,
                    "queued": [], "skipped": []}
        ledger = self._attempt_ledger()
        attempts = ledger["counted"]
        lagging = ledger["source_lag"]
        timed_out = ledger.get("transport") or {}
        annual_only = ledger.get("annual_only") or set()
        now = self.clock()
        windows_used = self._dispatch_windows_used()
        skipped: list[dict[str, Any]] = []
        for entry in companies:
            item = next(
                (i for i in entry.get("items", ()) if i["item_ref"] == "quarterly_financials"), None
            )
            if item is None:
                continue
            company_ref = entry["company_ref"]
            open_count = self._open_dispatches(company_ref)
            if open_count:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": f"已有 {open_count} 份 filing 在队列里等着跑"})
                continue
            held = self._held_periods(company_ref)
            held_ends = {period.rsplit("..", 1)[-1] for period in held if ".." in period}
            # The cheap observation first. When the statement lane's own
            # filings already show the newest four quarters answered, there is
            # nothing to queue and no reason to open megabytes of artifacts.
            filings = self._statement_filings(company_ref)
            recent = self._recent_quarters(filings)
            newest = recent[:REQUIRED_QUARTERS]
            if len(newest) < REQUIRED_QUARTERS or any(
                    filing["end"] not in held_ends for filing in newest):
                from_artifacts, any_artifact = self._artifact_filings(company_ref)
                if not filings and not any_artifact:
                    skipped.append({"ticker": entry.get("ticker"),
                                    "reason": "还没有这家公司的 SEC 财务数据原始件"})
                    continue
                recent = self._recent_quarters([*from_artifacts, *filings])
                newest = recent[:REQUIRED_QUARTERS]
                if not recent:
                    skipped.append({"ticker": entry.get("ticker"),
                                    "reason": "原始件读不到、哈希不符或里面没有 10-Q/10-K，不据此排队"})
                    continue
            missing = [filing for filing in newest if filing["end"] not in held_ends]
            if not missing:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": "最近四个季度的同比增速都已入账"})
                continue
            # One dispatch per filing: a 10-Q also reports the prior-year
            # quarter, so the same accession can answer two periods and running
            # it twice would spend the lane on a filing already fetched.
            wanted: list[dict[str, Any]] = []
            deferred = 0
            no_rule = 0
            derivations: list[dict[str, Any]] = []
            seen_accessions: set[str] = set()
            # Only the newest four: a quarter older than those does not make the
            # weekly report current, and walking further back is how the lane
            # once spent itself on 2023 filings whose concepts no longer resolve.
            for filing in newest:
                tried = attempts.get(filing["accession"], 0)
                if filing["end"] in held_ends or filing["accession"] in seen_accessions:
                    continue
                if filing.get("form") == ANNUAL_FORM and (
                        filing.get("fourth_quarter") is False
                        or filing["accession"] in annual_only):
                    # The annual rule compares a fourth quarter with the same
                    # quarter a year earlier, both reported by this 10-K.  A
                    # 10-K of fiscal-year totals has neither: queued, it could
                    # only fail.  Its fourth quarter is derived instead, from
                    # the filed rows Core already holds (FY - 9M).
                    derived = self._derive_fourth_quarter(company_ref, filing)
                    derivations.append(derived)
                    if derived["status"] == "committed":
                        held_ends.add(filing["end"])
                        continue
                    no_rule += 1
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "form": ANNUAL_FORM, "period_end": filing["end"],
                                    "reason": derived["reason"]})
                    continue
                if tried >= MAX_ATTEMPTS_PER_FILING:
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "reason": f"这份 filing 已经试过 {tried} 次"})
                    continue
                retry_at = source_lag_retry_at(lagging.get(filing["accession"], ()))
                if retry_at is not None and now < retry_at:
                    deferred += 1
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "reason": "SEC company facts 还没收录这份 filing，"
                                              f"{retry_at.isoformat(timespec='minutes')} 后再试",
                                    "retry_at": retry_at.isoformat()})
                    continue
                transport_at = transport_retry_at(timed_out.get(filing["accession"], ()))
                if transport_at is not None and now < transport_at:
                    deferred += 1
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "reason": "上次连 SEC 超时（不计入次数），"
                                              f"{transport_at.isoformat(timespec='minutes')} 后再试",
                                    "retry_at": transport_at.isoformat()})
                    continue
                seen_accessions.add(filing["accession"])
                # The budget is what forgiveness restores; the window salt is
                # what keeps each queued dispatch a new row.
                wanted.append({**filing, "attempt": tried,
                               "window_salt": windows_used.get(filing["accession"], 0)})
                if len(wanted) >= len(missing):
                    break
            committed = [d for d in derivations if d["status"] == "committed"]
            if not wanted and committed:
                return {
                    "status": "committed", "ticker": entry.get("ticker"),
                    "company_ref": company_ref, "quarters_held": item["have"],
                    "recent_quarters_missing": [filing["end"] for filing in missing],
                    "queued": [], "derived": derivations, "skipped": skipped,
                }
            if not wanted:
                if no_rule == len(missing):
                    reason = ("最近四个季度里缺的只有第四季，那份 10-K 只报全年数，"
                              "FY−9M 推导也没有成立（原因见同一家公司的上一条）")
                elif deferred:
                    reason = "最近四个季度里缺的那几份在等 SEC company facts 收录、在超时退避中或已试满次数"
                else:
                    reason = "最近四个季度里缺的那几份都已试满次数" + (
                        "或是只报全年数的 10-K" if no_rule else "")
                skipped.append({"ticker": entry.get("ticker"), "reason": reason})
                continue
            try:
                authorization = self.missions.authorize_sec_lane(
                    company_ref=company_ref, ticker=entry["ticker"],
                    actor_ref=self._automation(),
                )
            except Exception as exc:  # noqa: BLE001 - a refused grant is reported
                skipped.append({"ticker": entry.get("ticker"), "reason": f"{type(exc).__name__}: {exc}"})
                continue
            queued: list[dict[str, Any]] = []
            for filing in wanted[:MAX_QUEUED_PER_RUN]:
                filed = date.fromisoformat(filing["filed"])
                span = FILING_WINDOW_DAYS + int(filing.get("window_salt", 0))
                try:
                    record = self.missions.queue_sec_dispatch(
                        # The form selects the lane's rule: a 10-K runs under
                        # COMPANY_FACTS_RULE_REFS["10-K"], the annual pair.
                        authorization=authorization, form=filing.get("form") or QUARTERLY_FORM,
                        filed_from=(filed - timedelta(days=span)).isoformat(),
                        filed_to=(filed + timedelta(days=span)).isoformat(),
                        expected_accession=filing["accession"],
                        observation_ref=filing["observation_ref"],
                    )
                except Exception as exc:  # noqa: BLE001
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "reason": f"{type(exc).__name__}: {exc}"})
                    continue
                queued.append({
                    "accession": filing["accession"],
                    "form": filing.get("form") or QUARTERLY_FORM,
                    "period": filing.get("period") or f"..{filing['end']}",
                    "filed": filing["filed"], "attempt": int(filing.get("attempt", 0)) + 1,
                    "status": record.get("status", "queued"),
                })
            if queued:
                return {
                    "status": "queued", "ticker": entry.get("ticker"),
                    "company_ref": company_ref, "quarters_held": item["have"],
                    "recent_quarters_missing": [filing["end"] for filing in missing],
                    "queued": queued, "derived": derivations, "skipped": skipped,
                }
        return {"status": "idle", "skipped": skipped}

    # -- FY - 9M -------------------------------------------------------------

    def _derive_fourth_quarter(self, company_ref: str,
                               filing: Mapping[str, Any]) -> dict[str, Any]:
        """Derive, stage and offer one annual-only 10-K's fourth quarter.

        ``status`` is ``committed`` (the Ledger took it), ``staged`` (verified
        and staged, the Ledger said no and why), ``refused`` (the rule does not
        hold for these rows), ``unsigned`` or ``unavailable``.  Every status but
        ``committed`` carries the reason the skip list shows.
        """

        from .quantitative_claim_promotion import QuantitativeClaimPromotionError
        from .quantitative_claim_promotion_cli import WRITE_SCOPES, _signed_rules
        from .research_auto_commit import (
            SEC_FY_MINUS_9M_RULE_REF,
            ResearchAutoCommitRejected,
        )
        from .research_verification import ResearchVerificationError
        from .sec_fy_minus_9m import FyMinus9mRefused, annual_filing, derive
        from .store import GateRejected

        head = "这份 10-K 只报全年数，没有第四季单季行；"
        result: dict[str, Any] = {
            "accession": filing["accession"], "form": ANNUAL_FORM,
            "period_end": filing["end"], "rule_ref": SEC_FY_MINUS_9M_RULE_REF,
        }
        try:
            signed = _signed_rules(self.store.active_policy())
        except Exception:  # noqa: BLE001 - no readable policy signs nothing
            signed = frozenset()
        if SEC_FY_MINUS_9M_RULE_REF not in signed:
            return {**result, "status": "unsigned",
                    "reason": head + f"全年减前三季（FY−9M）规则 {SEC_FY_MINUS_9M_RULE_REF} "
                                     "还没有签入当前策略，不推导也不排队"}
        annual = annual_filing(self.connection, company_ref=company_ref,
                               accession=filing["accession"])
        if annual is None:
            return {**result, "status": "unavailable",
                    "reason": head + "它的申报行还没有由报表车道入库，FY−9M 无从推导"}
        try:
            derivation = derive(self.connection, annual["ingest_id"])
        except FyMinus9mRefused as exc:
            return {**result, "status": "refused", "reason": head + f"FY−9M 推导不成立：{exc}"}
        except (QuantitativeClaimPromotionError, ResearchVerificationError) as exc:
            return {**result, "status": "refused",
                    "reason": head + f"FY−9M 推导失败：{type(exc).__name__}: {exc}"}
        result.update({"period": derivation["current"]["q4"]["period"],
                       "value": derivation["growth"],
                       "q4": derivation["current"]["q4"]["value"],
                       "prior_q4": derivation["prior"]["q4"]["value"]})
        if self.staging is None:
            return {**result, "status": "unavailable",
                    "reason": head + "FY−9M 可以推导，但本车道没有配置候选暂存库，不写入"}
        try:
            mission = self.missions.mission(self._active_mission_version())
        except Exception as exc:  # noqa: BLE001 - no mission writes nothing
            return {**result, "status": "unavailable",
                    "reason": head + f"读不到生效中的任务：{type(exc).__name__}: {exc}"}
        missing = WRITE_SCOPES - set(mission["autonomy"]["may_write"])
        if missing:
            return {**result, "status": "unavailable",
                    "reason": head + f"任务还没有授予 {sorted(missing)} 写入范围，FY−9M 结论算好但不写入"}
        from .sec_fy_minus_9m import stage_fy_minus_9m_candidate

        actor = mission["autonomy"]["automation_principal"]
        try:
            bundle = stage_fy_minus_9m_candidate(
                self.connection, self.staging, ingest_id=annual["ingest_id"], actor_ref=actor)
        except (QuantitativeClaimPromotionError, ResearchVerificationError) as exc:
            return {**result, "status": "refused",
                    "reason": head + f"FY−9M 候选暂存被拒：{type(exc).__name__}: {exc}"}
        result["candidate_claim_ref"] = bundle["claim"]["id"]
        try:
            promoted = self.store.commit_policy_candidate(
                evidence=bundle["evidence"], claim=bundle["claim"],
                material=bundle["material"], numeric_spec=bundle["numeric_spec"],
                source_verification=bundle["source_verification"],
                numeric_verification=bundle["numeric_verification"],
                idempotency_key="policy-ledger:" + bundle["claim"]["id"])
        except (ResearchAutoCommitRejected, GateRejected) as exc:
            return {**result, "status": "staged",
                    "reason": head + f"FY−9M 候选已暂存，入账被拒：{type(exc).__name__}: {exc}"}
        return {**result, "status": "committed",
                "claim_version_ref": promoted.get("claim_version_ref"),
                "ledger_write": promoted.get("status") == "fresh"}

    def _active_mission_version(self) -> str:
        rows = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchall()
        if not rows:
            raise LookupError("no active mission")
        return rows[0]["mission_version_id"]

    def _automation(self) -> str:
        rows = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchall()
        if not rows:
            raise LookupError("no active mission")
        return self.missions.mission(rows[0]["mission_version_id"])["autonomy"]["automation_principal"]


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P10d).

    Queue the filings a company still needs for its four quarters, read out of
    the company-facts artifact authority already holds; the SEC lane
    dispatcher drains the queue.  The lane has no launcher of its own, so it
    is never ``unconfigured``: with no mission it simply queues nothing.
    """

    return MissionSecQuartersCoordinator(
        store=server.store,
        missions=server.coverage_mission,
        state_dir=server.state_dir,
        checklist=server.lane_company_checklist(),
        staging=_server_staging(server),
    ).dispatch_once()


def _server_staging(server: Any) -> Any | None:
    """The writer's candidate staging store, or None when it has none."""

    try:
        return server.candidate_staging
    except Exception:  # noqa: BLE001 - WriterServerError: not configured
        return None


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_sec_quarters",
    order=70,
    driver_key="mission_sec_quarters",
    handler=dispatch,
    note="P10d: the quarters a company still owes, from Core's own "
         "company-facts artifact.",
))


__all__ = [
    "ANNUAL_FORM",
    "LANE",
    "QUARTERLY_FORM",
    "accession_in_hand",
    "attempt_ledger",
    "classify_failure",
    "run_failure_text",
    "source_lag_retry_at",
    "transport_retry_at",
    "MAX_ATTEMPTS_PER_FILING",
    "MAX_QUEUED_PER_RUN",
    "RECENT_FILINGS",
    "YOY_METRIC",
    "MissionSecQuartersCoordinator",
    "REQUIRED_QUARTERS",
    "dispatch",
    "quarterly_filings",
    "read_artifact",
    "submissions_filings",
]
