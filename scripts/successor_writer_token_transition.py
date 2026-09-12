"""Closed proof for the bootstrap-owned core writer operation append."""
from __future__ import annotations

import hashlib
import json
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


def source_core_operations(root: Path, commit: str) -> list[str]:
    """Read the effective operation set from one exact clean source checkout."""
    root = root.resolve()
    _need(root.is_dir() and not root.is_symlink(), "source root is unavailable")
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain",
                                     "--untracked-files=all"], text=True)
    _need(head == commit and not dirty, "source checkout identity differs")
    code = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
            "import dalton_core.writer_server as w;w.install_lane_operations();"
            "print(json.dumps(sorted(w.CORE_OPERATIONS)))")
    raw = subprocess.check_output([sys.executable, "-I", "-c", code, str(root / "src")], text=True)
    value = json.loads(raw)
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
    _need(set(old) < set(new), "successor operations are not a strict append")
    result = deepcopy(dict(before)); index, core = _core(result)
    _need(core.get("operations") == old, "core operations differ from predecessor source")
    replacement = deepcopy(dict(core)); replacement["operations"] = new
    result["principals"][index] = replacement
    return result


def bootstrap_serialized_after(before_bytes: bytes, old: list[str], new: list[str]) -> bytes:
    """Replay ``replace_token_config``'s exact stable JSON serialization."""
    before = json.loads(before_bytes)
    # Bootstrap rewrites through Principal records and therefore always emits
    # this closed field set and sorted collections, regardless of input layout.
    principals = before.get("principals")
    _need(before.get("schema_version") is not None and isinstance(principals, list),
          "writer token document is invalid")
    fields = {"principal_id", "token", "operations", "allowed_invocation_refs",
              "work_order_refs", "unrestricted", "actor_ref"}
    _need(all(isinstance(p, Mapping) and set(p) == fields for p in principals),
          "writer principal shape differs from bootstrap serializer")
    projected = projected_after(before, old, new)
    for principal in projected["principals"]:
        for name in ("operations", "allowed_invocation_refs", "work_order_refs"):
            _need(isinstance(principal[name], list), "writer principal collection differs")
            principal[name] = sorted(principal[name])
    return _canonical(projected)


def build_transition(*, before_path: Path, predecessor_root: Path,
                     predecessor_commit: str, successor_root: Path,
                     successor_commit: str) -> dict[str, Any]:
    before_bytes = before_path.read_bytes(); before = json.loads(before_bytes)
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
    _need(set(row) == keys and row.get("kind") == "bootstrap_source_operations_append"
          and row.get("target") == "writer-tokens.json" and row.get("principal_id") == "core",
          "writer operation transition shape differs")
    old = source_core_operations(predecessor_root, row["predecessor"]["commit"])
    new = source_core_operations(successor_root, row["successor"]["commit"])
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
