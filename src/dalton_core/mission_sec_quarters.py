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
EXCUSED_GOVERNANCE = "governance"
EXCUSED_SOURCE_LAG = "source_lag"


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
    """The most recent quarterly 10-Q periods in a company-facts payload.

    Company facts carry every filing the issuer ever made.  The Playbook asks
    for the *past* four quarters, so only the newest few are candidates; live,
    without this bound the lane walked back into 2023 filings whose revenue
    concepts no longer resolve.
    """

    facts = (payload.get("facts") or {}).get("us-gaap") or {}
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for concept in facts.values():
        units = (concept or {}).get("units") or {}
        for rows in units.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping) or row.get("form") != "10-Q":
                    continue
                start, end = _parse_date(row.get("start")), _parse_date(row.get("end"))
                filed, accession = _parse_date(row.get("filed")), row.get("accn")
                if start is None or end is None or filed is None or not isinstance(accession, str):
                    continue
                span = (end - start).days
                if not MIN_QUARTER_DAYS <= span <= MAX_QUARTER_DAYS:
                    continue
                key = (start.isoformat(), end.isoformat())
                if key in seen:
                    continue
                seen[key] = {
                    "period": f"{start.isoformat()}..{end.isoformat()}",
                    "start": start.isoformat(), "end": end.isoformat(),
                    "accession": accession, "filed": filed.isoformat(),
                }
    ordered = sorted(seen.values(), key=lambda item: item["end"], reverse=True)
    return ordered[: max(1, int(limit))]


def submissions_filings(
    payload: Mapping[str, Any], *, limit: int = RECENT_FILINGS
) -> list[dict[str, Any]]:
    """The most recent 10-Qs listed in an EDGAR submissions payload.

    A submissions payload names each filing's accession, filing date and the
    period it reports, but not the period's start; the quarter is identified by
    its end, which is what a held Claim period is matched on.
    """

    recent = ((payload.get("filings") or {}).get("recent")) if isinstance(payload, Mapping) else None
    if not isinstance(recent, Mapping):
        return []
    columns = [recent.get(name) for name in ("form", "accessionNumber", "filingDate", "reportDate")]
    if not all(isinstance(column, list) for column in columns):
        return []
    seen: dict[str, dict[str, Any]] = {}
    for form, accession, filed, report in zip(*columns):
        if form != "10-Q" or not isinstance(accession, str):
            continue
        filed_day, end = _parse_date(filed), _parse_date(report)
        if filed_day is None or end is None or end.isoformat() in seen:
            continue
        seen[end.isoformat()] = {
            "period": None, "start": None, "end": end.isoformat(),
            "accession": accession, "filed": filed_day.isoformat(),
        }
    ordered = sorted(seen.values(), key=lambda item: item["end"], reverse=True)
    return ordered[: max(1, int(limit))]


def classify_failure(reason: Any) -> str | None:
    """``governance`` / ``source_lag`` for a failure that is not the filing's."""

    if not isinstance(reason, str) or not reason:
        return None
    if any(marker in reason for marker in GOVERNANCE_FAILURE_MARKERS + MISSION_DRIFT_MARKERS):
        return EXCUSED_GOVERNANCE
    if SOURCE_LAG_PATTERN.search(reason):
        return EXCUSED_SOURCE_LAG
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

    if not failed_at:
        return None
    delay = min(SOURCE_LAG_RETRY_BASE * (2 ** (len(failed_at) - 1)), SOURCE_LAG_RETRY_MAX)
    return max(failed_at) + delay


