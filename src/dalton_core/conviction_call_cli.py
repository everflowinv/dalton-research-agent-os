"""P15d child: decide whether one company is worth a call, and write one.

Approval first, then the gate, then -- and only then -- a model.  The run
checks the mission's ``may_write`` and its human checkpoints before it spends
anything, because a proposal made against a scope that was never granted is
money burnt to produce a refusal, and a proposal made against a mission with
no ``conviction_call`` checkpoint would be a proposal nobody has agreed to
decide.

The gate is the interesting part and it is free.  ``precheck`` reads the
theses, the debate map, the forecast-versus-consensus bridge and the company
file's own variant view, and answers a mechanical question: do we hold a view,
is it different from the market's, and can we source what the market's is.  A
company that fails any of the three is reported with the reason and no model
is called.  On the live Core today every company fails at least one, which is
the honest state of the system rather than a bug.

Nothing here decides that a call *should* be published.  It assembles the
table, asks, verifies, and hands the result to the authority, which refuses a
second call on unchanged evidence and a second call in the same week.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalyst_calendar import CatalystCalendarAuthority
from .cockpit_model import CockpitModel, CockpitModelError
from .conviction_call import (
    CHECKPOINT_KIND,
    POLICY_HASH,
    POLICY_REF,
    WRITE_SCOPE,
    ConvictionCallAuthority,
    evidence_fingerprint,
    precheck,
)
from .conviction_call_draft import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
    build_input_table,
    build_prompt,
    draft_conviction_call,
    route_family,
)
from .coverage_mission import CoverageMissionAuthority
from .research_quality_rubrics import CONVICTION_CALL
from .scheduler import SchedulerError
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"

# How far ahead a catalyst is still part of a pathway.  A year out is not a
# pathway, it is a calendar; a quarter is the horizon a person can hold.
CATALYST_HORIZON_DAYS = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)


def granted(mission: Any) -> bool:
    return WRITE_SCOPE in set((mission.get("autonomy") or {}).get("may_write") or [])


def checkpointed(mission: Any) -> bool:
    """A proposal is only worth making where someone has agreed to decide it."""

    return CHECKPOINT_KIND in set(
        (mission.get("autonomy") or {}).get("human_checkpoints") or [])


def _table_exists(connection: Any, name: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# ---------------------------------------------------------------------------
# reading the inputs out of a Core
# ---------------------------------------------------------------------------

def company_theses(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """Every current thesis for this company, in P14a's own shape."""

    from .event_judgement import company_theses as read

    try:
        return [
            {**row, "thesis_version_ref": row.get("ref")}
            for row in read(store.connection, company_ref)
        ]
    except Exception:  # noqa: BLE001 - an unreadable thesis is "no thesis"
        return []


