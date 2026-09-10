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

from .lane_failure_class import (
    CONTENT_REFUSED,
    DEPENDENCY_UNAVAILABLE,
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
CLEAR_EVENTS: frozenset[str] = frozenset({"resumed", "dependency_ok"})
EVENTS: frozenset[str] = PARK_EVENTS | CLEAR_EVENTS | {"terminal", "held"}

_FAILURE_CLASSES = frozenset({DEPENDENCY_UNAVAILABLE, CONTENT_REFUSED, TRANSIENT})


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
        self.path = str(path)
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
        sql += "ORDER BY recorded_at, event_id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

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


def summarise_events(
    rows: Iterable[Mapping[str, Any]], *, window: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Fold lane failure events into the parked-item backlog.

    Kept as a function on rows rather than a method on a connection so the
    cockpit, a test and a report can all run the same fold over the same rows,
    and so the fold is exercised without a database.
    """

    parked: dict[tuple[str, str], dict[str, Any]] = {}
    terminal: dict[tuple[str, str], dict[str, Any]] = {}
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
            parked.pop(key, None)
        elif event == "terminal":
            parked.pop(key, None)
            terminal[key] = {
                "lane": lane, "item_key": item, "first_seen": at,
                "reason": str(row.get("reason") or ""),
                "rule": str(row.get("rule") or ""),
            }

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
    return {
        "projection_kind": "lane_failure_backlog",
        "schema_version": SCHEMA_VERSION,
        "window": {"since": window[0], "until": window[1]} if window else None,
        "events": events,
        "dependencies": dependencies,
        "parked_items": sum(bucket["item_count"] for bucket in dependencies),
        "terminal_items": terminal_rows,
        "terminal_count": len(terminal_rows),
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

    def events(self, **kwargs: Any) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with LaneFailureLedger(self.path, read_only=True) as ledger:
            return ledger.events(**kwargs)


def lane_budget(
    lane: str, *, state_dir: str | Path | None = None,
    max_transient_failures: int | None = None, clock: Any | None = None,
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
    "CLEAR_EVENTS",
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
    "summarise_events",
]
