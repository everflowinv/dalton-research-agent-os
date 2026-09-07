#!/usr/bin/env python3
"""P9d-4e canary: one REAL Gemini web search through the whole Dalton chain.

This is the only script in the repo that spends a real provider call.  It runs
against a throwaway copy of the live Core, never the live Core itself:

1. copy live Core, publish a copy-only mission version with
   ``source:web-search`` = ``probe_only`` so a human rehearsal is authorized;
2. run the real search child with ``--allow-network`` against the host-owned
   OpenClaw web search broker (owner-approved governance record);
3. verify the discovery record, the URL refs, and that the stored raw artifact
   rebuilds the public-web URL authorities;
4. confirm formal authority counts are unchanged and integrity is ok.

Usage::

    python scripts/run_p9d4e_live_web_search_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --governance "$HOME/Library/Application Support/Dalton/state/dalton-core/connector-governance/gemini-web-search-v1.json" \
        --broker-socket "$HOME/.openclaw/dalton-web-search-broker.sock" \
        --broker-auth-key "$HOME/.openclaw/dalton-web-search-broker.sock.key" \
        --output temp/p9d4e/live-search-canary.json
"""

from __future__ import annotations

import argparse
import json
import shutil
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
from dalton_core.coverage_mission import CoverageMissionAuthority  # noqa: E402
from dalton_core.mission_source_discovery import (  # noqa: E402
    WEB_SEARCH_SOURCE_REF,
    WebSearchLauncher,
    build_discovery_parameters,
    discovery_query_hash,
    load_discovery_plan,
)
from dalton_core.public_web_connector import build_public_web_url_authorities  # noqa: E402
from dalton_core.public_web_core_search import SEARCH_PROFILE_REF  # noqa: E402
from dalton_core.raw_spool import RawSpool  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402

PLAN_PATH = ROOT / "deploy" / "phase9" / "p9d4-us-it-services-web-search-plan-v1.json"
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


