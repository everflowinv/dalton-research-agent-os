#!/usr/bin/env python3
"""Apply one reviewed OpenClaw broker config CAS in a stopped window.

Dalton's deployment orchestrator owns the broader stop/drain/backup window.
This helper rechecks that its three services and child tickets are stopped,
preserves the exact reviewed historical broker uncertainty set, and performs
one gateway stop/config CAS/start.  It never replays or refunds a journal row.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from scripts.prepare_successor_config_transition import (
    _json_bytes, _plugin_tree, canonical_hash,
    _reviewed_historical_unresolved, expected_openclaw_frame_transition_state,
    sha256_bytes,
)


GATEWAY_LABEL = "ai.openclaw.gateway"
DALTON_LABELS = (
    "space.lumos.dalton.writer",
    "space.lumos.dalton.controller",
    "space.lumos.dalton.control",
)


class BrokerStoppedWindowError(RuntimeError):
    pass


def _need(ok: Any, reason: str) -> None:
    if not ok:
        raise BrokerStoppedWindowError(reason)


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    _need(path.parent.is_dir() and not path.parent.is_symlink(),
          "broker config parent is unsafe")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _occupied(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _compare_and_install(
    path: Path, before: bytes, after: bytes,
    *, expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Replace exact owned bytes without overwriting a racing path owner."""

    _need(path.is_file() and not path.is_symlink(),
          "OpenClaw config is not an owned regular file")
    initial = path.stat()
    identity = (initial.st_dev, initial.st_ino)
    _need((expected_identity is None or identity == expected_identity)
          and path.read_bytes() == before,
          "OpenClaw config differs from owned CAS precondition")
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=".openclaw-config-candidate-", dir=path.parent)
    candidate = Path(candidate_name)
    held_descriptor, held_name = tempfile.mkstemp(
        prefix=".openclaw-config-held-", dir=path.parent)
    os.close(held_descriptor)
    held = Path(held_name); held.unlink()
    published_identity: tuple[int, int] | None = None
    held_is_owned = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(after); stream.flush(); os.fsync(stream.fileno())
        os.chmod(candidate, stat.S_IMODE(initial.st_mode))
        os.rename(path, held)
        moved = held.lstat()
        if (held.is_symlink() or (moved.st_dev, moved.st_ino) != identity
                or held.read_bytes() != before):
            if not _occupied(path):
                os.rename(held, path)
            raise BrokerStoppedWindowError(
                "OpenClaw config changed during owned CAS; conflicting inode "
                f"preserved at {held}")
        held_is_owned = True
        os.link(candidate, path)
        linked = candidate.lstat()
        published_identity = (linked.st_dev, linked.st_ino)
        # Persist the reviewed target name while the original inode is still
        # available for recovery from any preceding failure.
        _fsync_parent(path)
        candidate.unlink(); held.unlink()
        installed = path.stat()
        return installed.st_dev, installed.st_ino
    except Exception:
        if (published_identity is not None and path.is_file()
                and not path.is_symlink()):
            current = path.lstat()
            if (current.st_dev, current.st_ino) == published_identity:
                path.unlink()
        if _occupied(held) and not _occupied(path):
            os.rename(held, path)
            _fsync_parent(path)
        raise
    finally:
        candidate.unlink(missing_ok=True)
        if _occupied(held) and not _occupied(path):
            os.rename(held, path)
        elif (held_is_owned and _occupied(held)
              and (held.is_symlink() or held.is_file())):
            held.unlink()


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    data = _json_bytes(value)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    published_identity: tuple[int, int] | None = None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)
        linked = temporary.lstat()
        published_identity = (linked.st_dev, linked.st_ino)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        # A directory fsync can fail after publication.  Remove only the link
        # this call created; a racing owner may already have replaced it.
        if (published_identity is not None and path.is_file()
                and not path.is_symlink()):
            current = path.lstat()
            if (current.st_dev, current.st_ino) == published_identity:
                path.unlink()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def loaded(label: str, *, run=subprocess.run) -> bool:
    return run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def gateway_identity(*, run=subprocess.run) -> dict[str, Any]:
    result = run(
        ["launchctl", "print", f"gui/{os.getuid()}/{GATEWAY_LABEL}"],
        text=True, capture_output=True,
    )
    _need(result.returncode == 0, "OpenClaw gateway is not loaded")
    match = re.search(r"(?m)^\s*pid = (\d+)\s*$", result.stdout or "")
    _need(match is not None, "OpenClaw gateway pid is unavailable")
    pid = int(match.group(1))
    started = run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        text=True, capture_output=True,
    )
    _need(started.returncode == 0 and bool(started.stdout.strip()),
          "OpenClaw gateway start time is unavailable")
    moment = datetime.datetime.strptime(
        started.stdout.strip(), "%a %b %d %H:%M:%S %Y").astimezone()
    return {
        "pid": pid,
        "started_at": moment.astimezone(datetime.timezone.utc).isoformat()
            .replace("+00:00", "Z"),
        "started_at_ms": int(moment.timestamp() * 1000),
    }


