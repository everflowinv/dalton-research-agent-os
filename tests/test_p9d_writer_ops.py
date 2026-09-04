"""Writer ops for P9d-1: mission source discovery through the writer service."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.alphaengine_core_search import build_search_governance_record
from dalton_core.mission_source_discovery import AlphaEngineSearchLauncher
from dalton_core.store import canonical_json
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    WriterServer,
)
from tests.p9a_fixtures import OWNER
from tests.test_mission_source_discovery import ACN, AUTOMATION, NEW_DOC, plan_for_tests
from tests.test_p9a_writer_ops import (
    AUTOMATION_TOKEN,
    CORE_TOKEN,
    GOVERNANCE_TOKEN,
    P9aWriterHarness,
)


class P9dWriterHarness(P9aWriterHarness):
    """P9a harness plus a rehearsal search launcher and the test plan."""

    def __init__(self, root: Path):  # noqa: D107 - mirrors the parent harness
        self.socket = str(root / "writer.sock")
        self.plan_path = root / "plan.json"
        self.plan_path.write_text(json.dumps(plan_for_tests()), encoding="utf-8")
        governance_path = root / "search-governance.json"
        governance_path.write_text(
            canonical_json(build_search_governance_record(approved_by="human:lumos", status="approved")) + "\n",
            encoding="utf-8",
        )
        results_path = root / "results.json"
        results_path.write_text(json.dumps([{"doc_id": NEW_DOC.split(":")[1]}]), encoding="utf-8")
        self.launcher = AlphaEngineSearchLauncher(
            state_dir=root, governance_path=governance_path, plan_path=self.plan_path,
            mode_args=("--fake-search-file", str(results_path)),
        )
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal(
                "coverage-governance", GOVERNANCE_TOKEN, HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref=OWNER,
            ),
            "mission-automation": Principal(
                "mission-automation", AUTOMATION_TOKEN,
                frozenset({
                    "run_mission_source_discovery", "mission_source_discoveries",
                    "mission_document_reviews",
                }),
                actor_ref=AUTOMATION,
            ),
        }
        self.server = WriterServer(
            root / "core.sqlite", self.socket, principals,
            search_launcher=self.launcher, discovery_plan_path=self.plan_path,
        )
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.governance = WriterClient(self.socket, GOVERNANCE_TOKEN, timeout=60)
        self.core = WriterClient(self.socket, CORE_TOKEN, timeout=60)
        self.automation = WriterClient(self.socket, AUTOMATION_TOKEN, timeout=60)

    def close(self) -> None:
        self.launcher.close()
        super().close()


class P9dWriterOpsTests(unittest.TestCase):
    def test_discovery_ops_are_gated_recorded_and_readable(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        h = P9dWriterHarness(Path(root.name))
        self.addCleanup(h.close)
        state = h.bootstrap()
        mission = h.governance.call("create_coverage_mission", h.mission_params(state))

        # Controller tick under mission v1: AlphaEngine is probe_only, so the
        # automation path is refused with the reason, and nothing is spent.
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual(tick["status"], "idle")
        self.assertEqual(tick["discovery"]["status"], "not_authorized")
        self.assertIn("probe_only", tick["discovery"]["reason"])
        self.assertEqual(tick["acquisition"]["status"], "unconfigured")

        # A human owner may rehearse the search under the same mission.
        with self.assertRaises(RemoteAuthorizationError):
            h.automation.call("run_mission_source_discovery", {
                "company_ref": ACN, "spec_ref": "earnings-call-transcripts",
            })
        with self.assertRaises(RemoteError):
            h.governance.call("run_mission_source_discovery", {
                "company_ref": ACN, "spec_ref": "no-such-spec",
            })
        ticket = h.governance.call("run_mission_source_discovery", {
            "company_ref": ACN, "spec_ref": "earnings-call-transcripts", "as_of": "2026-09-02",
        })
        self.assertEqual(ticket["status"], "running")
        self.assertEqual(ticket["requested_by"], OWNER)
        self.assertEqual(ticket["parameters"]["query"], "Accenture ACN earnings call transcript")
        self.assertTrue(ticket["dispatch_ref"].startswith("mission-discovery-dispatch:"))
        h.launcher.wait(timeout=120)
        status = h.governance.call("mission_source_discovery_status", {"ticket_ref": ticket["id"]})
        self.assertEqual(status["status"], "succeeded", status)
        self.assertEqual(status["summary"]["new_document_count"], 1)

        # The next tick settles the dispatch; the discovered document waits
        # because automation still has no grant to acquire it.
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual([item["status"] for item in tick["settled_dispatches"]], ["succeeded"])
        self.assertEqual(tick["settled_dispatches"][0]["discovery_ref"], status["summary"]["discovery_ref"])
        listing = h.core.call("mission_source_discoveries", {"company_ref": ACN})
        self.assertEqual(listing["mission_version_ref"], mission["id"])
        self.assertEqual([item["id"] for item in listing["discoveries"]], [status["summary"]["discovery_ref"]])
        self.assertEqual(listing["discoveries"][0]["requested_by"], OWNER)
        self.assertEqual([item["status"] for item in listing["dispatches"]], ["succeeded"])
        documents = h.governance.call("mission_discovered_documents", {"status": "discovered"})
        self.assertEqual([item["document_ref"] for item in documents["documents"]], [NEW_DOC])
        progress = h.governance.call("coverage_mission_progress", {"mission_ref": mission["mission_ref"]})
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual((acn["discovery_count"], acn["discovered_document_count"], acn["acquired_document_count"]), (1, 1, 0))
        # Automation may read discoveries but only for the exact mission version it names.
        readable = h.automation.call("mission_source_discoveries", {"mission_version_ref": mission["id"]})
        self.assertEqual(len(readable["discoveries"]), 1)

    def test_document_reviews_read_and_human_only_resolution(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        h = P9dWriterHarness(Path(root.name))
        self.addCleanup(h.close)
        state = h.bootstrap()
        mission = h.governance.call("create_coverage_mission", h.mission_params(state))

        # The queue reads empty for the active mission; automation may read
        # its own queue but cannot resolve anything.
        reviews = h.governance.call("mission_document_reviews", {})
        self.assertEqual(reviews["mission_version_ref"], mission["id"])
        self.assertEqual(reviews["reviews"], [])
        read = h.automation.call("mission_document_reviews", {"mission_version_ref": mission["id"]})
        self.assertEqual(read["reviews"], [])
        with self.assertRaises(RemoteAuthorizationError):
            h.automation.call("resolve_mission_document_review", {
                "review_id": "mission-document-review:does-not-exist",
                "resolution": "dismissed", "rationale": "forged",
            })
        with self.assertRaises(RemoteError):
            h.governance.call("resolve_mission_document_review", {
                "review_id": "mission-document-review:does-not-exist",
                "resolution": "dismissed", "rationale": "no such review",
            })
        # extraction_staged is refused without a configured staging authority
        # even before the mission ledger is consulted.
        with self.assertRaises(RemoteError):
            h.governance.call("resolve_mission_document_review", {
                "review_id": "mission-document-review:does-not-exist",
                "resolution": "extraction_staged",
                "candidate_claim_version_ref": "candidate-claim-version:none",
            })

    def test_writer_without_plan_reports_unconfigured(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        h = P9aWriterHarness(Path(root.name))
        self.addCleanup(h.close)
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual(tick["status"], "unconfigured")
        with self.assertRaises(RemoteError):
            h.governance.call("run_mission_source_discovery", {
                "company_ref": ACN, "spec_ref": "earnings-call-transcripts",
            })


if __name__ == "__main__":
    unittest.main()
