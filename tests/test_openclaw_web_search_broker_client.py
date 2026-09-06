"""P9d-4d: the Dalton client for the host-owned OpenClaw web search broker."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from dalton_core.openclaw_connector_bridge import (
    BridgePermissionDenied,
    BridgeRateLimited,
    BridgeRequestRejected,
    BridgeResponseTooLarge,
)
from dalton_core.openclaw_web_search_broker_client import (
    WebSearchBrokerError,
    WebSearchBrokerHandle,
    load_broker_key,
    sign_request,
)
from dalton_core.public_web_connector import build_public_web_url_authorities, public_web_url_ref
from dalton_core.store import canonical_json, content_hash


ROOT = Path(__file__).resolve().parents[1]
BROKER_DIR = ROOT / "integrations" / "openclaw-web-search-broker"
KEY = "a" * 64
FUTURE = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="microseconds")
CITATIONS = [{"url": "https://Example.com/investors?q=ai#top", "title": "IR"},
             {"url": "https://news.example.org/demand", "title": "News"}]


def gemini_payload(query: str) -> dict:
    return {
        "query": query,
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "tookMs": 9,
        "externalContent": {"untrusted": True, "source": "web_search", "provider": "gemini", "wrapped": True},
        "content": "UNTRUSTED synthesis",
        "citations": CITATIONS,
    }


def seal(body: dict) -> dict:
    return {**body, "contentHash": content_hash(body)}


class FakeBroker:
    """A Unix-socket broker that verifies the client's HMAC exactly as the plugin does."""

    _counter = 0

    def __init__(self, root: Path, *, responder=None, key: str = KEY) -> None:
        FakeBroker._counter += 1
        self.path = str(root / f"broker-{FakeBroker._counter}.sock")
        self.key = key
        self.requests: list[dict] = []
        self.responder = responder or self._default_response
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        os.chmod(self.path, 0o600)
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            try:
                buffer = b""
                while not buffer.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                request = json.loads(buffer.decode("utf-8"))
                self.requests.append(request)
                connection.sendall((canonical_json(self._reply(request)) + "\n").encode("utf-8"))
            except Exception:  # a broken frame simply closes the connection
                pass
            finally:
                connection.close()

    def _reply(self, request: dict) -> dict:
        auth = dict(request.get("auth") or {})
        mac = auth.pop("mac", "")
        core = {k: v for k, v in request.items() if k != "auth"}
        expected = hmac.new(
            self.key.encode("utf-8"), canonical_json({**core, "auth": auth}).encode("utf-8"), sha256,
        ).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return seal({"schemaVersion": "0.1", "brokerVersion": "test", "runtimeVersion": "test",
                         "ok": False, "error": {"code": "AUTH_INVALID", "message": "bad mac"}})
        return self.responder(core)

    def _default_response(self, core: dict) -> dict:
        return seal({
            "schemaVersion": "0.1", "brokerVersion": "test", "runtimeVersion": "2026.9.1",
            "ok": True, "idempotencyStatus": "fresh", "callRef": core["callRef"],
            "requestHash": content_hash(core),
            "providerRequestId": "provider-request:web-search-broker:test",
            "result": {"content": [{"type": "text", "text": canonical_json(gemini_payload(core["query"]))}]},
        })

    def close(self) -> None:
        try:
            self.server.close()
        finally:
            Path(self.path).unlink(missing_ok=True)


def failure(code: str, message: str = "refused"):
    def responder(core: dict) -> dict:
        return seal({"schemaVersion": "0.1", "brokerVersion": "test", "runtimeVersion": "test",
                     "ok": False, "idempotencyStatus": "fresh", "callRef": core["callRef"],
                     "requestHash": content_hash(core), "error": {"code": code, "message": message}})
    return responder


class BrokerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key_path = self.root / "broker.key"
        self.key_path.write_text(KEY + "\n", encoding="utf-8")
        os.chmod(self.key_path, 0o600)

    def broker(self, **kwargs) -> FakeBroker:
        broker = FakeBroker(self.root, **kwargs)
        self.addCleanup(broker.close)
        return broker

    def handle(self, broker: FakeBroker, **kwargs) -> WebSearchBrokerHandle:
        return WebSearchBrokerHandle(
            socket_path=broker.path, auth_key_path=self.key_path, **kwargs,
        )

    def invoke(self, handle, **overrides):
        arguments = {"query": "Accenture AI demand", "count": 5, **overrides}
        return handle.invoke(
            "web_search", arguments,
            call_ref="credential-use:web-search:1", deadline_at=FUTURE, max_response_bytes=1_000_000,
        )

    def test_signed_request_is_closed_and_the_reply_frame_is_the_raw_artifact(self) -> None:
        broker = self.broker()
        result = self.invoke(self.handle(broker), date_after="2026-08-01", date_before="2026-09-06")
        self.assertEqual(result.request_id, "provider-request:web-search-broker:test")
        self.assertEqual(json.loads(result.result["content"][0]["text"]), gemini_payload("Accenture AI demand"))
        # The stored raw artifact is the exact reply frame and parses through
        # the frozen public-web URL authority rebuild.
        envelope = json.loads(result.raw_response.decode("utf-8"))
        self.assertEqual(envelope["ok"], True)
        self.assertFalse(result.raw_response.endswith(b"\n"))
        sent = broker.requests[0]
        self.assertEqual(set(sent) - {"auth"}, {
            "schemaVersion", "callRef", "profileId", "query", "count", "timeoutMs", "dateAfter", "dateBefore",
        })
        self.assertEqual((sent["query"], sent["count"], sent["dateAfter"], sent["dateBefore"]),
                         ("Accenture AI demand", 5, "2026-08-01", "2026-09-06"))
        self.assertEqual((sent["callRef"], sent["profileId"]), ("credential-use:web-search:1", "profile:web-search"))
        self.assertEqual(set(sent["auth"]), {"scheme", "clientId", "timestampMs", "nonce", "mac"})
        self.assertNotIn(KEY, json.dumps(sent))
        # Every call carries a fresh nonce.
        self.invoke(self.handle(broker))
        self.assertNotEqual(broker.requests[0]["auth"]["nonce"], broker.requests[1]["auth"]["nonce"])

    def test_broker_failures_map_to_bridge_errors(self) -> None:
        cases = {
            "PROVIDER_RATE_LIMITED": BridgeRateLimited,
            "PROVIDER_PERMISSION_DENIED": BridgePermissionDenied,
            "PROVIDER_CONTRACT_DRIFT": BridgeRequestRejected,
            "IDEMPOTENCY_CONFLICT": BridgeRequestRejected,
            "AUTH_INVALID": BridgeRequestRejected,
        }
        for code, expected in cases.items():
            broker = self.broker(responder=failure(code))
            with self.subTest(code=code), self.assertRaises(expected) as ctx:
                self.invoke(self.handle(broker))
            if expected is BridgeRateLimited:
                self.assertEqual(ctx.exception.retry_after_ms, 60_000)

    def test_wrong_key_and_unsafe_key_file_fail_closed(self) -> None:
        broker = self.broker(key="b" * 64)
        with self.assertRaises(BridgeRequestRejected):
            self.invoke(self.handle(broker))
        world_readable = self.root / "loose.key"
        world_readable.write_text(KEY + "\n", encoding="utf-8")
        os.chmod(world_readable, 0o644)
        with self.assertRaises(WebSearchBrokerError):
            load_broker_key(world_readable)
        with self.assertRaises(WebSearchBrokerError):
            WebSearchBrokerHandle(socket_path=broker.path, auth_key_path=world_readable)
        (self.root / "short.key").write_text("nope\n", encoding="utf-8")
        os.chmod(self.root / "short.key", 0o600)
        with self.assertRaises(WebSearchBrokerError):
            load_broker_key(self.root / "short.key")

    def test_client_side_refusals_never_open_the_socket(self) -> None:
        broker = self.broker()
        handle = self.handle(broker)
        with self.assertRaises(BridgeRequestRejected):
            handle.invoke("other_tool", {"query": "x", "count": 1}, call_ref="credential-use:a", deadline_at=FUTURE, max_response_bytes=1000)
        with self.assertRaises(BridgeRequestRejected):
            self.invoke(handle, freshness="week")
        with self.assertRaises(BridgeRequestRejected):
            self.invoke(handle, date_after="2026-08-01")
        with self.assertRaises(BridgeRequestRejected):
            self.invoke(handle, count=0)
        with self.assertRaises(BridgeRequestRejected):
            handle.invoke("web_search", {"query": "x", "count": 1}, call_ref="invocation:wrong",
                          deadline_at=FUTURE, max_response_bytes=1000)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="microseconds")
        with self.assertRaises(BridgeRequestRejected):
            handle.invoke("web_search", {"query": "x", "count": 1}, call_ref="credential-use:a",
                          deadline_at=past, max_response_bytes=1000)
        self.assertEqual(broker.requests, [])

    def test_oversized_reply_is_refused(self) -> None:
        def big(core: dict) -> dict:
            payload = gemini_payload(core["query"])
            payload["content"] = "x" * 5000
            return seal({"schemaVersion": "0.1", "brokerVersion": "t", "runtimeVersion": "t", "ok": True,
                         "idempotencyStatus": "fresh", "callRef": core["callRef"],
                         "requestHash": content_hash(core), "providerRequestId": "provider-request:x",
                         "result": {"content": [{"type": "text", "text": canonical_json(payload)}]}})
        broker = self.broker(responder=big)
        handle = self.handle(broker)
        with self.assertRaises(BridgeResponseTooLarge):
            handle.invoke("web_search", {"query": "q", "count": 1}, call_ref="credential-use:web-search:1",
                          deadline_at=FUTURE, max_response_bytes=1024)

    def test_malformed_replies_fail_closed(self) -> None:
        for responder in (
            lambda core: {"schemaVersion": "9.9", "ok": True},
            lambda core: seal({"schemaVersion": "0.1", "ok": True, "providerRequestId": "x"}),
            lambda core: seal({"schemaVersion": "0.1", "ok": True, "result": {"content": []}}),
        ):
            broker = self.broker(responder=responder)
            with self.subTest(responder=responder), self.assertRaises(WebSearchBrokerError):
                self.invoke(self.handle(broker))

    def test_reply_frame_rebuilds_public_web_url_authorities(self) -> None:
        """The stored frame must satisfy the frozen P9d-4b authority rebuild."""

        broker = self.broker()
        result = self.invoke(self.handle(broker))
        refs = [public_web_url_ref("https://example.com/investors?q=ai"),
                public_web_url_ref("https://news.example.org/demand")]
        source = {
            "id": "source-envelope:web-search:1", "source": "source:public-web", "operation": "search_web",
            "source_record_refs": refs, "completeness": "ranked", "status": "complete", "cursor": None,
            "retrieved_at": FUTURE, "raw_artifact_version_ref": "artifact-version:1",
            "raw_response_hash": sha256(result.raw_response).hexdigest(),
        }
        source["content_hash"] = content_hash(source)
        authorities = build_public_web_url_authorities(result.raw_response, source)
        self.assertEqual([item["url_ref"] for item in authorities], refs)
        self.assertEqual([item["host"] for item in authorities], ["example.com", "news.example.org"])