def stop_gateway(openclaw_root: Path, *, run=subprocess.run) -> None:
    node = openclaw_root.parents[2] / "bin/node"
    run([str(node), str(openclaw_root / "openclaw.mjs"), "gateway", "stop",
         "--force", "--json"], check=True, stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if not loaded(GATEWAY_LABEL, run=run):
            return
        time.sleep(1)
    raise BrokerStoppedWindowError("OpenClaw gateway did not stop")


def start_gateway(openclaw_root: Path, *, run=subprocess.run) -> dict[str, Any]:
    _need(not loaded(GATEWAY_LABEL, run=run),
          "refusing to bootstrap an already-loaded OpenClaw gateway")
    socket_paths = [
        Path.home() / ".openclaw/dalton-model-broker.sock",
        Path.home() / ".openclaw/dalton-web-search-broker.sock",
    ]
    for socket_path in socket_paths:
        if not (socket_path.exists() or socket_path.is_symlink()):
            continue
        import stat
        _need(not socket_path.is_symlink()
              and stat.S_ISSOCK(socket_path.lstat().st_mode),
              "stale broker path is not a Unix socket")
        socket_path.unlink()
    plist = Path.home() / "Library/LaunchAgents/ai.openclaw.gateway.plist"
    run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
    deadline = time.monotonic() + 90
    install = Path.home() / ".openclaw/workspace/patch/openclaw_install.py"
    while time.monotonic() < deadline:
        result = run([sys.executable, str(install), "--assert-running"],
                     text=True, capture_output=True)
        if (result.returncode == 0 and result.stdout.strip()
                and Path(result.stdout.strip()).resolve() == openclaw_root.resolve()):
            try:
                for socket_path in socket_paths:
                    with socket.socket(socket.AF_UNIX) as stream:
                        stream.settimeout(1)
                        stream.connect(str(socket_path))
                return gateway_identity(run=run)
            except OSError:
                pass
        time.sleep(1)
    raise BrokerStoppedWindowError(
        "managed OpenClaw gateway or model broker socket did not become ready")


def managed_openclaw_root(*, run=subprocess.run) -> Path:
    install = Path.home() / ".openclaw/workspace/patch/openclaw_install.py"
    result = run([sys.executable, str(install), "--assert-running"],
                 text=True, capture_output=True)
    _need(result.returncode == 0 and bool(result.stdout.strip()),
          "managed OpenClaw root is unavailable")
    root = Path(result.stdout.strip()).resolve()
    _need(root.is_dir() and not root.is_symlink()
          and (root / "openclaw.mjs").is_file(),
          "managed OpenClaw root is unsafe")
    return root


def _source_identity(source_root: Path, commit: str) -> None:
    source_root = source_root.resolve()
    head = subprocess.check_output(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(source_root), "status", "--porcelain",
         "--untracked-files=all"], text=True)
    _need(head == commit and not dirty,
          "reviewed source is dirty or at another commit")


