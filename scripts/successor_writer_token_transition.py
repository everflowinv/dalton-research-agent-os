"""Closed proof for the bootstrap-owned core writer operation append."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


class WriterTokenTransitionError(RuntimeError):
    pass


def _need(value: Any, reason: str) -> None:
    if not value:
        raise WriterTokenTransitionError(reason)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_regular_file(path: Path, reason: str) -> bytes:
    _need(path.is_file() and not path.is_symlink(), reason)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WriterTokenTransitionError(reason) from exc


def source_core_operations(root: Path, commit: str) -> list[str]:
    """Read the effective operation set from one exact clean source checkout."""
    _need(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit),
          "source commit is invalid")
    _need(root.is_dir() and not root.is_symlink(), "source root is unavailable")
    root = root.resolve()
    try:
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WriterTokenTransitionError("source checkout is unavailable") from exc
    _need(head == commit and not dirty, "source checkout identity differs")
    code = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
            "import dalton_core.writer_server as w;w.install_lane_operations();"
            "print(json.dumps(sorted(w.CORE_OPERATIONS)))")
    try:
        raw = subprocess.check_output(
            [sys.executable, "-I", "-B", "-c", code, str(root / "src")], text=True,
            stderr=subprocess.DEVNULL)
        value = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise WriterTokenTransitionError("source operations are unavailable") from exc
    _need(isinstance(value, list) and value == sorted(set(value))
          and all(isinstance(x, str) and x for x in value), "source operations are invalid")
    return value


def _core(value: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
    principals = value.get("principals")
    _need(isinstance(principals, list), "writer principals are invalid")
    found = [(i, p) for i, p in enumerate(principals)
             if isinstance(p, Mapping) and p.get("principal_id") == "core"]
    _need(len(found) == 1, "core writer principal is not unique")
    return found[0]


def projected_after(before: Mapping[str, Any], old: list[str], new: list[str]) -> dict[str, Any]:
    _need(isinstance(old, list) and old == sorted(set(old))
          and all(isinstance(item, str) and item for item in old)
          and isinstance(new, list) and new == sorted(set(new))
          and all(isinstance(item, str) and item for item in new),
          "source operations are invalid")
    _need(set(old) < set(new), "successor operations are not a strict append")
    result = deepcopy(dict(before)); index, core = _core(result)
    _need(core.get("operations") == old, "core operations differ from predecessor source")
    replacement = deepcopy(dict(core)); replacement["operations"] = new
    result["principals"][index] = replacement
    return result


def bootstrap_serialized_after(before_bytes: bytes, old: list[str], new: list[str]) -> bytes:
    """Replay ``replace_token_config``'s exact stable JSON serialization."""
    try:
        before = json.loads(before_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WriterTokenTransitionError("writer token document is invalid") from exc
    # Bootstrap rewrites through Principal records and therefore always emits
    # this closed field set and sorted collections, regardless of input layout.
    _need(isinstance(before, Mapping), "writer token document is invalid")
    principals = before.get("principals")
    _need(set(before) == {"schema_version", "principals"}
          and before.get("schema_version") == "0.1"
          and isinstance(principals, list) and bool(principals),
          "writer token document is invalid")
    fields = {"principal_id", "token", "operations", "allowed_invocation_refs",
              "work_order_refs", "unrestricted", "actor_ref"}
    _need(all(isinstance(p, Mapping) and set(p) == fields for p in principals),
          "writer principal shape differs from bootstrap serializer")
    principal_ids = [p["principal_id"] for p in principals]
    _need(all(isinstance(p["principal_id"], str) and p["principal_id"]
              and isinstance(p["token"], str) and p["token"]
              and isinstance(p["unrestricted"], bool)
              and (p["principal_id"] == "core") == p["unrestricted"]
              and (p["actor_ref"] is None or isinstance(p["actor_ref"], str))
              for p in principals)
          and len(principal_ids) == len(set(principal_ids)),
          "writer principal values differ from bootstrap serializer")
    _need(all(isinstance(p[name], list)
              and all(isinstance(item, str) and item for item in p[name])
              and len(p[name]) == len(set(p[name]))
              for p in principals
              for name in ("operations", "allowed_invocation_refs", "work_order_refs")),
          "writer principal collection differs")
    _index, core = _core(before)
    _need(core["unrestricted"] is True,
          "core authority differs from bootstrap serializer")
    projected = projected_after(before, old, new)
    for principal in projected["principals"]:
        for name in ("operations", "allowed_invocation_refs", "work_order_refs"):
            _need(isinstance(principal[name], list), "writer principal collection differs")
            principal[name] = sorted(principal[name])
    return _canonical(projected)


def build_transition(*, before_path: Path, predecessor_root: Path,
                     predecessor_commit: str, successor_root: Path,
                     successor_commit: str) -> dict[str, Any]:
    before_bytes = _read_regular_file(before_path, "writer token baseline is unavailable")
    old = source_core_operations(predecessor_root, predecessor_commit)
    new = source_core_operations(successor_root, successor_commit)
    after_bytes = bootstrap_serialized_after(before_bytes, old, new)
    return {"kind": "bootstrap_source_operations_append", "target": "writer-tokens.json",
            "principal_id": "core", "before_sha256": _sha(before_bytes),
            "predicted_after_sha256": _sha(after_bytes),
            "predecessor": {"commit": predecessor_commit, "operations": old,
                            "operations_hash": _sha(_canonical(old))},
            "successor": {"commit": successor_commit, "operations": new,
                          "operations_hash": _sha(_canonical(new))},
            "added_operations": sorted(set(new) - set(old))}


def validate_transition(row: Mapping[str, Any], *, before_bytes: bytes,
                        predecessor_root: Path, successor_root: Path) -> bytes:
    keys = {"kind", "target", "principal_id", "before_sha256", "predicted_after_sha256",
            "predecessor", "successor", "added_operations"}
    _need(isinstance(row, Mapping) and set(row) == keys
          and row.get("kind") == "bootstrap_source_operations_append"
          and row.get("target") == "writer-tokens.json" and row.get("principal_id") == "core",
          "writer operation transition shape differs")
    predecessor = row["predecessor"]
    successor = row["successor"]
    proof_keys = {"commit", "operations", "operations_hash"}
    _need(isinstance(predecessor, Mapping) and set(predecessor) == proof_keys
          and isinstance(successor, Mapping) and set(successor) == proof_keys,
          "source operation proof differs")
    old = source_core_operations(predecessor_root, predecessor["commit"])
    new = source_core_operations(successor_root, successor["commit"])
    _need(row["predecessor"] == {"commit": row["predecessor"]["commit"], "operations": old,
          "operations_hash": _sha(_canonical(old))} and row["successor"] == {
          "commit": row["successor"]["commit"], "operations": new,
          "operations_hash": _sha(_canonical(new))}, "source operation proof differs")
    _need(_sha(before_bytes) == row["before_sha256"]
          and row["added_operations"] == sorted(set(new)-set(old)), "writer baseline differs")
    after = bootstrap_serialized_after(before_bytes, old, new)
    _need(_sha(after) == row["predicted_after_sha256"], "writer projection differs")
    return after


def validate_rollback_state(before_bytes: bytes, after_bytes: bytes, current_bytes: bytes) -> None:
    """Recognize only an exact endpoint; never rewrite concurrent owner bytes."""
    _need(current_bytes in {before_bytes, after_bytes},
          "writer token state changed after the reviewed bootstrap transition")
