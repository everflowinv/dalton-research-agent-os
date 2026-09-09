#!/usr/bin/env python3
"""Raise the thesis-impact day budget cap, and repoint everything at it.

There are two ceilings on what a day may spend and they are separate
authorities:

* the **mission's** ``max_daily_cost_usd``, cascaded from the policy and
  mandate -- raised with ``publish_extraction_authority_chain.py``;
* the **thesis-impact day policy**, an immutable versioned cap in its own
  ledger, which every lane's model configuration names by version.

Raising only the first leaves the second refusing calls, and the refusal used
to print the day policy's numbers whichever cap had actually stopped it. So
this raises the day policy and repoints the configurations that reference it,
in one step, rather than leaving them to be found one at a time.

Appends a new policy version; never edits the old one. The lanes are restarted
by the installer, not here: this writes configuration and stops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore  # noqa: E402

POLICY_ID_PREFIX = "thesis-impact-day-budget-policy:production"
MODEL_CONFIG_NAMES = (
    "document-extraction-model-config.json",
    "research-planner-model-config.json",
)


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def raise_cap(config_path: Path, *, cap_usd: float, apply: bool) -> dict[str, Any]:
    service = json.loads(config_path.read_text(encoding="utf-8"))
    thesis = service["thesis_impact"]["config"]
    budget_db = Path(thesis["budget_db"]).expanduser().resolve()
    state_dir = Path(service["core_db"]).parent
    current_ref = thesis["budget_policy_version_id"]
    cap_micros = int(round(cap_usd * 1_000_000))
    with ThesisImpactBudgetStore(str(budget_db)) as budget:
        row = budget.connection.execute(
            "SELECT policy_version_id, day_cap_micros FROM thesis_impact_budget_policies "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        latest_ref = row["policy_version_id"] if row else None
        latest_cap = int(row["day_cap_micros"]) if row else 0
        if latest_cap >= cap_micros:
            return {"status": "unchanged", "reason": "the day cap is already at least this high",
                    "policy_version_id": latest_ref, "day_cap_micros": latest_cap}
        version = int(latest_ref.rsplit(":", 1)[1]) + 1 if latest_ref else 1
        new_ref = f"{POLICY_ID_PREFIX}:{version}"
        plan = {
            "status": "planned" if not apply else "applied",
            "from": {"policy_version_id": latest_ref, "day_cap_micros": latest_cap},
            "to": {"policy_version_id": new_ref, "day_cap_micros": cap_micros},
            "service_config": str(config_path),
            "model_configs": [],
        }
        if not apply:
            return plan
        budget.register_policy(policy_version_id=new_ref, day_cap_micros=cap_micros,
                               prior_version_id=latest_ref)
    # Everything that names the policy by version has to move with it, or the
    # lane keeps spending against a cap that is no longer the current one.
    thesis["budget_policy_version_id"] = new_ref
    _write_owner_only(config_path, service)
    for name in MODEL_CONFIG_NAMES:
        target = state_dir / name
        if not target.is_file():
            continue
        model_config = json.loads(target.read_text(encoding="utf-8"))
        if model_config.get("budget_policy_ref") == new_ref:
            continue
        model_config["budget_policy_ref"] = new_ref
        _write_owner_only(target, model_config)
        plan["model_configs"].append(str(target))
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="service.json")
    parser.add_argument("--cap-usd", type=float, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="without this the change is only described")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not 0 < args.cap_usd <= 1000:
        raise SystemExit("--cap-usd must be 0 < n <= 1000")
    result = raise_cap(args.config.expanduser().resolve(),
                       cap_usd=args.cap_usd, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
