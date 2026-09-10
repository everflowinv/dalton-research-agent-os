from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.macos_launchagent import SEC_PLAN_SELECTOR, render
from dalton_core.store import content_hash
from scripts.build_sec_8k_discovery_proposal import build_selector_proposal
from tests.test_sec_8k_discovery_proposal import Sec8KDiscoveryProposalTests


class SecPlanSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.plans = self.state / "discovery-plans"
        self.plans.mkdir(parents=True)

    def argv(self):
        paths = render(self.root / "agents", self.root / "bin", self.state,
                       self.root / "missing-config.json", self.root / "logs")
        return plistlib.loads(Path(paths["writer"]).read_bytes())["ProgramArguments"]

    def selected(self):
        argv = self.argv()
        return argv[argv.index("--sec-filings-discovery-plan") + 1]

    def write_plan_and_selector(self):
        candidate = Sec8KDiscoveryProposalTests().active_plan()
        candidate = {**candidate, "id": "discovery-plan:us-it-services:sec-filings:2"}
        candidate["content_hash"] = content_hash(
            {key: value for key, value in candidate.items() if key != "content_hash"})
        plan_path = self.plans / "us-it-services-sec-filings-v2.json"
        plan_path.write_text(json.dumps(candidate), encoding="utf-8")
        selector = build_selector_proposal(candidate)
        selector["status"] = "approved"
        selector["content_hash"] = content_hash(
            {key: value for key, value in selector.items() if key != "content_hash"})
        (self.plans / SEC_PLAN_SELECTOR).write_text(json.dumps(selector), encoding="utf-8")
        return candidate, selector, plan_path

    def test_absent_selector_keeps_the_fixed_v1_plan(self):
        self.assertEqual(
            self.selected(),
            str(self.plans.resolve() / "us-it-services-sec-filings-v1.json"),
        )

    def test_approved_selector_renders_the_exact_v2_plan(self):
        _plan, _selector, path = self.write_plan_and_selector()
        self.assertEqual(self.selected(), str(path.resolve()))

    def test_dangling_selector_is_invalid_instead_of_defaulting_to_v1(self):
        (self.plans / SEC_PLAN_SELECTOR).symlink_to(self.plans / "missing-selector")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            self.argv()

    def test_tampered_plan_is_refused(self):
        _plan, _selector, path = self.write_plan_and_selector()
        value = json.loads(path.read_text())
        value["budget"]["max_calls_24h"] += 1
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex((ValueError, RuntimeError), "hash"):
            self.argv()

    def test_tampered_selector_is_refused(self):
        _plan, selector, _path = self.write_plan_and_selector()
        selector["plan_hash"] = "0" * 64
        (self.plans / SEC_PLAN_SELECTOR).write_text(json.dumps(selector))
        with self.assertRaisesRegex(ValueError, "selector content hash"):
            self.argv()

    def test_selector_with_a_different_plan_ref_is_refused(self):
        _plan, selector, _path = self.write_plan_and_selector()
        selector["plan_ref"] = "discovery-plan:us-it-services:sec-filings:99"
        selector["content_hash"] = content_hash(
            {key: value for key, value in selector.items() if key != "content_hash"})
        (self.plans / SEC_PLAN_SELECTOR).write_text(json.dumps(selector))
        with self.assertRaisesRegex(ValueError, "ref, hash, and source"):
            self.argv()

    def test_path_escape_is_refused_even_when_selector_hash_matches(self):
        _plan, selector, _path = self.write_plan_and_selector()
        selector["plan_path"] = "../outside.json"
        selector["content_hash"] = content_hash(
            {key: value for key, value in selector.items() if key != "content_hash"})
        (self.plans / SEC_PLAN_SELECTOR).write_text(json.dumps(selector))
        with self.assertRaisesRegex(ValueError, "discovery-plans"):
            self.argv()

    def test_proposed_selector_does_not_activate(self):
        plan, _selector, _path = self.write_plan_and_selector()
        proposed = build_selector_proposal(plan)
        (self.plans / SEC_PLAN_SELECTOR).write_text(json.dumps(proposed))
        with self.assertRaisesRegex(ValueError, "not an approved"):
            self.argv()

    def test_builder_binds_selector_to_candidate_ref_hash_and_filename(self):
        candidate, _selector, _path = self.write_plan_and_selector()
        selector = build_selector_proposal(candidate)
        self.assertEqual(selector["status"], "proposed")
        self.assertEqual(selector["plan_ref"], candidate["id"])
        self.assertEqual(selector["plan_hash"], candidate["content_hash"])
        self.assertEqual(selector["plan_path"], "us-it-services-sec-filings-v2.json")
        self.assertEqual(selector["content_hash"], content_hash(
            {key: value for key, value in selector.items() if key != "content_hash"}))


if __name__ == "__main__":
    unittest.main()
