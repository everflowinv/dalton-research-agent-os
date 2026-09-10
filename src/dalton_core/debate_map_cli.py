"""P12c child: draw one subject's debate map, once, inside a mission's grant.

Approval first.  The run checks the mission's ``may_write`` before it spends
anything, because a model call made against a scope that was never granted is
money burnt to produce a refusal.

Nothing here decides that the map *should* change.  It assembles the table,
asks, verifies, and hands the result to the authority, which refuses a version
that learned nothing.  The judgement -- is this subject worth redrawing today
-- belongs to the lane, and the mechanism belongs to ``debate_map``.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cockpit_model import CockpitModel, CockpitModelError
from .coverage_mission import CoverageMissionAuthority
from .debate_map import (
    DebateMapAuthority,
    POLICY_HASH,
    POLICY_REF,
    contested_aspects,
    cited_refs,
    evidence_fingerprint,
)
from .debate_map_draft import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
    build_input_table,
    build_prompt,
    change_evidence,
    change_reason_for,
    draft_debate_map,
    route_family,
    subject_claim_rows,
    subject_driver_rows,
)
from .driver_template import debate_map_gaps
from .research_constitution import ResearchConstitutionAuthority
from .scheduler import SchedulerError
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"
WRITE_SCOPE = "debate_map"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)


def granted(mission: Any) -> bool:
    return WRITE_SCOPE in set((mission.get("autonomy") or {}).get("may_write") or [])


def subject_kind_for(subject_ref: str) -> str:
    return "industry" if subject_ref.startswith("industry:") else "company"


def can_rebind(previous: Any, mission: Any, constitution: Any,
               fingerprint: str) -> bool:
    """Whether only the mission identity moved, requiring no new judgement."""

    return bool(
        previous is not None
        and previous.get("mission_version_ref") != mission["id"]
        and previous["evidence_fingerprint"] == fingerprint
        and previous["constitution_ref"] == constitution["id"]
        and previous["constitution_hash"] == constitution["content_hash"]
        and previous["policy_ref"] == POLICY_REF
        and previous["policy_hash"] == POLICY_HASH
    )


def run_debate_map(
    *,
    state_dir: Path,
    subject_ref: str,
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
        "subject_ref": subject_ref,
        "map_status": None,
        "version_ref": None,
        "version": None,
        "claims": 0,
        "contested_aspects": 0,
        "debates": 0,
        "rejected": 0,
        "cost_micros": 0,
        "prompt_bytes": 0,
        "failure_reason": None,
        "policy_ref": POLICY_REF,
        "policy_hash": POLICY_HASH,
        "industry_classification": None,
        "template_gaps": [],
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "map_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        if not granted(mission):
            summary.update({
                "status": "held", "map_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "在授权前不起草，免得白花模型调用"
                ),
            })
            return summary
        actor_ref = mission["autonomy"]["automation_principal"]
        binding = (mission.get("bindings") or {}).get("constitution_version") or {}
        if not binding.get("ref"):
            summary.update({"status": "held", "map_status": "no_constitution",
                            "failure_reason": "the mission binds no constitution"})
            return summary
        constitution = ResearchConstitutionAuthority(store).constitution(binding["ref"])
        method = constitution["method"]

        classification = subject_classification(store, subject_ref)
        summary["industry_classification"] = classification
        rows = subject_claim_rows(store, subject_ref)
        summary["claims"] = len(rows)
        seeds = contested_aspects(rows)
        summary["contested_aspects"] = len(seeds)
        authority = DebateMapAuthority(store)
        previous = authority.current(subject_ref)
        current_fingerprint = evidence_fingerprint(
            row["claim_version_ref"] for row in rows)
        table = build_input_table(
            subject_ref=subject_ref,
            subject_kind=subject_kind_for(subject_ref),
            claim_rows=rows,
            driver_rows=subject_driver_rows(store, subject_ref, constitution),
            thesis=_thesis(store, subject_ref),
            method=method,
            previous=previous,
            industry_classification=classification,
        )
        summary["prompt_bytes"] = len(build_prompt(table).encode("utf-8"))
        if dry_run:
            summary.update({"status": "succeeded", "map_status": "dry_run"})
            return summary
        rebindable = can_rebind(previous, mission, constitution, current_fingerprint)
        if rebindable:
            # A mission version moved while the research inputs did not. Bind
            # the already verified map to the newly authorized mission without
            # buying the identical draft and verifier calls again.
            published = authority.publish_map(
                subject_ref=subject_ref,
                subject_kind=subject_kind_for(subject_ref),
                change_reason="mission_rebind",
                change_evidence_refs=sorted(cited_refs(previous)),
                constitution_ref=previous["constitution_ref"],
                constitution_hash=previous["constitution_hash"],
                policy_ref=previous["policy_ref"], policy_hash=previous["policy_hash"],
                evidence_fingerprint=current_fingerprint,
                mission_version_ref=mission["id"],
                mission_version_hash=mission["content_hash"],
                debates=previous["debates"],
                rejected_by_constitution=previous["rejected_by_constitution"],
                drafted_by=previous["drafted_by"], verified_by=previous["verified_by"],
                actor_ref=actor_ref, created_at=_now(),
            )
            summary.update({
                "status": "succeeded", "map_status": published["status"],
                "version_ref": published["id"], "version": published["version"],
                "debates": len(published["debates"]),
            })
            return summary
        if model_config_path is None:
            summary.update({"status": "succeeded", "map_status": "gated",
                            "failure_reason": "no model configured"})
            return summary
        config = json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        model = CockpitModel(
            config, scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        created_at = _now()
        try:
            drafted = draft_debate_map(
                table=table, method=method, model=model, mission=mission,
                created_at=created_at, previous=previous,
                family_of=lambda ref: route_family(config["model_router_db"], ref),
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "map_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "failed", "map_status": "model_unavailable",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["cost_micros"] = int(drafted.get("cost_micros") or 0)
        summary["rejected"] = len(drafted.get("rejected") or [])
        if drafted["status"] != "verified":
            run_status = (
                "succeeded"
                if drafted["status"] == "no_admitted_debates"
                else "failed"
            )
            summary.update({"status": run_status, "map_status": drafted["status"],
                            "failure_reason": drafted.get("reason")})
            return summary
        debates = drafted["debates"]
        summary["debates"] = len(debates)
        # Reported, never a refusal: a driver question this kind of company is
        # normally argued about that nothing on this map argues about is a hole
        # in the map, and the reader is the one who decides whether it matters.
        summary["template_gaps"] = debate_map_gaps(debates, classification)
        refs = change_evidence(debates, previous)
        mission_changed = (
            previous is not None
            and (previous.get("mission_version_ref") != mission["id"]
                 or previous.get("mission_version_hash") != mission["content_hash"])
        )
        if not refs and mission_changed:
            refs = sorted(cited_refs({"debates": debates}))
        if not refs:
            summary.update({"status": "succeeded", "map_status": "duplicate",
                            "failure_reason": "no reference the current version "
                                              "does not already cite"})
            return summary
        published = authority.publish_map(
            subject_ref=subject_ref,
            subject_kind=subject_kind_for(subject_ref),
            change_reason=("mission_rebind" if mission_changed
                           else change_reason_for(previous)),
            change_evidence_refs=refs,
            constitution_ref=constitution["id"],
            constitution_hash=constitution["content_hash"],
            evidence_fingerprint=current_fingerprint,
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
            debates=debates,
            rejected_by_constitution=drafted["rejected"],
            drafted_by=drafted["drafted_by"],
            verified_by=drafted["verified_by"],
            actor_ref=actor_ref,
            created_at=created_at,
        )
        summary.update({
            "status": "succeeded", "map_status": published["status"],
            "version_ref": published["id"], "version": published["version"],
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def subject_classification(store: Any, subject_ref: str) -> str | None:
    """The subject's current ``industry_classification``, when it has one.

    Companies have dossiers; industries do not, so an industry subject simply
    has no classification and gets the generic template. Read rather than
    inferred: the classification is a versioned judgement in the dossier, and a
    second place that decided it would be a second answer to one question.
    """

    from .company_dossier import CompanyDossierAuthority
    from .company_dossier_cli import table_exists

    if not table_exists(store.connection, "company_dossier_versions"):
        return None
    record = CompanyDossierAuthority(store).latest(subject_ref)
    if record is None:
        return None
    word = str((record.get("industry_classification") or {})
               .get("classification") or "")
    return word or None


def _thesis(store: Any, subject_ref: str) -> dict[str, Any] | None:
    from .company_research_view import build_company_research_view

    try:
        return build_company_research_view(store, subject_ref).get("thesis")
    except Exception:  # noqa: BLE001 - an unreadable thesis is "no thesis"
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draw one subject's debate map")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--subject-ref", required=True)
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--model-config")
    parser.add_argument("--scheduler-db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_debate_map(
        state_dir=Path(args.state_dir),
        subject_ref=args.subject_ref,
        summary_dir=Path(args.summary_dir),
        model_config_path=None if args.model_config is None else Path(args.model_config),
        scheduler_db=None if args.scheduler_db is None else Path(args.scheduler_db),
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


__all__ = [
    "SUMMARY_SCHEMA_VERSION",
    "WRITE_SCOPE",
    "build_parser",
    "granted",
    "main",
    "run_debate_map",
    "subject_kind_for",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
