from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.alphaengine_core_acquisition import (
    CAPABILITY_ID,
    AlphaEngineCoreAcquisition,
    AlphaEngineCoreAcquisitionError,
    StaticConnectorGovernance,
    build_governance_record,
    core_transcript_authority_probe,
    write_governance_proposal,
)
from dalton_core.capability_catalog import CapabilityCatalog, CatalogError
from dalton_core.connector import ConnectorStore
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_connector_bridge import HostToolInvocationResult
from dalton_core.raw_spool import RawSpool
from dalton_core.research_review import validate_human_review_decision
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, GateRejected, canonical_json, content_hash
from dalton_core.transcript_correction import (
    TranscriptCorrectionAuthority,
    bind_candidate_evidence_to_transcript_citation,
)
from dalton_core.writer_server import CORE_OPERATIONS, Principal, WriterServer


DOC_ID = "130000095976806"
DOCUMENT_REF = f"alphaengine-doc:{DOC_ID}"
REVIEWER = "human:tailscale-0123456789abcdef0123456789abcdef"
PAGE_ONE = (
    "Operator: Good afternoon. "
    "New bookings were $19.3 billion for the quarter, a 2% decrease in US "
    "dollars and 3% in local currency, with an overall book-to-bill of 1.0. "
    + "Prepared remarks continue with delivery and demand commentary. " * 40
)
PAGE_TWO = (
    "Analyst question about managed services pipeline timing. "
    + "Management answer paragraph. " * 20
    + "That is r ight."
)
DOCUMENT = PAGE_ONE + PAGE_TWO
DIGEST = hashlib.sha256(DOCUMENT.encode("utf-8")).hexdigest()


class FakeDocumentHandle:
    """Host-owned loopback stand-in that serves exact contiguous pages."""

    def __init__(self, text: str, page_chars: int) -> None:
        self.text = text
        self.page_chars = page_chars
        self.calls: list[dict] = []

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        del deadline_at, max_response_bytes
        assert tool_name == "get_document"
        self.calls.append({"arguments": dict(arguments), "call_ref": call_ref})
        offset = int(arguments["offset"])
        limit = min(self.page_chars, int(arguments["max_chars"]))
        page = self.text[offset:offset + limit]
        next_offset = offset + len(page)
        complete = next_offset >= len(self.text)
        payload = {
            "metadata": {"doc_id": arguments["doc_id"], "title": "Accenture Q3 2026"},
            "content_chars": len(self.text),
            "content_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "offset": offset,
            "returned_chars": len(page),
            "text": page,
            "next_offset": None if complete else next_offset,
            "complete": complete,
        }
        result = {"content": [{"type": "text", "text": canonical_json(payload)}]}
        request_id = f"provider-request:{len(self.calls)}"
        raw = canonical_json(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        ).encode("utf-8")
        return HostToolInvocationResult(
            request_id=request_id, raw_response=raw, result=result
        )


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def approved_governance() -> StaticConnectorGovernance:
    return StaticConnectorGovernance(
        build_governance_record(approved_by="human:lumos", status="approved")
    )


class CoreHarness:
    def __init__(self, governance: StaticConnectorGovernance, handle) -> None:
        self.clock = MutableClock()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.core = DaltonStore(str(root / "core.sqlite"))
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            str(root / "scheduler.sqlite"), clock=self.clock,
            default_lease_seconds=30, max_lease_seconds=60,
        )
        self.catalog = CapabilityCatalog(
            str(root / "catalog.sqlite"), clock=self.clock,
            approval_resolver=governance.approval,
            policy_resolver=governance.policy,
        )
        self.spool = RawSpool(str(root / "spool"), max_total_bytes=50_000_000)
        self.handle = handle
        self.acquisition = AlphaEngineCoreAcquisition(
            store=self.core,
            connectors=self.connectors,
            observability=self.observability,
            journal=self.journal,
            scheduler=self.scheduler,
            catalog=self.catalog,
            spool=self.spool,
            governance=governance,
            mcp_handle=handle,
            clock=self.clock,
        )

    def count(self, table: str) -> int:
        return int(
            self.core.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        )

    def close(self) -> None:
        self.catalog.close()
        self.scheduler.close()
        self.core.close()
        self.temp.cleanup()


