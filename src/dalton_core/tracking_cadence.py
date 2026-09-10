"""P14a: how often we look at each source for each company, and who decides.

The owner's instruction (2026-09-09) has two halves that pull in opposite
directions and both have to hold.

**Tracking is resident.**  Once a company's Initial Screen has passed, it is
tracked every day, for as long as it is covered, whatever else the system is
doing -- deep coverage of that company, the next company's screen, an ad-hoc
research task.  Nothing the brain decides may switch tracking off, and no
later stage exits it.  That is why membership is computed from one fact --
*this company's Initial Screen passed, under any version of this mission* --
and not from a status field somebody could set: a resident property
implemented as a mutable flag is a property that gets turned off by accident
at 03:00.

Resident tracking is deliberately **not** the Playbook's ``active_coverage``
stage (owner decision, 2026-09-09).  That stage is the sixth step of a
research process a company reaches after its Investment Memo; this is a
standing state a company enters the day its screen passes.  They are two
different things and this module writes no stage record at all.

**The rate is a judgement.**  How often to pull AlphaEngine for a company with
four documents is not a constant; it depends on how much is there.  So the
policy file carries a baseline for every source, the brain may publish a new
``TrackingCadenceVersion`` for a (company, source) pair with a reason and
evidence refs, and the baseline is what applies until it does.  A cadence
version is a *version*: append-only, never an in-place edit, so "we slowed IBM
down to weekly on the 12th because three searches in a row returned nothing
new" is readable off the chain a month later.

Two things the brain may not do, and both are enforced here rather than
trusted: it may not change a cadence the policy marks fixed (prices and the
filing calendar), and it may not stop a baseline pull.  It sets the interval;
it does not decide whether we look.

``next_due`` is the one-line question every existing source-discovery lane
asks before it spends a quota unit.  It is deliberately a pure function of
(cadence, last pull, now, open events): a lane that has to open a database to
find out whether it may run is a lane that will be run without the check.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .research_event import rfc3339
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("tracking_cadence_schema.sql")
POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "deploy/phase9/p14a-tracking-policy-v1.json"
)

WRITE_SCOPE = "observation"
FIRST_STAGE = "initial_screen"

# Why a cadence version exists.  The same closed vocabulary ADR-0008 froze for
# every other output-class authority; ``driver_event`` is what an abnormal move
# or a run of empty searches is.
CHANGE_REASONS: tuple[str, ...] = (
    "filing_actual", "driver_event", "assumption_review", "evidence_thicker",
    "human_revision",
    # W3: an imported prior document, legal only on a deliverable v0. Never
    # produced by this authority; here because the vocabulary is one word list
    # and a test pins the copies to the canonical tuple.
    "imported_prior",
)

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 30 * 24 * 3600
MAX_BECAUSE_CHARS = 1200
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class TrackingCadenceError(RuntimeError):
    """Base error for the cadence authority."""


class TrackingCadenceValidationError(TrackingCadenceError, ValueError):
    """A request does not satisfy the closed contract."""


class TrackingCadenceConflict(TrackingCadenceError):
    """A request conflicts with the immutable chain."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrackingCadenceValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise TrackingCadenceValidationError(f"{name} must be at most {maximum} characters")
    return value


def _interval(value: Any, name: str = "interval_seconds") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrackingCadenceValidationError(f"{name} must be an integer number of seconds")
    if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise TrackingCadenceValidationError(
            f"{name} must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds"
        )
    return value