def _children_clear(state_dir: Path, source_root: Path) -> None:
    sys.path.insert(0, str(source_root / "src"))
    try:
        from dalton_core.launch_drain import running_tickets
        active = running_tickets(state_dir)
    finally:
        sys.path.pop(0)
    _need(not active, f"Dalton launch drain has {len(active)} active child ticket(s)")


def _stopped_checks(
    *, state_dir: Path, source_root: Path, journal_path: Path,
    reviewed: Mapping[str, Any],
    run=subprocess.run,
) -> None:
    _need(not any(loaded(label, run=run) for label in DALTON_LABELS),
          "Dalton services must be unloaded")
    _children_clear(state_dir, source_root)
    current = _reviewed_historical_unresolved(journal_path)
    _need(current == dict(reviewed),
          "historical broker uncertainty set changed")


def _verify_plugins(
    *, row: Mapping[str, Any], source_root: Path, packet_root: Path,
) -> list[dict[str, Any]]:
    verified = []
    for plugin in row["managed_plugins"]:
        source = source_root / plugin["source_relative_path"]
        destination = Path(plugin["destination"])
        _need(destination.is_relative_to((packet_root / "managed-plugins").resolve())
              and plugin["source_commit"]
              == subprocess.check_output(
                  ["git", "-C", str(source_root), "rev-parse", "HEAD"],
                  text=True).strip()
              and _plugin_tree(source) == plugin["source_tree"]
              and _plugin_tree(destination) == plugin["source_tree"],
              "managed web-search plugin bytes or destination changed")
        definition = destination / "openclaw.plugin.json"
        _need(definition.is_file() and not definition.is_symlink(),
              "managed web-search plugin definition is unavailable")
        try:
            plugin_version = json.loads(definition.read_text(encoding="utf-8"))[
                "version"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
            raise BrokerStoppedWindowError(
                "managed web-search plugin version is unavailable") from exc
        _need(isinstance(plugin_version, str) and bool(plugin_version),
              "managed web-search plugin version is invalid")
        verified.append({"plugin_id": plugin["plugin_id"],
                         "destination": str(destination),
                         "tree_sha256": plugin["source_tree"]["tree_sha256"],
                         "version": plugin_version})
    return verified


def _verify_loaded_plugins(openclaw_root: Path, reviewed: list[dict[str, Any]],
                           *, run=subprocess.run) -> None:
    node = openclaw_root.parents[2] / "bin/node"
    for expected in reviewed:
        result = run([
            str(node), str(openclaw_root / "openclaw.mjs"), "plugins", "info",
            expected["plugin_id"], "--json",
        ], text=True, capture_output=True)
        try:
            wire = json.loads(result.stdout) if result.returncode == 0 else None
            plugin = wire["plugin"] if isinstance(wire, Mapping) else None
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            plugin = None
        _need(isinstance(plugin, Mapping)
              and plugin.get("id") == expected["plugin_id"]
              and plugin.get("rootDir") == expected["destination"]
              and plugin.get("version") == expected["version"]
              and plugin.get("enabled") is True
              and plugin.get("activated") is True
              and plugin.get("status") == "loaded",
              "managed web-search plugin is not loaded from reviewed bytes")


def _host_patch_artifacts(*, packet_root: Path, source_root: Path,
                          openclaw_root: Path, row: Mapping[str, Any]):
    """Resolve one exact repo-owned host patch without reading credentials."""
    required = {"source_commit", "helper_relative_path", "helper_sha256",
                "target_relative_path", "before", "before_sha256", "after",
                "after_sha256", "capability_check"}
    _need(set(row) == required and row.get("source_commit")
          == subprocess.check_output(
              ["git", "-C", str(source_root), "rev-parse", "HEAD"],
              text=True).strip()
          and row.get("capability_check") == "repo_helper_check_no_call",
          "model broker host patch authority differs")
    helper_rel = Path(str(row["helper_relative_path"]))
    target_rel = Path(str(row["target_relative_path"]))
    _need(not helper_rel.is_absolute() and ".." not in helper_rel.parts
          and helper_rel.as_posix()
          == "integrations/openclaw_host_patches/patch_provider_output_control_endpoint.py"
          and not target_rel.is_absolute() and ".." not in target_rel.parts
          and target_rel.parts and target_rel.parts[0] == "dist",
          "model broker host patch path is outside reviewed scope")
    helper = source_root / helper_rel
    target = openclaw_root / target_rel
    runtime_targets = sorted((openclaw_root / "dist").glob(
        "runtime-llm.runtime-*.mjs"))
    before = packet_root / Path(str(row["before"]))
    after = packet_root / Path(str(row["after"]))
    _need(len(runtime_targets) == 1 and target == runtime_targets[0]
          and all(path.is_file() and not path.is_symlink()
              for path in (helper, target, before, after))
          and sha256_bytes(helper.read_bytes()) == row["helper_sha256"]
          and sha256_bytes(before.read_bytes()) == row["before_sha256"]
          and sha256_bytes(after.read_bytes()) == row["after_sha256"],
          "model broker host patch artifacts differ")
    return helper, target, before.read_bytes(), after.read_bytes()


def apply_reviewed_host_patch(*, packet_root: Path, source_root: Path,
                              openclaw_root: Path, row: Mapping[str, Any],
                              receipt_path: Path,
                              run=subprocess.run) -> dict[str, Any]:
    """Install exact reviewed host bytes and prove capability without a call."""
    helper, target, before, after = _host_patch_artifacts(
        packet_root=packet_root, source_root=source_root,
        openclaw_root=openclaw_root, row=row)
    identity = _compare_and_install(target, before, after)
    try:
        checked = run([sys.executable, str(helper), "--openclaw-root",
                       str(openclaw_root), "--check"], text=True,
                      capture_output=True)
        _need(checked.returncode == 0
              and "OK provider output control endpoint" in checked.stdout,
              "model broker host capability check failed")
        result = {"schema_version": "openclaw-model-host-patch-receipt-0.1",
                  "status": "installed_checked_no_call",
                  "source_commit": row["source_commit"],
                  "helper_sha256": row["helper_sha256"],
                  "target_relative_path": row["target_relative_path"],
                  "before_sha256": row["before_sha256"],
                  "after_sha256": row["after_sha256"],
                  "installed_identity": list(identity), "model_calls": 0}
        result["content_hash"] = canonical_hash(result)
        _write_exclusive(receipt_path, result)
        return result
    except Exception:
        current = target.lstat() if target.is_file() and not target.is_symlink() else None
        if current is not None and (current.st_dev, current.st_ino) == identity:
            _compare_and_install(target, after, before, expected_identity=identity)
        raise


def rollback_reviewed_host_patch(*, packet_root: Path, source_root: Path,
                                 openclaw_root: Path, row: Mapping[str, Any],
                                 receipt_path: Path) -> dict[str, Any]:
    helper, target, before, after = _host_patch_artifacts(
        packet_root=packet_root, source_root=source_root,
        openclaw_root=openclaw_root, row=row)
    del helper
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in receipt.items()
                if key != "content_hash"}
    _need(set(receipt) == {"schema_version", "status", "source_commit",
                           "helper_sha256", "target_relative_path",
                           "before_sha256", "after_sha256",
                           "installed_identity", "model_calls", "content_hash"}
          and receipt.get("schema_version") == "openclaw-model-host-patch-receipt-0.1"
          and receipt.get("status") == "installed_checked_no_call"
          and receipt.get("content_hash") == canonical_hash(unsigned)
          and receipt.get("source_commit") == row["source_commit"]
          and receipt.get("helper_sha256") == row["helper_sha256"]
          and receipt.get("target_relative_path") == row["target_relative_path"]
          and receipt.get("before_sha256") == row["before_sha256"]
          and receipt.get("after_sha256") == row["after_sha256"],
          "model broker host patch receipt differs")
    _need(isinstance(receipt.get("installed_identity"), list)
          and len(receipt["installed_identity"]) == 2
          and all(isinstance(value, int) for value in receipt["installed_identity"])
          and receipt.get("model_calls") == 0,
          "model broker host patch receipt identity differs")
    identity = tuple(receipt["installed_identity"])
    _compare_and_install(target, after, before, expected_identity=identity)
    result = {"schema_version": "openclaw-model-host-patch-rollback-0.1",
              "status": "rolled_back", "restored_sha256": row["before_sha256"],
              "model_calls": 0}
    result["content_hash"] = canonical_hash(result)
    _write_exclusive(receipt_path.with_name("host-patch-rollback.json"), result)
    return result


