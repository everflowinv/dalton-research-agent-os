"""Pure resolution of per-call model budgets from an installed configuration."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MEMO_MAINTENANCE_ROUNDS_FIELD = "max_content_maintenance_rounds_per_group"

FIELDS = ("max_input_tokens", "max_output_tokens", "max_cost_usd", "timeout_seconds")
RUN_FIELDS = ("max_cost_usd", "max_units", "max_events",
              "max_events_per_company", "max_calls", "max_rounds",
              "max_cost_units", "max_seconds", "max_admissions_per_tick",
              MEMO_MAINTENANCE_ROUNDS_FIELD)
_PURPOSE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class CallBudgetError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Long-context purposes: a code floor every environment's stored config obeys
# ---------------------------------------------------------------------------
#
# 2026-10-09 (batch C2): the STRL-shaped products read the whole material pack
# -- 150-250K characters, which the WorkOrder counts as UTF-8 bytes -- and
# write one document of up to 16K tokens.  Every environment's stored model
# config said otherwise: ``purpose_call_budgets`` of 1 USD per purpose, and the
# host's shared call-cost policy (default 1 USD) overriding ``max_cost_usd``
# for every purpose it does not name.  At 1 USD the router refuses every
# long-context link with ``work_order_cost_budget_exceeded`` (claude-opus-5 at
# 600K input is ~2.9 USD by the router's plain estimate) and the call either
# fails or lands on whichever flash model is cheap enough.
#
# The owner accepted the cost of these products (batch D, decision 3), so the
# bounds are a property of the product, not of the environment: the code
# floor below is applied *after* the packaged defaults, the installed
# ``call_budget`` / ``purpose_call_budgets`` and the shared cost policy, and
# only ever raises a field.  A stored value above the floor still wins; a
# lower one cannot make the product unroutable.  The floor is a ceiling on
# what one call may cost -- the reservation the day ledger takes is still the
# priced chain ceiling for the actual prompt, and the mission's day budget
# and pools still bound real spend.
LONG_CONTEXT_INPUT_TOKENS = 600_000
LONG_CONTEXT_OUTPUT_TOKENS = 16_000
LONG_CONTEXT_TIMEOUT_SECONDS = 900
# The rate card the cost floor is sized for: the dearest long-context link in
# the code-default chains (gpt-6-astra / claude-fable-5-1, 10 / 50 USD per
# million tokens), served through a CLI gateway, which is the most expensive
# way to bill it (all input at the cache-write multiplier plus the gateway's
# hidden system prompt -- ``cockpit_model.profile_call_ceiling_usd``).  The
# adapter refuses a result whose metered cost exceeds the WorkOrder cap *after*
# paying for it, so the cap must cover the worst link, not the typical one.
LONG_CONTEXT_REFERENCE_RATES_USD = {"input_per_million_usd": 10.0, "output_per_million_usd": 50.0}


def long_context_call_cost_usd(*, input_tokens: int = LONG_CONTEXT_INPUT_TOKENS,
                               output_tokens: int = LONG_CONTEXT_OUTPUT_TOKENS,
                               rates: Mapping[str, Any] = LONG_CONTEXT_REFERENCE_RATES_USD) -> float:
    """Whole-dollar ceiling of one gateway-billed call at the reference rates."""

    from decimal import Decimal

    from .model_profile_bounds import (CLI_GATEWAY_CACHE_WRITE_MULTIPLIER,
                                       CLI_GATEWAY_SYSTEM_PROMPT_TOKENS)

    cost = (Decimal(str(rates["input_per_million_usd"])) * CLI_GATEWAY_CACHE_WRITE_MULTIPLIER
            * (int(input_tokens) + CLI_GATEWAY_SYSTEM_PROMPT_TOKENS)
            + Decimal(str(rates["output_per_million_usd"])) * int(output_tokens)) / Decimal(1_000_000)
    return float(math.ceil(cost))


LONG_CONTEXT_CALL_FLOOR: dict[str, Any] = {
    "max_input_tokens": LONG_CONTEXT_INPUT_TOKENS,
    "max_output_tokens": LONG_CONTEXT_OUTPUT_TOKENS,
    "max_cost_usd": long_context_call_cost_usd(),
    "timeout_seconds": LONG_CONTEXT_TIMEOUT_SECONDS,
}
#: Batch ar item 14 extends C2's product floor and routing preference to
#: memo, dossier and debate map drafting and verification. A bounded evidence
#: projection does not remove the product's long-context routing contract.
LONG_CONTEXT_PURPOSES: frozenset[str] = frozenset({
    "initial_screen", "initial_screen_verifier",
    "investment_memo", "investment_memo_verifier",
    "dossier", "dossier_verifier", "debate_map", "debate_map_verifier",
})
#: The same set tries the links that can carry the pack first
#: (``model_router.resolve_chain``).
LONG_CONTEXT_CHAIN_PURPOSES: frozenset[str] = LONG_CONTEXT_PURPOSES

# aw: explicit request controls, independent of gateway/interactive defaults.
THINKING_LEVELS: frozenset[str] = frozenset({"off", "minimal", "low", "medium", "high"})
MEDIUM_THINKING_PURPOSES: frozenset[str] = frozenset({
    "initial_screen", "investment_memo", "deep_insight_gate", "industry_framework",
})


def resolve_thinking_level(purpose: str | None, *, policy: Mapping[str, Any] | None = None,
                          requested: str | None = None, verifier: bool = False) -> str:
    """Freeze an explicit level; independent verifiers can only request low."""
    override = ((policy or {}).get("thinking_level_overrides") or {}).get(purpose, {})
    level = requested if requested is not None else override.get("level")
    if level is None:
        level = "medium" if purpose in MEDIUM_THINKING_PURPOSES else "low"
    if not isinstance(level, str) or level not in THINKING_LEVELS:
        raise CallBudgetError("thinking level must be off/minimal/low/medium/high")
    if (verifier or (isinstance(purpose, str) and purpose.endswith("_verifier"))) and level != "low":
        raise CallBudgetError("independent verifier thinking level must be low")
    return level


def call_budget_floor(purpose: str) -> dict[str, Any] | None:
    """The code floor for ``purpose``'s call budget, or None."""

    return dict(LONG_CONTEXT_CALL_FLOOR) if purpose in LONG_CONTEXT_PURPOSES else None


