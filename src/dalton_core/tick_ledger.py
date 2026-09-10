"""What each controller tick did, kept instead of overwritten.

``bounded_planner_driver.run_once`` returns a summary; ``service`` writes it
into ``run/heartbeat.json``; the next tick overwrites it.  Q2's weekly
reflection went looking for the idle-tick ratio, lane stalls and scheduling
density and found that **the Core keeps no tick ledger** -- so those three
metrics come back ``available: false``, and cost can only be attributed by
guessing at work-order prefixes.  That finding is this module.

One row per tick and one row per lane per tick, appended in a single
transaction at the end of the tick, in a database beside the scheduler's.  The
rows are deliberately thin: the status word the lane reported, the small
counts it reported, which pool it drinks from, and what the day's pools moved
by.  A tick summary is not an audit record and this is not the Research
Ledger; it is the operational history a person needs to answer "has this thing
been working" without watching it.

Three properties, each chosen because its absence is what made the old
arrangement useless:

* **Append-only, and retention is a reader's window.**  Nothing is updated or
  deleted.  The readers refuse to look further back than
  :data:`RETENTION_DAYS`, so a later archiver can move old rows without anyone
  having had to trust that no one pruned the evidence first.
* **Idle is a status word, not an absence.**  A lane that reported nothing at
  all and a lane that reported ``idle`` look identical in a dict and mean
  different things; the ledger records the word, and a lane that returned
  nothing is recorded as ``missing``.
* **Writing the ledger can fail without failing the tick.**  The driver
  records the outcome of its own bookkeeping in the summary
  (``tick_ledger: {"status": "recorded"|"unrecorded:..."}``) rather than
  swallowing it, because a silent bookkeeper is worse than none.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .store import canonical_json, content_hash


class TickLedgerError(RuntimeError):
    """The ledger could not record or read a tick."""


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("tick_ledger_schema.sql")
DEFAULT_FILENAME = "tick-ledger.sqlite"

# How far back any reader will look.  Not a deletion policy: the rows stay,
# and an archiver added later decides what to do with the older ones.
RETENTION_DAYS = 90

# Status words that mean "this lane had nothing to do", as opposed to "this
# lane could not do it".  Kept in step with Q2's ``IDLE_LANE_STATUSES`` by
# intent rather than by import: that module lives on another branch, and a
# reflection that cannot be computed without this list would be worse than one
# that agrees with it a week later.
IDLE_STATUS_WORDS: frozenset[str] = frozenset({
    "idle", "held", "not_granted", "up_to_date", "nothing_to_do", "complete",
    "no_work", "quiet",
})

# Words that mean the lane wanted to work and could not.  ``skipped`` is not
# here: a pool that is spent is a budget decision, and it gets its own column.
STALL_STATUS_WORDS: frozenset[str] = frozenset({
    "unavailable", "unconfigured", "failed", "error", "busy", "conflict",
    "refused", "missing",
})

# What is kept out of a lane's ``counts`` blob.  A tick summary can carry a
# whole result envelope; the ledger keeps small scalars and short lists of
# scalars, which is what a count is.
_MAX_COUNT_KEYS = 12
_MAX_TEXT = 200


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def status_word(status: Any) -> str:
    """``skipped:pool_exhausted`` -> ``skipped``; anything unusable -> ``missing``."""

    text = str(status or "").strip()
    if not text:
        return "missing"
    return text.split(":", 1)[0]


def mentions_pool_exhausted(value: Any, *, depth: int = 2) -> bool:
    """Whether this lane result says a pool ran out, wherever it says it.

    A lane that does its model work in a child process does not say it at the
    top level: the word arrives a tick later as ``settled.index_status`` or
    ``last.summary_status``, while the lane's own status is ``launched``. A
    column that read only the top-level status would report a week of budget
    decisions as an ordinary quiet week, which is the exact confusion this
    column exists to prevent.
    """

    from .budget_pools import POOL_EXHAUSTED_REASON

    suffix = ":" + POOL_EXHAUSTED_REASON
    if isinstance(value, str):
        return value == POOL_EXHAUSTED_REASON or value.endswith(suffix)
    if depth <= 0:
        return False
    if isinstance(value, Mapping):
        return any(mentions_pool_exhausted(item, depth=depth - 1)
                   for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(mentions_pool_exhausted(item, depth=depth - 1)
                   for item in value)
    return False


def bounded_counts(result: Mapping[str, Any]) -> dict[str, Any]:
    """The small, countable part of one lane's result.

    Numbers, booleans, short strings and short lists of those.  Everything
    else -- envelopes, nested results, prose -- is left where it came from.
    """

    counts: dict[str, Any] = {}
    for key in sorted(result):
        if key in {"status", "reason"} or len(counts) >= _MAX_COUNT_KEYS:
            continue
        value = result[key]
        if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            counts[key] = value
        elif isinstance(value, str) and len(value) <= _MAX_TEXT:
            counts[key] = value
        elif isinstance(value, (list, tuple)):
            counts[key] = len(value)
    if isinstance(result.get("reason"), str):
        counts["reason"] = result["reason"][:_MAX_TEXT]
    return counts


def _apply_planner_budget_migration(connection: sqlite3.Connection) -> None:
    """Add C2b's ``planner_budget_json`` column to a ledger that predates it.

    Additive, nullable and idempotent, exactly like C2's own pool migration:
    ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists,
    so a running installation would otherwise keep a ledger with no room for
    the column and every append would fail.  Nothing already written changes,
    and no ``content_hash`` moves -- old rows simply have no answer, which is
    the truth about them.
    """

    existing = {
        row["name"] for row in
        connection.execute("PRAGMA table_info(tick_ledger_ticks)").fetchall()
    }
    if existing and "planner_budget_json" not in existing:
        connection.execute(
            "ALTER TABLE tick_ledger_ticks ADD COLUMN planner_budget_json TEXT")


def _window(window: Any, *, now: datetime | None = None) -> tuple[str, str]:
    """Normalise a window to ``(since_day, until_day)``, clamped to retention.

    Accepts ``None`` (the retention window), an integer number of days, or a
    ``(since, until)`` pair of ``YYYY-MM-DD`` strings.
    """

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    until = moment.date()
    floor = until - timedelta(days=RETENTION_DAYS - 1)
    if window is None:
        since = floor
    elif isinstance(window, int) and not isinstance(window, bool):
        if window < 1:
            raise TickLedgerError("a window of days must be at least one day")
        since = until - timedelta(days=window - 1)
    elif isinstance(window, (tuple, list)) and len(window) == 2:
        try:
            since = datetime.strptime(str(window[0]), "%Y-%m-%d").date()
            until = datetime.strptime(str(window[1]), "%Y-%m-%d").date()
        except ValueError as exc:
            raise TickLedgerError("a window is two YYYY-MM-DD days") from exc
    else:
        raise TickLedgerError(
            "a window is None, a number of days, or a (since, until) pair"
        )
    if since < floor:
        since = floor
    return since.isoformat(), until.isoformat()


class TickLedger:
    """Append-only tick history, with the readers Q2's reflection needs."""

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
            _apply_planner_budget_migration(self.connection)
            # Deliberately not WAL. A database that has ever been in WAL mode
            # keeps that in its header, and ``connect_read_only`` refuses one
            # whose -wal and -shm sidecars are absent -- which is exactly the
            # state a cleanly closed ledger is in. The readers here are the
            # cockpit and the weekly reflection, reading a file the tick wrote
            # minutes ago and closed; the rollback journal is what lets them.
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TickLedger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # -- writing ------------------------------------------------------------

    def append_tick(
        self, summary: Mapping[str, Any], *, started_at: datetime,
        ended_at: datetime | None = None,
        pool_spend: Mapping[str, int] | None = None,
        lane_operations: Mapping[str, str] | None = None,
        lane_pools: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Record one tick and its lanes.  Appending the same tick twice is a
        no-op, so a retried write cannot double-count a day.

        ``pool_spend`` is the day's cumulative committed micros per pool at the
        end of this tick; the row keeps both that and the delta against the
        last tick recorded for the same day, because a cumulative number is
        what can be checked against the ledger and a delta is what can be
        summed over a week.
        """

        if self.read_only:
            raise TickLedgerError("the tick ledger is read_only")
        from .lane_registry import RESERVED_DRIVER_KEYS

        ended = ended_at or self.clock()
        started_text, ended_text = _utc(started_at), _utc(ended)
        day = ended.astimezone(timezone.utc).date().isoformat()
        tick_id = "tick:" + content_hash({
            "started_at": started_text, "ended_at": ended_text,
        })[:32]
        lanes = {
            key: value for key, value in summary.items()
            if key not in RESERVED_DRIVER_KEYS and isinstance(value, Mapping)
        }
        operations = dict(lane_operations or {})
        pools = dict(lane_pools or {})
        if not operations or not pools:
            from .budget_pools import pool_for_operation
            from .lane_registry import tick_lanes

            for spec in tick_lanes():
                operations.setdefault(spec.driver_key, spec.operation)
                pools.setdefault(
                    spec.driver_key, pool_for_operation(spec.operation))
        cumulative = {
            key: int(value) for key, value in (pool_spend or {}).items()
        }
        previous = self._last_cumulative(day)
        delta = {
            key: int(value) - int(previous.get(key, 0))
            for key, value in cumulative.items()
        }
        lane_rows = []
        idle_lanes = 0
        for key, result in sorted(lanes.items()):
            word = status_word(result.get("status"))
            exhausted = mentions_pool_exhausted(result)
            idle = word in IDLE_STATUS_WORDS
            idle_lanes += int(idle)
            lane_rows.append((
                tick_id, day, started_text, key,
                operations.get(key, key), pools.get(key, "coverage"),
                str(result.get("status") or ""), word, int(idle), int(exhausted),
                canonical_json(bounded_counts(result)),
                result.get("pool_spend_delta_micros"),
                started_text,
            ))
        tick_idle = bool(lane_rows) and idle_lanes == len(lane_rows)
        executed = summary.get("executed") or []
        skipped = summary.get("skipped") or []
        planner_budget = summary.get("planner_budget")
        if not isinstance(planner_budget, Mapping):
            planner_budget = None
        row = {
            "tick_id": tick_id, "day": day, "started_at": started_text,
            "ended_at": ended_text, "status": str(summary.get("status") or ""),
            "active_loop_count": int(summary.get("active_loop_count") or 0),
            "probes_executed": int(summary.get("probes_executed") or 0),
            "executed_count": len(executed) if isinstance(executed, Sequence) else 0,
            "skipped_count": len(skipped) if isinstance(skipped, Sequence) else 0,
            "lane_count": len(lane_rows), "idle": int(tick_idle),
            "pool_spend_json": canonical_json(delta),
            "pool_cumulative_json": canonical_json(cumulative),
            # C2b: consumed from the summary rather than recomputed, because
            # only the driver saw the per-loop answers. Absent (an older
            # driver) stays NULL: no answer is not the same as no holds.
            "planner_budget_json": (
                canonical_json(planner_budget) if planner_budget is not None
                else None),
        }
        row["content_hash"] = content_hash(row)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT 1 FROM tick_ledger_ticks WHERE tick_id=?", (tick_id,),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO tick_ledger_ticks(tick_id,day,started_at,ended_at,"
                    "status,active_loop_count,probes_executed,executed_count,"
                    "skipped_count,lane_count,idle,pool_spend_json,"
                    "pool_cumulative_json,planner_budget_json,content_hash,"
                    "created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["tick_id"], row["day"], row["started_at"],
                        row["ended_at"], row["status"], row["active_loop_count"],
                        row["probes_executed"], row["executed_count"],
                        row["skipped_count"], row["lane_count"], row["idle"],
                        row["pool_spend_json"], row["pool_cumulative_json"],
                        row["planner_budget_json"],
                        row["content_hash"], _utc(self.clock()),
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO tick_ledger_lanes(tick_id,day,started_at,"
                    "driver_key,lane_operation,pool,status,status_word,idle,"
                    "pool_exhausted,counts_json,pool_spend_delta_micros,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    lane_rows,
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return {
            "schema_version": SCHEMA_VERSION, "tick_id": tick_id, "day": day,
            "lane_count": len(lane_rows), "idle": tick_idle,
            "status": "duplicate" if existing is not None else "recorded",
            "pool_spend_delta_micros": delta,
        }

    def _last_cumulative(self, day: str) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT pool_cumulative_json FROM tick_ledger_ticks "
            "WHERE day=? ORDER BY started_at DESC LIMIT 1", (day,),
        ).fetchone()
        if row is None:
            return {}
        try:
            return {k: int(v) for k, v in json.loads(row[0]).items()}
        except (TypeError, ValueError):
            return {}

    # -- reading ------------------------------------------------------------

    def ticks(self, window: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
        """Every tick in the window, with its lanes, oldest first."""

        since, until = _window(window, now=now)
        rows = self.connection.execute(
            "SELECT * FROM tick_ledger_ticks WHERE day BETWEEN ? AND ? "
            "ORDER BY started_at", (since, until),
        ).fetchall()
        lanes: dict[str, list[dict[str, Any]]] = {}
        for lane in self.connection.execute(
            "SELECT * FROM tick_ledger_lanes WHERE day BETWEEN ? AND ? "
            "ORDER BY started_at, driver_key", (since, until),
        ).fetchall():
            lanes.setdefault(lane["tick_id"], []).append({
                "driver_key": lane["driver_key"],
                "lane_operation": lane["lane_operation"],
                "pool": lane["pool"], "status": lane["status"],
                "status_word": lane["status_word"], "idle": bool(lane["idle"]),
                "pool_exhausted": bool(lane["pool_exhausted"]),
                "counts": json.loads(lane["counts_json"]),
                "pool_spend_delta_micros": lane["pool_spend_delta_micros"],
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "available": bool(rows),
            "window": {"since": since, "until": until},
            "ticks": [
                {
                    "tick_id": row["tick_id"], "day": row["day"],
                    "started_at": row["started_at"], "ended_at": row["ended_at"],
                    "status": row["status"], "idle": bool(row["idle"]),
                    "active_loop_count": row["active_loop_count"],
                    "probes_executed": row["probes_executed"],
                    "executed_count": row["executed_count"],
                    "skipped_count": row["skipped_count"],
                    "pool_spend_delta_micros": json.loads(row["pool_spend_json"]),
                    "lanes": lanes.get(row["tick_id"], []),
                }
                for row in rows
            ],
            "tick_count": len(rows),
            "days": len({row["day"] for row in rows}),
        }

    def idle_ratio(self, window: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
        """The share of ticks in which every lane reported nothing to do.

        The shape matches Q2's ``idle_tick_ratio`` so its ``available: false``
        can become a number without the reflection changing what it prints.
        """

        since, until = _window(window, now=now)
        row = self.connection.execute(
            "SELECT COUNT(*) AS ticks, COALESCE(SUM(idle),0) AS idle "
            "FROM tick_ledger_ticks WHERE day BETWEEN ? AND ? AND lane_count > 0",
            (since, until),
        ).fetchone()
        ticks = int(row["ticks"])
        window_wire = {"since": since, "until": until}
        if not ticks:
            return {
                "available": False,
                "reason": "这段窗口里没有记过 tick（账本是 C2 之后才开始写的）",
                "ticks": 0, "window": window_wire,
            }
        idle = int(row["idle"])
        by_lane = self.connection.execute(
            "SELECT driver_key, COUNT(*) AS seen, COALESCE(SUM(idle),0) AS idle "
            "FROM tick_ledger_lanes WHERE day BETWEEN ? AND ? GROUP BY driver_key "
            "ORDER BY driver_key", (since, until),
        ).fetchall()
        return {
            "available": True, "window": window_wire, "ticks": ticks,
            "idle_ticks": idle, "ratio": round(idle / ticks, 4),
            "idle_by_lane": {
                lane["driver_key"]: round(int(lane["idle"]) / int(lane["seen"]), 4)
                for lane in by_lane
            },
        }

    def lane_stalls(self, window: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
        """Per lane: how often it could not work, and its longest run of that.

        A stall is a lane that wanted to run and could not -- unavailable,
        unconfigured, failed.  A spent pool is counted separately: it is a
        decision the budget made, not a thing that broke.
        """

        since, until = _window(window, now=now)
        rows = self.connection.execute(
            "SELECT driver_key, lane_operation, pool, status_word, status, "
            "pool_exhausted, started_at FROM tick_ledger_lanes "
            "WHERE day BETWEEN ? AND ? ORDER BY driver_key, started_at",
            (since, until),
        ).fetchall()
        window_wire = {"since": since, "until": until}
        if not rows:
            return {
                "available": False,
                "reason": "这段窗口里没有记过 tick，无法判断 lane 是否卡住",
                "window": window_wire, "lanes": {},
            }
        lanes: dict[str, dict[str, Any]] = {}
        runs: dict[str, int] = {}
        for row in rows:
            key = row["driver_key"]
            entry = lanes.setdefault(key, {
                "lane_operation": row["lane_operation"], "pool": row["pool"],
                "ticks": 0, "stalls": 0, "pool_exhausted_ticks": 0,
                "longest_stall_run": 0, "last_status": "", "last_seen": "",
                "statuses": Counter(),
            })
            entry["ticks"] += 1
            entry["statuses"][row["status_word"]] += 1
            entry["last_status"] = row["status"]
            entry["last_seen"] = row["started_at"]
            entry["pool_exhausted_ticks"] += int(row["pool_exhausted"])
            if row["status_word"] in STALL_STATUS_WORDS:
                entry["stalls"] += 1
                runs[key] = runs.get(key, 0) + 1
                entry["longest_stall_run"] = max(
                    entry["longest_stall_run"], runs[key])
            else:
                runs[key] = 0
        return {
            "available": True, "window": window_wire,
            "lanes": {
                key: {
                    **{k: v for k, v in entry.items() if k != "statuses"},
                    "stall_ratio": round(entry["stalls"] / entry["ticks"], 4),
                    "statuses": dict(sorted(entry["statuses"].items())),
                }
                for key, entry in sorted(lanes.items())
            },
        }

    def spend_by_pool(self, window: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
        """What each pool spent over the window, a day at a time.

        This is the answer to Q2's "cost is attributable only to a work-order
        prefix": every micro here was attributed at admission time to the pool
        the caller declared, not inferred afterwards from an id.

        A day's figure is **the last tick's cumulative**, not the sum of that
        day's deltas.  The day ledger holds a reservation until the call
        settles, so a pool's committed total goes up when a call is admitted
        and back down when it settles for less than it reserved.  Summing only
        the positive deltas therefore counts every reservation and forgives
        every refund, which on this ledger's reserve-then-settle profile
        overstated a week by an order of magnitude.  The last cumulative of a
        day is what that day actually cost, and it is the same number the day
        ledger itself would report.
        """

        since, until = _window(window, now=now)
        rows = self.connection.execute(
            "SELECT day, pool_cumulative_json FROM tick_ledger_ticks "
            "WHERE day BETWEEN ? AND ? ORDER BY started_at", (since, until),
        ).fetchall()
        window_wire = {"since": since, "until": until}
        if not rows:
            return {
                "available": False,
                "reason": "这段窗口里没有记过 tick，池的花费无从累计",
                "window": window_wire, "pools": {},
            }
        totals: Counter = Counter()
        by_day: dict[str, Counter] = {}
        for row in rows:
            try:
                cumulative = json.loads(row["pool_cumulative_json"])
            except (TypeError, ValueError):
                continue
            # Rows arrive oldest first, so the last one for a day wins.
            day_totals = by_day.setdefault(row["day"], Counter())
            for pool, micros in (cumulative or {}).items():
                day_totals[pool] = max(int(micros), 0)
        for day_totals in by_day.values():
            totals.update(day_totals)
        lane_rows = self.connection.execute(
            "SELECT pool, COUNT(*) AS ticks, "
            "COALESCE(SUM(pool_spend_delta_micros),0) AS reported "
            "FROM tick_ledger_lanes WHERE day BETWEEN ? AND ? GROUP BY pool",
            (since, until),
        ).fetchall()
        return {
            "available": True, "window": window_wire,
            "days": len({row["day"] for row in rows}),
            "ticks": len(rows),
            "pools": dict(sorted(totals.items())),
            "total_micros": int(sum(totals.values())),
            "by_day": {
                day: dict(sorted(counter.items()))
                for day, counter in sorted(by_day.items())
            },
            "lane_reported_micros": {
                row["pool"]: int(row["reported"]) for row in lane_rows
            },
        }


def default_path(state_dir: str | Path) -> Path:
    """Where the ledger lives: beside the scheduler, in the state directory."""

    return Path(state_dir) / DEFAULT_FILENAME


def summarise(ledger_path: str | Path, window: Any = None) -> dict[str, Any]:
    """All four readers at once, for a report or a cockpit panel."""

    with TickLedger(ledger_path, read_only=True) as ledger:
        return {
            "ticks": ledger.ticks(window),
            "idle_ratio": ledger.idle_ratio(window),
            "lane_stalls": ledger.lane_stalls(window),
            "spend_by_pool": ledger.spend_by_pool(window),
        }


__all__ = [
    "DEFAULT_FILENAME",
    "IDLE_STATUS_WORDS",
    "RETENTION_DAYS",
    "SCHEMA_VERSION",
    "STALL_STATUS_WORDS",
    "TickLedger",
    "TickLedgerError",
    "bounded_counts",
    "mentions_pool_exhausted",
    "default_path",
    "status_word",
    "summarise",
]
