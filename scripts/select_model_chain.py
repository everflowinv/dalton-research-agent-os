#!/usr/bin/env python3
"""Publish one stage's explicit model chain, through each environment's writer.

The cockpit's 「模型与预算」 page is the owner's door for this; this script is
the same door for a selection that has to be written down and reviewed before
it is made (a ``docs/ops`` step).  It makes exactly the call the page makes --
the ``set_model_selection`` writer operation, through the environment's own
writer socket with a one-operation human token -- so every environment appends
its own immutable routing-policy version with the owner as the actor, and the
rollback is publishing the previous chain.  Nothing writes a database here.

Environments are found the way the page's fan-out finds them
(``model_routing_sync.host_environments``); ``--state-dir`` narrows the run to
the one environment that owns that state directory.  The same code runs for
every environment: there is no per-environment branch.

Dry run unless ``--apply``.  The dry run reads each environment's router
read-only and prints, per environment: what the stage runs now, whether it is
already on the requested chain, and whether that router would accept it.

    scripts/select_model_chain.py --purpose research_localization \\
        --chain profile:zai-glm-5-3-flash,profile:deepseek-v4-flash
    scripts/select_model_chain.py --purpose research_localization \\
        --chain profile:zai-glm-5-3-flash,profile:deepseek-v4-flash \\
        --apply --actor human:lumos
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.model_routing_sync import (  # noqa: E402
    Environment,
    ModelRoutingSyncError,
    _default_writer_call,
    _selection_landed,
    environment_for_state_dir,
    host_environments,
    selection_params,
)

DEFAULT_MANAGER_CONFIG = "~/.dalton/manager.json"


def _current(environment: Environment, purpose: str) -> dict[str, Any]:
    """What ``purpose`` runs in this environment now, read-only."""

    from dalton_core.model_fallback_chain import effective_chain, profile_families
    from dalton_core.model_routing_sync import latest_policies
    from dalton_core.model_router import ModelRouter

    chains: dict[str, list[str]] = {}
    with ModelRouter(str(environment.router_db), read_only=True) as router:
        held = profile_families(router)
    for policy_id, policy in latest_policies(environment.router_db).items():
        effective = effective_chain(policy, purpose, profiles=held)
        chains[policy_id] = [str(item) for item in effective.get("chain") or ()]
    return chains


def _validate(environment: Environment, purpose: str, chain: Sequence[str]) -> str | None:
    from dalton_core.model_fallback_chain import FallbackChainError, validate_selection
    from dalton_core.model_router import ModelRouter

    try:
        with ModelRouter(str(environment.router_db), read_only=True) as router:
            validate_selection(router, purpose=purpose, mode="explicit", chain=list(chain))
    except FallbackChainError as exc:
        return str(exc)
    return None


def plan(environments: Sequence[Environment], *, purpose: str,
         chain: Sequence[str]) -> list[dict[str, Any]]:
    params = selection_params(purpose=purpose, mode="explicit", chain=chain)
    rows = []
    for environment in environments:
        row: dict[str, Any] = {"environment": environment.environment_id,
                               "name": environment.name,
                               "state_dir": str(environment.state_dir),
                               "writer_running": environment.is_running}
        if not Path(environment.router_db).is_file():
            rows.append({**row, "status": "no_router"})
            continue
        try:
            row["current"] = _current(environment, purpose)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            row["current_error"] = str(exc)
        refusal = _validate(environment, purpose, chain)
        if refusal is not None:
            rows.append({**row, "status": "refused", "reason": refusal})
            continue
        rows.append({**row, "status": ("selected" if _selection_landed(environment, params)
                                       else "to_publish")})
    return rows


def apply(rows: list[dict[str, Any]], environments: Sequence[Environment], *,
          purpose: str, chain: Sequence[str], actor_ref: str,
          writer_call: Callable[..., Any] = _default_writer_call) -> list[dict[str, Any]]:
    params = selection_params(purpose=purpose, mode="explicit", chain=chain)
    by_id = {item.environment_id: item for item in environments}
    out = []
    for row in rows:
        if row["status"] != "to_publish":
            out.append(row)
            continue
        environment = by_id[row["environment"]]
        if not environment.is_running:
            out.append({**row, "status": "writer_not_running"})
            continue
        try:
            result = writer_call(environment, actor_ref=actor_ref, params=params)
        except Exception as exc:  # noqa: BLE001 - one environment must not stop the rest
            out.append({**row, "status": "failed", "reason": str(exc)})
            continue
        out.append({**row, "status": "published", "result": result})
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--chain", required=True,
                        help="profile ids, first choice first, comma separated")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="only the environment that owns this state directory")
    parser.add_argument("--manager-config", type=Path,
                        default=Path(DEFAULT_MANAGER_CONFIG).expanduser())
    parser.add_argument("--legacy-service", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", default=None, help="human:<owner>, required with --apply")
    args = parser.parse_args(argv)
    chain = [item.strip() for item in args.chain.split(",") if item.strip()]
    if not chain:
        parser.error("--chain names no model")
    if args.apply and not (args.actor or "").startswith("human:"):
        parser.error("--apply needs --actor human:<owner>")
    try:
        environments = host_environments(args.manager_config,
                                         legacy_service_path=args.legacy_service)
    except ModelRoutingSyncError as exc:
        print(f"找不到研究环境清单：{exc}", file=sys.stderr)
        return 2
    if args.state_dir is not None:
        chosen = environment_for_state_dir(environments, args.state_dir)
        if chosen is None:
            print(f"{args.state_dir} 不是这台机器上任何研究环境的状态目录", file=sys.stderr)
            return 2
        environments = [chosen]
    rows = plan(environments, purpose=args.purpose, chain=chain)
    if args.apply:
        rows = apply(rows, environments, purpose=args.purpose, chain=chain,
                     actor_ref=args.actor)
    print(json.dumps({"purpose": args.purpose, "chain": chain,
                      "mode": "apply" if args.apply else "dry_run",
                      "environments": rows}, ensure_ascii=False, indent=1, default=str))
    bad = {"refused", "failed", "writer_not_running"}
    return 1 if any(row["status"] in bad for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
