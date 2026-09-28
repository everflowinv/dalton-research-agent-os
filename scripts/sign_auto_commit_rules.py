#!/usr/bin/env python3
"""Sign research auto-commit rules into any environment's active policy.

``scripts/sign_quantitative_auto_commit_policy.py`` solved this once, for one
install: the legacy Core, the ``coverage-mission:us-it-services`` mission and
the two filed-number rules, all hardwired.  The same wall is hit by every
environment that was created after its policy was written -- a workspace's
first governance policy carries no ``research_candidate_auto_commit`` block at
all, so ``DocumentExtractionService._admit_complete_reviews`` holds every
completed review with "active governance policy does not list
research-auto-commit:mission-document-qualitative:v1" and the reviews sit
there, finished and unusable.

This script is the same operation with nothing hardwired.  Point it at a state
directory; it reads that environment's active mission from
``coverage_mission_pointer``, follows the mission's own bindings to its
constitution and mandate, and publishes the next governance policy version with

    policy.research_candidate_auto_commit.rules += [<the rules you named>]

then rebinds the constitution and the mission to it -- the same four-step
cascade ``publish_extraction_authority_chain.apply_chain`` performs, because a
mission whose constitution binds a stale policy cannot spend and would be
broken by a policy publish that did not cascade.  Everything else in every
record is byte identical to its prior version.  No INSERT is hand-written:
the publish goes through ``DaltonStore``/the authorities, and with ``--apply``
through that environment's writer, as its ephemeral human principal.

Rules
-----

``--rule`` is repeatable and is checked against
``research_auto_commit.KNOWN_RULE_REFS``; this build refuses to sign a name it
does not implement.  With no ``--rule`` the default is every *signable* known
rule that the active policy does not list yet.  ``research-auto-commit:
sec-public-filing-count:v1`` is not in that default and cannot be combined
with anything: the evaluator accepts it only as the whole rule set, so it is
signed on its own or not at all.

Already-listed rules are dropped (``--only-missing``, the default), which makes
a second run a no-op rather than an error.  ``--no-only-missing`` refuses
instead, for a run that is meant to be adding something.

Usage
-----

    # read-only: the exact before/after and the cascade, nothing else
    .venv/bin/python scripts/sign_auto_commit_rules.py --state-dir <state>

    # only the qualitative document rule, rehearsed on a copy of that Core
    ... --rule research-auto-commit:mission-document-qualitative:v1 \
        --rehearse /tmp/rule-rehearsal --actor human:lumos

    # publish for real, through that environment's writer
    ... --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dalton_core.research_auto_commit import (  # noqa: E402
    COMPANY_FACTS_ANNUAL_RULE_REF,
    COMPANY_FACTS_RULE_REF,
    DOCUMENT_QUALITATIVE_RULE_REF,
    KNOWN_RULE_REFS,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    RULE_REF as FILING_COUNT_RULE_REF,
    SEC_FY_MINUS_9M_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
)
from dalton_core.store import content_hash  # noqa: E402
from scripts.publish_extraction_authority_chain import BODY_FIELDS, apply_chain  # noqa: E402

#: Every rule that may share a policy with the others, in the order a policy
#: that signs all of them ends up carrying them.  ``sec-public-filing-count``
#: is deliberately absent: see ``_validate_rule_set``.
SIGNABLE_RULE_REFS: tuple[str, ...] = (
    COMPANY_FACTS_RULE_REF,
    COMPANY_FACTS_ANNUAL_RULE_REF,
    DOCUMENT_QUALITATIVE_RULE_REF,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
    SEC_FY_MINUS_9M_RULE_REF,
)
#: The closed shape ``research_auto_commit._policy_rule`` requires of a policy
#: that never carried the block.  ``max_records`` is a per-window bound, not a
#: budget; 20 is what every signed policy in this codebase uses.
DEFAULT_MAX_RECORDS = 20
_VERSION_SUFFIX = re.compile(r"^(?P<prefix>.*?)(?P<number>[0-9]+)$")


class PlanError(SystemExit):
    """A refusal that is the owner's to resolve, not a crash."""


_IDENTITY_SEGMENT = re.compile(r"^[0-9a-f]{16,64}$")


