"""Bounded, authority-verified AlphaEngine document acquisition.

The coordinator owns pagination semantics, not transport.  A narrow page port
must execute one independently governed ConnectorRunner request for each page.
Every successful page is then re-read from immutable Connector/Artifact
authority and the exact raw JSON-RPC object before it can enter the assembled
document manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import validate_connector_runner_response
from .live_mcp_connector import (
    OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
    OPENCLAW_ALPHAENGINE_BRIDGE_REF,
    alphaengine_document_page_from_raw_response,
)
from .raw_spool import RawObject, RawSpool
from .store import canonical_json, content_hash


PLAN_SCHEMA_VERSION = "0.1"
PAGE_REQUEST_SCHEMA_VERSION = "0.1"
MANIFEST_SCHEMA_VERSION = "0.1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_REF_RE = re.compile(r"^alphaengine-doc:([A-Za-z0-9._-]{1,128})$")


class AlphaEngineDocumentAcquisitionError(Exception):
    pass


class AlphaEngineDocumentPagePort(Protocol):
    """Execute one exact, idempotent page request and return Runner authority."""

    def execute_page(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AlphaEngineDocumentAcquisitionError(f"{name} must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} must be finite JSON"
        ) from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} must be a non-empty string"
        )
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} must be an integer >= {minimum}"
        )
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} must be RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise AlphaEngineDocumentAcquisitionError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_content_hash(wire: Mapping[str, Any], name: str) -> None:
    declared = _hash(wire["content_hash"], f"{name}.content_hash")
    expected = content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    )
    if declared != expected:
        raise AlphaEngineDocumentAcquisitionError(f"{name} content_hash mismatch")


_PLAN_FIELDS = {
    "schema_version", "id", "created_at", "document_ref", "document_id",
    "connector_profile_template_ref", "connector_profile_template_hash",
    "source_ref", "source_hash", "bridge_ref", "bridge_hash", "operation",
    "max_pages", "page_max_response_bytes", "max_total_response_bytes",
    "max_document_chars", "content_hash",
}


def build_alphaengine_document_acquisition_plan(
    *,
    document_ref: str,
    created_at: str,
    max_pages: int = 20,
    page_max_response_bytes: int = 1_000_000,
    max_total_response_bytes: int = 20_000_000,
    max_document_chars: int = 2_000_000,
) -> dict[str, Any]:
    """Freeze the exact source identity and all acquisition safety bounds."""

    match = _DOCUMENT_REF_RE.fullmatch(document_ref)
    if match is None:
        raise AlphaEngineDocumentAcquisitionError("document_ref is invalid")
    inventory = load_packaged_connector_inventory()
    template = inventory["templates"]["alphaengine"]
    operation = next(
        item for item in template["operations"]
        if item["operation"] == "get_document"
    )
    identity = {
        "document_ref": document_ref,
        "created_at": _timestamp(created_at, "created_at"),
        "source_hash": content_hash(template["source_identity"]),
        "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
        "max_pages": max_pages,
        "page_max_response_bytes": page_max_response_bytes,
        "max_total_response_bytes": max_total_response_bytes,
        "max_document_chars": max_document_chars,
    }
    base = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "id": "alphaengine-document-plan:" + content_hash(identity),
        "created_at": identity["created_at"],
        "document_ref": document_ref,
        "document_id": match.group(1),
        "connector_profile_template_ref": template["id"],
        "connector_profile_template_hash": template["content_hash"],
        "source_ref": template["source_identity"]["source_ref"],
        "source_hash": identity["source_hash"],
        "bridge_ref": OPENCLAW_ALPHAENGINE_BRIDGE_REF,
        "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
        "operation": operation["operation"],
        "max_pages": max_pages,
        "page_max_response_bytes": page_max_response_bytes,
        "max_total_response_bytes": max_total_response_bytes,
        "max_document_chars": max_document_chars,
    }
    return validate_alphaengine_document_acquisition_plan(
        {**base, "content_hash": content_hash(base)}
    )


def validate_alphaengine_document_acquisition_plan(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _PLAN_FIELDS, "AlphaEngineDocumentAcquisitionPlan")
    if wire["schema_version"] != PLAN_SCHEMA_VERSION:
        raise AlphaEngineDocumentAcquisitionError("unsupported plan schema_version")
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    match = _DOCUMENT_REF_RE.fullmatch(
        _text(wire["document_ref"], "document_ref")
    )
    if match is None or wire["document_id"] != match.group(1):
        raise AlphaEngineDocumentAcquisitionError(
            "document_ref and document_id are inconsistent"
        )
    inventory = load_packaged_connector_inventory()
    template = inventory["templates"]["alphaengine"]
    operation = next(
        item for item in template["operations"]
        if item["operation"] == "get_document"
    )
    frozen = (
        template["id"], template["content_hash"],
        template["source_identity"]["source_ref"],
        content_hash(template["source_identity"]),
        OPENCLAW_ALPHAENGINE_BRIDGE_REF, OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
        operation["operation"],
    )
    actual = (
        wire["connector_profile_template_ref"],
        wire["connector_profile_template_hash"], wire["source_ref"],
        wire["source_hash"], wire["bridge_ref"], wire["bridge_hash"],
        wire["operation"],
    )
    if actual != frozen:
        raise AlphaEngineDocumentAcquisitionError(
            "plan differs from frozen AlphaEngine authority"
        )
    max_pages = _integer(wire["max_pages"], "max_pages", minimum=1)
    page_bytes = _integer(
        wire["page_max_response_bytes"],
        "page_max_response_bytes",
        minimum=1,
    )
    total_bytes = _integer(
        wire["max_total_response_bytes"],
        "max_total_response_bytes",
        minimum=1,
    )
    _integer(wire["max_document_chars"], "max_document_chars", minimum=1)
    if max_pages > int(operation["pagination"]["max_pages"]):
        raise AlphaEngineDocumentAcquisitionError(
            "max_pages exceeds frozen connector inventory"
        )
    if total_bytes < page_bytes:
        raise AlphaEngineDocumentAcquisitionError(
            "total response budget must cover at least one full page"
        )
    _validate_content_hash(wire, "AlphaEngineDocumentAcquisitionPlan")
    return wire


_PAGE_REQUEST_FIELDS = {
    "schema_version", "id", "created_at", "plan_ref", "plan_hash",
    "document_ref", "page_ordinal", "request_cursor", "expected_offset",
    "max_response_bytes", "content_hash",
}


def validate_alphaengine_document_page_request(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _PAGE_REQUEST_FIELDS, "AlphaEngineDocumentPageRequest")
    if wire["schema_version"] != PAGE_REQUEST_SCHEMA_VERSION:
        raise AlphaEngineDocumentAcquisitionError(
            "unsupported page request schema_version"
        )
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    for name in ("id", "plan_ref", "document_ref"):
        wire[name] = _text(wire[name], name)
    _hash(wire["plan_hash"], "plan_hash")
    ordinal = _integer(wire["page_ordinal"], "page_ordinal", minimum=1)
    offset = _integer(wire["expected_offset"], "expected_offset")
    _integer(wire["max_response_bytes"], "max_response_bytes", minimum=1)
    cursor = wire["request_cursor"]
    if cursor is not None and (
        not isinstance(cursor, str)
        or re.fullmatch(r"[0-9]+", cursor) is None
        or int(cursor) != offset
    ):
        raise AlphaEngineDocumentAcquisitionError(
            "page request cursor is not its exact numeric offset"
        )
    if (ordinal == 1) != (cursor is None and offset == 0):
        raise AlphaEngineDocumentAcquisitionError(
            "only page 1 may use the null zero cursor"
        )
    _validate_content_hash(wire, "AlphaEngineDocumentPageRequest")
    return wire


_PAGE_BINDING_FIELDS = {
    "page_ordinal", "page_request_ref", "page_request_hash",
    "runner_response_ref", "runner_response_hash", "runner_request_ref",
    "runner_request_hash", "connector_invocation_ref",
    "connector_invocation_hash", "connector_profile_ref",
    "connector_profile_hash", "call_spec_ref", "call_spec_hash",
    "physical_attempt_ref", "physical_attempt_hash", "usage_entry_ref",
    "usage_entry_hash", "cost_entry_ref", "cost_entry_hash",
    "quota_settlement_ref", "quota_settlement_hash",
    "raw_artifact_version_ref", "raw_artifact_version_hash",
    "source_envelope_ref", "source_envelope_hash", "request_cursor",
    "next_cursor", "offset", "returned_chars", "raw_response_hash",
    "raw_response_bytes", "document_quota_units", "content_hash",
}

_FAILED_PAGE_FIELDS = {
    "page_ordinal", "page_request_ref", "page_request_hash",
    "runner_response_ref", "runner_response_hash", "outcome", "retry_at",
    "physical_attempt_ref", "physical_attempt_hash", "usage_entry_ref",
    "usage_entry_hash", "cost_entry_ref", "cost_entry_hash",
    "quota_settlement_ref", "quota_settlement_hash",
    "document_quota_units", "content_hash",
}

_ASSEMBLED_OBJECT_FIELDS = {"content_hash", "size_bytes", "storage_locator"}

_MANIFEST_FIELDS = {
    "schema_version", "id", "created_at", "plan_ref", "plan_hash",
    "document_ref", "source_ref", "bridge_ref", "status",
    "termination_reason", "content_chars", "declared_content_sha256",
    "assembled_prefix_sha256", "assembled_object", "pages", "failed_page",
    "next_cursor", "physical_calls", "total_raw_response_bytes",
    "document_quota_units", "content_hash",
}


def _validate_page_binding(value: Any, ordinal: int) -> dict[str, Any]:
    wire = _closed(value, _PAGE_BINDING_FIELDS, f"pages[{ordinal - 1}]")
    if wire["page_ordinal"] != ordinal:
        raise AlphaEngineDocumentAcquisitionError("page ordinals are not contiguous")
    for name in _PAGE_BINDING_FIELDS:
        if name.endswith("_hash"):
            _hash(wire[name], f"pages[{ordinal - 1}].{name}")
    for name in (
        "page_request_ref", "runner_response_ref", "runner_request_ref",
        "connector_invocation_ref", "connector_profile_ref", "call_spec_ref",
        "physical_attempt_ref", "usage_entry_ref", "cost_entry_ref",
        "quota_settlement_ref", "raw_artifact_version_ref",
        "source_envelope_ref",
    ):
        _text(wire[name], f"pages[{ordinal - 1}].{name}")
    _integer(wire["offset"], "page offset")
    _integer(wire["returned_chars"], "page returned_chars")
    _integer(wire["raw_response_bytes"], "page raw_response_bytes")
    units = _integer(wire["document_quota_units"], "page document units")
    if units not in {0, 1} or units != (1 if ordinal == 1 else 0):
        raise AlphaEngineDocumentAcquisitionError(
            "page document quota units are inconsistent"
        )
    _validate_content_hash(wire, f"pages[{ordinal - 1}]")
    return wire


def _validate_failed_page(value: Any) -> dict[str, Any]:
    wire = _closed(value, _FAILED_PAGE_FIELDS, "failed_page")
    _integer(wire["page_ordinal"], "failed_page.page_ordinal", minimum=1)
    if wire["outcome"] not in {"retryable", "failed"}:
        raise AlphaEngineDocumentAcquisitionError("failed page outcome is invalid")
    for name in _FAILED_PAGE_FIELDS:
        if name.endswith("_hash"):
            _hash(wire[name], f"failed_page.{name}")
    for name in (
        "page_request_ref", "runner_response_ref", "physical_attempt_ref",
        "usage_entry_ref", "cost_entry_ref", "quota_settlement_ref",
    ):
        _text(wire[name], f"failed_page.{name}")
    units = _integer(
        wire["document_quota_units"], "failed_page.document_quota_units"
    )
    if units not in {0, 1}:
        raise AlphaEngineDocumentAcquisitionError(
            "failed page document quota units are invalid"
        )
    _validate_content_hash(wire, "failed_page")
    return wire


def validate_alphaengine_document_acquisition_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _MANIFEST_FIELDS, "AlphaEngineDocumentAcquisitionManifest")
    if wire["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise AlphaEngineDocumentAcquisitionError(
            "unsupported manifest schema_version"
        )
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    for name in ("id", "plan_ref", "document_ref", "source_ref", "bridge_ref"):
        wire[name] = _text(wire[name], name)
    _hash(wire["plan_hash"], "plan_hash")
    if wire["status"] not in {"complete", "partial", "failed"}:
        raise AlphaEngineDocumentAcquisitionError("manifest status is invalid")
    if wire["termination_reason"] not in {
        "terminal", "max_pages", "max_response_bytes", "max_document_chars",
        "page_failed",
    }:
        raise AlphaEngineDocumentAcquisitionError(
            "manifest termination_reason is invalid"
        )
    if not isinstance(wire["pages"], list):
        raise AlphaEngineDocumentAcquisitionError("pages must be an array")
    pages = [
        _validate_page_binding(item, ordinal)
        for ordinal, item in enumerate(wire["pages"], start=1)
    ]
    for prior, current in zip(pages, pages[1:]):
        if (
            prior["next_cursor"] != current["request_cursor"]
            or int(current["offset"])
            != int(prior["offset"]) + int(prior["returned_chars"])
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "manifest page cursor/offset chain is not contiguous"
            )
    failed = (
        None if wire["failed_page"] is None
        else _validate_failed_page(wire["failed_page"])
    )
    assembled = wire["assembled_object"]
    if assembled is not None:
        assembled = _closed(assembled, _ASSEMBLED_OBJECT_FIELDS, "assembled_object")
        _hash(assembled["content_hash"], "assembled_object.content_hash")
        _integer(assembled["size_bytes"], "assembled_object.size_bytes")
        _text(assembled["storage_locator"], "assembled_object.storage_locator")
    for name in ("content_chars", "physical_calls", "total_raw_response_bytes", "document_quota_units"):
        if wire[name] is not None:
            _integer(wire[name], name)
    for name in ("declared_content_sha256", "assembled_prefix_sha256"):
        if wire[name] is not None:
            _hash(wire[name], name)
    expected_calls = len(pages) + (1 if failed is not None else 0)
    expected_units = sum(item["document_quota_units"] for item in pages)
    if failed is not None:
        expected_units += failed["document_quota_units"]
    if (
        wire["physical_calls"] != expected_calls
        or wire["total_raw_response_bytes"]
        != sum(item["raw_response_bytes"] for item in pages)
        or wire["document_quota_units"] != expected_units
    ):
        raise AlphaEngineDocumentAcquisitionError(
            "manifest aggregate meters are inconsistent"
        )
    if pages:
        if (
            wire["content_chars"] is None
            or wire["declared_content_sha256"] is None
            or wire["assembled_prefix_sha256"] is None
            or assembled is None
            or assembled["content_hash"] != wire["assembled_prefix_sha256"]
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "manifest with pages lacks assembled document bindings"
            )
    elif any(
        item is not None
        for item in (
            wire["content_chars"], wire["declared_content_sha256"],
            wire["assembled_prefix_sha256"], assembled,
        )
    ):
        raise AlphaEngineDocumentAcquisitionError(
            "manifest without pages fabricated document content"
        )
    if wire["status"] == "complete":
        if (
            wire["termination_reason"] != "terminal"
            or not pages
            or failed is not None
            or wire["next_cursor"] is not None
            or pages[-1]["next_cursor"] is not None
            or wire["assembled_prefix_sha256"]
            != wire["declared_content_sha256"]
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "complete manifest lacks terminal full-document authority"
            )
    elif wire["status"] == "failed":
        if pages or failed is None or wire["termination_reason"] != "page_failed":
            raise AlphaEngineDocumentAcquisitionError(
                "failed manifest is inconsistent"
            )
    elif not pages:
        raise AlphaEngineDocumentAcquisitionError(
            "partial manifest must retain at least one successful page"
        )
    _validate_content_hash(wire, "AlphaEngineDocumentAcquisitionManifest")
    return wire


class AlphaEngineDocumentAcquisitionCoordinator:
    """Assemble a document only from exact, immutable page authority."""

    def __init__(
        self,
        *,
        plan: Mapping[str, Any],
        page_port: AlphaEngineDocumentPagePort,
        authority_reader: Any,
        spool: RawSpool,
    ) -> None:
        self.plan = validate_alphaengine_document_acquisition_plan(plan)
        execute_page = getattr(page_port, "execute_page", None)
        if not callable(execute_page):
            raise TypeError("page_port must expose execute_page")
        if type(spool) is not RawSpool:
            raise TypeError("document coordinator requires an exact RawSpool")
        required_reader_methods = (
            "get_invocation", "get_profile", "get_call_spec", "get_reservation",
            "get_physical_attempt", "get_usage_entry", "get_cost_entry",
            "get_quota_settlement", "get_source_envelope", "get_artifact_version",
        )
        if any(
            not callable(getattr(authority_reader, name, None))
            for name in required_reader_methods
        ):
            raise TypeError("authority_reader lacks immutable receipt methods")
        self.page_port = page_port
        self.authority = authority_reader
        self.spool = spool

    def _page_request(
        self, ordinal: int, cursor: str | None, expected_offset: int
    ) -> dict[str, Any]:
        identity = {
            "plan_hash": self.plan["content_hash"],
            "page_ordinal": ordinal,
            "request_cursor": cursor,
        }
        base = {
            "schema_version": PAGE_REQUEST_SCHEMA_VERSION,
            "id": "alphaengine-document-page:" + content_hash(identity),
            "created_at": self.plan["created_at"],
            "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"],
            "document_ref": self.plan["document_ref"],
            "page_ordinal": ordinal,
            "request_cursor": cursor,
            "expected_offset": expected_offset,
            "max_response_bytes": self.plan["page_max_response_bytes"],
        }
        return validate_alphaengine_document_page_request(
            {**base, "content_hash": content_hash(base)}
        )

    @staticmethod
    def _fresh_response_hash(response: Mapping[str, Any]) -> str:
        """Normalize duplicate delivery to the original immutable response hash."""

        base = {
            key: value for key, value in response.items()
            if key != "content_hash"
        }
        base["idempotency_status"] = "fresh"
        return content_hash(base)

    @staticmethod
    def _require_record(
        value: Any,
        *,
        expected_ref: str,
        expected_hash: str,
        name: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(value, Mapping)
            or value.get("id") != expected_ref
            or value.get("content_hash") != expected_hash
        ):
            raise AlphaEngineDocumentAcquisitionError(
                f"{name} differs from immutable authority"
            )
        return dict(value)

    def _quota_projection(
        self, settlement: Mapping[str, Any], reservation: Mapping[str, Any]
    ) -> int:
        if settlement["state"] == "released":
            return 0
        actual = int(settlement["actual"]["records"])
        if settlement["state"] == "indeterminate":
            return max(actual, int(reservation["reserved"]["records"]))
        return actual

    def _page_call_authority(
        self, request: Mapping[str, Any], response: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        invocation = self._require_record(
            self.authority.get_invocation(response["connector_invocation_ref"]),
            expected_ref=response["connector_invocation_ref"],
            expected_hash=response["connector_invocation_hash"],
            name="connector invocation",
        )
        profile = self._require_record(
            self.authority.get_profile(invocation["connector_profile_ref"]),
            expected_ref=invocation["connector_profile_ref"],
            expected_hash=invocation["connector_profile_hash"],
            name="connector profile",
        )
        call = self._require_record(
            self.authority.get_call_spec(invocation["call_spec_ref"]),
            expected_ref=invocation["call_spec_ref"],
            expected_hash=invocation["call_spec_hash"],
            name="connector call",
        )
        expected_parameters = {"document_ref": self.plan["document_ref"]}
        if request["request_cursor"] is not None:
            expected_parameters["cursor"] = request["request_cursor"]
        expected_query_hash = content_hash(
            {"operation": "get_document", "parameters": expected_parameters}
        )
        if (
            profile["source_identity"]["source_ref"] != self.plan["source_ref"]
            or profile["source_hash"] != self.plan["source_hash"]
            or profile["pagination"] != {
                "mode": "cursor", "cursor_field": "cursor", "max_pages": 20,
            }
            or int(profile["max_response_bytes"])
            != int(request["max_response_bytes"])
            or call["connector_profile_ref"] != profile["id"]
            or call["operation"] != "get_document"
            or call["parameters"] != expected_parameters
            or call["query_hash"] != expected_query_hash
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "page call differs from acquisition plan"
            )
        return invocation, profile, call

    def _failed_page(
        self, request: Mapping[str, Any], response: Mapping[str, Any]
    ) -> dict[str, Any]:
        invocation, _, _ = self._page_call_authority(request, response)
        attempt = self._require_record(
            self.authority.get_physical_attempt(response["physical_attempt_ref"]),
            expected_ref=response["physical_attempt_ref"],
            expected_hash=response["physical_attempt_hash"],
            name="failed physical attempt",
        )
        usage = self._require_record(
            self.authority.get_usage_entry(response["usage_entry_ref"]),
            expected_ref=response["usage_entry_ref"],
            expected_hash=response["usage_entry_hash"],
            name="failed usage",
        )
        cost = self._require_record(
            self.authority.get_cost_entry(response["cost_entry_ref"]),
            expected_ref=response["cost_entry_ref"],
            expected_hash=response["cost_entry_hash"],
            name="failed cost",
        )
        settlement = self._require_record(
            self.authority.get_quota_settlement(response["quota_settlement_ref"]),
            expected_ref=response["quota_settlement_ref"],
            expected_hash=response["quota_settlement_hash"],
            name="failed settlement",
        )
        reservation = self.authority.get_reservation(settlement["reservation_ref"])
        if not isinstance(reservation, Mapping):
            raise AlphaEngineDocumentAcquisitionError(
                "failed page reservation authority is missing"
            )
        projected_units = self._quota_projection(settlement, reservation)
        if (
            response["raw_artifact_version_ref"] is not None
            or response["source_envelope_ref"] is not None
            or attempt["outcome"] == "succeeded"
            or attempt["connector_invocation_ref"] != invocation["id"]
            or usage["physical_attempt_ref"] != attempt["id"]
            or cost["usage_entry_ref"] != usage["id"]
            or settlement["usage_entry_ref"] != usage["id"]
            or settlement["cost_entry_ref"] != cost["id"]
            or int(usage["metrics"]["calls"]) != 1
            or int(usage["metrics"]["bytes"]) != 0
            or int(usage["metrics"]["records"]) != 0
            or projected_units not in ({0, 1} if request["page_ordinal"] == 1 else {0})
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "failed page fabricated success authority"
            )
        base = {
            "page_ordinal": request["page_ordinal"],
            "page_request_ref": request["id"],
            "page_request_hash": request["content_hash"],
            "runner_response_ref": response["id"],
            "runner_response_hash": self._fresh_response_hash(response),
            "outcome": response["outcome"],
            "retry_at": response["retry_at"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "usage_entry_ref": usage["id"],
            "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"],
            "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "document_quota_units": projected_units,
        }
        return _validate_failed_page(
            {**base, "content_hash": content_hash(base)}
        )

    def _successful_page(
        self, request: Mapping[str, Any], response: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        invocation, profile, call = self._page_call_authority(request, response)
        attempt = self._require_record(
            self.authority.get_physical_attempt(response["physical_attempt_ref"]),
            expected_ref=response["physical_attempt_ref"],
            expected_hash=response["physical_attempt_hash"],
            name="physical attempt",
        )
        usage = self._require_record(
            self.authority.get_usage_entry(response["usage_entry_ref"]),
            expected_ref=response["usage_entry_ref"],
            expected_hash=response["usage_entry_hash"],
            name="usage entry",
        )
        cost = self._require_record(
            self.authority.get_cost_entry(response["cost_entry_ref"]),
            expected_ref=response["cost_entry_ref"],
            expected_hash=response["cost_entry_hash"],
            name="cost entry",
        )
        settlement = self._require_record(
            self.authority.get_quota_settlement(response["quota_settlement_ref"]),
            expected_ref=response["quota_settlement_ref"],
            expected_hash=response["quota_settlement_hash"],
            name="quota settlement",
        )
        reservation = self.authority.get_reservation(settlement["reservation_ref"])
        if not isinstance(reservation, Mapping):
            raise AlphaEngineDocumentAcquisitionError(
                "page reservation authority is missing"
            )
        artifact = self._require_record(
            self.authority.get_artifact_version(response["raw_artifact_version_ref"]),
            expected_ref=response["raw_artifact_version_ref"],
            expected_hash=response["raw_artifact_version_hash"],
            name="raw artifact",
        )
        source = self._require_record(
            self.authority.get_source_envelope(response["source_envelope_ref"]),
            expected_ref=response["source_envelope_ref"],
            expected_hash=response["source_envelope_hash"],
            name="source envelope",
        )
        raw = self.spool.read_object(artifact["artifact_content_hash"])
        raw_hash = hashlib.sha256(raw).hexdigest()
        max_chars = min(
            100_000, max(1, int(request["max_response_bytes"]) // 6)
        )
        provider_request_id, page = alphaengine_document_page_from_raw_response(
            raw,
            expected_doc_id=self.plan["document_id"],
            expected_offset=int(request["expected_offset"]),
            max_chars=max_chars,
        )
        expected_units = 1 if int(request["page_ordinal"]) == 1 else 0
        if (
            attempt["outcome"] != "succeeded"
            or attempt["connector_invocation_ref"] != invocation["id"]
            or usage["physical_attempt_ref"] != attempt["id"]
            or cost["usage_entry_ref"] != usage["id"]
            or settlement["state"] != "consumed"
            or settlement["usage_entry_ref"] != usage["id"]
            or settlement["cost_entry_ref"] != cost["id"]
            or settlement["actual"] != usage["metrics"]
            or int(usage["metrics"]["calls"]) != 1
            or int(usage["metrics"]["bytes"]) != len(raw)
            or int(usage["metrics"]["records"]) != expected_units
            or self._quota_projection(settlement, reservation) != expected_units
            or artifact["artifact_content_hash"] != raw_hash
            or int(artifact["size_bytes"]) != len(raw)
            or artifact["producer_execution_ref"] != invocation["execution_ref"]
            or source["connector_invocation_ref"] != invocation["id"]
            or source["connector_profile_ref"] != profile["id"]
            or source["physical_attempt_refs"] != [attempt["id"]]
            or source["result_physical_attempt_ref"] != attempt["id"]
            or source["source"] != self.plan["source_ref"]
            or source["operation"] != "get_document"
            or source["source_record_refs"] != [page["source_record_ref"]]
            or source["cursor"] != page["cursor"]
            or source["provider_request_id"] != provider_request_id
            or source["raw_artifact_version_ref"] != artifact["id"]
            or source["raw_response_hash"] != raw_hash
            or source["status"]
            != ("complete" if page["complete"] else "partial")
            or source["completeness"] != page["completeness"]
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "successful page differs from immutable authority"
            )
        base = {
            "page_ordinal": request["page_ordinal"],
            "page_request_ref": request["id"],
            "page_request_hash": request["content_hash"],
            "runner_response_ref": response["id"],
            "runner_response_hash": self._fresh_response_hash(response),
            "runner_request_ref": response["runner_request_ref"],
            "runner_request_hash": response["runner_request_hash"],
            "connector_invocation_ref": invocation["id"],
            "connector_invocation_hash": invocation["content_hash"],
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "call_spec_ref": call["id"],
            "call_spec_hash": call["content_hash"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "usage_entry_ref": usage["id"],
            "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"],
            "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "raw_artifact_version_ref": artifact["id"],
            "raw_artifact_version_hash": artifact["content_hash"],
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "request_cursor": request["request_cursor"],
            "next_cursor": page["cursor"],
            "offset": page["offset"],
            "returned_chars": page["returned_chars"],
            "raw_response_hash": raw_hash,
            "raw_response_bytes": len(raw),
            "document_quota_units": expected_units,
        }
        binding = _validate_page_binding(
            {**base, "content_hash": content_hash(base)},
            int(request["page_ordinal"]),
        )
        return binding, page

    def _assembled_object(self, text: str) -> RawObject:
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if self.spool.object_exists(digest):
            existing = self.spool.read_object(digest)
            if existing != data:
                raise AlphaEngineDocumentAcquisitionError(
                    "assembled document hash collision"
                )
            return RawObject(
                content_hash=digest,
                size_bytes=len(data),
                storage_locator=f"spool:objects/{digest[:2]}/{digest}",
            )
        sink_ref = "raw-sink:" + content_hash(
            {"plan_hash": self.plan["content_hash"], "assembled_hash": digest}
        )
        sink = self.spool.open_sink(
            sink_ref, max_response_bytes=max(1, len(data))
        )
        try:
            sink.write(data)
            return sink.finalize()
        except Exception:
            sink.abort()
            raise

    def _manifest(
        self,
        *,
        pages: list[dict[str, Any]],
        page_payloads: list[dict[str, Any]],
        failed_page: dict[str, Any] | None,
        status: str,
        termination_reason: str,
        next_cursor: str | None,
    ) -> dict[str, Any]:
        text = "".join(item["text"] for item in page_payloads)
        assembled = self._assembled_object(text).to_dict() if pages else None
        declared_chars = page_payloads[0]["content_chars"] if pages else None
        declared_hash = page_payloads[0]["content_sha256"] if pages else None
        prefix_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if pages else None
        if status == "complete" and (
            len(text) != declared_chars or prefix_hash != declared_hash
        ):
            raise AlphaEngineDocumentAcquisitionError(
                "terminal pages do not match declared full-document hash and length"
            )
        base = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "id": "alphaengine-document-acquisition:" + content_hash(
                {"plan_hash": self.plan["content_hash"]}
            ),
            "created_at": self.plan["created_at"],
            "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"],
            "document_ref": self.plan["document_ref"],
            "source_ref": self.plan["source_ref"],
            "bridge_ref": self.plan["bridge_ref"],
            "status": status,
            "termination_reason": termination_reason,
            "content_chars": declared_chars,
            "declared_content_sha256": declared_hash,
            "assembled_prefix_sha256": prefix_hash,
            "assembled_object": assembled,
            "pages": pages,
            "failed_page": failed_page,
            "next_cursor": next_cursor,
            "physical_calls": len(pages) + (1 if failed_page is not None else 0),
            "total_raw_response_bytes": sum(
                item["raw_response_bytes"] for item in pages
            ),
            "document_quota_units": sum(
                item["document_quota_units"] for item in pages
            ) + (
                0 if failed_page is None
                else failed_page["document_quota_units"]
            ),
        }
        return validate_alphaengine_document_acquisition_manifest(
            {**base, "content_hash": content_hash(base)}
        )

    def execute(self) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        payloads: list[dict[str, Any]] = []
        cursor: str | None = None
        expected_offset = 0
        raw_bytes = 0
        content_chars: int | None = None
        for ordinal in range(1, int(self.plan["max_pages"]) + 1):
            if (
                raw_bytes + int(self.plan["page_max_response_bytes"])
                > int(self.plan["max_total_response_bytes"])
            ):
                return self._manifest(
                    pages=pages, page_payloads=payloads, failed_page=None,
                    status="partial", termination_reason="max_response_bytes",
                    next_cursor=cursor,
                )
            request = self._page_request(ordinal, cursor, expected_offset)
            response = validate_connector_runner_response(
                self.page_port.execute_page(request)
            )
            if response["outcome"] != "succeeded":
                failed = self._failed_page(request, response)
                return self._manifest(
                    pages=pages, page_payloads=payloads, failed_page=failed,
                    status="partial" if pages else "failed",
                    termination_reason="page_failed", next_cursor=cursor,
                )
            binding, page = self._successful_page(request, response)
            if payloads and (
                page["content_chars"] != content_chars
                or page["content_sha256"] != payloads[0]["content_sha256"]
            ):
                raise AlphaEngineDocumentAcquisitionError(
                    "document identity changed between pages"
                )
            pages.append(binding)
            payloads.append(page)
            raw_bytes += int(binding["raw_response_bytes"])
            content_chars = int(page["content_chars"])
            cursor = page["cursor"]
            expected_offset = int(page["offset"]) + int(page["returned_chars"])
            if content_chars > int(self.plan["max_document_chars"]):
                return self._manifest(
                    pages=pages, page_payloads=payloads, failed_page=None,
                    status="partial", termination_reason="max_document_chars",
                    next_cursor=cursor,
                )
            if page["complete"]:
                return self._manifest(
                    pages=pages, page_payloads=payloads, failed_page=None,
                    status="complete", termination_reason="terminal",
                    next_cursor=None,
                )
        return self._manifest(
            pages=pages, page_payloads=payloads, failed_page=None,
            status="partial", termination_reason="max_pages",
            next_cursor=cursor,
        )


__all__ = [
    "AlphaEngineDocumentAcquisitionCoordinator",
    "AlphaEngineDocumentAcquisitionError",
    "AlphaEngineDocumentPagePort",
    "build_alphaengine_document_acquisition_plan",
    "validate_alphaengine_document_acquisition_manifest",
    "validate_alphaengine_document_acquisition_plan",
    "validate_alphaengine_document_page_request",
]
