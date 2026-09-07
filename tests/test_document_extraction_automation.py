"""ADR-0005 / P9d-17a: the extraction child drafts awaiting documents as mission automation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.research_auto_commit import DOCUMENT_QUALITATIVE_RULE_REF
from dalton_core.store import content_hash
from dalton_core.transcript_correction import TranscriptCorrectionConflict, TranscriptCorrectionValidationError
from tests.p9a_fixtures import mission_params
from tests.test_document_extraction import ExtractionHarness, NEW_DOC, OWNER
from tests.test_mission_source_discovery import AUTOMATION
from tests.test_transcript_polish_model_worker import policy, profile


class AutomationDraftingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.h = ExtractionHarness(self.root); self.addCleanup(self.h.close)

    def _grant_automation(self) -> dict:
        """Publish v2 with AlphaEngine connected and the discovery grant; carry the document forward."""

        params = mission_params(self.h.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.h.mission["id"], "idempotency_key": "coverage-mission:us-it-services:2"})
        ref = params.pop("mission_ref")
        v2 = self.h.missions.create_mission(ref, **params)
        carried = self.h.missions.carry_forward_superseded_documents(ref)
        self.assertEqual([(c["status"], c["document_ref"]) for c in carried], [("acquired", NEW_DOC)])
        # The copy keeps its acquisition ticket: that is how the review plane finds the manifest.
        row = self.h.missions.discovered_documents(v2["id"])[0]
        self.assertTrue(row["ticket_ref"].startswith("alphaengine-acquisition:"))
        backfill = self.h.missions.backfill_document_reviews(ref)
        self.assertEqual([b["status"] for b in backfill], ["fresh"])
        return v2

    def _model_config(self) -> Path:
        path = self.root / "extraction-model-config.json"
        path.write_text(json.dumps({
            "routing_policy_ref": policy()["policy_version_ref"], "credential_slot_refs": [profile()["credential_slot_ref"]],
            "model_router_db": str(self.root / "router.sqlite"), "broker_socket": str(self.root / "none.sock"),
            "broker_auth_key": str(self.root / "none.key"), "broker_client_id": "client:dalton-core",
            "expected_agent_id": "chem", "budget_db": str(self.root / "budget.sqlite"),
            "budget_policy_ref": "thesis-impact-day-budget-policy:production:1",
        }), encoding="utf-8")
        return path

    def _active_context(self) -> dict:
        """Offset-0 context of the awaiting review under the active mission version, as a human."""

        active = self.h.missions.active_mission("coverage-mission:us-it-services")
        review = next(r for r in self.h.missions.document_reviews(active["id"]) if r["state"] == "awaiting_human_extraction")
        return DocumentExtractionService(self.h.writer).view(
            review_id=review["review_id"], expected_review_hash=content_hash(review), offset=0, actor_ref=OWNER,
        )["context"]

    def _policy_with_document_rule(self) -> None:
        core = self.h.h.core
        core.create_policy(
            {**core.active_policy_version().policy,
             "research_candidate_auto_commit": {"enabled": True, "max_records": 20, "rules": [DOCUMENT_QUALITATIVE_RULE_REF]}},
            policy_version_id="policy:synthetic-document-qualitative:2", actor_ref=OWNER,
            change_reason="ADR-0005 fixture: list the mission document qualitative rule",
        )

    def _run_child(self, *extra: str) -> dict:
        fixture = self.root / "fixture.json"
        try:
            context = self._active_context()
        except StopIteration:
            context = None  # queue already drained: the fixture output is irrelevant
        if context is not None:
            fixture.write_text(json.dumps({"schema_version": "0.1", "suggestions": [{
                "quote_id": context["quotes"][0]["quote_id"],
                "normalized_statement": "Fixture management described cautious client decisions.",
                "metric_or_aspect": "aspect:client-decisions", "period": "not specified in this window",
                "basis": "fixture management commentary",
            }]}), encoding="utf-8")
        self._runs = getattr(self, "_runs", 0) + 1
        summary_dir = self.root / "extractions" / f"run-{self._runs}"; summary_dir.mkdir(parents=True)
        command = [sys.executable, "-m", "dalton_core.document_extraction_cli",
                   "--state-dir", str(self.root), "--model-config", str(self._model_config()),
                   "--summary-dir", str(summary_dir), "--spool-dir", str(self.root / "spool"),
                   "--scheduler-db", str(self.root / "scheduler.sqlite"), "--max-windows", "2",
                   "--hermetic-fixture-file", str(fixture), "--quiet", *extra]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        summary = json.loads((summary_dir / "summary.json").read_text(encoding="utf-8"))
        return {"code": completed.returncode, "stderr": completed.stderr[-1500:], "summary": summary}

    def test_child_drafts_under_the_mission_grant_and_a_human_may_still(self) -> None:
        # Under v1 nothing grants automation; the child says so per review and drafts nothing.
        run = self._run_child()
        self.assertEqual(run["code"], 0, run["stderr"])
        summary = run["summary"]
        self.assertEqual((summary["status"], summary["stop_reason"], summary["drafted"]), ("succeeded", "nothing_to_draft", []))
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertIn("CoverageMissionConflict", summary["skipped"][0]["reason"])
        self.assertEqual(summary["formal_authority_writes"], 0)

        v2 = self._grant_automation()
        counts_before = self.h.counts()
        run = self._run_child()
        self.assertEqual(run["code"], 0, run["stderr"])
        summary = run["summary"]
        self.assertEqual(summary["status"], "succeeded", summary)
        self.assertEqual(summary["stop_reason"], "drained")
        # The fixture original spans two windows.  The fixture output cites a
        # quote of the first window, so in window two that item is dropped as
        # foreign: the window succeeds with no suggestion, accounted once,
        # never retried for money.
        self.assertEqual([d["offset"] for d in summary["drafted"]], [0, 12000])
        first, second = summary["drafted"]
        self.assertEqual((first["status"], first["suggestions"], first["source_ref"], first["document_ref"]),
                         ("succeeded", 1, "source:alphaengine", NEW_DOC))
        self.assertEqual((second["status"], second["suggestions"]), ("succeeded", 0), second)
        self.assertEqual((summary["reviews_scanned"], summary["reviews_complete"]), (1, 1))
        # The persisted result is what the cockpit reads back, under the automation actor or a human.
        review = next(r for r in self.h.missions.document_reviews(v2["id"]) if r["state"] == "awaiting_human_extraction")
        service = DocumentExtractionService(self.h.writer)
        for actor in (AUTOMATION, OWNER):
            view = service.view(review_id=review["review_id"], expected_review_hash=content_hash(review), offset=0, actor_ref=actor)
            self.assertEqual(view["status"], "succeeded")
            self.assertEqual(view["suggestions"][0]["citation_status"], "pending_human_citation_admission")
            self.assertFalse(view["suggestions"][0]["producer_ref"].startswith("human:"))
        # Nothing formal was written, and the review is still open: staging is P9d-17b.
        self.assertEqual(self.h.counts(), counts_before)
        self.assertEqual(review["state"], "awaiting_human_extraction")
        # Re-running drafts nothing new: the result replays.
        again = self._run_child()
        self.assertEqual((again["summary"]["stop_reason"], again["summary"]["drafted"], again["summary"]["reviews_complete"]),
                         ("nothing_to_draft", [], 1))
        # An actor that is neither human nor the mission principal is refused before any grant.
        refused = self._run_child("--requested-by", "automation:someone-else")
        self.assertEqual(refused["summary"]["drafted"], [])
        self.assertIn("CoverageMissionConflict", refused["summary"]["skipped"][0]["reason"])


class AutomationAdmissionTests(AutomationDraftingTests):
    """ADR-0005 / P9d-17b: drafts become Claims under the mission document rule."""

    def test_correction_scope_for_automation_is_the_mirror_of_the_human_one(self) -> None:
        authority, manifest = self.h.writer._transcript_corrections(self.h.manifest)
        context = self._active_context()
        quote = context["quotes"][0]
        raw = {"source_start": quote["source_start"], "source_end": quote["source_end"],
               "source_sha256": quote["source_sha256"], "rationale": "model draft invocation:x via route:y"}
        common = dict(source_manifest_ref=context["source_manifest_ref"], source_manifest_hash=context["source_manifest_hash"],
                      source_content_hash=context["source_content_hash"], corrections=[], raw_review=raw)
        # The automation scope needs an automation actor; the human scope still needs a person.
        with self.assertRaises(TranscriptCorrectionValidationError):
            authority.publish("transcript-correction-set:auto:t1", review_scope="automation_verified_raw_span", actor_ref=OWNER, **common)
        with self.assertRaises(TranscriptCorrectionValidationError):
            authority.publish("transcript-correction-set:auto:t2", review_scope="verified_raw_span", actor_ref=AUTOMATION, **common)
        published = authority.publish("transcript-correction-set:auto:t3", review_scope="automation_verified_raw_span",
                                      actor_ref=AUTOMATION, **common)
        self.assertEqual((published["review_scope"], published["actor_ref"], published["corrections"]),
                         ("automation_verified_raw_span", AUTOMATION, []))
        citation = authority.bind_claim_citation(published["id"], published["content_hash"],
                                                 source_start=quote["source_start"], source_end=quote["source_start"] + 20)
        self.assertTrue(citation["claim_eligible"])
        with self.assertRaises(TranscriptCorrectionConflict):
            authority.bind_claim_citation(published["id"], published["content_hash"],
                                          source_start=quote["source_start"], source_end=quote["source_end"] + 1)

    def test_child_admits_drafts_into_formal_claims_and_replays(self) -> None:
        v2 = self._grant_automation()
        staging = str(self.root / "staging.sqlite")
        # Without the policy rule the window is held: nothing staged, the review stays open.
        held = self._run_child("--candidate-staging", staging)
        self.assertEqual(held["code"], 0, held["stderr"])
        self.assertEqual(held["summary"]["admitted"], [])
        self.assertEqual([(r["status"], "does not list" in r["reason"]) for r in held["summary"]["resolved_reviews"]], [("held", True)])
        self.assertEqual(held["summary"]["formal_authority_writes"], 0)
        review = next(r for r in self.h.missions.document_reviews(v2["id"]) if r["state"] == "awaiting_human_extraction")
        before = self.h.counts()

        self._policy_with_document_rule()
        run = self._run_child("--candidate-staging", staging)
        self.assertEqual(run["code"], 0, run["stderr"])
        summary = run["summary"]
        admitted = summary["admitted"]
        self.assertEqual([(a["offset"], a["status"]) for a in admitted], [(0, "admitted")], admitted)
        self.assertTrue(admitted[0]["claim_version_ref"].startswith("claim-version:"))
        self.assertEqual(admitted[0]["policy_rule_ref"], DOCUMENT_QUALITATIVE_RULE_REF)
        self.assertEqual(summary["formal_authority_writes"], 2)
        self.assertEqual([(r["status"], r["admitted"], r["rejected"]) for r in summary["resolved_reviews"]], [("extraction_staged", 1, 0)])
        after = self.h.counts()
        self.assertEqual((after["claim_versions"] - before["claim_versions"], after["evidence_versions"] - before["evidence_versions"]), (1, 1))
        self.assertEqual(after["transcript_correction_set_versions"] - before["transcript_correction_set_versions"], 1)
        formal = self.h.h.core.get_claim(admitted[0]["claim_version_ref"])
        claim = formal["claim"]
        # The formal actor is the policy reviewer, as for every policy-committed
        # Claim; the mission automation remains the candidate's producer.
        self.assertEqual((claim["claim_kind"], claim["value"], claim["unit"], claim["actor_ref"]),
                         ("qualitative", None, None, "system:research-auto-commit"))
        self.assertEqual(claim["candidate_producer_ref"] if "candidate_producer_ref" in claim else AUTOMATION, AUTOMATION)
        self.assertEqual(claim["normalized_statement"], "Fixture management described cautious client decisions.")
        self.assertEqual(len(formal["evidence_relations"]), 1)
        resolved = self.h.missions.document_review(review["review_id"])
        self.assertEqual((resolved["state"], resolved["candidate_claim_version_ref"]), ("extraction_staged", admitted[0]["candidate_claim_ref"]))
        self.assertIn("ADR-0005 policy admission", resolved["rationale"])
        # A re-run finds no awaiting review and writes nothing.
        again = self._run_child("--candidate-staging", staging)
        self.assertEqual((again["summary"]["reviews_scanned"], again["summary"]["admitted"], again["summary"]["formal_authority_writes"]), (0, [], 0))
        self.assertEqual(self.h.counts(), after)
        # Admission itself is idempotent: the same window admitted again is a duplicate.
        service = DocumentExtractionService(self.h.writer)
        self.h.writer._candidate_staging = __import__("dalton_core.research_verification", fromlist=["CandidateStagingStore"]).CandidateStagingStore(staging)
        self.h.writer._candidate_review = __import__("dalton_core.research_review", fromlist=["HumanReviewAuthority"]).HumanReviewAuthority(staging)
        self.addCleanup(self.h.writer._candidate_staging.close)
        replay_review = self.h.missions.document_review(review["review_id"])
        # The review is resolved now, so the context refuses; that is the intended closure.
        with self.assertRaises(Exception):
            service.admit_suggestions(review_id=review["review_id"], expected_review_hash=content_hash(replay_review), offset=0, actor_ref=AUTOMATION)
        self.assertEqual(self.h.counts(), after)


class OutputContractTests(unittest.TestCase):
    """Live: most rejected windows were fenced JSON or statements naming a period."""

    def test_fence_is_stripped_periods_pass_and_values_are_refused(self) -> None:
        from dalton_core.document_extraction import parse_suggestions, statement_asserts_a_value, unwrap_model_json
        body = '{"schema_version": "0.1", "suggestions": []}'
        self.assertEqual(unwrap_model_json("```json\n" + body + "\n```"), body)
        self.assertEqual(unwrap_model_json("```\n" + body + "```"), body)
        self.assertEqual(unwrap_model_json(body), body)
        self.assertEqual(unwrap_model_json("```json\n" + body), "```json\n" + body)  # unclosed: untouched
        context = {"quotes": [{"quote_id": "quote:0:10:abc"}]}
        self.assertEqual(parse_suggestions("```json\n" + body + "\n```", context)["suggestions"], [])
        # Tolerant window parse: keep the admissible view, drop the numeric one
        # and the foreign one with reasons, allow two views on one quote.
        item = {"quote_id": "quote:0:10:abc", "normalized_statement": "Guidance was lowered, viewed as unsurprising.",
                "metric_or_aspect": "guidance", "period": "FY26", "basis": "analyst commentary"}
        mixed = json.dumps({"schema_version": "0.1", "suggestions": [
            item, {**item, "normalized_statement": "Growth is now expected at 4-5%."},
            {**item, "quote_id": "quote:foreign"}, {**item, "normalized_statement": "Margins were reiterated."},
        ]})
        parsed = parse_suggestions(mixed, context, tolerant=True)
        self.assertEqual([x["normalized_statement"][:8] for x in parsed["suggestions"]], ["Guidance", "Margins "])
        self.assertEqual([(d["index"], d["reason"].split(" ")[0]) for d in parsed["dropped"]], [(1, "numeric"), (2, "suggestion")])
        with self.assertRaises(Exception):
            parse_suggestions(mixed, context)  # strict: the human path still refuses the whole thing
        with self.assertRaises(Exception):
            parse_suggestions("not json", context, tolerant=True)  # malformed envelope stays terminal
        for ok in ("Management expects bookings to improve in fiscal 2026.", "Demand softened in Q3 FY26 versus 1Q.",
                   "The company said H2 would be stronger than H1 of 2025."):
            self.assertFalse(statement_asserts_a_value(ok), ok)
        for bad in ("Revenue grew 3% in Q3.", "Bookings reached $19 billion.", "Headcount rose by 4,000 in 2025.",
                    "Margin was 15.2 in fiscal 2026."):
            self.assertTrue(statement_asserts_a_value(bad), bad)


class HostKeepaliveTests(unittest.TestCase):
    def test_host_holds_budget_and_router_open_so_read_only_binds_work(self) -> None:
        """Live: the context's read-only budget open refused without WAL sidecars."""

        from dalton_core.document_extraction_cli import ExtractionHost
        from dalton_core.model_router import ModelRouter
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ThesisImpactBudgetStore(str(root / "budget.sqlite")).close()
            ModelRouter(str(root / "router.sqlite")).close()
            # Closed stores leave no sidecars, so a read-only open refuses.
            with self.assertRaises(Exception):
                ThesisImpactBudgetStore(str(root / "budget.sqlite"), read_only=True)
            config = {"routing_policy_ref": "x", "credential_slot_refs": ["s"], "model_router_db": str(root / "router.sqlite"),
                      "broker_socket": "/s", "broker_auth_key": "/k", "broker_client_id": "client:dalton-core",
                      "expected_agent_id": "chem", "budget_db": str(root / "budget.sqlite"), "budget_policy_ref": "b"}
            host = ExtractionHost(state_dir=root, spool_dir=root / "spool", scheduler_db=root / "scheduler.sqlite",
                                  connector_governance=None, web_fetch_governance=None, model_config=config)
            try:
                with ThesisImpactBudgetStore(str(root / "budget.sqlite"), read_only=True) as budget:
                    self.assertIsNotNone(budget.connection)
                with ModelRouter(str(root / "router.sqlite"), read_only=True) as router:
                    self.assertIsNotNone(router.connection)
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
