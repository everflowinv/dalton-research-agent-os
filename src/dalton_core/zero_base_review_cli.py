"""W4: the child that runs the zero-base review, and the recheck that costs nothing.

Two modes, because two things are owed on different clocks.

``checks`` re-derives the judgement outcome ledger -- was the ``no_change``
right, did the ``revise`` go the way a later actual says -- from rows that are
already in the Core.  There is no model in it, so it may run as often as the
tick likes; the ``inputs_hash`` rule means a pass over unmoved rows writes
nothing.

``review`` does that and then pays for a producer and independent verifier per company whose
zero-base review is owed: at most :data:`MAX_REVIEWS_PER_RUN`, so a month
boundary that makes five companies due at once cannot spend the coverage pool
in a single tick.

Everything the review implies leaves as a proposal.  A rewritten thesis line
becomes a ``ThesisRevisionCandidate`` for the existing human decision loop --
and only when the mission grants both the scope and its matching checkpoint.
Where it does not, the review is still written and says so: the reading is
worth having even on a Core where nobody has granted the proposal yet.
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
from .judgement_outcome import (
    JudgementOutcomeAuthority,
    build_outcome_checks,
    checks_digest,
)
from .market_price import MarketPriceSeriesAuthority
from .model_configurations import register_model_config_name
from .store import DaltonStore
from .tracking_cadence import load_policy, screen_passed_companies
from .zero_base_review import (
    CANDIDATE_SCOPE,
    CHECKPOINT_KIND,
    WRITE_SCOPE,
    ZeroBaseReviewAuthority,
    ZeroBaseReviewError,
    build_context,
    build_review_body,
    due_reviews,
    review,
    verify_review,
)

SUMMARY_SCHEMA_VERSION = "0.1"
REVIEW_MODEL_CONFIG = register_model_config_name("zero-base-review-model-config.json")
VERIFIER_MODEL_CONFIG = register_model_config_name("zero-base-review-verifier-model-config.json")

# One company's prompt is a table: a handful of theses, a dozen debates, a
# dozen events. It has never needed a large window.
MAX_INPUT_TOKENS = 60_000
MAX_OUTPUT_TOKENS = 1_800
MAX_COST_USD = 0.12
TIMEOUT_SECONDS = 180
#: A month boundary makes every covered company due on the same tick. Two per
#: run means the backlog drains over the following few ticks instead of the
#: coverage pool draining in one.
MAX_REVIEWS_PER_RUN = 2

MODES: tuple[str, ...] = ("review", "checks")


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


def _model(config_path: Path | None, state_dir: Path, scheduler_db: Path | None) -> Any:
    if config_path is None:
        return None
    return CockpitModel(
        json.loads(Path(config_path).expanduser().read_text(encoding="utf-8")),
        scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
        max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
    )


def outcome_inputs(
    store: DaltonStore, *, mission: Any, tracked: list[str], policy: Any
) -> dict[str, Any]:
    """The three read-only tables the outcome formula needs.

    The universe rather than the tracked list for the price series: the basket
    is "the other covered companies", and a company that has not passed its
    screen is still a peer whose price says what the sector did.
    """

    from .tracking_lane_cli import thesis_stances

    prices = MarketPriceSeriesAuthority(store)
    universe = [member["company_ref"] for member in mission["universe"]]
    return {
        "series_by_company": {ref: prices.series(ref) for ref in universe},
        "thresholds": policy["abnormal_move"],
        "stances": thesis_stances(store, policy, tracked=universe),
        "company_refs": tracked,
    }


def run_checks(
    store: DaltonStore, *, mission: Any, tracked: list[str], policy: Any, actor_ref: str,
) -> dict[str, Any]:
    """Re-derive the outcome ledger.  No model, no clock, no opinions."""

    inputs = outcome_inputs(store, mission=mission, tracked=tracked, policy=policy)
    checks = build_outcome_checks(
        store.connection,
        company_refs=inputs["company_refs"],
        series_by_company=inputs["series_by_company"],
        thresholds=inputs["thresholds"],
        stances=inputs["stances"],
    )
    outcomes = JudgementOutcomeAuthority(store)
    written = outcomes.record_all(checks, actor_ref=actor_ref)
    return {**written, "digest": checks_digest(checks)}


def run_zero_base(
    *,
    state_dir: Path,
    summary_dir: Path,
    mode: str = "review",
    model_config: Path | None = None,
    verifier_model_config: Path | None = None,
    policy_path: Path | None = None,
    scheduler_db: Path | None = None,
    company_ref: str | None = None,
    max_reviews: int = MAX_REVIEWS_PER_RUN,
    model: Any = None,
    verifier_model: Any = None,
    family_resolver: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ZeroBaseReviewError(f"mode is one of {list(MODES)}")
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    moment = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": moment.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": mode,
        "review_status": None,
        "ungranted": [],
        "due": 0,
        "reviewed": 0,
        "refused": 0,
        "duplicate": 0,
        "candidates": 0,
        "candidates_ungranted": 0,
        "checks": None,
        "reviews": [],
        "failure_reason": None,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "review_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        granted = set(mission["autonomy"]["may_write"])
        checkpoints = set(mission["autonomy"]["human_checkpoints"])
        summary["ungranted"] = [
            word for word in (WRITE_SCOPE, CANDIDATE_SCOPE) if word not in granted
        ]
        actor = mission["autonomy"]["automation_principal"]
        policy = load_policy(policy_path)
        tracked = screen_passed_companies(missions, mission)
        if company_ref is not None:
            tracked = [ref for ref in tracked if ref == company_ref]

        # The derived ledger first, and in both modes: it costs nothing, it is
        # what the review's prompt reads, and a review written against last
        # month's outcome counts would be arguing from stale evidence.
        summary["checks"] = run_checks(
            store, mission=mission, tracked=tracked, policy=policy, actor_ref=actor,
        )
        if mode == "checks":
            summary.update({"status": "succeeded", "review_status": "checks_only"})
            return summary
        if WRITE_SCOPE in summary["ungranted"]:
            summary.update({
                "status": "idle", "review_status": "ungranted",
                "failure_reason": f"the mission does not grant {WRITE_SCOPE}; a review "
                                  "that can be published nowhere is not worth paying for",
            })
            return summary

        reviews = ZeroBaseReviewAuthority(store)
        outcomes = JudgementOutcomeAuthority(store)
        pending = due_reviews(
            store.connection, mission_ref=mission["mission_ref"],
            company_refs=tracked, now=moment,
        )
        summary["due"] = len(pending)
        if not pending:
            summary.update({"status": "succeeded", "review_status": "nothing_due"})
            return summary
        model = model or _model(model_config, state_dir, scheduler_db)
        verifier_model = verifier_model or _model(verifier_model_config, state_dir, scheduler_db)
        if model is None or verifier_model is None:
            summary.update({
                "status": "idle", "review_status": "gated",
                "failure_reason": "the zero-base review lane needs producer and verifier model configurations",
            })
            return summary
        may_propose = (
            CANDIDATE_SCOPE in granted and CHECKPOINT_KIND in checkpoints
        )
        if family_resolver is None:
            from .event_judgement import route_family_resolver
            config = (json.loads(Path(model_config).expanduser().read_text(encoding="utf-8"))
                      if model_config is not None else {})
            family_resolver = route_family_resolver(config.get("model_router_db"))
        for item in pending[:max(1, int(max_reviews))]:
            outcome = _review_one(
                store, reviews=reviews, outcomes=outcomes, mission=mission,
                item=item, model=model, verifier_model=verifier_model,
                family_resolver=family_resolver, moment=moment, actor_ref=actor,
                may_propose=may_propose,
            )
            summary["reviews"].append(outcome)
            if outcome["status"] == "refused":
                summary["refused"] += 1
                continue
            if outcome["status"] == "duplicate":
                summary["duplicate"] += 1
            else:
                summary["reviewed"] += 1
            summary["candidates"] += outcome.get("candidates", 0)
            summary["candidates_ungranted"] += outcome.get("candidates_ungranted", 0)
        summary["status"] = "succeeded"
        summary["review_status"] = "reviewed" if summary["reviewed"] else "refused"
        return summary
    except Exception as exc:  # noqa: BLE001 - the ticket reports, it does not crash
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    finally:
        store.close()
        _write_owner_only(summary_dir / "summary.json", summary)


def _review_one(
    store: DaltonStore,
    *,
    reviews: ZeroBaseReviewAuthority,
    outcomes: JudgementOutcomeAuthority,
    mission: Any,
    item: dict[str, Any],
    model: Any,
    verifier_model: Any,
    family_resolver: Any,
    moment: datetime,
    actor_ref: str,
    may_propose: bool,
) -> dict[str, Any]:
    company_ref = item["company_ref"]
    prior = reviews.for_company(mission["mission_ref"], company_ref)
    context = build_context(
        store.connection, mission=mission, company_ref=company_ref,
        trigger=item["trigger"], period_label=item["period_label"], now=moment,
        calibration=item.get("calibration"), prior_review=prior,
        outcome_counts=outcomes.counts(company_ref),
    )
    request_id = f"zbr{context['inputs_hash']}"[:32]
    answered = review(context, model=model, mission=mission, request_id=request_id)
    if answered["status"] != "reviewed":
        return {"company_ref": company_ref, "status": "refused",
                "trigger": item["trigger"], "period_label": item["period_label"],
                "reason": answered.get("reason"),
                "lane_status": answered.get("lane_status")}
    verified = verify_review(
        context, answered, model=verifier_model, mission=mission,
        request_id=f"{request_id}-verify", family_resolver=family_resolver,
    )
    if verified["status"] != "verified":
        return {"company_ref": company_ref, "status": "refused",
                "trigger": item["trigger"], "period_label": item["period_label"],
                "reason": verified.get("reason"), "lane_status": verified.get("lane_status"),
                "verification": verified}
    body = build_review_body(
        context, {**answered, "model": answered.get("model")},
        calibration_ref=(item.get("calibration") or {}).get("version_ref"),
    )
    body["verification"] = verified
    written = reviews.record(body, actor_ref=actor_ref)
    result = {
        "company_ref": company_ref, "status": written["status"],
        "trigger": item["trigger"], "period_label": item["period_label"],
        "review_ref": written["review_ref"], "review_version_ref": written["id"],
        "version": written["version"], "form_a_view": written["answers"]["form_a_view"],
        "candidates": 0, "candidates_ungranted": 0,
    }
    lines = written["answers"]["rewritten_lines"]
    if not lines:
        return result
    if not may_propose:
        result["candidates_ungranted"] = len(lines)
        result["candidate_reason"] = (
            f"the mission does not grant {CANDIDATE_SCOPE} with its "
            f"{CHECKPOINT_KIND} checkpoint; the rewrite is recorded in the review "
            "and proposed to nobody"
        )
        return result
    from .event_judgement import company_theses

    by_ref = {
        str(thesis["ref"]): thesis
        for thesis in company_theses(store.connection, company_ref)
    }
    for line in lines:
        thesis = by_ref.get(line["thesis_ref"])
        if thesis is None:  # pragma: no cover - validation already refused this
            continue
        reviews.record_revision_candidate(
            review=written, thesis=thesis, line=line, mission=mission,
            actor_ref=actor_ref,
        )
        result["candidates"] += 1
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W4 zero-base review")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--mode", choices=MODES, default="review")
    parser.add_argument("--model-config")
    parser.add_argument("--verifier-model-config")
    parser.add_argument("--tracking-policy")
    parser.add_argument("--scheduler")
    parser.add_argument("--company-ref")
    parser.add_argument("--max-reviews", type=int, default=MAX_REVIEWS_PER_RUN)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    summary = run_zero_base(
        state_dir=Path(args.state_dir),
        summary_dir=Path(args.summary_dir),
        mode=args.mode,
        model_config=None if not args.model_config else Path(args.model_config),
        verifier_model_config=(None if not args.verifier_model_config
                               else Path(args.verifier_model_config)),
        policy_path=None if not args.tracking_policy else Path(args.tracking_policy),
        scheduler_db=None if not args.scheduler else Path(args.scheduler),
        company_ref=args.company_ref,
        max_reviews=args.max_reviews,
    )
    if not args.quiet:
        json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, indent=1)
        sys.stdout.write("\n")
    return 0 if summary["status"] in ("succeeded", "idle") else 1


__all__ = [
    "MAX_REVIEWS_PER_RUN",
    "MODES",
    "REVIEW_MODEL_CONFIG",
    "VERIFIER_MODEL_CONFIG",
    "SUMMARY_SCHEMA_VERSION",
    "main",
    "outcome_inputs",
    "run_checks",
    "run_zero_base",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
