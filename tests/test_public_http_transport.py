from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

from dalton_core.credential_authority import (
    CredentialGrantEnvelope,
    CredentialGrantRejected,
)
from dalton_core.public_http_transport import (
    PublicHttpRequest,
    PublicHttpTransport,
    PublicTransportPolicyError,
    PublicTransportResponseTooLarge,
)
from dalton_core.store import content_hash


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> None:
        self.data.extend(value)


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        *,
        headers: list[tuple[str, str]] | None = None,
        reason: str = "OK",
    ) -> None:
        self.status = status
        self.reason = reason
        self._body = body
        self._offset = 0
        self._headers = headers or []
        self.closed = False

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers)

    def read(self, amt: int | None = None) -> bytes:
        if self._offset >= len(self._body):
            return b""
        end = len(self._body) if amt is None else self._offset + amt
        result = self._body[self._offset:end]
        self._offset = end
        return result

    def close(self) -> None:
        self.closed = True


class PublicHttpTransportTests(unittest.TestCase):
    def test_success_pins_validated_ip_and_strips_sensitive_response_headers(self) -> None:
        calls = []

        def exchange(target, method, headers, body, timeout):
            calls.append((target, method, headers, body, timeout))
            return FakeResponse(
                200,
                b'{"ok":true}',
                headers=[
                    ("Content-Type", "application/json"),
                    ("Set-Cookie", "secret=value"),
                    ("Content-Length", "11"),
                ],
            )

        transport = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=exchange,
            chunk_size=4,
        )
        sink = Sink()
        result = transport.request(
            PublicHttpRequest(
                "GET", "https://example.com/a?q=public",
                {"Accept": "application/json"},
            ),
            sink,
            allowed_hosts=["example.com"],
            allow_redirects=False,
            max_redirects=0,
            max_response_bytes=100,
            timeout_seconds=2,
        )
        self.assertEqual(b'{"ok":true}', bytes(sink.data))
        self.assertEqual("93.184.216.34", calls[0][0].connect_ip)
        self.assertEqual("example.com", calls[0][0].hostname)
        self.assertEqual("/a?q=public", calls[0][0].request_target)
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(("93.184.216.34",), result.resolved_ips)
        self.assertEqual(b'{"ok":true}', result.body)

    def test_rejects_private_mixed_dns_and_never_calls_exchange(self) -> None:
        calls = []
        transport = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34", "127.0.0.1"],
            exchange=lambda *args: calls.append(args),
        )
        with self.assertRaises(PublicTransportPolicyError):
            transport.request(
                PublicHttpRequest("GET", "https://example.com/", {}),
                Sink(),
                allowed_hosts=["example.com"],
                allow_redirects=False,
                max_redirects=0,
                max_response_bytes=100,
                timeout_seconds=2,
            )
        self.assertEqual([], calls)
        for blocked in (
            "127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1",
            "fc00::1", "fe80::1", "::ffff:127.0.0.1",
        ):
            with self.subTest(blocked=blocked):
                local = PublicHttpTransport(
                    resolver=lambda host, port, value=blocked: [value],
                    exchange=lambda *args: calls.append(args),
                )
                with self.assertRaises(PublicTransportPolicyError):
                    local.request(
                        PublicHttpRequest("GET", "https://example.com/", {}),
                        Sink(),
                        allowed_hosts=["example.com"],
                        allow_redirects=False,
                        max_redirects=0,
                        max_response_bytes=100,
                        timeout_seconds=2,
                    )

    def test_redirect_revalidates_host_dns_and_limit(self) -> None:
        responses = [
            FakeResponse(302, headers=[("Location", "https://cdn.example.com/final")]),
            FakeResponse(200, b"done"),
        ]
        calls = []

        def resolver(host, port):
            return ["93.184.216.34" if host == "example.com" else "104.16.1.1"]

        def exchange(target, *args):
            calls.append(target)
            return responses.pop(0)

        result = PublicHttpTransport(resolver=resolver, exchange=exchange).request(
            PublicHttpRequest("GET", "https://example.com/start", {}),
            Sink(),
            allowed_hosts=["example.com", "cdn.example.com"],
            allow_redirects=True,
            max_redirects=1,
            max_response_bytes=100,
            timeout_seconds=2,
        )
        self.assertEqual("https://cdn.example.com/final", result.final_url)
        self.assertEqual(["example.com", "cdn.example.com"], [call.hostname for call in calls])

        escape = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=lambda *args: FakeResponse(
                302, headers=[("Location", "https://evil.example/private")]
            ),
        )
        with self.assertRaises(PublicTransportPolicyError):
            escape.request(
                PublicHttpRequest("GET", "https://example.com/start", {}),
                Sink(),
                allowed_hosts=["example.com"],
                allow_redirects=True,
                max_redirects=1,
                max_response_bytes=100,
                timeout_seconds=2,
            )

    def test_rejects_credential_channels_url_tricks_and_non_idempotent_redirects(self) -> None:
        transport = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=lambda *args: FakeResponse(200),
        )
        base = dict(
            raw_sink=Sink(), allowed_hosts=["example.com"], allow_redirects=False,
            max_redirects=0, max_response_bytes=100, timeout_seconds=2,
        )
        hostile_requests = [
            PublicHttpRequest("GET", "http://example.com/", {}),
            PublicHttpRequest("GET", "https://user:pass@example.com/", {}),
            PublicHttpRequest("GET", "https://example.com:8443/", {}),
            PublicHttpRequest("GET", "https://example.com/#fragment", {}),
            PublicHttpRequest("GET", "https://example.com/?api_key=secret", {}),
            PublicHttpRequest("GET", "https://example.com/?signature=secret", {}),
            PublicHttpRequest("GET", "https://example.com/?auth%5Btoken%5D=secret", {}),
            PublicHttpRequest("GET", "https://example.com/", {"Authorization": "Bearer x"}),
            PublicHttpRequest("GET", "https://example.com/", {"Cookie": "a=b"}),
            PublicHttpRequest("GET", "https://example.com/", {"X-Custom": "value"}),
            PublicHttpRequest(
                "POST", "https://example.com/", {"Content-Type": "application/json"},
                b'{"api_key":"secret"}',
            ),
            PublicHttpRequest(
                "POST", "https://example.com/",
                {"Content-Type": "application/x-www-form-urlencoded"},
                b"token=secret",
            ),
            PublicHttpRequest(
                "POST", "https://example.com/",
                {"Content-Type": "application/x-www-form-urlencoded"},
                b"auth%5Bapi-key%5D=secret",
            ),
        ]
        for request in hostile_requests:
            with self.subTest(request=request), self.assertRaises(PublicTransportPolicyError):
                transport.request(request, base.pop("raw_sink", Sink()), **base)

        safe_post = transport.request(
            PublicHttpRequest(
                "POST", "https://example.com/",
                {"Content-Type": "application/x-www-form-urlencoded"},
                b"stock_code=600309&page=1",
            ),
            Sink(),
            allowed_hosts=["example.com"], allow_redirects=False, max_redirects=0,
            max_response_bytes=100, timeout_seconds=2,
        )
        self.assertEqual(200, safe_post.status)

        redirecting = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=lambda *args: FakeResponse(307, headers=[("Location", "/again")]),
        )
        with self.assertRaises(PublicTransportPolicyError):
            redirecting.request(
                PublicHttpRequest("POST", "https://example.com/start", {}, b"{}"),
                Sink(),
                allowed_hosts=["example.com"], allow_redirects=True, max_redirects=1,
                max_response_bytes=100, timeout_seconds=2,
            )

    def test_response_limits_and_ambiguous_redirects_fail_closed(self) -> None:
        length = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=lambda *args: FakeResponse(
                200, b"x" * 20, headers=[("Content-Length", "20")]
            ),
        )
        with self.assertRaises(PublicTransportResponseTooLarge):
            length.request(
                PublicHttpRequest("GET", "https://example.com/", {}), Sink(),
                allowed_hosts=["example.com"], allow_redirects=False, max_redirects=0,
                max_response_bytes=10, timeout_seconds=2,
            )
        streamed = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=lambda *args: FakeResponse(200, b"x" * 20),
            chunk_size=4,
        )
        with self.assertRaises(PublicTransportResponseTooLarge):
            streamed.request(
                PublicHttpRequest("GET", "https://example.com/", {}), Sink(),
                allowed_hosts=["example.com"], allow_redirects=False, max_redirects=0,
                max_response_bytes=10, timeout_seconds=2,
            )
        ambiguous = PublicHttpTransport(
            resolver=lambda host, port: ["93.184.216.34"],
            exchange=lambda *args: FakeResponse(
                302,
                headers=[("Location", "/one"), ("Location", "/two")],
            ),
        )
        with self.assertRaises(PublicTransportPolicyError):
            ambiguous.request(
                PublicHttpRequest("GET", "https://example.com/", {}), Sink(),
                allowed_hosts=["example.com"], allow_redirects=True, max_redirects=1,
                max_response_bytes=10, timeout_seconds=2,
            )


