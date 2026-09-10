"""Single-controller ownership for one Dalton service configuration."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


class ControllerConflict(RuntimeError):
    def __init__(self, pids: list[int], reason: str = "controller_already_running") -> None:
        super().__init__(reason)
        self.pids = pids
        self.reason = reason


def _controller_command(command: str, comm: str, config: Path) -> bool:
    """Match only a controller executable whose final argument is this config."""

    ending = f" --config {config}"
    if not command.endswith(ending):
        return False
    executable = Path(comm).name.lower()
    prefix = command[:-len(ending)]
    if executable == "daltond" or prefix.endswith("/daltond") or prefix == "daltond":
        return prefix.endswith("daltond")
    if prefix.endswith(" -m dalton_core.service"):
        return prefix.endswith(" -m dalton_core.service")
    return False


def resident_controllers(
    config: str | Path, *, self_pid: int | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[int]:
    """Return live controller PIDs whose argv names the exact same config."""

    target = Path(config).expanduser().resolve()
    try:
        completed = run(
            ["ps", "-axo", "pid=,comm=,command="], capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"controller_process_scan_failed: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "controller_process_scan_failed: " + completed.stderr.strip()[:300]
        )
    mine = os.getpid() if self_pid is None else self_pid
    found: list[int] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == mine:
            continue
        if _controller_command(parts[2], parts[1], target):
            found.append(pid)
    return sorted(set(found))


def heartbeat_path(config: str | Path) -> Path:
    path = Path(config).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"controller_config_unreadable: {exc}") from exc
    value = raw.get("heartbeat_path") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RuntimeError("controller_config_has_no_absolute_heartbeat_path")
    return Path(value).resolve()


class ControllerOwnership:
    """A lifetime advisory lock plus compatibility check for pre-lock daemons."""

    def __init__(self, config: str | Path) -> None:
        self.config = Path(config).expanduser().resolve()
        heartbeat = heartbeat_path(self.config)
        self.lock_path = heartbeat.with_name(heartbeat.name + ".controller.lock")
        self._file: Any = None

    def __enter__(self) -> "ControllerOwnership":
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            residents = resident_controllers(self.config)
            if residents:
                raise ControllerConflict(residents, "legacy_controller_already_running")
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "config": str(self.config)}) + "\n")
            handle.flush()
            self._file = handle
            return self
        except BlockingIOError as exc:
            handle.close()
            raise ControllerConflict([], "controller_lock_held") from exc
        except Exception:
            handle.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def check(config: str | Path) -> dict[str, Any]:
    """Read-only pre-install check; deliberately does not create the lock."""

    heartbeat_path(config)  # validate the same config shape the daemon uses
    pids = resident_controllers(config)
    if pids:
        raise ControllerConflict(pids, "legacy_controller_already_running")
    return {"status": "clear", "config": str(Path(config).expanduser().resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.config)
    except ControllerConflict as exc:
        print(json.dumps({"status": "refused", "reason": exc.reason,
                          "controller_pids": exc.pids}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed when ownership cannot be established
        print(json.dumps({"status": "error", "reason": str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
