"""Downloads are scoped, traceable, and never update the research database."""

import base64
import hashlib
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from dalton_core.cockpit_plane import CockpitError, CockpitPlane
from tests.test_mission_deliverable import ACN, DeliverableHarness


class ResearchDownloadTests(DeliverableHarness):
    def setUp(self):
        super().setUp()
        self.grant("deliverable")
        self.document = self.publish([
            {"title": "Thesis", "body": "Evidence and an explicit unknown.",
             "claim_refs": [], "numbers": [], "gaps": ["Margin outlook unknown"]}
        ])
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "core.sqlite"
        destination = sqlite3.connect(self.db)
        self.store.connection.backup(destination)
        destination.close()
        self.plane = CockpitPlane.__new__(CockpitPlane)
        self.plane.config = SimpleNamespace(core_db=self.db, mission_ref=self.mission_ref)

    def test_html_download_contains_real_product_and_preserves_source(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        response = self.plane.export_research(ACN, "html")
        raw = base64.b64decode(response["content_base64"], validate=True)
        self.assertEqual(response["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIn(self.document["id"].encode(), raw)
        self.assertIn(b"Margin outlook unknown", raw)
        self.assertEqual(response["manifest"]["mission_version_hash"], self.mission["content_hash"])
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertTrue(response["filename"].endswith(".html"))

    def test_scope_and_format_are_checked_before_rendering(self):
        with self.assertRaisesRegex(CockpitError, "范围"):
            self.plane.export_research("company:outside", "html")
        with self.assertRaisesRegex(CockpitError, "HTML"):
            self.plane.export_research(ACN, "../../core.sqlite")

    def test_missing_forecast_does_not_download_a_fabricated_model(self):
        with self.assertRaises(CockpitError):
            self.plane.export_research(ACN, "xlsx")
