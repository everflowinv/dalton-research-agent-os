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

The claim index ranks evidence by importance -- filing, management statement,
sell-side, news, other -- and crowd evidence is `other`, the bottom tier. That
needs no line anywhere: the index reaches importance through the discovery
spec and then the connector's source type, and the crowd is deliberately
absent from both tables, so it falls through to the default. Adding an entry
is what would raise it.

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

from .lane_registry import LaneSpec, register_lane
from .lane_child_launcher import LaneChildRejected
from .lane_failure_ledger import lane_budget
from .lane_permission_control import (
    authority_connection,
    clear_obsolete_permissions,
    permission_key,
    record_controlled_failure,
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
# management's own words, below sell-side, below news.
#
# It is the index's own lowest tier rather than a new word, and that turns out
# to need no integration line at all. `claim_index_tagging` reaches importance
# two ways: the discovery spec that found the document, and failing that the
# connector's source type. The crowd spec refs are deliberately absent from
# `SPEC_IMPORTANCE`, and `social_search` / `social_enumeration` are absent from
# `SOURCE_TYPE_IMPORTANCE`, so a crowd claim falls through both and lands on
# the default -- which is this. Adding an entry anywhere would be what raised
# it; leaving them out is what keeps it at the bottom.
CROWD_IMPORTANCE = "other"

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
# How many ticks a company sits out after a failure on one source.
#
# The first version never cleared the failure set, so one pass over five
# companies on a Core whose grant had not been bound left every pair marked and
# the lane silently idle for the life of the process -- reporting "idle", which
# is the same word it uses when there is genuinely nothing to do. A cool-off is
# what makes "we tried and it did not work" different from "we have stopped".
#
# Twelve ticks is roughly an hour at the controller's cadence: long enough that
# a source which is down is not asked once a minute, short enough that a
# credential bound at lunchtime is picked up in the afternoon.
FAILURE_COOL_OFF_TICKS = 12
DRIVER_KEY = "crowd_source"


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


class CrowdSourceExecution:
    """One source's child, run by the connector runner and ticketed by the launcher.

    This is the join S1's runner made possible. Before it there was no runner
    for a ``host_tool`` connector at all, so this lane spawned its children
    itself and the seven declared quotas were governance inputs nothing
    counted. The runner registers the call spec, the invocation, the quota
    reservation, the physical attempt, the raw artifact and the SourceEnvelope
    around the same child -- so the same fifty-a-day that used to be a sentence
    in a policy table is now a reservation that fails closed.

    The split of responsibilities is S1's and it is the right one: the runner
    owns the process and the authority chain, the launcher owns tickets,
    because the ticket directory is the durable record of which run produced
    which bytes. There is still exactly one child.
    """

    def __init__(
        self,
        *,
        source: str,
        launcher: Any,
        runner_factory: Callable[..., Any],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.launcher = launcher
        self.SOURCE_REF = launcher.SOURCE_REF
        self.runner_factory = runner_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(self, *, operation: str, parameters: Mapping[str, Any],
                actor_ref: str, company_ref: str) -> dict[str, Any]:
        """Run one bounded read and answer in the shape a summary has.

        Refusals that were always going to happen -- an unapproved record, a
        parameter this connector will not take -- come back as a failed status
        rather than an exception, because the tick reports them and carries on
        to the next source.
        """

        try:
            governance = self.launcher.load_governance(operation)
        except LaneChildRejected as exc:
            return {"status": "failed", "failure_reason": f"{type(exc).__name__}: {exc}"}
        if not getattr(governance, "approved", False):
            return {"status": "failed",
                    "failure_reason": (
                        f"gated:governance the {operation} record is not approved"
                    )}
        try:
            cleaned = self.launcher._validate(operation, parameters)
        except LaneChildRejected as exc:
            return {"status": "failed", "failure_reason": f"{type(exc).__name__}: {exc}"}

        digest = self.launcher.run_digest(operation, cleaned, governance.content_hash)
        ticket_id, ticket_dir = self.launcher.prepare_run(
            digest=digest,
            record={
                "source_ref": self.SOURCE_REF, "operation": operation,
                "parameters": dict(cleaned), "actor_ref": actor_ref,
                "company_ref": company_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "host-tool",
            },
        )
        runner = self.runner_factory(
            source=self.source, operation=operation, launcher=self.launcher,
            governance=governance,
        )
        try:
            receipt = runner.run(
                parameters=cleaned,
                work_ref=f"work:{self.SOURCE_REF}:{digest}",
                output_dir=ticket_dir,
            )
        except Exception as exc:  # noqa: BLE001 - one read, reported not raised
            reason = f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]
            self.launcher.settle_run(ticket_id, status="failed", exit_code=1,
                                     failure_reason=reason)
            return {"status": "failed", "failure_reason": reason,
                    "ticket_ref": ticket_id}
        self.launcher.settle_run(ticket_id, status="succeeded", exit_code=0)
        return {
            "status": "succeeded",
            "ticket_ref": ticket_id,
            "observation": dict(receipt.observation),
            # The runner hashes the child's stdout into the spool; that hash is
            # what a record cites, and it is the same object the child's own
            # spool write produced, because the spool is content addressed.
            "artifact": {"content_hash": receipt.raw_response_hash},
            "governance_ref": governance.id,
            "governance_hash": governance.content_hash,
            "connector_invocation_ref": receipt.connector_invocation_ref,
            "source_envelope_ref": receipt.source_envelope_ref,
        }


class MissionCrowdSourceLaneCoordinator:
    """One bounded child per source per tick, over the mission's companies.

    **The runner seam.** ``runners`` maps a source name to whatever executes
    that source's child. This coordinator asks each entry for two things and
    nothing else: ``SOURCE_REF``, and ``execute(operation=..., parameters=...,
    actor_ref=..., company_ref=...)`` returning a summary-shaped mapping with a
    ``status`` and, when it succeeded, an ``observation`` and an ``artifact``.
    ``CrowdSourceExecution`` below is the real one -- a launcher for the ticket
    and S1's ``HostToolRunner`` for the process and the authority chain -- and
    the tests pass a fake through the same door.
    """

    GRANTS = LANE_GRANTS

    def __init__(
        self,
        *,
        mission: Callable[[], Mapping[str, Any] | None],
        runners: Mapping[str, Any],
        source_map: Mapping[str, Any],
        ledger: CrowdObservationLedger,
        actor_ref: str = "automation:coverage-mission",
        since: str | None = None,
        clock: Callable[[], datetime] | None = None,
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.mission = mission
        self.runners = dict(runners)
        self.companies = list(source_map["companies"])
        self.ledger = ledger
        self.actor_ref = actor_ref
        self.since = since
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.failure_budget = lane_budget(
            DRIVER_KEY, state_dir=failure_ledger_dir, clock=self.clock,
            max_transient_failures=FAILURE_COOL_OFF_TICKS,
        )
        self._cursor: dict[str, int] = {}
        # Exact job input -> (reason, ticks remaining before it is retried).
        self._failed: dict[str, tuple[str, int]] = {}
        # What the lane was configured with last tick. A governance record that
        # changed -- the owner approving one, most likely -- is a change to the
        # thing that failed, so the cool-off is over immediately rather than in
        # an hour.
        self._configuration: str | None = None
        self._mission_snapshot: Mapping[str, Any] = {}

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
        self._mission_snapshot = mission
        may_write = set((mission.get("autonomy") or {}).get("may_write") or ())
        missing = sorted(self.GRANTS - may_write)
        if missing:
            business = "permission|top|crowd_source"
            launcher = next(iter(self.runners.values()), self)
            connection = authority_connection(*self.runners.values())
            current = permission_key(business, mission, launcher, connection=connection)
            clear_obsolete_permissions(
                self.failure_budget, current, scope_prefix="permission|top|")
            blocked = self.failure_budget.blocked(current)
            permission = blocked or record_controlled_failure(
                self.failure_budget, business, mission, launcher,
                reason="mission does not grant " + ",".join(missing),
                status="gated", connection=connection,
            )
            return {"status": "gated",
                    "reason": f"mission does not grant {missing}",
                    "failure": permission.as_wire()}
        for row in self.failure_budget.permission_items():
            if row["item_key"].startswith("permission|top|"):
                self.failure_budget.clear(row["item_key"])
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
            parameters = {key: value for key, value in job.items()
                          if key != "operation" and value}
            key = self._job_key(source, company["company_ref"],
                                job["operation"], parameters)
            prefix = f"{source}|{company['company_ref']}|input:"
            self._retire_superseded(prefix, key)
            if key in self._failed:
                continue
            execution = self.runners[source]
            permission = permission_key(
                key, self._mission_snapshot, execution,
                connection=authority_connection(execution),
            )
            clear_obsolete_permissions(
                self.failure_budget, permission, scope_prefix=key + "|permission:")
            if (self.failure_budget.blocked(key) is not None
                    or self.failure_budget.blocked(permission) is not None):
                continue
            self._cursor[source] = index + 1
            return {"company_ref": company["company_ref"], **job}
        return None

    def _job_key(self, source: str, company_ref: str, operation: str,
                 parameters: Mapping[str, Any]) -> str:
        return f"{source}|{company_ref}|input:" + content_hash({
            "source": source, "company_ref": company_ref,
            "operation": operation, "parameters": dict(parameters),
        })[:24]

    def _retire_superseded(self, prefix: str, current: str) -> None:
        rows = (self.failure_budget.parked_items()
                + self.failure_budget.terminal_items()
                + self.failure_budget.permission_items())
        for row in rows:
            key = row["item_key"]
            if key.startswith(prefix) and not key.startswith(current):
                self.failure_budget.retire(key)
                self._failed.pop(key, None)
        for key in tuple(self._failed):
            if key.startswith(prefix) and key != current:
                self.failure_budget.retire(key)
                self._failed.pop(key, None)

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

    # -- running one job ---------------------------------------------------

    def _run(self, source: str, connected: set[str]) -> dict[str, Any]:
        """One company, one source, one bounded read, recorded on the spot.

        Synchronous, unlike the first version of this lane, and that is the
        change S1's host-tool runner brings. The runner registers the call, the
        invocation, the reservation, the physical attempt and the
        SourceEnvelope around the same child this lane used to spawn and settle
        a tick later -- and the declared quota becomes a quota that is actually
        counted, because the reservation is what counts it. A receipt that does
        not come back is a run that did not happen; there is no half-finished
        state left for the next tick to find.
        """

        execution = self.runners[source]
        if execution.SOURCE_REF not in connected:
            return {"source": source, "status": "held",
                    "reason": f"{execution.SOURCE_REF} is not connected in this "
                              "mission version"}
        job = self._next_job(source)
        if job is None:
            return {"source": source, "status": "idle"}
        company_ref = job.pop("company_ref")
        operation = job.pop("operation")
        parameters = {key: value for key, value in job.items() if value}
        item_key = self._job_key(source, company_ref, operation, parameters)
        try:
            outcome = execution.execute(
                operation=operation, parameters=parameters,
                actor_ref=self.actor_ref, company_ref=company_ref,
            )
        except Exception as exc:  # noqa: BLE001 - one read, reported not raised
            reason = f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]
            failure = self._hold(item_key, execution, reason, "failed")
            return {"source": source, "status": "failed",
                    "company_ref": company_ref, "operation": operation,
                    "reason": reason, "failure": failure}
        if outcome.get("status") != "succeeded":
            reason = str(outcome.get("failure_reason")
                         or "the read failed without a reason")[:MAX_FAILURE_DETAIL_CHARS]
            failure = self._hold(item_key, execution, reason,
                                 str(outcome.get("status") or "failed"))
            return {"source": source, "status": "failed",
                    "company_ref": company_ref, "operation": operation,
                    "reason": reason, "ticket_ref": outcome.get("ticket_ref"),
                    "failure": failure}
        try:
            entries = observation_entries(
                source_ref=execution.SOURCE_REF, company_ref=company_ref,
                operation=operation, summary=outcome,
                observed_at=self.clock().astimezone(timezone.utc)
                .isoformat(timespec="microseconds"),
            )
            recorded = self.ledger.record(entries)
        except (CrowdSourceLaneError, OSError, KeyError) as exc:
            reason = f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]
            failure = self._hold(item_key, execution, reason, "failed")
            return {"source": source, "status": "failed",
                    "company_ref": company_ref, "operation": operation,
                    "reason": reason, "failure": failure}
        resumed = self.failure_budget.clear(item_key)
        return {
            "source": source, "status": "recorded", "company_ref": company_ref,
            "operation": operation, "ticket_ref": outcome.get("ticket_ref"),
            "recorded": len(recorded["recorded"]),
            "duplicates": len(recorded["duplicates"]),
            "grade": CROWD_GRADE,
            "resumed": resumed,
        }

    # -- the tick ----------------------------------------------------------

    def _hold(self, key: str, execution: Any, reason: str,
              status: str) -> dict[str, Any]:
        """Sit this pair out for a while, rather than for ever."""

        decision = record_controlled_failure(
            self.failure_budget, key, self._mission_snapshot, execution,
            reason=reason, status=status,
            connection=authority_connection(execution),
        )
        if decision.action == "retry":
            self._failed[key] = (reason, FAILURE_COOL_OFF_TICKS)
        return decision.as_wire()

    def _age_holds(self) -> None:
        """One tick off transient holds; observe configuration changes.

        The configuration is the governance hashes the runners are carrying. If
        one moved, the owner approved something or a record was replaced.
        Persistent authorization holds use their own control fingerprints and
        are retired only when the matching business input is reconsidered.
        """

        configuration = self._configuration_digest()
        if self._configuration is None:
            self._configuration = configuration
            return
        if configuration != self._configuration:
            self._configuration = configuration
            self._failed.clear()
            return
        self._failed = {
            key: (reason, remaining - 1)
            for key, (reason, remaining) in self._failed.items()
            if remaining > 1
        }

    def _configuration_digest(self) -> str:
        parts: list[str] = []
        for source in sorted(self.runners):
            runner = self.runners[source]
            paths = getattr(runner, "governance_paths", {}) or {}
            for operation in sorted(paths):
                try:
                    governance = runner.load_governance(operation)
                except Exception:  # noqa: BLE001 - unreadable is its own state
                    parts.append(f"{source}:{operation}:unreadable")
                    continue
                parts.append(
                    f"{source}:{operation}:{getattr(governance, 'content_hash', '')}"
                )
        return content_hash(parts)

    def held(self) -> dict[str, str]:
        """Which pairs are sitting out, and why. For the tick summary."""

        rows = {key: reason for key, (reason, _ticks) in self._failed.items()}
        for item in (
            self.failure_budget.parked_items()
            + self.failure_budget.terminal_items()
            + self.failure_budget.permission_items()
        ):
            rows[item["item_key"]] = item["reason"]
        return rows

    def dispatch_once(self) -> dict[str, Any]:
        """One bounded read per source, recorded before the tick returns."""

        gated = self._gate()
        if gated is not None:
            return {**gated, "sources": [], "held": {}}
        self._age_holds()
        connected = self._connected_sources()
        sources = [self._run(source, connected) for source in sorted(self.runners)]
        recorded = [item for item in sources if item["status"] == "recorded"]
        return {
            "status": "recorded" if recorded else "idle",
            "sources": sources,
            # Named rather than counted, because "idle" and "everything is
            # held" look identical from outside and are not the same thing.
            "held": self.held(),
        }


