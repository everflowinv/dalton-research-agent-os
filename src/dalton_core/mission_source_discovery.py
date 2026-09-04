"""Mission-driven AlphaEngine source discovery (P9d-1).

Three pieces sit here:

* ``DiscoveryPlan`` -- a human-authored, hash-bound manifest that says, per
  covered company, which search terms and which frozen search specs
  (document type, query template, look-back window, re-discovery cadence)
  the mission may run.  The Core never invents queries; every discovery
  record names the exact plan hash and spec it came from.
* ``AlphaEngineSearchLauncher`` -- the writer-owned launcher for the
  out-of-process ``dalton_core.alphaengine_search_cli`` child (the transport
  executor's SIGALRM watchdog needs a process main thread), mirroring the
  acquisition and SEC lane launchers: approved human governance record,
  single slot, owner-only tickets under ``<state>/discoveries/<ticket>/``.
* ``MissionSourceDiscoveryCoordinator`` -- what the controller tick calls.
  It settles finished children, launches at most one discovery and at most
  one budgeted document acquisition per call, and reports every skip with
  its reason (mission grant, cadence, budget, busy slot) instead of hiding it.

Nothing here writes Evidence, Claims or Theses.  Discovered documents that
Core acquires are raw connector authority; turning them into candidates is
the existing human-reviewed transcript path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .alphaengine_core_search import (
    SEARCH_DOCUMENT_TYPES,
    SearchConnectorGovernance,
    search_spec_hash,
    validate_search_spec,
)
from .bounded_alphaengine_probe import (
    MAX_CALLS_PER_WINDOW,
    count_recent_alphaengine_calls,
    document_in_authority,
)
from .coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionError,
    CoverageMissionNotFound,
)
from .store import DaltonStore, canonical_json, content_hash


DISCOVERY_PLAN_SCHEMA_VERSION = "0.1"
TICKET_SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "alphaengine-discovery"
LIVE_MODE_ARGS = ("--allow-network",)
# An acquisition child that failed (provider error, or orphaned by a deploy
# restart) is retried once this interval has passed; fresh documents are
# always acquired first.
ACQUISITION_RETRY_INTERVAL = timedelta(days=1)
_TICKET_RE = re.compile(r"alphaengine-discovery:[0-9a-f]{24}\Z")
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_AUTOMATION_RE = re.compile(r"automation:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_SPEC_REF_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_PLAN_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_ref", "source_ref", "companies", "specs",
    "content_hash",
})
_COMPANY_FIELDS = frozenset({"search_terms"})
_SPEC_FIELDS = frozenset({
    "spec_ref", "document_type", "query_template", "lookback_days",
    "rediscovery_interval_days", "retry_interval_days",
})


class DiscoveryPlanError(ValueError):
    """The discovery plan manifest is malformed or its hash does not bind."""


class DiscoveryLaunchError(RuntimeError):
    """Launcher configuration or filesystem failure."""


class DiscoveryLaunchRejected(ValueError):
    """The request was refused before any process was started."""


class DiscoveryLaunchConflict(RuntimeError):
    """Another discovery already holds the single slot."""


class DiscoveryTicketNotFound(LookupError):
    pass


# ---------------------------------------------------------------------------
# discovery plan
# ---------------------------------------------------------------------------
def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise DiscoveryPlanError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _plan_text(value: Any, name: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DiscoveryPlanError(f"{name} must be non-empty text (<= {maximum} chars)")
    return value.strip()


def _plan_time(value: Any) -> str:
    if not isinstance(value, str):
        raise DiscoveryPlanError("created_at must be RFC3339 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryPlanError("created_at must be RFC3339 text") from exc
    if parsed.tzinfo is None:
        raise DiscoveryPlanError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def validate_discovery_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
        raise DiscoveryPlanError("discovery plan has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != DISCOVERY_PLAN_SCHEMA_VERSION:
        raise DiscoveryPlanError("unsupported discovery plan schema_version")
    plan_id = _plan_text(wire["id"], "id")
    if not plan_id.startswith("discovery-plan:"):
        raise DiscoveryPlanError("discovery plan id must use the discovery-plan: namespace")
    mission_ref = _plan_text(wire["mission_ref"], "mission_ref")
    if not mission_ref.startswith("coverage-mission:"):
        raise DiscoveryPlanError("discovery plan mission_ref must use the coverage-mission: namespace")
    if wire["source_ref"] != "source:alphaengine":
        raise DiscoveryPlanError("discovery plan source_ref must be source:alphaengine (P9d-1)")
    companies = wire["companies"]
    if not isinstance(companies, Mapping) or not companies:
        raise DiscoveryPlanError("discovery plan companies must be a non-empty object")
    cleaned_companies: dict[str, dict[str, str]] = {}
    for company_ref in sorted(companies):
        entry = companies[company_ref]
        if not isinstance(entry, Mapping) or set(entry) != _COMPANY_FIELDS:
            raise DiscoveryPlanError(f"discovery plan company {company_ref} has an invalid shape")
        cleaned_companies[_plan_text(company_ref, "company_ref")] = {
            "search_terms": _plan_text(entry["search_terms"], "search_terms", maximum=120),
        }
    specs = wire["specs"]
    if not isinstance(specs, list) or not specs:
        raise DiscoveryPlanError("discovery plan specs must be a non-empty array")
    cleaned_specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in specs:
        if not isinstance(raw, Mapping) or set(raw) != _SPEC_FIELDS:
            raise DiscoveryPlanError("discovery plan spec has an invalid closed shape")
        spec_ref = _plan_text(raw["spec_ref"], "spec_ref", maximum=64)
        if _SPEC_REF_RE.fullmatch(spec_ref) is None or spec_ref in seen:
            raise DiscoveryPlanError("discovery plan spec_ref must be a unique kebab-case slug")
        seen.add(spec_ref)
        if raw["document_type"] not in SEARCH_DOCUMENT_TYPES:
            raise DiscoveryPlanError(f"discovery plan spec {spec_ref} document_type is not mapped")
        template = _plan_text(raw["query_template"], "query_template")
        if "{terms}" not in template or template.count("{") != 1 or template.count("}") != 1:
            raise DiscoveryPlanError(
                f"discovery plan spec {spec_ref} query_template must contain exactly one {{terms}}"
            )
        cleaned_specs.append({
            "spec_ref": spec_ref,
            "document_type": raw["document_type"],
            "query_template": template,
            "lookback_days": _positive_int(raw["lookback_days"], "lookback_days", maximum=3650),
            "rediscovery_interval_days": _positive_int(
                raw["rediscovery_interval_days"], "rediscovery_interval_days", maximum=365
            ),
            "retry_interval_days": _positive_int(
                raw["retry_interval_days"], "retry_interval_days", maximum=365
            ),
        })
    base = {
        "schema_version": DISCOVERY_PLAN_SCHEMA_VERSION,
        "id": plan_id,
        "created_at": _plan_time(wire["created_at"]),
        "mission_ref": mission_ref,
        "source_ref": "source:alphaengine",
        "companies": cleaned_companies,
        "specs": cleaned_specs,
    }
    expected = content_hash(base)
    if wire["content_hash"] != expected:
        raise DiscoveryPlanError("discovery plan content_hash does not bind its content")
    return {**base, "content_hash": expected}


def build_discovery_plan(
    *,
    plan_id: str,
    created_at: str,
    mission_ref: str,
    companies: Mapping[str, str],
    specs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Author a plan (hash appended) from search terms and spec rows."""

    base = {
        "schema_version": DISCOVERY_PLAN_SCHEMA_VERSION,
        "id": plan_id,
        "created_at": created_at,
        "mission_ref": mission_ref,
        "source_ref": "source:alphaengine",
        "companies": {ref: {"search_terms": terms} for ref, terms in companies.items()},
        "specs": [dict(spec) for spec in specs],
    }
    return validate_discovery_plan({**base, "content_hash": content_hash(base)})


