"""P13k: install the planner's own routing policy and model configuration.

The planner decides what the research works on next.  That is a different job
from reading a window of a filing, and it needs a different model, so it needs
a routing policy of its own: the extraction policy pins exactly one profile by
design, and sharing it would mean the two jobs could never differ.

The shape is deliberately the same as ``document_extraction_setup`` -- append a
policy version only when the filters actually change, reference profile *ids*
so daily profile refreshes never stale it, write one closed owner-only config.

What is different is that **nothing routes here unless the owner asks**.  The
planner's model is fifty times the unit cost of the extraction model, and while
that is affordable for what the planner does -- read a ~2,400 token state a few
times a day -- it is not a default anyone should acquire by upgrading.  The
installer only calls this when ``DALTON_PLANNER_MODEL_PROFILE`` is set, and it
records which profile was chosen so the answer to "what is deciding this" is on
disk rather than in someone's memory.

Never touches a credential: the broker key path is referenced, not read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .document_extraction import validate_model_config
from .model_configurations import register_model_config_name
from .model_deployment import ADAPTER_REF, ensure_broker_profiles
from .model_router import ModelRouter
from .store import canonical_json, content_hash

POLICY_ID = "model-routing-policy:dalton-openclaw-planner-decisions"
CONFIG_FILE_NAME = "research-planner-model-config.json"
# The lane registers its own configuration file rather than being listed in a
# script it cannot reach -- the whole point of the registry (P14-0). A repeat
# is a no-op, so the seed list and this agree instead of competing.
register_model_config_name(CONFIG_FILE_NAME)
# No default profile. A planner with no model configured plans nothing, which
# is the correct behaviour for an install nobody has pointed at a model.
CREDENTIAL_SLOTS_BY_PROVIDER = {
    "openai": "credential-slot:openclaw:openai",
    "deepseek": "credential-slot:openclaw:deepseek",
    "google": "credential-slot:openclaw:google",
    "anthropic": "credential-slot:openclaw:anthropic",
}


class PlannerSetupError(RuntimeError):
    """The planner model configuration cannot be installed as asked."""


def _policy_filters(
    profile_ids: list[str], *, family_independence: bool = False
) -> dict[str, Any]:
    return {
        "allowed_profile_ids": list(profile_ids),
        "allowed_providers": [],
        "allowed_families": [],
        "allowed_adapter_refs": [ADAPTER_REF],
        "required_modalities": ["text"],
        "family_independence_capabilities": (
            ["verify", "adjudicate"] if family_independence else []
        ),
    }


def ensure_planner_policy(
    router: ModelRouter, *, profile_ids: list[str] | None = None,
    now: datetime | None = None, policy_id: str = POLICY_ID,
    tier: str | None = None,
) -> dict[str, Any]:
    """Append a policy version only when the latest one differs; return its ref.

    ``policy_id`` is a parameter because more than one job wants its own model
    while wanting exactly this behaviour: pin by profile id, append only on a
    real change, cheapest-first among the pinned. The alternative was a second
    copy of the same two hundred lines with two names changed.

    ``tier`` is P14-M.  Given one, the policy pins that tier's whole fallback
    chain instead of a single profile, and carries the tier table with it, so
    the chain a lane runs is a property of the version it pinned rather than of
    whatever the code happened to say the day it ran.  The verifier tier also
    turns family independence on, because a chain that can reach three families
    is exactly the case where the filter has to be there.
    """

    from .model_fallback_chain import fallback_chains, tier_chain

    if tier is not None:
        chain = list(tier_chain(tier))
        if profile_ids and list(profile_ids) != chain:
            raise PlannerSetupError(
                f"tier {tier} pins its own chain; do not pass profile_ids as well"
            )
        profile_ids = chain
    if not profile_ids:
        raise PlannerSetupError("a routing policy must pin at least one profile")
    slug = policy_id.split(":", 1)[1]
    row = router.connection.execute(
        "SELECT policy_json FROM model_routing_policy_versions WHERE policy_id=? "
        "ORDER BY version DESC LIMIT 1",
        (policy_id,),
    ).fetchone()
    filters = _policy_filters(
        profile_ids, family_independence=tier == "verifier"
    )
    chains = fallback_chains() if tier is not None else None
    # Cheapest-first among the pinned profiles, then a stable tiebreak. With one
    # profile pinned the ordering is moot; it matters the day somebody pins two.
    preferences = [
        {"field": "estimated_cost_usd", "direction": "asc"},
        {"field": "profile_version_ref", "direction": "asc"},
    ]
    overrides = None
    if row is not None:
        latest = json.loads(row["policy_json"])
        # P14-M2: the owner's per-stage selection rides forward. This runs
        # again on every deploy, and rebuilding the content from the code's
        # defaults would drop ``purpose_overrides`` -- so re-running the
        # installer would quietly undo every model choice the owner had made.
        overrides = latest.get("purpose_overrides")
        if canonical_json(latest["filters"]) == canonical_json(filters) and \
                canonical_json(latest["ordered_preferences"]) == canonical_json(preferences) and \
                canonical_json(latest.get("fallback_chains")) == canonical_json(chains):
            return {"status": "duplicate", "policy_version_ref": latest["policy_version_ref"]}
        version = int(latest["version"]) + 1
        prior = latest["policy_version_ref"]
    else:
        version, prior = 1, None
    wire = {
        "schema_version": "0.1",
        "id": policy_id,
        "policy_version_ref": f"model-routing-policy-version:{slug}:{version}",
        "version": version,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(timespec="microseconds"),
        "prior_version_ref": prior,
        "filters": filters,
        "ordered_preferences": preferences,
    }
    if chains is not None:
        wire["fallback_chains"] = chains
    if overrides:
        wire["purpose_overrides"] = overrides
    wire["content_hash"] = content_hash(wire)
    result = router.register_policy(wire)
    return {"status": result.get("status", "fresh"),
            "policy_version_ref": wire["policy_version_ref"]}


def credential_slots_for(router: ModelRouter, profile_ids: list[str]) -> list[str]:
    """The credential slots the pinned profiles actually declare.

    Read from the registered profiles rather than guessed from the id, because
    a wrong slot here is a call that fails at the broker with no useful reason.
    """

    slots: list[str] = []
    for profile_id in profile_ids:
        row = router.connection.execute(
            "SELECT profile_json FROM model_endpoint_profile_versions WHERE profile_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
        if row is None:
            raise PlannerSetupError(
                f"{profile_id} is not registered in the model router; "
                "install the catalog before pinning it"
            )
        slot = json.loads(row["profile_json"]).get("credential_slot_ref")
        if not isinstance(slot, str) or not slot:
            raise PlannerSetupError(f"{profile_id} declares no credential slot")
        if slot not in slots:
            slots.append(slot)
    return slots


def install(
    config_path: str | Path,
    *,
    profile_ids: list[str] | None = None,
    now: datetime | None = None,
    policy_id: str = POLICY_ID,
    config_file_name: str = CONFIG_FILE_NAME,
    tier: str | None = None,
) -> dict[str, Any]:
    from .model_fallback_chain import tier_chain

    if tier is not None and not profile_ids:
        profile_ids = list(tier_chain(tier))
    if not profile_ids:
        raise PlannerSetupError("name the profiles to pin, or the tier to pin")
    config_path = Path(config_path).expanduser().resolve()
    service = json.loads(config_path.read_text(encoding="utf-8"))
    planner = service["bounded_planner"]["config"]
    thesis = service["thesis_impact"]["config"]
    state_dir = Path(service["core_db"]).parent
    router_db = str(Path(service["model_router_db"]).resolve())
    with ModelRouter(router_db) as router:
        # A profile the router has never seen cannot be pinned, and nothing in
        # the deploy registered them: the live catalog arrived from canary
        # scripts. Register what is missing first, then pin.
        catalog = ensure_broker_profiles(
            router, checked_at=now or datetime.now(timezone.utc))
        policy = ensure_planner_policy(router, profile_ids=list(profile_ids), now=now,
                                       policy_id=policy_id, tier=tier)
        slots = credential_slots_for(router, list(profile_ids))
    model_config = validate_model_config({
        "routing_policy_ref": policy["policy_version_ref"],
        "credential_slot_refs": slots,
        "model_router_db": router_db,
        "broker_socket": str(Path(planner["planner_broker_socket"]).resolve()),
        "broker_auth_key": str(Path(planner["planner_broker_auth_key"]).resolve()),
        "broker_client_id": planner["planner_broker_client_id"],
        "expected_agent_id": planner["planner_expected_agent_id"],
        "budget_db": str(Path(thesis["budget_db"]).resolve()),
        "budget_policy_ref": thesis["budget_policy_version_id"],
    })
    target = state_dir / config_file_name
    changed = (not target.exists()
               or json.loads(target.read_text(encoding="utf-8")) != model_config)
    if changed:
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(canonical_json(model_config) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    return {
        "policy": policy,
        "tier": tier,
        "catalog_profiles_added": catalog["added"],
        "profile_ids": list(profile_ids),
        "credential_slot_refs": slots,
        "model_config_path": str(target),
        "model_config_changed": changed,
        "routing_policy_ref": model_config["routing_policy_ref"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="service.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--profile-ids",
        help="comma-separated profile ids the planner may route to, e.g. profile:gpt-6-astra",
    )
    group.add_argument(
        "--tier",
        help="pin the whole fallback chain of one purpose tier, e.g. brain",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    profile_ids = (
        [item.strip() for item in args.profile_ids.split(",") if item.strip()]
        if args.profile_ids else None
    )
    try:
        result = install(args.config, profile_ids=profile_ids, tier=args.tier)
    except PlannerSetupError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "CONFIG_FILE_NAME",
    "POLICY_ID",
    "PlannerSetupError",
    "credential_slots_for",
    "ensure_planner_policy",
    "install",
]
