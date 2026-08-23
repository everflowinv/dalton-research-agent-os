from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import dalton_core.research_verification as research_verification
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_runner import RunnerConflict, RunnerValidationError
from dalton_core.openclaw_connector_bridge import HostToolInvocationResult
from dalton_core.public_http_transport import PublicHttpResponse
from dalton_core.public_web_connector import (
    GeminiWebSearchAdapter,
    OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
    OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF,
    PublicWebAuthorityConflict,
    PublicWebConnectorError,
    PublicWebFetchAdapter,
    PublicWebUrlAuthorityResolver,
    build_public_web_url_authorities,
    canonical_public_web_url,
    gemini_web_search_tool_arguments,
    public_web_url_ref,
    validate_gemini_search_parameters,
    validate_gemini_web_search_adapter_request,
)
from dalton_core.research_verification import (
    ResearchVerificationError,
    VerificationRejected,
    build_authority_source_material,
    build_candidate_evidence,
)
from dalton_core.store import canonical_json, content_hash


WHEN = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
FUTURE = "2030-01-01T00:00:00.000000+00:00"


def with_hash(value: dict) -> dict:
    return {**value, "content_hash": content_hash(value)}


class MemorySink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> None:
        self.data.extend(value)


def gemini_payload(*, citations: list[dict] | None = None) -> dict:
    return {
        "query": "Accenture AI demand",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "tookMs": 25,
        "externalContent": {
            "untrusted": True,
            "source": "web_search",
            "provider": "gemini",
            "wrapped": True,
        },
        "content": "UNTRUSTED ranked discovery synthesis",
        "citations": citations
        if citations is not None
        else [
            {"url": "https://Example.com/investors?q=ai#section", "title": "IR"},
            {"url": "https://example.com/investors?q=ai", "title": "duplicate"},
        ],
    }


def mcp_result(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": canonical_json(payload)}]}


def raw_result(payload: dict) -> bytes:
    return canonical_json(
        {"jsonrpc": "2.0", "id": "provider-request:gemini:1", "result": mcp_result(payload)}
    ).encode("utf-8")


class FakeHandle:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict, dict]] = []

    def invoke(self, tool_name, arguments, **kwargs):
        self.calls.append((tool_name, dict(arguments), dict(kwargs)))
        return HostToolInvocationResult(
            request_id="provider-request:gemini:1",
            raw_response=raw_result(self.payload),
            result=mcp_result(self.payload),
        )


def search_request(parameters: dict | None = None) -> dict:
    parameters = parameters or {
        "query": "Accenture AI demand",
        "date_after": "2026-08-01",
        "date_before": "2026-08-23",
    }
    template = load_packaged_connector_inventory()["templates"]["gemini-web-search"]
    operation = template["operations"][0]
    base = {
        "protocol_version": "0.1",
        "connector_invocation_ref": "connector-invocation:gemini:1",
        "reservation_ref": "connector-reservation:gemini:1",
        "physical_attempt_number": 1,
        "source_identity": template["source_identity"],
        "source_hash": content_hash(template["source_identity"]),
        "operation": "search_web",
        "tool_name": "web_search",
        "parameters": parameters,
        "query_hash": content_hash(
            {"operation": "search_web", "parameters": parameters}
        ),
        "input_schema_ref": operation["input_schema_ref"],
        "input_schema_hash": operation["input_schema_hash"],
        "output_schema_ref": operation["output_schema_ref"],
        "output_schema_hash": operation["output_schema_hash"],
        "bridge_ref": OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF,
        "bridge_hash": OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
        "credential_use_ref": "credential-use:gemini:1",
        "deadline_at": FUTURE,
        "max_response_bytes": 1_000_000,
        "max_records": 10,
        "raw_sink_ref": "raw-sink:" + "a" * 64,
    }
    return with_hash(base)


def source_envelope(payload: dict) -> dict:
    refs = [public_web_url_ref(item["url"]) for item in payload["citations"]]
    refs = list(dict.fromkeys(refs))
    raw = raw_result(payload)
    base = {
        "id": "source-envelope:gemini:1",
        "source": "source:public-web",
        "operation": "search_web",
        "source_record_refs": refs,
        "completeness": "ranked",
        "status": "complete" if refs else "empty",
        "cursor": None,
        "retrieved_at": WHEN.isoformat(timespec="microseconds"),
        "raw_artifact_version_ref": "artifact-version:gemini:1",
        "raw_response_hash": hashlib.sha256(raw).hexdigest(),
    }
    return with_hash(base)


