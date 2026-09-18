#!/usr/bin/env python3
"""Install one day's budget for the whole host and bind every environment to it.

Until 2026-09-17 each research environment carried its own ``max_daily_cost_usd``
and each one reported, truthfully, that it was inside its cap.  What no page
reported was the sum.  The owner's instruction is that the daily budget is
shared, so there is now one host-owned policy file and one binding file per
environment naming it.

The policy is a host file rather than a row in any environment's database for
the reason the shared per-call cost policy is: **no single environment's writer
may own a number that binds the others.**  It is written owner-only, content
hashed, and chained by ``prior_hash`` to the revision it replaces, so lowering
a cap is an auditable event and not an edit.

The binding file sits beside each environment's day ledger, and the admission
authority finds it there without any caller passing it.  That is deliberate: a
cap that only applied at the call sites somebody remembered to wire up is not a
cap.  Writing it is a configuration change the owner makes, not a database
write -- every environment's writer still owns every one of its databases.

    # look first -- reads only, changes nothing
    scripts/bind_shared_daily_budget.py

    # then install, as the owner
    scripts/bind_shared_daily_budget.py --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.model_routing_sync import (  # noqa: E402
    ModelRoutingSyncError, host_environments,
)
from dalton_core.shared_daily_budget import (  # noqa: E402
    DEFAULT_POLICY_PATH,
    SharedDailyBudgetError,
    binding_path_for_ledger,
    binding_wire,
    load_shared_daily_budget_binding,
    load_shared_daily_budget_policy,
    policy_wire,
    write_owner_only,
)

DEFAULT_MANAGER_CONFIG = "~/.dalton/manager.json"
DEFAULT_ACTOR = "human:lumos"
#: The legacy mission's own numbers, which is what "shared" has to start at:
#: a host cap installed below what the owner had already authorised would stop
#: work that was running, and this change is about stopping *overspend*, not
#: about taking capacity away.
DEFAULT_MAX_DAILY_COST_USD = 500.0
DEFAULT_MAX_DAILY_PAID_CALLS = 100000
DEFAULT_MAX_ALPHAENGINE_CALLS_24H = 130


def plan_policy(
    policy_path: Path,
    *,
    max_daily_cost_usd: float,
    max_daily_paid_calls: int,
    max_alphaengine_calls_24h: int,
    actor_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The policy revision this run would write, or why it would write none.

    Republishing the same three numbers writes nothing.  A revision is a record
    that the owner changed a cap; one that records no change would make the
    chain useless for the only question it exists to answer.
    """

    current: dict[str, Any] | None = None
    if policy_path.is_file():
        current = load_shared_daily_budget_policy(policy_path)
    wanted = {
        "max_daily_cost_usd": max_daily_cost_usd,
        "max_daily_paid_calls": max_daily_paid_calls,
        "max_alphaengine_calls_24h": max_alphaengine_calls_24h,
    }
    if current is not None and all(
        current[key] == value for key, value in wanted.items()
    ):
        return {"status": "unchanged", "policy": current, "current": current}
    policy = policy_wire(
        **wanted,
        revision=1 if current is None else int(current["revision"]) + 1,
        prior_hash=None if current is None else current["content_hash"],
        updated_at=(now or datetime.now(timezone.utc)).isoformat(timespec="microseconds"),
        actor_ref=actor_ref,
    )
    return {"status": "new" if current is None else "revised",
            "policy": policy, "current": current}


