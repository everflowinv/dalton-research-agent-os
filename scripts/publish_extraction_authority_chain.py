#!/usr/bin/env python3
"""Bind the mission's outer research budget so ADR-0005 drafting can spend (P9d-17a).

Live on 2026-09-07: constitution v2 binds ``policy-3`` while ``policy-4`` is
active, and neither policy-4 nor mandate ``us-it-services-constitution-p8a:1``
carries the closed ``research_budget`` that ADR-0004 section 7 (as enforced by
``DocumentExtractionService.outer_budget``) requires before a mission may spend
on models.  Every automated draft was refused with "mission constitution does
not bind current governance policy".

This publishes, in order, each version derived from the current one:

1. ``policy-5``: policy-4's body plus ``research_budget``;
2. mandate ``…-p8a:2``: v1 plus ``research_budget`` in its constraints, activated;
3. constitution ``us-it-services:3``: v2 rebinding policy-5 and mandate v2;
4. mission ``us-it-services:5``: v4 rebinding constitution v3 and mandate v2.

``research_budget`` equals the mission v4 budget the owner already published
(40 paid calls/day, 5 USD/day, 30 AlphaEngine calls/24h): a formalisation, not
an expansion.  Everything else in every record is byte-identical to its prior.

``--rehearse DIR``  copies the live Core into DIR, applies the chain through the
                    authorities directly, and verifies ``outer_budget`` for v5.
``--live``          applies the chain through the writer with the governance
                    CLI's ephemeral ``human:lumos`` principal, one op at a time,
                    each later step bound to the hash the writer returned.

Second run (P9d-17b): ``--add-auto-commit-rule`` lists the mission document
qualitative rule (``research-auto-commit:mission-document-qualitative:v1``) in
``research_candidate_auto_commit.rules`` so policy may admit automation
drafts as Claims.  Same cascade: policy, constitution, mission, each rebinding
only what changed; the mandate is republished only if it lacks the budget.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.agenda import AgendaStore  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority  # noqa: E402
from dalton_core.document_extraction import DocumentExtractionService  # noqa: E402
from dalton_core.research_constitution import ResearchConstitutionAuthority  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402

ACTOR = "human:lumos"
MISSION_REF = "coverage-mission:us-it-services"
MANDATE_REF = "mandate:us-it-services-constitution-p8a"
CONSTITUTION_REF = "constitution:us-it-services"
CHANGE_REASON = (
    "ADR-0005 / P9d-17a: carry the closed research_budget the mission already publishes "
    "(40 paid calls/day, 5 USD/day, 30 AlphaEngine calls/24h) so mission automation may "
    "draft within it; every other rule unchanged"
)
BODY_FIELDS = (
    "title", "objective", "industry_ref", "universe", "research_questions",
    "deliverables", "source_plan", "bindings", "autonomy", "budget",
)


def _live_state() -> Path:
    return Path(os.path.expanduser("~/Library/Application Support/Dalton/state/dalton-core"))


def _read_current(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    mission = json.loads(connection.execute(
        "SELECT v.record_json FROM coverage_mission_versions v JOIN coverage_mission_pointer p "
        "ON p.mission_version_id=v.mission_version_id WHERE p.mission_ref=?", (MISSION_REF,),
    ).fetchone()["record_json"])
    constitution = json.loads(connection.execute(
        "SELECT record_json FROM research_constitution_versions WHERE constitution_version_id=?",
        (mission["bindings"]["constitution_version"]["ref"],),
    ).fetchone()["record_json"])
    mandate = json.loads(connection.execute(
        "SELECT record_json FROM mandate_versions WHERE version_id=?",
        (mission["bindings"]["mandate_version"]["ref"],),
    ).fetchone()["record_json"])
    policy_row = connection.execute(
        "SELECT policy_version_id, policy_json, content_hash FROM governance_policy_versions "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return {"mission": mission, "constitution": constitution, "mandate": mandate,
            "policy_id": policy_row["policy_version_id"], "policy": json.loads(policy_row["policy_json"])}


def build_chain(current: dict[str, Any], *, now: str, add_rules: list[str] | None = None) -> dict[str, Any]:
    mission = current["mission"]
    budget = dict(mission["budget"])
    research_budget = {
        "max_daily_paid_calls": int(budget["max_daily_paid_calls"]),
        "max_daily_cost_usd": float(budget["max_daily_cost_usd"]),
        "max_alphaengine_calls_24h": int(budget["max_alphaengine_calls_24h"]),
    }
    policy_wire = current["policy"]
    policy_body = dict(policy_wire["policy"] if "policy" in policy_wire else policy_wire)
    changed = False
    if "research_budget" not in policy_body:
        policy_body["research_budget"] = research_budget
        changed = True
    auto = dict(policy_body.get("research_candidate_auto_commit") or {})
    rules = list(auto.get("rules") or [])
    for rule in add_rules or []:
        if rule not in rules:
            rules.append(rule)
            changed = True
    if add_rules:
        policy_body["research_candidate_auto_commit"] = {**auto, "rules": rules}
    if not changed:
        raise SystemExit("active policy already carries everything requested; nothing to do")
    version = int(current["policy_id"].rsplit("-", 1)[1]) + 1
    mandate = current["mandate"]
    mandate_version = int(mandate["version"]) + 1
    constitution = current["constitution"]
    constitution_version = int(constitution["version"]) + 1
    mission_version = int(mission["version"]) + 1
    return {
        "research_budget": research_budget,
        "mandate_needed": "research_budget" not in mandate["constraints"],
        "policy": {
            "policy": policy_body,
            "policy_version_id": f"policy-{version}", "version_number": version, "activate": True,
            "policy_ref": policy_wire.get("policy_ref", "commit-gate"), "effective_from": now,
            "effective_until": None, "prior_version_ref": current["policy_id"],
            "change_reason": CHANGE_REASON if not add_rules else (
                "ADR-0005 / P9d-17b: list the mission document qualitative rule so policy may admit "
                "automation-drafted qualitative Claims bound to exact raw spans; every other rule unchanged"),
            "content_hash_value": None,
        },
        "mandate": {
            "mandate_ref": MANDATE_REF, "objective": mandate["objective"],
            "scope_refs": list(mandate["scope_refs"]),
            "constraints": {**mandate["constraints"], "research_budget": research_budget},
            "success_criteria": dict(mandate["success_criteria"]),
            "effective_from": now, "effective_until": None, "activate": True,
            "version_id": f"{MANDATE_REF.replace('mandate:', 'mandate-version:')}:{mandate_version}",
            "idempotency_key": f"{MANDATE_REF}:{mandate_version}:research-budget",
        },
        "constitution": {
            "constitution_ref": CONSTITUTION_REF, "industry_ref": constitution["industry_ref"],
            "title": constitution["title"], "bindings": json.loads(json.dumps(constitution["bindings"])),
            "method": json.loads(json.dumps(constitution["method"])),
            "version_id": f"constitution-version:us-it-services:{constitution_version}",
            "prior_version_ref": constitution["id"],
            "idempotency_key": f"{CONSTITUTION_REF}:{constitution_version}:research-budget",
        },
        "mission": {
            "mission_ref": MISSION_REF,
            **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
            "version_id": f"coverage-mission-version:us-it-services:{mission_version}",
            "prior_version_ref": mission["id"],
            "idempotency_key": f"{MISSION_REF}:{mission_version}:research-budget",
        },
    }


def apply_chain(chain: dict[str, Any], apply: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Run the four publishes in order, binding each later step to returned hashes."""

    out: dict[str, Any] = {}
    policy = apply("create_policy", chain["policy"])
    policy_ref, policy_hash = _ref_hash(policy, chain["policy"]["policy_version_id"])
    out["policy"] = {"ref": policy_ref, "hash": policy_hash}
    if chain["mandate_needed"]:
        mandate = apply("create_mandate", chain["mandate"])
        mandate_ref, mandate_hash = _ref_hash(mandate, chain["mandate"]["version_id"])
        out["mandate"] = {"ref": mandate_ref, "hash": mandate_hash}
    else:
        current_mandate = chain["mission"]["bindings"]["mandate_version"]
        mandate_ref, mandate_hash = current_mandate["ref"], current_mandate["hash"]
        out["mandate"] = {"ref": mandate_ref, "hash": mandate_hash, "status": "unchanged"}
    constitution_params = json.loads(json.dumps(chain["constitution"]))
    constitution_params["bindings"]["governance_policy_version"] = {"ref": policy_ref, "hash": policy_hash}
    constitution_params["bindings"]["mandate_version"] = {"ref": mandate_ref, "hash": mandate_hash}
    constitution = apply("publish_research_constitution", constitution_params)
    constitution_ref, constitution_hash = _ref_hash(constitution, constitution_params["version_id"])
    out["constitution"] = {"ref": constitution_ref, "hash": constitution_hash}
    mission_params = json.loads(json.dumps(chain["mission"]))
    mission_params["bindings"]["constitution_version"] = {"ref": constitution_ref, "hash": constitution_hash}
    mission_params["bindings"]["mandate_version"] = {"ref": mandate_ref, "hash": mandate_hash}
    mission = apply("create_coverage_mission", mission_params)
    mission_ref, mission_hash = _ref_hash(mission, mission_params["version_id"])
    out["mission"] = {"ref": mission_ref, "hash": mission_hash, "status": mission.get("status")}
    return out


