#!/usr/bin/env python3
"""WP-A/A5: take the models that cannot serve out of the live routing chains.

On 2026-09-16 every routing policy on this host carried the same brain chain::

    profile:deepseek-v4-flash
    profile:zai-glm-5-3
    profile:gemini-3-8-flash-antigravity-high   <- then 30k ceiling; 170k revalidated 2026-09-22
    model-profile:claude-opus-5                 <- profile version expired 2026-08-15
    profile:gpt-6-astra                         <- 100% HTTP 429 since 2026-09-14T19:58

Three of the five links could not serve a brain-tier call, and two of them could
not serve *any* call.  The code changes in this work package stop each of them
costing money -- a rate limit settles at zero, a failing endpoint is cooled out
of selection, an oversized prompt skips the endpoint that cannot take it -- but
none of that removes a dead id from a chain the owner published.  Only
republishing the chain does, and that is what this script is.

It publishes through exactly the path the cockpit's model page uses: the
``set_model_selection`` writer operation, which calls
``model_selection.set_tier_selection`` / ``set_model_selection``.  So the result
is an ordinary immutable routing-policy version with an actor on it, every
registered lane configuration is repointed at it, rollback is republishing the
previous chain, and nothing here writes a row by hand.

Safe while the services are running: the writer owns the databases and this
talks to the writer.  Idempotent: a second run publishes nothing, because the
publisher refuses to append a version whose content has not changed.

    # look first -- reads only, changes nothing
    scripts/repair_brain_chains.py --state-dir ~/Library/Application\\ Support/Dalton/state/dalton-core

    # then apply, through the live writer, as the owner
    scripts/repair_brain_chains.py --state-dir ... --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.model_fallback_chain import (  # noqa: E402
    TIER_BRAIN, TIER_CHEAP, TIER_DELIVERABLE, TIER_VERIFIER,
    profiles_serving_transport, purpose_transports, tier_for,
)
from dalton_core.model_profile_bounds import MEASURED_INPUT_BOUNDS  # noqa: E402
from dalton_core.model_router import ModelRouter  # noqa: E402

DEFAULT_STATE_DIR = "~/Library/Application Support/Dalton/state/dalton-core"
DEFAULT_ACTOR = "human:lumos"
TIERS = (TIER_BRAIN, TIER_CHEAP, TIER_VERIFIER, TIER_DELIVERABLE)

#: The tiers whose calls are long-form reasoning prompts, and which a link with
#: a small measured transport ceiling therefore cannot serve. 交付物起草 is in
#: here for the same reason 高阶推理 is: a dossier prompt is not chunked.
LONG_PROMPT_TIERS = (TIER_BRAIN, TIER_DELIVERABLE)

#: The endpoint whose rate limiting started this. Removed from every chain by
#: default; ``--keep-astra`` keeps it as the last resort instead.
ASTRA = "profile:gpt-6-astra"

#: The smallest input a brain-tier link has to be able to take. A reasoning
#: prompt is 30k bytes on a quiet day, so an endpoint whose *measured* transport
#: ceiling is below this cannot draft, whatever its catalog entry claims. Not a
#: name-list: anything in model_profile_bounds.MEASURED_INPUT_BOUNDS under this
#: figure comes out of the brain chain, including the next one discovered.
BRAIN_MIN_INPUT_BOUND = 100_000

#: Stages whose output is a strict JSON contract rather than prose. A chain of
#: flash models will produce an argument that reads well and a schema that does
#: not validate, so these are pinned to the two endpoints that hold a contract.
#:
#: 2026-09-17: these three now *are* the 交付物起草 tier, so the contract is
#: carried by that tier's chain rather than by three per-stage pins, and this
#: script no longer publishes pins for them -- republishing a pin the tier save
#: has just dropped is how the two would come to disagree. Splitting the tier
#: out on a live host is scripts/split_deliverable_tier.py; repairing the
#: tier's chain afterwards is the ordinary tier pass above. A purpose named
#: with ``--structured-purpose`` that is *not* in the tier is still pinned.
STRUCTURED_PURPOSES: tuple[str, ...] = ("debate_map", "dossier", "model_spec")
STRUCTURED_CHAIN: tuple[str, ...] = (
    "profile:claude-opus-5", "profile:deepseek-v4-flash",
)


class RepairError(RuntimeError):
    pass


def transport_pin(
    purpose: str,
    required: Mapping[str, str],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    now: str,
    cooled: Mapping[str, Any] = {},
) -> dict[str, Any]:
    """The chain for a stage whose served endpoint is asserted on downstream.

    Publication has been producing nothing since the language checker's own
    configuration pinned ``dalton-openclaw-dossier-verifier:46``, a policy with
    no ``research_language_check`` override: the cheap tier's ordered
    preferences then picked the cheapest link (``profile:deepseek-v4-flash``,
    849 of 1,176 replays) and ``research_output_preparation`` raised "language
    checker served an unexpected transport or model" every single time.

    The chain is therefore every *routable* profile serving the required
    endpoint, uncooled ones first. A cooled one still belongs behind them -- a
    cooldown expires, and dropping it would shorten the chain permanently -- but
    it must never lead. Only one such profile is a single point of failure and
    the dry-run says so; none at all is refused rather than guessed at.

    The 30k transport ceiling is not a constraint here: language review is
    chunked at 4,500 characters.
    """

    serving = profiles_serving_transport(profiles, required)
    routable = [profile_id for profile_id in serving
                if resolve_profile_id(profile_id, profiles, now=now) == profile_id]
    chain = ([profile_id for profile_id in routable if profile_id not in cooled]
             + [profile_id for profile_id in routable if profile_id in cooled])
    notes: list[str] = []
    if not chain:
        return {"purpose": purpose, "chain": [], "notes": notes, "required": dict(required),
                "problem": (f"{purpose}：目录里没有任何当前可路由的档案提供 "
                            f"{required['model']}，无法钉定；先让目录同步登记它。")}
    if len(chain) == 1:
        notes.append(
            f"提示：只有 {chain[0]} 一个档案提供 {required['model']}，"
            "它一旦进入供应商冷却，这一环就没有后备可用。")
    elif chain[0] in cooled:
        notes.append(f"提示：{chain[0]} 正在冷却，但它是唯一未冷却之外的选择。")
    for profile_id in chain:
        if profile_id in cooled:
            notes.append(f"{profile_id} 正在冷却，放在链尾等冷却结束。")
    return {"purpose": purpose, "chain": chain, "notes": notes,
            "required": dict(required), "problem": None}


# --------------------------------------------------------------------------
# Pure planning. No database, no writer: given what the catalog holds and what
# a chain says, what should the chain say instead.
# --------------------------------------------------------------------------


def resolve_profile_id(profile_id: str, profiles: Mapping[str, Mapping[str, Any]],
                       *, now: str) -> str | None:
    """The live id for this one: itself, a sibling, or ``None`` if nothing serves.

    ``model-profile:claude-opus-5`` and ``profile:claude-opus-5`` are the same
    model registered under two id shapes; the first was written once in August
    and never refreshed. Matching on the part after the colon is how the dead
    one is mapped onto the live one without a hand-maintained rename table.
    """

    from datetime import datetime

    from dalton_core.model_fallback_chain import profile_unusable

    moment = datetime.fromisoformat(now)
    held = profiles.get(profile_id)
    if held is not None and profile_unusable(held, now=moment) is None:
        return profile_id
    tail = profile_id.split(":", 1)[-1]
    for candidate, profile in sorted(profiles.items()):
        if candidate == profile_id or candidate.split(":", 1)[-1] != tail:
            continue
        if profile_unusable(profile, now=moment) is None:
            return candidate
    return None


def repair_chain(
    chain: Sequence[str],
    *,
    tier: str,
    profiles: Mapping[str, Mapping[str, Any]],
    now: str,
    keep_astra: bool = False,
    cooled: Mapping[str, Any] = {},
) -> dict[str, Any]:
    """The chain this tier should carry, and one line per link about why."""

    notes: list[str] = []
    repaired: list[str] = []
    for profile_id in chain:
        resolved = resolve_profile_id(profile_id, profiles, now=now)
        if resolved is None:
            notes.append(f"移除 {profile_id}：目录里没有当前有效的档案")
            continue
        if resolved != profile_id:
            notes.append(f"{profile_id} → {resolved}：前者的档案已过期，后者是同一个模型的在用档案")
        if resolved == ASTRA and not keep_astra:
            notes.append(f"移除 {ASTRA}：自 2026-09-14T19:58 起 100% HTTP 429")
            continue
        bound = MEASURED_INPUT_BOUNDS.get(resolved)
        if tier in LONG_PROMPT_TIERS and bound is not None \
                and bound < BRAIN_MIN_INPUT_BOUND:
            notes.append(
                f"从 {tier} 链移除 {resolved}：实测输入上限 {bound}，"
                f"低于长提示词档位所需的 {BRAIN_MIN_INPUT_BOUND}")
            continue
        if resolved in cooled:
            notes.append(f"移除 {resolved}：当前处于供应商冷却中")
            continue
        if resolved not in repaired:
            repaired.append(resolved)
    if keep_astra and ASTRA in repaired and repaired[-1] != ASTRA:
        repaired = [item for item in repaired if item != ASTRA] + [ASTRA]
        notes.append(f"{ASTRA} 移到链尾：仍在限流，只作最后兜底")
    if not repaired:
        raise RepairError(
            f"{tier} 链修复后会是空的，拒绝发布；请先让目录同步登记可用模型")
    return {"tier": tier, "before": list(chain), "after": repaired, "notes": notes}


def _drafter_verifier_family_clash(
    purpose: str,
    chain: Sequence[str],
    policies: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Refuse a drafting pin that a verifier of the same family would check.

    The standing rule is that drafting and review come from different vendors,
    and it is enforced at route time by the family-independence filter. Enforced
    only there, a pin that breaks it does not fail loudly -- it makes every
    verification of that stage unroutable, which reads as "the verifier is
    down". Better to refuse the pin.
    """

    verifier = f"{purpose}_verifier"
    drafting = {(profiles.get(item) or {}).get("family") for item in chain}
    drafting.discard(None)
    for policy in policies:
        override = (policy.get("purpose_overrides") or {}).get(verifier)
        if not isinstance(override, Mapping) or override.get("mode") != "explicit":
            continue
        checking = {(profiles.get(item) or {}).get("family")
                    for item in override.get("chain") or ()}
        checking.discard(None)
        shared = drafting & checking
        if shared and checking <= drafting:
            return (f"{purpose}：拟定的起草链与 {verifier} 的复核链同属 "
                    f"{', '.join(sorted(shared))}，违反「起草与复核必须不同厂商」")
    return None


