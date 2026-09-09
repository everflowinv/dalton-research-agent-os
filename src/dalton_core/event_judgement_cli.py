"""P14a: the child that judges a bounded batch of events, one call each.

Everything expensive happens here, so everything careful happens here too.

The batch is bounded three ways and any of them can end the run: the number of
events per company, the number per run, and the ``event_response`` share of
the mission's day.  The pool is checked *before* each pair of calls and the
reservation is the two calls' own ceilings, because a pool that only counts
settled spend admits a whole day's work before the first bill arrives (P14e
found this and ``ThesisImpactBudgetStore`` found it before that).

Every event is judged at most once, and that is enforced by a UNIQUE column
rather than by this loop remembering: the loop is one process and the ledger
outlives it.

The verifier runs on a second model configuration.  If it is absent, or its
family cannot be resolved, or it resolves to the producer's family, the
judgement is recorded as ``refused`` and no effect is applied -- an unverified
decision is not a decision this system acts on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cockpit_model import CockpitModel
from .coverage_mission import CoverageMissionAuthority
from .event_judgement import (
    EventJudgementAuthority,
    EventJudgementError,
    apply_effect,
    build_context,
    judge,
    pool_state,
    reflect,
    reflection_is_owed,
    route_family_resolver,
    verify,
    verify_reflection,
)
from .mission_deliverable import MissionDeliverableAuthority
from .model_configurations import register_model_config_name
from .research_event import ResearchEventAuthority
from .source_capability_map import build_map, prompt_table
from .store import DaltonStore
from .tracking_cadence import load_policy, screen_passed_companies

SUMMARY_SCHEMA_VERSION = "0.1"
JUDGE_MODEL_CONFIG = register_model_config_name("event-judgement-model-config.json")
VERIFIER_MODEL_CONFIG = register_model_config_name("event-verifier-model-config.json")

# One event's prompt is a table, not a document: the theses, a handful of
# drivers, five past decisions and a dozen Claims. It has never needed a large
# window and giving it one would let a bad context grow into a bad answer.
MAX_INPUT_TOKENS = 60_000
MAX_OUTPUT_TOKENS = 1_500
MAX_COST_USD = 0.10
TIMEOUT_SECONDS = 180
MAX_EVENTS_PER_COMPANY = 3
MAX_EVENTS_PER_RUN = 8


def _write_owner_only(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def missing_write_scopes(mission: Any) -> list[str]:
    """What the judgement lane needs granted before it spends anything.

    ``deliverable`` is the event note, ``forecast_line`` the revision entry
    point, ``thesis_revision_candidate`` the ADR-0007 proposal.  Only the
    first is required: a mission that grants none of the other two still gets
    judgements and notes, and its revise-shaped decisions become proposals
    rather than being dropped.  The list is reported so an operator can see
    which half of the brain is switched off.
    """

    granted = set(mission["autonomy"]["may_write"])
    return [
        word for word in ("deliverable", "forecast_line", "thesis_revision_candidate")
        if word not in granted
    ]


def unjudged_events(
    events: ResearchEventAuthority,
    judgements: EventJudgementAuthority,
    *,
    company_ref: str,
    limit: int,
) -> list[dict[str, Any]]:
    """This company's newest events with no judgement, oldest of those first.

    Newest first from the ledger, then reversed: within one bounded batch the
    older events are judged first, so a decision reads the ones before it in
    the order they happened rather than backwards.
    """

    judged = judgements.judged_event_refs(company_ref)
    rows = [
        event for event in events.events(company_ref=company_ref, limit=limit * 8)
        if event["id"] not in judged
    ]
    return list(reversed(rows[: limit * 8]))[-limit:] if rows else []


def _model(config_path: Path | None, state_dir: Path, scheduler_db: Path | None) -> Any:
    if config_path is None:
        return None
    return CockpitModel(
        json.loads(Path(config_path).expanduser().read_text(encoding="utf-8")),
        scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
        max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
    )


def research_admitter_for(store: DaltonStore, mission: Any):
    """P14e's admission entry point, by name, or nothing.

    Called by name because P14e is a branch and not yet on main: if the module
    is absent the ``research`` decision is queued with a reason rather than
    silently dropped, and when the branch merges this begins working with no
    change here.
    """

    try:
        from .bounded_planner_loop import BoundedPlannerAuthority
        from .research_task import admit_inquiry, plan_admissions
    except ImportError:
        return None

    def admit(event: Any, judgement: Any) -> dict[str, Any]:
        authority = BoundedPlannerAuthority(store)
        inquiry = {
            "rank": 1,
            "question": judgement["research_question"],
            "wants": judgement["because"],
            "company_ref": event["company_ref"],
        }
        plan = {"inquiries": [inquiry]}
        entries = plan_admissions(authority, mission=mission, plan=plan)
        entry = entries[0] if entries else None
        if entry is None or not entry.get("admissible"):
            return {"status": "queued",
                    "reason": (entry or {}).get("reason") or "no admissible inquiry"}
        admitted = admit_inquiry(
            authority, _backlog(store), mission=mission,
            plan_ref=event["id"], inquiry=inquiry, entry=entry,
        )
        return {"status": "admitted", "loop_ref": admitted.get("loop_ref"),
                "inquiry_ref": admitted.get("inquiry_ref")}

    return admit


def _backlog(store: DaltonStore) -> Any:
    from .research_question_backlog import ResearchQuestionBacklog

    return ResearchQuestionBacklog(store)


def run_judgement(
    *,
    state_dir: Path,
    summary_dir: Path,
    judge_model_config: Path | None = None,
    verifier_model_config: Path | None = None,
    policy_path: Path | None = None,
    scheduler_db: Path | None = None,
    company_ref: str | None = None,
    max_events: int = MAX_EVENTS_PER_RUN,
    per_company: int = MAX_EVENTS_PER_COMPANY,
    dry_run: bool = False,
    judge_model: Any = None,
    verifier_model: Any = None,
    family_resolver: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": moment.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "judge",
        "judgement_status": None,
        "ungranted": [],
        "pool": None,
        "candidates": 0,
        "judged": 0,
        "refused": 0,
        "decisions": {},
        "actions": {},
        "reflections": 0,
        "reflections_refused": 0,
        "followups": [],
        "effects": [],
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "judgement_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        summary["ungranted"] = missing_write_scopes(mission)
        if "deliverable" in summary["ungranted"]:
            summary.update({
                "status": "idle", "judgement_status": "ungranted",
                "failure_reason": "the mission does not grant deliverable writes; "
                                  "a judgement that can produce no note is not worth paying for",
            })
            return summary
        events = ResearchEventAuthority(store)
        judgements = EventJudgementAuthority(store)
        tracked = screen_passed_companies(missions, mission)
        if company_ref is not None:
            tracked = [ref for ref in tracked if ref == company_ref]
        state = pool_state(judgements, mission, day=moment.date().isoformat())
        summary["pool"] = state

        batch: list[dict[str, Any]] = []
        for ref in tracked:
            batch.extend(
                unjudged_events(events, judgements, company_ref=ref, limit=per_company)
            )
        batch = batch[:max_events]
        summary["candidates"] = len(batch)
        if not batch:
            summary.update({"status": "idle", "judgement_status": "nothing_unjudged"})
            return summary
        if dry_run:
            summary.update({"status": "succeeded", "judgement_status": "dry_run"})
            return summary

        judge_model = judge_model or _model(judge_model_config, state_dir, scheduler_db)
        verifier_model = verifier_model or _model(
            verifier_model_config, state_dir, scheduler_db
        )
        if judge_model is None or verifier_model is None:
            summary.update({
                "status": "idle", "judgement_status": "gated",
                "failure_reason": "the judgement lane needs a judge and an independent "
                                  "verifier configuration; it will not run on one",
            })
            return summary
        if family_resolver is None:
            config = json.loads(
                Path(judge_model_config).expanduser().read_text(encoding="utf-8")
            ) if judge_model_config else {}
            family_resolver = route_family_resolver(config.get("model_router_db"))

        policy = load_policy(policy_path) if policy_path else None
        source_projection = build_map(
            mission=mission,
            cadences=None if policy is None else policy["cadences"],
        )
        table = prompt_table(source_projection)
        deliverables = MissionDeliverableAuthority(store)
        playbook = _playbook(store, mission)
        forecast_models, model_versions = _forecast_models(store, tracked)
        admitter = research_admitter_for(store, mission)
        actor = mission["autonomy"]["automation_principal"]

        spent = 0
        # Four calls, not two: a divergence or a revise-shaped decision owes a
        # reflection and its verification as well. Reserving the pair only
        # would admit an event whose reflection then has nothing left to spend,
        # which is the shape where a decision is recorded without the account
        # of what we may have missed -- exactly the half the owner asked for.
        reservation = int(MAX_COST_USD * 4 * 1_000_000)
        for event in batch:
            if state["remaining_micros"] - spent < reservation:
                summary["judgement_status"] = "skipped:pool_exhausted"
                break
            context = build_context(
                event=event, mission=mission, connection=store.connection,
                judgements=judgements,
                model_version=model_versions.get(event["company_ref"]),
                source_table=table,
                recent_events=events.events(company_ref=event["company_ref"], limit=40),
                source_keys=() if policy is None else sorted(policy["cadences"]),
            )
            request_id = event["id"].split(":", 1)[-1][:32]
            decided = judge(
                context, model=judge_model, mission=mission, request_id=request_id
            )
            checked = verify(
                context, decided, model=verifier_model, mission=mission,
                request_id=f"v{request_id}"[:32], family_resolver=family_resolver,
            )
            spent += int((decided.get("model") or {}).get("cost_micros") or 0)
            spent += int((checked.get("model") or {}).get("cost_micros") or 0)
            if decided["status"] != "judged" or checked.get("verdict") != "pass":
                summary["refused"] += 1
                summary["effects"].append({
                    "event_ref": event["id"], "status": "refused",
                    "reason": decided.get("reason") or checked.get("reason")
                    or "the verifier rejected the decision",
                    "findings": checked.get("findings") or [],
                })
                continue
            effect = apply_effect(
                event=event, judgement=decided, context=context, mission=mission,
                playbook=playbook, deliverables=deliverables,
                forecast_models=forecast_models,
                model_version=model_versions.get(event["company_ref"]),
                research_admitter=admitter, actor_ref=actor,
            )
            written = judgements.record(
                event=event, judgement=decided, verification=checked,
                effect=effect, mission=mission, actor_ref=actor,
            )
            # The owner's third instruction: when we changed our mind, or when
            # the price kept running against us, write down what we expected
            # and what we may have missed -- including when the decision was to
            # hold. The reflection changes nothing; it is attached to the
            # candidate so the person deciding sees both.
            reflection_ref = None
            if reflection_is_owed(event, decided):
                thought = reflect(context, decided, model=judge_model,
                                  mission=mission, request_id=f"f{request_id}"[:32])
                reviewed = verify_reflection(
                    context, thought, model=verifier_model, mission=mission,
                    request_id=f"fv{request_id}"[:32], family_resolver=family_resolver,
                )
                spent += int((thought.get("model") or {}).get("cost_micros") or 0)
                spent += int((reviewed.get("model") or {}).get("cost_micros") or 0)
                if thought["status"] == "reflected" and reviewed.get("verdict") == "pass":
                    recorded = judgements.record_reflection(
                        judgement=written, event=event, reflection=thought,
                        verification=reviewed, mission=mission, actor_ref=actor,
                    )
                    reflection_ref = recorded["id"]
                    summary["reflections"] += 1
                    summary["followups"].append({
                        "reflection_ref": reflection_ref,
                        "tracking": recorded["followup_tracking"],
                        "research": recorded["followup_research"],
                    })
                else:
                    summary["reflections_refused"] += 1
                    summary["effects"].append({
                        "event_ref": event["id"], "kind": "reflection",
                        "status": "refused",
                        "reason": thought.get("reason") or reviewed.get("reason")
                        or "the verifier rejected the reflection",
                    })
            if effect.get("status") == "candidate":
                effect = dict(effect)
                effect["candidates"] = [
                    judgements.record_thesis_candidate(
                        judgement_ref=written["id"], thesis=thesis,
                        company_ref=event["company_ref"], decision=decided["decision"],
                        because=decided["because"], evidence_refs=decided["citations"],
                        falsifier_ref=next(iter(thesis.get("falsifier_refs") or ()), None),
                        proposed_statement=None, proposed_confidence=None,
                        mission=mission, actor_ref=actor,
                        reflection_ref=reflection_ref,
                    )["id"]
                    for thesis in effect["theses"]
                ]
                effect.pop("theses", None)
            elif effect.get("status") == "proposed":
                effect = dict(effect)
                effect["proposal_ref"] = judgements.record_forecast_proposal(
                    judgement_ref=written["id"], company_ref=event["company_ref"],
                    model_version_ref=context.get("model_version_ref"),
                    change=decided["forecast_change"], decision=decided["decision"],
                    because=decided["because"], evidence_refs=decided["citations"],
                    mission=mission, actor_ref=actor, reason=effect["reason"],
                )["id"]
            summary["judged"] += 1
            summary["decisions"][decided["decision"]] = (
                summary["decisions"].get(decided["decision"], 0) + 1
            )
            summary["actions"][decided["action"]] = (
                summary["actions"].get(decided["action"], 0) + 1
            )
            summary["effects"].append({
                "event_ref": event["id"], "judgement_ref": written["id"], **effect,
            })
        summary["cost_micros"] = spent
        summary["formal_authority_writes"] = summary["judged"]
        summary["status"] = "succeeded"
        summary["judgement_status"] = summary["judgement_status"] or (
            "judged" if summary["judged"] else "refused"
        )
        return summary
    except EventJudgementError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def _playbook(store: DaltonStore, mission: Any) -> dict[str, Any] | None:
    from .research_playbook import read_exact_playbook_version

    try:
        return read_exact_playbook_version(
            store.connection, mission["bindings"]["playbook_version"]["ref"]
        )
    except Exception:  # noqa: BLE001 - no playbook means no note, not a crash
        return None


def _forecast_models(store: DaltonStore, companies: list[str]) -> tuple[Any, dict[str, Any]]:
    from .model_forecast_driver import ForecastModelAuthority

    authority = ForecastModelAuthority(store)
    versions: dict[str, Any] = {}
    for ref in companies:
        try:
            latest = authority.latest(ref)
        except Exception:  # noqa: BLE001
            latest = None
        if latest is not None:
            versions[ref] = latest
    return authority, versions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--judge-model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path)
    parser.add_argument("--tracking-policy", type=Path)
    parser.add_argument("--scheduler", type=Path)
    parser.add_argument("--company-ref")
    parser.add_argument("--max-events", type=int, default=MAX_EVENTS_PER_RUN)
    parser.add_argument("--per-company", type=int, default=MAX_EVENTS_PER_COMPANY)
    parser.add_argument("--dry-run", action="store_true", help="count and stop; no calls")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_judgement(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        judge_model_config=args.judge_model_config,
        verifier_model_config=args.verifier_model_config,
        policy_path=args.tracking_policy,
        scheduler_db=args.scheduler,
        company_ref=args.company_ref,
        max_events=args.max_events,
        per_company=args.per_company,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "JUDGE_MODEL_CONFIG",
    "MAX_COST_USD",
    "MAX_EVENTS_PER_COMPANY",
    "MAX_EVENTS_PER_RUN",
    "VERIFIER_MODEL_CONFIG",
    "build_parser",
    "main",
    "missing_write_scopes",
    "research_admitter_for",
    "run_judgement",
    "unjudged_events",
]
