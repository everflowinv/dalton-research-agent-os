from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
)
from dalton_core.bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from dalton_core.transcript_polish import (
    TRANSCRIPT_POLISH_CAPABILITY,
    TRANSCRIPT_POLISH_OPERATION,
    TRANSCRIPT_POLISH_OUTPUT_CONTRACT,
    TRANSCRIPT_POLISH_PERMISSION,
    TRANSCRIPT_POLISH_RUNTIME,
    TRANSCRIPT_POLISH_VERIFIER,
    TranscriptPolishAuthority,
    TranscriptPolishConflict,
    TranscriptPolishValidationError,
    TranscriptPolishWorker,
    parse_transcript_polish_candidate_text,
)
from dalton_core.transcript_correction import (
    TranscriptCorrectionAuthority,
    TranscriptCorrectionConflict,
    TranscriptCorrectionValidationError,
)
from tests.test_alphaengine_document_acquisition import (
    FakeAuthorityReader,
    FakePagePort,
)
from tests.test_connector import assert_wire_schema


WHEN = "2026-08-23T22:00:00.000000+00:00"
DOCUMENT_REF = "alphaengine-doc:transcript-polish-fixture"
ORIGINAL = (
    "Operator: Welcome to Accenture Q3 2026 earnings call. "
    "Um, revenue was USD 1.2 billion. "
    "Julie Sweet: We may improve margins, but we are not certain."
)
POLISHED = (
    "Operator: Welcome to Accenture Q3 2026 earnings call. "
    "Revenue was USD 1.2 billion. "
    "Julie Sweet: We may improve margins, but we are not certain."
)


def candidate_for(
    source: str,
    polished: str,
    *,
    start: int = 0,
    end: int | None = None,
) -> dict:
    end = len(source) if end is None else end
    source_slice = source[start:end]
    return {
        "schema_version": "0.1",
        "segments": [{
            "source_start": start,
            "source_end": end,
            "source_sha256": hashlib.sha256(source_slice.encode("utf-8")).hexdigest(),
            "polished_text": polished,
        }],
    }


def candidate(polished: str = POLISHED, *, start: int = 0, end: int | None = None) -> dict:
    return candidate_for(ORIGINAL, polished, start=start, end=end)


class _TranscriptFixture:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)
        plan = build_alphaengine_document_acquisition_plan(
            document_ref=DOCUMENT_REF,
            created_at=WHEN,
            max_pages=1,
            page_max_response_bytes=20_000,
            max_total_response_bytes=20_000,
            max_document_chars=10_000,
        )
        connector_authority = FakeAuthorityReader()
        port = FakePagePort(
            plan=plan,
            pages=[ORIGINAL],
            authority=connector_authority,
            spool=self.spool,
        )
        self.manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=port,
            authority_reader=connector_authority,
            spool=self.spool,
        ).execute()
        self.manifests = {self.manifest["id"]: self.manifest}
        self.evidence: dict[str, dict] = {}
        self.corrections = TranscriptCorrectionAuthority(
            self.store,
            spool=self.spool,
            manifest_resolver=lambda ref: self.manifests[ref],
            evidence_resolver=lambda ref: self.evidence[ref],
        )
        self.authority = TranscriptPolishAuthority(
            self.store,
            spool=self.spool,
            manifest_resolver=lambda ref: self.manifests[ref],
            correction_authority=self.corrections,
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)

    def materialize(self, value: dict | None = None, **overrides: object) -> dict:
        parameters = {
            "source_manifest_ref": self.manifest["id"],
            "source_manifest_hash": self.manifest["content_hash"],
            "source_content_hash": self.manifest["assembled_object"]["content_hash"],
            "candidate_text": json.dumps(value or candidate(), separators=(",", ":")),
            "additional_protected_terms": ["Accenture", "Julie Sweet"],
            "correction_set_version_ref": None,
            "correction_set_version_hash": None,
        }
        parameters.update(overrides)
        return self.authority.materialize(**parameters)

    def add_evidence(self, ref: str) -> dict:
        body = {"id": ref, "kind": "transcript-correction-evidence"}
        authority = {**body, "content_hash": content_hash(body)}
        self.evidence[ref] = authority
        return authority

    def correction(
        self,
        source_text: str,
        replacement_text: str | None,
        *,
        correction_kind: str,
        disposition: str = "accepted",
        evidence: list[dict] | None = None,
    ) -> dict:
        start = ORIGINAL.index(source_text)
        end = start + len(source_text)
        return {
            "source_start": start,
            "source_end": end,
            "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            "correction_kind": correction_kind,
            "disposition": disposition,
            "replacement_text": replacement_text,
            "rationale": "Exact correction review fixture.",
            "evidence_bindings": evidence or [],
        }

    @staticmethod
    def evidence_binding(authority: dict, evidence_kind: str) -> dict:
        return {
            "authority_ref": authority["id"],
            "authority_hash": authority["content_hash"],
            "evidence_kind": evidence_kind,
            "location": "exact-span:fixture",
        }


