#!/usr/bin/env python3
"""Local two-workspace acceptance with synthetic broker and capacity authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.shared_capacity import SharedCapacityAuthority, SharedCapacityExceeded
from dalton_core.shared_connector_capacity import (
    SharedConnectorCapacityAuthority, SharedConnectorCapacityExceeded, policy_record,
)
from dalton_core.store import content_hash
from dalton_core.workspace import WorkspacePaths, create_workspace_manifest, load_workspace_manifest
from dalton_core.workspace_process import workspace_plan
from dalton_core.workspace_release import install_release, validate_release

NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _tree(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append([path.relative_to(root).as_posix(), _sha(path), path.stat().st_mode & 0o777])
    return content_hash(rows)


def _wheel(path: Path, version: str) -> str:
    dist = f"dalton_workspace_acceptance-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("dalton_core/__init__.py", f"VERSION={version!r}\n")
        archive.writestr(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: dalton-workspace-acceptance\nVersion: {version}\n")
        archive.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nGenerator: acceptance\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{dist}/RECORD", "")
    return _sha(path)


def _installer(wheel: Path, venv: Path) -> None:
    version = zipfile.ZipFile(wheel).read("dalton_core/__init__.py")
    package = venv / "lib" / "python3.14" / "site-packages" / "dalton_core"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_bytes(version)
    (venv / "bin").mkdir(parents=True)
    for name in ("python", "daltond", "dalton-writer"):
        target = venv / "bin" / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o700)


def _reserve_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _replace_release(workspace: WorkspacePaths, release: Path, digest: str) -> bytes:
    path = workspace.manifest_path
    assert path is not None
    before = path.read_bytes()
    record = json.loads(before)
    record["release_ref"] = "release:sha256:" + digest
    record["release_path"] = str(release)
    shared = [item for item in record["shared_readonly_paths"] if item != str(workspace.release_path)]
    record["shared_readonly_paths"] = sorted([*shared, str(release)])
    body = {key: value for key, value in record.items() if key != "content_hash"}
    record["content_hash"] = content_hash(body)
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    load_workspace_manifest(path)
    return before


def _broker(socket_path: Path, ready: threading.Event, rows: list[dict]) -> None:
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(os.fspath(socket_path)); server.listen(2); ready.set()
        for _ in range(2):
            connection, _ = server.accept()
            with connection:
                request = json.loads(connection.recv(65536))
                body = {key: request[key] for key in ("workspace_id", "mission", "account")}
                accepted = request.get("signature") == content_hash(body)
                rows.append({"workspace_id": request.get("workspace_id"), "accepted": accepted})
                connection.sendall(json.dumps({"status": "stub_succeeded" if accepted else "refused", "request_hash": content_hash(request)}).encode())


def _stub_call(socket_path: Path, workspace: WorkspacePaths, account_ref: str) -> dict:
    mission_body = {"schema_version": "synthetic-test-mission-0.1", "workspace_id": workspace.workspace_id,
                    "mission_ref": "test-mission:" + workspace.slug, "transport": "STUB"}
    mission = {**mission_body, "content_hash": content_hash(mission_body)}
    account_body = {"schema_version": "synthetic-test-account-0.1", "account_ref": account_ref,
                    "workspace_id": workspace.workspace_id}
    account = {**account_body, "content_hash": content_hash(account_body)}
    body = {"workspace_id": workspace.workspace_id, "mission": mission, "account": account}
    request = {**body, "signature": content_hash(body)}
    code = (
        "import json,socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
        "s.sendall(sys.argv[2].encode()); print(s.recv(65536).decode()); s.close()"
    )
    completed = subprocess.run([sys.executable, "-c", code, str(socket_path), json.dumps(request, sort_keys=True)],
                               check=False, capture_output=True, text=True, timeout=10)
    response = json.loads(completed.stdout) if completed.returncode == 0 else {}
    return {"exit_code": completed.returncode, "status": response.get("status"),
            "request_hash": response.get("request_hash"), "mission_hash": mission["content_hash"],
            "account_hash": account["content_hash"]}


def _start_runtime(workspace: WorkspacePaths) -> tuple[subprocess.Popen[str], dict]:
    code = ("import json,os,pathlib,time; p=pathlib.Path(os.environ['DALTON_WORKSPACE_MANIFEST']); "
            "m=json.loads(p.read_text()); print(json.dumps({'workspace_id':m['workspace_id'],"
            "'release_ref':m['release_ref'],'pid':os.getpid()}),flush=True); time.sleep(30)")
    env = {**os.environ, "DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-c", code], env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    identity = json.loads(process.stdout.readline())
    if identity["workspace_id"] != workspace.workspace_id or identity["pid"] != process.pid:
        process.kill(); process.wait(timeout=5)
        raise RuntimeError("workspace runtime identity differs")
    return process, identity


def _stop_runtime(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate(); process.wait(timeout=5)
    if process.stdout is not None: process.stdout.close()
    if process.stderr is not None: process.stderr.close()


def _model_policy() -> dict:
    body = {"schema_version": "shared-model-capacity-policy-0.2", "id": "shared-capacity-policy:test-model",
            "status": "approved", "provider": "stub", "max_daily_calls": 10,
            "max_daily_cost_micros": 10000, "max_concurrency": 1,
            "approved_by": "human:test-fixture", "created_at": NOW.isoformat(),
            "account_ref": "provider-account:stub:test", "allowed_credential_slot_refs": ["credential-slot:stub:test"]}
    return {**body, "content_hash": content_hash(body)}


def _contend_model(database: Path, policy: dict, workspaces: list[WorkspacePaths]) -> list[str]:
    barrier = threading.Barrier(2)
    def reserve(workspace: WorkspacePaths) -> str:
        barrier.wait()
        try:
            with SharedCapacityAuthority(database, policy_ref=policy["id"], policy_hash=policy["content_hash"], clock=lambda: NOW) as authority:
                authority.reserve(workspace_id=workspace.workspace_id, invocation_ref="invocation:" + workspace.slug,
                                  provider="stub", credential_slot_ref="credential-slot:stub:test",
                                  maximum_cost_micros=100, expires_at=NOW + timedelta(minutes=5))
            return "reserved"
        except SharedCapacityExceeded:
            return "capacity_refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(reserve, workspaces))


def _contend_connector(database: Path, policy: dict, workspaces: list[WorkspacePaths]) -> list[str]:
    barrier = threading.Barrier(2)
    def reserve(workspace: WorkspacePaths) -> str:
        barrier.wait()
        try:
            with SharedConnectorCapacityAuthority(database, policy_ref=policy["id"], policy_hash=policy["content_hash"], clock=lambda: NOW) as authority:
                authority.reserve(workspace_id=workspace.workspace_id, invocation_ref="connector-invocation:" + workspace.slug,
                                  attempt_number=1, maximum_cost_micros=100, expires_at=NOW + timedelta(minutes=5))
            return "reserved"
        except SharedConnectorCapacityExceeded:
            return "capacity_refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(reserve, workspaces))


def run_acceptance(output: Path) -> dict:
    output = output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise RuntimeError("output root must be new")
    output.mkdir(mode=0o700, parents=True)
    with tempfile.TemporaryDirectory(prefix="dalton-two-workspace-") as directory:
        host = Path(directory) / "host"
        releases = []
        for version in ("0.1", "0.2"):
            wheel = Path(directory) / f"fixture-{version}-py3-none-any.whl"
            digest = _wheel(wheel, version)
            installed = install_release(host, wheel, digest, installer=_installer)
            releases.append((Path(installed["release_path"]), digest))
        ports = [_reserve_port(), _reserve_port()]
        while ports[1] == ports[0]: ports[1] = _reserve_port()
        workspaces = [create_workspace_manifest(host, slug, port, "release:sha256:" + releases[0][1], releases[0][0])
                      for slug, port in zip(("analyst-a", "analyst-b"), ports)]
        for workspace in workspaces:
            workspace.state_dir.mkdir(parents=True); workspace.config_path.parent.mkdir(parents=True)
            workspace.config_path.write_text(json.dumps({"workspace_id": workspace.workspace_id}) + "\n")
            (workspace.state_dir / "writer-tokens.json").write_text(json.dumps({"schema_version":"synthetic", "principals":[]}) + "\n")

        broker_path = Path(directory) / "stub-broker.sock"; broker_rows: list[dict] = []; ready = threading.Event()
        broker = threading.Thread(target=_broker, args=(broker_path, ready, broker_rows), daemon=True); broker.start(); ready.wait(5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = list(pool.map(lambda pair: _stub_call(broker_path, *pair),
                                  zip(workspaces, ("provider-account:stub:a", "provider-account:stub:b"))))
        broker.join(5)
        if broker.is_alive() or any(row["status"] != "stub_succeeded" or row["exit_code"] != 0 for row in calls):
            raise RuntimeError("concurrent STUB broker workload failed")

        occupied = socket.socket(); occupied.bind(("127.0.0.1", workspaces[0].cockpit_port))
        try:
            try: workspace_plan(workspaces[0].manifest_path, Path(directory) / "agents")
            except Exception as exc: port_refusal = {"refused": True, "reason_hash": _sha_bytes(str(exc).encode())}
            else: raise RuntimeError("occupied workspace port was accepted")
        finally: occupied.close()

        a_process, a_initial = _start_runtime(workspaces[0])
        b_process, b_identity = _start_runtime(workspaces[1])
        try:
            b_pid = b_process.pid; b_before = _tree(workspaces[1].workspace_root)
            a_before = workspaces[0].manifest_path.read_bytes()
            _stop_runtime(a_process)
            _replace_release(workspaces[0], releases[1][0], releases[1][1])
            validate_release(load_workspace_manifest(workspaces[0].manifest_path).release_path, releases[1][1])
            a_reinstalled_process, a_reinstalled = _start_runtime(load_workspace_manifest(workspaces[0].manifest_path))
            _stop_runtime(a_reinstalled_process)
            workspaces[0].manifest_path.write_bytes(a_before); os.chmod(workspaces[0].manifest_path, 0o600)
            rolled = load_workspace_manifest(workspaces[0].manifest_path)
            validate_release(rolled.release_path, releases[0][1])
            a_rollback_process, a_rollback = _start_runtime(rolled)
            b_after = _tree(workspaces[1].workspace_root)
            isolation = {"b_pid_before": b_pid, "b_pid_after": b_process.pid, "b_running": b_process.poll() is None,
                         "b_tree_before": b_before, "b_tree_after": b_after,
                         "a_runtime_pids":[a_initial["pid"], a_reinstalled["pid"], a_rollback["pid"]],
                         "a_runtime_release_refs":[a_initial["release_ref"], a_reinstalled["release_ref"], a_rollback["release_ref"]],
                         "a_manifest_restored_sha256": _sha(workspaces[0].manifest_path),
                         "a_manifest_original_sha256": _sha_bytes(a_before)}
            if (not isolation["b_running"] or b_before != b_after or b_process.pid != b_pid
                    or a_before != workspaces[0].manifest_path.read_bytes()
                    or isolation["a_runtime_release_refs"] != ["release:sha256:" + releases[0][1],
                                                               "release:sha256:" + releases[1][1],
                                                               "release:sha256:" + releases[0][1]]):
                raise RuntimeError("workspace B or rollback invariant failed")
        finally:
            for process in (a_process, b_process, locals().get("a_reinstalled_process"), locals().get("a_rollback_process")):
                if process is not None:
                    _stop_runtime(process)

        model_db = host / "fleet-capacity" / "model.sqlite"; model = _model_policy()
        SharedCapacityAuthority.initialize(model_db, model)
        model_results = _contend_model(model_db, model, workspaces)
        connector_db = host / "fleet-capacity" / "connectors.sqlite"
        connector = policy_record(id="connector-capacity:test", status="approved",
            scopes=[{"connector_ref":"connector:stub", "capability_ref":"capability:stub", "credential_slot_refs":["credential-slot:stub:test"]}],
            quota_scope_ref="quota-scope:stub:test", provider_account_ref="provider-account:stub:test",
            version=1, prior_policy_ref=None, rolling_window_seconds=86400, max_calls=10,
            max_cost_micros=10000, max_cost_micros_per_call=1000, max_concurrency=1,
            approved_by="human:test-fixture", created_at=NOW.isoformat())
        SharedConnectorCapacityAuthority.initialize(connector_db, connector)
        connector_results = _contend_connector(connector_db, connector, workspaces)
        if model_results != ["capacity_refused", "reserved"] or connector_results != ["capacity_refused", "reserved"]:
            raise RuntimeError("shared capacity contention did not admit exactly one workspace")
        result = {"schema_version":"two-workspace-local-acceptance-0.1", "status":"passed",
                  "synthetic_only":True, "provider_calls":0, "workspace_ids":[w.workspace_id for w in workspaces],
                  "broker":{"transport":"STUB", "calls":calls, "accepted":len(broker_rows)},
                  "port_collision":port_refusal, "release_rollback":isolation,
                  "capacity":{"model":model_results, "connector":connector_results},
                  "release_hashes":[item[1] for item in releases]}
    result_path = output / "result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"); os.chmod(result_path, 0o600)
    return {**result, "result_path":str(result_path), "result_sha256":_sha(result_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-root", type=Path, required=True)
    result = run_acceptance(parser.parse_args().output_root)
    print(json.dumps({key:result[key] for key in ("status","result_path","result_sha256")}, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
