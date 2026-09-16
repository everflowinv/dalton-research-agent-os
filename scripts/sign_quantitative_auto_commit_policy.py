#!/usr/bin/env python3
"""WP-F: sign the two deterministic filed-number auto-commit rules, once.

This install holds ~5,000 numbers that are already verified against the exact
SEC rows they came from -- filed XBRL statement lines, the margins derived from
two lines of one filing, and the company-filed document figures ADR-0007
admitted -- and not one of them can enter ``claim_versions``, because the
Ledger's rule is that an automated commit needs a *named* rule in a policy the
owner has signed.  That is the correct rule and this script does not weaken it.
It does exactly one thing: publish the next governance policy version with

    policy.research_candidate_auto_commit.rules += [
        "research-auto-commit:mission-verified-figure:v1",
        "research-auto-commit:sec-statement-line:v1",
    ]

and rebind the constitution and mission to it, which is the same four-step
cascade ``publish_extraction_authority_chain.py`` already uses (a mission whose
constitution binds a stale policy cannot spend, and would be broken by a policy
publish that did not cascade).  Everything else in every record is byte
identical to its prior version.  No INSERT is hand-written anywhere: the policy
goes through ``DaltonStore.create_policy`` and, with ``--apply``, through the
live writer's ephemeral human principal exactly as every other governance
change does.

What the owner is signing, in one paragraph each:

``research-auto-commit:mission-verified-figure:v1`` -- a number published by
the company in its own document, whose digits and as-reported label were found
in the exact quoted span before the figure row was written and are re-checked
against that stored quote at admission.  Numbers spoken on an earnings call are
refused by grade and stay qualitative.

``research-auto-commit:sec-statement-line:v1`` -- a row of the filer's own XBRL
exhibit, identified by accession, statement, ordinal and concept, re-read out of
Core at admission; or a gross/operating margin computed as the ratio of two such
rows of one filing and one period, recomputed by the Ledger's numeric verifier
from the two filed values.  No model is involved at any point in either rule.

Usage
-----

    # read-only: what would change, and nothing else
    .venv/bin/python scripts/sign_quantitative_auto_commit_policy.py \
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core"

    # rehearse the publish on a copy of the live Core
    ... --rehearse /tmp/wpf-policy-rehearsal

    # publish for real, through the live writer
    ... --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dalton_core.research_auto_commit import (  # noqa: E402
    KNOWN_RULE_REFS,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
)
from scripts.publish_extraction_authority_chain import (  # noqa: E402
    BODY_FIELDS,
    CONSTITUTION_REF,
    MISSION_REF,
    _read_current,
    apply_chain,
)

RULES = (MISSION_VERIFIED_FIGURE_RULE_REF, SEC_STATEMENT_LINE_RULE_REF)
CHANGE_REASON = (
    "WP-F: list the two deterministic filed-number auto-commit rules "
    "(mission-verified-figure:v1, sec-statement-line:v1) so numbers already "
    "re-verified against the exact SEC rows they came from may enter the Ledger "
    "without per-item human review; no model is involved in either rule and "
    "every other rule in this policy is unchanged"
)


def _live_state() -> Path:
    return Path(os.path.expanduser(
        "~/Library/Application Support/Dalton/state/dalton-core"))


def build_policy_chain(current: dict[str, Any], *, now: str,
                       rules: tuple[str, ...] = RULES) -> dict[str, Any]:
    """The next policy version, plus the constitution and mission that bind it."""

    unknown = [rule for rule in rules if rule not in KNOWN_RULE_REFS]
    if unknown:
        raise SystemExit(
            f"refusing to sign a rule this build does not implement: {unknown}")
    policy_wire = current["policy"]
    policy_body = dict(policy_wire["policy"] if "policy" in policy_wire else policy_wire)
    auto = dict(policy_body.get("research_candidate_auto_commit") or {})
    held = list(auto.get("rules") or [])
    added = [rule for rule in rules if rule not in held]
    if not added:
        raise SystemExit(
            "the active policy already lists both filed-number rules; nothing to do")
    # ``enabled`` and ``max_records`` are carried through unchanged when they
    # are already there; a policy that never had the block gets the closed
    # shape the evaluator requires, with the smallest bound that works.
    policy_body["research_candidate_auto_commit"] = {
        "enabled": auto.get("enabled", True),
        "rules": held + added,
        "max_records": auto.get("max_records", 20),
    }
    version = int(str(current["policy_id"]).rsplit("-", 1)[1]) + 1
    constitution = current["constitution"]
    mission = current["mission"]
    return {
        "added": added,
        "held": held,
        "policy": {
            "policy": policy_body,
            "policy_version_id": f"policy-{version}",
            "version_number": version,
            "activate": True,
            "policy_ref": policy_wire.get("policy_ref", "commit-gate"),
            "effective_from": now,
            "effective_until": None,
            "prior_version_ref": current["policy_id"],
            "change_reason": CHANGE_REASON,
            "content_hash_value": None,
        },
        # The mandate carries no auto-commit rule, so it is never republished
        # by this change; ``apply_chain`` rebinds the current one.
        "mandate_needed": False,
        "constitution": {
            "constitution_ref": CONSTITUTION_REF,
            "industry_ref": constitution["industry_ref"],
            "title": constitution["title"],
            "bindings": json.loads(json.dumps(constitution["bindings"])),
            "method": json.loads(json.dumps(constitution["method"])),
            "version_id": (
                f"constitution-version:us-it-services:{int(constitution['version']) + 1}"),
            "prior_version_ref": constitution["id"],
            "idempotency_key": (
                f"{CONSTITUTION_REF}:{int(constitution['version']) + 1}:wpf-filed-number-rules"),
        },
        "mission": {
            "mission_ref": MISSION_REF,
            **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
            "version_id": (
                f"coverage-mission-version:us-it-services:{int(mission['version']) + 1}"),
            "prior_version_ref": mission["id"],
            "idempotency_key": (
                f"{MISSION_REF}:{int(mission['version']) + 1}:wpf-filed-number-rules"),
        },
    }


def _read_state(state_dir: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        return _read_current(connection)
    finally:
        connection.close()


def plan(state_dir: Path) -> dict[str, Any]:
    current = _read_state(state_dir)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    chain = build_policy_chain(current, now=now)
    body = chain["policy"]["policy"]
    return {
        "mode": "dry-run",
        "state_dir": str(state_dir),
        "active_policy": current["policy_id"],
        "active_rules": chain["held"],
        "rules_to_add": chain["added"],
        "next_policy": chain["policy"]["policy_version_id"],
        "next_rules": body["research_candidate_auto_commit"]["rules"],
        "next_constitution": chain["constitution"]["version_id"],
        "next_mission": chain["mission"]["version_id"],
        "change_reason": CHANGE_REASON,
        "diff": {
            "policy.research_candidate_auto_commit": {
                "before": current["policy"].get("policy", current["policy"]).get(
                    "research_candidate_auto_commit"),
                "after": body["research_candidate_auto_commit"],
            },
        },
        # Loud on purpose: the cascade is not optional and an owner who reads
        # only this line should still know three records are published.
        "publishes": [
            chain["policy"]["policy_version_id"],
            chain["constitution"]["version_id"],
            chain["mission"]["version_id"],
        ],
    }


def rehearse(state_dir: Path, target: Path, *, actor: str) -> dict[str, Any]:
    """Apply the cascade on a byte copy of the Core; the live one is untouched."""

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
        current = _read_current(store.connection)
        chain = build_policy_chain(
            current, now=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        agenda = AgendaStore(store)
        constitutions = ResearchConstitutionAuthority(store)
        missions = CoverageMissionAuthority(store)

        def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            values = dict(params)
            if operation == "create_policy":
                policy = values.pop("policy")
                return {"policy_version": store.create_policy(
                    policy, actor_ref=actor, **values)}
            if operation == "create_mandate":
                return agenda.create_mandate(
                    values.pop("mandate_ref"), actor_ref=actor, **values)
            if operation == "publish_research_constitution":
                return constitutions.publish_constitution(
                    values.pop("constitution_ref"), actor_ref=actor, **values)
            if operation == "create_coverage_mission":
                return missions.create_mission(
                    values.pop("mission_ref"), actor_ref=actor, **values)
            raise SystemExit(operation)

        result = apply_chain(chain, apply)
        active = store.active_policy_version().to_dict()
        return {
            "mode": "rehearsal", "target": str(target), "chain": result,
            "added": chain["added"],
            "active_rules": active["policy"]["research_candidate_auto_commit"]["rules"],
        }
    finally:
        store.close()


def live(state_dir: Path, *, actor: str) -> dict[str, Any]:
    from dalton_core.governance_cli import ephemeral_call

    current = _read_state(state_dir)
    chain = build_policy_chain(
        current, now=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"

    def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result = ephemeral_call(token_config, socket, actor_ref=actor,
                                operation=operation, params=params)
        return result if isinstance(result, dict) else {"result": result}

    return {"mode": "live", "added": chain["added"], "chain": apply_chain(chain, apply)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=_live_state())
    parser.add_argument("--apply", action="store_true",
                        help="publish through the live writer as the given human actor")
    parser.add_argument("--rehearse", type=Path,
                        help="copy the Core here and publish the cascade on the copy")
    parser.add_argument("--actor", default=None,
                        help="human:<name> principal that signs this policy version")
    args = parser.parse_args(argv)
    state_dir = Path(os.path.expanduser(str(args.state_dir))).resolve()
    if args.apply and args.rehearse:
        raise SystemExit("--apply and --rehearse are different runs; pick one")
    if (args.apply or args.rehearse) and not (args.actor or "").startswith("human:"):
        raise SystemExit("--actor human:<name> is required to sign a policy version")
    if args.apply:
        result = live(state_dir, actor=args.actor)
    elif args.rehearse:
        result = rehearse(state_dir, Path(args.rehearse).expanduser().resolve(),
                          actor=args.actor)
    else:
        result = plan(state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - an owner-run script
    sys.exit(main())
