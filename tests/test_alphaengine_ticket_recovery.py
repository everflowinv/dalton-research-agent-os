"""Legacy orphan bindings recover through complete, verified acquisitions."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from dalton_core.alphaengine_acquisition_launcher import ReadOnlyAlphaEngineManifestReader, AcquisitionLaunchRejected
from dalton_core.document_extraction import completed_acquisition_manifest


class AlphaEngineTicketRecoveryTests(unittest.TestCase):
    def test_orphan_binding_uses_completed_ticket_but_corruption_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            document = "alphaengine-doc:123"
            refs = ["alphaengine-acquisition:" + char * 24 for char in "ab"]
            manifest = {"id": "manifest:complete", "document_ref": document,
                        "content_hash": "hash", "declared_content_sha256": "digest"}
            for ref, status in zip(refs, ["orphaned", "succeeded"]):
                target = state / "acquisitions" / ref.split(":")[1]
                target.mkdir(parents=True)
                records = {
                    "ticket.json": {"id": ref, "document_ref": document, "status": status,
                                    "started_at": "2026-09-09"},
                    "summary.json": {"document_ref": document, "manifest_ref": manifest["id"],
                                     "manifest_hash": "hash", "manifest_status": "complete",
                                     "assembled_content_sha256": "digest"},
                    "manifest.json": manifest,
                }
                for name, record in records.items():
                    path = target / name
                    path.write_text(json.dumps(record))
                    os.chmod(path, 0o600)
            reader = ReadOnlyAlphaEngineManifestReader(state_dir=state)
            original_ticket = (state / "acquisitions" / ("a" * 24) / "ticket.json").read_bytes()
            self.assertEqual(completed_acquisition_manifest(
                reader, ticket_ref=refs[0], document_ref=document), manifest)
            self.assertEqual((state / "acquisitions" / ("a" * 24) / "ticket.json").read_bytes(), original_ticket)
            # Failed children need not have written a summary or manifest.
            (state / "acquisitions" / ("a" * 24) / "manifest.json").unlink()
            (state / "acquisitions" / ("a" * 24) / "summary.json").unlink()
            self.assertEqual(completed_acquisition_manifest(
                reader, ticket_ref=refs[0], document_ref=document), manifest)
            path = state / "acquisitions" / ("b" * 24) / "summary.json"
            bad = json.loads(path.read_text()); bad["manifest_hash"] = "tampered"
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(AcquisitionLaunchRejected, "disagree"):
                completed_acquisition_manifest(reader, ticket_ref=refs[1], document_ref=document)
            with self.assertRaisesRegex(AcquisitionLaunchRejected, "disagree"):
                completed_acquisition_manifest(reader, ticket_ref=refs[0], document_ref="alphaengine-doc:other")
