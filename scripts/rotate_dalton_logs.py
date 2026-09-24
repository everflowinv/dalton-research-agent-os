#!/usr/bin/env python3
"""轮转 ~/Library/Logs/Dalton 下的服务日志。

这些文件从 2026-08-14 起就没有轮转过，因为 macOS 上唯一"标准"的办法
（/etc/newsyslog.d）需要 root，而 Dalton 全套是用户级 LaunchAgent。

做法选的是 copy-truncate，不是 rename-create：launchd 以 O_APPEND 打开
StandardOutPath/StandardErrorPath，并且在服务重启之前一直持有那个 fd。rename
之后服务会继续往已经改名的 inode 里写，新文件永远是空的，只有重启服务才能修
好 —— 而为了轮转日志重启整个研究流水线是不可接受的。copy-truncate 把内容复制
走再把原文件就地截断为 0；O_APPEND 保证下一次写从 0 开始，不会留空洞。

代价是截断和复制之间的极少量新字节可能丢失。对 stderr 日志来说这个代价是对的。

范围：每个 --log-dir（可重复）下递归的全部 *.log，包括各 workspace 子目录
（~/Library/Logs/Dalton/workspaces/<ws>/、~/.dalton/workspace-logs/<ws>/ 等）。
过去只扫顶层，workspace 子目录里的日志从来没有轮转过。符号链接（文件或目录）
一律不跟随，避免轮转到日志目录以外的文件；同一文件经不同目录重复出现只处理一次。
不存在的目录记入 missing_log_dirs 并跳过（新机器上 workspace-logs 可能还没有），
只有全部目录都不存在才算失败。

默认 dry-run：只打印会做什么。--apply 才动文件。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "dalton-log-rotation:0.1"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "Dalton"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_KEEP = 5


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def rotate_one(path: Path, *, keep: int, apply: bool) -> dict[str, Any]:
    archive = path.with_name(f"{path.name}.{stamp()}.gz")
    action = {"log": str(path), "size_bytes": path.stat().st_size,
              "archive": str(archive), "rotated": False, "pruned": []}
    if apply:
        # 复制 → 压缩 → 就地截断。先落地归档，再截断，任何一步失败都不会
        # 同时丢掉两份。
        with path.open("rb") as source, gzip.open(archive, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.chmod(archive, 0o600)
        descriptor = os.open(path, os.O_WRONLY)
        try:
            os.ftruncate(descriptor, 0)
        finally:
            os.close(descriptor)
        action["rotated"] = True
    archives = sorted(path.parent.glob(f"{path.name}.*.gz"))
    doomed = archives[:-keep] if len(archives) > keep else []
    for old in doomed:
        action["pruned"].append(str(old))
        if apply:
            old.unlink()
    return action


def _log_dirs(value: Any) -> list[Path]:
    if value is None:
        return [DEFAULT_LOG_DIR]
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(item) for item in value]


def discover_logs(directory: Path) -> list[Path]:
    """Every regular ``*.log`` under ``directory``, never through a symlink."""

    found: list[Path] = []
    for root, dirnames, filenames in os.walk(directory, followlinks=False):
        # os.walk does not descend into symlinked directories when
        # followlinks=False, but it still lists them; prune for clarity.
        dirnames[:] = sorted(
            name for name in dirnames if not (Path(root) / name).is_symlink()
        )
        for name in sorted(filenames):
            if not name.endswith(".log"):
                continue
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            found.append(path)
    return found


def rotate(args: argparse.Namespace) -> dict[str, Any]:
    directories: list[Path] = []
    missing: list[str] = []
    for raw in _log_dirs(args.log_dir):
        directory = raw.expanduser().resolve()
        if not directory.is_dir():
            missing.append(str(directory))
            continue
        if directory not in directories:
            directories.append(directory)
    if not directories:
        raise RuntimeError(f"日志目录不存在：{', '.join(missing)}")
    actions = []
    seen: set[Path] = set()
    for directory in directories:
        for path in discover_logs(directory):
            identity = path.resolve()
            if identity in seen:
                continue
            seen.add(identity)
            size = path.stat().st_size
            if size < args.max_bytes:
                actions.append({"log": str(path), "size_bytes": size,
                                "rotated": False, "reason": "未达到轮转阈值"})
                continue
            actions.append(rotate_one(path, keep=args.keep, apply=args.apply))
    return {"schema_version": SCHEMA, "writes_performed": bool(args.apply),
            "log_dir": str(directories[0]),
            "log_dirs": [str(item) for item in directories],
            "missing_log_dirs": missing,
            "max_bytes": args.max_bytes,
            "keep": args.keep, "actions": actions,
            "rotated_count": sum(1 for row in actions if row.get("rotated"))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, action="append", default=None,
                        help="要轮转的日志根目录，可重复；递归包含子目录"
                             "（缺省 ~/Library/Logs/Dalton）")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="超过这个大小才轮转（默认 16 MiB）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help="每个日志保留多少个历史归档（默认 5）")
    parser.add_argument("--apply", action="store_true", help="真正轮转；缺省只打印计划")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.keep < 1 or args.max_bytes < 1:
        parser.error("--keep 和 --max-bytes 都必须为正")
    try:
        result = rotate(args)
    except (OSError, RuntimeError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
