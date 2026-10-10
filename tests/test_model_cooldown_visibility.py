"""2026-10-07: a cooled model reaches 「需要你处理」, the parity report and the
model page, with the provider's real reason.

deepseek-v4-flash answered HTTP 402 (account balance exhausted) to every call
from 2026-10-04 on.  The OpenClaw facade turned that into an empty completion,
the broker into ``INVALID_HOST_RESULT``, the router into fourteen
``probe_failed`` cooldowns -- and ``needs_human`` read two table names the live
router never had, so none of it reached the owner.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core import needs_human
from dalton_core.cockpit_model_display import cooldown_note, cooldown_view
from dalton_core.model_fallback_chain import classify_model_failure
from dalton_core.model_profile_health import (
    cooldown_decision,
    failure_code_label,
    is_billing_code,
    is_immediate_cooldown_code,
)
from dalton_core.model_router import ModelRouter

NOW = datetime(2026, 10, 7, 22, 50, tzinfo=timezone.utc)
DEEPSEEK = "profile:deepseek-v4-flash"
GEMINI = "profile:gemini-3-8-flash-antigravity"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class BalanceClassificationTests(unittest.TestCase):
    def test_the_balance_code_is_its_own_cooldown_reason_even_on_probation(self) -> None:
        for code in ("PROVIDER_INSUFFICIENT_BALANCE", "PAYMENT_REQUIRED", "HTTP_402"):
            with self.subTest(code=code):
                self.assertTrue(is_billing_code(code))
                self.assertTrue(is_immediate_cooldown_code(code))
                decided = cooldown_decision(
                    failure_code=code, now=NOW, recent_failures=1,
                    previous_streak=3, on_probation=True)
                self.assertEqual(decided["reason"], "provider_billing")
                self.assertEqual(decided["failure_code"], code)
        self.assertFalse(is_billing_code("INVALID_HOST_RESULT"))
        self.assertFalse(is_billing_code("RATE_LIMITED"))

    def test_the_chain_falls_back_instead_of_halting(self) -> None:
        self.assertEqual(classify_model_failure("PROVIDER_INSUFFICIENT_BALANCE"),
                         "provider_failure")
        self.assertEqual(classify_model_failure(402), "provider_failure")
        self.assertEqual(classify_model_failure(401), "contract_violation")

    def test_codes_read_in_chinese(self) -> None:
        self.assertEqual(failure_code_label("PROVIDER_INSUFFICIENT_BALANCE"),
                         "供应商账户余额不足（HTTP 402）")
        self.assertIn("笼统代码", failure_code_label("INVALID_HOST_RESULT"))
        self.assertIn("SOMETHING_NEW", failure_code_label("SOMETHING_NEW"))
        self.assertEqual(failure_code_label(None), "未记录失败代码")


class LiveRouterSchemaTests(unittest.TestCase):
    """The reader against the table the live ModelRouter actually writes."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "model-router.sqlite"
        self.clock = datetime(2026, 10, 4, 15, 47, tzinfo=timezone.utc)
        self.router = ModelRouter(self.path, clock=lambda: self.clock)
        self.addCleanup(self.router.close)

    def test_a_balance_cooldown_is_one_card_with_the_owners_sentence(self) -> None:
        self.router.record_provider_outcome(profile_id=DEEPSEEK, outcome="served")
        # 2026-10-04T16:48Z is 10-05 in Shanghai, the owner's zone.
        self.clock = datetime(2026, 10, 4, 16, 48, tzinfo=timezone.utc)
        self.router.record_provider_outcome(
            profile_id=DEEPSEEK, outcome="provider_failure",
            failure_code="PROVIDER_INSUFFICIENT_BALANCE")
        self.clock = NOW - timedelta(minutes=5)
        cooled = self.router.record_provider_outcome(
            profile_id=DEEPSEEK, outcome="provider_failure",
            failure_code="PROVIDER_INSUFFICIENT_BALANCE")
        self.assertEqual(cooled["cooldown"]["reason"], "provider_billing")
        self.assertEqual(cooled["cooldown"]["failure_code"], "PROVIDER_INSUFFICIENT_BALANCE")

        items = needs_human.provider_failures(None, now=NOW, model_router_db=self.path)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["kind"], "model_cooldown")
        self.assertEqual(
            item["title"],
            "供应商账户余额不足：deepseek-v4-flash，自 10-05 起探活失败；充值后自动恢复")
        self.assertIn("HTTP 402", item["why_blocked"])
        self.assertIn("充值", item["action"])
        self.assertEqual(item["detail"]["failure_code"], "PROVIDER_INSUFFICIENT_BALANCE")
        self.assertEqual(item["detail"]["cooldowns_in_run"], 2)
        self.assertEqual(item["where_open"], "models")

    def test_a_served_call_or_an_expired_cooldown_leaves_no_card(self) -> None:
        self.router.record_provider_outcome(
            profile_id=DEEPSEEK, outcome="provider_failure", failure_code="RATE_LIMITED")
        self.assertEqual(len(needs_human.model_cooldowns(self.path, now=self.clock)), 1)
        later = self.clock + timedelta(hours=1)
        self.assertEqual(needs_human.model_cooldowns(self.path, now=later), [])
        self.clock += timedelta(minutes=1)
        self.router.record_provider_outcome(profile_id=DEEPSEEK, outcome="served")
        self.assertEqual(needs_human.model_cooldowns(
            self.path, now=self.clock + timedelta(minutes=1)), [])


