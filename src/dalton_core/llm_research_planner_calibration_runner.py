"""Durable paid runner for the frozen LLM research-planner calibration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .llm_research_planner import parse_planner_candidate_text
from .llm_research_planner_calibration import (
    build_calibration_prompt,
    load_planner_calibration_corpus,
    score_planner_outputs,
)
from .model_deployment import ADAPTER_REF
from .model_router import ModelRouter
from .openclaw_catalog_reconcile import (
    OpenClawCatalogError,
    load_openclaw_config,
    openclaw_broker_profiles_from_config,
)
from .openclaw_model_adapter import (
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
    PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
    owner_only_secret_file_provider,
)
from .research_context import count_dalton_search_tokens
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
DEFAULT_RUN_CAP_USD = Decimal("75")
DEFAULT_CASE_CAP_USD = Decimal("5")
DEFAULT_MAX_INPUT_TOKENS = 8_000
DEFAULT_MAX_OUTPUT_TOKENS = 800
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_BROKER_AGENT_ID = "chem"
_MANIFEST_FIELDS = {
    "schema_version", "id", "created_at", "repo_commit", "corpus_ref",
    "corpus_hash", "profile_id", "profile_version_ref", "model_family",
    "run_cap_usd", "per_case_cap_usd", "max_input_tokens",
    "max_output_tokens", "timeout_seconds", "case_refs", "execution_tier",
}
_RECORD_FIELDS = {
    "schema_version", "case_ref", "work_order", "route_decision_ref",
    "recovery_mode", "invocation", "result", "parsed_output", "parse_error",
    "accounted_cost_usd", "cost_reserve_usd",
}


class PlannerCalibrationRunError(RuntimeError):
    pass


def admit_dynamic_calibration_profile(
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Grant a brokered model research capability only inside calibration.

    Broker profiles can lack production research capability either because the
    route is newly discovered or because its curated role is narrower.  This
    local immutable derivative lets the paid offline calibration exercise the
    Planner contract without promoting the route into Dalton's deployment
    catalog.
    """

    wire = json.loads(canonical_json(profile))
    if (
        wire.get("adapter_ref") != ADAPTER_REF
        or wire.get("availability", {}).get("state") != "available"
        or not isinstance(wire.get("capabilities"), list)
        or not wire["capabilities"]
    ):
        raise PlannerCalibrationRunError(
            "profile lacks research capability and is not an admitted broker candidate"
        )
    source_hash = content_hash(wire)
    slug = wire["id"].removeprefix("profile:")
    wire.update({
        "profile_version_ref": (
            f"model-profile-version:calibration-{slug}-{source_hash[:16]}:1"
        ),
        "version": 1,
        "prior_version_ref": None,
        "capabilities": ["research"],
    })
    wire.pop("content_hash", None)
    return wire


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise PlannerCalibrationRunError("timestamp must include timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _money(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise PlannerCalibrationRunError(f"{name} must be decimal money")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PlannerCalibrationRunError(f"{name} must be decimal money") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise PlannerCalibrationRunError(f"{name} is outside the admitted range")
    return parsed


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PlannerCalibrationRunError(f"{name} has an unexpected shape")
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
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (canonical_json(value) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_run_manifest(
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
    case_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    available = [case["id"] for case in corpus["cases"]]
    selected = available if case_refs is None else list(case_refs)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected).issubset(set(available))
    ):
        raise PlannerCalibrationRunError("case_refs are not a unique corpus subset")
    selected = [case_ref for case_ref in available if case_ref in selected]
    run_cap = _money(run_cap_usd, "run_cap_usd", positive=True)
    case_cap = _money(per_case_cap_usd, "per_case_cap_usd", positive=True)
    if len(selected) * case_cap > run_cap:
        raise PlannerCalibrationRunError("case hard caps exceed the run hard cap")
    if len(repo_commit) != 40 or any(char not in "0123456789abcdef" for char in repo_commit):
        raise PlannerCalibrationRunError("repo_commit must be a full lowercase SHA")
    for value, name in (
        (max_input_tokens, "max_input_tokens"),
        (max_output_tokens, "max_output_tokens"),
        (timeout_seconds, "timeout_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise PlannerCalibrationRunError(f"{name} must be positive")
    created = _wire_time(created_at)
    identity = {
        "created_at": created,
        "repo_commit": repo_commit,
        "corpus_hash": corpus["content_hash"],
        "profile_id": profile["id"],
        "profile_version_ref": profile["profile_version_ref"],
        "model_family": profile["family"],
        "case_refs": selected,
        "run_cap_usd": format(run_cap, "f"),
        "per_case_cap_usd": format(case_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
        "execution_tier": PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "llm-planner-calibration-run:" + content_hash(identity)[:32],
        "created_at": created,
        "repo_commit": repo_commit,
        "corpus_ref": corpus["id"],
        "corpus_hash": corpus["content_hash"],
        "profile_id": profile["id"],
        "profile_version_ref": profile["profile_version_ref"],
        "model_family": profile["family"],
        "run_cap_usd": format(run_cap, "f"),
        "per_case_cap_usd": format(case_cap, "f"),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
        "case_refs": selected,
        "execution_tier": PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
    }


def validate_run_manifest(value: Any) -> dict[str, Any]:
    wire = _closed(value, _MANIFEST_FIELDS, "planner calibration manifest")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise PlannerCalibrationRunError("manifest schema is unsupported")
    for field in (
        "id", "created_at", "repo_commit", "corpus_ref", "corpus_hash",
        "profile_id", "profile_version_ref", "model_family", "execution_tier",
    ):
        if not isinstance(wire[field], str) or not wire[field]:
            raise PlannerCalibrationRunError(f"manifest {field} is invalid")
    if wire["execution_tier"] != PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC:
        raise PlannerCalibrationRunError("manifest execution tier is invalid")
    _money(wire["run_cap_usd"], "manifest run cap", positive=True)
    _money(wire["per_case_cap_usd"], "manifest case cap", positive=True)
    if not isinstance(wire["case_refs"], list) or not wire["case_refs"]:
        raise PlannerCalibrationRunError("manifest case refs are invalid")
    identity = {
        "created_at": wire["created_at"],
        "repo_commit": wire["repo_commit"],
        "corpus_hash": wire["corpus_hash"],
        "profile_id": wire["profile_id"],
        "profile_version_ref": wire["profile_version_ref"],
        "model_family": wire["model_family"],
        "case_refs": wire["case_refs"],
        "run_cap_usd": wire["run_cap_usd"],
        "per_case_cap_usd": wire["per_case_cap_usd"],
        "max_input_tokens": wire["max_input_tokens"],
        "max_output_tokens": wire["max_output_tokens"],
        "timeout_seconds": wire["timeout_seconds"],
        "execution_tier": wire["execution_tier"],
    }
    if wire["id"] != "llm-planner-calibration-run:" + content_hash(identity)[:32]:
        raise PlannerCalibrationRunError("manifest identity binding failed")
    return wire


def build_calibration_work_order(
    case: Mapping[str, Any], manifest: Mapping[str, Any]
) -> WorkOrder:
    run = validate_run_manifest(manifest)
    if case["id"] not in run["case_refs"]:
        raise PlannerCalibrationRunError("case is outside the run manifest")
    prompt = build_calibration_prompt(case)
    digest = hashlib.sha256(
        f"{run['id']}\0{case['id']}".encode("utf-8")
    ).hexdigest()[:32]
    return WorkOrder(
        schema_version=SCHEMA_VERSION,
        id=f"work:llm-planner-calibration-{digest}",
        created_at=run["created_at"],
        updated_at=run["created_at"],
        question=prompt,
        requested_capabilities=("research",),
        runtime_profile_ref="runtime-profile:dalton-model-broker:0.1",
        budget={
            "max_input_tokens": run["max_input_tokens"],
            "max_output_tokens": run["max_output_tokens"],
            "max_total_tokens": run["max_input_tokens"] + run["max_output_tokens"],
            "max_cost_usd": float(_money(run["per_case_cap_usd"], "case cap")),
            "max_seconds": run["timeout_seconds"],
        },
        idempotency_key=f"{run['id']}:{case['id']}",
        declared_side_effects=(),
        status="ready",
        input_refs=(f"planner-calibration-case:{case['id']}",),
        metadata={
            "phase": "planner-calibration",
            "corpus_ref": run["corpus_ref"],
            "corpus_hash": run["corpus_hash"],
            "case_ref": case["id"],
            "execution_tier": run["execution_tier"],
        },
    )


def _record_cost(invocation: ModelInvocation, case_cap: Decimal) -> tuple[str | None, str]:
    telemetry = invocation.usage.get("raw_provider_telemetry", {})
    cost = telemetry.get("cost", {}) if isinstance(telemetry, Mapping) else {}
    if isinstance(cost, Mapping) and cost.get("available") is True:
        actual = _money(cost.get("usd"), "provider cost")
        actual = actual.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP).normalize()
        return format(actual, "f"), "0"
    return None, format(case_cap, "f")


def _parse_result(result: ResultEnvelope) -> tuple[dict[str, Any], str | None]:
    if result.status != "succeeded":
        return {}, f"broker result failed: {result.error!r}"
    if set(result.outputs) != {"text", "content_hash"}:
        return {}, "successful result has invalid outputs"
    text = result.outputs["text"]
    if (
        not isinstance(text, str)
        or result.outputs["content_hash"]
        != hashlib.sha256(text.encode("utf-8")).hexdigest()
    ):
        return {}, "model text/hash binding is invalid"
    try:
        return parse_planner_candidate_text(text), None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def validate_record(value: Any) -> dict[str, Any]:
    wire = _closed(value, _RECORD_FIELDS, "planner calibration record")
    try:
        wire["work_order"] = WorkOrder.from_dict(wire["work_order"]).to_dict()
        wire["invocation"] = ModelInvocation.from_dict(wire["invocation"]).to_dict()
        wire["result"] = ResultEnvelope.from_dict(wire["result"]).to_dict()
    except Exception as exc:
        raise PlannerCalibrationRunError("record contracts are invalid") from exc
    if not isinstance(wire["parsed_output"], Mapping):
        raise PlannerCalibrationRunError("record parsed output is invalid")
    if wire["parse_error"] is not None and not isinstance(wire["parse_error"], str):
        raise PlannerCalibrationRunError("record parse error is invalid")
    return wire


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    seen = set()
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = validate_record(json.loads(line))
        except Exception as exc:
            raise PlannerCalibrationRunError(
                f"invalid record line {index}: {exc}"
            ) from exc
        if record["case_ref"] in seen:
            raise PlannerCalibrationRunError("record case refs must be unique")
        seen.add(record["case_ref"])
        records.append(record)
    return records


def _install_router(
    path: Path,
    *,
    profile: Mapping[str, Any],
    checked_at: datetime,
    case_cap: Decimal,
    run_id: str,
) -> tuple[dict[str, Any], str]:
    profile = json.loads(canonical_json(profile))
    profile_id = profile["id"]
    if profile.get("capabilities") != ["research"]:
        raise PlannerCalibrationRunError(
            "calibration router requires an exact research-only profile"
        )
    profile["limits"]["max_cost_usd"] = float(case_cap)
    suffix = hashlib.sha256(run_id.encode()).hexdigest()[:16]
    policy_ref = f"model-routing-policy-version:llm-planner-calibration-{suffix}:1"
    policy = {
        "schema_version": SCHEMA_VERSION,
        "policy_version_ref": policy_ref,
        "id": "model-routing-policy:llm-planner-calibration",
        "version": 1,
        "created_at": _wire_time(checked_at),
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": [profile_id],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": [ADAPTER_REF],
            "required_modalities": ["text"],
            "family_independence_capabilities": [],
        },
        "ordered_preferences": [{"field": "profile_version_ref", "direction": "asc"}],
    }
    with ModelRouter(path) as router:
        installed = router.register_profile(profile)
        installed_policy = router.register_policy(policy)
    if installed["status"] != "fresh" or installed_policy["status"] != "fresh":
        raise PlannerCalibrationRunError("calibration router did not install fresh")
    return installed["profile"], policy_ref


def _write_report(
    output_dir: Path,
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    corpus: Mapping[str, Any],
) -> dict[str, Any]:
    output_map = {
        record["case_ref"]: {
            "parsed_output": record["parsed_output"],
            "parse_error": record["parse_error"],
        }
        for record in records
    }
    score = score_planner_outputs(
        corpus, output_map, case_refs=manifest["case_refs"]
    )
    costs = [
        _money(
            record["accounted_cost_usd"]
            if record["accounted_cost_usd"] is not None
            else record["cost_reserve_usd"],
            "record cost",
        )
        for record in records
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_ref": manifest["id"],
        "repo_commit": manifest["repo_commit"],
        "profile_id": manifest["profile_id"],
        "profile_version_ref": manifest["profile_version_ref"],
        "recorded_cases": len(records),
        "total_cost_or_reserve_usd": format(sum(costs, Decimal("0")), "f"),
        "score": score,
    }
    _secure_write(output_dir / "report.json", report)
    return report


def run_live_planner_calibration(
    *,
    repo_root: Path,
    output_dir: Path,
    socket_path: Path,
    auth_key_path: Path,
    openclaw_config_path: Path,
    profile_id: str,
    expected_agent_id: str = DEFAULT_BROKER_AGENT_ID,
    run_cap_usd: Decimal = DEFAULT_RUN_CAP_USD,
    per_case_cap_usd: Decimal = DEFAULT_CASE_CAP_USD,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    case_refs: Sequence[str] | None = None,
    resume: bool = False,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    output_dir = output_dir.expanduser().resolve()
    head, dirty = _git_state(repo_root)
    if dirty and not allow_dirty:
        raise PlannerCalibrationRunError("repository must be clean for a paid run")
    corpus = load_planner_calibration_corpus()
    created_at = _now()
    try:
        catalog = openclaw_broker_profiles_from_config(
            load_openclaw_config(openclaw_config_path),
            checked_at=created_at,
            availability_ttl=timedelta(days=7),
            profile_ids=[profile_id],
        )
    except OpenClawCatalogError as exc:
        raise PlannerCalibrationRunError(f"invalid OpenClaw catalog: {exc}") from exc
    candidates = [
        admit_dynamic_calibration_profile(item)
        for item in catalog
        if item["id"] == profile_id
    ]
    if len(candidates) != 1:
        raise PlannerCalibrationRunError("candidate profile is not in the broker catalog")
    manifest_path = output_dir / "manifest.json"
    records_path = output_dir / "responses.jsonl"
    router_path = output_dir / "router.sqlite"
    if output_dir.exists() and resume:
        manifest = validate_run_manifest(json.loads(manifest_path.read_text()))
        created_at = datetime.fromisoformat(manifest["created_at"])
        with ModelRouter(router_path) as router:
            profile = router.get_profile(manifest["profile_version_ref"])
        suffix = hashlib.sha256(manifest["id"].encode()).hexdigest()[:16]
        policy_ref = f"model-routing-policy-version:llm-planner-calibration-{suffix}:1"
    elif output_dir.exists():
        raise PlannerCalibrationRunError("output directory already exists")
    else:
        manifest = build_run_manifest(
            corpus=corpus,
            profile=candidates[0],
            repo_commit=head,
            created_at=created_at,
            run_cap_usd=run_cap_usd,
            per_case_cap_usd=per_case_cap_usd,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            case_refs=case_refs,
        )
        output_dir.mkdir(parents=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        profile, policy_ref = _install_router(
            router_path,
            profile=candidates[0],
            checked_at=created_at,
            case_cap=_money(per_case_cap_usd, "case cap"),
            run_id=manifest["id"],
        )
        _secure_write(manifest_path, manifest)
    records = load_records(records_path)
    completed = {record["case_ref"] for record in records}
    case_cap = _money(manifest["per_case_cap_usd"], "case cap")
    run_cap = _money(manifest["run_cap_usd"], "run cap")
    spent = sum(
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
        auth_client_id="client:dalton-core",
        auth_key_provider=owner_only_secret_file_provider(auth_key_path),
        timeout_seconds=float(manifest["timeout_seconds"]),
        expected_agent_id=expected_agent_id,
        provider_control_mode=PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        clock=_now,
    )
    try:
        for index, case in enumerate(corpus["cases"], 1):
            if case["id"] not in manifest["case_refs"] or case["id"] in completed:
                continue
            if spent + case_cap > run_cap:
                raise PlannerCalibrationRunError("next case could exceed run cap")
            work = build_calibration_work_order(case, manifest)
            estimated_input = max(1, count_dalton_search_tokens(work.question))
            if estimated_input > manifest["max_input_tokens"]:
                raise PlannerCalibrationRunError("prompt exceeds input budget")
            routed = router.route(
                work,
                attempt_number=1,
                capability="research",
                policy_version_ref=policy_ref,
                credential_slot_refs=(profile["credential_slot_ref"],),
                required_modalities=("text",),
                required_context_tokens=estimated_input + manifest["max_output_tokens"],
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=manifest["max_output_tokens"],
                idempotency_key=work.idempotency_key,
                producer_family=None,
            )
            decision = routed["decision"]
            if decision["outcome"] != "selected":
                raise PlannerCalibrationRunError(
                    f"route rejected: {decision.get('rejection_reasons')}"
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
            parsed, parse_error = _parse_result(result)
            accounted, reserve = _record_cost(invocation, case_cap)
            record = validate_record({
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
            })
            _append_record(records_path, record)
            records.append(record)
            completed.add(case["id"])
            spent += _money(
                accounted if accounted is not None else reserve, "case cost"
            )
            _write_report(output_dir, records, manifest, corpus)
            print(canonical_json({
                "case": index,
                "case_ref": case["id"],
                "parse_error": parse_error,
                "accounted_cost_usd": accounted,
                "cost_reserve_usd": reserve,
            }), flush=True)
    finally:
        router.close()
    report = _write_report(output_dir, records, manifest, corpus)
    return {
        "status": "complete" if len(records) == len(manifest["case_refs"]) else "partial",
        "run_ref": manifest["id"],
        "repo_commit": manifest["repo_commit"],
        "profile_id": manifest["profile_id"],
        "completed_cases": len(records),
        "total_cases": len(manifest["case_refs"]),
        "spent_or_reserved_usd": format(spent, "f"),
        "score": report["score"],
        "output_dir": str(output_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one exact model against the frozen LLM planner corpus."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--openclaw-config", type=Path, required=True)
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--auth-key-path", type=Path, required=True)
    parser.add_argument("--expected-agent-id", default=DEFAULT_BROKER_AGENT_ID)
    parser.add_argument("--run-cap-usd", type=Decimal, default=DEFAULT_RUN_CAP_USD)
    parser.add_argument("--per-case-cap-usd", type=Decimal, default=DEFAULT_CASE_CAP_USD)
    parser.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--case-ref", action="append", dest="case_refs")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_live_planner_calibration(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            socket_path=args.socket_path,
            auth_key_path=args.auth_key_path,
            openclaw_config_path=args.openclaw_config,
            profile_id=args.profile_id,
            expected_agent_id=args.expected_agent_id,
            run_cap_usd=args.run_cap_usd,
            per_case_cap_usd=args.per_case_cap_usd,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
            case_refs=args.case_refs,
            resume=args.resume,
            allow_dirty=args.allow_dirty,
        )
    except (PlannerCalibrationRunError, OpenClawModelAdapterError) as exc:
        print(f"planner calibration failed: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PlannerCalibrationRunError",
    "build_calibration_work_order",
    "build_run_manifest",
    "load_records",
    "run_live_planner_calibration",
    "validate_record",
    "validate_run_manifest",
]