def verify_reviewed_host_patch(*, packet_root: Path, source_root: Path,
                               openclaw_root: Path, row: Mapping[str, Any],
                               receipt_path: Path,
                               run=subprocess.run) -> dict[str, Any]:
    """Recheck exact installed bytes, receipt, and static capability proof."""
    helper, target, _before, after = _host_patch_artifacts(
        packet_root=packet_root, source_root=source_root,
        openclaw_root=openclaw_root, row=row)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in receipt.items()
                if key != "content_hash"}
    current = target.lstat()
    _need(receipt.get("schema_version") == "openclaw-model-host-patch-receipt-0.1"
          and receipt.get("status") == "installed_checked_no_call"
          and receipt.get("content_hash") == canonical_hash(unsigned)
          and receipt.get("source_commit") == row["source_commit"]
          and receipt.get("helper_sha256") == row["helper_sha256"]
          and receipt.get("after_sha256") == row["after_sha256"]
          and receipt.get("installed_identity") == [current.st_dev, current.st_ino]
          and target.read_bytes() == after and receipt.get("model_calls") == 0,
          "installed model broker host patch differs")
    checked = run([sys.executable, str(helper), "--openclaw-root",
                   str(openclaw_root), "--check"], text=True,
                  capture_output=True)
    _need(checked.returncode == 0
          and "OK provider output control endpoint" in checked.stdout,
          "installed model broker host capability check failed")
    return {"status": "installed_checked_no_call",
            "receipt_sha256": sha256_bytes(receipt_path.read_bytes()),
            "target_sha256": row["after_sha256"], "model_calls": 0}


