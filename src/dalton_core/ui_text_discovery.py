"""Discover and seal stable batches of untranslated Cockpit authority text.

This module is deliberately transport-free.  A scheduled worker supplies the
existing localization ``prepare`` callback; page reads never import or invoke
it.  Batch manifests are immutable, while retry results may advance from
pending to completed without changing the paid pipeline's product identity.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from .cockpit_research_library import research_library
from .mission_stage import retired_claim_refs
from .store import canonical_json, content_hash


MANIFEST_SCHEMA = "cockpit-ui-text-batch:0.1"
RESULT_SCHEMA = "cockpit-ui-text-batch-result:0.1"
POLL_SCHEMA = "cockpit-ui-text-discovery-poll:0.1"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _text_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _claim_hash(wire: Mapping[str, Any]) -> str:
    if "content_hash" not in wire:  # compact fixtures and pre-self-hash records
        return content_hash(wire)
    unsigned = dict(wire); embedded = unsigned.pop("content_hash")
    digest = content_hash(unsigned)
    if embedded != digest:
        raise ValueError("claim embedded content hash drifted")
    return digest


def _safe_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("UI text state directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("UI text state file must not be a symlink")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def _replace(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("UI text result must not be a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _write_exclusive(temporary, value)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_closed(path: Path, schema: str, fields: set[str]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe UI text state: {path.name}")
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != schema \
            or set(value) != fields:
        raise ValueError(f"invalid UI text state: {path.name}")
    unsigned = dict(value)
    digest = unsigned.pop("content_hash")
    if digest != _hash(unsigned):
        raise ValueError(f"UI text state hash drifted: {path.name}")
    return value


def _current_index_canonicality(connection: Any) -> dict[str, bool]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claim_index_entry_versions'"
    ).fetchone()
    if table is None:
        return {}
    rows = connection.execute(
        "SELECT e.claim_version_ref,e.is_canonical FROM claim_index_entry_versions e "
        "JOIN (SELECT entry_ref,MAX(version_number) AS version_number "
        "FROM claim_index_entry_versions GROUP BY entry_ref) latest "
        "ON latest.entry_ref=e.entry_ref AND latest.version_number=e.version_number"
    ).fetchall()
    result: dict[str, bool] = {}
    for row in rows:
        ref = str(row["claim_version_ref"])
        result[ref] = result.get(ref, False) or bool(row["is_canonical"])
    return result


def _claim_rows(connection: Any, mission: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    subjects = {str(row["company_ref"]) for row in mission.get("universe") or []
                if isinstance(row, Mapping) and row.get("company_ref")}
    industry = mission.get("industry_ref")
    if isinstance(industry, str) and industry:
        subjects.add(industry)
    retired = retired_claim_refs(connection)
    rows = connection.execute(
        "SELECT c.claim_version_id,c.claim_ref,c.claim_json,c.content_hash "
        "FROM claim_versions c JOIN (SELECT claim_ref,MAX(version_number) AS version_number "
        "FROM claim_versions GROUP BY claim_ref) latest "
        "ON latest.claim_ref=c.claim_ref AND latest.version_number=c.version_number"
    ).fetchall()
    indexed = _current_index_canonicality(connection)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        ref = str(row["claim_version_id"])
        if ref in retired:
            continue
        wire = json.loads(row["claim_json"])
        if canonical_json(wire) != row["claim_json"] or _claim_hash(wire) != row["content_hash"]:
            raise ValueError(f"claim authority hash drifted: {ref}")
        if wire.get("subject_ref") not in subjects:
            continue
        text = wire.get("normalized_statement")
        if isinstance(text, str) and text.strip():
            found[ref] = {"text": text, "ref": ref, "hash": str(row["content_hash"]),
                          "canonical": indexed.get(ref, True)}
    return found


def _historical_library_claim_rows(connection: Any, refs: set[str],
                                   mission: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve exact library ClaimVersion refs, including superseded versions."""
    subjects = {str(row["company_ref"]) for row in mission.get("universe") or []
                if isinstance(row, Mapping) and row.get("company_ref")}
    if isinstance(mission.get("industry_ref"), str):
        subjects.add(mission["industry_ref"])
    found: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(refs), 400):
        batch = sorted(refs)[offset:offset + 400]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            "SELECT claim_version_id,claim_json,content_hash FROM claim_versions "
            f"WHERE claim_version_id IN ({placeholders})", batch).fetchall()
        for row in rows:
            ref = str(row["claim_version_id"])
            wire = json.loads(row["claim_json"])
            wire_hash = _claim_hash(wire)
            if canonical_json(wire) != row["claim_json"] or wire_hash != row["content_hash"]:
                raise ValueError(f"claim authority hash drifted: {ref}")
            text = wire.get("normalized_statement")
            if wire.get("subject_ref") in subjects and isinstance(text, str) and text.strip():
                found[ref] = {"text": text, "ref": ref, "hash": wire_hash,
                              "canonical": False}
    return found


