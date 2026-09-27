#!/usr/bin/env python3
"""Read-only parity check: does every environment have what its lanes need?

Runs ``dalton_core.workspace_health_parity`` against each environment on this
machine -- the legacy install and every workspace under ``~/.dalton/workspaces``
-- and prints one line per gap with the command or decision that closes it:

* governance: the active policy carries the runtime baseline (every signable
  auto-commit rule, the SEC company-facts auto-start rules, a closed research
  budget), the constitution binds it, the mandate budget is closed;
* lanes: per registered lane, whether a freshly rendered writer runs it, what
  its last tick said, and whether the policy passes its own precondition;
* authorization: the mission's write scopes and SEC source, every covered
  company's CIK, the writer's core principal;
* mission: no open review stranded on a superseded mission version;
* config: service.json and every model config point at this environment's own
  databases and at routing policies that exist; mission plans and dossier /
  framework policies bind the active mission and constitution;
* host: the shared daily budget, routing alignment, credential slots and
  per-lane file inputs (``workspace_lane_parity``).

Nothing is written, no service is touched, nothing leaves the machine: every
database is opened ``mode=ro``.  Meant for every patrol:

    .venv/bin/python scripts/check_workspace_parity.py                # all environments
    .venv/bin/python scripts/check_workspace_parity.py --verbose      # ok rows too
    .venv/bin/python scripts/check_workspace_parity.py --json
    .venv/bin/python scripts/check_workspace_parity.py --workspace ws-7d894366d1132e2930475a60
    .venv/bin/python scripts/check_workspace_parity.py --state-dir /path/to/dalton-core

Exit status is 0, or 1 with ``--fail-on-gap`` when any environment has a gap.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.workspace_health_parity import (  # noqa: E402
    Environment,
    check_environment,
    render,
)

DEFAULT_HOST_ROOT = Path.home() / ".dalton"
DEFAULT_LEGACY_STATE = (Path.home() / "Library" / "Application Support" / "Dalton"
                        / "state" / "dalton-core")
DEFAULT_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def discover(host_root: Path, legacy_state: Path | None,
             launch_agents: Path) -> list[dict[str, Any]]:
    """Every environment on this machine, legacy first."""

    found: list[dict[str, Any]] = []
    if legacy_state is not None and (legacy_state / "core.sqlite").is_file():
        plist = launch_agents / "space.lumos.dalton.writer.plist"
        found.append({"name": "legacy", "state_dir": legacy_state,
                      "plist_path": plist if plist.is_file() else None})
    fleet = host_root / "workspaces"
    if fleet.is_dir():
        for manifest in sorted(fleet.glob("*/workspace.json")):
            found.append(workspace_environment(manifest, launch_agents))
    return found


def workspace_environment(manifest: Path, launch_agents: Path) -> dict[str, Any]:
    value = json.loads(manifest.read_text(encoding="utf-8"))
    slug = value["slug"]
    plist = launch_agents / f"space.lumos.dalton.workspace.{slug}.writer.plist"
    return {"name": slug, "state_dir": Path(value["state_dir"]),
            "service_config": Path(value["config_path"]),
            "plist_path": plist if plist.is_file() else None}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--workspace", action="append", default=[],
                        help="workspace slug (repeatable); default: every environment")
    target.add_argument("--state-dir", type=Path, action="append", default=[],
                        help="a dalton-core state directory (repeatable)")
    parser.add_argument("--host-root", type=Path, default=DEFAULT_HOST_ROOT)
    parser.add_argument("--legacy-state-dir", type=Path, default=DEFAULT_LEGACY_STATE)
    parser.add_argument("--launch-agents-dir", type=Path, default=DEFAULT_LAUNCH_AGENTS)
    parser.add_argument("--manager-config", type=Path, default=None)
    parser.add_argument("--no-host", action="store_true",
                        help="skip the machine-wide rows (shared budget, routing alignment, "
                             "credential slots, lane file inputs)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="print ok and info rows too")
    parser.add_argument("--fail-on-gap", action="store_true")
    args = parser.parse_args(argv)

    if args.state_dir:
        targets = [{"name": str(path), "state_dir": path} for path in args.state_dir]
    elif args.workspace:
        targets = [workspace_environment(args.host_root / "workspaces" / slug / "workspace.json",
                                         args.launch_agents_dir) for slug in args.workspace]
    else:
        targets = discover(args.host_root.expanduser(), args.legacy_state_dir.expanduser(),
                           args.launch_agents_dir.expanduser())
    if not targets:
        parser.error("no environment found; pass --state-dir")
    # Routing alignment and credential slots compare a workspace against the
    # legacy install, the environment every workspace's routing was copied
    # from.  Legacy itself is the reference and is not compared back.
    legacy = args.legacy_state_dir.expanduser()
    reference = legacy if (legacy / "core.sqlite").is_file() else None
    reports = []
    for item in targets:
        own = Path(item["state_dir"])
        env = Environment(item["name"], item["state_dir"],
                          service_config=item.get("service_config"),
                          plist_path=item.get("plist_path"))
        try:
            reports.append(check_environment(
                env, include_host=not args.no_host,
                source_state_dir=None if reference in {None, own} else reference,
                manager_config_path=args.manager_config))
        finally:
            env.close()
    if args.json:
        json.dump({"environments": reports}, sys.stdout, ensure_ascii=False, indent=1,
                  sort_keys=True, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(reports, verbose=args.verbose))
    gaps = sum(report["counts"].get("gap", 0) for report in reports)
    return 1 if args.fail_on_gap and gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
