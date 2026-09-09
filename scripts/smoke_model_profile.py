#!/usr/bin/env python3
"""Ask one model one trivial question, and say exactly why if it cannot.

A profile can be registered, current, pinned by a policy and still not run:
the model has to be allowed by the broker plugin's ``llm.allowedModels``, by
the dedicated agent's own ``models`` map, and be executable by whatever runtime
it declares. Three separate lists, in two different files, and a failure in any
of them arrives as ``HOST_COMPLETION_FAILED`` -- three words that name none of
them.

So this makes the smallest possible real call and reports what happened. It is
the answer to "is this model actually usable", which the reconciler asks for
every new profile (``smoke_required_profile_ids``) and which nothing could
previously answer without running a whole lane.

Costs one very short completion. Writes nothing but the plan store's untouched
scheduler rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.cockpit_model import CockpitModel, CockpitModelError  # noqa: E402
from dalton_core.model_router import ModelRouter  # noqa: E402
from dalton_core.research_planner_setup import (  # noqa: E402
    _policy_filters,
    credential_slots_for,
)
from dalton_core.store import content_hash  # noqa: E402

SMOKE_POLICY_ID = "model-routing-policy:dalton-openclaw-smoke"
PROMPT = (
    "Reply with exactly this JSON object and nothing else: "
    '{"ok": true}'
)


def _ensure_smoke_policy(router: ModelRouter, profile_id: str, now: str) -> str:
    """A throwaway policy pinning one profile, appended per profile tested."""

    slug = SMOKE_POLICY_ID.split(":", 1)[1]
    rows = router.connection.execute(
        "SELECT policy_json FROM model_routing_policy_versions WHERE policy_id=? "
        "ORDER BY version DESC LIMIT 1", (SMOKE_POLICY_ID,),
    ).fetchone()
    filters = _policy_filters([profile_id])
    if rows is not None:
        latest = json.loads(rows["policy_json"])
        if latest["filters"]["allowed_profile_ids"] == [profile_id]:
            return latest["policy_version_ref"]
        version, prior = int(latest["version"]) + 1, latest["policy_version_ref"]
    else:
        version, prior = 1, None
    wire = {
        "schema_version": "0.1", "id": SMOKE_POLICY_ID,
        "policy_version_ref": f"model-routing-policy-version:{slug}:{version}",
        "version": version, "created_at": now, "prior_version_ref": prior,
        "filters": filters,
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }
    wire["content_hash"] = content_hash(wire)
    router.register_policy(wire)
    return wire["policy_version_ref"]


def smoke(config_path: Path, *, profile_id: str) -> dict[str, Any]:
    from datetime import datetime, timezone

    service = json.loads(config_path.read_text(encoding="utf-8"))
    planner = service["bounded_planner"]["config"]
    thesis = service["thesis_impact"]["config"]
    state_dir = Path(service["core_db"]).parent
    router_db = str(Path(service["model_router_db"]).resolve())
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with ModelRouter(router_db) as router:
        policy_ref = _ensure_smoke_policy(router, profile_id, now)
        slots = credential_slots_for(router, [profile_id])
    model = CockpitModel(
        {
            "routing_policy_ref": policy_ref, "credential_slot_refs": slots,
            "model_router_db": router_db,
            "broker_socket": str(Path(planner["planner_broker_socket"]).resolve()),
            "broker_auth_key": str(Path(planner["planner_broker_auth_key"]).resolve()),
            "broker_client_id": planner["planner_broker_client_id"],
            "expected_agent_id": planner["planner_expected_agent_id"],
            "budget_db": str(Path(thesis["budget_db"]).resolve()),
            "budget_policy_ref": thesis["budget_policy_version_id"],
        },
        scheduler_db=str(state_dir / "scheduler.sqlite"),
        max_input_tokens=2_000, max_output_tokens=200, max_cost_usd=0.05,
        timeout_seconds=120,
    )
    # The budget is scoped to a mission, so the smoke call needs the real one:
    # a synthesised mission would be refused by the budget binding.
    from dalton_core.coverage_mission import CoverageMissionAuthority
    from dalton_core.store import DaltonStore

    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer LIMIT 1"
        ).fetchone()
        if pointer is None:
            return {"profile_id": profile_id, "status": "no_mission",
                    "reason": "the budget is scoped to a mission and none is published"}
        mission = CoverageMissionAuthority(store).mission(pointer["mission_version_id"])
    finally:
        store.close()
    try:
        call = model.call(purpose="ask", request_id=f"smoke-{profile_id.split(':')[-1]}"[:32],
                          prompt=PROMPT, mission=mission)
    except CockpitModelError as exc:
        return {"profile_id": profile_id, "status": "failed",
                "policy_version_ref": policy_ref, "reason": str(exc)}
    return {
        "profile_id": profile_id, "status": "ok",
        "policy_version_ref": policy_ref,
        "replayed": bool(call.get("replayed")),
        "cost_micros": call.get("cost_micros"),
        "reply": (call.get("text") or "")[:200],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="service.json")
    parser.add_argument("--profile-id", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = smoke(args.config.expanduser().resolve(), profile_id=args.profile_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
