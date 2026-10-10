"""#21: the IR-page watcher end to end, against a fake changedetection API.

The child's real HTTP code is exercised: a throwaway ``http.server`` on an
ephemeral loopback port plays changedetection.io's ``/api/v1/watch`` routes,
and the sweep's host-tool runner is replaced by one that runs the real child
in process with the argv the sweep built.  Nothing reaches 127.0.0.1:5055 --
the port a real changedetection may be listening on -- or any other host.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from dalton_core import ir_page_watch_cli, ir_page_watch_core
from dalton_core.ir_page_watch_core import (
    DEFAULT_BASE_URL,
    GOVERNANCE_FILENAME_BY_OPERATION,
    IR_PAGES_ABSENT_REASON,
    OPERATIONS,
    build_ir_page_watch_governance_record,
    is_material,
    sweep_ir_pages,
)
from dalton_core.lane_failure_class import DEPENDENCY_UNAVAILABLE, classify

ACN = "company:sec-cik:0001467373"
NEWS = "https://investor.accenture.com/news-and-events/news-releases"
EVENTS = "https://investor.accenture.com/news-and-events/events-and-presentations"
UNDECLARED = "https://example.com/somebody-elses-page"
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)

DECLARATION = {
    "schema_version": "0.1",
    "id": "ir-page-map:test",
    "companies": [{
        "company_ref": ACN, "ticker": "ACN",
        "pages": [
            {"url": NEWS, "page_kind": "press_releases", "label": "news"},
            {"url": EVENTS, "page_kind": "events", "label": "events"},
        ],
    }],
}

# watch id -> (url, {stamp: snapshot text}).  ``news`` gained a line; ``events``
# changed only in whitespace and blank lines; ``other`` is not declared.
WATCHES = {
    "w-news": (NEWS, {
        "1790000000": "Accenture news\nQ3 results\n",
        "1791000000": "Accenture news\nAccenture signs $1bn deal\nQ3 results\n",
    }),
    "w-events": (EVENTS, {
        "1790000000": "Events\nInvestor day\n",
        "1791000000": "Events\n\n   Investor day   \n\n",
    }),
    "w-other": (UNDECLARED, {
        "1790000000": "a", "1791000000": "b",
    }),
}


class FakeChangedetection(BaseHTTPRequestHandler):
    calls: list[str] = []

    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def _send(self, body: Any, *, text: bool = False) -> None:
        raw = (body if text else json.dumps(body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if text else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        type(self).calls.append(self.path)
        parts = self.path.strip("/").split("/")
        if parts == ["api", "v1", "watch"]:
            self._send({
                watch_id: {"url": url, "title": watch_id, "last_changed": max(history),
                           "last_checked": max(history), "history": list(history),
                           "paused": False}
                for watch_id, (url, history) in WATCHES.items()
            })
            return
        watch_id = parts[3]
        url, history = WATCHES[watch_id]
        if len(parts) == 4:
            self._send({"url": url, "title": watch_id})
        elif len(parts) == 5:
            self._send({stamp: stamp for stamp in history})
        else:
            self._send(history[parts[5]], text=True)


class InProcessRunner:
    """Runs the real child with the sweep's own argv; no Core, no subprocess."""

    def __init__(self, *, command: Any, **kwargs: Any) -> None:
        self.command = command

    def run(self, *, parameters: Any, work_ref: str, output_dir: Any = None) -> Any:
        with tempfile.TemporaryDirectory() as scratch:
            argv = self.command(parameters, Path(scratch), {})
            # argv is [python, -m, dalton_core.ir_page_watch_cli, ...flags]
            args = ir_page_watch_cli.build_parser().parse_args(argv[3:])
            wire = ir_page_watch_cli.run(args)
        return SimpleNamespace(
            observation=wire, raw_response_hash="c" * 64,
            connector_invocation_ref=f"connector-invocation:host-tool:{work_ref}",
        )


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.governance = self.root / "connector-governance"
        self.governance.mkdir()
        for operation in OPERATIONS:
            (self.governance / GOVERNANCE_FILENAME_BY_OPERATION[operation]).write_text(
                json.dumps(build_ir_page_watch_governance_record(
                    operation=operation, approved_by="human:owner", status="approved")),
                encoding="utf-8")
        self.declaration = self.root / "ir-pages.json"
        self.declaration.write_text(json.dumps(DECLARATION), encoding="utf-8")
        FakeChangedetection.calls = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeChangedetection)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.assertNotEqual(self.base_url, DEFAULT_BASE_URL)

    def sweep(self, **overrides: Any) -> dict[str, Any]:
        values = dict(
            store=None, connectors=None, observability=None, spool=None,
            declaration_path=self.declaration, governance_dir=self.governance,
            state_dir=self.root, clock=lambda: NOW, base_url=self.base_url,
        )
        values.update(overrides)
        with mock.patch("dalton_core.host_tool_runner.HostToolRunner", InProcessRunner):
            return sweep_ir_pages(**values)

    def test_a_material_diff_becomes_one_change_cited_by_its_page(self) -> None:
        found = self.sweep()
        self.assertEqual(found["status"], "watched")
        self.assertEqual(found["watch_count"], 3)
        self.assertEqual(found["undeclared_count"], 1)
        # Both declared pages were read; only the one that said something is
        # a change.  The undeclared watch was listed and never read.
        self.assertEqual(found["diff_read_count"], 2)
        self.assertEqual(found["immaterial_count"], 1)
        self.assertEqual(len(found["changes"]), 1)
        change = found["changes"][0]
        self.assertEqual(change["company_ref"], ACN)
        self.assertEqual(change["source_refs"][0], NEWS)
        self.assertEqual(change["payload"]["url"], NEWS)
        self.assertIn("1bn deal", change["payload"]["excerpt"])
        self.assertFalse(any("w-other" in path for path in FakeChangedetection.calls
                             if path != "/api/v1/watch"))

    def test_the_sweep_is_bounded_per_call(self) -> None:
        found = self.sweep(max_diffs=1)
        self.assertEqual(found["diff_read_count"], 1)

    def test_no_declaration_says_so_in_chinese_and_reads_nothing(self) -> None:
        self.declaration.unlink()
        found = self.sweep()
        self.assertEqual(found["status"], "unconfigured")
        self.assertEqual(found["reason"], IR_PAGES_ABSENT_REASON)
        self.assertIn("ir-pages.json", found["reason"])
        self.assertEqual(FakeChangedetection.calls, [])

    def test_a_missing_approval_is_unconfigured_in_chinese(self) -> None:
        (self.governance / GOVERNANCE_FILENAME_BY_OPERATION["get_watch_diff"]).unlink()
        found = self.sweep()
        self.assertEqual(found["status"], "unconfigured")
        self.assertIn("审批", found["reason"])
        self.assertEqual(FakeChangedetection.calls, [])

    def test_a_watcher_that_is_down_is_a_dependency_outage(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(Exception) as ctx:
            self.sweep()
        found = classify(f"{type(ctx.exception).__name__}: {ctx.exception}",
                         status="failed", lane="mission_ownership")
        self.assertEqual(found.failure_class, DEPENDENCY_UNAVAILABLE)
        self.assertEqual(found.dependency, "changedetection")

    def test_materiality(self) -> None:
        self.assertFalse(is_material({"previous_snapshot_hash": "a", "current_snapshot_hash": "a",
                                      "added_line_count": 3, "removed_line_count": 0}))
        self.assertFalse(is_material({"previous_snapshot_hash": "a", "current_snapshot_hash": "b",
                                      "added_line_count": 0, "removed_line_count": 0}))
        self.assertTrue(is_material({"previous_snapshot_hash": "a", "current_snapshot_hash": "b",
                                     "added_line_count": 0, "removed_line_count": 1}))

    def test_production_argv_is_unchanged_when_the_port_is_the_default(self) -> None:
        captured: list[list[str]] = []

        class Capture(InProcessRunner):
            def run(self, *, parameters: Any, work_ref: str, output_dir: Any = None) -> Any:
                captured.append(self.command(parameters, Path("/tmp/x"), {}))
                return SimpleNamespace(observation={"watches": [], "undeclared_count": 0},
                                       raw_response_hash="c" * 64,
                                       connector_invocation_ref="x")

        with mock.patch("dalton_core.host_tool_runner.HostToolRunner", Capture):
            sweep_ir_pages(store=None, connectors=None, observability=None, spool=None,
                           declaration_path=self.declaration,
                           governance_dir=self.governance, state_dir=self.root,
                           clock=lambda: NOW)
        self.assertNotIn("--base-url", captured[0])


class OwnershipLaneIrTests(unittest.TestCase):
    """The lane's half: Chinese reasons, parking on changedetection, recovery."""

    def coordinator(self, ir_watch: Any = None, reason: str | None = None) -> Any:
        from dalton_core.mission_ownership_lane import MissionOwnershipLaneCoordinator

        return MissionOwnershipLaneCoordinator(
            connection=None, state_dir=Path(tempfile.mkdtemp()), launcher=None,
            mission=lambda: None, ir_watch=ir_watch, ir_watch_reason=reason,
            clock=lambda: NOW,
        )

    def test_no_declaration_is_reported_in_chinese(self) -> None:
        from dalton_core.mission_ownership_lane import ir_watch_absent_reason

        launcher = SimpleNamespace(ir_declaration_path=None, ir_governance_dir=None)
        reason = ir_watch_absent_reason(SimpleNamespace(), launcher)
        self.assertEqual(reason, IR_PAGES_ABSENT_REASON)
        found = self.coordinator(reason=reason)._watch_ir_pages({})
        self.assertEqual(found["status"], "unconfigured")
        self.assertIn("ir-pages.json", found["reason"])

    def test_a_down_watcher_parks_and_is_not_asked_every_tick(self) -> None:
        calls: list[int] = []

        def down() -> dict:
            calls.append(1)
            raise ir_page_watch_cli.IrPageWatchRunError(
                "changedetection is not answering at http://127.0.0.1:5055/api/v1/watch: "
                "<urlopen error [Errno 61] Connection refused>")

        lane = self.coordinator(ir_watch=down)
        first = lane._watch_ir_pages({})
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure"]["action"], "parked")
        self.assertIn("changedetection", first["display_reason"])
        # The free probe after a park, then the interval holds it.
        lane._watch_ir_pages({})
        third = lane._watch_ir_pages({})
        self.assertEqual(third["status"], "parked")
        self.assertEqual(len(calls), 2)

        lane.ir_watch = lambda: {"status": "watched", "changes": []}
        lane.budget._probe_spent.clear()
        recovered = lane._watch_ir_pages({})
        self.assertEqual(recovered["status"], "watched")
        self.assertIsNone(lane.budget.parked("ir-pages"))


if __name__ == "__main__":
    unittest.main()
