"""P14-M2: let one more of the gateway's models through, and nothing else.

The owner sees a model in the cockpit's 「可用但未放行」 column and presses
「放行」.  What that has to mean, exactly:

* two keys move, both inside
  ``plugins.entries.dalton-openclaw-model-broker`` -- one string appended to
  ``llm.allowedModels``, one object appended to ``config.profiles``.  Nothing
  else in the OpenClaw configuration is read for its value, written, or
  reordered;
* a timestamped backup exists before the write does;
* the file that lands parses back to exactly the object we meant to write, and
  differs from the file that was there only inside the broker's own subtree --
  both checked, not assumed, because this is a host configuration file that
  something other than Dalton owns;
* the caller is told to reload the gateway, because the broker reads its plugin
  configuration at gateway start and until it does the new model is a name in a
  file.

Deliberately *not* here: any change to ``models.providers``.  Dalton does not
configure the host's providers -- it reads which ones exist.  A model that is
not already in ``models.providers`` cannot be allowed by this, and the refusal
says so rather than inventing a provider entry.

No credential, key, header or provider setting is read or written.  The patch
is built from the provider's own public model entry: context window, output
limit, and the model reference.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .openclaw_catalog_reconcile import OpenClawCatalogError, load_openclaw_config
from .openclaw_model_discovery import (
    BROKER_PLUGIN_ID,
    allowed_model_refs,
    broker_profiles,
    provider_models,
)

# What the owner has to do afterwards.  Returned rather than printed, because
# the caller is a governance operation whose answer ends up on a web page.
RELOAD_INSTRUCTION = (
    "重载 openclaw 网关后这个模型才真的可用（broker 在网关启动时才读它的插件配置）："
    "openclaw gateway restart"
)
# The two profile settings this does not read off the provider entry, because
# the provider entry does not carry them.  Both match what every existing
# broker profile in the live configuration uses.
DEFAULT_TIMEOUT_MS = 600_000
BACKUP_SUFFIX = "bak-dalton-allow"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class AllowPatchError(OpenClawCatalogError):
    """The broker subtree cannot be patched as asked."""


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def _curated_profile_id(model_ref: str) -> str | None:
    """The id Dalton's static catalog already uses for this model, if any."""

    from datetime import timedelta

    from .model_deployment import openclaw_broker_profiles

    moment = datetime.now(timezone.utc)
    for profile in openclaw_broker_profiles(
        checked_at=moment, availability_ttl=timedelta(days=7)
    ):
        if f"{profile['provider']}/{profile['model']}" == model_ref:
            return str(profile["id"])
    return None


def profile_id_for(config: Mapping[str, Any], model_ref: str) -> str:
    """A deterministic broker profile id for one model reference.

    The live configuration names most profiles after the model alone
    (``openai/gpt-6-astra`` -> ``profile:gpt-6-astra``) and falls back to
    provider-and-model when the bare name would be ambiguous
    (``qwen/deepseek-v4-flash-0731``).  That rule is reproduced rather than
    replaced: a new id that did not look like its neighbours would be the one
    thing a reader of the file stumbles on.
    """

    provider, _, model = model_ref.partition("/")
    if not provider or not model:
        raise AllowPatchError("a model reference is provider/model")
    # Dalton's own curated catalog wins when it already names this model.
    # Inventing a second id for a model Dalton has a profile for would make the
    # catalog sync register the new id and *retire* the curated one, which is
    # the opposite of letting the model through.
    curated = _curated_profile_id(model_ref)
    if curated is not None:
        return curated
    providers = provider_models(config)
    same_model = [
        ref for ref in providers if ref.partition("/")[2] == model
    ]
    existing = set(broker_profiles(config))
    bare = f"profile:{_slug(model)}"
    if len(same_model) <= 1 and bare not in existing:
        return bare
    qualified = f"profile:{_slug(provider)}-{_slug(model)}"
    if qualified in existing:
        raise AllowPatchError(
            f"{model_ref} already has a broker profile ({qualified})"
        )
    return qualified


def build_allow_patch(config: Mapping[str, Any], model_ref: str) -> dict[str, Any]:
    """What would change, without changing anything.

    Returns ``status: "already_allowed"`` when the broker already passes the
    model and already has a profile for it -- pressing 「放行」 twice is a thing
    an owner will do, and the second press must not append a duplicate.
    """

    if not isinstance(model_ref, str) or "/" not in model_ref:
        raise AllowPatchError("a model reference is provider/model")
    providers = provider_models(config)
    entry = providers.get(model_ref)
    if entry is None:
        raise AllowPatchError(
            f"{model_ref} is not in models.providers; this only lets through a "
            "model the gateway already has, it does not configure a new one"
        )
    allowed = allowed_model_refs(config)
    profiles = broker_profiles(config)
    served = {
        profile_id: item
        for profile_id, item in profiles.items()
        if item["model_ref"] == model_ref
    }
    add_allowed = model_ref not in allowed
    if served:
        profile_id = sorted(served)[0]
        profile: dict[str, Any] | None = None
    else:
        profile_id = profile_id_for(config, model_ref)
        profile = {
            "id": profile_id,
            "model": model_ref,
            "maxTokens": int(entry["max_output_tokens"]),
            "timeoutMs": DEFAULT_TIMEOUT_MS,
        }
    return {
        "schema_version": "0.1",
        "status": "already_allowed" if not add_allowed and profile is None else "planned",
        "model_ref": model_ref,
        "profile_id": profile_id,
        "provider": entry["provider"],
        "unpriced": bool(entry["unpriced"]),
        "allowed_models_add": [model_ref] if add_allowed else [],
        "profiles_add": [profile] if profile is not None else [],
        "plugin_id": BROKER_PLUGIN_ID,
        "subtree": f"plugins.entries.{BROKER_PLUGIN_ID}",
    }


