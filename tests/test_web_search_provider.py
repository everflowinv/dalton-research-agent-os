"""Provider selection follows the host without rewriting historical searches."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dalton_core.public_web_search_cli import main
from dalton_core.web_search_provider import (
    WebSearchProviderConfigurationError, resolve_web_search_provider,
)


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "openclaw.json"
        self.socket = self.root / "dalton-web-search-broker.sock"

    def select(self, provider):
        self.config.write_text(json.dumps({
            "tools": {"web": {"search": {"provider": provider}}},
            "plugins": {"entries": {"dalton-openclaw-web-search-broker": {
                "config": {"expectedProvider": "antigravity"},
            }}},
        }))

    def test_live_selection_rereads_host_not_stale_plugin_pin(self):
        for provider in ("gemini", "antigravity", "gemini"):
            self.select(provider)
            self.assertEqual(resolve_web_search_provider(
                networked=True, broker_socket=self.socket,
            ), provider)

    def test_rehearsal_never_reads_live_config(self):
        self.config.write_text("not json")
        self.assertEqual(resolve_web_search_provider(
            networked=False, openclaw_config_path=self.config,
        ), "gemini")

    def test_missing_or_invalid_selection_refuses_without_leaking_config(self):
        for body in ({}, {"tools": {"web": {"search": {"provider": "secret invalid"}}}}):
            self.config.write_text(json.dumps(body))
            with self.assertRaises(WebSearchProviderConfigurationError) as caught:
                resolve_web_search_provider(networked=True, broker_socket=self.socket)
            self.assertNotIn("secret", str(caught.exception))

    def test_explicit_compatibility_pin_is_preserved(self):
        self.select("gemini")
        self.assertEqual(resolve_web_search_provider(
            networked=True, expected_provider="antigravity", broker_socket=self.socket,
        ), "antigravity")

    def test_new_network_children_bind_same_provider_in_handle_and_authority(self):
        args = [
            "--state-dir", str(self.root), "--governance", str(self.root / "governance.json"),
            "--discovery-plan", str(self.root / "plan.json"), "--company-ref", "company:test",
            "--spec-ref", "news", "--requested-by", "automation:test",
            "--mission-version-ref", "coverage-mission-version:test:1",
            "--mission-version-hash", "a" * 64, "--allow-network", "--quiet",
            "--broker-socket", str(self.socket), "--broker-auth-key", str(self.root / "key"),
        ]
        module = "dalton_core.public_web_search_cli."
        with patch(module + "WebSearchConnectorGovernance.load"), \
             patch(module + "load_discovery_plan", return_value={}), \
             patch(module + "WebSearchBrokerHandle") as handle, \
             patch(module + "run_discovery", return_value={"status": "succeeded"}) as run:
            for provider in ("gemini", "antigravity", "gemini"):
                self.select(provider)
                self.assertEqual(main(args), 0)
                self.assertEqual(handle.call_args.kwargs["expected_provider"], provider)
                self.assertEqual(run.call_args.kwargs["expected_provider"], provider)


if __name__ == "__main__":
    unittest.main()
