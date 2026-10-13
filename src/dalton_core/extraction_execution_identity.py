"""Extraction execution identity and closed replay of older frozen policies.

Shared policy revision metadata and other task limits are not extraction input.
Historical bytes are never guessed: only exact Scheduler Work authority matches
are reusable, after the original policy hash chain proves its frozen config.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .call_budget import freeze_shared_budget_policy
from .shared_call_budget_policy import (LIMITS_SCHEMA_VERSIONS,
    load_shared_call_budget_policy)
from .store import content_hash

CONTRACT_REF = "extraction-execution-config:0.1"
MAX_PRIOR_POLICIES = 64


def execution_config_binding(config: Mapping[str, Any]) -> dict[str, str]:
    frozen = freeze_shared_budget_policy(config)
    policy = frozen.get("shared_call_budget_policy_snapshot")
    if policy is None or policy["schema_version"] not in LIMITS_SCHEMA_VERSIONS:
        return {"config_hash": content_hash(config)}
    body = dict(frozen)
    # Each task's Work binds its own resolved call budget separately. Including
    # another task's limits here would change an already-paid source window.
    body.pop("shared_call_budget_policy_snapshot", None)
    return {"config_hash": content_hash(body), "config_hash_contract_ref": CONTRACT_REF}


def _old_configs(config: Mapping[str, Any]):
    """Current raw bytes, then at most 64 exact prior-hash policy receipts.

    Official manager receipts are beside its host configuration. Allow the
    policy's own directory or its parent (the installed connections layout).
    Only exact hash filenames are read; no directory or Scheduler scans.
    """
    raw = {k: v for k, v in config.items() if k != "shared_call_budget_policy_snapshot"}
    yield raw
    frozen = freeze_shared_budget_policy(config)
    policy = frozen.get("shared_call_budget_policy_snapshot")
    if policy is None:
        return
    path = Path(config["shared_call_budget_policy_path"])
    directories = (path.parent / "shared-call-budget-revisions",
                   path.parent.parent / "shared-call-budget-revisions")
    for _ in range(MAX_PRIOR_POLICIES):
        yield {**raw, "shared_call_budget_policy_snapshot": policy}
        prior_hash = policy["prior_hash"]
        if prior_hash is None:
            return
        candidates = [d / (prior_hash + ".json") for d in directories]
        receipt = next((p for p in candidates if p.is_file()), None)
        if receipt is None:
            # Missing history cannot authorize a historical replay. The
            # caller's old Work still fails its existing exact drift gate.
            return
        prior = load_shared_call_budget_policy(receipt)
        if prior["content_hash"] != prior_hash or prior["revision"] != policy["revision"] - 1:
            raise ValueError("extraction shared policy history differs from its exact chain")
        policy = prior


def reusable_legacy_context(current: Mapping[str, Any], config: Mapping[str, Any],
                            scheduler, build_work) -> dict[str, Any] | None:
    binding = current.get("model_binding", {})
    if binding.get("config_hash_contract_ref") != CONTRACT_REF or scheduler is None:
        return None
    # Prefer an already-created current Work over any historical variant.
    active_work = build_work(current, model_config=config)
    authority = scheduler.work_order_authority(active_work.id)
    if authority is not None:
        if authority["work_order_hash"] != content_hash(active_work.to_dict()) or authority["work_order"] != active_work.to_dict():
            raise ValueError("current extraction Work authority differs")
        return None
    seen = set()
    current_binding = execution_config_binding(config)
    for old_config in _old_configs(config):
        old_hash = content_hash(old_config)
        if old_hash in seen:
            continue
        seen.add(old_hash)
        if execution_config_binding(old_config) != current_binding:
            continue
        base = {k: v for k, v in current.items() if k not in {"id", "content_hash"}}
        base["model_binding"] = {k: v for k, v in binding.items() if k != "config_hash_contract_ref"}
        base["model_binding"]["config_hash"] = old_hash
        historical = {"id": "document-extraction-context:" + content_hash(base)[:32], **base}
        historical["content_hash"] = content_hash(historical)
        work = build_work(historical, model_config=config)
        authority = scheduler.work_order_authority(work.id)
        if authority is None:
            continue
        if authority["work_order_hash"] != content_hash(work.to_dict()) or authority["work_order"] != work.to_dict():
            raise ValueError("historical extraction Work authority differs")
        return historical
    return None


def current_context(context, config):
    """Normalize only the execution config, retaining all strong source proof."""
    base = {k: v for k, v in context.items() if k not in {"id", "content_hash"}}
    binding = dict(base["model_binding"])
    binding.pop("config_hash_contract_ref", None)
    binding.update(execution_config_binding(config))
    base["model_binding"] = binding
    current = {"id": "document-extraction-context:" + content_hash(base)[:32], **base}
    current["content_hash"] = content_hash(current)
    return current
