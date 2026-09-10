"""Deployment wiring for the dossier and earnings producer/verifier pairs."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.mission_dossier_lane import argv_fragment as dossier_argv
from dalton_core.mission_earnings_season_lane import argv_fragment as earnings_argv
from dalton_core.mission_deep_insight_lane import argv_fragment as deep_insight_argv
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import install
from tests.test_document_extraction_setup import _service
from tests.test_openclaw_catalog_reconcile import _config


class DeploymentModelPairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = _service(self.root)
        service = json.loads(self.config.read_text("utf-8"))
        self.state = Path(service["core_db"]).resolve().parent
        self.context = type("Context", (), {"state": self.state})()
        # Production synchronizes the current broker catalog before installing
        # role policies. Role setup must not resurrect static profile seeds.
        with ModelRouter(service["model_router_db"]) as router:
            sync_openclaw_model_catalog(router, _config(),
                                       checked_at=datetime.now(timezone.utc))

    def install_pair(self, producer_policy: str, producer_file: str,
                     verifier_policy: str, verifier_file: str) -> None:
        install(self.config, tier="brain", policy_id=producer_policy,
                config_file_name=producer_file)
        install(self.config, tier="verifier", policy_id=verifier_policy,
                config_file_name=verifier_file)

    def test_dossier_pair_becomes_the_real_lane_argv(self) -> None:
        self.install_pair(
            "model-routing-policy:dalton-openclaw-company-dossier",
            "dossier-model-config.json",
            "model-routing-policy:dalton-openclaw-dossier-verifier",
            "company-dossier-verifier-model-config.json",
        )
        policy = self.state / "p12a-dossier-policy-v1.json"
        policy.write_bytes((Path(__file__).parents[1] / "deploy/phase9" /
                            policy.name).read_bytes())
        self.assertEqual(dossier_argv(self.context), [
            "--company-dossier-model-config",
            str(self.state / "dossier-model-config.json"),
            "--company-dossier-policy", str(policy),
            "--company-dossier-verifier-model-config",
            str(self.state / "company-dossier-verifier-model-config.json"),
        ])
        self.assertEqual(deep_insight_argv(self.context), [
            "--deep-insight-gate-model-config",
            str(self.state / "dossier-model-config.json"),
            "--deep-insight-gate-policy", str(policy),
            "--deep-insight-gate-verifier-model-config",
            str(self.state / "company-dossier-verifier-model-config.json"),
        ])

    def test_earnings_pair_becomes_the_real_lane_argv(self) -> None:
        self.install_pair(
            "model-routing-policy:dalton-openclaw-earnings-season",
            "earnings-season-model-config.json",
            "model-routing-policy:dalton-openclaw-earnings-season-verifier",
            "earnings-season-verifier-model-config.json",
        )
        self.assertEqual(earnings_argv(self.context), [
            "--earnings-season-model-config",
            str(self.state / "earnings-season-model-config.json"),
            "--earnings-season-verifier-model-config",
            str(self.state / "earnings-season-verifier-model-config.json"),
        ])

    def test_verifier_policies_keep_family_independence(self) -> None:
        self.install_pair(
            "model-routing-policy:dalton-openclaw-earnings-season",
            "earnings-season-model-config.json",
            "model-routing-policy:dalton-openclaw-earnings-season-verifier",
            "earnings-season-verifier-model-config.json",
        )
        verifier = json.loads((self.state /
                               "earnings-season-verifier-model-config.json").read_text("utf-8"))
        with ModelRouter(str(self.state / "model-router.sqlite")) as router:
            policy = router.get_policy(verifier["routing_policy_ref"])
        self.assertEqual(policy["filters"]["family_independence_capabilities"],
                         ["verify", "adjudicate"])

    def test_one_file_alone_keeps_each_lane_disabled(self) -> None:
        (self.state / "dossier-model-config.json").write_text("{}")
        (self.state / "p12a-dossier-policy-v1.json").write_text("{}")
        self.assertNotIn("--company-dossier-verifier-model-config",
                         dossier_argv(self.context))
        (self.state / "earnings-season-model-config.json").write_text("{}")
        self.assertEqual(earnings_argv(self.context), [])

    def test_installer_is_env_gated_and_rejects_half_pairs(self) -> None:
        script = (Path(__file__).parents[1] / "deploy/macos/install.sh").read_text("utf-8")
        for prefix in ("DOSSIER", "EARNINGS"):
            self.assertIn(f'DALTON_{prefix}_MODEL_PROFILE', script)
            self.assertIn(f'DALTON_{prefix}_VERIFIER_MODEL_PROFILE', script)
            self.assertIn(f"set both DALTON_{prefix}_MODEL_*", script)
        self.assertIn('if [[ ! -f "$dossier_policy_file"', script)
        self.assertLess(script.index('validate_model_pair "DALTON_EVENT"'),
                        script.index('mkdir -p "$config_dir"'))
        self.assertIn("unknown model tier", script)
        self.assertIn("must set a profile or tier per role, not both", script)

    @unittest.skipUnless(Path("/bin/zsh").is_file(), "macOS installer requires zsh")
    def test_actual_shell_preflight_refuses_invalid_pairs_before_side_effects(self) -> None:
        script = (Path(__file__).parents[1] / "deploy/macos/install.sh").read_text("utf-8")
        # Execute the installer's actual preflight, stopping before its first
        # filesystem mutation; no service, venv or owner state is touched.
        prefix = script.split('mkdir -p "$config_dir"', 1)[0] + '\nprint PRECHECK_OK\n'
        cases = [
            {"DALTON_DOSSIER_MODEL_TIER": "brain"},
            {"DALTON_DOSSIER_MODEL_TIER": "brain", "DALTON_DOSSIER_VERIFIER_MODEL_TIER": "brain"},
            {"DALTON_EARNINGS_MODEL_TIER": "unknown", "DALTON_EARNINGS_VERIFIER_MODEL_TIER": "verifier"},
            {"DALTON_EVENT_JUDGEMENT_MODEL_PROFILE": "profile:one",
             "DALTON_EVENT_JUDGEMENT_MODEL_TIER": "brain",
             "DALTON_EVENT_VERIFIER_MODEL_TIER": "verifier"},
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                result = subprocess.run(["/bin/zsh", "-s"], input=prefix, text=True,
                                        capture_output=True, env={"PATH": "/usr/bin:/bin", **settings})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("PRECHECK_OK", result.stdout)
        result = subprocess.run(["/bin/zsh", "-s"], input=prefix, text=True,
                                capture_output=True, env={"PATH": "/usr/bin:/bin",
                                    "DALTON_DOSSIER_MODEL_TIER": "brain",
                                    "DALTON_DOSSIER_VERIFIER_MODEL_TIER": "verifier"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRECHECK_OK", result.stdout)

    def test_legacy_dossier_paths_remain_a_fallback(self) -> None:
        (self.state / "initial-screen-model-config.json").write_text("{}")
        (self.state / "dossier-verifier-model-config.json").write_text("{}")
        policy = self.state / "p12a-dossier-policy-v1.json"
        policy.write_text("{}")
        argv = dossier_argv(self.context)
        self.assertIn(str(self.state / "initial-screen-model-config.json"), argv)
        self.assertIn(str(self.state / "dossier-verifier-model-config.json"), argv)


if __name__ == "__main__":
    unittest.main()
