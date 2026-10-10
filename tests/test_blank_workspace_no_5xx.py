"""Canary: every cockpit GET on a fresh workspace answers without a 5xx.

2026-10-08 inspection DEFECT-01: on a workspace with no published research
mission the legacy page's ``/v1/mission-document-review`` read went through the
writer, which refused (``mission_version_ref is required without a discovery
plan``), and the HTTP surface turned that into 503 -- the page showed
「读取失败：HTTP 503」.  A fresh workspace is a normal state, not a fault: every
read answers (an empty state, or a 400 in the owner's words for a read that
needs a company), never 5xx.

The route list is read from the HTTP surface's own source, so a route added
later is in this canary without anyone remembering to add it.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dalton_core import agenda_control
from dalton_core.agenda_control import (
    AgendaControlApplication, AgendaControlConfig, AgendaControlPlane, _handler,
)
from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane
from dalton_core.research_review_control import (
    ResearchReviewControlConfig, ResearchReviewControlPlane,
)
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.store import content_hash
from dalton_core.workspace_creation import create_blank_workspace
from dalton_core.writer_server import CORE_OPERATIONS, Principal, WriterServer


LOGIN = "owner@example.com"
HOST = "dalton.example.ts.net"

# Reads that need a parameter get one that cannot exist on a fresh workspace.
QUERIES = {
    "/v1/cockpit/job": "?id=cockpit-job:none",
    "/v1/cockpit/deliverable": "?ref=deliverable:none",
    "/v1/cockpit/decision-status": "?kind=approval&ref=none",
    "/v1/cockpit/model": "?company=company:none",
    "/v1/cockpit/research": "?company=company:none",
    "/v1/cockpit/export": "?company=company:none&format=html",
    "/v1/cockpit/history": "?kind=ask",
    "/v1/cockpit/ui-texts": "?keys=",
}


def http_get_routes() -> list[str]:
    source = Path(agenda_control.__file__).read_text(encoding="utf-8")
    routes = set(re.findall(r'path == "(/[^"]*)"', source))
    # POST-only routes live in the ``actions`` table, not behind ``path ==``.
    return sorted(route for route in routes if route != "/favicon.ico")


class Dashboard:
    """The writer's dashboard principal on a fresh Core: nothing to list."""

    def list_agenda_feedback_targets(self, **_kwargs):
        return []

    def answer_subjects(self):
        return []

    def intent_context_bindings(self):
        return []