class TranscriptPolishTests(_TranscriptFixture, unittest.TestCase):
    def test_verified_candidate_forms_mapped_derived_artifact_once(self) -> None:
        artifact = self.materialize()
        self.assertEqual(artifact["status"], "fresh")
        self.assertEqual(artifact["citation_authority"], "source_lineage_only")
        self.assertEqual(artifact["claim_citation_mode"], "raw_span")
        self.assertEqual(self.authority.polished_text(artifact["id"]), POLISHED)
        self.assertEqual(artifact["span_mappings"][0]["source_start"], 0)
        self.assertEqual(artifact["span_mappings"][0]["source_end"], len(ORIGINAL))
        self.assertEqual(artifact["span_mappings"][0]["polished_end"], len(POLISHED))
        assert_wire_schema(
            self,
            "transcript-polish-artifact-version.schema.json",
            {key: value for key, value in artifact.items() if key != "status"},
        )
        duplicate = self.materialize()
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["id"], artifact["id"])
        alternative = candidate(POLISHED.replace(", but ", "; however, "))
        with self.assertRaisesRegex(TranscriptPolishConflict, "continue"):
            self.materialize(alternative)
        revised = self.materialize(alternative, prior_version_ref=artifact["id"])
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["prior_version_ref"], artifact["id"])

    def test_admitted_correction_is_applied_with_exact_source_lineage(self) -> None:
        official = self.add_evidence("authority:accenture-official-name")
        correction_set = self.corrections.publish(
            "transcript-correction-set:fixture",
            source_manifest_ref=self.manifest["id"],
            source_manifest_hash=self.manifest["content_hash"],
            source_content_hash=self.manifest["assembled_object"]["content_hash"],
            review_scope="targeted_flags",
            corrections=[self.correction(
                "Accenture",
                "Accenture plc",
                correction_kind="proper_name",
                evidence=[self.evidence_binding(official, "primary_reference")],
            )],
            actor_ref="human:owner",
        )
        assert_wire_schema(
            self,
            "transcript-correction-set-version.schema.json",
            {key: value for key, value in correction_set.items() if key != "status"},
        )
        resolved = self.corrections.resolve(
            correction_set["id"], correction_set["content_hash"]
        )
        expected = ORIGINAL.replace("Accenture", "Accenture plc")
        self.assertEqual(resolved["resolved_text"], expected)
        model_source = self.authority.model_source_context(
            source_manifest_ref=self.manifest["id"],
            source_manifest_hash=self.manifest["content_hash"],
            source_content_hash=self.manifest["assembled_object"]["content_hash"],
            correction_set_version_ref=correction_set["id"],
            correction_set_version_hash=correction_set["content_hash"],
        )
        self.assertEqual(model_source["resolved_source_text"], expected)
        self.assertEqual(
            model_source["resolved_source_hash"],
            hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        )
        polished = expected.replace("Um, ", "")
        artifact = self.materialize(
            candidate_for(expected, polished),
            correction_set_version_ref=correction_set["id"],
            correction_set_version_hash=correction_set["content_hash"],
            additional_protected_terms=["Accenture plc", "Julie Sweet"],
        )
        self.assertEqual(
            artifact["claim_citation_mode"],
            "raw_span_plus_admitted_correction",
        )
        self.assertEqual(artifact["correction_set_version_ref"], correction_set["id"])
        self.assertEqual(len(artifact["correction_mappings"]), 1)
        self.assertEqual(artifact["unresolved_correction_spans"], [])
        self.assertEqual(self.authority.polished_text(artifact["id"]), polished)
        start = ORIGINAL.index("Accenture")
        binding = self.corrections.bind_claim_citation(
            correction_set["id"],
            correction_set["content_hash"],
            source_start=start,
            source_end=start + len("Accenture"),
        )
        assert_wire_schema(
            self, "transcript-claim-citation-binding.schema.json", binding
        )
        self.assertTrue(binding["claim_eligible"])
        self.assertEqual(
            binding["citation_mode"], "raw_span_plus_admitted_correction"
        )
        self.assertEqual(
            self.corrections.claim_citation_binding(binding["id"]), binding
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM transcript_claim_citation_bindings"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE transcript_correction_set_versions SET actor_ref='human:other' "
                "WHERE version_id=?",
                (correction_set["id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE transcript_claim_citation_bindings "
                "SET claim_eligible=0 WHERE binding_id=?",
                (binding["id"],),
            )

    def test_high_risk_correction_requires_utterance_evidence(self) -> None:
        filing = self.add_evidence("authority:earnings-release")
        weak = self.correction(
            "1.2",
            "1.3",
            correction_kind="numeric",
            evidence=[self.evidence_binding(filing, "primary_reference")],
        )
        source_args = {
            "source_manifest_ref": self.manifest["id"],
            "source_manifest_hash": self.manifest["content_hash"],
            "source_content_hash": self.manifest["assembled_object"]["content_hash"],
            "review_scope": "targeted_flags",
            "actor_ref": "human:owner",
        }
        with self.assertRaisesRegex(
            TranscriptCorrectionValidationError, "audio or official transcript"
        ):
            self.corrections.publish(
                "transcript-correction-set:weak-numeric",
                corrections=[weak],
                **source_args,
            )
        official = self.add_evidence("authority:official-transcript")
        strong = self.correction(
            "1.2",
            "1.3",
            correction_kind="numeric",
            evidence=[self.evidence_binding(official, "official_transcript_span")],
        )
        drifted = dict(strong)
        drifted["evidence_bindings"] = [
            {**strong["evidence_bindings"][0], "authority_hash": "0" * 64}
        ]
        with self.assertRaises(TranscriptCorrectionConflict):
            self.corrections.publish(
                "transcript-correction-set:drifted-evidence",
                corrections=[drifted],
                **source_args,
            )
        admitted = self.corrections.publish(
            "transcript-correction-set:strong-numeric",
            corrections=[strong],
            **source_args,
        )
        self.assertEqual(admitted["accepted_count"], 1)
        unresolved = self.corrections.publish(
            "transcript-correction-set:unresolved-numeric",
            corrections=[self.correction(
                "1.2",
                "1.3",
                correction_kind="numeric",
                disposition="unresolved",
            )],
            **source_args,
        )
        resolution = self.corrections.resolve(
            unresolved["id"], unresolved["content_hash"]
        )
        self.assertEqual(resolution["resolved_text"], ORIGINAL)
        self.assertEqual(len(resolution["unresolved_correction_spans"]), 1)
        start = ORIGINAL.index("1.2")
        binding = self.corrections.bind_claim_citation(
            unresolved["id"],
            unresolved["content_hash"],
            source_start=start,
            source_end=start + len("1.2"),
        )
        self.assertFalse(binding["claim_eligible"])
        self.assertEqual(binding["blocking_reason"], "unresolved_correction_overlap")
        with self.assertRaises(TranscriptCorrectionValidationError):
            self.corrections.publish(
                "transcript-correction-set:model-admission",
                corrections=[strong],
                **{**source_args, "actor_ref": "model:transcript-worker"},
            )

    def test_numbers_names_spans_and_source_authority_fail_closed(self) -> None:
        changed_number = candidate(POLISHED.replace("1.2 billion", "1.3 billion"))
        with self.assertRaisesRegex(TranscriptPolishConflict, "numeric"):
            self.materialize(changed_number)
        removed_negation = candidate(POLISHED.replace("not certain", "certain"))
        with self.assertRaisesRegex(TranscriptPolishConflict, "negation"):
            self.materialize(removed_negation)
        changed_uncertainty = candidate(POLISHED.replace("may improve", "will improve"))
        with self.assertRaisesRegex(TranscriptPolishConflict, "uncertainty"):
            self.materialize(changed_uncertainty)
        swapped_numbers_text = POLISHED.replace("2026", "1.2 billion", 1).replace(
            "USD 1.2 billion", "USD 2026", 1
        )
        with self.assertRaisesRegex(TranscriptPolishConflict, "numeric"):
            self.materialize(candidate(swapped_numbers_text))
        changed_name = candidate(POLISHED.replace("Julie Sweet", "Jane Sweet"))
        with self.assertRaisesRegex(TranscriptPolishConflict, "protected"):
            self.materialize(changed_name)
        swapped_names_text = POLISHED.replace("Accenture", "__NAME_A__", 1).replace(
            "Julie Sweet", "Accenture", 1
        ).replace("__NAME_A__", "Julie Sweet", 1)
        with self.assertRaisesRegex(TranscriptPolishConflict, "protected"):
            self.materialize(candidate(swapped_names_text))
        introduced_name = candidate(POLISHED + " New Product")
        with self.assertRaisesRegex(TranscriptPolishConflict, "protected"):
            self.materialize(introduced_name)
        broken_span = candidate(start=1)
        with self.assertRaisesRegex(TranscriptPolishConflict, "partition"):
            self.materialize(broken_span)
        wrong_hash = candidate()
        wrong_hash["segments"][0]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(TranscriptPolishConflict, "span hash"):
            self.materialize(wrong_hash)
        with self.assertRaises(TranscriptPolishConflict):
            self.materialize(source_manifest_hash="0" * 64)
        with self.assertRaises(TranscriptPolishValidationError):
            self.materialize(additional_protected_terms=["Absent Corporation"])

    def test_candidate_contract_and_sql_immutability(self) -> None:
        wire = candidate()
        parsed = parse_transcript_polish_candidate_text(json.dumps(wire))
        assert_wire_schema(self, "transcript-polish-candidate-v0.1.schema.json", parsed)
        with self.assertRaises(TranscriptPolishValidationError):
            parse_transcript_polish_candidate_text(
                '{"schema_version":"0.1","schema_version":"0.1","segments":[]}'
            )
        with self.assertRaises(TranscriptPolishValidationError):
            parse_transcript_polish_candidate_text("```json\n{}\n```")
        artifact = self.materialize()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE transcript_polish_artifact_versions SET actor_ref='other' WHERE version_id=?",
                (artifact["id"],),
            )


class TranscriptPolishLoopIntegrationTests(_TranscriptFixture, unittest.TestCase):
    def test_existing_scheduler_and_bounded_loop_form_observed_outcome(self) -> None:
        observability = ObservabilityStore(self.store)
        scheduler = Scheduler(connection=self.store.connection)
        planner = BoundedPlannerAuthority(self.store)
        control = BoundedPlannerControlPlane(planner, observability, scheduler)
        agenda = AgendaStore(self.store)
        backlog = ResearchQuestionBacklog(self.store)
        worker = TranscriptPolishWorker(self.authority)
        mandate = agenda.create_mandate(
            "mandate:transcript-polish",
            objective="Create a conservative model-context derivative of one exact transcript",
            scope_refs=["example"],
            constraints={"citation_authority": "source_lineage_only"},
            success_criteria={"numeric_and_name_conservation": True},
            effective_from="2026-08-23T00:00:00+00:00",
            effective_until="2026-09-23T00:00:00+00:00",
            actor_ref="human:owner",
            version_id="mandate-version:transcript-polish:1",
            idempotency_key="transcript-polish:mandate:1",
        )
        question = backlog.record_question(
            mandate_version_ref=mandate["id"],
            company_ref="example",
            question="Can the exact transcript be polished without factual drift?",
            answer_criteria="Require full source mapping and conservation before deriving output.",
            source_refs=["source:alphaengine"],
            actor_ref="core",
            idempotency_key="transcript-polish:question:1",
        )
        parameters = {
            "source_ref": "source:alphaengine",
            "locator": DOCUMENT_REF,
            "query_terms": ["transcript-polish"],
            "source_manifest_ref": self.manifest["id"],
            "source_manifest_hash": self.manifest["content_hash"],
            "source_content_hash": self.manifest["assembled_object"]["content_hash"],
            "additional_protected_terms": ["Accenture", "Julie Sweet"],
            "correction_set_version_ref": None,
            "correction_set_version_hash": None,
            "prior_polished_artifact_version_ref": None,
        }
        template = planner.publish_probe_template(
            "probe-template:transcript-polish:alphaengine",
            capability_ref=TRANSCRIPT_POLISH_CAPABILITY,
            operation=TRANSCRIPT_POLISH_OPERATION,
            runtime_profile_ref=TRANSCRIPT_POLISH_RUNTIME,
            parameter_contract={
                "allowed_fields": list(parameters),
                "required_fields": list(parameters),
                "constants": {
                    "source_ref": "source:alphaengine",
                    "source_manifest_ref": self.manifest["id"],
                    "source_manifest_hash": self.manifest["content_hash"],
                    "source_content_hash": self.manifest["assembled_object"]["content_hash"],
                },
            },
            output_contract_ref=TRANSCRIPT_POLISH_OUTPUT_CONTRACT,
            verifier_ref=TRANSCRIPT_POLISH_VERIFIER,
            permission_scope=TRANSCRIPT_POLISH_PERMISSION,
            declared_side_effects=[],
            cost={"cost_units": 1, "max_attempts": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        loop = planner.create_loop(
            "bounded-loop:transcript-polish:example",
            question_version_ref=question["question_version_ref"],
            template_bindings=[{
                "coverage_item_ref": "transcript-polish",
                "template_version_ref": template["id"],
                "parameters": parameters,
            }],
            required_coverage_items=["transcript-polish"],
            budget={"max_rounds": 1, "max_cost_units": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        proposal = planner.propose_next_capital_lease(loop["id"])
        admitted = control.admit_proposal(proposal["id"])
        round_wire = admitted["round"]
        row = self.store.connection.execute(
            "SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?",
            (round_wire["work_order_ref"],),
        ).fetchone()
        work_order = json.loads(row["work_order_json"])
        lease = scheduler.claim(
            "worker:transcript-polish", work_order_id=round_wire["work_order_ref"]
        )
        outputs = worker.execute(work_order, json.dumps(candidate()))
        assert_wire_schema(self, "transcript-polish-probe-output.schema.json", outputs)
        result = {
            "schema_version": "0.1",
            "id": "result:transcript-polish:1",
            "created_at": WHEN,
            "work_order_ref": round_wire["work_order_ref"],
            "invocation_ref": "invocation:transcript-polish-fixture:1",
            "status": "succeeded",
            "outputs": outputs,
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "error": None,
            "metadata": {"candidate_fixture": True},
        }
        scheduler.complete(
            round_wire["work_order_ref"],
            1,
            "worker:transcript-polish",
            lease["lease_token"],
            result,
            idempotency_key="transcript-polish:complete:1",
        )
        outcome = control.record_outcome(round_wire["id"])
        self.assertEqual(outcome["outcome"]["outcome_kind"], "observed")
        self.assertEqual(outcome["outcome"]["formal_claim_refs"], [])
        self.assertEqual(outcome["manifest"]["entries"][0]["match_count"], 1)


if __name__ == "__main__":
    unittest.main()
