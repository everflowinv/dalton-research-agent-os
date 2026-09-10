"""The event lane's declared request fits the catalog it is configured to use."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.cockpit_model import build_work
from dalton_core.event_judgement_cli import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
)
from dalton_core.model_fallback_chain import tier_chain
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import (
    credential_slots_for,
    ensure_planner_policy,
)
from tests.test_openclaw_catalog_reconcile import _config


NOW = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)


class EventRouteBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.router = ModelRouter(Path(self.directory.name) / "router.sqlite", clock=lambda: NOW)
        self.addCleanup(self.router.close)
        sync_openclaw_model_catalog(self.router, _config(), checked_at=NOW)

    def route(self, tier: str, purpose: str, *, producer_family: str | None = None):
        policy = ensure_planner_policy(
            self.router, tier=tier, now=NOW,
            policy_id=f"model-routing-policy:event-{tier}",
        )["policy_version_ref"]
        work = build_work(
            purpose=purpose,
            request_id=f"route-budget-{tier}-{producer_family or 'producer'}",
            prompt="x" * MAX_INPUT_TOKENS,
            mission_version_ref="coverage-mission-version:test:1",
            max_input_tokens=MAX_INPUT_TOKENS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD,
            max_seconds=TIMEOUT_SECONDS,
            created_at=NOW.isoformat(timespec="microseconds"),
        )
        return self.router.route(
            work,
            attempt_number=1,
            capability="research",
            policy_version_ref=policy,
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain(tier))),
            required_modalities=["text"],
            required_context_tokens=MAX_INPUT_TOKENS,
            estimated_input_tokens=MAX_INPUT_TOKENS,
            estimated_output_tokens=MAX_OUTPUT_TOKENS,
            idempotency_key=f"event-route-budget:{tier}:{producer_family or 'producer'}",
            producer_family=producer_family,
            tier=tier,
            purpose=purpose,
        )["decision"]

    def test_current_priced_brain_route_fits_unchanged_owner_cap(self) -> None:
        decision = self.route("brain", "event_judgement")
        self.assertEqual(decision["outcome"], "selected", decision)
        selected = next(row for row in decision["candidate_snapshot"] if row["eligible"])
        self.assertLessEqual(float(selected["estimated_cost_usd"]), MAX_COST_USD)

    def test_verifier_fits_and_skips_the_producer_family(self) -> None:
        decision = self.route(
            "verifier", "event_judgement_verifier",
            producer_family="anthropic-claude-5",
        )
        self.assertEqual(decision["outcome"], "selected", decision)
        self.assertEqual(decision["selected_endpoint"]["provider"], "zai")
        selected = next(row for row in decision["candidate_snapshot"] if row["eligible"])
        self.assertLessEqual(float(selected["estimated_cost_usd"]), MAX_COST_USD)


if __name__ == "__main__":
    unittest.main()