def plan_repair(
    policies: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    now: str,
    keep_astra: bool = False,
    cooled: Mapping[str, Any] = {},
    structured_purposes: Sequence[str] = STRUCTURED_PURPOSES,
    structured_chain: Sequence[str] = STRUCTURED_CHAIN,
    purpose_policy_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """What to publish, per tier and per structured purpose, across every policy.

    A tier save is one chain for every policy on the host -- that is what the
    cockpit's tier editor does -- so a host whose policies disagree about a tier
    cannot be repaired by one save, and this says so instead of picking one.
    """

    per_tier: dict[str, dict[tuple[str, ...], list[str]]] = {tier: {} for tier in TIERS}
    diffs: list[dict[str, Any]] = []
    for policy in policies:
        tiers = (policy.get("fallback_chains") or {}).get("tiers") or {}
        for tier in TIERS:
            chain = tiers.get(tier)
            if not chain:
                continue
            repaired = repair_chain(chain, tier=tier, profiles=profiles, now=now,
                                    keep_astra=keep_astra, cooled=cooled)
            per_tier[tier].setdefault(tuple(repaired["after"]), []).append(
                policy["id"])
            if repaired["after"] != list(chain):
                diffs.append({"policy_id": policy["id"],
                              "policy_version_ref": policy["policy_version_ref"],
                              **repaired})
    publish: list[dict[str, Any]] = []
    conflicts: list[str] = []
    notes_only: list[str] = []
    for tier in TIERS:
        options = per_tier[tier]
        if not options:
            continue
        if len(options) > 1:
            conflicts.append(
                f"{tier}：不同策略修复后得到不同的链 "
                + "; ".join(f"{list(chain)} ← {sorted(ids)}"
                            for chain, ids in options.items()))
            continue
        target = next(iter(options))
        current = {
            tuple((policy.get("fallback_chains") or {}).get("tiers", {}).get(tier)
                  or ())
            for policy in policies
            if (policy.get("fallback_chains") or {}).get("tiers", {}).get(tier)
        }
        if current != {target}:
            publish.append({"kind": "tier", "tier": tier, "chain": list(target)})
    # WP-A/A4b: the stages whose served endpoint is asserted on downstream come
    # first, because a wrong model there is not a worse answer -- it is no
    # answer at all, and it has been that way for 1,176 replays.
    for purpose, required in sorted(purpose_transports().items()):
        pin = transport_pin(purpose, required, profiles, now=now, cooled=cooled)
        if pin["problem"] is not None:
            conflicts.append(pin["problem"])
            continue
        reachable = (policies if purpose_policy_ids is None
                     else [policy for policy in policies
                           if policy["id"] in set(purpose_policy_ids)])
        held = {
            tuple(((policy.get("purpose_overrides") or {}).get(purpose) or {})
                  .get("chain") or ())
            for policy in reachable
        }
        if held != {tuple(pin["chain"])}:
            publish.append({"kind": "purpose", "purpose": purpose,
                            "tier": tier_for(purpose), "chain": pin["chain"],
                            "notes": pin["notes"],
                            "reason": (f"下游按供应商和模型逐次核对，必须由 "
                                       f"{required['model']} 提供")})
        elif pin["notes"]:
            notes_only.extend(f"{purpose}：{note}" for note in pin["notes"])
    for purpose in structured_purposes:
        if tier_for(purpose) == TIER_DELIVERABLE:
            unsplit = [
                policy["id"] for policy in policies
                if not (policy.get("fallback_chains") or {}).get("tiers", {}).get(
                    TIER_DELIVERABLE)
            ]
            if unsplit:
                notes_only.append(
                    f"{purpose}：属于交付物起草档位，但这些策略还没有这一档的链"
                    f"（{', '.join(sorted(set(unsplit)))}）；"
                    "先跑 scripts/split_deliverable_tier.py 把它拆出来。")
            continue
        wanted = [profile_id for profile_id in structured_chain
                  if resolve_profile_id(profile_id, profiles, now=now) == profile_id]
        if len(wanted) != len(list(structured_chain)):
            conflicts.append(
                f"{purpose}：结构化输出链里有模型当前不可用 "
                f"（要求 {list(structured_chain)}）")
            continue
        clash = _drafter_verifier_family_clash(
            purpose, wanted, policies, profiles)
        if clash:
            conflicts.append(clash)
            continue
        # Only the policies a per-stage save actually reaches. A tier save
        # repoints every lane configuration *and* the resident service pins; a
        # per-stage save reaches the lane configurations only, so measuring
        # "is this already published" against the service-pinned policies would
        # make the script want to publish the same three pins on every run.
        reachable = (policies if purpose_policy_ids is None
                     else [policy for policy in policies
                           if policy["id"] in set(purpose_policy_ids)])
        held = {
            tuple(((policy.get("purpose_overrides") or {}).get(purpose) or {})
                  .get("chain") or ())
            for policy in reachable
        }
        if held != {tuple(wanted)}:
            publish.append({"kind": "purpose", "purpose": purpose,
                            "tier": tier_for(purpose), "chain": wanted})
    return {"diffs": diffs, "publish": publish, "conflicts": conflicts,
            "notes": notes_only}


# --------------------------------------------------------------------------
# Reading the host, and publishing through the writer.
# --------------------------------------------------------------------------


def read_state(state_dir: Path, *, read_only: bool = True) -> dict[str, Any]:
    """Latest version of every policy, and the catalog.

    Read-only by default, which on a running host is the strict WAL-reader
    connection that cannot write even by accident. A *stopped* host has no WAL
    sidecars for that reader to attach to, so ``--offline`` -- which is already
    a declaration that nothing else owns these files -- opens them normally.
    """

    import sqlite3

    router_db = state_dir / "model-router.sqlite"
    if not router_db.is_file():
        raise RepairError(f"这里没有模型路由数据库：{router_db}")
    try:
        opened = ModelRouter(str(router_db), read_only=read_only)
    except sqlite3.OperationalError as exc:
        raise RepairError(
            f"无法以只读方式打开 {router_db}：{exc}。"
            "常驻服务没有在跑时请加 --offline。"
        ) from exc
    with opened as router:
        rows = router.connection.execute(
            "SELECT policy_json FROM model_routing_policy_versions p "
            "WHERE p.version=(SELECT MAX(q.version) FROM model_routing_policy_versions q "
            "WHERE q.policy_id=p.policy_id) ORDER BY p.policy_id"
        ).fetchall()
        policies = [json.loads(row["policy_json"]) for row in rows]
        profiles = {profile["id"]: profile for profile in router.latest_profiles()}
        cooled = router.active_cooldowns()
        now = router.clock().isoformat()
        lane_policy_ids = _lane_policy_ids(router, state_dir)
    return {"policies": policies, "profiles": profiles, "cooled": cooled,
            "now": now, "lane_policy_ids": lane_policy_ids}


def _lane_policy_ids(router: ModelRouter, state_dir: Path) -> list[str]:
    """The logical policies pinned by this host's lane model configurations.

    These are the ones a per-stage save repoints. The resident service pins
    (planner, assessment, thesis impact) are repointed by a *tier* save and not
    by a per-stage one, so they are deliberately absent.
    """

    found: set[str] = set()
    for path in sorted(state_dir.glob("*model-config.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ref = config.get("routing_policy_ref")
        if not isinstance(ref, str):
            continue
        row = router.connection.execute(
            "SELECT policy_id FROM model_routing_policy_versions "
            "WHERE policy_version_ref=?", (ref,)).fetchone()
        if row is not None:
            found.add(row["policy_id"])
    return sorted(found)


def apply_through_writer(state_dir: Path, publish: Sequence[Mapping[str, Any]],
                         *, actor_ref: str) -> list[dict[str, Any]]:
    """Publish each change with the governance CLI's ephemeral human principal.

    Tiers first, then the per-stage pins: a tier save drops every purpose
    override belonging to that tier, so doing it the other way round would
    publish the structured-output chains and then throw them away.
    """

    from dalton_core.governance_cli import ephemeral_call

    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"
    ordered = ([item for item in publish if item["kind"] == "tier"]
               + [item for item in publish if item["kind"] == "purpose"])
    results: list[dict[str, Any]] = []
    for item in ordered:
        params: dict[str, Any] = {"mode": "explicit", "chain": list(item["chain"])}
        if item["kind"] == "tier":
            params["tier"] = item["tier"]
        else:
            params["purpose"] = item["purpose"]
        try:
            outcome = ephemeral_call(
                token_config, socket, actor_ref=actor_ref,
                operation="set_model_selection", params=params,
            )
        except Exception as exc:  # noqa: BLE001 - see below
            # A lost answer is not a lost publication. The writer owns the
            # databases and finishes what it started, so the only honest way to
            # find out whether this landed is to read the policies again. If it
            # did, carry on; if it did not, stop before the *next* save, because
            # a tier save after a half-applied one would drop the stage pins
            # this run was about to write.
            if _already_published(state_dir, item):
                results.append({"request": params,
                                "result": {"status": "published",
                                           "note": "writer answer was lost; "
                                                   "the published version is in place",
                                           "error": str(exc)}})
                continue
            raise RepairError(
                f"发布 {params} 时与 writer 的通信失败，且策略没有变化：{exc}。"
                "常驻服务在跑吗？修好后重跑同一条命令即可，这个脚本是幂等的。"
            ) from exc
        results.append({"request": params, "result": outcome})
    return results


def _already_published(state_dir: Path, item: Mapping[str, Any]) -> bool:
    """Re-read the policies and say whether this change is now in force."""

    try:
        state = read_state(state_dir)
        plan = plan_repair(
            state["policies"], state["profiles"], now=state["now"],
            cooled=state["cooled"], purpose_policy_ids=state["lane_policy_ids"])
    except Exception:  # noqa: BLE001 - if we cannot read, we cannot claim
        return False
    return not any(
        pending["kind"] == item["kind"]
        and pending.get("tier") == item.get("tier")
        and pending.get("purpose") == item.get("purpose")
        for pending in plan["publish"]
    )


def apply_offline(state_dir: Path, publish: Sequence[Mapping[str, Any]],
                  *, actor_ref: str) -> list[dict[str, Any]]:
    """The same publications, called directly. Only for a stopped host.

    Same functions the writer operation calls, so the published content is
    identical; what is missing is the writer's exclusive ownership of the
    databases, which is why this is not the default.
    """

    from dalton_core.model_selection import set_model_selection, set_tier_selection

    ordered = ([item for item in publish if item["kind"] == "tier"]
               + [item for item in publish if item["kind"] == "purpose"])
    results: list[dict[str, Any]] = []
    for item in ordered:
        if item["kind"] == "tier":
            outcome = set_tier_selection(
                state_dir, tier=item["tier"], mode="explicit",
                chain=list(item["chain"]), actor_ref=actor_ref)
        else:
            outcome = set_model_selection(
                state_dir, purpose=item["purpose"], mode="explicit",
                chain=list(item["chain"]), actor_ref=actor_ref)
        results.append({"request": item, "result": outcome})
    return results


def render(plan: Mapping[str, Any]) -> str:
    lines: list[str] = []
    if not plan["diffs"] and not plan["publish"]:
        lines.append("没有需要修复的链：所有策略的 "
                     + "/".join(TIERS) + " 链都指向当前有效的模型。")
    for diff in plan["diffs"]:
        lines.append(f"[{diff['policy_id']}] {diff['tier']}")
        lines.append(f"  现在： {' → '.join(diff['before'])}")
        lines.append(f"  修复后： {' → '.join(diff['after'])}")
        for note in diff["notes"]:
            lines.append(f"    · {note}")
    if plan["publish"]:
        lines.append("")
        lines.append("将要发布（每条都是一个新的不可变策略版本，回滚就是把上一版再发布一次）：")
        for item in plan["publish"]:
            subject = (f"档位 {item['tier']}" if item["kind"] == "tier"
                       else f"环节 {item['purpose']}（{item['tier']} 档）")
            lines.append(f"  · {subject}： {' → '.join(item['chain'])}")
            if item.get("reason"):
                lines.append(f"      理由：{item['reason']}")
            for note in item.get("notes") or ():
                lines.append(f"      {note}")
    for note in plan.get("notes") or ():
        lines.append(f"  · {note}")
    for conflict in plan["conflicts"]:
        lines.append(f"!! 需要人来决定：{conflict}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="Dalton Core state directory (holds model-router.sqlite)")
    parser.add_argument("--apply", action="store_true",
                        help="publish the repair; without it the script only reads")
    parser.add_argument("--offline", action="store_true",
                        help="publish without the writer; only for a stopped host")
    parser.add_argument("--actor", default=DEFAULT_ACTOR,
                        help="the human who is making this choice")
    parser.add_argument("--keep-astra", action="store_true",
                        help=f"keep {ASTRA} as the last link instead of removing it")
    parser.add_argument("--structured-purpose", action="append", default=None,
                        help="a stage to pin to the structured-output chain "
                             f"(default: {', '.join(STRUCTURED_PURPOSES)})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    state_dir = Path(os.path.expanduser(args.state_dir)).resolve()
    try:
        state = read_state(state_dir, read_only=not (args.apply and args.offline))
        plan = plan_repair(
            state["policies"], state["profiles"], now=state["now"],
            keep_astra=args.keep_astra, cooled=state["cooled"],
            structured_purposes=tuple(args.structured_purpose
                                      or STRUCTURED_PURPOSES),
            purpose_policy_ids=state["lane_policy_ids"],
        )
    except RepairError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    applied: list[dict[str, Any]] = []
    if args.apply:
        if plan["conflicts"]:
            print(render(plan))
            print("\n有需要人来决定的冲突，什么都没有发布。", file=sys.stderr)
            return 3
        if not args.actor.startswith("human:"):
            print("--actor 必须是 human: 开头：选模型是人的决定。", file=sys.stderr)
            return 2
        applied = (apply_offline(state_dir, plan["publish"], actor_ref=args.actor)
                   if args.offline
                   else apply_through_writer(state_dir, plan["publish"],
                                             actor_ref=args.actor))
    if args.json:
        print(json.dumps({"state_dir": str(state_dir), "applied": bool(args.apply),
                          "plan": plan, "results": applied},
                         ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render(plan))
        if args.apply:
            print("")
            print(f"已发布 {len(applied)} 项。再跑一次这个命令应当输出「没有需要修复的链」。")
        elif plan["publish"]:
            print("")
            print("这是一次只读预演。加 --apply 才会发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
