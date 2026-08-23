"""Deterministic proposal-only package for the transcript connector."""

from __future__ import annotations

import json
from typing import Any

from .store import canonical_json, content_hash


CREATED_AT = "2026-08-23T15:00:00.000000+00:00"
SLUG = "earnings-call-transcript"
CONNECTOR_REF = "connector:earnings-call-transcript"
PROFILE_REF = "connector-profile-template:earnings-call-transcript:0.1"
FIXTURE_REF = "connector-fixture-manifest:earnings-call-transcript:0.1"
TARGET_REF = "adapter:earnings-call-transcript-fetch:0.1"
SOURCE_REF = "source:company-earnings-call-transcript"
SOURCE_VERSION = "proposal-2026-08-23"


def _with_hash(value: dict[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def _schemas() -> tuple[dict[str, Any], dict[str, Any], str, str]:
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "url_ref", "issuer_ref", "ticker", "company_name", "fiscal_year",
            "fiscal_quarter", "source_role",
        ],
        "properties": {
            "url_ref": {
                "type": "string",
                "pattern": "^public-web-url:sha256:[0-9a-f]{64}$",
            },
            "issuer_ref": {"type": "string", "minLength": 1, "maxLength": 500},
            "ticker": {
                "type": "string", "minLength": 1, "maxLength": 10,
                "pattern": "^[A-Z][A-Z0-9.-]{0,9}$",
            },
            "company_name": {
                "type": "string", "minLength": 1, "maxLength": 200,
            },
            "fiscal_year": {"type": "integer", "minimum": 1900, "maximum": 2200},
            "fiscal_quarter": {"type": "integer", "minimum": 1, "maximum": 4},
            "source_role": {
                "type": "string",
                "enum": ["issuer_primary", "third_party_transcript"],
            },
        },
    }
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_record_refs", "next_cursor", "provider_status"],
        "properties": {
            "source_record_refs": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "next_cursor": {"type": ["string", "null"]},
            "provider_status": {"type": "integer", "minimum": 100},
        },
    }
    return (
        input_schema,
        output_schema,
        "schema:connector-proposal:earnings-call-transcript:fetch_get:input:0.1",
        "schema:connector-proposal:earnings-call-transcript:fetch_get:output:0.1",
    )


def _fixture_case(scenario: str) -> dict[str, Any]:
    succeeded = scenario in {"success", "empty", "partial"}
    outcome = (
        "succeeded" if succeeded else
        "rate_limited" if scenario == "rate_limited" else
        "timeout" if scenario == "timeout" else "failed"
    )
    return {
        "case_ref": f"fixture:{SLUG}:fetch_get:{scenario}:0.1",
        "scenario": scenario,
        "operation": "fetch_get",
        "outcome": outcome,
        "provider_status": (
            None if scenario == "timeout" else
            429 if scenario == "rate_limited" else 200
        ),
        "source_status": (
            "complete" if scenario == "success" else
            "empty" if scenario == "empty" else
            "partial" if scenario == "partial" else None
        ),
        "completeness": "partial" if succeeded else None,
        "page": None,
        "next_cursor": None,
        "source_record_refs": (
            ["record:earnings-call-transcript:synthetic:1"]
            if scenario in {"success", "partial"} else []
        ),
        "raw_payload_hash": (
            content_hash({
                "synthetic": True,
                "connector": SLUG,
                "operation": "fetch_get",
                "scenario": scenario,
            })
            if succeeded else None
        ),
        "error_code": None if succeeded else scenario,
    }


