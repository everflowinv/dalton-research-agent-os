"""Directed-document verifier contract 0.2: a verdict about the candidate, not a copy of it.

Live until 2026-09-30, every gemini-3.8-flash pass of a directed-document draft
restated or rewrote the candidate statement (35 of 35 stored passing replies,
legacy 27 + ws-7d 8), and the 0.1 contract refused each one because the verifier
had not reproduced the statement byte-for-byte.  Contract 0.2 asks only for the
verdict, the findings and a reference to the one candidate.  These tests pin:

* what 0.2 accepts and what it still refuses (the stricter half is unchanged);
* that the staged claim is always the drafted statement, even when a reply
  carries a rewrite with figures;
* that stored 0.1 verifier Work replays byte-for-byte and stays on 0.1;
* an offline replay of the stored live replies (only when a copy exists under
  ``DALTON_VERIFIER_REPLAY`` -- the live bytes are never committed).
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.annual_report_qualitative import (
    AnnualReportQualitativeError, VERIFIER_OUTPUT_SCHEMA, VERIFIER_PROVIDER_SCHEMA_HASH,
    parse_verifier_text,
)
from dalton_core.document_research_qualitative import (
    VERIFIER_PROVIDER_CONTRACT_REF_V2, VERIFIER_PROVIDER_SCHEMA_HASH_V2,
    parse_directed_verifier_text, parse_verifier_text_v2, verifier_candidate_ref,
    verifier_prompt, verifier_prompt_v2,
)
from dalton_core.store import canonical_json, content_hash

STATEMENT = ("Alphabet disclosed remaining performance obligations at FY2025 year end, "
             "primarily related to Google Cloud.")
DRAFT = {
    "schema_version": "0.1", "status": "answered",
    "answer": "Alphabet reports its backlog company-wide.",
    "candidate": {"normalized_statement": STATEMENT,
                  "metric_or_aspect": "remaining performance obligations",
                  "period": "FY2025",
                  "basis": "10-K revenue note: $242.8 billion, just over half within 24 months.",
                  "cited_match_indexes": [1]},
    "missing": [],
}
REF = verifier_candidate_ref(DRAFT)
ERROR = {"code": "unsupported", "severity": "error", "detail": "not in the excerpts"}


def _reply(**overrides):
    return json.dumps({"schema_version": "0.2", "verdict": "pass", "candidate_ref": REF,
                       "findings": [], **overrides})


class ContractTests(unittest.TestCase):
    def test_a_pass_needs_only_the_verdict_and_the_candidate_reference(self):
        parsed = parse_verifier_text_v2(_reply(), draft=DRAFT)
        self.assertEqual(parsed, {"schema_version": "0.2", "verdict": "pass",
                                  "candidate_ref": REF, "findings": []})

    def test_a_restated_statement_is_tolerated_and_never_kept(self):
        # e71b2f03 / 15c68a78: the pass rewrote the statement with $242.8 billion.
        parsed = parse_verifier_text_v2(
            _reply(verified_statement="Alphabet had $242.8 billion of RPO."), draft=DRAFT)
        self.assertNotIn("verified_statement", parsed)
        self.assertNotIn("242.8", canonical_json(parsed))
        fenced = "```json\n" + _reply(verified_statement=None) + "\n```"
        self.assertEqual(parse_verifier_text_v2(fenced, draft=DRAFT)["verdict"], "pass")

    def test_the_stricter_half_is_unchanged(self):
        cases = (
            (_reply(findings=[ERROR]), "no error finding"),
            (_reply(verdict="reject"), "error finding"),
            (_reply(candidate_ref="candidate:" + "0" * 16), "candidate_ref"),
            (_reply(candidate_ref=None), "candidate_ref"),
            (json.dumps({"schema_version": "0.2", "verdict": "pass", "findings": []}),
             "closed shape"),
            (_reply(extra="x"), "closed shape"),
            (_reply(verified_statement=7), "closed shape"),
            (_reply(schema_version="0.1"), "closed shape"),
            (_reply(verdict="maybe"), "verdict"),
            (_reply(findings=[{"code": "x", "severity": "fatal", "detail": "y"}]), "finding"),
            (_reply(findings=[{"code": "x", "severity": "error", "detail": " "}]), "finding"),
            ("Here you go: " + _reply(), "strict JSON"),
        )
        for text, message in cases:
            with self.subTest(message=message, text=text[:60]), self.assertRaisesRegex(
                    AnnualReportQualitativeError, message):
                parse_verifier_text_v2(text, draft=DRAFT)

    def test_a_rejection_with_its_reason_is_a_valid_reply(self):
        parsed = parse_verifier_text_v2(_reply(verdict="reject", findings=[ERROR]), draft=DRAFT)
        self.assertEqual(parsed["verdict"], "reject")

    def test_the_reference_names_this_draft_and_no_other(self):
        other = {**DRAFT, "answer": "A different answer."}
        self.assertNotEqual(verifier_candidate_ref(other), REF)
        with self.assertRaisesRegex(AnnualReportQualitativeError, "candidate_ref"):
            parse_verifier_text_v2(_reply(), draft=other)

    def test_the_retired_contract_is_unchanged_and_selected_by_the_stored_work(self):
        restated = json.dumps({"schema_version": "0.1", "verdict": "pass",
                               "verified_statement": "Alphabet had $242.8 billion of RPO.",
                               "findings": []})
        with self.assertRaisesRegex(AnnualReportQualitativeError, "exact candidate"):
            parse_verifier_text(restated, draft=DRAFT)
        with self.assertRaisesRegex(AnnualReportQualitativeError, "exact candidate"):
            parse_directed_verifier_text(restated, metadata={"draft": DRAFT})
        with self.assertRaisesRegex(AnnualReportQualitativeError, "exact candidate"):
            parse_directed_verifier_text(restated, metadata={
                "draft": DRAFT, "verifier_output_schema_version": "0.1"})
        self.assertEqual(parse_directed_verifier_text(_reply(), metadata={
            "draft": DRAFT, "verifier_output_schema_version": "0.2"})["verdict"], "pass")
        with self.assertRaisesRegex(AnnualReportQualitativeError, "unsupported"):
            parse_directed_verifier_text(_reply(), metadata={
                "draft": DRAFT, "verifier_output_schema_version": "0.9"})

    def test_the_prompts(self):
        proof, producer = {"matches": []}, {"model_family": "x"}
        old = verifier_prompt(question="q", search_proof=proof, draft=DRAFT,
                              producer_proof=producer)
        # The 0.1 request is byte-exact what stored Work was asked with.
        self.assertEqual(old, canonical_json({
            "task": ("Independently verify that the draft answers the complete question and "
                     "every claim is supported by the exact registered-document excerpts. "
                     "Reject unsupported inference. Return JSON only."),
            "question": "q", "search_proof": proof, "draft": DRAFT,
            "producer_proof": producer, "output_schema": VERIFIER_OUTPUT_SCHEMA}))
        new = json.loads(verifier_prompt_v2(question="q", search_proof=proof, draft=DRAFT,
                                            producer_proof=producer))
        self.assertEqual(new["candidate_ref"], REF)
        self.assertNotIn("verified_statement", canonical_json(new["output_schema"]))
        self.assertIn("Do not restate", new["task"])
        self.assertIn("severity error", new["task"])

    def test_the_provider_schema_is_the_packaged_allowlisted_one(self):
        from importlib import resources

        from dalton_core.openclaw_model_adapter import _VERIFIER_PROVIDER_CONTRACTS

        version, resource, _name, purposes = _VERIFIER_PROVIDER_CONTRACTS[
            VERIFIER_PROVIDER_CONTRACT_REF_V2]
        schema = json.loads(resources.files("dalton_core").joinpath(resource).read_text("utf-8"))
        self.assertEqual(version, "0.2")
        self.assertEqual(purposes, frozenset({"mission_directed_document_verifier"}))
        self.assertEqual(content_hash(schema), VERIFIER_PROVIDER_SCHEMA_HASH_V2)
        self.assertNotIn("verified_statement", schema["properties"])
        self.assertEqual(schema["required"],
                         ["schema_version", "verdict", "candidate_ref", "findings"])


class ExecutorContractTests(unittest.TestCase):
    """The contract as the executor asks it, stages it and replays it."""

    def setUp(self):
        from tests import test_mission_document_research as fixtures

        self.fixtures = fixtures
        self.case = fixtures.MissionDocumentResearchTests()
        self.case.addCleanup = self.addCleanup
        self.case.fail = self.fail

    def _run(self, verifier_reply, *, draft_statement=None, recovery=False):
        fixture, authority, args, _registration, _launcher = self.case._fixture()
        if recovery:
            self.case._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        statement = draft_statement or "Managed services revenue is recognized over time."
        draft = self.fixtures.RouteBoundCountingFakeAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        verifier = self.fixtures.RouteBoundCountingFakeAdapter(verifier_reply)
        executor, _d, _v = self.case._executor(
            fixture, authority, draft_adapter=draft, verifier_adapter=verifier)
        return fixture, authority, admission, executor, statement

    def _verifier_works(self, executor, admission):
        rows = executor.scheduler.connection.execute(
            "SELECT work_order_json FROM scheduler_work_orders ORDER BY rowid").fetchall()
        works = [json.loads(row["work_order_json"]) for row in rows]
        return [work for work in works
                if work["metadata"].get("mission_document_research_admission_ref")
                == admission["id"]
                and work["metadata"].get("stage") == "independent_qualitative_verifier"]

    def _drive(self, executor, admission, *, until=("complete",), limit=30):
        current = None
        for _ in range(limit):
            current = executor.run_once(admission["id"])
            if current["status"] in until:
                return current
        self.fail(f"never reached {until}: {current}")

    def test_a_new_admission_is_verified_under_0_2_and_stages_the_drafted_statement(self):
        def restating_pass(work):
            return {"schema_version": "0.2", "verdict": "pass",
                    "candidate_ref": work.metadata["verifier_candidate_ref"],
                    "verified_statement": "Managed services revenue of $1.2 billion is "
                                          "recognized over 36 months.",
                    "findings": []}

        _fixture, _authority, admission, executor, statement = self._run(restating_pass)
        outcome = self._drive(executor, admission)
        [work] = self._verifier_works(executor, admission)
        metadata = work["metadata"]
        self.assertEqual(metadata["verifier_output_schema_version"], "0.2")
        self.assertEqual(metadata["verifier_provider_contract"],
                         VERIFIER_PROVIDER_CONTRACT_REF_V2)
        self.assertEqual(metadata["verifier_provider_schema_hash"],
                         VERIFIER_PROVIDER_SCHEMA_HASH_V2)
        self.assertEqual(json.loads(work["question"])["candidate_ref"],
                         metadata["verifier_candidate_ref"])
        record = executor.staging.connection.execute(
            "SELECT record_json FROM candidate_claim_versions").fetchall()
        statements = {json.loads(row[0])["normalized_statement"] for row in record}
        self.assertEqual(statements, {statement})
        self.assertNotIn("1.2 billion", canonical_json(outcome))

    def _assert_rejection_settles(self, executor, admission, *, contract):
        from dalton_core.mission_document_research_executor import (
            _blueprints, read_mission_document_research_observations,
        )

        outcome = self._drive(executor, admission)
        self.assertEqual(outcome["research_status"], "no_verified_claim")
        # Idempotent: the same verdict, the same observation, no new Work.
        works_before = executor.scheduler.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders").fetchone()[0]
        self.assertEqual(executor.run_once(admission["id"]), outcome)
        self.assertEqual(executor.scheduler.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders").fetchone()[0], works_before)
        [observation] = [
            item for item in read_mission_document_research_observations(
                executor.connection) if item["admission_ref"] == admission["id"]]
        [verifier] = self._verifier_works(executor, admission)
        self.assertEqual(observation["outcome"], "no_verified_claim")
        self.assertEqual(observation["stage"], "independent_qualitative_verifier")
        self.assertEqual(observation["work_order_ref"], verifier["id"])
        self.assertEqual(observation["draft_proof_ref"],
                         verifier["metadata"]["draft_proof"]["id"])
        self.assertIn("unsupported: not in the excerpts", observation["missing_evidence"])
        self.assertEqual(verifier["metadata"]["verifier_output_schema_version"], contract)
        # Nothing was staged and no staging Work was even created.
        staging = _blueprints(admission)[3]
        self.assertIsNone(executor.scheduler.work_order_authority(staging["id"]))
        self.assertEqual(executor.staging.connection.execute(
            "SELECT count(*) FROM candidate_claim_versions").fetchone()[0], 0)
        return observation, verifier

    def test_a_rejecting_verifier_ends_the_admission_as_no_verified_claim(self):
        def rejecting(work):
            return {"schema_version": "0.2", "verdict": "reject",
                    "candidate_ref": work.metadata["verifier_candidate_ref"],
                    "findings": [ERROR]}

        _fixture, _authority, admission, executor, _statement = self._run(rejecting)
        self._assert_rejection_settles(executor, admission, contract="0.2")

    def test_a_0_1_rejection_settles_the_same_way_and_its_work_is_unchanged(self):
        from dalton_core import mission_document_research_executor as executor_module
        from dalton_core.document_research_qualitative import verifier_prompt

        def rejecting_0_1(_work):
            return {"schema_version": "0.1", "verdict": "reject",
                    "verified_statement": None, "findings": [ERROR]}

        _fixture, _authority, admission, executor, _statement = self._run(rejecting_0_1)
        with mock.patch.object(executor_module, "_admission_verifier_contract",
                               return_value="0.1"):
            for _ in range(6):   # up to and including the verifier's own completion
                current = executor.run_once(admission["id"])
                if (current.get("stage") == "independent_qualitative_verifier"
                        and current["status"] == "succeeded"):
                    break
        [stored] = self._verifier_works(executor, admission)
        _observation, verifier = self._assert_rejection_settles(
            executor, admission, contract="0.1")
        # The 0.1 Work is re-derived byte for byte, under the 0.1 request.
        self.assertEqual(canonical_json(verifier), canonical_json(stored))
        metadata = verifier["metadata"]
        self.assertEqual(verifier["question"], verifier_prompt(
            question=metadata["question"], search_proof=metadata["retrieval_proof"],
            draft=metadata["draft"], producer_proof={
                key: metadata["draft_proof"][key] for key in (
                    "route_decision_ref", "model_invocation_ref", "model_family",
                    "request_binding_hash")}))

    def test_a_malformed_rejection_is_still_a_contract_failure(self):
        def reasonless(work):          # reject with no error finding
            return {"schema_version": "0.2", "verdict": "reject",
                    "candidate_ref": work.metadata["verifier_candidate_ref"],
                    "findings": []}

        _fixture, _authority, admission, executor, _statement = self._run(reasonless)
        failed = self._drive(executor, admission,
                             until=("failed", "stopped", "blocked", "waiting"))
        self.assertEqual(failed["stage"], "independent_qualitative_verifier")
        from dalton_core.mission_document_research_executor import (
            read_mission_document_research_observations,
        )
        self.assertFalse([item for item in read_mission_document_research_observations(
            executor.connection) if item["outcome"] == "no_verified_claim"])

    def test_a_wrong_reference_is_a_contract_failure_not_a_pass(self):
        def wrong(_work):
            return {"schema_version": "0.2", "verdict": "pass",
                    "candidate_ref": "candidate:" + "f" * 16, "findings": []}

        _fixture, _authority, admission, executor, _statement = self._run(wrong)
        self._drive(executor, admission, until=("failed", "stopped", "blocked", "waiting"))
        self.assertEqual(executor.staging.connection.execute(
            "SELECT count(*) FROM candidate_claim_versions").fetchone()[0], 0)

    def test_stored_0_1_verifier_work_replays_and_its_recovery_stays_0_1(self):
        """An admission first asked under 0.1 is never re-asked under 0.2 behind its ledger."""

        from dalton_core import mission_document_research_executor as executor_module

        def restating_0_1_pass(work):
            if work.metadata["verifier_output_schema_version"] == "0.1":
                return {"schema_version": "0.1", "verdict": "pass",
                        "verified_statement": "A rewrite with $1.2 billion.", "findings": []}
            return {"schema_version": "0.2", "verdict": "pass",
                    "candidate_ref": work.metadata["verifier_candidate_ref"], "findings": []}

        _fixture, _authority, admission, executor, _statement = self._run(
            restating_0_1_pass, recovery=True)
        # The verifier stage is first asked the way every stored one was.
        with mock.patch.object(executor_module, "_admission_verifier_contract",
                               return_value="0.1"):
            first = self._drive(executor, admission, until=("failed",))
        self.assertEqual(first["stage"], "independent_qualitative_verifier")
        # From here on nothing is patched: the stored 0.1 Work alone decides.
        retried = executor.run_once(admission["id"])
        self.assertEqual(retried["reason"], "automatic_bounded_contract_retry")
        final = self._drive(executor, admission,
                            until=("stopped", "blocked", "complete", "waiting"))
        self.assertEqual(final.get("reason"), "contract_failed_after_automatic_retry")
        works = self._verifier_works(executor, admission)
        self.assertEqual(len(works), 3)
        for work in works:
            self.assertEqual(work["metadata"]["verifier_output_schema_version"], "0.1")
            self.assertEqual(work["metadata"]["verifier_provider_schema_hash"],
                             VERIFIER_PROVIDER_SCHEMA_HASH)
            self.assertNotIn("verifier_candidate_ref", work["metadata"])
            self.assertEqual(json.loads(work["question"])["output_schema"],
                             VERIFIER_OUTPUT_SCHEMA)
        self.assertEqual(executor_module._admission_verifier_contract(
            executor.scheduler, admission["id"]), "0.1")


class StoredReplyReplayTests(unittest.TestCase):
    """Offline replay of the verifier replies live paid for (copies only, never committed).

    ``DALTON_VERIFIER_REPLAY`` names a JSON list of {"draft", "text", "kind"}
    pairs copied out of ``mission-document-research-runs/*/summary.json``.
    Each stored 0.1 reply is replayed twice: as it was (the 0.1 rule), and as
    the same judgement under 0.2 -- same verdict, same findings, the restated
    statement kept as the tolerated extra key, and the reference to its draft.
    """

    PATH = Path(os.environ.get("DALTON_VERIFIER_REPLAY",
                               "/tmp/docverify-replay/pairs.json"))

    def setUp(self):
        if not self.PATH.is_file():
            self.skipTest(f"no stored replies at {self.PATH}")
        self.pairs = json.loads(self.PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _as_0_2(value, draft):
        wire = {"schema_version": "0.2", "verdict": value["verdict"],
                "candidate_ref": verifier_candidate_ref(draft),
                "findings": value["findings"]}
        if "verified_statement" in value:
            wire["verified_statement"] = value["verified_statement"]
        return json.dumps(wire, ensure_ascii=False)

    def test_replay(self):
        from dalton_core.annual_report_qualitative import unfence_json_text

        counts = {"old_accepted": 0, "old_refused": 0, "new_accepted": 0,
                  "new_refused": 0, "claim_is_draft": 0, "mutations_refused": 0}
        for pair in self.pairs:
            draft, text = pair["draft"], pair["text"]
            value = json.loads(unfence_json_text(text))
            try:
                parse_verifier_text(text, draft=draft)
                counts["old_accepted"] += 1
            except AnnualReportQualitativeError:
                counts["old_refused"] += 1
            parsed = parse_verifier_text_v2(self._as_0_2(value, draft), draft=draft)
            counts["new_accepted"] += 1
            self.assertEqual(parsed["verdict"], value["verdict"])
            self.assertNotIn("verified_statement", parsed)
            # The claim that would be staged is the drafted statement.
            counts["claim_is_draft"] += int(
                draft["candidate"]["normalized_statement"] not in canonical_json(parsed))
            for mutated in (
                {**value, "findings": [*value["findings"], ERROR]},       # pass + error
                {**value, "verdict": "reject", "findings": []},           # reject, no reason
            ):
                with self.assertRaises(AnnualReportQualitativeError):
                    parse_verifier_text_v2(self._as_0_2(mutated, draft), draft=draft)
                counts["mutations_refused"] += 1
            with self.assertRaises(AnnualReportQualitativeError):
                parse_verifier_text_v2(self._as_0_2(value, {**draft, "answer": "other"}),
                                       draft=draft)
        print("\nverifier replay:", json.dumps({"replies": len(self.pairs), **counts}))
        self.assertEqual(counts["new_accepted"], len(self.pairs))


if __name__ == "__main__":
    unittest.main()
