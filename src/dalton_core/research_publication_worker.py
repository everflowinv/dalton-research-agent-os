"""Read-only discovery worker for hash-bound research presentation preparation."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from .cockpit_research_library import research_library

SCHEMA_VERSION = "research-publication-worker-state:0.1"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _identity(product: Mapping[str, Any]) -> dict[str, str]:
    fields = {key: product.get(key) for key in ("kind", "subject_ref", "version_ref")}
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise ValueError("available research product lacks a stable identity")
    return {key: str(value) for key, value in fields.items()}


def _state_path(state_dir: Path, identity: Mapping[str, str]) -> Path:
    return state_dir / (_hash(identity) + ".json")


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"publication worker state is unreadable: {path.name}") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or set(value) != {"schema_version", "status", "identity", "product_hash",
                              "result", "content_hash"}):
        raise ValueError(f"publication worker state is invalid: {path.name}")
    body = dict(value); digest = body.pop("content_hash")
    if digest != _hash(body):
        raise ValueError(f"publication worker state hash drifted: {path.name}")
    return value


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def poll_once(
    connection: Any,
    mission: Mapping[str, Any],
    *,
    state_dir: Path,
    prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    library_reader: Callable[[Any, Mapping[str, Any], str], Mapping[str, Any]] = research_library,
) -> dict[str, Any]:
    """Discover current products and prepare each unseen hash independently.

    Library reads never call ``prepare`` for missing products or completed
    hashes. The injected prepare callback is the only place allowed to spend.
    """

    summaries = []
    seen: set[tuple[str, str]] = set()
    for member in mission.get("universe") or []:
        company_ref = member.get("company_ref") if isinstance(member, Mapping) else None
        if not isinstance(company_ref, str) or not company_ref:
            continue
        library = library_reader(connection, mission, company_ref)
        for product in library.get("products") or []:
            if not isinstance(product, Mapping) or product.get("status") != "available":
                continue
            identity = _identity(product)
            product_hash = _hash(product)
            key = (_hash(identity), product_hash)
            if key in seen:
                continue
            seen.add(key)
            path = _state_path(state_dir, identity)
            prior = _read_state(path)
            if (prior is not None and prior.get("status") == "completed"
                    and prior.get("product_hash") == product_hash):
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": "unchanged"})
                continue
            if (prior is not None and prior.get("status") == "pending"
                    and prior.get("product_hash") == product_hash):
                summaries.append({"identity": identity, "product_hash": product_hash,
                                  "status": "pending"})
                continue
            try:
                outcome = prepare(product)
                completed = isinstance(outcome, Mapping) and outcome.get("status") == "completed"
                state = {"schema_version": SCHEMA_VERSION,
                         "status": "completed" if completed else "pending",
                         "identity": identity, "product_hash": product_hash,
                         "result": dict(outcome) if isinstance(outcome, Mapping) else {
                             "reason": "prepare returned no result"}}
            except Exception as exc:  # one product must not stop the rest
                state = {"schema_version": SCHEMA_VERSION, "status": "pending",
                         "identity": identity, "product_hash": product_hash,
                         "result": {"reason": f"{type(exc).__name__}: {exc}"}}
            state["content_hash"] = _hash(state)
            _atomic_write(path, state)
            summaries.append({"identity": identity, "product_hash": product_hash,
                              "status": state["status"]})
    return {"schema_version": "research-publication-worker-poll:0.1",
            "products": summaries,
            "completed": sum(row["status"] == "completed" for row in summaries),
            "pending": sum(row["status"] == "pending" for row in summaries),
            "unchanged": sum(row["status"] == "unchanged" for row in summaries)}


def run_periodic(
    connection: Any, mission: Mapping[str, Any], *, state_dir: Path,
    prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    stop_event: Any, interval_seconds: float, max_loops: int | None = None,
    library_reader: Callable[[Any, Mapping[str, Any], str], Mapping[str, Any]] = research_library,
) -> list[dict[str, Any]]:
    if interval_seconds <= 0 or max_loops is not None and max_loops < 1:
        raise ValueError("periodic worker bounds are invalid")
    results = []
    while not stop_event.is_set() and (max_loops is None or len(results) < max_loops):
        results.append(poll_once(connection, mission, state_dir=state_dir,
                                 prepare=prepare, library_reader=library_reader))
        if max_loops is not None and len(results) >= max_loops:
            break
        stop_event.wait(interval_seconds)
    return results


__all__ = ["SCHEMA_VERSION", "poll_once", "run_periodic"]