# ---------------------------------------------------------------------------
# the policy file
# ---------------------------------------------------------------------------


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """The baselines and thresholds, validated on the way in.

    Read rather than imported so the owner can change a threshold by
    publishing a new file; validated here so a typo in it is a refusal at
    load rather than a silently absent cadence at 04:00.
    """

    location = Path(path or POLICY_PATH)
    try:
        wire = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrackingCadenceValidationError(
            f"tracking policy at {location} is unreadable: {exc}"
        ) from exc
    if not isinstance(wire, Mapping) or wire.get("schema_version") != SCHEMA_VERSION:
        raise TrackingCadenceValidationError("tracking policy has an unexpected schema version")
    cadences: dict[str, dict[str, Any]] = {}
    for entry in wire.get("cadences") or ():
        key = _text(entry.get("source_key"), "cadences[].source_key", maximum=64)
        if key in cadences:
            raise TrackingCadenceValidationError(f"tracking policy names {key} twice")
        cadences[key] = {
            "source_key": key,
            "interval_seconds": _interval(entry.get("interval_seconds")),
            "adjustable": bool(entry.get("adjustable")),
            "because": _text(entry.get("because"), "cadences[].because", maximum=MAX_BECAUSE_CHARS),
        }
    if not cadences:
        raise TrackingCadenceValidationError("a tracking policy with no cadences tracks nothing")
    moves = wire.get("abnormal_move")
    if not isinstance(moves, Mapping):
        raise TrackingCadenceValidationError("tracking policy needs an abnormal_move block")
    pull = wire.get("immediate_pull")
    if not isinstance(pull, Mapping):
        raise TrackingCadenceValidationError("tracking policy needs an immediate_pull block")
    unknown = set(pull.get("source_keys") or ()) - set(cadences)
    if unknown:
        raise TrackingCadenceValidationError(
            f"immediate_pull names sources with no baseline cadence: {sorted(unknown)}"
        )
    stances = wire.get("thesis_stances") or {}
    if not isinstance(stances, Mapping):
        raise TrackingCadenceValidationError("thesis_stances must be an object")
    overrides = stances.get("overrides") or {}
    if not isinstance(overrides, Mapping):
        raise TrackingCadenceValidationError("thesis_stances.overrides must be an object")
    policy = {
        "policy_ref": _text(wire.get("policy_ref"), "policy_ref"),
        "cadences": cadences,
        "abnormal_move": dict(moves),
        "thesis_stances": {
            "default": str(stances.get("default") or "long"),
            "overrides": {str(key): str(value) for key, value in overrides.items()},
        },
        "immediate_pull": {
            "trigger_kinds": tuple(pull.get("trigger_kinds") or ()),
            "source_keys": tuple(pull.get("source_keys") or ()),
            "within_seconds": int(pull.get("within_seconds") or 0),
        },
    }
    policy["content_hash"] = content_hash({
        "policy_ref": policy["policy_ref"],
        "cadences": {key: dict(value) for key, value in cadences.items()},
        "abnormal_move": policy["abnormal_move"],
        "thesis_stances": policy["thesis_stances"],
        "immediate_pull": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in policy["immediate_pull"].items()
        },
    })
    return policy


def baseline_cadences(policy: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in policy["cadences"].items()}


# ---------------------------------------------------------------------------
# who is tracked
# ---------------------------------------------------------------------------


