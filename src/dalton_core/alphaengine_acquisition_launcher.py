"""Writer-owned launcher for out-of-process AlphaEngine acquisitions.

The writer service cannot run ``AlphaEngineCoreAcquisition`` on one of its
threads: ``ConnectorTransportExecutor`` bounds each provider call with a
``SIGALRM`` watchdog that only the process main thread may own, and the
writer's single store thread times out after 30 seconds.  So a human
governance operation *launches* ``dalton_core.alphaengine_acquisition_cli`` as
a child process against the same state directory, records a ticket, and
returns at once.  A second operation reads the ticket.

What this launcher enforces before anything is spawned:

* the committed governance record on disk is ``approved`` -- a ``proposed``
  record, a hash mismatch or a non-human approver refuses the launch;
* only one acquisition runs at a time;
* the document ref has the closed ``alphaengine-doc:<id>`` shape.

The child inherits nothing secret: the loopback MCP endpoint is an argument,
credentials stay inside the host-owned MCP process, and the Core path is the
writer's own state directory.  Tickets and child output are owner-only files
under ``<state>/acquisitions/<ticket>/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .alphaengine_core_acquisition import StaticConnectorGovernance
from .store import canonical_json


TICKET_SCHEMA_VERSION = "0.1"
_DOCUMENT_REF_RE = re.compile(r"alphaengine-doc:[A-Za-z0-9._-]{1,64}\Z")
_TICKET_RE = re.compile(r"alphaengine-acquisition:[0-9a-f]{24}\Z")
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
LIVE_MODE_ARGS = ("--allow-network",)


class AcquisitionLaunchError(RuntimeError):
    """Launcher configuration or filesystem failure."""


class AcquisitionLaunchRejected(ValueError):
    """The request was refused before any process was started."""


class AcquisitionLaunchConflict(RuntimeError):
    """Another acquisition already holds the single slot."""


class AcquisitionTicketNotFound(LookupError):
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


class AlphaEngineAcquisitionLauncher:
    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        mcp_endpoint: str | None = None,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
        spool_dir: str | Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.governance_path = Path(governance_path).expanduser().resolve()
        # RawSpool data directory for the raw page objects.  The writer passes
        # its own transcript spool so ``stage_transcript_candidate`` can read
        # the page bytes back; ``None`` keeps the CLI default.
        self.spool_dir = (
            None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        )
        self.mode_args = tuple(str(item) for item in mode_args)
        self.mcp_endpoint = mcp_endpoint
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        if not self.state_dir.is_dir():
            raise AcquisitionLaunchError("acquisition state directory is missing")
        self.tickets_dir = _secure_dir(self.state_dir / "acquisitions")

    # ------------------------------------------------------------------
    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def load_governance(self) -> StaticConnectorGovernance:
        try:
            governance = StaticConnectorGovernance.load(self.governance_path)
        except FileNotFoundError as exc:
            raise AcquisitionLaunchRejected(
                "connector governance record is missing; owner approval is required"
            ) from exc
        except Exception as exc:  # hash / shape failures are rejections, not crashes
            raise AcquisitionLaunchRejected(
                f"connector governance record is invalid: {exc}"
            ) from exc
        if not governance.approved:
            raise AcquisitionLaunchRejected(
                "connector governance record is not approved; owner approval is required"
            )
        if _HUMAN_RE.fullmatch(governance.approved_by) is None:
            raise AcquisitionLaunchRejected(
                "connector governance record must be approved by a human principal"
            )
        return governance

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def _command(
        self,
        *,
        document_ref: str,
        ticket_dir: Path,
        max_pages: int,
        expected_content_sha256: str | None,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", "dalton_core.alphaengine_acquisition_cli",
            "--document-ref", document_ref,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--summary-dir", str(ticket_dir),
            "--max-pages", str(max_pages),
            "--quiet",
        ]
        if expected_content_sha256 is not None:
            command += ["--expected-content-sha256", expected_content_sha256]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        if self.networked and self.mcp_endpoint is not None:
            command += ["--mcp-endpoint", self.mcp_endpoint]
        command += list(self.mode_args)
        return command

    # ------------------------------------------------------------------
    def start(
        self,
        *,
        document_ref: str,
        actor_ref: str,
        expected_content_sha256: str | None = None,
        max_pages: int = 20,
    ) -> dict[str, Any]:
        if not isinstance(document_ref, str) or _DOCUMENT_REF_RE.fullmatch(document_ref) is None:
            raise AcquisitionLaunchRejected("document_ref must be alphaengine-doc:<id>")
        if not isinstance(actor_ref, str) or _HUMAN_RE.fullmatch(actor_ref) is None:
            raise AcquisitionLaunchRejected("acquisition must be requested by a human principal")
        return self._launch(
            document_ref=document_ref, actor_ref=actor_ref,
            expected_content_sha256=expected_content_sha256, max_pages=max_pages,
        )

    def start_bounded_probe(
        self,
        *,
        document_ref: str,
        caller_ref: str,
        expected_content_sha256: str | None = None,
        max_pages: int = 20,
    ) -> dict[str, Any]:
        """Launch one acquisition requested by the bounded planner automation.

        Same governed subprocess as the human path; the ticket records the
        automation principal so the request lineage never impersonates a
        human.  Callers enforce the owner's call budget before arriving here.
        """

        if not isinstance(document_ref, str) or _DOCUMENT_REF_RE.fullmatch(document_ref) is None:
            raise AcquisitionLaunchRejected("document_ref must be alphaengine-doc:<id>")
        if (
            not isinstance(caller_ref, str)
            or not re.fullmatch(r"automation:[A-Za-z0-9][A-Za-z0-9._/-]*", caller_ref)
        ):
            raise AcquisitionLaunchRejected(
                "bounded probe acquisition must name its automation principal"
            )
        return self._launch(
            document_ref=document_ref, actor_ref=caller_ref,
            expected_content_sha256=expected_content_sha256, max_pages=max_pages,
        )

    def _launch(
        self,
        *,
        document_ref: str,
        actor_ref: str,
        expected_content_sha256: str | None,
        max_pages: int,
    ) -> dict[str, Any]:
        if expected_content_sha256 is not None and (
            not isinstance(expected_content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_content_sha256) is None
        ):
            raise AcquisitionLaunchRejected("expected_content_sha256 must be a lowercase sha256 hex")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 40:
            raise AcquisitionLaunchRejected("max_pages must be an integer between 1 and 40")
        governance = self.load_governance()
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise AcquisitionLaunchConflict(
                    f"acquisition {self._current[0]} is still running"
                )
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(
                canonical_json({
                    "document_ref": document_ref, "actor_ref": actor_ref,
                    "started_at": started_at, "governance_hash": governance.content_hash,
                }).encode("utf-8")
            ).hexdigest()[:24]
            ticket_id = f"alphaengine-acquisition:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._command(
                document_ref=document_ref, ticket_dir=ticket_dir,
                max_pages=max_pages, expected_content_sha256=expected_content_sha256,
            )
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
                "actor_ref": actor_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "loopback-mcp" if self.networked else "rehearsal",
                "expected_content_sha256": expected_content_sha256,
                "max_pages": max_pages,
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
            raise AcquisitionLaunchRejected("ticket_ref must be alphaengine-acquisition:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AcquisitionTicketNotFound(ticket_ref) from exc
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
                    # The writer restarted or the child died without a ticket
                    # update.  Do not guess success from a stray summary file.
                    record["status"] = "orphaned"
                    record["completed_at"] = _wire_time(self.clock())
                    _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        summary = None
        if record["status"] != "running" and summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {**record, "summary": summary}

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

    def wait(self, timeout: float | None = None) -> int | None:
        """Test hook: wait for the current child to finish."""

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
    "AcquisitionLaunchConflict",
    "AcquisitionLaunchError",
    "AcquisitionLaunchRejected",
    "AcquisitionTicketNotFound",
    "AlphaEngineAcquisitionLauncher",
    "LIVE_MODE_ARGS",
]
