#!/usr/bin/env python3
"""Make every research environment's model configuration the legacy one.

The owner's instruction of 2026-09-17 is that the model configuration is the
same in every environment on this Mac, and that today the configuration they
all have to match is the **legacy (IT services) environment's** -- that is the
one whose chains were repaired, whose deliverable tier was split, and whose
routing the owner has actually been reading.  The workspaces were forked from
older policy content and have been drifting ever since.

``model_routing_sync`` keeps them together from now on, by fanning a save out
to every environment.  This script closes the gap that opened *before* that
existed: it reads the legacy environment's current selections and publishes the
same ones into every workspace that differs.

It publishes through the same door the cockpit's model page uses -- the
``set_model_selection`` writer operation, through each environment's own writer
socket and token config -- so every environment still appends its own immutable
routing-policy version in its own lineage, with the owner as the actor, and
rollback is republishing the previous chain.  Nothing writes a row by hand and
nothing writes another environment's database.

Idempotent: an environment already on the legacy chains is planned as
``aligned`` and nothing is published.  Safe while the services are running,
because it talks to the writers rather than around them.

    # look first -- reads only, changes nothing
    scripts/align_model_routing.py

    # then apply, through each live writer, as the owner
    scripts/align_model_routing.py --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.model_routing_sync import (  # noqa: E402
    LEGACY_ENVIRONMENT_ID,
    Environment,
    ModelRoutingSyncError,
    current_purpose_overrides,
    current_tier_chains,
    host_environments,
    selection_params,
)

DEFAULT_MANAGER_CONFIG = "~/.dalton/manager.json"
DEFAULT_ACTOR = "human:lumos"


class AlignmentError(RuntimeError):
    """The alignment cannot be planned or applied as asked."""


# ---------------------------------------------------------------------------
# What "the legacy configuration" is, when the legacy environment holds several
# policy lineages.


def _agreed(
    values: Mapping[str, Any], subject: str,
) -> tuple[Any | None, list[str]]:
    """The one value every policy agrees on, or nothing and a complaint.

    An environment has several policy lineages -- each lane pins its own -- and
    "the legacy configuration" only means something if they say the same
    thing.  Where they do not, this refuses to pick a winner: copying one
    lineage's chain over the others would be this script inventing a decision
    the owner never made.
    """

    distinct: list[Any] = []
    for value in values.values():
        if value not in distinct:
            distinct.append(value)
    if not distinct:
        return None, [f"{subject}：源环境没有这一项"]
    if len(distinct) > 1:
        rendered = " / ".join(json.dumps(item, ensure_ascii=False, sort_keys=True)
                              for item in distinct)
        return None, [f"{subject}：源环境内部就不一致（{rendered}），需要你先决定用哪一条"]
    return distinct[0], []


def source_selections(environment: Environment) -> dict[str, Any]:
    """Every tier chain and per-stage override the source environment holds."""

    tier_chains = current_tier_chains(environment.router_db)
    overrides = current_purpose_overrides(environment.router_db)
    tiers: dict[str, list[str]] = {}
    purposes: dict[str, list[str]] = {}
    conflicts: list[str] = []
    for tier in sorted({name for held in tier_chains.values() for name in held}):
        # A lineage that does not declare this tier is not disagreeing about
        # it; it simply has no chain there. Only the lineages that hold one
        # have to agree, or there is nothing to copy.
        declared = {policy_id: held[tier] for policy_id, held in tier_chains.items()
                    if held.get(tier)}
        chain, complaints = _agreed(declared, f"类别「{tier}」")
        conflicts.extend(complaints)
        if chain:
            tiers[tier] = list(chain)
    for purpose in sorted({name for held in overrides.values() for name in held}):
        entries = {
            policy_id: held.get(purpose) for policy_id, held in overrides.items()
        }
        if any(entry is None for entry in entries.values()):
            # A stage pinned in one lineage and not in another is not a
            # disagreement about the model; it is a lineage that never had that
            # stage. Only the pinned ones are propagated.
            entries = {key: value for key, value in entries.items() if value is not None}
        selection, complaints = _agreed(
            {key: (value or {}).get("chain") if (value or {}).get("mode") == "explicit"
             else None for key, value in entries.items()},
            f"环节「{purpose}」",
        )
        if complaints and any((value or {}).get("mode") == "explicit"
                              for value in entries.values()):
            conflicts.extend(complaints)
        if selection:
            purposes[purpose] = list(selection)
    return {"tiers": tiers, "purposes": purposes, "conflicts": conflicts}


def target_selections(environment: Environment) -> dict[str, Any]:
    """The same view of an environment that is about to be aligned."""

    tier_chains = current_tier_chains(environment.router_db)
    overrides = current_purpose_overrides(environment.router_db)
    return {"tier_chains": tier_chains, "purpose_overrides": overrides}


def plan_environment(
    source: Mapping[str, Any], environment: Environment,
) -> dict[str, Any]:
    """What this environment would have to publish to match the source.

    Per policy lineage, because that is the unit an environment actually holds:
    an environment where one lane's policy matches and another's does not is
    *not* aligned, and reporting one number for it would hide exactly the lane
    that is still spending on the wrong model.
    """

    try:
        held = target_selections(environment)
    except (ModelRoutingSyncError, OSError, ValueError) as exc:
        return {"environment_id": environment.environment_id,
                "name": environment.name, "status": "unreadable",
                "reason": str(exc), "publish": [], "differences": []}
    differences: list[dict[str, Any]] = []
    publish: list[dict[str, Any]] = []
    for tier, wanted in sorted(source["tiers"].items()):
        stale = {
            policy_id: chains.get(tier)
            for policy_id, chains in held["tier_chains"].items()
            if chains.get(tier) != wanted
        }
        if not stale:
            continue
        for policy_id, before in sorted(stale.items()):
            differences.append({"kind": "tier", "policy_id": policy_id,
                                "subject": tier, "before": before, "after": wanted})
        publish.append({"kind": "tier", "tier": tier, "chain": list(wanted)})
    for purpose, wanted in sorted(source["purposes"].items()):
        stale = {}
        for policy_id, overrides in held["purpose_overrides"].items():
            entry = overrides.get(purpose) or {}
            before = list(entry.get("chain") or []) if entry.get("mode") == "explicit" else None
            if before != wanted:
                stale[policy_id] = before
        if not stale:
            continue
        for policy_id, before in sorted(stale.items()):
            differences.append({"kind": "purpose", "policy_id": policy_id,
                                "subject": purpose, "before": before, "after": wanted})
        publish.append({"kind": "purpose", "purpose": purpose, "chain": list(wanted)})
    return {
        "environment_id": environment.environment_id,
        "name": environment.name,
        "state_dir": str(environment.state_dir),
        "writer_running": environment.is_running,
        "status": "aligned" if not publish else "differs",
        "publish": publish,
        "differences": differences,
    }


def plan(
    manager_config_path: str | Path,
    *,
    source_id: str = LEGACY_ENVIRONMENT_ID,
    legacy_service_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read every environment and say what each would have to publish."""

    environments = host_environments(
        manager_config_path, legacy_service_path=legacy_service_path)
    by_id = {item.environment_id: item for item in environments}
    source = by_id.get(source_id)
    if source is None:
        raise AlignmentError(
            f"这台机器上找不到源环境 {source_id}；已知的是："
            + "、".join(sorted(by_id)) or "（一个都没有）"
        )
    selections = source_selections(source)
    targets = [plan_environment(selections, item)
               for item in environments if item.environment_id != source_id]
    return {
        "source": {"environment_id": source.environment_id, "name": source.name,
                   "state_dir": str(source.state_dir),
                   "tiers": selections["tiers"], "purposes": selections["purposes"]},
        "conflicts": selections["conflicts"],
        "environments": targets,
        "environments_differing": sum(1 for item in targets if item["status"] == "differs"),
    }


