#!/usr/bin/env python3
"""Read-only replay: live Claims promoted from document research that are not self-contained.

    PYTHONPATH=src .venv/bin/python scripts/replay_hollow_claims_2026_09_30.py \\
        --state-dir <state-dir> \\
        [--state-dir .../ws-7d.../state/dalton-core] [--out /tmp/hollow.json] [--refs]

Opens every Core with ``mode=ro`` and writes nothing but ``--out``.  For every
``mission_document_research_promotions`` row whose Claim is live (not retired,
or reinstated), it asks the directed-document draft contract 0.2's own rule
(``document_research_qualitative.self_contained_rejection``) of the promoted
statement against the planner question it answered:

* the question names a figure (significant digits: "15.8%", "970" / "9.7 亿")
  and the statement never states it; or
* the statement points back at the question ("这个数字", "this figure", "the
  number", ...) without the value.

Those are the hollow Claims the contract now refuses before staging.  The
output per environment is the list, the reason for each, and
``selection_sha256`` -- the hash ``claim_admission_cli retire
--expect-selection`` checks (sorted claim-version refs, newline-joined).
``--refs`` prints only ``<state-dir>\\t<claim-version-ref>`` lines, for the
owner script.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from dalton_core.claim_admission_cli import selection_hash
from dalton_core.claim_retirement import retired_claim_version_refs
from dalton_core.document_research_qualitative import self_contained_rejection


def _connect(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{state / 'core.sqlite'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def replay(state: Path) -> dict[str, Any]:
    connection = _connect(state)
    try:
        retired = retired_claim_version_refs(connection)
        rows = connection.execute(
            "SELECT p.admission_ref AS admission_ref, p.record_json AS promotion, "
            "p.created_at AS promoted_at, a.record_json AS admission, a.company_ref AS company "
            "FROM mission_document_research_promotions p "
            "JOIN mission_document_research_admissions a ON a.admission_id=p.admission_ref "
            "ORDER BY p.created_at, p.admission_ref").fetchall()
        summary: dict[str, Any] = {"state_dir": str(state), "promotions": len(rows),
                                   "examined": 0, "already_retired": 0, "selected": []}
        for row in rows:
            promotion = json.loads(row["promotion"])
            ref = promotion["claim_version_ref"]
            if ref in retired:
                summary["already_retired"] += 1
                continue
            summary["examined"] += 1
            claim_row = connection.execute(
                "SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (ref,),
            ).fetchone()
            if claim_row is None:
                continue
            claim = json.loads(claim_row["claim_json"])
            question = str((json.loads(row["admission"]).get("planner_inquiry") or {})
                           .get("question") or "")
            statement = str(claim.get("normalized_statement") or "")
            reason = self_contained_rejection(question=question, statement=statement)
            if reason is None:
                continue
            summary["selected"].append({
                "claim_version_ref": ref, "admission_ref": row["admission_ref"],
                "subject_ref": claim.get("subject_ref") or row["company"],
                "promoted_at": row["promoted_at"], "question": question,
                "statement": statement, "reason": reason})
    finally:
        connection.close()
    summary["selected_count"] = len(summary["selected"])
    summary["selection_sha256"] = selection_hash(
        item["claim_version_ref"] for item in summary["selected"])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay_hollow_claims_2026_09_30")
    parser.add_argument("--state-dir", action="append", required=True)
    parser.add_argument("--out")
    parser.add_argument("--refs", action="store_true",
                        help="print only <state-dir>\\t<claim-version-ref> lines")
    args = parser.parse_args(argv)
    results = [replay(Path(item).expanduser()) for item in args.state_dir]
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
    if args.refs:
        for result in results:
            for item in result["selected"]:
                print(f"{result['state_dir']}\t{item['claim_version_ref']}")
        return 0
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
