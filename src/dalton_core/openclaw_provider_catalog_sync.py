"""Safely project OpenClaw's provider inventory into its Dalton broker entry.

The planner is deliberately narrow: it changes only the broker's
``llm.allowedModels`` and ``config.profiles`` values.  The file writer adds a
same-host advisory lock and an exact-byte compare-and-swap around that pure
operation.  Callers receive only a public receipt; the planned full config is
available only from the explicitly internal-looking plan object.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BROKER_PLUGIN_ID = "dalton-openclaw-model-broker"
AUTO_PROFILE_PREFIX = "profile:auto-"
AUTO_PROFILE_HASH_LENGTH = 12
AUTO_PROFILE_TIMEOUT_MS = 600_000
BROKER_MAX_TOKENS = 1_000_000

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_SLUG_RUN = re.compile(r"[^a-z0-9]+")


class CatalogSyncError(RuntimeError):
    """The catalog cannot be safely planned or installed."""


class CatalogSyncRaceError(CatalogSyncError):
    """The config changed after it was read; a later tick must retry."""


@dataclass(frozen=True)
class CatalogSyncPlan:
    """An internal full-config proposal plus its credential-free receipt."""

    config: dict[str, Any]
    receipt: dict[str, Any]


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    device: int
    inode: int
    size: int
    mtime_ns: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


def _need(condition: Any, reason: str) -> None:
    if not condition:
        raise CatalogSyncError(reason)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogSyncError(f"{name} must be an object")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise CatalogSyncError(f"{name} must be an array")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CatalogSyncError(f"{name} must be a positive integer")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def _output_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _reject_duplicate_keys(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CatalogSyncError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_config(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except CatalogSyncError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogSyncError("OpenClaw config is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CatalogSyncError("OpenClaw config must be an object")
    return value


def _slug(value: str) -> str:
    slug = _SLUG_RUN.sub("-", value.lower()).strip("-")
    return (slug or "unnamed")[:48].rstrip("-")


def auto_profile_id(model_ref: str) -> str:
    """Return the one reserved, stable profile identity for a provider route."""

    provider, separator, model = model_ref.partition("/")
    _need(bool(separator and provider and model), "model ref must be provider/model")
    digest = hashlib.sha256(model_ref.encode("utf-8")).hexdigest()
    return (
        f"{AUTO_PROFILE_PREFIX}{_slug(provider)}-{_slug(model)}-"
        f"{digest[:AUTO_PROFILE_HASH_LENGTH]}"
    )


def _is_owned_auto_profile(profile: Mapping[str, Any]) -> bool:
    model_ref = profile.get("model")
    profile_id = profile.get("id")
    return (
        isinstance(model_ref, str)
        and isinstance(profile_id, str)
        and profile_id == auto_profile_id(model_ref)
    )


def _provider_inventory(config: Mapping[str, Any]) -> dict[str, int]:
    models = _object(config.get("models"), "models")
    providers = _object(models.get("providers"), "models.providers")
    inventory: dict[str, int] = {}
    for provider, raw_provider in providers.items():
        if not isinstance(provider, str) or not _TOKEN.fullmatch(provider):
            raise CatalogSyncError("provider id must be a canonical token")
        provider_config = _object(raw_provider, f"models.providers.{provider}")
        provider_models = _array(
            provider_config.get("models"), f"models.providers.{provider}.models"
        )
        for index, raw_model in enumerate(provider_models):
            model = _object(raw_model, f"models.providers.{provider}.models[{index}]")
            model_id = model.get("id")
            if not isinstance(model_id, str) or not _TOKEN.fullmatch(model_id):
                raise CatalogSyncError(f"provider {provider} has an invalid model id")
            model_ref = f"{provider}/{model_id}"
            if model_ref in inventory:
                raise CatalogSyncError(f"duplicate provider model: {model_ref}")
            max_tokens = _positive_int(
                model.get("maxTokens"), f"{model_ref}.maxTokens"
            )
            if max_tokens > BROKER_MAX_TOKENS:
                raise CatalogSyncError(
                    f"{model_ref}.maxTokens exceeds broker maximum "
                    f"{BROKER_MAX_TOKENS}"
                )
            inventory[model_ref] = max_tokens
    return inventory


def _broker_entry(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Any]]:
    plugins = _object(config.get("plugins"), "plugins")
    entries = _object(plugins.get("entries"), "plugins.entries")
    plugin = _object(
        entries.get(BROKER_PLUGIN_ID),
        f"plugins.entries.{BROKER_PLUGIN_ID}",
    )
    llm = _object(plugin.get("llm"), f"plugins.entries.{BROKER_PLUGIN_ID}.llm")
    _array(llm.get("allowedModels"), "broker llm.allowedModels")
    plugin_config = _object(
        plugin.get("config"), f"plugins.entries.{BROKER_PLUGIN_ID}.config"
    )
    profiles = _array(plugin_config.get("profiles"), "broker config.profiles")
    return plugin, profiles


def plan_openclaw_provider_catalog_sync(
    config: Mapping[str, Any],
) -> CatalogSyncPlan:
    """Return a pure proposal that reconciles only the broker-managed fields."""

    source = _object(config, "OpenClaw config")
    inventory = _provider_inventory(source)
    source_plugin, source_profiles = _broker_entry(source)
    prior_allowed = list(source_plugin["llm"]["allowedModels"])
    if any(not isinstance(item, str) for item in prior_allowed):
        raise CatalogSyncError("broker llm.allowedModels entries must be strings")
    prior_refs = set(prior_allowed)
    desired = copy.deepcopy(dict(source))
    desired_plugin, desired_profiles_raw = _broker_entry(desired)

    seen_profile_ids: set[str] = set()
    occupied_profile_ids: dict[str, str] = {}
    existing_refs: set[str] = set()
    kept_profiles: list[dict[str, Any]] = []
    removed_profile_ids: list[str] = []
    updated_profile_ids: list[str] = []
    preserved_profile_ids: list[str] = []

    for index, raw_profile in enumerate(desired_profiles_raw):
        profile = _object(raw_profile, f"broker config.profiles[{index}]")
        profile_id = profile.get("id")
        model_ref = profile.get("model")
        if not isinstance(profile_id, str) or not profile_id:
            raise CatalogSyncError(f"broker config.profiles[{index}].id is invalid")
        if profile_id in seen_profile_ids:
            raise CatalogSyncError(f"duplicate broker profile id: {profile_id}")
        seen_profile_ids.add(profile_id)
        if not isinstance(model_ref, str) or "/" not in model_ref:
            raise CatalogSyncError(f"broker profile {profile_id} has an invalid model")
        occupied_profile_ids[profile_id] = model_ref
        if model_ref not in inventory:
            removed_profile_ids.append(profile_id)
            continue

        kept = copy.deepcopy(dict(profile))
        if _is_owned_auto_profile(kept):
            provider_max_tokens = inventory[model_ref]
            if kept.get("maxTokens") != provider_max_tokens:
                kept["maxTokens"] = provider_max_tokens
                updated_profile_ids.append(profile_id)
            else:
                preserved_profile_ids.append(profile_id)
        else:
            preserved_profile_ids.append(profile_id)
        kept_profiles.append(kept)
        existing_refs.add(model_ref)

    added_profile_ids: list[str] = []
    missing_profile_refs = sorted(set(inventory) - existing_refs)
    for model_ref in missing_profile_refs:
        profile_id = auto_profile_id(model_ref)
        collision_ref = occupied_profile_ids.get(profile_id)
        if collision_ref is not None and collision_ref != model_ref:
            raise CatalogSyncError(
                f"reserved auto profile id collision: {profile_id} is occupied by "
                f"{collision_ref}"
            )
        kept_profiles.append({
            "id": profile_id,
            "model": model_ref,
            "maxTokens": inventory[model_ref],
            "timeoutMs": AUTO_PROFILE_TIMEOUT_MS,
        })
        added_profile_ids.append(profile_id)

    desired_plugin["llm"]["allowedModels"] = sorted(inventory)
    desired_plugin["config"]["profiles"] = kept_profiles

    added_model_refs = sorted(set(inventory) - prior_refs)
    removed_model_refs = sorted(prior_refs - set(inventory))
    changed = desired != source
    receipt = {
        "schema_version": "openclaw-provider-catalog-sync-0.1",
        "changed": changed,
        "provider_model_count": len(inventory),
        "allowed_model_count": len(inventory),
        "profile_count": len(kept_profiles),
        "added_model_refs": added_model_refs,
        "removed_model_refs": removed_model_refs,
        "added_profile_ids": added_profile_ids,
        "removed_profile_ids": removed_profile_ids,
        "updated_profile_ids": updated_profile_ids,
        "preserved_profile_ids": preserved_profile_ids,
        "before_config_sha256": _sha256(_canonical_bytes(source)),
        "after_config_sha256": _sha256(_canonical_bytes(desired)),
    }
    return CatalogSyncPlan(config=desired, receipt=receipt)


def _read_snapshot(path: Path) -> _Snapshot:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatalogSyncError(f"cannot open OpenClaw config: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        _need(stat.S_ISREG(before.st_mode), "OpenClaw config must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    stable_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    )
    stable_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    )
    if stable_before != stable_after or len(data) != before.st_size:
        raise CatalogSyncRaceError("OpenClaw config changed while it was read")
    try:
        named = path.lstat()
    except OSError as exc:
        raise CatalogSyncRaceError("OpenClaw config changed after it was read") from exc
    if stat.S_ISLNK(named.st_mode) or (
        named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns
    ) != stable_after:
        raise CatalogSyncRaceError("OpenClaw config identity changed after it was read")
    return _Snapshot(
        data=data,
        device=before.st_dev,
        inode=before.st_ino,
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
    )


def _same_snapshot(path: Path, expected: _Snapshot) -> bool:
    try:
        actual = _read_snapshot(path)
    except CatalogSyncError:
        return False
    return actual == expected


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _occupied(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _next_backup_path(config_path: Path, requested: str | Path | None) -> Path:
    if requested is not None:
        selected = Path(requested).expanduser()
        _need(selected.parent == config_path.parent, "backup must share the config directory")
        _need(not _occupied(selected), "backup path already exists")
        return selected
    base = config_path.with_name(config_path.name + ".dalton-catalog-sync.bak")
    candidate = base
    suffix = 0
    while _occupied(candidate):
        suffix += 1
        candidate = base.with_name(f"{base.name}.{suffix}")
    return candidate


def _write_exclusive_bytes(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        published = True
        _fsync_directory(path.parent)
    except Exception:
        if published:
            try:
                if path.stat().st_ino == temporary.stat().st_ino:
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _install_without_overwrite(
    path: Path,
    snapshot: _Snapshot,
    after: bytes,
    backup_path: Path,
) -> None:
    """Publish after exact CAS without overwriting a non-cooperating writer."""

    candidate_fd, candidate_name = tempfile.mkstemp(
        prefix=f".{path.name}.candidate.", dir=path.parent
    )
    candidate = Path(candidate_name)
    held_fd, held_name = tempfile.mkstemp(
        prefix=f".{path.name}.held.", dir=path.parent
    )
    os.close(held_fd)
    held = Path(held_name)
    held.unlink()
    moved = False
    published = False
    try:
        os.fchmod(candidate_fd, 0o600)
        with os.fdopen(candidate_fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())

        if not _same_snapshot(path, snapshot):
            raise CatalogSyncRaceError("OpenClaw config changed before catalog publish")
        # Finish the durable backup while the live name is still continuously
        # available.  A later CAS loss may leave this exact harmless backup,
        # but it must never cause old bytes to be restored over a new owner.
        _write_exclusive_bytes(backup_path, snapshot.data)
        os.rename(path, held)
        moved = True
        held_stat = held.lstat()
        if (
            stat.S_ISLNK(held_stat.st_mode)
            or (held_stat.st_dev, held_stat.st_ino) != snapshot.identity
            or held_stat.st_size != snapshot.size
            or held_stat.st_mtime_ns != snapshot.mtime_ns
            or held.read_bytes() != snapshot.data
        ):
            raise CatalogSyncRaceError(
                f"OpenClaw config changed during catalog CAS; conflicting bytes "
                f"preserved at {held}"
            )
        try:
            os.link(candidate, path)
        except FileExistsError as exc:
            raise CatalogSyncRaceError(
                f"OpenClaw config was recreated during catalog CAS; original "
                f"preserved at {held}"
            ) from exc
        published = True
        _fsync_directory(path.parent)
        candidate.unlink()
        held.unlink()
        moved = False
        _fsync_directory(path.parent)
    except Exception:
        if moved and _occupied(held) and not _occupied(path):
            try:
                os.link(held, path)
                _fsync_directory(path.parent)
                held.unlink()
                moved = False
            except FileExistsError:
                pass
        raise
    finally:
        candidate.unlink(missing_ok=True)
        if published and _occupied(held):
            held.unlink(missing_ok=True)


def apply_openclaw_provider_catalog_sync(
    path: str | Path,
    *,
    backup_path: str | Path | None = None,
    before_replace: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Lock, plan, exact-CAS install, and return a credential-free receipt."""

    # Make the path absolute without resolving its final component: a config
    # symlink must be rejected by O_NOFOLLOW rather than silently followed.
    config_path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    _need(config_path.parent.is_dir(), "OpenClaw config parent is unavailable")
    _need(not config_path.parent.is_symlink(), "OpenClaw config parent is unsafe")
    try:
        initial_path_stat = config_path.lstat()
    except OSError as exc:
        raise CatalogSyncError(f"cannot inspect OpenClaw config: {exc}") from exc
    _need(
        stat.S_ISREG(initial_path_stat.st_mode),
        "OpenClaw config must be a regular file, not a symlink",
    )
    lock_path = config_path.with_name(config_path.name + ".dalton-catalog-sync.lock")
    lock_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise CatalogSyncError(f"cannot open catalog sync lock: {exc}") from exc
    lock_stat = os.fstat(lock_descriptor)
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(lock_descriptor)
        raise CatalogSyncError("catalog sync lock must be a regular file")
    os.fchmod(lock_descriptor, 0o600)
    with os.fdopen(lock_descriptor, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            snapshot = _read_snapshot(config_path)
            parsed = _parse_config(snapshot.data)
            plan = plan_openclaw_provider_catalog_sync(parsed)
            receipt = copy.deepcopy(plan.receipt)
            receipt["before_sha256"] = _sha256(snapshot.data)
            if not receipt["changed"]:
                receipt["after_sha256"] = receipt["before_sha256"]
                receipt["backup_created"] = False
                return receipt

            after = _output_bytes(plan.config)
            selected_backup = _next_backup_path(config_path, backup_path)
            if before_replace is not None:
                before_replace(config_path)
            if not _same_snapshot(config_path, snapshot):
                raise CatalogSyncRaceError(
                    "OpenClaw config changed after planning; retry on the next tick"
                )
            _install_without_overwrite(
                config_path, snapshot, after, selected_backup
            )
            installed = _read_snapshot(config_path)
            if installed.data != after:
                raise CatalogSyncRaceError(
                    "installed OpenClaw config changed before verification"
                )
            receipt["after_sha256"] = _sha256(after)
            receipt["backup_created"] = True
            receipt["backup_sha256"] = _sha256(snapshot.data)
            receipt["backup_file"] = selected_backup.name
            return receipt
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# Short aliases for callers that already establish the OpenClaw context.
plan_catalog_sync = plan_openclaw_provider_catalog_sync
apply_catalog_sync = apply_openclaw_provider_catalog_sync
reconcile_openclaw_broker_catalog = apply_openclaw_provider_catalog_sync


__all__ = [
    "AUTO_PROFILE_TIMEOUT_MS",
    "BROKER_MAX_TOKENS",
    "BROKER_PLUGIN_ID",
    "CatalogSyncError",
    "CatalogSyncPlan",
    "CatalogSyncRaceError",
    "apply_catalog_sync",
    "apply_openclaw_provider_catalog_sync",
    "auto_profile_id",
    "plan_catalog_sync",
    "plan_openclaw_provider_catalog_sync",
    "reconcile_openclaw_broker_catalog",
]
