"""Install dedicated annual-report model configs from current approved roles.

The annual lane has separate producer and verifier configuration files so the
Cockpit can repoint either purpose independently.  On first installation each
file inherits the complete current routing, credential, transport and budget
configuration of an already installed equivalent role.  Existing annual
files are authority owned by the operator and are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME,
    load_annual_report_model_config,
    load_annual_report_model_configs,
)
from .store import canonical_json

DRAFT_SOURCE_NAMES = ("dossier-model-config.json", "initial-screen-model-config.json")
VERIFIER_SOURCE_NAMES = (
    "company-dossier-verifier-model-config.json",
    "dossier-verifier-model-config.json",
)


class AnnualReportSetupError(RuntimeError):
    pass


def _first_existing(state_dir: Path, names: Sequence[str], label: str) -> Path:
    for name in names:
        candidate = state_dir / name
        if candidate.is_file():
            return candidate
    raise AnnualReportSetupError(
        f"no installed {label} model configuration is available to seed the annual lane"
    )


def _write_new(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def install(config_path: str | Path) -> dict[str, Any]:
    service_path = Path(config_path).expanduser().resolve()
    try:
        service = json.loads(service_path.read_text(encoding="utf-8"))
        state_dir = Path(service["core_db"]).expanduser().resolve().parent
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AnnualReportSetupError("service config cannot locate the Core state directory") from exc

    targets = (
        state_dir / DRAFT_MODEL_CONFIG_NAME,
        state_dir / VERIFIER_MODEL_CONFIG_NAME,
    )
    sources = (
        targets[0] if targets[0].is_file() else _first_existing(
            state_dir, DRAFT_SOURCE_NAMES, "producer"
        ),
        targets[1] if targets[1].is_file() else _first_existing(
            state_dir, VERIFIER_SOURCE_NAMES, "independent verifier"
        ),
    )
    try:
        configs = tuple(
            load_annual_report_model_config(path, label)
            for path, label in zip(sources, ("annual-report draft seed", "annual-report verifier seed"))
        )
        if configs[0]["model_router_db"] != configs[1]["model_router_db"]:
            raise AnnualReportSetupError(
                "annual-report producer and verifier seeds must use one Router authority"
            )
    except ValueError as exc:
        raise AnnualReportSetupError(str(exc)) from exc

    changed: list[str] = []
    preserved: list[str] = []
    for target, config in zip(targets, configs):
        if target.exists():
            preserved.append(str(target))
        else:
            _write_new(target, config)
            changed.append(str(target))
    # Re-read the installed pair, including owner-only mode and shared Router.
    try:
        load_annual_report_model_configs(state_dir)
    except ValueError as exc:
        raise AnnualReportSetupError(str(exc)) from exc
    return {
        "status": "installed" if changed else "preserved",
        "created": changed,
        "preserved": preserved,
        "draft_source": str(sources[0]),
        "verifier_source": str(sources[1]),
        "draft_model_config": str(targets[0]),
        "verifier_model_config": str(targets[1]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="service.json")
    args = parser.parse_args(argv)
    try:
        result = install(args.config)
    except AnnualReportSetupError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["AnnualReportSetupError", "install"]
