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
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REQUIRED_QUARTERS = 4
MAX_QUEUED_PER_RUN = 4
# A 10-Q's quarterly row spans one quarter; the same filing also carries
# year-to-date rows, which are not a quarter and which the lane refuses.
MIN_QUARTER_DAYS = 80
MAX_QUARTER_DAYS = 100
FILING_WINDOW_DAYS = 2
MAX_ATTEMPTS_PER_FILING = 3
SPOOL_ROOTS = ("transcript-spool", "connector-spool", "raw-spool")


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def quarterly_filings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Distinct quarterly 10-Q periods in a company-facts payload, newest first."""

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
    return sorted(seen.values(), key=lambda item: item["end"], reverse=True)


def read_artifact(state_dir: Path, content_sha256: str) -> Mapping[str, Any] | None:
    """The exact artifact bytes, verified against the hash authority recorded."""

    for name in SPOOL_ROOTS:
        path = state_dir / name / "connector-spool" / "objects" / content_sha256[:2] / content_sha256
        if not path.is_file():
            continue
        raw = path.read_bytes()
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
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.missions = missions
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.checklist = checklist
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- authority reads -----------------------------------------------------

    def _facts_artifact(self, company_ref: str) -> str | None:
        """The newest SEC company-facts artifact hash behind this company's Claims."""

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
                if artifact is not None:
                    return artifact["artifact_content_hash"]
        return None

    def _held_periods(self, company_ref: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT claim_version_id AS id, json_extract(claim_json,'$.period') AS period "
            "FROM claim_versions WHERE json_extract(claim_json,'$.subject_ref')=? "
            "AND json_extract(claim_json,'$.value') IS NOT NULL", (company_ref,),
        ).fetchall()
        try:
            retired = {
                r["claim_version_ref"] for r in self.connection.execute(
                    "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
                ).fetchall()
            }
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
                "SELECT COUNT(*) AS n FROM coverage_mission_sec_dispatches "
                "WHERE company_ref=? AND status IN ('pending','launched')", (company_ref,),
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
        """

        try:
            rows = self.connection.execute(
                "SELECT expected_accession, COUNT(*) AS n FROM coverage_mission_sec_dispatches "
                "GROUP BY expected_accession"
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
        attempts = self._dispatch_attempts()
        skipped: list[dict[str, Any]] = []
        for entry in companies:
            item = next(
                (i for i in entry.get("items", ()) if i["item_ref"] == "quarterly_financials"), None
            )
            if item is None or item["have"] >= REQUIRED_QUARTERS:
                if item is not None:
                    skipped.append({"ticker": entry.get("ticker"), "reason": "已有四个季度"})
                continue
            company_ref = entry["company_ref"]
            open_count = self._open_dispatches(company_ref)
            if open_count:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": f"已有 {open_count} 份 filing 在队列里等着跑"})
                continue
            digest = self._facts_artifact(company_ref)
            if digest is None:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": "还没有这家公司的 SEC 财务数据原始件"})
                continue
            payload = read_artifact(self.state_dir, digest)
            if payload is None:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": "原始件读不到或哈希不符，不据此排队"})
                continue
            held = self._held_periods(company_ref)
            # One dispatch per filing: a 10-Q also reports the prior-year
            # quarter, so the same accession can answer two periods and running
            # it twice would spend the lane on a filing already fetched.
            wanted: list[dict[str, Any]] = []
            seen_accessions: set[str] = set()
            for filing in quarterly_filings(payload):
                tried = attempts.get(filing["accession"], 0)
                if filing["period"] in held or filing["accession"] in seen_accessions:
                    continue
                if tried >= MAX_ATTEMPTS_PER_FILING:
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "reason": f"这份 filing 已经试过 {tried} 次"})
                    continue
                seen_accessions.add(filing["accession"])
                wanted.append({**filing, "attempt": tried})
                if len(wanted) >= max(1, REQUIRED_QUARTERS - item["have"]):
                    break
            if not wanted:
                skipped.append({"ticker": entry.get("ticker"),
                                "reason": "原始件里没有还没入库的季度"})
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
                span = FILING_WINDOW_DAYS + int(filing.get("attempt", 0))
                try:
                    record = self.missions.queue_sec_dispatch(
                        authorization=authorization, form="10-Q",
                        filed_from=(filed - timedelta(days=span)).isoformat(),
                        filed_to=(filed + timedelta(days=span)).isoformat(),
                        expected_accession=filing["accession"],
                        observation_ref=f"sec-company-facts-artifact:{digest}",
                    )
                except Exception as exc:  # noqa: BLE001
                    skipped.append({"ticker": entry.get("ticker"), "accession": filing["accession"],
                                    "reason": f"{type(exc).__name__}: {exc}"})
                    continue
                queued.append({
                    "accession": filing["accession"], "period": filing["period"],
                    "filed": filing["filed"], "attempt": int(filing.get("attempt", 0)) + 1,
                    "status": record.get("status", "queued"),
                })
            if queued:
                return {
                    "status": "queued", "ticker": entry.get("ticker"),
                    "company_ref": company_ref, "quarters_held": item["have"],
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


__all__ = [
    "MAX_ATTEMPTS_PER_FILING",
    "MAX_QUEUED_PER_RUN",
    "MissionSecQuartersCoordinator",
    "REQUIRED_QUARTERS",
    "quarterly_filings",
    "read_artifact",
]
