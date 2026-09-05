#!/usr/bin/env python3
"""Rehearse document-review RPCs on SQLite backups; never write source state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.writer_client import WriterClient  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority  # noqa: E402
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError  # noqa: E402
from dalton_core.writer_server import HUMAN_GOVERNANCE_OPERATIONS, Principal, WriterServer  # noqa: E402


def snapshot(source: Path, destination: Path) -> None:
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def counts(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("claim_versions", "evidence_versions", "thesis_versions", "connector_invocations")}
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--source-staging", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new report path")
    with tempfile.TemporaryDirectory(prefix="dalton-document-review-") as folder:
        root = Path(folder)
        core, staging = root / "core.sqlite", root / "staging.sqlite"
        snapshot(args.source_core, core)
        snapshot(args.source_staging, staging)
        before = counts(core)
        store = DaltonStore(core)
        try:
            missions = CoverageMissionAuthority(store)
            backfill = missions.backfill_document_reviews("coverage-mission:us-it-services")
            assert missions.backfill_document_reviews("coverage-mission:us-it-services") == []
        finally:
            store.close()
        connection = sqlite3.connect(core)
        try:
            mission = connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                ("coverage-mission:us-it-services",),
            ).fetchone()[0]
        finally:
            connection.close()
        principals = {
            "human": Principal("human", "canary-human", HUMAN_GOVERNANCE_OPERATIONS, actor_ref="human:canary"),
            "automation": Principal("automation", "canary-auto", HUMAN_GOVERNANCE_OPERATIONS, actor_ref="automation:coverage-mission"),
        }
        server = WriterServer(core, str(root / "writer.sock"), principals, candidate_staging_path=staging)
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = WriterClient(str(root / "writer.sock"), "canary-human")
            queue = client.call("mission_document_reviews", {
                "mission_version_ref": mission, "state": "awaiting_human_extraction", "include_candidates": True,
            })
            if not queue["reviews"]:
                raise RuntimeError("no pending document reviews in source snapshot; canary cannot exercise resolution")
            row = queue["reviews"][0]
            params = {
                "review_id": row["review_id"], "expected_review_hash": row["review_hash"],
                "resolution": "dismissed", "rationale": "Isolated canary only; source queue unchanged.",
            }
            try:
                client.call("resolve_mission_document_review", {**params, "expected_review_hash": "0" * 64})
            except RemoteError:
                pass
            else:
                raise AssertionError("stale hash was accepted")
            auto = WriterClient(str(root / "writer.sock"), "canary-auto")
            try:
                auto.call("resolve_mission_document_review", params)
            except RemoteAuthorizationError:
                pass
            else:
                raise AssertionError("automation resolved a human review")
            fresh = client.call("resolve_mission_document_review", params)
            duplicate = client.call("resolve_mission_document_review", params)
            assert fresh["status"] == "fresh" and duplicate["status"] == "duplicate"
            try:
                client.call("resolve_mission_document_review", {**params, "rationale": "different"})
            except RemoteError:
                pass
            else:
                raise AssertionError("changed replay payload was accepted")
        finally:
            server.stop()
            thread.join(timeout=5)
        after = counts(core)
        connection = sqlite3.connect(core)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        assert before == after and integrity == "ok"
        report = {
            "ok": True, "mission_version_ref": mission, "pending_count_in_page": len(queue["reviews"]),
            "candidate_matches": sum(len(row["candidates"]) for row in queue["reviews"]),
            "backfilled_reviews": sum(row["status"] == "fresh" for row in backfill),
            "fresh_then_duplicate": True, "stale_hash_rejected": True,
            "changed_replay_rejected": True, "automation_rejected": True,
            "before": before, "after": after, "integrity": integrity,
            "network_calls": 0, "paid_calls": 0, "live_writes": 0,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