# ---------------------------------------------------------------------------
# Applying, through each environment's own writer.


def apply_plan(
    plan_result: Mapping[str, Any],
    manager_config_path: str | Path,
    *,
    actor_ref: str,
    legacy_service_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Publish each environment's missing selections through its own writer.

    Tiers before per-stage pins, for the reason ``repair_brain_chains.py``
    documents: a tier save drops every purpose override belonging to that tier,
    so the other order would publish the stage pins and then throw them away.
    """

    from dalton_core.governance_cli import ephemeral_call

    environments = {
        item.environment_id: item
        for item in host_environments(manager_config_path,
                                      legacy_service_path=legacy_service_path)
    }
    results: list[dict[str, Any]] = []
    for target in plan_result["environments"]:
        if target["status"] != "differs":
            results.append({"environment_id": target["environment_id"],
                            "status": target["status"]})
            continue
        environment = environments[target["environment_id"]]
        ordered = ([item for item in target["publish"] if item["kind"] == "tier"]
                   + [item for item in target["publish"] if item["kind"] == "purpose"])
        published: list[dict[str, Any]] = []
        for item in ordered:
            params = selection_params(
                tier=item.get("tier"), purpose=item.get("purpose"),
                mode="explicit", chain=item["chain"])
            try:
                outcome = ephemeral_call(
                    environment.token_config, environment.writer_socket,
                    actor_ref=actor_ref, operation="set_model_selection",
                    params=params)
            except Exception as exc:  # noqa: BLE001 - one environment, not all
                published.append({"request": params, "status": "failed",
                                  "reason": str(exc)})
                # Stop this environment here: a tier save after a failed one
                # would drop stage pins this run was about to write. The other
                # environments are untouched and the script is idempotent.
                break
            published.append({"request": params,
                              "status": (outcome or {}).get("status", "published")
                              if isinstance(outcome, Mapping) else "published"})
        results.append({"environment_id": target["environment_id"],
                        "name": target["name"],
                        "status": ("failed" if any(item["status"] == "failed"
                                                   for item in published)
                                   else "aligned"),
                        "published": published})
    return results


def render(plan_result: Mapping[str, Any], *, verbose: bool = False) -> str:
    """The before/after the owner reads, per environment and per policy."""

    source = plan_result["source"]
    lines = [f"源环境：{source['name']}（{source['environment_id']}）",
             f"  状态目录：{source['state_dir']}"]
    for tier, chain in sorted(source["tiers"].items()):
        lines.append(f"  类别 {tier}：{' → '.join(chain)}")
    for purpose, chain in sorted(source["purposes"].items()):
        lines.append(f"  环节 {purpose}：{' → '.join(chain)}")
    for complaint in plan_result["conflicts"]:
        lines.append(f"  ⚠ {complaint}")
    if not plan_result["environments"]:
        lines.append("这台机器上没有其它研究环境。")
        return "\n".join(lines)
    for target in plan_result["environments"]:
        running = "writer 在跑" if target.get("writer_running") else "writer 没在跑"
        lines.append("")
        lines.append(f"环境：{target['name']}（{target['environment_id']}）· {running}")
        if target["status"] == "unreadable":
            lines.append(f"  读不到：{target['reason']}")
            continue
        if target["status"] == "aligned":
            lines.append("  已经和源环境一致，不需要发布。")
            continue
        # Grouped by what actually changes rather than one block per policy:
        # twelve lineages carrying the same stale chain is one fact about this
        # environment, and printing it twelve times buried the one lineage that
        # differed from the other eleven.
        grouped: dict[tuple[str, str, str, str], list[str]] = {}
        for difference in target["differences"]:
            before = ("（没有这一项）" if difference["before"] is None
                      else " → ".join(difference["before"]) or "（空）")
            key = (difference["kind"], difference["subject"], before,
                   " → ".join(difference["after"]))
            grouped.setdefault(key, []).append(difference["policy_id"])
        for (kind, subject, before, after), policy_ids in grouped.items():
            lines.append(
                f"  {'类别' if kind == 'tier' else '环节'} {subject}"
                f"（{len(policy_ids)} 条策略）"
            )
            lines.append(f"    现在：{before}")
            lines.append(f"    将改为：{after}")
            if verbose:
                lines.append(f"    涉及：{'、'.join(sorted(policy_ids))}")
        lines.append(f"  需要发布 {len(target['publish'])} 次选择。")
    lines.append("")
    lines.append(f"共 {plan_result['environments_differing']} 个环境与源环境不同。")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-config", type=Path,
                        default=Path(DEFAULT_MANAGER_CONFIG))
    parser.add_argument("--legacy-service", type=Path, default=None,
                        help="the legacy environment's service.json, if it is not "
                             "where it usually is")
    parser.add_argument("--source", default=LEGACY_ENVIRONMENT_ID,
                        help="the environment every other one is aligned to")
    parser.add_argument("--apply", action="store_true",
                        help="publish through each environment's live writer")
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true",
                        help="list every policy lineage behind each difference")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = plan(args.manager_config, source_id=args.source,
                      legacy_service_path=args.legacy_service)
    except (AlignmentError, ModelRoutingSyncError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.apply:
        if not str(args.actor).startswith("human:"):
            print("--actor 必须是 human: 开头的人的身份", file=sys.stderr)
            return 2
        for complaint in result["conflicts"]:
            # A subject the source disagrees with itself about was never put in
            # the plan, so applying the rest is safe. It is still said out loud:
            # the owner has a decision to make, and a silent skip would let a
            # lane keep the chain this run was supposed to have replaced.
            print(f"跳过（源环境自己就不一致）：{complaint}", file=sys.stderr)
        result["applied"] = apply_plan(
            result, args.manager_config, actor_ref=args.actor,
            legacy_service_path=args.legacy_service)
        # Read the host again and say what is still different. A tier save
        # drops the stage pins of its own tier, so a stage that matched before
        # this run can be missing after it; the script is idempotent and a
        # second run closes that, but the owner has to be told to make one
        # rather than discover it on the model page next week.
        try:
            after = plan(args.manager_config, source_id=args.source,
                         legacy_service_path=args.legacy_service)
        except (AlignmentError, ModelRoutingSyncError) as exc:
            result["residual"] = {"status": "unreadable", "reason": str(exc)}
        else:
            result["residual"] = {
                "environments_differing": after["environments_differing"],
                "note": ("每个环境都和源环境一致了。"
                         if not after["environments_differing"] else
                         f"还有 {after['environments_differing']} 个环境与源环境不同；"
                         "这个脚本是幂等的，再跑一次同样的命令即可。"),
            }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render(result, verbose=args.verbose))
        for applied in result.get("applied", []):
            print(f"已处理 {applied['environment_id']}：{applied['status']}")
        residual = result.get("residual")
        if residual:
            print(residual.get("note") or residual.get("reason", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
