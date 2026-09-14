#!/usr/bin/env python3
"""Run two real isolated Dalton engines with controlled zero-cost transports."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dalton_core.bootstrap import bootstrap
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore, content_hash
from dalton_core.workspace import create_workspace_manifest, load_workspace_manifest
from dalton_core.workspace_mission_setup import (
    draft_first_mission, publish_first_mission_to_store,
)
from dalton_core.workspace_runtime import validate_child_command
from dalton_core.workspace_runtime_setup import install as install_runtime
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def _tokens(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text())
    return {item["principal_id"]: item["token"] for item in value["principals"]}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _bounded_planner(state: Path) -> dict[str, Any]:
    return {
        "enabled": True, "interval_seconds": 0.2,
        "config": {
            "writer_socket": str(state / "run/writer.sock"),
            "token_config": str(state / "writer-tokens.json"),
            "scheduler_db": str(state / "scheduler.sqlite"),
            "user_agent": "Dalton Workspace Runtime Acceptance",
            "max_response_bytes": 8_388_608, "timeout_seconds": 10.0,
            "max_probes_per_tick": 1, "filed_window_days": 400,
            "observation_mandate_version_ref": None,
            "doctrine_pack_version_ref": None, "doctrine_pack_version_hash": None,
            "planner_routing_policy_ref": None, "planner_credential_slot_refs": None,
            "planner_model_router_db": None, "planner_broker_socket": None,
            "planner_broker_auth_key": None, "planner_broker_client_id": "client:acceptance",
            "planner_expected_agent_id": "fixture", "planner_max_cost_usd": 0.01,
            "planner_call_budget": {"max_input_tokens": 1, "max_output_tokens": 1,
                                    "max_cost_usd": 0.01, "timeout_seconds": 1},
        },
    }


def _create(host: Path, release: Path, slug: str, port: int) -> Any:
    workspace = create_workspace_manifest(
        host, slug, port, "release:sha256:" + "a" * 64, release)
    bootstrap(workspace.state_dir, workspace.config_path,
              workspace_manifest=workspace.manifest_path)
    install_runtime(workspace.manifest_path, actor_ref="human:acceptance-owner")
    config = json.loads(workspace.config_path.read_text())
    config["plugins"] = []
    config["backup"]["enabled"] = False
    config["bounded_planner"] = _bounded_planner(workspace.state_dir)
    _write(workspace.config_path, config)
    foundation = json.loads((workspace.state_dir / "research-foundation.json").read_text())
    foundation_body = {key: value for key, value in foundation.items()
                       if key != "content_hash"}
    foundation_body["mission_defaults"]["source_plan"] = [{
        "source_ref": "source:sec-edgar", "role": "controlled fixture filings",
        "status": "connected",
    }]
    foundation = {**foundation_body, "content_hash": content_hash(foundation_body)}
    _write(workspace.state_dir / "research-foundation.json", foundation)
    proposal = draft_first_mission(
        workspace, goal="Research the fixture software industry and $SAME",
        method_foundation=foundation, industry="Fixture Software",
        companies=[{"company_ref": "company:ticker:same", "ticker": "SAME"}],
    )
    with DaltonStore(workspace.state_dir / "core.sqlite") as store:
        mission = publish_first_mission_to_store(
            store, workspace, proposal=proposal, proposal_hash=proposal["content_hash"],
            actor_ref="human:acceptance-owner", method_foundation=foundation)
    return workspace, mission, proposal, foundation


def _start(workspace: Any, module: str, *args: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "PYTHONDONTWRITEBYTECODE": "1",
           "DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path)}
    return subprocess.Popen(
        [sys.executable, "-m", module, *args], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _start_writer(workspace: Any) -> subprocess.Popen[str]:
    state = workspace.state_dir
    process = _start(
        workspace, "dalton_core.writer_server", "--db", str(state / "core.sqlite"),
        "--scheduler", str(state / "scheduler.sqlite"),
        "--socket", str(workspace.writer_socket), "--token-config",
        str(state / "writer-tokens.json"), "--transcript-spool-dir",
        str(state / "transcript-spool"))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if workspace.writer_socket.exists():
            return process
        if process.poll() is not None:
            out, err = process.communicate()
            raise RuntimeError(f"writer exited early: {out[-300:]} {err[-500:]}")
        time.sleep(0.02)
    raise RuntimeError("writer did not publish its workspace socket")


def _start_controller(workspace: Any) -> subprocess.Popen[str]:
    process = _start(workspace, "dalton_core.service", "--config", str(workspace.config_path))
    deadline = time.monotonic() + 20
    heartbeat = workspace.state_dir / "run/heartbeat.json"
    while time.monotonic() < deadline:
        if heartbeat.is_file():
            value = json.loads(heartbeat.read_text())
            if value.get("pid") == process.pid and value.get("last_tick_at"):
                return process
        if process.poll() is not None:
            out, err = process.communicate()
            raise RuntimeError(f"controller exited early: {out[-300:]} {err[-700:]}")
        time.sleep(0.05)
    raise RuntimeError("controller did not complete a real tick")


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _heartbeat(workspace: Any) -> dict[str, Any]:
    return json.loads((workspace.state_dir / "run/heartbeat.json").read_text())


def _tick_count(workspace: Any) -> int:
    path = workspace.state_dir / "tick-ledger.sqlite"
    if not path.is_file():
        return 0
    with sqlite3.connect(path) as connection:
        try:
            return connection.execute("SELECT count(*) FROM tick_ledger_ticks").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def _wait_more_ticks(workspace: Any, before: int) -> int:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = _tick_count(workspace)
        if current > before:
            return current
        time.sleep(0.05)
    raise RuntimeError("workspace controller did not continue research scheduling")


def _research_tick_evidence(workspace: Any) -> dict[str, Any]:
    with sqlite3.connect(workspace.state_dir / "tick-ledger.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        tick = connection.execute(
            "SELECT * FROM tick_ledger_ticks ORDER BY started_at DESC LIMIT 1").fetchone()
        if tick is None or tick["lane_count"] <= 0:
            raise RuntimeError("controller tick did not schedule registered research lanes")
        lanes = connection.execute(
            "SELECT driver_key,lane_operation,status FROM tick_ledger_lanes "
            "WHERE tick_id=? ORDER BY driver_key", (tick["tick_id"],)).fetchall()
        mission_stage_row = connection.execute(
            "SELECT l.tick_id,l.driver_key,l.lane_operation,l.status "
            "FROM tick_ledger_lanes l JOIN tick_ledger_ticks t ON t.tick_id=l.tick_id "
            "WHERE l.driver_key='mission_stage' AND l.status='entered' "
            "ORDER BY t.started_at ASC LIMIT 1").fetchone()
    lane_records = [dict(row) for row in lanes]
    if mission_stage_row is None:
        raise RuntimeError("real mission-stage engine operation did not enter initial screening")
    mission_stage = dict(mission_stage_row)
    return {"tick_id": tick["tick_id"], "status": tick["status"],
            "lane_count": tick["lane_count"], "mission_stage": mission_stage,
            "lanes": lane_records}


def _assert_initial_screen(progress: dict[str, Any]) -> dict[str, Any]:
    companies = progress.get("companies") or []
    if not companies or any(
        item.get("current_stage") != "initial_screen"
        or item.get("current_status") != "entered"
        or item.get("record_count", 0) < 1
        for item in companies
    ):
        raise RuntimeError("mission authority did not record initial_screen/entered")
    return {"companies": [{
        "company_ref": item["company_ref"],
        "current_stage": item["current_stage"],
        "current_status": item["current_status"],
        "record_count": item["record_count"],
    } for item in companies]}


def run_acceptance(output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise RuntimeError("output root must be new")
    output.mkdir(mode=0o700, parents=True)
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="dwr-", dir="/tmp") as temporary:
        root = Path(temporary)
        host, release = root / "fleet", root / "release"
        release.mkdir()
        a, mission_a, proposal_a, foundation_a = _create(host, release, "alpha", _free_port())
        writer_a = _start_writer(a); processes.append(writer_a)
        controller_a = _start_controller(a); processes.append(controller_a)
        a_pid, a_before = controller_a.pid, _tick_count(a)
        try:
            # B is created and opened while A's real controller remains alive.
            b, mission_b, _proposal_b, foundation_b = _create(host, release, "beta", _free_port())
            writer_b = _start_writer(b); processes.append(writer_b)
            controller_b = _start_controller(b); processes.append(controller_b)
            a_after = _wait_more_ticks(a, a_before)
            if controller_a.pid != a_pid or controller_a.poll() is not None:
                raise RuntimeError("workspace A controller changed while B was created")

            tokens_a, tokens_b = _tokens(a.state_dir / "writer-tokens.json"), _tokens(
                b.state_dir / "writer-tokens.json")
            try:
                WriterClient(str(a.writer_socket), tokens_b["core"]).call(
                    "bounded_planner_active_loops", {})
            except RemoteAuthorizationError as exc:
                cross_token = {"refused": True, "reason": type(exc).__name__}
            else:
                raise RuntimeError("workspace B token entered workspace A writer")
            old = os.environ.get("DALTON_WORKSPACE_MANIFEST")
            os.environ["DALTON_WORKSPACE_MANIFEST"] = str(a.manifest_path)
            try:
                try:
                    validate_child_command(
                        [sys.executable, "-m", "dalton_core.research_task_cli",
                         "--state-dir", str(b.state_dir), "--db",
                         str(b.state_dir / "core.sqlite")], state_dir=a.state_dir)
                except Exception as exc:
                    cross_task = {"refused": True, "reason": type(exc).__name__}
                else:
                    raise RuntimeError("workspace A admitted a workspace B child task")
            finally:
                if old is None:
                    os.environ.pop("DALTON_WORKSPACE_MANIFEST", None)
                else:
                    os.environ["DALTON_WORKSPACE_MANIFEST"] = old
            try:
                with DaltonStore(b.state_dir / "core.sqlite") as store:
                    publish_first_mission_to_store(
                        store, b, proposal=proposal_a,
                        proposal_hash=proposal_a["content_hash"],
                        actor_ref="human:acceptance-owner",
                        method_foundation=foundation_b)
            except Exception as exc:
                cross_approval = {"refused": True, "reason": type(exc).__name__}
            else:
                raise RuntimeError("workspace A proposal entered workspace B authority")

            with DaltonStore(a.state_dir / "core.sqlite") as store:
                progress_a = CoverageMissionAuthority(store).mission_progress(
                    mission_a["mission_ref"])
            with DaltonStore(b.state_dir / "core.sqlite") as store:
                progress_b = CoverageMissionAuthority(store).mission_progress(
                    mission_b["mission_ref"])
            if (progress_a["mission_version_ref"] != mission_a["id"]
                    or progress_b["mission_version_ref"] != mission_b["id"]):
                raise RuntimeError("controller workspace mission authority differs")
            stage_a, stage_b = _assert_initial_screen(progress_a), _assert_initial_screen(progress_b)
            receipt = {
                "schema_version": "workspace-runtime-acceptance-0.1", "status": "passed",
                "fixture_transports_only": True, "external_provider_calls": 0,
                "workspaces": [{"workspace_id": a.workspace_id, "mission_ref": mission_a["mission_ref"],
                                "company_ref": "company:ticker:same", "controller_pid": a_pid},
                               {"workspace_id": b.workspace_id, "mission_ref": mission_b["mission_ref"],
                                "company_ref": "company:ticker:same",
                                "controller_pid": controller_b.pid}],
                "continuous_processing": {"a_ticks_before_b": a_before,
                                          "a_ticks_after_b": a_after,
                                          "a_pid_unchanged": True},
                "engine": {"writer_processes": 2, "controller_processes": 2,
                           "a_bounded_planner": _heartbeat(a)["bounded_planner"],
                           "b_bounded_planner": _heartbeat(b)["bounded_planner"],
                           "a_research_tick": _research_tick_evidence(a),
                           "b_research_tick": _research_tick_evidence(b),
                           "a_mission_progress": stage_a,
                           "b_mission_progress": stage_b,
                           "mission_versions": [progress_a["mission_version_ref"],
                                                progress_b["mission_version_ref"]]},
                "isolation": {"cross_writer_token": cross_token,
                              "cross_child_task": cross_task,
                              "cross_mission_approval": cross_approval},
            }
        finally:
            for process in reversed(processes):
                _stop(process)
    receipt = {**receipt, "content_hash": content_hash(receipt)}
    _write(output / "receipt.json", receipt)
    return {**receipt, "receipt_path": str(output / "receipt.json")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    result = run_acceptance(parser.parse_args().output_root)
    print(json.dumps({"status": result["status"], "receipt_path": result["receipt_path"],
                      "content_hash": result["content_hash"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