@contextmanager
def operation_lock(config_path: Path, operation: str):
    path = config_path.with_name(config_path.name + ".dalton-broker-config.lock")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BrokerStoppedWindowError(
            f"broker config operation lock exists: {path}") from exc
    opened = os.fstat(descriptor)
    identity = (opened.st_dev, opened.st_ino)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes({"operation": operation, "pid": os.getpid()}))
            stream.flush(); os.fsync(stream.fileno())
        yield
    finally:
        if path.is_file() and not path.is_symlink():
            current = path.lstat()
            if (current.st_dev, current.st_ino) == identity:
                path.unlink()


def apply_reviewed_transition(
    *, packet_root: Path, transition: Mapping[str, Any],
    source_root: Path, config_path: Path, openclaw_root: Path,
    state_dir: Path, journal_path: Path, receipt_dir: Path,
    run=subprocess.run,
) -> dict[str, Any]:
    """Apply exact reviewed bytes; the outer orchestrator already stopped Dalton."""

    before, after, row = expected_openclaw_frame_transition_state(
        packet_root=packet_root, manifest=transition)
    _source_identity(source_root, transition["source_commit"])
    verified_plugins = _verify_plugins(
        row=row, source_root=source_root, packet_root=packet_root)
    _need(config_path.is_file() and not config_path.is_symlink()
          and config_path.read_bytes() == before,
          "OpenClaw config differs from reviewed before bytes")
    identity = gateway_identity(run=run)
    reviewed = row["historical_unresolved"]
    _stopped_checks(state_dir=state_dir, source_root=source_root,
                    journal_path=journal_path, reviewed=reviewed,
                    run=run)
    with operation_lock(config_path, "apply"):
        _need(not receipt_dir.exists() and not receipt_dir.is_symlink(),
              "broker transition receipt directory already exists")
        receipt_dir.mkdir(mode=0o700)
        _atomic(receipt_dir / "openclaw.json.before", before)
        owned_identity: tuple[int, int] | None = None
        try:
            _need(config_path.read_bytes() == before,
                  "OpenClaw config drifted before gateway stop")
            stop_gateway(openclaw_root, run=run)
            _need(config_path.read_bytes() == before,
                  "OpenClaw config drifted while gateway stopped")
            _stopped_checks(state_dir=state_dir, source_root=source_root,
                            journal_path=journal_path, reviewed=reviewed,
                            run=run)
            host_patch = row.get("managed_host_patch")
            if host_patch is not None:
                apply_reviewed_host_patch(
                    packet_root=packet_root, source_root=source_root,
                    openclaw_root=openclaw_root, row=host_patch,
                    receipt_path=receipt_dir / "host-patch-receipt.json", run=run)
            owned_identity = _compare_and_install(config_path, before, after)
            new_identity = start_gateway(openclaw_root, run=run)
            _verify_loaded_plugins(openclaw_root, verified_plugins, run=run)
            _need(config_path.read_bytes() == after,
                  "OpenClaw config changed after gateway restart")
            _stopped_checks(state_dir=state_dir, source_root=source_root,
                            journal_path=journal_path, reviewed=reviewed,
                            run=run)
            receipt = {
                "schema_version": "openclaw-broker-stopped-window-receipt-0.1",
                "status": "applied_gateway_restarted",
                "source_commit": transition["source_commit"],
                "transition_content_hash": transition["content_hash"],
                "openclaw_before_sha256": sha256_bytes(before),
                "openclaw_after_sha256": sha256_bytes(after),
                "historical_unresolved_sha256": reviewed["records_sha256"],
                "historical_unresolved_count": len(reviewed["records"]),
                "retry_authorized": False, "refund_authorized": False,
                "managed_plugins": verified_plugins,
                "managed_host_patch": (
                    {"receipt_sha256": sha256_bytes((
                        receipt_dir / "host-patch-receipt.json").read_bytes()),
                     "status": "installed_checked_no_call"}
                    if host_patch is not None else None),
                "before_gateway": identity, "after_gateway": new_identity,
                "model_calls": 0,
            }
            receipt["content_hash"] = canonical_hash(receipt)
            _write_exclusive(receipt_dir / "receipt.json", receipt)
        except Exception:
            current = (config_path.read_bytes()
                       if config_path.is_file() and not config_path.is_symlink()
                       else None)
            if loaded(GATEWAY_LABEL, run=run):
                stop_gateway(openclaw_root, run=run)
            if current == after:
                _need(_reviewed_historical_unresolved(journal_path) == dict(reviewed),
                      "failure recovery refused changed broker uncertainty")
                _need(owned_identity is not None,
                      "failure recovery lacks owned config identity")
                _compare_and_install(
                    config_path, after, before,
                    expected_identity=owned_identity)
            host_receipt = receipt_dir / "host-patch-receipt.json"
            if host_receipt.is_file() and not host_receipt.is_symlink():
                rollback_reviewed_host_patch(
                    packet_root=packet_root, source_root=source_root,
                    openclaw_root=openclaw_root,
                    row=row["managed_host_patch"], receipt_path=host_receipt)
            start_gateway(openclaw_root, run=run)
            raise
        return receipt