def _next_version_id(current_id: str, version: int) -> str:
    """``…:15`` for version 15 becomes ``…:16``; the prefix is never guessed.

    A workspace's first mission version is named by its proposal hash
    (``coverage-mission-version:<slug>:<24 hex>``), not by a number.  Reading
    trailing digits out of a hash either refused (ws-7d's ``…a5c``) or, for a
    hash that happens to end in digits, silently minted a wrong id; the hash
    segment is replaced by the version, the name the writer's own cascade
    gives every later version (``coverage-mission-version:<slug>:2``).
    """

    prefix, _, last = current_id.rpartition(":")
    if prefix and _IDENTITY_SEGMENT.fullmatch(last):
        return f"{prefix}:{version}"
    match = _VERSION_SUFFIX.match(current_id)
    if match is None:
        raise PlanError(f"cannot derive the next version id from {current_id!r}")
    return f"{match.group('prefix')}{version}"


def _validate_rule_set(rules: list[str]) -> None:
    """Mirror ``research_auto_commit._policy_rule`` before anything is written.

    Publishing a rule list the evaluator then rejects would leave the mission
    worse off than before: the block would exist, be malformed, and every
    candidate -- including the ones that used to pass -- would be refused with
    "active governance policy rule set is not supported".
    """

    if not rules:
        raise PlanError("a policy rule set cannot be empty")
    if len(set(rules)) != len(rules):
        raise PlanError(f"the resulting rule set repeats a rule: {rules}")
    unknown = [rule for rule in rules if rule not in KNOWN_RULE_REFS]
    if unknown:
        raise PlanError(
            "refusing to sign a rule this build does not implement: "
            f"{unknown}; known rules are {sorted(KNOWN_RULE_REFS)}")
    if FILING_COUNT_RULE_REF in rules and rules != [FILING_COUNT_RULE_REF]:
        raise PlanError(
            f"{FILING_COUNT_RULE_REF} is only accepted as the entire rule set; "
            "sign it alone or sign the others without it")


def read_current(connection: sqlite3.Connection, *,
                 mission_ref: str | None = None) -> dict[str, Any]:
    """The active mission of this environment and everything it binds."""

    connection.row_factory = sqlite3.Row
    pointers = connection.execute(
        "SELECT mission_ref, mission_version_id FROM coverage_mission_pointer"
    ).fetchall()
    if not pointers:
        raise PlanError("this Core has no active coverage mission")
    if mission_ref is not None:
        pointers = [row for row in pointers if row["mission_ref"] == mission_ref]
        if not pointers:
            raise PlanError(f"no active mission named {mission_ref}")
    if len(pointers) > 1:
        raise PlanError(
            "this Core has more than one active mission; name one with "
            f"--mission-ref: {sorted(row['mission_ref'] for row in pointers)}")
    pointer = pointers[0]
    mission = json.loads(connection.execute(
        "SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?",
        (pointer["mission_version_id"],),
    ).fetchone()["record_json"])
    constitution = json.loads(connection.execute(
        "SELECT record_json FROM research_constitution_versions WHERE constitution_version_id=?",
        (mission["bindings"]["constitution_version"]["ref"],),
    ).fetchone()["record_json"])
    mandate = json.loads(connection.execute(
        "SELECT record_json FROM mandate_versions WHERE version_id=?",
        (mission["bindings"]["mandate_version"]["ref"],),
    ).fetchone()["record_json"])
    # The *active* policy, by its pointer.  The newest row is usually the same
    # record, but a policy published without ``activate`` is not the one the
    # evaluator reads, and deriving the next version from it would fork the
    # chain.
    policy_row = connection.execute(
        "SELECT v.policy_version_id, v.version_number, v.policy_ref, v.policy_json "
        "FROM governance_policy_pointer p JOIN governance_policy_versions v "
        "ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1"
    ).fetchone()
    if policy_row is None:
        raise PlanError("this Core has no active governance policy")
    return {
        "mission_ref": pointer["mission_ref"],
        "mission": mission,
        "constitution": constitution,
        "mandate": mandate,
        "policy_id": policy_row["policy_version_id"],
        "policy_version_number": int(policy_row["version_number"]),
        "policy_ref": policy_row["policy_ref"],
        "policy": json.loads(policy_row["policy_json"]),
    }


def change_reason(added: list[str]) -> str:
    return (
        "list the research auto-commit rule(s) "
        + ", ".join(added)
        + " in policy.research_candidate_auto_commit.rules so candidates that "
        "already match them may enter the Ledger without per-item human review; "
        "every other rule in this policy is unchanged and the constitution and "
        "mission are rebound to this policy version only"
    )


