#!/usr/bin/env python3
"""Switch off the legacy environment's leftover ``agenda.enabled=true`` (P2-8).

ADR-0009 retired the Perception/Agenda plane.  Since then the controller has
ignored ``agenda.enabled`` unless the environment also opts in with
``legacy_agenda_plane: true``, and the heartbeat reports ``agenda.state =
retired``.  The legacy ``service.json`` still says ``"enabled": true`` for a
block that points at the retired 万华 (``company_ref: wanhua``,
``expected_agent_id: chem``) configuration.  Nothing runs from it, but anyone
reading the file for a diagnosis sees an enabled agenda that isn't there.

This script sets that one boolean to ``false`` and changes nothing else.

* The ``agenda.config`` block stays as it is.  ``model_selection`` still reads
  the ``agenda_planning`` routing pin from it, and old cycles name that policy.
* Nothing is restarted.  The running controller already treats the plane as
  retired, so the change only makes the file agree with what runs.  The next
  restart picks it up whenever that happens.
* The file is copied beside itself as ``service.pre-agenda-retire-<UTC>.json``
  (mode 0600) before it is rewritten.  The rewrite is atomic and keeps mode
  0600.

It refuses to act if the environment has deliberately opted back into the
retired plane (``legacy_agenda_plane: true``).  That opt-in is the owner's to
undo, not this script's.

    # look first -- reads only, changes nothing
    scripts/disable_legacy_agenda.py

    # then, as the owner
    scripts/disable_legacy_agenda.py apply

``--service`` points at a different ``service.json``.  The default is the legacy
installation's ``~/Library/Application Support/Dalton/config/service.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_SERVICE = Path("~/Library/Application Support/Dalton/config/service.json")
SCHEMA = "dalton-disable-legacy-agenda:0.1"
LEGACY_AGENDA_PLANE_KEY = "legacy_agenda_plane"


class RefusedError(RuntimeError):
    """The file is not in a state this script is allowed to change."""


def _utc_stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def plan(service_path: Path) -> dict[str, Any]:
    """What would change, read-only."""

    raw = json.loads(service_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RefusedError(f"{service_path} is not a JSON object")
    agenda = raw.get("agenda")
    opted_in = raw.get(LEGACY_AGENDA_PLANE_KEY, False) is True
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "service": str(service_path),
        "agenda_present": isinstance(agenda, dict),
        "agenda_enabled": agenda.get("enabled") if isinstance(agenda, dict) else None,
        "agenda_company_ref": ((agenda.get("config") or {}).get("company_ref")
                               if isinstance(agenda, dict) else None),
        LEGACY_AGENDA_PLANE_KEY: opted_in,
    }
    if opted_in:
        result["action"] = "refused"
        result["reason"] = ("legacy_agenda_plane is true: the owner opted back into the "
                            "retired plane on purpose; undo that first")
    elif not isinstance(agenda, dict):
        result["action"] = "none"
        result["reason"] = "no agenda block"
    elif agenda.get("enabled") is not True:
        result["action"] = "none"
        result["reason"] = "agenda.enabled is already false"
    else:
        result["action"] = "disable"
        result["reason"] = ("agenda.enabled is true but the plane is retired (ADR-0009); "
                            "set it to false, keep agenda.config")
    return result


def _validate(document: dict[str, Any]) -> None:
    """The rewritten document must still parse as a service config."""

    from dalton_core.service import ServiceConfig

    ServiceConfig.from_mapping(document)


def apply(service_path: Path, *, now: datetime | None = None,
          validate: Callable[[dict[str, Any]], None] = _validate) -> dict[str, Any]:
    result = plan(service_path)
    if result["action"] == "refused":
        raise RefusedError(result["reason"])
    if result["action"] != "disable":
        return {**result, "writes_performed": False}
    original = service_path.read_bytes()
    document = json.loads(original.decode("utf-8"))
    document["agenda"]["enabled"] = False
    validate(document)
    stamp = _utc_stamp(now or datetime.now(timezone.utc))
    backup = service_path.with_name(f"service.pre-agenda-retire-{stamp}.json")
    if backup.exists():
        raise RefusedError(f"backup {backup} already exists; not overwriting it")
    shutil.copy2(service_path, backup)
    os.chmod(backup, 0o600)
    if backup.read_bytes() != original:
        raise RefusedError(f"backup {backup} does not match the original; stopped")
    body = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".service.", suffix=".json",
                                             dir=str(service_path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, service_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {**result, "writes_performed": True, "backup": str(backup),
            "restart_required": False,
            "note": ("the running controller already treats the agenda plane as retired; "
                     "the file now says so too")}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", nargs="?", choices=("plan", "apply"), default="plan",
                        help="plan (default, read-only) or apply")
    parser.add_argument("--apply", dest="apply_flag", action="store_true",
                        help="same as the 'apply' mode word")
    parser.add_argument("--service", type=Path, default=DEFAULT_SERVICE)
    args = parser.parse_args(argv)
    service_path = args.service.expanduser()
    try:
        if args.mode == "apply" or args.apply_flag:
            result = apply(service_path)
        else:
            result = {**plan(service_path), "writes_performed": False,
                      "next": "run again with 'apply' to write"}
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