def _ref_hash(record: dict[str, Any], expected_ref: str) -> tuple[str, str]:
    body = record.get("version") if isinstance(record.get("version"), dict) else record
    if isinstance(body.get("policy_version"), dict):
        body = body["policy_version"]
    ref = body.get("id") or body.get("policy_version_id") or body.get("version_id")
    digest = body.get("content_hash")
    if ref != expected_ref or not isinstance(digest, str):
        raise SystemExit(f"publish returned an unexpected record for {expected_ref}: {json.dumps(record)[:400]}")
    return ref, digest


def rehearse(state_dir: Path, target: Path, *, add_rules: list[str] | None = None) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("core.sqlite", "core.sqlite-wal", "core.sqlite-shm"):
        source = state_dir / name
        if source.exists():
            shutil.copy(source, target / name)
    store = DaltonStore(str(target / "core.sqlite"))
    try:
        current = _read_current(store.connection)
        chain = build_chain(current, now=datetime.now(timezone.utc).isoformat(timespec="microseconds"), add_rules=add_rules)
        agenda = AgendaStore(store)
        constitutions = ResearchConstitutionAuthority(store)
        missions = CoverageMissionAuthority(store)

        def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            values = dict(params)
            if operation == "create_policy":
                policy = values.pop("policy")
                return {"policy_version": store.create_policy(policy, actor_ref=ACTOR, **values)}
            if operation == "create_mandate":
                mandate_ref = values.pop("mandate_ref")
                return agenda.create_mandate(mandate_ref, actor_ref=ACTOR, **values)
            if operation == "publish_research_constitution":
                constitution_ref = values.pop("constitution_ref")
                return constitutions.publish_constitution(constitution_ref, actor_ref=ACTOR, **values)
            if operation == "create_coverage_mission":
                mission_ref = values.pop("mission_ref")
                return missions.create_mission(mission_ref, actor_ref=ACTOR, **values)
            raise SystemExit(operation)

        result = apply_chain(chain, apply)

        class _Host:
            pass
        host = _Host()
        host.store = store
        host.coverage_mission = missions
        outer = DocumentExtractionService(host).outer_budget(result["mission"]["ref"])
        grants = {}
        principal = missions.mission(result["mission"]["ref"])["autonomy"]["automation_principal"]
        for source in ("source:alphaengine", "source:web-search"):
            try:
                missions.authorize_source_discovery(company_ref="company:sec-cik:0001467373", source_ref=source,
                                                    requested_by=principal, mission_version_ref=result["mission"]["ref"])
                grants[source] = "granted"
            except Exception as exc:
                grants[source] = f"{type(exc).__name__}: {exc}"
        policy_rules = store.active_policy_version().to_dict()["policy"].get("research_candidate_auto_commit", {}).get("rules")
        return {"chain": result, "research_budget": chain["research_budget"], "outer_budget": outer,
                "automation_grants": grants, "auto_commit_rules": policy_rules}
    finally:
        store.close()


def live(state_dir: Path, *, add_rules: list[str] | None = None) -> dict[str, Any]:
    from dalton_core.governance_cli import ephemeral_call
    read = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        current = _read_current(read)
    finally:
        read.close()
    chain = build_chain(current, now=datetime.now(timezone.utc).isoformat(timespec="microseconds"), add_rules=add_rules)
    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"

    def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result = ephemeral_call(token_config, socket, actor_ref=ACTOR, operation=operation, params=params)
        return result if isinstance(result, dict) else {"result": result}

    return {"chain": apply_chain(chain, apply), "research_budget": chain["research_budget"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=_live_state())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rehearse", type=Path, help="copy the Core here and apply the chain on the copy")
    mode.add_argument("--live", action="store_true", help="apply through the live writer as human:lumos")
    parser.add_argument("--add-auto-commit-rule", action="append", default=[],
                        help="list this research auto-commit rule in the policy (P9d-17b)")
    args = parser.parse_args(argv)
    rules = list(args.add_auto_commit_rule)
    result = (rehearse(args.state_dir, args.rehearse, add_rules=rules) if args.rehearse is not None
              else live(args.state_dir, add_rules=rules))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
