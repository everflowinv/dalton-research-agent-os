"""P14f: the child that writes one earnings-season window per occurrence.

Everything expensive happens here, so everything careful happens here too.

The run is bounded three ways and any of them can end it: the number of
occurrences per run, the ``event_response`` share of the mission's day, and
the fact that an occurrence already written about is skipped by the ledger
rather than by this loop remembering.  The deliverable's idempotency key is
the occurrence, not the event, so a second calendar event for the same report
-- the one C1 emits when the company confirms a date it had estimated -- costs
a query and not a call.

The verifier runs on a second model configuration.  If it is absent, or its
family cannot be resolved, or it resolves to the producer's family, the window
is recorded as refused and nothing is published: a calibration proposes a
thesis revision to a person, and an unverified proposal is not one this system
puts in front of them.

Order inside a calibration is not an accident.  The deterministic work first --
the model's history catches up with the filing, the reconciliation rows are
read, the guidance is paired -- then one call, then the effects, and the
effects in the order the ledger's foreign keys require: judgement, reflection,
candidates, forecast proposals.  The forward periods are not revised at any
point; the event left behind says so, and the judgement lane decides.
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
from .earnings_calibration import (
    actualize_for_report,
    apply_calibration_effects,
    build_calibration_context,
    draft_calibration,
    emit_calibration_event,
    input_table_for,
    publish_calibration,
    reconciliation_rows,
    tier_counts,
    verify_calibration,
)
from .earnings_preview import (
    build_preview_context,
    draft_preview,
    publish_preview,
    verify_preview,
)
from .earnings_season import (
    CALIBRATION_KIND,
    PREVIEW_KIND,
    EarningsSeasonError,
    reported_period,
)
from .event_judgement import (
    EventJudgementAuthority,
    company_theses,
    market_view_rows,
    pool_state,
    recent_claims,
    route_family_resolver,
)
from .mission_deliverable import MissionDeliverableAuthority
from .mission_earnings_season_lane import due_occurrences
from .model_configurations import register_model_config_name
from .research_event import ResearchEventAuthority, record_event
from .store import DaltonStore, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
WRITER_MODEL_CONFIG = register_model_config_name("earnings-season-model-config.json")
VERIFIER_MODEL_CONFIG = register_model_config_name(
    "earnings-season-verifier-model-config.json"
)

# A window's prompt is a handful of tables: our cells for one quarter, the
# guidance events, the reconciliation rows, the theses and a dozen Claims.  It
# has never needed a large window and giving it one would let a thin context
# grow into a long answer.
MAX_INPUT_TOKENS = 60_000
MAX_OUTPUT_TOKENS = 2_000
MAX_COST_USD = 0.12
TIMEOUT_SECONDS = 240
MAX_OCCURRENCES_PER_RUN = 3

# What the two windows need granted before anything is paid for.
REQUIRED_SCOPES: tuple[str, ...] = ("deliverable",)
OPTIONAL_SCOPES: tuple[str, ...] = (
    # The calibration's actualisation publishes a ForecastModelVersion.
    "forecast_line",
    # The event it leaves for the judgement lane.
    "market_event",
    # ADR-0007's proposal.
    "thesis_revision_candidate",
)


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
    """What is not granted, reported rather than assumed.

    Only ``deliverable`` is required: a season with no document to publish is
    not worth paying for.  Without ``forecast_line`` the calibration does not
    actualise and says so; without ``market_event`` the judgement lane is not
    told the quarter settled; without ``thesis_revision_candidate`` the
    proposal is queued with ADR-0007 named.  Each of those is a thinner run,
    not a broken one.
    """

    granted = set(mission["autonomy"]["may_write"])
    return [word for word in REQUIRED_SCOPES + OPTIONAL_SCOPES if word not in granted]


def config_fingerprint(*paths: Path | None) -> str:
    """A short hash of the configurations this run used.

    Folded into every ``request_id`` for P14a's reason: a cockpit WorkOrder is
    content addressed on the request and not on the configuration that
    answered it, so without this a run made under a misconfigured verifier
    would replay its old route for ever and the occurrence could never be
    retried.
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


