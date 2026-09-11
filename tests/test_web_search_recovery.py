import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import re

from dalton_core.store import content_hash
from dalton_core.web_search_provider import PROVIDER_SELECTION_POLICY
from dalton_core.web_search_recovery import provider_recovery_available


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "openclaw.json"
        self.select("gemini")
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript('''
            CREATE TABLE runner_request_journal (runner_request_ref TEXT, connector_invocation_ref TEXT);
            CREATE TABLE runner_attempt_journal_events (
              event_seq INTEGER, event_id TEXT, runner_request_ref TEXT, state TEXT,
              reservation_ref TEXT, event_at TEXT, payload_json TEXT, content_hash TEXT, created_at TEXT);
        ''')
        self.connection.execute("INSERT INTO runner_request_journal VALUES ('request:1','invocation:1')")
        self.launcher = SimpleNamespace(
            networked=True, _ticket_re=re.compile(r"web-search-discovery:[0-9a-f]{24}"),
            _ticket_path=lambda _: self.root / "ticket.json", expected_provider=None,
            openclaw_config_path=self.config, broker_socket=self.root / "broker.sock",
        )
        self.dispatch = {
            "ticket_ref": "web-search-discovery:" + "a" * 24, "status": "failed",
            "company_ref": "company:test", "mission_version_ref": "mission:1",
            "mission_version_hash": "b" * 64, "spec_ref": "news", "source_ref": "source:web-search",
        }
        self.summary = {**self.dispatch, "authorization": dict(self.dispatch), "search": {
            "connector_invocation_ref": "invocation:1", "runner_response_ref": "response:1",
        }}
        self.write_summary()
        self.events("provider_contract_drift")

    def select(self, provider):
        self.config.write_text(json.dumps({"tools": {"web": {"search": {"provider": provider}}}}))

    def write_summary(self):
        (self.root / "summary.json").write_text(json.dumps(self.summary))

    def events(self, code):
        self.connection.execute("DELETE FROM runner_attempt_journal_events")
        events = [("observed", {"error": {"code": code}}), ("responded", {"response": {
            "id": "response:1", "connector_invocation_ref": "invocation:1", "outcome": "failed",
        }})]
        for ordinal, (state, payload) in enumerate(events, 1):
            body = {"runner_request_ref": "request:1", "request_ordinal": ordinal,
                    "state": state, "reservation_ref": None, "event_at": "2026-09-11T16:00:00Z",
                    "recorded_at": "2026-09-11T16:00:00Z", "payload": payload}
            self.connection.execute("INSERT INTO runner_attempt_journal_events VALUES (?,?,?,?,?,?,?,?,?)",
                (ordinal, f"event:{ordinal}", "request:1", state, None, body["event_at"],
                 json.dumps(payload), content_hash(body), body["recorded_at"]))

    def eligible(self):
        return provider_recovery_available(self.launcher, self.dispatch, self.connection)

    def test_one_recovery_after_policy_upgrade_or_provider_change(self):
        self.assertTrue(self.eligible())
        self.summary.update(expected_provider="gemini", provider_selection_policy=PROVIDER_SELECTION_POLICY)
        self.write_summary()
        self.assertFalse(self.eligible(), "same failed selection must observe the configured retry interval")
        self.select("antigravity")
        self.assertTrue(self.eligible())

    def test_other_failures_and_unresolved_calls_do_not_bypass_cadence(self):
        self.events("permission_denied")
        self.assertFalse(self.eligible())
        self.events("provider_contract_drift")
        self.connection.execute("DELETE FROM runner_attempt_journal_events WHERE state='responded'")
        self.assertFalse(self.eligible())

    def test_invalid_proof_and_wrong_company_do_not_release_retry(self):
        self.connection.execute("UPDATE runner_attempt_journal_events SET content_hash='tampered' WHERE state='observed'")
        self.assertFalse(self.eligible())
        self.events("provider_contract_drift")
        self.summary["company_ref"] = "company:other"
        self.write_summary()
        self.assertFalse(self.eligible())

    def test_completed_or_running_dispatch_is_never_retried(self):
        for status in ("succeeded", "launched", "pending"):
            self.dispatch["status"] = status
            self.assertFalse(self.eligible())


if __name__ == "__main__":
    unittest.main()
