"""P9d-17a: installing the extraction model configuration is idempotent and closed."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.document_extraction_setup import CONFIG_FILE_NAME, POLICY_ID, install
from dalton_core.model_router import ModelRouter
from dalton_core.store import content_hash


def _service(root: Path) -> Path:
    state = root / "state"; state.mkdir()
    (root / "openclaw").mkdir()
    (root / "openclaw" / "broker.sock").write_text("")
    (root / "openclaw" / "broker.sock.key").write_text("k")
    ModelRouter(str(state / "model-router.sqlite")).close()
    config = {
        "core_db": str(state / "core.sqlite"), "model_router_db": str(state / "model-router.sqlite"),
        "control": {"config": {"host": "127.0.0.1", "port": 1,
                               "research_review": {"candidate_staging_path": str(state / "staging.sqlite")}}},
        "bounded_planner": {"config": {
            "planner_broker_socket": str(root / "openclaw" / "broker.sock"),
            "planner_broker_auth_key": str(root / "openclaw" / "broker.sock.key"),
            "planner_broker_client_id": "client:dalton-core", "planner_expected_agent_id": "chem",
        }},
        "thesis_impact": {"config": {"budget_db": str(state / "budget.sqlite"),
                                     "budget_policy_version_id": "thesis-impact-day-budget-policy:production:1"}},
    }
    path = root / "service.json"; path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


class ExtractionSetupTests(unittest.TestCase):
    def test_duplicate_latest_policy_preserves_valid_explicit_prior_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
            first = install(config_path, now=now)
            target = root / "state" / CONFIG_FILE_NAME
            before = target.read_bytes()
            with ModelRouter(str(root / "state" / "model-router.sqlite")) as router:
                prior = router.get_policy(first["routing_policy_ref"])
                latest = {k: v for k, v in prior.items() if k != "content_hash"}
                latest.update({
                    "policy_version_ref": "model-routing-policy-version:dalton-openclaw-extraction:2",
                    "version": 2,
                    "prior_version_ref": first["routing_policy_ref"],
                    "purpose_overrides": {"research_localization": {
                        "actor_ref": "operator:test",
                        "chain": ["profile:zai-glm-5-3-flash"],
                        "mode": "explicit",
                    }},
                })
                latest["content_hash"] = content_hash(latest)
                router.register_policy(latest)

            result = install(config_path, now=now)
            self.assertEqual(result["policy"], {
                "status": "duplicate",
                "policy_version_ref": "model-routing-policy-version:dalton-openclaw-extraction:2",
            })
            self.assertEqual(result["routing_policy_ref"], first["routing_policy_ref"])
            self.assertFalse(result["model_config_changed"])
            self.assertEqual(target.read_bytes(), before)

    def test_duplicate_policy_refuses_missing_or_different_existing_policy(self) -> None:
        for existing_ref in (
            "model-routing-policy-version:missing:1",
            "model-routing-policy-version:other:1",
        ):
            with self.subTest(existing_ref=existing_ref), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = _service(root)
                install(config_path)
                target = root / "state" / CONFIG_FILE_NAME
                wire = json.loads(target.read_text())
                if existing_ref.endswith("other:1"):
                    with ModelRouter(str(root / "state" / "model-router.sqlite")) as router:
                        other = router.get_policy(wire["routing_policy_ref"])
                        other = {k: v for k, v in other.items() if k != "content_hash"}
                        other.update({"id": "model-routing-policy:other",
                                      "policy_version_ref": existing_ref})
                        other["content_hash"] = content_hash(other)
                        router.register_policy(other)
                wire["routing_policy_ref"] = existing_ref
                target.write_text(json.dumps(wire))
                before = target.read_bytes()
                with self.assertRaises(Exception):
                    install(config_path)
                self.assertEqual(target.read_bytes(), before)

    def test_duplicate_latest_does_not_preserve_structurally_stale_prior_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            first = install(config_path, profile_ids=["profile:deepseek-v4-flash"])
            target = root / "state" / CONFIG_FILE_NAME
            # Move the requested structure forward, then deliberately restore
            # the old explicit pin. The latest policy is duplicate on reinstall,
            # but the old pin no longer represents the requested profile set.
            second = install(config_path, profile_ids=[
                "profile:deepseek-v4-flash", "profile:gemini-3-7-flash",
            ])
            wire = json.loads(target.read_text())
            wire["routing_policy_ref"] = first["routing_policy_ref"]
            target.write_text(json.dumps(wire))
            result = install(config_path, profile_ids=[
                "profile:deepseek-v4-flash", "profile:gemini-3-7-flash",
            ])
            self.assertEqual(result["policy"]["status"], "duplicate")
            self.assertEqual(result["routing_policy_ref"], second["routing_policy_ref"])
            self.assertTrue(result["model_config_changed"])

    def test_credential_slots_are_derived_from_preserved_policy_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            first = install(config_path, tier="cheap",
                            credential_slots=["credential-slot:openclaw:deepseek"])
            target = root / "state" / CONFIG_FILE_NAME
            with ModelRouter(str(root / "state" / "model-router.sqlite")) as router:
                prior = router.get_policy(first["routing_policy_ref"])
                latest = {k: v for k, v in prior.items() if k != "content_hash"}
                latest.update({
                    "policy_version_ref": "model-routing-policy-version:dalton-openclaw-extraction:2",
                    "version": 2, "prior_version_ref": first["routing_policy_ref"],
                    "purpose_overrides": {"research_localization": {
                        "actor_ref": "operator:test",
                        "chain": ["profile:zai-glm-5-3-flash"], "mode": "explicit",
                    }},
                })
                latest["content_hash"] = content_hash(latest)
                router.register_policy(latest)
            before = target.read_bytes()
            with patch("dalton_core.research_planner_setup.credential_slots_for",
                       return_value=["credential-slot:openclaw:deepseek"]) as slots:
                result = install(config_path, tier="cheap")
            self.assertEqual(slots.call_args.kwargs["policy_version_ref"],
                             first["routing_policy_ref"])
            self.assertEqual(result["routing_policy_ref"], first["routing_policy_ref"])
            self.assertEqual(target.read_bytes(), before)

    def test_reinstall_and_tier_switch_preserve_only_valid_budget_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
            install(config_path, now=now)
            target = root / "state" / CONFIG_FILE_NAME
            wire = json.loads(target.read_text(encoding="utf-8"))
            budgets = {
                "reading_limits": {"max_document_chars": 900000, "max_pdf_pages": 700},
                "call_budget": {"max_cost_usd": 0.04},
                "purpose_call_budgets": {"document_numeric_extraction": {"max_output_tokens": 777}},
                "run_budget": {"max_units": 9},
                "purpose_run_budgets": {"document_extraction": {"max_calls": 4}},
                "transport_retry": {"max_definitely_not_sent_retries": 1, "queue_wait_seconds": 0, "retry_backoff_seconds": 0},
                "provider_retry": {
                    "max_same_profile_retries": 4,
                    "retry_backoff_seconds": 7,
                },
            }
            wire.update(budgets)
            wire["unknown_private_field"] = "must not survive"
            target.write_text(json.dumps(wire), encoding="utf-8")
            install(config_path, profile_ids=["profile:deepseek-v4-flash"], now=now)
            rewritten = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual({key: rewritten[key] for key in budgets}, budgets)
            self.assertNotIn("unknown_private_field", rewritten)

    def test_invalid_existing_reading_limits_refuse_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            install(config_path)
            target = root / "state" / CONFIG_FILE_NAME
            wire = json.loads(target.read_text())
            wire["reading_limits"] = {"max_pdf_pages": False}
            target.write_text(json.dumps(wire))
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                install(config_path)
            self.assertEqual(target.read_bytes(), before)

    def test_invalid_existing_budget_refuses_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            install(config_path, now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc))
            target = root / "state" / CONFIG_FILE_NAME
            original = json.loads(target.read_text(encoding="utf-8"))
            target.write_text(json.dumps({**original, "call_budget": {"max_cost_usd": -1}}))
            with self.assertRaises(ValueError):
                install(config_path, now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(json.loads(target.read_text()),
                             {**original, "call_budget": {"max_cost_usd": -1}})

    def test_install_appends_policy_once_writes_config_and_points_service_at_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = _service(root)
            now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
            first = install(config_path, now=now)
            self.assertEqual(first["policy"]["status"], "fresh")
            self.assertEqual(first["routing_policy_ref"], "model-routing-policy-version:dalton-openclaw-extraction:1")
            self.assertTrue(first["model_config_changed"] and first["service_config_changed"])
            target = root / "state" / CONFIG_FILE_NAME
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(set(written), {"routing_policy_ref", "credential_slot_refs", "model_router_db", "broker_socket",
                                            "broker_auth_key", "broker_client_id", "expected_agent_id", "budget_db", "budget_policy_ref",
                                            "provider_retry"})
            self.assertEqual(written["provider_retry"], {
                "max_same_profile_retries": 1,
                "retry_backoff_seconds": 2,
            })
            self.assertEqual((written["credential_slot_refs"], written["expected_agent_id"], written["budget_policy_ref"]),
                             (["credential-slot:openclaw:deepseek"], "chem", "thesis-impact-day-budget-policy:production:1"))
            service = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(service["control"]["config"]["research_review"]["document_extraction_model_config_path"], str(target))
            self.assertEqual(service["control"]["config"]["research_review"]["candidate_staging_path"], str(root / "state" / "staging.sqlite"))
            with ModelRouter(str(root / "state" / "model-router.sqlite")) as router:
                policy = router.get_policy(first["routing_policy_ref"])
                self.assertEqual(policy["filters"]["allowed_profile_ids"], ["profile:deepseek-v4-flash"])
                self.assertEqual(policy["id"], POLICY_ID)
            # Idempotent: nothing appended, nothing rewritten.
            second = install(config_path, now=now)
            self.assertEqual((second["policy"]["status"], second["model_config_changed"], second["service_config_changed"]),
                             ("duplicate", False, False))
            # A changed profile list is a new policy version, prior-linked.
            third = install(config_path, profile_ids=["profile:deepseek-v4-flash", "profile:gemini-3-7-flash"], now=now)
            self.assertEqual(third["routing_policy_ref"], "model-routing-policy-version:dalton-openclaw-extraction:2")
            with ModelRouter(str(root / "state" / "model-router.sqlite")) as router:
                self.assertEqual(router.get_policy(third["routing_policy_ref"])["prior_version_ref"], first["routing_policy_ref"])


if __name__ == "__main__":
    unittest.main()
