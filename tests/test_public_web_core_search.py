"""P9d-4a: Core-hosted Gemini ``search_web`` -- governance, spec, adapter, executor."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_governance import (
    ConnectorGovernance,
    ConnectorGovernanceError,
    GEMINI_WEB_SEARCH_KIND,
    build_governance_record,
)
from dalton_core.connector_governance_cli import approve_governance_record
from dalton_core.connector_runner import RunnerValidationError
from dalton_core.live_mcp_connector import (
    GEMINI_WEB_SEARCH_CREDENTIAL_SLOT_REF,
    host_tool_bridge_for,
    host_tool_bridge_for_operation,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_connector_bridge import BridgeRateLimited, HostToolInvocationResult
from dalton_core.openclaw_web_search_broker_client import WebSearchProviderContractDrift
from dalton_core.public_web_connector import (
    OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
    OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF,
    public_web_url_ref,
)
from dalton_core.public_web_core_search import (
    FakeWebSearchHandle,
    PublicWebCoreSearch,
    PublicWebCoreSearchError,
    SEARCH_CAPABILITY_ID,
    SEARCH_PROFILE_REF,
    WebSearchConnectorGovernance,
    build_web_search_governance_record,
    count_recent_web_search_calls,
    public_web_urls_in_authority,
    validate_web_search_spec,
    web_search_spec_hash,
    web_search_adapter_hash,
    web_search_provider_contract_hash,
    write_web_search_governance_proposal,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json, content_hash


ROOT = Path(__file__).resolve().parents[1]
CITATIONS = [
    {"url": "https://Example.com/investors?q=ai#section", "title": "IR"},
    {"url": "https://example.com/investors?q=ai", "title": "duplicate after canonicalization"},
    {"url": "https://news.example.org/accenture-ai", "title": "News"},
]
SPEC = {"query": "Accenture AI demand", "date_after": "2026-08-01", "date_before": "2026-09-06"}


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def approved_governance() -> WebSearchConnectorGovernance:
    return WebSearchConnectorGovernance(
        build_web_search_governance_record(approved_by="human:lumos", status="approved")
    )


class WebSearchHarness:
    """Core + governed web search in one temp dir (search runs in-process)."""

    def __init__(
        self, root: Path, handle, *, governance=None,
        clock: Clock | None = None, expected_provider: str = "gemini",
    ) -> None:
        self.clock = clock or Clock()
        self.core = DaltonStore(str(root / "core.sqlite"))
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            str(root / "scheduler.sqlite"), clock=self.clock,
            default_lease_seconds=30, max_lease_seconds=60,
        )
        governance = governance or approved_governance()
        self.catalog = CapabilityCatalog(
            str(root / "catalog.sqlite"), clock=self.clock,
            approval_resolver=governance.approval, policy_resolver=governance.policy,
        )
        self.spool = RawSpool(str(root / "spool"), max_total_bytes=50_000_000)
        self.handle = handle
        self.search = PublicWebCoreSearch(
            store=self.core, connectors=self.connectors, observability=self.observability,
            journal=self.journal, scheduler=self.scheduler, catalog=self.catalog,
            spool=self.spool, governance=governance, host_handle=self.handle, clock=self.clock,
            expected_provider=expected_provider,
        )

    def close(self) -> None:
        self.catalog.close()
        self.scheduler.close()
        self.core.close()


class RateLimitedHandle:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        self.calls += 1
        raise BridgeRateLimited("gemini quota exhausted", retry_after_ms=30_000)


class ProviderDriftHandle:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, *args, **kwargs):
        self.calls += 1
        raise WebSearchProviderContractDrift(
            "PROVIDER_CONTRACT_DRIFT: host provider differs from broker config"
        )


class GovernanceAndSpecTests(unittest.TestCase):
    def test_governance_record_is_a_registered_kind_and_cli_approvable(self) -> None:
        proposed = build_web_search_governance_record(approved_by="human:lumos")
        self.assertEqual(proposed["capability_id"], SEARCH_CAPABILITY_ID)
        self.assertEqual(proposed["allowed_permissions"]["credential_slot_refs"], [GEMINI_WEB_SEARCH_CREDENTIAL_SLOT_REF])
        # Generic loader dispatches on capability id; the generic builder is byte-identical.
        generic = ConnectorGovernance(proposed)
        self.assertEqual(generic.kind, GEMINI_WEB_SEARCH_KIND)
        self.assertEqual(
            canonical_json(build_governance_record(GEMINI_WEB_SEARCH_KIND, approved_by="human:lumos")),
            canonical_json(proposed),
        )
        # The committed deploy record is exactly this proposal.
        committed = json.loads((ROOT / "deploy/connector-governance/gemini-web-search-v1.json").read_text())
        self.assertEqual(committed, proposed)
        self.assertEqual(committed["status"], "proposed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.json"
            write_web_search_governance_proposal(path, approved_by="human:lumos")
            self.assertFalse(WebSearchConnectorGovernance.load(path).approved)
            approved = approve_governance_record(path, approved_by="human:lumos")
            self.assertEqual(approved["status"], "approved")
            self.assertTrue(WebSearchConnectorGovernance.load(path).approved)
        # A record for another capability is refused by the narrowed class.
        with self.assertRaises(PublicWebCoreSearchError):
            WebSearchConnectorGovernance(build_governance_record("sec-company-facts", approved_by="human:lumos"))
        with self.assertRaises(ConnectorGovernanceError):
            ConnectorGovernance({**proposed, "capability_id": "capability:dalton:connector:unknown"})

    def test_bridge_registry_serves_search_web_without_touching_alphaengine(self) -> None:
        bridge = host_tool_bridge_for_operation("search_web")
        self.assertEqual(
            (bridge.template_key, bridge.bridge_ref, bridge.bridge_hash, bridge.source_type,
             bridge.credential_slot_ref, dict(bridge.tool_names)),
            ("gemini-web-search", OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_REF,
             OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH, "public_web",
             GEMINI_WEB_SEARCH_CREDENTIAL_SLOT_REF, {"search_web": "web_search"}),
        )
        # P13af: search_library is no longer resolvable by name -- AlphaEngine
        # and Guidepoint both expose one, on different sources and under
        # different approvals. The single-key path refuses rather than picks.
        with self.assertRaises(RunnerValidationError):
            host_tool_bridge_for_operation("search_library")
        alpha = host_tool_bridge_for("source:alphaengine", "search_library")
        self.assertEqual((alpha.template_key, alpha.source_type, alpha.plan_prefix),
                         ("alphaengine", "authenticated_library", "live-mcp-plan:alphaengine"))
        with self.assertRaises(RunnerValidationError):
            host_tool_bridge_for_operation("fetch_get")

    def test_spec_is_closed_and_needs_an_explicit_window(self) -> None:
        cleaned = validate_web_search_spec({**SPEC, "query": "  Accenture AI demand "})
        self.assertEqual(cleaned, SPEC)
        self.assertEqual(
            web_search_spec_hash(SPEC), content_hash({"operation": "search_web", "parameters": SPEC}),
        )
        for bad in (
            {**SPEC, "freshness": "week"},
            {"query": "x", "date_after": "2026-08-01"},
            {**SPEC, "date_after": "2026-09-07"},
            {**SPEC, "query": ""},
            {**SPEC, "date_before": "not-a-date"},
        ):
            with self.subTest(bad=bad), self.assertRaises(PublicWebCoreSearchError):
                validate_web_search_spec(bad)


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_search_leaves_url_refs_raw_artifact_and_free_replay(self) -> None:
        h = WebSearchHarness(self.root, FakeWebSearchHandle(CITATIONS))
        self.addCleanup(h.close)
        request = h.search.build_request(
            SPEC, created_at="2026-09-11T12:00:00.000000+00:00"
        )
        # Absent configuration is the immutable Gemini contract: neither the
        # request shape nor its historical adapter hash gains provider fields.
        legacy_identity = {
            "operation": "search_web",
            "parameters": SPEC,
            "created_at": request["created_at"],
        }
        self.assertEqual(set(request), {
            "operation", "parameters", "created_at", "query_hash", "request_hash",
        })
        self.assertEqual(request["request_hash"], content_hash(legacy_identity))
        self.assertEqual(web_search_adapter_hash(), content_hash({
            "target_ref": "host-tool:gemini-web-search",
            "package": "openclaw-gemini-web-search-live-adapter:0.1",
            "bridge_hash": OPENCLAW_GEMINI_WEB_SEARCH_BRIDGE_HASH,
            "operation": "search_web",
        }))
        receipt = h.search.search(request)
        expected_refs = [
            public_web_url_ref("https://example.com/investors?q=ai"),
            public_web_url_ref("https://news.example.org/accenture-ai"),
        ]
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(receipt["document_refs"], expected_refs)
        self.assertEqual((receipt["source_status"], receipt["next_cursor"], receipt["provider_calls"]),
                         ("complete", None, 1))
        self.assertEqual(receipt["connector_profile_ref"], SEARCH_PROFILE_REF)
        self.assertEqual(h.handle.calls[0]["arguments"], {
            "query": "Accenture AI demand", "count": 10,
            "date_after": "2026-08-01", "date_before": "2026-09-06",
        })
        envelope = h.search.receipts.get_source_envelope(receipt["source_envelope_ref"])
        self.assertEqual(
            (envelope["source"], envelope["operation"], envelope["completeness"], envelope["status"]),
            ("source:public-web", "search_web", "ranked", "complete"),
        )
        # The raw JSON-RPC bytes are in the spool; synthesis stays inside them.
        raw = h.spool.read_object(envelope["raw_response_hash"])
        self.assertIn(b"UNTRUSTED rehearsal synthesis", raw)
        authorities = h.search.url_authorities(receipt["source_envelope_ref"])
        self.assertEqual([item["canonical_url"] for item in authorities],
                         ["https://example.com/investors?q=ai", "https://news.example.org/accenture-ai"])
        self.assertEqual([item["url_ref"] for item in authorities], expected_refs)
        # Replay is durable and free: same refs, no second host call.
        replay = h.search.search(request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["document_refs"], expected_refs)
        self.assertEqual(replay["provider_calls"], 0)
        self.assertEqual(len(h.handle.calls), 1)
        self.assertEqual(count_recent_web_search_calls(h.core.connection, as_of=h.clock()), 1)
        # Nothing has been fetched, so no discovered URL is in authority yet.
        self.assertEqual(public_web_urls_in_authority(h.core.connection, expected_refs), [])
        with self.assertRaises(PublicWebCoreSearchError):
            public_web_urls_in_authority(h.core.connection, ["alphaengine-doc:1"])
        self.assertEqual(h.core.connection.execute(
            "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref=?", (SEARCH_PROFILE_REF,),
        ).fetchone()[0], 1)
        for table in ("evidence_versions", "claim_versions"):
            self.assertEqual(h.core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        # Trailing window: 25 hours later the call no longer counts.
        h.clock.advance(hours=25)
        self.assertEqual(count_recent_web_search_calls(h.core.connection, as_of=h.clock()), 0)

    def test_antigravity_contract_versions_identity_profile_and_raw_reverification(self) -> None:
        provider = "antigravity"
        handle = FakeWebSearchHandle(CITATIONS, provider=provider)
        h = WebSearchHarness(
            self.root, handle, expected_provider=provider,
        )
        self.addCleanup(h.close)
        request = h.search.build_request(
            SPEC, created_at="2026-09-11T12:00:00.000000+00:00"
        )
        self.assertEqual(
            request["provider_contract_hash"],
            web_search_provider_contract_hash(provider),
        )
        legacy_handle = FakeWebSearchHandle(CITATIONS)
        legacy_search = PublicWebCoreSearch(
            store=h.core, connectors=h.connectors, observability=h.observability,
            journal=h.journal, scheduler=h.scheduler, catalog=h.catalog,
            spool=h.spool, governance=approved_governance(),
            host_handle=legacy_handle, clock=h.clock,
        )
        legacy = legacy_search.build_request(
            SPEC, created_at=request["created_at"]
        )
        self.assertNotEqual(request["request_hash"], legacy["request_hash"])
        legacy_receipt = legacy_search.search(legacy)
        self.assertEqual(legacy_receipt["connector_profile_ref"], SEARCH_PROFILE_REF)

        receipt = h.search.search(request)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertIn(":provider-antigravity:", receipt["connector_profile_ref"])
        profile = h.connectors.get_profile(receipt["connector_profile_ref"])
        self.assertEqual(profile["adapter_hash"], web_search_adapter_hash(provider))
        self.assertEqual(
            profile["terms_policy_ref"],
            "policy:terms:openclaw-web-search-provider:0.1:antigravity",
        )
        authorities = h.search.url_authorities(receipt["source_envelope_ref"])
        self.assertEqual(len(authorities), 2)
        replay = h.search.search(request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(handle.calls), 1)
        self.assertEqual(
            count_recent_web_search_calls(h.core.connection, as_of=h.clock()), 2
        )

    def test_antigravity_contract_rejects_a_gemini_payload_before_raw_commit(self) -> None:
        handle = FakeWebSearchHandle(CITATIONS, provider="gemini")
        h = WebSearchHarness(
            self.root, handle, expected_provider="antigravity",
        )
        self.addCleanup(h.close)
        receipt = h.search.search(h.search.build_request(SPEC))
        self.assertEqual(receipt["outcome"], "failed")
        self.assertIsNone(receipt["source_envelope_ref"])
        self.assertEqual(len(handle.calls), 1)

    def test_broker_provider_mismatch_is_terminal_and_never_creates_an_envelope(self) -> None:
        handle = ProviderDriftHandle()
        h = WebSearchHarness(
            self.root, handle, expected_provider="antigravity",
        )
        self.addCleanup(h.close)
        receipt = h.search.search(h.search.build_request(SPEC))
        self.assertEqual(receipt["outcome"], "failed")
        self.assertIsNone(receipt["source_envelope_ref"])
        self.assertEqual(handle.calls, 1)

    def test_empty_citations_and_rate_limit_are_recorded_not_hidden(self) -> None:
        (self.root / "empty").mkdir()
        h = WebSearchHarness(self.root / "empty", FakeWebSearchHandle([]))
        self.addCleanup(h.close)
        receipt = h.search.search(h.search.build_request(SPEC))
        self.assertEqual((receipt["outcome"], receipt["document_refs"], receipt["source_status"]),
                         ("succeeded", [], "empty"))
        limited_root = self.root / "limited"
        limited_root.mkdir()
        limited = WebSearchHarness(limited_root, RateLimitedHandle())
        self.addCleanup(limited.close)
        receipt = limited.search.search(limited.search.build_request(SPEC))
        # Runner outcome vocabulary: a rate limit is a retryable failed attempt.
        self.assertEqual(receipt["outcome"], "retryable")
        self.assertIsNone(receipt["source_envelope_ref"])
        self.assertEqual(receipt["document_refs"], [])
        self.assertEqual(limited.handle.calls, 1)
        # The failed call is still a recorded invocation against the budget.
        self.assertEqual(count_recent_web_search_calls(limited.core.connection, as_of=limited.clock()), 1)

    def test_proposed_governance_refuses_before_any_call(self) -> None:
        proposed = WebSearchConnectorGovernance(build_web_search_governance_record(approved_by="human:lumos"))
        h = WebSearchHarness(self.root, FakeWebSearchHandle(CITATIONS), governance=proposed)
        self.addCleanup(h.close)
        with self.assertRaises(PublicWebCoreSearchError):
            h.search.search(h.search.build_request(SPEC))
        self.assertEqual(h.handle.calls, [])

    def test_request_identity_and_payload_drift_fail_closed(self) -> None:
        h = WebSearchHarness(self.root, FakeWebSearchHandle(CITATIONS))
        self.addCleanup(h.close)
        request = h.search.build_request(SPEC)
        with self.assertRaises(PublicWebCoreSearchError):
            h.search.search({**request, "query_hash": "0" * 64})
        with self.assertRaises(PublicWebCoreSearchError):
            h.search.search({**request, "extra": True})

        class DriftingHandle(FakeWebSearchHandle):
            def invoke(self, tool_name, arguments, **kwargs):
                result = super().invoke(tool_name, arguments, **kwargs)
                payload = json.loads(result.result["content"][0]["text"])
                payload["query"] = "another query"
                drifted = {"content": [{"type": "text", "text": canonical_json(payload)}]}
                return HostToolInvocationResult(
                    request_id=result.request_id, raw_response=result.raw_response, result=drifted,
                )

        drift_root = self.root / "drift"
        drift_root.mkdir()
        drifting = WebSearchHarness(drift_root, DriftingHandle(CITATIONS))
        self.addCleanup(drifting.close)
        # The executor turns the adapter's conflict into a failed attempt: no
        # envelope, no document refs, the reason is on the receipt.
        receipt = drifting.search.search(drifting.search.build_request(SPEC))
        self.assertNotEqual(receipt["outcome"], "succeeded")
        self.assertIsNone(receipt["source_envelope_ref"])
        self.assertEqual(receipt["document_refs"], [])
        self.assertEqual(drifting.core.connection.execute(
            "SELECT COUNT(*) FROM connector_source_envelopes"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
