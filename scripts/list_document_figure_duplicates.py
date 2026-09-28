"""List the document-figure Claims that repeat another of the same document -- read-only.

    PYTHONPATH=src .venv/bin/python scripts/list_document_figure_duplicates.py \\
        --state-dir /Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core \\
        [--out /tmp/figure-duplicates.json] [--refs-only]

2026-09-28: the promoter keyed a figure on its free-text period and its own row,
so one 10-K's revenue entered the Ledger once per spelling of its year (META:
"2025", "full year 2025", "Year Ended December 31, 2025").  This replays the
rule the promoter now applies (``document_figure_identity.duplicate_groups``:
one document, company, metric, period as dates and amount) over every live
company-filed figure, and lists the live Claims of the figures it would not
have promoted, each beside the Claim of the figure it repeats, which stays.

A Claim is found from its figure two ways: the promotion ledger's
``claim_version_ref``, and the ``claim:mission-figure:`` ref the figure's
candidate identity names.  Retired Claims are not listed.  The Core is opened
``mode=ro``; nothing is written but ``--out``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from dalton_core.claim_index_figures import MissionFigureAuthorityResolver
from dalton_core.claim_retirement import retired_claim_version_refs
from dalton_core.document_figure_identity import duplicate_groups
from dalton_core.store import content_hash


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def live_claims(connection: sqlite3.Connection, figure: dict[str, Any],
                retired: set[str]) -> list[dict[str, Any]]:
    refs: set[str] = set()
    try:
        refs.update(str(row[0]) for row in connection.execute(
            "SELECT claim_version_ref FROM quantitative_claim_promotions "
            "WHERE origin_kind='document_figure' AND origin_ref=? "
            "AND claim_version_ref IS NOT NULL", (figure["figure_id"],)))
    except sqlite3.Error:
        pass
    suffix = content_hash({"figure_id": figure["figure_id"],
                           "figure_hash": figure["content_hash"]})[:32]
    refs.update(str(row[0]) for row in connection.execute(
        "SELECT claim_version_id FROM claim_versions WHERE claim_ref=?",
        (f"claim:mission-figure:{suffix}",)))
    claims = []
    for ref in sorted(refs - retired):
        row = connection.execute(
            "SELECT claim_json, created_at FROM claim_versions WHERE claim_version_id=?",
            (ref,)).fetchone()
        if row is None:
            continue
        claim = json.loads(row["claim_json"])
        claims.append({"claim_version_ref": ref, "created_at": row["created_at"],
                       "period": claim.get("period"),
                       "statement": str(claim.get("normalized_statement") or "")[:200]})
    return claims


def duplicates(state: Path) -> dict[str, Any]:
    connection = _ro(state / "core.sqlite")
    try:
        figures = [item for item in MissionFigureAuthorityResolver(connection).figures()
                   if item["source_grade"] == "company-filed-document"]
        folded, identities = duplicate_groups(connection, figures)
        retired = retired_claim_version_refs(connection)
        by_id = {item["figure_id"]: item for item in figures}
        rows = []
        for figure_id, keeper_id in sorted(folded.items(), key=lambda kv: (
                by_id[kv[1]]["company_ref"], by_id[kv[1]]["metric_ref"], kv[1], kv[0])):
            figure, keeper = by_id[figure_id], by_id[keeper_id]
            retire = live_claims(connection, figure, retired)
            if not retire:
                continue
            identity = identities[figure_id]
            rows.append({
                "company_ref": figure["company_ref"],
                "document_ref": figure["document_ref"],
                "metric_ref": figure["metric_ref"],
                "period": identity["period"],
                "amount": identity["amount"],
                "figure_id": figure_id,
                "as_reported_label": figure["as_reported_label"],
                "period_as_written": figure["period"],
                "value": figure["value"], "scale": figure["scale"],
                "retire": retire,
                "keeps": {
                    "figure_id": keeper_id,
                    "as_reported_label": keeper["as_reported_label"],
                    "period_as_written": keeper["period"],
                    "value": keeper["value"], "scale": keeper["scale"],
                    "label_now": identities[keeper_id]["label"],
                    "claims": live_claims(connection, keeper, retired),
                },
            })
    finally:
        connection.close()
    return {"state_dir": str(state), "duplicates": rows,
            "claim_version_refs": [claim["claim_version_ref"]
                                   for row in rows for claim in row["retire"]]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--refs-only", action="store_true",
                        help="print only the claim version refs to retire, one per line")
    args = parser.parse_args(argv)
    result = duplicates(args.state_dir.expanduser())
    if args.out is not None:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.refs_only:
        for ref in result["claim_version_refs"]:
            print(ref)
    else:
        for row in result["duplicates"]:
            keeps = row["keeps"]
            print(f"{row['company_ref']}  {row['metric_ref']}  {row['period']}  "
                  f"{row['value']} {row['scale'] or ''}  «{row['as_reported_label']}» "
                  f"({row['period_as_written']})")
            for claim in row["retire"]:
                print(f"    retire {claim['claim_version_ref']}")
            print(f"    keeps  {keeps['value']} {keeps['scale'] or ''} «{keeps['as_reported_label']}» "
                  f"({keeps['period_as_written']}) "
                  + ", ".join(claim["claim_version_ref"] for claim in keeps["claims"]))
        print(f"{len(result['claim_version_refs'])} claim(s) to retire")
    return 0


if __name__ == "__main__":
    sys.exit(main())
