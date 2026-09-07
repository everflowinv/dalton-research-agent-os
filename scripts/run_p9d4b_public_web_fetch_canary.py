#!/usr/bin/env python3
"""P9d-4b canary: web search discovery + public-web fetch on a read-only copy of a live Core.

Proves, without network, paid calls or any write to the source Core:

1. under the live mission (``source:web-search`` = ``not_connected``) neither
   automation nor a human may run a web search; the tick reports the reason;
2. a networked ``WebSearchLauncher`` refuses before spawning (no host bridge);
3. after the copy publishes the next mission version with ``probe_only``, a
   human rehearsal runs the real search child (fake citations) and queues
   URL refs, and a human rehearsal fetch runs the real fetch child (fake
   page) into authority;
4. after ``connected`` + ``source_discovery``, the automation principal runs
   every company/spec of the committed web plan and every discovered URL
   fetch to ``idle`` under the plan's shared 24h cap; every dispatch settles,
   every URL is ``acquired`` and queued for human extraction;
5. AlphaEngine discovery rows are untouched; formal authority counts are
   unchanged; ``PRAGMA integrity_check`` is ok.

Usage::

    python scripts/run_p9d4b_public_web_fetch_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --output temp/p9d4b/live-copy-canary.json
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
from dalton_core.public_web_core_fetch import (  # noqa: E402
    FETCH_PROFILE_PREFIX,
    build_web_fetch_governance_record,
)
from dalton_core.public_web_core_search import (  # noqa: E402
    SEARCH_PROFILE_REF,
    build_web_search_governance_record,
)
from dalton_core.public_web_fetch_launcher import PublicWebFetchLauncher  # noqa: E402
from dalton_core.store import DaltonStore, canonical_json  # noqa: E402

PLAN_PATH = ROOT / "deploy" / "phase9" / "p9d4-us-it-services-web-search-plan-v2.json"
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
    counts["web_fetch_calls"] = int(store.connection.execute(
        "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref LIKE ?", (f"{FETCH_PROFILE_PREFIX}:%",),
    ).fetchone()[0])
    counts["web_document_reviews"] = int(store.connection.execute(
        "SELECT COUNT(*) FROM coverage_mission_document_reviews WHERE source_ref=?", (WEB_SEARCH_SOURCE_REF,),
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
        "settled_documents": tick["settled_documents"],
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
        fetch_governance_path = governance_dir / "web-fetch-v1.json"
        fetch_governance_path.write_text(
            canonical_json(build_web_fetch_governance_record(approved_by=args.owner, status="approved")) + "\n",
            encoding="utf-8",
        )
        page_path = state / "fake-page.html"
        page_path.write_bytes(
            b"<html><body><h1>Rehearsal page</h1><p>Served to the fetch child on a throwaway Core copy; "
            b"never page content of a real site.</p></body></html>"
        )

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
            fetch_launcher = PublicWebFetchLauncher(
                state_dir=state, governance_path=fetch_governance_path,
                mode_args=("--fake-page-file", str(page_path)),
            )
            coordinator = MissionSourceDiscoveryCoordinator(
                store=store, missions=missions, plan=plan,
                search_launcher=rehearsal_launcher, acquisition_launcher=fetch_launcher,
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
                # 3b. human rehearsal fetch of the first discovered URL through the real child.
                queued = missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)
                fetch_ticket = fetch_launcher.start(document_ref=queued["document_ref"], actor_ref=args.owner)
                missions.mark_discovered_document_launched(queued["record_id"], fetch_ticket["id"])
                fetch_code = fetch_launcher.wait(timeout=300)
                fetch_status = fetch_launcher.status(fetch_ticket["id"])
                fetch_summary = fetch_status.get("summary") or {}
                fetch_log = state / "fetches" / fetch_ticket["id"].split(":", 1)[1] / "run.log"
                checks["human_fetch"] = {
                    "ticket_status": fetch_status["status"], "exit_code": fetch_code,
                    "run_log_tail": fetch_log.read_text(encoding="utf-8")[-2000:] if fetch_log.exists() else None,
                    "url_ref": queued["document_ref"],
                    "summary_status": fetch_summary.get("status"),
                    "failure_reason": fetch_summary.get("failure_reason"),
                    "canonical_url": fetch_summary.get("canonical_url"),
                    "document_ref": fetch_summary.get("document_ref"),
                    "body_bytes": fetch_summary.get("body_bytes"),
                    "raw_media_type": fetch_summary.get("raw_media_type"),
                    "provider_calls": fetch_summary.get("provider_calls"),
                    "transport": fetch_summary.get("transport"),
                    "formal_authority_writes": fetch_summary.get("formal_authority_writes"),
                }
                checks["tick_after_human_fetch"] = _tick(coordinator)

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
                # Every search yields the same two rehearsal URLs, so the copy
                # fetches at most two documents per mission version.
                for _ in range(pairs + 2 + 4):
                    tick = _tick(coordinator)
                    ticks.append(tick)
                    if rehearsal_launcher.running():
                        rehearsal_launcher.wait(timeout=300)
                    if fetch_launcher.running():
                        fetch_launcher.wait(timeout=300)
                    if (
                        tick["discovery"]["status"] in {"idle", "budget_exhausted", "not_authorized", "rejected"}
                        and tick["acquisition"]["status"] in {"idle", "budget_exhausted", "not_authorized", "rejected"}
                        and not tick["settled_dispatches"] and not tick["settled_documents"]
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
                checks["reviews_under_connected"] = [
                    {"document_ref": r["document_ref"], "state": r["state"], "source_ref": r["source_ref"]}
                    for r in missions.document_reviews(connected["id"])
                ]
            finally:
                fetch_launcher.close()
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
    fetched = checks["human_fetch"]
    reviews = checks["reviews_under_connected"]
    conditions = {
        "live_web_search_not_connected": checks["mission_live"]["web_search_status"] == "not_connected",
        "live_tick_refused": checks["tick_live_mission"]["discovery"]["status"] == "not_authorized"
        and "not_connected" in checks["tick_live_mission"]["discovery"].get("reason", ""),
        "live_human_refused": checks["human_under_live_mission"].startswith("CoverageMissionConflict"),
        "live_tick_wrote_nothing": checks["counts_after_live_tick"] == before,
        "networked_launch_refused": checks["networked_launch"].startswith("DiscoveryLaunchRejected"),
        "human_search_succeeded": human["ticket_status"] == "succeeded" and human["summary_status"] == "succeeded"
        and human["new_document_count"] == 2 and human["provider_calls"] == 1 and human["transport"] == "fake"
        and human["formal_authority_writes"] == 0,
        "human_search_settled": bool(checks["tick_after_human"]["settled_dispatches"])
        and checks["tick_after_human"]["settled_dispatches"][0]["status"] == "succeeded",
        # probe_only: automation may not fetch, and the tick says so.
        "probe_only_automation_fetch_refused": checks["tick_after_human"]["acquisition"]["status"] == "not_authorized",
        "human_fetch_succeeded": fetched["ticket_status"] == "succeeded" and fetched["summary_status"] == "succeeded"
        and fetched["provider_calls"] == 1 and fetched["transport"] == "fake" and fetched["formal_authority_writes"] == 0,
        # Under probe_only the page is acquired, but the automation principal
        # may not queue a review (P9d-2 grant), and the tick says so.
        "human_fetch_settled_without_automation_review": bool(checks["tick_after_human_fetch"]["settled_documents"])
        and checks["tick_after_human_fetch"]["settled_documents"][0]["status"] == "acquired"
        and str(checks["tick_after_human_fetch"]["settled_documents"][0].get("review_status")).startswith("not_registered:"),
        "connected_mission_published": checks["mission_connected"]["status"] == "fresh",
        "all_dispatches_succeeded_by_automation": bool(dispatches)
        and all(d["status"] == "succeeded" and d["requested_by"].startswith("automation:") for d in dispatches)
        and len(dispatches) == 5 * len(checks["plan"]["specs"]),
        "documents_acquired_or_already_held": bool(documents)
        and all(d["status"] in {"acquired", "already_in_authority"} and d["source_ref"] == WEB_SEARCH_SOURCE_REF for d in documents)
        and any(d["status"] == "acquired" for d in documents),
        # Under connected every newly acquired page enters the human queue.
        "reviews_queued": bool(reviews)
        and all(r["state"] == "awaiting_human_extraction" and r["source_ref"] == WEB_SEARCH_SOURCE_REF for r in reviews),
        "reached_idle": checks["ticks_under_connected"][-1]["status"] == "idle"
        and not checks["ticks_under_connected"][-1]["settled_dispatches"]
        and not checks["ticks_under_connected"][-1]["settled_documents"],
        "call_accounting": after["web_search_calls"] == before["web_search_calls"] + 1 + len(dispatches)
        and after["web_fetch_calls"] >= before["web_fetch_calls"] + 2
        and after["web_document_reviews"] >= before["web_document_reviews"] + 1,
        "formal_counts_unchanged": all(after[t] == before[t] for t in COUNTED_TABLES),
        "alphaengine_rows_unchanged": all(after[k] == before[k] for k in alpha_tables),
        "integrity_ok": checks["integrity_check"] == "ok",
    }
    result["conditions"] = conditions
    result["ok"] = all(conditions.values())
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