def same_routing_policy(writer: Path | None, verifier: Path | None) -> str | None:
    """The cheap pre-check: two configurations that route the same way."""

    if writer is None or verifier is None:
        return None
    try:
        left = json.loads(Path(writer).expanduser().read_text(encoding="utf-8"))
        right = json.loads(Path(verifier).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if left == right:
        return "the writer and verifier configurations are the same file"
    if left.get("routing_policy_ref") == right.get("routing_policy_ref"):
        return (
            "the writer and verifier configurations name the same routing policy "
            f"({left.get('routing_policy_ref')}), so both calls select the same model"
        )
    return None


def _model(config_path: Path | None, state_dir: Path, scheduler_db: Path | None) -> Any:
    if config_path is None:
        return None
    return CockpitModel(
        json.loads(Path(config_path).expanduser().read_text(encoding="utf-8")),
        scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
        max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
    )


def _playbook(store: DaltonStore, mission: Any) -> dict[str, Any] | None:
    from .research_playbook import read_exact_playbook_version

    try:
        return read_exact_playbook_version(
            store.connection, mission["bindings"]["playbook_version"]["ref"]
        )
    except Exception:  # noqa: BLE001 - no playbook means no document, not a crash
        return None


def guidance_profile_for(store: DaltonStore, company_ref: str) -> dict[str, Any] | None:
    """P12f's computed table for one company, by name, or nothing."""

    try:
        from .company_dossier_cli import guidance_material
        from .guidance_profile import build_profile
    except ImportError:  # pragma: no cover - both are on main
        return None
    try:
        guides, actuals = guidance_material(store, company_ref)
        return build_profile(company_ref=company_ref, guides=guides, actuals=actuals)
    except Exception:  # noqa: BLE001 - an unreadable profile is no profile
        return None


def open_debates_for(store: DaltonStore, company_ref: str) -> list[dict[str, Any]]:
    """P12c's open debates for one company, by name, or an empty list."""

    try:
        from .debate_map import DebateMapAuthority
    except ImportError:  # pragma: no cover
        return []
    try:
        return list(DebateMapAuthority(store).open_debates(company_ref) or ())
    except Exception:  # noqa: BLE001
        return []


def consensus_reader_for(store: DaltonStore) -> Any:
    """P11b's ``latest_consensus``, by name, or nothing.

    Called by name because that authority is being built beside this one.  When
    it is absent every consensus block says ``available: false`` and why, which
    is the honest answer and not a degraded one.
    """

    try:
        from .consensus_estimate import latest_consensus  # type: ignore[attr-defined]
    except ImportError:
        return None

    def read(company_ref: str) -> Any:
        return latest_consensus(store, company_ref)

    return read


def reconcile_for(store: DaltonStore, mission: Any, company_ref: str) -> Any:
    """Reconcile whatever is pending for this company and hand back the authority.

    Reconciling first because a calibration written the day of the print is
    exactly when the pairing becomes possible: the actual Claim has just
    landed.  A failure answers ``None`` on purpose -- a calibration with no
    reconciliation row still has guidance and Claims, and says in its gaps
    that it has no rows.
    """

    try:
        from .forecast_reconciliation import ForecastReconciliationAuthority
    except ImportError:  # pragma: no cover
        return None
    try:
        authority = ForecastReconciliationAuthority(store)
    except Exception:  # noqa: BLE001
        return None
    actor = mission["autonomy"]["automation_principal"]

    def resolver(_company_ref: str) -> dict[str, Any]:
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission.get("mission_ref") or mission["id"],
            "actor_ref": actor,
        }

    try:
        authority.reconcile_pending(
            requested_by=actor, mission_resolver=resolver, company_ref=company_ref,
        )
    except Exception:  # noqa: BLE001 - an ungranted or unbuildable pairing is a gap
        pass
    return authority