def screen_passed_companies(
    missions: Any, mission: Mapping[str, Any]
) -> list[str]:
    """Every company whose Initial Screen has passed, under *any* version.

    Across every version of the same mission, not the current one, and that is
    the whole of what makes residency real.  A stage record binds the mission
    version it was written under; publishing a new version does not copy the
    old records forward, so the four companies that passed under v13 would
    read as unpassed the moment v14 exists -- and every one of them would fall
    out of tracking on the day the owner granted the scope that lets tracking
    write anything.  The document checklist found the same shape and counts
    the same way.

    Deliberately monotone otherwise: no later stage, no research task and no
    judgement decision removes a company.  A company leaves by leaving the
    mission universe, which is a human act.
    """

    mission_ref = mission["mission_ref"]
    try:
        rows = missions.connection.execute(
            "SELECT DISTINCT company_ref FROM coverage_mission_stage_records "
            "WHERE stage_ref=? AND status='gate_passed' AND mission_version_ref IN "
            "(SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?)",
            (FIRST_STAGE, mission_ref),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    passed = {row["company_ref"] for row in rows}
    universe = [member["company_ref"] for member in mission["universe"]]
    return [ref for ref in universe if ref in passed]


def active_coverage_metrics(events: Any, judgements: Any, company_ref: str) -> dict[str, int]:
    """The three figures ``STAGE_SPINE['active_coverage']`` declares.

    Counted off the two ledgers rather than extracted from a document, which
    is why they are counts and why this function exists next to the stage
    rather than inside the extraction path.
    """

    seen = sum(events.counts(company_ref).values())
    judged = judgements.judged_count(company_ref)
    return {
        "metric:tracked-events-seen": seen,
        "metric:tracked-events-judged": judged,
        "metric:tracked-events-open": max(0, seen - judged),
    }


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


def cadence_ref_for(company_ref: str, source_key: str) -> str:
    return f"tracking-cadence:{content_hash({'company': company_ref, 'source': source_key})[:32]}"


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise TrackingCadenceConflict(f"{name} is missing")
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TrackingCadenceConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise TrackingCadenceConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise TrackingCadenceConflict(f"{name} content hash drifted")
    for column, key in (
        ("version_id", "id"), ("cadence_ref", "cadence_ref"),
        ("version_number", "version"), ("company_ref", "company_ref"),
        ("source_key", "source_key"), ("interval_seconds", "interval_seconds"),
        ("change_reason", "change_reason"), ("actor_ref", "actor_ref"),
        ("created_at", "created_at"),
    ):
        if wire.get(key) != row[column]:
            raise TrackingCadenceConflict(f"{name} identity column {column} drifted")
    return wire


class TrackingCadenceAuthority:
    """company x source -> how often, why, and on what evidence."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("TrackingCadenceAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def latest(self, company_ref: str, source_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tracking_cadence_versions WHERE cadence_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (cadence_ref_for(company_ref, source_key),),
        ).fetchone()
        return None if row is None else _decode(row, "TrackingCadenceVersion")

    def versions(self, company_ref: str, source_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM tracking_cadence_versions WHERE cadence_ref=? "
            "ORDER BY version_number ASC",
            (cadence_ref_for(company_ref, source_key),),
        ).fetchall()
        return [_decode(row, "TrackingCadenceVersion") for row in rows]

    def cadence(
        self, company_ref: str, source_key: str, *, policy: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The interval in force: the newest version, or the policy baseline."""

        baseline = policy["cadences"].get(source_key)
        if baseline is None:
            raise TrackingCadenceValidationError(
                f"{source_key} has no baseline cadence in {policy['policy_ref']}"
            )
        latest = self.latest(company_ref, source_key)
        if latest is None:
            return {
                "company_ref": company_ref, "source_key": source_key,
                "interval_seconds": baseline["interval_seconds"],
                "because": baseline["because"], "origin": "baseline",
                "adjustable": baseline["adjustable"], "version_ref": None,
                "version": 0,
            }
        return {
            "company_ref": company_ref, "source_key": source_key,
            "interval_seconds": latest["interval_seconds"],
            "because": latest["because"], "origin": "version",
            "adjustable": baseline["adjustable"], "version_ref": latest["id"],
            "version": latest["version"],
        }

    def propose(
        self,
        *,
        company_ref: str,
        source_key: str,
        interval_seconds: int,
        because: str,
        evidence_refs: Sequence[str],
        change_reason: str,
        policy: Mapping[str, Any],
        mission: Mapping[str, Any],
        actor_ref: str,
        decision: str | None = None,
    ) -> dict[str, Any]:
        """Publish a new cadence version.  Never an edit; often a duplicate.

        Refused when: the policy marks the source fixed; the evidence refs are
        empty; or the proposal changes nothing the current version does not
        already say on the same evidence (ADR-0008's ``duplicate`` guard -- a
        version that cannot name what it learned is a rewrite).
        """

        company_ref = _text(company_ref, "company_ref")
        source_key = _text(source_key, "source_key", maximum=64)
        interval_seconds = _interval(interval_seconds)
        because = _text(because, "because", maximum=MAX_BECAUSE_CHARS)
        change_reason = _text(change_reason, "change_reason", maximum=64)
        if change_reason not in CHANGE_REASONS:
            raise TrackingCadenceValidationError(
                f"change_reason must be one of {list(CHANGE_REASONS)}"
            )
        refs = [_text(ref, "evidence_refs[]") for ref in (evidence_refs or ())]
        if not refs:
            raise TrackingCadenceValidationError(
                "a cadence version with no evidence refs is a rewrite, not a revision"
            )
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref.startswith("automation:"):
            if actor_ref != mission["autonomy"]["automation_principal"]:
                raise TrackingCadenceConflict("automation actor is not the mission principal")
            if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
                raise TrackingCadenceConflict(
                    f"mission does not grant {WRITE_SCOPE} writes to automation"
                )
        elif not actor_ref.startswith("human:"):
            raise TrackingCadenceValidationError(
                "actor_ref must be a human: or automation: principal"
            )
        baseline = policy["cadences"].get(source_key)
        if baseline is None:
            raise TrackingCadenceValidationError(
                f"{source_key} has no baseline cadence in {policy['policy_ref']}"
            )
        if not baseline["adjustable"]:
            raise TrackingCadenceConflict(
                f"{source_key} is a fixed cadence in {policy['policy_ref']}; "
                "the brain sets rates, it does not decide whether we look"
            )
        cadence_ref = cadence_ref_for(company_ref, source_key)
        with self.store._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM tracking_cadence_versions WHERE cadence_ref=? "
                "ORDER BY version_number DESC LIMIT 1",
                (cadence_ref,),
            ).fetchone()
            current = None if row is None else _decode(row, "TrackingCadenceVersion")
            if current is not None and (
                current["interval_seconds"] == interval_seconds
                and set(refs) <= set(current["evidence_refs"])
            ):
                return {**current, "status": "duplicate"}
            if current is None and interval_seconds == baseline["interval_seconds"]:
                return {
                    "status": "duplicate", "cadence_ref": cadence_ref,
                    "reason": "the baseline already says this",
                    "interval_seconds": interval_seconds,
                }
            version = 1 if current is None else current["version"] + 1
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": f"tracking-cadence-version:{content_hash({'ref': cadence_ref, 'version': version})[:32]}",
                "created_at": _now(),
                "cadence_ref": cadence_ref,
                "version": version,
                "prior_version_ref": None if current is None else current["id"],
                "company_ref": company_ref,
                "source_key": source_key,
                "interval_seconds": interval_seconds,
                "baseline_interval_seconds": baseline["interval_seconds"],
                "because": because,
                "evidence_refs": list(dict.fromkeys(refs)),
                "change_reason": change_reason,
                "decision": None if decision is None else _text(decision, "decision", maximum=64),
                "policy_ref": policy["policy_ref"],
                "policy_hash": policy["content_hash"],
                "mission_version_ref": mission["id"],
                "mission_version_hash": mission["content_hash"],
                "actor_ref": actor_ref,
            }
            record["content_hash"] = content_hash(record)
            cur.execute(
                "INSERT INTO tracking_cadence_versions(version_id,cadence_ref,version_number,"
                "prior_version_id,company_ref,source_key,interval_seconds,change_reason,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], cadence_ref, version, record["prior_version_ref"],
                    company_ref, source_key, interval_seconds, change_reason,
                    canonical_json(record), record["content_hash"], actor_ref,
                    record["created_at"],
                ),
            )
            written = _decode(
                cur.execute(
                    "SELECT * FROM tracking_cadence_versions WHERE version_id=?",
                    (record["id"],),
                ).fetchone(),
                "TrackingCadenceVersion",
            )
        return {**written, "status": "published"}


