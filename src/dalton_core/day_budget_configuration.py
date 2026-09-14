"""Synchronize an environment's paid-call policy and its runtime references."""
from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .store import content_hash
from .thesis_impact_budget import ThesisImpactBudgetStore


def _write(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".day-budget-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _wire(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def synchronize_day_budget_policy(state_dir: str | Path,
        service_config_path: str | Path | None = None, *, cap_usd: float,
        dry_run: bool = False) -> dict[str, Any]:
    """Keep usage/history, append a cap version, then CAS every local reference.

    A failed file update restores the preceding bindings. The unreferenced
    immutable policy remains available for an idempotent retry. No shared
    template or another environment's configuration is modified.
    """
    if (isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float))
            or not math.isfinite(cap_usd) or cap_usd <= 0):
        raise ValueError("daily cost must be positive and finite")
    micros = round(cap_usd * 1_000_000)
    if micros <= 0:
        raise ValueError("daily cost must be representable in USD micros")
    state = Path(state_dir).resolve()
    from .lane_registry import load_lanes
    from .model_configurations import model_config_names
    load_lanes()
    from . import claim_index_tagging  # noqa: F401 - registry registration

    originals: dict[Path, bytes] = {}
    models: dict[Path, dict[str, Any]] = {}
    for name in model_config_names():
        path = state / name
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
            raise ValueError("model budget configuration must be owner-managed")
        originals[path] = path.read_bytes()
        models[path] = json.loads(originals[path])
    if not models:
        raise ValueError("environment has no model budget configurations")
    databases = {Path(value["budget_db"]).resolve() for value in models.values()}
    if len(databases) != 1 or next(iter(databases)).parent != state:
        raise ValueError("model budget ledgers must be local to this environment")
    db = next(iter(databases))
    refs = {value["budget_policy_ref"] for value in models.values()}
    if service_config_path is None:
        candidate = state.parent.parent / "config" / "service.json"
        service_config_path = candidate if candidate.is_file() else None
    if service_config_path is not None and Path(service_config_path).is_symlink():
        raise ValueError("service configuration cannot be a symlink")
    service_path = None if service_config_path is None else Path(service_config_path).resolve()
    service = None
    if service_path is not None:
        if (service_path.is_symlink() or not service_path.is_file()
                or service_path.stat().st_uid != os.getuid() or service_path.stat().st_mode & 0o022):
            raise ValueError("service configuration must be owner-managed")
        originals[service_path] = service_path.read_bytes()
        service = json.loads(originals[service_path])
        if Path(service["core_db"]).resolve() != state / "core.sqlite":
            raise ValueError("service configuration belongs to another environment")
        for section in ("thesis_impact", "bounded_planner"):
            config = (service.get(section) or {}).get("config")
            if config and config.get("budget_db") and Path(config["budget_db"]).resolve() != db:
                raise ValueError("service budget ledger differs from model ledger")
    # Dry-run never creates an authority, a lock, or a revision receipt.
    if dry_run:
        with ThesisImpactBudgetStore(db, read_only=True) as budget:
            for ref in refs:
                budget.policy(ref)
        return {"status": "planned", "day_cap_micros": micros, "config_count": len(models)}

    lock_path = state / ".day-budget-configuration.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if any(path.read_bytes() != data for path, data in originals.items()):
            raise ValueError("budget configuration changed; reload before saving")
        with ThesisImpactBudgetStore(db) as budget:
            # Follow the existing chain, including an unreferenced version
            # left by an interrupted attempt; never fork or rewrite history.
            ends = set()
            for ref in refs:
                budget.policy(ref)
                while True:
                    row = budget.connection.execute(
                        "SELECT policy_version_id FROM thesis_impact_budget_policies WHERE prior_version_id=?",
                        (ref,)).fetchone()
                    if row is None:
                        break
                    ref = row[0]
                ends.add(ref)
            if len(ends) != 1:
                raise ValueError("model budget policies do not share one authority chain")
            prior = ends.pop()
            current = budget.policy(prior)
            if current["day_cap_micros"] == micros:
                selected = prior
            else:
                selected = "thesis-impact-day-budget-policy:configured:" + content_hash(
                    {"prior": prior, "day_cap_micros": micros})[:32]
                budget.register_policy(policy_version_id=selected,
                    day_cap_micros=micros, prior_version_id=prior)
        changes = {}
        for path, value in models.items():
            changes[path] = _wire({**value, "budget_policy_ref": selected})
        if service is not None:
            for section in ("thesis_impact", "bounded_planner"):
                config = (service.get(section) or {}).get("config")
                if not config:
                    continue
                for key in ("budget_policy_ref", "budget_policy_version_id"):
                    if key in config:
                        config[key] = selected
            changes[service_path] = _wire(service)
        written = []
        try:
            for path, data in changes.items():
                if path.read_bytes() != originals[path]:
                    raise ValueError("budget configuration changed during synchronization")
                if data != originals[path]:
                    _write(path, data)
                    written.append(path)
            body = {"policy_version_id": selected, "day_cap_micros": micros,
                "config_hashes": {path.name: content_hash(json.loads(data)) for path, data in changes.items()}}
            revision = content_hash(body)
            directory = state / "day-budget-revisions"
            directory.mkdir(mode=0o700, exist_ok=True)
            _write(directory / (revision + ".json"), _wire({**body, "content_hash": revision}))
        except BaseException:
            for path in reversed(written):
                if path.read_bytes() == changes[path]:
                    _write(path, originals[path])
            raise
        return {"status": "synchronized", "policy_version_id": selected,
            "day_cap_micros": micros, "config_count": len(models),
            "revision_ref": "day-budget-revision:" + revision}
