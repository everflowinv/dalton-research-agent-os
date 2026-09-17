"""D4 on the command line: the same list, for when the page is not reachable.

``python -m dalton_core.needs_human_cli --state-dir <state>``

Read-only from start to finish.  It opens the Core, the Scheduler and the model
router in read-only mode, reads the governance folder and the heartbeat file,
and writes nothing anywhere -- which is what makes it safe to point at a live
installation while the service is running.

Text by default, because the reader is a person; ``--json`` for a machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .needs_human import KINDS, LEGACY_ENVIRONMENT, collect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="列出目前只有人能解开的事，按紧急度排序。")
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="Core 所在的 state 目录")
    parser.add_argument("--core-db", type=Path, default=None,
                        help="默认取 <state-dir>/core.sqlite")
    parser.add_argument("--governance-dir", type=Path, default=None,
                        help="默认取 <state-dir>/connector-governance")
    parser.add_argument("--heartbeat", type=Path, default=None,
                        help="默认取 <state-dir>/run/heartbeat.json")
    parser.add_argument("--scheduler-db", type=Path, default=None)
    parser.add_argument("--model-router-db", type=Path, default=None)
    parser.add_argument("--workspace-manager-config", type=Path, default=None,
                        help="有多个研究环境时，管理配置的路径")
    # 这份清单属于哪个研究环境。默认是 legacy：命令行默认指向老环境的 state
    # 目录，而老环境不替别的环境代办，所以它不列任何工作区条目。给一个工作区
    # slug，就只列那个工作区自己的事。
    parser.add_argument("--environment", default=LEGACY_ENVIRONMENT,
                        help="这份清单属于哪个研究环境：legacy（默认）或工作区 slug")
    parser.add_argument("--kind", action="append", choices=list(KINDS), default=None,
                        help="只看某一类；可以给多次")
    parser.add_argument("--actionable-only", action="store_true",
                        help="只看真正需要你动手的，隐藏知会类条目")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def render(result: Mapping[str, Any]) -> str:
    lines = [result["headline"], f"（统计时间 {result['as_of']}）", ""]
    if not result["items"]:
        return "\n".join(lines).strip() + "\n"
    for index, item in enumerate(result["items"], start=1):
        head = f"{index}. [{item['urgency_label']}] {item['title']}"
        if not item["actionable"]:
            head += "（知会，不用你动手）"
        lines.append(head)
        lines.append(f"   为什么卡住：{item['why_blocked']}")
        lines.append(f"   你要做的：{item['action']}")
        lines.append(f"   在哪里做：{item['where']} 页")
        lines.append(f"   不做的后果：{item['consequence']}")
        gaps = (item.get("detail") or {}).get("question_gaps") or []
        for gap in gaps:
            lines.append(
                f"     · 第 {gap.get('question_number')} 问缺：{gap.get('missing')}"
                f"（下一步：{gap.get('next_step')}）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = collect(
        core_db=args.core_db,
        state_dir=args.state_dir,
        heartbeat_path=args.heartbeat,
        governance_dir=args.governance_dir,
        scheduler_db=args.scheduler_db,
        model_router_db=args.model_router_db,
        workspace_manager_config_path=args.workspace_manager_config,
        environment=args.environment,
    )
    items = list(result["items"])
    if args.kind:
        items = [item for item in items if item["kind"] in set(args.kind)]
    if args.actionable_only:
        items = [item for item in items if item["actionable"]]
    result = {**result, "items": items}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    else:
        sys.stdout.write(render(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["build_parser", "main", "render"]
