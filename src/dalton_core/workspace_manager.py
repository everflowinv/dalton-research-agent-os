"""Owner-scoped workspace creation, outside any research writer namespace.

The web process only launches this fixed module with an operator-installed
configuration. Request data cannot select filesystem paths, ports or commands.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .workspace import WorkspaceError, load_workspace_manifest

CONFIG_ENV = "DALTON_WORKSPACE_MANAGER_CONFIG"


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".workspace-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _config(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError("研究环境管理配置不可用")
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise WorkspaceError("研究环境管理配置必须由当前用户独占管理")
    raw = json.loads(path.read_text())
    required = {"host_root", "release_path", "release_ref", "owner_login", "tailscale_host",
                "tailscale_executable", "launch_agents_dir", "ports"}
    optional = {"shared_readonly_paths", "shared_model_capacity_bindings", "shared_connector_capacity",
                "legacy_workspace", "connections_path", "runtime_templates",
                "shared_call_budget_policy_path"}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
        raise WorkspaceError("研究环境管理配置格式无效")
    for key in ("host_root", "release_path", "tailscale_executable", "launch_agents_dir"):
        if not isinstance(raw[key], str) or not Path(raw[key]).is_absolute():
            raise WorkspaceError("研究环境管理路径无效")
    if not isinstance(raw["owner_login"], str) or not raw["owner_login"].strip():
        raise WorkspaceError("研究环境管理者未配置")
    host = raw["tailscale_host"]
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        raise WorkspaceError("研究环境访问地址无效")
    ports = raw["ports"]
    if not isinstance(ports, list) or not ports or len(ports) > 32 or len(set(ports)) != len(ports) \
            or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
        raise WorkspaceError("研究环境可用端口无效")
    if raw.get("connections_path") is not None:
        connection_path = raw["connections_path"]
        if not isinstance(connection_path, str) or not Path(connection_path).is_absolute():
            raise WorkspaceError("共享连接目录路径无效")
    if raw.get("shared_call_budget_policy_path") is not None:
        value = raw["shared_call_budget_policy_path"]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise WorkspaceError("共享模型费用策略路径无效")
    templates = raw.get("runtime_templates")
    if templates is not None:
        if not isinstance(templates, dict) or set(templates) != {"model", "service"}:
            raise WorkspaceError("研究运行模板配置无效")
        for binding in templates.values():
            if (not isinstance(binding, dict) or set(binding) != {"path", "sha256"}
                    or not isinstance(binding["path"], str)
                    or not Path(binding["path"]).is_absolute()
                    or not isinstance(binding["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", binding["sha256"])):
                raise WorkspaceError("研究运行模板绑定无效")
    return raw


def _runtime_templates(config: Mapping[str, Any]) -> dict[str, Path]:
    """Check the operator-pinned immutable templates before provisioning."""
    result = {}
    for name, binding in config.get("runtime_templates", {}).items():
        path = Path(binding["path"])
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022
                or hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]):
            raise WorkspaceError("研究运行模板校验失败")
        result[name] = path.resolve()
    return result


def provision_runtime(config: Mapping[str, Any], manifest: Path, *, login: str) -> dict[str, Any] | None:
    """Reuse the installed OS before starting this workspace's processes."""
    templates = _runtime_templates(config)
    if not templates:
        return None  # Backward compatible low-level blank-state installation.
    from .workspace_model_setup import install_runtime_template
    from .workspace_service_setup import install_service_template
    from .workspace_runtime_setup import install
    from .workspace_control_setup import configure_workspace_control
    # Establish the base control shape before installing reusable authorities.
    # Its bootstrap may create the empty router and base principals; installing
    # model/runtime state afterwards makes those writes final and observable.
    configure_workspace_control(manifest, owner_login=login,
        tailscale_host=config["tailscale_host"], tailscale_executable=config["tailscale_executable"])
    model = install_runtime_template(manifest, templates["model"])
    runtime = install(manifest, actor_ref="human:tailscale-" + hashlib.sha256(login.encode()).hexdigest()[:32])
    service = install_service_template(manifest, templates["service"])
    workspace = load_workspace_manifest(manifest)
    receipt = {"schema_version": "dalton-workspace-runtime-ready-0.1",
               "workspace_id": workspace.workspace_id,
               "model": model, "runtime": runtime, "service": service,
               "template_sha256": {key: value["sha256"] for key, value in config["runtime_templates"].items()}}
    _write(workspace.state_dir / "runtime-ready.json", receipt)
    return receipt