def load_discovery_plan(path: str | Path) -> dict[str, Any]:
    return validate_discovery_plan(json.loads(Path(path).read_text(encoding="utf-8")))


def plan_spec(plan: Mapping[str, Any], spec_ref: str) -> dict[str, Any]:
    for spec in plan["specs"]:
        if spec["spec_ref"] == spec_ref:
            return dict(spec)
    raise DiscoveryPlanError(f"discovery plan has no spec {spec_ref}")


def build_discovery_parameters(
    plan: Mapping[str, Any], *, spec_ref: str, company_ref: str, as_of: date
) -> dict[str, Any]:
    """Deterministic ``search_library`` parameters for one plan spec and company."""

    spec = plan_spec(plan, spec_ref)
    company = plan["companies"].get(company_ref)
    if company is None:
        raise DiscoveryPlanError(f"discovery plan does not cover {company_ref}")
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise DiscoveryPlanError("as_of must be a calendar date")
    query = spec["query_template"].replace("{terms}", company["search_terms"])
    return validate_search_spec({
        "query": query,
        "filters": {
            "document_type": spec["document_type"],
            "date_from": (as_of - timedelta(days=spec["lookback_days"])).isoformat(),
            "date_to": as_of.isoformat(),
        },
        "cursor": None,
    })