def build_rule_chain(current: dict[str, Any], *, now: str,
                     rules: list[str]) -> dict[str, Any]:
    """The next policy version, plus the constitution and mission that bind it."""

    unknown = [rule for rule in rules if rule not in KNOWN_RULE_REFS]
    if unknown:
        raise PlanError(
            "refusing to sign a rule this build does not implement: "
            f"{unknown}; known rules are {sorted(KNOWN_RULE_REFS)}")
    policy_wire = current["policy"]
    policy_body = dict(policy_wire["policy"] if "policy" in policy_wire else policy_wire)
    before = policy_body.get("research_candidate_auto_commit")
    auto = dict(before or {})
    held = list(auto.get("rules") or [])
    added = [rule for rule in rules if rule not in held]
    _validate_rule_set(held + added)
    # ``enabled`` and ``max_records`` are carried through unchanged when they
    # are already there; a policy that never had the block gets the closed
    # shape the evaluator requires, with the bound every signed policy uses.
    after = {
        "enabled": auto.get("enabled", True),
        "rules": held + added,
        "max_records": auto.get("max_records", DEFAULT_MAX_RECORDS),
    }
    policy_body["research_candidate_auto_commit"] = after
    version = current["policy_version_number"] + 1
    constitution = current["constitution"]
    mission = current["mission"]
    constitution_version = int(constitution["version"]) + 1
    mission_version = int(mission["version"]) + 1
    # One key per (record, version, exact rule set): a retry of this run is the
    # same publish, a different run adding different rules is not.
    stamp = content_hash(sorted(added))[:12]
    return {
        "added": added,
        "held": held,
        "before": before,
        "after": after,
        "policy": {
            "policy": policy_body,
            "policy_version_id": _next_version_id(current["policy_id"], version),
            "version_number": version,
            "activate": True,
            "policy_ref": current["policy_ref"],
            "effective_from": now,
            "effective_until": None,
            "prior_version_ref": current["policy_id"],
            "change_reason": change_reason(added),
            "content_hash_value": None,
        },
        # No auto-commit rule lives in the mandate, so it is never republished
        # by this change; ``apply_chain`` rebinds the current one.
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
                f":auto-commit-rules:{stamp}"),
        },
        "mission": {
            "mission_ref": mission["mission_ref"],
            **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
            "version_id": _next_version_id(mission["id"], mission_version),
            "prior_version_ref": mission["id"],
            "idempotency_key": (
                f"{mission['mission_ref']}:{mission_version}"
                f":auto-commit-rules:{stamp}"),
        },
    }


def requested_rules(current: dict[str, Any], rules: list[str], *,
                    only_missing: bool = True) -> dict[str, Any]:
    """Split what was asked for into what is already signed and what is not."""

    auto = current["policy"].get("policy", current["policy"]).get(
        "research_candidate_auto_commit") or {}
    held = list(auto.get("rules") or [])
    wanted = list(rules) if rules else [
        rule for rule in SIGNABLE_RULE_REFS if rule not in held]
    unknown = [rule for rule in wanted if rule not in KNOWN_RULE_REFS]
    if unknown:
        raise PlanError(
            "refusing to sign a rule this build does not implement: "
            f"{unknown}; known rules are {sorted(KNOWN_RULE_REFS)}")
    listed = [rule for rule in wanted if rule in held]
    missing = [rule for rule in wanted if rule not in held]
    if listed and not only_missing:
        raise PlanError(
            f"the active policy already lists {listed}; drop them from --rule "
            "or keep the default --only-missing")
    return {"held": held, "requested": wanted, "already_listed": listed,
            "to_add": missing}


def _read_state(state_dir: Path, *, mission_ref: str | None) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        return read_current(connection, mission_ref=mission_ref)
    finally:
        connection.close()