def open_debates(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """The live debates for this company, or none when there is no map.

    The authority is only constructed when its table is already there: opening
    it installs its schema, and a lane that reads a company file should not
    leave a debate-map schema behind on a Core that has never drawn one.
    """

    from .debate_map import DebateMapAuthority, table_exists

    if not table_exists(store.connection):
        return []
    try:
        return DebateMapAuthority(store).open_debates(company_ref)
    except Exception:  # noqa: BLE001 - an unreadable map is no map
        return []


def latest_dossier(store: Any, company_ref: str) -> dict[str, Any] | None:
    if not _table_exists(store.connection, "company_dossier_versions"):
        return None
    from .company_dossier import CompanyDossierAuthority

    try:
        return CompanyDossierAuthority(store).latest(company_ref)
    except Exception:  # noqa: BLE001 - an unreadable file is no file
        return None


def latest_valuation(store: Any, company_ref: str) -> dict[str, Any] | None:
    if not _table_exists(store.connection, "valuation_snapshot_versions"):
        return None
    from .valuation_snapshot import ValuationSnapshotAuthority

    try:
        return ValuationSnapshotAuthority(store).latest_version(company_ref)
    except Exception:  # noqa: BLE001
        return None


def upcoming_catalysts(store: Any, company_ref: str) -> list[dict[str, Any]]:
    if not _table_exists(store.connection, "catalyst_calendar_versions"):
        return []
    try:
        authority = CatalystCalendarAuthority(store)
        return [
            row for row in authority.upcoming(horizon_days=CATALYST_HORIZON_DAYS)
            if row.get("company_ref") == company_ref
        ]
    except Exception:  # noqa: BLE001
        return []


def consensus_gap(store: Any, company_ref: str) -> dict[str, Any]:
    """Our forecast against the street's, or the reason there is no street.

    P11b's consensus authority is Wave 2 and is not on this branch.  It is
    resolved by name at call time rather than imported, so that this lane
    starts working the day that authority lands without a change here, and
    reports an honest ``unavailable`` until then.  A gap section that quietly
    vanished when the source was missing would read as "we agree with
    consensus", which is the one thing a conviction call must never
    accidentally say.
    """

    try:
        from . import consensus_estimate  # type: ignore[attr-defined]
    except ImportError:
        return {"status": "unavailable", "metrics": [],
                "reason": "this Core has no consensus authority (P11b is not built yet), "
                          "so the forecast-versus-street gap cannot be computed"}
    reader = getattr(consensus_estimate, "latest_consensus", None)
    if reader is None:
        return {"status": "unavailable", "metrics": [],
                "reason": "the consensus module on this Core exposes no "
                          "latest_consensus reader"}
    try:
        found = reader(store, company_ref)
    except Exception as exc:  # noqa: BLE001 - an unreadable street is no street
        return {"status": "unavailable", "metrics": [],
                "reason": f"the consensus authority could not be read: "
                          f"{type(exc).__name__}: {exc}"}
    if not found or not (found.get("metrics") or ()):
        return {"status": "unavailable", "metrics": [],
                "reason": f"no consensus estimate is held for {company_ref}"}
    return {"status": "available", "reason": None,
            "metrics": [dict(row) for row in found["metrics"]]}


def gate_inputs(store: Any, company_ref: str) -> dict[str, Any]:
    """Everything the deterministic gate reads, in one place."""

    dossier = latest_dossier(store, company_ref)
    return {
        "theses": company_theses(store, company_ref),
        "open_debates": open_debates(store, company_ref),
        "consensus_gap": consensus_gap(store, company_ref),
        "dossier": dossier,
        "dossier_variant_view": None if dossier is None else dossier.get("variant_view"),
        "catalysts": upcoming_catalysts(store, company_ref),
        "valuation": latest_valuation(store, company_ref),
    }


def fingerprint_of(inputs: dict[str, Any], gate: dict[str, Any]) -> str:
    """The exact material this call would be drawn from."""

    refs = [row["thesis_version_ref"] for row in inputs["theses"]
            if row.get("thesis_version_ref")]
    refs += [row["debate_ref"] for row in gate["divergent_debates"] if row.get("debate_ref")]
    refs += [ref for row in gate["consensus_gaps"] for ref in row.get("refs") or ()]
    refs += [row["entry_ref"] for row in inputs["catalysts"] if row.get("entry_ref")]
    if inputs["dossier"] is not None:
        refs.append(inputs["dossier"]["id"])
    if inputs["valuation"] is not None:
        refs.append(inputs["valuation"]["id"])
    return evidence_fingerprint(refs)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def run_conviction_call(
    *,
    state_dir: Path,
    company_ref: str,
    summary_dir: Path,
    model_config_path: Path | None = None,
    scheduler_db: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "mode": "dry_run" if dry_run else "model",
        "company_ref": company_ref,
        "call_status": None,
        "proposal_ref": None,
        "direction": None,
        "eligible": None,
        "gate_reasons": [],
        "evidence_fingerprint": None,
        "divergent_debates": 0,
        "consensus_gaps": 0,
        "cost_micros": 0,
        "prompt_bytes": 0,
        "rubric_findings": [],
        "failure_reason": None,
        "policy_ref": POLICY_REF,
        "policy_hash": POLICY_HASH,
        "rubric_ref": CONVICTION_CALL.rubric_ref,
        "rubric_hash": CONVICTION_CALL.content_hash,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "call_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        if not granted(mission):
            summary.update({
                "status": "held", "call_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "在授权前不起草，免得白花模型调用"),
            })
            return summary
        if not checkpointed(mission):
            summary.update({
                "status": "held", "call_status": "no_checkpoint",
                "failure_reason": (
                    f"任务 {mission['id']} 没有 {CHECKPOINT_KIND} 人类检查点；"
                    "没人答应裁决的提案不值得提"),
            })
            return summary
        actor_ref = mission["autonomy"]["automation_principal"]

        inputs = gate_inputs(store, company_ref)
        gate = precheck(
            company_ref=company_ref,
            theses=inputs["theses"],
            open_debates=inputs["open_debates"],
            consensus_gap=inputs["consensus_gap"],
            dossier_variant_view=inputs["dossier_variant_view"],
        )
        summary.update({
            "eligible": gate["eligible"],
            "gate_reasons": list(gate["reasons"]),
            "divergent_debates": len(gate["divergent_debates"]),
            "consensus_gaps": len(gate["consensus_gaps"]),
        })
        if not gate["eligible"]:
            # The cheapest refusal there is, and the most common one. Agreeing
            # with the market has no value, so a company that cannot be shown
            # to differ from it is not drafted at all.
            summary.update({"status": "succeeded", "call_status": "not_eligible",
                            "failure_reason": ", ".join(gate["reasons"])})
            return summary

        fingerprint = fingerprint_of(inputs, gate)
        summary["evidence_fingerprint"] = fingerprint
        table = build_input_table(
            company_ref=company_ref,
            precheck_record=gate,
            theses=inputs["theses"],
            open_debates=inputs["open_debates"],
            consensus_gap=inputs["consensus_gap"],
            dossier_variant_view=inputs["dossier_variant_view"],
            dossier_version_ref=None if inputs["dossier"] is None else inputs["dossier"]["id"],
            catalyst_entries=inputs["catalysts"],
            valuation=inputs["valuation"],
        )
        summary["prompt_bytes"] = len(build_prompt(table).encode("utf-8"))
        if dry_run:
            summary.update({"status": "succeeded", "call_status": "dry_run"})
            return summary
        if model_config_path is None:
            summary.update({"status": "succeeded", "call_status": "gated",
                            "failure_reason": "no model configured"})
            return summary

        authority = ConvictionCallAuthority(store)
        config = json.loads(
            Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        model = CockpitModel(
            config, scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        created_at = _now()
        try:
            drafted = draft_conviction_call(
                table=table, model=model, mission=mission,
                family_of=lambda ref: route_family(config["model_router_db"], ref),
                rubric_ref=CONVICTION_CALL.rubric_ref,
                rubric_hash=CONVICTION_CALL.content_hash,
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "call_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "succeeded", "call_status": "model_unavailable",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["cost_micros"] = int(drafted.get("cost_micros") or 0)
        summary["rubric_findings"] = list(drafted["rubric"]["findings"])
        if drafted["status"] != "verified":
            summary.update({"status": "succeeded", "call_status": drafted["status"],
                            "failure_reason": drafted.get("reason")})
            return summary
        call = drafted["call"]
        published = authority.propose(
            company_ref=company_ref,
            direction=call["direction"],
            decision=call["decision"],
            confidence=call["confidence"],
            time_horizon=call["time_horizon"],
            variant_view=call["variant_view"],
            consensus_gap=call["consensus_gap"],
            event_pathway=call["event_pathway"],
            risk_reward=call["risk_reward"],
            falsifiers=call["falsifiers"],
            thesis_refs=call["thesis_refs"],
            debate_refs=call["debate_refs"],
            evidence_fingerprint=fingerprint,
            precheck_record=gate,
            rubric=drafted["rubric"],
            mission=mission,
            actor_ref=actor_ref,
            created_at=created_at,
            drafted_by=drafted["drafted_by"],
            verified_by=drafted["verified_by"],
        )
        summary.update({
            "status": "succeeded", "call_status": published["status"],
            "proposal_ref": published.get("id"),
            "direction": published.get("direction"),
            "failure_reason": published.get("reason"),
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Propose one company's conviction call, or say why not")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--model-config")
    parser.add_argument("--scheduler-db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_conviction_call(
        state_dir=Path(args.state_dir),
        company_ref=args.company_ref,
        summary_dir=Path(args.summary_dir),
        model_config_path=None if args.model_config is None else Path(args.model_config),
        scheduler_db=None if args.scheduler_db is None else Path(args.scheduler_db),
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


__all__ = [
    "CATALYST_HORIZON_DAYS",
    "SUMMARY_SCHEMA_VERSION",
    "WRITE_SCOPE",
    "build_parser",
    "checkpointed",
    "company_theses",
    "consensus_gap",
    "fingerprint_of",
    "gate_inputs",
    "granted",
    "latest_dossier",
    "latest_valuation",
    "main",
    "open_debates",
    "run_conviction_call",
    "upcoming_catalysts",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
