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
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .budget_pools import POOL_EXHAUSTED_STATUS
from .call_budget import resolve_run_budget
from .cockpit_model import CockpitModel
from .coverage_mission import CoverageMissionAuthority
from .event_judgement import (
    MAX_PROMPT_BYTES,
    PURPOSE,
    REFLECTION_PURPOSE,
    REFLECTION_VERIFIER_PURPOSE,
    VERIFIER_PURPOSE,
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
from .store import content_hash
from .store import DaltonStore
from .tracking_cadence import load_policy, screen_passed_companies

SUMMARY_SCHEMA_VERSION = "0.1"
EVENT_MODEL_CONTRACT_VERSION = "2"
JUDGE_MODEL_CONFIG = register_model_config_name("event-judgement-model-config.json")
VERIFIER_MODEL_CONFIG = register_model_config_name("event-verifier-model-config.json")

# One event's prompt is a table, not a document: the theses, a handful of
# drivers, five past decisions and a dozen Claims. It has never needed a large
# window and giving it one would let a bad context grow into a bad answer.
# CockpitModel enforces this field as UTF-8 bytes and also exposes it to the
# router as the worst-case input-token reservation. Keep the two bounds equal:
# advertising the old 60k reservation made every current brain route exceed
# this lane's unchanged $0.10 per-call cap before a call could start.
MAX_INPUT_TOKENS = MAX_PROMPT_BYTES
MAX_OUTPUT_TOKENS = 1_500
MAX_COST_USD = 1.0
TIMEOUT_SECONDS = 180
MAX_EVENTS_PER_COMPANY = 3
MAX_EVENTS_PER_RUN = 8
HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


def event_model_contract_ref(budget: dict[str, Any] | None = None) -> str:
    limits = budget or {
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD,
        "timeout_seconds": TIMEOUT_SECONDS,
    }
    fingerprint = content_hash({
        "version": EVENT_MODEL_CONTRACT_VERSION,
        "max_input_bytes": limits["max_input_tokens"],
        "max_output_tokens": limits["max_output_tokens"],
        "max_cost_usd": str(limits["max_cost_usd"]),
        "timeout_seconds": limits["timeout_seconds"],
    })
    return f"event-model-contract:{fingerprint[:16]}"


class EventCockpitModel(CockpitModel):
    """Namespace scheduler identity to this lane's prompt and budget contract."""

    def call(self, *, purpose: str, request_id: str, prompt: str,
             mission: Any, producer_route_decision_refs=()):
        return super().call(
            purpose=purpose,
            request_id=f"{event_model_contract_ref(self.budget_for(purpose))}:{request_id}",
            prompt=prompt,
            mission=mission,
            producer_route_decision_refs=producer_route_decision_refs,
        )


def buyback_group_key(event: dict[str, Any]) -> tuple[str, ...] | None:
    payload = event.get("payload") or {}
    if event.get("kind") != "buyback_disclosure":
        return None
    if payload.get("market") == "HK":
        cluster = str(payload.get("cluster_key") or "")
        week = cluster.split(":", 1)[0]
        try:
            datetime.strptime(f"{week}-1", "%G-W%V-%u")
        except ValueError:
            return None
        else:
            return ("hk_week", str(event.get("company_ref")), week)
    accession = payload.get("accession")
    if (payload.get("disclosure_kind") == "issuer_purchases_table"
            and isinstance(accession, str) and accession):
        return ("us_filing", accession)
    return None


def _closed_hk_week(group_key: tuple[str, ...] | None, now: datetime) -> bool:
    if group_key is None or group_key[0] != "hk_week":
        return True
    local = now.astimezone(HK_TIMEZONE)
    year, week, _ = local.isocalendar()
    return group_key[2] < f"{year:04d}-W{week:02d}"


def group_evidence_events(
    events: ResearchEventAuthority, event_group: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """All evidence in an HK week; only new rows are judgement aliases."""

    key = buyback_group_key(event_group[0])
    if key is None or key[0] != "hk_week":
        return event_group
    rows = events.connection.execute(
        "SELECT event_id FROM research_events WHERE company_ref=? "
        "AND kind='buyback_disclosure' ORDER BY occurred_at, event_id",
        (event_group[0]["company_ref"],),
    ).fetchall()
    matching = [event for row in rows if buyback_group_key(
        event := events.event(row["event_id"])) == key]
    primary = event_group[0]
    return [primary, *(event for event in matching if event["id"] != primary["id"])]


def incremental_group_hash(event_group: list[dict[str, Any]]) -> str:
    return content_hash({"events": [
        {"id": event["id"], "content_hash": event.get("content_hash")}
        for event in sorted(event_group, key=lambda item: item["id"])
    ]})


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
    limit: int | None,
) -> list[dict[str, Any]]:
    """This company's oldest events with no judgement.

    An anti-join in SQL rather than "read the newest N and filter": the filter
    version reads a fixed window and, once that window is entirely judged,
    returns nothing while older events sit unjudged for ever. Oldest first so
    a decision reads the events before it in the order they happened, and so
    the backlog drains from the end that is not moving.
    """

    rows = events.connection.execute(
        "SELECT e.event_id AS event_id FROM research_events e "
        "LEFT JOIN event_judgements j ON j.event_ref = e.event_id "
        "WHERE e.company_ref = ? AND j.event_ref IS NULL "
        "ORDER BY e.occurred_at ASC, e.event_id ASC LIMIT ?",
        (company_ref, max(1, int(limit))),
    ).fetchall()
    return [events.event(row["event_id"]) for row in rows]


def unjudged_event_groups(
    events: ResearchEventAuthority,
    judgements: EventJudgementAuthority,
    *,
    company_ref: str,
    limit: int,
    now: datetime | None = None,
    mission_version_refs: tuple[str, ...] | None = None,
    newest_first: bool = False,
) -> list[list[dict[str, Any]]]:
    """Oldest eligible events, grouped by one filing or closed HK ISO week.

    A US 10-Q normally contributes three monthly rows. They remain three
    immutable events because the month in which purchases stopped is evidence,
    but the analyst reads their common accession as one table and makes one
    judgement. HK daily rows remain raw while the current Hong Kong week is
    open, then share one company/week judgement slot. Other kinds, including
    Form 4 insider transactions, retain one slot each.
    """

    mission_sql = ""
    parameters: list[Any] = [company_ref]
    if mission_version_refs is not None:
        if not mission_version_refs:
            return []
        mission_sql = " AND e.mission_version_ref IN (" + ",".join(
            "?" for _ in mission_version_refs) + ")"
        parameters.extend(mission_version_refs)
    rows = events.connection.execute(
        "SELECT e.event_id AS event_id FROM research_events e "
        "LEFT JOIN event_judgements j ON j.event_ref = e.event_id "
        "WHERE e.company_ref = ? AND j.event_ref IS NULL " + mission_sql +
        "ORDER BY e.occurred_at " + ("DESC" if newest_first else "ASC")
        + ", e.event_id " + ("DESC" if newest_first else "ASC"),
        parameters,
    ).fetchall()
    groups: list[list[dict[str, Any]]] = []
    positions: dict[tuple[str, ...], int] = {}
    moment = now or datetime.now(timezone.utc)
    for row in rows:
        event = events.event(row["event_id"])
        grouped_key = buyback_group_key(event)
        if not _closed_hk_week(grouped_key, moment):
            continue
        key = grouped_key or ("event", event["id"])
        if key in positions:
            groups[positions[key]].append(event)
            continue
        if limit is not None and len(groups) >= max(1, int(limit)):
            continue
        positions[key] = len(groups)
        groups.append([event])
    return groups


def config_fingerprint(*paths: Path | None) -> str:
    """A short hash of the model configurations this run is using.

    Folded into every ``request_id`` because a cockpit WorkOrder is content
    addressed on (purpose, request_id, mission version, prompt) and *not* on
    the configuration that answered it. Without this, a run made under a
    misconfigured verifier poisons every event it touched: the configuration
    is fixed, the request is identical, and the scheduler replays the old
    result -- including its old route -- for ever. With it, fixing the
    configuration is a different request and the events become retryable.
    """

    material = []
    for path in paths:
        if path is None:
            material.append(None)
            continue
        try:
            material.append(Path(path).expanduser().read_text(encoding="utf-8"))
        except OSError:
            material.append(str(path))
    return content_hash({"configs": material})[:8]


def same_routing_policy(judge: Path | None, verifier: Path | None) -> str | None:
    """The cheap pre-check: two configurations that route the same way.

    Catches the misconfiguration that actually happens -- somebody copies the
    judge's file to the verifier's name -- before a single call is paid for.
    It is not the guarantee; the guarantee is the family check on the route
    the broker took. It is the difference between finding out for nothing and
    finding out for the price of a pair of calls.
    """

    if judge is None or verifier is None:
        return None
    try:
        left = json.loads(Path(judge).expanduser().read_text(encoding="utf-8"))
        right = json.loads(Path(verifier).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if left == right:
        return "the judge and verifier configurations are the same file"
    if left.get("routing_policy_ref") == right.get("routing_policy_ref"):
        return (
            "the judge and verifier configurations name the same routing policy "
            f"({left.get('routing_policy_ref')}), so both calls select the same model"
        )
    return None


def _model(config_path: Path | None, state_dir: Path, scheduler_db: Path | None) -> Any:
    if config_path is None:
        return None
    return EventCockpitModel(
        json.loads(Path(config_path).expanduser().read_text(encoding="utf-8")),
        scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
        max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
    )


def _call_budget(model: Any, purpose: str) -> dict[str, Any]:
    resolver = getattr(model, "budget_for", None)
    if callable(resolver):
        return resolver(purpose)
    return {
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD,
        "timeout_seconds": TIMEOUT_SECONDS,
    }


def _event_run_budget(config_path: Path | None, model: Any) -> dict[str, Any]:
    config = getattr(model, "config", None)
    if config is None and config_path is not None:
        config = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
    return resolve_run_budget(
        config or {}, PURPOSE,
        defaults={
            "max_events": MAX_EVENTS_PER_RUN,
            "max_events_per_company": MAX_EVENTS_PER_COMPANY,
        },
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
    event_refs: tuple[str, ...] | None = None,
    event_group_hash: str | None = None,
    max_events: int | None = None,
    per_company: int | None = None,
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
        "config_fingerprint": None,
        "reflections": 0,
        "reflections_refused": 0,
        "followups": [],
        "effects": [],
        "failed_model_traces": [],
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
        day = moment.date().isoformat()
        state = pool_state(judgements, mission, day=day)
        summary["pool"] = state

        run_budget = _event_run_budget(judge_model_config, judge_model)
        effective_max_events = (
            max_events if max_events is not None else int(run_budget["max_events"])
        )
        effective_per_company = (
            per_company
            if per_company is not None
            else int(run_budget["max_events_per_company"])
        )

        allowed_versions = tuple(
            row["mission_version_id"] for row in store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_versions "
                "WHERE mission_ref=?", (mission["mission_ref"],)
            ).fetchall())
        batch: list[list[dict[str, Any]]] = []
        if event_refs is not None:
            expected_refs = set(event_refs)
            current = unjudged_event_groups(
                events, judgements, company_ref=company_ref or "", limit=None,
                mission_version_refs=allowed_versions, newest_first=True, now=moment)
            matching = [group for group in current
                        if expected_refs.intersection(item["id"] for item in group)]
            target = matching[0] if len(matching) == 1 else []
            if target:
                key = buyback_group_key(target[0]) or ("event", target[0]["id"])
                judged_refs = {
                    row["event_ref"] for row in events.connection.execute(
                        "SELECT event_ref FROM event_judgements"
                    ).fetchall()
                }
                valid = (
                    len({item["id"] for item in target}) == len(target)
                    and {item["id"] for item in target} == expected_refs
                    and company_ref in tracked
                    and all(item["company_ref"] == company_ref for item in target)
                    and all(item.get("mission_version_ref") in allowed_versions
                            for item in target)
                    and all((buyback_group_key(item) or ("event", item["id"])) == key
                            for item in target)
                    and _closed_hk_week(buyback_group_key(target[0]), moment)
                    and incremental_group_hash(target) == event_group_hash
                    and all(item["id"] not in judged_refs for item in target)
                )
                if valid:
                    batch = [target]
        else:
            for ref in tracked:
                batch.extend(unjudged_event_groups(
                    events, judgements, company_ref=ref,
                    limit=effective_per_company,
                    mission_version_refs=allowed_versions, now=moment,
                ))
        batch = batch[:effective_max_events]
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
        # Before anything is paid for. The route-level family check is the
        # guarantee, but it costs a pair of calls to reach; this costs a file
        # read and catches the misconfiguration that actually happens.
        shared = same_routing_policy(judge_model_config, verifier_model_config)
        if shared is not None:
            summary.update({
                "status": "idle", "judgement_status": "gated:same_family",
                "failure_reason": shared,
            })
            return summary
        fingerprint = config_fingerprint(judge_model_config, verifier_model_config)
        summary["config_fingerprint"] = fingerprint
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
        # W4: the price authority, read once per run rather than per event.
        # The buyback context compares what a company paid for its own shares
        # with what they cost now, and "what they cost now" is one row per
        # company however many events are in the batch. A Core with no price
        # series gets an empty map and the context block says so.
        prices, market_caps = _price_reads(store, tracked)

        spent = 0
        # Four calls, not two: a divergence or a revise-shaped decision owes a
        # reflection and its verification as well. Reserving the pair only
        # would admit an event whose reflection then has nothing left to spend,
        # which is the shape where a decision is recorded without the account
        # of what we may have missed -- exactly the half the owner asked for.
        reservation = int(sum(
            Decimal(str(_call_budget(model, purpose)["max_cost_usd"]))
            for model, purpose in (
                (judge_model, PURPOSE),
                (verifier_model, VERIFIER_PURPOSE),
                (judge_model, REFLECTION_PURPOSE),
                (verifier_model, REFLECTION_VERIFIER_PURPOSE),
            )
        ) * 1_000_000)
        for event_group in batch:
            event = event_group[0]
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
                price=prices.get(event["company_ref"]),
                market_cap=market_caps.get(event["company_ref"]),
            )
            evidence_group = group_evidence_events(events, event_group)
            context["grouped_events"] = evidence_group
            request_id = f"{fingerprint}{incremental_group_hash(evidence_group)[:24]}"
            decided = judge(
                context, model=judge_model, mission=mission, request_id=request_id
            )
            checked = verify(
                context, decided, model=verifier_model, mission=mission,
                request_id=f"v{request_id}"[:31], family_resolver=family_resolver,
            )
            for outcome in (decided, checked):
                if outcome.get("failure_trace") is not None:
                    summary["failed_model_traces"].append(outcome["failure_trace"])
            spent += judgements.record_spend(
                call=decided.get("model"), purpose="event_judgement",
                outcome=decided["status"], event_ref=event["id"], mission=mission, day=day,
            )
            spent += judgements.record_spend(
                call=checked.get("model"), purpose="event_judgement",
                outcome=f"verify:{checked['status']}", event_ref=event["id"],
                mission=mission, day=day,
            )
            if decided["status"] != "judged" or checked.get("verdict") != "pass":
                summary["refused"] += 1
                summary["effects"].append({
                    "event_ref": event["id"], "status": "refused",
                    "reason": decided.get("reason") or checked.get("reason")
                    or "the verifier rejected the decision",
                    "findings": checked.get("findings") or [],
                })
                # A verifier that shares the producer's family will do so for
                # every event in the batch. Stopping here costs one pair
                # instead of eight, and the run says which half is wrong
                # rather than reporting eight refusals nobody can act on.
                if "model_family_not_independent" in str(checked.get("reason") or ""):
                    summary["judgement_status"] = "gated:same_family"
                    break
                # C2: the same reasoning for a spent pool. This lane keeps its
                # own book of what it spent and gates on it above; the day
                # ledger's event_response pool is the same money seen from the
                # other side, and it can refuse first -- a hand-run or a
                # replayed call moves one book and not the other. Whichever
                # gate says stop, the word is the same one.
                if POOL_EXHAUSTED_STATUS in {
                    decided.get("lane_status"), checked.get("lane_status")
                }:
                    summary["judgement_status"] = POOL_EXHAUSTED_STATUS
                    break
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
            for grouped_event in event_group[1:]:
                alias_judgement = dict(decided)
                alias_judgement["model"] = {
                    **(decided.get("model") or {}), "cost_micros": 0,
                    "grouped_under_event_ref": event["id"],
                }
                alias_verification = dict(checked)
                alias_verification["model"] = {
                    **(checked.get("model") or {}), "cost_micros": 0,
                    "grouped_under_event_ref": event["id"],
                }
                judgements.record(
                    event=grouped_event, judgement=alias_judgement,
                    verification=alias_verification,
                    effect={"kind": "grouped_judgement", "status": "recorded",
                            "primary_event_ref": event["id"]},
                    mission=mission, actor_ref=actor,
                )
            # The owner's third instruction: when we changed our mind, or when
            # the price kept running against us, write down what we expected
            # and what we may have missed -- including when the decision was to
            # hold. The reflection changes nothing; it is attached to the
            # candidate so the person deciding sees both.
            reflection_ref = None
            if reflection_is_owed(event, decided):
                thought = reflect(context, decided, model=judge_model,
                                  mission=mission, request_id=f"f{request_id}"[:31])
                reviewed = verify_reflection(
                    context, thought, model=verifier_model, mission=mission,
                    request_id=f"fv{request_id}"[:30], family_resolver=family_resolver,
                )
                for outcome in (thought, reviewed):
                    if outcome.get("failure_trace") is not None:
                        summary["failed_model_traces"].append(outcome["failure_trace"])
                spent += judgements.record_spend(
                    call=thought.get("model"), purpose="thesis_reflection",
                    outcome=thought["status"], event_ref=event["id"], mission=mission,
                    day=day,
                )
                spent += judgements.record_spend(
                    call=reviewed.get("model"), purpose="thesis_reflection",
                    outcome=f"verify:{reviewed['status']}", event_ref=event["id"],
                    mission=mission, day=day,
                )
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
        if summary["judged"] == 0 and summary["refused"]:
            reasons = [str(item.get("reason") or "judgement contract refused")
                       for item in summary["effects"]
                       if item.get("status") == "refused"][:3]
            summary["status"] = "failed"
            summary["judgement_status"] = summary["judgement_status"] or "refused"
            summary["failure_reason"] = (
                "all attempted event judgements were refused: " + "; ".join(reasons)
            )[:500]
        else:
            summary["status"] = "succeeded"
            summary["judgement_status"] = summary["judgement_status"] or (
                "partial" if summary["refused"] else "judged"
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


def _price_reads(
    store: DaltonStore, companies: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The latest close and market capitalisation per company, or nothing.

    Read by name and defensively: the price lane is a different slice and a
    Core that has not run it has no such table. A judgement that failed because
    the price authority was absent would be the market data deciding whether
    the brain gets to think.
    """

    try:
        from .market_price import MarketPriceSeriesAuthority
    except ImportError:  # pragma: no cover - the module ships with the package
        return {}, {}
    closes: dict[str, Any] = {}
    caps: dict[str, Any] = {}
    try:
        prices = MarketPriceSeriesAuthority(store)
    except Exception:  # noqa: BLE001 - no price schema on this Core
        return {}, {}
    for ref in companies:
        try:
            close = prices.latest_close(ref)
            cap = prices.latest_observation(ref, "market_cap")
        except Exception:  # noqa: BLE001 - one company, not the run
            continue
        if close is not None:
            closes[ref] = close
        if cap is not None:
            caps[ref] = cap
    return closes, caps


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
    parser.add_argument("--event-ref", action="append", dest="event_refs")
    parser.add_argument("--event-group-hash")
    parser.add_argument("--max-events", type=int)
    parser.add_argument("--per-company", type=int)
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
        event_refs=None if args.event_refs is None else tuple(args.event_refs),
        event_group_hash=args.event_group_hash,
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
    "config_fingerprint",
    "same_routing_policy",
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