# ---------------------------------------------------------------------------
# launcher
# ---------------------------------------------------------------------------
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


class AlphaEngineSearchLauncher:
    """Launch the search-discovery child against the writer's state directory."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        plan_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        mcp_endpoint: str | None = None,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
        spool_dir: str | Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.plan_path = Path(plan_path).expanduser().resolve()
        self.spool_dir = None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        self.mode_args = tuple(str(item) for item in mode_args)
        self.mcp_endpoint = mcp_endpoint
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        if not self.state_dir.is_dir():
            raise DiscoveryLaunchError("discovery state directory is missing")
        self.tickets_dir = _secure_dir(self.state_dir / "discoveries")

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def load_governance(self) -> SearchConnectorGovernance:
        try:
            governance = SearchConnectorGovernance.load(self.governance_path)
        except FileNotFoundError as exc:
            raise DiscoveryLaunchRejected(
                "search connector governance record is missing; owner approval is required"
            ) from exc
        except Exception as exc:
            raise DiscoveryLaunchRejected(
                f"search connector governance record is invalid: {exc}"
            ) from exc
        if not governance.approved:
            raise DiscoveryLaunchRejected(
                "search connector governance record is not approved; owner approval is required"
            )
        if _HUMAN_RE.fullmatch(governance.approved_by) is None:
            raise DiscoveryLaunchRejected(
                "search connector governance record must be approved by a human principal"
            )
        return governance

    def load_plan(self) -> dict[str, Any]:
        try:
            return load_discovery_plan(self.plan_path)
        except FileNotFoundError as exc:
            raise DiscoveryLaunchRejected("discovery plan is missing") from exc
        except (DiscoveryPlanError, json.JSONDecodeError) as exc:
            raise DiscoveryLaunchRejected(f"discovery plan is invalid: {exc}") from exc

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def _command(
        self,
        *,
        company_ref: str,
        spec_ref: str,
        requested_by: str,
        mission_version_ref: str,
        mission_version_hash: str,
        as_of: str,
        ticket_dir: Path,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", "dalton_core.alphaengine_search_cli",
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--discovery-plan", str(self.plan_path),
            "--company-ref", company_ref,
            "--spec-ref", spec_ref,
            "--requested-by", requested_by,
            "--mission-version-ref", mission_version_ref,
            "--mission-version-hash", mission_version_hash,
            "--as-of", as_of,
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        if self.networked and self.mcp_endpoint is not None:
            command += ["--mcp-endpoint", self.mcp_endpoint]
        command += list(self.mode_args)
        return command

    def start(
        self,
        *,
        authorization: Mapping[str, Any],
        spec_ref: str,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Spawn one discovery child bound to an exact mission authorization."""

        required = {
            "mission_version_ref", "mission_version_hash", "mission_ref", "company_ref",
            "ticker", "source_ref", "actor_ref", "requested_by", "scope",
            "max_alphaengine_calls_24h",
        }
        if not isinstance(authorization, Mapping) or set(authorization) != required:
            raise DiscoveryLaunchRejected("discovery launch requires an exact mission authorization")
        if authorization["scope"] != "source_discovery":
            raise DiscoveryLaunchRejected("discovery authorization scope is not source_discovery")
        requested_by = authorization["requested_by"]
        if not isinstance(requested_by, str) or (
            _HUMAN_RE.fullmatch(requested_by) is None
            and _AUTOMATION_RE.fullmatch(requested_by) is None
        ):
            raise DiscoveryLaunchRejected("discovery requester must use the human: or automation: namespace")
        if not isinstance(spec_ref, str) or _SPEC_REF_RE.fullmatch(spec_ref) is None:
            raise DiscoveryLaunchRejected("spec_ref must be a kebab-case slug")
        plan = self.load_plan()
        if plan["mission_ref"] != authorization["mission_ref"]:
            raise DiscoveryLaunchRejected("discovery plan does not belong to the authorized mission")
        if plan["source_ref"] != authorization["source_ref"]:
            raise DiscoveryLaunchRejected("discovery plan source differs from the authorization")
        if authorization["company_ref"] not in plan["companies"]:
            raise DiscoveryLaunchRejected("discovery plan does not cover the authorized company")
        try:
            plan_spec(plan, spec_ref)
        except DiscoveryPlanError as exc:
            raise DiscoveryLaunchRejected(str(exc)) from exc
        governance = self.load_governance()
        as_of_date = as_of or self.clock().date()
        if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
            raise DiscoveryLaunchRejected("as_of must be a calendar date")
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise DiscoveryLaunchConflict(f"discovery {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(
                canonical_json({
                    "authorization": dict(authorization), "spec_ref": spec_ref,
                    "as_of": as_of_date.isoformat(), "started_at": started_at,
                    "governance_hash": governance.content_hash,
                    "plan_hash": plan["content_hash"],
                }).encode("utf-8")
            ).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._command(
                company_ref=authorization["company_ref"], spec_ref=spec_ref,
                requested_by=requested_by,
                mission_version_ref=authorization["mission_version_ref"],
                mission_version_hash=authorization["mission_version_hash"],
                as_of=as_of_date.isoformat(), ticket_dir=ticket_dir,
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
                "company_ref": authorization["company_ref"],
                "spec_ref": spec_ref,
                "requested_by": requested_by,
                "actor_ref": authorization["actor_ref"],
                "mission_version_ref": authorization["mission_version_ref"],
                "mission_version_hash": authorization["mission_version_hash"],
                "as_of": as_of_date.isoformat(),
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "plan_ref": plan["id"],
                "plan_hash": plan["content_hash"],
                "transport": "loopback-mcp" if self.networked else "rehearsal",
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
            raise DiscoveryLaunchRejected(f"ticket_ref must be {TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DiscoveryTicketNotFound(ticket_ref) from exc
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
                    record["status"] = "orphaned"
                    record["completed_at"] = _wire_time(self.clock())
                    _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        summary = None
        if record["status"] != "running" and summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {**record, "summary": summary}

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None

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


# ---------------------------------------------------------------------------
# controller-tick coordinator
# ---------------------------------------------------------------------------
def alphaengine_calls_remaining(
    connection: Any, *, mission_cap: int, owner_cap: int = MAX_CALLS_PER_WINDOW,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """Trailing-24h AlphaEngine calls (search + document pages) against the tighter cap."""

    spent = count_recent_alphaengine_calls(connection, as_of=as_of)
    cap = min(int(mission_cap), int(owner_cap))
    return {"spent": spent, "cap": cap, "remaining": max(0, cap - spent)}


def _parse_wire_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class MissionSourceDiscoveryCoordinator:
    """Advance mission source discovery by at most one search + one acquisition."""

    def __init__(
        self,
        *,
        store: DaltonStore,
        missions: CoverageMissionAuthority,
        plan: Mapping[str, Any],
        search_launcher: Any | None,
        acquisition_launcher: Any | None,
        clock: Callable[[], datetime] | None = None,
        owner_call_cap: int = MAX_CALLS_PER_WINDOW,
    ) -> None:
        self.store = store
        self.missions = missions
        self.plan = validate_discovery_plan(plan)
        self.search_launcher = search_launcher
        self.acquisition_launcher = acquisition_launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner_call_cap = int(owner_call_cap)

    # -- settlement ----------------------------------------------------------
    def settle_dispatches(self) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        if self.search_launcher is None:
            return settled
        for dispatch in self.missions.open_discovery_dispatches():
            try:
                ticket = self.search_launcher.status(dispatch["ticket_ref"])
            except DiscoveryTicketNotFound:
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="failed", reason="ticket is missing"
                )
                settled.append({"dispatch_ref": dispatch["dispatch_id"], "status": result["status"]})
                continue
            if ticket["status"] == "running":
                continue
            summary = ticket.get("summary") or {}
            if ticket["status"] == "succeeded" and summary.get("discovery_ref"):
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="succeeded"
                )
            else:
                reason = (
                    summary.get("failure_reason")
                    or f"child ended {ticket['status']} (exit {ticket.get('exit_code')})"
                )
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="failed", reason=str(reason)[:500]
                )
            settled.append({
                "dispatch_ref": dispatch["dispatch_id"], "status": result["status"],
                "ticket_ref": dispatch["ticket_ref"],
                "discovery_ref": summary.get("discovery_ref"),
                "new_document_count": summary.get("new_document_count"),
            })
        return settled

    def settle_documents(self) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        if self.acquisition_launcher is None:
            return settled
        for document in self.missions.launched_discovered_documents():
            try:
                ticket = self.acquisition_launcher.status(document["ticket_ref"])
            except LookupError:
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquisition_failed", reason="ticket is missing"
                )
                settled.append({"record_id": document["record_id"], "status": result["status"]})
                continue
            if ticket.get("status") == "running":
                continue
            review_status: str | None = None
            review_id: str | None = None
            if ticket.get("status") == "succeeded" and document_in_authority(
                self.store.connection, document["document_ref"]
            ):
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquired"
                )
                # P9d-2: every acquired document enters the human extraction
                # queue.  Registration re-derives the mission grant, so a
                # superseded mission simply leaves the document without a
                # review; the tick result reports the reason.
                try:
                    review = self.missions.register_document_review(
                        document["record_id"],
                        requested_by=self.missions.mission(
                            document["mission_version_ref"]
                        )["autonomy"]["automation_principal"],
                    )
                    review_status, review_id = review["status"], review["review_id"]
                except CoverageMissionError as exc:
                    review_status = f"not_registered:{type(exc).__name__}"
            else:
                reason = (
                    "acquisition succeeded but the document is not in authority"
                    if ticket.get("status") == "succeeded"
                    else f"acquisition ended {ticket.get('status')} (exit {ticket.get('exit_code')})"
                )
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquisition_failed", reason=reason
                )
            entry = {
                "record_id": document["record_id"], "document_ref": document["document_ref"],
                "status": result["status"], "ticket_ref": document["ticket_ref"],
            }
            if review_status is not None:
                entry["review_status"] = review_status
                if review_id is not None:
                    entry["review_id"] = review_id
            settled.append(entry)
        return settled

    # -- discovery launch ----------------------------------------------------
    def _cadence_block(self, mission_version_ref: str, company_ref: str, spec: Mapping[str, Any]) -> str | None:
        latest = self.missions.discovery_dispatches(
            mission_version_ref, company_ref=company_ref, spec_ref=spec["spec_ref"], limit=1
        )
        if not latest:
            return None
        row = latest[0]
        age = self.clock() - _parse_wire_time(row["created_at"])
        if row["status"] == "launched":
            return "previous discovery still open"
        if row["status"] == "succeeded":
            if age < timedelta(days=spec["rediscovery_interval_days"]):
                return f"rediscovered {age.days}d ago; interval {spec['rediscovery_interval_days']}d"
            return None
        if age < timedelta(days=spec["retry_interval_days"]):
            return f"last attempt {row['status']} {age.days}d ago; retry interval {spec['retry_interval_days']}d"
        return None

    def _reserved_calls(self) -> int:
        """Children launched but not yet settled may not have recorded their call yet."""

        open_dispatches = len(self.missions.open_discovery_dispatches(limit=100))
        open_documents = len(self.missions.launched_discovered_documents(limit=100))
        return open_dispatches + open_documents

    def _budget(self, mission_cap: int) -> dict[str, int]:
        budget = alphaengine_calls_remaining(
            self.store.connection, mission_cap=mission_cap,
            owner_cap=self.owner_call_cap, as_of=self.clock(),
        )
        reserved = self._reserved_calls()
        budget["reserved"] = reserved
        budget["remaining"] = max(0, budget["remaining"] - reserved)
        return budget

    def launch_discovery(self) -> dict[str, Any]:
        if self.search_launcher is None:
            return {"status": "unconfigured", "reason": "search launcher is not configured"}
        try:
            mission = self.missions.active_mission(self.plan["mission_ref"])
        except CoverageMissionNotFound:
            return {"status": "no_active_mission", "mission_ref": self.plan["mission_ref"]}
        if self.search_launcher.running():
            return {"status": "busy", "reason": "a discovery child is still running"}
        skipped: list[dict[str, Any]] = []
        for member in mission["universe"]:
            company_ref = member["company_ref"]
            if company_ref not in self.plan["companies"]:
                skipped.append({"company_ref": company_ref, "reason": "not in discovery plan"})
                continue
            for spec in self.plan["specs"]:
                block = self._cadence_block(mission["id"], company_ref, spec)
                if block is not None:
                    skipped.append({
                        "company_ref": company_ref, "spec_ref": spec["spec_ref"], "reason": block,
                    })
                    continue
                try:
                    authorization = self.missions.authorize_source_discovery(
                        company_ref=company_ref,
                        source_ref=self.plan["source_ref"],
                        requested_by=mission["autonomy"]["automation_principal"],
                        mission_version_ref=mission["id"],
                        mission_version_hash=mission["content_hash"],
                    )
                except CoverageMissionError as exc:
                    # The grant is mission-wide; one refusal means every
                    # candidate would be refused.  Report and stop.
                    return {
                        "status": "not_authorized",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "mission_version_ref": mission["id"],
                        "skipped": skipped,
                    }
                budget = self._budget(authorization["max_alphaengine_calls_24h"])
                if budget["remaining"] < 1:
                    return {"status": "budget_exhausted", "budget": budget, "skipped": skipped}
                parameters = build_discovery_parameters(
                    self.plan, spec_ref=spec["spec_ref"], company_ref=company_ref,
                    as_of=self.clock().date(),
                )
                try:
                    ticket = self.search_launcher.start(
                        authorization=authorization, spec_ref=spec["spec_ref"],
                        as_of=self.clock().date(),
                    )
                except DiscoveryLaunchConflict as exc:
                    return {"status": "busy", "reason": str(exc), "skipped": skipped}
                except DiscoveryLaunchRejected as exc:
                    return {
                        "status": "rejected", "reason": f"{type(exc).__name__}: {exc}",
                        "company_ref": company_ref, "spec_ref": spec["spec_ref"],
                        "skipped": skipped,
                    }
                dispatch = self.missions.record_discovery_dispatch(
                    authorization=authorization,
                    discovery_plan_ref=self.plan["id"],
                    discovery_plan_hash=self.plan["content_hash"],
                    spec_ref=spec["spec_ref"],
                    query_hash=search_spec_hash(parameters),
                    ticket_ref=ticket["id"],
                )
                return {
                    "status": "launched",
                    "dispatch_ref": dispatch["dispatch_id"],
                    "ticket_ref": ticket["id"],
                    "company_ref": company_ref,
                    "spec_ref": spec["spec_ref"],
                    "budget": budget,
                    "skipped": skipped,
                }
        return {"status": "idle", "skipped": skipped}

    # -- document acquisition ------------------------------------------------
    def launch_acquisition(self) -> dict[str, Any]:
        if self.acquisition_launcher is None:
            return {"status": "unconfigured", "reason": "acquisition launcher is not configured"}
        if self.missions.launched_discovered_documents(limit=1):
            return {"status": "busy", "reason": "a discovered-document acquisition is still open"}
        retry = False
        document = self.missions.next_discovered_document()
        if document is None:
            # No fresh documents: retry the oldest acquisition failure whose
            # interval has passed (e.g. a child orphaned by a deploy restart).
            document = self.missions.retryable_failed_document(
                older_than=ACQUISITION_RETRY_INTERVAL, as_of=self.clock()
            )
            retry = document is not None
        if document is None:
            return {"status": "idle"}
        try:
            authorization = self.missions.authorize_source_discovery(
                company_ref=document["company_ref"],
                source_ref=document["source_ref"],
                requested_by=self.missions.mission(document["mission_version_ref"])["autonomy"]["automation_principal"],
                mission_version_ref=document["mission_version_ref"],
            )
        except CoverageMissionError as exc:
            return {
                "status": "not_authorized", "record_id": document["record_id"],
                "document_ref": document["document_ref"],
                "reason": f"{type(exc).__name__}: {exc}",
            }
        budget = self._budget(authorization["max_alphaengine_calls_24h"])
        if budget["remaining"] < 1:
            return {"status": "budget_exhausted", "budget": budget, "document_ref": document["document_ref"]}
        try:
            ticket = self.acquisition_launcher.start_bounded_probe(
                document_ref=document["document_ref"],
                caller_ref=authorization["actor_ref"],
            )
        except Exception as exc:  # launcher conflict / rejection: report, keep the row
            name = type(exc).__name__
            status = "busy" if name.endswith("Conflict") else "rejected"
            return {
                "status": status, "reason": f"{name}: {exc}",
                "document_ref": document["document_ref"], "record_id": document["record_id"],
            }
        if retry:
            self.missions.mark_failed_document_retry_launched(document["record_id"], ticket["id"])
        else:
            self.missions.mark_discovered_document_launched(document["record_id"], ticket["id"])
        return {
            "status": "launched", "record_id": document["record_id"],
            "document_ref": document["document_ref"], "ticket_ref": ticket["id"],
            "retry": retry, "budget": budget,
        }

    def dispatch_once(self) -> dict[str, Any]:
        settled_dispatches = self.settle_dispatches()
        settled_documents = self.settle_documents()
        # Acquiring an already-discovered document comes before spending the
        # shared budget on a new search: known gaps first, then new ones.
        acquisition = self.launch_acquisition()
        discovery = self.launch_discovery()
        active = discovery.get("status") == "launched" or acquisition.get("status") == "launched"
        return {
            "status": "launched" if active else "idle",
            "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"],
            "settled_dispatches": settled_dispatches,
            "settled_documents": settled_documents,
            "discovery": discovery,
            "acquisition": acquisition,
        }


__all__ = [
    "AlphaEngineSearchLauncher",
    "DISCOVERY_PLAN_SCHEMA_VERSION",
    "DiscoveryLaunchConflict",
    "DiscoveryLaunchError",
    "DiscoveryLaunchRejected",
    "DiscoveryPlanError",
    "DiscoveryTicketNotFound",
    "LIVE_MODE_ARGS",
    "MissionSourceDiscoveryCoordinator",
    "alphaengine_calls_remaining",
    "build_discovery_parameters",
    "build_discovery_plan",
    "load_discovery_plan",
    "plan_spec",
    "validate_discovery_plan",
]
