from __future__ import annotations

import copy
import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.connector_inventory import load_connector_proposal_package
from dalton_core.connector_runner import RunnerValidationError
from dalton_core.earnings_call_transcript_connector import (
    EarningsCallTranscriptConflict,
    EarningsCallTranscriptError,
    EarningsCallTranscriptFetchAdapter,
    build_earnings_call_transcript_projection,
    validate_earnings_call_transcript_projection,
)
from dalton_core.earnings_call_transcript_proposal import (
    build_earnings_call_transcript_proposal_package,
)
from dalton_core.public_http_transport import PublicHttpResponse
from dalton_core.public_web_connector import (
    PublicWebFetchAdapter,
    PublicWebUrlAuthorityResolver,
    canonical_public_web_url,
    public_web_url_ref,
    validate_public_web_url_authority,
)
from dalton_core.store import content_hash
from scripts.run_isolated_earnings_call_transcript_canary import run as run_canary


WHEN = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)
FUTURE = "2030-01-01T00:00:00.000000+00:00"
ISSUER = "company:sec-cik:0001467373"
URL = "https://investor.accenture.example/transcripts/ACN/2026/q3"
ROOT = Path(__file__).parents[1]


def with_hash(value: dict) -> dict:
    return {**value, "content_hash": content_hash(value)}


def transcript_html(
    *,
    company: str = "Accenture plc",
    ticker: str = "ACN",
    year: int = 2026,
    quarter: int = 3,
    qa: bool = True,
) -> bytes:
    paragraphs = [
        (
            f"Management paragraph {index}. {company} ({ticker}) discussed its "
            f"Q{quarter} fiscal {year} results, demand, bookings and delivery. "
            + "This sentence preserves the original prepared remarks context. " * 3
        )
        for index in range(1, 23)
    ]
    if qa:
        paragraphs.extend([
            "Operator Instructions. We will now begin the question-and-answer session.",
            "Your first question comes from the analyst covering IT services demand.",
        ])
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return (
        "<!doctype html><html><head>"
        f"<title>{company} ({ticker}) Q{quarter} {year} Earnings Call Transcript</title>"
        "</head><body>"
        f"<h1>{company} ({ticker}) Q{quarter} {year} Earnings Call Transcript</h1>"
        f"{body}</body></html>"
    ).encode("utf-8")


def parameters(**overrides) -> dict:
    value = {
        "url_ref": public_web_url_ref(URL),
        "issuer_ref": ISSUER,
        "ticker": "ACN",
        "company_name": "Accenture plc",
        "fiscal_year": 2026,
        "fiscal_quarter": 3,
        "source_role": "issuer_primary",
    }
    value.update(overrides)
    return value


def url_authority(url: str = URL) -> dict:
    canonical = canonical_public_web_url(url)
    url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    source_hash = "1" * 64
    identity = {
        "url_ref": "public-web-url:sha256:" + url_hash,
        "source_envelope_hash": source_hash,
    }
    return validate_public_web_url_authority(with_hash({
        "schema_version": "0.1",
        "id": "public-web-url-authority:" + content_hash(identity),
        "created_at": WHEN.isoformat(timespec="microseconds"),
        "url_ref": identity["url_ref"],
        "canonical_url": canonical,
        "url_hash": url_hash,
        "host": "investor.accenture.example",
        "discovery_source_envelope_ref": "source-envelope:gemini:transcript:1",
        "discovery_source_envelope_hash": source_hash,
        "discovery_raw_artifact_version_ref": "artifact-version:gemini:transcript:1",
        "search_record_ref": identity["url_ref"],
    }))


def fetch_request(operation: str = "fetch_get", **parameter_overrides) -> dict:
    params = parameters(**parameter_overrides)
    source_identity = {
        "source_ref": "source:company-earnings-call-transcript",
        "source_type": "public_web",
        "source_version": "proposal-2026-08-23",
    }
    base = {
        "protocol_version": "0.1",
        "runner_request_ref": "connector-runner-request:transcript:1",
        "runner_request_hash": "1" * 64,
        "connector_invocation_ref": "connector-invocation:transcript:1",
        "profile_ref": "connector-profile:transcript:1",
        "profile_hash": "2" * 64,
        "call_spec_ref": "connector-call:transcript:1",
        "call_spec_hash": "3" * 64,
        "reservation_ref": "connector-reservation:transcript:1",
        "reservation_hash": "4" * 64,
        "physical_attempt_number": 1,
        "source_identity": source_identity,
        "source_hash": content_hash(source_identity),
        "adapter_ref": "adapter:earnings-call-transcript-fetch:0.1",
        "adapter_hash": "5" * 64,
        "resolver_ref": "resolver:public-web:0.1",
        "resolver_manifest_hash": "6" * 64,
        "operation": operation,
        "parameters": params,
        "query_hash": content_hash({"operation": operation, "parameters": params}),
        "input_schema_ref": "schema:earnings-call-transcript:fetch_get:input:0.1",
        "input_schema_hash": "7" * 64,
        "output_schema_ref": "schema:earnings-call-transcript:fetch_get:output:0.1",
        "output_schema_hash": "8" * 64,
        "allowed_hosts": ["investor.accenture.example"],
        "network_policy": {
            "allowed_schemes": ["https"],
            "allow_redirects": True,
            "max_redirects": 2,
            "resolve_public_only": True,
        },
        "credential_grant_ref": None,
        "deadline_at": FUTURE,
        "max_response_bytes": 2_000_000,
        "max_records": 1,
        "raw_sink_ref": "raw-sink:" + "a" * 64,
    }
    return with_hash(base)