@unittest.skipUnless(shutil.which("node"), "node is required for the cross-language broker check")
class NodeBrokerRoundTripTests(unittest.TestCase):
    """Sign in Python, verify and answer in the real Node broker."""

    def test_python_client_and_node_broker_agree_on_the_wire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = root / "harness.mjs"
            harness.write_text(f'''
import {{ WebSearchBroker }} from "{BROKER_DIR}/src/broker.mjs";
import {{ BrokerServer }} from "{BROKER_DIR}/src/server.mjs";
const payload = {{
  query: "Accenture AI demand", provider: "gemini", model: "gemini-2.5-flash", tookMs: 3,
  externalContent: {{ untrusted: true, source: "web_search", provider: "gemini", wrapped: true }},
  content: "UNTRUSTED synthesis",
  citations: [{{ url: "https://Example.com/investors?q=ai#top", title: "IR" }}],
}};
const runtime = {{ version: "2026.9.1", webSearch: {{ async search() {{ return payload; }} }} }};
const broker = new WebSearchBroker(runtime, {{ clientId: "client:dalton-core", expectedProvider: "gemini", socketName: "broker.sock" }}, {{ hostConfig: {{}} }});
const server = new BrokerServer(broker);
const socketPath = await server.start("{root}");
process.stdout.write(socketPath + "\\n");
process.on("SIGTERM", async () => {{ await server.stop(); process.exit(0); }});
''', encoding="utf-8")
            process = subprocess.Popen(
                ["node", str(harness)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                socket_path = (process.stdout.readline() or "").strip()
                self.assertTrue(socket_path, process.stderr.read() if process.poll() else "broker did not start")
                key_path = root / "broker.sock.key"
                handle = WebSearchBrokerHandle(socket_path=socket_path, auth_key_path=key_path)
                result = handle.invoke(
                    "web_search", {"query": "Accenture AI demand", "count": 3},
                    call_ref="credential-use:web-search:node", deadline_at=FUTURE, max_response_bytes=1_000_000,
                )
                body = json.loads(result.raw_response.decode("utf-8"))
                self.assertEqual((body["ok"], body["idempotencyStatus"], body["callRef"]),
                                 (True, "fresh", "credential-use:web-search:node"))
                self.assertEqual(json.loads(result.result["content"][0]["text"])["provider"], "gemini")
                # The same call ref replays without a second host search.
                replay = handle.invoke(
                    "web_search", {"query": "Accenture AI demand", "count": 3},
                    call_ref="credential-use:web-search:node", deadline_at=FUTURE, max_response_bytes=1_000_000,
                )
                self.assertEqual(json.loads(replay.raw_response.decode("utf-8"))["idempotencyStatus"], "duplicate")
                # A different query under that ref is an idempotency conflict.
                with self.assertRaises(BridgeRequestRejected):
                    handle.invoke(
                        "web_search", {"query": "another", "count": 3},
                        call_ref="credential-use:web-search:node", deadline_at=FUTURE, max_response_bytes=1_000_000,
                    )
            finally:
                process.terminate()
                process.wait(timeout=30)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
