"""Installed model configuration for registered annual-report plan execution."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Mapping

from .call_budget import resolve_call_budget, resolve_run_budget
from .document_extraction import validate_model_config
from .model_configurations import register_model_config_name
from .provider_retry import validate_provider_retry
from .store import content_hash


DRAFT_MODEL_CONFIG_NAME = register_model_config_name(
    "registered-annual-report-draft-model-config.json"
)
VERIFIER_MODEL_CONFIG_NAME = register_model_config_name(
    "registered-annual-report-verifier-model-config.json"
)


class AnnualReportRuntimeError(ValueError):
    pass


def load_annual_report_model_config(path: str | Path, label: str) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise AnnualReportRuntimeError(f"{label} must be an owner-only regular file: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnnualReportRuntimeError(f"{label} is not configured: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AnnualReportRuntimeError(f"{label} cannot be read: {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AnnualReportRuntimeError(f"{label} must contain a JSON object")
    retry = raw.get("provider_retry")
    base = dict(raw)
    base.pop("provider_retry", None)
    try:
        config = validate_model_config(base)
        config["provider_retry"] = (
            None if retry is None else validate_provider_retry(retry)
        )
    except Exception as exc:
        raise AnnualReportRuntimeError(f"{label} is invalid: {exc}") from exc
    return config


def load_annual_report_model_configs(
    state_dir: str | Path,
    *,
    draft_path: str | Path | None = None,
    verifier_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the two independent, Cockpit-editable installed configurations."""

    root = Path(state_dir).expanduser().resolve()
    draft = Path(draft_path).expanduser().resolve() if draft_path else root / DRAFT_MODEL_CONFIG_NAME
    verifier = (
        Path(verifier_path).expanduser().resolve()
        if verifier_path else root / VERIFIER_MODEL_CONFIG_NAME
    )
    configs = (load_annual_report_model_config(
                   draft, "annual-report draft model configuration"),
               load_annual_report_model_config(
                   verifier, "annual-report verifier model configuration"))
    if configs[0]["model_router_db"] != configs[1]["model_router_db"]:
        raise AnnualReportRuntimeError(
            "annual-report draft and verifier must use one Router authority"
        )
    return configs


def plan_model_execution(config: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    """Freeze the installed route, retry and paid-call bounds into a plan."""

    defaults = {
        "max_input_tokens": 32_000 if purpose.endswith("draft") else 48_000,
        "max_output_tokens": 4_000,
        "max_cost_usd": 1.0,
        "timeout_seconds": 120,
    }
    try:
        call = resolve_call_budget(config, purpose, defaults=defaults)
        run = resolve_run_budget(config, purpose, defaults={"max_units": 1})
    except Exception as exc:
        raise AnnualReportRuntimeError(
            f"annual-report {purpose} budget configuration is invalid: {exc}"
        ) from exc
    attempts = run.get("max_units", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise AnnualReportRuntimeError(
            f"annual-report {purpose} run budget max_units must be a positive integer"
        )
    retry = config.get("provider_retry")
    default_elapsed = (
        attempts * call["timeout_seconds"]
        + max(0, attempts - 1) * (0 if retry is None else retry["retry_backoff_seconds"])
    )
    max_elapsed = run.get("max_seconds", default_elapsed)
    return {
        "routing_policy_ref": config["routing_policy_ref"],
        "credential_slot_refs": list(config["credential_slot_refs"]),
        "max_input_tokens": call["max_input_tokens"],
        "max_output_tokens": call["max_output_tokens"],
        "max_cost_usd": float(call["max_cost_usd"]),
        "max_seconds": call["timeout_seconds"],
        "max_elapsed_seconds": max_elapsed,
        "max_attempts": attempts,
        "provider_retry": retry,
    }


def adapter_for_config(config: Mapping[str, Any], *, router: Any, purpose: str) -> Any:
    from .openclaw_model_adapter import OpenClawModelAdapter

    return OpenClawModelAdapter(
        config["broker_socket"],
        route_resolver=router.get_decision,
        auth_client_id=config["broker_client_id"],
        auth_key_provider=lambda: Path(config["broker_auth_key"]).read_bytes().strip(),
        expected_agent_id=config["expected_agent_id"],
        timeout_seconds=float(resolve_call_budget(
            config, purpose,
            defaults={"max_input_tokens": 32_000, "max_output_tokens": 4_000,
                      "max_cost_usd": 1.0, "timeout_seconds": 120},
        )["timeout_seconds"]),
        queue_wait_seconds=float((config.get("transport_retry") or {}).get(
            "queue_wait_seconds", 0
        )),
    )


def scheduler_policy(model_executions: tuple[Mapping[str, Any], Mapping[str, Any]]) -> dict[str, Any]:
    """Scheduler authority for the maximum attempts frozen into either model node."""

    maximum = max(3, *(int(item["max_attempts"]) for item in model_executions))
    call_seconds = max(60, *(int(item["max_seconds"]) for item in model_executions))
    total_lease_seconds = max(300, call_seconds)
    return {
        "max_attempts": maximum,
        "max_lease_seconds": call_seconds,
        "max_total_lease_seconds": total_lease_seconds,
        "policy_version_id": "scheduler-policy:registered-annual-report:" + content_hash({
            "max_attempts": maximum,
            "max_lease_seconds": call_seconds,
            "max_total_lease_seconds": total_lease_seconds,
        })[:24],
    }


__all__ = [
    "AnnualReportRuntimeError", "DRAFT_MODEL_CONFIG_NAME",
    "VERIFIER_MODEL_CONFIG_NAME", "adapter_for_config",
    "load_annual_report_model_config", "load_annual_report_model_configs",
    "plan_model_execution",
    "scheduler_policy",
]
