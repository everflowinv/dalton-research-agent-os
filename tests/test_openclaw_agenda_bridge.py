from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_agenda_bridge import (
    OpenClawAgendaBridge,
    OpenClawAgendaBridgeConfig,
    render_agenda_card,
)
from dalton_core.store import DaltonStore


NOW = "2026-08-14T10:00:00.000000+00:00"
LATER = "2026-09-14T10:00:00.000000+00:00"


def policy():
    return {
        "schema_version": "0.1", "enabled": True, "selected_count": 1,
        "max_model_calls_per_cycle": 1, "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5, "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000, "max_output_tokens": 2000,
        "feature_weights": {"mandate_relevance": 4, "catalyst_urgency": 3, "evidence_staleness": 2, "decision_impact": 4},
        "trial_company_refs": ["wanhua"], "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


class CoreClient:
    def __init__(self, agenda: AgendaStore):
        self.agenda = agenda

    def claim_agenda_outbox(self, **params):
        return self.agenda.claim_outbox(actor_ref="core", **params)

    def record_agenda_delivery(self, **params):
        return self.agenda.record_delivery(actor_ref="core", **params)


class FeedbackClient:
    def __init__(self, agenda: AgendaStore):
        self.agenda = agenda

    def list_agenda_feedback_targets(self, **params):
        return self.agenda.feedback_targets(**params)

    def record_agenda_feedback(self, **params):
        return self.agenda.record_feedback(actor_ref="bridge:openclaw-discord", **params)


class FakeOpenClaw:
    def __init__(self, *, recover_existing: bool = False):
        self.sent_body = None
        self.send_calls = 0
        self.recover_existing = recover_existing

    def __call__(self, argv, _timeout):
        action = argv[2]
        if action == "search":
            marker = argv[argv.index("--query") + 1]
            found = self.recover_existing or self.sent_body is not None
            messages = [] if not found else [[{
                "id": "discord-message-1", "content": self.sent_body or marker,
                "timestamp": NOW, "author": {"bot": True},
            }]]
            return {"payload": {"ok": True, "results": {"messages": messages}}}
        if action == "send":
            self.send_calls += 1
            self.sent_body = argv[argv.index("--message") + 1]
            return {"messageId": "discord-message-1", "payload": {"ok": True}}
        if action == "reactions":
            return {"payload": {"ok": True, "reactions": [{
                "emoji": {"raw": "✅", "name": "✅"},
                "users": [{"id": "932169512197955636", "username": "lumos"}],
            }]}}
        raise AssertionError(action)


class OpenClawAgendaBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        ObservabilityStore(self.store)
        self.agenda = AgendaStore(self.store)
        created_policy = self.agenda.create_policy(
            policy(), effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id="agenda-policy-version:1",
            idempotency_key="policy:1",
        )
        mandate = self.agenda.create_mandate(
            "mandate:coverage", objective="Find the best question", scope_refs=["wanhua"],
            constraints={"mode": "shadow"}, success_criteria={"feedback": True},
            effective_from=NOW, effective_until=LATER, actor_ref="human:owner",
            version_id="mandate-version:1", idempotency_key="mandate:1",
        )
        cycle = self.agenda.start_cycle(
            "agenda:2026-08-14:wanhua", perception_snapshot_ref="perception:1",
            perception_snapshot_hash="a" * 64, mandate_version_ref=mandate["id"],
            policy_version_ref=created_policy["id"], company_ref="wanhua", actor_ref="core",
            cycle_id="agenda-cycle:1", idempotency_key="cycle:1",
        )
        self.agenda.add_candidates(
            cycle["cycle_id"], actor_ref="core", idempotency_key="candidates:1",
            candidates=[{
                "candidate_id": "candidate:1", "company_ref": "wanhua",
                "question": "MDI 价差是否改变盈利预期？",
                "answer_criteria": "核对价格、成本和销量",
                "features": {"mandate_relevance": 3, "catalyst_urgency": 2, "evidence_staleness": 1, "decision_impact": 3},
                "rationale": "重要", "source_refs": ["evidence:1"],
            }],
        )
        self.decision = self.agenda.decide_cycle(
            cycle["cycle_id"], actor_ref="core", decision_id="decision:1",
            idempotency_key="decision:1",
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def config(self):
        return OpenClawAgendaBridgeConfig.from_mapping({
            "openclaw_executable": sys.executable,
            "writer_socket": str(Path(self.temp.name) / "writer.sock"),
            "token_config": str(Path(self.temp.name) / "tokens.json"),
            "account": "default", "target": "channel:1481256083589697566",
            "guild_id": "932170193180958741", "channel_id": "1481256083589697566",
            "endpoint_ref": "openclaw:discord:default:channel:1481256083589697566",
            "control_url": "https://dalton.example.ts.net:8793/",
            "company_labels": {"wanhua": "万华化学"},
            "feedback_user_ids": [],
            "timeout_seconds": 30, "claim_ttl_seconds": 120, "retry_seconds": 60,
            "max_attempts": 5, "batch_size": 1, "feedback_limit": 10,
        })

    def bridge(self, runner):
        return OpenClawAgendaBridge(
            self.config(), runner=runner, core_client=CoreClient(self.agenda),
            feedback_client=FeedbackClient(self.agenda),
        )

    def test_delivers_once_and_points_to_control_plane(self):
        runner = FakeOpenClaw()
        result = self.bridge(runner).run_once()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["feedback"]["recorded"], 0)
        self.assertEqual(runner.send_calls, 1)
        self.assertIn("DALTON-OUTBOX-", runner.sent_body)
        self.assertIn("https://dalton.example.ts.net:8793/", runner.sent_body)
        self.assertNotIn("点 ✅", runner.sent_body)

    def test_reconciles_existing_marker_without_duplicate_send(self):
        pending = self.agenda.pending_outbox()[0]
        _body, marker = render_agenda_card(
            pending, {"wanhua": "万华化学"}, "https://dalton.example.ts.net:8793/"
        )
        runner = FakeOpenClaw(recover_existing=True)
        runner.sent_body = marker
        result = self.bridge(runner).run_once()
        self.assertEqual(result["deliveries"][0]["recovered"], True)
        self.assertEqual(runner.send_calls, 0)


if __name__ == "__main__":
    unittest.main()
