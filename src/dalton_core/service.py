"""Persistent, event-driven controller for Dalton Research Agent OS.

The controller keeps deterministic maintenance alive: it reclaims expired
leases, rebuilds disposable projections after authority changes, runs scoped
plugins, and emits an owner-only heartbeat.  It does not keep an LLM session
alive and it does not claim research work itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
import concurrent.futures
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .dashboard_projector import project_dashboard
from .agenda_coordinator import AgendaCoordinator, AgendaCoordinatorConfig
from .agenda_control import AgendaControlConfig
from .backup import DatabaseBackupManager
from .openclaw_agenda_bridge import OpenClawAgendaBridge, OpenClawAgendaBridgeConfig
from .plugins.static_dashboard import StaticDashboardPlugin
from .scheduler import Scheduler
from .thesis_impact_production import ThesisImpactProductionConfig
from .bounded_planner_driver import (
    BoundedPlannerDriver,
    BoundedPlannerDriverConfig,
    BoundedPlannerDriverError,
)
from .weekly_brief_coordinator import (
    WeeklyBriefCoordinator,
    WeeklyBriefCoordinatorConfig,
)
from .workspace_runtime import WorkspaceRuntimeError, validate_runtime_context


SCHEMA_VERSION = "0.1"


class ServiceConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    workspace_id: str
    manifest_path: Path
    manifest_hash: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _absolute_path(value: Any, name: str, *, nullable: bool = False) -> Path | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ServiceConfigError(f"{name} must be an absolute path")
    return Path(value)


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ServiceConfigError(f"{name} must be positive")
    return float(value)


# ADR-0009: the Perception/Agenda plane is retired.  ``perception.py`` reads a
# legacy Coverage sqlite belonging to a product that was itself retired on
# 2026-09-04, so the plane has no input; ``ResearchEvent`` is the one event
# plane.  Nothing is deleted -- the modules, the tests and the delivered
# history all stay -- but the daily run is constructed only when the config
# says ``legacy_agenda_plane: true``.  An ``agenda`` block left behind in an
# old service.json therefore cannot start the run by accident.
LEGACY_AGENDA_PLANE_KEY = "legacy_agenda_plane"
LEGACY_AGENDA_RETIRED_LINE = "legacy agenda plane retired (ADR-0009)"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    core_db: Path
    scheduler_db: Path
    projection_db: Path
    model_router_db: Path | None
    capability_catalog_db: Path | None
    heartbeat_path: Path
    writer_socket: Path
    tick_seconds: float
    projection_min_interval_seconds: float
    plugin_retry_seconds: float
    plugins: tuple[StaticDashboardPlugin, ...]
    agenda: AgendaCoordinatorConfig | None
    agenda_interval_seconds: float | None
    weekly_brief: WeeklyBriefCoordinatorConfig | None
    weekly_brief_interval_seconds: float | None
    bounded_planner: BoundedPlannerDriverConfig | None
    bounded_planner_interval_seconds: float | None
    outbox: OpenClawAgendaBridgeConfig | None
    outbox_interval_seconds: float | None
    control: AgendaControlConfig | None
    backup_root: Path | None
    backup_interval_seconds: float | None
    thesis_impact: ThesisImpactProductionConfig | None
    thesis_impact_interval_seconds: float | None
    # P10h: reading throughput. It lives in the config rather than in the
    # installer's environment because a plain re-install must not quietly put
    # it back to the built-in default -- which is exactly what happened the
    # first time it was set.
    document_extraction_max_windows: int | None = None
    # P11n: how many of those windows may also be read for figures. Separate
    # because it is a separate spend, and zero because an install that has not
    # asked for the figures pass should not get it.
    document_extraction_numeric_windows: int | None = None
    # P11r: and how many may be read for the *names* of the measures the market
    # judges a company on. A third setting because it is a third spend, and
    # because it reads a different set of documents than the figures pass does.
    document_extraction_discovery_windows: int | None = None
    # P12d: the AlphaEngine safety cap. In the config so a plain re-install
    # keeps the owner's number, exactly as the window settings are.
    alphaengine_owner_call_cap: int | None = None
    # ADR-0009: the one key that can bring the retired plane back. Default
    # false, so a config that never heard of the retirement gets the
    # retirement.
    legacy_agenda_plane: bool = False
    workspace: WorkspaceBinding | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ServiceConfig":
        required = {
            "schema_version", "core_db", "scheduler_db", "projection_db",
            "model_router_db", "capability_catalog_db", "heartbeat_path",
            "writer_socket", "tick_seconds", "projection_min_interval_seconds",
            "plugin_retry_seconds", "plugins",
        }
        optional = {
            "agenda", "weekly_brief", "bounded_planner", "outbox", "control",
            "backup", "thesis_impact", "document_extraction",
            "alphaengine_owner_call_cap", LEGACY_AGENDA_PLANE_KEY, "workspace",
        }
        if not required.issubset(raw) or set(raw) - required - optional or raw.get("schema_version") != SCHEMA_VERSION:
            raise ServiceConfigError("service config has an invalid shape or schema version")
        plugin_rows = raw["plugins"]
        if not isinstance(plugin_rows, list):
            raise ServiceConfigError("plugins must be an array")
        plugins: list[StaticDashboardPlugin] = []
        for plugin_raw in plugin_rows:
            if not isinstance(plugin_raw, Mapping):
                raise ServiceConfigError("plugin config must be an object")
            if plugin_raw.get("type") != "static_dashboard":
                raise ServiceConfigError(f"unsupported built-in plugin: {plugin_raw.get('type')}")
            plugins.append(StaticDashboardPlugin.from_mapping(plugin_raw))
        if len({plugin.name for plugin in plugins}) != len(plugins):
            raise ServiceConfigError("plugin names must be unique")
        legacy_agenda_plane = raw.get(LEGACY_AGENDA_PLANE_KEY, False)
        if not isinstance(legacy_agenda_plane, bool):
            raise ServiceConfigError(f"{LEGACY_AGENDA_PLANE_KEY} must be boolean")
        agenda_config = None
        agenda_interval = None
        agenda_raw = raw.get("agenda")
        if agenda_raw is not None:
            if not isinstance(agenda_raw, Mapping) or set(agenda_raw) != {
                "enabled", "interval_seconds", "config",
            }:
                raise ServiceConfigError("agenda service config is invalid")
            if not isinstance(agenda_raw["enabled"], bool):
                raise ServiceConfigError("agenda.enabled must be boolean")
            # ADR-0009: ``enabled`` is no longer enough. The retired plane runs
            # only when the config also opts in by name, and the shape above is
            # still checked either way so a stale block stays legible.
            if agenda_raw["enabled"] and legacy_agenda_plane:
                if not isinstance(agenda_raw["config"], Mapping):
                    raise ServiceConfigError("agenda.config must be an object")
                try:
                    agenda_config = AgendaCoordinatorConfig.from_mapping(agenda_raw["config"])
                except Exception as exc:
                    raise ServiceConfigError("agenda coordinator config is invalid") from exc
                agenda_interval = _positive_number(agenda_raw["interval_seconds"], "agenda.interval_seconds")
        weekly_brief_config = None
        weekly_brief_interval = None
        weekly_brief_raw = raw.get("weekly_brief")
        if weekly_brief_raw is not None:
            if not isinstance(weekly_brief_raw, Mapping) or set(weekly_brief_raw) != {
                "enabled", "interval_seconds", "config",
            } or not isinstance(weekly_brief_raw["enabled"], bool):
                raise ServiceConfigError("weekly brief service config is invalid")
            if weekly_brief_raw["enabled"]:
                if not isinstance(weekly_brief_raw["config"], Mapping):
                    raise ServiceConfigError("weekly_brief.config must be an object")
                try:
                    weekly_brief_config = WeeklyBriefCoordinatorConfig.from_mapping(
                        weekly_brief_raw["config"]
                    )
                except Exception as exc:
                    raise ServiceConfigError(
                        "weekly brief coordinator config is invalid"
                    ) from exc
                weekly_brief_interval = _positive_number(
                    weekly_brief_raw["interval_seconds"],
                    "weekly_brief.interval_seconds",
                )
        bounded_planner_config = None
        bounded_planner_interval = None
        bounded_planner_raw = raw.get("bounded_planner")
        if bounded_planner_raw is not None:
            if not isinstance(bounded_planner_raw, Mapping) or set(bounded_planner_raw) != {
                "enabled", "interval_seconds", "config",
            } or not isinstance(bounded_planner_raw["enabled"], bool):
                raise ServiceConfigError("bounded planner service config is invalid")
            if bounded_planner_raw["enabled"]:
                if not isinstance(bounded_planner_raw["config"], Mapping):
                    raise ServiceConfigError("bounded_planner.config must be an object")
                try:
                    bounded_planner_config = BoundedPlannerDriverConfig.from_mapping(
                        dict(bounded_planner_raw["config"])
                    )
                except Exception as exc:
                    raise ServiceConfigError(
                        "bounded planner driver config is invalid"
                    ) from exc
                bounded_planner_interval = _positive_number(
                    bounded_planner_raw["interval_seconds"],
                    "bounded_planner.interval_seconds",
                )
        backup_root = None
        backup_interval = None
        backup_raw = raw.get("backup")
        if backup_raw is not None:
            if not isinstance(backup_raw, Mapping) or set(backup_raw) != {
                "enabled", "root", "interval_seconds",
            } or not isinstance(backup_raw["enabled"], bool):
                raise ServiceConfigError("backup service config is invalid")
            if backup_raw["enabled"]:
                backup_root = _absolute_path(backup_raw["root"], "backup.root")
                backup_interval = _positive_number(backup_raw["interval_seconds"], "backup.interval_seconds")
        outbox_config = None
        outbox_interval = None
        outbox_raw = raw.get("outbox")
        if outbox_raw is not None:
            if not isinstance(outbox_raw, Mapping) or set(outbox_raw) != {
                "enabled", "interval_seconds", "config",
            } or not isinstance(outbox_raw["enabled"], bool):
                raise ServiceConfigError("outbox service config is invalid")
            if outbox_raw["enabled"]:
                if not isinstance(outbox_raw["config"], Mapping):
                    raise ServiceConfigError("outbox.config must be an object")
                try:
                    outbox_config = OpenClawAgendaBridgeConfig.from_mapping(outbox_raw["config"])
                except Exception as exc:
                    raise ServiceConfigError("OpenClaw agenda bridge config is invalid") from exc
                outbox_interval = _positive_number(
                    outbox_raw["interval_seconds"], "outbox.interval_seconds"
                )
        if weekly_brief_config is not None:
            if outbox_config is None:
                raise ServiceConfigError(
                    "enabled weekly brief coordinator requires the outbox bridge"
                )
            if (
                weekly_brief_config.plan.destination_ref
                != outbox_config.endpoint_ref
            ):
                raise ServiceConfigError(
                    "weekly brief destination must match the outbox endpoint"
                )
            if outbox_config.weekly_brief_attachment_dir is None:
                raise ServiceConfigError(
                    "weekly brief delivery requires an attachment directory"
                )
        control_config = None
        control_raw = raw.get("control")
        if control_raw is not None:
            if (
                not isinstance(control_raw, Mapping)
                or set(control_raw) != {"enabled", "config"}
                or not isinstance(control_raw["enabled"], bool)
            ):
                raise ServiceConfigError("Agenda control service config is invalid")
            if control_raw["enabled"]:
                if not isinstance(control_raw["config"], Mapping):
                    raise ServiceConfigError("control.config must be an object")
                try:
                    control_config = AgendaControlConfig.from_mapping(control_raw["config"])
                except Exception as exc:
                    raise ServiceConfigError("Agenda control config is invalid") from exc
        thesis_impact_config = None
        thesis_impact_interval = None
        thesis_impact_raw = raw.get("thesis_impact")
        if thesis_impact_raw is not None:
            if (
                not isinstance(thesis_impact_raw, Mapping)
                or set(thesis_impact_raw) != {"enabled", "interval_seconds", "config"}
                or not isinstance(thesis_impact_raw["enabled"], bool)
            ):
                raise ServiceConfigError("thesis-impact service config is invalid")
            if thesis_impact_raw["enabled"]:
                if not isinstance(thesis_impact_raw["config"], Mapping):
                    raise ServiceConfigError(
                        "thesis-impact.config must be an object"
                    )
                try:
                    thesis_impact_config = ThesisImpactProductionConfig.from_mapping(
                        thesis_impact_raw["config"]
                    )
                except Exception as exc:
                    raise ServiceConfigError(
                        "thesis-impact production config is invalid"
                    ) from exc
                thesis_impact_interval = _positive_number(
                    thesis_impact_raw["interval_seconds"],
                    "thesis_impact.interval_seconds",
                )
        extraction_max_windows = None
        extraction_numeric_windows = None
        extraction_discovery_windows = None
        owner_call_cap = raw.get("alphaengine_owner_call_cap")
        if owner_call_cap is not None and (
            isinstance(owner_call_cap, bool) or not isinstance(owner_call_cap, int)
            or not 1 <= owner_call_cap <= 2000
        ):
            raise ServiceConfigError("alphaengine_owner_call_cap must be an integer 1..2000")
        extraction_raw = raw.get("document_extraction")
        if extraction_raw is not None:
            if not isinstance(extraction_raw, Mapping) or not set(extraction_raw) <= {
                "max_windows_per_tick", "numeric_windows_per_tick",
                "discovery_windows_per_tick",
            } or "max_windows_per_tick" not in extraction_raw:
                raise ServiceConfigError("document_extraction service config is invalid")
            value = extraction_raw["max_windows_per_tick"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
                raise ServiceConfigError(
                    "document_extraction.max_windows_per_tick must be an integer 1..50"
                )
            extraction_max_windows = value
            numeric = extraction_raw.get("numeric_windows_per_tick")
            if numeric is not None:
                if isinstance(numeric, bool) or not isinstance(numeric, int) or not 0 <= numeric <= 50:
                    raise ServiceConfigError(
                        "document_extraction.numeric_windows_per_tick must be an integer 0..50"
                    )
                extraction_numeric_windows = numeric
            discovery = extraction_raw.get("discovery_windows_per_tick")
            if discovery is not None:
                if isinstance(discovery, bool) or not isinstance(discovery, int) or not 0 <= discovery <= 50:
                    raise ServiceConfigError(
                        "document_extraction.discovery_windows_per_tick must be an integer 0..50"
                    )
                extraction_discovery_windows = discovery
        workspace = None
        workspace_raw = raw.get("workspace")
        if workspace_raw is not None:
            if not isinstance(workspace_raw, Mapping) or set(workspace_raw) != {
                    "workspace_id", "manifest_path", "manifest_hash"}:
                raise ServiceConfigError("workspace binding has an invalid closed shape")
            import uuid
            try:
                identity = str(uuid.UUID(workspace_raw["workspace_id"]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ServiceConfigError("workspace.workspace_id must be a canonical UUID") from exc
            if identity != workspace_raw["workspace_id"]:
                raise ServiceConfigError("workspace.workspace_id must be a canonical UUID")
            digest = workspace_raw["manifest_hash"]
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ServiceConfigError("workspace.manifest_hash must be lowercase SHA-256")
            workspace = WorkspaceBinding(
                identity, _absolute_path(workspace_raw["manifest_path"],
                                         "workspace.manifest_path").resolve(), digest)
        return cls(
            document_extraction_max_windows=extraction_max_windows,
            document_extraction_numeric_windows=extraction_numeric_windows,
            document_extraction_discovery_windows=extraction_discovery_windows,
            alphaengine_owner_call_cap=owner_call_cap,
            legacy_agenda_plane=legacy_agenda_plane,
            core_db=_absolute_path(raw["core_db"], "core_db"),
            scheduler_db=_absolute_path(raw["scheduler_db"], "scheduler_db"),
            projection_db=_absolute_path(raw["projection_db"], "projection_db"),
            model_router_db=_absolute_path(raw["model_router_db"], "model_router_db", nullable=True),
            capability_catalog_db=_absolute_path(
                raw["capability_catalog_db"], "capability_catalog_db", nullable=True
            ),
            heartbeat_path=_absolute_path(raw["heartbeat_path"], "heartbeat_path"),
            writer_socket=_absolute_path(raw["writer_socket"], "writer_socket"),
            workspace=workspace,
            tick_seconds=_positive_number(raw["tick_seconds"], "tick_seconds"),
            projection_min_interval_seconds=_positive_number(
                raw["projection_min_interval_seconds"], "projection_min_interval_seconds"
            ),
            plugin_retry_seconds=_positive_number(raw["plugin_retry_seconds"], "plugin_retry_seconds"),
            plugins=tuple(plugins),
            agenda=agenda_config,
            agenda_interval_seconds=agenda_interval,
            weekly_brief=weekly_brief_config,
            weekly_brief_interval_seconds=weekly_brief_interval,
            bounded_planner=bounded_planner_config,
            bounded_planner_interval_seconds=bounded_planner_interval,
            outbox=outbox_config,
            outbox_interval_seconds=outbox_interval,
            control=control_config,
            backup_root=backup_root,
            backup_interval_seconds=backup_interval,
            thesis_impact=thesis_impact_config,
            thesis_impact_interval_seconds=thesis_impact_interval,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "ServiceConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            mode = stat.S_IMODE(config_path.stat().st_mode)
            if mode & 0o022:
                raise ServiceConfigError("service config must not be group/world writable")
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except ServiceConfigError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ServiceConfigError("service config is unavailable or invalid") from exc
        if not isinstance(raw, Mapping):
            raise ServiceConfigError("service config must be an object")
        parsed = cls.from_mapping(raw)
        if parsed.workspace is not None:
            from .workspace import (
                WorkspaceError, load_workspace_manifest, validate_service_mapping_paths,
            )
            try:
                workspace = load_workspace_manifest(parsed.workspace.manifest_path)
            except WorkspaceError as exc:
                raise ServiceConfigError("workspace manifest is invalid") from exc
            if (workspace.workspace_id != parsed.workspace.workspace_id
                    or workspace.content_hash != parsed.workspace.manifest_hash
                    or workspace.manifest_path != parsed.workspace.manifest_path
                    or workspace.config_path != config_path
                    or workspace.state_dir / "core.sqlite" != parsed.core_db
                    or workspace.state_dir / "scheduler.sqlite" != parsed.scheduler_db
                    or workspace.state_dir / "dashboard-projection.sqlite" != parsed.projection_db
                    or workspace.state_dir / "model-router.sqlite" != parsed.model_router_db
                    or workspace.writer_socket != parsed.writer_socket
                    or workspace.state_dir / "run" / "heartbeat.json" != parsed.heartbeat_path):
                raise ServiceConfigError(
                    "service config paths do not match the bound workspace manifest")
            if parsed.control is not None and parsed.control.port != workspace.cockpit_port:
                raise ServiceConfigError(
                    "Cockpit port does not match the bound workspace manifest")
            try:
                validate_service_mapping_paths(raw, workspace)
            except WorkspaceError as exc:
                raise ServiceConfigError("service config contains an undeclared external path") from exc
        return parsed


def _file_signature(path: Path | None) -> tuple[Any, ...]:
    if path is None:
        return (None,)
    values: list[Any] = [str(path)]
    for candidate in (path, Path(f"{path}-wal")):
        try:
            info = candidate.stat()
            values.extend((info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            values.extend((None, None))
    return tuple(values)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class DaltonService:
    """Deterministic controller loop with fail-open projection plugins."""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self._stop = threading.Event()
        self._scheduler: Scheduler | None = None
        self._started_at = _utc_now()
        self._last_tick_at: str | None = None
        self._last_sweep_at: str | None = None
        self._last_projection_at: str | None = None
        self._last_projection_monotonic = 0.0
        self._projection_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._projection_future: concurrent.futures.Future[
            tuple[dict[str, Any], tuple[Any, ...]]
        ] | None = None
        self._projection_error: str | None = None
        self._last_source_signature: tuple[Any, ...] | None = None
        self._projection_watermark: str | None = None
        self._expired_lease_count = 0
        self._last_error: str | None = None
        self._plugin_states: dict[str, dict[str, Any]] = {
            plugin.name: {
                "state": "pending", "last_attempt_at": None, "last_success_at": None,
                "last_error": None, "result": None, "retry_at_monotonic": 0.0,
            }
            for plugin in config.plugins
        }
        self._plugin_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._plugin_futures: dict[
            str, concurrent.futures.Future[dict[str, Any]]
        ] = {}
        self._agenda = None if config.agenda is None else AgendaCoordinator(config.agenda)
        self._agenda_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._agenda_future: concurrent.futures.Future[dict[str, Any]] | None = None
        self._agenda_last_launch_monotonic = 0.0
        self._agenda_state: dict[str, Any] = {
            # ADR-0009: "retired" rather than "disabled" when the opt-in is
            # absent, so the heartbeat says the plane is gone rather than
            # merely switched off this run.
            "state": (
                "pending" if self._agenda is not None
                else "disabled" if config.legacy_agenda_plane
                else "retired"
            ),
            "last_started_at": None, "last_completed_at": None,
            "last_result": None, "last_error": None,
        }
        self._weekly_brief = (
            None if config.weekly_brief is None
            else WeeklyBriefCoordinator(config.weekly_brief)
        )
        self._weekly_brief_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._weekly_brief_future: concurrent.futures.Future[dict[str, Any]] | None = None
        self._weekly_brief_last_launch_monotonic = 0.0
        self._weekly_brief_state: dict[str, Any] = {
            "state": "disabled" if self._weekly_brief is None else "pending",
            "last_started_at": None, "last_completed_at": None,
            "last_result": None, "last_error": None,
        }
        self._bounded_planner = (
            None if config.bounded_planner is None
            else BoundedPlannerDriver(config.bounded_planner)
        )
        self._bounded_planner_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._bounded_planner_future: concurrent.futures.Future[dict[str, Any]] | None = None
        self._bounded_planner_last_launch_monotonic = 0.0
        self._bounded_planner_state: dict[str, Any] = {
            "state": "disabled" if self._bounded_planner is None else "pending",
            "last_started_at": None, "last_completed_at": None,
            "last_result": None, "last_error": None,
        }
        self._outbox = None if config.outbox is None else OpenClawAgendaBridge(config.outbox)
        self._outbox_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._outbox_future: concurrent.futures.Future[dict[str, Any]] | None = None
        self._outbox_last_launch_monotonic = 0.0
        self._outbox_state: dict[str, Any] = {
            "state": "disabled" if self._outbox is None else "pending",
            "last_started_at": None, "last_completed_at": None,
            "last_result": None, "last_error": None,
        }
        self._backup = None if config.backup_root is None else DatabaseBackupManager(
            config.backup_root,
            {
                "core": config.core_db,
                "scheduler": config.scheduler_db,
                **({"model-router": config.model_router_db} if config.model_router_db else {}),
            },
        )
        self._last_backup_monotonic = 0.0
        self._backup_state: dict[str, Any] = {
            "state": "disabled" if self._backup is None else "pending",
            "last_success_at": None, "last_snapshot_id": None, "last_error": None,
        }

    def _sources(self) -> tuple[Any, ...]:
        return (
            _file_signature(self.config.core_db),
            _file_signature(self.config.scheduler_db),
            _file_signature(self.config.model_router_db),
            _file_signature(self.config.capability_catalog_db),
        )

    def start(self) -> None:
        if self._scheduler is not None:
            return
        self.config.scheduler_db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._scheduler = Scheduler(self.config.scheduler_db)
        if not self.config.legacy_agenda_plane:
            # ADR-0009: one line, once, at start. launchd captures stderr, so
            # the log says why no agenda cycle will ever appear again.
            print(LEGACY_AGENDA_RETIRED_LINE, file=sys.stderr, flush=True)
        if self._agenda is not None:
            self._agenda_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dalton-agenda"
            )
        if self._weekly_brief is not None:
            self._weekly_brief_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dalton-weekly-brief"
            )
        if self._bounded_planner is not None:
            self._bounded_planner_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dalton-bounded-planner"
            )
        if self._outbox is not None:
            self._outbox_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dalton-outbox"
            )
        if self.config.plugins:
            # Rendering and publishing may include bounded remote reads.  They
            # must not prevent the controller loop from renewing its heartbeat.
            self._plugin_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dalton-static-dashboard"
            )
        self._projection_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dalton-dashboard-projection"
        )
        self._write_heartbeat("starting")

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        projection_executor, self._projection_executor = self._projection_executor, None
        if projection_executor is not None:
            projection_executor.shutdown(wait=True, cancel_futures=True)
        plugin_executor, self._plugin_executor = self._plugin_executor, None
        if plugin_executor is not None:
            plugin_executor.shutdown(wait=True, cancel_futures=True)
        outbox_executor, self._outbox_executor = self._outbox_executor, None
        if outbox_executor is not None:
            outbox_executor.shutdown(wait=False, cancel_futures=True)
        weekly_executor, self._weekly_brief_executor = (
            self._weekly_brief_executor, None
        )
        if weekly_executor is not None:
            weekly_executor.shutdown(wait=False, cancel_futures=True)
        planner_executor, self._bounded_planner_executor = (
            self._bounded_planner_executor, None
        )
        if planner_executor is not None:
            planner_executor.shutdown(wait=False, cancel_futures=True)
        executor, self._agenda_executor = self._agenda_executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            scheduler.close()

    def _poll_agenda(self) -> None:
        if self._agenda is None:
            return
        future = self._agenda_future
        if future is not None and future.done():
            self._agenda_future = None
            self._agenda_state["last_completed_at"] = _utc_now()
            try:
                result = future.result()
            except Exception as exc:
                self._agenda_state.update(
                    state="error", last_error=f"{type(exc).__name__}: {exc}", last_result=None
                )
            else:
                self._agenda_state.update(
                    state=str(result.get("status", "ready")), last_error=None, last_result=result
                )
        interval = self.config.agenda_interval_seconds
        executor = self._agenda_executor
        if self._agenda_future is None and interval is not None and executor is not None:
            elapsed = time.monotonic() - self._agenda_last_launch_monotonic
            if self._agenda_last_launch_monotonic == 0.0 or elapsed >= interval:
                self._agenda_last_launch_monotonic = time.monotonic()
                self._agenda_state.update(state="running", last_started_at=_utc_now(), last_error=None)
                self._agenda_future = executor.submit(self._agenda.run_once)

    def _poll_outbox(self) -> None:
        if self._outbox is None:
            return
        future = self._outbox_future
        if future is not None and future.done():
            self._outbox_future = None
            self._outbox_state["last_completed_at"] = _utc_now()
            try:
                result = future.result()
            except Exception as exc:
                self._outbox_state.update(
                    state="error", last_error=f"{type(exc).__name__}: {exc}", last_result=None
                )
            else:
                self._outbox_state.update(
                    state=str(result.get("status", "ready")), last_error=None, last_result=result
                )
        interval = self.config.outbox_interval_seconds
        executor = self._outbox_executor
        if self._outbox_future is None and interval is not None and executor is not None:
            elapsed = time.monotonic() - self._outbox_last_launch_monotonic
            if self._outbox_last_launch_monotonic == 0.0 or elapsed >= interval:
                self._outbox_last_launch_monotonic = time.monotonic()
                self._outbox_state.update(state="running", last_started_at=_utc_now(), last_error=None)
                self._outbox_future = executor.submit(self._outbox.run_once)

    def _poll_weekly_brief(self) -> None:
        if self._weekly_brief is None:
            return
        future = self._weekly_brief_future
        if future is not None and future.done():
            self._weekly_brief_future = None
            self._weekly_brief_state["last_completed_at"] = _utc_now()
            try:
                result = future.result()
            except Exception as exc:
                self._weekly_brief_state.update(
                    state="error", last_error=f"{type(exc).__name__}: {exc}",
                    last_result=None,
                )
            else:
                self._weekly_brief_state.update(
                    state=str(result.get("status", "ready")),
                    last_error=None, last_result=result,
                )
        interval = self.config.weekly_brief_interval_seconds
        executor = self._weekly_brief_executor
        if (
            self._weekly_brief_future is None
            and interval is not None
            and executor is not None
        ):
            elapsed = time.monotonic() - self._weekly_brief_last_launch_monotonic
            if self._weekly_brief_last_launch_monotonic == 0.0 or elapsed >= interval:
                self._weekly_brief_last_launch_monotonic = time.monotonic()
                self._weekly_brief_state.update(
                    state="running", last_started_at=_utc_now(), last_error=None
                )
                self._weekly_brief_future = executor.submit(
                    self._weekly_brief.run_once
                )

    def _poll_bounded_planner(self) -> None:
        if self._bounded_planner is None:
            return
        future = self._bounded_planner_future
        if future is not None and future.done():
            self._bounded_planner_future = None
            self._bounded_planner_state["last_completed_at"] = _utc_now()
            try:
                result = future.result()
            except Exception as exc:
                self._bounded_planner_state.update(
                    state="error", last_error=f"{type(exc).__name__}: {exc}",
                    last_result=None,
                )
            else:
                self._bounded_planner_state.update(
                    state=str(result.get("status", "completed")),
                    last_error=None, last_result=result,
                )
        interval = self.config.bounded_planner_interval_seconds
        executor = self._bounded_planner_executor
        if (
            self._bounded_planner_future is None
            and interval is not None
            and executor is not None
        ):
            elapsed = time.monotonic() - self._bounded_planner_last_launch_monotonic
            if self._bounded_planner_last_launch_monotonic == 0.0 or elapsed >= interval:
                self._bounded_planner_last_launch_monotonic = time.monotonic()
                self._bounded_planner_state.update(
                    state="running", last_started_at=_utc_now(), last_error=None
                )
                self._bounded_planner_future = executor.submit(
                    self._bounded_planner.run_once
                )

    def _run_backup(self) -> None:
        if self._backup is None or self.config.backup_interval_seconds is None:
            return
        elapsed = time.monotonic() - self._last_backup_monotonic
        if self._last_backup_monotonic != 0.0 and elapsed < self.config.backup_interval_seconds:
            return
        try:
            manifest = self._backup.snapshot()
        except Exception as exc:
            self._backup_state.update(state="error", last_error=f"{type(exc).__name__}: {exc}")
            return
        self._last_backup_monotonic = time.monotonic()
        self._backup_state.update(
            state="ready", last_success_at=_utc_now(),
            last_snapshot_id=manifest["snapshot_id"], last_error=None,
        )

    def _build_projection(
        self, source_signature: tuple[Any, ...]
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        snapshot = project_dashboard(
            self.config.core_db,
            self.config.scheduler_db,
            self.config.projection_db,
            capability_catalog_db=self.config.capability_catalog_db,
            model_router_db=self.config.model_router_db,
        )
        return snapshot, source_signature

    def _project(self, source_signature: tuple[Any, ...] | None = None) -> None:
        if self._projection_future is not None:
            return
        executor = self._projection_executor
        if executor is None:
            raise RuntimeError("dashboard projection executor is unavailable")
        signature_before = source_signature or self._sources()
        self._last_projection_monotonic = time.monotonic()
        self._projection_future = executor.submit(
            self._build_projection, signature_before
        )

    def _poll_projection(self) -> None:
        future = self._projection_future
        if future is None or not future.done():
            return
        self._projection_future = None
        try:
            snapshot, signature_before = future.result()
        except Exception as exc:
            self._projection_error = f"{type(exc).__name__}: {exc}"
            return
        self._projection_error = None
        self._last_projection_at = _utc_now()
        self._projection_watermark = snapshot["metadata"]["source_watermark"]
        # Keep the pre-build signature.  If an authority changes while the
        # projector is reading, the next tick sees a mismatch and rebuilds;
        # recording the post-build signature could silently miss that write.
        self._last_source_signature = signature_before
        for plugin in self.config.plugins:
            self._run_plugin(plugin)

    def _run_plugin(self, plugin: StaticDashboardPlugin) -> None:
        if plugin.name in self._plugin_futures:
            return
        state = self._plugin_states[plugin.name]
        state["last_attempt_at"] = _utc_now()
        # Keep an already published dashboard healthy while its replacement is
        # prepared.  The completed future below atomically advances success or
        # exposes the refresh error on a later controller tick.
        if state["state"] == "pending":
            state["state"] = "running"
        executor = self._plugin_executor
        if executor is None:
            raise RuntimeError("static dashboard executor is unavailable")
        self._plugin_futures[plugin.name] = executor.submit(
            plugin.on_projection, self.config.projection_db
        )

    def _poll_plugins(self) -> None:
        for name, future in tuple(self._plugin_futures.items()):
            if not future.done():
                continue
            del self._plugin_futures[name]
            state = self._plugin_states[name]
            try:
                result = future.result()
            except Exception as exc:
                state.update(
                    state="error", last_error=f"{type(exc).__name__}: {exc}",
                    retry_at_monotonic=(
                        time.monotonic() + self.config.plugin_retry_seconds
                    ),
                )
            else:
                state.update(
                    state="ready", last_success_at=_utc_now(), last_error=None,
                    result=result, retry_at_monotonic=0.0,
                )

    def _retry_plugins(self) -> None:
        self._poll_plugins()
        if not self.config.projection_db.exists():
            return
        now = time.monotonic()
        for plugin in self.config.plugins:
            state = self._plugin_states[plugin.name]
            if state["state"] == "error" and now >= state["retry_at_monotonic"]:
                self._run_plugin(plugin)

    def run_once(
        self, *, force_projection: bool = False, wait_for_projection: bool = False
    ) -> dict[str, Any]:
        self.start()
        assert self._scheduler is not None
        expired = self._scheduler.sweep_expired()
        self._expired_lease_count += len(expired)
        self._last_sweep_at = _utc_now()
        self._poll_agenda()
        self._poll_weekly_brief()
        self._poll_bounded_planner()
        self._poll_outbox()
        self._run_backup()
        self._poll_projection()
        self._poll_plugins()
        current_signature = self._sources()
        elapsed = time.monotonic() - self._last_projection_monotonic
        should_project = (
            force_projection
            or self._last_source_signature is None
            or current_signature != self._last_source_signature
        ) and (
            force_projection
            or self._last_projection_monotonic == 0.0
            or elapsed >= self.config.projection_min_interval_seconds
        ) and self._projection_future is None and not self._plugin_futures
        if should_project:
            self._project(current_signature)
        elif self._projection_future is None:
            self._retry_plugins()
        if wait_for_projection:
            future = self._projection_future
            if future is not None:
                concurrent.futures.wait((future,))
                self._poll_projection()
            plugin_futures = tuple(self._plugin_futures.values())
            if plugin_futures:
                concurrent.futures.wait(plugin_futures)
            self._poll_plugins()
        self._last_tick_at = _utc_now()
        self._last_error = self._projection_error
        degraded = self._projection_error is not None or any(
            plugin["state"] == "error" for plugin in self._plugin_states.values()
        ) or self._agenda_state["state"] == "error" or self._weekly_brief_state[
            "state"
        ] == "error" or self._outbox_state["state"] in {"error", "degraded"}
        state = "degraded" if degraded else "running"
        return self._write_heartbeat(state)

    def _heartbeat(self, state: str) -> dict[str, Any]:
        plugin_states = {
            name: {key: value for key, value in details.items() if key != "retry_at_monotonic"}
            for name, details in self._plugin_states.items()
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "service": "daltond",
            "state": state,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "last_tick_at": self._last_tick_at,
            "last_sweep_at": self._last_sweep_at,
            "expired_lease_count": self._expired_lease_count,
            "last_projection_at": self._last_projection_at,
            "projection_watermark": self._projection_watermark,
            "projection": {
                "state": (
                    "running" if self._projection_future is not None
                    else "error" if self._projection_error is not None
                    else "ready" if self._projection_watermark is not None
                    else "pending"
                ),
                "last_error": self._projection_error,
            },
            "writer_socket_present": self.config.writer_socket.exists(),
            "plugins": plugin_states,
            "agenda": dict(self._agenda_state),
            "weekly_brief": dict(self._weekly_brief_state),
            "bounded_planner": dict(self._bounded_planner_state),
            "outbox": dict(self._outbox_state),
            "backup": dict(self._backup_state),
            "last_error": self._last_error,
        }

    def _write_heartbeat(self, state: str) -> dict[str, Any]:
        value = self._heartbeat(state)
        _atomic_json(self.config.heartbeat_path, value)
        return value

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as exc:
                    self._last_tick_at = _utc_now()
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._write_heartbeat("degraded")
                self._stop.wait(self.config.tick_seconds)
        finally:
            self._write_heartbeat("stopping")
            self.close()


def main(argv: Iterable[str] | None = None) -> int:
    from .controller_singleton import ControllerConflict, ControllerOwnership

    parser = argparse.ArgumentParser(description="Run the persistent Dalton controller")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="run one forced maintenance cycle")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        validate_runtime_context(config_path=args.config)
        with ControllerOwnership(args.config):
            config = ServiceConfig.from_file(args.config)
            service = DaltonService(config)
            if args.once:
                try:
                    result = service.run_once(
                        force_projection=True, wait_for_projection=True
                    )
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0 if result["state"] == "running" else 1
                finally:
                    service.close()
            signal.signal(signal.SIGTERM, lambda _signum, _frame: service.stop())
            signal.signal(signal.SIGINT, lambda _signum, _frame: service.stop())
            service.serve_forever()
            return 0
    except ControllerConflict as exc:
        print(json.dumps({"status": "refused", "reason": exc.reason,
                          "controller_pids": exc.pids}, sort_keys=True), file=sys.stderr)
        return 1
    except WorkspaceRuntimeError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
