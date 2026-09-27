"""Replay the recheck's near-duplicate rule over what it committed -- read-only, no model.

    PYTHONPATH=src .venv/bin/python scripts/replay_recheck_near_duplicates.py \\
        --state-dir /Volumes/EveSSD/Dalton/legacy-state/dalton-core \\
        --since 2026-09-27T09:00 --out /tmp/recheck-near-duplicates.json

For every Claim the outage recheck committed since ``--since`` (read from the
extraction runs' own summaries, ``support_recheck.admitted``), in the order they
were committed, it asks the question ``ClaimSupportRecheck`` now asks before
committing: is there a live Claim -- one committed before it, not retired --
about the same subject, citing the same document with an overlapping span,
that says the same thing (``claim_support_recheck.near_duplicate``)?  Those are
the Claims the rule would have held back, each with the Claim it restates and
the similarity.

As a check on the other side it also lists, for the same committed Claims, the
closest same-span pair the rule let through, so the margin between "restates"
and "says something else" is on the page.

The Core is opened ``mode=ro``; nothing is written but ``--out``.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def committed_by_recheck(state: Path, since: str) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in glob.glob(str(state / "extractions" / "*" / "summary.json")):
        try:
            summary = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(summary.get("created_at") or "") < since:
            continue
        for item in (summary.get("support_recheck") or {}).get("admitted") or ():
            ref = item.get("claim_version_ref")
            if isinstance(ref, str) and ref not in found:
                found[ref] = {"claim_version_ref": ref,
                              "candidate_claim_ref": item.get("candidate_claim_ref"),
                              "run_at": summary["created_at"]}
    return list(found.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    from dalton_core.claim_retirement import retired_claim_version_refs
    from dalton_core.claim_support_recheck import (
        NEAR_DUPLICATE_RATIO, BINDING_PREFIX, _normalized, live_claims_citing, near_duplicate,
        spans_overlap)

    state = args.state_dir.expanduser()
    core = _ro(state / "core.sqlite")
    committed = committed_by_recheck(state, args.since)
    rows = {}
    for entry in committed:
        row = core.execute(
            "SELECT c.claim_json, c.created_at, e.evidence_json FROM claim_versions c "
            "JOIN evidence_relations r ON r.claim_version_id=c.claim_version_id AND r.relation='supports' "
            "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
            "WHERE c.claim_version_id=?", (entry["claim_version_ref"],)).fetchone()
        if row is None:
            continue
        claim = json.loads(row["claim_json"])
        evidence = json.loads(row["evidence_json"])
        binding_ref = next((item.get("ref") for item in evidence.get("artifact_refs") or ()
                            if str(item.get("ref", "")).startswith(BINDING_PREFIX)), None)
        binding = core.execute(
            "SELECT source_content_hash, source_start, source_end FROM transcript_claim_citation_bindings "
            "WHERE binding_id=?", (binding_ref,)).fetchone() if binding_ref else None
        if binding is None:
            continue
        rows[entry["claim_version_ref"]] = {
            **entry, "created_at": row["created_at"], "subject_ref": claim.get("subject_ref"),
            "statement": claim.get("normalized_statement"),
            "binding": {"document_sha256": binding[0], "start": binding[1], "end": binding[2]}}
    ordered = sorted(rows.values(), key=lambda item: (item["created_at"], item["claim_version_ref"]))
    ledger = live_claims_citing(core, {item["binding"]["document_sha256"] for item in ordered})
    created = {ref: at for ref, at in core.execute("SELECT claim_version_id, created_at FROM claim_versions")}
    retired_now = retired_claim_version_refs(core)
    blocked, passed = [], []
    for item in ordered:
        best, closest = None, None
        for other in ledger.get(item["binding"]["document_sha256"], ()):
            if other["ref"] == item["claim_version_ref"] or other["subject_ref"] != item["subject_ref"]:
                continue
            if created.get(other["ref"], "") >= item["created_at"]:
                continue  # not in the Ledger yet when this one was committed
            if not spans_overlap(item["binding"], other["binding"]):
                continue
            ratio = near_duplicate(item["statement"], other["statement"])
            raw = round(difflib.SequenceMatcher(None, _normalized(item["statement"]),
                                                _normalized(other["statement"]), autojunk=False).ratio(), 3)
            if ratio is not None and (best is None or ratio > best["similarity"]):
                best = {"similarity": ratio, "duplicate_of": other["ref"], "of_statement": other["statement"]}
            if ratio is None and (closest is None or raw > closest["similarity"]):
                closest = {"similarity": raw, "other": other["ref"], "other_statement": other["statement"]}
        base = {key: item[key] for key in ("claim_version_ref", "candidate_claim_ref", "subject_ref",
                                           "statement", "created_at")}
        base["retired_now"] = item["claim_version_ref"] in retired_now
        if best is not None:
            blocked.append({**base, **best})
        elif closest is not None:
            passed.append({**base, "closest_let_through": closest})
    passed.sort(key=lambda item: -item["closest_let_through"]["similarity"])
    summary = {
        "state_dir": str(state), "since": args.since, "threshold": NEAR_DUPLICATE_RATIO,
        "committed_by_recheck": len(committed), "readable": len(ordered),
        "would_block": len(blocked),
        "would_block_live_now": sum(1 for item in blocked if not item["retired_now"]),
        "closest_let_through": [item["closest_let_through"]["similarity"] for item in passed[:5]],
    }
    args.out.write_text(json.dumps({"summary": summary, "would_block": blocked,
                                    "closest_let_through": passed[:10]},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for item in blocked:
        print(f"- {item['claim_version_ref']}  sim={item['similarity']}  dup_of={item['duplicate_of']}")
        print(f"    new: {item['statement']}")
        print(f"    old: {item['of_statement']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