def run_budget_floor(purpose: str, *, max_units: int | None = None,
                     call_budgets: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    """Reserve the required drafts and one verifier, without buying repairs.

    This is a per-run ceiling, like the per-call floor; mission/day/pool
    authorization still governs admission and actual spend. Old installed
    caps cannot make a product unrunnable, and higher call caps are included.
    """
    if max_units is not None:
        validate_run_budget_overrides({"max_units": max_units})
    drafts = {"investment_memo": 4, "dossier": max_units if max_units is not None else 3,
              "debate_map": 1}.get(purpose)
    if drafts is None:
        return None
    budgets = call_budgets or {}
    if not isinstance(budgets, Mapping):
        raise CallBudgetError("run call budgets must be an object")
    floor = float(LONG_CONTEXT_CALL_FLOOR["max_cost_usd"])

    def cost(name: str) -> float:
        checked = _checked(budgets.get(name, {}), f"run call budgets.{name}")
        return max(floor, float(checked.get("max_cost_usd", floor)))
    return {"max_cost_usd": drafts * cost(purpose) + cost(purpose + "_verifier")}


def _apply_floor(resolved: dict[str, Any], floor: Mapping[str, Any] | None) -> dict[str, Any]:
    if floor:
        for field, minimum in floor.items():
            if field in resolved and resolved[field] < minimum:
                resolved[field] = minimum
    return resolved


def _checked(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CallBudgetError(f"{name} must be an object")
    unknown = sorted(set(value) - set(FIELDS))
    if unknown:
        raise CallBudgetError(f"{name} has unknown fields: {unknown}")
    out = dict(value)
    for field in ("max_input_tokens", "max_output_tokens", "timeout_seconds"):
        if field in out and (isinstance(out[field], bool) or not isinstance(out[field], int)
                             or out[field] <= 0):
            raise CallBudgetError(f"{name}.{field} must be a positive integer")
    if "max_cost_usd" in out:
        cost = out["max_cost_usd"]
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(float(cost)) or float(cost) <= 0):
            raise CallBudgetError(f"{name}.max_cost_usd must be a positive finite number")
    return out


def validate_budget_overrides(value: Any) -> dict[str, Any]:
    """Validate and copy a partial owner override without inventing defaults."""
    return _checked(value, "call budget override")


def validate_run_budget_overrides(value: Any) -> dict[str, Any]:
    """Validate a partial multi-unit run bound without supplying policy."""
    if not isinstance(value, Mapping):
        raise CallBudgetError("run budget override must be an object")
    unknown = sorted(set(value) - set(RUN_FIELDS))
    if unknown:
        raise CallBudgetError(f"run budget override has unknown fields: {unknown}")
    out = dict(value)
    for field in RUN_FIELDS[1:]:
        if field in out and (isinstance(out[field], bool) or not isinstance(out[field], int)
                             or out[field] <= 0):
            raise CallBudgetError(f"run budget override.{field} must be a positive integer")
    if MEMO_MAINTENANCE_ROUNDS_FIELD in out and out[MEMO_MAINTENANCE_ROUNDS_FIELD] not in {1, 2}:
        raise CallBudgetError("memo content maintenance rounds per group must be 1 or 2")
    if "max_cost_usd" in out:
        cost = out["max_cost_usd"]
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(float(cost)) or float(cost) <= 0):
            raise CallBudgetError(
                "run budget override.max_cost_usd must be a positive finite number")
    return out


