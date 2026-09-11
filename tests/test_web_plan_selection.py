"""Selected web acquisition policy survives LaunchAgent regeneration."""
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.macos_launchagent import WEB_PLAN_SELECTOR, render
from dalton_core.mission_source_discovery import load_discovery_plan
from dalton_core.store import content_hash


class WebPlanSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = self.root / "state"
        self.plans = self.state / "discovery-plans"
        self.plans.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "deploy/phase9/p9d4-us-it-services-web-search-plan-v3.json"
        self.plan = load_discovery_plan(source)
        self.plan.update(schema_version="0.5", id="discovery-plan:web:cooldown")
        self.plan["acquisition"]["failure_cooldown"] = {
            "minimum_distinct_urls": 3, "window_seconds": 86400,
            "cooldown_seconds": 21600,
        }
        self.seal(self.plan)
        self.path = self.plans / "web-cooldown.json"
        self.path.write_text(json.dumps(self.plan))
        self.selector = {
            "schema_version": "web-discovery-plan-selection-0.1",
            "id": "web-plan-selection:test", "status": "approved",
            "source_ref": "source:web-search", "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"], "plan_path": self.path.name,
        }

    @staticmethod
    def seal(value):
        value["content_hash"] = content_hash({k: v for k, v in value.items()
                                              if k != "content_hash"})

    def select(self):
        self.seal(self.selector)
        (self.plans / WEB_PLAN_SELECTOR).write_text(json.dumps(self.selector))

    def argv(self):
        paths = render(self.root / "agents", self.root / "bin", self.state,
                       self.root / "absent.json", self.root / "logs")
        return plistlib.loads(Path(paths["writer"]).read_bytes())["ProgramArguments"]

    def test_default_and_selected_plan_survive_repeated_render(self):
        args = self.argv()
        self.assertEqual(args[args.index("--web-search-discovery-plan") + 1],
                         str(self.plans / "us-it-services-web-search-v3.json"))
        original = self.path.read_bytes()
        self.select()
        for _ in range(2):
            args = self.argv()
            self.assertEqual(args[args.index("--web-search-discovery-plan") + 1], str(self.path))
            self.assertEqual(args[args.index("--sec-filings-discovery-plan") + 1],
                             str(self.plans / "us-it-services-sec-filings-v1.json"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_changed_policy_bytes_cannot_borrow_selection(self):
        self.select()
        self.plan["acquisition"]["failure_cooldown"]["cooldown_seconds"] += 1
        self.seal(self.plan)
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "ref, hash, and source"):
            self.argv()

    def test_foreign_source_proposed_selection_and_path_escape_refused(self):
        for key, value in (("source_ref", "source:sec-edgar"),
                           ("status", "proposed"), ("plan_path", "../outside.json")):
            with self.subTest(key=key):
                old = self.selector[key]
                self.selector[key] = value
                self.select()
                with self.assertRaises(ValueError):
                    self.argv()
                self.selector[key] = old

    def test_dangling_selector_does_not_silently_restore_default(self):
        (self.plans / WEB_PLAN_SELECTOR).symlink_to(self.plans / "missing.json")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            self.argv()
