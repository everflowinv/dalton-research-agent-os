"""Closed R25 research-publication file transition.

This helper is deliberately ignorant of arbitrary destination paths.  It
validates the six reviewed authorities plus the bounded localization seed
tree, installs them with exclusive-create semantics, and removes only bytes
that the same manifest proves this release installed.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
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
              and 0 <= raw["size"] <= 2_000_000,
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


def artifact_bytes(packet_root: Path, row: Mapping[str, Any]) -> bytes:
    packet_root = packet_root.resolve()
    path = packet_root / row["artifact"]
    _need(path.is_relative_to(packet_root) and path.is_file()
          and not path.is_symlink(), "research publication artifact is unavailable")
    data = path.read_bytes()
    _need(len(data) == row["size"] and _sha(data) == row["sha256"],
          "research publication artifact bytes differ")
    return data


def apply(*, packet_root: Path, state_dir: Path, launch_agents_dir: Path,
          transition: Mapping[str, Any]) -> list[Path]:
    """Install the closed inventory; an existing target always refuses."""
    value = validate_transition(transition)
    roots = {"launch_agent": launch_agents_dir.resolve()}
    state = state_dir.resolve()
    created: list[Path] = []
    try:
        for row in value["files"]:
            target = ((roots["launch_agent"] / row["path"])
                      if row["kind"] == "launch_agent" else state / row["path"])
            root = roots["launch_agent"] if row["kind"] == "launch_agent" else state
            _need(target.is_relative_to(root), "research publication target escaped its root")
            _need(not target.exists() and not target.is_symlink(),
                  f"research publication target already exists: {row['path']}")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, row["mode"])
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(artifact_bytes(packet_root, row)); stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            os.chmod(target, row["mode"]); created.append(target)
        return created
    except BaseException:
        for target in reversed(created):
            target.unlink()
        raise


def rollback(*, state_dir: Path, launch_agents_dir: Path,
             transition: Mapping[str, Any]) -> list[Path]:
    """Remove only unchanged files installed by this manifest.

    Runtime-created work/review records are intentionally outside ``files``
    and are never traversed or removed.
    """
    value = validate_transition(transition)
    state = state_dir.resolve(); launch = launch_agents_dir.resolve()
    removed: list[Path] = []
    for row in reversed(value["files"]):
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
