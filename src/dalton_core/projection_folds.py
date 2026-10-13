"""Incremental reads of append-only authority tables for long-lived readers.

2026-09-29: the controller's dashboard projection re-read every scheduler
event, lease, result envelope and formal result, every model invocation,
usage and cost entry, every artifact and every connector attempt on each
round -- about a gigabyte from the external volume per round on the legacy
Core, in bursts of 0.5-0.8 GB per ten seconds -- and held one read
transaction on ``scheduler.sqlite`` for the whole round (25 minutes on the
live controller), so the WAL could not checkpoint past it.

Every table read here is append-only: the schemas carry triggers that refuse
an UPDATE or a DELETE.  So a row read once never changes, and a table's new
rows are exactly those with a larger ``rowid`` (SQLite never reuses a rowid
below the maximum while nothing is deleted, and inserts commit in rowid order
because there is one writer at a time).  An :class:`AppendOnlyFold` keeps a
reduction of a table in process memory and, each round, reads only the rows
past its high-water ``rowid`` -- through the primary key, a B-tree seek.

What a fold may not do is outlive its database.  A restore, or a test that
reuses a path, puts different rows under the same name, so every read first
checks the file identity and then that the row at the high-water mark is
still the row it remembers; either mismatch rebuilds the fold from scratch
rather than projecting a mixture of two databases.  A reduction that raises
half-way is dropped the same way, so a failed round never leaves a
half-folded state behind.
"""

from __future__ import annotations

import os
import json
import sys
import time
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "AppendOnlyFold",
    "FoldRead",
    "clear_folds",
    "source_identity",
]


def _phase(event: str, name: str, rows: int, fresh: bool, started: float) -> None:
    """Bounded operational timing; source rows and exception text never enter it.

    Service stderr is already owner managed. Emit directly so a cold pending
    projection is diagnosable even without application logging configuration.
    Diagnostic output must never alter a projection's failure semantics.
    """
    try:
        sys.stderr.write(json.dumps({"event":event,"name":name,"rows":rows,
            "fresh":fresh,"elapsed_seconds":round(time.monotonic()-started,6)},
            separators=(",",":"))+"\n")
        sys.stderr.flush()
    except (OSError,ValueError):
        pass


def source_identity(path: str | Path) -> tuple[str, int, int]:
    """The database a path names *now*: resolved path, device and inode."""

    resolved = Path(path).resolve()
    try:
        info = os.stat(resolved)
    except OSError:
        return (str(resolved), -1, -1)
    return (str(resolved), int(info.st_dev), int(info.st_ino))


@dataclass
class _Entry:
    identity: tuple[str, int, int]
    state: Any
    last_rowid: int = 0
    last_key: Any = None
    count: int = 0
    max_created: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class FoldRead:
    """One round's view of a fold: its state and the table's fingerprint.

    ``count`` and ``max_created`` are what ``COUNT(*)`` and
    ``MAX(created_at)`` over the table return, kept as the rows go by, so a
    watermark needs no scan of its own.  ``fresh`` says the fold was (re)built
    from the first row this round; ``added`` is how many rows it read.
    """

    state: Any
    count: int
    max_created: str | None
    fresh: bool
    added: int


_REGISTRY: dict[tuple[str, str], _Entry] = {}
_REGISTRY_LOCK = threading.Lock()


