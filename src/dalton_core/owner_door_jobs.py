"""Owner doors as governed jobs: the store thread only takes the request.

2026-10-07 (legacy, 01:01-01:17 UTC).  Right after a deploy restarted the
writer -- every page of a 5.5 GB Core cold -- the owner ran
``document_recovery_cli authorize-unproved``.  The door assembled the whole
document-research runtime (inventory of every acquired document, a receipt
re-verification per fetched page, the stage chain) *on the single store
thread*, sat in SQLite reads for more than fifteen minutes, no lane tick
completed, every lane op timed out and the writer had to be killed.

A door is now two halves:

* **request** -- on the store thread, cheap: validate, refuse while the writer
  is still cold, append one ``requested`` receipt to an append-only ledger,
  hand the heavy half to this module's own worker thread and answer at once
  with the job's ref.  A second request for the same door and admission while
  one is in flight answers with that job instead of starting another.
* **work** -- on the ``dalton-owner-door`` thread, with its *own* SQLite
  handles (exactly what a lane's child process does), never touching the
  store thread's connection.  Its ``running`` and terminal receipts are
  appended by that thread too.

The caller (the CLI, or anything else that holds a door) polls the job by
calling the same operation with ``job_ref``; that lookup is one indexed read.
Lanes never queue behind an owner door again, because nothing an owner door
does on the store thread takes longer than a primary-key read and one insert.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

#: How long after the writer starts heavy owner doors are refused (deferred)
#: rather than queued.  A just-restarted writer reads every page of the Core
#: from disk; the first lane ticks need that time more than an owner door
#: does, and the door is just as cheap to ask for ten minutes later.
COLD_START_SECONDS = 600.0
JOB_PREFIX = "owner-door-job:"
STATES = ("requested", "running", "succeeded", "failed")
TERMINAL_STATES = frozenset({"succeeded", "failed"})
MAX_RESULT_CHARS = 20_000
BUSY_TIMEOUT_MS = 15_000

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS owner_door_job_events ("
    "event_id TEXT PRIMARY KEY, job_ref TEXT NOT NULL, "
    "sequence INTEGER NOT NULL CHECK (sequence > 0), "
    "state TEXT NOT NULL CHECK (state IN "
    "('requested','running','succeeded','failed')), "
    "operation TEXT NOT NULL, admission_ref TEXT NOT NULL, actor_ref TEXT NOT NULL, "
    "writer_boot_ref TEXT NOT NULL, record_json TEXT NOT NULL, "
    "content_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
    "UNIQUE (job_ref, sequence))",
    "CREATE INDEX IF NOT EXISTS owner_door_job_events_door "
    "ON owner_door_job_events(operation, admission_ref, created_at)",
    # Append-only: a receipt is evidence of what the owner asked for and what
    # the door did; it is never rewritten.
    "CREATE TRIGGER IF NOT EXISTS owner_door_job_events_no_update "
    "BEFORE UPDATE ON owner_door_job_events BEGIN "
    "SELECT RAISE(ABORT, 'owner door job receipts are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS owner_door_job_events_no_delete "
    "BEFORE DELETE ON owner_door_job_events BEGIN "
    "SELECT RAISE(ABORT, 'owner door job receipts are append-only'); END",
)


class OwnerDoorJobError(RuntimeError):
    """An owner door job request that cannot be accepted."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ensure_schema(connection: Any) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def _append(connection: Any, *, job_ref: str, state: str, operation: str,
            admission_ref: str, actor_ref: str, boot_ref: str,
            record: Mapping[str, Any]) -> dict[str, Any]:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM owner_door_job_events WHERE job_ref=?",
        (job_ref,)).fetchone()
    sequence = int(row[0]) + 1
    created_at = _now()
    body = {"job_ref": job_ref, "sequence": sequence, "state": state,
            "operation": operation, "admission_ref": admission_ref,
            "actor_ref": actor_ref, "writer_boot_ref": boot_ref,
            "record": dict(record), "created_at": created_at}
    record_json = _canonical(body["record"])
    if len(record_json) > MAX_RESULT_CHARS:
        body["record"] = {"status": record.get("status"), "truncated": True,
                          "excerpt": record_json[:MAX_RESULT_CHARS]}
        record_json = _canonical(body["record"])
    digest = sha256(_canonical(body).encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO owner_door_job_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"{job_ref}:{sequence}", job_ref, sequence, state, operation,
         admission_ref, actor_ref, boot_ref, record_json, digest, created_at))
    connection.commit()
    return body


