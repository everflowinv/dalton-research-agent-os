"""S2: the Guidepoint search lane -- governance, spec, adapter, executor, licence.

Everything here runs offline against a fake host handle that serves a packaged
fixture of synthetic excerpts.  No real Guidepoint content appears in this
repository, and no test reaches the local proxy.
"""

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
    build_governance_record,
)
from dalton_core.connector_runner import RunnerValidationError
from dalton_core.guidepoint_core import (
    SEARCH_KIND,
    TRANSCRIPT_KIND,
    guidepoint_schema_hash,
    guidepoint_source_hash,
)
from dalton_core.guidepoint_search import (
    EXCERPT_REF_PREFIX,
    MAX_VERBATIM_WORDS,
    QUOTE_POLICY,
    SEARCH_CAPABILITY_ID,
    SEARCH_PROFILE_REF,
    FakeGuidepointHandle,
    GuidepointCoreSearch,
    GuidepointQuotePolicyError,
    GuidepointSearchError,
    GuidepointSearchGovernance,
    count_recent_guidepoint_search_calls,
    guidepoint_excerpt_ref,
    guidepoint_excerpt_records,
    guidepoint_excerpts_from_raw_response,
    guidepoint_operation_narrowing,
    guidepoint_search_schema_hash,
    validate_guidepoint_search_spec,
    verify_guidepoint_quote,
)
from dalton_core.live_mcp_connector import (
    OPENCLAW_GUIDEPOINT_BRIDGE_HASH,
    OPENCLAW_GUIDEPOINT_BRIDGE_REF,
    host_tool_bridge_for,
    host_tool_bridge_for_operation,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_connector_bridge import (
    BridgeRateLimited,
    HostToolInvocationResult,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "guidepoint_search_library_synthetic.json"
ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))
OWNER = "human:lumos"
SPEC = {
    "query": "What are experts seeing in US IT services demand",
    "filters": {
        "document_type": "expert_call_transcript",
        "industry": "IT Services",
        "date_from": "2025-09-09",
        "date_to": "2026-09-09",
    },
    "cursor": None,
}


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def approved_governance() -> GuidepointSearchGovernance:
    return GuidepointSearchGovernance(
        build_governance_record(SEARCH_KIND, approved_by=OWNER, status="approved")
    )


def proposed_governance() -> GuidepointSearchGovernance:
    return GuidepointSearchGovernance(
        build_governance_record(SEARCH_KIND, approved_by=OWNER, status="proposed")
    )