# -- the lane ----------------------------------------------------------------

# The approved records this lane runs under, named by version rather than
# discovered, so a future v2 is a deliberate edit here and not something the
# writer picks up because a file appeared. One per operation, because a schema
# hash binds one operation.
GOVERNANCE_FILES = {
    "xueqiu": {
        "search_posts": "xueqiu-search-posts-v1.json",
        "get_post": "xueqiu-get-post-v1.json",
        "hot_rank": "xueqiu-hot-rank-v1.json",
    },
    "x": {
        "user_timeline": "x-xreach-user-timeline-v1.json",
        "search": "x-xreach-search-v1.json",
        "thread": "x-xreach-thread-v1.json",
    },
    "employee-reviews": {"blind_reviews": "employee-reviews-blind-v1.json"},
}
CROWD_SOURCE_MAP = "p9-us-it-services-crowd-sources-v1.json"
LAUNCHER_KWARG = "crowd_source_launcher"


# Which packaged template and which identity function each source answers to.
# The identity is what the host-tool runner binds its profile and its schema
# check to, and every one of these already existed -- the runner asks for
# exactly the shape the three `*_core` modules were already producing.
SOURCE_IDENTITY = {
    "xueqiu": ("xueqiu-posts", "xueqiu_core", "xueqiu_identity"),
    "x": ("x-xreach-crowd", "xreach_core", "xreach_identity"),
    "employee-reviews": ("employee-reviews", "employee_reviews_core",
                         "employee_reviews_identity"),
}


