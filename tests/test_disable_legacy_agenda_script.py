"""scripts/disable_legacy_agenda.py: plan by default, backup before apply."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "disable_legacy_agenda", ROOT / "scripts" / "disable_legacy_agenda.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

NOW = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)


def _document(**extra: object) -> dict:
    return {
        "schema_version": "0.1",
        "agenda": {"enabled": True, "interval_seconds": 3600, "config": {
            "company_ref": "wanhua", "expected_agent_id": "chem",
            "routing_policy_ref": "model-routing-policy-version:dalton-openclaw:11"}},
        "tick_seconds": 5,
        **extra,
    }


class DisableLegacyAgendaTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.service = self.dir / "service.json"

    def write(self, document: dict) -> bytes:
        body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        self.service.write_bytes(body)
        os.chmod(self.service, 0o600)
        return body

    def test_default_is_a_read_only_plan(self) -> None:
        before = self.write(_document())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(script.main(["--service", str(self.service)]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["action"], "disable")
        self.assertFalse(result["writes_performed"])
        self.assertEqual(result["agenda_company_ref"], "wanhua")
        self.assertEqual(self.service.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["service.json"])

    def test_apply_backs_up_then_flips_only_the_flag(self) -> None:
        before = self.write(_document())
        result = script.apply(self.service, now=NOW, validate=lambda document: None)
        self.assertTrue(result["writes_performed"])
        backup = self.dir / "service.pre-agenda-retire-20260930T220000Z.json"
        self.assertEqual(result["backup"], str(backup))
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.service.stat().st_mode), 0o600)
        after = json.loads(self.service.read_text())
        expected = _document()
        expected["agenda"]["enabled"] = False
        self.assertEqual(after, expected)  # config block and everything else kept
        # Idempotent: a second run has nothing to do and writes nothing.
        again = script.apply(self.service, now=NOW, validate=lambda document: None)
        self.assertEqual(again["action"], "none")
        self.assertFalse(again["writes_performed"])

    def test_apply_mode_word_is_accepted(self) -> None:
        self.write(_document())
        # The minimal document is not a full service config, so skip the real
        # validator (covered by its own test below) by wrapping ``apply``.
        original_apply = script.apply
        self.addCleanup(setattr, script, "apply", original_apply)
        script.apply = lambda path: original_apply(path, now=NOW,
                                                   validate=lambda document: None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(["apply", "--service", str(self.service)]), 0)
        self.assertFalse(json.loads(self.service.read_text())["agenda"]["enabled"])

    def test_an_explicit_opt_in_is_refused_and_untouched(self) -> None:
        before = self.write(_document(legacy_agenda_plane=True))
        with self.assertRaises(script.RefusedError):
            script.apply(self.service, now=NOW, validate=lambda document: None)
        self.assertEqual(self.service.read_bytes(), before)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(["apply", "--service", str(self.service)]), 2)
        self.assertIn("legacy_agenda_plane", err.getvalue())

    def test_an_invalid_result_is_not_written(self) -> None:
        before = self.write(_document())

        def refuse(document: dict) -> None:
            raise ValueError("would not parse")

        with self.assertRaises(ValueError):
            script.apply(self.service, now=NOW, validate=refuse)
        self.assertEqual(self.service.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["service.json"])

    def test_real_validator_accepts_a_disabled_agenda(self) -> None:
        # The flag flip must leave a document the controller still parses.
        from dalton_core.service import ServiceConfig

        root = self.dir
        document = {
            "schema_version": "0.1", "core_db": str(root / "core.sqlite"),
            "scheduler_db": str(root / "scheduler.sqlite"),
            "projection_db": str(root / "projection.sqlite"),
            "model_router_db": str(root / "router.sqlite"),
            "capability_catalog_db": str(root / "catalog.sqlite"),
            "heartbeat_path": str(root / "run" / "heartbeat.json"),
            "writer_socket": str(root / "run" / "writer.sock"),
            "tick_seconds": 5, "plugin_retry_seconds": 60, "plugins": [],
            "agenda": {"enabled": False, "interval_seconds": 3600, "config": {}},
        }
        script._validate(document)
        self.assertIsNone(ServiceConfig.from_mapping(document).agenda)


if __name__ == "__main__":
    unittest.main()
