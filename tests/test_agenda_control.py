from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.agenda_control import (
    AgendaControlApplication,
    AgendaControlConfig,
    AgendaControlPlane,
    _handler,
    _parse_time,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.store import DaltonStore
from tests.agenda_fixtures import register_perception


NOW = "2026-08-14T10:00:00.000000+00:00"
LATER = "2026-09-14T10:00:00.000000+00:00"
LOGIN = "owner@example.com"


def policy():
    return {
        "schema_version": "0.1", "enabled": True, "selected_count": 1,
        "max_model_calls_per_cycle": 1, "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5, "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000, "max_output_tokens": 2000,
        "feature_weights": {
            "mandate_relevance": 4, "catalyst_urgency": 3,
            "evidence_staleness": 2, "decision_impact": 4,
        },
        "trial_company_refs": ["wanhua"], "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


class Client:
    def __init__(self, agenda: AgendaStore, actor_ref: str):
        self.agenda = agenda
        self.actor_ref = actor_ref

    def list_agenda_feedback_targets(self, **params):
        return self.agenda.feedback_targets(**params)

    def record_agenda_feedback(self, **params):
        return self.agenda.record_feedback(actor_ref=self.actor_ref, **params)


class ReviewPlane:
    def __init__(self):
        self.calls = []

    def record(self, login, value):
        self.calls.append(("research", login, dict(value)))
        return {"status": "research-recorded"}

    def record_transcript(self, login, value):
        self.calls.append(("transcript", login, dict(value)))
        return {"status": "transcript-recorded"}

    def view(self, login):
        return {"as_of": NOW, "reviewer_ref": login, "items": []}

    def transcript_view(self, login):
        return {"as_of": NOW, "reviewer_ref": login, "items": []}


class AgendaControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = DaltonStore(root / "core.sqlite")
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
        snapshot = register_perception(self.agenda, "perception:1")
        cycle = self.agenda.start_cycle(
            "agenda:control:wanhua",
            perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref=mandate["id"],
            policy_version_ref=created_policy["id"], company_ref="wanhua",
            actor_ref="core", cycle_id="agenda-cycle:control",
            idempotency_key="cycle:control",
        )
        self.agenda.add_candidates(
            cycle["cycle_id"], actor_ref="core", idempotency_key="candidates:control",
            candidates=[{
                "candidate_id": "candidate:control", "company_ref": "wanhua",
                "question": "盈利是否改变？", "answer_criteria": "核对价格和成本",
                "features": {"mandate_relevance": 3, "catalyst_urgency": 2, "evidence_staleness": 1, "decision_impact": 3},
                "rationale": "重要", "source_refs": ["evidence:1"],
            }],
        )
        self.decision = self.agenda.decide_cycle(
            cycle["cycle_id"], actor_ref="core", decision_id="decision:control",
            idempotency_key="decision:control",
        )
        claim = self.agenda.claim_outbox(
            endpoint_ref="openclaw:discord:test", actor_ref="core",
            idempotency_key="claim:control", now=NOW,
        )["claims"][0]
        self.agenda.record_delivery(
            claim["message_id"], state="delivered", actor_ref="core",
            delivery_attempt_id=claim["delivery_attempt_id"],
            delivery_receipt_id="discord:control", idempotency_key="delivery:control",
        )
        self.config = AgendaControlConfig.from_mapping({
            "host": "127.0.0.1", "port": 8793,
            "tailscale_host": "dalton.example.ts.net",
            "tailscale_executable": sys.executable,
            "allowed_tailscale_logins": [LOGIN],
            "writer_socket": str(root / "writer.sock"),
            "token_config": str(root / "tokens.json"),
            "endpoint_ref": "openclaw:discord:test",
            "feedback_timeout_seconds": 86400,
            "sweep_interval_seconds": 60,
        })
        self.plane = AgendaControlPlane(
            self.config,
            dashboard_client=Client(self.agenda, "bridge:tailscale-dashboard"),
            timeout_client=Client(self.agenda, "automation:agenda-timeout"),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_dashboard_feedback_uses_hashed_subject_and_csrf(self):
        app = AgendaControlApplication(self.config, self.plane)
        self.assertEqual(app.allowed_login(LOGIN), LOGIN)
        self.assertIsNone(app.allowed_login("intruder@example.com"))
        _session_id, session, _created = app.session(LOGIN, None)
        with self.assertRaises(PermissionError):
            app.post(
                LOGIN, session, "wrong",
                b'{"decision_ref":"decision:control","verdict":"agree"}',
            )
        result = app.post(
            LOGIN, session, session.csrf,
            b'{"decision_ref":"decision:control","verdict":"disagree"}',
        )
        self.assertEqual(result["verdict"], "disagree")
        row = self.store.connection.execute(
            "SELECT subject_ref,source,actor_ref FROM agenda_feedback"
        ).fetchone()
        self.assertTrue(row["subject_ref"].startswith("human:tailscale-"))
        self.assertNotIn("owner@example.com", row["subject_ref"])
        self.assertEqual(row["source"], "tailscale_dashboard")
        self.assertEqual(row["actor_ref"], "bridge:tailscale-dashboard")

    def test_one_session_and_csrf_guard_agenda_and_both_review_planes(self):
        review = ReviewPlane()
        app = AgendaControlApplication(self.config, self.plane, review)
        session_id, session, created = app.session(LOGIN, None)
        self.assertTrue(created)
        same_id, same_session, created_again = app.session(
            LOGIN, f"dalton_session={session_id}"
        )
        self.assertEqual(same_id, session_id)
        self.assertIs(same_session, session)
        self.assertFalse(created_again)
        with self.assertRaises(PermissionError):
            app.post_review(LOGIN, session, "wrong", b'{"x":1}')
        self.assertEqual(
            app.post_review(LOGIN, session, session.csrf, b'{"x":1}')["status"],
            "research-recorded",
        )
        self.assertEqual(
            app.post_transcript_review(
                LOGIN, session, session.csrf, b'{"y":2}'
            )["status"],
            "transcript-recorded",
        )
        self.assertEqual(
            review.calls,
            [
                ("research", LOGIN, {"x": 1}),
                ("transcript", LOGIN, {"y": 2}),
            ],
        )

    def test_embedded_review_config_has_no_second_host_or_core_path(self):
        raw = {
            "host": "127.0.0.1", "port": 8793,
            "tailscale_host": "dalton.example.ts.net",
            "tailscale_executable": sys.executable,
            "allowed_tailscale_logins": [LOGIN],
            "writer_socket": str(Path(self.temp.name) / "writer.sock"),
            "token_config": str(Path(self.temp.name) / "tokens.json"),
            "endpoint_ref": "openclaw:discord:test",
            "feedback_timeout_seconds": 86400,
            "sweep_interval_seconds": 60,
            "research_review": {
                "candidate_staging_path": str(
                    Path(self.temp.name) / "candidate.sqlite"
                ),
                "transcript_review_directory": str(
                    Path(self.temp.name) / "review-inbox"
                ),
                "reconcile_interval_seconds": 60,
            },
        }
        config = AgendaControlConfig.from_mapping(raw)
        self.assertIsNotNone(config.research_review)
        raw["research_review"]["core_db"] = str(
            Path(self.temp.name) / "core.sqlite"
        )
        with self.assertRaises(Exception):
            AgendaControlConfig.from_mapping(raw)

    def test_single_http_surface_serves_cockpit_and_all_review_routes(self):
        review = ReviewPlane()
        app = AgendaControlApplication(self.config, self.plane, review)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            headers = {
                "Host": "dalton.example.ts.net",
                "Tailscale-User-Login": LOGIN,
            }
            connection.request("GET", "/", headers=headers)
            response = connection.getresponse()
            html = response.read().decode("utf-8")
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            self.assertEqual(response.status, 200)
            self.assertIn("Dalton Cockpit", html)
            connection.request(
                "GET", "/v1/research-review",
                headers={**headers, "Cookie": cookie},
            )
            response = connection.getresponse()
            review_payload = json.loads(response.read())
            self.assertTrue(review_payload["enabled"])
            connection.request(
                "GET", "/v1/transcript-review",
                headers={**headers, "Cookie": cookie},
            )
            response = connection.getresponse()
            transcript_payload = json.loads(response.read())
            self.assertTrue(transcript_payload["enabled"])
            self.assertEqual(
                review_payload["csrf_token"],
                transcript_payload["csrf_token"],
            )
            body = b'{"x":1}'
            connection.request(
                "POST", "/v1/research-review/decision", body=body,
                headers={
                    **headers, "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Dalton-CSRF": review_payload["csrf_token"],
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.loads(response.read())["status"], "research-recorded"
            )
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_timeout_is_separate_and_late_human_feedback_overrides_effective_view(self):
        target = self.agenda.feedback_targets(endpoint_ref="openclaw:discord:test")[0]
        after_deadline = _parse_time(target["delivered_at"], "delivered_at") + timedelta(hours=25)
        first = self.plane.sweep(now=after_deadline)
        second = self.plane.sweep(now=after_deadline)
        self.assertEqual(first["recorded"], 1)
        self.assertEqual(second["existing"], 1)
        view = self.plane.view(LOGIN, now=after_deadline)
        self.assertEqual(view["items"][0]["resolution"], "auto_accept_timeout")
        self.plane.record(LOGIN, self.decision["id"], "disagree")
        view = self.plane.view(LOGIN, now=after_deadline)
        self.assertEqual(view["items"][0]["resolution"], "explicit_human")
        self.assertEqual(view["items"][0]["effective_verdict"], "disagree")
        rows = self.store.connection.execute(
            "SELECT subject_ref,source,verdict FROM agenda_feedback ORDER BY created_at"
        ).fetchall()
        self.assertEqual(rows[0]["subject_ref"], "automation:timeout")
        self.assertEqual(rows[0]["source"], "auto_accept_timeout")
        self.assertEqual(rows[0]["verdict"], "agree")


if __name__ == "__main__":
    unittest.main()