class Harness:
    """Core + governed Guidepoint search in one temp dir (runs in-process)."""

    def __init__(self, root: Path, handle, *, governance=None, clock: Clock | None = None) -> None:
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
        self.search = GuidepointCoreSearch(
            store=self.core, connectors=self.connectors, observability=self.observability,
            journal=self.journal, scheduler=self.scheduler, catalog=self.catalog,
            spool=self.spool, governance=governance, host_handle=self.handle,
            clock=self.clock,
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
        raise BridgeRateLimited("guidepoint quota exhausted", retry_after_ms=30_000)


class GovernanceAndContractTests(unittest.TestCase):
    def test_the_lane_binds_the_owner_approved_search_record_and_no_other(self) -> None:
        record = build_governance_record(SEARCH_KIND, approved_by=OWNER)
        self.assertEqual(record["capability_id"], SEARCH_CAPABILITY_ID)
        self.assertEqual(record["expected_schema_hash"], guidepoint_search_schema_hash())
        # The shipped, owner-signed record is exactly what the lane loads, and
        # its hash is the one P13ae froze.
        committed = json.loads(
            (ROOT / "deploy/connector-governance/guidepoint-search-library-v1.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(committed["expected_schema_hash"], guidepoint_search_schema_hash())
        self.assertEqual(committed["expected_source_hash"], guidepoint_source_hash())
        self.assertEqual(committed["status"], "proposed")
        # The get_transcript record is a valid governance record for another
        # capability; the search lane must not accept it.
        with self.assertRaises(GuidepointSearchError):
            GuidepointSearchGovernance(
                build_governance_record(TRANSCRIPT_KIND, approved_by=OWNER)
            )
        with self.assertRaises(GuidepointSearchError):
            GuidepointSearchGovernance(
                build_governance_record("sec-company-facts", approved_by=OWNER)
            )

    def test_a_record_whose_schema_hash_drifted_is_refused_at_load(self) -> None:
        record = dict(build_governance_record(SEARCH_KIND, approved_by=OWNER))
        record["expected_schema_hash"] = guidepoint_schema_hash("get_transcript")
        from dalton_core.store import content_hash

        record.pop("content_hash")
        record["content_hash"] = content_hash(record)
        # It is a well-formed generic record ...
        self.assertEqual(ConnectorGovernance(record).kind, SEARCH_KIND)
        # ... and still not the frozen search_library contract.
        with self.assertRaises(GuidepointSearchError):
            GuidepointSearchGovernance(record)

    def test_the_bridge_is_resolved_by_source_and_operation(self) -> None:
        bridge = host_tool_bridge_for("source:guidepoint", "search_library")
        self.assertEqual(
            (bridge.template_key, bridge.bridge_ref, bridge.bridge_hash,
             bridge.credential_slot_ref),
            ("guidepoint", OPENCLAW_GUIDEPOINT_BRIDGE_REF,
             OPENCLAW_GUIDEPOINT_BRIDGE_HASH, "credential-slot:guidepoint"),
        )
        # AlphaEngine exposes search_library too, which is why the operation
        # name alone must refuse rather than pick.
        with self.assertRaises(RunnerValidationError):
            host_tool_bridge_for_operation("search_library")
        alpha = host_tool_bridge_for("source:alphaengine", "search_library")
        self.assertNotEqual(alpha.bridge_ref, bridge.bridge_ref)

    def test_the_spec_is_closed_and_has_no_cursor(self) -> None:
        cleaned = validate_guidepoint_search_spec({**SPEC, "query": f"  {SPEC['query']} "})
        self.assertEqual(cleaned, SPEC)
        for bad in (
            {**SPEC, "cursor": "page-2"},
            {**SPEC, "filters": {**SPEC["filters"], "geography": "US"}},
            {**SPEC, "filters": {**SPEC["filters"], "document_type": "sell_side_report"}},
            {**SPEC, "filters": {k: v for k, v in SPEC["filters"].items() if k != "date_to"}},
            {**SPEC, "query": ""},
            {"query": SPEC["query"], "filters": SPEC["filters"]},
        ):
            with self.subTest(bad=bad), self.assertRaises(GuidepointSearchError):
                validate_guidepoint_search_spec(bad)

    def test_the_narrowing_is_a_new_proposal_that_leaves_v1_untouched(self) -> None:
        narrowing = guidepoint_operation_narrowing(proposed_by=OWNER)
        self.assertEqual(narrowing["status"], "proposed")
        self.assertEqual(narrowing["availability"], "not_available_upstream")
        self.assertEqual(narrowing["operation"], "get_transcript")
        self.assertEqual(
            narrowing["supersedes_ref"], f"connector-governance:{TRANSCRIPT_KIND}:v1"
        )
        committed = json.loads(
            (ROOT / "deploy/connector-governance/guidepoint-get-transcript-narrowing-v1.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(committed, narrowing)
        # The approved record and its hashes are exactly as P13ae shipped them.
        approved = json.loads(
            (ROOT / "deploy/connector-governance/guidepoint-get-transcript-v1.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(approved["expected_schema_hash"], guidepoint_schema_hash("get_transcript"))
        self.assertEqual(
            approved,
            build_governance_record(
                TRANSCRIPT_KIND, approved_by=OWNER, status="proposed",
                effective_from="2026-09-09T00:00:00+00:00",
            ),
        )
        with self.assertRaises(GuidepointSearchError):
            guidepoint_operation_narrowing(proposed_by="automation:dalton")


class ExcerptIdentityTests(unittest.TestCase):
    def test_the_same_passage_from_two_queries_is_one_document(self) -> None:
        # The same excerpt surfaced by a company query and by an industry
        # query must be the same ref, or the ledger records it twice.
        first = guidepoint_excerpt_records({"data": ROWS})
        second = guidepoint_excerpt_records(
            {"data": [{**row, "context": "a different surrounding passage"} for row in ROWS]}
        )
        self.assertEqual(
            [item["excerpt_ref"] for item in first],
            [item["excerpt_ref"] for item in second],
        )
        for ref in (item["excerpt_ref"] for item in first):
            self.assertTrue(ref.startswith(EXCERPT_REF_PREFIX))

    def test_the_ref_ignores_the_expert_s_title_and_normalizes_whitespace(self) -> None:
        base = dict(ROWS[0])
        reworded = {
            **base,
            "respondent": {
                **base["respondent"], "title": "Managing Director", "company": "Elsewhere",
            },
            "question": f"  {base['question']}  ",
        }
        self.assertEqual(
            guidepoint_excerpt_records({"data": [base]})[0]["excerpt_ref"],
            guidepoint_excerpt_records({"data": [reworded]})[0]["excerpt_ref"],
        )
        # A different question is a different passage.
        self.assertNotEqual(
            guidepoint_excerpt_records({"data": [base]})[0]["excerpt_ref"],
            guidepoint_excerpt_ref(
                transcript_name=base["transcript_name"], date=base["date"],
                respondent=base["respondent"]["full_name"], question="Something else entirely?",
            ),
        )

    def test_a_malformed_or_duplicated_payload_is_refused(self) -> None:
        for bad in (
            {"data": [{**ROWS[0], "answer": ""}]},
            {"data": [{**ROWS[0], "respondent": {}}]},
            {"data": [{**ROWS[0], "date": "June 2026"}]},
            {"data": [{**ROWS[0], "reference_url": "http://insecure.invalid/x"}]},
            {"data": [ROWS[0], dict(ROWS[0])]},
            {"results": "not an array"},
            {},
        ):
            with self.subTest(bad=bad), self.assertRaises(RunnerValidationError):
                guidepoint_excerpt_records(bad)

    def test_the_live_sse_framing_is_readable_and_so_is_plain_json(self) -> None:
        # The Guidepoint proxy answers tools/call over text/event-stream, so
        # the recorded artifact is SSE frames. The first live smoke query
        # found this: the search succeeded and re-reading the artifact failed.
        plain = FakeGuidepointHandle(ROWS).invoke(
            "search_library", {"query": "x", "size": 20},
            call_ref="credential-use:test:1",
            deadline_at="2099-01-01T00:00:00+00:00", max_response_bytes=500_000,
        )
        framed = FakeGuidepointHandle(ROWS, sse=True).invoke(
            "search_library", {"query": "x", "size": 20},
            call_ref="credential-use:test:1",
            deadline_at="2099-01-01T00:00:00+00:00", max_response_bytes=500_000,
        )
        self.assertTrue(framed.raw_response.startswith(b"event: message"))
        self.assertNotEqual(plain.raw_response, framed.raw_response)
        self.assertEqual(
            [item["excerpt_ref"]
             for item in guidepoint_excerpts_from_raw_response(plain.raw_response)],
            [item["excerpt_ref"]
             for item in guidepoint_excerpts_from_raw_response(framed.raw_response)],
        )
        for bad in (b"", b"not json", b"data: {}\n\n", "text".encode("utf-16")):
            with self.subTest(bad=bad), self.assertRaises(RunnerValidationError):
                guidepoint_excerpts_from_raw_response(bad)

    def test_the_excerpt_keeps_the_whole_licensed_passage(self) -> None:
        record = guidepoint_excerpt_records({"data": [ROWS[0]]})[0]
        self.assertIn(ROWS[0]["answer"], record["excerpt_text"])
        self.assertEqual(record["quote_policy"], dict(QUOTE_POLICY))


class QuotePolicyTests(unittest.TestCase):
    def excerpt(self):
        return guidepoint_excerpt_records({"data": [ROWS[0]]})[0]

    def test_a_short_verbatim_quote_is_admitted_with_its_citation(self) -> None:
        excerpt = self.excerpt()
        checked = verify_guidepoint_quote("Budgets moved into shorter phased programmes", excerpt=excerpt)
        self.assertEqual(checked["word_count"], 6)
        self.assertEqual(checked["max_verbatim_words"], MAX_VERBATIM_WORDS)
        self.assertEqual(checked["excerpt_ref"], excerpt["excerpt_ref"])
        self.assertEqual(
            checked["citation_markdown"], ROWS[0]["source_attribution"]["markdown"]
        )

    def test_a_quotation_longer_than_the_licence_is_refused(self) -> None:
        excerpt = self.excerpt()
        answer = ROWS[0]["answer"]
        too_long = " ".join(answer.split()[: MAX_VERBATIM_WORDS + 1])
        # It is genuinely verbatim -- the refusal is the licence, not accuracy.
        self.assertIn(too_long, " ".join(excerpt["excerpt_text"].split()))
        with self.assertRaises(GuidepointQuotePolicyError) as caught:
            verify_guidepoint_quote(too_long, excerpt=excerpt)
        self.assertIn("20 verbatim words", str(caught.exception))
        # One word shorter passes, so the boundary is where it says it is.
        just_short = " ".join(answer.split()[:MAX_VERBATIM_WORDS])
        self.assertEqual(
            verify_guidepoint_quote(just_short, excerpt=excerpt)["word_count"],
            MAX_VERBATIM_WORDS,
        )

    def test_a_quotation_that_is_not_in_the_excerpt_is_refused(self) -> None:
        with self.assertRaises(GuidepointQuotePolicyError):
            verify_guidepoint_quote("budgets collapsed entirely", excerpt=self.excerpt())
        with self.assertRaises(GuidepointQuotePolicyError):
            verify_guidepoint_quote("   ", excerpt=self.excerpt())
        with self.assertRaises(GuidepointQuotePolicyError):
            verify_guidepoint_quote("Budgets moved", excerpt={"no": "excerpt"})

    def test_a_manifest_may_be_stricter_than_the_licence_but_never_looser(self) -> None:
        excerpt = self.excerpt()
        with self.assertRaises(GuidepointQuotePolicyError):
            verify_guidepoint_quote(
                "Budgets moved into shorter phased programmes",
                excerpt={**excerpt, "quote_policy": {"max_verbatim_words": 3}},
            )


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_search_leaves_excerpt_refs_a_raw_artifact_and_a_free_replay(self) -> None:
        h = Harness(self.root, FakeGuidepointHandle(ROWS))
        self.addCleanup(h.close)
        request = h.search.build_request(SPEC)
        receipt = h.search.search(request)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(receipt["connector_profile_ref"], SEARCH_PROFILE_REF)
        self.assertEqual(
            (receipt["source_status"], receipt["next_cursor"], receipt["provider_calls"]),
            ("complete", None, 1),
        )
        self.assertEqual(receipt["quote_policy"], dict(QUOTE_POLICY))
        expected = [
            item["excerpt_ref"] for item in guidepoint_excerpt_records({"data": ROWS})
        ]
        self.assertEqual(receipt["document_refs"], expected)
        # The upstream dialect is compiled from the frozen parameters.
        self.assertEqual(
            h.handle.calls[0]["arguments"],
            {
                "query": SPEC["query"],
                "size": 20,
                "keywords": ["IT Services"],
                "start_date": "2025-09-09",
                "end_date": "2026-09-09",
            },
        )
        envelope = h.search.receipts.get_source_envelope(receipt["source_envelope_ref"])
        self.assertEqual(
            (envelope["source"], envelope["operation"], envelope["completeness"],
             envelope["status"]),
            ("source:guidepoint", "search_library", "ranked", "complete"),
        )
        # The raw JSON-RPC bytes are in the spool and reproduce the refs.
        raw = h.spool.read_object(envelope["raw_response_hash"])
        import hashlib

        self.assertEqual(hashlib.sha256(raw).hexdigest(), envelope["raw_response_hash"])
        recovered = guidepoint_excerpts_from_raw_response(raw)
        self.assertEqual([item["excerpt_ref"] for item in recovered], expected)
        self.assertEqual([item["excerpt_ref"] for item in h.search.excerpts(envelope["id"])],
                         expected)
        # Replay is durable and free.
        replay = h.search.search(request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["document_refs"], expected)
        self.assertEqual(replay["provider_calls"], 0)
        self.assertEqual(len(h.handle.calls), 1)
        self.assertEqual(
            count_recent_guidepoint_search_calls(h.core.connection, as_of=h.clock()), 1
        )
        for table in ("evidence_versions", "claim_versions"):
            self.assertEqual(
                h.core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )
        h.clock.advance(hours=25)
        self.assertEqual(
            count_recent_guidepoint_search_calls(h.core.connection, as_of=h.clock()), 0
        )

    def test_proposed_governance_refuses_before_any_call(self) -> None:
        h = Harness(self.root, FakeGuidepointHandle(ROWS), governance=proposed_governance())
        self.addCleanup(h.close)
        with self.assertRaises(GuidepointSearchError):
            h.search.search(h.search.build_request(SPEC))
        self.assertEqual(h.handle.calls, [])
        self.assertEqual(
            h.core.connection.execute(
                "SELECT COUNT(*) FROM connector_invocations"
            ).fetchone()[0],
            0,
        )

    def test_an_empty_page_and_a_rate_limit_are_recorded_not_hidden(self) -> None:
        (self.root / "empty").mkdir()
        h = Harness(self.root / "empty", FakeGuidepointHandle([]))
        self.addCleanup(h.close)
        receipt = h.search.search(h.search.build_request(SPEC))
        self.assertEqual(
            (receipt["outcome"], receipt["document_refs"], receipt["source_status"]),
            ("succeeded", [], "empty"),
        )
        limited_root = self.root / "limited"
        limited_root.mkdir()
        limited = Harness(limited_root, RateLimitedHandle())
        self.addCleanup(limited.close)
        receipt = limited.search.search(limited.search.build_request(SPEC))
        self.assertEqual(receipt["outcome"], "retryable")
        self.assertIsNone(receipt["source_envelope_ref"])
        self.assertEqual(limited.handle.calls, 1)
        # A refused call is still a recorded invocation against the budget.
        self.assertEqual(
            count_recent_guidepoint_search_calls(
                limited.core.connection, as_of=limited.clock()
            ),
            1,
        )

    def test_a_payload_that_leaves_the_frozen_shape_becomes_a_failed_attempt(self) -> None:
        class DriftingHandle(FakeGuidepointHandle):
            def invoke(self, tool_name, arguments, **kwargs):
                result = super().invoke(tool_name, arguments, **kwargs)
                payload = json.loads(result.result["content"][0]["text"])
                # A row with no respondent: not an excerpt this lane can name.
                payload["data"][0] = {**payload["data"][0], "respondent": None}
                drifted = {"content": [{"type": "text", "text": canonical_json(payload)}]}
                return HostToolInvocationResult(
                    request_id=result.request_id, raw_response=result.raw_response,
                    result=drifted,
                )

        drift_root = self.root / "drift"
        drift_root.mkdir()
        h = Harness(drift_root, DriftingHandle(ROWS))
        self.addCleanup(h.close)
        receipt = h.search.search(h.search.build_request(SPEC))
        self.assertNotEqual(receipt["outcome"], "succeeded")
        self.assertIsNone(receipt["source_envelope_ref"])
        self.assertEqual(receipt["document_refs"], [])
        self.assertEqual(
            h.core.connection.execute(
                "SELECT COUNT(*) FROM connector_source_envelopes"
            ).fetchone()[0],
            0,
        )

    def test_request_identity_drift_fails_closed(self) -> None:
        h = Harness(self.root, FakeGuidepointHandle(ROWS))
        self.addCleanup(h.close)
        request = h.search.build_request(SPEC)
        with self.assertRaises(GuidepointSearchError):
            h.search.search({**request, "query_hash": "0" * 64})
        with self.assertRaises(GuidepointSearchError):
            h.search.search({**request, "extra": True})
        self.assertEqual(h.handle.calls, [])


if __name__ == "__main__":
    unittest.main()
