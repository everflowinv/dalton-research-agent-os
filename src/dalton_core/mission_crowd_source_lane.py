"""S3: run the three crowd sources on a tick, and grade what comes back.

The lane is small on purpose. Once a tick, for each mission company, at most
one child per source: what is being said about this company on Xueqiu, on X,
and by the people who work there. What comes back is recorded as an
observation, deduplicated by the post's own id so that the same post read on
Tuesday and again on Wednesday is one record, and graded.

**The grade is the point of the module.** Everything these three sources
produce is anonymous or unverifiable, and the system already has a place for
"how did this come to be known": `document_figure_grade`. What it does *not*
have is a word for the crowd, and the reason it does not is that no figure may
be taken from a crowd source at all -- so the right change is not to add a
grade there. `figure_worthy()` already answers False for a document kind it
does not know, which is exactly the refusal wanted, and the tests below pin it
so that adding one later has to be a decision.

What the crowd layer does need is a word for the *claim index*, which ranks
evidence by importance -- first-hand filing, then management's own words, then
sell-side, then news. Crowd evidence sorts below all of those. That word is
`CROWD_IMPORTANCE` here, and the one-line change the integrator makes is to
map this module's `CROWD_GRADE` to it in the claim index's importance table.
Until that line exists, nothing here can be sorted *above* anything, because
nothing here is graded at all by the index.

**The hard rule, stated as code.** No quantitative Claim may rest on a crowd
source alone. `admissible_as_sole_quantitative_source` answers False for the
crowd grade and there is no argument that flips it. A crowd observation can say
that people are unhappy about layoffs; it cannot say how many.

**Not wired.** Wave 0 builds the lane registry, and registration is a line
there rather than an edit to ten files here. The line this lane needs is in the
report.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"

# The grade every record from these three sources carries.
CROWD_GRADE = "crowd-anonymous-post"
# The Ledger `basis` text, in the same vocabulary the other grades use.
CROWD_BASIS = "crowd-anonymous-post"
# Appended to any statement built from one of these records.
CROWD_QUALIFIER = (
    "as written by an anonymous or unverified poster and recorded here as "
    "sentiment; not a figure and not a statement of fact about the company"
)
# Where this sorts in the claim index: below first-hand filings, below
# management's own words, below sell-side, below news. The word itself is
# owned here so that the index's table can point at it in one line.
CROWD_IMPORTANCE = "background"

# The document kinds these sources produce. They are deliberately absent from
# `document_figure_grade.GRADE_BY_SPEC`: a kind with no grade there is a kind
# no figure may be read from, which is the refusal wanted, expressed by
# omission rather than by a new special case.
CROWD_SPEC_REFS = ("xueqiu-post", "x-post", "blind-employee-review")

SPEC_REF_BY_SOURCE = {
    "source:xueqiu": "xueqiu-post",
    "source:x": "x-post",
    "source:blind": "blind-employee-review",
}

# The two write scopes this lane needs. `observation` because that is what a
# post is; `source_discovery` because reading a company's crowd chatter is also
# a statement about which sources hold anything about it.
LANE_GRANTS = frozenset({"observation", "source_discovery"})

MAX_RECORDS_PER_RUN = 400
MAX_FAILURE_DETAIL_CHARS = 500
LEDGER_FILENAME = "crowd-observations.jsonl"


class CrowdSourceLaneError(RuntimeError):
    """The crowd-source lane cannot run as configured."""


def admissible_as_sole_quantitative_source(grade: Any) -> bool:
    """Whether a Claim with a number in it may cite this grade and nothing else.

    False for the crowd grade, always. This is a one-line function because it
    is a one-line rule, and having it be a function is what lets a caller be
    tested against it rather than remembering it.
    """

    return grade != CROWD_GRADE


def crowd_qualify(statement: str) -> str:
    """Add what the crowd grade means to a statement built from one of these."""

    if not isinstance(statement, str) or not statement.strip():
        raise CrowdSourceLaneError("a crowd observation needs a statement")
    return f"{statement.rstrip().rstrip('.')}, {CROWD_QUALIFIER}."


# -- the per-mission mapping -------------------------------------------------


def load_crowd_source_map(path: str | Path) -> dict[str, Any]:
    """Which handle, query and employer slug belong to which covered company.

    This is a mapping file rather than a lookup because there is no algorithm
    for it: Accenture's employer page on a review site, its Chinese name on a
    retail forum and its corporate account on X are three facts a person knows
    and nothing derives. Wrong entries here are the failure mode that fills a
    company's file with another company's chatter, so every field is optional
    and an absent one means "this source has nothing for this company" rather
    than "guess".
    """

    location = Path(path).expanduser().resolve()
    try:
        wire = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CrowdSourceLaneError(f"crowd source map is unreadable: {exc}") from exc
    if not isinstance(wire, Mapping) or wire.get("schema_version") != SCHEMA_VERSION:
        raise CrowdSourceLaneError("crowd source map has an unsupported schema_version")
    companies = wire.get("companies")
    if not isinstance(companies, list) or not companies:
        raise CrowdSourceLaneError("crowd source map lists no companies")
    cleaned: list[dict[str, Any]] = []
    for entry in companies:
        if not isinstance(entry, Mapping):
            raise CrowdSourceLaneError("crowd source map entries must be objects")
        company_ref = entry.get("company_ref")
        ticker = entry.get("ticker")
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise CrowdSourceLaneError("a crowd source map entry has no company_ref")
        if not isinstance(ticker, str) or not ticker.strip():
            raise CrowdSourceLaneError(f"{company_ref} has no ticker")
        handles = entry.get("x_handles") or []
        if not isinstance(handles, list) or any(
            not isinstance(item, str) or not item.strip() for item in handles
        ):
            raise CrowdSourceLaneError(f"{company_ref} has a malformed x_handles list")
        cleaned.append({
            "company_ref": company_ref.strip(),
            "ticker": ticker.strip().upper(),
            "xueqiu_query": (entry.get("xueqiu_query") or "").strip() or None,
            "x_handles": [item.strip().lstrip("@") for item in handles],
            "employer_slug": (entry.get("employer_slug") or "").strip() or None,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "id": wire.get("id"),
        "industry_ref": wire.get("industry_ref"),
        "companies": cleaned,
    }


# -- the ledger --------------------------------------------------------------


class CrowdObservationLedger:
    """Append-only, owner-only, deduplicated by the record's own id.

    Not an authority and not pretending to be one: it holds no version chain
    and adjudicates nothing. It is the lane's own memory of which posts it has
    already recorded, which is what keeps a tick from re-recording a timeline
    it read an hour ago. When the integrator points this lane at the real
    evidence authority, this becomes the projection that authority already
    provides and goes away.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._seen: set[tuple[str, str]] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:  # a torn last write; the next append fixes it
                    continue
                self._seen.add((row.get("source_ref", ""), row.get("record_id", "")))

    def known(self, source_ref: str, record_id: str) -> bool:
        return (source_ref, record_id) in self._seen

    def record(self, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Append the entries this ledger has not seen; report both counts."""

        fresh: list[dict[str, Any]] = []
        duplicates: list[str] = []
        for entry in entries:
            key = (entry["source_ref"], entry["record_id"])
            if key in self._seen:
                duplicates.append(entry["record_id"])
                continue
            row = dict(entry)
            row["content_hash"] = content_hash(row)
            self._seen.add(key)
            fresh.append(row)
        if fresh:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.touch()
            os.chmod(self.path, 0o600)
            with self.path.open("a", encoding="utf-8") as handle:
                for row in fresh:
                    handle.write(canonical_json(row) + "\n")
        return {"recorded": [row["record_id"] for row in fresh],
                "duplicates": duplicates}


def observation_entries(
    *,
    source_ref: str,
    company_ref: str,
    operation: str,
    summary: Mapping[str, Any],
    observed_at: str,
) -> list[dict[str, Any]]:
    """One ledger entry per record in a child's observation.

    The entry carries what makes it checkable -- which post, when, which raw
    artifact it came out of, which approval the run was made under -- and the
    grade. It does not carry the post's text: the text is in the artifact, and
    copying it here would make two places to correct.
    """

    observation = summary.get("observation")
    if not isinstance(observation, Mapping):
        raise CrowdSourceLaneError("the run succeeded without an observation")
    records = observation.get("posts")
    if records is None:
        records = observation.get("reviews")
    if records is None:
        records = observation.get("ranking")
    if not isinstance(records, list):
        raise CrowdSourceLaneError("the observation carries no records")
    artifact = summary.get("artifact") or {}
    entries: list[dict[str, Any]] = []
    for record in records[:MAX_RECORDS_PER_RUN]:
        if not isinstance(record, Mapping):
            continue
        record_id = record.get("post_id") or record.get("review_id") or record.get("symbol")
        if not isinstance(record_id, str) or not record_id:
            continue
        entries.append({
            "schema_version": SCHEMA_VERSION,
            "source_ref": source_ref,
            "spec_ref": SPEC_REF_BY_SOURCE.get(source_ref, "crowd-record"),
            "company_ref": company_ref,
            "operation": operation,
            "record_id": record_id,
            "record_created_at": record.get("created_at"),
            # A locked body is a real row with substituted prose. Carrying the
            # flag means a later reader can build a rating series from every
            # row and a word count from only the readable ones.
            "body_locked": bool(record.get("body_locked", False)),
            "grade": CROWD_GRADE,
            "basis": CROWD_BASIS,
            "importance": CROWD_IMPORTANCE,
            "artifact_hash": artifact.get("content_hash"),
            "governance_ref": summary.get("governance_ref"),
            "governance_hash": summary.get("governance_hash"),
            "observed_at": observed_at,
        })
    return entries


# -- the coordinator ---------------------------------------------------------


def _failure_reason(summary: Any) -> str | None:
    if not isinstance(summary, Mapping):
        return None
    reason = summary.get("failure_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip()[:MAX_FAILURE_DETAIL_CHARS]


class MissionCrowdSourceLaneCoordinator:
    """One bounded child per source per tick, over the mission's companies."""

    GRANTS = LANE_GRANTS

    def __init__(
        self,
        *,
        mission: Callable[[], Mapping[str, Any] | None],
        launchers: Mapping[str, Any],
        source_map: Mapping[str, Any],
        ledger: CrowdObservationLedger,
        actor_ref: str = "automation:coverage-mission",
        since: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.mission = mission
        self.launchers = dict(launchers)
        self.companies = list(source_map["companies"])
        self.ledger = ledger
        self.actor_ref = actor_ref
        self.since = since
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._open: dict[str, tuple[str, str, str]] = {}
        self._cursor: dict[str, int] = {}
        self._failed: dict[str, str] = {}

    # -- gating ------------------------------------------------------------

    def _gate(self) -> dict[str, Any] | None:
        """The mission's own grant, checked before anything is spawned."""

        try:
            mission = self.mission()
        except Exception as exc:  # noqa: BLE001 - never break the tick
            return {"status": "unavailable",
                    "reason": f"{type(exc).__name__}: {exc}"}
        if not mission:
            return {"status": "unconfigured",
                    "reason": "no active mission version on this writer"}
        may_write = set((mission.get("autonomy") or {}).get("may_write") or ())
        missing = sorted(self.GRANTS - may_write)
        if missing:
            return {"status": "gated",
                    "reason": f"mission does not grant {missing}"}
        return None

    def _connected_sources(self) -> set[str]:
        try:
            mission = self.mission() or {}
        except Exception:  # noqa: BLE001
            return set()
        return {
            item.get("source_ref")
            for item in (mission.get("source_plan") or [])
            if isinstance(item, Mapping) and item.get("status") == "connected"
        }

    # -- the job for one source -------------------------------------------

    def _next_job(self, source: str) -> dict[str, Any] | None:
        """The next company this source has anything to say about."""

        if not self.companies:
            return None
        start = self._cursor.get(source, 0)
        for offset in range(len(self.companies)):
            index = (start + offset) % len(self.companies)
            company = self.companies[index]
            job = self._job_for(source, company)
            if job is None:
                continue
            key = f"{source}|{company['company_ref']}"
            if key in self._failed:
                continue
            self._cursor[source] = index + 1
            return {"company_ref": company["company_ref"], **job}
        return None

    def _job_for(self, source: str, company: Mapping[str, Any]) -> dict[str, Any] | None:
        if source == "xueqiu":
            query = company.get("xueqiu_query")
            return None if not query else {
                "operation": "search_posts", "query": query, "since": self.since}
        if source == "x":
            handles = company.get("x_handles") or []
            return None if not handles else {
                "operation": "user_timeline", "handle": handles[0],
                "since": self.since}
        if source == "employee-reviews":
            slug = company.get("employer_slug")
            return None if not slug else {
                "operation": "blind_reviews", "employer_slug": slug,
                "since": self.since}
        return None

    # -- settling ----------------------------------------------------------

    def _settle(self, source: str) -> dict[str, Any] | None:
        open_run = self._open.get(source)
        if open_run is None:
            return None
        ticket_ref, company_ref, operation = open_run
        launcher = self.launchers[source]
        try:
            ticket = launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            self._open.pop(source, None)
            return {"source": source, "company_ref": company_ref,
                    "outcome": "failed",
                    "failure_reason": "lane ticket is no longer on disk"}
        except Exception:  # noqa: BLE001 - unreadable now; try next tick
            return None
        if ticket.get("status") == "running":
            return None
        self._open.pop(source, None)
        summary = ticket.get("summary")
        if ticket.get("status") != "succeeded":
            reason = _failure_reason(summary) or f"lane run {ticket.get('status')}"
            self._failed[f"{source}|{company_ref}"] = reason
            return {"source": source, "company_ref": company_ref,
                    "outcome": "failed", "failure_reason": reason}
        try:
            entries = observation_entries(
                source_ref=launcher.SOURCE_REF, company_ref=company_ref,
                operation=operation, summary=summary,
                observed_at=self.clock().astimezone(timezone.utc)
                .isoformat(timespec="microseconds"),
            )
            recorded = self.ledger.record(entries)
        except (CrowdSourceLaneError, OSError, KeyError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._failed[f"{source}|{company_ref}"] = reason
            return {"source": source, "company_ref": company_ref,
                    "outcome": "failed", "failure_reason": reason}
        return {
            "source": source, "company_ref": company_ref, "outcome": "succeeded",
            "recorded": len(recorded["recorded"]),
            "duplicates": len(recorded["duplicates"]),
            "grade": CROWD_GRADE,
        }

    # -- launching ---------------------------------------------------------

    def _launch(self, source: str, connected: set[str]) -> dict[str, Any]:
        launcher = self.launchers[source]
        if source in self._open:
            return {"source": source, "status": "busy"}
        if launcher.SOURCE_REF not in connected:
            return {"source": source, "status": "held",
                    "reason": f"{launcher.SOURCE_REF} is not connected in this "
                              "mission version"}
        job = self._next_job(source)
        if job is None:
            return {"source": source, "status": "idle"}
        company_ref = job.pop("company_ref")
        operation = job.pop("operation")
        params = {key: value for key, value in job.items() if value}
        try:
            ticket = launcher.start(operation=operation, actor_ref=self.actor_ref,
                                    **params)
        except LaneChildConflict as exc:
            return {"source": source, "status": "deferred",
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._failed[f"{source}|{company_ref}"] = reason
            return {"source": source, "status": "rejected",
                    "company_ref": company_ref, "reason": reason}
        self._open[source] = (ticket["id"], company_ref, operation)
        return {"source": source, "status": "launched", "company_ref": company_ref,
                "operation": operation, "ticket_ref": ticket["id"]}

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        """Settle what finished, then start at most one child per source."""

        gated = self._gate()
        if gated is not None:
            return {**gated, "settled": [], "sources": []}
        connected = self._connected_sources()
        settled = [item for item in
                   (self._settle(source) for source in sorted(self.launchers))
                   if item is not None]
        sources = [self._launch(source, connected)
                   for source in sorted(self.launchers)]
        launched = [item for item in sources if item["status"] == "launched"]
        return {
            "status": "launched" if launched else "idle",
            "settled": settled,
            "sources": sources,
        }


__all__ = [
    "CROWD_BASIS",
    "CROWD_GRADE",
    "CROWD_IMPORTANCE",
    "CROWD_QUALIFIER",
    "CROWD_SPEC_REFS",
    "LANE_GRANTS",
    "LEDGER_FILENAME",
    "MAX_RECORDS_PER_RUN",
    "SPEC_REF_BY_SOURCE",
    "CrowdObservationLedger",
    "CrowdSourceLaneError",
    "MissionCrowdSourceLaneCoordinator",
    "admissible_as_sole_quantitative_source",
    "crowd_qualify",
    "load_crowd_source_map",
    "observation_entries",
]