def _counts(store: DaltonStore) -> dict[str, int]:
    counts = {t: int(store.connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in COUNTED_TABLES}
    counts["web_search_calls"] = int(store.connection.execute(
        "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref=?", (SEARCH_PROFILE_REF,),
    ).fetchone()[0])
    return counts


def run(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "checks": {}}
    checks = result["checks"]
    plan = load_discovery_plan(PLAN_PATH)
    spec_ref = args.spec_ref or plan["specs"][0]["spec_ref"]
    checks["plan"] = {"id": plan["id"], "hash": plan["content_hash"], "spec_ref": spec_ref,
                      "budget": plan["budget"]}
    with tempfile.TemporaryDirectory(prefix="dalton-p9d4e-canary-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        _copy_core(args.source_core, state / "core.sqlite")
        governance_dir = state / "connector-governance"
        governance_dir.mkdir(mode=0o700)
        governance_path = governance_dir / "gemini-web-search-v1.json"
        # The owner-approved record, copied as-is; the child re-validates it.
        shutil.copy(args.governance, governance_path)
        record = json.loads(governance_path.read_text())
        checks["governance"] = {"id": record["id"], "status": record["status"],
                                "approved_by": record["approved_by"]}
        spool_dir = state / "connector-spool"

        store = DaltonStore(state / "core.sqlite")
        missions = CoverageMissionAuthority(store)
        launcher = WebSearchLauncher(
            state_dir=state, governance_path=governance_path, plan_path=PLAN_PATH,
            spool_dir=spool_dir,
            broker_socket=args.broker_socket, broker_auth_key=args.broker_auth_key,
            broker_client_id=args.broker_client_id, broker_profile_id=args.broker_profile_id,
        )
        try:
            active = missions.active_mission(plan["mission_ref"])
            checks["counts_before"] = _counts(store)
            params = build_next_version_params(
                active, add_scopes=[], source_statuses={WEB_SEARCH_SOURCE_REF: "probe_only"},
            )
            params["actor_ref"] = args.owner
            mission_ref = params.pop("mission_ref")
            mission = missions.create_mission(mission_ref, **params)
            checks["mission_probe_only"] = {"id": mission["id"], "status": mission["status"]}

            authorization = missions.authorize_source_discovery(
                company_ref=args.company_ref, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=args.owner,
            )
            as_of = datetime.now(timezone.utc).date()
            parameters = build_discovery_parameters(
                plan, spec_ref=spec_ref, company_ref=args.company_ref, as_of=as_of,
            )
            checks["query"] = parameters
            ticket = launcher.start(authorization=authorization, spec_ref=spec_ref, as_of=as_of)
            missions.record_discovery_dispatch(
                authorization=authorization, discovery_plan_ref=plan["id"],
                discovery_plan_hash=plan["content_hash"], spec_ref=spec_ref,
                query_hash=discovery_query_hash(plan, parameters), ticket_ref=ticket["id"],
            )
            code = launcher.wait(timeout=600)
            status = launcher.status(ticket["id"])
            summary = status.get("summary") or {}
            log = state / "discoveries" / ticket["id"].split(":", 1)[1] / "run.log"
            checks["search"] = {
                "ticket_status": status["status"], "exit_code": code,
                "transport": summary.get("transport"),
                "summary_status": summary.get("status"),
                "failure_reason": summary.get("failure_reason"),
                "discovery_ref": summary.get("discovery_ref"),
                "document_count": summary.get("document_count"),
                "new_document_count": summary.get("new_document_count"),
                "discovered_urls": summary.get("discovered_urls"),
                "receipt": summary.get("search"),
                "provider_calls": summary.get("provider_calls"),
                "formal_authority_writes": summary.get("formal_authority_writes"),
                "run_log_tail": log.read_text(encoding="utf-8")[-2500:] if log.exists() else None,
            }

            records = missions.source_discoveries(mission["id"], company_ref=args.company_ref)
            if records:
                record = records[0]
                checks["discovery_record"] = {
                    "id": record["id"], "source_ref": record["source_ref"],
                    "document_refs": record["document_refs"],
                    "new_document_refs": record["new_document_refs"],
                    "requested_by": record["requested_by"],
                }
                envelope = store.connection.execute(
                    "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                    (record["source_envelope_ref"],),
                ).fetchone()
                source = json.loads(envelope["record_json"])
                spool = RawSpool(str(spool_dir), max_total_bytes=1_000_000_000)
                raw = spool.read_object(source["raw_response_hash"])
                authorities = build_public_web_url_authorities(raw, source)
                checks["url_authorities"] = [
                    {"host": a["host"], "canonical_url": a["canonical_url"]} for a in authorities
                ]
                checks["raw_artifact_bytes"] = len(raw)
                # Prove the synthesized answer stayed inside the raw artifact.
                payload = json.loads(raw.decode("utf-8"))
                inner = json.loads(payload["result"]["content"][0]["text"])
                checks["payload_keys"] = sorted(inner)
                checks["payload_provider"] = inner.get("provider")
                checks["answer_kept_in_raw_only"] = "EXTERNAL_UNTRUSTED_CONTENT" in raw.decode("utf-8")
            checks["counts_after"] = _counts(store)
            checks["integrity_check"] = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            launcher.close()
            store.close()

    before, after = checks["counts_before"], checks["counts_after"]
    search = checks["search"]
    conditions = {
        "governance_approved": checks["governance"]["status"] == "approved",
        "real_transport": search["transport"] == "openclaw-search-broker",
        "search_succeeded": search["ticket_status"] == "succeeded" and search["summary_status"] == "succeeded",
        "one_provider_call": search["provider_calls"] == 1,
        "urls_discovered": bool(search.get("discovered_urls")),
        "no_formal_writes": search["formal_authority_writes"] == 0,
        "discovery_recorded": bool(checks.get("discovery_record")),
        "url_authorities_rebuilt": bool(checks.get("url_authorities")),
        "payload_is_gemini_answer": checks.get("payload_provider") == "gemini",
        "formal_counts_unchanged": all(after[t] == before[t] for t in COUNTED_TABLES),
        "one_recorded_invocation": after["web_search_calls"] == before["web_search_calls"] + 1,
        "integrity_ok": checks["integrity_check"] == "ok",
    }
    result["conditions"] = conditions
    result["ok"] = all(conditions.values())
    result["source_core_untouched"] = True  # opened read-only; only the copy was written
    result["paid_calls"] = 1
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--governance", type=Path, required=True)
    parser.add_argument("--broker-socket", required=True)
    parser.add_argument("--broker-auth-key", required=True)
    parser.add_argument("--broker-client-id", default="client:dalton-core")
    parser.add_argument("--broker-profile-id", default="profile:web-search")
    parser.add_argument("--company-ref", default="company:sec-cik:0001467373")
    parser.add_argument("--spec-ref")
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
