"""Durable multi-profile smoke matrix for thesis-impact verifier calibration."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .model_deployment import openclaw_broker_profiles
from .openclaw_model_adapter import (
    OpenClawModelAdapterError,
    PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
)
from .store import canonical_json, content_hash
from .thesis_impact_calibration import (
    load_frozen_calibration_corpus,
    validate_calibration_corpus,
)
from .thesis_impact_calibration_runner import (
    DEFAULT_BROKER_AGENT_ID,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    ThesisImpactCalibrationRunError,
    _append_record,
    _git_state,
    _money,
    _secure_write,
    load_calibration_records,
    run_live_calibration,
)


SCHEMA_VERSION = "0.2"
DEFAULT_TOTAL_CAP_USD = Decimal("4.60")
DEFAULT_PER_CASE_CAP_USD = Decimal("0.20")
DEFAULT_MATRIX_MAX_INPUT_TOKENS = 10_000
_MANIFEST_FIELDS = {
    "schema_version", "id", "created_at", "repo_commit", "corpus_ref",
    "corpus_hash", "execution_tier", "profile_ids", "case_refs",
    "total_cap_usd", "per_case_cap_usd", "max_input_tokens",
    "max_output_tokens", "timeout_seconds",
}
_RECORD_FIELDS = {
    "schema_version", "profile_id", "status", "started_at", "completed_at",
    "accounted_cost_usd", "unpriced_reserve_usd", "spent_or_reserved_usd",
    "succeeded_calls", "valid_outputs", "run_summary", "error",
}


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ThesisImpactCalibrationRunError("matrix timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ThesisImpactCalibrationRunError(f"{name} has an unexpected shape")
    return dict(value)


def build_calibration_matrix_manifest(
    *,
    corpus: Mapping[str, Any],
    profiles: list[Mapping[str, Any]],
    repo_commit: str,
    created_at: datetime,
    case_refs: list[str] | tuple[str, ...],
    total_cap_usd: Decimal,
    per_case_cap_usd: Decimal,
    max_input_tokens: int,
    max_output_tokens: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    frozen = validate_calibration_corpus(corpus)
    if len(repo_commit) != 40 or any(char not in "0123456789abcdef" for char in repo_commit):
        raise ThesisImpactCalibrationRunError("repo_commit must be a full lowercase SHA")
    profile_ids = [profile.get("id") for profile in profiles]
    if (
        not profile_ids
        or len(set(profile_ids)) != len(profile_ids)
        or not all(isinstance(item, str) and item.startswith("profile:") for item in profile_ids)
    ):
        raise ThesisImpactCalibrationRunError("matrix profiles are invalid")
    available_case_refs = [case["id"] for case in frozen["cases"]]
    requested_case_refs = list(case_refs)
    if (
        not requested_case_refs
        or len(set(requested_case_refs)) != len(requested_case_refs)
        or not set(requested_case_refs).issubset(set(available_case_refs))
    ):
        raise ThesisImpactCalibrationRunError("matrix cases are not a unique corpus subset")
    selected_case_refs = [
        case_ref for case_ref in available_case_refs if case_ref in requested_case_refs
    ]
    total_cap = _money(total_cap_usd, "total_cap_usd", positive=True)
    case_cap = _money(per_case_cap_usd, "per_case_cap_usd", positive=True)
    if len(profile_ids) * len(selected_case_refs) * case_cap > total_cap:
        raise ThesisImpactCalibrationRunError("matrix reservations exceed the total cap")
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
        "profile_ids": profile_ids,
        "case_refs": selected_case_refs,
        "total_cap_usd": format(total_cap, "f"),
        "per_case_cap_usd": format(case_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "thesis-impact-calibration-matrix:" + content_hash(identity)[:32],
        "created_at": _wire_time(created_at),
        "repo_commit": repo_commit,
        "corpus_ref": frozen["id"],
        "corpus_hash": content_hash(frozen),
        "execution_tier": PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        "profile_ids": profile_ids,
        "case_refs": selected_case_refs,
        "total_cap_usd": format(total_cap, "f"),
        "per_case_cap_usd": format(case_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
    }


def validate_calibration_matrix_manifest(value: Any) -> dict[str, Any]:
    wire = _closed(value, _MANIFEST_FIELDS, "calibration matrix manifest")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ThesisImpactCalibrationRunError("unsupported matrix manifest schema")
    if wire["execution_tier"] != PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC:
        raise ThesisImpactCalibrationRunError("matrix must use calibration-posthoc-v1")
    for field in ("id", "created_at", "repo_commit", "corpus_ref", "corpus_hash"):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ThesisImpactCalibrationRunError(f"matrix {field} must be text")
    for field in ("profile_ids", "case_refs"):
        items = wire[field]
        if (
            not isinstance(items, list)
            or not items
            or len(set(items)) != len(items)
            or not all(isinstance(item, str) and item for item in items)
        ):
            raise ThesisImpactCalibrationRunError(f"matrix {field} is invalid")
    total_cap = _money(wire["total_cap_usd"], "matrix total cap", positive=True)
    case_cap = _money(wire["per_case_cap_usd"], "matrix case cap", positive=True)
    if len(wire["profile_ids"]) * len(wire["case_refs"]) * case_cap > total_cap:
        raise ThesisImpactCalibrationRunError("matrix reservations exceed total cap")
    for field in ("max_input_tokens", "max_output_tokens", "timeout_seconds"):
        if isinstance(wire[field], bool) or not isinstance(wire[field], int) or wire[field] < 1:
            raise ThesisImpactCalibrationRunError(f"matrix {field} is invalid")
    return wire


def validate_calibration_matrix_record(value: Any) -> dict[str, Any]:
    wire = _closed(value, _RECORD_FIELDS, "calibration matrix record")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ThesisImpactCalibrationRunError("unsupported matrix record schema")
    if wire["status"] not in {"complete", "failed"}:
        raise ThesisImpactCalibrationRunError("matrix record status is invalid")
    for field in ("profile_id", "started_at", "completed_at"):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ThesisImpactCalibrationRunError(f"matrix record {field} must be text")
    accounted = _money(wire["accounted_cost_usd"], "matrix record accounted cost")
    reserve = _money(wire["unpriced_reserve_usd"], "matrix record reserve")
    combined = _money(wire["spent_or_reserved_usd"], "matrix record combined cost")
    if accounted + reserve != combined:
        raise ThesisImpactCalibrationRunError("matrix record cost fields do not reconcile")
    for field in ("succeeded_calls", "valid_outputs"):
        if isinstance(wire[field], bool) or not isinstance(wire[field], int) or wire[field] < 0:
            raise ThesisImpactCalibrationRunError(f"matrix record {field} is invalid")
    if wire["run_summary"] is not None and not isinstance(wire["run_summary"], Mapping):
        raise ThesisImpactCalibrationRunError("matrix run_summary must be an object or null")
    if wire["error"] is not None and not isinstance(wire["error"], str):
        raise ThesisImpactCalibrationRunError("matrix error must be text or null")
    wire["run_summary"] = (
        None if wire["run_summary"] is None else dict(wire["run_summary"])
    )
    return wire


def load_calibration_matrix_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = validate_calibration_matrix_record(json.loads(line))
        except (json.JSONDecodeError, ThesisImpactCalibrationRunError) as exc:
            raise ThesisImpactCalibrationRunError(
                f"invalid matrix record line {index}: {exc}"
            ) from exc
        if record["profile_id"] in seen:
            raise ThesisImpactCalibrationRunError("duplicate matrix profile record")
        seen.add(record["profile_id"])
        records.append(record)
    return records


def _profile_cost_and_counts(
    profile_dir: Path,
) -> tuple[Decimal, Decimal, int, int]:
    records = load_calibration_records(profile_dir / "responses.jsonl")
    accounted = sum(
        _money(record["accounted_cost_usd"], "profile accounted cost")
        for record in records
        if record["accounted_cost_usd"] is not None
    )
    reserve = sum(
        _money(record["cost_reserve_usd"], "profile unpriced reserve")
        for record in records
        if record["accounted_cost_usd"] is None
    )
    succeeded = sum(record["result"]["status"] == "succeeded" for record in records)
    valid = sum(
        record["result"]["status"] == "succeeded"
        and record["parse_error"] is None
        for record in records
    )
    return accounted, reserve, succeeded, valid


def _write_matrix_summary(
    output_dir: Path,
    manifest: Mapping[str, Any],
    records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    accounted = sum(
        _money(record["accounted_cost_usd"], "matrix summary accounted cost")
        for record in records
    )
    reserve = sum(
        _money(record["unpriced_reserve_usd"], "matrix summary reserve")
        for record in records
    )
    spent_or_reserved = sum(
        _money(record["spent_or_reserved_usd"], "matrix summary cost")
        for record in records
    )
    complete = sum(record["status"] == "complete" for record in records)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "matrix_ref": manifest["id"],
        "status": (
            "complete"
            if complete == len(manifest["profile_ids"])
            else "finished_with_failures"
            if len(records) == len(manifest["profile_ids"])
            else "partial"
        ),
        "profile_count": len(manifest["profile_ids"]),
        "recorded_profiles": len(records),
        "complete_profiles": complete,
        "failed_profiles": sum(record["status"] == "failed" for record in records),
        "succeeded_calls": sum(record["succeeded_calls"] for record in records),
        "valid_outputs": sum(record["valid_outputs"] for record in records),
        "accounted_cost_usd": format(accounted, "f"),
        "unpriced_reserve_usd": format(reserve, "f"),
        "spent_or_reserved_usd": format(spent_or_reserved, "f"),
        "total_cap_usd": manifest["total_cap_usd"],
        "records": list(records),
    }
    _secure_write(output_dir / "matrix-summary.json", summary)
    return summary


def run_live_calibration_matrix(
    *,
    repo_root: Path,
    output_dir: Path,
    socket_path: Path,
    auth_key_path: Path,
    profile_ids: list[str] | tuple[str, ...] | None = None,
    case_refs: list[str] | tuple[str, ...] | None = None,
    expected_agent_id: str = DEFAULT_BROKER_AGENT_ID,
    total_cap_usd: Decimal = DEFAULT_TOTAL_CAP_USD,
    per_case_cap_usd: Decimal = DEFAULT_PER_CASE_CAP_USD,
    max_input_tokens: int = DEFAULT_MATRIX_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    resume: bool = False,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    output_dir = output_dir.expanduser().resolve()
    head, dirty = _git_state(repo_root)
    if dirty and not allow_dirty:
        raise ThesisImpactCalibrationRunError("repository must be clean for a paid run")
    now = datetime.now(timezone.utc)
    corpus = load_frozen_calibration_corpus()
    catalog = openclaw_broker_profiles(
        checked_at=now,
        availability_ttl=timedelta(days=7),
    )
    catalog_by_id = {profile["id"]: profile for profile in catalog}
    selected_ids = list(profile_ids) if profile_ids is not None else list(catalog_by_id)
    unknown = sorted(set(selected_ids) - set(catalog_by_id))
    if unknown:
        raise ThesisImpactCalibrationRunError(f"unknown matrix profiles: {unknown}")
    selected_profiles = [catalog_by_id[profile_id] for profile_id in selected_ids]
    selected_cases = (
        list(case_refs)
        if case_refs is not None
        else [corpus["cases"][0]["id"]]
    )
    requested = build_calibration_matrix_manifest(
        corpus=corpus,
        profiles=selected_profiles,
        repo_commit=head,
        created_at=now,
        case_refs=selected_cases,
        total_cap_usd=total_cap_usd,
        per_case_cap_usd=per_case_cap_usd,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    manifest_path = output_dir / "matrix-manifest.json"
    records_path = output_dir / "matrix-records.jsonl"
    if output_dir.exists():
        if not resume:
            raise ThesisImpactCalibrationRunError("matrix output directory already exists")
        if not manifest_path.is_file():
            raise ThesisImpactCalibrationRunError("matrix resume directory is incomplete")
        manifest = validate_calibration_matrix_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        comparable = dict(requested)
        comparable["created_at"] = manifest["created_at"]
        if canonical_json(manifest) != canonical_json(comparable):
            raise ThesisImpactCalibrationRunError("matrix resume arguments differ from manifest")
    else:
        output_dir.mkdir(parents=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        manifest = requested
        _secure_write(manifest_path, manifest)

    records = load_calibration_matrix_records(records_path)
    completed = {record["profile_id"] for record in records}
    if not completed.issubset(set(manifest["profile_ids"])):
        raise ThesisImpactCalibrationRunError("matrix checkpoint contains an unknown profile")
    spent = sum(
        _money(record["spent_or_reserved_usd"], "matrix checkpoint cost")
        for record in records
    )
    total_cap = _money(manifest["total_cap_usd"], "matrix total cap")
    case_cap = _money(manifest["per_case_cap_usd"], "matrix case cap")
    profile_reserve = case_cap * len(manifest["case_refs"])
    for profile_index, profile_id in enumerate(manifest["profile_ids"], 1):
        if profile_id in completed:
            continue
        if spent + profile_reserve > total_cap:
            raise ThesisImpactCalibrationRunError("next profile could exceed matrix total cap")
        started = datetime.now(timezone.utc)
        profile_dir = output_dir / profile_id.removeprefix("profile:")
        run_summary: dict[str, Any] | None = None
        error: str | None = None
        try:
            run_summary = run_live_calibration(
                repo_root=repo_root,
                output_dir=profile_dir,
                socket_path=socket_path,
                auth_key_path=auth_key_path,
                profile_id=profile_id,
                expected_agent_id=expected_agent_id,
                run_cap_usd=profile_reserve,
                per_case_cap_usd=case_cap,
                max_input_tokens=manifest["max_input_tokens"],
                max_output_tokens=manifest["max_output_tokens"],
                timeout_seconds=manifest["timeout_seconds"],
                execution_tier=PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
                case_refs=manifest["case_refs"],
                resume=profile_dir.exists(),
                allow_dirty=allow_dirty,
            )
            status = "complete" if run_summary["status"] == "complete" else "failed"
        except (ThesisImpactCalibrationRunError, OpenClawModelAdapterError) as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        accounted, reserve, succeeded_calls, valid_outputs = _profile_cost_and_counts(
            profile_dir
        )
        if not profile_dir.exists():
            accounted = Decimal("0")
            reserve = Decimal("0")
        cost = accounted + reserve
        record = validate_calibration_matrix_record({
            "schema_version": SCHEMA_VERSION,
            "profile_id": profile_id,
            "status": status,
            "started_at": _wire_time(started),
            "completed_at": _wire_time(datetime.now(timezone.utc)),
            "accounted_cost_usd": format(accounted, "f"),
            "unpriced_reserve_usd": format(reserve, "f"),
            "spent_or_reserved_usd": format(cost, "f"),
            "succeeded_calls": succeeded_calls,
            "valid_outputs": valid_outputs,
            "run_summary": run_summary,
            "error": error,
        })
        _append_record(records_path, record)
        records.append(record)
        completed.add(profile_id)
        spent += cost
        summary = _write_matrix_summary(output_dir, manifest, records)
        print(canonical_json({
            "profile": profile_index,
            "profile_id": profile_id,
            "status": status,
            "accounted_cost_usd": record["accounted_cost_usd"],
            "unpriced_reserve_usd": record["unpriced_reserve_usd"],
            "spent_or_reserved_usd": record["spent_or_reserved_usd"],
            "valid_outputs": valid_outputs,
            "matrix_spent_or_reserved_usd": summary["spent_or_reserved_usd"],
        }), flush=True)
    return _write_matrix_summary(output_dir, manifest, records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a durable one-or-more-case verifier smoke across broker profiles."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile-id", action="append", dest="profile_ids")
    parser.add_argument("--case-ref", action="append", dest="case_refs")
    parser.add_argument("--total-cap-usd", type=Decimal, default=DEFAULT_TOTAL_CAP_USD)
    parser.add_argument("--per-case-cap-usd", type=Decimal, default=DEFAULT_PER_CASE_CAP_USD)
    parser.add_argument(
        "--max-input-tokens",
        type=int,
        default=DEFAULT_MATRIX_MAX_INPUT_TOKENS,
    )
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--auth-key-path", type=Path, required=True)
    parser.add_argument("--expected-agent-id", default=DEFAULT_BROKER_AGENT_ID)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_live_calibration_matrix(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            socket_path=args.socket_path,
            auth_key_path=args.auth_key_path,
            profile_ids=args.profile_ids,
            case_refs=args.case_refs,
            expected_agent_id=args.expected_agent_id,
            total_cap_usd=args.total_cap_usd,
            per_case_cap_usd=args.per_case_cap_usd,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
            allow_dirty=args.allow_dirty,
        )
    except (ThesisImpactCalibrationRunError, OpenClawModelAdapterError) as exc:
        print(f"calibration matrix failed: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_calibration_matrix_manifest",
    "load_calibration_matrix_records",
    "run_live_calibration_matrix",
    "validate_calibration_matrix_manifest",
    "validate_calibration_matrix_record",
]
