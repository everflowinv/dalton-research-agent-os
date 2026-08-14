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
import signal
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .dashboard_projector import project_dashboard
from .plugins.static_dashboard import StaticDashboardPlugin
from .scheduler import Scheduler


SCHEMA_VERSION = "0.1"


class ServiceConfigError(RuntimeError):
    pass


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

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ServiceConfig":
        expected = {
            "schema_version", "core_db", "scheduler_db", "projection_db",
            "model_router_db", "capability_catalog_db", "heartbeat_path",
            "writer_socket", "tick_seconds", "projection_min_interval_seconds",
            "plugin_retry_seconds", "plugins",
        }
        if set(raw) != expected or raw.get("schema_version") != SCHEMA_VERSION:
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
        return cls(
            core_db=_absolute_path(raw["core_db"], "core_db"),
            scheduler_db=_absolute_path(raw["scheduler_db"], "scheduler_db"),
            projection_db=_absolute_path(raw["projection_db"], "projection_db"),
            model_router_db=_absolute_path(raw["model_router_db"], "model_router_db", nullable=True),
            capability_catalog_db=_absolute_path(
                raw["capability_catalog_db"], "capability_catalog_db", nullable=True
            ),
            heartbeat_path=_absolute_path(raw["heartbeat_path"], "heartbeat_path"),
            writer_socket=_absolute_path(raw["writer_socket"], "writer_socket"),
            tick_seconds=_positive_number(raw["tick_seconds"], "tick_seconds"),
            projection_min_interval_seconds=_positive_number(
                raw["projection_min_interval_seconds"], "projection_min_interval_seconds"
            ),
            plugin_retry_seconds=_positive_number(raw["plugin_retry_seconds"], "plugin_retry_seconds"),
            plugins=tuple(plugins),
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
        return cls.from_mapping(raw)


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
        self._write_heartbeat("starting")

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            scheduler.close()

    def _project(self, source_signature: tuple[Any, ...] | None = None) -> None:
        signature_before = source_signature or self._sources()
        snapshot = project_dashboard(
            self.config.core_db,
            self.config.scheduler_db,
            self.config.projection_db,
            capability_catalog_db=self.config.capability_catalog_db,
            model_router_db=self.config.model_router_db,
        )
        now = _utc_now()
        self._last_projection_at = now
        self._last_projection_monotonic = time.monotonic()
        self._projection_watermark = snapshot["metadata"]["source_watermark"]
        # Keep the pre-build signature.  If an authority changes while the
        # projector is reading, the next tick sees a mismatch and rebuilds;
        # recording the post-build signature could silently miss that write.
        self._last_source_signature = signature_before
        for plugin in self.config.plugins:
            self._run_plugin(plugin)

    def _run_plugin(self, plugin: StaticDashboardPlugin) -> None:
        state = self._plugin_states[plugin.name]
        state["last_attempt_at"] = _utc_now()
        try:
            result = plugin.on_projection(self.config.projection_db)
        except Exception as exc:
            state.update(
                state="error",
                last_error=f"{type(exc).__name__}: {exc}",
                retry_at_monotonic=time.monotonic() + self.config.plugin_retry_seconds,
            )
            return
        state.update(
            state="ready",
            last_success_at=_utc_now(),
            last_error=None,
            result=result,
            retry_at_monotonic=0.0,
        )

    def _retry_plugins(self) -> None:
        if not self.config.projection_db.exists():
            return
        now = time.monotonic()
        for plugin in self.config.plugins:
            state = self._plugin_states[plugin.name]
            if state["state"] == "error" and now >= state["retry_at_monotonic"]:
                self._run_plugin(plugin)

    def run_once(self, *, force_projection: bool = False) -> dict[str, Any]:
        self.start()
        assert self._scheduler is not None
        expired = self._scheduler.sweep_expired()
        self._expired_lease_count += len(expired)
        self._last_sweep_at = _utc_now()
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
        )
        if should_project:
            self._project(current_signature)
        else:
            self._retry_plugins()
        self._last_tick_at = _utc_now()
        self._last_error = None
        state = "degraded" if any(
            plugin["state"] == "error" for plugin in self._plugin_states.values()
        ) else "running"
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
            "writer_socket_present": self.config.writer_socket.exists(),
            "plugins": plugin_states,
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
    parser = argparse.ArgumentParser(description="Run the persistent Dalton controller")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="run one forced maintenance cycle")
    args = parser.parse_args(list(argv) if argv is not None else None)
    config = ServiceConfig.from_file(args.config)
    service = DaltonService(config)
    if args.once:
        try:
            result = service.run_once(force_projection=True)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["state"] == "running" else 1
        finally:
            service.close()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: service.stop())
    signal.signal(signal.SIGINT, lambda _signum, _frame: service.stop())
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
