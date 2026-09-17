#!/usr/bin/env python3
"""Turn the three hand-written deliverable pins into the 交付物起草 tier.

On 2026-09-16 the drafting stages whose product is a schema-bound document --
公司档案 (``dossier``), 争议图 (``debate_map``), 模型规格 (``model_spec``) -- were
held on one chain by hand: three ``purpose_overrides`` entries, each naming
``profile:claude-opus-5`` then ``profile:deepseek-v4-flash``.  That worked and
it was invisible.  The owner could not see the three as one group, could not
reorder them from the model page, and a 整类保存 of the 高阶推理 tier would have
dropped all three without saying so -- a tier save drops every override of that
tier, which is exactly right for a preference and exactly wrong for this.

The code side of the fix is the fourth tier (``model_fallback_chain``:
``TIER_DELIVERABLE``).  This script is the live side of it, and it publishes
through the same path the model page's 整类保存 button uses -- the
``set_model_selection`` writer operation with a ``tier`` key, which is
``model_selection.set_tier_selection``.  One save does both halves:

* ``fallback_chains.tiers.deliverable`` is created, carrying the chain the
  ``dossier`` override pins today, so nothing about what actually runs changes
  on the day of the split; and
* every ``purpose_overrides`` entry belonging to the tier -- the three above --
  is dropped, because a tier chain and a per-stage pin saying the same thing is
  how they later come to say different things.  ``research_language_check``
  and every other override is untouched: it is a cheap-tier stage, and its pin
  is a transport contract rather than a preference.

Afterwards the three stages read 「交付物起草」 on the model page with a
draggable chain and a 整类保存 button, and reordering them is the owner's, not
a script's.

Safe while the services are running: the writer owns the databases and this
talks to the writer.  Idempotent: a second run publishes nothing, because the
publisher refuses to append a version whose content has not changed.

    # look first -- reads only, changes nothing
    scripts/split_deliverable_tier.py --state-dir ~/Library/Application\\ Support/Dalton/state/dalton-core

    # then apply, through the live writer, as the owner
    scripts/split_deliverable_tier.py --state-dir ... --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.model_fallback_chain import (  # noqa: E402
    TIER_DELIVERABLE, profile_unusable, purpose_tiers,
)
from dalton_core.model_router import ModelRouter  # noqa: E402

DEFAULT_STATE_DIR = "~/Library/Application Support/Dalton/state/dalton-core"
DEFAULT_ACTOR = "human:lumos"

#: The stage whose existing pin *is* the new tier's chain.  Reading the chain
#: off the live policy rather than hard-coding it is the point: the split must
#: change who configures these models, not which models they are.
SOURCE_PURPOSE = "dossier"


class SplitError(RuntimeError):
    pass


def deliverable_purposes() -> tuple[str, ...]:
    """The stages the new tier owns, read from the code's purpose map."""

    return tuple(sorted(name for name, tier in purpose_tiers().items()
                        if tier == TIER_DELIVERABLE))


def resolve_profile_id(profile_id: str, profiles: Mapping[str, Mapping[str, Any]],
                       *, now: str) -> str | None:
    """The live id for this one: itself, a sibling, or ``None`` if nothing serves.

    ``model-profile:claude-opus-5`` and ``profile:claude-opus-5`` are the same
    model registered under two id shapes, and an override written by hand in
    August may name the one whose availability window has since closed.
    Publishing that id would publish a link that can never be selected.
    """

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


