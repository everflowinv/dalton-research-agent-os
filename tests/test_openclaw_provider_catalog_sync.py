from __future__ import annotations

import copy
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.openclaw_provider_catalog_sync import (
    CatalogSyncError,
    CatalogSyncRaceError,
    apply_openclaw_provider_catalog_sync,
    auto_profile_id,
    plan_openclaw_provider_catalog_sync,
)


def _model(model_id: str, max_tokens: int) -> dict:
    return {
        "id": model_id,
        "maxTokens": max_tokens,
        "contextWindow": max_tokens * 10,
        "cost": {"input": 1, "output": 2},
    }


def _config() -> dict:
    return {
        "owner": {"credential": "top-secret", "keep": [1, 2, 3]},
        "models": {
            "providers": {
                "openai": {
                    "apiKey": "provider-secret",
                    "models": [_model("gpt-main", 8_000)],
                }
            }
        },
        "plugins": {
            "entries": {
                "unrelated": {"enabled": True, "secret": "plugin-secret"},
                "dalton-openclaw-model-broker": {
                    "enabled": True,
                    "llm": {"allowedModels": ["old/gone"], "other": "same"},
                    "config": {
                        "authKey": "broker-secret",
                        "profiles": [{
                            "id": "profile:old",
                            "model": "old/gone",
                            "maxTokens": 10,
                        }],
                        "maxFrameBytes": 123,
                    },
                    "outside": {"same": True},
                },
            }
        },
    }


def _broker(config: dict) -> dict:
    return config["plugins"]["entries"]["dalton-openclaw-model-broker"]


def _apply_worker(path: str, start: object, output: object) -> None:
    """Spawn-safe worker proving that the sidecar lock crosses processes."""

    try:
        start.wait()
        output.put(("ok", apply_openclaw_provider_catalog_sync(path)))
    except BaseException as exc:  # pragma: no cover - asserted by the parent
        output.put(("error", f"{type(exc).__name__}: {exc}"))