class LedgerAndLegacyTests(unittest.TestCase):
    """What a cooldown blocks, read off the link ledger; and the older tables."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "model-router.sqlite"

    def _live_shape(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE model_profile_cooldowns (
                cooldown_sequence INTEGER PRIMARY KEY AUTOINCREMENT, cooldown_id TEXT,
                profile_id TEXT, reason TEXT, failure_code TEXT, failure_count INTEGER,
                streak INTEGER, started_at TEXT, until TEXT, cooldown_hash TEXT,
                cooldown_json TEXT, created_at TEXT);
            CREATE TABLE model_profile_health_events (
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT,
                outcome TEXT, failure_code TEXT, observed_at TEXT);
            CREATE TABLE model_route_chain_links (
                link_sequence INTEGER PRIMARY KEY AUTOINCREMENT, work_order_id TEXT,
                capability TEXT, attempt_number INTEGER, purpose TEXT, tier TEXT,
                chain_position INTEGER, profile_id TEXT, served INTEGER,
                skip_reason TEXT, created_at TEXT);
            CREATE TABLE model_endpoint_profile_versions (
                profile_version_ref TEXT, profile_id TEXT, version INTEGER, provider TEXT);
        """)
        return db

    def test_the_card_names_the_stages_that_lost_the_link_and_who_answered(self) -> None:
        db = self._live_shape()
        started = NOW - timedelta(hours=2)
        db.execute("INSERT INTO model_endpoint_profile_versions VALUES (?,?,?,?)",
                   ("v1", DEEPSEEK, 1, "deepseek"))
        db.execute("INSERT INTO model_profile_health_events (profile_id, outcome, observed_at) "
                   "VALUES (?, 'served', ?)",
                   (DEEPSEEK, _iso(datetime(2026, 10, 4, 15, 47, tzinfo=timezone.utc))))
        db.execute("INSERT INTO model_profile_health_events (profile_id, outcome, failure_code, "
                   "observed_at) VALUES (?, 'provider_failure', 'INVALID_HOST_RESULT', ?)",
                   (DEEPSEEK, _iso(datetime(2026, 10, 4, 16, 48, tzinfo=timezone.utc))))
        db.execute(
            "INSERT INTO model_profile_cooldowns (cooldown_id, profile_id, reason, failure_code, "
            "failure_count, streak, started_at, until, cooldown_json, created_at) "
            "VALUES ('c1', ?, 'probe_failed', 'INVALID_HOST_RESULT', 1, 14, ?, ?, ?, ?)",
            (DEEPSEEK, _iso(started), _iso(started + timedelta(hours=6)),
             json.dumps({"route_decision_ref": "route-decision:x"}), _iso(started)))
        for index in range(3):
            work = f"work:{index}"
            db.execute("INSERT INTO model_route_chain_links (work_order_id, capability, "
                       "attempt_number, purpose, tier, chain_position, profile_id, served, "
                       "skip_reason, created_at) VALUES (?, 'research', 1, "
                       "'research_localization', 'cheap', 1, ?, 0, 'provider_cooldown', ?)",
                       (work, DEEPSEEK, _iso(NOW - timedelta(hours=1))))
            db.execute("INSERT INTO model_route_chain_links (work_order_id, capability, "
                       "attempt_number, purpose, tier, chain_position, profile_id, served, "
                       "skip_reason, created_at) VALUES (?, 'research', 1, "
                       "'research_localization', 'cheap', 2, ?, 1, NULL, ?)",
                       (work, GEMINI, _iso(NOW - timedelta(hours=1))))
        db.commit()
        db.close()

        [item] = needs_human.provider_failures(None, now=NOW, model_router_db=self.path)
        self.assertEqual(item["title"],
                         "模型冷却中：deepseek-v4-flash，自 10-05 起一直失败（宿主返回了空或无效的文本）")
        self.assertIn("「整理研究内容的中文初稿」跳过它 3 次，改由 gemini-3-8-flash-antigravity 承接",
                      item["why_blocked"])
        self.assertIn("provider=deepseek", item["action"])
        self.assertEqual(item["detail"]["route_decision_ref"], "route-decision:x")
        self.assertEqual(item["detail"]["blocks"][0]["purpose"], "research_localization")
        self.assertTrue(item["detail"]["owner_steps"])

        from dalton_core.workspace_health_parity import Environment, model_cooldown_rows

        env = Environment("legacy", self.path.parent)
        self.addCleanup(env.close)
        [row] = model_cooldown_rows(env, now=NOW)
        self.assertEqual((row["check"], row["status"]), ("model_cooldowns", "info"))
        self.assertIn("1 个模型在冷却：deepseek-v4-flash", row["detail"])
        self.assertIn("research_localization×3", row["detail"])
        self.assertEqual(row["cooldowns"][0]["failure_code"], "INVALID_HOST_RESULT")

    def test_an_older_core_with_the_legacy_table_still_shows_its_cooldown(self) -> None:
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE model_credential_cooldowns (credential_slot_ref TEXT, "
                   "reason TEXT, cooldown_until TEXT, created_at TEXT)")
        db.execute("INSERT INTO model_credential_cooldowns VALUES (?, ?, ?, ?)",
                   ("profile:gpt-6-astra", "provider_rate_limited",
                    _iso(NOW + timedelta(hours=1)), _iso(NOW - timedelta(hours=1))))
        db.commit()
        db.close()
        [item] = needs_human.provider_failures(None, now=NOW, model_router_db=self.path)
        self.assertEqual(item["kind"], "model_cooldown")
        self.assertIn("gpt-6-astra", item["title"])

    def test_parity_says_nothing_is_cooling_rather_than_staying_silent(self) -> None:
        self._live_shape().close()
        from dalton_core.workspace_health_parity import Environment, model_cooldown_rows

        env = Environment("legacy", self.path.parent)
        self.addCleanup(env.close)
        self.assertEqual(model_cooldown_rows(env, now=NOW)[0]["detail"], "没有模型在冷却")


