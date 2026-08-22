"""Owner-authorized 3x30 production canary campaign for the pinned verifier.

This module never runs a paid call by itself: it orchestrates repeated
``run_live_calibration`` rounds against one exact broker profile under the
``provider-controlled-v1`` tier with a frozen thinking level, enforces a
campaign-level hard spend cap across rounds, and evaluates the frozen
production acceptance gate (0 false positives, 0 high-severity misses, 0
schema/control failures, every round complete) from the durable per-round
records.  The owner authorizes the run by supplying explicit caps; the
campaign summary is the review evidence for any later production policy
activation decision.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .openclaw_model_adapter import (
    OpenClawModelAdapterError,
    PROVIDER_CONTROL_MODE_REQUIRED,
    VERIFIER_THINKING_LEVELS,
)
from .store import canonical_json, content_hash
from .thesis_impact_calibration import load_frozen_calibration_corpus
from .thesis_impact_calibration_runner import (
    DEFAULT_BROKER_AGENT_ID,
    ThesisImpactCalibrationRunError,
    _git_state,
    _money,
    _secure_write,
    load_calibration_records,
    run_live_calibration,
    validate_calibration_run_manifest,
)


CAMPAIGN_SCHEMA_VERSION = "0.1"
PRODUCTION_MINIMUM_ROUNDS = 3
MAX_ROUNDS = 10
DEFAULT_PROFILE_ID = "profile:gemini-3-7-flash"
DEFAULT_THINKING_LEVEL = "low"
DEFAULT_ROUNDS = PRODUCTION_MINIMUM_ROUNDS
DEFAULT_PER_CASE_CAP_USD = Decimal("0.05")
DEFAULT_PER_ROUND_CAP_USD = Decimal("1.60")
DEFAULT_CAMPAIGN_CAP_USD = Decimal("5.00")
DEFAULT_MAX_INPUT_TOKENS = 30_000
DEFAULT_MAX_OUTPUT_TOKENS = 4_000
DEFAULT_TIMEOUT_SECONDS = 180
_CAMPAIGN_FIELDS = {
    "schema_version", "id", "created_at", "repo_commit", "corpus_ref",
    "corpus_hash", "profile_id", "execution_tier", "thinking_level", "rounds",
    "case_refs", "case_count", "per_case_cap_usd", "per_round_cap_usd",
    "campaign_cap_usd", "max_input_tokens", "max_output_tokens",
    "timeout_seconds",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ThesisImpactCalibrationRunError("campaign timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ThesisImpactCalibrationRunError(f"{name} has an unexpected shape")
    return dict(value)


def build_verifier_canary_manifest(
    *,
    corpus: Mapping[str, Any],
    profile_id: str,
    repo_commit: str,
    created_at: datetime,
    thinking_level: str,
    rounds: int = DEFAULT_ROUNDS,
    per_case_cap_usd: Decimal = DEFAULT_PER_CASE_CAP_USD,
    per_round_cap_usd: Decimal = DEFAULT_PER_ROUND_CAP_USD,
    campaign_cap_usd: Decimal = DEFAULT_CAMPAIGN_CAP_USD,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    case_refs: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Freeze one production canary campaign before any paid call."""

    from .thesis_impact_calibration import validate_calibration_corpus

    frozen = validate_calibration_corpus(corpus)
    if len(repo_commit) != 40 or any(
        char not in "0123456789abcdef" for char in repo_commit
    ):
        raise ThesisImpactCalibrationRunError("repo_commit must be a full lowercase SHA")
    if not isinstance(profile_id, str) or not profile_id.startswith("profile:"):
        raise ThesisImpactCalibrationRunError("profile_id must be a broker profile ref")
    if thinking_level not in VERIFIER_THINKING_LEVELS:
        raise ThesisImpactCalibrationRunError("thinking_level is unsupported")
    if (
        isinstance(rounds, bool)
        or not isinstance(rounds, int)
        or rounds < 1
        or rounds > MAX_ROUNDS
    ):
        raise ThesisImpactCalibrationRunError(
            f"rounds must be an integer between 1 and {MAX_ROUNDS}"
        )
    case_cap = _money(per_case_cap_usd, "per_case_cap_usd", positive=True)
    round_cap = _money(per_round_cap_usd, "per_round_cap_usd", positive=True)
    campaign_cap = _money(campaign_cap_usd, "campaign_cap_usd", positive=True)
    available = [case["id"] for case in frozen["cases"]]
    selected = available if case_refs is None else list(case_refs)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected).issubset(available)
    ):
        raise ThesisImpactCalibrationRunError("case_refs are not a unique corpus subset")
    selected = [case_ref for case_ref in available if case_ref in selected]
    if len(selected) * case_cap > round_cap:
        raise ThesisImpactCalibrationRunError("round reservations exceed the round cap")
    if rounds * round_cap > campaign_cap:
        raise ThesisImpactCalibrationRunError("round reservations exceed the campaign cap")
    for value, name in (
        (max_input_tokens, "max_input_tokens"),
        (max_output_tokens, "max_output_tokens"),
        (timeout_seconds, "timeout_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ThesisImpactCalibrationRunError(f"{name} must be a positive integer")
    identity = {
        "repo_commit": repo_commit,
        "corpus_hash": content_hash(frozen),
        "profile_id": profile_id,
        "execution_tier": PROVIDER_CONTROL_MODE_REQUIRED,
        "thinking_level": thinking_level,
        "rounds": rounds,
        "case_refs": selected,
        "per_case_cap_usd": format(case_cap, "f"),
        "per_round_cap_usd": format(round_cap, "f"),
        "campaign_cap_usd": format(campaign_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
    }
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "id": "thesis-impact-verifier-canary:" + content_hash(identity)[:32],
        "created_at": _wire_time(created_at),
        "repo_commit": repo_commit,
        "corpus_ref": frozen["id"],
        "corpus_hash": content_hash(frozen),
        "profile_id": profile_id,
        "execution_tier": PROVIDER_CONTROL_MODE_REQUIRED,
        "thinking_level": thinking_level,
        "rounds": rounds,
        "case_refs": selected,
        "case_count": len(selected),
        "per_case_cap_usd": format(case_cap, "f"),
        "per_round_cap_usd": format(round_cap, "f"),
        "campaign_cap_usd": format(campaign_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
    }


def validate_verifier_canary_manifest(value: Any) -> dict[str, Any]:
    wire = _closed(value, _CAMPAIGN_FIELDS, "verifier canary campaign manifest")
    if wire["schema_version"] != CAMPAIGN_SCHEMA_VERSION:
        raise ThesisImpactCalibrationRunError("unsupported campaign manifest schema")
    if wire["execution_tier"] != PROVIDER_CONTROL_MODE_REQUIRED:
        raise ThesisImpactCalibrationRunError("campaign must use provider-controlled-v1")
    if wire["thinking_level"] not in VERIFIER_THINKING_LEVELS:
        raise ThesisImpactCalibrationRunError("campaign thinking_level is unsupported")
    if (
        isinstance(wire["rounds"], bool)
        or not isinstance(wire["rounds"], int)
        or wire["rounds"] < 1
        or wire["rounds"] > MAX_ROUNDS
    ):
        raise ThesisImpactCalibrationRunError("campaign rounds is invalid")
    for field in (
        "id", "created_at", "repo_commit", "corpus_ref", "corpus_hash", "profile_id",
    ):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ThesisImpactCalibrationRunError(f"campaign {field} must be text")
    if (
        not isinstance(wire["case_refs"], list)
        or not wire["case_refs"]
        or len(set(wire["case_refs"])) != len(wire["case_refs"])
        or not all(isinstance(item, str) and item for item in wire["case_refs"])
    ):
        raise ThesisImpactCalibrationRunError("campaign case_refs is invalid")
    if wire["case_count"] != len(wire["case_refs"]):
        raise ThesisImpactCalibrationRunError("campaign case_count does not match case_refs")
    case_cap = _money(wire["per_case_cap_usd"], "campaign per_case_cap_usd", positive=True)
    round_cap = _money(wire["per_round_cap_usd"], "campaign per_round_cap_usd", positive=True)
    campaign_cap = _money(
        wire["campaign_cap_usd"], "campaign campaign_cap_usd", positive=True
    )
    if wire["case_count"] * case_cap > round_cap:
        raise ThesisImpactCalibrationRunError("round reservations exceed the round cap")
    if wire["rounds"] * round_cap > campaign_cap:
        raise ThesisImpactCalibrationRunError("round reservations exceed the campaign cap")
    for field in ("max_input_tokens", "max_output_tokens", "timeout_seconds"):
        if (
            isinstance(wire[field], bool)
            or not isinstance(wire[field], int)
            or wire[field] < 1
        ):
            raise ThesisImpactCalibrationRunError(f"campaign {field} is invalid")
    return wire


def evaluate_round_records(
    records: list[Mapping[str, Any]],
    score: Mapping[str, Any] | None,
    *,
    thinking_level: str,
    case_count: int,
) -> dict[str, Any]:
    """Evaluate one round's durable records against the frozen gate."""

    reasons: list[str] = []
    failed_calls = 0
    parse_failures = 0
    control_failures = 0
    thinking_failures = 0
    for record in records:
        result = record.get("result", {})
        metadata = result.get("metadata", {}) if isinstance(result, Mapping) else {}
        if result.get("status") != "succeeded":
            failed_calls += 1
        if record.get("parse_error") is not None:
            parse_failures += 1
        if metadata.get("required_provider_controls") is not True or not metadata.get(
            "provider_control_schema_hash"
        ):
            control_failures += 1
        work_order = record.get("work_order", {})
        work_metadata = (
            work_order.get("metadata", {}) if isinstance(work_order, Mapping) else {}
        )
        if work_metadata.get("verifier_thinking_level") != thinking_level:
            thinking_failures += 1
    if failed_calls:
        reasons.append(f"{failed_calls} broker calls did not succeed")
    if parse_failures:
        reasons.append(f"{parse_failures} outputs failed the closed schema")
    if control_failures:
        reasons.append(f"{control_failures} records lack the provider-control contract")
    if thinking_failures:
        reasons.append(f"{thinking_failures} records do not bind the thinking level")
    false_positives: int | None = None
    high_severity_misses: int | None = None
    accuracy: Any = None
    if score is not None:
        false_positives = score.get("false_positives")
        high_severity_misses = score.get("high_severity_misses")
        accuracy = score.get("accuracy")
        coverage = score.get("coverage", {})
        if (
            not isinstance(coverage, Mapping)
            or coverage.get("numerator") != case_count
            or coverage.get("denominator") != case_count
        ):
            reasons.append("round coverage is incomplete")
        if false_positives:
            reasons.append(f"{false_positives} false positives")
        if high_severity_misses:
            reasons.append(f"{high_severity_misses} high-severity misses")
    else:
        reasons.append("round has no scored output report")
    complete = len(records) == case_count
    if not complete:
        reasons.append("round did not record every case")
    return {
        "cases_recorded": len(records),
        "cases_expected": case_count,
        "complete": complete,
        "failed_calls": failed_calls,
        "parse_failures": parse_failures,
        "control_failures": control_failures,
        "thinking_binding_failures": thinking_failures,
        "false_positives": false_positives,
        "high_severity_misses": high_severity_misses,
        "accuracy": accuracy,
        "accepted": not reasons,
        "rejection_reasons": reasons,
    }


def _round_cost(round_dir: Path) -> tuple[Decimal, Decimal]:
    accounted = Decimal("0")
    reserve = Decimal("0")
    for record in load_calibration_records(round_dir / "responses.jsonl"):
        if record["accounted_cost_usd"] is not None:
            accounted += _money(
                record["accounted_cost_usd"], "round accounted cost"
            )
        else:
            reserve += _money(record["cost_reserve_usd"], "round cost reserve")
    return accounted, reserve


def _evaluate_round_dir(
    round_dir: Path,
    *,
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_path = round_dir / "manifest.json"
    if not manifest_path.is_file():
        return {
            "round_dir": str(round_dir),
            "status": "missing",
            "accepted": False,
            "rejection_reasons": ["round directory has no durable manifest"],
            "accounted_cost_usd": "0",
            "unpriced_reserve_usd": "0",
            "spent_or_reserved_usd": "0",
            "evaluation": None,
        }
    try:
        manifest = validate_calibration_run_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (json.JSONDecodeError, ThesisImpactCalibrationRunError) as exc:
        return {
            "round_dir": str(round_dir),
            "status": "invalid",
            "accepted": False,
            "rejection_reasons": [f"round manifest is invalid: {exc}"],
            "accounted_cost_usd": "0",
            "unpriced_reserve_usd": "0",
            "spent_or_reserved_usd": "0",
            "evaluation": None,
        }
    binding_reasons: list[str] = []
    if (
        manifest["execution_tier"] != campaign["execution_tier"]
        or manifest["thinking_level"] != campaign["thinking_level"]
        or manifest["profile_id"] != campaign["profile_id"]
        or manifest["corpus_hash"] != campaign["corpus_hash"]
        or manifest["case_refs"] != campaign["case_refs"]
    ):
        binding_reasons.append("round manifest does not bind the exact campaign contract")
    score: Mapping[str, Any] | None = None
    score_path = round_dir / "score.json"
    if score_path.is_file():
        try:
            score = json.loads(score_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            binding_reasons.append(f"round score report is unreadable: {exc}")
    records = load_calibration_records(round_dir / "responses.jsonl")
    evaluation = evaluate_round_records(
        records,
        score,
        thinking_level=campaign["thinking_level"],
        case_count=campaign["case_count"],
    )
    accounted, reserve = _round_cost(round_dir)
    reasons = binding_reasons + evaluation["rejection_reasons"]
    return {
        "round_dir": str(round_dir),
        "run_ref": manifest["id"],
        "status": "complete" if evaluation["complete"] else "partial",
        "accounted_cost_usd": format(accounted, "f"),
        "unpriced_reserve_usd": format(reserve, "f"),
        "spent_or_reserved_usd": format(accounted + reserve, "f"),
        "evaluation": evaluation,
        "accepted": not reasons,
        "rejection_reasons": reasons,
    }


def evaluate_campaign_gate(
    rounds: list[Mapping[str, Any]],
    *,
    campaign: Mapping[str, Any],
    spent_or_reserved: Decimal,
) -> dict[str, Any]:
    reasons: list[str] = []
    if len(rounds) != campaign["rounds"]:
        reasons.append("campaign did not evaluate every round")
    if campaign["rounds"] < PRODUCTION_MINIMUM_ROUNDS:
        reasons.append(
            f"production requires at least {PRODUCTION_MINIMUM_ROUNDS} complete rounds"
        )
    for index, item in enumerate(rounds, 1):
        if not item.get("accepted"):
            reasons.append(f"round {index} was not accepted: " + "; ".join(
                item.get("rejection_reasons", ["unknown"])
            ))
    campaign_cap = _money(campaign["campaign_cap_usd"], "campaign cap")
    if spent_or_reserved > campaign_cap:
        reasons.append("aggregate spend exceeded the campaign cap")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "rounds_evaluated": len(rounds),
        "rounds_accepted": sum(bool(item.get("accepted")) for item in rounds),
        "production_minimum_rounds": PRODUCTION_MINIMUM_ROUNDS,
        "aggregate_spent_or_reserved_usd": format(spent_or_reserved, "f"),
        "campaign_cap_usd": campaign["campaign_cap_usd"],
    }


def run_verifier_canary(
    *,
    repo_root: Path,
    output_dir: Path,
    socket_path: Path,
    auth_key_path: Path,
    profile_id: str = DEFAULT_PROFILE_ID,
    expected_agent_id: str = DEFAULT_BROKER_AGENT_ID,
    thinking_level: str = DEFAULT_THINKING_LEVEL,
    rounds: int = DEFAULT_ROUNDS,
    per_case_cap_usd: Decimal = DEFAULT_PER_CASE_CAP_USD,
    per_round_cap_usd: Decimal = DEFAULT_PER_ROUND_CAP_USD,
    campaign_cap_usd: Decimal = DEFAULT_CAMPAIGN_CAP_USD,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    openclaw_config_path: Path | None = None,
    resume: bool = False,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """Run or resume one owner-authorized production canary campaign."""

    repo_root = repo_root.resolve()
    output_dir = output_dir.expanduser().resolve()
    head, dirty = _git_state(repo_root)
    if dirty and not allow_dirty:
        raise ThesisImpactCalibrationRunError("repository must be clean for a paid run")
    corpus = load_frozen_calibration_corpus()
    campaign = build_verifier_canary_manifest(
        corpus=corpus,
        profile_id=profile_id,
        repo_commit=head,
        created_at=_now(),
        thinking_level=thinking_level,
        rounds=rounds,
        per_case_cap_usd=per_case_cap_usd,
        per_round_cap_usd=per_round_cap_usd,
        campaign_cap_usd=campaign_cap_usd,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    manifest_path = output_dir / "campaign.json"
    if output_dir.exists():
        if not resume:
            raise ThesisImpactCalibrationRunError("output directory already exists")
        if not manifest_path.is_file():
            raise ThesisImpactCalibrationRunError("resume directory is incomplete")
        persisted = validate_verifier_canary_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        comparable = dict(campaign)
        comparable["created_at"] = persisted["created_at"]
        if canonical_json(persisted) != canonical_json(comparable):
            raise ThesisImpactCalibrationRunError(
                "resume arguments differ from the persisted campaign"
            )
        campaign = persisted
    else:
        output_dir.mkdir(parents=True, mode=0o700)
        _secure_write(manifest_path, campaign)

    round_cap = _money(campaign["per_round_cap_usd"], "campaign round cap")
    campaign_cap = _money(campaign["campaign_cap_usd"], "campaign cap")
    round_evaluations: list[dict[str, Any]] = []
    spent = Decimal("0")
    campaign_error: str | None = None
    for index in range(1, campaign["rounds"] + 1):
        round_dir = output_dir / f"round-{index}"
        if spent + round_cap > campaign_cap:
            campaign_error = "next round could exceed the campaign hard cap"
            break
        round_resume = round_dir.exists()
        try:
            run_live_calibration(
                repo_root=repo_root,
                output_dir=round_dir,
                socket_path=socket_path,
                auth_key_path=auth_key_path,
                profile_id=campaign["profile_id"],
                expected_agent_id=expected_agent_id,
                run_cap_usd=round_cap,
                per_case_cap_usd=_money(
                    campaign["per_case_cap_usd"], "campaign per-case cap"
                ),
                max_input_tokens=campaign["max_input_tokens"],
                max_output_tokens=campaign["max_output_tokens"],
                timeout_seconds=campaign["timeout_seconds"],
                execution_tier=campaign["execution_tier"],
                thinking_level=campaign["thinking_level"],
                openclaw_config_path=openclaw_config_path,
                resume=round_resume,
                allow_dirty=allow_dirty,
            )
        except (ThesisImpactCalibrationRunError, OpenClawModelAdapterError) as exc:
            campaign_error = f"round {index} failed: {exc}"
            round_evaluations.append(_evaluate_round_dir(round_dir, campaign=campaign))
            break
        evaluation = _evaluate_round_dir(round_dir, campaign=campaign)
        round_evaluations.append(evaluation)
        spent += _money(evaluation["spent_or_reserved_usd"], "round cost")
        if not evaluation["accepted"]:
            campaign_error = f"round {index} did not pass the acceptance gate"
            break

    for index in range(len(round_evaluations) + 1, campaign["rounds"] + 1):
        round_evaluations.append(
            _evaluate_round_dir(output_dir / f"round-{index}", campaign=campaign)
        )
    spent = sum(
        (
            _money(item["spent_or_reserved_usd"], "campaign round cost")
            for item in round_evaluations
        ),
        Decimal("0"),
    )
    gate = evaluate_campaign_gate(
        round_evaluations, campaign=campaign, spent_or_reserved=spent
    )
    status = "complete"
    if campaign_error is not None:
        status = "failed"
    elif not gate["eligible"]:
        status = "finished_not_eligible"
    summary = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_ref": campaign["id"],
        "status": status,
        "error": campaign_error,
        "profile_id": campaign["profile_id"],
        "thinking_level": campaign["thinking_level"],
        "execution_tier": campaign["execution_tier"],
        "rounds": round_evaluations,
        "production_gate": gate,
        "output_dir": str(output_dir),
    }
    _secure_write(output_dir / "campaign-summary.json", summary)
    return summary


def main(argv: list[str | None] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the owner-authorized 3x30 provider-controlled verifier canary."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--thinking-level", choices=sorted(VERIFIER_THINKING_LEVELS))
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--per-case-cap-usd", type=Decimal, default=DEFAULT_PER_CASE_CAP_USD)
    parser.add_argument("--per-round-cap-usd", type=Decimal, default=DEFAULT_PER_ROUND_CAP_USD)
    parser.add_argument("--campaign-cap-usd", type=Decimal, default=DEFAULT_CAMPAIGN_CAP_USD)
    parser.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--openclaw-config", type=Path)
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--auth-key-path", type=Path, required=True)
    parser.add_argument("--expected-agent-id", default=DEFAULT_BROKER_AGENT_ID)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_verifier_canary(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            socket_path=args.socket_path,
            auth_key_path=args.auth_key_path,
            profile_id=args.profile_id,
            expected_agent_id=args.expected_agent_id,
            thinking_level=(
                args.thinking_level
                if args.thinking_level is not None
                else DEFAULT_THINKING_LEVEL
            ),
            rounds=args.rounds,
            per_case_cap_usd=args.per_case_cap_usd,
            per_round_cap_usd=args.per_round_cap_usd,
            campaign_cap_usd=args.campaign_cap_usd,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
            openclaw_config_path=args.openclaw_config,
            resume=args.resume,
            allow_dirty=args.allow_dirty,
        )
    except (ThesisImpactCalibrationRunError, OpenClawModelAdapterError) as exc:
        print(f"verifier canary failed: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["production_gate"]["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRODUCTION_MINIMUM_ROUNDS",
    "ThesisImpactCalibrationRunError",
    "build_verifier_canary_manifest",
    "evaluate_campaign_gate",
    "evaluate_round_records",
    "run_verifier_canary",
    "validate_verifier_canary_manifest",
]
