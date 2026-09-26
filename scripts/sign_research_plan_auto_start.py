#!/usr/bin/env python3
"""Sign the SEC company-facts research-plan auto-start rules into a policy.

ws-7d's active policy (``policy-4``) was written without a
``research_plan_auto_start`` block.  The SEC company-facts lane checks for it
before it does anything (``sec_company_facts_lane.check_core_governance_rules``)
and refused every run with

    lane precondition failed: active Core governance policy 'policy-4' does not
    authorize the SEC company-facts lane ... research_plan_auto_start must be
    {enabled: true, rules: ['research-plan-auto-start:sec-public-company-facts:v1', ...]}

while the quarterly coordinator kept queuing, so each refusal was an attempt
spent.  The legacy install signed the same block long ago (``policy-18``):

    research_plan_auto_start = {enabled: true, rules: [
        research-plan-auto-start:sec-public-company-facts:v1,
        research-plan-auto-start:sec-public-company-facts-annual:v1]}

This script publishes the next policy version with exactly the missing part
of that block -- the rules above that the active policy does not list yet --
and nothing else, then rebinds the constitution and the mission to it: the
same cascade ``sign_auto_commit_rules.py`` and ``switch_weekly_brief_plan.py``
perform, because a mission whose constitution binds a stale policy cannot
spend.  No INSERT is hand-written: rehearsal goes through ``DaltonStore`` and
the authorities, ``--apply`` through that environment's writer as its
ephemeral human principal (``--actor``).

``research_candidate_auto_commit`` is not touched: ws-7d already lists the
company-facts growth rule the lane needs there.  Nothing that sets how much a
mission may spend moves either.

What the rules allow
--------------------

A research plan whose frozen scope is the packaged SEC company-facts read
(10-Q, and with the annual rule 10-K) may start without a per-plan human
approval.  That read is a public SEC API call with no model in it.  Pass
``--rule research-plan-auto-start:sec-public-company-facts:v1`` to sign only
the quarterly rule, which is all the lane's precondition requires.

Usage
-----

    PY=.venv/bin/python
    W=/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core

    # read-only: before/after, the three records that would be published
    $PY scripts/sign_research_plan_auto_start.py --state-dir "$W"

    # the whole cascade on a copy of the Core
    $PY scripts/sign_research_plan_auto_start.py --state-dir "$W" \\
        --rehearse /tmp/plan-auto-start-rehearsal --actor human:owner

    # for real, through that environment's writer
    $PY scripts/sign_research_plan_auto_start.py --state-dir "$W" --apply --actor human:owner

A second run after ``--apply`` reports ``already-signed``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dalton_core.research_plan import (  # noqa: E402
    PLAN_AUTO_START_RULE_REFS,
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
)
from dalton_core.sec_company_facts_lane import (  # noqa: E402
    LanePreconditionError,
    check_core_governance_rules,
)
from dalton_core.store import content_hash  # noqa: E402
from scripts.publish_extraction_authority_chain import BODY_FIELDS, apply_chain  # noqa: E402
from scripts.sign_auto_commit_rules import (  # noqa: E402
    PlanError,
    _next_version_id,
    read_current,
)

#: The SEC company-facts rules legacy ``policy-18`` lists, in its order.  The
#: list-filings rule (``PLAN_AUTO_START_RULE_REF``) is not the company-facts
#: lane's and is not signed by default.
SEC_COMPANY_FACTS_AUTO_START_RULES: tuple[str, ...] = (
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
)
BLOCK = "research_plan_auto_start"


def _policy_body(policy_wire: dict[str, Any]) -> dict[str, Any]:
    return dict(policy_wire["policy"] if "policy" in policy_wire else policy_wire)


def validate_block(block: Any) -> None:
    """Mirror ``ResearchPlanAuthority.authorize_plan_by_policy`` before writing.

    A block the plan authority then rejects would be worse than none: every
    plan -- including the ones that pass today -- would be refused.
    """

    rules = block.get("rules") if isinstance(block, dict) else None
    if (
        not isinstance(block, dict)
        or set(block) != {"enabled", "rules"}
        or block["enabled"] is not True
        or not isinstance(rules, list)
        or not rules
        or len(set(rules)) != len(rules)
        or any(rule not in PLAN_AUTO_START_RULE_REFS for rule in rules)
    ):
        raise PlanError(f"the resulting {BLOCK} block would not be accepted: {block}")


def lane_precondition(policy_id: str, body: dict[str, Any]) -> str:
    """What the SEC lane's own precondition says about this policy body."""

    core = type("PolicyOnly", (), {"active_policy": lambda _self: {
        "policy_version_id": policy_id, "policy": body}})()
    try:
        check_core_governance_rules(core)
    except LanePreconditionError as exc:
        return f"refused: {exc}"
    return "ok"


