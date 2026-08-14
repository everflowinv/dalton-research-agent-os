"""Render scoped macOS LaunchAgents for Dalton writer and controller."""

from __future__ import annotations

import argparse
import os
import plistlib
import tempfile
from pathlib import Path
from typing import Any, Iterable


WRITER_LABEL = "space.lumos.dalton.writer"
CONTROLLER_LABEL = "space.lumos.dalton.controller"


def _atomic_plist(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def render(
    launch_agents_dir: str | Path,
    python_env_bin: str | Path,
    state_dir: str | Path,
    config_path: str | Path,
    log_dir: str | Path,
) -> dict[str, str]:
    destination = Path(launch_agents_dir).expanduser().resolve()
    bin_dir = Path(python_env_bin).expanduser().resolve()
    state = Path(state_dir).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    logs = Path(log_dir).expanduser().resolve()
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(logs, 0o700)
    common: dict[str, Any] = {
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "WorkingDirectory": str(state),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    writer = common | {
        "Label": WRITER_LABEL,
        "ProgramArguments": [
            str(bin_dir / "dalton-writer"),
            "--db", str(state / "core.sqlite"),
            "--socket", str(state / "run" / "writer.sock"),
            "--token-config", str(state / "writer-tokens.json"),
        ],
        "StandardOutPath": str(logs / "writer.stdout.log"),
        "StandardErrorPath": str(logs / "writer.stderr.log"),
    }
    controller = common | {
        "Label": CONTROLLER_LABEL,
        "ProgramArguments": [str(bin_dir / "daltond"), "--config", str(config)],
        "StandardOutPath": str(logs / "controller.stdout.log"),
        "StandardErrorPath": str(logs / "controller.stderr.log"),
    }
    writer_path = destination / f"{WRITER_LABEL}.plist"
    controller_path = destination / f"{CONTROLLER_LABEL}.plist"
    _atomic_plist(writer_path, writer)
    _atomic_plist(controller_path, controller)
    return {"writer": str(writer_path), "controller": str(controller_path)}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Dalton macOS LaunchAgents")
    parser.add_argument("--launch-agents-dir", type=Path, required=True)
    parser.add_argument("--python-env-bin", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    paths = render(
        args.launch_agents_dir,
        args.python_env_bin,
        args.state_dir,
        args.config,
        args.log_dir,
    )
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
