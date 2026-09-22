import gzip
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dalton_core.raw_spool import RawSpoolReader


class ArchiveReadRaceTests(unittest.TestCase):
    def test_reader_crossing_archive_publication_uses_verified_compressed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = b"original evidence" * 100
            digest = hashlib.sha256(payload).hexdigest()
            root = (Path(directory) / "connector-spool").resolve()
            shard = root / "objects" / digest[:2]
            shard.mkdir(parents=True)
            plain = shard / digest
            plain.write_bytes(payload)
            archive = plain.with_name(digest + ".gz")
            original_open = os.open

            def concurrently_archive(path, flags, *args, **kwargs):
                if Path(path) == plain:
                    archive.write_bytes(gzip.compress(payload, mtime=0))
                    plain.unlink()
                return original_open(path, flags, *args, **kwargs)

            with patch("dalton_core.raw_spool.os.open", side_effect=concurrently_archive):
                self.assertEqual(RawSpoolReader(root).read_object(digest), payload)

    def test_reader_crossing_restore_publication_retries_plain_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = b"archived evidence" * 100
            digest = hashlib.sha256(payload).hexdigest()
            root = (Path(directory) / "connector-spool").resolve()
            shard = root / "objects" / digest[:2]
            shard.mkdir(parents=True)
            plain = shard / digest
            archive = plain.with_name(digest + ".gz")
            archive.write_bytes(gzip.compress(payload, mtime=0))
            original_open = os.open
            opened = []

            def concurrently_restore(path, flags, *args, **kwargs):
                opened.append(Path(path))
                if Path(path) == archive:
                    plain.write_bytes(payload)
                    archive.unlink()
                return original_open(path, flags, *args, **kwargs)

            with patch("dalton_core.raw_spool.os.open", side_effect=concurrently_restore):
                self.assertEqual(RawSpoolReader(root).read_object(digest), payload)
            self.assertEqual(opened, [archive, plain])
