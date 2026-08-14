from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore, AgendaValidationError
from dalton_core.observability import ObservabilityStore
from dalton_core.store import DaltonStore


NOW = "2026-08-14T10:00:00.000000+00:00"
LATER = "2026-09-14T10:00:00.000000+00:00"


def policy():
    return {
        "schema_version": "0.1",
        "enabled": True,
        "selected_count": 2,
        "max_model_calls_per_cycle": 1,
        "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5,
        "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000,
        "max_output_tokens": 2000,
        "feature_weights": {
            "mandate_relevance": 4,
            "catalyst_urgency": 3,
            "evidence_staleness": 2,
            "decision_impact": 4,
        },
        "trial_company_refs": ["wanhua"],
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


class AgendaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.tmp.name) / "core.sqlite")
        ObservabilityStore(self.store)
        self.agenda = AgendaStore(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def govern(self):
        p = self.agenda.create_policy(
            policy(), effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id="agenda-policy-version:1",
            idempotency_key="policy:1",
        )
        m = self.agenda.create_mandate(
            "mandate:coverage-quality",
            objective="Find the most decision-useful unanswered question",
            scope_refs=["wanhua"], constraints={"mode": "shadow"},
            success_criteria={"human_feedback_required": True},
            effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id="mandate-version:1",
            idempotency_key="mandate:1",
        )
        self.agenda.set_pause(
            False, reason="owner approved Phase 1 shadow", actor_ref="human:owner",
            version_id="agenda-control-version:2", idempotency_key="resume:1",
        )
        return p, m

    def test_fail_closed_bootstrap_and_bounded_features(self):
        self.assertTrue(self.agenda.control_state()["paused"])
        bad = policy()
        bad["feature_weights"]["unknown"] = 1
        with self.assertRaises(AgendaValidationError):
            self.agenda.create_policy(
                bad, effective_from=NOW, effective_until=LATER,
                actor_ref="human:owner",
            )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM agenda_policy_versions").fetchone()[0],
            0,
        )

    def test_cycle_selection_is_deterministic_and_outbox_requires_receipt(self):
        p, m = self.govern()
        started = self.agenda.start_cycle(
            "agenda:2026-08-14:wanhua",
            perception_snapshot_ref="perception:1",
            perception_snapshot_hash="a" * 64,
            mandate_version_ref=m["id"], policy_version_ref=p["id"],
            company_ref="wanhua", actor_ref="core",
            cycle_id="agenda-cycle:1", idempotency_key="cycle:1",
        )
        candidates = [
            {
                "candidate_id": "candidate:b",
                "company_ref": "wanhua",
                "question": "Question B?",
                "answer_criteria": "Answer B",
                "features": {"mandate_relevance": 3, "catalyst_urgency": 2, "evidence_staleness": 1, "decision_impact": 3},
                "rationale": "display only",
                "source_refs": ["evidence:b"],
            },
            {
                "candidate_id": "candidate:a",
                "company_ref": "wanhua",
                "question": "Question A?",
                "answer_criteria": "Answer A",
                "features": {"mandate_relevance": 3, "catalyst_urgency": 2, "evidence_staleness": 1, "decision_impact": 3},
                "rationale": "display only",
                "source_refs": ["evidence:a"],
            },
            {
                "candidate_id": "candidate:c",
                "company_ref": "wanhua",
                "question": "Question C?",
                "answer_criteria": "Answer C",
                "features": {"mandate_relevance": 1, "catalyst_urgency": 0, "evidence_staleness": 0, "decision_impact": 1},
                "rationale": "display only",
                "source_refs": ["evidence:c"],
            },
        ]
        self.agenda.add_candidates(
            started["cycle_id"], candidates=candidates, actor_ref="core",
            idempotency_key="candidates:1",
        )
        decision = self.agenda.decide_cycle(
            started["cycle_id"], actor_ref="core", decision_id="decision:1",
            idempotency_key="decision:1",
        )
        self.assertEqual(decision["selected_candidate_refs"], ["candidate:a", "candidate:b"])
        pending = self.agenda.pending_outbox()
        self.assertEqual(len(pending), 1)
        with self.assertRaises(AgendaValidationError):
            self.agenda.record_delivery(pending[0]["message_id"], state="delivered", actor_ref="bridge")
        delivered = self.agenda.record_delivery(
            pending[0]["message_id"], state="delivered", actor_ref="bridge",
            delivery_receipt_id="receipt:1",
        )
        self.assertEqual(delivered["delivery_receipt_id"], "receipt:1")
        self.assertEqual(self.agenda.pending_outbox(), [])

    def test_direct_writes_and_idempotency_conflicts_fail_closed(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO agenda_domain_events(event_id,event_type,aggregate_ref,payload_json,actor_ref,created_at,content_hash) VALUES('x','x','x','{}','x',?,?)",
                (NOW, "a" * 64),
            )
        first = self.agenda.set_pause(
            False, reason="test", actor_ref="human:owner",
            version_id="agenda-control-version:test", idempotency_key="pause:test",
        )
        duplicate = self.agenda.set_pause(
            False, reason="test", actor_ref="human:owner",
            version_id="agenda-control-version:test", idempotency_key="pause:test",
        )
        conflict = self.agenda.set_pause(
            True, reason="different", actor_ref="human:owner",
            version_id="agenda-control-version:different", idempotency_key="pause:test",
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(conflict["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
