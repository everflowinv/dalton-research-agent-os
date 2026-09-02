#!/usr/bin/env python3
"""P9c canary: forecast reconciliation on a read-only copy of an existing Core.

Nothing here touches the source Core, the network or a paid model.  Steps:

1. Copy the source Core.  Confirm the live forecast lines have no actual yet
   (no pending pair: the FY2026 lines wait for their filings).
2. Publish two rehearsal ``estimate`` forecast lines (human, clearly labelled)
   for the fiscal quarter the 10-K fixture reports, so a real SEC actual can
   be reconciled: one inside the notable band, one that crosses the
   overturn threshold.
3. Run the 10-K company-facts lane as ``automation:coverage-mission`` against
   the companyfacts fixture (same path as the P9b canary).  With the copied
   mission v1 (no ``forecast_reconciliation`` scope) the lane must commit the
   Claim and report the reconciliation as *skipped*, not silently omit it.
4. Publish mission v2 in the copy with the scope appended (the exact params
   ``scripts/build_mission_v2_params.py`` would hand to ``dalton-gov``), then
   reconcile the pending pairs as the controller tick would: two records,
   one raising the ``forecast_overturn`` checkpoint; a human decides it.
5. Re-read through CompanyResearchView and the integrity report.

Usage::

    python scripts/run_p9c_forecast_reconciliation_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --fixture temp/acn-companyfacts.json --output temp/p9c-canary.json
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
sys.path.insert(0, str(ROOT / "scripts"))

from build_mission_v2_params import build_next_version_params  # noqa: E402
from dalton_core.company_research_view import build_company_research_view  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority, CoverageMissionError  # noqa: E402
from dalton_core.forecast_reconciliation import ForecastReconciliationAuthority  # noqa: E402
from dalton_core.model_forecast import ModelForecastAuthority  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402

POLICY_CANDIDATE = ROOT / "deploy" / "phase1" / "governance-policy-v4-company-facts-annual.candidate.params.json"
CANARY_PERIOD = {"start": "2025-06-01", "end": "2025-08-31", "calendar": "company:fiscal", "kind": "quarter"}
CANARY_LINES = (
    ("forecast-line:canary:acn:revenue:q4fy25:notable", "17300"),
    ("forecast-line:canary:acn:revenue:q4fy25:overturn", "16800"),
)


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


def _scenario(store: DaltonStore) -> tuple[str, str]:
    row = store.connection.execute(
        "SELECT v.version_id, v.content_hash FROM model_input_versions v "
        "JOIN model_input_pointer p ON p.version_id=v.version_id "
        "WHERE v.input_kind='scenario' ORDER BY v.version_id LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("source Core has no admitted scenario; publish one before the canary")
    return row["version_id"], row["content_hash"]


def run(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "checks": {}}
    checks = result["checks"]
    with tempfile.TemporaryDirectory(prefix="dalton-p9c-canary-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        _copy_core(args.source_core, state / "core.sqlite")
        company_ref = f"company:sec-cik:{int(args.cik):010d}"

        store = DaltonStore(state / "core.sqlite")
        try:
            missions = CoverageMissionAuthority(store)
            active = missions.active_mission(args.mission_ref)
            checks["mission_v1"] = {
                "id": active["id"], "version": active["version"],
                "grants_forecast_reconciliation": "forecast_reconciliation" in active["autonomy"]["may_write"],
                "has_forecast_overturn_checkpoint": "forecast_overturn" in active["autonomy"]["human_checkpoints"],
            }
            reconciler = ForecastReconciliationAuthority(store)
            checks["live_lines_pending_pairs_before"] = len(reconciler.pending_pairs(company_ref))
            checks["reconciliations_before"] = store.connection.execute(
                "SELECT COUNT(*) FROM forecast_reconciliations"
            ).fetchone()[0]
            forecast = ModelForecastAuthority(store)
            scenario_ref, scenario_hash = _scenario(store)
            published = []
            for line_ref, value in CANARY_LINES:
                line = forecast.publish_line(
                    line_ref, subject_ref=company_ref, metric_or_aspect="metric:revenue-usd",
                    period=CANARY_PERIOD, unit="million", currency="USD", value=value,
                    value_kind="estimate", scenario_version_ref=scenario_ref,
                    scenario_version_hash=scenario_hash, actor_ref=args.owner,
                    rationale="P9c canary rehearsal estimate on a throwaway Core copy; not a research view.",
                    version_id=f"forecast-line-version:{line_ref.split('forecast-line:')[-1]}:1",
                    prior_version_ref=None, idempotency_key=f"p9c-canary:{line_ref}",
                )
                published.append({"ref": line["id"], "value": line["value"]})
            checks["canary_lines"] = published
            policy = store.active_policy()
            candidate = json.loads(POLICY_CANDIDATE.read_text(encoding="utf-8"))
            if policy["policy_version_id"] != candidate["policy_version_id"]:
                raise RuntimeError("copy is not on policy-4; run the P9b canary path first")
            authorization = missions.sec_lane_authorization_for_company(company_ref)
            claims_before = store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        finally:
            store.close()

        summary_dir = state / "summary"
        command = [
            sys.executable, "-m", "dalton_core.sec_lane_cli",
            "--state-dir", str(state), "--staging", str(state / "candidate-staging.sqlite"),
            "--rehearsal-approved-by", args.owner,
            "--issuer", args.issuer, "--issuer-cik", f"{args.issuer}={args.cik}",
            "--filed-from", args.filed_from, "--filed-to", args.filed_to, "--form", "10-K",
            "--actor", authorization["actor_ref"],
            "--fixture-company-facts", str(args.fixture),
            "--summary-dir", str(summary_dir), "--quiet",
            "--expected-accession", args.expected_accession,
            "--mission-version-ref", authorization["mission_version_ref"],
            "--mission-version-hash", authorization["mission_version_hash"],
            "--mission-company-ref", authorization["company_ref"],
        ]
        proc = subprocess.run(
            command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True,
        )
        checks["lane_exit_code"] = proc.returncode
        checks["lane_stderr_tail"] = proc.stderr[-2000:]
        lane: dict[str, Any] = {}
        summary_path = summary_dir / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            issuer = summary["issuers"][0]
            lane = {
                "ok": summary["ok"], "status": issuer.get("status"),
                "facts": issuer.get("facts"),
                "candidate_period": (issuer.get("candidate") or {}).get("period"),
                "mission_stage_claim_status": (issuer.get("mission_stage_claim") or {}).get("status"),
                "forecast_reconciliation": issuer.get("forecast_reconciliation"),
            }
        checks["lane"] = lane

        store = DaltonStore(state / "core.sqlite")
        try:
            claims_after = store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
            checks["claims_before_after"] = [claims_before, claims_after]
            missions = CoverageMissionAuthority(store)
            reconciler = ForecastReconciliationAuthority(store)
            checks["pending_pairs_under_v1"] = len(reconciler.pending_pairs(company_ref))
            checks["reconciliations_after_lane"] = store.connection.execute(
                "SELECT COUNT(*) FROM forecast_reconciliations"
            ).fetchone()[0]
            try:
                missions.authorize_forecast_reconciliation(company_ref=company_ref)
                checks["v1_authorizes_reconciliation"] = True
            except CoverageMissionError as exc:
                checks["v1_authorizes_reconciliation"] = f"{type(exc).__name__}: {exc}"

            active = missions.active_mission(args.mission_ref)
            params = build_next_version_params(active, add_scopes=["forecast_reconciliation"])
            params["actor_ref"] = args.owner
            mission_ref = params.pop("mission_ref")
            v2 = missions.create_mission(mission_ref, **params)
            checks["mission_v2"] = {
                "id": v2["id"], "status": v2["status"], "hash": v2["content_hash"],
                "may_write": v2["autonomy"]["may_write"],
            }
            tick = reconciler.reconcile_pending(
                requested_by=v2["autonomy"]["automation_principal"],
                mission_resolver=lambda ref: missions.authorize_forecast_reconciliation(
                    company_ref=ref, actor_ref=v2["autonomy"]["automation_principal"],
                ),
            )
            checks["tick_under_v2"] = {
                "status": tick["status"],
                "created": [
                    {
                        "ref": item["id"], "forecast_value": item["forecast_value"],
                        "actual_value": item["actual_value"], "unit": item["unit"],
                        "deviation_percent": item["deviation_percent"],
                        "direction": item["direction"], "band": item["band"],
                        "human_checkpoint": item["human_checkpoint"],
                        "mission_version_ref": item["mission_binding"]["mission_version_ref"],
                        "requested_by": item["requested_by"], "actor_ref": item["actor_ref"],
                    }
                    for item in tick["created"]
                ],
                "skipped": tick["skipped"],
            }
            replay = reconciler.reconcile_pending(
                requested_by=v2["autonomy"]["automation_principal"],
                mission_resolver=lambda ref: missions.authorize_forecast_reconciliation(
                    company_ref=ref, actor_ref=v2["autonomy"]["automation_principal"],
                ),
            )
            checks["tick_replay_status"] = replay["status"]
            candidates = [item for item in tick["created"] if item["human_checkpoint"] == "forecast_overturn"]
            if candidates:
                decision = reconciler.decide_overturn(
                    reconciliation_ref=candidates[0]["id"],
                    reconciliation_hash=candidates[0]["content_hash"],
                    decision="keep_forecast",
                    rationale="P9c canary: rehearsal estimate was deliberately low; no live forecast changed.",
                    actor_ref=args.owner, idempotency_key="p9c-canary:decide:1",
                )
                checks["overturn_decision"] = {
                    "status": decision["status"],
                    "checkpoint_status": reconciler.checkpoint_status(candidates[0]["id"]),
                }
            view = build_company_research_view(store, company_ref)
            checks["company_research_view"] = {
                "forecast_reconciliation_count": len(view["forecast_reconciliations"]),
                "bands": sorted(item["band"] for item in view["forecast_reconciliations"]),
                "last_research_stop_kind": (view["last_research_stop"] or {}).get("kind"),
            }
            checks["integrity"] = {
                "reconciliation": reconciler.integrity_report(),
                "core": store.connection.execute("PRAGMA integrity_check").fetchone()[0],
            }
            checks["live_forecast_lines_still_unreconciled"] = store.connection.execute(
                "SELECT COUNT(*) FROM model_forecast_line_versions WHERE line_ref NOT LIKE 'forecast-line:canary:%'"
            ).fetchone()[0] - store.connection.execute(
                "SELECT COUNT(*) FROM forecast_reconciliations r JOIN model_forecast_line_versions v "
                "ON v.version_id=r.forecast_line_version_ref WHERE v.line_ref NOT LIKE 'forecast-line:canary:%'"
            ).fetchone()[0]
        finally:
            store.close()

    lane_outcome = (checks.get("lane") or {}).get("forecast_reconciliation") or {}
    tick_created = checks.get("tick_under_v2", {}).get("created", [])
    result["ok"] = (
        checks["lane_exit_code"] == 0
        and checks["lane"].get("status") == "committed"
        and checks["claims_before_after"][1] == checks["claims_before_after"][0] + 1
        and checks["reconciliations_after_lane"] == checks["reconciliations_before"]
        and lane_outcome.get("status") == "skipped"
        and all("forecast_reconciliation" in item["reason"] for item in lane_outcome.get("skipped", []))
        and checks["v1_authorizes_reconciliation"] is not True
        and checks["mission_v2"]["status"] == "fresh"
        and checks["tick_under_v2"]["status"] == "reconciled"
        and len(tick_created) == len(CANARY_LINES)
        and {item["band"] for item in tick_created} == {"notable", "overturn_candidate"}
        and checks["tick_replay_status"] == "idle"
        and checks.get("overturn_decision", {}).get("checkpoint_status") == "decided:keep_forecast"
        and checks["company_research_view"]["forecast_reconciliation_count"] == len(CANARY_LINES)
        and checks["integrity"]["reconciliation"]["status"] == "ok"
        and checks["integrity"]["core"] == "ok"
        and checks["live_lines_pending_pairs_before"] == 0
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--issuer", default="ACN")
    parser.add_argument("--cik", default="1467373")
    parser.add_argument("--filed-from", default="2025-09-01")
    parser.add_argument("--filed-to", default="2025-12-31")
    parser.add_argument("--expected-accession", default="0001467373-25-000217")
    parser.add_argument("--mission-ref", default="coverage-mission:us-it-services")
    parser.add_argument("--owner", default="human:lumos")
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
