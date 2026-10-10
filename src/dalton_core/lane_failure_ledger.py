"""P17d: the operations backlog -- which work is parked on which dependency.

The classification lives in :mod:`lane_failure_class`; this is where its
verdicts are kept so that somebody other than the process that made them can
read them.  It is the same sidecar shape as the C2 tick ledger: one sqlite file
beside the scheduler, append-only, opened read-only by the cockpit, with
retention as a reader-side window rather than a delete.

The reader that matters is :meth:`LaneFailureLedger.parked_by_dependency`.  It
answers the question the predecessor project could not answer without opening a
CSV: *what is this system waiting on, since when, and how much of it is there.*
The answer is computed by replaying the events, not by reading a column, so it
is the same answer before and after a restart and no bookkeeping can drift out
of step with the evidence.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .sqlite_path import sqlite_path
from .lane_failure_class import (
    CONTENT_REFUSED,
    DEPENDENCY_UNAVAILABLE,
    NOT_PERMITTED,
    MAX_REASON_CHARS,
    TRANSIENT,
    UNKNOWN_DEPENDENCY,
)
from .store import content_hash


class LaneFailureLedgerError(RuntimeError):
    """The ledger could not record or read a lane failure."""


SCHEMA_VERSION = "0.1"
DEFAULT_FILENAME = "lane-failure-ledger.sqlite"
_SCHEMA_PATH = Path(__file__).with_name("lane_failure_ledger_schema.sql")

# The same 90 days the tick ledger reads back over, for the same reason: the
# rows stay, the reader declines to look further, and an archiver can be added
# later without anyone having had to trust that nothing was pruned.
RETENTION_DAYS = 90

# The events a lane appends.  ``parked_again`` is kept apart from ``parked``
# because "this dependency has now failed us eleven times" is a different fact
# from "this item is waiting", and the first is what tells an operator which
# outage to go and fix.
PARK_EVENTS: frozenset[str] = frozenset({"parked", "parked_again"})
CLEAR_EVENTS: frozenset[str] = frozenset({"resumed", "dependency_ok", "permission_ok"})
EVENTS: frozenset[str] = PARK_EVENTS | CLEAR_EVENTS | {"terminal", "held", "not_permitted", "superseded"}

_FAILURE_CLASSES = frozenset({DEPENDENCY_UNAVAILABLE, CONTENT_REFUSED, NOT_PERMITTED, TRANSIENT})

# 2026-10-09 (ops noise): a parked item whose dependency came back, or that
# has not been parked again for a while, stops counting as "waiting".  Live,
# legacy showed 「模型调用预算：20 项等待处理，模型服务：7 项等待处理」 for items
# first parked 2026-09-14..24 whose model budget and chains had recovered weeks
# before: the dependency_ok that would release them was written by *another*
# lane, and the fold only honoured its own lane's.
#
# The release is still an event, appended (``resumed`` with one of the two
# rules below), never an edit: :func:`release_stale_parks` writes them, and
# until it has run the fold applies the same two rules on read, so the
# cockpit's count is right the moment this code is deployed.
AUTO_RESOLVED_RULE = "parked_dependency_recovered"
AUTO_ARCHIVED_RULE = "parked_expired_no_recurrence"
AUTO_RELEASE_RULES: frozenset[str] = frozenset({AUTO_RESOLVED_RULE, AUTO_ARCHIVED_RULE})
#: A parked item not parked again for this many days is archived.  A lane that
#: still cannot get through parks it again on its next attempt, so archiving
#: cannot hide a live outage for longer than one attempt.
DEFAULT_PARKED_EXPIRY_DAYS = 7


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def default_path(state_dir: str | Path) -> Path:
    """Where the ledger lives: beside the scheduler, in the state directory."""

    return Path(state_dir) / DEFAULT_FILENAME


def _window(window: Any, *, now: datetime | None = None) -> tuple[str, str]:
    """``(since_day, until_day)``, clamped to retention.  As the tick ledger."""

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    until = moment.date()
    floor = until - timedelta(days=RETENTION_DAYS - 1)
    if window is None:
        since = floor
    elif isinstance(window, int) and not isinstance(window, bool):
        if window < 1:
            raise LaneFailureLedgerError("a window of days must be at least one day")
        since = until - timedelta(days=window - 1)
    elif isinstance(window, (tuple, list)) and len(window) == 2:
        try:
            since = datetime.strptime(str(window[0]), "%Y-%m-%d").date()
            until = datetime.strptime(str(window[1]), "%Y-%m-%d").date()
        except ValueError as exc:
            raise LaneFailureLedgerError("a window is two YYYY-MM-DD days") from exc
    else:
        raise LaneFailureLedgerError(
            "a window is None, a number of days, or a (since, until) pair")
    if since < floor:
        since = floor
    return since.isoformat(), until.isoformat()


class LaneFailureLedger:
    """Append-only lane failure history, and the ops backlog read off it."""

    def __init__(
        self, path: str | Path = ":memory:", *, read_only: bool = False,
        clock: Any | None = None,
    ) -> None:
        self.path = sqlite_path(path)
        self.read_only = read_only
        if not read_only and self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.touch(mode=0o600, exist_ok=True)
        if read_only:
            from .readonly_sqlite import connect_read_only

            self.connection = connect_read_only(self.path)
        else:
            self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            # Deliberately not WAL, for the tick ledger's reason: a cleanly
            # closed WAL database has no sidecars, and ``connect_read_only``
            # refuses one whose sidecars are absent.  The cockpit reads a file
            # the writer closed minutes ago.
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LaneFailureLedger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def append_event(
        self, *, lane: str, item_key: str, event: str, failure_class: str,
        reason: str, rule: str, dependency: str | None = None,
        status: str | None = None, at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one classified failure.  The same event twice is one row."""

        if self.read_only:
            raise LaneFailureLedgerError("the lane failure ledger is read_only")
        if event not in EVENTS:
            raise LaneFailureLedgerError(
                f"{event!r} is not a lane failure event; the events are "
                + ", ".join(sorted(EVENTS)))
        if failure_class not in _FAILURE_CLASSES:
            raise LaneFailureLedgerError(
                f"{failure_class!r} is not a failure class; the classes are "
                + ", ".join(sorted(_FAILURE_CLASSES)))
        moment = (at or self.clock()).astimezone(timezone.utc)
        row = {
            "recorded_at": _utc(moment),
            "day": moment.date().isoformat(),
            "lane": str(lane),
            "item_key": str(item_key),
            "event": event,
            "failure_class": failure_class,
            "dependency": (str(dependency) if dependency else None),
            "reason": str(reason or "")[:MAX_REASON_CHARS],
            "rule": str(rule or "unmapped"),
            "status": (str(status)[:MAX_REASON_CHARS] if status else None),
        }
        return self._insert(row)

    def append_dependency_ok(
        self, *, lane: str, dependency: str, resumed: Sequence[str] = (),
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record that a probe of ``dependency`` succeeded.

        This is the resume half of the P14e hold, generalised: the round that
        was held resumed because the *next* writer call worked, and here any
        successful read of a named dependency releases everything parked on it.
        One row names the dependency; the items it freed are in the reason, so
        the replay does not depend on a join.
        """

        name = str(dependency or UNKNOWN_DEPENDENCY)
        freed = ", ".join(str(item) for item in resumed) or "nothing was parked"
        return self.append_event(
            lane=lane, item_key="", event="dependency_ok",
            failure_class=DEPENDENCY_UNAVAILABLE, dependency=name,
            reason=f"a probe of {name} succeeded; resumed: {freed}",
            rule="dependency_probe_succeeded", at=at,
        )

    def _insert(self, row: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record["content_hash"] = content_hash(record)
        event_id = "lane-failure:" + record["content_hash"][:32]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT 1 FROM lane_failure_events WHERE event_id=?", (event_id,),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO lane_failure_events(event_id,recorded_at,day,lane,"
                    "item_key,event,failure_class,dependency,reason,rule,status,"
                    "content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, record["recorded_at"], record["day"],
                        record["lane"], record["item_key"], record["event"],
                        record["failure_class"], record["dependency"],
                        record["reason"], record["rule"], record["status"],
                        record["content_hash"],
                    ),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        # ``record`` first: it carries the lane's own ``status`` field, and
        # spreading it last would overwrite the append result with it.
        return {
            **record,
            "schema_version": SCHEMA_VERSION, "event_id": event_id,
            "status": "duplicate" if existing is not None else "recorded",
            "lane_status": record["status"],
        }

    # -- reading -----------------------------------------------------------

    def events(
        self, window: Any = None, *, now: datetime | None = None,
        lane: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every event in the window, oldest first.  The replay input."""

        since, until = _window(window, now=now)
        sql = ("SELECT * FROM lane_failure_events WHERE day BETWEEN ? AND ? ")
        params: list[Any] = [since, until]
        if lane is not None:
            sql += "AND lane=? "
            params.append(lane)
        # A content hash is not a sequence: preserve append order when two
        # operations share a timestamp, including with an injected clock.
        sql += "ORDER BY recorded_at, rowid"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def release_stale_parks(
        self, *, now: datetime | None = None,
        expiry_days: int | None = DEFAULT_PARKED_EXPIRY_DAYS,
    ) -> list[dict[str, Any]]:
        """Append a ``resumed`` event for every parked item the fold would
        release (dependency recovered elsewhere, or expired).  Idempotent: an
        item released once is no longer parked, so a second run appends
        nothing.  Returns the releases written."""

        if self.read_only:
            raise LaneFailureLedgerError("the lane failure ledger is read_only")
        moment = (now or self.clock()).astimezone(timezone.utc)
        rows = self.events(now=moment)
        written = []
        for release in stale_parks(rows, now=moment, expiry_days=expiry_days):
            result = self.append_event(
                lane=release["lane"], item_key=release["item_key"], event="resumed",
                failure_class=DEPENDENCY_UNAVAILABLE, dependency=release["dependency"],
                reason=release["reason"], rule=release["rule"],
                status=release["resolution"], at=moment)
            written.append({**release, "event_id": result["event_id"]})
        return written

    def parked_by_dependency(
        self, window: Any = None, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        """The ops backlog: what is parked, on what, since when.

        Computed by folding the events rather than by reading a state column.
        An item is parked when its last word was ``parked``; it stops being
        parked when its dependency answers, when it is resumed by name, or when
        a later read of it turned out to be terminal.
        """

        rows = self.events(window, now=now)
        return summarise_events(rows, window=_window(window, now=now))

    def dependency_history(
        self, dependency: str, window: Any = None, *, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Everything ever said about one dependency, oldest first."""

        since, until = _window(window, now=now)
        return [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM lane_failure_events WHERE dependency=? "
                "AND day BETWEEN ? AND ? ORDER BY recorded_at, event_id",
                (str(dependency), since, until),
            ).fetchall()
        ]


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _release_words(resolution: str, dependency: str, *, recovered_at: str | None = None,
                   recovered_lane: str | None = None, days: int | None = None) -> str:
    if resolution == "resolved":
        return (f"dependency {dependency} recovered: a probe succeeded in lane "
                f"{recovered_lane} at {recovered_at}; released automatically")
    return (f"not parked again for {days} days; archived automatically "
            "(a lane that still cannot get through parks it again)")


def stale_parks(
    rows: Iterable[Mapping[str, Any]], *, now: datetime | None = None,
    expiry_days: int | None = DEFAULT_PARKED_EXPIRY_DAYS,
) -> list[dict[str, Any]]:
    """The parked items the fold would release, and why.

    ``resolved``: a ``dependency_ok`` for the item's dependency was recorded
    after the item was last parked -- in any lane, because a dependency (the
    model budget, a model chain, SEC) is shared.  ``archived``: not parked
    again for ``expiry_days``.  ``now`` defaults to the newest event, so the
    answer is a function of the rows alone.
    """

    rows = list(rows)
    folded = _fold(rows, release=None)
    parked = folded["parked"]
    if not parked:
        return []
    recovered: dict[str, tuple[str, str]] = {}
    newest: str = ""
    for row in rows:
        at = str(row.get("recorded_at") or "")
        newest = max(newest, at)
        if row.get("event") == "dependency_ok":
            dependency = str(row.get("dependency") or UNKNOWN_DEPENDENCY)
            if at >= recovered.get(dependency, ("", ""))[0]:
                recovered[dependency] = (at, str(row.get("lane") or ""))
    moment = now or _parse_at(newest)
    out = []
    for entry in parked.values():
        dependency = entry["dependency"]
        last = entry["last_seen"]
        base = {"lane": entry["lane"], "item_key": entry["item_key"],
                "dependency": dependency, "first_seen": entry["first_seen"],
                "last_seen": last, "attempts": entry["attempts"]}
        seen = recovered.get(dependency)
        if dependency != UNKNOWN_DEPENDENCY and seen is not None and seen[0] > last:
            out.append({**base, "resolution": "resolved", "rule": AUTO_RESOLVED_RULE,
                        "recovered_at": seen[0], "recovered_lane": seen[1],
                        "reason": _release_words("resolved", dependency,
                                                 recovered_at=seen[0], recovered_lane=seen[1])})
            continue
        last_at = _parse_at(last)
        if (expiry_days is not None and moment is not None and last_at is not None
                and moment - last_at > timedelta(days=int(expiry_days))):
            out.append({**base, "resolution": "archived", "rule": AUTO_ARCHIVED_RULE,
                        "reason": _release_words("archived", dependency, days=int(expiry_days))})
    out.sort(key=lambda item: (item["lane"], item["item_key"]))
    return out


def summarise_events(
    rows: Iterable[Mapping[str, Any]], *, window: tuple[str, str] | None = None,
    now: datetime | None = None,
    expiry_days: int | None = DEFAULT_PARKED_EXPIRY_DAYS,
) -> dict[str, Any]:
    """Fold lane failure events into the parked-item backlog.

    Kept as a function on rows rather than a method on a connection so the
    cockpit, a test and a report can all run the same fold over the same rows,
    and so the fold is exercised without a database.

    Parked items released automatically -- by an appended ``resumed`` event
    with an auto-release rule, or by the same two rules applied on read
    (:func:`stale_parks`) -- are not counted as waiting; they are listed under
    ``released_items`` with the reason.  ``expiry_days=None`` turns the
    read-side rules off (the appended events still count).
    """

    rows = list(rows)
    pending = {(item["lane"], item["item_key"]): item
               for item in stale_parks(rows, now=now, expiry_days=expiry_days)}
    folded = _fold(rows, release=pending)
    return _backlog(folded, window=window)


def _fold(rows: Iterable[Mapping[str, Any]], *,
          release: Mapping[tuple[str, str], Mapping[str, Any]] | None) -> dict[str, Any]:
    parked: dict[tuple[str, str], dict[str, Any]] = {}
    terminal: dict[tuple[str, str], dict[str, Any]] = {}
    permissions: dict[tuple[str, str], dict[str, Any]] = {}
    released: dict[tuple[str, str], dict[str, Any]] = {}
    events = 0
    for row in rows:
        events += 1
        lane = str(row.get("lane") or "")
        item = str(row.get("item_key") or "")
        event = str(row.get("event") or "")
        dependency = str(row.get("dependency") or UNKNOWN_DEPENDENCY)
        at = str(row.get("recorded_at") or "")
        key = (lane, item)
        if event in PARK_EVENTS:
            entry = parked.get(key)
            if entry is None:
                parked[key] = {
                    "lane": lane, "item_key": item, "dependency": dependency,
                    "first_seen": at, "last_seen": at, "attempts": 1,
                    "reason": str(row.get("reason") or ""),
                    "rule": str(row.get("rule") or ""),
                }
            else:
                entry["last_seen"] = at
                entry["attempts"] += 1
                entry["dependency"] = dependency
                entry["reason"] = str(row.get("reason") or entry["reason"])
            terminal.pop(key, None)
        elif event == "dependency_ok":
            for other, entry in list(parked.items()):
                if entry["dependency"] == dependency and other[0] == lane:
                    parked.pop(other, None)
        elif event == "resumed":
            entry = parked.pop(key, None)
            rule = str(row.get("rule") or "")
            if entry is not None and rule in AUTO_RELEASE_RULES:
                released[key] = {
                    "lane": lane, "item_key": item, "dependency": entry["dependency"],
                    "first_seen": entry["first_seen"], "last_seen": entry["last_seen"],
                    "attempts": entry["attempts"], "released_at": at, "rule": rule,
                    "resolution": ("resolved" if rule == AUTO_RESOLVED_RULE else "archived"),
                    "reason": str(row.get("reason") or ""), "recorded": True,
                }
        elif event == "terminal":
            parked.pop(key, None)
            terminal[key] = {
                "lane": lane, "item_key": item, "first_seen": at,
                "reason": str(row.get("reason") or ""),
                "rule": str(row.get("rule") or ""),
            }
            permissions.pop(key, None)
        elif event == "not_permitted":
            parked.pop(key, None)
            terminal.pop(key, None)
            permissions[key] = {
                "lane": lane, "item_key": item, "first_seen": at,
                "reason": str(row.get("reason") or ""),
                "rule": str(row.get("rule") or ""),
            }
        elif event == "permission_ok":
            permissions.pop(key, None)
        elif event == "superseded":
            parked.pop(key, None)
            terminal.pop(key, None)
            permissions.pop(key, None)
        if event in PARK_EVENTS:
            released.pop(key, None)
    for key, item in (release or {}).items():
        entry = parked.pop(key, None)
        if entry is not None:
            released[key] = {**item, "released_at": None, "recorded": False}
    return {"parked": parked, "terminal": terminal, "permissions": permissions,
            "released": released, "events": events}


def _backlog(folded: Mapping[str, Any], *, window: tuple[str, str] | None) -> dict[str, Any]:
    parked = folded["parked"]
    terminal = folded["terminal"]
    permissions = folded["permissions"]
    events = folded["events"]
    by_dependency: dict[str, dict[str, Any]] = {}
    for entry in parked.values():
        bucket = by_dependency.setdefault(entry["dependency"], {
            "dependency": entry["dependency"], "items": [],
            "first_seen": entry["first_seen"], "last_seen": entry["last_seen"],
            "attempts": 0,
        })
        bucket["items"].append(dict(entry))
        bucket["attempts"] += int(entry["attempts"])
        bucket["first_seen"] = min(bucket["first_seen"], entry["first_seen"])
        bucket["last_seen"] = max(bucket["last_seen"], entry["last_seen"])
    for bucket in by_dependency.values():
        bucket["items"].sort(key=lambda item: (item["lane"], item["item_key"]))
        bucket["item_count"] = len(bucket["items"])
        bucket["lanes"] = sorted({item["lane"] for item in bucket["items"]})

    dependencies = sorted(
        by_dependency.values(),
        key=lambda bucket: (-bucket["item_count"], bucket["dependency"]),
    )
    terminal_rows = sorted(
        terminal.values(), key=lambda row: (row["lane"], row["item_key"]))
    permission_rows = sorted(
        permissions.values(), key=lambda row: (row["lane"], row["item_key"]))
    return {
        "projection_kind": "lane_failure_backlog",
        "schema_version": SCHEMA_VERSION,
        "window": {"since": window[0], "until": window[1]} if window else None,
        "events": events,
        "dependencies": dependencies,
        "parked_items": sum(bucket["item_count"] for bucket in dependencies),
        "terminal_items": terminal_rows,
        "terminal_count": len(terminal_rows),
        "permission_items": permission_rows,
        "permission_count": len(permission_rows),
        # Parked items that stopped waiting without a lane resuming them by
        # name: the dependency came back elsewhere, or they expired.
        "released_items": sorted(folded["released"].values(),
                                 key=lambda row: (row["lane"], row["item_key"])),
        "released_count": len(folded["released"]),
    }


class LedgerWriter:
    """Opens the ledger for one append and closes it again.

    A lane holds one of these for the life of the writer, not a connection: a
    park is a rare event -- the whole point of the classification is that most
    ticks append nothing -- and a long-lived handle on a sqlite file the
    cockpit also opens is a cost with no matching benefit.  Every method here
    raises on failure; :class:`~.lane_failure_class.LaneFailureBudget` catches
    it and reports ``unrecorded:<Error>`` rather than failing the tick.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append_event(self, **kwargs: Any) -> dict[str, Any]:
        with LaneFailureLedger(self.path) as ledger:
            return ledger.append_event(**kwargs)

    def append_dependency_ok(self, **kwargs: Any) -> dict[str, Any]:
        with LaneFailureLedger(self.path) as ledger:
            return ledger.append_dependency_ok(**kwargs)

    def release_stale_parks(self, **kwargs: Any) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with LaneFailureLedger(self.path) as ledger:
            return ledger.release_stale_parks(**kwargs)

    def events(self, **kwargs: Any) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with LaneFailureLedger(self.path, read_only=True) as ledger:
            return ledger.events(**kwargs)


def lane_budget(
    lane: str, *, state_dir: str | Path | None = None,
    max_transient_failures: int | None = None, clock: Any | None = None,
    probe_interval_seconds: int | None = None,
    block_ttl_seconds: int | None = None,
) -> Any:
    """One lane's failure budget, backed by the ledger and replayed from it.

    ``state_dir`` absent means no ledger: the budget still classifies and still
    parks, in this process only, which is what every lane's budget was before
    P17d.  A lane in a unit test therefore needs no file.

    Constructing this **replays** the durable half of the ledger, so a writer
    that restarts while AlphaEngine is still down does not send three fresh
    children at a dead desktop page before rediscovering it.  The transient
    count is deliberately not replayed; see ``LaneFailureBudget.replay``.
    """

    from .lane_failure_class import DEFAULT_MAX_TRANSIENT_FAILURES, LaneFailureBudget

    ledger: LedgerWriter | None = None
    if state_dir is not None:
        ledger = LedgerWriter(default_path(state_dir))
    budget = LaneFailureBudget(
        lane, ledger=ledger, clock=clock,
        **({} if probe_interval_seconds is None else {
            "probe_interval_seconds": int(probe_interval_seconds)}),
        **({} if block_ttl_seconds is None else {
            "block_ttl_seconds": int(block_ttl_seconds)}),
        max_transient_failures=(
            DEFAULT_MAX_TRANSIENT_FAILURES if max_transient_failures is None
            else int(max_transient_failures)),
    )
    if ledger is not None:
        try:
            budget.replay(ledger.events(lane=lane))
        except Exception:  # noqa: BLE001 - an unreadable ledger is not a lane failure
            pass
    return budget


__all__ = [
    "AUTO_ARCHIVED_RULE",
    "AUTO_RELEASE_RULES",
    "AUTO_RESOLVED_RULE",
    "CLEAR_EVENTS",
    "DEFAULT_PARKED_EXPIRY_DAYS",
    "DEFAULT_FILENAME",
    "EVENTS",
    "PARK_EVENTS",
    "RETENTION_DAYS",
    "SCHEMA_VERSION",
    "LaneFailureLedger",
    "LaneFailureLedgerError",
    "LedgerWriter",
    "default_path",
    "lane_budget",
    "release_stale_parks",
    "stale_parks",
    "summarise_events",
]


def release_stale_parks(state_dir: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    """The writer's sweep: append the auto-release events for one state dir."""

    return LedgerWriter(default_path(state_dir)).release_stale_parks(**kwargs)
