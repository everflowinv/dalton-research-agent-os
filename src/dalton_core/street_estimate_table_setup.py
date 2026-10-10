"""Install paired street-table pins for an existing workspace, without calls."""
import argparse
import json
import sqlite3
from pathlib import Path
from .research_planner_setup import install
from .street_estimate_table_model import MODEL_CONFIG, VERIFIER_CONFIG
from .store import content_hash


def preview(config: str, **selection) -> dict:
    service = json.loads(Path(config).read_text())
    router_path = Path(service["model_router_db"]).resolve()
    from .model_fallback_chain import tier_chain
    roles = []
    with sqlite3.connect(router_path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        for role, filename in (("producer", MODEL_CONFIG), ("verifier", VERIFIER_CONFIG)):
            tier = selection.get(role + "_tier")
            profiles = selection.get(role + "_profile_ids")
            if bool(tier) == bool(profiles):
                raise ValueError("each role requires exactly one tier or explicit profile list")
            chosen = list(tier_chain(tier)) if tier else profiles
            held = []
            for profile in chosen:
                row = connection.execute("SELECT profile_json FROM model_endpoint_profile_versions "
                    "WHERE profile_id=? ORDER BY rowid DESC LIMIT 1", (profile,)).fetchone()
                if row is None:
                    raise ValueError(f"profile is not registered: {profile}")
                wire = json.loads(row[0])
                held.append({"id": profile, "version_ref": wire["profile_version_ref"],
                             "hash": wire["content_hash"], "family": wire["family"]})
            roles.append({"role": role, "config_file": filename, "tier": tier, "profiles": held})
    families = [{p["family"] for p in role["profiles"]} for role in roles]
    if len(families[0]) == len(families[1]) == 1 and families[0] == families[1]:
        raise ValueError("producer and verifier selections contain only the same model family")
    body = {"mode": "preview", "service_config_hash": content_hash(service),
        "config": str(Path(config).resolve()), "roles": roles,
        "writes_on_apply": ["model routing policy versions/idempotency", "two state-directory model configs"],
        "model_calls": 0, "writer_argv_change_required": False}
    return {**body, "plan_hash": content_hash(body)}


def configure(config: str, *, producer_tier: str | None = None, verifier_tier: str | None = None,
              producer_profile_ids: list[str] | None = None, verifier_profile_ids: list[str] | None = None) -> dict:
    if bool(producer_tier) == bool(producer_profile_ids) or bool(verifier_tier) == bool(verifier_profile_ids):
        raise ValueError("each role requires exactly one tier or explicit profile list")
    if ((producer_tier and producer_tier == verifier_tier)
            or (producer_profile_ids and producer_profile_ids == verifier_profile_ids)):
        raise ValueError("name distinct producer and verifier selections")
    # Reuse existing installed broker, credentials, router and immutable day
    # budget authorities. This creates no credential or spending permission.
    results = [install(config, tier=tier, profile_ids=profiles, policy_id=policy, config_file_name=name)
        for tier, profiles, policy, name in (
            (producer_tier, producer_profile_ids, "model-routing-policy:dalton-openclaw-street-estimate", MODEL_CONFIG),
            (verifier_tier, verifier_profile_ids, "model-routing-policy:dalton-openclaw-street-estimate-verifier", VERIFIER_CONFIG))]
    return {"status": "configured", "configurations": results,
            "note": "No model call; writer automatically discovers the paired state-directory configs."}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--apply", action="store_true", help="install reviewed paired configs; no model calls")
    p.add_argument("--expected-plan-hash", help="require the same source config and profile versions")
    for role in ("producer", "verifier"):
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--" + role + "-tier")
        group.add_argument("--" + role + "-profile-ids", help="comma-separated existing approved profiles")
    args = p.parse_args(argv)
    try:
        selection = dict(producer_tier=args.producer_tier, verifier_tier=args.verifier_tier,
            producer_profile_ids=args.producer_profile_ids.split(",") if args.producer_profile_ids else None,
            verifier_profile_ids=args.verifier_profile_ids.split(",") if args.verifier_profile_ids else None)
        plan = preview(args.config, **selection)
        if args.expected_plan_hash is not None and plan["plan_hash"] != args.expected_plan_hash:
            raise ValueError("reviewed street configuration plan changed")
        result = configure(args.config, **selection) if args.apply else {"status": "preview", **plan}
    except Exception as exc:
        result = {"status": "refused", "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"configured", "preview"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
