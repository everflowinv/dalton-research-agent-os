"""The service config of an environment whose state lives behind a symlink.

The legacy installation keeps ``~/Library/Application Support/Dalton/config``
where it has always been and points ``.../Dalton/state`` at an external volume.
Every derivation of ``<root>/config/service.json`` used to call ``resolve()``
first, so it asked the filesystem where the *bytes* are rather than which
installation the directory *belongs to*, landed on
``/Volumes/EveSSD/Dalton/config/service.json`` -- a path that has never existed
-- and reported four pinned stages as unconfigured.  A workspace environment
has no symlink, so the two environments disagreed and neither was obviously
wrong.

These tests pin the rule both ways round: a symlinked state directory finds the
config of the installation it was named through, and an installation that
genuinely has no ``service.json`` still reads as having none.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dalton_core.model_selection import purpose_policy_bindings
from dalton_core.service_config_location import (
    service_config_candidates,
    service_config_path,
)
from dalton_core.thesis_impact_production import (
    ThesisImpactProductionError,
    thesis_impact_runtime_config,
)

SERVICE_PINS = {
    "plan": "model-routing-policy-version:pin-planner:9",
    "agenda_planning": "model-routing-policy-version:pin-agenda:11",
    "thesis_impact_assessment": "model-routing-policy-version:pin-assessment:9",
    "thesis_impact_verifier": "model-routing-policy-version:pin-verifier:4",
}


def service_document() -> dict:
    """The four pins ``_SERVICE_PURPOSE_PINS`` reads, and nothing else."""

    return {
        "schema_version": "0.1",
        "bounded_planner": {"enabled": True, "config": {
            "planner_routing_policy_ref": SERVICE_PINS["plan"]}},
        "agenda": {"enabled": True, "config": {
            "routing_policy_ref": SERVICE_PINS["agenda_planning"]}},
        "thesis_impact": {"enabled": True, "config": {
            "assessment_routing_policy_ref":
                SERVICE_PINS["thesis_impact_assessment"],
            "verifier_routing_policy_ref":
                SERVICE_PINS["thesis_impact_verifier"],
            "budget_db": "/nowhere/budget.sqlite"}},
    }


class SymlinkedStateDirectoryTests(unittest.TestCase):
    """The legacy shape: ``<root>/state`` is a symlink to another volume."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.root = self.tmp / "install"
        (self.root / "config").mkdir(parents=True)
        self.service = self.root / "config" / "service.json"
        self.service.write_text(json.dumps(service_document()), encoding="utf-8")
        self.volume = self.tmp / "volume" / "legacy-state"
        self.stored_state = self.volume / "dalton-core"
        self.stored_state.mkdir(parents=True)
        os.symlink(self.volume, self.root / "state")
        #: How the owner, the LaunchAgent and ``service.json`` all name it.
        self.named_state = self.root / "state" / "dalton-core"

    def test_the_named_path_finds_the_installation_it_belongs_to(self) -> None:
        self.assertEqual(service_config_path(self.named_state), self.service)

    def test_resolving_first_would_have_missed_it(self) -> None:
        """The exact derivation this module replaces, kept as the contrast."""

        old = (Path(self.named_state).expanduser().resolve().parents[1]
               / "config" / "service.json")
        self.assertNotEqual(old, self.service)
        self.assertFalse(old.is_file())

    def test_a_caller_that_already_resolved_still_finds_it(self) -> None:
        """Only when the resolved layout happens to hold the config."""

        resolved_root = self.volume.parent
        (resolved_root / "config").mkdir(parents=True)
        (resolved_root / "config" / "service.json").write_text("{}", encoding="utf-8")
        self.assertEqual(service_config_path(self.stored_state),
                         resolved_root / "config" / "service.json")

    def test_the_named_derivation_is_preferred_over_the_resolved_one(self) -> None:
        resolved_root = self.volume.parent
        (resolved_root / "config").mkdir(parents=True)
        (resolved_root / "config" / "service.json").write_text("{}", encoding="utf-8")
        self.assertEqual(service_config_path(self.named_state), self.service)
        self.assertEqual(service_config_candidates(self.named_state)[0], self.service)

    def test_every_service_pinned_stage_resolves_through_the_symlink(self) -> None:
        bindings = purpose_policy_bindings(self.named_state)
        for purpose, ref in SERVICE_PINS.items():
            self.assertEqual(bindings[purpose]["status"], "configured", purpose)
            self.assertEqual(bindings[purpose]["policy_version_ref"], ref, purpose)
            self.assertTrue(bindings[purpose]["source"].startswith(str(self.service)))

    def test_the_planner_fallback_no_longer_hides_the_service_pin(self) -> None:
        """A local planner config must not stand in for the resident pin.

        ``plan`` is the one service-pinned stage with a file fallback, which is
        why it was the only one that appeared to survive the bug -- while in
        fact reporting the file's policy rather than the one the running
        planner is pinned to.
        """

        (self.named_state / "research-planner-model-config.json").write_text(
            json.dumps({"routing_policy_ref": "model-routing-policy-version:file:62"}),
            encoding="utf-8")
        binding = purpose_policy_bindings(self.named_state)["plan"]
        self.assertEqual(binding["policy_version_ref"], SERVICE_PINS["plan"])

    def test_a_runtime_config_reader_sees_the_same_file(self) -> None:
        """The lane's own on/off switch reads the file, not an absence.

        ``thesis_impact_runtime_config`` returns ``None`` when there is no
        service config, which is indistinguishable from "the owner turned the
        lane off".  Writing an enabled block whose config is malformed makes
        the difference observable: a reader that found the file complains
        about its contents, and a reader that did not returns ``None``.
        """

        document = service_document()
        document["thesis_impact"]["config"] = "not a mapping"
        self.service.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ThesisImpactProductionError):
            thesis_impact_runtime_config(self.named_state)