def fetch_request(url_ref: str, host: str, operation: str = "fetch_get") -> dict:
    parameters = {"url_ref": url_ref}
    base = {
        "protocol_version": "0.1",
        "runner_request_ref": "connector-runner-request:web-fetch:1",
        "runner_request_hash": "1" * 64,
        "connector_invocation_ref": "connector-invocation:web-fetch:1",
        "profile_ref": "connector-profile:web-fetch:1",
        "profile_hash": "2" * 64,
        "call_spec_ref": "connector-call:web-fetch:1",
        "call_spec_hash": "3" * 64,
        "reservation_ref": "connector-reservation:web-fetch:1",
        "reservation_hash": "4" * 64,
        "physical_attempt_number": 1,
        "source_identity": {
            "source_ref": "source:public-web",
            "source_type": "public_web",
            "source_version": "inventory-2026-08-14",
        },
        "source_hash": content_hash(
            {
                "source_ref": "source:public-web",
                "source_type": "public_web",
                "source_version": "inventory-2026-08-14",
            }
        ),
        "adapter_ref": "transport:public-http:0.1",
        "adapter_hash": "5" * 64,
        "resolver_ref": "resolver:public-web:0.1",
        "resolver_manifest_hash": "6" * 64,
        "operation": operation,
        "parameters": parameters,
        "query_hash": content_hash(
            {"operation": operation, "parameters": parameters}
        ),
        "input_schema_ref": f"schema:web-fetch:{operation}:input:0.1",
        "input_schema_hash": "7" * 64,
        "output_schema_ref": f"schema:web-fetch:{operation}:output:0.1",
        "output_schema_hash": "8" * 64,
        "allowed_hosts": [host],
        "network_policy": {
            "allowed_schemes": ["https"],
            "allow_redirects": True,
            "max_redirects": 2,
            "resolve_public_only": True,
        },
        "credential_grant_ref": None,
        "deadline_at": FUTURE,
        "max_response_bytes": 1_000_000,
        "max_records": 1,
        "raw_sink_ref": "raw-sink:" + "b" * 64,
    }
    return with_hash(base)


