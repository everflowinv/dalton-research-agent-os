"""Day-ledger settlements a locked budget file would otherwise have lost.

2026-09-25, legacy: four cockpit admissions -- two event judgements
($0.45 each), a localization verifier and a language revision -- finished
their calls, and the settlement after each one hit ``database is locked`` for
longer than the bounded retry.  ``_settle_without_losing_the_lease`` then did
the right thing for the Scheduler (it completed the attempt anyway) and the
wrong thing for the ledger: it printed one line and forgot the settlement.  An
open admission keeps charging its whole reservation against every day
(``ThesisImpactBudgetStore._day_committed`` sums open reservations without a
day filter), so the day ledger read about $1.8 high, $0.9 of it in the event
pool, and nothing in the running system would ever close them.

The same can happen one step earlier: an admission whose ``admit`` the caller
gave up on after the lock retry.  If that insert did land, the caller never
learned its id, never called the model under it, and never settles it.

This registry keeps those two facts beside the budget file, outside its write
lock, so recording them cannot fail for the reason they exist:

* ``record_settlement`` -- the exact settlement that could not be written
  (admission id and actual cost).  Replayed verbatim; ``settle`` is idempotent
  per admission, and one already settled at another amount stands.
* ``record_unconfirmed_admission`` -- the (WorkOrder, attempt, phase) of an
  admit the caller abandoned.  No model call was made under it, so if the
  admission exists and is still open it is settled at zero: void, not spend.

:func:`drain` replays them against a budget store the next time a cockpit
call opens one, and stops at the first lock -- a locked ledger is the next
call's business, and the records wait.  Planning and replaying the older
orphans, whose settlement was never recorded anywhere, stays with
:mod:`.cockpit_reservation_recovery`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "cockpit-pending-settlement:0.1"
KIND_SETTLE = "settle"
KIND_VOID_IF_ADMITTED = "void_if_admitted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _is_lock_error(exc: BaseException) -> bool:
    from .sqlite_contention import is_sqlite_lock_error

    return isinstance(exc, sqlite3.OperationalError) and is_sqlite_lock_error(exc)


class PendingSettlements:
    """Owner-only JSON records beside one budget database."""

    def __init__(self, budget_path: str | os.PathLike[str] | None) -> None:
        path = "" if budget_path is None else os.fspath(budget_path)
        self.directory: Path | None = (
            None if not path or path == ":memory:" or path.startswith("file:")
            else Path(path + ".pending-settlements"))

    @classmethod
    def for_budget(cls, budget: Any) -> "PendingSettlements":
        return cls(getattr(budget, "path", None))

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    def _path(self, key: str) -> Path:
        assert self.directory is not None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        return self.directory / f"{digest}.json"

    def _write(self, key: str, value: Mapping[str, Any]) -> bool:
        if self.directory is None:
            return False
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self._path(key)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(descriptor, (json.dumps(dict(value), sort_keys=True) + "\n")
                         .encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            return True
        except OSError:
            return False

    def record_settlement(self, *, admission_id: str, actual_micros: int,
                          work_order_ref: str | None = None,
                          attempt_number: int | None = None,
                          reason: str = "") -> bool:
        return self._write(admission_id, {
            "schema_version": SCHEMA_VERSION, "kind": KIND_SETTLE,
            "admission_id": admission_id, "actual_micros": int(actual_micros),
            "work_order_ref": work_order_ref, "attempt_number": attempt_number,
            "reason": reason[:300], "recorded_at": _now(), "pid": os.getpid(),
        })

    def record_unconfirmed_admission(self, *, work_order_ref: str, attempt_number: int,
                                     phase: str, reason: str = "") -> bool:
        return self._write(f"{work_order_ref}|{attempt_number}|{phase}", {
            "schema_version": SCHEMA_VERSION, "kind": KIND_VOID_IF_ADMITTED,
            "work_order_ref": work_order_ref, "attempt_number": int(attempt_number),
            "phase": phase, "reason": reason[:300], "recorded_at": _now(),
            "pid": os.getpid(),
        })

    def pending(self) -> list[tuple[Path, dict[str, Any]]]:
        if self.directory is None or not self.directory.is_dir():
            return []
        out = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("schema_version") == SCHEMA_VERSION:
                out.append((path, record))
        return out

    def drain(self, budget: Any) -> list[dict[str, Any]]:
        """Replay what is pending against ``budget``; stop at the first lock."""

        from .thesis_impact_budget import ThesisImpactBudgetError

        results: list[dict[str, Any]] = []
        for path, record in self.pending():
            try:
                outcome = self._replay(budget, record)
            except sqlite3.OperationalError as exc:
                if _is_lock_error(exc):
                    results.append({"record": path.name, "status": "locked"})
                    break
                results.append({"record": path.name, "status": "kept",
                                "error": f"{type(exc).__name__}: {exc}"})
                continue
            except ThesisImpactBudgetError as exc:
                # Settled since at another amount, or nothing to settle:
                # the ledger's own answer stands and the record is done.
                outcome = {"status": "superseded", "error": str(exc)}
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            results.append({"record": path.name, **outcome})
        return results

    @staticmethod
    def _replay(budget: Any, record: Mapping[str, Any]) -> dict[str, Any]:
        if record.get("kind") == KIND_SETTLE:
            settled = budget.settle(str(record["admission_id"]),
                                    actual_micros=int(record["actual_micros"]))
            return {"status": "settled" if settled.get("status") != "duplicate"
                    else "already_settled",
                    "admission_id": record["admission_id"],
                    "actual_micros": int(record["actual_micros"])}
        if record.get("kind") == KIND_VOID_IF_ADMITTED:
            found = budget.admission(
                work_order_ref=str(record["work_order_ref"]),
                attempt_number=int(record["attempt_number"]),
                phase=str(record["phase"]))
            if found is None:
                return {"status": "never_admitted"}
            admission = found["admission"]
            if found.get("settlement") is not None:
                return {"status": "already_settled",
                        "admission_id": admission["admission_id"]}
            budget.settle(admission["admission_id"], actual_micros=0)
            return {"status": "voided", "admission_id": admission["admission_id"],
                    "released_micros": int(admission.get("reserved_micros") or 0)}
        return {"status": "unknown_kind"}


__all__ = ["KIND_SETTLE", "KIND_VOID_IF_ADMITTED", "PendingSettlements"]
