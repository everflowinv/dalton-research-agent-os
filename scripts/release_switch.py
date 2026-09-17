#!/usr/bin/env python3
"""把整套 Dalton 服务原子地切到一个发布，并把所有"指针"一起改过去。

point_services_at_release.sh 只认 space.lumos.dalton*.plist，于是
com.dalton.research-publication-worker 被漏掉；它也完全不动 manager.json、
workspace.json、current-release.json / current-runtime-config.json 和发布 worker
配置里的 publication_gate。结果是 2026-09-16 的现状：

    9 个 space.lumos.* 服务  → 37b73370
    发布 worker             → d1f2f062
    manager.json / 两个 workspace / gate → 23986f7a

三份互相矛盾的"当前发布"。本脚本把它们当成一件事来做：先全部校验，再全部写，
最后统一健康校验；任何一步不通过就停下，不做一半。

默认 dry-run：打印将要做的每一次修改（含逐条 diff 摘要），不写任何东西。
--apply 才落地。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "dalton-release-switch:0.1"
RELEASE_POINTER_SCHEMA = "dalton-current-release-0.2"
RUNTIME_POINTER_SCHEMA = "dalton-runtime-config-pointer-0.2"
AGENT_PATTERNS = ("space.lumos.dalton*.plist", "com.dalton.*.plist")
HOME = Path.home()


class SwitchError(RuntimeError):
    """切换被拒绝。"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SwitchError(f"{path} 不是一个 JSON 对象")
    return value


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> str:
    """同目录临时文件 + fsync + rename，读者只会看到旧的或新的。"""

    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.switch.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return sha256_bytes(body.encode("utf-8"))


# ---------------------------------------------------------------- discovery

def discover_agents(agents_dir: Path) -> list[Path]:
    """每一个 Dalton LaunchAgent，两种命名都要。"""

    found: set[Path] = set()
    for pattern in AGENT_PATTERNS:
        found.update(agents_dir.glob(pattern))
    return sorted(found)


def agent_release(plist_path: Path) -> tuple[str | None, dict[str, Any]]:
    with plist_path.open("rb") as stream:
        data = plistlib.load(stream)
    arguments = data.get("ProgramArguments") or []
    executable = str(arguments[0]) if arguments else ""
    match = re.search(r"/runtime/releases/([0-9a-f]{64})/venv/bin/python$", executable)
    return (match.group(1) if match else None), data


def release_of(path: str) -> str | None:
    match = re.search(r"/runtime/releases/([0-9a-f]{64})/venv", str(path))
    return match.group(1) if match else None


# ------------------------------------------------------------------- plan

def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    release_root = args.release.expanduser().resolve()
    if release_root.name == "venv":
        release_root = release_root.parent
    digest = release_root.name
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SwitchError("发布目录名必须是 64 位小写 sha256")
    venv = release_root / "venv"
    python = venv / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SwitchError(f"发布里没有可执行的 python：{python}")
    release_ref = f"release:sha256:{digest}"

    manifest_path = release_root / "release-manifest.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        source_commit = str(manifest.get("source_commit", ""))
        candidate_manifest_sha256 = sha256_file(manifest_path)
    else:
        manifest = None
        source_commit = args.source_commit or ""
        candidate_manifest_sha256 = args.candidate_manifest_sha256 or ""
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise SwitchError(
            "缺少 source_commit：发布里没有 release-manifest.json 时必须显式 --source-commit")
    if re.fullmatch(r"[0-9a-f]{64}", candidate_manifest_sha256) is None:
        raise SwitchError(
            "缺少 candidate_manifest_sha256：没有 release-manifest.json 时必须显式给出")

    agents_dir = args.launch_agents_dir.expanduser().resolve()
    agents = discover_agents(agents_dir)
    if not agents:
        raise SwitchError("没有发现任何 Dalton LaunchAgent")
    agent_rows = []
    for plist_path in agents:
        current, data = agent_release(plist_path)
        agent_rows.append({
            "label": data.get("Label") or plist_path.stem,
            "plist": str(plist_path),
            "current_release": current,
            "needs_change": current != digest,
            "has_stderr_path": bool(data.get("StandardErrorPath")),
        })

    manager_path = args.manager.expanduser().resolve()
    manager = read_json(manager_path)
    workspaces = sorted((args.host_root.expanduser().resolve() / "workspaces")
                        .glob("*/workspace.json"))
    workspace_rows = []
    for path in workspaces:
        record = read_json(path)
        workspace_rows.append({
            "path": str(path), "slug": record.get("slug"),
            "current_release": release_of(record.get("release_path", "")),
            "needs_change": record.get("release_ref") != release_ref,
        })

    worker_config_path = args.worker_config.expanduser().resolve()
    worker_config = read_json(worker_config_path) if worker_config_path.is_file() else None
    gate = (worker_config or {}).get("publication_gate") or {}

    release_pointer = Path(gate.get("release_pointer") or args.release_pointer or "")
    runtime_pointer = Path(gate.get("runtime_pointer") or args.runtime_pointer or "")
    if not release_pointer.is_absolute() or not runtime_pointer.is_absolute():
        raise SwitchError("发布指针路径必须是绝对路径（取自 worker 配置或命令行）")

    return {
        "schema_version": SCHEMA,
        "writes_performed": False,
        "release_hash": digest,
        "release_ref": release_ref,
        "release_path": str(venv),
        "source_commit": source_commit,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "release_manifest": str(manifest_path) if manifest else None,
        "launch_agents": agent_rows,
        "launch_agents_without_log_paths": [
            row["label"] for row in agent_rows if not row["has_stderr_path"]],
        "manager": {
            "path": str(manager_path),
            "current_release_ref": manager.get("release_ref"),
            "needs_change": manager.get("release_ref") != release_ref,
        },
        "workspaces": workspace_rows,
        "worker_config": {
            "path": str(worker_config_path),
            "current_expected_release_ref": gate.get("expected_release_ref"),
            "current_expected_source_commit": gate.get("expected_source_commit"),
            "needs_change": (gate.get("expected_release_ref") != release_ref
                             or gate.get("expected_source_commit") != source_commit),
        },
        "release_pointer": str(release_pointer),
        "runtime_pointer": str(runtime_pointer),
        "drift_before": sorted({
            *(row["current_release"] for row in agent_rows if row["current_release"]),
            *(row["current_release"] for row in workspace_rows if row["current_release"]),
            *( [release_of(manager.get("release_path", ""))]
               if release_of(manager.get("release_path", "")) else []),
        }),
    }