def attempt_ledger(connection: Any, state_dir: Path | None = None) -> dict[str, Any]:
    """The retry budget per accession, and the failures it does not count.

    ``counted``: dispatches that still count against ``MAX_ATTEMPTS_PER_FILING``
    -- every dispatch, less the voided ones and the ones whose failure was
    governance or source lag.  ``source_lag``: when each accession's lag
    failures happened, for the backoff.  ``excused``: how many were forgiven,
    by kind, so the report can say so.
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
        return {"counted": {}, "source_lag": {}, "excused": {}}
    counted = {row["expected_accession"]: int(row["n"])
               for row in rows if row["expected_accession"]}
    try:
        failed = connection.execute(
            "SELECT d.dispatch_id AS dispatch_id, d.expected_accession AS expected_accession, "
            "d.status AS status, d.ticket_ref AS ticket_ref, "
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
    excused: dict[str, dict[str, int]] = {}
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
        if kind is None:
            continue
        counted[accession] = max(0, counted.get(accession, 0) - 1)
        bucket = excused.setdefault(accession, {})
        bucket[kind] = bucket.get(kind, 0) + 1
        if kind == EXCUSED_SOURCE_LAG:
            when = _instant(row["settled_at"]) or _instant(row["updated_at"])
            if when is not None:
                lag.setdefault(accession, []).append(when)
    return {"counted": counted, "source_lag": lag, "excused": excused}


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
    ) -> None:
        self.store = store
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
        """10-Qs named by the newest company-facts and submissions payloads held.

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
            payload = read_artifact(self.state_dir, digest)
            if payload is None:
                continue
            held_facts = payload.get("facts")
            if facts is None and isinstance(held_facts, Mapping) \
                    and isinstance(held_facts.get("us-gaap"), Mapping):
                facts = [{**item, "observation_ref": f"sec-company-facts-artifact:{digest}"}
                         for item in quarterly_filings(payload)]
            elif listed is None and isinstance(payload.get("filings"), Mapping):
                listed = [{**item, "observation_ref": f"sec-submissions-artifact:{digest}"}
                          for item in submissions_filings(payload)]
        return [*(facts or []), *(listed or [])], any_held

    def _statement_filings(self, company_ref: str) -> list[dict[str, Any]]:
        """10-Qs the statement lane has ingested for this company.

        Each is an accession SEC served for this company, recorded under the
        mission's own governance: an observation, and the only one that is
        refreshed on a schedule as new filings land.
        """

        try:
            rows = self.connection.execute(
                "SELECT ingest_id, accession, filed, report_date "
                "FROM coverage_mission_statement_filings "
                "WHERE company_ref=? AND form='10-Q'", (company_ref,),
            ).fetchall()
        except Exception:  # noqa: BLE001 - an older Core has no statement lane
            return []
        result: list[dict[str, Any]] = []
        for row in rows:
            filed, end = _parse_date(row["filed"]), _parse_date(row["report_date"])
            if filed is None or end is None or not isinstance(row["accession"], str):
                continue
            result.append({
                "period": None, "start": None, "end": end.isoformat(),
                "accession": row["accession"], "filed": filed.isoformat(),
                "observation_ref": f"statement-ingest:{row['ingest_id']}",
            })
        return result

    @staticmethod
    def _recent_quarters(filings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """One filing per quarter end, newest first, preferring one that names its start."""

        by_end: dict[str, dict[str, Any]] = {}
        for filing in filings:
            current = by_end.get(filing["end"])
            if current is None or (current.get("start") is None and filing.get("start")):
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
                                    "reason": "原始件读不到、哈希不符或里面没有 10-Q，不据此排队"})
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
            seen_accessions: set[str] = set()
            # Only the newest four: a quarter older than those does not make the
            # weekly report current, and walking further back is how the lane
            # once spent itself on 2023 filings whose concepts no longer resolve.
            for filing in newest:
                tried = attempts.get(filing["accession"], 0)
                if filing["end"] in held_ends or filing["accession"] in seen_accessions:
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
                seen_accessions.add(filing["accession"])
                # The budget is what forgiveness restores; the window salt is
                # what keeps each queued dispatch a new row.
                wanted.append({**filing, "attempt": tried,
                               "window_salt": windows_used.get(filing["accession"], 0)})
                if len(wanted) >= len(missing):
                    break
            if not wanted:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": ("最近四个季度里缺的那几份在等 SEC company facts 收录或已试满次数"
                                           if deferred else "最近四个季度里缺的那几份都已试满次数")})
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
                        authorization=authorization, form="10-Q",
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
                    "period": filing.get("period") or f"..{filing['end']}",
                    "filed": filing["filed"], "attempt": int(filing.get("attempt", 0)) + 1,
                    "status": record.get("status", "queued"),
                })
            if queued:
                return {
                    "status": "queued", "ticker": entry.get("ticker"),
                    "company_ref": company_ref, "quarters_held": item["have"],
                    "recent_quarters_missing": [filing["end"] for filing in missing],
                    "queued": queued, "skipped": skipped,
                }
        return {"status": "idle", "skipped": skipped}

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
    ).dispatch_once()


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_sec_quarters",
    order=70,
    driver_key="mission_sec_quarters",
    handler=dispatch,
    note="P10d: the quarters a company still owes, from Core's own "
         "company-facts artifact.",
))


__all__ = [
    "LANE",
    "attempt_ledger",
    "classify_failure",
    "run_failure_text",
    "source_lag_retry_at",
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
