"""Fail-closed tests for the review evidence collector."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_review_evidence.py"
SPEC = importlib.util.spec_from_file_location("collect_review_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


class ReviewEvidenceCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        (self.root / "src").mkdir()
        (self.root / "docs" / "vision.md").write_text(
            "# Vision\nKeep authority exact.\n", encoding="utf-8"
        )
        (self.root / "src" / "control.py").write_text(
            "CONTROL = 'fail-closed'\n", encoding="utf-8"
        )
        self.manifest_path = self.root / "manifest.json"
        self.output = self.root / "out" / "evidence.md"

    def manifest(self, **overrides) -> dict:
        value = {
            "schema_version": "0.1",
            "name": "collector-test",
            "documents": ["docs/vision.md"],
            "implementation": ["src/control.py"],
            "commands": [{
                "label": "nonempty-command",
                "argv": [sys.executable, "-c", "print('command evidence')"],
                "timeout_seconds": 10,
            }],
        }
        value.update(overrides)
        self.manifest_path.write_text(
            json.dumps(value, sort_keys=True), encoding="utf-8"
        )
        return value

    def collect(self) -> dict:
        return collector.collect(
            root=self.root,
            manifest_path=self.manifest_path,
            output=self.output,
        )

    def test_collects_nonempty_document_implementation_and_command_blocks(self) -> None:
        self.manifest()
        result = self.collect()
        rendered = self.output.read_text(encoding="utf-8")
        self.assertEqual(result["status"], "collected")
        self.assertEqual(result["document_count"], 1)
        self.assertEqual(result["implementation_count"], 1)
        self.assertEqual(result["command_count"], 1)
        self.assertIn("Keep authority exact.", rendered)
        self.assertIn("CONTROL = 'fail-closed'", rendered)
        self.assertIn("command evidence", rendered)
        self.assertIn("shell=false; fail-closed", rendered)

    def test_missing_file_fails_without_publishing_output(self) -> None:
        self.manifest(documents=["docs/missing.md"])
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "does not exist"
        ):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_empty_file_fails_without_publishing_output(self) -> None:
        (self.root / "docs" / "vision.md").write_text(" \n", encoding="utf-8")
        self.manifest()
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "evidence is empty"
        ):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_nonzero_command_fails_without_publishing_output(self) -> None:
        self.manifest(commands=[{
            "label": "failing-command",
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
            "timeout_seconds": 10,
        }])
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, r"command failed \(7\)"
        ):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_empty_command_evidence_fails_without_publishing_output(self) -> None:
        self.manifest(commands=[{
            "label": "empty-command",
            "argv": [sys.executable, "-c", "pass"],
            "timeout_seconds": 10,
        }])
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "command evidence is empty"
        ):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_path_escape_and_output_overwrite_are_rejected(self) -> None:
        outside = self.root.parent / "outside-review-evidence.md"
        outside.write_text("outside\n", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        self.manifest(documents=["../outside-review-evidence.md"])
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "escapes repository root"
        ):
            self.collect()
        self.manifest()
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "cannot overwrite selected evidence"
        ):
            collector.collect(
                root=self.root,
                manifest_path=self.manifest_path,
                output=self.root / "docs" / "vision.md",
            )

    def test_existing_output_is_rejected_to_prevent_stale_evidence(self) -> None:
        self.manifest()
        self.output.parent.mkdir()
        self.output.write_text("stale\n", encoding="utf-8")
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "output already exists"
        ):
            self.collect()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "stale\n")

    def test_manifest_shape_is_closed_and_categories_cannot_be_empty(self) -> None:
        value = self.manifest()
        value["unexpected"] = True
        self.manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError, "manifest keys mismatch"
        ):
            self.collect()
        self.manifest(implementation=[])
        with self.assertRaisesRegex(
            collector.EvidenceCollectionError,
            "implementation must contain at least one file",
        ):
            self.collect()
