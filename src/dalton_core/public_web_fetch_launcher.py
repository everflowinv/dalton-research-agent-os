"""Writer-owned launcher for the out-of-process public-web fetch child (P9d-4b).

Mirrors ``alphaengine_acquisition_launcher``: approved human governance
record on disk, a single slot, owner-only tickets under
``<state>/fetches/<ticket>/`` (``ticket.json``, ``summary.json``,
``manifest.json``, ``run.log``).  The coordinator drives it through the same
``start_bounded_probe(document_ref=..., caller_ref=...)`` / ``status`` /
``read_completed_manifest`` surface the AlphaEngine launcher offers, with a
``public-web-url:sha256:<hash>`` ref as the document.

A networked launch runs the child with a real ``PublicHttpTransport``
(credential-free public HTTPS only); the rehearsal mode serves one local
file as every page.  Which URLs may be fetched is never decided here: the
child rebuilds the URL authority from the exact search artifact that cited
it and refuses anything else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .public_web_core_fetch import (
    DEFAULT_USER_AGENT,
    WebFetchConnectorGovernance,
    validate_public_web_fetch_manifest,
)
from .child_tickets import adopt_finished_child
from .store import canonical_json


TICKET_SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "public-web-fetch"
DEFAULT_CATALOG_NAME = "catalog-web-fetch.sqlite"
LIVE_MODE_ARGS = ("--allow-network",)
_URL_REF_RE = re.compile(r"public-web-url:sha256:[0-9a-f]{64}\Z")
_TICKET_RE = re.compile(r"public-web-fetch:[0-9a-f]{24}\Z")
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_AUTOMATION_RE = re.compile(r"automation:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


class FetchLaunchError(RuntimeError):
    """Launcher configuration or filesystem failure."""


class FetchLaunchRejected(ValueError):
    """The request was refused before any process was started."""


class FetchLaunchConflict(RuntimeError):
    """Another fetch already holds the single slot."""


class FetchTicketNotFound(LookupError):
    pass


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class PublicWebFetchLauncher:
    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        user_agent: str = DEFAULT_USER_AGENT,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
        spool_dir: str | Path | None = None,
        catalog_db: str | Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.spool_dir = None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        self.catalog_db = (
            Path(catalog_db).expanduser().resolve() if catalog_db is not None
            else self.state_dir / DEFAULT_CATALOG_NAME
        )
        self.mode_args = tuple(str(item) for item in mode_args)
        self.user_agent = user_agent
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        if not self.state_dir.is_dir():
            raise FetchLaunchError("fetch state directory is missing")
        self.tickets_dir = _secure_dir(self.state_dir / "fetches")

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def load_governance(self) -> WebFetchConnectorGovernance:
        try:
            governance = WebFetchConnectorGovernance.load(self.governance_path)
        except FileNotFoundError as exc:
            raise FetchLaunchRejected(
                "public-web fetch governance record is missing; owner approval is required"
            ) from exc
        except Exception as exc:
            raise FetchLaunchRejected(f"public-web fetch governance record is invalid: {exc}") from exc
        if not governance.approved:
            raise FetchLaunchRejected(
                "public-web fetch governance record is not approved; owner approval is required"
            )
        if _HUMAN_RE.fullmatch(governance.approved_by) is None:
            raise FetchLaunchRejected("public-web fetch governance record must be approved by a human principal")
        return governance

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def _command(self, *, url_ref: str, requested_by: str, ticket_dir: Path) -> list[str]:
        command = [
            self.python_executable, "-m", "dalton_core.public_web_fetch_cli",
            "--url-ref", url_ref,
            "--requested-by", requested_by,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--summary-dir", str(ticket_dir),
            "--catalog-db", str(self.catalog_db),
            "--user-agent", self.user_agent,
            "--quiet",
        ]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        command += list(self.mode_args)
        return command

    def start(self, *, document_ref: str, actor_ref: str) -> dict[str, Any]:
        """Fetch one discovered URL on a human's request."""

        if not isinstance(actor_ref, str) or _HUMAN_RE.fullmatch(actor_ref) is None:
            raise FetchLaunchRejected("fetch must be requested by a human principal")
        return self._launch(document_ref=document_ref, requested_by=actor_ref)

    def start_bounded_probe(self, *, document_ref: str, caller_ref: str, **_ignored: Any) -> dict[str, Any]:
        """Fetch one discovered URL for the mission automation principal."""

        if not isinstance(caller_ref, str) or _AUTOMATION_RE.fullmatch(caller_ref) is None:
            raise FetchLaunchRejected("bounded fetch must name its automation principal")
        return self._launch(document_ref=document_ref, requested_by=caller_ref)

    def _launch(self, *, document_ref: str, requested_by: str) -> dict[str, Any]:
        if not isinstance(document_ref, str) or _URL_REF_RE.fullmatch(document_ref) is None:
            raise FetchLaunchRejected("document_ref must be public-web-url:sha256:<hash>")
        governance = self.load_governance()
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise FetchLaunchConflict(f"fetch {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(
                canonical_json({
                    "document_ref": document_ref, "requested_by": requested_by,
                    "started_at": started_at, "governance_hash": governance.content_hash,
                }).encode("utf-8")
            ).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._command(url_ref=document_ref, requested_by=requested_by, ticket_dir=ticket_dir)
            log_path = ticket_dir / "run.log"
            log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.state_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            finally:
                os.close(log_fd)
            record = {
                "schema_version": TICKET_SCHEMA_VERSION,
                "id": ticket_id,
                "document_ref": document_ref,
                "actor_ref": requested_by,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "public-https" if self.networked else "rehearsal",
                "catalog_db": str(self.catalog_db),
                "started_at": started_at,
                "pid": process.pid,
                "status": "running",
                "exit_code": None,
                "completed_at": None,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._current = (ticket_id, process)
            return dict(record)

    def status(self, ticket_ref: str) -> dict[str, Any]:
        if not isinstance(ticket_ref, str) or _TICKET_RE.fullmatch(ticket_ref) is None:
            raise FetchLaunchRejected(f"ticket_ref must be {TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FetchTicketNotFound(ticket_ref) from exc
        with self._lock:
            process = None
            if self._current is not None and self._current[0] == ticket_ref:
                process = self._current[1]
            if record["status"] == "running":
                if process is not None:
                    code = process.poll()
                    if code is not None:
                        record["exit_code"] = code
                        record["completed_at"] = _wire_time(self.clock())
                        record["status"] = "succeeded" if code == 0 else "failed"
                        _write_owner_only(path, record)
                elif not self._pid_alive(record.get("pid")):
                    # The writer restarted (or the child died) before this
                    # ticket was settled.  If the child left its own final
                    # summary, take that; the settle path re-verifies
                    # authority anyway.  Otherwise it is orphaned.
                    now = _wire_time(self.clock())
                    if not adopt_finished_child(record, path.with_name("summary.json"), now=now):
                        record["status"] = "orphaned"
                        record["completed_at"] = now
                    _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        summary = None
        if record["status"] != "running" and summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {**record, "summary": summary}

    def read_completed_manifest(self, ticket_ref: str, document_ref: str) -> dict[str, Any]:
        """Read a settled fetch without polling, spawning or mutating it."""

        if not isinstance(ticket_ref, str) or _TICKET_RE.fullmatch(ticket_ref) is None:
            raise FetchLaunchRejected("invalid fetch ticket reference")
        directory = self._ticket_path(ticket_ref).parent
        if directory.is_symlink() or directory.parent.is_symlink():
            raise FetchLaunchRejected("fetch directory cannot be a symlink")
        records = []
        for name in ("ticket.json", "summary.json", "manifest.json"):
            path = directory / name
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                            or info.st_uid != os.getuid() or info.st_size > 2000000):
                        raise FetchLaunchRejected("fetch file must be bounded and owner-only")
                    value = json.loads(handle.read(2000001).decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                records.append(value)
            except (OSError, ValueError) as exc:
                raise FetchLaunchRejected("completed fetch files are unavailable") from exc
        ticket, summary, manifest = records
        if (ticket.get("id") != ticket_ref or ticket.get("status") != "succeeded"
                or ticket.get("document_ref") != document_ref
                or summary.get("url_ref") != document_ref
                or manifest.get("url_ref") != document_ref
                or summary.get("manifest_ref") != manifest.get("id")
                or summary.get("manifest_hash") != manifest.get("content_hash")
                or summary.get("status") != "succeeded"):
            raise FetchLaunchRejected("ticket, summary and manifest disagree")
        return validate_public_web_fetch_manifest(manifest)

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        with self._lock:
            current = self._current
        if current is None:
            return None
        return current[1].wait(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            current, self._current = self._current, None
        if current is not None and current[1].poll() is None:
            current[1].terminate()
            try:
                current[1].wait(timeout=5)
            except subprocess.TimeoutExpired:
                current[1].kill()


__all__ = [
    "FetchLaunchConflict",
    "FetchLaunchError",
    "FetchLaunchRejected",
    "FetchTicketNotFound",
    "LIVE_MODE_ARGS",
    "PublicWebFetchLauncher",
    "TICKET_PREFIX",
]