class AbsentServiceConfigTests(unittest.TestCase):
    """An installation with no ``service.json`` still has none."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.root = self.tmp / "install"
        self.volume = self.tmp / "volume" / "legacy-state"
        self.stored_state = self.volume / "dalton-core"
        self.stored_state.mkdir(parents=True)
        self.root.mkdir(parents=True)
        os.symlink(self.volume, self.root / "state")
        self.named_state = self.root / "state" / "dalton-core"

    def test_a_missing_config_is_reported_at_the_named_location(self) -> None:
        derived = service_config_path(self.named_state)
        self.assertEqual(derived, self.root / "config" / "service.json")
        self.assertFalse(derived.is_file())

    def test_a_missing_config_leaves_every_pinned_stage_unconfigured(self) -> None:
        bindings = purpose_policy_bindings(self.named_state)
        for purpose in SERVICE_PINS:
            self.assertEqual(bindings[purpose]["status"], "unconfigured", purpose)
            self.assertIsNone(bindings[purpose]["policy_version_ref"], purpose)
        self.assertEqual(bindings["human_intent"]["status"], "unconfigured")

    def test_an_unreadable_config_is_never_borrowed_from_elsewhere(self) -> None:
        """A directory outside any installation gets no config, not a stranger's."""

        stray = self.tmp / "elsewhere" / "state" / "dalton-core"
        stray.mkdir(parents=True)
        self.assertFalse(service_config_path(stray).is_file())
        self.assertIsNone(thesis_impact_runtime_config(stray))


class DerivationRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # Resolved here on purpose: macOS puts temporary directories under
        # ``/var``, which is itself a symlink, and these three tests are about
        # the rule rather than about a link above the installation.
        self.tmp = Path(directory.name).resolve()

    def test_a_relative_path_is_made_absolute_without_following_links(self) -> None:
        root = self.tmp / "install"
        (root / "config").mkdir(parents=True)
        (root / "state" / "dalton-core").mkdir(parents=True)
        (root / "config" / "service.json").write_text("{}", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(root / "state")
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(service_config_path("dalton-core"),
                         Path(os.path.abspath(root / "config" / "service.json")))

    def test_candidates_are_deduplicated_when_no_link_is_involved(self) -> None:
        state = self.tmp / "install" / "state" / "dalton-core"
        state.mkdir(parents=True)
        self.assertEqual(len(service_config_candidates(state)), 1)

    def test_a_root_level_directory_has_no_installation_to_name(self) -> None:
        self.assertEqual(service_config_candidates("/"), ())
        self.assertFalse(service_config_path("/").is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