def plan(state_dir: Path, *, rules: list[str], mission_ref: str | None = None,
         only_missing: bool = True) -> dict[str, Any]:
    """Read-only: exactly what would be published, and nothing else."""

    current = _read_state(state_dir, mission_ref=mission_ref)
    split = requested_rules(current, rules, only_missing=only_missing)
    auto = current["policy"].get("policy", current["policy"]).get(
        "research_candidate_auto_commit")
    base = {
        "mode": "dry-run",
        "state_dir": str(state_dir),
        "mission_ref": current["mission_ref"],
        "active_mission": current["mission"]["id"],
        "active_constitution": current["constitution"]["id"],
        "active_mandate": current["mandate"]["id"],
        "active_policy": current["policy_id"],
        "active_rules": split["held"],
        "requested_rules": split["requested"],
        "already_listed": split["already_listed"],
        "rules_to_add": split["to_add"],
    }
    if not split["to_add"]:
        # Idempotent on purpose: a second run of an applied change is a report,
        # not a failure.  ``--apply`` still refuses, in ``live``.
        return {**base, "status": "already-signed", "publishes": [],
                "diff": {"policy.research_candidate_auto_commit":
                         {"before": auto, "after": auto}}}
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    chain = build_rule_chain(current, now=now, rules=split["to_add"])
    return {
        **base,
        "status": "would-publish",
        "next_policy": chain["policy"]["policy_version_id"],
        "next_constitution": chain["constitution"]["version_id"],
        "next_mission": chain["mission"]["version_id"],
        "change_reason": chain["policy"]["change_reason"],
        "diff": {"policy.research_candidate_auto_commit":
                 {"before": chain["before"], "after": chain["after"]}},
        # Loud on purpose: the cascade is not optional and an owner who reads
        # only this line should still know three records are published.
        "publishes": [
            chain["policy"]["policy_version_id"],
            chain["constitution"]["version_id"],
            chain["mission"]["version_id"],
        ],
    }


def _chain_to_apply(current: dict[str, Any], *, rules: list[str],
                    only_missing: bool) -> dict[str, Any]:
    split = requested_rules(current, rules, only_missing=only_missing)
    if not split["to_add"]:
        raise PlanError(
            "the active policy already lists every rule requested; nothing to do")
    return build_rule_chain(
        current, now=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        rules=split["to_add"])


def rehearse(state_dir: Path, target: Path, *, actor: str, rules: list[str],
             mission_ref: str | None = None,
             only_missing: bool = True) -> dict[str, Any]:
    """Apply the cascade on a byte copy of the Core; the live one is untouched."""

    from dalton_core.agenda import AgendaStore
    from dalton_core.coverage_mission import CoverageMissionAuthority
    from dalton_core.research_auto_commit import policy_lists_document_rule
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
        chain = _chain_to_apply(current, rules=rules, only_missing=only_missing)
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
            raise PlanError(operation)

        result = apply_chain(chain, apply)
        active = store.active_policy_version().to_dict()
        mission = missions.active_mission(current["mission_ref"])
        return {
            "mode": "rehearsal",
            "target": str(target),
            "mission_ref": current["mission_ref"],
            "added": chain["added"],
            "chain": result,
            "active_rules": active["policy"]["research_candidate_auto_commit"]["rules"],
            # The two questions the rehearsal exists to answer: does the
            # evaluator accept the rule set, and does the mission bind it.
            "policy_lists_document_rule": policy_lists_document_rule(active),
            "mission_binds_new_constitution": (
                mission["bindings"]["constitution_version"]["ref"]
                == result["constitution"]["ref"]),
        }
    finally:
        store.close()


def live(state_dir: Path, *, actor: str, rules: list[str],
         mission_ref: str | None = None,
         only_missing: bool = True) -> dict[str, Any]:
    """Publish through this environment's writer, as its ephemeral human."""

    from dalton_core.governance_cli import ephemeral_call

    current = _read_state(state_dir, mission_ref=mission_ref)
    chain = _chain_to_apply(current, rules=rules, only_missing=only_missing)
    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"
    if not token_config.is_file():
        raise PlanError(f"no writer token config at {token_config}")

    def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result = ephemeral_call(token_config, socket, actor_ref=actor,
                                operation=operation, params=params)
        return result if isinstance(result, dict) else {"result": result}

    return {"mode": "live", "mission_ref": current["mission_ref"],
            "added": chain["added"], "chain": apply_chain(chain, apply)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="the dalton-core state directory of any environment")
    parser.add_argument("--mission-ref", default=None,
                        help="which active mission to rebind; only needed when "
                             "the Core has more than one")
    parser.add_argument("--rule", action="append", default=[], dest="rules",
                        help="research auto-commit rule ref to sign (repeatable); "
                             "default: every signable rule the policy lacks")
    parser.add_argument("--only-missing", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="skip rules the policy already lists (default); "
                             "--no-only-missing refuses instead")
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
    common = {"rules": list(args.rules), "mission_ref": args.mission_ref,
              "only_missing": args.only_missing}
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