def requested_rules(current: dict[str, Any], rules: list[str]) -> dict[str, Any]:
    body = _policy_body(current["policy"])
    before = body.get(BLOCK)
    if before is not None and not isinstance(before, dict):
        raise PlanError(f"the active policy's {BLOCK} is not an object: {before!r}")
    if isinstance(before, dict) and before.get("enabled") is False:
        # Somebody switched it off on purpose; turning it back on is theirs.
        raise PlanError(
            f"the active policy has {BLOCK}.enabled = false; this script only adds "
            "missing rules and will not re-enable a block someone disabled")
    held = list((before or {}).get("rules") or [])
    wanted = list(rules) if rules else list(SEC_COMPANY_FACTS_AUTO_START_RULES)
    unknown = [rule for rule in wanted if rule not in PLAN_AUTO_START_RULE_REFS]
    if unknown:
        raise PlanError(
            "refusing to sign a rule this build does not implement: "
            f"{unknown}; known rules are {sorted(PLAN_AUTO_START_RULE_REFS)}")
    return {"before": before, "held": held,
            "requested": wanted,
            "already_listed": [rule for rule in wanted if rule in held],
            "to_add": [rule for rule in wanted if rule not in held]}


def change_reason(added: list[str]) -> str:
    return (
        f"list the research-plan auto-start rule(s) {', '.join(added)} in "
        f"policy.{BLOCK}.rules so the SEC company-facts lane's packaged public "
        "read may start without per-plan approval, as on the legacy install "
        "(policy-18); every other rule in this policy is unchanged and the "
        "constitution and mission are rebound to this policy version only"
    )


def build_chain(current: dict[str, Any], *, now: str, rules: list[str]) -> dict[str, Any]:
    """The next policy version, plus the constitution and mission that bind it."""

    split = requested_rules(current, rules)
    added = split["to_add"]
    if not added:
        raise PlanError(f"the active policy already lists every rule requested in {BLOCK}")
    body = _policy_body(current["policy"])
    after = {"enabled": True, "rules": split["held"] + added}
    validate_block(after)
    body[BLOCK] = after
    version = current["policy_version_number"] + 1
    constitution = current["constitution"]
    mission = current["mission"]
    constitution_version = int(constitution["version"]) + 1
    mission_version = int(mission["version"]) + 1
    stamp = content_hash(sorted(added))[:12]
    policy_id = _next_version_id(current["policy_id"], version)
    return {
        "added": added,
        "held": split["held"],
        "before": split["before"],
        "after": after,
        "lane_precondition_after": lane_precondition(policy_id, body),
        "policy": {
            "policy": body,
            "policy_version_id": policy_id,
            "version_number": version,
            "activate": True,
            "policy_ref": current["policy_ref"],
            "effective_from": now,
            "effective_until": None,
            "prior_version_ref": current["policy_id"],
            "change_reason": change_reason(added),
            "content_hash_value": None,
        },
        "mandate_needed": False,
        "constitution": {
            "constitution_ref": constitution["constitution_ref"],
            "industry_ref": constitution["industry_ref"],
            "title": constitution["title"],
            "bindings": json.loads(json.dumps(constitution["bindings"])),
            "method": json.loads(json.dumps(constitution["method"])),
            "version_id": _next_version_id(constitution["id"], constitution_version),
            "prior_version_ref": constitution["id"],
            "idempotency_key": (
                f"{constitution['constitution_ref']}:{constitution_version}"
                f":plan-auto-start:{stamp}"),
        },
        "mission": {
            "mission_ref": mission["mission_ref"],
            **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
            "version_id": _next_version_id(mission["id"], mission_version),
            "prior_version_ref": mission["id"],
            "idempotency_key": (
                f"{mission['mission_ref']}:{mission_version}:plan-auto-start:{stamp}"),
        },
    }