# ---------------------------------------------------------------------------
# next_due: the one line an existing lane adds
# ---------------------------------------------------------------------------


def immediate_pull_sources(
    policy: Mapping[str, Any], events: Sequence[Mapping[str, Any]], now: datetime
) -> dict[str, str]:
    """source_key -> the event ref that pulled it forward, or nothing.

    A price that moved without a reason is the one case where waiting for the
    baseline is wrong, so the trigger kinds in the policy schedule a pull of
    the news sources outside their own cadence.  The window is short on
    purpose: an event from last week does not justify a pull today, and a
    trigger with no expiry would make the cadence permanently meaningless.
    """

    rule = policy["immediate_pull"]
    window = timedelta(seconds=max(0, int(rule["within_seconds"])))
    if not window:
        return {}
    pulled: dict[str, str] = {}
    for event in events:
        if event.get("kind") not in rule["trigger_kinds"]:
            continue
        try:
            occurred = datetime.fromisoformat(
                str(event["occurred_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        if now - occurred > window or occurred > now + timedelta(seconds=60):
            continue
        for source_key in rule["source_keys"]:
            pulled.setdefault(source_key, str(event.get("id") or event.get("event_ref") or ""))
    return pulled


def next_due(
    cadence: Mapping[str, Any],
    *,
    now: datetime,
    last_pull_at: str | datetime | None = None,
    pulled_forward_by: str | None = None,
) -> dict[str, Any]:
    """Is this (company, source) due, and when is it next due?

    Pure, so a lane can ask it without opening anything, and so the answer is
    the same for the lane that asks and the report that explains it.
    """

    interval = timedelta(seconds=int(cadence["interval_seconds"]))
    if last_pull_at is None:
        return {
            "due": True, "due_at": now.isoformat(timespec="seconds"),
            "reason": "never pulled", "interval_seconds": int(cadence["interval_seconds"]),
            "origin": cadence.get("origin", "baseline"),
        }
    if isinstance(last_pull_at, datetime):
        last = last_pull_at.astimezone(timezone.utc)
    else:
        last = datetime.fromisoformat(rfc3339(last_pull_at, "last_pull_at"))
    due_at = last + interval
    if pulled_forward_by:
        return {
            "due": True, "due_at": now.isoformat(timespec="seconds"),
            "reason": f"pulled forward by {pulled_forward_by}",
            "interval_seconds": int(cadence["interval_seconds"]),
            "origin": "immediate",
        }
    return {
        "due": now >= due_at,
        "due_at": due_at.isoformat(timespec="seconds"),
        "reason": "baseline" if cadence.get("origin") == "baseline" else "cadence version",
        "interval_seconds": int(cadence["interval_seconds"]),
        "origin": cadence.get("origin", "baseline"),
    }


def due_sources(
    authority: TrackingCadenceAuthority,
    *,
    company_ref: str,
    policy: Mapping[str, Any],
    now: datetime,
    last_pulls: Mapping[str, str | None],
    events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """Every source's answer for one company, in one call.

    What the tracking lane reports and what the report tells the existing
    discovery lanes to consult.
    """

    pulled = immediate_pull_sources(policy, events, now)
    answers: dict[str, dict[str, Any]] = {}
    for source_key in sorted(policy["cadences"]):
        cadence = authority.cadence(company_ref, source_key, policy=policy)
        answers[source_key] = {
            **cadence,
            **next_due(
                cadence, now=now, last_pull_at=last_pulls.get(source_key),
                pulled_forward_by=pulled.get(source_key),
            ),
        }
    return answers


__all__ = [
    "CHANGE_REASONS",
    "FIRST_STAGE",
    "POLICY_PATH",
    "SCHEMA_VERSION",
    "TrackingCadenceAuthority",
    "TrackingCadenceConflict",
    "TrackingCadenceError",
    "TrackingCadenceValidationError",
    "WRITE_SCOPE",
    "active_coverage_metrics",
    "baseline_cadences",
    "cadence_ref_for",
    "due_sources",
    "immediate_pull_sources",
    "load_policy",
    "next_due",
    "screen_passed_companies",
]