class BlankWorkspaceNo5xxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        release = root / "release"
        release.mkdir()
        catalog = {"schema_version": "dalton-shared-connection-catalog-0.1", "models": [], "sources": []}
        catalog["content_hash"] = content_hash(catalog)
        catalog_path = root / "connections.json"
        catalog_path.write_text(json.dumps(catalog))
        receipt = create_blank_workspace(
            root, "new", 18952, "release:sha256:" + "b" * 64, release,
            request_id="canary", display_name="新研究", shared_readonly_paths=[catalog_path],
            connection_catalog=dict(path=str(catalog_path), content_hash=catalog["content_hash"]))
        env = patch.dict(os.environ, DALTON_WORKSPACE_MANIFEST=receipt["manifest_path"])
        env.start()
        self.addCleanup(env.stop)
        state = root / "workspaces/new/state/dalton-core"
        cockpit = CockpitPlane(
            CockpitConfig(core_db=state / "core.sqlite", state_dir=state,
                          heartbeat_path=state / "run/heartbeat.json",
                          scheduler_db=state / "scheduler.sqlite",
                          journal_path=state / "cockpit/journal.sqlite"),
            writer_socket=state / "run/writer.sock", token_config=state / "writer-tokens.json")
        self.addCleanup(cockpit.close)
        # The writer as it stands on a fresh workspace: no discovery plan, no
        # mission.  The review plane reaches it the way it reaches the socket.
        writer = WriterServer(str(state / "core.sqlite"), str(state / "run/writer.sock"), {
            "core": Principal("core", "test-token", CORE_OPERATIONS, unrestricted=True)})
        self.addCleanup(writer._close_store)
        self.addCleanup(writer._close_executors)

        def governance(*_args, operation, params, **_kwargs):
            return getattr(writer, f"_op_{operation}")(dict(params))

        CandidateStagingStore(root / "staging.sqlite").close()
        review = ResearchReviewControlPlane(
            ResearchReviewControlConfig(root / "staging.sqlite", root, 60),
            writer_socket=root / "unused.sock", token_config=root / "unused.json",
            writer=object(), governance_call=governance)
        self.addCleanup(review.close)
        config = AgendaControlConfig.from_mapping({
            "host": "127.0.0.1", "port": 8794, "tailscale_host": HOST,
            "tailscale_executable": sys.executable, "allowed_tailscale_logins": [LOGIN],
            "writer_socket": str(root / "writer.sock"), "token_config": str(root / "tokens.json"),
            "endpoint_ref": "openclaw:discord:test", "feedback_timeout_seconds": 86400,
            "sweep_interval_seconds": 60,
        })
        plane = AgendaControlPlane(config, dashboard_client=Dashboard(), timeout_client=Dashboard())
        app = AgendaControlApplication(config, plane, review, None, cockpit)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            connection.request("GET", path, headers={"Host": HOST, "Tailscale-User-Login": LOGIN})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_every_get_route_answers_without_a_server_error(self):
        routes = http_get_routes()
        # The source scan found the surface, not an empty list.
        self.assertIn("/v1/mission-document-review", routes)
        self.assertIn("/v1/cockpit/overview", routes)
        # Batch ad: the default cockpit (v2), its administrator page, the
        # previous cockpit and the retired review page.
        for page in ("/", "/admin", "/legacy", "/legacy/review", "/weekly-brief"):
            self.assertIn(page, routes)
        self.assertGreater(len(routes), 25)
        for route in routes:
            with self.subTest(route=route):
                status, body = self.get(route + QUERIES.get(route, ""))
                self.assertLess(status, 500, f"{route} -> {status} {body[:200]!r}")
                if status >= 400:
                    # A refusal on a fresh workspace is said in Chinese.
                    message = json.loads(body).get("display_message", "")
                    self.assertTrue(re.search(r"[一-鿿]", message), (route, body[:200]))

    def test_evidence_and_comparison_reads_are_honest_empty_states(self):
        """Batch ah: the evidence chain and the one comparison on a fresh
        workspace answer 200 with an empty state in four parts (G12)."""
        for path in ("/v2/data/evidence/source?claim=claim-version:none",
                     "/v2/data/evidence/document?claim=claim-version:none&source=0",
                     "/v2/data/evidence/statement-line?line=statement-ingest:none%231",
                     "/v2/data/charts/comparison"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200, body[:200])
                value = json.loads(body)
                self.assertEqual(value["status"], "empty")
                self.assertTrue(re.search(r"[一-鿿]", value["empty_reason"]))
                if path.startswith("/v2/data/evidence/"):
                    self.assertTrue(all(value["empty"].get(k) for k in ("state", "impact", "owner", "action")), value)
        status, body = self.get("/v2/data/evidence/source")
        self.assertEqual(status, 400)
        self.assertTrue(re.search(r"[一-鿿]", json.loads(body)["display_message"]))

    def test_document_review_queue_says_no_mission_was_published(self):
        status, body = self.get("/v1/mission-document-review")
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["items"], [])
        self.assertIsNone(value["mission_version_ref"])
        self.assertEqual(value["empty_reason"], "no_published_mission")
        self.assertEqual(value["empty_label"], "还没有发布研究任务")

    def test_company_reads_name_the_missing_mission(self):
        for route in ("/v1/cockpit/model", "/v1/cockpit/research"):
            with self.subTest(route=route):
                status, body = self.get(route + QUERIES[route])
                self.assertEqual(status, 400)
                self.assertIn("还没有发布研究任务", json.loads(body)["display_message"])


if __name__ == "__main__":
    unittest.main()
