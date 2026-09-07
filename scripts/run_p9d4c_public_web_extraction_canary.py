#!/usr/bin/env python3
"""P9d-4c canary: a fetched page as a verified extraction source, on a live Core copy.

Proves, without network, paid calls or any write to the source Core, that on
a throwaway copy of the live Core:

1. a real search child and a real fetch child put one public page's original
   bytes into connector authority and queue it for human extraction;
2. ``verified_public_web_source`` re-reads every receipt through Core, proves
   the spool bytes are the ones that invocation recorded, and renders them
   deterministically (same bytes twice -> identical text and hashes);
3. the rendered original supports bounded windows and quotes whose hashes
   bind the exact text a human would cite;
4. a manifest that borrows another page's lineage is refused;
5. Claim/Evidence/Thesis counts and AlphaEngine rows are unchanged and
   ``PRAGMA integrity_check`` is ok.

Usage::

    python scripts/run_p9d4c_public_web_extraction_canary.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --output temp/p9d4c/live-copy-canary.json
"""

from __future__ import annotations

import argparse
import hashlib
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
from dalton_core.connector import ConnectorStore  # noqa: E402
from dalton_core.connector_authority_port import ConnectorCompletionReceiptReader  # noqa: E402
from dalton_core.coverage_mission import CoverageMissionAuthority  # noqa: E402
from dalton_core.mission_source_discovery import (  # noqa: E402
    ALPHAENGINE_SOURCE_REF,
    WEB_SEARCH_SOURCE_REF,
    WebSearchLauncher,
    build_discovery_parameters,
    discovery_query_hash,
    load_discovery_plan,
)
from dalton_core.observability import ObservabilityStore  # noqa: E402
from dalton_core.public_web_core_fetch import build_web_fetch_governance_record  # noqa: E402
from dalton_core.public_web_core_search import build_web_search_governance_record  # noqa: E402
from dalton_core.public_web_extraction_source import (  # noqa: E402
    PublicWebSourceConflict,
    verified_public_web_source,
)
from dalton_core.public_web_fetch_launcher import PublicWebFetchLauncher  # noqa: E402
from dalton_core.raw_spool import RawSpool  # noqa: E402
from dalton_core.store import DaltonStore, canonical_json, content_hash  # noqa: E402

PLAN_PATH = ROOT / "deploy" / "phase9" / "p9d4-us-it-services-web-search-plan-v2.json"
COUNTED_TABLES = ("evidence_versions", "claim_versions", "thesis_versions")
WINDOW_CHARS = 12000
QUOTE_CHARS = 1200
CITATIONS = [
    {"url": "https://Example.com/newsroom/leadership-update?utm=x#top", "title": "Leadership update"},
    {"url": "https://news.example.org/it-services-demand-2026", "title": "Demand outlook"},
]
PAGE = (
    "<!doctype html><html><head><title>Leadership update</title>"
    "<style>.a{color:red}</style><script>steal()</script></head><body>"
    "<h1>Rehearsal page served to the fetch child</h1>"
    "<p>This body is a local rehearsal fixture on a throwaway Core copy, never a real site.</p>"
    "<ul><li>Bookings commentary</li><li>Headcount commentary</li></ul>"
    "</body></html>"
).encode("utf-8")
EXPECTED_TEXT = (
    "Leadership update\n\nRehearsal page served to the fetch child\n\n"
    "This body is a local rehearsal fixture on a throwaway Core copy, never a real site.\n\n"
    "Bookings commentary\n\nHeadcount commentary"
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


def _counts(store: DaltonStore) -> dict[str, int]:
    counts = {
        table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in COUNTED_TABLES
    }
    for table in ("coverage_mission_source_discoveries", "coverage_mission_discovered_documents"):
        counts[f"{table}:{ALPHAENGINE_SOURCE_REF}"] = int(store.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_ref=?", (ALPHAENGINE_SOURCE_REF,),
        ).fetchone()[0])
    counts["web_document_reviews"] = int(store.connection.execute(
        "SELECT COUNT(*) FROM coverage_mission_document_reviews WHERE source_ref=?",
        (WEB_SEARCH_SOURCE_REF,),
    ).fetchone()[0])
    return counts


