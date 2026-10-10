#!/usr/bin/env python3
"""把一个工作区的既有研究资料库换成它自己的真实目录，默认只看不改。

prior-research（本机既有研究资料库）按工作区各有一份，不共享。
新建工作区现在从一个空的 ``<state>/feeds/prior-research`` 起步；这个脚本是给**已经**
链到别的环境的工作区用的（``feeds/prior-research -> 其他环境的资料库``）。

它做的事：

  1. 只读：找出这个工作区的 Core（core.sqlite，mode=ro）里引用过的每一个
     ``prior-research-doc:sha256:...``；按链接指向的资料库逐家公司读 manifest，把引用
     映射回 ``<公司>/<相对路径>``（文档编号就是这两者的 sha256，与读取时同一个算法）。
  2. 只读：列出要复制的文件和总大小，以及链接指向的整个资料库大小（du）。
  3. ``--apply``：在同一目录下先建 ``prior-research.localizing``，复制被引用的文件并写入
     只含这些条目的 manifest（原条目原样保留；若 manifest 本身被引用则整份原样复制，连同
     该公司的全部文件），逐个核对 sha256；然后把旧链接改名为
     ``prior-research.legacy-link-20260930``（不删除），再把新目录改名为
     ``prior-research``。writer 的参数 ``--prior-research-corpus-root`` 指向的路径不变，
     所以不需要重新渲染 plist，重启即可。回执写到 ``<state>/host-scheme-repairs/``。

先停止新任务并排空在途任务，再停止读取此目录的服务。脚本只负责资料本地化，
不代替服务的安全停派、排空或重启。核对预览和回执后，再恢复原来运行的服务。

    scripts/localize_workspace_prior_research.py --workspace <slug>
    scripts/localize_workspace_prior_research.py --workspace <slug> --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.prior_research_core import (  # noqa: E402
    MANIFEST_NAME,
    PriorResearchError,
    company_folders,
    document_ref,
    manifest_for,
)
from dalton_core.workspace import load_workspace_manifest  # noqa: E402

RELATIVE = Path("feeds") / "prior-research"
BACKUP_SUFFIX = "legacy-link-20260930"
STAGING_SUFFIX = "localizing"
_REF_RE = re.compile(r"prior-research-doc:sha256:[0-9a-f]{64}")
DEFAULT_FLEET_ROOT = Path.home() / ".dalton" / "workspaces"


class LocalizeError(RuntimeError):
    """本地化被拒绝。"""


def referenced_documents(core_db: Path) -> set[str]:
    """Every prior-research document id this Core mentions, read-only."""

    if not core_db.is_file():
        return set()
    connection = sqlite3.connect(f"file:{core_db}?mode=ro", uri=True, timeout=30)
    found: set[str] = set()
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [row[1] for row in connection.execute(
                f'PRAGMA table_info("{table}")')
                if (row[2] or "").upper() in {"TEXT", ""}]
            for column in columns:
                try:
                    rows = connection.execute(
                        f'SELECT "{column}" FROM "{table}" '
                        f'WHERE instr("{column}", ?) > 0',
                        ("prior-research-doc:sha256:",))
                    for (value,) in rows:
                        if isinstance(value, str):
                            found.update(_REF_RE.findall(value))
                except sqlite3.Error:
                    continue
    finally:
        connection.close()
    return found


def corpus_index(root: Path) -> dict[str, dict[str, Any]]:
    """document id -> where it lives in this corpus, for every manifest entry."""

    index: dict[str, dict[str, Any]] = {}
    for company in company_folders(root):
        index[document_ref(company, MANIFEST_NAME)] = {
            "company": company, "path": MANIFEST_NAME, "manifest": True}
        try:
            entries, _refused = manifest_for(root, company)
        except PriorResearchError:
            continue
        for entry in entries:
            index[document_ref(company, entry["path"])] = {
                "company": company, "path": entry["path"], "manifest": False}
    return index


def _du_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(dirpath) / name).lstat().st_size
            except OSError:
                pass
    return total


def cockpit_port_busy(port: int) -> bool:
    probe = socket.socket()
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def build_plan(slug: str, fleet_root: Path) -> dict[str, Any]:
    manifest = fleet_root / slug / "workspace.json"
    if not manifest.is_file():
        raise LocalizeError(f"找不到工作区清单：{manifest}")
    workspace = load_workspace_manifest(manifest)
    state = workspace.state_dir
    target = state / RELATIVE
    plan: dict[str, Any] = {
        "workspace": slug, "state_dir": str(state), "target": str(target),
        "cockpit_port": workspace.cockpit_port,
    }
    if not target.is_symlink():
        plan["status"] = "already_local" if target.is_dir() else "absent"
        plan["note"] = ("已经是本环境自己的目录，不需要处理。" if target.is_dir() else
                        "目录不存在；repair_workspace_lane_parity.py 会建一个空目录。")
        return plan
    source = target.resolve()
    plan.update({"status": "linked", "link_target": os.readlink(target),
                 "source": str(source)})
    if (target.parent / f"prior-research.{BACKUP_SUFFIX}").exists() \
            or (target.parent / f"prior-research.{BACKUP_SUFFIX}").is_symlink():
        raise LocalizeError(f"备份名 prior-research.{BACKUP_SUFFIX} 已被占用，请人工确认")
    referenced = referenced_documents(state / "core.sqlite")
    index = corpus_index(source) if source.is_dir() else {}
    unresolved = sorted(ref for ref in referenced if ref not in index)
    by_company: dict[str, dict[str, Any]] = {}
    for ref in sorted(referenced & set(index)):
        where = index[ref]
        row = by_company.setdefault(where["company"], {"whole": False, "paths": set()})
        if where["manifest"]:
            row["whole"] = True
        else:
            row["paths"].add(where["path"])
    files: list[dict[str, Any]] = []
    manifests: dict[str, Any] = {}
    for company, row in sorted(by_company.items()):
        folder = source / company
        raw = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
        documents = raw.get("documents") if isinstance(raw, dict) else raw
        documents = documents if isinstance(documents, list) else []
        if row["whole"]:
            kept = documents
            manifests[company] = {"verbatim": True}
        else:
            kept = [item for item in documents if isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and item["path"].strip() in row["paths"]]
            body = dict(raw) if isinstance(raw, dict) else {}
            body["documents"] = kept
            manifests[company] = {"verbatim": False, "value": body}
        for item in kept:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = (folder / item["path"].strip()).resolve()
            if not path.is_relative_to(folder.resolve()) or not path.is_file():
                continue
            files.append({"company": company, "path": item["path"].strip(),
                          "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    plan.update({
        "referenced": len(referenced), "unresolved": unresolved,
        "companies": sorted(by_company), "manifests": manifests, "files": files,
        "copy_bytes": sum(item["bytes"] for item in files)
        + sum(len(json.dumps(item.get("value", {}))) for item in manifests.values()),
        "source_du_bytes": _du_bytes(source) if source.is_dir() else 0,
        "cockpit_port_busy": cockpit_port_busy(workspace.cockpit_port),
    })
    return plan


def render(plan: dict[str, Any]) -> str:
    lines = [f"工作区：{plan['workspace']}", f"目标：{plan['target']}"]
    if plan["status"] != "linked":
        return "\n".join([*lines, plan["note"]]) + "\n"
    lines += [
        f"现在是链接：-> {plan['link_target']}",
        f"Core 里引用过的既有研究文档：{plan['referenced']} 个；"
        f"能在资料库里找到的公司：{len(plan['companies'])} 家",
        f"将复制：{len(plan['files'])} 个文件 + {len(plan['manifests'])} 份 manifest，"
        f"约 {plan['copy_bytes']} 字节",
        f"链接指向的整个资料库（du）：{plan['source_du_bytes']} 字节",
    ]
    for item in plan["files"]:
        lines.append(f"  {item['company']}/{item['path']}（{item['bytes']} 字节）")
    if plan["unresolved"]:
        lines.append(f"!! 有 {len(plan['unresolved'])} 个引用在资料库里找不到（apply 会拒绝，"
                     "除非加 --allow-unresolved）：" + "、".join(plan["unresolved"][:5]))
    lines.append(f"旧链接会改名为 prior-research.{BACKUP_SUFFIX}（不删除），新目录权限 700。")
    if plan["cockpit_port_busy"]:
        lines.append("服务正在运行：apply 前要先停（包装脚本会处理）。")
    return "\n".join(lines) + "\n"


def apply(plan: dict[str, Any], *, allow_unresolved: bool = False) -> dict[str, Any]:
    if plan["status"] != "linked":
        return {"status": plan["status"], "note": plan["note"]}
    if plan["unresolved"] and not allow_unresolved:
        raise LocalizeError("有引用在资料库里找不到；确认后加 --allow-unresolved")
    if cockpit_port_busy(plan["cockpit_port"]):
        raise LocalizeError("这个工作区的服务还在运行，先停服务")
    target = Path(plan["target"])
    source = Path(plan["source"])
    staging = target.with_name(f"prior-research.{STAGING_SUFFIX}")
    backup = target.with_name(f"prior-research.{BACKUP_SUFFIX}")
    if staging.exists() or staging.is_symlink():
        raise LocalizeError(f"{staging} 已存在（上次中断留下的），请人工检查后改名再重跑")
    staging.mkdir(mode=0o700)
    for company, manifest in plan["manifests"].items():
        folder = staging / company
        folder.mkdir(mode=0o700)
        if manifest["verbatim"]:
            shutil.copy2(source / company / MANIFEST_NAME, folder / MANIFEST_NAME)
        else:
            (folder / MANIFEST_NAME).write_text(
                json.dumps(manifest["value"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
    for item in plan["files"]:
        destination = staging / item["company"] / item["path"]
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source / item["company"] / item["path"], destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
            raise LocalizeError(f"复制后校验不符：{item['company']}/{item['path']}")
    # The swap: two renames in one directory. The old link is kept.
    os.rename(target, backup)
    try:
        os.rename(staging, target)
    except OSError:
        os.rename(backup, target)
        raise
    receipt = {
        "schema_version": "dalton-prior-research-localization-0.1",
        "at": datetime.now(timezone.utc).isoformat(),
        "workspace": plan["workspace"], "target": str(target),
        "previous_link": plan["link_target"], "backup": str(backup),
        "files": plan["files"], "companies": plan["companies"],
        "unresolved": plan["unresolved"],
    }
    receipts = Path(plan["state_dir"]) / "host-scheme-repairs"
    receipts.mkdir(mode=0o700, exist_ok=True)
    path = receipts / f"prior-research-localization-{receipt['at'][:19].replace(':', '')}.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return {"status": "localized", "backup": str(backup), "receipt": str(path),
            "files": len(plan["files"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--fleet-root", type=Path, default=DEFAULT_FLEET_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-unresolved", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.workspace, Path(args.fleet_root).expanduser())
        if not args.apply:
            if args.json:
                print(json.dumps({**plan, "mode": "dry-run"}, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(render(plan) + "\n这是 dry-run，什么都没写。\n")
            return 0
        result = apply(plan, allow_unresolved=args.allow_unresolved)
    except (LocalizeError, PriorResearchError) as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