def plan_bindings(
    manager_config_path: str | Path,
    *,
    policy_path: Path,
    policy_hash: str,
    legacy_service_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """One binding per environment, and whether it is already in place."""

    environments = host_environments(
        manager_config_path, legacy_service_path=legacy_service_path)
    rows: list[dict[str, Any]] = []
    for environment in environments:
        target = binding_path_for_ledger(environment.budget_db)
        wanted = binding_wire(
            policy_path=str(policy_path),
            policy_hash=policy_hash,
            manager_config_path=str(Path(manager_config_path).expanduser().resolve()),
            environment_id=environment.environment_id,
        )
        status = "new"
        if target.is_file():
            try:
                held = load_shared_daily_budget_binding(target)
            except SharedDailyBudgetError:
                status = "invalid"
            else:
                status = ("unchanged" if held["content_hash"] == wanted["content_hash"]
                          else "revised")
        rows.append({
            "environment_id": environment.environment_id,
            "name": environment.name,
            "binding_path": str(target),
            "ledger_present": environment.budget_db.is_file(),
            "status": status,
            "binding": wanted,
        })
    return rows


def apply_plan(policy_path: Path, policy_plan: Mapping[str, Any],
               bindings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Write the policy, then the bindings.

    Policy first: a binding that names a hash no file carries is worse than no
    binding at all, because the admission authority refuses rather than guesses.
    """

    written: list[str] = []
    if policy_plan["status"] != "unchanged":
        write_owner_only(policy_path, policy_plan["policy"])
        written.append(str(policy_path))
    for row in bindings:
        if row["status"] == "unchanged":
            continue
        write_owner_only(Path(row["binding_path"]), row["binding"])
        written.append(row["binding_path"])
    return {"written": written, "count": len(written)}


def render(policy_path: Path, policy_plan: Mapping[str, Any],
           bindings: Sequence[Mapping[str, Any]], *, applied: bool) -> str:
    policy = policy_plan["policy"]
    lines = [
        f"共享每日预算策略：{policy_path}",
        f"  每天总费用上限：${policy['max_daily_cost_usd']}",
        f"  每天付费调用上限：{policy['max_daily_paid_calls']} 次",
        f"  AlphaEngine 24 小时上限：{policy['max_alphaengine_calls_24h']} 次",
        f"  版本：第 {policy['revision']} 版（{policy_plan['status']}）",
        "",
        "绑定到以下研究环境：",
    ]
    for row in bindings:
        ledger = "" if row["ledger_present"] else "（这里还没有日账本）"
        lines.append(f"  {row['name']}（{row['environment_id']}）· {row['status']}{ledger}")
        lines.append(f"    {row['binding_path']}")
    lines.append("")
    lines.append("已写入。" if applied else "这是试运行，什么都没有写。加 --apply 才会写。")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-config", type=Path,
                        default=Path(DEFAULT_MANAGER_CONFIG))
    parser.add_argument("--policy-path", type=Path, default=Path(DEFAULT_POLICY_PATH))
    parser.add_argument("--legacy-service", type=Path, default=None)
    parser.add_argument("--max-daily-cost-usd", type=float,
                        default=DEFAULT_MAX_DAILY_COST_USD)
    parser.add_argument("--max-daily-paid-calls", type=int,
                        default=DEFAULT_MAX_DAILY_PAID_CALLS)
    parser.add_argument("--max-alphaengine-calls-24h", type=int,
                        default=DEFAULT_MAX_ALPHAENGINE_CALLS_24H)
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not str(args.actor).startswith("human:"):
        print("--actor 必须是 human: 开头的人的身份", file=sys.stderr)
        return 2
    policy_path = args.policy_path.expanduser()
    if not policy_path.is_absolute():
        print("--policy-path 必须是绝对路径", file=sys.stderr)
        return 2
    try:
        policy_plan = plan_policy(
            policy_path,
            max_daily_cost_usd=args.max_daily_cost_usd,
            max_daily_paid_calls=args.max_daily_paid_calls,
            max_alphaengine_calls_24h=args.max_alphaengine_calls_24h,
            actor_ref=args.actor,
        )
        bindings = plan_bindings(
            args.manager_config, policy_path=policy_path,
            policy_hash=policy_plan["policy"]["content_hash"],
            legacy_service_path=args.legacy_service)
    except (SharedDailyBudgetError, ModelRoutingSyncError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result: dict[str, Any] = {"policy_path": str(policy_path),
                              "policy": policy_plan, "bindings": bindings}
    if args.apply:
        result["applied"] = apply_plan(policy_path, policy_plan, bindings)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render(policy_path, policy_plan, bindings, applied=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