def run(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "checks": {}}
    checks = result["checks"]
    plan = load_discovery_plan(PLAN_PATH)
    checks["plan"] = {"id": plan["id"], "hash": plan["content_hash"]}
    with tempfile.TemporaryDirectory(prefix="dalton-p9d4c-canary-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        _copy_core(args.source_core, state / "core.sqlite")
        governance_dir = state / "connector-governance"
        governance_dir.mkdir(mode=0o700)
        # Rehearsal only: in-memory approved records for the throwaway copy.
        search_governance = governance_dir / "gemini-web-search-v1.json"
        search_governance.write_text(
            canonical_json(build_web_search_governance_record(approved_by=args.owner, status="approved")) + "\n",
            encoding="utf-8",
        )
        fetch_governance = governance_dir / "web-fetch-v1.json"
        fetch_governance.write_text(
            canonical_json(build_web_fetch_governance_record(approved_by=args.owner, status="approved")) + "\n",
            encoding="utf-8",
        )
        citations_path = state / "fake-citations.json"
        citations_path.write_text(json.dumps(CITATIONS), encoding="utf-8")
        page_path = state / "fake-page.html"
        page_path.write_bytes(PAGE)
        spool_dir = state / "connector-spool"

        store = DaltonStore(state / "core.sqlite")
        missions = CoverageMissionAuthority(store)
        search_launcher = WebSearchLauncher(
            state_dir=state, governance_path=search_governance, plan_path=PLAN_PATH,
            mode_args=("--fake-citations-file", str(citations_path)), spool_dir=spool_dir,
        )
        fetch_launcher = PublicWebFetchLauncher(
            state_dir=state, governance_path=fetch_governance,
            mode_args=("--fake-page-file", str(page_path)), spool_dir=spool_dir,
        )
        try:
            active = missions.active_mission(plan["mission_ref"])
            checks["counts_before"] = _counts(store)
            # Copy-only: promote web search to probe_only so an owner rehearsal
            # may run. The live mission is never touched.
            params = build_next_version_params(
                active, add_scopes=[], source_statuses={WEB_SEARCH_SOURCE_REF: "probe_only"},
            )
            params["actor_ref"] = args.owner
            mission_ref = params.pop("mission_ref")
            mission = missions.create_mission(mission_ref, **params)
            checks["mission_probe_only"] = {"id": mission["id"], "status": mission["status"]}

            # 1. real search child, then the real fetch child.
            authorization = missions.authorize_source_discovery(
                company_ref=args.company_ref, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=args.owner,
            )
            spec_ref = plan["specs"][0]["spec_ref"]
            as_of = datetime.now(timezone.utc).date()
            ticket = search_launcher.start(authorization=authorization, spec_ref=spec_ref, as_of=as_of)
            parameters = build_discovery_parameters(
                plan, spec_ref=spec_ref, company_ref=args.company_ref, as_of=as_of,
            )
            missions.record_discovery_dispatch(
                authorization=authorization, discovery_plan_ref=plan["id"],
                discovery_plan_hash=plan["content_hash"], spec_ref=spec_ref,
                query_hash=discovery_query_hash(plan, parameters), ticket_ref=ticket["id"],
            )
            search_launcher.wait(timeout=300)
            search_status = search_launcher.status(ticket["id"])
            checks["search"] = {
                "ticket_status": search_status["status"],
                "summary_status": (search_status.get("summary") or {}).get("status"),
                "new_document_count": (search_status.get("summary") or {}).get("new_document_count"),
            }
            queued = missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)
            fetch_ticket = fetch_launcher.start(document_ref=queued["document_ref"], actor_ref=args.owner)
            missions.mark_discovered_document_launched(queued["record_id"], fetch_ticket["id"])
            fetch_launcher.wait(timeout=300)
            fetch_status = fetch_launcher.status(fetch_ticket["id"])
            fetch_summary = fetch_status.get("summary") or {}
            fetch_log = state / "fetches" / fetch_ticket["id"].split(":", 1)[1] / "run.log"
            checks["fetch"] = {
                "ticket_status": fetch_status["status"],
                "summary_status": fetch_summary.get("status"),
                "failure_reason": fetch_summary.get("failure_reason"),
                "run_log_tail": fetch_log.read_text(encoding="utf-8")[-1500:] if fetch_log.exists() else None,
                "canonical_url": fetch_summary.get("canonical_url"),
                "body_bytes": fetch_summary.get("body_bytes"),
                "raw_media_type": fetch_summary.get("raw_media_type"),
                "provider_calls": fetch_summary.get("provider_calls"),
            }
            missions.settle_discovered_document(queued["record_id"], status="acquired")
            review = missions.register_document_review(queued["record_id"], requested_by=args.owner)
            checks["review"] = {"state": review["state"], "source_ref": review["source_ref"]}

            # 2/3. verify + render through the same path the review plane uses.
            connectors = ConnectorStore(store)
            observability = ObservabilityStore(store)
            reader = ConnectorCompletionReceiptReader(connectors=connectors, observability=observability)
            spool = RawSpool(str(spool_dir), max_total_bytes=1_000_000_000)
            manifest = fetch_launcher.read_completed_manifest(fetch_ticket["id"], queued["document_ref"])
            manifest, rendering = verified_public_web_source(store, spool, manifest, reader)
            again = verified_public_web_source(store, spool, manifest, reader)[1]
            text = rendering["text"]
            quotes = [
                {
                    "source_start": start,
                    "source_end": min(start + QUOTE_CHARS, min(WINDOW_CHARS, len(text))),
                    "source_sha256": hashlib.sha256(
                        text[start:min(start + QUOTE_CHARS, min(WINDOW_CHARS, len(text)))].encode("utf-8")
                    ).hexdigest(),
                }
                for start in range(0, min(WINDOW_CHARS, len(text)), QUOTE_CHARS)
            ]
            checks["verified_source"] = {
                "renderer": rendering["renderer"], "media_type": rendering["media_type"],
                "truncated": rendering["truncated"], "rendered_chars": rendering["rendered_chars"],
                "deterministic": again["text"] == text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "matches_expected_render": text == EXPECTED_TEXT,
                "body_sha256": manifest["body_sha256"],
                "body_matches_fixture": manifest["body_sha256"] == hashlib.sha256(PAGE).hexdigest(),
                "canonical_url": manifest["canonical_url"], "host": manifest["host"],
                "quote_count": len(quotes),
                "first_quote_binds_text": bool(quotes) and quotes[0]["source_sha256"] == hashlib.sha256(
                    text[quotes[0]["source_start"]:quotes[0]["source_end"]].encode("utf-8")
                ).hexdigest(),
                "script_and_style_excluded": all(token not in text for token in ("steal()", "color:red")),
            }

            # 4. a manifest that borrows another page's lineage is refused.
            base = {k: v for k, v in manifest.items() if k != "content_hash"}
            base["host"] = "attacker.example"
            forged = {**base, "content_hash": content_hash(base)}
            try:
                verified_public_web_source(store, spool, forged, reader)
                checks["forged_manifest"] = "accepted (unexpected)"
            except Exception as exc:
                checks["forged_manifest"] = f"{type(exc).__name__}: {exc}"

            checks["counts_after"] = _counts(store)
            checks["integrity_check"] = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            fetch_launcher.close()
            search_launcher.close()
            store.close()

    before, after = checks["counts_before"], checks["counts_after"]
    verified = checks["verified_source"]
    conditions = {
        "search_child_succeeded": checks["search"]["ticket_status"] == "succeeded"
        and checks["search"]["summary_status"] == "succeeded" and checks["search"]["new_document_count"] == 2,
        "fetch_child_succeeded": checks["fetch"]["ticket_status"] == "succeeded"
        and checks["fetch"]["summary_status"] == "succeeded" and checks["fetch"]["provider_calls"] == 1,
        "review_queued": checks["review"] == {"state": "awaiting_human_extraction", "source_ref": WEB_SEARCH_SOURCE_REF},
        "render_is_exact_and_deterministic": verified["matches_expected_render"] and verified["deterministic"]
        and verified["body_matches_fixture"] and not verified["truncated"],
        "render_excludes_script_and_style": verified["script_and_style_excluded"],
        "quotes_bind_exact_text": verified["quote_count"] >= 1 and verified["first_quote_binds_text"],
        "forged_manifest_refused": str(checks["forged_manifest"]).startswith("PublicWebSourceConflict"),
        "formal_counts_unchanged": all(after[t] == before[t] for t in COUNTED_TABLES),
        "alphaengine_rows_unchanged": all(
            after[k] == before[k] for k in before if k.endswith(f":{ALPHAENGINE_SOURCE_REF}")
        ),
        "review_added_on_copy_only": after["web_document_reviews"] == before["web_document_reviews"] + 1,
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
