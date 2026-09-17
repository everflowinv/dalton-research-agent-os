#!/usr/bin/env python3
"""把一个既有工作区补齐到和老环境一样的研究通道，默认只看不改。

新建的工作区拿到的 writer 参数比老环境少了十几个，于是雪球、专家访谈、卖方快报、
行情、事件日历、持股披露这些通道每一轮都报 `lane_unconfigured_hold`。原因不是权限
没批，而是这些通道要读的文件根本不在工作区的状态目录里：宿主级的语料目录、本仓库
打包的连接契约、以及一份"按任务生成"但首次发布其实没生成的检索计划。

新建流程已经在 `workspace_runtime_setup.install` 里补上了，这个脚本是给**已经建好**
的工作区用的。它做三件事，缺一不可：

  1. 把缺的东西装进工作区状态目录（链接宿主来源、按本工作区所有者署名批准打包契约、
     按本任务的公司范围生成检索计划）；
  2. 用 `install_workspace` 重新渲染这个工作区的 LaunchAgent —— 参数是渲染时从状态
     目录读出来的，不重渲染就等于什么都没发生；
  3. 告诉你重启 writer 的命令。它**不会**替你重启：重启要排空正在跑的通道，那是你
     的决定，不是脚本的。

默认 dry-run，逐条打印将要新增的每一个文件与它的来源。--apply 才落地。

    scripts/repair_workspace_lane_parity.py --workspace ws-7d89...
    scripts/repair_workspace_lane_parity.py --workspace ws-7d89... --apply
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.workspace import load_workspace_manifest  # noqa: E402
from dalton_core.mission_company_names import resolve_universe_names  # noqa: E402
from dalton_core.workspace_lane_parity import (  # noqa: E402
    apply_parity_actions,
    audit_lanes,
    plan_parity_actions,
    read_active_mission,
    render_audit,
    resolve_host_sources,
    unknown_universe_tickers,
)


def _sec_identity(workspace: Any) -> str | None:
    """The SEC contact string this workspace already runs its filings lane on."""

    try:
        value = json.loads(workspace.config_path.read_text(encoding="utf-8"))
        return str(value["bounded_planner"]["config"]["user_agent"]) or None
    except (OSError, ValueError, KeyError, TypeError):
        return None

DEFAULT_FLEET_ROOT = Path.home() / ".dalton" / "workspaces"
DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


class RepairError(RuntimeError):
    """修复被拒绝。"""


def manifest_path(slug: str, fleet_root: Path) -> Path:
    path = fleet_root / slug / "workspace.json"
    if not path.is_file():
        raise RepairError(f"找不到工作区清单：{path}")
    return path


def cockpit_port_busy(port: int) -> bool:
    """Whether this workspace's services are up.

    ``install_workspace`` refuses to render a plist for a workspace whose
    cockpit port is taken, and it is right to: rendering under a live service
    would leave the running process and its own plist describing two different
    writers. So this is checked before anything is written rather than
    discovered halfway through, and the owner is told the one order that works:
    stop, repair, start.
    """

    probe = socket.socket()
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def stop_instructions(slug: str) -> str:
    roles = ("control", "controller", "thesis-impact", "writer")
    lines = ["这个工作区的服务正在运行，先停下来再补（plist 不能在服务活着的时候重渲染）：", ""]
    lines += [f"  launchctl bootout gui/$(id -u)/space.lumos.dalton.workspace.{slug}.{role}"
              for role in roles]
    lines += ["", "然后重新执行本脚本的 --apply，最后按它给出的命令把服务拉起来。"]
    return "\n".join(lines) + "\n"


def company_name_overrides(values: Sequence[str] | None) -> dict[str, list[str]]:
    """``--company-name MSFT=Microsoft`` into a table, refusing a typo loudly."""

    table: dict[str, list[str]] = {}
    for raw in values or ():
        ticker, separator, name = str(raw).partition("=")
        if not separator or not ticker.strip() or not name.strip():
            raise RepairError(f"--company-name 要写成 TICKER=名称，收到的是：{raw}")
        table.setdefault(ticker.strip().upper(), []).append(name.strip())
    return table


def sec_name_resolver(state_dir: Path, identity: str | None):
    """Ask SEC what an issuer is registered as, or give up quietly.

    The same bounded, workspace-local resolver the first mission publish uses.
    A failure here is not fatal: the owner can say the name with
    ``--company-name``, and the audit says which company is waiting for one.
    """

    if not identity:
        return None

    def resolve(ticker: str) -> dict[str, str]:
        from dalton_core.workspace_mission_setup import resolve_sec_ticker

        return dict(resolve_sec_ticker(ticker, state_dir=state_dir, identity=identity))

    return resolve


def build_plan(
    slug: str, *, fleet_root: Path, launch_agents_dir: Path,
    actor_ref: str, source_state_dir: Path | None,
    lanes: Sequence[str] | None,
    company_names: Mapping[str, Sequence[str]] | None = None,
    resolve_names: bool = True,
) -> dict[str, Any]:
    """先全部算清楚再决定要不要写，所以计划和执行读的是同一份结果。"""

    workspace = load_workspace_manifest(manifest_path(slug, fleet_root))
    host_sources = resolve_host_sources(source_state_dir=source_state_dir)
    mission = read_active_mission(workspace.state_dir)
    if mission is None:
        raise RepairError(
            "这个工作区还没有发布研究任务；按任务生成的检索计划无从谈起，"
            "先在驾驶舱里发布第一个任务再回来跑这个脚本。")
    actions = plan_parity_actions(
        workspace.state_dir, actor_ref=actor_ref, host_sources=host_sources,
        mission=mission, lanes=lanes,
    )
    identity = _sec_identity(workspace)
    resolve_name = sec_name_resolver(workspace.state_dir, identity) if resolve_names else None
    names = dict(company_names or {})
    unnamed = unknown_universe_tickers(mission)
    if unnamed and resolve_name is not None:
        names.update(resolve_universe_names(
            [item for item in mission.get("universe") or []
             if str(item.get("ticker") or "").strip().upper() in set(unnamed)],
            resolve=resolve_name))
    before = audit_lanes(workspace.state_dir, host_sources=host_sources)
    return {
        "workspace_id": workspace.workspace_id,
        "slug": workspace.slug,
        "state_dir": str(workspace.state_dir),
        "manifest_path": str(workspace.manifest_path),
        "launch_agents_dir": str(launch_agents_dir),
        "actor_ref": actor_ref,
        "mission_ref": mission["mission_ref"],
        "host_sources": {k: str(v) for k, v in sorted(host_sources.items())},
        "actions": [action.as_wire() for action in actions],
        "company_names": {k: list(v) for k, v in sorted(names.items())},
        "unnamed_companies": sorted(set(unnamed) - set(names)),
        "_actions": actions,
        "_mission": mission,
        "unconfigured_before": before["unconfigured_lanes"],
        "cockpit_port": workspace.cockpit_port,
        "cockpit_port_busy": cockpit_port_busy(workspace.cockpit_port),
    }


def render_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"工作区：{plan['slug']}（{plan['workspace_id']}）",
        f"状态目录：{plan['state_dir']}",
        f"研究任务：{plan['mission_ref']}",
        f"署名：{plan['actor_ref']}",
        "",
    ]
    if not plan["actions"]:
        lines.append("没有可补的东西：这个工作区已有的宿主级输入都装好了。")
    else:
        lines.append(f"将新增 {len(plan['actions'])} 个文件：")
        for action in plan["actions"]:
            verb = {"link": "链接", "governance": "批准并写入",
                    "mission_plan": "按任务生成", "seed": "写入默认值"}[action["kind"]]
            lines.append(f"  [{verb}] {action['target']}")
            if action["kind"] == "link":
                lines.append(f"      来源：{action['detail']}")
            else:
                lines.append(f"      内容：{action['detail']}")
            lines.append(f"      原因：{action['reason']}")
    lines += ["", "然后会重新渲染这个工作区的 LaunchAgent（writer/controller/control）。"]
    if plan["company_names"]:
        lines.append("已查到的公司名称（会写进资料通道检索计划）：" + "、".join(
            f"{ticker}={'/'.join(names)}" for ticker, names in plan["company_names"].items()))
    if plan["unnamed_companies"]:
        lines.append("还查不到名称：" + "、".join(plan["unnamed_companies"])
                     + "；资料通道需要公司名字才能把文档归属到公司，"
                       "请用 --company-name TICKER=名称 补上。")
    if plan["unconfigured_before"]:
        lines.append("当前未装的通道：" + "、".join(plan["unconfigured_before"]))
    if plan["cockpit_port_busy"]:
        lines += ["", stop_instructions(plan["slug"]).rstrip()]
    return "\n".join(lines) + "\n"


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    from dalton_core.workspace_process import install_workspace

    performed = apply_parity_actions(
        plan["_actions"], actor_ref=plan["actor_ref"], mission=plan["_mission"],
        company_names=plan["company_names"])
    installed = install_workspace(plan["manifest_path"], plan["launch_agents_dir"])
    after = audit_lanes(plan["state_dir"])
    return {
        "status": "applied",
        "performed": performed,
        "plists": installed["plists"],
        "configured_lanes": after["configured_lanes"],
        "unconfigured_lanes": after["unconfigured_lanes"],
        "audit": after,
    }


def restart_instructions(slug: str) -> str:
    """The writer reads its lane arguments once, at launch. So: say so."""

    namespace = f"space.lumos.dalton.workspace.{slug}"
    lines = ["", "新参数只有服务重启后才会生效。把它们拉起来："]
    lines += [f"  launchctl bootstrap gui/$(id -u) "
              f"~/Library/LaunchAgents/{namespace}.{role}.plist"
              for role in ("writer", "controller", "control")]
    lines += ["", "重启后在驾驶舱看一轮 tick 摘要，确认这些通道不再是 held。", ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True, help="工作区 slug")
    parser.add_argument("--actor-ref", default=None,
                        help="批准连接契约的所有者，形如 human:you@example.com；"
                             "默认沿用该工作区任务的发布者")
    parser.add_argument("--fleet-root", type=Path, default=DEFAULT_FLEET_ROOT)
    parser.add_argument("--launch-agents-dir", type=Path, default=DEFAULT_LAUNCH_AGENTS_DIR)
    parser.add_argument("--source-state-dir", type=Path, default=None,
                        help="另一个本机环境的状态目录，用来定位宿主级来源")
    parser.add_argument("--lane", action="append", default=None,
                        help="只补这一条通道，可重复")
    parser.add_argument("--company-name", action="append", default=None,
                        metavar="TICKER=名称",
                        help="告诉资料通道这家公司叫什么，可重复；查不到 SEC 名称时用它")
    parser.add_argument("--no-sec-name-lookup", action="store_true",
                        help="不去问 SEC 公司名称（离线或不想发网络请求时）")
    parser.add_argument("--apply", action="store_true", help="真的写入并重新渲染")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    fleet_root = Path(args.fleet_root).expanduser()
    actor_ref = args.actor_ref
    if actor_ref is None:
        workspace = load_workspace_manifest(manifest_path(args.workspace, fleet_root))
        mission = read_active_mission(workspace.state_dir) or {}
        actor_ref = mission.get("actor_ref")
        if not isinstance(actor_ref, str) or not actor_ref.startswith("human:"):
            parser.error("无法从任务里读出所有者，请用 --actor-ref human:... 指定")
    try:
        plan = build_plan(
            args.workspace, fleet_root=fleet_root,
            launch_agents_dir=Path(args.launch_agents_dir).expanduser(),
            actor_ref=actor_ref,
            source_state_dir=(None if args.source_state_dir is None
                              else Path(args.source_state_dir).expanduser()),
            lanes=args.lane,
            company_names=company_name_overrides(args.company_name),
            resolve_names=not args.no_sec_name_lookup,
        )
    except RepairError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.apply:
        wire = {k: v for k, v in plan.items() if not k.startswith("_")}
        if args.json:
            json.dump({**wire, "status": "dry-run"}, sys.stdout,
                      ensure_ascii=False, sort_keys=True, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(render_plan(plan))
            sys.stdout.write("\n这是 dry-run，什么都没写。确认后加 --apply。\n")
        return 0
    if plan["cockpit_port_busy"]:
        sys.stderr.write(stop_instructions(args.workspace))
        return 2
    result = apply_plan(plan)
    if args.json:
        json.dump({k: v for k, v in result.items() if k != "audit"}, sys.stdout,
                  ensure_ascii=False, sort_keys=True, indent=2)
        sys.stdout.write("\n")
    else:
        for row in result["performed"]:
            print(f"[{row['result']}] {row['target']}")
        print()
        print("已重新渲染：")
        for role, path in sorted(result["plists"].items()):
            print(f"  {role}: {path}")
        print()
        sys.stdout.write(render_audit(result["audit"]))
        sys.stdout.write(restart_instructions(args.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