def _read_state(state_dir: Path, *, mission_ref: str | None) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        return read_current(connection, mission_ref=mission_ref)
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def plan(state_dir: Path, *, rules: list[str], mission_ref: str | None = None) -> dict[str, Any]:
    """Read-only: exactly what would be published, and nothing else."""

    current = _read_state(state_dir, mission_ref=mission_ref)
    split = requested_rules(current, rules)
    base = {
        "mode": "dry-run",
        "state_dir": str(state_dir),
        "mission_ref": current["mission_ref"],
        "active_mission": current["mission"]["id"],
        "active_constitution": current["constitution"]["id"],
        "active_mandate": current["mandate"]["id"],
        "active_policy": current["policy_id"],
        "lane_precondition_now": lane_precondition(
            current["policy_id"], _policy_body(current["policy"])),
        "active_rules": split["held"],
        "requested_rules": split["requested"],
        "already_listed": split["already_listed"],
        "rules_to_add": split["to_add"],
    }
    if not split["to_add"]:
        return {**base, "status": "already-signed", "publishes": [],
                "diff": {f"policy.{BLOCK}": {"before": split["before"],
                                             "after": split["before"]}}}
    chain = build_chain(current, now=_now(), rules=rules)
    return {
        **base,
        "status": "would-publish",
        "next_policy": chain["policy"]["policy_version_id"],
        "next_constitution": chain["constitution"]["version_id"],
        "next_mission": chain["mission"]["version_id"],
        "change_reason": chain["policy"]["change_reason"],
        "lane_precondition_after": chain["lane_precondition_after"],
        "diff": {f"policy.{BLOCK}": {"before": chain["before"], "after": chain["after"]}},
        "publishes": [
            chain["policy"]["policy_version_id"],
            chain["constitution"]["version_id"],
            chain["mission"]["version_id"],
        ],
    }


def rehearse(state_dir: Path, target: Path, *, actor: str, rules: list[str],
             mission_ref: str | None = None) -> dict[str, Any]:
    """Apply the cascade on a copy of the Core; the live one is only read."""

    from dalton_core.agenda import AgendaStore
    from dalton_core.coverage_mission import CoverageMissionAuthority
    from dalton_core.research_constitution import ResearchConstitutionAuthority
    from dalton_core.store import DaltonStore

    target.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target / "core.sqlite"))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    store = DaltonStore(str(target / "core.sqlite"))
    try:
        current = read_current(store.connection, mission_ref=mission_ref)
        chain = build_chain(current, now=_now(), rules=rules)
        agenda = AgendaStore(store)
        constitutions = ResearchConstitutionAuthority(store)
        missions = CoverageMissionAuthority(store)

        def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            values = dict(params)
            if operation == "create_policy":
                policy = values.pop("policy")
                return {"policy_version": store.create_policy(policy, actor_ref=actor, **values)}
            if operation == "create_mandate":
                return agenda.create_mandate(values.pop("mandate_ref"), actor_ref=actor, **values)
            if operation == "publish_research_constitution":
                return constitutions.publish_constitution(
                    values.pop("constitution_ref"), actor_ref=actor, **values)
            if operation == "create_coverage_mission":
                return missions.create_mission(
                    values.pop("mission_ref"), actor_ref=actor, **values)
            raise PlanError(operation)

        result = apply_chain(chain, apply)
        mission = missions.active_mission(current["mission_ref"])
        try:
            check_core_governance_rules(store)
            precondition = "ok"
        except LanePreconditionError as exc:
            precondition = f"refused: {exc}"
        # The SEC grant each company would get under the new mission version:
        # the drain re-authorizes every dispatch against it.
        grants: dict[str, str] = {}
        for member in mission["universe"]:
            try:
                missions.authorize_sec_lane(
                    company_ref=member["company_ref"], ticker=member["ticker"],
                    actor_ref=mission["autonomy"]["automation_principal"],
                    mission_version_ref=mission["id"],
                    mission_version_hash=mission["content_hash"])
                grants[member["ticker"]] = "ok"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                grants[member["ticker"]] = f"{type(exc).__name__}: {exc}"
        return {
            "mode": "rehearsal",
            "target": str(target),
            "mission_ref": current["mission_ref"],
            "added": chain["added"],
            "chain": result,
            f"active_{BLOCK}": store.active_policy()["policy"].get(BLOCK),
            "lane_precondition_after": precondition,
            "sec_lane_grants": grants,
            "mission_binds_new_constitution": (
                mission["bindings"]["constitution_version"]["ref"]
                == result["constitution"]["ref"]),
        }
    finally:
        store.close()


