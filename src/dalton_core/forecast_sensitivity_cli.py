"""P13-M3: the child that computes one company's sensitivity table.

Like the driver-model child and for the same reasons: **no model call**. Every
number here is arithmetic over a ForecastModelVersion that already exists and
the filings behind it, so the run costs nothing, cannot be refused by the
router, and produces the same bytes every time.

One run does one thing. It finds a company whose driver model or whose street
estimate has moved since the last projection, recomputes the table against the
current model, and appends it. A company whose model has not been revised and
whose street has not moved has nothing new to say, and recomputing it would
append a row identical to the last one -- which the authority refuses as a
duplicate anyway, so the lane does not ask.

The write scope is ``model_run``: this record is what the model produced when
it was run, several times, against assumptions nobody adopted. It is
deliberately *not* ``forecast_line``. A scenario is not a forecast, and the day
a what-if could be published as one is the day the version chain stops meaning
"what we thought" and starts meaning "what we tried".

Exit 0 when the run completed, including when it decided nothing needed doing.
``formal_authority_writes`` is 0: a sensitivity table is not a Claim about the
world, it is a claim about a model.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .consensus_bridge import build_bridge, consensus_fingerprint, read_consensus
from .coverage_mission import CoverageMissionAuthority
from .forecast_sensitivity import (
    WRITE_SCOPE,
    SensitivityError,
    SensitivityProjectionAuthority,
    SensitivityUnavailable,
    build_projection,
    fingerprint,
    projection_readiness,
)
from .model_forecast_driver import ForecastModelAuthority
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def missing_write_scope(mission: Any) -> str | None:
    """Why this mission may not record a model run, if it may not."""

    granted = set((mission.get("autonomy") or {}).get("may_write") or [])
    if WRITE_SCOPE in granted:
        return None
    return f"the mission does not grant the {WRITE_SCOPE} write scope"


def _table_exists(connection: Any, name: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def latest_valuation(store: Any, company_ref: str) -> dict[str, Any] | None:
    """P11a's snapshot for this company, when this Core has one.

    Read by table probe rather than by import, because constructing the
    authority is what installs its schema, and a lane computing a sensitivity
    table should not leave a valuation schema behind on a Core that has never
    valued anything.
    """

    if not _table_exists(store.connection, "valuation_snapshot_versions"):
        return None
    try:
        from .valuation_snapshot import ValuationSnapshotAuthority

        return ValuationSnapshotAuthority(store).latest_version(company_ref)
    except Exception:  # noqa: BLE001 - an unreadable snapshot is no snapshot
        return None


def projection_fingerprint(
    store: Any, models: ForecastModelAuthority, company_ref: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None, str | None]:
    """This company's model, its street, and what the pair currently hashes to."""

    record = models.latest(company_ref)
    if record is None:
        return None, {"status": "unavailable", "payload": None,
                      "reason": "this company has no driver model"}, None, None
    consensus = read_consensus(store, company_ref)
    street = consensus_fingerprint(consensus)
    return record, consensus, street, fingerprint(record, street)


def pending_companies(
    store: Any,
    missions: CoverageMissionAuthority,
    models: ForecastModelAuthority,
    projections: SensitivityProjectionAuthority,
    mission: dict[str, Any],
    *,
    company_ref: str | None = None,
) -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
    """Every company whose model or street has moved since its last projection.

    All of them rather than the first, so the coordinator can skip one that
    keeps failing. The lesson the specification lane learnt the hard way: one
    company that cannot be projected must not stand in front of the other four
    forever.
    """

    universe = {
        str(item.get("company_ref"))
        for item in (mission.get("universe") or []) if isinstance(item, dict)
    }
    if company_ref is not None:
        # Named companies go through the universe too. The mission is the only
        # statement of which companies this automation may work on, and a hand
        # run that could reach past it would write a record for a company
        # nobody admitted, under the mission's own principal.
        if company_ref not in universe:
            raise SensitivityUnavailable(
                f"{company_ref} is not in this mission's universe")
        refs = [company_ref]
    else:
        refs = sorted(ref for ref in models.companies() if ref in universe)
    out: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    for ref in refs:
        record, consensus, _, digest = projection_fingerprint(store, models, ref)
        if record is None or digest is None:
            if company_ref is not None:
                raise SensitivityUnavailable(str(consensus.get("reason")))
            continue
        held = projections.latest(ref)
        if held is not None and str(held.get("fingerprint")) == digest:
            continue
        out.append((ref, record, consensus, digest))
    return out


