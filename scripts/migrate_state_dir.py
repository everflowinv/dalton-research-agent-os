#!/usr/bin/env python3
"""把 Dalton 的状态目录迁到外置盘，并用符号链接把原路径接过去。

覆盖四组数据：

  legacy      ~/Library/Application Support/Dalton/state
  workspaces  ~/.dalton/workspaces          （注意粒度，见下）
  backups     备份根目录（从 legacy service.json 的 backup.root 读）
  releases    ~/.dalton/runtime/releases    （默认不迁，见下）

## 为什么是符号链接，而不是改所有配置里的路径

改配置的办法要求把每一处绝对路径都找全：10 个 plist 的 ProgramArguments、
manager.json、两个 workspace.json 的六个字段、每个 workspace 的 service.json、
21 个模型配置里的 core_db/scheduler_db/budget_db/model_router_db、发布 worker
配置里的 8 个绝对路径、connector-governance 与 discovery-plans 里的路径、备份根
目录，以及 Core 里存着绝对路径的行。漏掉任何一处的后果不是启动失败，而是
**两个进程打开两个不同的 core.sqlite** —— 一个静默的脑裂，可能几天后才发现。

符号链接把这件事变成一次原子的 rename：要么所有读者看到旧目录，要么所有读者
看到新目录，不存在"一半配置指向新盘"的中间态。它也顺便解决了 Darwin 的
sockaddr_un 只有 103 字节的问题：writer socket 的路径字符串完全不变。

## 符号链接的粒度不是随便选的

workspace.py 的 `_absolute()` 会 `resolve()`，`_inside()` 又要求 state_dir 必须
在 workspace_root 之下。所以：

  * 只把 `~/.dalton/workspaces/<slug>/state` 做成软链 → state_dir 解析到
    /Volumes/…，workspace_root 还在 ~/.dalton → `state_dir escapes
    workspace_root`，**两个 workspace 的全部服务都起不来**。
  * 把 `~/.dalton/workspaces` 整个目录做成软链 → workspace_root 和 state_dir
    一起解析到 /Volumes/…，`_inside` 成立，`(host/"workspaces").resolve()` 也
    跟着一起变，校验全部通过。

legacy 没有 workspace manifest（DALTON_WORKSPACE_MANIFEST 未设置时
validate_runtime_context 直接返回 None），所以 legacy 在 `state` 这一层做软链
是安全的。

## releases 默认不迁

发布 venv 挪到外置盘意味着盘没挂上时**一个服务都起不来**（现在是数据读不到，
服务还能起来报错）。要迁就显式加 --include-releases。

默认 dry-run：打印计划、空间估算和全部预检结论，不动任何东西。--apply 才落地。
本脚本不会自己执行 --apply 以外的任何写操作。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "dalton-state-migration:0.1"
HOME = Path.home()
DEFAULT_LEGACY = HOME / "Library" / "Application Support" / "Dalton" / "state"
DEFAULT_WORKSPACES = HOME / ".dalton" / "workspaces"
DEFAULT_RELEASES = HOME / ".dalton" / "runtime" / "releases"
AGENT_PATTERNS = ("space.lumos.dalton*.plist", "com.dalton.*.plist")


class MigrationError(RuntimeError):
    """迁移被拒绝。"""


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            item = Path(root) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    return total


def mount_info(path: Path) -> dict[str, Any]:
    output = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                            check=False).stdout
    best = {"mount_point": "/", "line": ""}
    for line in output.splitlines():
        if " on " not in line:
            continue
        _device, _, rest = line.partition(" on ")
        point = rest.split(" (", 1)[0]
        if str(path).startswith(point) and len(point) >= len(best["mount_point"]):
            best = {"mount_point": point, "line": line}
    flags = best["line"].rsplit("(", 1)[-1].rstrip(")").split(", ") if best["line"] else []
    return {"mount_point": best["mount_point"], "flags": flags,
            "filesystem": flags[0] if flags else None,
            "read_only": "read-only" in flags,
            "no_owners": "noowners" in flags,
            "network": any(item in flags for item in ("nfs", "smbfs", "webdav"))}


def target_capability_probe(destination: Path) -> dict[str, Any]:
    """真的在目标盘上试一次：权限位、属主、软链、SQLite 排他锁。"""

    destination.mkdir(parents=True, exist_ok=True)
    probe = destination / f".migration-probe-{stamp()}"
    probe.mkdir()
    result: dict[str, Any] = {}
    try:
        sample = probe / "mode.json"
        sample.write_text("{}", encoding="utf-8")
        os.chmod(sample, 0o600)
        info = sample.stat()
        result["preserves_mode_0600"] = info.st_mode & 0o777 == 0o600
        result["preserves_owner"] = info.st_uid == os.getuid()
        link = probe / "link"
        link.symlink_to(sample)
        result["supports_symlinks"] = link.is_symlink()
        database = probe / "probe.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE t(a)")
            connection.execute("INSERT INTO t VALUES(1)")
            connection.commit()
            result["sqlite_wal_usable"] = (
                connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
        finally:
            connection.close()
        result["case_sensitive"] = not (probe / "MODE.JSON").exists()
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    result["ok"] = all(result.get(name) for name in (
        "preserves_mode_0600", "preserves_owner", "supports_symlinks",
        "sqlite_wal_usable"))
    return result


def sqlite_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.sqlite")
                  if path.is_file() and not path.is_symlink())


def checkpoint(database: Path) -> dict[str, Any]:
    """TRUNCATE checkpoint：把 WAL 折进主库，让 rsync 复制的是完整状态。"""

    connection = sqlite3.connect(database, timeout=30)
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    return {"database": str(database), "wal_checkpoint": list(row or []),
            "integrity": integrity}


def launch_agents(agents_dir: Path) -> list[tuple[str, Path]]:
    found: dict[str, Path] = {}
    for pattern in AGENT_PATTERNS:
        for path in sorted(agents_dir.glob(pattern)):
            found[path.stem] = path
    return sorted(found.items())


def launchctl(action: str, label: str, plist_path: Path) -> str:
    domain = f"gui/{os.getuid()}"
    command = (["launchctl", "bootout", f"{domain}/{label}"] if action == "stop"
               else ["launchctl", "bootstrap", domain, str(plist_path)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return f"{' '.join(command)} -> {completed.returncode} {completed.stderr.strip()[:200]}"


def rsync_flags() -> list[str]:
    """macOS 自带的是 openrsync：没有 -A/-X，扩展属性用长选项。"""

    probe = subprocess.run(["rsync", "--version"], capture_output=True, text=True, check=False)
    if "openrsync" in (probe.stdout + probe.stderr):
        return ["-aH", "--extended-attributes", "--delete"]
    return ["-aHAX", "--delete"]


def rsync(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = ["rsync", *rsync_flags(), f"{source}/", f"{target}/"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise MigrationError(f"rsync 失败：{completed.stderr.strip()[:1000]}")
    return " ".join(command)


def units(args: argparse.Namespace) -> list[dict[str, Any]]:
    """要迁的每一组：源目录、目标目录、软链粒度和理由。"""

    destination = args.dest.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    legacy = args.legacy_state.expanduser().resolve()
    if legacy.is_dir():
        rows.append({"name": "legacy", "source": legacy,
                     "target": destination / "legacy-state",
                     "reason": "legacy 没有 workspace manifest，state 这一层做软链是安全的"})
    workspaces = args.workspaces.expanduser().resolve()
    if workspaces.is_dir():
        rows.append({
            "name": "workspaces", "source": workspaces,
            "target": destination / "workspaces",
            "reason": ("必须整目录做软链：只链 <slug>/state 会让 workspace.py 的 "
                       "_inside() 判定 state_dir escapes workspace_root，两个 "
                       "workspace 的服务全部起不来")})
    if args.backup_root is not None:
        backups = args.backup_root.expanduser().resolve()
        if backups.is_dir():
            rows.append({"name": "backups", "source": backups,
                         "target": destination / "backups",
                         "reason": "备份是最大的一块，也是最适合放外置盘的一块"})
    if args.include_releases:
        releases = args.releases.expanduser().resolve()
        if releases.is_dir():
            rows.append({
                "name": "releases", "source": releases,
                "target": destination / "releases",
                "reason": "显式要求；注意盘没挂上时所有服务都无法启动"})
    return rows


def plan(args: argparse.Namespace) -> dict[str, Any]:
    destination = args.dest.expanduser().resolve()
    rows = units(args)
    if not rows:
        raise MigrationError("没有发现任何要迁的目录")
    sized = []
    total = 0
    for row in rows:
        size = tree_bytes(row["source"])
        total += size
        sized.append({"name": row["name"], "source": str(row["source"]),
                      "target": str(row["target"]), "bytes": size,
                      "symlink_reason": row["reason"],
                      "sqlite_files": [str(path) for path in sqlite_files(row["source"])]})
    mount = mount_info(destination)
    try:
        free = shutil.disk_usage(destination if destination.exists()
                                 else destination.parent).free
    except OSError as exc:
        raise MigrationError(f"无法读取目标盘可用空间：{exc}") from exc
    # 迁移期间源和目标同时存在，目标盘至少要放得下全部内容再加一倍余量。
    required = int(total * args.free_space_multiple)
    agents = launch_agents(args.launch_agents_dir.expanduser().resolve())
    blocking: list[str] = []
    warnings: list[str] = []
    if mount["read_only"]:
        blocking.append("目标盘是只读挂载")
    if mount["network"]:
        blocking.append("目标是网络文件系统，SQLite 的锁在上面不可靠")
    if free < required:
        blocking.append(f"目标盘可用 {free} 字节，需要 {required} 字节")
    if mount["no_owners"]:
        warnings.append(
            "目标盘以 noowners 挂载：属主检查会一律通过，0700/0600 的"
            "\"只有本人能看\"约束在这块盘上不再由内核保证。想保留这条约束，"
            "先 `sudo diskutil enableOwnership /Volumes/<卷名>` 再迁。")
    if mount["mount_point"] == "/":
        warnings.append("目标解析到根卷：外置盘可能没挂上，现在迁只会把数据搬到内置盘")
    return {
        "schema_version": SCHEMA,
        "writes_performed": False,
        "destination": str(destination),
        "mount": mount,
        "units": sized,
        "total_bytes": total,
        "required_bytes": required,
        "free_bytes": free,
        "launch_agents": [label for label, _ in agents],
        "blocking": blocking,
        "warnings": warnings,
        "symlink_versus_config_rewrite": (
            "选软链：改配置要改遍 10 个 plist、manager.json、2 个 workspace.json、"
            "每个 workspace 的 service.json、21 个模型配置、发布 worker 配置的 8 个"
            "路径、connector-governance 与 discovery-plans，漏一处就是两个进程打开"
            "两个 core.sqlite 的静默脑裂；软链是一次 rename，没有中间态，而且 "
            "writer socket 路径字符串不变（Darwin sun_path 只有 103 字节）。"),
        "steps": [
            "停止全部 LaunchAgent（先 controller / control，再 writer）",
            "对每个 *.sqlite 做 PRAGMA wal_checkpoint(TRUNCATE) 并 integrity_check",
            "在目标盘上做一次能力探测（权限位、属主、软链、WAL）",
            "rsync（openrsync: -aH --extended-attributes --delete）每一组到目标盘",
            "把原目录改名为 <dir>.pre-migration-<时间戳>（保留，供回滚）",
            "在原路径建立指向目标盘的符号链接",
            "启动全部 LaunchAgent",
            "健康校验：writer socket 可连、controller 心跳前进",
        ],
        "rollback": [
            f"scripts/migrate_state_dir.py --dest {destination} --rollback --apply",
            "（即：停服务 → 删软链 → 把 <dir>.pre-migration-<时间戳> 改回原名 → 起服务）",
        ],
    }


def health(args: argparse.Namespace) -> dict[str, Any]:
    import socket as socket_module
    socket_path = args.writer_socket.expanduser()
    readable = False
    if socket_path.exists():
        probe = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        try:
            probe.settimeout(5)
            probe.connect(os.fspath(socket_path))
            readable = True
        except OSError:
            readable = False
        finally:
            probe.close()
    heartbeat_path = args.legacy_state.expanduser() / "dalton-core" / "run" / "heartbeat.json"

    def tick() -> Any:
        try:
            value = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value.get("last_projection_at") if isinstance(value, dict) else None

    before = tick()
    time.sleep(args.heartbeat_wait_seconds)
    after = tick()
    advanced = (isinstance(before, str) and isinstance(after, str) and after > before)
    return {"writer_socket_readable": readable, "heartbeat_before": before,
            "heartbeat_after": after, "controller_heartbeat_advanced": advanced,
            "ok": readable and (advanced or before is None)}


def rebind_workspaces(target: Path, launch_agents_dir: Path) -> list[str]:
    """迁移后每个工作区的 service.json 与 plist 必须指向解析后的真实路径。

    运行时把 manifest 路径与各数据库路径 resolve() 之后逐字比较，而 service.json
    里写的是迁移前的字面路径，不改写服务会以「service config paths do not match
    the bound workspace manifest」拒绝启动。另外 launchd 打不开外置卷上的
    stdout/stderr（EX_CONFIG 78），所以 plist 的日志文件改到启动卷上。
    """

    actions: list[str] = []
    home = Path.home()
    for manifest in sorted(target.glob("*/workspace.json")):
        real_root = manifest.parent.resolve()
        slug = real_root.name
        old_root = home / ".dalton" / "workspaces" / slug
        service_path = real_root / "config" / "service.json"
        if service_path.is_file():
            text = service_path.read_text(encoding="utf-8")
            replaced = text.replace(str(old_root), str(real_root))
            record = json.loads(replaced)
            binding = dict(record.get("workspace") or {})
            binding["manifest_path"] = str(manifest.resolve())
            record["workspace"] = binding
            temporary = service_path.with_name(".service.json.migrate.tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, service_path)
            actions.append(f"rebound {slug}/config/service.json to {real_root}")
        log_dir = home / "Library" / "Logs" / "Dalton" / "workspaces" / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        for plist_path in launch_agents_dir.glob(f"space.lumos.dalton.workspace.{slug}.*.plist"):
            import plistlib
            with plist_path.open("rb") as stream:
                data = plistlib.load(stream)
            role = plist_path.stem.rsplit(".", 1)[-1]
            data["StandardOutPath"] = str(log_dir / f"{role}.stdout.log")
            data["StandardErrorPath"] = str(log_dir / f"{role}.stderr.log")
            with plist_path.open("wb") as stream:
                plistlib.dump(data, stream)
            actions.append(f"launchd logs for {plist_path.name} -> {log_dir}")
    return actions


def apply_migration(args: argparse.Namespace, prepared: dict[str, Any]) -> dict[str, Any]:
    if prepared["blocking"]:
        raise MigrationError("预检不通过：" + "；".join(prepared["blocking"]))
    probe = target_capability_probe(Path(prepared["destination"]))
    if not probe["ok"]:
        raise MigrationError("目标盘能力探测不通过：" + json.dumps(probe, ensure_ascii=False))
    agents = launch_agents(args.launch_agents_dir.expanduser().resolve())
    actions: list[str] = [f"probe={json.dumps(probe, ensure_ascii=False)}"]
    for label, path in agents:
        actions.append(launchctl("stop", label, path))
    time.sleep(args.stop_settle_seconds)
    checkpoints = []
    for row in prepared["units"]:
        for database in row["sqlite_files"]:
            checkpoints.append(checkpoint(Path(database)))
    bad = [item for item in checkpoints if item["integrity"] != "ok"]
    if bad:
        for label, path in agents:
            actions.append(launchctl("start", label, path))
        raise MigrationError("有数据库 integrity_check 未通过，已原样恢复服务："
                             + json.dumps(bad, ensure_ascii=False))
    moved = []
    marker = stamp()
    try:
        for row in prepared["units"]:
            source = Path(row["source"])
            target = Path(row["target"])
            actions.append(rsync(source, target))
            kept = source.with_name(f"{source.name}.pre-migration-{marker}")
            os.rename(source, kept)
            os.symlink(target, source)
            moved.append({"name": row["name"], "source": str(source),
                          "target": str(target), "preserved": str(kept)})
            if row["name"] == "workspaces":
                actions.extend(rebind_workspaces(target, args.launch_agents_dir.expanduser().resolve()))
    except Exception as exc:
        # rsync 在改名之前失败时源目录原封不动；已经改名的组软链是完整的。
        # 无论哪种情况服务都必须回来，不能让一次复制失败变成整套系统停机。
        for label, path in agents:
            actions.append(launchctl("start", label, path))
        raise MigrationError(
            f"{exc}；服务已重新启动，已迁移的组：{[row['name'] for row in moved]}") from exc
    for label, path in agents:
        actions.append(launchctl("start", label, path))
    time.sleep(args.start_settle_seconds)
    return {"actions": actions, "checkpoints": checkpoints, "moved": moved,
            "preserved_marker": marker, "health": health(args)}


def apply_rollback(args: argparse.Namespace) -> dict[str, Any]:
    agents = launch_agents(args.launch_agents_dir.expanduser().resolve())
    actions = []
    for label, path in agents:
        actions.append(launchctl("stop", label, path))
    time.sleep(args.stop_settle_seconds)
    restored = []
    for row in units(args):
        source = Path(row["source"])
        candidates = sorted(source.parent.glob(f"{source.name}.pre-migration-*"))
        if not candidates:
            continue
        kept = candidates[-1]
        if source.is_symlink():
            source.unlink()
        elif source.exists():
            raise MigrationError(f"{source} 不是符号链接，拒绝覆盖")
        os.rename(kept, source)
        restored.append({"source": str(source), "restored_from": str(kept)})
    for label, path in agents:
        actions.append(launchctl("start", label, path))
    time.sleep(args.start_settle_seconds)
    return {"actions": actions, "restored": restored, "health": health(args)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, required=True,
                        help="外置盘上的目标根目录，例如 /Volumes/EveSSD/Dalton")
    parser.add_argument("--legacy-state", type=Path, default=DEFAULT_LEGACY)
    parser.add_argument("--workspaces", type=Path, default=DEFAULT_WORKSPACES)
    parser.add_argument("--releases", type=Path, default=DEFAULT_RELEASES)
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument("--include-releases", action="store_true")
    parser.add_argument("--launch-agents-dir", type=Path,
                        default=HOME / "Library" / "LaunchAgents")
    parser.add_argument("--writer-socket", type=Path,
                        default=DEFAULT_LEGACY / "dalton-core" / "run" / "writer.sock")
    parser.add_argument("--free-space-multiple", type=float, default=2.0)
    parser.add_argument("--stop-settle-seconds", type=float, default=10.0)
    parser.add_argument("--start-settle-seconds", type=float, default=20.0)
    parser.add_argument("--heartbeat-wait-seconds", type=float, default=30.0)
    parser.add_argument("--rollback", action="store_true",
                        help="撤销上一次迁移：删软链，把保留的原目录改回去")
    parser.add_argument("--apply", action="store_true", help="真正执行；缺省只打印计划")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            result = {"schema_version": SCHEMA, "mode": "rollback",
                      "writes_performed": bool(args.apply),
                      "units": [{"name": row["name"], "source": str(row["source"])}
                                for row in units(args)]}
            if args.apply:
                result["rollback"] = apply_rollback(args)
        else:
            result = plan(args)
            if args.apply:
                result["applied"] = apply_migration(args, result)
                result["writes_performed"] = True
    except (MigrationError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("blocking"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
