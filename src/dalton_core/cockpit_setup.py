"""Point the cockpit at what it reads (P9d-18, ADR-0006).

The cockpit plane needs five paths that already exist in ``service.json`` or
next to the state, none of them a credential:

- the Core database (opened read-only by the cockpit);
- the state directory (lane tickets under ``discoveries/``, ``fetches/`` …);
- the heartbeat file the service writes every tick;
- the scheduler database (cockpit model calls are WorkOrders like any other);
- the extraction model configuration, if installed, so ad-hoc questions and
  goal drafts route and spend exactly like an extraction window.

Idempotent: writes ``control.config.cockpit`` only when it differs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .cockpit_plane import CockpitConfig

JOURNAL_DIR = "cockpit"
JOURNAL_FILE = "journal.sqlite"


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def install(config_path: str | Path) -> dict[str, Any]:
    config_path = Path(config_path).expanduser().resolve()
    service = json.loads(config_path.read_text(encoding="utf-8"))
    core_db = Path(service["core_db"]).resolve()
    state_dir = core_db.parent
    control = service["control"]["config"]
    review = control.get("research_review") or {}
    model_config = review.get("document_extraction_model_config_path")
    wire = {
        "core_db": str(core_db),
        "state_dir": str(state_dir),
        "heartbeat_path": str(Path(service["heartbeat_path"]).resolve()),
        "scheduler_db": str(Path(service["scheduler_db"]).resolve()),
        "journal_path": str(state_dir / JOURNAL_DIR / JOURNAL_FILE),
        "model_config_path": None if model_config is None else str(Path(model_config).resolve()),
    }
    existing = control.get("cockpit")
    if isinstance(existing, dict) and existing.get("mission_ref"):
        wire["mission_ref"] = existing["mission_ref"]
    if isinstance(existing, dict) and "openclaw_config_path" in existing:
        wire["openclaw_config_path"] = existing["openclaw_config_path"]
    CockpitConfig.from_mapping(wire)  # closed-shape check before anything is written
    (state_dir / JOURNAL_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    changed = existing != wire
    if changed:
        control["cockpit"] = wire
        _write_owner_only(config_path, service)
    return {"cockpit": wire, "service_config_changed": changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="service.json")
    args = parser.parse_args(argv)
    print(json.dumps(install(args.config), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