def freeze_shared_budget_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze only the new limits contract for a finite execution batch.

    Old cost-only policies retain their historical hot-read semantics. The
    snapshot is execution input, never an installed file or a policy write.
    """
    from .shared_call_budget_policy import LIMITS_SCHEMA_VERSIONS, load_shared_call_budget_policy
    frozen = dict(config)
    if frozen.get("shared_call_budget_policy_snapshot") is not None:
        _shared_policy(frozen)
    elif frozen.get("shared_call_budget_policy_path") is not None:
        policy = load_shared_call_budget_policy(frozen["shared_call_budget_policy_path"])
        if policy["schema_version"] in LIMITS_SCHEMA_VERSIONS:
            frozen["shared_call_budget_policy_snapshot"] = policy
    return frozen


def _shared_policy(config: Mapping[str, Any]) -> dict[str, Any] | None:
    from .shared_call_budget_policy import load_shared_call_budget_policy, validate_shared_call_budget_policy
    snapshot = config.get("shared_call_budget_policy_snapshot")
    if snapshot is not None:
        if config.get("shared_call_budget_policy_path") is None:
            raise CallBudgetError("shared budget snapshot requires its installed policy path")
        return validate_shared_call_budget_policy(snapshot)
    path = config.get("shared_call_budget_policy_path")
    return None if path is None else load_shared_call_budget_policy(path)


def bound_run_argument(config: Mapping[str, Any], purpose: str, field: str,
                       requested: Any, resolved: Any) -> Any:
    """A task may request fewer units, never bypass an explicit shared bound."""
    value = resolved if requested is None else requested
    policy = _shared_policy(config)
    if policy is not None:
        from .shared_call_budget_policy import effective_shared_limits
        upper = effective_shared_limits(policy, purpose, kind="run").get(field)
        if upper is not None:
            value = min(value, upper)
    return value


def resolve_run_budget(config: Mapping[str, Any], purpose: str, *,
                       defaults: Mapping[str, Any],
                       call_budgets: Mapping[str, Mapping[str, Any]] | None = None,
                       max_units: int | None = None) -> dict[str, Any]:
    """Resolve overrides, then enforce required product-call arithmetic."""
    if not isinstance(config, Mapping):
        raise CallBudgetError("model configuration must be an object")
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    resolved = default_run_budget(purpose, defaults=defaults)
    if not resolved:
        raise CallBudgetError("run budget defaults must not be empty")
    resolved.update(validate_run_budget_overrides(config.get("run_budget", {})))
    per_purpose = config.get("purpose_run_budgets", {})
    if not isinstance(per_purpose, Mapping):
        raise CallBudgetError("purpose_run_budgets must be an object")
    for key, value in per_purpose.items():
        if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
            raise CallBudgetError("purpose_run_budgets keys must be canonical purpose tokens")
        validate_run_budget_overrides(value)
    resolved.update(validate_run_budget_overrides(per_purpose.get(purpose, {})))
    shared_policy = _shared_policy(config)
    if shared_policy is not None:
        from .shared_call_budget_policy import effective_shared_limits
        resolved.update(effective_shared_limits(shared_policy, purpose, kind="run"))
    if purpose == 'investment_memo' and MEMO_MAINTENANCE_ROUNDS_FIELD in resolved:
        # This new permission count belongs to shared administrator authority;
        # a local configuration cannot silently opt a project into round two.
        resolved[MEMO_MAINTENANCE_ROUNDS_FIELD] = (
            (shared_policy or {}).get('purpose_run_limits',{}).get('investment_memo',{}).get(
                MEMO_MAINTENANCE_ROUNDS_FIELD,1))
    if max_units is not None:
        resolved.update(validate_run_budget_overrides({"max_units": bound_run_argument(
            config, purpose, "max_units", max_units, resolved.get("max_units"))}))
    if purpose in {"investment_memo", "dossier", "debate_map"} and call_budgets is None:
        call_budgets = {name: resolve_call_budget(config, name, defaults=LONG_CONTEXT_CALL_FLOOR)
                        for name in (purpose, purpose + "_verifier")}
    return _apply_floor(resolved, run_budget_floor(
        purpose, max_units=resolved.get("max_units"), call_budgets=call_budgets))


def default_run_budget(purpose: str, *,
                       defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read packaged run bounds, preserving unlisted callers' own defaults."""
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    path = Path(__file__).with_name("run_budget_defaults.json")
    if path.is_file():
        try:
            wire = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CallBudgetError(f"packaged run budget defaults cannot be read: {exc}") from exc
        if (not isinstance(wire, Mapping) or set(wire) != {"schema_version", "purposes"}
                or wire.get("schema_version") != "0.1"
                or not isinstance(wire.get("purposes"), Mapping)):
            raise CallBudgetError("packaged run budget defaults have an invalid shape")
        for key, value in wire["purposes"].items():
            if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
                raise CallBudgetError("packaged run purpose keys must be canonical tokens")
            validate_run_budget_overrides(value)
        if purpose in wire["purposes"]:
            resolved = ({} if defaults is None
                        else validate_run_budget_overrides(defaults))
            resolved.update(validate_run_budget_overrides(wire["purposes"][purpose]))
        elif defaults is not None:
            resolved = validate_run_budget_overrides(defaults)
        else:
            raise CallBudgetError(f"no packaged run budget for purpose {purpose}")
    elif defaults is not None:
        resolved = validate_run_budget_overrides(defaults)
    else:
        raise CallBudgetError("packaged run budget defaults are missing")
    if not resolved:
        raise CallBudgetError("run budget defaults must not be empty")
    return _apply_floor(resolved, run_budget_floor(purpose, max_units=resolved.get("max_units")))


