import hashlib
import json
import plistlib
import tempfile
import unittest
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.successor_research_publication_transition import (
    FIXED_FILES, LAUNCH_AGENT_NAME, ResearchPublicationTransitionError,
    apply, rollback, validate_transition,
    validate_waiting_checkpoint,
)
from scripts import execute_successor_stopped_window_candidate as execute


class ResearchPublicationTransitionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packet = self.root / "packet"; self.packet.mkdir()
        self.state = self.root / "state"; self.state.mkdir()
        self.launch = self.root / "LaunchAgents"; self.launch.mkdir()
        paths = [(name, "authority", 0o600) for name in sorted(FIXED_FILES)]
        paths += [("research-localization/index.json", "seed", 0o600),
                  ("research-localization/records/" + "a" * 64 + ".json", "seed", 0o600),
                  (LAUNCH_AGENT_NAME, "launch_agent", 0o644)]
        rows = []
        for index, (name, kind, mode) in enumerate(paths):
            data = (plistlib.dumps({"Label": "com.dalton.research-publication-worker",
                    "StartInterval": 300, "RunAtLoad": True, "ProgramArguments": ["/runtime/python", "-m",
                    "dalton_core.research_output_preparation", "run-worker", "--config",
                    "/state/research-publication-worker-config.json"]})
                    if kind == "launch_agent" else
                    (json.dumps({"schema_version": "research-publication-worker-config:0.1",
                     "core_db": str(self.state / "core.sqlite"), "scheduler_db": str(self.state / "scheduler.sqlite"),
                     "model_config": str(self.state / "a.json"), "verifier_config": str(self.state / "b.json"),
                     "checker_config": str(self.state / "c.json"), "brain_config": str(self.state / "d.json"),
                     "work_dir": str(self.state / "research-publication-work"),
                     "output_directory": str(self.state / "research-localization"), "workers": 4,
                     "chunk_chars": 4500, "max_cost_per_call": 1.0, "draft_attempts": 2,
                     "publication_gate": {"release_pointer": "/Users/everflow/Projects/dalton-owner-activation-20260910/current-release.json",
                     "runtime_pointer": "/Users/everflow/Projects/dalton-owner-activation-20260910/current-runtime-config.json",
                     "expected_release_ref": "foundation-r25", "expected_source_commit": "e" * 40}},
                     sort_keys=True).encode() if name == "research-publication-worker-config.json"
                     else (name + "\n").encode()))
            artifact = f"artifacts/{index}"
            target = self.packet / artifact; target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
            rows.append({"path": name, "kind": kind, "artifact": artifact,
                         "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data), "mode": mode})
        self.transition = {"schema_version": "successor-research-publication-transition-0.1",
                           "kind": "exclusive_add",
                           "launch_agent_label": "com.dalton.research-publication-worker",
                           "files": rows}

    def tearDown(self): self.temp.cleanup()

    def test_apply_and_exact_rollback_preserve_runtime_records(self):
        created = apply(packet_root=self.packet, state_dir=self.state,
                        launch_agents_dir=self.launch, transition=self.transition)
        self.assertEqual(len(created), len(self.transition["files"]))
        runtime = self.state / "research-publication-work/result.json"
        runtime.parent.mkdir(); runtime.write_text("business record\n")
        rollback(state_dir=self.state, launch_agents_dir=self.launch,
                 transition=self.transition)
        self.assertTrue(runtime.is_file())
        self.assertFalse(any(path.exists() for path in created))

    def test_executor_installs_starts_and_observes_real_waiting_worker(self):
        created = apply(packet_root=self.packet, state_dir=self.state,
                        launch_agents_dir=self.launch, transition=self.transition)
        manifest = {"research_publication_transition": self.transition}
        worker = execute.SuccessorOrchestrator(self.packet, io.StringIO())
        checkpoint = self.state / "research-publication-work" / "worker-last-run.json"
        checkpoint.parent.mkdir()
        checkpoint.write_text(json.dumps({
            "schema_version": "research-publication-worker-checkpoint:0.1",
            "status": "waiting_for_release_publication", "model_calls": 0,
            "observed_release_sha256": None, "observed_runtime_sha256": None,
            "checked_at": "2026-09-13T01:00:00+00:00",
        }) + "\n")

        def start(label):
            self.assertEqual("com.dalton.research-publication-worker", label)
            config = self.state / "research-publication-worker-config.json"
            runtime = Path(os.environ.get(
                "DALTON_ACCEPTED_RUNTIME_SOURCE_ROOT",
                "/Users/everflow/Projects/dalton-foundation-r25-final-v11-worktree"))
            self.assertTrue((runtime / "src/dalton_core/research_output_preparation.py").is_file())
            self.assertEqual(
                "045484612406452fd31a6eeafcbbed31b166dffd",
                subprocess.check_output(
                    ["git", "-C", str(runtime), "rev-parse", "HEAD"],
                    text=True).strip())
            env = dict(os.environ, PYTHONPATH=str(runtime / "src"))
            completed = subprocess.run(
                [sys.executable, "-m", "dalton_core.research_output_preparation",
                 "run-worker", "--config", str(config)],
                capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(0, completed.returncode, completed.stderr)

        worker.start = start
        with patch.object(execute.r11, "STATE", self.state), \
             patch.object(execute.r11, "LAUNCH_AGENTS", self.launch):
            worker._activate_research_publication_worker(manifest)
        checkpoint_value = json.loads(checkpoint.read_text())
        self.assertEqual("waiting_for_release_publication", checkpoint_value["status"])
        self.assertEqual(0, checkpoint_value["model_calls"])
        plist = self.launch / LAUNCH_AGENT_NAME
        self.assertEqual(0o644, plist.stat().st_mode & 0o777)
        rollback(state_dir=self.state, launch_agents_dir=self.launch,
                 transition=self.transition)
        self.assertFalse(plist.exists())
        self.assertTrue(checkpoint.is_file())

    def test_existing_target_refuses_without_overwrite(self):
        target = self.state / sorted(FIXED_FILES)[0]; target.write_text("owner\n")
        with self.assertRaisesRegex(ResearchPublicationTransitionError, "already exists"):
            apply(packet_root=self.packet, state_dir=self.state,
                  launch_agents_dir=self.launch, transition=self.transition)
        self.assertEqual(target.read_text(), "owner\n")

    def test_apply_refuses_to_skip_launch_agent(self):
        with self.assertRaisesRegex(ResearchPublicationTransitionError,
                                    "requires a LaunchAgents directory"):
            apply(packet_root=self.packet, state_dir=self.state,
                  launch_agents_dir=None, transition=self.transition)
        self.assertEqual([], list(self.state.iterdir()))

    def test_rollback_refuses_changed_installed_file(self):
        apply(packet_root=self.packet, state_dir=self.state,
              launch_agents_dir=self.launch, transition=self.transition)
        target = self.state / sorted(FIXED_FILES)[0]; target.write_text("changed\n")
        with self.assertRaisesRegex(ResearchPublicationTransitionError, "changed"):
            rollback(state_dir=self.state, launch_agents_dir=self.launch,
                     transition=self.transition)
        self.assertEqual(target.read_text(), "changed\n")

    def test_partial_apply_preserves_concurrently_changed_created_file(self):
        import scripts.successor_research_publication_transition as module
        real = module.artifact_bytes; calls = 0
        first = None
        def fail_second(packet, row):
            nonlocal calls, first
            calls += 1
            if calls == 2:
                assert first is not None
                first.write_text("concurrent owner bytes\n")
                raise ResearchPublicationTransitionError("fixture fault")
            data = real(packet, row)
            first = ((self.launch / row["path"])
                     if row["kind"] == "launch_agent" else self.state / row["path"])
            return data
        with patch.object(module, "artifact_bytes", side_effect=fail_second), \
                self.assertRaisesRegex(ResearchPublicationTransitionError,
                                       "preserved concurrently changed"):
            apply(packet_root=self.packet, state_dir=self.state,
                  launch_agents_dir=self.launch, transition=self.transition)
        self.assertEqual(first.read_text(), "concurrent owner bytes\n")

    def test_closed_paths_reject_traversal_and_unlisted_seed(self):
        for path in ("../escape", "research-localization/records/not-a-hash.json",
                     "research-localization/extra.json"):
            bad = {**self.transition, "files": [dict(row) for row in self.transition["files"]]}
            seed = next(row for row in bad["files"] if row["kind"] == "seed")
            seed["path"] = path
            with self.subTest(path=path), self.assertRaises(ResearchPublicationTransitionError):
                validate_transition(bad)

    def test_waiting_checkpoint_is_closed_and_utc(self):
        value = {"schema_version": "research-publication-worker-checkpoint:0.1",
                 "status": "waiting_for_release_publication", "model_calls": 0,
                 "observed_release_sha256": "a" * 64,
                 "observed_runtime_sha256": None,
                 "checked_at": "2026-09-13T17:00:00+00:00"}
        self.assertEqual(value, validate_waiting_checkpoint(value))
        for changed in ({**value, "model_calls": 1},
                        {**value, "status": "active"},
                        {**value, "checked_at": "2026-09-13T17:00:00-04:00"}):
            with self.assertRaises(ResearchPublicationTransitionError):
                validate_waiting_checkpoint(changed)


if __name__ == "__main__": unittest.main()
