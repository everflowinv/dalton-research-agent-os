"""Who holds a cockpit attempt's lease, and whether they are still there.

2026-09-24: two legacy event-judgement attempts kept their leases for the
whole frozen 2h10m.  One had its model answer ($0.272 at the broker) and lost
the ``scheduler.complete`` to ``database is locked``; the backstop's own
completion hit the same lock and only printed it.  The other's process never
completed at all.  Either way the scheduler could only wait for ``expires_at``,
and every re-ask in between was told "this request is already running".

The scheduler proves *that* an attempt is leased, not that anybody is still
working it.  This registry keeps that second fact beside the scheduler file,
outside its write lock so recording it cannot fail for the reason it exists:

* ``record`` -- written right after a claim: which process holds the lease.
* ``release`` -- removed once the attempt's completion committed.
* ``abandon`` -- written when the holder gave up completing it after the
  bounded retry: the exact completion it could not commit (envelope,
  idempotency key and the lease token that authorises it), plus one line in
  an append-only failure log.  The holder raises and never touches the lease
  again, so the record is itself the proof that nobody else will.

A later ask of the same work that finds the attempt still leased reads these
records (:func:`holder_gone`).  An abandoned completion is replayed verbatim
-- it is the holder's own completion, committed late -- and a holder whose
process is proved dead is expired through ``Scheduler.expire_orphaned_lease``.
A holder that may still be alive is never touched: an unknown answer keeps
the lease, as it always did, until it expires.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "cockpit-lease-holder:0.1"
FAILURE_LOG_NAME = "release-failures.jsonl"
# The failure log is for people reading after an incident, not an authority:
# keep one rotated generation so it can never grow without bound.
FAILURE_LOG_MAX_BYTES = 1 << 20
# No lease outlives twice its policy's longest lifetime (a few hours); a
# record older than this names an attempt the scheduler settled long ago.
STALE_RECORD_SECONDS = 3 * 24 * 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pid_alive(pid: Any) -> bool:
    from .lane_child_launcher import pid_alive

    return pid_alive(pid)


class LeaseHolderRegistry:
    """Owner-only JSON records beside one scheduler database."""

    def __init__(self, scheduler_path: str | os.PathLike[str]) -> None:
        path = os.fspath(scheduler_path)
        self.directory: Path | None = (
            None if path == ":memory:" or path.startswith("file:")
            else Path(path + ".lease-holders"))

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    @staticmethod
    def _work_key(work_order_id: str) -> str:
        return hashlib.sha256(work_order_id.encode("utf-8")).hexdigest()[:32]

    def _path(self, work_order_id: str, lease_revision_ref: str) -> Path:
        assert self.directory is not None
        safe = "".join(ch for ch in lease_revision_ref if ch.isalnum() or ch in "-_")
        return self.directory / f"{self._work_key(work_order_id)}--{safe}.json"

    def _ensure_dir(self) -> Path:
        assert self.directory is not None
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.directory

    @staticmethod
    def _write(path: Path, value: Mapping[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        data = (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def record(self, *, work_order_id: str, attempt_number: int,
               lease_revision_ref: str, owner_ref: str) -> None:
        """Note that this process holds the lease; never raises."""

        if not self.enabled:
            return
        try:
            directory = self._ensure_dir()
            self._prune(directory)
            self._write(self._path(work_order_id, lease_revision_ref), {
                "schema_version": SCHEMA_VERSION,
                "status": "held",
                "work_order_id": work_order_id,
                "attempt_number": attempt_number,
                "lease_revision_ref": lease_revision_ref,
                "owner_ref": owner_ref,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "recorded_at": _now(),
            })
        except OSError as exc:
            print(f"cockpit-model: could not record the holder of {work_order_id} "
                  f"attempt {attempt_number}: {exc}", file=sys.stderr)

    def release(self, work_order_id: str, lease_revision_ref: str) -> None:
        """Forget the holder once its completion committed; never raises."""

        if not self.enabled:
            return
        try:
            self._path(work_order_id, lease_revision_ref).unlink(missing_ok=True)
        except OSError:
            pass

    def abandon(self, *, work_order_id: str, attempt_number: int,
                lease_revision_ref: str, owner_ref: str, lease_token: str,
                result_envelope: Mapping[str, Any], idempotency_key: str,
                retry_at: str | None, error: str,
                database_path: str | None, traceback: list[str]) -> bool:
        """Persist the completion this holder could not commit.

        Returns whether the record was written; a failure here is printed,
        because the caller is already on its way out with the original error.
        """

        if not self.enabled:
            return False
        record = {
            "schema_version": SCHEMA_VERSION,
            "status": "abandoned",
            "work_order_id": work_order_id,
            "attempt_number": attempt_number,
            "lease_revision_ref": lease_revision_ref,
            "owner_ref": owner_ref,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "abandoned_at": _now(),
            "error": error[:1000],
            "database_path": database_path,
            "traceback": list(traceback)[-8:],
            "pending_completion": {
                "lease_token": lease_token,
                "result_envelope": dict(result_envelope),
                "idempotency_key": idempotency_key,
                "retry_at": retry_at,
            },
        }
        try:
            directory = self._ensure_dir()
            self._write(self._path(work_order_id, lease_revision_ref), record)
            log_entry = {key: value for key, value in record.items()
                         if key != "pending_completion"}
            log_entry["result_status"] = result_envelope.get("status")
            log_entry["result_envelope_id"] = result_envelope.get("id")
            self._append_failure(directory, log_entry)
            return True
        except OSError as exc:
            print(f"cockpit-model: could not record the abandoned completion of "
                  f"{work_order_id} attempt {attempt_number}: {exc}", file=sys.stderr)
            return False

    @staticmethod
    def _append_failure(directory: Path, entry: Mapping[str, Any]) -> None:
        log = directory / FAILURE_LOG_NAME
        try:
            if log.stat().st_size > FAILURE_LOG_MAX_BYTES:
                os.replace(log, log.with_name(FAILURE_LOG_NAME + ".1"))
        except FileNotFoundError:
            pass
        line = (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            _write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def holders_for(self, work_order_id: str) -> list[dict[str, Any]]:
        if not self.enabled or not self.directory.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob(f"{self._work_key(work_order_id)}--*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
                    and value.get("work_order_id") == work_order_id):
                found.append(value)
        return found

    @staticmethod
    def _prune(directory: Path) -> None:
        cutoff = time.time() - STALE_RECORD_SECONDS
        for path in directory.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


def holder_gone(record: Mapping[str, Any]) -> str | None:
    """Why the holder of ``record`` can no longer complete it, or ``None``.

    ``abandoned`` -- the holder itself recorded that it gave up.
    ``process_exited`` -- same host, and its pid names no live process (a
    zombie counts as exited).  Anything unprovable -- another host, a live pid
    that may or may not be the holder, this very process -- is ``None``: the
    lease stays held until it expires, as before.
    """

    if record.get("status") == "abandoned":
        return "abandoned"
    if record.get("status") != "held":
        return None
    if record.get("host") != socket.gethostname():
        return None
    pid = record.get("pid")
    if pid == os.getpid():
        return None
    if not _pid_alive(pid):
        return "process_exited"
    return None


__all__ = ["LeaseHolderRegistry", "holder_gone"]