class FakePublicTransport:
    def __init__(self, *, status: int = 200, headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.calls: list[tuple] = []

    def request(self, request, raw_sink, **kwargs):
        self.calls.append((request, dict(kwargs)))
        body = b"<html><body>original source</body></html>" if request.method == "GET" else b""
        raw_sink.write(body)
        return PublicHttpResponse(
            status=self.status,
            reason="OK",
            headers=self.headers,
            final_url=request.url,
            redirect_chain=(),
            bytes_written=len(body),
            resolved_ips=("93.184.216.34",),
            body=body,
        )


class PublicWebConnectorTests(unittest.TestCase):
    def test_inventory_matches_current_openclaw_gemini_filter_contract(self) -> None:
        template = load_packaged_connector_inventory()["templates"]["gemini-web-search"]
        operation = template["operations"][0]
        schema = next(
            item["document"]
            for item in template["schema_documents"]
            if item["schema_ref"] == operation["input_schema_ref"]
        )
        self.assertEqual(operation["pagination"]["mode"], "none")
        self.assertEqual(schema["required"], ["query"])
        self.assertNotIn("cursor", schema["properties"])
        self.assertEqual(
            schema["properties"]["freshness"]["enum"],
            ["day", "week", "month", "year"],
        )

    def test_gemini_time_filters_are_mutually_exclusive(self) -> None:
        self.assertEqual(
            validate_gemini_search_parameters(
                {"query": "q", "freshness": "week"}
            ),
            {"query": "q", "freshness": "week"},
        )
        self.assertEqual(
            validate_gemini_search_parameters(
                {"query": "q", "date_after": "2026-08-01"}
            )["date_after"],
            "2026-08-01",
        )
        with self.assertRaises(RunnerValidationError):
            validate_gemini_search_parameters(
                {
                    "query": "q",
                    "date_after": "2026-08-01",
                    "freshness": "day",
                }
            )
        with self.assertRaises(RunnerValidationError):
            validate_gemini_search_parameters(
                {
                    "query": "q",
                    "date_after": "2026-08-23",
                    "date_before": "2026-08-01",
                }
            )

    def test_search_adapter_preserves_raw_result_and_emits_only_url_refs(self) -> None:
        request = validate_gemini_web_search_adapter_request(search_request())
        sink = MemorySink()
        handle = FakeHandle(gemini_payload())
        observation = GeminiWebSearchAdapter()(request, sink, handle)
        expected_ref = public_web_url_ref("https://example.com/investors?q=ai")
        self.assertEqual(observation["outcome"], "succeeded")
        self.assertEqual(observation["source_record_refs"], [expected_ref])
        self.assertEqual(
            observation["structured_output"],
            {
                "source_record_refs": [expected_ref],
                "next_cursor": None,
                "provider_status": 200,
            },
        )
        self.assertEqual(bytes(sink.data), raw_result(gemini_payload()))
        self.assertEqual(handle.calls[0][0], "web_search")
        self.assertEqual(
            handle.calls[0][1],
            {
                "query": "Accenture AI demand",
                "count": 10,
                "date_after": "2026-08-01",
                "date_before": "2026-08-23",
            },
        )
        self.assertNotIn(
            "content", canonical_json(observation["structured_output"])
        )

    def test_search_request_fails_closed_on_schema_or_bridge_drift(self) -> None:
        mixed = search_request(
            {
                "query": "Accenture AI demand",
                "date_after": "2026-08-01",
                "freshness": "day",
            }
        )
        with self.assertRaises(RunnerValidationError):
            validate_gemini_web_search_adapter_request(mixed)
        drifted = search_request()
        drifted["tool_name"] = "web_fetch"
        drifted["content_hash"] = content_hash(
            {key: value for key, value in drifted.items() if key != "content_hash"}
        )
        with self.assertRaises(RunnerConflict):
            validate_gemini_web_search_adapter_request(drifted)

    def test_public_url_canonicalization_rejects_non_fetchable_discovery(self) -> None:
        self.assertEqual(
            canonical_public_web_url("https://Example.com:443/a#fragment"),
            "https://example.com/a",
        )
        for value in (
            "http://example.com/a",
            "https://user@example.com/a",
            "https://127.0.0.1/a",
            "https://example.com/a?access_token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(PublicWebConnectorError):
                canonical_public_web_url(value)

    def test_url_authority_is_rebuilt_from_exact_search_artifact(self) -> None:
        payload = gemini_payload()
        source = source_envelope(payload)
        authorities = build_public_web_url_authorities(raw_result(payload), source)
        self.assertEqual(len(authorities), 1)
        authority = authorities[0]
        self.assertEqual(authority["host"], "example.com")
        self.assertEqual(
            authority["canonical_url"], "https://example.com/investors?q=ai"
        )
        tampered = copy.deepcopy(source)
        tampered["raw_response_hash"] = "0" * 64
        tampered["content_hash"] = content_hash(
            {key: value for key, value in tampered.items() if key != "content_hash"}
        )
        with self.assertRaises(PublicWebAuthorityConflict):
            build_public_web_url_authorities(raw_result(payload), tampered)
        oversized = gemini_payload(
            citations=[
                {"url": f"https://example.com/source/{index}"}
                for index in range(11)
            ]
        )
        with self.assertRaisesRegex(
            RunnerValidationError, "exceeded max_records"
        ):
            build_public_web_url_authorities(
                raw_result(oversized), source_envelope(oversized)
            )

    def test_fetch_adapter_uses_only_authorized_url_and_original_bytes(self) -> None:
        payload = gemini_payload()
        authority = build_public_web_url_authorities(
            raw_result(payload), source_envelope(payload)
        )[0]
        resolver = PublicWebUrlAuthorityResolver([authority])
        transport = FakePublicTransport()
        adapter = PublicWebFetchAdapter(
            url_authority_resolver=resolver,
            transport=transport,
            clock=lambda: WHEN,
        )
        sink = MemorySink()
        observation = adapter(
            fetch_request(authority["url_ref"], "example.com"), sink
        )
        self.assertEqual(observation["outcome"], "succeeded")
        self.assertTrue(
            observation["source_record_refs"][0].startswith(
                "public-web-document:url-sha256:"
            )
        )
        self.assertEqual(
            observation["provider_usage"]["raw_media_type"], "text/html"
        )
        self.assertEqual(
            bytes(sink.data), b"<html><body>original source</body></html>"
        )
        request, kwargs = transport.calls[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url, authority["canonical_url"])
        self.assertEqual(kwargs["allowed_hosts"], ["example.com"])

    def test_fetch_adapter_rejects_unbound_ref_or_host(self) -> None:
        payload = gemini_payload()
        authority = build_public_web_url_authorities(
            raw_result(payload), source_envelope(payload)
        )[0]
        adapter = PublicWebFetchAdapter(
            url_authority_resolver=PublicWebUrlAuthorityResolver([authority]),
            transport=FakePublicTransport(),
            clock=lambda: WHEN,
        )
        with self.assertRaises(PublicWebAuthorityConflict):
            adapter(
                fetch_request("public-web-url:sha256:" + "0" * 64, "example.com"),
                MemorySink(),
            )
        with self.assertRaises(RunnerConflict):
            adapter(
                fetch_request(authority["url_ref"], "other.example"),
                MemorySink(),
            )

    def test_only_fetched_original_can_form_public_web_evidence_material(self) -> None:
        summary = {
            "id": "authority-resolution:public-web:1",
            "content_hash": "1" * 64,
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "source_envelope_ref": "source-envelope:public-web:1",
            "source_envelope_hash": "2" * 64,
            "artifact_ref": "artifact-version:public-web:1",
            "artifact_hash": "3" * 64,
            "source_ref": "source:public-web",
            "operation": "search_web",
            "source_record_refs": ["public-web-url:sha256:" + "4" * 64],
            "source_schema_hash": "5" * 64,
            "source_content_hash": "6" * 64,
            "published_at": None,
            "updated_at": None,
            "as_of": None,
            "retrieved_at": WHEN.isoformat(timespec="microseconds"),
            "completeness": "ranked",
            "status": "complete",
        }
        records = {
            "profile": {
                "source_identity": {
                    "source_ref": "source:public-web",
                    "source_type": "public_web",
                    "source_version": "inventory-2026-08-14",
                }
            },
            "observation": {
                "structured_output": {
                    "source_record_refs": summary["source_record_refs"],
                    "next_cursor": None,
                    "provider_status": 200,
                },
                "cursor": None,
            },
        }
        with self.assertRaisesRegex(
            ResearchVerificationError, "fetch_get the original source first"
        ):
            build_authority_source_material(
                SimpleNamespace(summary=summary, records=records)
            )
        fetched_summary = {
            **summary,
            "operation": "fetch_get",
            "completeness": "partial",
            "source_record_refs": [
                "public-web-document:url-sha256:"
                + "7" * 64
                + ":body-sha256:"
                + "8" * 64
            ],
        }
        fetched_records = copy.deepcopy(records)
        fetched_records["observation"]["structured_output"][
            "source_record_refs"
        ] = fetched_summary["source_record_refs"]
        material = build_authority_source_material(
            SimpleNamespace(summary=fetched_summary, records=fetched_records)
        )
        self.assertEqual(material["operation"], "fetch_get")
        self.assertEqual(material["source_type"], "public_web")

        historical_search_material = {
            **material,
            "operation": "search_web",
        }
        historical_search_material["content_hash"] = content_hash(
            {
                key: value
                for key, value in historical_search_material.items()
                if key != "content_hash"
            }
        )
        verification_base = {
            "schema_version": "0.1",
            "id": "verification-bundle:public-web:historical-search",
            "created_at": WHEN.isoformat(timespec="microseconds"),
            "kind": "source",
            "subject_ref": historical_search_material["id"],
            "subject_hash": historical_search_material["content_hash"],
            "verdict": "pass",
            "checkpoint_ref": "research-checkpoint:historical-search",
            "checkpoint_hash": "9" * 64,
            "verifier_ref": research_verification._AUTHORITY_SOURCE_VERIFIER_REF,
            "verifier_hash": research_verification._AUTHORITY_SOURCE_VERIFIER_HASH,
            "findings": [],
        }
        historical_verification = with_hash(verification_base)
        with self.assertRaisesRegex(
            VerificationRejected, "fetch_get the original source first"
        ):
            build_candidate_evidence(
                historical_search_material,
                historical_verification,
                candidate_evidence_ref="candidate-evidence:historical-search",
                actor_ref="system:offline-verifier",
                created_at=WHEN.isoformat(timespec="microseconds"),
                verification_mode="connector_authority",
            )


if __name__ == "__main__":
    unittest.main()
