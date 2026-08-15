"""Deterministic offline AlphaEngine fixture and host-owned MCP adapter stub.

The adapter never opens a socket and never receives serialized credential
material.  Its third argument is an opaque host-owned handle supplied only
after the credential authority revalidates the exact per-call use receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import canonical_json, content_hash


REFERENCE_FIXTURE_DIR = Path(__file__).with_name("reference_shadow_fixtures")
ALPHAENGINE_FIXTURE_CREATED_AT = "2026-08-15T03:00:00.000000+00:00"
_SCENARIOS = (
    "success",
    "empty",
    "partial",
    "pagination",
    "schema_drift",
    "rate_limited",
    "timeout",
    "malformed",
    "permission_denied",
    "revoked",
)
_BEHAVIOR = {
    "success": "return",
    "empty": "return",
    "partial": "return",
    "pagination": "return",
    "schema_drift": "normalize_error",
    "rate_limited": "rate_limited",
    "timeout": "timeout",
    "malformed": "normalize_error",
    "permission_denied": "permission_denied",
    "revoked": "revoked",
}
_PARENT_PARAMETERS = {
    "query": "synthetic semiconductor demand",
    "filters": {
        "company": "Synthetic Corp",
        "date_from": "2026-01-01",
        "date_to": "2026-06-30",
        "document_type": "research_report",
    },
}


class RecordedAlphaEngineError(ValueError):
    pass


def _case(scenario: str, inventory: Mapping[str, Any]) -> dict[str, Any]:
    inventory_case = next(
        item
        for item in inventory["fixtures"]["alphaengine"]["cases"]
        if item["operation"] == "search_library"
        and item["scenario"] == scenario
    )
    raw_payload = (
        {
            "synthetic": True,
            "connector": "alphaengine",
            "operation": "search_library",
            "scenario": scenario,
        }
        if inventory_case["raw_payload_hash"] is not None
        or scenario == "timeout"
        else None
    )
    if inventory_case["raw_payload_hash"] is not None and (
        content_hash(raw_payload) != inventory_case["raw_payload_hash"]
    ):
        raise RecordedAlphaEngineError("inventory raw fixture hash drifted")
    base = {
        "scenario_ref": f"recorded-mcp-scenario:alphaengine:search-library:{scenario}:0.2",
        "scenario": scenario,
        "behavior": _BEHAVIOR[scenario],
        "advance_seconds": 10 if scenario == "timeout" else 0,
        "provider_request_id": (
            None if scenario == "revoked"
            else f"fixture:alphaengine:{scenario.replace('_', '-')}:request:1"
        ),
        "provider_status": inventory_case["provider_status"],
        "retry_after_ms": 1000 if scenario == "rate_limited" else None,
        "source_status": inventory_case["source_status"],
        "completeness": inventory_case["completeness"],
        "next_cursor": inventory_case["next_cursor"],
        "source_record_refs": inventory_case["source_record_refs"],
        "error_code": inventory_case["error_code"],
        "raw_payload": raw_payload,
        "inventory_case_ref": inventory_case["case_ref"],
        "inventory_case_hash": content_hash(inventory_case),
    }
    return {**base, "scenario_hash": content_hash(base)}


def _build_recorded_alphaengine_fixture() -> dict[str, Any]:
    inventory = load_packaged_connector_inventory()
    template = inventory["templates"]["alphaengine"]
    fixture = inventory["fixtures"]["alphaengine"]
    base = {
        "schema_version": "0.2",
        "id": "recorded-mcp-fixture:alphaengine:search-library:0.2",
        "created_at": ALPHAENGINE_FIXTURE_CREATED_AT,
        "connector_profile_template_ref": template["id"],
        "connector_profile_template_hash": template["content_hash"],
        "inventory_fixture_ref": fixture["id"],
        "inventory_fixture_hash": fixture["content_hash"],
        "source_ref": "source:alphaengine",
        "transport_target_ref": "mcp-target:alphaengine",
        "transport_target_hash": template["transport"]["target_hash"],
        "operation": "search_library",
        "parent_parameters": _PARENT_PARAMETERS,
        "parent_query_hash": content_hash(
            {"operation": "search_library", "parameters": _PARENT_PARAMETERS}
        ),
        "scenarios": [_case(scenario, inventory) for scenario in _SCENARIOS],
    }
    return {**base, "content_hash": content_hash(base)}


@lru_cache(maxsize=1)
def _recorded_alphaengine_fixture_json() -> str:
    return canonical_json(_build_recorded_alphaengine_fixture())


def build_recorded_alphaengine_fixture() -> dict[str, Any]:
    return json.loads(_recorded_alphaengine_fixture_json())


def validate_recorded_alphaengine_fixture(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        wire = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RecordedAlphaEngineError("recorded AlphaEngine fixture must be finite JSON") from exc
    expected = build_recorded_alphaengine_fixture()
    if wire != expected:
        raise RecordedAlphaEngineError(
            "recorded AlphaEngine fixture differs from the deterministic frozen build"
        )
    return wire


def load_recorded_alphaengine_fixture(
    path: str | Path | None = None,
) -> dict[str, Any]:
    fixture_path = (
        REFERENCE_FIXTURE_DIR / "alphaengine.json"
        if path is None
        else Path(path)
    )
    try:
        parsed = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecordedAlphaEngineError(
            "packaged AlphaEngine fixture is unreadable"
        ) from exc
    return validate_recorded_alphaengine_fixture(parsed)


def recorded_alphaengine_scenario(
    fixture: Mapping[str, Any], scenario: str
) -> dict[str, Any]:
    wire = validate_recorded_alphaengine_fixture(fixture)
    matches = [item for item in wire["scenarios"] if item["scenario"] == scenario]
    if len(matches) != 1:
        raise RecordedAlphaEngineError("recorded scenario is not uniquely frozen")
    return matches[0]


class RecordedAlphaEngineAdapter:
    """Replay one frozen AlphaEngine scenario without touching the real MCP."""

    def __init__(
        self,
        scenario: str,
        *,
        advance_clock: Callable[[float], None] | None = None,
        invocation_probe: Callable[[Mapping[str, Any], Any], None] | None = None,
    ) -> None:
        self.fixture = load_recorded_alphaengine_fixture()
        self.scenario = recorded_alphaengine_scenario(self.fixture, scenario)
        self._advance_clock = advance_clock
        self._invocation_probe = invocation_probe

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any,
    ) -> dict[str, Any]:
        from .mcp_managed_runner import (
            validate_mcp_managed_adapter_request,
            validate_mcp_managed_transport_observation,
        )

        request_wire = validate_mcp_managed_adapter_request(request)
        expected = self.scenario
        actual_authority = (
            request_wire["recorded_fixture_ref"],
            request_wire["recorded_fixture_hash"],
            request_wire["recorded_scenario_ref"],
            request_wire["recorded_scenario_hash"],
            request_wire["recorded_behavior"],
        )
        expected_authority = (
            self.fixture["id"],
            self.fixture["content_hash"],
            expected["scenario_ref"],
            expected["scenario_hash"],
            expected["behavior"],
        )
        if actual_authority != expected_authority:
            raise RecordedAlphaEngineError("adapter scenario authority drifted")
        if credential_handle is None:
            raise RecordedAlphaEngineError("host-owned credential handle is missing")
        if self._invocation_probe is not None:
            self._invocation_probe(request_wire, credential_handle)
        if expected["behavior"] == "revoked":
            raise RecordedAlphaEngineError(
                "revoked fixture must be stopped by use-time credential authority"
            )
        if expected["behavior"] == "normalize_error":
            raise RecordedAlphaEngineError(str(expected["error_code"]))

        raw_payload = expected["raw_payload"]
        if raw_payload is not None:
            raw_sink.write(canonical_json(raw_payload).encode("utf-8"))
        if expected["advance_seconds"] and self._advance_clock is not None:
            self._advance_clock(float(expected["advance_seconds"]))

        if expected["behavior"] == "rate_limited":
            structured_output = None
            error = {
                "code": "rate_limited",
                "message": "synthetic AlphaEngine rate limit",
                "retryable": True,
            }
            outcome = "rate_limited"
        elif expected["behavior"] == "permission_denied":
            structured_output = None
            error = {
                "code": "permission_denied",
                "message": "synthetic AlphaEngine permission denial",
                "retryable": False,
            }
            outcome = "failed"
        elif expected["behavior"] == "timeout":
            # The returned object must remain internally valid so the runner,
            # not the fixture validator, owns the elapsed-deadline decision.
            structured_output = {
                "source_record_refs": [],
                "next_cursor": None,
                "provider_status": 200,
            }
            error = None
            outcome = "succeeded"
        else:
            structured_output = {
                "source_record_refs": expected["source_record_refs"],
                "next_cursor": expected["next_cursor"],
                "provider_status": expected["provider_status"],
            }
            error = None
            outcome = "succeeded"

        observed_refs = (
            structured_output["source_record_refs"]
            if structured_output is not None
            else []
        )
        observed_cursor = (
            structured_output["next_cursor"]
            if structured_output is not None
            else None
        )
        observed_status = (
            structured_output["provider_status"]
            if structured_output is not None
            else expected["provider_status"]
        )
        observed_source_status = (
            "empty" if expected["behavior"] == "timeout"
            else expected["source_status"]
        )
        observed_completeness = (
            "ranked" if expected["behavior"] == "timeout"
            else expected["completeness"]
        )
        base = {
            "protocol_version": "0.2",
            "request_hash": request_wire["content_hash"],
            "outcome": outcome,
            "provider_request_id": expected["provider_request_id"],
            "provider_status_code": observed_status,
            "retry_after_ms": expected["retry_after_ms"],
            "structured_output": structured_output,
            "source_record_refs": observed_refs,
            "cursor": observed_cursor,
            "provider_usage": {"calls": 1},
            "source_status": observed_source_status,
            "completeness": observed_completeness,
            "error": error,
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )


__all__ = [
    "ALPHAENGINE_FIXTURE_CREATED_AT",
    "RecordedAlphaEngineAdapter",
    "RecordedAlphaEngineError",
    "build_recorded_alphaengine_fixture",
    "load_recorded_alphaengine_fixture",
    "recorded_alphaengine_scenario",
    "validate_recorded_alphaengine_fixture",
]
