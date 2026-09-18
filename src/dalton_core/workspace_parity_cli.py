"""Read-only: which research lanes this workspace's writer actually runs, and why not.

The question this answers is the one an owner asks after creating a second
environment and finding it quieter than the first: *the same machine, the same
release, the same approvals -- so where did the other ten lanes go?*  Before
this the answer lived in a tick summary that said ``held`` and a plist nobody
reads.

Three more rows come after the lanes, and they answer the money-and-models half
of the same question: is this environment inside the machine's shared daily
budget, is its model routing the machine's, and does it list the machine's
broker credential slots.  A new environment gets all three at creation; these
rows are how an owner checks an old one, and each names the command that fixes
it.

It writes nothing and opens nothing: the mission is read through a read-only
database connection and the writer's own plist is parsed rather than
re-rendered, so this is safe to run against a live workspace while its writer
is up.  The repair that acts on the answer is a separate, explicit script --
``scripts/repair_workspace_lane_parity.py`` -- for the same reason the release
switch is: reading and changing must not be the same command.

    python -m dalton_core.workspace_parity_cli --workspace ws-7d89...
    python -m dalton_core.workspace_parity_cli --state-dir /path/to/dalton-core
    python -m dalton_core.workspace_parity_cli --workspace ws-7d89... --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .workspace_lane_parity import (
    audit_lanes,
    render_audit,
    resolve_host_sources,
)

DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def workspace_state_dir(slug: str, *, fleet_root: str | Path | None = None) -> Path:
    """The state directory of one workspace, from its manifest.

    Resolved through the manifest rather than by joining paths, because the
    manifest is the only thing that may say where a workspace keeps its state
    -- two of them on this machine live on an external volume behind a link.
    """

    from .workspace import load_workspace_manifest

    root = (Path(fleet_root).expanduser() if fleet_root is not None
            else Path.home() / ".dalton" / "workspaces")
    manifest = root / slug / "workspace.json"
    if not manifest.is_file():
        raise SystemExit(f"找不到工作区清单：{manifest}")
    return load_workspace_manifest(manifest).state_dir


def writer_plist_path(slug: str, *, launch_agents_dir: str | Path | None = None) -> Path:
    directory = (Path(launch_agents_dir).expanduser() if launch_agents_dir is not None
                 else DEFAULT_LAUNCH_AGENTS_DIR)
    return directory / f"space.lumos.dalton.workspace.{slug}.writer.plist"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace", help="工作区 slug，例如 ws-7d894366d1132e2930475a60")
    target.add_argument("--state-dir", type=Path,
                        help="直接给出状态目录（离线或测试时用）")
    parser.add_argument("--fleet-root", type=Path, default=None,
                        help="工作区根目录，默认 ~/.dalton/workspaces")
    parser.add_argument("--launch-agents-dir", type=Path, default=None)
    parser.add_argument("--source-state-dir", type=Path, default=None,
                        help="另一个本机环境的状态目录；用它来定位宿主级来源，"
                             "也用它来比对模型路由和凭证槽位")
    parser.add_argument("--manager-config", type=Path, default=None,
                        help="本机研究环境清单；默认从这个环境自己的绑定文件或 "
                             "service.json 里读出来")
    parser.add_argument("--json", action="store_true", help="输出机器可读的结果")
    args = parser.parse_args(argv)

    if args.workspace is not None:
        state = workspace_state_dir(args.workspace, fleet_root=args.fleet_root)
        plist = writer_plist_path(args.workspace,
                                  launch_agents_dir=args.launch_agents_dir)
    else:
        state = Path(args.state_dir).expanduser().resolve()
        plist = None
    report = audit_lanes(
        state,
        host_sources=resolve_host_sources(source_state_dir=args.source_state_dir),
        plist_path=plist if plist is not None and plist.is_file() else None,
        manager_config_path=args.manager_config,
        source_state_dir=args.source_state_dir,
    )
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_audit(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