def build_earnings_call_transcript_proposal_package() -> dict[str, dict[str, Any]]:
    input_schema, output_schema, input_ref, output_ref = _schemas()
    fixture = _with_hash({
        "schema_version": "0.1",
        "id": FIXTURE_REF,
        "created_at": CREATED_AT,
        "connector_template_ref": PROFILE_REF,
        "recording_boundary": "public_provider",
        "authenticated": False,
        "synthetic": True,
        "operations": [{"operation": "fetch_get", "pagination_mode": "none"}],
        "cases": [
            _fixture_case(scenario)
            for scenario in (
                "success", "empty", "partial", "schema_drift",
                "rate_limited", "timeout", "malformed",
            )
        ],
    })
    target_hash = content_hash({
        "kind": "public_https",
        "target_ref": TARGET_REF,
        "host_policy": "per_call_authority",
        "allowed_hosts": [],
    })
    operation = {
        "operation": "fetch_get",
        "source_method": "fetch_get",
        "input_schema_ref": input_ref,
        "input_schema_hash": content_hash(input_schema),
        "output_schema_ref": output_ref,
        "output_schema_hash": content_hash(output_schema),
        "completeness_ceiling": "partial",
        "pagination": {
            "mode": "none", "cursor_field": None,
            "bounded_window_required": False, "max_pages": 1,
        },
        "side_effects": ["read:recorded-fixture"],
    }
    auth_boundary = {
        "mode": "none", "owner": "none",
        "credential_material": "forbidden", "use_time_authority": "none",
    }
    profile = _with_hash({
        "schema_version": "0.1",
        "id": PROFILE_REF,
        "created_at": CREATED_AT,
        "connector_ref": CONNECTOR_REF,
        "source_identity": {
            "source_ref": SOURCE_REF,
            "source_type": "public_web",
            "source_version": SOURCE_VERSION,
        },
        "transport": {
            "kind": "public_https", "target_ref": TARGET_REF,
            "target_hash": target_hash, "host_policy": "per_call_authority",
            "allowed_hosts": [],
        },
        "auth_boundary": auth_boundary,
        "route_restrictions": {
            "allowed_target_refs": [TARGET_REF],
            "forbidden_target_refs": [
                "route:search-summary", "route:head-only",
                "route:credential-channel", "route:private-network",
                "route:unapproved-transcript-host",
            ],
            "fallback_routes": [],
            "provenance_label_required": True,
        },
        "schema_documents": [
            {
                "schema_ref": input_ref,
                "schema_hash": content_hash(input_schema),
                "document": input_schema,
            },
            {
                "schema_ref": output_ref,
                "schema_hash": content_hash(output_schema),
                "document": output_schema,
            },
        ],
        "operations": [operation],
        "fixture_manifest_ref": FIXTURE_REF,
        "fixture_manifest_hash": fixture["content_hash"],
        "readiness": {
            "level": "inventory_connected", "lease_eligible": False,
            "live_execution_allowed": False,
            "required_gate": "killable_total_deadline_public_transport",
        },
    })
    proposal_operation = {
        "operation": "fetch_get",
        "input_schema_ref": input_ref,
        "input_schema_hash": content_hash(input_schema),
        "output_schema_ref": output_ref,
        "output_schema_hash": content_hash(output_schema),
        "completeness": "partial",
        "pagination": "none",
        "side_effects": ["read:recorded-fixture"],
    }
    proposal = _with_hash({
        "schema_version": "0.2",
        "id": "connector-proposal-manifest:earnings-call-transcript:0.2",
        "created_at": CREATED_AT,
        "capability_proposal_ref": "capability-proposal:connector:earnings-call-transcript:0.1",
        "connector_ref": CONNECTOR_REF,
        "source_identity": {
            "source": SOURCE_REF, "adapter": TARGET_REF,
            "source_version": SOURCE_VERSION, "adapter_version": "proposal-0.1",
        },
        "adapter_package_ref": "inventory-artifact:adapter-contract:earnings-call-transcript:0.1",
        "adapter_source_hash": content_hash({
            "target_ref": TARGET_REF, "operations": ["fetch_get"],
        }),
        "profile_template_ref": PROFILE_REF,
        "profile_template_hash": profile["content_hash"],
        "operations": [proposal_operation],
        "fixture_manifest_ref": FIXTURE_REF,
        "fixture_manifest_hash": fixture["content_hash"],
        "offline_attestation_policy_ref": "policy:connector-inventory-offline:0.1",
        "requested_canary": None,
        "promotion_policy_ref": "policy:connector-promotion:0.1",
        "builder_ref": "builder:earnings-call-transcript-proposal:0.1",
        "transport_kind": "public_https",
        "transport_target_ref": TARGET_REF,
        "transport_target_hash": target_hash,
        "auth_boundary": auth_boundary,
        "inventory_state": "proposal_only",
    })
    return {"profile": profile, "fixture": fixture, "proposal": proposal}


__all__ = ["build_earnings_call_transcript_proposal_package"]
