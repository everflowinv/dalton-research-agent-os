"""Read-only display attachments, published explicitly outside research authorities.

An attachment is selected only for the exact source projection. Reading the
Cockpit never creates a directory, calls a model, or changes a research record.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .research_localization import (ResearchLocalizationError, select_localized,
                                   source_content_hash, validate_localization)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 16 * 1024 * 1024


def directory_for_database(database: str | Path) -> Path:
    return Path(database).expanduser().resolve().parent / "research-localization"


def directory_for_connection(connection: sqlite3.Connection) -> Path | None:
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return directory_for_database(row[2])
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError("display attachment is missing or exceeds the size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("display attachment must be an object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("display attachment path must not be a symlink")
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(data) > _MAX_BYTES:
        raise ValueError("display attachment exceeds the size limit")
    fd, tmp = tempfile.mkstemp(prefix=".localization-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def publish_attachment(directory: str | Path, product: Mapping[str, Any],
                       candidate: Mapping[str, Any]) -> Path:
    """Explicitly publish a validated display file; no SQLite write is made."""
    import fcntl

    valid = validate_localization(product, candidate)
    root = Path(directory).expanduser()
    if root.is_symlink():
        raise ValueError("display attachment directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = root / "records"
    if records.is_symlink():
        raise ValueError("display records directory must not be a symlink")
    records.mkdir(exist_ok=True, mode=0o700)
    target = records / (valid["content_hash"] + ".json")
    if target.exists():
        if _read_json(target) != valid:
            raise ValueError("immutable display attachment changed")
    else:
        _atomic_json(target, valid)
    lock = root / ".index.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        index_path = root / "index.json"
        index = _read_json(index_path) if index_path.exists() else {
            "schema_version": "research-localization-index:0.1", "entries": {}}
        if index.get("schema_version") != "research-localization-index:0.1" or not isinstance(index.get("entries"), dict):
            raise ValueError("unsupported display index")
        index["entries"][source_content_hash(product)] = valid["content_hash"]
        _atomic_json(index_path, index)
    return target


def localize_library(connection: sqlite3.Connection, library: Mapping[str, Any]) -> dict[str, Any]:
    root = directory_for_connection(connection)
    if root is None or not root.exists():
        return dict(library)
    result = copy.deepcopy(dict(library))
    try:
        if root.is_symlink():
            raise ValueError("unsafe display directory")
        index = _read_json(root / "index.json")
        if index.get("schema_version") != "research-localization-index:0.1":
            raise ValueError("unsupported display index")
        entries = index["entries"]
        if not isinstance(entries, dict):
            raise ValueError("invalid display index entries")
    except (OSError, ValueError, KeyError):
        return result
    for i, product in enumerate(result.get("products") or []):
        key = entries.get(source_content_hash(product))
        if not isinstance(key, str) or not _SHA.fullmatch(key):
            continue
        try:
            if (root / "records").is_symlink():
                raise ValueError("unsafe display records directory")
            candidate = _read_json(root / "records" / (key + ".json"))
            if candidate.get("content_hash") != key:
                raise ValueError("display index binding mismatch")
            result["products"][i] = select_localized(product, candidate)
        except (OSError, ValueError, KeyError, TypeError, ResearchLocalizationError):
            # A stale/invalid translation cannot hide the underlying research.
            product["localization_status"] = "原文已更新，中文版本待同步"
    return result


def publish_ui_texts(directory: str | Path, batches: list[dict[str, Any]]) -> Path:
    """Publish exact-string display mappings after the same fidelity checks."""
    entries: dict[str, str] = {}
    for batch in batches:
        source, candidate = batch["source"], batch["localization"]
        valid = validate_localization(source, candidate)
        for original, localized in zip(source["sections"], valid["sections"]):
            old, new = original["body"], localized["body"]
            if old in entries and entries[old] != new:
                raise ValueError("conflicting translations for the same exact text")
            entries[old] = new
    root = Path(directory)
    if root.is_symlink():
        raise ValueError("unsafe display directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / "ui-texts.json"
    _atomic_json(target, {"schema_version": "cockpit-ui-texts:0.1", "batches": batches})
    return target


@functools.lru_cache(maxsize=8)
def _load_ui_cached(path_string: str, inode: int, modified_ns: int, size: int) -> dict[str, str]:
    payload = _read_json(Path(path_string))
    if payload.get("schema_version") != "cockpit-ui-texts:0.1":
        return {}
    entries = {}
    for batch in payload.get("batches", []):
        source = batch["source"]
        valid = validate_localization(source, batch["localization"])
        for original, localized in zip(source["sections"], valid["sections"]):
            old, new = original["body"], localized["body"]
            if old in entries and entries[old] != new:
                raise ValueError("conflicting display translation")
            entries[old] = new
    return entries


def load_ui_texts(database: str | Path) -> dict[str, str]:
    path = directory_for_database(database) / "ui-texts.json"
    try:
        if path.parent.is_symlink() or path.is_symlink():
            return {}
        stat = path.stat()
        return dict(_load_ui_cached(str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError, KeyError, TypeError, ResearchLocalizationError):
        return {}