def _library_claim_refs(connection: Any, mission: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    for member in mission.get("universe") or []:
        if not isinstance(member, Mapping) or not member.get("company_ref"):
            continue
        library = research_library(connection, mission, str(member["company_ref"]),
                                   localize=False)
        for product in library.get("products") or []:
            for section in product.get("sections") or []:
                for source in section.get("sources") or []:
                    if isinstance(source, Mapping):
                        if source.get("kind") == "claim" and isinstance(source.get("ref"), str):
                            refs.add(source["ref"])
                    elif isinstance(source, str):
                        refs.add(source)
    return refs


def _discover(connection: Any, mission: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims = _claim_rows(connection, mission)
    library_refs = _library_claim_refs(connection, mission)
    for ref, row in _historical_library_claim_rows(connection, library_refs - set(claims),
                                                   mission).items():
        claims[ref] = row
    by_text: dict[str, dict[str, Any]] = {}
    for ref, row in claims.items():
        if not row["canonical"] and ref not in library_refs:
            continue
        # Current mission Claims are visible in the conclusion list.  A Claim
        # cited by a library section receives an additional exact source role.
        roles = ["canonical_claim"] if row["canonical"] else []
        if ref in library_refs:
            roles.append("library_claim_source")
        source = {"kind": "claim", "roles": roles, "ref": ref, "hash": row["hash"]}
        entry = by_text.setdefault(row["text"], {
            "text": row["text"], "text_sha256": _text_hash(row["text"]), "sources": []})
        entry["sources"].append(source)
    for entry in by_text.values():
        entry["sources"].sort(key=lambda item: (item["ref"], item["hash"]))
    return sorted(by_text.values(), key=lambda item: (item["text_sha256"], item["text"]))


def _validate_manifest(path: Path) -> dict[str, Any]:
    value = _read_closed(path, MANIFEST_SCHEMA, {
        "schema_version", "batch_ref", "mission_ref", "entries", "product", "content_hash"})
    entries = value["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > 30:
        raise ValueError(f"invalid UI text batch entries: {path.name}")
    if entries != sorted(entries, key=lambda item: (item["text_sha256"], item["text"])):
        raise ValueError(f"UI text batch ordering drifted: {path.name}")
    if sum(len(item["text"]) for item in entries) > 4500:
        raise ValueError(f"UI text batch is oversized: {path.name}")
    for item in entries:
        if set(item) != {"text", "text_sha256", "sources"} \
                or not isinstance(item["text"], str) or not item["text"] \
                or item["text_sha256"] != _text_hash(item["text"]) \
                or not isinstance(item["sources"], list) or not item["sources"]:
            raise ValueError(f"invalid UI text batch entry: {path.name}")
        for source in item["sources"]:
            if (not isinstance(source, dict)
                    or set(source) != {"kind", "roles", "ref", "hash"}
                    or source["kind"] != "claim"
                    or not isinstance(source["roles"], list)
                    or not source["roles"]
                    or not set(source["roles"]).issubset(
                        {"canonical_claim", "library_claim_source"})
                    or not all(isinstance(source[key], str) and source[key]
                               for key in ("ref", "hash"))):
                raise ValueError(f"invalid UI text source binding: {path.name}")
    batch_hash = _hash({"mission_ref": value["mission_ref"], "entries": entries})
    if value["batch_ref"] != f"ui-text-batch:{batch_hash}":
        raise ValueError(f"UI text batch identity drifted: {path.name}")
    expected = _product(value["mission_ref"], value["batch_ref"], entries)
    if value["product"] != expected:
        raise ValueError(f"UI text batch product drifted: {path.name}")
    return value


def _verify_manifest_authority(connection: Any, manifest: Mapping[str, Any]) -> None:
    """Re-read every immutable ClaimVersion named by a sealed manifest."""
    for entry in manifest["entries"]:
        for source in entry["sources"]:
            row = connection.execute(
                "SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?",
                (source["ref"],),
            ).fetchone()
            if row is None:
                raise ValueError("sealed UI text source is missing")
            wire = json.loads(row["claim_json"])
            wire_hash = _claim_hash(wire)
            if (canonical_json(wire) != row["claim_json"]
                    or wire_hash != row["content_hash"]
                    or row["content_hash"] != source["hash"]
                    or wire.get("normalized_statement") != entry["text"]):
                raise ValueError("sealed UI text source authority drifted")


def _product(mission_ref: str, batch_ref: str,
             entries: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "ui_text", "label": "界面文字", "status": "available",
        "subject_ref": mission_ref, "version_ref": batch_ref,
        "sections": [{"title": "界面文字", "body": item["text"], "gaps": [],
                      "sources": item["sources"]} for item in entries],
        "gaps": [],
    }


def _chunks(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for entry in entries:
        size = len(entry["text"])
        if size > 4500:
            raise ValueError("one UI text value exceeds the batch character bound")
        if current and (len(current) == 30 or chars + size > 4500):
            result.append(current); current = []; chars = 0
        current.append(entry); chars += size
    if current:
        result.append(current)
    return result


def _eligible(manifest: Mapping[str, Any], mission_ref: str,
              discovered: list[Mapping[str, Any]]) -> bool:
    if manifest["mission_ref"] != mission_ref:
        return False
    current = {
        entry["text"]: {(source["ref"], source["hash"])
                        for source in entry["sources"]}
        for entry in discovered
    }
    return all(entry["text"] in current and
               {(source["ref"], source["hash"]) for source in entry["sources"]}
               .issubset(current[entry["text"]]) for entry in manifest["entries"])


def poll_ui_texts(connection: Any, mission: Mapping[str, Any], *, state_dir: Path,
                  mapping: Mapping[str, str],
                  prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> dict[str, Any]:
    """Seal newly discovered strings and prepare at most four batches.

    ``prepare`` receives the manifest's stable ``ui_text`` product.  Returning
    ``{"status": "completed"}`` records completion; every other result stays
    retryable and is offered again on the next scheduled poll.
    """
    if not isinstance(mapping, Mapping):
        raise TypeError("mapping must be an exact source-to-display mapping")
    root = Path(state_dir)
    _safe_directory(root)
    manifests = root / "manifests"; results = root / "results"
    _safe_directory(manifests); _safe_directory(results)
    lock_path = root / ".discovery.lock"
    if lock_path.is_symlink():
        raise ValueError("UI text discovery lock must not be a symlink")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = [_validate_manifest(path) for path in sorted(manifests.glob("*.json"))]
        for manifest in existing:
            _verify_manifest_authority(connection, manifest)
        discovered = _discover(connection, mission)
        mission_ref = mission.get("mission_ref")
        if not isinstance(mission_ref, str) or not mission_ref:
            raise ValueError("mission_ref must be non-empty text")
        assigned = {entry["text"] for manifest in existing
                    if _eligible(manifest, mission_ref, discovered)
                    for entry in manifest["entries"]}
        new = [entry for entry in discovered if entry["text"] not in mapping
               and entry["text"] not in assigned]
        for entries in _chunks(new):
            digest = _hash({"mission_ref": mission_ref, "entries": entries})
            batch_ref = f"ui-text-batch:{digest}"
            unsigned = {"schema_version": MANIFEST_SCHEMA, "batch_ref": batch_ref,
                        "mission_ref": mission_ref, "entries": entries,
                        "product": _product(mission_ref, batch_ref, entries)}
            manifest = {**unsigned, "content_hash": _hash(unsigned)}
            _write_exclusive(manifests / f"{digest}.json", manifest)
            existing.append(manifest)

        summaries = []
        attempted = 0
        for manifest in existing:
            texts = [entry["text"] for entry in manifest["entries"]]
            result_path = results / (manifest["batch_ref"].split(":", 1)[1] + ".json")
            prior = None
            if result_path.exists() or result_path.is_symlink():
                prior = _read_closed(result_path, RESULT_SCHEMA, {
                    "schema_version", "batch_ref", "manifest_hash", "status", "result",
                    "attempts", "content_hash"})
                if prior["batch_ref"] != manifest["batch_ref"] \
                        or prior["manifest_hash"] != manifest["content_hash"]:
                    raise ValueError("UI text result binding drifted")
            mapped = all(text in mapping for text in texts)
            if mapped:
                summaries.append({"batch_ref": manifest["batch_ref"],
                                  "status": "mapped"})
                continue
            summaries.append({"batch_ref": manifest["batch_ref"], "status": "eligible",
                              "attempts": 0 if prior is None else prior["attempts"],
                              "manifest": manifest, "result_path": result_path})

        eligible = [row for row in summaries if row["status"] == "eligible"]
        for row in eligible:
            if not _eligible(row["manifest"], mission_ref, discovered):
                row["status"] = "ineligible"
        queue = sorted((row for row in eligible if row["status"] == "eligible"),
                       key=lambda row: (row["attempts"] != 0, row["attempts"],
                                        row["batch_ref"]))[:4]
        selected = {row["batch_ref"] for row in queue}
        for row in eligible:
            if row["status"] == "eligible" and row["batch_ref"] not in selected:
                row["status"] = "deferred"
        for row in queue:
            manifest = row.pop("manifest")
            result_path = row.pop("result_path")
            attempted += 1
            try:
                outcome = prepare(manifest["product"])
                completed = isinstance(outcome, Mapping) and outcome.get("status") == "completed"
                result_value: Mapping[str, Any] = dict(outcome) if isinstance(outcome, Mapping) \
                    else {"reason": "prepare returned no result"}
            except Exception as exc:
                completed = False
                result_value = {"reason": f"{type(exc).__name__}: {exc}"}
            unsigned_result = {"schema_version": RESULT_SCHEMA,
                               "batch_ref": manifest["batch_ref"],
                               "manifest_hash": manifest["content_hash"],
                               "status": "completed" if completed else "pending",
                               "attempts": row["attempts"] + 1, "result": result_value}
            saved = {**unsigned_result, "content_hash": _hash(unsigned_result)}
            _replace(result_path, saved)
            row["status"] = saved["status"]
            row["attempts"] = saved["attempts"]
        for row in summaries:
            row.pop("manifest", None); row.pop("result_path", None)
        return {"schema_version": POLL_SCHEMA, "discovered": len(discovered),
                "sealed": len(new), "attempted": attempted, "batches": summaries,
                "pending": sum(row["status"] in {"pending", "deferred"} for row in summaries)}