def default_call_budget(purpose: str, *,
                        defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read the packaged baseline, with a caller fallback for old installations."""
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    path = Path(__file__).with_name("call_budget_defaults.json")
    if path.is_file():
        try:
            wire = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CallBudgetError(f"packaged call budget defaults cannot be read: {exc}") from exc
        if (not isinstance(wire, Mapping) or set(wire) != {"schema_version", "defaults", "purposes"}
                or wire.get("schema_version") != "0.1"
                or not isinstance(wire.get("purposes"), Mapping)):
            raise CallBudgetError("packaged call budget defaults have an invalid shape")
        packaged = _checked(wire["defaults"], "packaged defaults")
        for key, value in wire["purposes"].items():
            if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
                raise CallBudgetError("packaged purpose keys must be canonical tokens")
            _checked(value, f"packaged purposes.{key}")
        if purpose in wire["purposes"]:
            base = packaged
            base.update(_checked(wire["purposes"][purpose],
                                 f"packaged purposes.{purpose}"))
        elif defaults is not None:
            # An unlisted existing caller remains authoritative for its own
            # legacy defaults. The catalog base is for read-only discovery,
            # not a reason to silently change a runtime contract.
            base = _checked(defaults, "defaults")
        else:
            base = packaged
    elif defaults is not None:
        base = _checked(defaults, "defaults")
    else:
        raise CallBudgetError("packaged call budget defaults are missing")
    missing = sorted(set(FIELDS) - set(base))
    if missing:
        raise CallBudgetError(f"call budget defaults are missing fields: {missing}")
    return _apply_floor({field: base[field] for field in FIELDS}, call_budget_floor(purpose))


def resolve_call_budget(config: Mapping[str, Any], purpose: str, *,
                        defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Return purpose > general > caller defaults, after closed validation."""
    if not isinstance(config, Mapping):
        raise CallBudgetError("model configuration must be an object")
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    resolved = default_call_budget(purpose, defaults=defaults)
    resolved.update(_checked(config.get("call_budget", {}), "call_budget"))
    per_purpose = config.get("purpose_call_budgets", {})
    if not isinstance(per_purpose, Mapping):
        raise CallBudgetError("purpose_call_budgets must be an object")
    for key, value in per_purpose.items():
        if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
            raise CallBudgetError("purpose_call_budgets keys must be canonical purpose tokens")
        _checked(value, f"purpose_call_budgets.{key}")
    resolved.update(_checked(per_purpose.get(purpose, {}),
                             f"purpose_call_budgets.{purpose}"))
    shared_policy = _shared_policy(config)
    if shared_policy is not None:
        from .shared_call_budget_policy import effective_shared_limits
        resolved.update(effective_shared_limits(shared_policy, purpose))
    # Last, so neither an installed override nor the shared cost policy can
    # take a long-context purpose below what its prompt needs.
    return _apply_floor({field: resolved[field] for field in FIELDS}, call_budget_floor(purpose))


def budget_fingerprint(budget: Mapping[str, Any]) -> str:
    checked = _checked(budget, "budget")
    if set(checked) != set(FIELDS):
        raise CallBudgetError("budget fingerprint requires all four fields")
    wire = json.dumps({field: checked[field] for field in FIELDS},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


__all__ = ["CallBudgetError", "LONG_CONTEXT_CALL_FLOOR", "LONG_CONTEXT_CHAIN_PURPOSES", "LONG_CONTEXT_PURPOSES",
           "THINKING_LEVELS", "MEDIUM_THINKING_PURPOSES", "resolve_thinking_level",
           "budget_fingerprint", "call_budget_floor", "long_context_call_cost_usd", "default_call_budget", "default_run_budget",
           "resolve_call_budget", "resolve_run_budget", "run_budget_floor", "validate_budget_overrides",
           "validate_run_budget_overrides"]
