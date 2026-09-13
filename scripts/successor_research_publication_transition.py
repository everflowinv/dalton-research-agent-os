"""Closed R25 research-publication file transition.

This helper is deliberately ignorant of arbitrary destination paths.  It
validates the six reviewed authorities plus the bounded localization seed
tree, installs them with exclusive-create semantics, and removes only bytes
that the same manifest proves this release installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "successor-research-publication-transition-0.1"
FIXED_FILES = frozenset({
    "research-localization-draft-model-config.json",
    "research-localization-verifier-model-config.json",
    "research-language-check-model-config.json",
    "research-language-revision-model-config.json",
    "research-language-policy.json",
    "research-publication-worker-config.json",
})
LAUNCH_AGENT_LABEL = "com.dalton.research-publication-worker"
LAUNCH_AGENT_NAME = f"{LAUNCH_AGENT_LABEL}.plist"
_HEX = r"[0-9a-f]{64}"
_SEED_PATTERNS = (
    re.compile(r"research-localization/(?:index|ui-texts)\.json"),
    re.compile(r"research-localization/\.index\.lock"),
    re.compile(rf"research-localization/records/{_HEX}\.json"),
    re.compile(rf"research-localization/language-reviews/{_HEX}\.(?:json|md)"),
    re.compile(rf"research-localization/ui-records/{_HEX}\.json"),
)
WORKER_CONFIG = "research-publication-worker-config.json"
_OWNER_ROOT = Path("/Users/everflow/Projects/dalton-owner-activation-20260910")


def validate_worker_config_bytes(data: bytes, *,
                                 expected_source_commit: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchPublicationTransitionError(
            "research publication worker config is invalid") from exc
    expected = {"schema_version", "core_db", "scheduler_db", "model_config",
                "verifier_config", "checker_config", "brain_config", "work_dir",
                "output_directory", "workers", "chunk_chars", "max_cost_per_call",
                "draft_attempts", "publication_gate"}
    _need(isinstance(value, dict) and set(value) == expected
          and value.get("schema_version") == "research-publication-worker-config:0.1"
          and value.get("workers") in {4, 8} and value.get("chunk_chars") == 4500
          and value.get("max_cost_per_call") == 1.0
          and value.get("draft_attempts") == 2,
          "research publication worker config shape differs")
    for key in ("core_db", "scheduler_db", "model_config", "verifier_config",
                "checker_config", "brain_config", "work_dir", "output_directory"):
        _need(isinstance(value[key], str) and Path(value[key]).is_absolute(),
              f"research publication worker {key} is not absolute")
    _need(Path(value["work_dir"]).name == "research-publication-work"
          and Path(value["output_directory"]).name == "research-localization",
          "research publication worker output paths differ")
    gate = value["publication_gate"]
    release_pointer = Path(gate.get("release_pointer", "")) if isinstance(gate, dict) else Path("")
    runtime_pointer = Path(gate.get("runtime_pointer", "")) if isinstance(gate, dict) else Path("")
    canonical_pointers = (release_pointer == _OWNER_ROOT / "current-release.json"
                          and runtime_pointer == _OWNER_ROOT / "current-runtime-config.json")
    confined_pointers = (expected_source_commit is None and release_pointer.is_absolute()
                         and runtime_pointer.is_absolute()
                         and release_pointer.parent == runtime_pointer.parent
                         and release_pointer.is_relative_to(Path("/private/tmp")))
    _need(isinstance(gate, dict) and set(gate) == {
        "release_pointer", "runtime_pointer", "expected_release_ref",
        "expected_source_commit"}
        and (canonical_pointers or confined_pointers)
        and gate["expected_release_ref"] == "foundation-r25"
        and isinstance(gate["expected_source_commit"], str)
        and re.fullmatch(r"[0-9a-f]{40}", gate["expected_source_commit"])
        and (expected_source_commit is None
             or gate["expected_source_commit"] == expected_source_commit),
        "research publication gate authority differs")
    return value


def validate_waiting_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(value, Mapping) and set(value) == {
        "schema_version", "status", "model_calls", "observed_release_sha256",
        "observed_runtime_sha256", "checked_at"}
        and value.get("schema_version") == "research-publication-worker-checkpoint:0.1"
        and value.get("status") == "waiting_for_release_publication"
        and value.get("model_calls") == 0,
        "research publication waiting checkpoint shape differs")
    for key in ("observed_release_sha256", "observed_runtime_sha256"):
        _need(value.get(key) is None or (isinstance(value[key], str)
              and re.fullmatch(_HEX, value[key])),
              "research publication checkpoint pointer hash differs")
    try:
        timestamp = datetime.fromisoformat(str(value.get("checked_at", "")))
    except ValueError as exc:
        raise ResearchPublicationTransitionError(
            "research publication checkpoint time is invalid") from exc
    _need(timestamp.tzinfo is not None and timestamp.utcoffset().total_seconds() == 0,
          "research publication checkpoint time is not UTC")
    return dict(value)


class ResearchPublicationTransitionError(RuntimeError):
    pass


def _need(value: Any, reason: str) -> None:
    if not value:
        raise ResearchPublicationTransitionError(reason)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: Any) -> str:
    _need(isinstance(value, str) and value, "transition path is empty")
    path = Path(value)
    _need(not path.is_absolute() and ".." not in path.parts
          and path.as_posix() == value, "transition path is unsafe")
    return value


def _allowed(path: str, *, kind: str) -> bool:
    if kind == "authority":
        return path in FIXED_FILES
    if kind == "launch_agent":
        return path == LAUNCH_AGENT_NAME
    return kind == "seed" and any(pattern.fullmatch(path) for pattern in _SEED_PATTERNS)


def _no_symlink_components(root: Path, path: Path, *, include_leaf: bool) -> bool:
    relative = path.relative_to(root)
    cursor = root
    parts = relative.parts if include_leaf else relative.parts[:-1]
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return False
    return True


def validate_transition(value: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(value, Mapping) and set(value) == {
        "schema_version", "kind", "launch_agent_label", "files"
    }, "research publication transition shape differs")
    _need(value.get("schema_version") == SCHEMA_VERSION
          and value.get("kind") == "exclusive_add"
          and value.get("launch_agent_label") == LAUNCH_AGENT_LABEL,
          "research publication transition identity differs")
    rows = value.get("files")
    _need(isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
          and rows, "research publication file inventory is empty")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    kinds: dict[str, int] = {}
    for raw in rows:
        _need(isinstance(raw, Mapping) and set(raw) == {
            "path", "kind", "artifact", "sha256", "size", "mode"
        }, "research publication file row shape differs")
        path = _safe_relative(raw["path"])
        kind = raw["kind"]
        _need(kind in {"authority", "seed", "launch_agent"}
              and _allowed(path, kind=kind),
              f"research publication path is outside the closed inventory: {path}")
        _need(path not in seen, "research publication file path is duplicated")
        seen.add(path); kinds[kind] = kinds.get(kind, 0) + 1
        artifact = _safe_relative(raw["artifact"])
        digest = raw["sha256"]
        _need(isinstance(digest, str) and re.fullmatch(_HEX, digest),
              "research publication file hash is invalid")
        _need(isinstance(raw["size"], int) and not isinstance(raw["size"], bool)
              and 0 <= raw["size"] <= 16_000_000,
              "research publication file size is invalid")
        expected_mode = 0o644 if kind == "launch_agent" else 0o600
        _need(raw["mode"] == expected_mode,
              "research publication file mode differs")
        parsed.append(dict(raw))
    _need({row["path"] for row in parsed if row["kind"] == "authority"}
          == FIXED_FILES, "research publication authority inventory differs")
    _need(kinds.get("launch_agent") == 1,
          "research publication LaunchAgent inventory differs")
    return {**dict(value), "files": sorted(parsed, key=lambda row: row["path"])}


def build_transition(*, packet_root: Path,
                     authority_files: Mapping[str, Path],
                     seed_files: Mapping[str, Path],
                     launch_agent_path: Path) -> dict[str, Any]:
    """Build the closed row from packet-confined frozen artifacts."""
    _need(set(authority_files) == FIXED_FILES,
          "research publication authority inventory differs")
    sources = [(name, "authority", path) for name, path in authority_files.items()]
    sources += [(name, "seed", path) for name, path in seed_files.items()]
    sources.append((LAUNCH_AGENT_NAME, "launch_agent", launch_agent_path))
    packet = packet_root.resolve(); rows = []
    for name, kind, source in sources:
        source = source.absolute()
        _need(source.is_relative_to(packet),
              "research publication artifact is outside packet")
        relative = source.relative_to(packet).as_posix()
        data = source.read_bytes() if source.is_file() and not source.is_symlink() else b""
        _need(data or (kind == "seed" and name.endswith(".index.lock")),
              "research publication artifact is unavailable")
        rows.append({"path": name, "kind": kind, "artifact": relative,
                     "sha256": _sha(data), "size": len(data),
                     "mode": 0o644 if kind == "launch_agent" else 0o600})
    return validate_transition({"schema_version": SCHEMA_VERSION,
        "kind": "exclusive_add", "launch_agent_label": LAUNCH_AGENT_LABEL,
        "files": rows})


def artifact_bytes(packet_root: Path, row: Mapping[str, Any]) -> bytes:
    packet_root = packet_root.resolve()
    path = packet_root / row["artifact"]
    _need(path.is_relative_to(packet_root) and path.is_file()
          and not path.is_symlink()
          and _no_symlink_components(packet_root, path, include_leaf=True),
          "research publication artifact is unavailable")
    data = path.read_bytes()
    _need(len(data) == row["size"] and _sha(data) == row["sha256"],
          "research publication artifact bytes differ")
    if row["kind"] == "launch_agent":
        try:
            plist = plistlib.loads(data)
        except Exception as exc:
            raise ResearchPublicationTransitionError(
                "research publication LaunchAgent is invalid") from exc
        argv = plist.get("ProgramArguments")
        _need(plist.get("Label") == LAUNCH_AGENT_LABEL
              and plist.get("StartInterval") == 300
              and plist.get("RunAtLoad") is True
              and isinstance(argv, list) and len(argv) == 6
              and isinstance(argv[0], str) and Path(argv[0]).is_absolute()
              and argv[1:5] == ["-m", "dalton_core.research_output_preparation",
                                "run-worker", "--config"]
              and isinstance(argv[5], str) and Path(argv[5]).is_absolute()
              and Path(argv[5]).name == "research-publication-worker-config.json",
              "research publication LaunchAgent contract differs")
    elif row["kind"] == "authority" and row["path"] == WORKER_CONFIG:
        validate_worker_config_bytes(data)
    return data


def apply(*, packet_root: Path, state_dir: Path, launch_agents_dir: Path | None,
          transition: Mapping[str, Any]) -> list[Path]:
    """Install the closed inventory; an existing target always refuses."""
    value = validate_transition(transition)
    _need(launch_agents_dir is not None,
          "research publication transition requires a LaunchAgents directory")
    roots = {"launch_agent": (None if launch_agents_dir is None else launch_agents_dir.resolve())}
    state = state_dir.resolve()
    created: list[tuple[Path, dict[str, Any]]] = []
    try:
        for row in value["files"]:
            if row["kind"] == "launch_agent" and roots["launch_agent"] is None:
                continue
            target = ((roots["launch_agent"] / row["path"])
                      if row["kind"] == "launch_agent" else state / row["path"])
            root = roots["launch_agent"] if row["kind"] == "launch_agent" else state
            _need(target.is_relative_to(root), "research publication target escaped its root")
            _need(_no_symlink_components(root, target, include_leaf=True),
                  "research publication target has a symlink component")
            _need(not target.exists() and not target.is_symlink(),
                  f"research publication target already exists: {row['path']}")
            data = artifact_bytes(packet_root, row)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _need(target.parent.resolve().is_relative_to(root)
                  and _no_symlink_components(root, target, include_leaf=False),
                  "research publication target parent changed")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), row["mode"])
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data); stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            os.chmod(target, row["mode"]); created.append((target, row))
        return [target for target, _row in created]
    except BaseException as exc:
        conflicts = []
        for target, row in reversed(created):
            if (target.is_file() and not target.is_symlink()
                    and stat.S_IMODE(target.stat().st_mode) == row["mode"]
                    and target.stat().st_size == row["size"]
                    and _sha(target.read_bytes()) == row["sha256"]):
                target.unlink()
            else:
                conflicts.append(row["path"])
        if conflicts:
            raise ResearchPublicationTransitionError(
                "transition failed and preserved concurrently changed targets: "
                + ",".join(sorted(conflicts))) from exc
        raise


def rollback(*, state_dir: Path, launch_agents_dir: Path | None,
             transition: Mapping[str, Any]) -> list[Path]:
    """Remove only unchanged files installed by this manifest.

    Runtime-created work/review records are intentionally outside ``files``
    and are never traversed or removed.
    """
    value = validate_transition(transition)
    state = state_dir.resolve()
    launch = None if launch_agents_dir is None else launch_agents_dir.resolve()
    removed: list[Path] = []
    for row in reversed(value["files"]):
        if row["kind"] == "launch_agent" and launch is None:
            continue
        target = ((launch / row["path"])
                  if row["kind"] == "launch_agent" else state / row["path"])
        if not target.exists() and not target.is_symlink():
            continue
        _need(target.is_file() and not target.is_symlink()
              and stat.S_IMODE(target.stat().st_mode) == row["mode"]
              and target.stat().st_size == row["size"]
              and _sha(target.read_bytes()) == row["sha256"],
              f"research publication rollback target changed: {row['path']}")
        target.unlink(); removed.append(target)
    return removed


def rollback_preserving_changed(*, state_dir: Path, launch_agents_dir: Path,
                                transition: Mapping[str, Any]) -> dict[str, list[str]]:
    """Deployment rollback: delete exact initial bytes and retain later records."""
    value = validate_transition(transition); state = state_dir.resolve()
    launch = launch_agents_dir.resolve(); removed = []; preserved = []
    for row in reversed(value["files"]):
        target = ((launch / row["path"]) if row["kind"] == "launch_agent"
                  else state / row["path"])
        if not target.exists() and not target.is_symlink():
            continue
        if (target.is_file() and not target.is_symlink()
                and stat.S_IMODE(target.stat().st_mode) == row["mode"]
                and target.stat().st_size == row["size"]
                and _sha(target.read_bytes()) == row["sha256"]):
            target.unlink(); removed.append(row["path"])
        else:
            preserved.append(row["path"])
    return {"removed": sorted(removed), "preserved_changed": sorted(preserved)}
