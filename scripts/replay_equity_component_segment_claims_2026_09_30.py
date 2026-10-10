#!/usr/bin/env python3
"""Read-only replay of the 2026-09-30 segment-axis rule over admitted segment Claims.

    PYTHONPATH=src .venv/bin/python scripts/replay_equity_component_segment_claims_2026_09_30.py \\
        --state-dir <state-dir> \\
        --state-dir <state-dir> \\
        [--out /tmp/equity-segment-replay.json] [--refs-out refs.tsv] [--list]

Opens every Core with ``mode=ro`` and writes nothing but ``--out`` /
``--refs-out``.  Per environment, over the live (not retired) quantitative
Claims the promoter admitted as ``segment <metric>: <member>`` from a filed
statement line, it re-reads the stored line and asks
``quantitative_claim_promotion.is_segment_line`` -- the rule the promoter now
applies -- whether the line is a part of the business.  The ones it refuses are
selected, in three groups:

* ``equity``      -- a column of the statement of stockholders' equity
  (``StatementEquityComponentsAxis``, or an equity-component member such as
  ``RetainedEarningsMember`` / ``ParentMember`` on any axis);
* ``other``       -- other attributes of a number that are not segments: share
  class, AOCI reclassification, change in accounting estimate, derivative risk,
  equity-method investee, commitments, contingencies, the corporate /
  reconciling column of ``ConsolidationItemsAxis``;
* ``renamed``     -- real numbers the promoter now names honestly
  (``non_segment_metric``): revenue by industry / pricing basis / timing of
  transfer, and EPS by share class when the class is the only dimension.
  Retiring the old "segment ..." Claim is optional; once it is retired, the
  next quantitative-claim-promotion lane pass promotes the row under its new
  name (listed per claim as ``new_metric``).  No governed relabel exists: the
  writer has no operation that supersedes an admitted Claim's name.

``selection_sha256`` per group and environment is the hash
``claim_admission_cli retire --claim-version-ref ...`` prints for the same list
(sorted, newline-joined claim-version refs).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from dalton_core.claim_retirement import retired_claim_version_refs
from dalton_core.quantitative_claim_promotion import (
    BREAKDOWN_SUFFIX,
    SEGMENT_CLASSIFICATION_RULE,
    is_segment_line,
    non_segment_metric,
)

EQUITY_AXES = frozenset({"us-gaap:StatementEquityComponentsAxis"})
EQUITY_MEMBERS = frozenset({
    "us-gaap:RetainedEarningsMember",
    "us-gaap:RetainedEarningsUnappropriatedMember",
    "us-gaap:RetainedEarningsAppropriatedMember",
    "us-gaap:AdditionalPaidInCapitalMember",
    "us-gaap:AdditionalPaidInCapitalCommonStockMember",
    "us-gaap:AccumulatedOtherComprehensiveIncomeMember",
    "us-gaap:AccumulatedTranslationAdjustmentMember",
    "us-gaap:TreasuryStockMember",
    "us-gaap:TreasuryStockCommonMember",
    "us-gaap:TreasuryStockPreferredMember",
    "us-gaap:CommonStockMember",
    "us-gaap:CommonStockIncludingAdditionalPaidInCapitalMember",
    "us-gaap:PreferredStockMember",
    "us-gaap:ParentMember",
    "us-gaap:NoncontrollingInterestMember",
})
GROUPS = ("equity", "other", "renamed")


def selection_hash(refs) -> str:
    return hashlib.sha256("\n".join(sorted(set(refs))).encode("utf-8")).hexdigest()


def group_of(line: dict) -> str:
    axis = str(line.get("dimension_axis") or "")
    member = str(line.get("dimension_member") or "")
    if axis in EQUITY_AXES or member in EQUITY_MEMBERS:
        return "equity"
    if non_segment_metric(line) is not None:
        return "renamed"
    return "other"


def _connect(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{state / 'core.sqlite'}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def replay(state: Path) -> dict:
    connection = _connect(state)
    try:
        retired = retired_claim_version_refs(connection)
        rows = connection.execute(
            "SELECT p.promotion_id, p.company_ref, p.metric_or_aspect, p.period, p.value, "
            "p.unit, p.claim_version_ref, l.line_id, l.concept, l.label, l.dimension_axis, "
            "l.dimension_member, l.dimension_count, f.entity_name, f.accession, f.form "
            "FROM quantitative_claim_promotions p "
            "LEFT JOIN coverage_mission_statement_lines l ON l.line_id=p.origin_ref "
            "LEFT JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
            "WHERE p.origin_kind='statement_line' AND p.disposition='admitted' "
            "AND substr(p.metric_or_aspect, 1, ?)=? ORDER BY p.company_ref, p.promotion_id",
            (len(BREAKDOWN_SUFFIX), BREAKDOWN_SUFFIX),
        ).fetchall()
        # A later version of the same Claim would make the promoted version
        # stale; none is expected (the promoter writes version 1 only).
        superseded = {
            str(row[0]) for row in connection.execute(
                "SELECT prior_version_id FROM claim_versions WHERE prior_version_id IS NOT NULL")
        }
        missing_versions = 0
        selected: dict[str, list[dict]] = {name: [] for name in GROUPS}
        summary = {"state_dir": str(state), "rule": SEGMENT_CLASSIFICATION_RULE,
                   "admitted_segment_claims": len(rows), "already_retired": 0,
                   "kept_as_segment": 0, "unreadable_line": 0}
        for row in rows:
            ref = row["claim_version_ref"]
            if not ref:
                continue
            if connection.execute("SELECT 1 FROM claim_versions WHERE claim_version_id=?",
                                  (ref,)).fetchone() is None:
                missing_versions += 1
                continue
            if ref in retired:
                summary["already_retired"] += 1
                continue
            if row["line_id"] is None:
                summary["unreadable_line"] += 1
                continue
            line = {key: row[key] for key in row.keys()}
            if is_segment_line(line):
                summary["kept_as_segment"] += 1
                continue
            selected[group_of(line)].append({
                "claim_version_ref": ref,
                "company": row["entity_name"] or row["company_ref"],
                "company_ref": row["company_ref"],
                "metric_or_aspect": row["metric_or_aspect"],
                "new_metric": (non_segment_metric(line) or (None,))[0],
                "period": row["period"],
                "value": row["value"], "unit": row["unit"],
                "axis": row["dimension_axis"], "member": row["dimension_member"],
                "dimension_count": row["dimension_count"],
                "concept": row["concept"], "accession": row["accession"],
                "superseded": ref in superseded,
            })
        summary["missing_claim_version"] = missing_versions
        summary["groups"] = {}
        for name in GROUPS:
            items = selected[name]
            by_company = collections.Counter(item["company"] for item in items)
            by_axis_member = collections.Counter(
                f"{item['axis']} / {item['member']}" for item in items)
            by_metric = collections.Counter(item["metric_or_aspect"] for item in items)
            summary["groups"][name] = {
                "count": len(items),
                "selection_sha256": selection_hash(item["claim_version_ref"] for item in items),
                "by_company": dict(by_company.most_common()),
                "by_axis_member": dict(by_axis_member.most_common()),
                "by_metric": dict(by_metric.most_common()),
                "superseded": sum(1 for item in items if item["superseded"]),
                "claims": items,
            }
        return summary
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", action="append", required=True)
    parser.add_argument("--out", help="write the full replay (every claim) as JSON")
    parser.add_argument("--refs-out", help="write 'group<TAB>state-dir<TAB>ref' lines")
    parser.add_argument("--list", action="store_true", help="print every selected Claim")
    args = parser.parse_args(argv)
    results = [replay(Path(item).expanduser()) for item in args.state_dir]
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
    if args.refs_out:
        with open(args.refs_out, "w", encoding="utf-8") as handle:
            for result in results:
                for name in GROUPS:
                    for item in sorted(result["groups"][name]["claims"],
                                       key=lambda entry: entry["claim_version_ref"]):
                        handle.write(f"{name}\t{result['state_dir']}\t"
                                     f"{item['claim_version_ref']}\n")
    for result in results:
        brief = {key: value for key, value in result.items() if key != "groups"}
        brief["groups"] = {}
        for name, group in result["groups"].items():
            shown = dict(group)
            if not args.list:
                shown.pop("claims")
            brief["groups"][name] = shown
        print(json.dumps(brief, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
