"""Install the approved document-extraction model configuration (P9d-17a).

ADR-0005 makes drafting a mission automation step, so the writer must carry
an extraction model configuration on live.  That configuration names three
things that already exist on the host and one that must be appended:

- the OpenClaw model broker the bounded planner already uses (socket, auth
  key, client id, agent id), read from ``bounded_planner.config``;
- the shared day budget ledger and policy the thesis-impact lane already
  spends against, read from ``thesis_impact.config``;
- the model router database;
- a routing policy version for extraction, appended here if the latest
  version of ``model-routing-policy:dalton-openclaw-extraction`` does not
  already carry the requested filters.  Policies reference profile *ids*
  (``profile:deepseek-v4-flash``), so daily profile refreshes never stale it.

Idempotent.  Writes the closed configuration JSON (owner-only) next to the
state, and points ``control.config.research_review.document_extraction_model_config_path``
at it so the launch agent passes it to the writer.  Never touches a
credential: the broker key path is referenced, not read.
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
from .model_deployment import ADAPTER_REF
from .model_router import ModelRouter
from .store import canonical_json, content_hash

POLICY_ID = "model-routing-policy:dalton-openclaw-extraction"
DEFAULT_PROFILE_IDS = ("profile:deepseek-v4-flash",)
DEFAULT_CREDENTIAL_SLOTS = ("credential-slot:openclaw:deepseek",)
CONFIG_FILE_NAME = "document-extraction-model-config.json"


def _policy_filters(profile_ids: list[str]) -> dict[str, Any]:
    return {
        "allowed_profile_ids": list(profile_ids),
        "allowed_providers": [],
        "allowed_families": [],
        "allowed_adapter_refs": [ADAPTER_REF],
        "required_modalities": ["text"],
        "family_independence_capabilities": [],
    }


def ensure_extraction_policy(
    router: ModelRouter, *, profile_ids: list[str], now: datetime | None = None
) -> dict[str, Any]:
    """Append a policy version only when the latest one differs; return its ref."""

    slug = POLICY_ID.split(":", 1)[1]
    row = router.connection.execute(
        "SELECT policy_json FROM model_routing_policy_versions WHERE policy_id=? "
        "ORDER BY version DESC LIMIT 1",
        (POLICY_ID,),
    ).fetchone()
    filters = _policy_filters(profile_ids)
    preferences = [
        {"field": "estimated_cost_usd", "direction": "asc"},
        {"field": "profile_version_ref", "direction": "asc"},
    ]
    if row is not None:
        latest = json.loads(row["policy_json"])
        if canonical_json(latest["filters"]) == canonical_json(filters) and \
                canonical_json(latest["ordered_preferences"]) == canonical_json(preferences):
            return {"status": "duplicate", "policy_version_ref": latest["policy_version_ref"]}
        version = int(latest["version"]) + 1
        prior = latest["policy_version_ref"]
    else:
        version, prior = 1, None
    wire = {
        "schema_version": "0.1",
        "id": POLICY_ID,
        "policy_version_ref": f"model-routing-policy-version:{slug}:{version}",
        "version": version,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(timespec="microseconds"),
        "prior_version_ref": prior,
        "filters": filters,
        "ordered_preferences": preferences,
    }
    wire["content_hash"] = content_hash(wire)
    result = router.register_policy(wire)
    return {"status": result.get("status", "fresh"), "policy_version_ref": wire["policy_version_ref"]}


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def install(
    config_path: str | Path,
    *,
    profile_ids: list[str] | None = None,
    credential_slots: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config_path = Path(config_path).expanduser().resolve()
    service = json.loads(config_path.read_text(encoding="utf-8"))
    planner = service["bounded_planner"]["config"]
    thesis = service["thesis_impact"]["config"]
    state_dir = Path(service["core_db"]).parent
    router_db = str(Path(service["model_router_db"]).resolve())
    with ModelRouter(router_db) as router:
        policy = ensure_extraction_policy(router, profile_ids=list(profile_ids or DEFAULT_PROFILE_IDS), now=now)
    model_config = validate_model_config({
        "routing_policy_ref": policy["policy_version_ref"],
        "credential_slot_refs": list(credential_slots or DEFAULT_CREDENTIAL_SLOTS),
        "model_router_db": router_db,
        "broker_socket": str(Path(planner["planner_broker_socket"]).resolve()),
        "broker_auth_key": str(Path(planner["planner_broker_auth_key"]).resolve()),
        "broker_client_id": planner["planner_broker_client_id"],
        "expected_agent_id": planner["planner_expected_agent_id"],
        "budget_db": str(Path(thesis["budget_db"]).resolve()),
        "budget_policy_ref": thesis["budget_policy_version_id"],
    })
    target = state_dir / CONFIG_FILE_NAME
    changed = not target.exists() or json.loads(target.read_text(encoding="utf-8")) != model_config
    if changed:
        _write_owner_only(target, model_config)
    review = service["control"]["config"].setdefault("research_review", {})
    config_changed = review.get("document_extraction_model_config_path") != str(target)
    if config_changed:
        review["document_extraction_model_config_path"] = str(target)
        _write_owner_only(config_path, service)
    return {
        "policy": policy,
        "model_config_path": str(target),
        "model_config_changed": changed,
        "service_config_changed": config_changed,
        "routing_policy_ref": model_config["routing_policy_ref"],
        "budget_policy_ref": model_config["budget_policy_ref"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="service.json")
    parser.add_argument("--profile-ids", default=",".join(DEFAULT_PROFILE_IDS))
    parser.add_argument("--credential-slots", default=",".join(DEFAULT_CREDENTIAL_SLOTS))
    args = parser.parse_args(argv)
    result = install(
        args.config,
        profile_ids=[item.strip() for item in args.profile_ids.split(",") if item.strip()],
        credential_slots=[item.strip() for item in args.credential_slots.split(",") if item.strip()],
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
