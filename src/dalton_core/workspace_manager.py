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
import subprocess
import sys
import tempfile
import time
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
                "legacy_workspace", "connections_path"}
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
    return raw


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
    return {key: record.get(key) for key in ("workspace_id", "name", "url", "status")} | {
        "current": record.get("workspace_id") == current_id,
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
        items.append(_public(legacy, current_id or "legacy"))
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


def request_create(config_path: Path, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Run outside the caller's namespace; never modify process-wide environment."""
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


def _readiness(config: Mapping[str, Any], workspace: Any, *, timeout: float = 30.0) -> None:
    """Require the new Cockpit to identify its workspace and blank state."""
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
            if (payload.get("state") == "awaiting_mission"
                    and payload.get("workspace", {}).get("workspace_id") == workspace.workspace_id):
                return
            last_error = WorkspaceError("新研究环境返回了错误的身份或状态")
        except Exception as exc:  # readiness retries transport and incomplete startup
            last_error = exc
        time.sleep(0.1)
    raise WorkspaceError("新研究环境尚未通过空白状态检查") from last_error


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
                    _readiness(config, held, timeout=2.0)
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
        try:
            catalog, catalog_paths = _catalog(config)
            shared_paths = list(config.get("shared_readonly_paths", ()))
            shared_paths.extend([str(config_path.resolve()), *catalog_paths])
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
            os.environ["DALTON_WORKSPACE_MANIFEST"] = str(manifest)
            configure_workspace_control(
                manifest, owner_login=login, tailscale_host=config["tailscale_host"],
                tailscale_executable=config["tailscale_executable"])
            service = json.loads(workspace.config_path.read_text())
            service["control"]["config"]["cockpit"]["workspace_manager_config_path"] = str(
                config_path.resolve())
            from .service import ServiceConfig
            ServiceConfig.from_mapping(service)
            _write(workspace.config_path, service)
            if not record.get("installed"):
                install_workspace(manifest, config["launch_agents_dir"])
                record["installed"] = True
                _write(target, record)
            namespace = label_namespace(workspace.slug)
            for role in ("writer", "controller", "control", "thesis-impact"):
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
            _write(target, record)
            return {"status": "running", "workspace": _public(record, None)}
        except Exception:
            record.update(status="failed", retryable=True, url=None)
            _write(target, record)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--login", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    print(json.dumps(create_managed_workspace(args.config, args.login, args.name, args.request_id), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