def source_identity(source: str, operation: str) -> dict[str, Any]:
    """The frozen identity of one operation of one crowd source."""

    import importlib

    try:
        _template, module_name, function_name = SOURCE_IDENTITY[source]
    except KeyError as exc:
        raise CrowdSourceLaneError(f"{source} is not a crowd source") from exc
    module = importlib.import_module(f".{module_name}", __package__)
    function = getattr(module, function_name)
    # Blind has one operation and its identity takes no argument, because
    # there is nothing to choose between.
    return function() if source == "employee-reviews" else function(operation)


def build_crowd_source_runner(
    *,
    source: str,
    operation: str,
    launcher: Any,
    governance: Any,
    store: Any,
    connectors: Any,
    observability: Any,
    spool: Any,
    actor_ref: str = "automation:coverage-mission",
    clock: Callable[[], datetime] | None = None,
) -> Any:
    """A host-tool runner bound to one crowd operation and its child command.

    The runner stays generic: everything source-specific -- which template,
    which identity, which argv -- is supplied here. ``connector_slug`` is the
    template key, which is deliberately the same string these connectors'
    quota entries are keyed by, so the fifty-a-day in
    ``connector_quota_policy`` becomes the reservation ceiling without another
    table to keep in step.
    """

    from .host_tool_runner import HostToolRunner

    template_key = SOURCE_IDENTITY[source][0]

    def command(parameters: Mapping[str, Any], output_dir: Path,
                context: Mapping[str, str]) -> list[str]:
        return launcher.child_command(
            operation=operation, output_dir=output_dir, context=context,
            **dict(parameters)
        )

    identity = source_identity(source, operation)
    return HostToolRunner(
        store=store, connectors=connectors, observability=observability, spool=spool,
        template_key=template_key,
        identity=identity,
        governance=governance,
        command=command,
        connector_slug=template_key,
        credential_slot_refs=identity.get("credential_slot_refs", ()),
        actor_ref=actor_ref,
        clock=clock,
    )


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (S3).

    One bounded read per source per tick, run through the connector runner and
    recorded before the tick returns. The coordinator is cached across ticks
    because what it holds is how far round the company list each source has
    got and which pairs are cooling off -- process state, deliberately not a
    table, because losing it costs one duplicate read and nothing else.
    """

    launchers = server.lane_launcher(LAUNCHER_KWARG)
    if not launchers:
        return {"status": "unconfigured",
                "reason": "no crowd-source lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        def runner_factory(*, source: str, operation: str, launcher: Any,
                           governance: Any) -> Any:
            return build_crowd_source_runner(
                source=source, operation=operation, launcher=launcher,
                governance=governance, store=server.store,
                connectors=server.connectors,
                observability=server.observability, spool=server.spool,
            )

        coordinator = MissionCrowdSourceLaneCoordinator(
            mission=mission,
            runners={
                source: CrowdSourceExecution(
                    source=source, launcher=launcher,
                    runner_factory=runner_factory,
                )
                for source, launcher in launchers.by_source.items()
            },
            source_map=load_crowd_source_map(launchers.source_map_path),
            ledger=CrowdObservationLedger(
                Path(launchers.state_dir) / LEDGER_FILENAME),
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # Off unless the map is named. Without it there is nothing to ask any of
    # the three sources, because none of the three can be asked about a company
    # by ticker.
    parser.add_argument("--crowd-source-map",
                        help="per-mission handles, queries and employer slugs")
    parser.add_argument("--crowd-source-governance-dir",
                        help="directory holding the seven approved records")
    parser.add_argument("--crowd-source-xueqiu-tool", default=None)
    parser.add_argument("--crowd-source-xueqiu-fallback-tool", default=None)
    parser.add_argument("--crowd-source-xreach-tool", default=None)
    parser.add_argument("--crowd-source-credential-grant", default=None,
                        help="host grant envelope naming the cookie slots")
    parser.add_argument("--crowd-source-fixture", default=None,
                        help="rehearsal only: replay a captured run")


def build_launcher(args: Any) -> Any | None:
    """Build whichever of the three this Core is configured for.

    A source with no approved record is simply absent rather than present and
    broken: an absent source reports nothing, and a present one that refuses
    every tick fills the tick summary with the same sentence forever.
    """

    if args.crowd_source_map is None:
        return None
    from .crowd_source_launcher import (
        CrowdSourceLaunchers,
        EmployeeReviewsLauncher,
        XreachLauncher,
        XueqiuLauncher,
    )

    state_dir = Path(args.db).expanduser().resolve().parent
    governance_dir = Path(
        args.crowd_source_governance_dir or (state_dir / "connector-governance")
    ).expanduser().resolve()
    mode_args = (
        ("--fixture-file", args.crowd_source_fixture)
        if args.crowd_source_fixture is not None else ("--allow-network",)
    )

    def paths(source: str) -> dict[str, Path]:
        found = {}
        for operation, name in GOVERNANCE_FILES[source].items():
            candidate = governance_dir / name
            if candidate.is_file():
                found[operation] = candidate
        return found

    built: dict[str, Any] = {}
    xueqiu_paths = paths("xueqiu")
    if xueqiu_paths:
        built["xueqiu"] = XueqiuLauncher(
            state_dir=state_dir, governance_paths=xueqiu_paths,
            credential_grant_path=args.crowd_source_credential_grant,
            tool=args.crowd_source_xueqiu_tool,
            fallback_tool=args.crowd_source_xueqiu_fallback_tool,
            mode_args=mode_args,
        )
    x_paths = paths("x")
    if x_paths:
        built["x"] = XreachLauncher(
            state_dir=state_dir, governance_paths=x_paths,
            credential_grant_path=args.crowd_source_credential_grant,
            tool=args.crowd_source_xreach_tool, mode_args=mode_args,
        )
    review_paths = paths("employee-reviews")
    if review_paths:
        built["employee-reviews"] = EmployeeReviewsLauncher(
            state_dir=state_dir, governance_paths=review_paths,
            mode_args=mode_args,
        )
    if not built:
        return None
    launchers = CrowdSourceLaunchers(**built)
    launchers.state_dir = state_dir
    launchers.source_map_path = Path(args.crowd_source_map).expanduser().resolve()
    return launchers


def approved_records(governance_dir: Path) -> list[str]:
    """The records on disk that actually say ``approved``.

    Presence is not approval. All seven of these ship ``proposed``, and the
    installer copies them whether or not the owner has looked at them, so a
    check for the file existing turns the lane on for records nobody has
    agreed to -- every child then refuses, once a tick, forever. Read the
    status.
    """

    found: list[str] = []
    for names in GOVERNANCE_FILES.values():
        for name in names.values():
            path = governance_dir / name
            try:
                wire = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(wire, Mapping) and wire.get("status") == "approved":
                found.append(name)
    return found


def argv_fragment(context: Any) -> list[str]:
    # Enabled by the map and at least one *approved* record being on disk, and
    # off on a Core with neither -- which is every Core until the owner
    # approves, because all seven records ship proposed.
    source_map = context.state / "phase9" / CROWD_SOURCE_MAP
    governance = context.state / "connector-governance"
    if not source_map.is_file() or not approved_records(governance):
        return []
    argv = ["--crowd-source-map", str(source_map),
            "--crowd-source-governance-dir", str(governance)]
    # INT2: install.sh links the three host tools under state/host-tools; a
    # tool that is not there is a flag that is not passed, and the child
    # refuses that operation by name rather than guessing a path.
    tools = context.state / "host-tools"
    for flag, name in (("--crowd-source-xueqiu-tool", "agent-reach"),
                       ("--crowd-source-xueqiu-fallback-tool", "xueqiu-hot-rank"),
                       ("--crowd-source-xreach-tool", "xreach")):
        tool = tools / name
        if tool.exists():
            argv.extend([flag, str(tool)])
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_crowd_sources",
    # Last in the tick. The crowd is the least of the evidence, and a tick that
    # runs out of time should run out of it here rather than before a filing.
    # 120 belongs to the human-feeds lane; the gap is deliberate room between
    # lanes that land in the same week.
    order=140,
    driver_key="mission_crowd_sources",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="S3: what the crowd is saying about a covered company -- Xueqiu, X "
         "and anonymous employee reviews. Observation-class evidence at the "
         "lowest importance; never the sole source of a number.",
))


__all__ = [
    "CROWD_BASIS",
    "CROWD_SOURCE_MAP",
    "GOVERNANCE_FILES",
    "LANE",
    "LAUNCHER_KWARG",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "CROWD_GRADE",
    "CROWD_IMPORTANCE",
    "CROWD_QUALIFIER",
    "CROWD_SPEC_REFS",
    "LANE_GRANTS",
    "LEDGER_FILENAME",
    "MAX_RECORDS_PER_RUN",
    "SPEC_REF_BY_SOURCE",
    "CrowdObservationLedger",
    "CrowdSourceExecution",
    "SOURCE_IDENTITY",
    "approved_records",
    "build_crowd_source_runner",
    "source_identity",
    "CrowdSourceLaneError",
    "FAILURE_COOL_OFF_TICKS",
    "MissionCrowdSourceLaneCoordinator",
    "admissible_as_sole_quantitative_source",
    "crowd_qualify",
    "load_crowd_source_map",
    "observation_entries",
]
