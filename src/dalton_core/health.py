"""Health probe for the Dalton LaunchAgent deployment."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .service import ServiceConfig


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def check(config_path: str | Path, *, max_age_seconds: float | None = None) -> dict[str, Any]:
    config = ServiceConfig.from_file(config_path)
    checks: dict[str, Any] = {}
    try:
        heartbeat = json.loads(config.heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        heartbeat = None
    checks["heartbeat_present"] = isinstance(heartbeat, dict)
    checks["controller_state_running"] = (
        isinstance(heartbeat, dict) and heartbeat.get("state") == "running"
    )
    pid = heartbeat.get("pid") if isinstance(heartbeat, dict) else None
    pid_alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            pid_alive = True
        except OSError:
            pass
    checks["controller_pid_alive"] = pid_alive
    age_limit = max_age_seconds or max(30.0, config.tick_seconds * 6)
    last_tick = _parse_time(heartbeat.get("last_tick_at")) if isinstance(heartbeat, dict) else None
    age = None if last_tick is None else (datetime.now(timezone.utc) - last_tick).total_seconds()
    checks["heartbeat_fresh"] = age is not None and 0 <= age <= age_limit
    checks["heartbeat_age_seconds"] = age
    socket_ready = False
    try:
        socket_ready = stat.S_ISSOCK(config.writer_socket.stat().st_mode)
        if socket_ready:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(str(config.writer_socket))
            finally:
                probe.close()
    except OSError:
        socket_ready = False
    checks["writer_socket_ready"] = socket_ready
    control_ready = True
    if config.control is not None:
        control_ready = False
        try:
            with socket.create_connection(
                (config.control.host, config.control.port), timeout=1.0
            ):
                control_ready = True
        except OSError:
            pass
    checks["control_socket_ready"] = control_ready
    checks["core_db_present"] = config.core_db.is_file()
    checks["scheduler_db_present"] = config.scheduler_db.is_file()
    checks["projection_present"] = config.projection_db.is_file()
    plugin_ok = isinstance(heartbeat, dict) and all(
        item.get("state") == "ready" for item in heartbeat.get("plugins", {}).values()
    )
    checks["plugins_ready"] = plugin_ok
    required = (
        "heartbeat_present", "controller_state_running", "controller_pid_alive", "heartbeat_fresh",
        "writer_socket_ready", "core_db_present", "scheduler_db_present",
        "projection_present", "plugins_ready", "control_socket_ready",
    )
    return {
        "ok": all(checks[name] for name in required),
        "state": heartbeat.get("state") if isinstance(heartbeat, dict) else "missing",
        "checks": checks,
        "heartbeat": heartbeat,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Dalton service deployment")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=float)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = check(args.config, max_age_seconds=args.max_age_seconds)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
