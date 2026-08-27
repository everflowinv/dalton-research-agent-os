#!/usr/bin/env python3
"""Build CompanyResearchViews over an isolated copy of the live Core.

Copies the live Core read-only via SQLite backup, rebuilds the research view
for every lane issuer, exercises the structured query filters, and hands the
ACN view's claim refs to the ContextMaterializer to produce one token-bounded
ContextPack.  No writes, no network, no paid model calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from dalton_core.company_research_view import (
    build_company_research_view,
    query_company_research,
)
from dalton_core.context_materializer import ContextMaterializer
from dalton_core.observability import ObservabilityStore
from dalton_core.research_context import build_claim_index, build_reference_fixture_plan
from dalton_core.store import DaltonStore, content_hash


COMPANIES = (
    "company:sec-cik:0001467373",
    "company:sec-cik:0001058290",
    "company:sec-cik:0001352010",
    "company:sec-cik:0000051143",
    "company:sec-cik:001688568",
)
ACN = COMPANIES[0]
NOW = "2026-08-27T00:00:00+00:00"


def run(source_core: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="p8b-view-canary-") as directory:
        copied = Path(directory) / "core.sqlite"
        read_only = sqlite3.connect(f"{source_core.as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(copied)
        read_only.backup(target)
        target.close()
        read_only.close()

        store = DaltonStore(str(copied))
        try:
            views = {}
            for company in COMPANIES:
                view = build_company_research_view(store, company)
                again = build_company_research_view(store, company)
                if json.dumps(again, sort_keys=True) != json.dumps(view, sort_keys=True):
                    raise RuntimeError(f"view for {company} is not deterministic")
                views[company] = view

            acn = views[ACN]
            if acn["thesis"]["status"] != "current":
                raise RuntimeError("ACN thesis was not projected as current")
            if acn["thesis"]["thesis_ref"] != "thesis:acn:ai-reinvention-growth":
                raise RuntimeError("ACN view bound the wrong thesis")
            acn_aspects = {
                row["metric_or_aspect"] for row in acn["claims"]
            }
            if acn_aspects != {
                "quarterly_revenue_yoy_growth",
                "aspect:new-bookings-direction-local-currency",
            }:
                raise RuntimeError(f"ACN view claims were wrong: {sorted(acn_aspects)}")
            if any(row["status"] != "proposed" for row in acn["claims"]):
                raise RuntimeError("ACN claims should all project as proposed")
            if len(acn["impact"]) != 1 or acn["impact"][0]["verdict"] != "pass":
                raise RuntimeError("ACN view did not project the verified assessment")
            if acn["last_weekly_issue"] is None:
                raise RuntimeError("ACN view missed the w35 brief issue")
            if views[COMPANIES[4]]["thesis"]["status"] != "insufficient":
                raise RuntimeError("DXC should project as insufficient")

            by_status = query_company_research(store, status="proposed")
            by_aspect = query_company_research(
                store, aspect="quarterly_revenue_yoy_growth"
            )
            by_company = query_company_research(store, company_ref=ACN)
            by_period = query_company_research(store, period="FY2026Q3")
            if len(by_aspect) != 5:
                raise RuntimeError("revenue-growth aspect query should hit 5 issuers")
            if len(by_company) != 2 or len(by_status) != 6:
                raise RuntimeError("company or status query counts were wrong")

            observability = ObservabilityStore(store)
            materializer = ContextMaterializer(store, observability, None)
            plan = build_reference_fixture_plan(
                task_ref="work-order:p8b-view-canary",
                task_hash=content_hash(
                    {"work_order_ref": "work-order:p8b-view-canary"}
                ),
                created_at=NOW,
            )
            index = build_claim_index(ledger=store, created_at=NOW)
            specs = [
                {
                    "kind": "claim",
                    "ref": row["claim_version_ref"],
                    "hash": row["claim_version_hash"],
                    "priority": 10,
                }
                for row in acn["claims"]
            ]
            pack = materializer.build_authority_context_pack(
                specs, task_ref=plan["task_ref"], task_hash=plan["task_hash"],
                compiled_plan_ref=plan["id"], compiled_plan_hash=plan["content_hash"],
                claim_index_ref=index["id"], claim_index_hash=index["content_hash"],
                claim_index=index, created_at=NOW,
                max_tokens=8_000, max_bytes=64_000,
            )
            materialization = materializer.materialize(
                pack, max_rendered_tokens=8_000, max_rendered_bytes=64_000,
                compiled_plan=plan, claim_index=index, created_at=NOW,
            )
            manifest = materialization.manifest
            if manifest["totals"]["selected_count"] != len(specs):
                raise RuntimeError("ContextPack did not select every view claim")
            if manifest["totals"]["rendered_tokens"] > pack["budget"]["max_tokens"]:
                raise RuntimeError("ContextPack exceeded its token budget")

            integrity = store.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            return {
                "status": "passed",
                "mode": "isolated-live-copy",
                "paid_model_calls": 0,
                "companies": [
                    {
                        "company_ref": company,
                        "view_id": views[company]["id"],
                        "thesis_status": views[company]["thesis"]["status"],
                        "thesis_ref": views[company]["thesis"]["thesis_ref"],
                        "claim_count": len(views[company]["claims"]),
                        "open_question_count": len(views[company]["open_questions"]),
                        "impact_count": len(views[company]["impact"]),
                        "last_issue": (
                            views[company]["last_weekly_issue"] or {}
                        ).get("issue_version_ref"),
                        "last_stop_kind": (
                            views[company]["last_research_stop"] or {}
                        ).get("kind"),
                        "built_as_of": views[company]["built_as_of"],
                    }
                    for company in COMPANIES
                ],
                "query_counts": {
                    "status_proposed": len(by_status),
                    "aspect_revenue_growth": len(by_aspect),
                    "company_acn": len(by_company),
                    "period_fy2026q3": len(by_period),
                },
                "acn_context_pack": {
                    "context_pack_ref": pack["id"],
                    "selected_count": manifest["totals"]["selected_count"],
                    "rendered_tokens": manifest["totals"]["rendered_tokens"],
                    "rendered_bytes": manifest["totals"]["rendered_bytes"],
                },
                "integrity_check": integrity,
            }
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-core", type=Path,
        default=Path(
            "/Users/everflow/Library/Application Support/Dalton/state/dalton-core/"
            "core.sqlite"
        ),
    )
    args = parser.parse_args()
    result = run(args.source_core.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
