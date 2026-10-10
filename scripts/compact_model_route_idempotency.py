#!/usr/bin/env python3
"""Owner script: stop ``model_route_idempotency`` duplicating every route decision.

Found 2026-09-30 (cockpit review P1-1): on legacy, ``model-router.sqlite`` is
2.1 GB and 921 MB of it is ``model_route_idempotency`` -- 67,097 rows of ~15 KB.
Each row's ``result_json`` was ``{"status": "fresh", "decision": <decision>}``
where ``<decision>`` is byte-for-byte the ``decision_json`` already stored,
immutably, in ``model_route_decisions`` under the same ``decision_id`` (checked
on the newest 3,000 rows: 3,000 of 3,000 identical).  The bulk is the decision's
candidate snapshot.  An idempotency row only needs the key, the request hash
and which decision it produced.

Since the same change, ``ModelRouter.route`` writes new rows as a reference
(``idempotency_result_reference``) and replays both shapes
(``idempotent_route_replay``), so nothing here is needed for correctness.  This
script rewrites the *old* rows into the reference shape, and optionally
reclaims the space.

What it does, and does not do:

* Default is a dry run: it opens the database read-only (``mode=ro``) and
  reports how many rows are inline copies and how many bytes they hold.  It
  changes nothing.  It reads the whole table, so expect it to take a while on
  a 2 GB file.
* ``--apply`` rewrites, in short batches, only rows whose inline decision is
  JSON-identical to the stored decision (checked in SQL, per row, inside the
  batch's transaction).  Anything else is left as it is and counted.  The
  table is append-only by trigger; each batch drops ``model_idempotency_no_update``,
  updates, and recreates it from its own stored SQL *in the same transaction*,
  so no other connection ever sees the table without it.  Each batch holds the
  router's write lock for well under a second, so this is safe with the
  services running -- a router writer waits on its busy timeout -- but a quiet
  moment is kinder.
* Without VACUUM the file does not shrink (``auto_vacuum`` is off on legacy):
  the ~900 MB become free pages that SQLite reuses before growing the file
  again.  ``--vacuum`` rebuilds the file to give the space back, and refuses to
  run unless ``--services-stopped`` is given *and* ``lsof`` shows no other
  process holding the database or its WAL.  VACUUM needs free space about the
  size of the result on the same volume, and must never run under a live
  service.  It is never run automatically.

    # look first -- read-only
    scripts/compact_model_route_idempotency.py \\
        --db ~/Library/Application\\ Support/Dalton/state/dalton-core/model-router.sqlite

    # rewrite the old rows (services may run)
    scripts/compact_model_route_idempotency.py --db ... --apply

    # optional, in a stopped window only: give the space back to the disk
    scripts/compact_model_route_idempotency.py --db ... --vacuum --services-stopped

Take a backup of the database before ``--apply`` and ``--vacuum``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.model_router import (  # noqa: E402
    IDEMPOTENCY_RESULT_REFERENCE, idempotency_result_reference,
)
from dalton_core.store import canonical_json  # noqa: E402

TRIGGER = "model_idempotency_no_update"
DEFAULT_BATCH = 500

#: One row of the old shape whose decision matches the stored one, exactly.
_INLINE_COPY = (
    "json_type(i.result_json, '$.decision') = 'object' "
    "AND json_extract(i.result_json, '$.status') = 'fresh' "
    "AND (SELECT COUNT(*) FROM json_each(i.result_json)) = 2 "
    "AND json(json_extract(i.result_json, '$.decision')) = json(d.decision_json)"
)


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def survey(path: Path) -> dict[str, Any]:
    """Read-only: how much of the table is inline copies of decisions."""

    with _read_only(path) as conn:
        total, total_bytes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(result_json)), 0) "
            "FROM model_route_idempotency").fetchone()
        referenced = conn.execute(
            "SELECT COUNT(*) FROM model_route_idempotency "
            "WHERE json_extract(result_json, '$.result') = ?",
            (IDEMPOTENCY_RESULT_REFERENCE,)).fetchone()[0]
        copies, copy_bytes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(i.result_json)), 0) "
            "FROM model_route_idempotency i JOIN model_route_decisions d "
            f"ON d.decision_id = i.decision_id WHERE {_INLINE_COPY}").fetchone()
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return {
        "rows": total,
        "result_json_bytes": total_bytes,
        "already_references": referenced,
        "inline_copies": copies,
        "inline_copy_bytes": copy_bytes,
        "left_untouched": total - referenced - copies,
        "file_bytes": page_size * page_count,
        "free_page_bytes": page_size * freelist,
    }


def apply(path: Path, *, batch: int = DEFAULT_BATCH) -> dict[str, int]:
    """Rewrite inline copies to references, batch by batch, trigger kept."""

    conn = sqlite3.connect(str(path), isolation_level=None, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (TRIGGER,)).fetchone()
        if trigger_sql is None:
            raise SystemExit(f"{TRIGGER} is missing; refusing to touch an unguarded table")
        high = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM model_route_idempotency").fetchone()[0]
        rewritten = 0
        low = 0
        while low < high:
            upper = low + batch
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT i.idempotency_key, i.decision_id FROM model_route_idempotency i "
                    "JOIN model_route_decisions d ON d.decision_id = i.decision_id "
                    f"WHERE i.rowid > ? AND i.rowid <= ? AND {_INLINE_COPY}",
                    (low, upper)).fetchall()
                if rows:
                    conn.execute(f"DROP TRIGGER {TRIGGER}")
                    conn.executemany(
                        "UPDATE model_route_idempotency SET result_json=? "
                        "WHERE idempotency_key=?",
                        [(canonical_json(idempotency_result_reference(decision_id)), key)
                         for key, decision_id in rows])
                    conn.execute(trigger_sql[0])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            rewritten += len(rows)
            low = upper
        return {"rewritten": rewritten}
    finally:
        conn.close()


def _other_holders(path: Path) -> list[str]:
    """PIDs other than ours holding the database or its WAL/SHM open."""

    lsof = shutil.which("lsof")
    if lsof is None:
        raise SystemExit("lsof is not available; cannot prove the services are stopped")
    files = [str(path)] + [f"{path}{suffix}" for suffix in ("-wal", "-shm")
                           if Path(f"{path}{suffix}").exists()]
    found = subprocess.run([lsof, "-t", *files], capture_output=True, text=True)
    return sorted({pid for pid in found.stdout.split()
                   if pid.strip() and pid.strip() != str(os.getpid())})


def vacuum(path: Path) -> dict[str, int]:
    holders = _other_holders(path)
    if holders:
        raise SystemExit(
            "refusing to VACUUM: the database is open in process(es) "
            + ", ".join(holders) + " -- stop the Dalton services first")
    before = path.stat().st_size
    free = shutil.disk_usage(path.parent).free
    if free < before:
        raise SystemExit(f"refusing to VACUUM: {free} bytes free, need about {before}")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    return {"bytes_before": before, "bytes_after": path.stat().st_size}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path,
                        help="the model-router.sqlite to compact")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite inline copies to references (default: dry run)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--vacuum", action="store_true",
                        help="rebuild the file afterwards; stopped services only")
    parser.add_argument("--services-stopped", action="store_true",
                        help="the owner confirms every Dalton service is stopped")
    args = parser.parse_args(argv)
    path = args.db.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"no such database: {path}")
    if args.vacuum and not args.services_stopped:
        raise SystemExit("--vacuum needs --services-stopped: never VACUUM a live database")
    report: dict[str, Any] = {"db": str(path), "mode": "dry-run"}
    if args.apply:
        report["mode"] = "apply"
        report.update(apply(path, batch=max(1, args.batch)))
    if args.vacuum:
        report["mode"] += "+vacuum"
        report["vacuum"] = vacuum(path)
    report["survey"] = survey(path)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
