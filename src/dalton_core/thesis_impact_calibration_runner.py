"""Durable, admission-bounded live runner for verifier calibration.

The runner sends only model-visible corpus inputs to one exact broker profile.
Each completion is appended and fsynced before the next case.  A crash after a
provider completion but before the append is recovered through the broker's
``replayOnly`` path, so resume never needs an unbounded retry loop.

The frozen budgets are admission and ex-post telemetry gates.  The broker can
bound requested output tokens but cannot stop a provider from reporting extra
host-side input/context or output after the call.  Any such overrun is therefore
recorded durably and stops the run before the next case; it is not mislabeled as
unspent budget.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .model_deployment import ADAPTER_REF, openclaw_broker_profiles
from .model_router import ModelRouter
from .openclaw_model_adapter import (
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
    owner_only_secret_file_provider,
)
from .research_context import count_dalton_search_tokens
from .store import canonical_json, content_hash
from .thesis_impact import VERIFIER_OUTPUT_SCHEMA_VERSION
from .thesis_impact_calibration import (
    build_calibration_prompt,
    load_frozen_calibration_corpus,
    score_verifier_outputs,
    validate_calibration_corpus,
)


SCHEMA_VERSION = "0.1"
DEFAULT_PROFILE_ID = "profile:deepseek-v4-flash"
DEFAULT_RUN_CAP_USD = Decimal("0.25")
DEFAULT_CASE_CAP_USD = Decimal("0.01")
DEFAULT_MAX_INPUT_TOKENS = 3_000
DEFAULT_MAX_OUTPUT_TOKENS = 1_000
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_BROKER_AGENT_ID = "chem"
_MANIFEST_FIELDS = {
    "schema_version", "id", "created_at", "repo_commit", "corpus_ref",
    "corpus_hash", "profile_id", "profile_version_ref", "model_family",
    "run_cap_usd", "per_case_cap_usd", "max_input_tokens",
    "max_output_tokens", "timeout_seconds", "case_refs",
}
_RECORD_FIELDS = {
    "schema_version", "case_ref", "work_order", "route_decision_ref",
    "recovery_mode", "invocation", "result", "parsed_output", "parse_error",
    "accounted_cost_usd", "cost_reserve_usd",
}


class ThesisImpactCalibrationRunError(RuntimeError):
    """A live calibration admission, persistence, or broker check failed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ThesisImpactCalibrationRunError("run timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _money(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ThesisImpactCalibrationRunError(f"{name} must be decimal money")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ThesisImpactCalibrationRunError(f"{name} must be decimal money") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise ThesisImpactCalibrationRunError(f"{name} is outside the admitted range")
    return parsed


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ThesisImpactCalibrationRunError(f"{name} has an unexpected shape")
    return dict(value)


def _git_state(repo_root: Path) -> tuple[str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip())
    return head, dirty


def _secure_write(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _append_record(path: Path, value: Mapping[str, Any]) -> None:
    line = canonical_json(value) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def build_calibration_run_manifest(
    *,
    corpus: Mapping[str, Any],
    profile: Mapping[str, Any],
    repo_commit: str,
    created_at: datetime,
    run_cap_usd: Decimal,
    per_case_cap_usd: Decimal,
    max_input_tokens: int,
    max_output_tokens: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Freeze exact model, corpus, code, and maximum admitted spend."""

    frozen = validate_calibration_corpus(corpus)
    if len(repo_commit) != 40 or any(char not in "0123456789abcdef" for char in repo_commit):
        raise ThesisImpactCalibrationRunError("repo_commit must be a full lowercase SHA")
    run_cap = _money(run_cap_usd, "run_cap_usd", positive=True)
    case_cap = _money(per_case_cap_usd, "per_case_cap_usd", positive=True)
    if len(frozen["cases"]) * case_cap > run_cap:
        raise ThesisImpactCalibrationRunError(
            "case count times per-case hard cap exceeds the run hard cap"
        )
    for value, name in (
        (max_input_tokens, "max_input_tokens"),
        (max_output_tokens, "max_output_tokens"),
        (timeout_seconds, "timeout_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ThesisImpactCalibrationRunError(f"{name} must be a positive integer")
    required_profile = {
        "id", "profile_version_ref", "family", "provider", "model",
        "credential_slot_ref",
    }
    if not isinstance(profile, Mapping) or not required_profile.issubset(profile):
        raise ThesisImpactCalibrationRunError("candidate profile is incomplete")
    identity = {
        "corpus_hash": content_hash(frozen),
        "profile_version_ref": profile["profile_version_ref"],
        "repo_commit": repo_commit,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "thesis-impact-calibration-run:" + content_hash(identity)[:32],
        "created_at": _wire_time(created_at),
        "repo_commit": repo_commit,
        "corpus_ref": frozen["id"],
        "corpus_hash": content_hash(frozen),
        "profile_id": profile["id"],
        "profile_version_ref": profile["profile_version_ref"],
        "model_family": profile["family"],
        "run_cap_usd": format(run_cap, "f"),
        "per_case_cap_usd": format(case_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
        "case_refs": [case["id"] for case in frozen["cases"]],
    }


def validate_calibration_run_manifest(value: Any) -> dict[str, Any]:
    wire = _closed(value, _MANIFEST_FIELDS, "calibration run manifest")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ThesisImpactCalibrationRunError("unsupported run manifest schema")
    for field in (
        "id", "created_at", "repo_commit", "corpus_ref", "corpus_hash",
        "profile_id", "profile_version_ref", "model_family",
    ):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ThesisImpactCalibrationRunError(f"manifest {field} must be text")
    for field in ("run_cap_usd", "per_case_cap_usd"):
        _money(wire[field], f"manifest {field}", positive=True)
    for field in ("max_input_tokens", "max_output_tokens", "timeout_seconds"):
        if isinstance(wire[field], bool) or not isinstance(wire[field], int) or wire[field] < 1:
            raise ThesisImpactCalibrationRunError(f"manifest {field} is invalid")
    if (
        not isinstance(wire["case_refs"], list)
        or not wire["case_refs"]
        or len(set(wire["case_refs"])) != len(wire["case_refs"])
        or not all(isinstance(item, str) and item for item in wire["case_refs"])
    ):
        raise ThesisImpactCalibrationRunError("manifest case_refs are invalid")
    if len(wire["case_refs"]) * _money(
        wire["per_case_cap_usd"], "manifest per_case_cap_usd"
    ) > _money(wire["run_cap_usd"], "manifest run_cap_usd"):
        raise ThesisImpactCalibrationRunError("manifest maximum spend exceeds run cap")
    return wire


def build_calibration_work_order(
    case: Mapping[str, Any], manifest: Mapping[str, Any]
) -> WorkOrder:
    """Create a deterministic WorkOrder so resume keeps one broker identity."""

    run = validate_calibration_run_manifest(manifest)
    prompt = build_calibration_prompt(case)
    case_ref = case.get("id")
    if case_ref not in run["case_refs"]:
        raise ThesisImpactCalibrationRunError("case is outside the run manifest")
    identifier = hashlib.sha256(
        f"{run['id']}\0{case_ref}".encode("utf-8")
    ).hexdigest()[:32]
    created_at = run["created_at"]
    input_block = case["input"]
    return WorkOrder(
        schema_version=SCHEMA_VERSION,
        id=f"work:thesis-impact-calibration-{identifier}",
        created_at=created_at,
        updated_at=created_at,
        question=prompt,
        requested_capabilities=("verify",),
        runtime_profile_ref="runtime-profile:dalton:0.1",
        budget={
            "max_input_tokens": run["max_input_tokens"],
            "max_output_tokens": run["max_output_tokens"],
            "max_total_tokens": run["max_input_tokens"] + run["max_output_tokens"],
            "max_cost_usd": float(_money(
                run["per_case_cap_usd"], "manifest per_case_cap_usd"
            )),
            "max_seconds": run["timeout_seconds"],
        },
        idempotency_key=f"{run['id']}:{case_ref}",
        declared_side_effects=(),
        status="ready",
        input_refs=(
            input_block["assessment"]["id"],
            input_block["claim"]["id"],
            input_block["thesis"]["id"],
        ),
        metadata={
            "phase": "verification-calibration",
            "corpus_ref": run["corpus_ref"],
            "corpus_hash": run["corpus_hash"],
            "case_ref": case_ref,
            "verifier_output_schema_version": VERIFIER_OUTPUT_SCHEMA_VERSION,
        },
    )


def _strict_json_output(result: ResultEnvelope) -> tuple[dict[str, Any], str | None]:
    if result.status != "succeeded":
        raise ThesisImpactCalibrationRunError(
            f"broker result failed: {result.error!r}"
        )
    if set(result.outputs) != {"text", "content_hash"}:
        raise ThesisImpactCalibrationRunError("successful result has invalid outputs")
    text = result.outputs["text"]
    if (
        not isinstance(text, str)
        or not text
        or result.outputs["content_hash"]
        != hashlib.sha256(text.encode("utf-8")).hexdigest()
    ):
        raise ThesisImpactCalibrationRunError("model text/hash binding is invalid")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (TypeError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(parsed, dict):
        return {}, "model output JSON root is not an object"
    return parsed, None


def _record_cost(
    invocation: ModelInvocation,
    case_cap: Decimal,
    *,
    allow_over_cap: bool = False,
) -> tuple[str | None, str]:
    telemetry = invocation.usage.get("raw_provider_telemetry", {})
    cost = telemetry.get("cost", {}) if isinstance(telemetry, Mapping) else {}
    if isinstance(cost, Mapping) and cost.get("available") is True:
        actual = _money(cost.get("usd"), "provider cost")
        if actual > case_cap and not allow_over_cap:
            raise ThesisImpactCalibrationRunError("provider cost exceeds per-case cap")
        # Broker telemetry crosses a JSON number boundary.  Remove binary-float
        # noise while keeping substantially more precision than USD micros.
        actual = actual.quantize(
            Decimal("0.000000000001"), rounding=ROUND_HALF_UP
        ).normalize()
        return format(actual, "f"), "0"
    return None, format(case_cap, "f")


def validate_calibration_record(value: Any) -> dict[str, Any]:
    wire = _closed(value, _RECORD_FIELDS, "calibration record")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ThesisImpactCalibrationRunError("unsupported record schema")
    if wire["recovery_mode"] not in {"fresh_execute", "replay_duplicate"}:
        raise ThesisImpactCalibrationRunError("record recovery_mode is invalid")
    for field in ("case_ref", "route_decision_ref"):
        if not isinstance(wire[field], str) or not wire[field]:
            raise ThesisImpactCalibrationRunError(f"record {field} must be text")
    try:
        work = WorkOrder.from_dict(wire["work_order"]).to_dict()
        invocation = ModelInvocation.from_dict(wire["invocation"]).to_dict()
        result = ResultEnvelope.from_dict(wire["result"]).to_dict()
    except Exception as exc:
        raise ThesisImpactCalibrationRunError("record contracts are invalid") from exc
    if (
        invocation["work_order_ref"] != work["id"]
        or result["work_order_ref"] != work["id"]
        or result["invocation_ref"] != invocation["id"]
    ):
        raise ThesisImpactCalibrationRunError("record contract bindings drifted")
    if not isinstance(wire["parsed_output"], Mapping):
        raise ThesisImpactCalibrationRunError("record parsed_output must be an object")
    if wire["parse_error"] is not None and not isinstance(wire["parse_error"], str):
        raise ThesisImpactCalibrationRunError("record parse_error must be text or null")
    if wire["accounted_cost_usd"] is not None:
        _money(wire["accounted_cost_usd"], "record accounted cost")
    _money(wire["cost_reserve_usd"], "record cost reserve")
    wire["work_order"] = work
    wire["invocation"] = invocation
    wire["result"] = result
    wire["parsed_output"] = dict(wire["parsed_output"])
    return wire


def load_calibration_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = validate_calibration_record(json.loads(line))
        except (json.JSONDecodeError, ThesisImpactCalibrationRunError) as exc:
            raise ThesisImpactCalibrationRunError(
                f"invalid calibration record line {index}: {exc}"
            ) from exc
        if record["case_ref"] in seen:
            raise ThesisImpactCalibrationRunError("duplicate case record")
        seen.add(record["case_ref"])
        records.append(record)
    return records


def calibration_output_map(
    records: list[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    run = validate_calibration_run_manifest(manifest)
    outputs: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = validate_calibration_record(raw)
        if record["case_ref"] not in run["case_refs"]:
            raise ThesisImpactCalibrationRunError("record case is outside manifest")
        if record["result"]["status"] == "succeeded":
            outputs[record["case_ref"]] = record["parsed_output"]
    return outputs


def _write_checkpoint(
    output_dir: Path,
    records: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    corpus: Mapping[str, Any],
) -> dict[str, Any]:
    outputs = calibration_output_map(records, manifest)
    report = score_verifier_outputs(outputs, corpus=corpus)
    _secure_write(output_dir / "outputs.json", outputs)
    _secure_write(output_dir / "score.json", report)
    return report


def _install_exact_router(
    router_path: Path,
    *,
    profile_id: str,
    checked_at: datetime,
    per_case_cap_usd: Decimal,
    run_id: str,
) -> tuple[dict[str, Any], str]:
    profiles = [
        profile for profile in openclaw_broker_profiles(
            checked_at=checked_at, availability_ttl=timedelta(days=7)
        ) if profile["id"] == profile_id
    ]
    if len(profiles) != 1:
        raise ThesisImpactCalibrationRunError("candidate broker profile is unavailable")
    profile = profiles[0]
    if "verify" not in profile["capabilities"]:
        raise ThesisImpactCalibrationRunError("candidate profile cannot verify")
    profile["capabilities"] = ["verify"]
    profile["limits"]["max_cost_usd"] = float(per_case_cap_usd)
    policy_ref = "model-routing-policy-version:thesis-impact-calibration-" + hashlib.sha256(
        run_id.encode("utf-8")
    ).hexdigest()[:16] + ":1"
    policy = {
        "schema_version": SCHEMA_VERSION,
        "policy_version_ref": policy_ref,
        "id": "model-routing-policy:thesis-impact-calibration",
        "version": 1,
        "created_at": _wire_time(checked_at),
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": [profile_id],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": [ADAPTER_REF],
            "required_modalities": ["text"],
            "family_independence_capabilities": ["verify"],
        },
        "ordered_preferences": [
            {"field": "profile_version_ref", "direction": "asc"}
        ],
    }
    with ModelRouter(router_path) as router:
        installed = router.register_profile(profile)
        installed_policy = router.register_policy(policy)
    if installed.get("status") != "fresh" or installed_policy.get("status") != "fresh":
        raise ThesisImpactCalibrationRunError("new calibration router did not install fresh")
    return installed["profile"], policy_ref


def run_live_calibration(
    *,
    repo_root: Path,
    output_dir: Path,
    socket_path: Path,
    auth_key_path: Path,
    profile_id: str = DEFAULT_PROFILE_ID,
    expected_agent_id: str = DEFAULT_BROKER_AGENT_ID,
    run_cap_usd: Decimal = DEFAULT_RUN_CAP_USD,
    per_case_cap_usd: Decimal = DEFAULT_CASE_CAP_USD,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    resume: bool = False,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """Run or resume one exact-profile calibration with durable checkpoints."""

    repo_root = repo_root.resolve()
    output_dir = output_dir.expanduser().resolve()
    head, dirty = _git_state(repo_root)
    if dirty and not allow_dirty:
        raise ThesisImpactCalibrationRunError("repository must be clean for a paid run")
    corpus = load_frozen_calibration_corpus()
    created_at = _now()
    candidate_profiles = [
        profile for profile in openclaw_broker_profiles(
            checked_at=created_at, availability_ttl=timedelta(days=7)
        ) if profile["id"] == profile_id
    ]
    if len(candidate_profiles) != 1:
        raise ThesisImpactCalibrationRunError("candidate profile is not in the broker catalog")
    requested_manifest = build_calibration_run_manifest(
        corpus=corpus,
        profile=candidate_profiles[0],
        repo_commit=head,
        created_at=created_at,
        run_cap_usd=run_cap_usd,
        per_case_cap_usd=per_case_cap_usd,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    manifest_path = output_dir / "manifest.json"
    router_path = output_dir / "router.sqlite"
    records_path = output_dir / "responses.jsonl"
    if output_dir.exists():
        if not resume:
            raise ThesisImpactCalibrationRunError("output directory already exists")
        if not manifest_path.is_file() or not router_path.is_file():
            raise ThesisImpactCalibrationRunError("resume directory is incomplete")
        manifest = validate_calibration_run_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        comparable = dict(requested_manifest)
        comparable["created_at"] = manifest["created_at"]
        if canonical_json(manifest) != canonical_json(comparable):
            raise ThesisImpactCalibrationRunError("resume arguments differ from manifest")
        with ModelRouter(router_path) as router:
            profile = router.get_profile(manifest["profile_version_ref"])
        if profile is None:
            raise ThesisImpactCalibrationRunError("resume profile disappeared")
        policy_ref = "model-routing-policy-version:thesis-impact-calibration-" + hashlib.sha256(
            manifest["id"].encode("utf-8")
        ).hexdigest()[:16] + ":1"
    else:
        output_dir.mkdir(parents=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        manifest = requested_manifest
        profile, policy_ref = _install_exact_router(
            router_path,
            profile_id=profile_id,
            checked_at=created_at,
            per_case_cap_usd=_money(per_case_cap_usd, "per_case_cap_usd"),
            run_id=manifest["id"],
        )
        if profile["profile_version_ref"] != manifest["profile_version_ref"]:
            raise ThesisImpactCalibrationRunError("installed profile differs from manifest")
        _secure_write(manifest_path, manifest)

    records = load_calibration_records(records_path)
    completed = {record["case_ref"] for record in records}
    if not completed.issubset(set(manifest["case_refs"])):
        raise ThesisImpactCalibrationRunError("checkpoint contains an unknown case")
    run_cap = _money(manifest["run_cap_usd"], "manifest run cap")
    case_cap = _money(manifest["per_case_cap_usd"], "manifest case cap")
    spent_or_reserved = sum(
        _money(
            record["accounted_cost_usd"]
            if record["accounted_cost_usd"] is not None
            else record["cost_reserve_usd"],
            "checkpoint cost",
        )
        for record in records
    )
    router = ModelRouter(router_path)
    adapter = OpenClawModelAdapter(
        socket_path,
        route_resolver=router.get_decision,
        expected_agent_id=expected_agent_id,
        auth_client_id="client:dalton-core",
        auth_key_provider=owner_only_secret_file_provider(auth_key_path),
        timeout_seconds=float(manifest["timeout_seconds"]),
        clock=_now,
    )
    try:
        for index, case in enumerate(corpus["cases"], 1):
            if case["id"] in completed:
                continue
            if spent_or_reserved + case_cap > run_cap:
                raise ThesisImpactCalibrationRunError(
                    "next case could exceed the run hard cap"
                )
            work = build_calibration_work_order(case, manifest)
            estimated_input = max(1, count_dalton_search_tokens(work.question))
            if estimated_input > manifest["max_input_tokens"]:
                raise ThesisImpactCalibrationRunError("prompt exceeds frozen input budget")
            routed = router.route(
                work,
                attempt_number=1,
                capability="verify",
                policy_version_ref=policy_ref,
                credential_slot_refs=(profile["credential_slot_ref"],),
                required_modalities=("text",),
                required_context_tokens=estimated_input + manifest["max_output_tokens"],
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=manifest["max_output_tokens"],
                idempotency_key=work.idempotency_key,
                producer_family="human-gold-authority",
            )
            if routed.get("status") not in {"fresh", "duplicate"}:
                raise ThesisImpactCalibrationRunError("route did not converge")
            decision = routed["decision"]
            if decision.get("outcome") != "selected":
                raise ThesisImpactCalibrationRunError(
                    f"candidate route rejected: {decision.get('rejection_reasons')}"
                )
            replay_invocation, replay_result = adapter.replay(work, decision, profile)
            if replay_result.status == "succeeded":
                invocation, result = replay_invocation, replay_result
                recovery_mode = "replay_duplicate"
            elif (
                replay_result.error is not None
                and replay_result.error.get("code") == "IDEMPOTENCY_MISS"
            ):
                invocation, result = adapter.execute(work, decision, profile)
                recovery_mode = "fresh_execute"
            else:
                invocation, result = replay_invocation, replay_result
                recovery_mode = "replay_duplicate"
            if result.status == "succeeded":
                parsed, parse_error = _strict_json_output(result)
            else:
                parsed = {}
                parse_error = f"broker result failed: {result.error!r}"
            accounted, reserve = _record_cost(
                invocation,
                case_cap,
                allow_over_cap=result.status != "succeeded",
            )
            record = {
                "schema_version": SCHEMA_VERSION,
                "case_ref": case["id"],
                "work_order": work.to_dict(),
                "route_decision_ref": decision["id"],
                "recovery_mode": recovery_mode,
                "invocation": invocation.to_dict(),
                "result": result.to_dict(),
                "parsed_output": parsed,
                "parse_error": parse_error,
                "accounted_cost_usd": accounted,
                "cost_reserve_usd": reserve,
            }
            record = validate_calibration_record(record)
            _append_record(records_path, record)
            records.append(record)
            completed.add(case["id"])
            spent_or_reserved += _money(
                accounted if accounted is not None else reserve,
                "case cost",
            )
            _write_checkpoint(output_dir, records, manifest, corpus)
            print(
                canonical_json({
                    "case": index,
                    "case_ref": case["id"],
                    "recovery_mode": recovery_mode,
                    "result_status": result.status,
                    "accounted_cost_usd": accounted,
                    "cost_reserve_usd": reserve,
                }),
                flush=True,
            )
            if result.status != "succeeded":
                raise ThesisImpactCalibrationRunError(parse_error)
    finally:
        router.close()
    report = _write_checkpoint(output_dir, records, manifest, corpus)
    succeeded_cases = sum(
        record["result"]["status"] == "succeeded" for record in records
    )
    return {
        "status": (
            "complete" if succeeded_cases == len(corpus["cases"]) else "partial"
        ),
        "run_ref": manifest["id"],
        "repo_commit": manifest["repo_commit"],
        "profile_id": manifest["profile_id"],
        "completed_cases": succeeded_cases,
        "recorded_cases": len(records),
        "total_cases": len(corpus["cases"]),
        "spent_or_reserved_usd": format(spent_or_reserved, "f"),
        "run_cap_usd": manifest["run_cap_usd"],
        "score": report,
        "output_dir": str(output_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one exact broker verifier against the frozen calibration corpus."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--run-cap-usd", type=Decimal, default=DEFAULT_RUN_CAP_USD)
    parser.add_argument("--per-case-cap-usd", type=Decimal, default=DEFAULT_CASE_CAP_USD)
    parser.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--socket-path", type=Path,
        default=Path("/Users/everflow/.openclaw/dalton-model-broker.sock"),
    )
    parser.add_argument(
        "--auth-key-path", type=Path,
        default=Path("/Users/everflow/.openclaw/dalton-model-broker.sock.key"),
    )
    parser.add_argument("--expected-agent-id", default=DEFAULT_BROKER_AGENT_ID)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_live_calibration(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            socket_path=args.socket_path,
            auth_key_path=args.auth_key_path,
            profile_id=args.profile_id,
            expected_agent_id=args.expected_agent_id,
            run_cap_usd=args.run_cap_usd,
            per_case_cap_usd=args.per_case_cap_usd,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
            allow_dirty=args.allow_dirty,
        )
    except (ThesisImpactCalibrationRunError, OpenClawModelAdapterError) as exc:
        print(f"calibration failed: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ThesisImpactCalibrationRunError",
    "build_calibration_run_manifest",
    "build_calibration_work_order",
    "calibration_output_map",
    "load_calibration_records",
    "run_live_calibration",
    "validate_calibration_record",
    "validate_calibration_run_manifest",
]