def live(state_dir: Path, *, actor: str, rules: list[str],
         mission_ref: str | None = None) -> dict[str, Any]:
    """Publish through this environment's writer, as its ephemeral human."""

    from dalton_core.governance_cli import ephemeral_call

    current = _read_state(state_dir, mission_ref=mission_ref)
    chain = build_chain(current, now=_now(), rules=rules)
    if chain["lane_precondition_after"] != "ok":
        raise PlanError(f"refusing: the new policy would still fail the lane "
                        f"precondition: {chain['lane_precondition_after']}")
    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"
    if not token_config.is_file():
        raise PlanError(f"no writer token config at {token_config}")

    def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result = ephemeral_call(token_config, socket, actor_ref=actor,
                                operation=operation, params=params)
        return result if isinstance(result, dict) else {"result": result}

    result = apply_chain(chain, apply)
    after = _read_state(state_dir, mission_ref=mission_ref)
    return {"mode": "live", "mission_ref": current["mission_ref"],
            "added": chain["added"], "chain": result,
            "lane_precondition_after": lane_precondition(
                after["policy_id"], _policy_body(after["policy"]))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="the dalton-core state directory of any environment")
    parser.add_argument("--mission-ref", default=None,
                        help="which active mission to rebind; only needed when "
                             "the Core has more than one")
    parser.add_argument("--rule", action="append", default=[], dest="rules",
                        help="research-plan auto-start rule ref to sign (repeatable); "
                             "default: the SEC company-facts rules legacy policy-18 lists")
    parser.add_argument("--apply", action="store_true",
                        help="publish through this environment's writer as --actor")
    parser.add_argument("--rehearse", type=Path,
                        help="copy the Core here and publish the cascade on the copy")
    parser.add_argument("--actor", default=None,
                        help="human:<owner> principal that signs this policy version")
    args = parser.parse_args(argv)
    state_dir = Path(os.path.expanduser(str(args.state_dir))).resolve()
    if args.apply and args.rehearse:
        raise PlanError("--apply and --rehearse are different runs; pick one")
    if (args.apply or args.rehearse) and not (args.actor or "").startswith("human:"):
        raise PlanError("--actor human:<owner> is required to sign a policy version")
    if not (state_dir / "core.sqlite").is_file():
        raise PlanError(f"no Core at {state_dir / 'core.sqlite'}")
    common = {"rules": list(args.rules), "mission_ref": args.mission_ref}
    if args.apply:
        result = live(state_dir, actor=args.actor, **common)
    elif args.rehearse:
        result = rehearse(state_dir, Path(args.rehearse).expanduser().resolve(),
                          actor=args.actor, **common)
    else:
        result = plan(state_dir, **common)
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - an owner-run script
    sys.exit(main())