def rollback_reviewed_transition(
    *, packet_root: Path, transition: Mapping[str, Any],
    source_root: Path, config_path: Path, openclaw_root: Path,
    state_dir: Path, journal_path: Path, receipt_path: Path,
    run=subprocess.run,
) -> dict[str, Any]:
    """Restore reviewed before bytes without touching historical uncertainty."""

    before, after, row = expected_openclaw_frame_transition_state(
        packet_root=packet_root, manifest=transition)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrokerStoppedWindowError("broker transition receipt is invalid") from exc
    _need(isinstance(receipt, Mapping)
          and receipt.get("schema_version")
              == "openclaw-broker-stopped-window-receipt-0.1"
          and receipt.get("status") == "applied_gateway_restarted"
          and receipt.get("source_commit") == transition["source_commit"]
          and receipt.get("transition_content_hash") == transition["content_hash"]
          and receipt.get("openclaw_before_sha256") == sha256_bytes(before)
          and receipt.get("openclaw_after_sha256") == sha256_bytes(after)
          and receipt.get("historical_unresolved_sha256")
              == row["historical_unresolved"]["records_sha256"]
          and receipt.get("retry_authorized") is False
          and receipt.get("refund_authorized") is False,
          "broker transition receipt does not bind this transition")
    unsigned_receipt = {key: value for key, value in receipt.items()
                        if key != "content_hash"}
    _need(receipt.get("content_hash") == canonical_hash(unsigned_receipt),
          "broker transition receipt content hash differs")
    host_receipt = receipt_path.parent / "host-patch-receipt.json"
    expected_host = receipt.get("managed_host_patch")
    _need((row.get("managed_host_patch") is None and expected_host is None)
          or (row.get("managed_host_patch") is not None
              and expected_host == {
                  "receipt_sha256": sha256_bytes(host_receipt.read_bytes()),
                  "status": "installed_checked_no_call"}),
          "model broker host patch receipt binding differs")
    _source_identity(source_root, transition["source_commit"])
    _verify_plugins(row=row, source_root=source_root, packet_root=packet_root)
    _need(config_path.is_file() and not config_path.is_symlink()
          and config_path.read_bytes() == after,
          "refusing broker rollback over concurrent config change")
    initial_config = config_path.stat()
    initial_config_identity = (initial_config.st_dev, initial_config.st_ino)
    identity = gateway_identity(run=run)
    reviewed = row["historical_unresolved"]
    _stopped_checks(state_dir=state_dir, source_root=source_root,
                    journal_path=journal_path, reviewed=reviewed,
                    run=run)
    with operation_lock(config_path, "rollback"):
        stop_gateway(openclaw_root, run=run)
        _need(config_path.read_bytes() == after,
              "OpenClaw config drifted while gateway stopped for rollback")
        _stopped_checks(state_dir=state_dir, source_root=source_root,
                        journal_path=journal_path, reviewed=reviewed,
                        run=run)
        _compare_and_install(
            config_path, after, before,
            expected_identity=initial_config_identity)
        if row.get("managed_host_patch") is not None:
            _need(host_receipt.is_file() and not host_receipt.is_symlink(),
                  "model broker host patch receipt is unavailable")
            rollback_reviewed_host_patch(
                packet_root=packet_root, source_root=source_root,
                openclaw_root=openclaw_root, row=row["managed_host_patch"],
                receipt_path=host_receipt)
        restored_identity = start_gateway(openclaw_root, run=run)
        _need(config_path.read_bytes() == before
              and _reviewed_historical_unresolved(journal_path) == dict(reviewed),
              "broker rollback result differs")
        result = {
            "schema_version": "openclaw-broker-stopped-window-rollback-0.1",
            "status": "rolled_back_gateway_restarted",
            "source_commit": transition["source_commit"],
            "transition_content_hash": transition["content_hash"],
            "restored_sha256": sha256_bytes(before),
            "historical_unresolved_sha256": reviewed["records_sha256"],
            "retry_authorized": False, "refund_authorized": False,
            "gateway": restored_identity, "model_calls": 0,
        }
        result["content_hash"] = canonical_hash(result)
        _write_exclusive(receipt_path.parent / "rollback.json", result)
        return result