def choose_company(
    store: Any,
    missions: CoverageMissionAuthority,
    models: ForecastModelAuthority,
    projections: SensitivityProjectionAuthority,
    mission: dict[str, Any],
    *,
    company_ref: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], str] | None:
    pending = pending_companies(store, missions, models, projections, mission,
                                company_ref=company_ref)
    return pending[0] if pending else None


def run_sensitivity(
    *,
    state_dir: Path,
    summary_dir: Path,
    company_ref: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "publish",
        "company_ref": company_ref,
        "sensitivity_status": None,
        "projection_ref": None,
        "projection_version": None,
        "model_version_ref": None,
        "fingerprint": None,
        "drivers_selected": 0,
        "drivers_with_bands": 0,
        "selection_status": None,
        "impact_metric": None,
        "horizon_quarters": 0,
        "what_if_cells": 0,
        "what_if_cells_unavailable": 0,
        "bridge_status": None,
        "bridge_metrics": 0,
        "bridge_reason": None,
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        models = ForecastModelAuthority(store)
        projections = SensitivityProjectionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "sensitivity_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        try:
            chosen = choose_company(store, missions, models, projections, mission,
                                    company_ref=company_ref)
        except SensitivityUnavailable as exc:
            summary.update({"status": "succeeded",
                            "sensitivity_status": f"refused:{exc}"})
            return summary
        if chosen is None:
            summary.update({"status": "idle",
                            "sensitivity_status": "nothing_to_project"})
            return summary
        ref, record, consensus, digest = chosen
        summary.update({"company_ref": ref, "fingerprint": digest,
                        "model_version_ref": str(record.get("id"))})
        refusal = missing_write_scope(mission)
        if refusal is not None and not dry_run:
            summary.update({"status": "succeeded",
                            "sensitivity_status": f"refused:{refusal}"})
            return summary
        if dry_run:
            summary.update({"status": "succeeded", "sensitivity_status": "gated",
                            "failure_reason": refusal})
            return summary
        built = build_bridge(record, consensus, store=store,
                             valuation=latest_valuation(store, ref))
        try:
            body = build_projection(
                record,
                bridge=built["bridge"], bridge_detail=built["detail"],
                consensus_fingerprint=consensus_fingerprint(consensus),
                actor_ref=mission["autonomy"]["automation_principal"],
                mission_version_ref=mission["id"],
            )
        except SensitivityUnavailable as exc:
            # Refused whole. A projection with no drivers in it is not a
            # projection with a gap; it is a claim that nothing about this
            # company matters, which is never something we found out.
            summary.update({"status": "succeeded",
                            "sensitivity_status": f"refused:{exc}"})
            return summary
        stored = projections.publish(body)
        readiness = projection_readiness(stored)
        summary.update({
            "status": "succeeded",
            "sensitivity_status": ("published" if stored["status"] == "fresh"
                                   else stored["status"]),
            "projection_ref": stored["id"],
            "projection_version": stored["version"],
            "bridge_reason": (stored.get("consensus_bridge") or {}).get("reason"),
            **readiness,
        })
        return summary
    except SensitivityError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--company-ref",
                        help="project this company rather than the next")
    parser.add_argument("--dry-run", action="store_true",
                        help="choose and stop; no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_sensitivity(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        company_ref=args.company_ref, dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "build_parser",
    "choose_company",
    "latest_valuation",
    "main",
    "missing_write_scope",
    "pending_companies",
    "projection_fingerprint",
    "run_sensitivity",
]