# ------------------------------------------------------------------ writes

def _backup(path: Path, backup_dir: Path) -> str | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    target = backup_dir / f"{path.name}.{abs(hash(str(path))):x}"
    shutil.copy2(path, target)
    return str(target)


def repoint_agent(plist_path: Path, venv: Path) -> bool:
    with plist_path.open("rb") as stream:
        data = plistlib.load(stream)
    arguments = list(data.get("ProgramArguments") or [])
    if not arguments:
        raise SwitchError(f"{plist_path} 没有 ProgramArguments")
    wanted = str(venv / "bin" / "python")
    if arguments[0] == wanted:
        return False
    arguments[0] = wanted
    data["ProgramArguments"] = arguments
    temporary = plist_path.with_name(f".{plist_path.name}.switch.tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, plist_path.stat().st_mode & 0o777)
    os.replace(temporary, plist_path)
    # 写完立刻回读：一次静默失败曾让全部服务留在旧发布上一整轮巡检。
    confirmed, _ = agent_release(plist_path)
    if str(plist_path).endswith(".plist"):
        with plist_path.open("rb") as stream:
            after = plistlib.load(stream)
        if (after.get("ProgramArguments") or [None])[0] != wanted:
            raise SwitchError(f"{plist_path} 回读结果与写入不一致")
    return True


def launchctl(action: str, label: str, plist_path: Path) -> str:
    domain = f"gui/{os.getuid()}"
    if action == "stop":
        command = ["launchctl", "bootout", f"{domain}/{label}"]
    else:
        command = ["launchctl", "bootstrap", domain, str(plist_path)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return f"{' '.join(command)} -> {completed.returncode} {completed.stderr.strip()[:200]}"


def write_pointers(plan: dict[str, Any], *, release_pointer: Path,
                   runtime_pointer: Path) -> dict[str, Any]:
    """两个发布指针，按 published_runtime_gate 的字段契约写。

    gate 要求：两份都 deployed_verified；release.release_ref /
    release.source_commit / runtime.base_release_commit 与 worker 配置一致；
    release.current_runtime_config_sha256 等于 runtime 指针文件的 sha256；两份的
    candidate_manifest_sha256 相同。runtime 必须先写，因为 release 里要记它的
    哈希；中间态会让 gate 停在 waiting，不会让任何工作开始，这正是想要的。
    """

    previous_release = read_json(release_pointer) if release_pointer.is_file() else {}
    previous_runtime = read_json(runtime_pointer) if runtime_pointer.is_file() else {}
    runtime_value = {
        **{key: value for key, value in previous_runtime.items()
           if key not in {"schema_version", "status", "at", "base_release_commit",
                          "candidate_manifest_sha256", "prior_runtime_config_sha256"}},
        "schema_version": RUNTIME_POINTER_SCHEMA,
        "status": "deployed_verified",
        "at": now(),
        "base_release_commit": plan["source_commit"],
        "candidate_manifest_sha256": plan["candidate_manifest_sha256"],
        "prior_runtime_config_sha256": (
            sha256_file(runtime_pointer) if runtime_pointer.is_file() else None),
    }
    runtime_sha = atomic_write_json(runtime_pointer, runtime_value)
    release_value = {
        **{key: value for key, value in previous_release.items()
           if key not in {"schema_version", "status", "release_ref", "source_commit",
                          "candidate_manifest_sha256", "current_runtime_config_sha256",
                          "verified_at", "previous_release_sha256"}},
        "schema_version": RELEASE_POINTER_SCHEMA,
        "status": "deployed_verified",
        "release_ref": plan["release_ref"],
        "source_commit": plan["source_commit"],
        "candidate_manifest_sha256": plan["candidate_manifest_sha256"],
        "current_runtime_config_sha256": runtime_sha,
        "previous_release_sha256": (
            sha256_file(release_pointer) if release_pointer.is_file() else None),
        "verified_at": now(),
    }
    release_sha = atomic_write_json(release_pointer, release_value)
    return {"runtime_pointer_sha256": runtime_sha, "release_pointer_sha256": release_sha}


def apply_switch(args: argparse.Namespace, plan: dict[str, Any]) -> dict[str, Any]:
    venv = Path(plan["release_path"])
    backup_dir = args.backup_dir.expanduser().resolve()
    actions: list[str] = []

    # 1) plist
    labels: list[tuple[str, Path]] = []
    for row in plan["launch_agents"]:
        path = Path(row["plist"])
        _backup(path, backup_dir)
        if repoint_agent(path, venv):
            actions.append(f"repointed {row['label']}")
        labels.append((row["label"], path))

    # 2) manager.json
    manager_path = Path(plan["manager"]["path"])
    _backup(manager_path, backup_dir)
    manager = read_json(manager_path)
    manager["release_path"] = plan["release_path"]
    manager["release_ref"] = plan["release_ref"]
    manager["shared_readonly_paths"] = sorted(
        set(manager.get("shared_readonly_paths") or []) | {plan["release_path"]})
    atomic_write_json(manager_path, manager)
    actions.append("updated manager.json")

    # 3) workspace.json（content_hash 是对除它自身以外的全部字段算的）
    for row in plan["workspaces"]:
        path = Path(row["path"])
        _backup(path, backup_dir)
        record = read_json(path)
        record["release_path"] = plan["release_path"]
        record["release_ref"] = plan["release_ref"]
        record["shared_readonly_paths"] = sorted(
            set(record.get("shared_readonly_paths") or []) | {plan["release_path"]})
        body = {key: value for key, value in record.items() if key != "content_hash"}
        record["content_hash"] = content_hash(body)
        atomic_write_json(path, record)
        actions.append(f"updated workspace {row['slug']}")
        # service.json 里的 workspace.manifest_hash 绑定的是 workspace.json 的
        # content_hash；manifest 一改，不同步这里服务会拒绝启动
        # （"service config paths do not match the bound workspace manifest"）。
        service_path = Path(str(record.get("config_path") or path.parent / "config" / "service.json"))
        if service_path.is_file():
            _backup(service_path, backup_dir)
            service = read_json(service_path)
            binding = dict(service.get("workspace") or {})
            if binding.get("manifest_hash") != record["content_hash"]:
                binding["manifest_hash"] = record["content_hash"]
                service["workspace"] = binding
                atomic_write_json(service_path, service)
                actions.append(f"synced workspace manifest hash into {service_path.name} ({row['slug']})")

    # 4) 两个发布指针
    pointers = write_pointers(
        plan, release_pointer=Path(plan["release_pointer"]),
        runtime_pointer=Path(plan["runtime_pointer"]))
    actions.append("rewrote current-release.json / current-runtime-config.json")

    # 5) 发布 worker 配置的 gate 绑定
    worker_path = Path(plan["worker_config"]["path"])
    if worker_path.is_file():
        _backup(worker_path, backup_dir)
        worker = read_json(worker_path)
        gate = dict(worker.get("publication_gate") or {})
        gate["expected_release_ref"] = plan["release_ref"]
        gate["expected_source_commit"] = plan["source_commit"]
        worker["publication_gate"] = gate
        atomic_write_json(worker_path, worker)
        actions.append("updated publication worker gate binding")

    # 6) 重启
    for label, path in labels:
        actions.append(launchctl("stop", label, path))
    time.sleep(3)
    for label, path in labels:
        actions.append(launchctl("start", label, path))
    # launchd 在 bootout 之后的几秒里仍持有旧标签（"Bootstrap failed: 5: Input/output
    # error"），legacy 的 writer/controller 两次发布都撞到。等一会儿再对失败的
    # 标签重试，而不是把它们留给下一轮巡检。
    retry_labels = [(label, path) for (label, path), line in zip(labels, actions[-len(labels):])
                    if "-> 0" not in line]
    if retry_labels:
        time.sleep(6)
        for label, path in retry_labels:
            launchctl("stop", label, path)
        time.sleep(3)
        for label, path in retry_labels:
            actions.append("retry " + launchctl("start", label, path))
    time.sleep(args.settle_seconds)
    return {"actions": actions, **pointers}


# ------------------------------------------------------------------ health

def controller_heartbeat(state_dir: Path) -> str | None:
    """控制器最后一次投影的时间；它前进就说明 tick 循环还活着。"""

    path = state_dir / "run" / "heartbeat.json"
    if not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, ValueError, SwitchError):
        return None
    for key in ("last_projection_at", "at", "updated_at"):
        if isinstance(value.get(key), str):
            return value[key]
    return None


def writer_socket_readable(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(5)
        probe.connect(os.fspath(socket_path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def health(args: argparse.Namespace, plan: dict[str, Any]) -> dict[str, Any]:
    digest = plan["release_hash"]
    agents_dir = args.launch_agents_dir.expanduser().resolve()
    agent_state = []
    for path in discover_agents(agents_dir):
        current, data = agent_release(path)
        agent_state.append({"label": data.get("Label") or path.stem,
                            "release": current, "on_target": current == digest})
    manager = read_json(Path(plan["manager"]["path"]))
    before = controller_heartbeat(args.state_dir.expanduser().resolve())
    time.sleep(args.heartbeat_wait_seconds)
    after = controller_heartbeat(args.state_dir.expanduser().resolve())
    advanced = None
    if before is not None and after is not None:
        advanced = after > before
    checks = {
        "every_agent_on_target": all(row["on_target"] for row in agent_state),
        "manager_matches": manager.get("release_ref") == plan["release_ref"],
        "controller_heartbeat_advanced": advanced,
        "writer_socket_readable": writer_socket_readable(
            args.writer_socket.expanduser().resolve()),
        "agents": agent_state,
        "heartbeat_before": before,
        "heartbeat_after": after,
    }
    checks["ok"] = bool(checks["every_agent_on_target"] and checks["manager_matches"]
                        and checks["writer_socket_readable"]
                        and checks["controller_heartbeat_advanced"] is not False)
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("release", type=Path,
                        help="~/.dalton/runtime/releases/<sha> 或它下面的 venv")
    parser.add_argument("--launch-agents-dir", type=Path,
                        default=HOME / "Library" / "LaunchAgents")
    parser.add_argument("--manager", type=Path, default=HOME / ".dalton" / "manager.json")
    parser.add_argument("--host-root", type=Path, default=HOME / ".dalton")
    parser.add_argument("--state-dir", type=Path,
                        default=HOME / "Library" / "Application Support" / "Dalton"
                        / "state" / "dalton-core")
    parser.add_argument("--writer-socket", type=Path,
                        default=HOME / "Library" / "Application Support" / "Dalton"
                        / "state" / "dalton-core" / "run" / "writer.sock")
    parser.add_argument("--worker-config", type=Path,
                        default=HOME / "Library" / "Application Support" / "Dalton"
                        / "state" / "dalton-core"
                        / "research-publication-worker-config.json")
    parser.add_argument("--release-pointer", type=Path, default=None)
    parser.add_argument("--runtime-pointer", type=Path, default=None)
    parser.add_argument("--source-commit", default=None)
    parser.add_argument("--candidate-manifest-sha256", default=None)
    parser.add_argument("--backup-dir", type=Path,
                        default=HOME / ".dalton" / "release-switch-backups")
    parser.add_argument("--settle-seconds", type=float, default=15.0)
    parser.add_argument("--heartbeat-wait-seconds", type=float, default=20.0)
    parser.add_argument("--health-only", action="store_true",
                        help="只做健康校验，不改任何东西")
    parser.add_argument("--apply", action="store_true", help="真正切换；缺省只打印计划")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args)
        if args.health_only:
            plan["health"] = health(args, plan)
        elif args.apply:
            plan["applied"] = apply_switch(args, plan)
            plan["writes_performed"] = True
            plan["health"] = health(args, plan)
    except (SwitchError, OSError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    if plan.get("health") is not None and not plan["health"]["ok"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
