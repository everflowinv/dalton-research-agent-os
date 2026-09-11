"""Writer-owned launcher for out-of-process SEC company-facts lane runs.

The S7d lane (``dalton_core.sec_company_facts_lane``) drives the Gate 1 chain
``get_company_facts -> resolver -> verifier -> staging -> policy commit ->
closure`` against the live Core state directory.  Like the AlphaEngine
acquisition it cannot run on a writer thread: ``ConnectorTransportExecutor``
bounds each provider call with a ``SIGALRM`` watchdog that only the process
main thread may own.  So a human governance operation *launches*
``dalton_core.sec_lane_cli`` as a child process, records a ticket and returns
at once; a second operation reads the ticket back.

What this launcher enforces before anything is spawned:

* the committed SEC connector governance record on disk is ``approved`` by a
  ``human:*`` principal (a ``proposed`` record, a hash mismatch or a non-human
  approver refuses the launch);
* the requester is either a ``human:*`` principal or the exact automation
  principal in an exact CoverageMission authorization receipt;
* issuers are upper-case tickers, the filed window is two ISO dates in order;
* only one lane run holds the slot at a time.

The child inherits nothing secret: SEC is a public, credential-free host, the
Core path is the writer's own state directory, and the candidate staging file
is the same owner-only file the Cockpit reviews.  Tickets and child output are
owner-only files under ``<state>/sec-lane-runs/<ticket>/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .sec_public_adapter import SEC_COMPANY_FACTS_FORMS
from .store import canonical_json

TICKET_SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "sec-lane-run"
_TICKET_RE = re.compile(r"sec-lane-run:[0-9a-f]{24}\Z")
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_AUTOMATION_RE = re.compile(r"automation:[A-Za-z0-9._-]+\Z")
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.]{0,7}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}\Z")
LIVE_MODE_ARGS = ("--allow-network",)
# Children inherit the writer's launchd ``ProcessType``.  Measured on
# 2026-08-26 (CPU probe, 6M-iteration loop): ``ProcessType: Background``
# 1.98s vs ``Standard`` 0.32s, and neither ``setpriority(PRIO_DARWIN_PROCESS,
# 0, 0)`` in the child nor ``taskpolicy -B -p <pid>`` from outside lifts the
# launchd clamp (1.88s / 2.38s).  The only lever is the writer LaunchAgent's
# ``ProcessType`` (``macos_launchagent``), so this launcher does not try to
# re-prioritise the child.  A live CTSH step under Background took 6m31s.
# Writer-hosted children cannot be attached to without root (sample /
# sys.remote_exec both need the task port), so the child writes its own
# Python stack to run.log every interval; a stall then has a trace.
STACK_DUMP_SECONDS = 60
CLI_MODULE = "dalton_core.sec_lane_cli"
MAX_ISSUERS = 8


class LaneLaunchError(RuntimeError):
    """Launcher configuration or filesystem failure."""


class LaneLaunchRejected(ValueError):
    """The request was refused before any process was started."""


class LaneLaunchConflict(RuntimeError):
    """Another lane run already holds the single slot."""


class LaneTicketNotFound(LookupError):
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


def _iso_date(value: Any, name: str) -> str:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise LaneLaunchRejected(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise LaneLaunchRejected(f"{name} is not a calendar date") from exc
    return value


def _default_governance_loader(path: Path) -> Any:
    # Lazy: the generic governance module lands in the S7d-2 slice.  A writer
    # built without it refuses every launch instead of crashing at import.
    try:
        from .connector_governance import load_connector_governance
    except ImportError as exc:  # pragma: no cover - depends on the sibling slice
        raise LaneLaunchRejected(
            "connector governance module is unavailable on this writer"
        ) from exc
    return load_connector_governance(path)


class SecLaneLauncher:
    """Spawn ``dalton_core.sec_lane_cli`` against the writer's state directory."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        staging_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
        spool_dir: str | Path | None = None,
        user_agent: str | None = None,
        web_fetch_governance_path: str | Path | None = None,
        annual_report_draft_model_config_path: str | Path | None = None,
        annual_report_verifier_model_config_path: str | Path | None = None,
        annual_report_draft_fixture_path: str | Path | None = None,
        annual_report_verifier_fixture_path: str | Path | None = None,
        governance_loader: Callable[[Path], Any] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.staging_path = Path(staging_path).expanduser().resolve()
        self.spool_dir = (
            None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        )
        self.mode_args = tuple(str(item) for item in mode_args)
        self.python_executable = python_executable or sys.executable
        self.user_agent = user_agent
        self.web_fetch_governance_path = (
            None if web_fetch_governance_path is None
            else Path(web_fetch_governance_path).expanduser().resolve()
        )
        self.annual_report_draft_model_config_path = (
            None if annual_report_draft_model_config_path is None
            else Path(annual_report_draft_model_config_path).expanduser().resolve()
        )
        self.annual_report_verifier_model_config_path = (
            None if annual_report_verifier_model_config_path is None
            else Path(annual_report_verifier_model_config_path).expanduser().resolve()
        )
        self.annual_report_draft_fixture_path = (
            None if annual_report_draft_fixture_path is None
            else Path(annual_report_draft_fixture_path).expanduser().resolve()
        )
        self.annual_report_verifier_fixture_path = (
            None if annual_report_verifier_fixture_path is None
            else Path(annual_report_verifier_fixture_path).expanduser().resolve()
        )
        if ((self.annual_report_draft_fixture_path is None)
                != (self.annual_report_verifier_fixture_path is None)):
            raise LaneLaunchError("annual-report rehearsal fixtures must be configured as a pair")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._governance_loader = governance_loader or _default_governance_loader
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        if not self.state_dir.is_dir():
            raise LaneLaunchError("lane state directory is missing")
        self.tickets_dir = _secure_dir(self.state_dir / "sec-lane-runs")
        self._recover_annual_ticket()

    # ------------------------------------------------------------------
    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def load_governance(self) -> Any:
        try:
            governance = self._governance_loader(self.governance_path)
        except LaneLaunchRejected:
            raise
        except FileNotFoundError as exc:
            raise LaneLaunchRejected(
                "SEC connector governance record is missing; owner approval is required"
            ) from exc
        except Exception as exc:  # hash / shape failures are rejections, not crashes
            raise LaneLaunchRejected(
                f"SEC connector governance record is invalid: {exc}"
            ) from exc
        if not getattr(governance, "approved", False):
            raise LaneLaunchRejected(
                "SEC connector governance record is not approved; owner approval is required"
            )
        approved_by = getattr(governance, "approved_by", None)
        if not isinstance(approved_by, str) or _HUMAN_RE.fullmatch(approved_by) is None:
            raise LaneLaunchRejected(
                "SEC connector governance record must be approved by a human principal"
            )
        for name in ("id", "content_hash"):
            if not isinstance(getattr(governance, name, None), str):
                raise LaneLaunchRejected(f"SEC connector governance record lacks {name}")
        return governance

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def _command(
        self,
        *,
        issuers: Sequence[str],
        filed_from: str,
        filed_to: str,
        actor_ref: str,
        ticket_dir: Path,
        form: str = "10-Q",
        expected_accession: str | None = None,
        mission_context: Mapping[str, Any] | None = None,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", CLI_MODULE,
            "--state-dir", str(self.state_dir),
            "--staging", str(self.staging_path),
            "--governance", str(self.governance_path),
            "--summary-dir", str(ticket_dir),
            "--actor", actor_ref,
            "--filed-from", filed_from,
            "--filed-to", filed_to,
            "--form", form,
            "--quiet",
            "--stack-dump-seconds", str(STACK_DUMP_SECONDS),
        ]
        if expected_accession is not None:
            command += ["--expected-accession", expected_accession]
        if mission_context is not None:
            command += [
                "--mission-version-ref", mission_context["mission_version_ref"],
                "--mission-version-hash", mission_context["mission_version_hash"],
                "--mission-company-ref", mission_context["company_ref"],
            ]
        for ticker in issuers:
            command += ["--issuer", ticker]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        if self.user_agent is not None:
            command += ["--user-agent", self.user_agent]
        command += list(self.mode_args)
        return command

    # ------------------------------------------------------------------
    def start(
        self,
        *,
        issuers: Sequence[str],
        filed_from: str,
        filed_to: str,
        actor_ref: str,
        form: str = "10-Q",
        expected_accession: str | None = None,
        mission_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actor_ref, str) or (
            _HUMAN_RE.fullmatch(actor_ref) is None
            and _AUTOMATION_RE.fullmatch(actor_ref) is None
        ):
            raise LaneLaunchRejected("lane run actor must use the human: or automation: namespace")
        if _AUTOMATION_RE.fullmatch(actor_ref):
            required = {
                "mission_version_ref", "mission_version_hash", "mission_ref", "company_ref",
                "ticker", "actor_ref", "paid_calls_reserved", "cost_usd_reserved", "budget",
            }
            if not isinstance(mission_context, Mapping) or set(mission_context) != required:
                raise LaneLaunchRejected("automation lane run requires an exact mission authorization")
        elif mission_context is not None:
            raise LaneLaunchRejected("human lane runs must not carry mission automation context")
        if expected_accession is not None and (
            not isinstance(expected_accession, str)
            or _ACCESSION_RE.fullmatch(expected_accession) is None
        ):
            raise LaneLaunchRejected("expected_accession must be a hyphenated SEC accession")
        if _AUTOMATION_RE.fullmatch(actor_ref) and expected_accession is None:
            raise LaneLaunchRejected("automation lane run must bind the observed accession")
        if not isinstance(form, str) or form not in SEC_COMPANY_FACTS_FORMS:
            raise LaneLaunchRejected(
                f"form must be one of {'|'.join(SEC_COMPANY_FACTS_FORMS)}"
            )
        if (
            isinstance(issuers, (str, bytes))
            or not isinstance(issuers, Sequence)
            or not 1 <= len(issuers) <= MAX_ISSUERS
        ):
            raise LaneLaunchRejected(
                f"issuers must list between 1 and {MAX_ISSUERS} tickers"
            )
        tickers: list[str] = []
        for item in issuers:
            if not isinstance(item, str) or _TICKER_RE.fullmatch(item) is None:
                raise LaneLaunchRejected("issuer tickers must be upper-case symbols")
            if item in tickers:
                raise LaneLaunchRejected(f"issuer {item} is listed twice")
            tickers.append(item)
        if mission_context is not None and (
            mission_context.get("actor_ref") != actor_ref
            or tickers != [mission_context.get("ticker")]
            or mission_context.get("paid_calls_reserved") != 0
            or mission_context.get("cost_usd_reserved") != 0.0
        ):
            raise LaneLaunchRejected("automation lane mission authorization does not match the run")
        filed_from = _iso_date(filed_from, "filed_from")
        filed_to = _iso_date(filed_to, "filed_to")
        if filed_from > filed_to:
            raise LaneLaunchRejected("filed_from must not be after filed_to")
        if not self.staging_path.parent.is_dir():
            raise LaneLaunchRejected("candidate staging directory is missing")
        governance = self.load_governance()
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise LaneLaunchConflict(
                    f"lane run {self._current[0]} is still running"
                )
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(
                canonical_json({
                    "issuers": tickers, "filed_from": filed_from, "filed_to": filed_to,
                    "form": form,
                    "actor_ref": actor_ref, "started_at": started_at,
                    "governance_hash": governance.content_hash,
                    "expected_accession": expected_accession,
                    "mission_context": dict(mission_context) if mission_context is not None else None,
                }).encode("utf-8")
            ).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._command(
                issuers=tickers, filed_from=filed_from, filed_to=filed_to,
                actor_ref=actor_ref, ticket_dir=ticket_dir, form=form,
                expected_accession=expected_accession,
                mission_context=mission_context,
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
                "issuers": tickers,
                "filed_from": filed_from,
                "filed_to": filed_to,
                "form": form,
                "actor_ref": actor_ref,
                "expected_accession": expected_accession,
                "mission_context": (
                    dict(mission_context) if mission_context is not None else None
                ),
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "sec-public-https" if self.networked else "rehearsal",
                "staging_path": str(self.staging_path),
                "started_at": started_at,
                "pid": process.pid,
                "status": "running",
                "exit_code": None,
                "completed_at": None,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._current = (ticket_id, process)
            return dict(record)

    def start_registered_annual_report(
        self, *, plan_version_ref: str, actor_ref: str
    ) -> dict[str, Any]:
        """Run one already approved and started 0.2 plan in the lane child."""

        if not isinstance(actor_ref, str) or _HUMAN_RE.fullmatch(actor_ref) is None:
            raise LaneLaunchRejected("annual-report lane run actor must be a human principal")
        if not isinstance(plan_version_ref, str) or not plan_version_ref.startswith(
            "research-plan:"
        ):
            raise LaneLaunchRejected("plan_version_ref must be a research plan version")
        missing = self._annual_configuration_errors()
        if missing:
            raise LaneLaunchRejected(
                "registered annual-report execution is not configured: " + ", ".join(missing)
            )
        # Validate both files, including their common Router authority and budgets,
        # before a child can occupy the single lane slot.
        from .annual_report_runtime import load_annual_report_model_configs
        try:
            load_annual_report_model_configs(
                self.state_dir,
                draft_path=self.annual_report_draft_model_config_path,
                verifier_path=self.annual_report_verifier_model_config_path,
            )
        except Exception as exc:
            raise LaneLaunchRejected(str(exc)) from exc
        governance = self.load_governance()
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise LaneLaunchConflict(f"lane run {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(canonical_json({
                "operation": "registered_annual_report",
                "plan_version_ref": plan_version_ref,
                "actor_ref": actor_ref,
                "started_at": started_at,
                "governance_hash": governance.content_hash,
            }).encode("utf-8")).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._annual_command(
                plan_version_ref=plan_version_ref, actor_ref=actor_ref,
                ticket_dir=ticket_dir,
            )
            process = self._spawn(command, ticket_dir, append=False)
            record = {
                "schema_version": TICKET_SCHEMA_VERSION, "id": ticket_id,
                "operation": "registered_annual_report",
                "plan_version_ref": plan_version_ref, "actor_ref": actor_ref,
                "governance_ref": governance.id, "governance_hash": governance.content_hash,
                "transport": "local-registered-sec-source",
                "staging_path": str(self.staging_path), "started_at": started_at,
                "pid": process.pid, "status": "running", "exit_code": None,
                "completed_at": None, "resume_count": 0,
                "deadline_resume_attempted": False,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._current = (ticket_id, process)
            self._start_monitor(ticket_id, process)
            return dict(record)

    def _annual_configuration_errors(self) -> list[str]:
        required = {
            "connector spool": self.spool_dir,
            "public-web fetch governance": self.web_fetch_governance_path,
            "annual-report draft model configuration": self.annual_report_draft_model_config_path,
            "annual-report verifier model configuration": self.annual_report_verifier_model_config_path,
        }
        if self.annual_report_draft_fixture_path is not None:
            required.update({
                "annual-report draft fixture": self.annual_report_draft_fixture_path,
                "annual-report verifier fixture": self.annual_report_verifier_fixture_path,
            })
        return [name for name, path in required.items() if path is None or not path.exists()]

    def _annual_command(
        self, *, plan_version_ref: str, actor_ref: str, ticket_dir: Path
    ) -> list[str]:
        command = [
                self.python_executable, "-m", CLI_MODULE,
                "--state-dir", str(self.state_dir),
                "--staging", str(self.staging_path),
                "--governance", str(self.governance_path),
                "--summary-dir", str(ticket_dir),
                "--actor", actor_ref,
                "--annual-plan-ref", plan_version_ref,
                "--web-fetch-governance", str(self.web_fetch_governance_path),
                "--annual-draft-model-config", str(self.annual_report_draft_model_config_path),
                "--annual-verifier-model-config", str(self.annual_report_verifier_model_config_path),
                "--spool-dir", str(self.spool_dir),
                "--quiet", "--stack-dump-seconds", str(STACK_DUMP_SECONDS),
                *self.mode_args,
            ]
        if self.annual_report_draft_fixture_path is not None:
            command += [
                "--annual-draft-fixture", str(self.annual_report_draft_fixture_path),
                "--annual-verifier-fixture", str(self.annual_report_verifier_fixture_path),
                "--annual-fixture-real-wait",
                "--annual-fixture-wait-marker", str(ticket_dir / "backoff-wait.json"),
            ]
        return command

    def _spawn(
        self, command: Sequence[str], ticket_dir: Path, *, append: bool
    ) -> subprocess.Popen[bytes]:
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        log_fd = os.open(str(ticket_dir / "run.log"), flags, 0o600)
        try:
            return subprocess.Popen(
                list(command), cwd=str(self.state_dir), stdin=subprocess.DEVNULL,
                stdout=log_fd, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        finally:
            os.close(log_fd)

    def _annual_resume_state(self, record: Mapping[str, Any]) -> tuple[bool, bool]:
        """Return (resumable, deadline_expired) from exact Core plan/Scheduler rows."""

        try:
            from .research_plan import (
                _resolved_plan_work_orders, plan_start_ref_for,
                read_exact_research_plan_start, read_exact_research_plan_version,
            )

            uri = f"file:{self.state_dir / 'core.sqlite'}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            try:
                cursor = connection.cursor()
                plan = read_exact_research_plan_version(
                    cursor, record["plan_version_ref"]
                )
                read_exact_research_plan_start(
                    cursor, plan_start_ref_for(plan["id"])
                )
                if (plan["schema_version"] != "0.2"
                        or plan["execution_scope"]["operation"]
                        != "search_registered_annual_report"):
                    return False, False
                mission_version_ref = plan["execution_scope"]["parameters"][
                    "mission_version_ref"
                ]
                active = cursor.execute(
                    "SELECT p.mission_version_id,p.version_number AS pointer_version,"
                    "p.content_hash AS pointer_hash,v.version_number,v.content_hash "
                    "FROM coverage_mission_versions v "
                    "JOIN coverage_mission_pointer p ON p.mission_ref=v.mission_ref "
                    "WHERE v.mission_version_id=?",
                    (mission_version_ref,),
                ).fetchone()
                if (active is None
                        or active["mission_version_id"] != mission_version_ref
                        or active["pointer_version"] != active["version_number"]
                        or active["pointer_hash"] != active["content_hash"]):
                    # Revoked (no pointer) and superseded mission versions never
                    # gain new execution merely because an old child died.
                    return False, False
                for work in _resolved_plan_work_orders(plan, cursor):
                    formal = cursor.execute(
                        "SELECT terminal_state FROM scheduler_formal_results "
                        "WHERE work_order_id=?", (work["id"],)
                    ).fetchone()
                    if formal is not None:
                        if formal["terminal_state"] != "succeeded":
                            return False, False
                        continue
                    admitted = cursor.execute(
                        "SELECT created_at FROM scheduler_attempt_events "
                        "WHERE work_order_id=? ORDER BY event_seq LIMIT 1", (work["id"],)
                    ).fetchone()
                    if admitted is None:
                        # The prior completion can be replayed to admit this node.
                        return True, False
                    maximum = work.get("budget", {}).get("max_elapsed_seconds")
                    if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
                        deadline = datetime.fromisoformat(admitted["created_at"]) + timedelta(
                            seconds=maximum
                        )
                        return True, self.clock() >= deadline
                    return False, False
                return True, False
            finally:
                connection.close()
        except Exception:
            return False, False

    def _resume_annual_locked(self, ticket_id: str, record: dict[str, Any]) -> bool:
        if (record.get("id") != ticket_id
                or _TICKET_RE.fullmatch(ticket_id) is None
                or not isinstance(record.get("plan_version_ref"), str)
                or not record["plan_version_ref"].startswith("research-plan:")
                or not isinstance(record.get("actor_ref"), str)
                or _HUMAN_RE.fullmatch(record["actor_ref"]) is None
                or record.get("staging_path") != str(self.staging_path)):
            return False
        if self._annual_configuration_errors():
            return False
        try:
            from .annual_report_runtime import load_annual_report_model_configs

            load_annual_report_model_configs(
                self.state_dir,
                draft_path=self.annual_report_draft_model_config_path,
                verifier_path=self.annual_report_verifier_model_config_path,
            )
            governance = self.load_governance()
        except Exception:
            return False
        if (governance.id != record.get("governance_ref")
                or governance.content_hash != record.get("governance_hash")):
            return False
        resumable, expired = self._annual_resume_state(record)
        if not resumable or (expired and record.get("deadline_resume_attempted")):
            return False
        ticket_dir = self._ticket_path(ticket_id).parent
        (ticket_dir / "summary.json").unlink(missing_ok=True)
        command = self._annual_command(
            plan_version_ref=record["plan_version_ref"],
            actor_ref=record["actor_ref"], ticket_dir=ticket_dir,
        )
        process = self._spawn(command, ticket_dir, append=True)
        record["pid"] = process.pid
        record["status"] = "running"
        record["exit_code"] = None
        record["completed_at"] = None
        record["resume_count"] = int(record.get("resume_count", 0)) + 1
        if expired:
            record["deadline_resume_attempted"] = True
        _write_owner_only(self._ticket_path(ticket_id), record)
        self._current = (ticket_id, process)
        return True

    def _start_monitor(self, ticket_id: str, process: subprocess.Popen[bytes]) -> None:
        def monitor() -> None:
            current = process
            while not self._stop.is_set():
                code = current.wait()
                if self._stop.is_set():
                    return
                with self._lock:
                    path = self._ticket_path(ticket_id)
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        return
                    summary_exists = path.with_name("summary.json").is_file()
                    if code != 0 and not summary_exists and self._resume_annual_locked(
                        ticket_id, record
                    ):
                        current = self._current[1]
                        continue
                    record["exit_code"] = code
                    record["completed_at"] = _wire_time(self.clock())
                    record["status"] = "succeeded" if code == 0 else "failed"
                    _write_owner_only(path, record)
                    return

        self._monitor = threading.Thread(
            target=monitor, name=f"annual-resume-{ticket_id[-8:]}", daemon=True
        )
        self._monitor.start()

    def _recover_annual_ticket(self) -> None:
        if self._annual_configuration_errors():
            return
        candidates = sorted(self.tickets_dir.glob("*/ticket.json"), reverse=True)
        for path in candidates:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (record.get("operation") != "registered_annual_report"
                    or record.get("status") not in {"running", "orphaned"}
                    or path.with_name("summary.json").is_file()):
                continue
            if self._pid_alive(record.get("pid")):
                self._start_orphan_monitor(record["id"], int(record["pid"]))
                return
            with self._lock:
                if self._resume_annual_locked(record["id"], record):
                    self._start_monitor(record["id"], self._current[1])
            return

    def _start_orphan_monitor(self, ticket_id: str, pid: int) -> None:
        """Watch a child inherited from the prior Writer and resume on death."""

        def monitor() -> None:
            while not self._stop.wait(0.25):
                if self._pid_alive(pid):
                    continue
                with self._lock:
                    path = self._ticket_path(ticket_id)
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        return
                    if (record.get("status") not in {"running", "orphaned"}
                            or record.get("pid") != pid
                            or path.with_name("summary.json").is_file()):
                        return
                    if self._resume_annual_locked(ticket_id, record):
                        self._start_monitor(ticket_id, self._current[1])
                return

        self._monitor = threading.Thread(
            target=monitor, name=f"annual-orphan-{ticket_id[-8:]}", daemon=True
        )
        self._monitor.start()

    def status(self, ticket_ref: str) -> dict[str, Any]:
        if not isinstance(ticket_ref, str) or _TICKET_RE.fullmatch(ticket_ref) is None:
            raise LaneLaunchRejected(f"ticket_ref must be {TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LaneTicketNotFound(ticket_ref) from exc
        with self._lock:
            process = None
            if self._current is not None and self._current[0] == ticket_ref:
                process = self._current[1]
            if record["status"] == "running":
                if process is not None:
                    code = process.poll()
                    if (code is not None
                            and (self._monitor is None or not self._monitor.is_alive())):
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
        """Test hook: follow automatic annual resumes through terminal exit."""

        deadline = None if timeout is None else datetime.now().timestamp() + timeout
        seen = False
        while True:
            with self._lock:
                current = self._current
                monitor = self._monitor
            if current is None:
                return None if not seen else 0
            seen = True
            remaining = None if deadline is None else max(0.0, deadline - datetime.now().timestamp())
            try:
                code = current[1].wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise
            if monitor is not None:
                remaining = None if deadline is None else max(0.0, deadline - datetime.now().timestamp())
                monitor.join(timeout=remaining)
                if monitor.is_alive():
                    raise subprocess.TimeoutExpired(current[1].args, timeout)
            with self._lock:
                if self._current is current:
                    return code

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            current, self._current = self._current, None
        if current is not None and current[1].poll() is None:
            current[1].terminate()
            try:
                current[1].wait(timeout=5)
            except subprocess.TimeoutExpired:
                current[1].kill()
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=5)


__all__ = [
    "CLI_MODULE",
    "LIVE_MODE_ARGS",
    "LaneLaunchConflict",
    "LaneLaunchError",
    "LaneLaunchRejected",
    "LaneTicketNotFound",
    "SecLaneLauncher",
    "TICKET_PREFIX",
]