def _apply(config: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """The patched object, built by copying and appending, never by editing."""

    patched = copy.deepcopy(dict(config))
    plugins = patched.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    entry = entries.get(BROKER_PLUGIN_ID)
    if not isinstance(entry, Mapping):
        raise AllowPatchError(
            "this configuration has no dalton-openclaw-model-broker plugin entry"
        )
    entry = dict(entry)
    llm = dict(entry.get("llm") or {})
    allowed = list(llm.get("allowedModels") or [])
    for ref in patch["allowed_models_add"]:
        if ref not in allowed:
            allowed.append(ref)
    llm["allowedModels"] = allowed
    entry["llm"] = llm
    plugin_config = dict(entry.get("config") or {})
    profiles = list(plugin_config.get("profiles") or [])
    known = {
        item.get("id") for item in profiles if isinstance(item, Mapping)
    }
    for profile in patch["profiles_add"]:
        if profile["id"] in known:
            raise AllowPatchError(f"{profile['id']} is already a broker profile")
        profiles.append(dict(profile))
    plugin_config["profiles"] = profiles
    entry["config"] = plugin_config
    entries[BROKER_PLUGIN_ID] = entry
    return patched


def changed_paths(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Every top-level path whose value differs, to two levels of plugins.

    Two levels is exactly what is needed to say "only the broker's own entry
    moved": one level would say "plugins changed", which is true of a change to
    any plugin at all.
    """

    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key == "plugins":
            continue
        if json.dumps(before.get(key), sort_keys=True) != json.dumps(
            after.get(key), sort_keys=True
        ):
            changed.append(key)
    before_plugins = before.get("plugins") or {}
    after_plugins = after.get("plugins") or {}
    for key in sorted(set(before_plugins) | set(after_plugins)):
        if key == "entries":
            continue
        if json.dumps(before_plugins.get(key), sort_keys=True) != json.dumps(
            after_plugins.get(key), sort_keys=True
        ):
            changed.append(f"plugins.{key}")
    before_entries = (before_plugins or {}).get("entries") or {}
    after_entries = (after_plugins or {}).get("entries") or {}
    for key in sorted(set(before_entries) | set(after_entries)):
        if json.dumps(before_entries.get(key), sort_keys=True) != json.dumps(
            after_entries.get(key), sort_keys=True
        ):
            changed.append(f"plugins.entries.{key}")
    return changed


def _render(config: Mapping[str, Any]) -> str:
    """The file's own formatting: two-space indent, unescaped, one newline."""

    return json.dumps(dict(config), ensure_ascii=False, indent=2) + "\n"


def apply_allow_patch(
    path: str | Path,
    model_ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Back up, patch the broker subtree, verify, and say what to do next.

    The order matters and is the whole of the safety here: the backup is on
    disk before the write happens, the write goes to a temporary file in the
    same directory and is renamed over the original, and the file that lands is
    read back and compared -- both to what we meant to write and to what was
    there before -- before this returns.  A verification failure restores the
    backup rather than leaving a host configuration in a state nobody chose.
    """

    target = Path(path).expanduser()
    if not target.is_file():
        raise AllowPatchError(f"there is no OpenClaw configuration at {target}")
    original_text = target.read_text(encoding="utf-8")
    before = load_openclaw_config(target)
    patch = build_allow_patch(before, model_ref)
    if patch["status"] == "already_allowed":
        return {
            **patch,
            "config_path": str(target),
            "backup_path": None,
            "changed_paths": [],
            "reload_instruction": RELOAD_INSTRUCTION,
        }
    after = _apply(before, patch)
    changed = changed_paths(before, after)
    expected = [f"plugins.entries.{BROKER_PLUGIN_ID}"]
    if changed != expected:
        raise AllowPatchError(
            "refusing to write: the patch would change "
            + (", ".join(changed) or "nothing")
            + " rather than only the broker plugin subtree"
        )
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%S")
    backup = target.with_name(f"{target.name}.{BACKUP_SUFFIX}-{stamp}")
    if backup.exists():
        raise AllowPatchError(f"a backup named {backup.name} already exists")
    backup.write_text(original_text, encoding="utf-8")
    os.chmod(backup, 0o600)
    rendered = _render(after)
    tmp = target.with_name(f".{target.name}.dalton-allow.tmp")
    try:
        tmp.write_text(rendered, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        # Read back rather than trust the write: this is somebody else's file.
        landed = load_openclaw_config(target)
        if json.dumps(landed, sort_keys=True) != json.dumps(after, sort_keys=True):
            raise AllowPatchError("the written configuration did not read back the same")
        if changed_paths(before, landed) != expected:
            raise AllowPatchError(
                "the written configuration changed something outside the broker subtree"
            )
    except BaseException:
        # Put the file back exactly as it was. The backup stays: a restore that
        # deletes its own evidence is not a restore.
        target.write_text(original_text, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        raise
    return {
        **patch,
        "status": "applied",
        "config_path": str(target),
        "backup_path": str(backup),
        "changed_paths": changed,
        "reload_instruction": RELOAD_INSTRUCTION,
    }


__all__ = [
    "BACKUP_SUFFIX",
    "DEFAULT_TIMEOUT_MS",
    "RELOAD_INSTRUCTION",
    "AllowPatchError",
    "apply_allow_patch",
    "build_allow_patch",
    "changed_paths",
    "profile_id_for",
]
