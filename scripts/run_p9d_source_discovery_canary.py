#!/usr/bin/env python3
"""P9d-1 canary: mission source discovery on a read-only copy of an existing Core.

Nothing here touches the source Core, the network or a paid model.  Steps:

1. Copy the source Core into a throwaway state directory.  Read the active
   mission (v1: AlphaEngine ``probe_only``, no ``source_discovery`` scope).
2. Controller-tick the discovery coordinator under v1 as the mission's
   automation principal: it must refuse with the ``probe_only`` reason and
   write nothing.
3. Run one *human-requested* discovery through the real launcher and the
   real child (``alphaengine_search_cli`` in fake-search mode, served a
   canned result: one document the source Core already holds and one it does
   not).  The child re-derives the grant, runs the governed ``search_library``
   into Core connector authority and appends the discovery record; the next
   tick settles the dispatch and leaves the new document ``discovered`` --
   automation still has no grant to acquire it.
4. Publish mission v2 in the copy (``build_mission_v2_params`` params:
   ``source_discovery`` appended, AlphaEngine promoted to ``connected``).
   Tick: the discovered document is acquired through the existing bounded
   probe launcher (real acquisition child in fake-document mode), then a
   second company's search is launched while the first waits out its
   cadence.  Settle everything.
5. Re-read progress, counts and ``PRAGMA integrity_check``.  Evidence /
   Claim / Thesis counts must be unchanged.

Usage::

    python scripts/run_p9d_source_discovery_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --acquisition-governance "$HOME/Library/Application Support/Dalton/state/dalton-core/connector-governance/alphaengine-get-document-v1.json" \
        --output temp/p9d-canary.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_mission_v2_params import build_next_version_params  # noqa: E402
from dalton_core.alphaengine_acquisition_launcher import AlphaEngineAcquisitionLauncher  # noqa: E402
from dalton_core.alphaengine_core_search import (  # noqa: E402
    SEARCH_PROFILE_REF,
    build_search_governance_record,
    search_spec_hash,
)
from dalton_core.bounded_alphaengine_probe import ALPHAENGINE_PROFILE_REF  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority  # noqa: E402
from dalton_core.mission_source_discovery import (  # noqa: E402
    AlphaEngineSearchLauncher,
    MissionSourceDiscoveryCoordinator,
    build_discovery_parameters,
    load_discovery_plan,
)
from dalton_core.store import DaltonStore, canonical_json  # noqa: E402

PLAN_PATH = ROOT / "deploy" / "phase9" / "p9d-us-it-services-discovery-plan-v1.json"
NEW_DOC_ID = "130000099999999"
COUNTED_TABLES = ("evidence_versions", "claim_versions", "thesis_versions")


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


def _known_document(store: DaltonStore) -> str:
    row = store.connection.execute(
        "SELECT record_json FROM connector_call_specs WHERE operation='get_document' "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("source Core holds no AlphaEngine document; run the AE probe first")
    return json.loads(row["record_json"])["parameters"]["document_ref"]


def _counts(store: DaltonStore) -> dict[str, int]:
    counts = {
        table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in COUNTED_TABLES
    }
    for label, profile in (("search_calls", SEARCH_PROFILE_REF), ("document_calls", ALPHAENGINE_PROFILE_REF)):
        counts[label] = int(store.connection.execute(
            "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref=?", (profile,),
        ).fetchone()[0])
    for table in ("coverage_mission_source_discoveries", "coverage_mission_discovered_documents",
                  "coverage_mission_discovery_dispatches"):
        counts[table] = int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def _tick(coordinator: MissionSourceDiscoveryCoordinator) -> dict[str, Any]:
    tick = coordinator.dispatch_once()
    return {
        "status": tick["status"],
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
    checks["plan"] = {"id": plan["id"], "hash": plan["content_hash"], "specs": [s["spec_ref"] for s in plan["specs"]]}
    with tempfile.TemporaryDirectory(prefix="dalton-p9d-canary-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        _copy_core(args.source_core, state / "core.sqlite")
        governance_dir = state / "connector-governance"
        governance_dir.mkdir(mode=0o700)
        search_governance = governance_dir / "alphaengine-search-library-v1.json"
        # Rehearsal only: an in-memory *approved* search record for the copy.
        # The committed deploy record stays ``proposed`` until the owner approves it.
        search_governance.write_text(
            canonical_json(build_search_governance_record(approved_by=args.owner, status="approved")) + "\n",
            encoding="utf-8",
        )
        acquisition_governance = governance_dir / "alphaengine-get-document-v1.json"
        shutil.copy(args.acquisition_governance, acquisition_governance)
        results_path = state / "fake-search-results.json"
        fake_document = state / "fake-document.txt"
        fake_document.write_text(
            "Operator: Good afternoon. This is a rehearsal transcript served to the "
            "acquisition child on a throwaway Core copy. " * 40,
            encoding="utf-8",
        )
        if args.source_catalog is not None:
            # 0. Demonstrate the shared-catalog epoch hazard on a copy of the
            #    live catalog: the get_document descriptor sits at epoch 1 while
            #    SEC's publish moved the catalog to epoch 2, so an acquisition
            #    against that file must refuse with StaleCatalog.
            shared_catalog = state / "catalog-shared-copy.sqlite"
            shutil.copy(args.source_catalog, shared_catalog)
            stale_launcher = AlphaEngineAcquisitionLauncher(
                state_dir=state, governance_path=acquisition_governance,
                mode_args=("--fake-document-file", str(fake_document)),
                catalog_db=shared_catalog,
            )
            try:
                stale_ticket = stale_launcher.start_bounded_probe(
                    document_ref=f"alphaengine-doc:{NEW_DOC_ID}", caller_ref="automation:coverage-mission",
                )
                stale_code = stale_launcher.wait(timeout=300)
                stale_status = stale_launcher.status(stale_ticket["id"])
                stale_log = state / "acquisitions" / stale_ticket["id"].split(":", 1)[1] / "run.log"
                checks["shared_catalog_acquisition"] = {
                    "catalog": "copy of --source-catalog",
                    "ticket_status": stale_status["status"], "exit_code": stale_code,
                    "run_log_tail": stale_log.read_text(encoding="utf-8")[-600:] if stale_log.exists() else None,
                }
            finally:
                stale_launcher.close()

        store = DaltonStore(state / "core.sqlite")
        missions = CoverageMissionAuthority(store)
        try:
            known = _known_document(store)
            results_path.write_text(json.dumps([
                {"doc_id": known.split(":", 1)[1], "title": "known document"},
                {"doc_id": NEW_DOC_ID, "title": "canary new document"},
            ]), encoding="utf-8")
            checks["known_document"] = known
            active = missions.active_mission(plan["mission_ref"])
            checks["mission_v1"] = {
                "id": active["id"], "version": active["version"],
                "alphaengine_status": next(s["status"] for s in active["source_plan"] if s["source_ref"] == "source:alphaengine"),
                "grants_source_discovery": "source_discovery" in active["autonomy"]["may_write"],
                "max_alphaengine_calls_24h": active["budget"]["max_alphaengine_calls_24h"],
            }
            checks["counts_before"] = _counts(store)

            search_launcher = AlphaEngineSearchLauncher(
                state_dir=state, governance_path=search_governance, plan_path=PLAN_PATH,
                mode_args=("--fake-search-file", str(results_path)),
            )
            acquisition_launcher = AlphaEngineAcquisitionLauncher(
                state_dir=state, governance_path=acquisition_governance,
                mode_args=("--fake-document-file", str(fake_document)),
            )
            coordinator = MissionSourceDiscoveryCoordinator(
                store=store, missions=missions, plan=plan,
                search_launcher=search_launcher, acquisition_launcher=acquisition_launcher,
            )
            try:
                # 2. automation under v1 is refused
                checks["tick_v1"] = _tick(coordinator)
                checks["counts_after_v1_tick"] = _counts(store)

                # 3. human-requested discovery through the real child
                authorization = missions.authorize_source_discovery(
                    company_ref=args.company_ref, source_ref=plan["source_ref"], requested_by=args.owner,
                )
                spec_ref = plan["specs"][0]["spec_ref"]
                as_of = datetime.now(timezone.utc).date()
                ticket = search_launcher.start(authorization=authorization, spec_ref=spec_ref, as_of=as_of)
                parameters = build_discovery_parameters(
                    plan, spec_ref=spec_ref, company_ref=args.company_ref, as_of=as_of,
                )
                dispatch = missions.record_discovery_dispatch(
                    authorization=authorization, discovery_plan_ref=plan["id"],
                    discovery_plan_hash=plan["content_hash"], spec_ref=spec_ref,
                    query_hash=search_spec_hash(parameters), ticket_ref=ticket["id"],
                )
                code = search_launcher.wait(timeout=300)
                status = search_launcher.status(ticket["id"])
                summary = status.get("summary") or {}
                run_log = state / "discoveries" / ticket["id"].split(":", 1)[1] / "run.log"
                checks["human_discovery"] = {
                    "ticket_status": status["status"], "exit_code": code,
                    "run_log_tail": run_log.read_text(encoding="utf-8")[-3000:] if run_log.exists() else None,
                    "dispatch_ref": dispatch["dispatch_id"],
                    "query": parameters["query"], "filters": parameters["filters"],
                    "summary_status": summary.get("status"),
                    "failure_reason": summary.get("failure_reason"),
                    "discovery_ref": summary.get("discovery_ref"),
                    "document_count": summary.get("document_count"),
                    "new_document_count": summary.get("new_document_count"),
                    "in_authority_document_count": summary.get("in_authority_document_count"),
                    "provider_calls": summary.get("provider_calls"),
                    "transport": summary.get("transport"),
                }
                checks["tick_after_human"] = _tick(coordinator)
                checks["documents_after_human"] = [
                    {"company_ref": d["company_ref"], "document_ref": d["document_ref"], "status": d["status"]}
                    for d in missions.discovered_documents(active["id"])
                ]

                # 4. mission v2: scope + connected
                params = build_next_version_params(
                    active, add_scopes=["source_discovery"],
                    source_statuses={"source:alphaengine": "connected"},
                )
                params["actor_ref"] = args.owner
                mission_ref = params.pop("mission_ref")
                v2 = missions.create_mission(mission_ref, **params)
                checks["mission_v2"] = {
                    "id": v2["id"], "status": v2["status"], "hash": v2["content_hash"],
                    "may_write": v2["autonomy"]["may_write"],
                    "alphaengine_status": next(s["status"] for s in v2["source_plan"] if s["source_ref"] == "source:alphaengine"),
                }
                # v1's discovery rows belong to v1; under v2 the search for the
                # first company runs again (new version, no cadence history) and
                # the new document is queued from that discovery.
                ticks: list[dict[str, Any]] = []
                # Ten company/spec pairs plus one final settlement tick.
                # Keep a twelfth slot as a guard so the canary proves the
                # coordinator reaches idle with no open child or dispatch.
                for _ in range(12):
                    tick = _tick(coordinator)
                    ticks.append(tick)
                    if search_launcher.running():
                        search_launcher.wait(timeout=300)
                    if acquisition_launcher._current is not None and acquisition_launcher._current[1].poll() is None:
                        acquisition_launcher.wait(timeout=300)
                    if (
                        tick["discovery"]["status"] in {"idle", "budget_exhausted", "not_authorized", "rejected"}
                        and tick["acquisition"]["status"] in {"idle", "budget_exhausted", "not_authorized", "rejected"}
                        and not tick["settled_dispatches"] and not tick["settled_documents"]
                    ):
                        break
                checks["ticks_under_v2"] = ticks
                failed_logs: dict[str, str] = {}
                for dispatch in missions.discovery_dispatches(v2["id"]):
                    if dispatch["status"] == "failed":
                        log = state / "discoveries" / dispatch["ticket_ref"].split(":", 1)[1] / "run.log"
                        if log.exists():
                            failed_logs[dispatch["ticket_ref"]] = log.read_text(encoding="utf-8")[-800:]
                for document in missions.discovered_documents(v2["id"], status="acquisition_failed"):
                    log = state / "acquisitions" / document["ticket_ref"].split(":", 1)[1] / "run.log"
                    if log.exists():
                        failed_logs[document["ticket_ref"]] = log.read_text(encoding="utf-8")[-800:]
                checks["failed_child_logs_under_v2"] = failed_logs
                checks["documents_under_v2"] = [
                    {"company_ref": d["company_ref"], "document_ref": d["document_ref"], "status": d["status"],
                     "failure_reason": d["failure_reason"]}
                    for d in missions.discovered_documents(v2["id"])
                ]
                checks["dispatches_under_v2"] = [
                    {"company_ref": d["company_ref"], "spec_ref": d["spec_ref"], "status": d["status"],
                     "requested_by": d["requested_by"], "failure_reason": d["failure_reason"]}
                    for d in missions.discovery_dispatches(v2["id"])
                ]
            finally:
                search_launcher.close()
                acquisition_launcher.close()

            # 5. projections + integrity
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
    v2_docs = {(d["company_ref"], d["document_ref"]): d["status"] for d in checks["documents_under_v2"]}
    new_ref = f"alphaengine-doc:{NEW_DOC_ID}"
    stale = checks.get("shared_catalog_acquisition")
    result["ok"] = all([
        stale is None or (
            stale["ticket_status"] == "failed" and "StaleCatalog" in (stale["run_log_tail"] or "")
        ),
        checks["tick_v1"]["discovery"]["status"] == "not_authorized",
        "probe_only" in checks["tick_v1"]["discovery"].get("reason", ""),
        checks["counts_after_v1_tick"] == before,
        human["ticket_status"] == "succeeded" and human["summary_status"] == "succeeded",
        human["new_document_count"] == 1 and human["in_authority_document_count"] == 1,
        human["provider_calls"] == 1,
        checks["tick_after_human"]["settled_dispatches"]
        and checks["tick_after_human"]["settled_dispatches"][0]["status"] == "succeeded",
        checks["tick_after_human"]["acquisition"]["status"] == "not_authorized",
        checks["mission_v2"]["status"] == "fresh",
        v2_docs.get((args.company_ref, new_ref)) == "acquired",
        any(d["status"] == "succeeded" and d["requested_by"].startswith("automation:")
            for d in checks["dispatches_under_v2"]),
        all(d["status"] == "succeeded" for d in checks["dispatches_under_v2"]),
        checks["ticks_under_v2"][-1]["status"] == "idle",
        not checks["ticks_under_v2"][-1]["settled_dispatches"],
        not checks["ticks_under_v2"][-1]["settled_documents"],
        after["search_calls"] >= before["search_calls"] + 2,
        after["document_calls"] >= before["document_calls"] + 1,
        all(after[t] == before[t] for t in COUNTED_TABLES),
        checks["integrity_check"] == "ok",
    ])
    result["source_core_untouched"] = True  # opened read-only; only the copy was written
    result["network_calls"] = 0
    result["paid_calls"] = 0
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--acquisition-governance", type=Path, required=True,
                        help="the live *approved* alphaengine-get-document governance record (copied)")
    parser.add_argument("--source-catalog", type=Path,
                        help="the live shared catalog.sqlite (copied) to demonstrate the StaleCatalog hazard")
    parser.add_argument("--company-ref", default="company:sec-cik:0001467373")
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
