"""Read the model ledger separately from qualitative document figures."""
from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path
import sqlite3

from .model_forecast_driver import ForecastModelAuthority, model_readiness


def financial_state(connection: Any, company_ref: str,
                    mission: Mapping[str, Any], *,
                    state_dir: str | Path | None = None) -> dict[str, Any]:
    result, checked_forecast = _financial_state(connection, company_ref, mission)
    if state_dir is not None:
        from .model_execution_failure_feedback import CONTRACT_REF, last_failures
        try:
            feedback = last_failures(connection, state_dir=state_dir,
                                     company_ref=company_ref, checked_forecast=checked_forecast)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            feedback = {"contract_ref": CONTRACT_REF, "status": "unavailable",
                        "reason": "operations_failure_ledger_could_not_be_verified",
                        "error_type": type(exc).__name__, "items": []}
        result["model_execution_failures"] = feedback["items"]
        result["model_execution_failure_summary"] = {
            key: value for key, value in feedback.items() if key != "items"}
    return result


def _financial_state(connection: Any, company_ref: str,
                     mission: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Describe available model inputs without certifying stage completion.

    Use the authority's checked reader without its provisioning constructor.
    A missing or invalid ledger is unknown, never a zero-count assertion.
    """
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='forecast_model_versions'"
        ).fetchone() is None:
            return {"status": "unavailable", "reason": "model_ledger_not_installed"}, None
        reader = ForecastModelAuthority.__new__(ForecastModelAuthority)
        reader.connection = connection
        model = reader.latest(company_ref)
        if model is None:
            return {"status": "missing", "reason": "no_forecast_model"}, None
        ready = model_readiness(model)
        return {
            "status": "available",
            "model_version_ref": model["id"],
            "model_content_hash": model["content_hash"],
            "mission_version_ref": model.get("mission_version_ref"),
            "current_mission": model.get("mission_version_ref") == mission["id"],
            "history_quarters": ready["history_quarters"],
            "forecast_quarters": ready["forecast_quarters"],
            "drivers_with_history": ready["drivers_with_history"],
            "driver_history": [
                {"driver_ref": driver["ref"], "label": driver["label"],
                 "concept": driver.get("concept"), "role": driver.get("role"),
                 "status": driver["status"],
                 "history_points": len(driver.get("history") or [])}
                for driver in model["drivers"]
            ],
            "computed_result_lines": len(ready["results_computed"]),
            "partial_result_lines": len(ready["results_partial"]),
            "unavailable_result_lines": len(ready["results_unavailable"]),
            "stage_completion": "not_assessed",
        }, model
    except Exception as exc:
        return {"status": "unavailable", "reason": "model_authority_read_failed",
                "error_type": type(exc).__name__}, None