class ModelPageMarkTests(unittest.TestCase):
    COOLING = {"profile_id": DEEPSEEK, "reason": "probe_failed",
               "failure_code": "INVALID_HOST_RESULT", "streak": 14,
               "started_at": "2026-10-07T17:03:48+00:00",
               "until": "2026-10-07T23:03:48+00:00"}

    def test_the_note_carries_the_code_and_the_view_carries_a_badge(self) -> None:
        self.assertIn("笼统代码", cooldown_note(self.COOLING))
        billing = {**self.COOLING, "reason": "provider_billing",
                   "failure_code": "PROVIDER_INSUFFICIENT_BALANCE"}
        self.assertIn("余额不足", cooldown_note(billing))
        view = cooldown_view(billing, display_name=lambda item: item.split(":")[1])
        self.assertEqual(view["display_name"], "deepseek-v4-flash")
        self.assertEqual(view["badge"], "冷却中，至 23:03（UTC）")
        self.assertEqual(view["failure_code_label"], "供应商账户余额不足（HTTP 402）")
        self.assertIsNone(cooldown_view(None))

    def test_the_page_marks_cooled_models_in_the_editor_picker_and_routing_card(self) -> None:
        page = (Path(needs_human.__file__).parent / "cockpit_control.html").read_text(
            encoding="utf-8")
        self.assertIn('(r.cooldowns||{})[profileId]', page)
        self.assertIn('choice.cooldown?"（"+choice.cooldown.badge+"）"', page)
        self.assertIn('l.cooldown?"（"+l.cooldown.badge+"）"', page)
        self.assertIn("card.cooldown_note", page)
        self.assertIn("p.cooldown_note", page)


if __name__ == "__main__":
    unittest.main()