def _events(connection: Any, job_ref: str) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT sequence,state,operation,admission_ref,actor_ref,writer_boot_ref,"
            "record_json,created_at FROM owner_door_job_events WHERE job_ref=? "
            "ORDER BY sequence", (job_ref,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    out = []
    for row in rows:
        sequence, state, operation, admission_ref, actor_ref, boot, record_json, at = tuple(row)
        try:
            record = json.loads(record_json)
        except (TypeError, ValueError):
            record = {"unreadable": True}
        out.append({"sequence": sequence, "state": state, "operation": operation,
                    "admission_ref": admission_ref, "actor_ref": actor_ref,
                    "writer_boot_ref": boot, "record": record, "created_at": at})
    return out


class OwnerDoorJobs:
    """One worker thread for heavy owner doors, and their receipt ledger."""

    def __init__(self, core_path: str | Path | None, *,
                 cold_start_seconds: float = COLD_START_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.core_path = None if core_path is None else Path(core_path)
        self.cold_start_seconds = float(cold_start_seconds)
        self._monotonic = monotonic
        self._started: float | None = None
        self.boot_ref = "writer-boot:" + uuid.uuid4().hex[:24]
        self._queue: "queue.Queue[tuple[str, Mapping[str, Any], Callable[[], Mapping[str, Any]]] | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._in_flight: dict[tuple[str, str], str] = {}

    # -- lifecycle ---------------------------------------------------------

    def mark_started(self) -> None:
        """The writer has just started: the cold window begins now."""

        self._started = self._monotonic()

    def cold_seconds_remaining(self) -> float:
        if self._started is None or self.cold_start_seconds <= 0:
            return 0.0
        return max(0.0, self.cold_start_seconds - (self._monotonic() - self._started))

    def close(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put(None)
            # Daemon: a door that is still reading does not hold the writer
            # open on shutdown.  Its job is reported ``interrupted`` by the
            # next writer, and asking again starts it afresh.
            thread.join(timeout=1.0)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="dalton-owner-door", daemon=True)
                self._thread.start()

    # -- the store-thread half --------------------------------------------

    def deferred(self, operation: str, admission_ref: str) -> dict[str, Any] | None:
        """The cold-start answer, or ``None`` when the door may be requested."""

        remaining = self.cold_seconds_remaining()
        if remaining <= 0:
            return None
        wait = int(remaining) + 1
        return {
            "status": "deferred", "reason": "writer_cold_start",
            "operation": operation, "admission_ref": admission_ref,
            "retry_after_seconds": wait, "model_calls": 0,
            "explanation": (
                f"系统刚刚重启，启动后的前 {int(self.cold_start_seconds // 60)} 分钟内"
                f"暂不受理这类需要大量读取的恢复操作，以免拖慢正在进行的研究。"
                f"这次没有排队、没有写入、没有产生费用；请在约 {wait} 秒后再试一次。"),
        }

    def request(self, connection: Any, *, operation: str, admission_ref: str,
                actor_ref: str, params: Mapping[str, Any],
                work: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        """Append the request receipt and hand ``work`` to the worker thread.

        ``work`` must not touch the store thread's connection: it runs on the
        owner-door thread with whatever handles it opens itself.
        """

        key = (operation, admission_ref)
        with self._lock:
            running = self._in_flight.get(key)
        if running is not None:
            view = self.view(connection, running)
            if view["state"] not in TERMINAL_STATES:
                return {**view, "status": "in_progress",
                        "note": "这项研究的同一个操作已经在处理，没有重复提交。"}
        requested_at = _now()
        job_ref = JOB_PREFIX + sha256(_canonical({
            "operation": operation, "admission_ref": admission_ref,
            "actor_ref": actor_ref, "requested_at": requested_at,
            "boot": self.boot_ref}).encode("utf-8")).hexdigest()[:32]
        record = {"status": "requested",
                  "params": {key: value for key, value in params.items()
                             if key in {"admission_ref", "max_cost_usd", "actor_ref"}}}
        _append(connection, job_ref=job_ref, state="requested", operation=operation,
                admission_ref=admission_ref, actor_ref=actor_ref,
                boot_ref=self.boot_ref, record=record)
        meta = {"job_ref": job_ref, "operation": operation,
                "admission_ref": admission_ref, "actor_ref": actor_ref}
        with self._lock:
            self._in_flight[key] = job_ref
        self._ensure_thread()
        self._queue.put((job_ref, meta, work))
        return {"status": "requested", "job_ref": job_ref, "state": "requested",
                "operation": operation, "admission_ref": admission_ref,
                "requested_at": requested_at,
                "note": ("请求已记录，正在后台处理，不影响正在进行的研究。"
                         "命令行用同一条命令加 job_ref 查询结果（会自动等待）。")}

    def view(self, connection: Any, job_ref: str) -> dict[str, Any]:
        """The job's current state, from its receipts.  One indexed read."""

        events = _events(connection, job_ref)
        if not events:
            return {"status": "unknown_job", "job_ref": job_ref, "state": "unknown"}
        first, last = events[0], events[-1]
        state = last["state"]
        view: dict[str, Any] = {
            "job_ref": job_ref, "state": state, "operation": first["operation"],
            "admission_ref": first["admission_ref"], "actor_ref": first["actor_ref"],
            "requested_at": first["created_at"], "updated_at": last["created_at"],
        }
        if state in TERMINAL_STATES:
            view["status"] = "finished"
            view["result"] = last["record"].get("result")
            if state == "failed":
                view["error"] = last["record"].get("error")
        elif last["writer_boot_ref"] != self.boot_ref:
            # The writer that took it stopped before it finished.  Nothing of
            # the door's own idempotent write is lost; asking again redoes it.
            view["state"] = "interrupted"
            view["status"] = "interrupted"
            view["note"] = "处理这个请求时系统重启了，操作没有完成；可以直接再试一次，不会重复执行。"
        else:
            view["status"] = "in_progress"
        return view

    # -- the worker half ---------------------------------------------------

    def _connect(self) -> Any:
        if self.core_path is None:
            raise OwnerDoorJobError("owner door jobs have no Core to record into")
        connection = sqlite3.connect(str(self.core_path), timeout=BUSY_TIMEOUT_MS / 1000)
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    def _record(self, meta: Mapping[str, Any], state: str,
                record: Mapping[str, Any]) -> None:
        try:
            connection = self._connect()
        except Exception:  # noqa: BLE001 - nothing to record into
            return
        try:
            _append(connection, job_ref=meta["job_ref"], state=state,
                    operation=meta["operation"], admission_ref=meta["admission_ref"],
                    actor_ref=meta["actor_ref"], boot_ref=self.boot_ref, record=record)
        except Exception:  # noqa: BLE001 - the door's own rows are the evidence
            pass
        finally:
            connection.close()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_ref, meta, work = item
            self._record(meta, "running", {"status": "running"})
            started = time.monotonic()
            try:
                result = dict(work())
                state, record = "succeeded", {
                    "status": "succeeded", "result": result,
                    "seconds": round(time.monotonic() - started, 3)}
            except Exception as exc:  # noqa: BLE001 - recorded, never raised
                state, record = "failed", {
                    "status": "failed",
                    "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
                    "seconds": round(time.monotonic() - started, 3)}
            self._record(meta, state, record)
            with self._lock:
                key = (meta["operation"], meta["admission_ref"])
                if self._in_flight.get(key) == job_ref:
                    del self._in_flight[key]

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Test helper: whether every queued job has finished in time."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._in_flight and self._queue.empty():
                    return True
            time.sleep(0.01)
        return False


__all__ = [
    "COLD_START_SECONDS", "JOB_PREFIX", "OwnerDoorJobError", "OwnerDoorJobs",
    "TERMINAL_STATES", "ensure_schema",
]