def run_earnings_season(
    *,
    state_dir: Path,
    summary_dir: Path,
    model_config: Path | None = None,
    verifier_model_config: Path | None = None,
    policy_path: Path | None = None,
    scheduler_db: Path | None = None,
    company_ref: str | None = None,
    max_occurrences: int = MAX_OCCURRENCES_PER_RUN,
    dry_run: bool = False,
    writer_model: Any = None,
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
        "mode": "dry_run" if dry_run else "write",
        "season_status": None,
        "ungranted": [],
        "pool": None,
        "candidates_seen": 0,
        "previews": 0,
        "calibrations": 0,
        "refused": 0,
        "decisions": {},
        "candidates": 0,
        "forecast_proposals": 0,
        "reflections": 0,
        "events": 0,
        "config_fingerprint": None,
        "windows": [],
        "failure_reason": None,
        "cost_micros": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "season_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        granted = set(mission["autonomy"]["may_write"])
        summary["ungranted"] = missing_write_scopes(mission)
        if "deliverable" not in granted:
            summary.update({
                "status": "idle", "season_status": "ungranted",
                "failure_reason": "the mission does not grant deliverable writes; a "
                                  "preview nobody can publish is not worth paying for",
            })
            return summary
        events = ResearchEventAuthority(store)
        judgements = EventJudgementAuthority(store)
        deliverables = MissionDeliverableAuthority(store)
        due = due_occurrences(store, missions, mission, now=moment)
        if company_ref is not None:
            due = [row for row in due if row["company_ref"] == company_ref]
        due.sort(key=lambda row: (row["window"] != "calibration", row["expected_date"]))
        due = due[:max(1, int(max_occurrences))]
        summary["candidates_seen"] = len(due)
        if not due:
            summary.update({"status": "idle", "season_status": "nothing_due"})
            return summary
        day = moment.date().isoformat()
        state = pool_state(judgements, mission, day=day)
        summary["pool"] = state
        if dry_run:
            summary.update({
                "status": "succeeded", "season_status": "dry_run",
                "windows": [
                    {"company_ref": row["company_ref"], "window": row["window"],
                     "expected_date": row["expected_date"],
                     "date_confidence": row["date_confidence"],
                     "occurrence_ref": row["occurrence_ref"]}
                    for row in due
                ],
            })
            return summary

        writer_model = writer_model or _model(model_config, state_dir, scheduler_db)
        verifier_model = verifier_model or _model(
            verifier_model_config, state_dir, scheduler_db
        )
        if writer_model is None or verifier_model is None:
            summary.update({
                "status": "idle", "season_status": "gated",
                "failure_reason": "this lane needs a writer and an independent "
                                  "verifier configuration; it will not run on one",
            })
            return summary
        shared = same_routing_policy(model_config, verifier_model_config)
        if shared is not None:
            summary.update({
                "status": "idle", "season_status": "gated:same_family",
                "failure_reason": shared,
            })
            return summary
        fingerprint = config_fingerprint(model_config, verifier_model_config)
        summary["config_fingerprint"] = fingerprint
        if family_resolver is None:
            config = json.loads(
                Path(model_config).expanduser().read_text(encoding="utf-8")
            ) if model_config else {}
            family_resolver = route_family_resolver(config.get("model_router_db"))
        playbook = _playbook(store, mission)
        if playbook is None:
            summary.update({
                "status": "idle", "season_status": "no_playbook",
                "failure_reason": "the mission binds no playbook version, so no "
                                  "deliverable can be published",
            })
            return summary
        source_keys = _source_keys(policy_path)
        consensus_reader = consensus_reader_for(store)
        actor = mission["autonomy"]["automation_principal"]

        spent = 0
        reservation = int(MAX_COST_USD * 2 * 1_000_000)
        for occurrence in due:
            if state["remaining_micros"] - spent < reservation:
                summary["season_status"] = "skipped:pool_exhausted"
                break
            outcome = _one_window(
                store=store, missions=missions, mission=mission, playbook=playbook,
                events=events, judgements=judgements, deliverables=deliverables,
                occurrence=occurrence, writer_model=writer_model,
                verifier_model=verifier_model, family_resolver=family_resolver,
                fingerprint=fingerprint, granted=granted, actor=actor,
                consensus_reader=consensus_reader, source_keys=source_keys,
                moment=moment, day=day,
            )
            spent += int(outcome.pop("cost_micros", 0))
            summary["windows"].append(outcome)
            if outcome["status"] == "published":
                key = "previews" if occurrence["window"] == "preview" else "calibrations"
                summary[key] += 1
                decision = outcome.get("decision")
                if decision:
                    summary["decisions"][decision] = summary["decisions"].get(decision, 0) + 1
                summary["candidates"] += len(outcome.get("candidates") or ())
                summary["forecast_proposals"] += len(
                    outcome.get("forecast_proposals") or ()
                )
                summary["reflections"] += 1 if outcome.get("reflection_ref") else 0
                summary["events"] += 1 if outcome.get("event_ref") else 0
            else:
                summary["refused"] += 1
        summary["cost_micros"] = spent
        summary["status"] = "succeeded"
        summary.setdefault("season_status", None)
        if summary["season_status"] is None:
            summary["season_status"] = (
                "written" if summary["previews"] or summary["calibrations"]
                else "refused"
            )
        return summary
    except Exception as exc:  # noqa: BLE001 - a child reports rather than crashes
        summary.update({"status": "failed",
                        "failure_reason": f"{type(exc).__name__}: {exc}"})
        return summary
    finally:
        store.close()
        _write_owner_only(summary_dir / "summary.json", summary)


def _source_keys(policy_path: Path | None) -> list[str]:
    if policy_path is None:
        return []
    try:
        from .tracking_cadence import load_policy

        policy = load_policy(policy_path)
    except Exception:  # noqa: BLE001 - no policy is no adjustable source
        return []
    return sorted(policy["cadences"])


def _one_window(
    *,
    store: DaltonStore,
    missions: Any,
    mission: Any,
    playbook: Any,
    events: ResearchEventAuthority,
    judgements: EventJudgementAuthority,
    deliverables: MissionDeliverableAuthority,
    occurrence: dict[str, Any],
    writer_model: Any,
    verifier_model: Any,
    family_resolver: Any,
    fingerprint: str,
    granted: set,
    actor: str,
    consensus_reader: Any,
    source_keys: list[str],
    moment: datetime,
    day: str,
) -> dict[str, Any]:
    """One occurrence, one window, at most two calls."""

    from .model_forecast_driver import ForecastModelAuthority

    company_ref = occurrence["company_ref"]
    window = occurrence["window"]
    result: dict[str, Any] = {
        "company_ref": company_ref, "window": window,
        "occurrence_ref": occurrence["occurrence_ref"],
        "expected_date": occurrence["expected_date"],
        "date_confidence": occurrence["date_confidence"],
        "status": "refused", "reason": None, "cost_micros": 0,
    }
    forecast_models = ForecastModelAuthority(store)
    try:
        model_version = forecast_models.latest(company_ref)
    except Exception:  # noqa: BLE001
        model_version = None
    theses = company_theses(store.connection, company_ref)
    claims = recent_claims(store.connection, company_ref)
    request_id = f"{fingerprint}{occurrence['occurrence_ref'].split(':', 1)[-1]}"[:32]

    if window == "preview":
        context = build_preview_context(
            occurrence=occurrence, mission=mission, model_version=model_version,
            guidance_profile=guidance_profile_for(store, company_ref),
            theses=theses, debates=open_debates_for(store, company_ref),
            claims=claims, consensus_reader=consensus_reader,
        )
        drafted = draft_preview(
            context, model=writer_model, mission=mission, request_id=request_id
        )
        checked = verify_preview(
            context, drafted, model=verifier_model, mission=mission,
            request_id=f"v{request_id}"[:31], family_resolver=family_resolver,
        )
        cost = _book(judgements, drafted, checked, occurrence, mission, day, PREVIEW_KIND)
        result["cost_micros"] = cost
        if drafted["status"] != "drafted" or checked.get("verdict") != "pass":
            result["reason"] = drafted.get("reason") or checked.get("reason") or (
                "the verifier rejected the preview"
            )
            result["findings"] = checked.get("findings")
            return result
        published = publish_preview(
            deliverables, context=context, draft=drafted, mission=mission,
            playbook=playbook, actor_ref=actor,
        )
        result.update({"status": "published", "deliverable_ref": published["id"],
                       "publish_status": published.get("status"),
                       "gaps": context["gaps"]})
        return result

    # -- the calibration ----------------------------------------------------
    actualisation = {"status": "skipped",
                     "reason": "the mission does not grant forecast_line writes"}
    if "forecast_line" in granted:
        actualisation = actualize_for_report(
            forecast_models, company_ref=company_ref,
            input_table=input_table_for(missions, company_ref), actor_ref=actor,
        )
        if actualisation.get("version_ref"):
            try:
                model_version = forecast_models.model(actualisation["version_ref"])
            except Exception:  # noqa: BLE001
                pass
    reconciliations = reconcile_for(store, mission, company_ref)
    period = reported_period(model_version, occurrence["expected_date"])
    rows = reconciliation_rows(
        reconciliations, company_ref=company_ref,
        period_end=None if period is None else str(period.get("end")),
    ) if reconciliations else []
    try:
        context = build_calibration_context(
            occurrence=occurrence, mission=mission, model_version=model_version,
            actualisation=actualisation, reconciliations=rows,
            guidance_profile=guidance_profile_for(store, company_ref),
            theses=theses, claims=claims,
            market_view=market_view_rows(
                events.events(company_ref=company_ref, limit=40)
            ),
            source_keys=source_keys, consensus_reader=consensus_reader, now=moment,
        )
    except EarningsSeasonError as exc:
        result["reason"] = str(exc)
        return result
    drafted = draft_calibration(
        context, model=writer_model, mission=mission, request_id=request_id
    )
    checked = verify_calibration(
        context, drafted, model=verifier_model, mission=mission,
        request_id=f"v{request_id}"[:31], family_resolver=family_resolver,
    )
    result["cost_micros"] = _book(
        judgements, drafted, checked, occurrence, mission, day, CALIBRATION_KIND
    )
    result["tiers"] = tier_counts(rows)["counts"]
    result["actualisation"] = actualisation.get("status")
    if drafted["status"] != "drafted" or checked.get("verdict") != "pass":
        result["reason"] = drafted.get("reason") or checked.get("reason") or (
            "the verifier rejected the calibration"
        )
        result["findings"] = checked.get("findings")
        return result
    published = publish_calibration(
        deliverables, context=context, output=drafted, mission=mission,
        playbook=playbook, actor_ref=actor,
    )
    result.update({"status": "published", "deliverable_ref": published["id"],
                   "publish_status": published.get("status"),
                   "gaps": context["gaps"]})
    # The event first: the judgement records hang off it, and it is also the
    # thing that tells the judgement lane the forward view is unreviewed.
    if "market_event" not in granted:
        result["event_ref"] = None
        result["effects"] = {"status": "queued",
                             "reason": "the mission does not grant market_event "
                                       "writes, so the judgement lane is not told "
                                       "this quarter settled"}
        return result
    event = emit_calibration_event(
        context=context, output=drafted,
        record_event=lambda **kwargs: record_event(events, **kwargs),
        mission=mission, actor_ref=actor,
    )
    result["event_ref"] = event["id"]
    if "thesis_revision_candidate" not in granted and any(
        row["action"] == "revise_thesis" for row in drafted["theses"]
    ):
        result["effects"] = {
            "status": "queued",
            "reason": "the mission does not grant thesis_revision_candidate writes; "
                      "ADR-0007 says automation proposes and a person decides, and "
                      "this Core has not been given the word for the proposal",
        }
        return result
    effects = apply_calibration_effects(
        judgements, event=event, context=context, output=drafted,
        verification=checked, mission=mission, actor_ref=actor,
    )
    result["effects"] = {"status": effects["status"]}
    result["decision"] = effects.get("decision")
    result["candidates"] = effects.get("candidates") or []
    result["forecast_proposals"] = effects.get("forecast_proposals") or []
    result["reflection_ref"] = effects.get("reflection_ref")
    result["judgement_ref"] = effects.get("judgement_ref")
    return result


def _book(
    judgements: EventJudgementAuthority, drafted: Any, checked: Any,
    occurrence: Any, mission: Any, day: str, purpose: str,
) -> int:
    """Both calls into the pool's one book, whatever they returned.

    A refused draft still cost what it cost, and the day the cap matters is
    the day a model is misbehaving -- which is the day the refusals are.
    """

    spent = judgements.record_spend(
        call=drafted.get("model"), purpose=purpose, outcome=drafted["status"],
        event_ref=occurrence["event_ref"], mission=mission, day=day,
    )
    spent += judgements.record_spend(
        call=checked.get("model"), purpose=purpose,
        outcome=f"verify:{checked.get('status')}",
        event_ref=occurrence["event_ref"], mission=mission, day=day,
    )
    return spent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path)
    parser.add_argument("--tracking-policy", type=Path)
    parser.add_argument("--scheduler", type=Path)
    parser.add_argument("--company-ref")
    parser.add_argument("--max-occurrences", type=int, default=MAX_OCCURRENCES_PER_RUN)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what is due and stop; no calls")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_earnings_season(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        model_config=args.model_config,
        verifier_model_config=args.verifier_model_config,
        policy_path=args.tracking_policy,
        scheduler_db=args.scheduler,
        company_ref=args.company_ref,
        max_occurrences=args.max_occurrences,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_COST_USD",
    "MAX_OCCURRENCES_PER_RUN",
    "OPTIONAL_SCOPES",
    "REQUIRED_SCOPES",
    "VERIFIER_MODEL_CONFIG",
    "WRITER_MODEL_CONFIG",
    "build_parser",
    "config_fingerprint",
    "consensus_reader_for",
    "guidance_profile_for",
    "main",
    "missing_write_scopes",
    "open_debates_for",
    "reconcile_for",
    "run_earnings_season",
    "same_routing_policy",
]
