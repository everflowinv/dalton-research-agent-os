from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dalton_core.connector_authority_port import ReadOnlyConnectorReceiptReader
from dalton_core.raw_spool import RawSpool, RawSpoolReader, RawSpoolError


class DocumentReadPortTests(unittest.TestCase):
    def test_spool_reader_never_creates_or_chmods_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = RawSpool(root, max_total_bytes=1024)
            sink = spool.open_sink("raw-sink:" + "a" * 64, max_response_bytes=100)
            sink.write(b"previously unclaimed source paragraph")
            record = sink.finalize()
            with patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")), \
                 patch.object(os, "chmod", side_effect=AssertionError("chmod")):
                reader = RawSpoolReader(root)
                self.assertEqual(reader.read_object(record.content_hash), b"previously unclaimed source paragraph")
                with self.assertRaises(RawSpoolError):
                    RawSpoolReader(root / "missing")
            self.assertFalse((root / "missing").exists())
            self.assertFalse(hasattr(reader, "open_sink"))
            self.assertFalse(hasattr(reader, "gc_orphans"))

    def test_spool_reader_rejects_symlinked_object_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = RawSpool(root, max_total_bytes=1024)
            sink = spool.open_sink("raw-sink:" + "b" * 64, max_response_bytes=100)
            sink.write(b"retained text")
            record = sink.finalize()
            prefix = root / "connector-spool/objects" / record.content_hash[:2]
            moved = root / "moved"
            prefix.rename(moved)
            prefix.symlink_to(moved, target_is_directory=True)
            with self.assertRaises(RawSpoolError):
                RawSpoolReader(root).read_object(record.content_hash)

    def test_receipt_reader_uses_select_only_and_keeps_callers_connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE connector_profile_versions (profile_version_id TEXT, record_json TEXT)")
        connection.execute("CREATE TABLE connector_source_envelopes (source_envelope_id TEXT, record_json TEXT)")
        expected = {"id": "profile:existing", "content_hash": "a" * 64}
        connection.execute("INSERT INTO connector_profile_versions VALUES (?,?)", (expected["id"], json.dumps(expected)))
        connection.commit()
        seen = []

        def authorize(action, *_):
            seen.append(action)
            return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ} else sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        reader = ReadOnlyConnectorReceiptReader(connection)
        self.assertEqual(reader.get_profile(expected["id"]), expected)
        self.assertIsNone(reader.get_source_envelope("source:absent"))
        self.assertFalse(hasattr(reader, "register_profile"))
        self.assertFalse(hasattr(reader, "close"))
        self.assertTrue(seen)
        connection.execute("SELECT 1").fetchone()
