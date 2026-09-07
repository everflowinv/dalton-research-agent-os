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
from dalton_core.store import content_hash
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

    def _run_child(self, *extra: str) -> dict:
        fixture = self.root / "fixture.json"
        context = self._active_context()
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
        # quote of the first window, so window two is a terminal invalid
        # result: accounted once, no suggestion, never retried for money.
        self.assertEqual([d["offset"] for d in summary["drafted"]], [0, 12000])
        first, second = summary["drafted"]
        self.assertEqual((first["status"], first["suggestions"], first["source_ref"], first["document_ref"]),
                         ("succeeded", 1, "source:alphaengine", NEW_DOC))
        self.assertEqual((second["status"], second["suggestions"]), ("failed", 0), second)
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
