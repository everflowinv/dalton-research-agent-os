"""Read the model ledger separately from qualitative document figures."""
from __future__ import annotations

from typing import Any, Mapping

from .model_forecast_driver import ForecastModelAuthority, model_readiness


def financial_state(connection: Any, company_ref: str,
                    mission: Mapping[str, Any]) -> dict[str, Any]:
    """Describe available model inputs without certifying stage completion.

    Use the authority's checked reader without its provisioning constructor.
    A missing or invalid ledger is unknown, never a zero-count assertion.
    """
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='forecast_model_versions'"
        ).fetchone() is None:
            return {"status": "unavailable", "reason": "model_ledger_not_installed"}
        reader = ForecastModelAuthority.__new__(ForecastModelAuthority)
        reader.connection = connection
        model = reader.latest(company_ref)
        if model is None:
            return {"status": "missing", "reason": "no_forecast_model"}
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
        }
    except Exception as exc:
        return {"status": "unavailable", "reason": "model_authority_read_failed",
                "error_type": type(exc).__name__}