def _runtime_shared_paths(templates: Mapping[str, Path]) -> list[str]:
    if not templates:
        return []
    from .workspace_model_setup import _validate_bundle
    from .workspace_service_setup import _validate_template
    model = _validate_bundle(json.loads(templates["model"].read_text()))
    service = _validate_template(json.loads(templates["service"].read_text()))
    if any(model["broker"][key] != service["broker"][key]
           for key in ("socket_path", "auth_key_path")):
        raise WorkspaceError("模型与研究引擎连接不一致")
    shared = list(dict.fromkeys([*service["shared_readonly_paths"],
                                *model.get("shared_readonly_paths", [])]))
    if any(not isinstance(path, str) or not Path(path).is_absolute() for path in shared):
        raise WorkspaceError("研究运行模板共享路径无效")
    return [str(Path(path).resolve()) for path in shared]


def set_shared_call_budget(config_path: Path, login: str, purpose: str,
                           max_cost_usd: float, expected_hash: str) -> dict[str, Any]:
    """CAS one host-owned purpose ceiling; callable only by the manager child."""
    from .model_selection import PURPOSE_LABELS
    from .shared_call_budget_policy import (SCHEMA_VERSION,
        load_shared_call_budget_policy)
    from .store import content_hash
    config = _config(config_path)
    if login != config["owner_login"]:
        raise PermissionError("workspace owner mismatch")
    if purpose not in PURPOSE_LABELS:
        raise WorkspaceError("模型调用用途无效")
    from .call_budget import validate_budget_overrides
    try:
        max_cost_usd = validate_budget_overrides(
            {"max_cost_usd": max_cost_usd})["max_cost_usd"]
    except ValueError as exc:
        raise WorkspaceError("模型单次费用上限无效") from exc
    target = Path(config.get("shared_call_budget_policy_path", ""))
    if not target.is_absolute():
        raise WorkspaceError("共享模型费用策略尚未配置")
    if (target.is_symlink() or not target.is_file() or target.stat().st_uid != os.getuid()
            or target.stat().st_mode & 0o022):
        raise WorkspaceError("共享模型费用策略必须由当前用户独占管理")
    lock = target.with_name("." + target.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        current = load_shared_call_budget_policy(target)
        if current["content_hash"] != expected_hash:
            raise WorkspaceError("共享模型费用策略已更新，请刷新后重试")
        purposes = dict(current["purpose_max_cost_usd"])
        if (purposes.get(purpose, current["default_max_cost_usd"])
                == float(max_cost_usd)):
            return {"status": "unchanged", "policy": current}
        purposes[purpose] = float(max_cost_usd)
        body = {"schema_version": SCHEMA_VERSION,
                "default_max_cost_usd": current["default_max_cost_usd"],
                "purpose_max_cost_usd": purposes,
                "revision": current["revision"] + 1,
                "prior_hash": current["content_hash"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "actor_ref": "human:" + hashlib.sha256(login.encode()).hexdigest()[:32]}
        policy = {**body, "content_hash": content_hash(body)}
        receipt_dir = Path(config["host_root"]) / "shared-call-budget-revisions"
        receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt = receipt_dir / (policy["content_hash"] + ".json")
        _write(receipt, policy)
        try: _write(target, policy)
        except Exception:
            receipt.unlink(missing_ok=True)
            raise
        return {"status": "updated", "policy": policy,
                "receipt_ref": "shared-call-budget-revision:" + policy["content_hash"]}


def request_set_shared_call_budget(config_path: Path, login: str,
        purpose: str, max_cost_usd: float, expected_hash: str) -> dict[str, Any]:
    config = _config(config_path)
    if login != config["owner_login"]:
        raise PermissionError("workspace owner mismatch")
    environment = dict(os.environ); environment.pop("DALTON_WORKSPACE_MANIFEST", None)
    result = subprocess.run([sys.executable, "-m", "dalton_core.workspace_manager",
        "--config", str(config_path), "--login", login, "--set-shared-purpose", purpose,
        "--max-cost-usd", str(max_cost_usd), "--expected-hash", expected_hash],
        env=environment, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise WorkspaceError("共享模型费用没有保存，请刷新后重试")
    return json.loads(result.stdout.splitlines()[-1])


def _catalog(config: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Pin the installed catalog and collect its exact read-only transport paths."""
    configured = config.get("connections_path")
    if configured is None:
        return None, []
    path = Path(configured).resolve()
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError("共享连接目录不可用")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "models", "sources", "content_hash"}:
        raise WorkspaceError("共享连接目录格式无效")
    from .store import content_hash
    body = {key: raw[key] for key in raw if key != "content_hash"}
    if raw["content_hash"] != content_hash(body):
        raise WorkspaceError("共享连接目录校验失败")
    transport_paths: list[str] = []
    for row in [*raw["models"], *raw["sources"]]:
        if not isinstance(row, dict) or not isinstance(row.get("transport"), dict):
            raise WorkspaceError("共享连接目录传输配置无效")
        for key in ("socket_path", "config_path"):
            value = row["transport"].get(key)
            if value is not None:
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise WorkspaceError("共享连接目录传输路径无效")
                transport_paths.append(str(Path(value).resolve()))
    return {"path": str(path), "content_hash": raw["content_hash"]}, [str(path), *transport_paths]


def _public(record: Mapping[str, Any], current_id: str | None) -> dict[str, Any]:
    slug = record.get("slug")
    return {key: record.get(key) for key in (
        "workspace_id", "name", "url", "status", "retryable", "failure_reason"
    )} | {
        "current": record.get("workspace_id") == current_id,
        "slug": slug if slug else "legacy",
    }


def list_workspaces(config_path: Path | None, login: str, current_id: str | None) -> dict[str, Any]:
    if config_path is None:
        return {"enabled": False, "can_create": False, "items": [],
                "create_disabled_reason": "研究环境管理尚未配置"}
    config = _config(config_path)
    if login != config["owner_login"]:
        raise PermissionError("workspace owner mismatch")
    items = []
    legacy = config.get("legacy_workspace")
    if legacy:
        row = _public(legacy, current_id or "legacy")
        # The legacy row's name in the manager config is the install-time
        # default; a cockpit rename writes display.json beside it instead.
        try:
            display = json.loads(
                (Path(config["host_root"]) / "legacy-display" / "display.json").read_text())
            if display.get("display_name"):
                row["name"] = display["display_name"]
        except (OSError, ValueError):
            pass
        items.append(row)
    for path in sorted((Path(config["host_root"]) / "creation-requests").glob("*.json")):
        record = json.loads(path.read_text())
        if record.get("owner_login") == login:
            items.append(_public(record, current_id))
    catalog, _ = _catalog(config)
    catalog_counts = {"models": 0, "sources": 0}
    if catalog is not None:
        catalog_raw = json.loads(Path(catalog["path"]).read_text(encoding="utf-8"))
        catalog_counts = {"models": len(catalog_raw["models"]),
                          "sources": len(catalog_raw["sources"])}
    return {"enabled": True, "can_create": True, "current_workspace_id": current_id or "legacy",
            "items": items, "isolation": "blank",
            "shared_connections": {**catalog_counts, "available": catalog is not None}}


def rename_workspace(config_path: Path, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Rename one workspace (or the legacy environment) from the cockpit.

    Presentation metadata only: the display name in ``display.json`` (hash
    re-bound) and the ``name`` on the creation-request record the list reads.
    No authority, route or Core state is touched, which is why this runs in
    the caller's process rather than out-of-process like creation.
    """

    if set(value) != {"slug", "name"}:
        raise WorkspaceError("请填写研究环境名称")
    slug, name = value["slug"], value["name"]
    if not isinstance(slug, str) or not re.fullmatch(r"(legacy|ws-[a-f0-9]{24})", slug):
        raise WorkspaceError("研究环境标识无效")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 \
            or any(ord(c) < 32 for c in name):
        raise WorkspaceError("名称需为 1 到 80 个字符")
    name = name.strip()
    config = _config(config_path)
    if config["owner_login"] != login:
        raise PermissionError("workspace owner mismatch")
    root = Path(config["host_root"])

    def _rename_display(workspace_root: Path) -> None:
        display = workspace_root / "display.json"
        wire = json.loads(display.read_text(encoding="utf-8")) if display.is_file() else {}
        body = {key: item for key, item in wire.items() if key != "content_hash"}
        body["display_name"] = name
        _write(display, {**body, "content_hash": _metadata_hash(body)})

    if slug == "legacy":
        _rename_display(root / "legacy-display")
        return {"status": "renamed", "slug": slug, "name": name}
    record_path = root / "creation-requests" / (slug.removeprefix("ws-") + ".json")
    if not record_path.is_file():
        raise WorkspaceError("没有这个研究环境")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("owner_login") != login:
        raise PermissionError("workspace owner mismatch")
    record["name"] = name
    _write(record_path, record)
    _rename_display(root / "workspaces" / slug)
    return {"status": "renamed", "slug": slug, "name": name}


def _metadata_hash(body: Mapping[str, Any]) -> str:
    from .store import content_hash
    return content_hash(body)


def request_create(config_path: Path, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"name", "request_id"}:
        raise WorkspaceError("请填写研究环境名称")
    name, request_id = value["name"], value["request_id"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 \
            or any(ord(c) < 32 for c in name):
        raise WorkspaceError("名称需为 1 到 80 个字符")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
        raise WorkspaceError("创建请求标识无效，请刷新后重试")
    config = _config(config_path)
    if config["owner_login"] != login:
        raise PermissionError("workspace owner mismatch")
    environment = dict(os.environ)
    environment.pop("DALTON_WORKSPACE_MANIFEST", None)
    result = subprocess.run(
        [sys.executable, "-m", "dalton_core.workspace_manager", "--config", str(config_path),
         "--login", login, "--name", name.strip(), "--request-id", request_id],
        env=environment, capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode:
        # Subprocess diagnostics can contain owner-local paths or credentials.
        raise WorkspaceError("研究环境尚未准备完成。可以重试同一次创建请求。")
    return json.loads(result.stdout.splitlines()[-1])


def _port(config: Mapping[str, Any], records: list[dict[str, Any]]) -> int:
    used = {r.get("port") for r in records}
    for port in config["ports"]:
        if port in used:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise WorkspaceError("可用研究环境数量已满")


def _serve(config: Mapping[str, Any], port: int) -> None:
    executable = config["tailscale_executable"]
    before = subprocess.run([executable, "serve", "status", "--json"], check=True,
                            capture_output=True, text=True)
    status = json.loads(before.stdout)
    # Never replace another application at this HTTPS port.
    existing = status.get("TCP", {}).get(str(port))
    web = status.get("Web", {}).get(f"{config['tailscale_host']}:{port}", {})
    target = f"http://127.0.0.1:{port}"
    if existing:
        if web.get("Handlers", {}).get("/", {}).get("Proxy") == target:
            return
        raise WorkspaceError("该访问端口已经用于其他服务")
    subprocess.run([executable, "serve", "--bg", f"--https={port}", target],
                   check=True, capture_output=True, text=True, timeout=30)
    after = subprocess.run([executable, "serve", "status", "--json"], check=True,
                           capture_output=True, text=True)
    observed = json.loads(after.stdout)
    if observed.get("Web", {}).get(f"{config['tailscale_host']}:{port}", {}).get(
            "Handlers", {}).get("/", {}).get("Proxy") != target:
        raise WorkspaceError("新研究环境访问地址尚未验证")
    for key in ("TCP", "Web"):
        if any(observed.get(key, {}).get(k) != v for k, v in status.get(key, {}).items()):
            raise WorkspaceError("已有访问地址发生变化，需要检查")


def _readiness(config: Mapping[str, Any], workspace: Any, *, timeout: float = 30.0,
               require_blank: bool = True) -> None:
    """Require Cockpit identity plus live writer and controller progress."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{workspace.cockpit_port}/v1/cockpit/overview"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            url, headers={"Tailscale-User-Login": config["owner_login"],
                          "Host": "127.0.0.1"})
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:  # noqa: S310
                payload = json.loads(response.read())
            if ((not require_blank or payload.get("state") == "awaiting_mission")
                    and payload.get("workspace", {}).get("workspace_id") == workspace.workspace_id):
                _writer_read_probe(workspace)
                _controller_tick_probe(workspace)
                return
            last_error = WorkspaceError("新研究环境返回了错误的身份或状态")
        except Exception as exc:  # readiness retries transport and incomplete startup
            last_error = exc
        time.sleep(0.1)
    raise WorkspaceError("新研究环境尚未通过空白状态检查") from last_error


def _writer_read_probe(workspace: Any) -> None:
    """Prove this workspace's writer answers an authenticated local read."""
    from .writer_client import WriterClient

    token_record = json.loads(
        (workspace.state_dir / "writer-tokens.json").read_text(encoding="utf-8"))
    tokens = {row["principal_id"]: row["token"]
              for row in token_record.get("principals", [])}
    if "core" not in tokens:
        raise WorkspaceError("新研究环境 writer 缺少本地读取身份")
    reply = WriterClient(str(workspace.writer_socket), tokens["core"]).call(
        "bounded_planner_active_loops", {})
    if reply.get("projection_kind") != "bounded_planner_active_loops":
        raise WorkspaceError("新研究环境 writer 未返回预期读取结果")


def _controller_tick_probe(workspace: Any) -> None:
    """Prove a live controller PID has written a heartbeat and scheduler tick."""
    from .service import ServiceConfig

    heartbeat_path = ServiceConfig.from_file(workspace.config_path).heartbeat_path
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    pid = heartbeat.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1 \
            or not heartbeat.get("last_tick_at"):
        raise WorkspaceError("新研究环境 controller 心跳尚未就绪")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise WorkspaceError("新研究环境 controller 进程未运行") from exc
    ledger = workspace.state_dir / "tick-ledger.sqlite"
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as connection:
        count = connection.execute(
            "SELECT count(*) FROM tick_ledger_ticks").fetchone()[0]
    if int(count) < 1:
        raise WorkspaceError("新研究环境 controller 尚未完成调度 tick")


def create_managed_workspace(config_path: Path, login: str, name: str, request_id: str) -> dict[str, Any]:
    from .workspace_creation import create_blank_workspace
    from .workspace_control_setup import configure_workspace_control
    from .workspace_process import install_workspace, label_namespace

    config = _config(config_path)
    if config["owner_login"] != login:
        raise PermissionError("workspace owner mismatch")
    root = Path(config["host_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256((login + "\0" + request_id).encode()).hexdigest()[:24]
    target = root / "creation-requests" / (key + ".json")
    with (root / ".creation.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            record = json.loads(target.read_text())
            if record["name"] != name or record["owner_login"] != login:
                raise WorkspaceError("这次创建请求已用于其他名称")
            if record["status"] == "running":
                try:
                    held = load_workspace_manifest(
                        root / "workspaces" / record["slug"] / "workspace.json")
                    _readiness(config, held, timeout=2.0, require_blank=False)
                    return {"status": "running", "workspace": _public(record, None)}
                except (OSError, ValueError, WorkspaceError):
                    record.update(status="failed", retryable=True, url=None)
                    _write(target, record)
        else:
            records = [json.loads(p.read_text()) for p in target.parent.glob("*.json")]
            record = {"name": name, "owner_login": login, "request_id": request_id,
                      "slug": "ws-" + key, "port": _port(config, records),
                      "status": "creating", "url": None, "workspace_id": None}
            _write(target, record)
        manifest = root / "workspaces" / record["slug"] / "workspace.json"
        if manifest.exists():
            held = load_workspace_manifest(manifest)
            try:
                _readiness(config, held, timeout=2.0, require_blank=False)
            except (OSError, ValueError, WorkspaceError):
                database = held.state_dir / "core.sqlite"
                if database.is_file():
                    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as core:
                        active = core.execute("SELECT count(*) FROM coverage_mission_pointer").fetchone()[0]
                    if active:
                        record.update(status="recovery_required", retryable=False,
                            failure_reason="该环境已有研究任务，服务需要恢复。研究数据和任务配置已保留。")
                        _write(target, record)
                        raise WorkspaceError(record["failure_reason"])
            else:
                # A prior route-publication failure must not reset an engine
                # that already started research through its local endpoint.
                _serve(config, record["port"])
                record.update(workspace_id=held.workspace_id, status="running", retryable=False,
                    url=f"https://{config['tailscale_host']}:{record['port']}/")
                record.pop("failure_reason", None)
                _write(target, record)
                return {"status": "running", "workspace": _public(record, None)}
        try:
            templates = _runtime_templates(config)
            catalog, catalog_paths = _catalog(config)
            shared_paths = list(config.get("shared_readonly_paths", ()))
            if config.get("shared_call_budget_policy_path"):
                shared_paths.append(str(Path(config["shared_call_budget_policy_path"]).resolve()))
            shared_paths.extend([
                str(config_path.resolve()), str(Path(config["tailscale_executable"]).resolve()),
                *catalog_paths,
                *(str(path) for path in templates.values()),
                *_runtime_shared_paths(templates),
            ])
            # Always call creation: this resumes a manifest-only/bootstrap-only
            # attempt and preserves the workspace UUID and token bytes.
            create_blank_workspace(
                root, record["slug"], record["port"], config["release_ref"],
                config["release_path"], request_id=request_id, display_name=name,
                shared_readonly_paths=tuple(dict.fromkeys(shared_paths)),
                shared_model_capacity_bindings=config.get("shared_model_capacity_bindings", ()),
                shared_connector_capacity=config.get("shared_connector_capacity", ()),
                connection_catalog=catalog,
            )
            workspace = load_workspace_manifest(manifest)
            record["workspace_id"] = workspace.workspace_id
            record["status"] = "creating"
            record.pop("retryable", None)
            _write(target, record)
            if provision_runtime(config, manifest, login=login) is None:
                configure_workspace_control(
                    manifest, owner_login=login, tailscale_host=config["tailscale_host"],
                    tailscale_executable=config["tailscale_executable"])
            service = json.loads(workspace.config_path.read_text())
            service["control"]["config"]["cockpit"]["workspace_manager_config_path"] = str(
                config_path.resolve())
            from .service import ServiceConfig
            from .workspace import validate_service_mapping_paths
            parsed_service = ServiceConfig.from_mapping(service)
            validate_service_mapping_paths(service, workspace)
            _write(workspace.config_path, service)
            ServiceConfig.from_file(workspace.config_path)
            if not record.get("installed"):
                install_workspace(manifest, config["launch_agents_dir"])
                record["installed"] = True
                _write(target, record)
            configured_roles = ["writer", "controller"]
            if parsed_service.control is not None:
                configured_roles.append("control")
            if parsed_service.thesis_impact is not None:
                configured_roles.append("thesis-impact")
            namespace = label_namespace(workspace.slug)
            for role in configured_roles:
                label = f"{namespace}.{role}"
                plist = Path(config["launch_agents_dir"]) / f"{label}.plist"
                if not plist.is_file():
                    raise WorkspaceError(f"研究环境服务定义缺失: {role}")
                exists = subprocess.run(
                    ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                    capture_output=True, text=True)
                if exists.returncode:
                    subprocess.run(
                        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                        check=True, capture_output=True, text=True)
            _readiness(config, workspace)
            _serve(config, record["port"])
            record.update(status="running", retryable=False,
                          url=f"https://{config['tailscale_host']}:{record['port']}/")
            record.pop("failure_reason", None)
            _write(target, record)
            return {"status": "running", "workspace": _public(record, None)}
        except Exception:
            record.update(status="failed", retryable=True, url=None,
                          failure_reason="研究环境尚未准备完成，可以安全重试。")
            _write(target, record)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--login", required=True)
    parser.add_argument("--name")
    parser.add_argument("--request-id")
    parser.add_argument("--set-shared-purpose")
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--expected-hash")
    args = parser.parse_args()
    if args.set_shared_purpose is not None:
        result = set_shared_call_budget(args.config, args.login, args.set_shared_purpose,
                                        args.max_cost_usd, args.expected_hash)
    else:
        if args.name is None or args.request_id is None:
            parser.error("--name and --request-id are required")
        result = create_managed_workspace(args.config, args.login, args.name, args.request_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