class MemorySink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> None:
        self.data.extend(value)


class FakeTransport:
    def __init__(self, body: bytes | None = None) -> None:
        self.body = body if body is not None else transcript_html()
        self.calls = []

    def request(self, request, raw_sink, **kwargs):
        self.calls.append((request, dict(kwargs)))
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


class EarningsCallTranscriptConnectorTests(unittest.TestCase):
    def adapter(self, body: bytes | None = None) -> EarningsCallTranscriptFetchAdapter:
        return EarningsCallTranscriptFetchAdapter(
            url_authority_resolver=PublicWebUrlAuthorityResolver([url_authority()]),
            approved_issuer_hosts={ISSUER: ["investor.accenture.example"]},
            approved_third_party_hosts=["www.roic.ai"],
            transport=FakeTransport(body),
            clock=lambda: WHEN,
        )

    def test_projection_binds_exact_body_url_company_period_and_parser(self) -> None:
        body = transcript_html()
        projection = build_earnings_call_transcript_projection(
            body, canonical_url=URL, parameters=parameters()
        )
        self.assertEqual(projection["ticker"], "ACN")
        self.assertEqual(projection["fiscal_year"], 2026)
        self.assertEqual(projection["fiscal_quarter"], 3)
        self.assertGreaterEqual(projection["paragraph_count"], 20)
        self.assertEqual(projection["raw_body_hash"], hashlib.sha256(body).hexdigest())
        self.assertEqual(
            validate_earnings_call_transcript_projection(projection), projection
        )

        tampered = copy.deepcopy(projection)
        tampered["raw_body_hash"] = "0" * 64
        tampered["content_hash"] = content_hash({
            key: value for key, value in tampered.items() if key != "content_hash"
        })
        with self.assertRaises(EarningsCallTranscriptConflict):
            validate_earnings_call_transcript_projection(tampered)

    def test_proposal_package_is_exact_and_cannot_grant_live_execution(self) -> None:
        packaged = load_connector_proposal_package(
            ROOT / "deploy" / "connector-proposals" / "earnings-call-transcript"
        )
        self.assertEqual(
            packaged, build_earnings_call_transcript_proposal_package()
        )
        self.assertEqual(packaged["proposal"]["inventory_state"], "proposal_only")
        self.assertFalse(packaged["profile"]["readiness"]["lease_eligible"])
        self.assertFalse(
            packaged["profile"]["readiness"]["live_execution_allowed"]
        )
        self.assertEqual(
            packaged["profile"]["transport"]["host_policy"],
            "per_call_authority",
        )

    def test_isolated_acn_canary_preserves_zero_authority_side_effects(self) -> None:
        result = run_canary()
        self.assertTrue(result["ok"])
        self.assertEqual(result["ticker"], "ACN")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["paid_model_calls"], 0)
        self.assertEqual(result["formal_evidence_written"], 0)
        self.assertFalse(result["lease_eligible"])
        self.assertFalse(result["live_execution_allowed"])

    def test_adapter_preserves_original_bytes_and_emits_bound_transcript_ref(self) -> None:
        body = transcript_html()
        adapter = self.adapter(body)
        sink = MemorySink()
        observation = adapter(fetch_request(), sink)
        self.assertEqual(observation["outcome"], "succeeded")
        record_ref = observation["source_record_refs"][0]
        self.assertTrue(record_ref.startswith(
            "earnings-call-transcript:issuer_primary:ticker:ACN:fy:2026:q:3:"
        ))
        self.assertIn(
            "body-sha256:" + hashlib.sha256(body).hexdigest(), record_ref
        )
        self.assertEqual(bytes(sink.data), body)

    def test_wrong_identity_period_paywall_or_missing_qa_fails_closed(self) -> None:
        cases = (
            (transcript_html(company="Other Services"), parameters()),
            (transcript_html(year=2025), parameters()),
            (transcript_html(quarter=2), parameters()),
            (transcript_html(qa=False), parameters()),
            (
                transcript_html() + b"<strong>Upgrade to Unlock</strong>",
                parameters(),
            ),
        )
        for body, params in cases:
            with self.subTest(body_hash=hashlib.sha256(body).hexdigest()):
                with self.assertRaises(EarningsCallTranscriptError):
                    build_earnings_call_transcript_projection(
                        body, canonical_url=URL, parameters=params
                    )

    def test_head_unapproved_host_and_role_relabel_are_rejected(self) -> None:
        with self.assertRaises(RunnerValidationError):
            self.adapter()(fetch_request("fetch_head"), MemorySink())

        adapter = EarningsCallTranscriptFetchAdapter(
            url_authority_resolver=PublicWebUrlAuthorityResolver([url_authority()]),
            approved_issuer_hosts={ISSUER: ["other.example"]},
            approved_third_party_hosts=["www.roic.ai"],
            transport=FakeTransport(),
            clock=lambda: WHEN,
        )
        with self.assertRaises(EarningsCallTranscriptConflict):
            adapter(fetch_request(), MemorySink())
        with self.assertRaises(EarningsCallTranscriptConflict):
            self.adapter()(fetch_request(source_role="third_party_transcript"), MemorySink())
        with self.assertRaises(RunnerValidationError):
            PublicWebFetchAdapter(
                url_authority_resolver=PublicWebUrlAuthorityResolver([url_authority()]),
                source_identity={
                    "source_ref": "source:fake-official",
                    "source_type": "official_filing",
                    "source_version": "forbidden",
                },
            )


if __name__ == "__main__":
    unittest.main()
