from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_release_copied_state_rehearsal import (
    RehearsalBindingError,
    _canonical_sha256,
    _verify_frozen_source,
    validate_final_snapshots,
)


class _IdentityRehearsalModule:
    @staticmethod
    def model_config_inventory(state_dir: Path):
        return {
            path.name: json.loads(path.read_text())
            for path in sorted(state_dir.glob("*-model-config.json"))
        }

    @staticmethod
    def rewrite_paths(value, _replacements):
        return value

    @staticmethod
    def invert(replacements):
        return replacements


class ReleaseCopiedStateRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "service.json"
        self.service = {"backup": {"enabled": True, "keep_latest": 3}}
        self.config.write_text(json.dumps(self.service), encoding="utf-8")
        self.rehearsal = SimpleNamespace(
            temp_state=self.state,
            temp_config=self.config,
            replacements={},
        )

    def _models(self, count: int):
        models = {
            f"role-{index}-model-config.json": {"routing_policy_ref": f"route:{index}"}
            for index in range(count)
        }
        for name, value in models.items():
            (self.state / name).write_text(json.dumps(value), encoding="utf-8")
        return models

    def test_dynamic_inventory_accepts_fourteen_or_fifteen_without_a_stage_constant(self) -> None:
        for count in (14, 15):
            with self.subTest(count=count):
                for path in self.state.glob("*.json"):
                    path.unlink()
                models = self._models(count)
                result = validate_final_snapshots(
                    _IdentityRehearsalModule,
                    self.rehearsal,
                    expected_models=models,
                    expected_service=self.service,
                )
                self.assertEqual(result["model_config_count"], count)
                self.assertEqual(result["model_config_semantic_sha256"], _canonical_sha256(models))

    def test_model_drift_is_rejected_after_the_tick(self) -> None:
        models = self._models(15)
        (self.state / "role-0-model-config.json").write_text(
            json.dumps({"routing_policy_ref": "changed"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(RehearsalBindingError, "model configuration drifted"):
            validate_final_snapshots(
                _IdentityRehearsalModule,
                self.rehearsal,
                expected_models=models,
                expected_service=self.service,
            )

    def test_service_drift_is_rejected_after_the_tick(self) -> None:
        models = self._models(14)
        changed = {"backup": {"enabled": True, "keep_latest": 4}}
        self.config.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(RehearsalBindingError, "service configuration drifted"):
            validate_final_snapshots(
                _IdentityRehearsalModule,
                self.rehearsal,
                expected_models=models,
                expected_service=self.service,
            )

    def test_source_mutated_during_fake_run_is_rejected_before_binding(self) -> None:
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        tracked = source / "tracked.txt"
        tracked.write_text("frozen\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git", "-C", str(source), "-c", "user.name=Dalton Test",
                "-c", "user.email=dalton@example.invalid", "commit", "-qm", "freeze",
            ],
            check=True,
        )
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        _verify_frozen_source(source, commit)  # fake pre-run preflight
        tracked.write_text("mutated while rehearsal ran\n", encoding="utf-8")
        with self.assertRaisesRegex(RehearsalBindingError, "source checkout is not clean"):
            _verify_frozen_source(source, commit)  # fake pre-binding check


if __name__ == "__main__":
    unittest.main()