class CredentialBoundaryTests(unittest.TestCase):
    def grant(self) -> dict:
        wire = {
            "schema_version": "0.1",
            "id": "credential-grant:alphaengine:one",
            "created_at": "2026-08-14T18:00:00.000000+00:00",
            "expires_at": "2026-08-14T18:05:00.000000+00:00",
            "authority_ref": "credential-authority:openclaw",
            "grant_kind": "mcp_managed",
            "target_ref": "mcp-server:alphaengine",
            "connector_profile_ref": "connector-profile:alphaengine:v1",
            "connector_profile_hash": "1" * 64,
            "capability_lease_ref": "lease:alphaengine",
            "capability_lease_hash": "2" * 64,
            "adapter_ref": "adapter:openclaw:mcp-call:0.1",
            "adapter_hash": "3" * 64,
            "principal_ref": "principal:connector-runner",
            "credential_slot_refs": ["credential-slot:alphaengine"],
            "allowed_operations": ["search_library"],
            "max_calls": 1,
        }
        wire["content_hash"] = content_hash(wire)
        return wire

    def test_grant_is_closed_opaque_and_not_accepted_by_public_transport(self) -> None:
        envelope = CredentialGrantEnvelope.from_dict(self.grant())
        self.assertEqual("mcp_managed", envelope.grant_kind)
        serialized = json.dumps(envelope.to_dict())
        for forbidden in ("Bearer ", "api_key", "password", "cookie"):
            self.assertNotIn(forbidden, serialized)
        parameters = inspect.signature(PublicHttpTransport.request).parameters
        self.assertNotIn("credential_grant", parameters)
        adapter_schema = json.loads(
            (CONTRACTS / "connector-adapter-request.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            None, adapter_schema["properties"]["credential_grant_ref"]["const"]
        )

        hostile = self.grant()
        hostile["secret"] = "must-not-enter"
        with self.assertRaises(CredentialGrantRejected):
            CredentialGrantEnvelope.from_dict(hostile)

    def test_credential_contract_is_closed(self) -> None:
        schema = json.loads(
            (CONTRACTS / "credential-grant-envelope.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(self.grant()))


if __name__ == "__main__":
    unittest.main()