class OpenClawProviderCatalogPlanTests(unittest.TestCase):
    def test_add_delete_and_idempotence_preserve_every_unmanaged_value(self) -> None:
        source = _config()
        before = copy.deepcopy(source)
        plan = plan_openclaw_provider_catalog_sync(source)
        profile_id = auto_profile_id("openai/gpt-main")

        self.assertEqual(source, before)
        self.assertEqual(_broker(plan.config)["llm"], {
            "allowedModels": ["openai/gpt-main"], "other": "same",
        })
        self.assertEqual(_broker(plan.config)["config"]["profiles"], [{
            "id": profile_id,
            "model": "openai/gpt-main",
            "maxTokens": 8_000,
            "timeoutMs": 600_000,
        }])
        expected = copy.deepcopy(before)
        expected_broker = _broker(expected)
        expected_broker["llm"]["allowedModels"] = ["openai/gpt-main"]
        expected_broker["config"]["profiles"] = _broker(plan.config)["config"]["profiles"]
        self.assertEqual(plan.config, expected)
        self.assertEqual(plan.receipt["added_profile_ids"], [profile_id])
        self.assertEqual(plan.receipt["added_model_refs"], ["openai/gpt-main"])
        self.assertEqual(plan.receipt["removed_profile_ids"], ["profile:old"])

        again = plan_openclaw_provider_catalog_sync(plan.config)
        self.assertFalse(again.receipt["changed"])
        self.assertEqual(again.config, plan.config)

    def test_explicit_variants_controls_thinking_and_caps_are_preserved(self) -> None:
        source = _config()
        profiles = [{
            "id": "profile:main-low",
            "model": "openai/gpt-main",
            "maxTokens": 2_000,
            "timeoutMs": 12_345,
            "thinkingLevel": "low",
            "providerControls": {"mode": "declared", "nested": {"x": 1}},
            "caps": ["research", "verify"],
        }, {
            "id": "profile:main-xhigh",
            "model": "openai/gpt-main",
            "maxTokens": 1_000,
            "thinkingLevel": "xhigh",
        }]
        _broker(source)["config"]["profiles"] = copy.deepcopy(profiles)
        _broker(source)["llm"]["allowedModels"] = ["openai/gpt-main"]

        plan = plan_openclaw_provider_catalog_sync(source)
        self.assertFalse(plan.receipt["changed"])
        self.assertEqual(_broker(plan.config)["config"]["profiles"], profiles)

    def test_owned_auto_profile_tracks_max_tokens_but_keeps_custom_timeout(self) -> None:
        source = _config()
        profile_id = auto_profile_id("openai/gpt-main")
        _broker(source)["config"]["profiles"] = [{
            "id": profile_id,
            "model": "openai/gpt-main",
            "maxTokens": 4_000,
            "timeoutMs": 777_777,
            "thinkingLevel": "high",
        }]
        _broker(source)["llm"]["allowedModels"] = ["openai/gpt-main"]

        plan = plan_openclaw_provider_catalog_sync(source)
        self.assertEqual(_broker(plan.config)["config"]["profiles"], [{
            "id": profile_id,
            "model": "openai/gpt-main",
            "maxTokens": 8_000,
            "timeoutMs": 777_777,
            "thinkingLevel": "high",
        }])
        self.assertEqual(plan.receipt["updated_profile_ids"], [profile_id])

    def test_auto_prefix_lookalike_is_explicit_and_never_rewritten(self) -> None:
        source = _config()
        fake = {
            "id": "profile:auto-openai-gpt-main-not-the-route-hash",
            "model": "openai/gpt-main",
            "maxTokens": 321,
            "timeoutMs": 99,
        }
        _broker(source)["config"]["profiles"] = [copy.deepcopy(fake)]
        _broker(source)["llm"]["allowedModels"] = ["openai/gpt-main"]
        plan = plan_openclaw_provider_catalog_sync(source)
        self.assertEqual(_broker(plan.config)["config"]["profiles"], [fake])
        self.assertFalse(plan.receipt["changed"])

    def test_reserved_id_collision_with_different_route_fails_closed(self) -> None:
        source = _config()
        source["models"]["providers"]["other"] = {
            "models": [_model("kept", 100)]
        }
        _broker(source)["config"]["profiles"] = [{
            "id": auto_profile_id("openai/gpt-main"),
            "model": "other/kept",
            "maxTokens": 100,
        }]
        with self.assertRaisesRegex(CatalogSyncError, "collision"):
            plan_openclaw_provider_catalog_sync(source)

    def test_duplicate_provider_model_fails_closed(self) -> None:
        source = _config()
        source["models"]["providers"]["openai"]["models"].append(
            _model("gpt-main", 9_000)
        )
        with self.assertRaisesRegex(CatalogSyncError, "duplicate provider model"):
            plan_openclaw_provider_catalog_sync(source)

    def test_non_string_allowed_model_fails_closed(self) -> None:
        source = _config()
        _broker(source)["llm"]["allowedModels"] = ["old/gone", 7]
        with self.assertRaisesRegex(CatalogSyncError, "must be strings"):
            plan_openclaw_provider_catalog_sync(source)

    def test_provider_max_tokens_above_broker_limit_fails_closed(self) -> None:
        source = _config()
        source["models"]["providers"]["openai"]["models"][0]["maxTokens"] = 1_000_001
        before = copy.deepcopy(source)
        with self.assertRaisesRegex(CatalogSyncError, "exceeds broker maximum"):
            plan_openclaw_provider_catalog_sync(source)
        self.assertEqual(source, before)

    def test_empty_inventory_clears_both_managed_lists(self) -> None:
        source = _config()
        source["models"]["providers"] = {}
        plan = plan_openclaw_provider_catalog_sync(source)
        self.assertEqual(_broker(plan.config)["llm"]["allowedModels"], [])
        self.assertEqual(_broker(plan.config)["config"]["profiles"], [])
        self.assertEqual(plan.receipt["provider_model_count"], 0)


class OpenClawProviderCatalogApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "openclaw.json"

    def _write(self, value: dict) -> bytes:
        data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        self.path.write_bytes(data)
        return data

    def test_apply_creates_exact_0600_backup_and_secret_free_receipt(self) -> None:
        before = self._write(_config())
        receipt = apply_openclaw_provider_catalog_sync(self.path)
        backup = self.root / receipt["backup_file"]
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        public = json.dumps(receipt, sort_keys=True)
        for secret in ("top-secret", "provider-secret", "plugin-secret", "broker-secret"):
            self.assertNotIn(secret, public)
        self.assertNotIn("config", receipt)

    def test_existing_default_backup_is_not_overwritten(self) -> None:
        self._write(_config())
        first = self.path.with_name(self.path.name + ".dalton-catalog-sync.bak")
        first.write_bytes(b"existing")
        receipt = apply_openclaw_provider_catalog_sync(self.path)
        self.assertEqual(first.read_bytes(), b"existing")
        self.assertEqual(receipt["backup_file"], first.name + ".1")

    def test_duplicate_json_key_is_rejected_without_mutation(self) -> None:
        raw = b'{"models":{},"models":{},"plugins":{}}\n'
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(CatalogSyncError, "duplicate JSON key"):
            apply_openclaw_provider_catalog_sync(self.path)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_overlarge_provider_max_tokens_does_not_mutate_host(self) -> None:
        config = _config()
        config["models"]["providers"]["openai"]["models"][0]["maxTokens"] = 1_000_001
        raw = self._write(config)
        with self.assertRaisesRegex(CatalogSyncError, "exceeds broker maximum"):
            apply_openclaw_provider_catalog_sync(self.path)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.root.glob("*.dalton-catalog-sync.bak*")), [])

    def test_external_edit_after_plan_wins_and_no_backup_is_created(self) -> None:
        self._write(_config())
        racing = b'{"external":"owner-edit"}\n'

        def edit(path: Path) -> None:
            path.write_bytes(racing)

        with self.assertRaisesRegex(CatalogSyncRaceError, "next tick"):
            apply_openclaw_provider_catalog_sync(self.path, before_replace=edit)
        self.assertEqual(self.path.read_bytes(), racing)
        self.assertEqual(
            list(self.root.glob("*.dalton-catalog-sync.bak*")), []
        )

    def test_three_process_writers_serialize_and_converge(self) -> None:
        self._write(_config())
        context = multiprocessing.get_context("fork")
        start = context.Event()
        output = context.Queue()
        workers = [
            context.Process(
                target=_apply_worker, args=(str(self.path), start, output)
            )
            for _ in range(3)
        ]
        for worker in workers:
            worker.start()
        start.set()
        results = [output.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)
        errors = [value for state, value in results if state == "error"]
        self.assertEqual(errors, [])
        receipts = [value for state, value in results if state == "ok"]
        self.assertEqual(sum(bool(row["changed"]) for row in receipts), 1)
        self.assertEqual(
            len(list(self.root.glob("*.dalton-catalog-sync.bak*"))), 1
        )
        landed = json.loads(self.path.read_text())
        self.assertFalse(
            plan_openclaw_provider_catalog_sync(landed).receipt["changed"]
        )

    def test_noncooperating_writer_during_publish_wins_without_restore(self) -> None:
        original = self._write(_config())
        racing = b'{"external":"mid-cas-owner"}\n'
        real_link = os.link

        def race(source: object, destination: object, *args: object, **kwargs: object):
            if Path(destination) == self.path:
                self.path.write_bytes(racing)
            return real_link(source, destination, *args, **kwargs)

        with patch(
            "dalton_core.openclaw_provider_catalog_sync.os.link", side_effect=race
        ):
            with self.assertRaisesRegex(CatalogSyncRaceError, "recreated"):
                apply_openclaw_provider_catalog_sync(self.path)
        self.assertEqual(self.path.read_bytes(), racing)
        held = list(self.root.glob(f".{self.path.name}.held.*"))
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].read_bytes(), original)

    def test_config_and_lock_symlinks_are_rejected(self) -> None:
        real = self.root / "real.json"
        real.write_text(json.dumps(_config()))
        self.path.symlink_to(real)
        with self.assertRaises(CatalogSyncError):
            apply_openclaw_provider_catalog_sync(self.path)
        self.path.unlink()
        self._write(_config())
        lock = self.path.with_name(self.path.name + ".dalton-catalog-sync.lock")
        lock.unlink(missing_ok=True)
        lock.symlink_to(real)
        with self.assertRaisesRegex(CatalogSyncError, "sync lock"):
            apply_openclaw_provider_catalog_sync(self.path)

    def test_idempotent_apply_does_not_rewrite_or_create_backup(self) -> None:
        self._write(_config())
        apply_openclaw_provider_catalog_sync(self.path)
        before = self.path.read_bytes()
        before_stat = self.path.stat()
        second = apply_openclaw_provider_catalog_sync(self.path)
        after_stat = self.path.stat()
        self.assertFalse(second["changed"])
        self.assertFalse(second["backup_created"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            (after_stat.st_ino, after_stat.st_mtime_ns),
            (before_stat.st_ino, before_stat.st_mtime_ns),
        )


if __name__ == "__main__":
    unittest.main()