class GovernanceTests(unittest.TestCase):
    def test_proposed_governance_fails_closed(self) -> None:
        governance = StaticConnectorGovernance(
            build_governance_record(approved_by="human:lumos")
        )
        self.assertEqual(governance.status, "proposed")
        with self.assertRaises(AlphaEngineCoreAcquisitionError):
            governance.approval({"capability_id": CAPABILITY_ID})
        with self.assertRaises(AlphaEngineCoreAcquisitionError):
            governance.policy({"policy_ref": governance.policy_ref})

    def test_governance_record_is_hash_bound_and_written_owner_only(self) -> None:
        record = build_governance_record(approved_by="human:lumos")
        tampered = {**record, "max_lease_seconds": 999}
        with self.assertRaises(AlphaEngineCoreAcquisitionError):
            StaticConnectorGovernance(tampered)
        with self.assertRaises(AlphaEngineCoreAcquisitionError):
            StaticConnectorGovernance({**record, "approved_by": "system:bot"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "governance.json"
            written = write_governance_proposal(path, approved_by="human:lumos")
            self.assertEqual(written["status"], "proposed")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = StaticConnectorGovernance.load(path)
            self.assertEqual(loaded.content_hash, written["content_hash"])

    def test_approval_binds_exact_source_and_schema_hash(self) -> None:
        governance = approved_governance()
        self.assertIsNone(
            governance.approval(
                {
                    "capability_id": CAPABILITY_ID,
                    "source_ref": "x:y",
                    "source_hash": "0" * 64,
                    "schema_hash": governance.wire["expected_schema_hash"],
                }
            )
        )
        receipt = governance.approval(
            {
                "capability_id": CAPABILITY_ID,
                "source_ref": "connector-profile-template:alphaengine:0.1",
                "source_hash": governance.wire["expected_source_hash"],
                "schema_hash": governance.wire["expected_schema_hash"],
            }
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["approved_by"], "human:lumos")


class CoreAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handle = FakeDocumentHandle(DOCUMENT, page_chars=len(PAGE_ONE))
        self.harness = CoreHarness(approved_governance(), self.handle)
        self.addCleanup(self.harness.close)

    def test_two_page_document_lands_in_core_authority(self) -> None:
        h = self.harness
        plan = h.acquisition.build_plan(DOCUMENT_REF)
        result = h.acquisition.acquire(plan)
        manifest = result["manifest"]
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(len(manifest["pages"]), 2)
        self.assertEqual(manifest["physical_calls"], 2)
        self.assertEqual(manifest["document_quota_units"], 1)
        self.assertEqual(manifest["assembled_object"]["content_hash"], DIGEST)
        self.assertEqual(manifest["content_chars"], len(DOCUMENT))
        self.assertEqual(result["provider_calls"], 2)
        self.assertEqual(len(self.handle.calls), 2)
        self.assertEqual(h.count("connector_source_envelopes"), 2)
        self.assertEqual(h.count("connector_invocations"), 2)
        self.assertEqual(h.count("observability_artifact_versions_v2"), 2)
        self.assertEqual(h.count("connector_physical_attempts"), 2)
        self.assertEqual(h.count("connector_quota_settlements"), 2)
        self.assertEqual(h.spool.read_object(DIGEST).decode("utf-8"), DOCUMENT)
        probe = core_transcript_authority_probe(h.core, manifest)
        self.assertTrue(probe["ok"], probe)
        # The page-1 envelope binds the whole-document digest, which is what
        # the formal transcript gate compares against the citation binding.
        first = manifest["pages"][0]
        source = json.loads(
            h.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                (first["source_envelope_ref"],),
            ).fetchone()["record_json"]
        )
        self.assertEqual(source["source_record_refs"], [f"{DOCUMENT_REF}:sha256:{DIGEST}"])
        self.assertEqual(source["status"], "partial")
        self.assertEqual(h.count("evidence_versions"), 0)
        self.assertEqual(h.count("claim_versions"), 0)

    def test_second_acquisition_replays_without_provider_calls(self) -> None:
        h = self.harness
        plan = h.acquisition.build_plan(DOCUMENT_REF)
        first = h.acquisition.acquire(plan)
        h.clock.advance(5)
        second = h.acquisition.acquire(plan)
        self.assertEqual(first["manifest"], second["manifest"])
        self.assertEqual(second["provider_calls"], 0)
        self.assertEqual(second["replayed_pages"], 2)
        self.assertEqual(len(self.handle.calls), 2)
        self.assertEqual(h.count("connector_source_envelopes"), 2)

    def test_core_authority_promotes_transcript_candidate(self) -> None:
        h = self.harness
        plan = h.acquisition.build_plan(DOCUMENT_REF)
        manifest = h.acquisition.acquire(plan)["manifest"]
        corrections = TranscriptCorrectionAuthority(
            h.core,
            spool=h.spool,
            manifest_resolver=lambda ref: manifest if ref == manifest["id"] else None,
            evidence_resolver=lambda _ref: None,
        )
        flag_start = DOCUMENT.index("r ight")
        correction_set = corrections.publish(
            "transcript-correction-set:acn:q3fy26:core",
            source_manifest_ref=manifest["id"],
            source_manifest_hash=manifest["content_hash"],
            source_content_hash=DIGEST,
            review_scope="targeted_flags",
            corrections=[{
                "source_start": flag_start,
                "source_end": flag_start + len("r ight"),
                "source_sha256": hashlib.sha256(b"r ight").hexdigest(),
                "correction_kind": "terminology",
                "disposition": "unresolved",
                "replacement_text": None,
                "rationale": "ASR word-boundary flag outside the cited span.",
                "evidence_bindings": [],
            }],
            actor_ref=REVIEWER,
        )
        span = "New bookings were $19.3 billion"
        start = DOCUMENT.index(span)
        end = DOCUMENT.index("book-to-bill of 1.0.") + len("book-to-bill of 1.0.")
        citation = corrections.bind_claim_citation(
            correction_set["id"], correction_set["content_hash"],
            source_start=start, source_end=end,
        )
        self.assertTrue(citation["claim_eligible"])

        first = manifest["pages"][0]
        source = json.loads(
            h.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                (first["source_envelope_ref"],),
            ).fetchone()["record_json"]
        )
        artifact = h.observability.get_artifact_version_v2(
            source["raw_artifact_version_ref"]
        )
        when = source["retrieved_at"]
        evidence_body = {
            "schema_version": "0.1",
            "id": "candidate-evidence-version:acn:core",
            "created_at": when,
            "candidate_evidence_ref": "candidate-evidence:acn:q3fy26:transcript",
            "version": 1,
            "source_type": "alphaengine_document",
            "source_ref": "source:alphaengine",
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "artifact_refs": [{"ref": artifact["id"], "hash": artifact["content_hash"]}],
            "retrieved_at": when,
            "valid_until": None,
            "source_lineage": [source["id"]],
            "independence_group": "independence:source:alphaengine",
            "source_verification_ref": "verification-bundle:source:acn:core",
            "source_verification_hash": "1" * 64,
            "actor_ref": "system:offline-verifier",
            "prior_version_ref": None,
        }
        evidence = bind_candidate_evidence_to_transcript_citation(
            {**evidence_body, "content_hash": content_hash(evidence_body)}, citation
        )
        claim_body = {
            "schema_version": "0.1",
            "id": "candidate-claim-version:acn:core",
            "created_at": when,
            "candidate_claim_ref": "candidate-claim:acn:q3fy26:new-bookings-growth-lc:transcript",
            "version": 1,
            "subject_ref": "company:sec-cik:0001467373",
            "metric_or_aspect": "metric:new-bookings-growth-local-currency",
            "period": {
                "kind": "fiscal_quarter", "label": "FY2026Q3",
                "start": "2026-03-01T00:00:00.000000+00:00",
                "end": "2026-05-31T23:59:59.000000+00:00",
            },
            "basis": "management-reported",
            "normalized_statement": (
                "Accenture reported Q3 FY2026 new bookings decreased 3% in local currency year over year."
            ),
            "semantic_verification_status": "unverified",
            "claim_kind": "quantitative", "value": "-3", "unit": "percent",
            "currency": None, "scale": "one",
            "candidate_evidence_refs": [{"ref": evidence["id"], "hash": evidence["content_hash"]}],
            "source_verification_ref": "verification-bundle:source:acn:core",
            "source_verification_hash": "1" * 64,
            "numeric_spec_ref": "numeric-spec:acn:core",
            "numeric_spec_hash": "2" * 64,
            "numeric_verification_ref": "verification-bundle:numeric:acn:core",
            "numeric_verification_hash": "3" * 64,
            "actor_ref": "system:offline-verifier",
            "prior_version_ref": None,
        }
        claim = {**claim_body, "content_hash": content_hash(claim_body)}
        decision_body = {
            "schema_version": "0.1", "id": "human-review-decision:acn:core",
            "created_at": when,
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
            "verdict": "accept",
            "reviewed_semantics": {
                key: claim[key] for key in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement",
                )
            },
            "proposed_revisions": None, "relation": "supports",
            "rationale": "Core-held AlphaEngine authority with exact raw span.",
            "findings": ["raw span and numeric meaning agree"],
            "reviewer_ref": REVIEWER,
            "authorization": "explicit_human_review", "source": "tailscale_review",
            "source_event_ref": "research-review:acn:core",
        }
        decision = validate_human_review_decision(
            {**decision_body, "content_hash": content_hash(decision_body)}
        )
        result = h.core.commit_reviewed_candidate(
            decision=decision, evidence=evidence, claim=claim,
            idempotency_key="reviewed-ledger:acn:core",
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(h.count("evidence_versions"), 1)
        self.assertEqual(h.count("claim_versions"), 1)

    def test_governance_schema_mismatch_blocks_publication(self) -> None:
        record = build_governance_record(approved_by="human:lumos", status="approved")
        tampered = {
            key: value for key, value in record.items() if key != "content_hash"
        }
        tampered["expected_schema_hash"] = "f" * 64
        tampered["content_hash"] = content_hash(tampered)
        governance = StaticConnectorGovernance(tampered)
        harness = CoreHarness(governance, self.handle)
        self.addCleanup(harness.close)
        with self.assertRaises(CatalogError):
            harness.acquisition.ensure_governed_authorities()
        self.assertEqual(len(self.handle.calls), 0)


class CoreGuardTests(unittest.TestCase):
    def test_core_without_connector_authority_rejects_promotion(self) -> None:
        core = DaltonStore(":memory:")
        self.addCleanup(core.close)
        ObservabilityStore(core)
        when = "2026-08-26T14:00:00.000000+00:00"
        evidence_body = {
            "schema_version": "0.1",
            "id": "candidate-evidence-version:guard",
            "created_at": when,
            "candidate_evidence_ref": "candidate-evidence:guard",
            "version": 1,
            "source_type": "alphaengine_document",
            "source_ref": "source:alphaengine",
            "source_envelope_ref": "source-envelope:guard",
            "source_envelope_hash": "4" * 64,
            "artifact_refs": [{"ref": "artifact-version:guard", "hash": "5" * 64}],
            "retrieved_at": when,
            "valid_until": None,
            "source_lineage": ["source-envelope:guard"],
            "independence_group": "independence:source:alphaengine",
            "source_verification_ref": "verification-bundle:source:guard",
            "source_verification_hash": "1" * 64,
            "actor_ref": "system:offline-verifier",
            "prior_version_ref": None,
        }
        evidence = {**evidence_body, "content_hash": content_hash(evidence_body)}
        claim_body = {
            "schema_version": "0.1",
            "id": "candidate-claim-version:guard",
            "created_at": when,
            "candidate_claim_ref": "candidate-claim:guard",
            "version": 1,
            "subject_ref": "company:sec-cik:0001467373",
            "metric_or_aspect": "metric:guard",
            "period": "FY2026Q3",
            "basis": "management-reported",
            "normalized_statement": "guard",
            "semantic_verification_status": "unverified",
            "claim_kind": "quantitative", "value": "-3", "unit": "percent",
            "currency": None, "scale": "one",
            "candidate_evidence_refs": [{"ref": evidence["id"], "hash": evidence["content_hash"]}],
            "source_verification_ref": "verification-bundle:source:guard",
            "source_verification_hash": "1" * 64,
            "numeric_spec_ref": "numeric-spec:guard",
            "numeric_spec_hash": "2" * 64,
            "numeric_verification_ref": "verification-bundle:numeric:guard",
            "numeric_verification_hash": "3" * 64,
            "actor_ref": "system:offline-verifier",
            "prior_version_ref": None,
        }
        claim = {**claim_body, "content_hash": content_hash(claim_body)}
        decision_body = {
            "schema_version": "0.1", "id": "human-review-decision:guard",
            "created_at": when,
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
            "verdict": "accept",
            "reviewed_semantics": {
                key: claim[key] for key in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement",
                )
            },
            "proposed_revisions": None, "relation": "supports",
            "rationale": "guard", "findings": ["guard"],
            "reviewer_ref": REVIEWER,
            "authorization": "explicit_human_review", "source": "tailscale_review",
            "source_event_ref": "research-review:guard",
        }
        decision = validate_human_review_decision(
            {**decision_body, "content_hash": content_hash(decision_body)}
        )
        with self.assertRaises(GateRejected) as raised:
            core.commit_reviewed_candidate(
                decision=decision, evidence=evidence, claim=claim,
                idempotency_key="reviewed-ledger:guard",
            )
        self.assertIn("connector authority is unavailable", str(raised.exception))
        for table in ("evidence_versions", "claim_versions", "reviewed_candidate_commits"):
            self.assertEqual(
                core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )


class WriterConnectorSchemaTests(unittest.TestCase):
    def test_writer_opens_connector_authority_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = WriterServer(
                root / "core.sqlite", root / "w.sock",
                {"core": Principal("core", "token-1", CORE_OPERATIONS, unrestricted=True)},
            )
            server._open_store()
            try:
                names = {
                    row[0] for row in server.store.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                server._close_store()
            self.assertLessEqual(
                {"connector_source_envelopes", "connector_invocations",
                 "observability_artifact_versions_v2"},
                names,
            )


if __name__ == "__main__":
    unittest.main()
