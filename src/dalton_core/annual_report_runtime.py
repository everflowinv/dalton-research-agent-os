"""Installed model configuration for registered annual-report plan execution."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Mapping

from .call_budget import resolve_call_budget, resolve_run_budget
from .model_transport import (
    broker_frame_execution_binding,
)
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
ANNUAL_LEASE_COMPLETION_GRACE_SECONDS = 30


class AnnualReportRuntimeError(ValueError):
    pass


def validate_annual_transport_retry(value: Any) -> dict[str, int]:
    fields = {
        "max_definitely_not_sent_retries", "queue_wait_seconds",
        "retry_backoff_seconds",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AnnualReportRuntimeError("transport_retry has an invalid closed shape")
    normalized = dict(value)
    if any(isinstance(normalized[name], bool)
           or not isinstance(normalized[name], int)
           or normalized[name] < 0 for name in fields):
        raise AnnualReportRuntimeError(
            "transport_retry values must be finite non-negative integers"
        )
    return normalized


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
    transport_retry = raw.get("transport_retry")
    base = dict(raw)
    base.pop("provider_retry", None)
    # The shared document reader retains its historical protocol ceiling.
    # Annual execution is instead bounded by its frozen Work attempt/deadline
    # budget, so validate this owner policy without inventing a retry ceiling.
    base.pop("transport_retry", None)
    try:
        config = validate_model_config(base)
        config["provider_retry"] = (
            None if retry is None else validate_provider_retry(retry)
        )
        config["transport_retry"] = (
            None if transport_retry is None
            else validate_annual_transport_retry(transport_retry)
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
    transport = config.get("transport_retry") or {
        "max_definitely_not_sent_retries": 0,
        "queue_wait_seconds": 0,
        "retry_backoff_seconds": 0,
    }
    tries = int(transport["max_definitely_not_sent_retries"]) + 1
    required_transport_seconds = (
        tries * (call["timeout_seconds"] + transport["queue_wait_seconds"])
        + (tries - 1) * transport["retry_backoff_seconds"]
        + ANNUAL_LEASE_COMPLETION_GRACE_SECONDS
    )
    default_elapsed = (
        attempts * (required_transport_seconds if retry is not None else call["timeout_seconds"])
        + max(0, attempts - 1) * (0 if retry is None else retry["retry_backoff_seconds"])
    )
    max_elapsed = run.get("max_seconds", default_elapsed)
    execution = {
        "routing_policy_ref": config["routing_policy_ref"],
        "credential_slot_refs": list(config["credential_slot_refs"]),
        "budget_db": config["budget_db"],
        "budget_policy_ref": config["budget_policy_ref"],
        "max_input_tokens": call["max_input_tokens"],
        "max_output_tokens": call["max_output_tokens"],
        "max_cost_usd": float(call["max_cost_usd"]),
        "max_seconds": call["timeout_seconds"],
        "max_elapsed_seconds": max_elapsed,
        "max_attempts": attempts,
        "provider_retry": retry,
        "transport_retry": config.get("transport_retry"),
        "broker_frame_policy": broker_frame_execution_binding(config),
    }
    # With provider retry enabled the shared worker routes one candidate per
    # Scheduler attempt. Refuse a plan that cannot fit even that single
    # configured queue/call/retry window inside its hard elapsed budget. The
    # legacy multi-route case is checked later against the actual Router.
    if retry is not None:
        if required_transport_seconds > max_elapsed:
            raise AnnualReportRuntimeError(
                f"annual-report {purpose} one-attempt transport bound {required_transport_seconds}s "
                f"exceeds run max_seconds {max_elapsed}s"
            )
    return execution


def adapter_for_config(
    config: Mapping[str, Any], *, router: Any, purpose: str,
    model_execution: Mapping[str, Any] | None = None,
) -> Any:
    from .openclaw_model_adapter import OpenClawModelAdapter
    from .model_transport import (
        LEGACY_BROKER_MAX_FRAME_BYTES,
        resolve_broker_max_frame_bytes,
    )

    if model_execution is None:
        frame_bytes = resolve_broker_max_frame_bytes(config)
    elif model_execution.get("broker_frame_policy") is None:
        # Executions persisted before broker-frame-policy-0.1 keep their
        # original client bound rather than silently changing on replay.
        frame_bytes = LEGACY_BROKER_MAX_FRAME_BYTES
    else:
        frame_bytes = resolve_broker_max_frame_bytes(
            model_execution["broker_frame_policy"]
        )

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
        max_frame_bytes=frame_bytes,
    )


def annual_attempt_lease_seconds(
    model_execution: Mapping[str, Any], *, router: Any, purpose: str
) -> int:
    """Return one claim's worst-case broker wall time from frozen authority.

    Provider retries make every paid call a separate Scheduler attempt, so a
    claim reaches one route.  Legacy plans without that policy can still walk
    the pinned fallback chain inside one claim; those claims cover every live
    link the shared worker can visit.  A definitely-not-sent retry is local to
    each route and therefore multiplies only that route's queue/call window.
    """

    retry = model_execution.get("transport_retry")
    transport = (
        {"max_definitely_not_sent_retries": 0,
         "queue_wait_seconds": 0, "retry_backoff_seconds": 0}
        if retry is None else validate_annual_transport_retry(retry)
    )
    candidates = 1
    if model_execution.get("provider_retry") is None:
        from .model_fallback_chain import (
            resolve_chain, tier_chain, tier_for,
        )

        policy = router.get_policy(model_execution["routing_policy_ref"])
        has_chain = bool(policy.get("fallback_chains")) or purpose in (
            policy.get("purpose_overrides") or {}
        )
        if has_chain:
            profiles = {item["id"]: item for item in router.latest_profiles()}
            tier = tier_for(purpose)
            resolved = resolve_chain(
                policy, tier=tier, purpose=purpose, profiles=profiles,
            )
            chain = (
                tuple(resolved["chain"])
                if resolved is not None else tier_chain(tier)
            )
            candidates = max(1, len(chain))
    tries = transport["max_definitely_not_sent_retries"] + 1
    per_route = (
        tries * (
            int(model_execution["max_seconds"])
            + transport["queue_wait_seconds"]
        )
        + (tries - 1) * transport["retry_backoff_seconds"]
    )
    required = candidates * per_route
    required += ANNUAL_LEASE_COMPLETION_GRACE_SECONDS
    maximum = int(model_execution.get(
        "max_elapsed_seconds",
        int(model_execution["max_seconds"]) * int(model_execution["max_attempts"]),
    ))
    if required > maximum:
        raise AnnualReportRuntimeError(
            f"annual-report {purpose} one-attempt transport bound {required}s "
            f"exceeds Work max_elapsed_seconds {maximum}s"
        )
    return required


def scheduler_policy(model_executions: tuple[Mapping[str, Any], Mapping[str, Any]]) -> dict[str, Any]:
    """Scheduler authority spanning each model Work's hard elapsed bound."""

    maximum = max(3, *(int(item["max_attempts"]) for item in model_executions))
    # Worker claims use their exact queue/call/retry bound.  The Scheduler is
    # shared by both annual stages and cannot read the Router here, so its
    # immutable ceiling is the largest already-frozen Work elapsed budget.
    # This admits the exact claim without creating authority beyond the Work.
    total_lease_seconds = max(
        300,
        *(int(item.get(
            "max_elapsed_seconds",
            int(item["max_seconds"]) * int(item["max_attempts"]),
        )) for item in model_executions),
    )
    return {
        "max_attempts": maximum,
        "max_lease_seconds": total_lease_seconds,
        "max_total_lease_seconds": total_lease_seconds,
        "policy_version_id": "scheduler-policy:registered-annual-report:" + content_hash({
            "max_attempts": maximum,
            "max_lease_seconds": total_lease_seconds,
            "max_total_lease_seconds": total_lease_seconds,
        })[:24],
    }


__all__ = [
    "ANNUAL_LEASE_COMPLETION_GRACE_SECONDS", "AnnualReportRuntimeError",
    "DRAFT_MODEL_CONFIG_NAME",
    "VERIFIER_MODEL_CONFIG_NAME", "adapter_for_config",
    "annual_attempt_lease_seconds",
    "load_annual_report_model_config", "load_annual_report_model_configs",
    "plan_model_execution", "validate_annual_transport_retry",
    "scheduler_policy",
]
