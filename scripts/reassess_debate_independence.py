#!/usr/bin/env python3
"""Read-only: what each current debate map would count as independent sources today.

A published debate map version is immutable and keeps the
``source_independence`` it was screened with.  After the provenance-side broker
attribution lands (AlphaEngine's recorded publisher, and the sending house of
every sales note), the existing maps still read 0/0 until the lane publishes
their next version -- which it does when the subject's evidence next moves.
This prints, per subject, the stored counts next to the counts the same ladder
gives now, so the owner can see which ``candidate`` debates have become
groundable before the redraw.

Opens ``core.sqlite`` with ``mode=ro`` and ``query_only``; writes nothing.

    PYTHONPATH=src python scripts/reassess_debate_independence.py \\
        --state-dir "<state>/dalton-core" [--subject-ref company:ticker:amzn]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--subject-ref")
    args = parser.parse_args(argv)

    from dalton_core.debate_map_draft import reassess_source_independence

    path = args.state_dir.expanduser() / "core.sqlite"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        rows = connection.execute(
            "SELECT v.subject_ref, v.version_number, v.record_json FROM debate_map_versions v "
            "WHERE v.version_number=(SELECT MAX(w.version_number) FROM debate_map_versions w "
            "WHERE w.map_ref=v.map_ref) ORDER BY v.subject_ref"
        ).fetchall()
        report = []
        for row in rows:
            if args.subject_ref and row["subject_ref"] != args.subject_ref:
                continue
            debates = reassess_source_independence(connection, json.loads(row["record_json"]))
            report.append({
                "subject_ref": row["subject_ref"],
                "version_number": row["version_number"],
                "debates": len(debates),
                "groundable_now": sum(1 for item in debates if item["groundable_now"]),
                "detail": debates,
            })
    finally:
        connection.close()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
