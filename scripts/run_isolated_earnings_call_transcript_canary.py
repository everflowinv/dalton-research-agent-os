#!/usr/bin/env python3
"""Run one zero-network ACN transcript acquisition canary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dalton_core.connector_inventory import load_connector_proposal_package
from dalton_core.earnings_call_transcript_connector import (
    EarningsCallTranscriptFetchAdapter,
    build_earnings_call_transcript_projection,
)
from dalton_core.public_http_transport import PublicHttpResponse
from dalton_core.public_web_connector import (
    PublicWebUrlAuthorityResolver,
    canonical_public_web_url,
    public_web_url_ref,
    validate_public_web_url_authority,
)
from dalton_core.store import content_hash


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deploy" / "connector-proposals" / "earnings-call-transcript"
WHEN = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)
URL = "https://investor.accenture.example/transcripts/ACN/2026/q3"
ISSUER = "company:sec-cik:0001467373"


def _with_hash(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "content_hash": content_hash(value)}


def _body() -> bytes:
    paragraphs = [
        (
            f"Management paragraph {index}. Accenture plc (ACN) discussed its Q3 "
            "fiscal 2026 results, demand, bookings and delivery. "
            + "This sentence preserves the original prepared remarks context. " * 3
        )
        for index in range(1, 23)
    ]
    paragraphs.extend([
        "Operator Instructions. We will now begin the question-and-answer session.",
        "Your first question comes from the analyst covering IT services demand.",
    ])
    return (
        "<!doctype html><html><head><title>Accenture plc (ACN) Q3 2026 "
        "Earnings Call Transcript</title></head><body><h1>Accenture plc (ACN) "
        "Q3 2026 Earnings Call Transcript</h1>"
        + "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
        + "</body></html>"
    ).encode("utf-8")


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> None:
        self.data.extend(value)


class _RecordedTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def request(self, request, raw_sink, **kwargs):
        raw_sink.write(self.body)
        return PublicHttpResponse(
            status=200,
            reason="OK",
            headers={"content-type": "text/html; charset=utf-8"},
            final_url=request.url,
            redirect_chain=(),
            bytes_written=len(self.body),
            resolved_ips=("93.184.216.34",),
            body=self.body,
        )


def _authority() -> dict[str, Any]:
    canonical = canonical_public_web_url(URL)
    url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    source_hash = "1" * 64
    identity = {
        "url_ref": "public-web-url:sha256:" + url_hash,
        "source_envelope_hash": source_hash,
    }
    return validate_public_web_url_authority(_with_hash({
        "schema_version": "0.1",
        "id": "public-web-url-authority:" + content_hash(identity),
        "created_at": WHEN.isoformat(timespec="microseconds"),
        "url_ref": identity["url_ref"],
        "canonical_url": canonical,
        "url_hash": url_hash,
        "host": "investor.accenture.example",
        "discovery_source_envelope_ref": "source-envelope:gemini:transcript-canary:1",
        "discovery_source_envelope_hash": source_hash,
        "discovery_raw_artifact_version_ref": "artifact-version:gemini:transcript-canary:1",
        "search_record_ref": identity["url_ref"],
    }))


def _request(parameters: dict[str, Any]) -> dict[str, Any]:
    source_identity = {
        "source_ref": "source:company-earnings-call-transcript",
        "source_type": "public_web",
        "source_version": "proposal-2026-08-23",
    }
    operation = "fetch_get"
    base = {
        "protocol_version": "0.1",
        "runner_request_ref": "connector-runner-request:transcript-canary:1",
        "runner_request_hash": "1" * 64,
        "connector_invocation_ref": "connector-invocation:transcript-canary:1",
        "profile_ref": "connector-profile:transcript-canary:1",
        "profile_hash": "2" * 64,
        "call_spec_ref": "connector-call:transcript-canary:1",
        "call_spec_hash": "3" * 64,
        "reservation_ref": "connector-reservation:transcript-canary:1",
        "reservation_hash": "4" * 64,
        "physical_attempt_number": 1,
        "source_identity": source_identity,
        "source_hash": content_hash(source_identity),
        "adapter_ref": "adapter:earnings-call-transcript-fetch:0.1",
        "adapter_hash": "5" * 64,
        "resolver_ref": "resolver:public-web:0.1",
        "resolver_manifest_hash": "6" * 64,
        "operation": operation,
        "parameters": parameters,
        "query_hash": content_hash({"operation": operation, "parameters": parameters}),
        "input_schema_ref": "schema:connector-proposal:earnings-call-transcript:fetch_get:input:0.1",
        "input_schema_hash": "7" * 64,
        "output_schema_ref": "schema:connector-proposal:earnings-call-transcript:fetch_get:output:0.1",
        "output_schema_hash": "8" * 64,
        "allowed_hosts": ["investor.accenture.example"],
        "network_policy": {
            "allowed_schemes": ["https"], "allow_redirects": True,
            "max_redirects": 2, "resolve_public_only": True,
        },
        "credential_grant_ref": None,
        "deadline_at": "2030-01-01T00:00:00.000000+00:00",
        "max_response_bytes": 2_000_000,
        "max_records": 1,
        "raw_sink_ref": "raw-sink:" + "a" * 64,
    }
    return _with_hash(base)


def run() -> dict[str, Any]:
    package = load_connector_proposal_package(PACKAGE)
    authority = _authority()
    parameters = {
        "url_ref": public_web_url_ref(URL),
        "issuer_ref": ISSUER,
        "ticker": "ACN",
        "company_name": "Accenture plc",
        "fiscal_year": 2026,
        "fiscal_quarter": 3,
        "source_role": "issuer_primary",
    }
    body = _body()
    projection = build_earnings_call_transcript_projection(
        body, canonical_url=URL, parameters=parameters
    )
    sink = _Sink()
    observation = EarningsCallTranscriptFetchAdapter(
        url_authority_resolver=PublicWebUrlAuthorityResolver([authority]),
        approved_issuer_hosts={ISSUER: ["investor.accenture.example"]},
        approved_third_party_hosts=["www.roic.ai"],
        transport=_RecordedTransport(body),
        clock=lambda: WHEN,
    )(_request(parameters), sink)
    if observation["outcome"] != "succeeded" or bytes(sink.data) != body:
        raise RuntimeError("isolated transcript canary did not preserve authority bytes")
    return {
        "ok": True,
        "connector_ref": package["profile"]["connector_ref"],
        "inventory_state": package["proposal"]["inventory_state"],
        "lease_eligible": package["profile"]["readiness"]["lease_eligible"],
        "live_execution_allowed": package["profile"]["readiness"]["live_execution_allowed"],
        "ticker": projection["ticker"],
        "fiscal_year": projection["fiscal_year"],
        "fiscal_quarter": projection["fiscal_quarter"],
        "raw_body_hash": projection["raw_body_hash"],
        "projection_hash": projection["content_hash"],
        "source_record_ref": observation["source_record_refs"][0],
        "network_calls": 0,
        "paid_model_calls": 0,
        "formal_evidence_written": 0,
    }


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
