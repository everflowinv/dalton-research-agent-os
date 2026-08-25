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
    AgendaControlError,
    AgendaControlPlane,
    CockpitIntentDispatcher,
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
        self.answer_calls = []

    def list_agenda_feedback_targets(self, **params):
        return self.agenda.feedback_targets(**params)

    def record_agenda_feedback(self, **params):
        return self.agenda.record_feedback(actor_ref=self.actor_ref, **params)

    def intent_context_bindings(self):
        return []

    def answer_subjects(self):
        return [{
            "ref": "answer-subject:test",
            "hash": "a" * 64,
            "label": "wanhua · Find the best question",
            "company_ref": "wanhua",
            "policy_state": "active",
        }]

    def route_answer(self, **params):
        self.answer_calls.append(dict(params))
        return {
            "context_pack": {
                "id": "answer-context-pack:test",
                "content_hash": "b" * 64,
                "claim_versions": [],
                "evidence_versions": [],
            },
            "decision": {
                "route": "recommend_agenda_item",
                "reason_codes": ["question_not_admitted"],
                "write_performed": False,
            },
        }


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

    def trajectory_view(self, login):
        return {
            "as_of": NOW,
            "viewer_ref": login,
            "projection_only": True,
            "items": [],
        }


class IntentPlane:
    def __init__(self):
        self.calls = []

    def view(self, login):
        return {
            "as_of": NOW,
            "actor_ref": login,
            "candidate_only": True,
            "execution_enabled": False,
            "items": [],
        }

    def compose(self, login, value):
        self.calls.append(("compose", login, dict(value)))
        return {
            "status": "fresh",
            "candidate": {"candidate_only": True, "executable": False},
        }

    def confirm(self, login, value):
        self.calls.append(("confirm", login, dict(value)))
        return {"status": "dispatched"}


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

    def test_one_session_and_csrf_guard_all_cockpit_write_planes(self):
        review = ReviewPlane()
        intent = IntentPlane()
        app = AgendaControlApplication(self.config, self.plane, review, intent)
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
        with self.assertRaises(PermissionError):
            app.post_intent(
                LOGIN,
                session,
                "wrong",
                '{"request_id":"1","utterance":"查 ACN"}'.encode(),
            )
        self.assertEqual(
            app.post_intent(
                LOGIN,
                session,
                session.csrf,
                b'{"request_id":"1","utterance":"\xe6\x9f\xa5 ACN"}',
            )["status"],
            "fresh",
        )
        self.assertEqual(
            app.post_intent_confirm(
                LOGIN,
                session,
                session.csrf,
                b'{"request_id":"2","candidate_version_ref":"candidate:1","candidate_version_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","decision":"confirm"}',
            )["status"],
            "dispatched",
        )
        self.assertEqual(
            review.calls,
            [
                ("research", LOGIN, {"x": 1}),
                ("transcript", LOGIN, {"y": 2}),
            ],
        )
        self.assertEqual(intent.calls, [
            ("compose", LOGIN, {"request_id": "1", "utterance": "查 ACN"}),
            ("confirm", LOGIN, {
                "request_id": "2",
                "candidate_version_ref": "candidate:1",
                "candidate_version_hash": "a" * 64,
                "decision": "confirm",
            }),
        ])
        with self.assertRaises(PermissionError):
            app.post_answer(
                LOGIN,
                session,
                "wrong",
                b'{"subject_binding":{},"question":"Why?"}',
            )
        answer_subject = self.plane.answer_view()["subjects"][0]
        answer = app.post_answer(
            LOGIN,
            session,
            session.csrf,
            json.dumps({
                "subject_binding": answer_subject,
                "question": "Why?",
            }).encode(),
        )
        self.assertEqual(answer["decision"]["route"], "recommend_agenda_item")
        self.assertFalse(answer["decision"]["write_performed"])

    def test_answer_refresh_dispatch_uses_ephemeral_human_and_closed_decision(self):
        calls = []

        def governance_call(token_config, writer_socket, **params):
            calls.append((token_config, writer_socket, params))
            return {
                "status": "fresh",
                "reservation": {"id": "answer-refresh-reservation:test"},
                "dispatch": {
                    "id": "answer-refresh-dispatch:test",
                    "work_order_ref": "work:bounded-planner:test",
                },
            }

        plane = AgendaControlPlane(
            self.config,
            dashboard_client=self.plane.dashboard,
            timeout_client=self.plane.timeout,
            governance_call=governance_call,
        )
        app = AgendaControlApplication(self.config, plane)
        _session_id, session, _created = app.session(LOGIN, None)
        subject = plane.answer_view()["subjects"][0]
        payload = {
            "subject_binding": subject,
            "question": "Why?",
            "route_decision_ref": "answer-route-decision:test",
            "route_decision_hash": "c" * 64,
            "route_as_of": "2026-08-25T10:00:00.000000+00:00",
        }
        with self.assertRaises(PermissionError):
            app.post_answer_refresh(
                LOGIN, session, "wrong", json.dumps(payload).encode()
            )
        with self.assertRaises(AgendaControlError):
            app.post_answer_refresh(
                LOGIN,
                session,
                session.csrf,
                json.dumps({**payload, "max_cost_units": 100}).encode(),
            )
        result = app.post_answer_refresh(
            LOGIN, session, session.csrf, json.dumps(payload).encode()
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(len(calls), 1)
        token_config, writer_socket, call = calls[0]
        self.assertEqual(token_config, self.config.token_config)
        self.assertEqual(writer_socket, self.config.writer_socket)
        self.assertEqual(call["operation"], "dispatch_answer_refresh")
        self.assertTrue(call["actor_ref"].startswith("human:tailscale-"))
        self.assertNotIn(LOGIN, call["actor_ref"])
        self.assertEqual(call["params"], payload)

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
            "intent_composer": {
                "staging_path": str(Path(self.temp.name) / "intent.sqlite"),
                "scheduler_db": str(Path(self.temp.name) / "scheduler.sqlite"),
                "model_router_db": str(Path(self.temp.name) / "router.sqlite"),
                "broker_socket": str(Path(self.temp.name) / "broker.sock"),
                "broker_auth_key": str(Path(self.temp.name) / "broker.key"),
                "routing_policy_ref": "model-routing-policy-version:intent:1",
                "credential_slot_refs": ["credential-slot:openclaw:intent"],
                "broker_client_id": "client:dalton-intent",
                "expected_agent_id": "chem",
                "timeout_seconds": 60,
                "max_input_tokens": 16000,
                "max_output_tokens": 1200,
                "max_cost_usd": 1.0,
            },
        }
        config = AgendaControlConfig.from_mapping(raw)
        self.assertIsNotNone(config.research_review)
        self.assertIsNotNone(config.intent_composer)
        raw["research_review"]["core_db"] = str(
            Path(self.temp.name) / "core.sqlite"
        )
        with self.assertRaises(Exception):
            AgendaControlConfig.from_mapping(raw)

    def test_single_http_surface_serves_cockpit_and_all_review_routes(self):
        review = ReviewPlane()
        intent = IntentPlane()
        app = AgendaControlApplication(self.config, self.plane, review, intent)
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
            connection.request(
                "GET", "/v1/research-trajectory",
                headers={**headers, "Cookie": cookie},
            )
            response = connection.getresponse()
            trajectory_payload = json.loads(response.read())
            self.assertTrue(trajectory_payload["enabled"])
            self.assertTrue(trajectory_payload["projection_only"])
            self.assertEqual(
                review_payload["csrf_token"],
                transcript_payload["csrf_token"],
            )
            self.assertEqual(
                review_payload["csrf_token"],
                trajectory_payload["csrf_token"],
            )
            connection.request(
                "GET", "/v1/intent",
                headers={**headers, "Cookie": cookie},
            )
            response = connection.getresponse()
            intent_payload = json.loads(response.read())
            self.assertTrue(intent_payload["enabled"])
            self.assertTrue(intent_payload["candidate_only"])
            self.assertFalse(intent_payload["execution_enabled"])
            self.assertEqual(
                review_payload["csrf_token"], intent_payload["csrf_token"]
            )
            connection.request(
                "GET", "/v1/answer",
                headers={**headers, "Cookie": cookie},
            )
            response = connection.getresponse()
            answer_payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertTrue(answer_payload["refresh_enabled"])
            self.assertFalse(answer_payload["adhoc_research_enabled"])
            self.assertEqual(
                review_payload["csrf_token"], answer_payload["csrf_token"]
            )
            body = b'{}'
            connection.request(
                "POST", "/v1/research-trajectory", body=body,
                headers={
                    **headers, "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Dalton-CSRF": review_payload["csrf_token"],
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
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
            body = b'{"request_id":"intent-1","utterance":"check ACN"}'
            connection.request(
                "POST", "/v1/intent/compose", body=body,
                headers={
                    **headers, "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Dalton-CSRF": review_payload["csrf_token"],
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["candidate"]["candidate_only"])
            self.assertEqual(
                intent.calls,
                [("compose", LOGIN, {"request_id": "intent-1", "utterance": "check ACN"})],
            )
            body = b'{"request_id":"confirm-1","candidate_version_ref":"candidate:1","candidate_version_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","decision":"confirm"}'
            connection.request(
                "POST", "/v1/intent/confirm", body=body,
                headers={
                    **headers, "Cookie": cookie,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Dalton-CSRF": review_payload["csrf_token"],
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["status"], "dispatched")
            body = json.dumps({
                "subject_binding": answer_payload["subjects"][0],
                "question": "Why?",
            }).encode()
            connection.request(
                "POST", "/v1/answer/route", body=body,
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
                json.loads(response.read())["decision"]["route"],
                "recommend_agenda_item",
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

    def test_confirmed_effects_route_through_original_writer_principals(self):
        governance_calls = []

        def governance_call(token_config, writer_socket, **kwargs):
            governance_calls.append(kwargs)
            return {"status": "governance-recorded", "operation": kwargs["operation"]}

        review = ReviewPlane()
        dispatcher = CockpitIntentDispatcher(
            self.config,
            self.plane,
            review,
            governance_call=governance_call,
        )
        confirmation = {
            "id": "intent-confirmation:" + "1" * 64,
            "content_hash": "2" * 64,
            "created_at": "2026-08-25T05:00:00+00:00",
        }
        candidate = {
            "id": "intent-candidate-version:" + "3" * 64,
            "content_hash": "4" * 64,
        }
        question_effect = {
            "kind": "research_question_draft",
            "question": "Why?",
            "answer_criteria": "Use governed sources.",
            "subject_binding": {"kind": "mandate", "ref": "mandate-version:1"},
        }
        result = dispatcher.dispatch(LOGIN, {
            "candidate": {**candidate, "candidate": {
                "effect": question_effect, "rationale": "Question",
            }},
            "utterance": {"verbatim_text": "Why?"},
        }, confirmation)
        self.assertEqual(result["operation"], "admit_intent_question")
        self.assertEqual(governance_calls[-1]["operation"], "admit_intent_question")
        priority = {
            "kind": "priority_override_candidate",
            "scope_bindings": [{"ref": "mandate-version:1"}],
            "weight_deltas": {"decision_impact": 2},
            "rationale": "Seven-day focus",
            "effective_for_days": 7,
        }
        dispatcher.dispatch(LOGIN, {
            "candidate": {**candidate, "candidate": {
                "effect": priority, "rationale": "Priority",
            }},
            "utterance": {"verbatim_text": "Raise priority"},
        }, confirmation)
        self.assertEqual(governance_calls[-1]["operation"], "create_priority_override")
        approval = {
            "kind": "context_bound_approval_candidate",
            "target_binding": {
                "kind": "candidate_claim", "ref": "candidate-claim:1",
                "hash": "5" * 64,
            },
            "verdict": "accept",
        }
        dispatcher.dispatch(LOGIN, {
            "candidate": {**candidate, "candidate": {
                "effect": approval, "rationale": "Reviewed exact claim",
            }},
            "utterance": {"verbatim_text": "Accept claim"},
        }, confirmation)
        self.assertEqual(review.calls[-1][0], "research")


if __name__ == "__main__":
    unittest.main()