def clear_folds() -> None:
    """Forget every fold (tests; a process that wants its memory back)."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()


class AppendOnlyFold:
    """A reduction of one append-only table, advanced by ``rowid``.

    ``columns`` is SQL written by this package, never caller input.  ``key``
    names a column (in ``columns``) that identifies a row, used to recognise
    the high-water row on the next round.  ``reduce(state, row)`` folds one
    row into ``state`` in place; ``snapshot(state)`` returns what callers get
    (a copy by default, so a later round cannot change a list a caller is
    still iterating).
    """

    def __init__(
        self,
        name: str,
        table: str,
        columns: str,
        *,
        key: str,
        init: Callable[[], Any],
        reduce: Callable[[Any, sqlite3.Row], None],
        snapshot: Callable[[Any], Any] | None = None,
        created: str | None = "created_at",
        cold_cover_index: str | None = None,
        cold_cover_sql: str | None = None,
    ) -> None:
        self.name = name
        self.table = table
        self.columns = columns
        self.key = key
        self.init = init
        self.reduce = reduce
        self.snapshot = snapshot or _copy
        self.created = created
        self.cold_cover_index = cold_cover_index
        self.cold_cover_sql = cold_cover_sql
        self._select = (
            f"SELECT rowid AS fold_rowid, {columns} FROM {table}"
            " WHERE rowid > ? ORDER BY rowid"
        )
        self._probe = f"SELECT {key} FROM {table} WHERE rowid = ?"

    def _cold_select(self, connection: sqlite3.Connection) -> str:
        """Cover the first historical scan only; append reads keep rowid seeks.

        The optional index is package configuration. Older authorities without
        it use the existing query. A present partial/mismatched index is not a
        substitute for the complete historical source.
        """
        name = self.cold_cover_index
        if name is None:
            return self._select
        if not name.replace("_", "").isalnum():
            raise ValueError("cold cover index is not a package SQL identifier")
        row = connection.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        if row is None:
            return self._select
        listed = next((r for r in connection.execute(f"PRAGMA index_list({self.table})")
                       if r[1] == name), None)
        columns = tuple(r[2] for r in connection.execute(f"PRAGMA index_info({name})"))
        expected = tuple(value.strip() for value in self.columns.split(","))
        # index_info names expressions as None, and a comma inside json_extract
        # is not a column separator. Expression folds bind the entire exact
        # package-declared CREATE statement instead of trusting a None marker.
        if self.cold_cover_sql is None:
            complete_columns = columns == expected
        else:
            complete_columns = (isinstance(row[1], str)
                and " ".join(row[1].split())
                == " ".join(self.cold_cover_sql.replace(" IF NOT EXISTS", "").split()))
        if (row[0] != self.table or listed is None or listed[2] or listed[4]
                or not complete_columns):
            raise ValueError("cold cover index does not cover the complete fold source")
        return (f"SELECT rowid AS fold_rowid, {self.columns} FROM {self.table}"
                f" INDEXED BY {name} WHERE rowid > ? ORDER BY rowid")

    def _entry(self, identity: tuple[str, int, int]) -> _Entry:
        with _REGISTRY_LOCK:
            entry = _REGISTRY.get((identity[0], self.name))
            if entry is None or entry.identity != identity:
                entry = _Entry(identity=identity, state=self.init())
                _REGISTRY[(identity[0], self.name)] = entry
            return entry

    def _drop(self, entry: _Entry) -> None:
        with _REGISTRY_LOCK:
            key = (entry.identity[0], self.name)
            if _REGISTRY.get(key) is entry:
                del _REGISTRY[key]

    def read(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, int, int],
        *,
        finish: Callable[[Any], None] | None = None,
    ) -> FoldRead:
        """Advance the fold to what ``connection`` sees and return it.

        ``finish(state)`` runs under the fold's lock after the new rows are
        folded, in the same read transaction: it may drop entries the caller
        knows will never be needed again, or complete entries from other
        tables.  If it raises, the fold is dropped like any other failure.
        """

        entry = self._entry(identity)
        with entry.lock:
            started = time.monotonic()
            fresh = entry.last_rowid == 0
            _phase("projection_fold_started",self.name,0,fresh,started)
            if not fresh:
                try:
                    row = connection.execute(self._probe, (entry.last_rowid,)).fetchone()
                except BaseException:
                    _phase("projection_fold_failed",self.name,0,fresh,started)
                    raise
                if row is None or row[0] != entry.last_key:
                    # Not the database this fold was built from.
                    self._drop(entry)
                    entry = self._entry(identity)
                    fresh = True
            added = 0
            try:
                select = self._cold_select(connection) if fresh else self._select
                rows = connection.execute(select, (entry.last_rowid,)).fetchall()
                for row in rows:
                    self.reduce(entry.state, row)
                    entry.last_rowid = int(row["fold_rowid"])
                    entry.last_key = row[self.key]
                    entry.count += 1
                    if self.created is not None:
                        created = row[self.created]
                        if created is not None and (
                            entry.max_created is None or created > entry.max_created
                        ):
                            entry.max_created = created
                    added += 1
                if finish is not None:
                    finish(entry.state)
                view = self.snapshot(entry.state)
            except BaseException:
                _phase("projection_fold_failed",self.name,added,fresh,started)
                self._drop(entry)
                raise
            _phase("projection_fold_completed",self.name,added,fresh,started)
            return FoldRead(
                state=view, count=entry.count, max_created=entry.max_created,
                fresh=fresh, added=added,
            )


def _copy(state: Any) -> Any:
    if isinstance(state, dict):
        return dict(state)
    if isinstance(state, list):
        return list(state)
    return state


def retire_keys(state: dict[Any, Any], gone: Iterable[Any] | Any) -> None:
    """Drop every key of ``state`` that ``gone`` contains (a set or mapping)."""

    doomed = [key for key in state if key in gone]
    for key in doomed:
        del state[key]