def _verifier_family_clash(
    chain: Sequence[str],
    policies: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Refuse a tier chain that the matching verifiers could not stay independent of.

    The standing rule is that drafting and review come from different vendors,
    and route-time family independence enforces it.  Enforced only there, a
    tier chain drawn entirely from the verifier's own families does not fail
    loudly -- it makes every verification of these stages unroutable, which
    reads as "the verifier is down".  Better to refuse the publication.
    """

    drafting = {(profiles.get(item) or {}).get("family") for item in chain}
    drafting.discard(None)
    for purpose in deliverable_purposes():
        verifier = f"{purpose}_verifier"
        for policy in policies:
            override = (policy.get("purpose_overrides") or {}).get(verifier)
            if not isinstance(override, Mapping) or override.get("mode") != "explicit":
                continue
            checking = {(profiles.get(item) or {}).get("family")
                        for item in override.get("chain") or ()}
            checking.discard(None)
            shared = drafting & checking
            if shared and checking <= drafting:
                return (f"交付物起草链与 {verifier} 的复核链同属 "
                        f"{', '.join(sorted(shared))}，违反「起草与复核必须不同厂商」")
    return None


# --------------------------------------------------------------------------
# Pure planning. No database, no writer: given the policies and the catalog,
# what the one tier save should carry and what it will remove.
# --------------------------------------------------------------------------


def plan_split(
    policies: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    now: str,
    chain: Sequence[str] | None = None,
    source_purpose: str = SOURCE_PURPOSE,
) -> dict[str, Any]:
    """The tier save to publish, and the exact before/after of every policy.

    A tier save writes the same chain into every policy on the host -- that is
    what the model page's 整类保存 does -- so a host whose policies pin the
    drafting stages differently cannot be split by one save, and this says so
    instead of picking one.
    """

    purposes = deliverable_purposes()
    if not purposes:
        raise SplitError("代码里没有任何环节属于交付物起草档位，无从拆分。")
    diffs: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for policy in policies:
        tiers = dict((policy.get("fallback_chains") or {}).get("tiers") or {})
        overrides = dict(policy.get("purpose_overrides") or {})
        removed = {name: entry for name, entry in overrides.items()
                   if name in purposes}
        diffs.append({
            "policy_id": policy["id"],
            "policy_version_ref": policy["policy_version_ref"],
            "tiers_before": {tier: list(links) for tier, links in sorted(tiers.items())},
            "overrides_before": {name: list(entry.get("chain") or ())
                                 for name, entry in sorted(overrides.items())},
            "removed_overrides": sorted(removed),
            "kept_overrides": sorted(set(overrides) - set(removed)),
        })
    wanted = list(chain) if chain is not None else None
    if wanted is None:
        pinned = {
            tuple(((policy.get("purpose_overrides") or {}).get(source_purpose) or {})
                  .get("chain") or ())
            for policy in policies
        }
        pinned.discard(())
        if len(pinned) > 1:
            conflicts.append(
                f"不同策略给 {source_purpose} 钉定了不同的链："
                + "；".join(" → ".join(item) for item in sorted(pinned))
                + "。请用 --chain 明确这一档要用哪条链。")
            pinned = set()
        if not pinned:
            declared = {
                tuple((policy.get("fallback_chains") or {}).get("tiers", {})
                      .get(TIER_DELIVERABLE) or ())
                for policy in policies
            }
            declared.discard(())
            if len(declared) == 1:
                wanted = list(next(iter(declared)))
        else:
            wanted = list(next(iter(pinned)))
    if wanted is None and not conflicts:
        conflicts.append(
            f"策略里既没有 {source_purpose} 的逐环节钉定，也没有交付物起草档位的链，"
            "无法推断这一档该用哪些模型；请用 --chain 指定。")
    resolved: list[str] = []
    notes: list[str] = []
    for profile_id in wanted or ():
        live = resolve_profile_id(profile_id, profiles, now=now)
        if live is None:
            conflicts.append(
                f"{profile_id} 在目录里没有当前有效的档案，发布后这一环永远不会被选中。")
            continue
        if live != profile_id:
            notes.append(f"{profile_id} → {live}：前者的档案已过期，后者是同一个模型的在用档案")
        if live not in resolved:
            resolved.append(live)
    if wanted is not None and not resolved and not conflicts:
        conflicts.append("拆分后交付物起草链会是空的，拒绝发布。")
    if resolved and not conflicts:
        clash = _verifier_family_clash(resolved, policies, profiles)
        if clash is not None:
            conflicts.append(clash)
    publish: list[dict[str, Any]] = []
    if resolved and not conflicts:
        settled = all(
            list((policy.get("fallback_chains") or {}).get("tiers", {})
                 .get(TIER_DELIVERABLE) or ()) == resolved
            and not (set(policy.get("purpose_overrides") or {}) & set(purposes))
            for policy in policies
        )
        if not settled:
            publish.append({"kind": "tier", "tier": TIER_DELIVERABLE,
                            "chain": list(resolved)})
    for diff in diffs:
        after = dict(diff["tiers_before"])
        if resolved:
            after[TIER_DELIVERABLE] = list(resolved)
        diff["tiers_after"] = dict(sorted(after.items()))
        diff["overrides_after"] = {
            name: links for name, links in diff["overrides_before"].items()
            if name not in set(purposes)
        }
    return {"purposes": list(purposes), "chain": list(resolved), "diffs": diffs,
            "publish": publish, "conflicts": conflicts, "notes": notes}


# --------------------------------------------------------------------------
# Reading the host, and publishing through the writer.
# --------------------------------------------------------------------------


def read_state(state_dir: Path, *, read_only: bool = True) -> dict[str, Any]:
    """Latest version of every policy, and the catalog.

    Read-only by default, which on a running host is the strict WAL-reader
    connection that cannot write even by accident.
    """

    import sqlite3

    router_db = state_dir / "model-router.sqlite"
    if not router_db.is_file():
        raise SplitError(f"这里没有模型路由数据库：{router_db}")
    try:
        opened = ModelRouter(str(router_db), read_only=read_only)
    except sqlite3.OperationalError as exc:
        raise SplitError(
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
        now = router.clock().isoformat()
    return {"policies": policies, "profiles": profiles, "now": now}


def apply_through_writer(state_dir: Path, publish: Sequence[Mapping[str, Any]],
                         *, actor_ref: str) -> list[dict[str, Any]]:
    """Publish the tier save with the governance CLI's ephemeral human principal."""

    from dalton_core.governance_cli import ephemeral_call

    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"
    results: list[dict[str, Any]] = []
    for item in publish:
        params = {"tier": item["tier"], "mode": "explicit",
                  "chain": list(item["chain"])}
        try:
            outcome = ephemeral_call(
                token_config, socket, actor_ref=actor_ref,
                operation="set_model_selection", params=params,
            )
        except Exception as exc:  # noqa: BLE001
            # A lost answer is not a lost publication: the writer owns the
            # databases and finishes what it started. Re-read before claiming
            # anything, and say plainly that re-running is safe.
            raise SplitError(
                f"发布 {params} 时与 writer 的通信失败：{exc}。"
                "常驻服务在跑吗？这个脚本是幂等的，修好后重跑同一条命令即可，"
                "先跑一次不带 --apply 的预演看看是否已经生效。"
            ) from exc
        results.append({"request": params, "result": outcome})
    return results


def apply_offline(state_dir: Path, publish: Sequence[Mapping[str, Any]],
                  *, actor_ref: str) -> list[dict[str, Any]]:
    """The same publication, called directly. Only for a stopped host."""

    from dalton_core.model_selection import set_tier_selection

    results: list[dict[str, Any]] = []
    for item in publish:
        outcome = set_tier_selection(
            state_dir, tier=item["tier"], mode="explicit",
            chain=list(item["chain"]), actor_ref=actor_ref)
        results.append({"request": item, "result": outcome})
    return results


def render(plan: Mapping[str, Any]) -> str:
    """The exact before/after, per policy, in the owner's words."""

    lines: list[str] = []
    lines.append("交付物起草档位包含的环节：" + "、".join(plan["purposes"]))
    if plan["chain"]:
        lines.append("这一档将使用： " + " → ".join(plan["chain"]))
    for note in plan.get("notes") or ():
        lines.append(f"  · {note}")
    for diff in plan["diffs"]:
        lines.append("")
        lines.append(f"[{diff['policy_id']}] {diff['policy_version_ref']}")
        lines.append("  档位链 现在：")
        for tier, links in diff["tiers_before"].items():
            lines.append(f"    {tier}： {' → '.join(links)}")
        lines.append("  档位链 拆分后：")
        for tier, links in diff["tiers_after"].items():
            mark = " ←新增" if tier not in diff["tiers_before"] else ""
            lines.append(f"    {tier}： {' → '.join(links)}{mark}")
        lines.append("  逐环节钉定 现在：")
        if not diff["overrides_before"]:
            lines.append("    （没有）")
        for name, links in diff["overrides_before"].items():
            lines.append(f"    {name}： {' → '.join(links) or '跟随档位'}")
        lines.append("  逐环节钉定 拆分后：")
        if not diff["overrides_after"]:
            lines.append("    （没有）")
        for name, links in diff["overrides_after"].items():
            lines.append(f"    {name}： {' → '.join(links) or '跟随档位'}")
        if diff["removed_overrides"]:
            lines.append("    移除：" + "、".join(diff["removed_overrides"])
                         + "（改由交付物起草档位承载）")
        if diff["kept_overrides"]:
            lines.append("    保留：" + "、".join(diff["kept_overrides"]))
    if plan["publish"]:
        lines.append("")
        lines.append("将要发布（一个新的不可变策略版本，回滚就是把上一版再发布一次）：")
        for item in plan["publish"]:
            lines.append(f"  · 档位 {item['tier']}： {' → '.join(item['chain'])}")
    elif not plan["conflicts"]:
        lines.append("")
        lines.append("没有需要拆分的：交付物起草档位已经就位，逐环节钉定也已经清干净。")
    for conflict in plan["conflicts"]:
        lines.append(f"!! 需要人来决定：{conflict}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="Dalton Core state directory (holds model-router.sqlite)")
    parser.add_argument("--apply", action="store_true",
                        help="publish the split; without it the script only reads")
    parser.add_argument("--offline", action="store_true",
                        help="publish without the writer; only for a stopped host")
    parser.add_argument("--actor", default=DEFAULT_ACTOR,
                        help="the human who is making this choice")
    parser.add_argument("--chain", action="append", default=None,
                        help="name the tier's chain explicitly, first choice "
                             f"first (default: whatever {SOURCE_PURPOSE} is "
                             "pinned to today)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    state_dir = Path(os.path.expanduser(args.state_dir)).resolve()
    try:
        state = read_state(state_dir, read_only=not (args.apply and args.offline))
        plan = plan_split(state["policies"], state["profiles"], now=state["now"],
                          chain=args.chain)
    except SplitError as exc:
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
        try:
            applied = (apply_offline(state_dir, plan["publish"], actor_ref=args.actor)
                       if args.offline
                       else apply_through_writer(state_dir, plan["publish"],
                                                 actor_ref=args.actor))
        except SplitError as exc:
            print(render(plan))
            print(str(exc), file=sys.stderr)
            return 4
    if args.json:
        print(json.dumps({"state_dir": str(state_dir), "applied": bool(args.apply),
                          "plan": plan, "results": applied},
                         ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render(plan))
        if args.apply:
            print("")
            print(f"已发布 {len(applied)} 项。"
                  "再跑一次这个命令应当输出「没有需要拆分的」。"
                  "接下来可以在模型页的「交付物起草」里拖动调整顺序。")
        elif plan["publish"]:
            print("")
            print("这是一次只读预演。加 --apply 才会发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
