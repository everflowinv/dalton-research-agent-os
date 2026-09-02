#!/usr/bin/env python3
"""P9b canary: 10-K company-facts lane on a read-only copy of an existing Core.

Three checks, none of which touch the source Core, the network or a paid model:

1. **Template registry compatibility** -- every historical ResearchPlanVersion
   in the copy is re-read through the exact-read path (which rebuilds the
   scope from the packaged SEC template and the ``SEC_TEMPLATE_REGISTRY``).
   A widened company-facts output contract must leave old plans readable.
2. **Annual policy candidate** -- the ``policy-4`` candidate params file is
   installed on the copy as ``human:<actor>``; the lane precondition check
   must accept a policy that lists the quarterly and the annual rules.
3. **Annual lane run** -- ``dalton_core.sec_lane_cli`` runs against the copy
   with ``--form 10-K`` and a companyfacts fixture (for example a saved
   ``data.sec.gov`` payload for Accenture), in rehearsal governance mode.
   The summary must be ``committed`` with the annual rule ref and a
   fourth-quarter period; re-reading every plan afterwards must still pass.

Usage::

    python scripts/run_p9b_annual_lane_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --fixture temp/acn-companyfacts.json --issuer ACN --cik 1467373 \
        --filed-from 2025-09-01 --filed-to 2025-12-31 --output temp/p9b-canary.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.research_plan import (  # noqa: E402
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
    ResearchPlanAuthority,
    sec_template_registry,
)
from dalton_core.sec_company_facts_lane import check_core_governance_rules  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402

POLICY_CANDIDATE = ROOT / "deploy" / "phase1" / "governance-policy-v4-company-facts-annual.candidate.params.json"


def _copy_core(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _reread_all_plans(store: DaltonStore) -> dict[str, Any]:
    plans = ResearchPlanAuthority(store)
    refs = [
        row[0] for row in store.connection.execute(
            "SELECT version_id FROM research_plan_versions ORDER BY created_at, version_id"
        ).fetchall()
    ]
    bindings: dict[str, int] = {}
    for ref in refs:
        wire = plans.plan_version(ref)  # exact read: rebuilds + revalidates scope
        key = wire["execution_scope"]["connector_profile_hash"][:12]
        bindings[key] = bindings.get(key, 0) + 1
    return {"plan_count": len(refs), "profile_hash_prefixes": bindings}


def run(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "checks": {}}
    registry = sec_template_registry()
    result["checks"]["registry"] = {
        "tags": [tag for tag, _entry in registry],
        "head_profile_hash": registry[-1][1]["connector_profile_hash"],
    }
    with tempfile.TemporaryDirectory(prefix="dalton-p9b-canary-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        _copy_core(args.source_core, state / "core.sqlite")

        store = DaltonStore(state / "core.sqlite")
        try:
            result["checks"]["historical_plans_before"] = _reread_all_plans(store)
            active = store.active_policy()
            result["checks"]["active_policy_before"] = active["policy_version_id"]
            candidate = json.loads(POLICY_CANDIDATE.read_text(encoding="utf-8"))
            if candidate["prior_version_ref"] != active["policy_version_id"]:
                raise RuntimeError(
                    "policy candidate prior_version_ref does not match the copy's active policy"
                )
            installed = store.create_policy(
                candidate["policy"],
                policy_version_id=candidate["policy_version_id"],
                version_number=candidate["version_number"],
                prior_version_ref=candidate["prior_version_ref"],
                policy_ref=candidate.get("policy_ref"),
                effective_from=candidate.get("effective_from"),
                effective_until=candidate.get("effective_until"),
                actor_ref=args.actor,
                change_reason=candidate["change_reason"],
                activate=True,
            )
            result["checks"]["policy_installed"] = installed["policy_version_id"]
            precondition = check_core_governance_rules(store)
            result["checks"]["lane_precondition_policy"] = precondition["policy_version_id"]
            claims_before = store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        finally:
            store.close()

        summary_dir = state / "summary"
        command = [
            sys.executable, "-m", "dalton_core.sec_lane_cli",
            "--state-dir", str(state),
            "--staging", str(state / "candidate-staging.sqlite"),
            "--rehearsal-approved-by", args.actor,
            "--issuer", args.issuer, "--issuer-cik", f"{args.issuer}={args.cik}",
            "--filed-from", args.filed_from, "--filed-to", args.filed_to,
            "--form", "10-K",
            "--actor", args.actor,
            "--fixture-company-facts", str(args.fixture),
            "--summary-dir", str(summary_dir), "--quiet",
        ]
        proc = subprocess.run(
            command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True,
        )
        result["checks"]["lane_exit_code"] = proc.returncode
        result["checks"]["lane_stderr_tail"] = proc.stderr[-2000:]
        summary_path = summary_dir / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            issuer = summary["issuers"][0]
            result["checks"]["lane"] = {
                "ok": summary["ok"],
                "form": summary.get("form"),
                "status": issuer.get("status"),
                "error": issuer.get("error"),
                "plan_form": (issuer.get("plan") or {}).get("parameters", {}).get("form"),
                "policy_version_ref": issuer.get("policy_version_ref"),
                "facts": issuer.get("facts"),
                "candidate": {
                    key: (issuer.get("candidate") or {}).get(key)
                    for key in ("period", "value", "unit", "normalized_statement")
                },
                "verifications": issuer.get("verifications"),
                "promotion": issuer.get("promotion"),
                "integrity": issuer.get("integrity"),
            }

        store = DaltonStore(state / "core.sqlite")
        try:
            claims_after = store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
            result["checks"]["claims_before_after"] = [claims_before, claims_after]
            result["checks"]["historical_plans_after"] = _reread_all_plans(store)
            authorization = store.connection.execute(
                "SELECT record_json FROM research_plan_policy_authorizations "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            result["checks"]["latest_authorization_rule"] = (
                json.loads(authorization[0])["rule_ref"] if authorization else None
            )
        finally:
            store.close()

    lane = result["checks"].get("lane") or {}
    result["ok"] = (
        proc.returncode == 0
        and lane.get("status") == "committed"
        and lane.get("plan_form") == "10-K"
        and result["checks"]["latest_authorization_rule"] == PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF
        and result["checks"]["claims_before_after"][1] == result["checks"]["claims_before_after"][0] + 1
        and result["checks"]["historical_plans_after"]["plan_count"]
        == result["checks"]["historical_plans_before"]["plan_count"] + 1
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True, help="saved companyfacts JSON body")
    parser.add_argument("--issuer", default="ACN")
    parser.add_argument("--cik", default="1467373")
    parser.add_argument("--filed-from", default="2025-09-01")
    parser.add_argument("--filed-to", default="2025-12-31")
    parser.add_argument("--actor", default="human:lumos")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run(args)
    text = json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
