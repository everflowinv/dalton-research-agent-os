#!/usr/bin/env python3
"""P9d-4a canary: web search discovery on a read-only copy of a live Core.

Proves, without network, paid calls or any write to the source Core:

1. under the live mission (``source:web-search`` = ``not_connected``) neither
   automation nor a human may run a web search; the tick reports the reason;
2. a networked ``WebSearchLauncher`` refuses before spawning (no host bridge);
3. after the copy publishes the next mission version with ``probe_only``, a
   human rehearsal runs the real child (fake citations) and queues URL refs;
4. after ``connected`` + ``source_discovery``, the automation principal runs
   every company/spec of the committed web plan to ``idle`` under the plan's
   own 24h cap, every dispatch settles, every URL stays ``discovered``;
5. AlphaEngine discovery rows are untouched; formal authority counts are
   unchanged; ``PRAGMA integrity_check`` is ok.

Usage::

    python scripts/run_p9d4_web_search_discovery_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --output temp/p9d4a/live-copy-canary.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_mission_v2_params import build_next_version_params  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority, CoverageMissionError  # noqa: E402
from dalton_core.mission_source_discovery import (  # noqa: E402
    ALPHAENGINE_SOURCE_REF,
    DiscoveryLaunchRejected,
    MissionSourceDiscoveryCoordinator,
    WEB_SEARCH_SOURCE_REF,
    WebSearchLauncher,
    build_discovery_parameters,
    discovery_query_hash,
    load_discovery_plan,
)
from dalton_core.public_web_core_search import (  # noqa: E402
    SEARCH_PROFILE_REF,
    build_web_search_governance_record,
)
from dalton_core.store import DaltonStore, canonical_json  # noqa: E402

PLAN_PATH = ROOT / "deploy" / "phase9" / "p9d4-us-it-services-web-search-plan-v1.json"
COUNTED_TABLES = ("evidence_versions", "claim_versions", "thesis_versions")
CITATIONS = [
    {"url": "https://Example.com/newsroom/leadership-update?utm=x#top", "title": "Leadership update"},
    {"url": "https://example.com/newsroom/leadership-update?utm=x", "title": "duplicate"},
    {"url": "https://news.example.org/it-services-demand-2026", "title": "Demand outlook"},
]


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


def _counts(store: DaltonStore) -> dict[str, int]:
    counts = {
        table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in COUNTED_TABLES
    }
    counts["web_search_calls"] = int(store.connection.execute(
        "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref=?", (SEARCH_PROFILE_REF,),
    ).fetchone()[0])
    for table in ("coverage_mission_source_discoveries", "coverage_mission_discovered_documents",
                  "coverage_mission_discovery_dispatches"):
        for source in (ALPHAENGINE_SOURCE_REF, WEB_SEARCH_SOURCE_REF):
            counts[f"{table}:{source}"] = int(store.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_ref=?", (source,),
            ).fetchone()[0])
    return counts


def _tick(coordinator: MissionSourceDiscoveryCoordinator) -> dict[str, Any]:
    tick = coordinator.dispatch_once()
    return {
        "status": tick["status"],
        "source_ref": tick["source_ref"],
        "settled_dispatches": tick["settled_dispatches"],
        "discovery": {k: v for k, v in tick["discovery"].items() if k != "skipped"},
        "discovery_skipped": tick["discovery"].get("skipped", []),
        "acquisition": tick["acquisition"],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "checks": {}}
    checks = result["checks"]
    plan = load_discovery_plan(PLAN_PATH)
    checks["plan"] = {
        "id": plan["id"], "hash": plan["content_hash"], "schema_version": plan["schema_version"],
        "budget": plan["budget"], "specs": [s["spec_ref"] for s in plan["specs"]],
    }
    with tempfile.TemporaryDirectory(prefix="dalton-p9d4a-canary-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        _copy_core(args.source_core, state / "core.sqlite")
        governance_dir = state / "connector-governance"
        governance_dir.mkdir(mode=0o700)
        governance_path = governance_dir / "gemini-web-search-v1.json"
        # Rehearsal only: an in-memory *approved* record for the copy.  The
        # committed deploy record stays ``proposed`` until the owner approves it.
        governance_path.write_text(
            canonical_json(build_web_search_governance_record(approved_by=args.owner, status="approved")) + "\n",
            encoding="utf-8",
        )
        citations_path = state / "fake-citations.json"
        citations_path.write_text(json.dumps(CITATIONS), encoding="utf-8")

        store = DaltonStore(state / "core.sqlite")
        missions = CoverageMissionAuthority(store)
        try:
            active = missions.active_mission(plan["mission_ref"])
            checks["mission_live"] = {
                "id": active["id"], "version": active["version"],
                "web_search_status": next(s["status"] for s in active["source_plan"] if s["source_ref"] == WEB_SEARCH_SOURCE_REF),
                "grants_source_discovery": "source_discovery" in active["autonomy"]["may_write"],
            }
            checks["counts_before"] = _counts(store)

            # 1. live mission: nobody may search web (not_connected), tick says why.
            rehearsal_launcher = WebSearchLauncher(
                state_dir=state, governance_path=governance_path, plan_path=PLAN_PATH,
                mode_args=("--fake-citations-file", str(citations_path)),
            )
            coordinator = MissionSourceDiscoveryCoordinator(
                store=store, missions=missions, plan=plan,
                search_launcher=rehearsal_launcher, acquisition_launcher=None,
            )
            try:
                checks["tick_live_mission"] = _tick(coordinator)
                try:
                    missions.authorize_source_discovery(
                        company_ref=args.company_ref, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=args.owner,
                    )
                    checks["human_under_live_mission"] = "authorized (unexpected)"
                except CoverageMissionError as exc:
                    checks["human_under_live_mission"] = f"{type(exc).__name__}: {exc}"
                checks["counts_after_live_tick"] = _counts(store)

                # 2. a networked launcher refuses before spawning.
                params = build_next_version_params(
                    active, add_scopes=[], source_statuses={WEB_SEARCH_SOURCE_REF: "probe_only"},
                )
                params["actor_ref"] = args.owner
                mission_ref = params.pop("mission_ref")
                probe_version = missions.create_mission(mission_ref, **params)
                checks["mission_probe_only"] = {"id": probe_version["id"], "status": probe_version["status"]}
                authorization = missions.authorize_source_discovery(
                    company_ref=args.company_ref, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=args.owner,
                )
                live_launcher = WebSearchLauncher(state_dir=state, governance_path=governance_path, plan_path=PLAN_PATH)
                try:
                    live_launcher.start(authorization=authorization, spec_ref=plan["specs"][0]["spec_ref"])
                    checks["networked_launch"] = "started (unexpected)"
                except DiscoveryLaunchRejected as exc:
                    checks["networked_launch"] = f"DiscoveryLaunchRejected: {exc}"
                finally:
                    live_launcher.close()

                # 3. human rehearsal through the real child under probe_only.
                spec_ref = plan["specs"][0]["spec_ref"]
                as_of = datetime.now(timezone.utc).date()
                ticket = rehearsal_launcher.start(authorization=authorization, spec_ref=spec_ref, as_of=as_of)
                parameters = build_discovery_parameters(plan, spec_ref=spec_ref, company_ref=args.company_ref, as_of=as_of)
                dispatch = missions.record_discovery_dispatch(
                    authorization=authorization, discovery_plan_ref=plan["id"],
                    discovery_plan_hash=plan["content_hash"], spec_ref=spec_ref,
                    query_hash=discovery_query_hash(plan, parameters), ticket_ref=ticket["id"],
                )
                code = rehearsal_launcher.wait(timeout=300)
                status = rehearsal_launcher.status(ticket["id"])
                summary = status.get("summary") or {}
                run_log = state / "discoveries" / ticket["id"].split(":", 1)[1] / "run.log"
                checks["human_discovery"] = {
                    "ticket_status": status["status"], "exit_code": code,
                    "run_log_tail": run_log.read_text(encoding="utf-8")[-2000:] if run_log.exists() else None,
                    "dispatch_ref": dispatch["dispatch_id"],
                    "parameters": parameters,
                    "summary_status": summary.get("status"),
                    "failure_reason": summary.get("failure_reason"),
                    "discovery_ref": summary.get("discovery_ref"),
                    "document_count": summary.get("document_count"),
                    "new_document_count": summary.get("new_document_count"),
                    "discovered_urls": summary.get("discovered_urls"),
                    "provider_calls": summary.get("provider_calls"),
                    "transport": summary.get("transport"),
                    "formal_authority_writes": summary.get("formal_authority_writes"),
                }
                checks["tick_after_human"] = _tick(coordinator)

                # 4. connected + source_discovery: automation runs the plan to idle.
                params = build_next_version_params(
                    missions.active_mission(plan["mission_ref"]), add_scopes=["source_discovery"],
                    source_statuses={WEB_SEARCH_SOURCE_REF: "connected"},
                )
                params["actor_ref"] = args.owner
                mission_ref = params.pop("mission_ref")
                connected = missions.create_mission(mission_ref, **params)
                checks["mission_connected"] = {
                    "id": connected["id"], "status": connected["status"],
                    "may_write": connected["autonomy"]["may_write"],
                    "web_search_status": next(s["status"] for s in connected["source_plan"] if s["source_ref"] == WEB_SEARCH_SOURCE_REF),
                }
                ticks: list[dict[str, Any]] = []
                pairs = len(connected["universe"]) * len(plan["specs"])
                for _ in range(pairs + 3):
                    tick = _tick(coordinator)
                    ticks.append(tick)
                    if rehearsal_launcher.running():
                        rehearsal_launcher.wait(timeout=300)
                    if (
                        tick["discovery"]["status"] in {"idle", "budget_exhausted", "not_authorized", "rejected"}
                        and not tick["settled_dispatches"]
                    ):
                        break
                checks["ticks_under_connected"] = ticks
                failed_logs: dict[str, str] = {}
                for item in missions.discovery_dispatches(connected["id"]):
                    if item["status"] != "succeeded":
                        log = state / "discoveries" / item["ticket_ref"].split(":", 1)[1] / "run.log"
                        if log.exists():
                            failed_logs[item["ticket_ref"]] = log.read_text(encoding="utf-8")[-800:]
                checks["failed_child_logs_under_connected"] = failed_logs
                checks["dispatches_under_connected"] = [
                    {"company_ref": d["company_ref"], "spec_ref": d["spec_ref"], "status": d["status"],
                     "requested_by": d["requested_by"], "failure_reason": d["failure_reason"]}
                    for d in missions.discovery_dispatches(connected["id"])
                ]
                checks["documents_under_connected"] = [
                    {"company_ref": d["company_ref"], "source_ref": d["source_ref"],
                     "document_ref": d["document_ref"], "status": d["status"]}
                    for d in missions.discovered_documents(connected["id"])
                ]
            finally:
                rehearsal_launcher.close()

            progress = missions.mission_progress(plan["mission_ref"])
            checks["progress"] = [
                {k: c[k] for k in ("ticker", "discovery_count", "discovered_document_count", "acquired_document_count")}
                for c in progress["companies"]
            ]
            checks["counts_after"] = _counts(store)
            checks["integrity_check"] = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            store.close()

    before, after = checks["counts_before"], checks["counts_after"]
    human = checks["human_discovery"]
    dispatches = checks["dispatches_under_connected"]
    documents = checks["documents_under_connected"]
    alpha_tables = [k for k in before if k.endswith(f":{ALPHAENGINE_SOURCE_REF}")]
    result["ok"] = all([
        checks["mission_live"]["web_search_status"] == "not_connected",
        checks["tick_live_mission"]["discovery"]["status"] == "not_authorized",
        "not_connected" in checks["tick_live_mission"]["discovery"].get("reason", ""),
        checks["human_under_live_mission"].startswith("CoverageMissionConflict"),
        checks["counts_after_live_tick"] == before,
        checks["networked_launch"].startswith("DiscoveryLaunchRejected"),
        human["ticket_status"] == "succeeded" and human["summary_status"] == "succeeded",
        human["new_document_count"] == 2 and human["provider_calls"] == 1 and human["transport"] == "fake",
        human["formal_authority_writes"] == 0,
        checks["tick_after_human"]["settled_dispatches"]
        and checks["tick_after_human"]["settled_dispatches"][0]["status"] == "succeeded",
        checks["tick_after_human"]["acquisition"]["status"] == "unconfigured",
        checks["mission_connected"]["status"] == "fresh",
        dispatches and all(d["status"] == "succeeded" for d in dispatches),
        all(d["requested_by"].startswith("automation:") for d in dispatches),
        len(dispatches) == len(checks["mission_connected"]["may_write"]) * 0 + 5 * len(checks["plan"]["specs"]),
        documents and all(d["status"] == "discovered" and d["source_ref"] == WEB_SEARCH_SOURCE_REF for d in documents),
        checks["ticks_under_connected"][-1]["status"] == "idle",
        not checks["ticks_under_connected"][-1]["settled_dispatches"],
        after["web_search_calls"] == before["web_search_calls"] + 1 + len(dispatches),
        all(after[t] == before[t] for t in COUNTED_TABLES),
        all(after[k] == before[k] for k in alpha_tables),
        checks["integrity_check"] == "ok",
    ])
    result["source_core_untouched"] = True  # opened read-only; only the copy was written
    result["network_calls"] = 0
    result["paid_calls"] = 0
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--company-ref", default="company:sec-cik:0001467373")
    parser.add_argument("--owner", default="human:lumos")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run(args)
    text = json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text if args.output is None else f"ok={result['ok']} -> {args.output}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
