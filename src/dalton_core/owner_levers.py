"""The owner's levers: pause, resume, stop, emergency stop, prioritise.

Vision-gap #11 (2026-09-30 report, section 1.3).  Before this module the only
way to stop part of the running system was a shell script that rewrote a JSON
setting in every environment (``docs/ops/pause-support-backfill.sh``), and the
only way to say "look at this company first" was a table ADR-0009 retired.

**One authority, append-only receipts.**  Every lever the owner pulls is one
row in ``owner_lever_receipts``: who (always a ``human:`` principal -- the
writer binds it from the authenticated token), when, why, until when, and what
came of it.  Nothing is ever updated.  The state the controller obeys is
*folded* from the rows by :func:`lever_state`: for each company, lane and the
global switch the newest pause/resume receipt wins, and a pause whose
``until`` has passed simply reads as resumed.  That is how a TTL "auto-resumes"
without a timer, a daemon or a write: expiry is a fact about the clock.

**Where the fold is obeyed** (each a single choke point the rest of the code
already passes through):

- ``WriterServer._run_lane``: a paused lane, or every lane during an emergency
  stop, answers ``paused`` without running; finished children are still
  reaped.  While it runs, a lane sees the mission universe without the paused
  companies (:func:`dispatch_scope` / :func:`dispatch_view`).
- ``LaneChildLauncher.spawn``: no child for a paused company or lane, and no
  child at all during an emergency stop (:func:`spawn_refusal`).
- ``ThesisImpactBudgetStore.admit``: the day ledger every paid model call is
  admitted against refuses new calls during an emergency stop
  (:func:`emergency_refusal_for_state_dir`); in-flight calls settle normally.
- The admission authorities (document research, annual research, research
  questions, bounded-planner rounds) call :func:`assert_admission_open`.
- ``BoundedPlannerDriver.run_once`` reads the fold once per tick and writes it
  into the tick summary, so the tick ledger says "paused by owner until ...".

The state lives in the Core, so it survives restarts, and every environment
runs the same code against its own Core: a new workspace needs nothing extra
-- the table is created on first use.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "owner-lever-receipt-0.1"
_SCHEMA_PATH = Path(__file__).with_name("owner_lever_schema.sql")

LEVERS: tuple[str, ...] = (
    "pause", "resume", "stop_line", "emergency_stop", "resume_all",
    "prioritize", "coverage_change",
)
SCOPE_KINDS: tuple[str, ...] = ("company", "lane", "line", "global", "mission")
# Which scope each lever acts on.  Closed: a lever on the wrong kind of thing
# is a refusal, not a guess.
LEVER_SCOPES: dict[str, frozenset[str]] = {
    "pause": frozenset({"company", "lane"}),
    "resume": frozenset({"company", "lane"}),
    "stop_line": frozenset({"line"}),
    "emergency_stop": frozenset({"global"}),
    "resume_all": frozenset({"global"}),
    "prioritize": frozenset({"company"}),
    "coverage_change": frozenset({"mission"}),
}
# Only a pause and an emergency stop may expire.  A prioritisation is a
# TrackingCadenceVersion and ADR-0009 is explicit that ordering pressure is a
# versioned cadence "rather than a nudge that silently expires".
TTL_LEVERS = frozenset({"pause", "emergency_stop"})
GLOBAL_SCOPE_REF = "global"
MAX_TTL = timedelta(days=30)
MAX_REASON_CHARS = 600
# The SEC dispatch is not a registry lane but is the one other thing the
# controller tick starts on its own, so it may be paused like one.
EXTRA_LANE_OPERATIONS = frozenset({"dispatch_coverage_mission_sec_lane"})
PAUSED_STATUS = "paused"
# Lanes an emergency stop does not silence.  Owner delivery (vision-gap #13)
# is how the owner hears that the stop took effect; it starts no research,
# no model call and no paid fetch.  A pause of the lane *by name* still holds.
EMERGENCY_EXEMPT_LANES = frozenset({"dispatch_owner_delivery"})
EMERGENCY_REASON_CODE = "owner_emergency_stop"
PAUSE_REASON_CODE = "owner_pause"
_LINE_PREFIXES = ("mission-document-research-admission:",)


class OwnerLeverError(RuntimeError):
    """Base error for the owner lever authority."""


class OwnerLeverValidationError(OwnerLeverError, ValueError):
    """A lever request does not satisfy the closed contract."""


class OwnerLeverConflict(OwnerLeverError):
    """A lever request conflicts with what is already recorded."""


class OwnerLeverHold(OwnerLeverError):
    """New work was refused because the owner paused it.

    The message always starts ``paused by owner`` or ``owner_emergency_stop``
    so a lane's failure classifier and the cockpit's translator can both read
    it without importing this module.
    """


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any, name: str = "until") -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise OwnerLeverValidationError(f"{name} is not an ISO timestamp") from exc
    else:
        raise OwnerLeverValidationError(f"{name} must be an ISO timestamp")
    if moment.tzinfo is None:
        raise OwnerLeverValidationError(f"{name} must carry a timezone")
    return moment.astimezone(timezone.utc)


def _text(value: Any, name: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OwnerLeverValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise OwnerLeverValidationError(f"{name} must be at most {maximum} characters")
    return value


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


def _decode(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record_json = row["record_json"] if isinstance(row, sqlite3.Row) else row[0]
    wire = json.loads(record_json)
    return wire if isinstance(wire, dict) else None


class OwnerLeverAuthority:
    """The append-only receipt book.  Writes only through a DaltonStore."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("OwnerLeverAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def record(
        self,
        *,
        lever: str,
        scope_kind: str,
        scope_ref: str,
        reason: str,
        actor_ref: str,
        until: str | datetime | None = None,
        outcome: str = "applied",
        refs: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one receipt.  The same idempotency key replays the first."""

        if lever not in LEVERS:
            raise OwnerLeverValidationError(f"lever must be one of {list(LEVERS)}")
        if scope_kind not in LEVER_SCOPES[lever]:
            raise OwnerLeverValidationError(
                f"{lever} acts on {sorted(LEVER_SCOPES[lever])}, not {scope_kind}")
        scope_ref = _text(scope_ref, "scope_ref", maximum=240)
        if scope_kind == "global" and scope_ref != GLOBAL_SCOPE_REF:
            raise OwnerLeverValidationError("the global scope is spelled 'global'")
        reason = _text(reason, "reason", maximum=MAX_REASON_CHARS)
        actor_ref = _text(actor_ref, "actor_ref", maximum=160)
        if not actor_ref.startswith("human:"):
            raise OwnerLeverValidationError("only a person may pull an owner lever")
        outcome = _text(outcome, "outcome", maximum=64)
        moment = (now or _utc_now()).astimezone(timezone.utc)
        until_text = None
        if until is not None:
            if lever not in TTL_LEVERS:
                raise OwnerLeverValidationError(f"{lever} does not expire; leave until empty")
            until_at = parse_time(until)
            if until_at <= moment:
                raise OwnerLeverValidationError("until must be in the future")
            if until_at - moment > MAX_TTL:
                raise OwnerLeverValidationError("a pause may last at most 30 days; renew it after")
            until_text = _iso(until_at)
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key", maximum=200)
        request = {
            "lever": lever, "scope_kind": scope_kind, "scope_ref": scope_ref,
            "reason": reason, "until": until_text, "actor_ref": actor_ref,
            "outcome": outcome, "refs": json.loads(canonical_json(dict(refs or {}))),
        }
        with self.store._transaction() as cur:
            if idempotency_key is not None:
                row = cur.execute(
                    "SELECT record_json FROM owner_lever_receipts WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if row is not None:
                    existing = _decode(row) or {}
                    if existing.get("request_hash") != content_hash(request):
                        raise OwnerLeverConflict(
                            "idempotency key was already used for a different lever")
                    return {**existing, "status": "replayed"}
            last = cur.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM owner_lever_receipts").fetchone()[0]
            prior = cur.execute(
                "SELECT receipt_id FROM owner_lever_receipts WHERE scope_kind=? AND scope_ref=? "
                "ORDER BY sequence DESC LIMIT 1", (scope_kind, scope_ref)).fetchone()
            sequence = int(last) + 1
            created_at = moment.isoformat(timespec="microseconds")
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": "owner-lever-receipt:" + content_hash(
                    {"sequence": sequence, "request": request, "at": created_at})[:32],
                "sequence": sequence,
                "prior_receipt_ref": None if prior is None else prior[0],
                **request,
                "request_hash": content_hash(request),
                "created_at": created_at,
            }
            record["content_hash"] = content_hash(record)
            cur.execute(
                "INSERT INTO owner_lever_receipts(receipt_id,sequence,lever,scope_kind,scope_ref,"
                "prior_receipt_id,reason,until_at,outcome,actor_ref,idempotency_key,record_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], sequence, lever, scope_kind, scope_ref,
                 record["prior_receipt_ref"], reason, until_text, outcome, actor_ref,
                 idempotency_key, canonical_json(record), record["content_hash"], created_at))
        return {**record, "status": "recorded"}

    def receipts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return receipts(self.connection, limit=limit)

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        return lever_state(self.connection, now=now)


def receipts(connection: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    """Newest first.  A Core that never had a lever answers an empty list."""

    try:
        rows = connection.execute(
            "SELECT record_json FROM owner_lever_receipts ORDER BY sequence DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    return [wire for wire in (_decode(row) for row in rows) if wire is not None]


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------


def empty_state(now: datetime | None = None) -> dict[str, Any]:
    return {"as_of": _iso(now or _utc_now()), "emergency": None, "companies": {},
            "lanes": {}, "expired": [], "receipt_count": 0}


def lever_state(connection: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """What is paused right now, folded from the receipts.

    ``companies`` and ``lanes`` map a scope ref to the pause receipt in force;
    ``emergency`` is the emergency-stop receipt in force or ``None``;
    ``expired`` lists pauses whose ``until`` has passed (auto-resumed) so a
    page can say so.  Works on a read-only connection and on a Core that has
    never seen a lever.
    """

    moment = (now or _utc_now()).astimezone(timezone.utc)
    state = empty_state(moment)
    try:
        rows = connection.execute(
            "SELECT record_json FROM owner_lever_receipts "
            "WHERE lever IN ('pause','resume','emergency_stop','resume_all') "
            "ORDER BY sequence ASC").fetchall()
        state["receipt_count"] = int(connection.execute(
            "SELECT COUNT(*) FROM owner_lever_receipts").fetchone()[0])
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return state
        raise
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        wire = _decode(row)
        if wire is not None:
            latest[(wire["scope_kind"], wire["scope_ref"])] = wire
    for (kind, ref), wire in sorted(latest.items()):
        if wire["lever"] not in {"pause", "emergency_stop"}:
            continue
        until = wire.get("until")
        if until is not None and parse_time(until) <= moment:
            state["expired"].append(wire)
            continue
        if kind == "global":
            state["emergency"] = wire
        elif kind == "company":
            state["companies"][ref] = wire
        elif kind == "lane":
            state["lanes"][ref] = wire
    return state


def lever_state_for_state_dir(
    state_dir: str | Path | None, now: datetime | None = None,
) -> dict[str, Any]:
    """The fold read from ``<state_dir>/core.sqlite`` with ``mode=ro``.

    For the processes that hold no store: lane children, the day ledger, the
    model adapter.  An absent or unreadable Core answers "nothing paused":
    a lever is an instruction the owner gave, and a read error is not one.
    """

    if state_dir is None:
        return empty_state(now)
    path = Path(state_dir) / "core.sqlite"
    if not path.is_file():
        return empty_state(now)
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return empty_state(now)
    try:
        connection.row_factory = sqlite3.Row
        return lever_state(connection, now=now)
    except (sqlite3.Error, ValueError, KeyError):
        return empty_state(now)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# sentences: the ledger's (English, stable) and the cockpit's (Chinese)
# ---------------------------------------------------------------------------


def paused_sentence(receipt: Mapping[str, Any]) -> str:
    """``paused by owner until 2026-10-07T08:00:00Z (reason)`` -- the ledger's words."""

    until = receipt.get("until")
    head = ("owner_emergency_stop: emergency stop by owner"
            if receipt.get("lever") == "emergency_stop" else "paused by owner")
    when = f" until {until}" if until else " until resumed"
    return f"{head}{when} ({str(receipt.get('reason') or '')[:120]})"


def paused_label(receipt: Mapping[str, Any]) -> str:
    until = receipt.get("until")
    head = "紧急停机" if receipt.get("lever") == "emergency_stop" else "已由你暂停"
    when = f"，到 {until} 自动恢复" if until else "，直到你恢复"
    return f"{head}{when}（理由：{receipt.get('reason') or '未写'}）"


def tick_summary(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the controller tick records about the levers, or ``None``.

    ``None`` when nothing is paused, so a quiet system's tick ledger does not
    gain a row that says nothing.  The status word is ``paused`` (an owner's
    choice, not a stall).
    """

    emergency = state.get("emergency")
    companies = state.get("companies") or {}
    lanes = state.get("lanes") or {}
    if not emergency and not companies and not lanes:
        return None
    parts: list[str] = []
    if emergency:
        parts.append(paused_sentence(emergency))
    for ref, receipt in sorted(companies.items()):
        parts.append(f"company {ref} {paused_sentence(receipt)}")
    for ref, receipt in sorted(lanes.items()):
        parts.append(f"lane {ref} {paused_sentence(receipt)}")
    return {
        "status": f"{PAUSED_STATUS}:" + "; ".join(parts)[:400],
        "reason": "; ".join(parts)[:400],
        "emergency_stop": bool(emergency),
        "paused_companies": len(companies),
        "paused_lanes": len(lanes),
    }


# ---------------------------------------------------------------------------
# enforcement helpers
# ---------------------------------------------------------------------------


def lane_refusal(state: Mapping[str, Any], operation: str) -> dict[str, Any] | None:
    """The payload a lane answers instead of running, or ``None`` to run."""

    emergency = state.get("emergency")
    if operation in EMERGENCY_EXEMPT_LANES:
        emergency = None
    receipt = emergency or (state.get("lanes") or {}).get(operation)
    if receipt is None:
        return None
    sentence = paused_sentence(receipt)
    return {
        "status": f"{PAUSED_STATUS}:{sentence}"[:400],
        "reason": sentence,
        "reason_code": EMERGENCY_REASON_CODE if emergency else PAUSE_REASON_CODE,
        "owner_lever_ref": receipt.get("id"),
        "paused_until": receipt.get("until"),
    }


def admission_refusal(
    state: Mapping[str, Any], company_ref: str | None = None,
) -> str | None:
    emergency = state.get("emergency")
    if emergency:
        return paused_sentence(emergency) + "; no new admissions"
    if company_ref:
        receipt = (state.get("companies") or {}).get(company_ref)
        if receipt is not None:
            return f"company {company_ref} {paused_sentence(receipt)}"
    return None


def assert_admission_open(connection: sqlite3.Connection, company_ref: str | None = None) -> None:
    """Raise :class:`OwnerLeverHold` if the owner has stopped new admissions here."""

    try:
        state = lever_state(connection)
    except sqlite3.Error:
        return
    refusal = admission_refusal(state, company_ref)
    if refusal is not None:
        raise OwnerLeverHold(refusal)


def emergency_refusal_for_state_dir(state_dir: str | Path | None) -> str | None:
    emergency = lever_state_for_state_dir(state_dir).get("emergency")
    return None if emergency is None else paused_sentence(emergency) + "; no new paid model calls"


def _record_company_refs(record: Mapping[str, Any], extra: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    for source in (record, extra):
        for key in ("company_ref", "subject_ref"):
            value = source.get(key)
            if isinstance(value, str) and value.startswith("company:"):
                refs.add(value)
        for key in ("company_refs",):
            value = source.get(key)
            if isinstance(value, (list, tuple)):
                refs.update(str(item) for item in value if isinstance(item, str))
    return refs


def spawn_refusal(
    state_dir: str | Path | None, record: Mapping[str, Any] | None,
    command_kwargs: Mapping[str, Any] | None = None, *, lane: str | None = None,
) -> str | None:
    """Why a lane child may not start now, or ``None``."""

    state = lever_state_for_state_dir(state_dir)
    if state.get("emergency"):
        return paused_sentence(state["emergency"]) + "; no new lane children"
    if lane and lane in (state.get("lanes") or {}):
        return f"lane {lane} {paused_sentence(state['lanes'][lane])}"
    companies = state.get("companies") or {}
    if companies:
        for ref in sorted(_record_company_refs(record or {}, command_kwargs or {})):
            if ref in companies:
                return f"company {ref} {paused_sentence(companies[ref])}"
    return None


# -- the universe a dispatching lane sees -------------------------------------

_SCOPE = threading.local()


@contextmanager
def dispatch_scope(paused_companies: Sequence[str] | frozenset[str]) -> Iterator[None]:
    """While a lane dispatches, the paused companies are not in its universe.

    Thread-local and set only by the writer around one lane tick, so an owner
    operation or an authority's own bookkeeping on the same thread before or
    after never sees a filtered mission.  The authority's internal reads
    (authorisations, records, settles) use the unfiltered record, so a child
    already running for a company that was just paused still settles.
    """

    previous = getattr(_SCOPE, "paused", None)
    _SCOPE.paused = frozenset(paused_companies)
    try:
        yield
    finally:
        _SCOPE.paused = previous


def dispatch_view(mission: dict[str, Any]) -> dict[str, Any]:
    """The mission as a dispatching lane should see it (a copy if filtered)."""

    paused = getattr(_SCOPE, "paused", None)
    if not paused or not isinstance(mission, dict) or not isinstance(mission.get("universe"), list):
        return mission
    universe = [member for member in mission["universe"]
                if member.get("company_ref") not in paused]
    if len(universe) == len(mission["universe"]):
        return mission
    return {**mission, "universe": universe,
            "owner_paused_company_refs": sorted(paused)}


def line_kind(line_ref: str) -> str:
    """``admission`` for a document-research admission, else ``question``."""

    if any(line_ref.startswith(prefix) for prefix in _LINE_PREFIXES):
        return "admission"
    return "question"


def ttl_until(hours: Any, *, now: datetime | None = None) -> str | None:
    """``hours`` from the cockpit (``None``/0 = until resumed) as an ISO time."""

    if hours in (None, "", 0, "0"):
        return None
    try:
        value = float(hours)
    except (TypeError, ValueError) as exc:
        raise OwnerLeverValidationError("暂停时长必须是小时数") from exc
    if not 0 < value <= MAX_TTL.total_seconds() / 3600:
        raise OwnerLeverValidationError("暂停时长必须在 0 到 720 小时之间")
    return _iso((now or _utc_now()) + timedelta(hours=value))


_SAFE_REF = re.compile(r"^[A-Za-z0-9:._\-/]+$")


def safe_ref(value: Any, name: str) -> str:
    text = _text(value, name, maximum=240)
    if not _SAFE_REF.fullmatch(text):
        raise OwnerLeverValidationError(f"{name} has characters a ref may not contain")
    return text


__all__ = [
    "EMERGENCY_REASON_CODE", "EXTRA_LANE_OPERATIONS", "GLOBAL_SCOPE_REF", "LEVERS",
    "LEVER_SCOPES", "OwnerLeverAuthority", "OwnerLeverConflict", "OwnerLeverError",
    "OwnerLeverHold", "OwnerLeverValidationError", "PAUSED_STATUS", "PAUSE_REASON_CODE",
    "SCOPE_KINDS", "TTL_LEVERS", "admission_refusal", "assert_admission_open",
    "dispatch_scope", "dispatch_view", "emergency_refusal_for_state_dir", "empty_state",
    "lane_refusal", "lever_state", "lever_state_for_state_dir", "line_kind",
    "paused_label", "paused_sentence", "receipts", "safe_ref", "spawn_refusal",
    "tick_summary", "ttl_until",
]
