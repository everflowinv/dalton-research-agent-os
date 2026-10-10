"""Vision-gap #13: owner delivery to Feishu -- contract, ledger, producers, caps, lane.

Hermetic: the host tool is a fake runner, every database is a temp file, and
opening a socket or starting a real ``openclaw`` fails the test.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core import owner_delivery as od
from dalton_core.store import canonical_json, content_hash

REPO = Path(__file__).resolve().parents[1]
CHAT = "oc_0123456789abcdef"
OWNER = "human:owner"
T0 = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai


def _no_network(*args, **kwargs):  # pragma: no cover - only on a regression
    raise AssertionError(f"owner delivery opened a network connection: {args!r}")


def _no_process(*args, **kwargs):  # pragma: no cover - only on a regression
    raise AssertionError(f"owner delivery started a real process: {args!r}")


class FakeSender:
    def __init__(self, fail: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, chat_id: str, body: str) -> str:
        if self.fail:
            raise od.DeliverySendError(self.fail, "host said no")
        self.sent.append((chat_id, body))
        return f"om_{len(self.sent):04d}"


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


class Env:
    """A state directory with the minimal tables each producer reads."""

    def __init__(self, root: Path) -> None:
        self.state = root / "state"
        self.state.mkdir()
        self.core_path = self.state / "core.sqlite"
        core = sqlite3.connect(self.core_path)
        core.executescript("""
            CREATE TABLE weekly_brief_issue_versions (version_id TEXT PRIMARY KEY,
                record_json TEXT, created_at TEXT, content_hash TEXT);
            CREATE TABLE weekly_brief_issue_pointer (brief_ref TEXT PRIMARY KEY,
                version_id TEXT);
            CREATE TABLE owner_lever_receipts (receipt_id TEXT PRIMARY KEY,
                sequence INTEGER, lever TEXT, record_json TEXT, created_at TEXT);
            CREATE TABLE conviction_call_proposals (proposal_id TEXT PRIMARY KEY,
                company_ref TEXT, direction TEXT, created_at TEXT);
            CREATE TABLE conviction_call_decisions (decision_id TEXT PRIMARY KEY,
                proposal_ref TEXT);
        """)
        core.close()
        self.router_path = self.state / "model-router.sqlite"
        router = sqlite3.connect(self.router_path)
        router.executescript("""
            CREATE TABLE model_fallback_notices (notice_sequence INTEGER PRIMARY KEY
                AUTOINCREMENT, notice_id TEXT UNIQUE, notice_json TEXT, created_at TEXT);
            CREATE TABLE model_fallback_notice_acks (notice_id TEXT UNIQUE);
        """)
        router.close()
        self.settings = {**od.DEFAULT_SETTINGS, "cockpit_base_url": "https://cockpit.test:8793/"}

    def approve(self, at: datetime = T0) -> dict:
        record = od.approve_contract(None, approved_by=OWNER, chat_id=CHAT, now=at)
        od.write_contract(self.state, record, now=at)
        return record

    def _core(self, sql: str, args: tuple) -> None:
        with closing(sqlite3.connect(self.core_path)) as core, core:
            core.execute(sql, args)

    def brief(self, version: int, at: datetime, brief_ref: str = "brief:it") -> str:
        version_id = f"weekly-brief-issue:{brief_ref}:{version}"
        issue = {"id": version_id, "version": version, "industry_ref": "industry:it-services",
                 "period": {"start": "2026-09-28", "end": "2026-10-04"},
                 "change_summary": {"is_baseline": False, "new_claim_version_refs": ["c1", "c2"],
                                    "carried_claim_version_refs": ["c0"],
                                    "removed_claim_version_refs": [],
                                    "changed_driver_refs": ["d1"], "new_gap_refs": [],
                                    "resolved_gap_refs": ["g1"],
                                    "changed_thesis_company_refs": ["company:ticker:epam"]}}
        self._core("INSERT INTO weekly_brief_issue_versions VALUES (?,?,?,?)",
                   (version_id, json.dumps(issue), _iso(at), "h" * 64))
        self._core("INSERT OR REPLACE INTO weekly_brief_issue_pointer VALUES (?,?)",
                   (brief_ref, version_id))
        return version_id

    def lever(self, sequence: int, lever: str, at: datetime) -> str:
        receipt_id = f"owner-lever-receipt:{sequence}"
        wire = {"id": receipt_id, "lever": lever, "scope_kind": "global", "scope_ref": "global",
                "reason": "市场异常，先停一下", "until": None, "created_at": _iso(at)}
        self._core("INSERT INTO owner_lever_receipts VALUES (?,?,?,?,?)",
                   (receipt_id, sequence, lever, json.dumps(wire, ensure_ascii=False), _iso(at)))
        return receipt_id

    def call(self, ref: str, at: datetime, direction: str = "long") -> None:
        self._core("INSERT INTO conviction_call_proposals VALUES (?,?,?,?)",
                   (ref, "company:ticker:epam", direction, _iso(at)))

    def decide(self, ref: str) -> None:
        self._core("INSERT INTO conviction_call_decisions VALUES (?,?)", ("d:" + ref, ref))

    def notice(self, ref: str, at: datetime, message: str = "模型 x 已在 OpenClaw 消失") -> None:
        with closing(sqlite3.connect(self.router_path)) as router, router:
            router.execute(
                "INSERT INTO model_fallback_notices(notice_id, notice_json, created_at) "
                "VALUES (?,?,?)", (ref, json.dumps({"id": ref, "message": message},
                                                   ensure_ascii=False), _iso(at)))

    def run(self, at: datetime, sender: FakeSender, **overrides) -> dict:
        return od.run_delivery_pass(self.state, now=at, sender=sender,
                                    settings={**self.settings, **overrides})

    def receipts(self) -> list[sqlite3.Row]:
        with od.DeliveryLedger(self.state / od.LEDGER_FILE_NAME, read_only=True) as ledger:
            return ledger.rows()


class HermeticCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.env = Env(Path(self.temp.name))
        for target in ("socket.create_connection", "socket.socket.connect",
                       "urllib.request.urlopen", "http.client.HTTPConnection.connect"):
            guard = patch(target, _no_network)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch("subprocess.run", _no_process)
        guard.start()
        self.addCleanup(guard.stop)


class ContractTests(HermeticCase):
    def test_the_committed_record_is_exactly_what_code_seeds(self) -> None:
        committed = json.loads(
            (REPO / "deploy/connector-governance" / od.CONTRACT_FILE_NAME).read_text())
        self.assertEqual(committed, od.build_contract_record())
        self.assertEqual(committed["status"], "proposed")
        self.assertIsNone(committed["recipient"]["chat_id"])

    def test_proposed_sends_nothing_and_says_why(self) -> None:
        od.write_contract(self.env.state, od.build_contract_record())
        self.env.call("call:1", T0)
        sender = FakeSender()
        result = self.env.run(T0, sender)
        self.assertEqual(result["status"], "not_granted")
        self.assertIn("还没有批准", result["reason"])
        self.assertEqual(sender.sent, [])
        self.assertFalse((self.env.state / od.LEDGER_FILE_NAME).exists())

    def test_approval_needs_a_person_and_a_chat_id(self) -> None:
        with self.assertRaises(od.OwnerDeliveryError):
            od.approve_contract(None, approved_by="automation:x", chat_id=CHAT)
        for bad in ("", "研究群", "https://feishu.cn/x", "ou_0123456789abcdef"):
            with self.assertRaises(od.OwnerDeliveryError, msg=bad):
                od.approve_contract(None, approved_by=OWNER, chat_id=bad)
        record = od.approve_contract(None, approved_by=OWNER, chat_id=CHAT, now=T0)
        state = od.evaluate_contract(record)
        self.assertTrue(state.approved)
        self.assertEqual(state.chat_id, CHAT)

    def test_a_hand_edit_or_a_changed_contract_voids_the_approval(self) -> None:
        record = od.approve_contract(None, approved_by=OWNER, chat_id=CHAT, now=T0)
        edited = {**record, "recipient": {"chat_id": "oc_ffffffffffffffff", "account": None}}
        self.assertFalse(od.evaluate_contract(edited).approved)
        self.assertIn("手工改过", od.evaluate_contract(edited).reason)
        terms = json.loads(canonical_json(od.CONTRACT_TERMS))
        terms["artifact_kinds"].append("every_claim")
        body = {k: v for k, v in record.items() if k != "content_hash"}
        body["terms"] = terms
        widened = {**body, "content_hash": content_hash(body)}
        self.assertIn("合同已经变了", od.evaluate_contract(widened).reason)
        self.assertFalse(od.evaluate_contract({**record, "extra": 1}).approved)

    def test_revoke_returns_to_proposed(self) -> None:
        self.assertEqual(od.revoke_contract(self.env.approve()), od.build_contract_record())


class LedgerTests(HermeticCase):
    def test_receipts_are_append_only(self) -> None:
        with od.DeliveryLedger(self.env.state / od.LEDGER_FILE_NAME) as ledger:
            ledger.append(attempt_id="a1", kind="weekly_brief", refs=["v1"],
                          key=od.idempotency_key("weekly_brief", ["v1"]),
                          outcome="delivered", at=T0, message_id="om_1")
            for sql in ("UPDATE delivery_receipts SET outcome='failed'",
                        "DELETE FROM delivery_receipts"):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    with ledger.connection:
                        ledger.connection.execute(sql)

    def test_one_settled_receipt_per_identity(self) -> None:
        key = od.idempotency_key("weekly_brief", ["v1"])
        with od.DeliveryLedger(self.env.state / od.LEDGER_FILE_NAME) as ledger:
            ledger.append(attempt_id="a1", kind="weekly_brief", refs=["v1"], key=key,
                          outcome="delivered", at=T0, message_id="om_1")
            ledger.append(attempt_id="a2", kind="weekly_brief", refs=["v1"], key=key,
                          outcome="failed", at=T0, reason="x")
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.append(attempt_id="a3", kind="weekly_brief", refs=["v1"], key=key,
                              outcome="delivered", at=T0, message_id="om_2")

    def test_the_identity_is_the_versions_and_the_channel(self) -> None:
        self.assertEqual(od.idempotency_key("weekly_brief", ["b", "a"]),
                         od.idempotency_key("weekly_brief", ["a", "b", "a"]))
        self.assertNotEqual(od.idempotency_key("weekly_brief", ["a"]),
                            od.idempotency_key("weekly_brief", ["a"], channel="discord"))

    def test_a_pass_that_died_mid_send_is_indeterminate_not_resent(self) -> None:
        self.env.approve()
        version = self.env.brief(1, T0 + timedelta(minutes=1))
        key = od.idempotency_key("weekly_brief", [version])
        with od.DeliveryLedger(self.env.state / od.LEDGER_FILE_NAME) as ledger:
            ledger.append(attempt_id="a-crashed", kind="weekly_brief", refs=[version], key=key,
                          outcome="attempting", at=T0 + timedelta(minutes=2), body="x")
        sender = FakeSender()
        result = self.env.run(T0 + timedelta(hours=1), sender)
        self.assertEqual(result["interrupted_settled"], ["a-crashed"])
        self.assertEqual(sender.sent, [])
        self.assertEqual([r["outcome"] for r in self.env.receipts()],
                         ["attempting", "indeterminate"])


class ProducerTests(HermeticCase):
    def test_a_weekly_brief_is_delivered_once_with_summary_and_link(self) -> None:
        self.env.approve()
        self.env.brief(1, T0 - timedelta(days=7))  # before approval: not replayed
        self.env.brief(2, T0 + timedelta(minutes=5))
        sender = FakeSender()
        first = self.env.run(T0 + timedelta(minutes=10), sender)
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(len(sender.sent), 1)
        chat, body = sender.sent[0]
        self.assertEqual(chat, CHAT)
        self.assertIn("每周研究 Brief", body)
        self.assertIn("第 2 期", body)
        self.assertIn("新增正式 Claim 2 条", body)
        self.assertIn("https://cockpit.test:8793/weekly-brief", body)
        again = self.env.run(T0 + timedelta(hours=3), sender)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(again["status"], "idle")
        delivered = [r for r in self.env.receipts() if r["outcome"] == "delivered"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["message_id"], "om_0001")
        self.assertEqual(delivered[0]["recipient_ref"], CHAT)
        self.assertEqual(json.loads(delivered[0]["artifact_refs_json"]),
                         ["weekly-brief-issue:brief:it:2"])

    def test_a_new_brief_version_is_a_new_message(self) -> None:
        self.env.approve()
        self.env.brief(1, T0 + timedelta(minutes=1))
        sender = FakeSender()
        self.env.run(T0 + timedelta(minutes=2), sender)
        self.env.brief(2, T0 + timedelta(days=7))
        self.env.run(T0 + timedelta(days=7, minutes=1), sender)
        self.assertEqual(len(sender.sent), 2)

    def test_the_owners_emergency_stop_goes_out_first_and_at_once(self) -> None:
        self.env.approve()
        self.env.lever(1, "emergency_stop", T0 - timedelta(days=1))  # history: not replayed
        self.env.brief(1, T0 + timedelta(minutes=1))
        self.env.lever(2, "emergency_stop", T0 + timedelta(minutes=2))
        sender = FakeSender()
        self.env.run(T0 + timedelta(minutes=3), sender)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("紧急停机已生效", sender.sent[0][1])
        self.assertIn("市场异常", sender.sent[0][1])
        # Batch ad: the link opens the v2 administrator page's run controls.
        self.assertIn("https://cockpit.test:8793/admin#run", sender.sent[0][1])
        self.assertIn("每周研究 Brief", sender.sent[1][1])
        self.env.lever(3, "resume_all", T0 + timedelta(minutes=4))
        self.env.run(T0 + timedelta(minutes=5), sender)
        self.assertIn("已恢复全部运行", sender.sent[2][1])

    def test_the_needs_human_digest_batches_and_waits_its_window(self) -> None:
        self.env.approve()
        for index in range(3):
            self.env.call(f"call:{index}", T0 + timedelta(minutes=index), direction="short")
        sender = FakeSender()
        self.env.run(T0 + timedelta(minutes=10), sender)
        self.assertEqual(len(sender.sent), 1, "three items, one digest")
        body = sender.sent[0][1]
        self.assertIn("需要你处理：3 件等你裁决（新增 3 件）", body)
        self.assertEqual(body.count("是否采纳这条投资 call：做空"), 3)
        # Batch ad: the link opens the v2 cockpit's 待我处理 page.
        self.assertIn("在「待办决策」页逐件裁决：https://cockpit.test:8793/#/tasks", body)
        # A fourth item inside the window waits; nothing is sent per item.
        self.env.call("call:3", T0 + timedelta(hours=1))
        waited = self.env.run(T0 + timedelta(hours=2), sender)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(waited["waiting"][0]["reason"], "digest_interval")
        # After the window: one digest, announcing only the new one.
        self.env.decide("call:0")
        self.env.run(T0 + timedelta(hours=6, minutes=11), sender)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("3 件等你裁决（新增 1 件）", sender.sent[1][1])
        self.assertEqual(sender.sent[1][1].count("［新］"), 1)
        # Nothing new: no digest at all, however long it has been.
        self.env.run(T0 + timedelta(days=2), sender)
        self.assertEqual(len(sender.sent), 2)

    def test_the_digest_window_is_configurable(self) -> None:
        self.env.approve()
        self.env.call("call:a", T0)
        sender = FakeSender()
        self.env.run(T0 + timedelta(minutes=1), sender, digest_interval_hours=1)
        self.env.call("call:b", T0 + timedelta(minutes=2))
        self.env.run(T0 + timedelta(minutes=62), sender, digest_interval_hours=1)
        self.assertEqual(len(sender.sent), 2)

    def test_fallback_notices_are_immediate_but_rate_limited(self) -> None:
        self.env.approve()
        self.env.notice("n:old", T0 - timedelta(hours=1))  # before approval
        self.env.notice("n:1", T0 + timedelta(minutes=1), "模型 a 已在 OpenClaw 消失；环节 x 已自动回退到 b")
        sender = FakeSender()
        self.env.run(T0 + timedelta(minutes=2), sender)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("模型回退通知（1 条）", sender.sent[0][1])
        self.assertIn("AI 服务设置：https://cockpit.test:8793/admin#ai", sender.sent[0][1])
        self.assertNotIn("n:old", sender.sent[0][1])
        self.env.notice("n:2", T0 + timedelta(minutes=10))
        self.env.notice("n:3", T0 + timedelta(minutes=20))
        waited = self.env.run(T0 + timedelta(minutes=30), sender)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(waited["waiting"][0]["reason"], "fallback_rate_limit")
        self.env.run(T0 + timedelta(minutes=63), sender)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("模型回退通知（2 条）", sender.sent[1][1])
        self.env.run(T0 + timedelta(hours=5), sender)
        self.assertEqual(len(sender.sent), 2)

    def test_an_acknowledged_notice_is_not_pushed(self) -> None:
        self.env.approve()
        self.env.notice("n:acked", T0 + timedelta(minutes=1))
        with closing(sqlite3.connect(self.env.router_path)) as router, router:
            router.execute("INSERT INTO model_fallback_notice_acks VALUES ('n:acked')")
        sender = FakeSender()
        self.env.run(T0 + timedelta(minutes=2), sender)
        self.assertEqual(sender.sent, [])


class CapAndFailureTests(HermeticCase):
    def test_the_day_cap_spends_by_priority_and_resets_next_day(self) -> None:
        self.env.approve()
        self.env.brief(1, T0 + timedelta(minutes=1), brief_ref="brief:a")
        self.env.brief(1, T0 + timedelta(minutes=1), brief_ref="brief:b")
        self.env.lever(1, "emergency_stop", T0 + timedelta(minutes=2))
        self.env.call("call:1", T0 + timedelta(minutes=1))
        sender = FakeSender()
        result = self.env.run(T0 + timedelta(minutes=3), sender, daily_message_cap=2)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("紧急停机", sender.sent[0][1])
        self.assertEqual({row["reason"] for row in result["waiting"]}, {"daily_cap"})
        capped = [r for r in self.env.receipts() if r["outcome"] == "capped"]
        self.assertEqual(len(capped), 2)
        self.assertIn("每日上限", capped[0]["reason"])
        # Capped again the same day: no new capped rows, no sends.
        self.env.run(T0 + timedelta(hours=2), sender, daily_message_cap=2)
        self.assertEqual(len([r for r in self.env.receipts() if r["outcome"] == "capped"]), 2)
        self.assertEqual(len(sender.sent), 2)
        # Next Shanghai day: the rest goes out.
        self.env.run(T0 + timedelta(days=1), sender, daily_message_cap=2)
        self.assertEqual(len(sender.sent), 4)

    def test_a_failure_is_a_receipt_with_a_reason_and_is_retried(self) -> None:
        self.env.approve()
        self.env.brief(1, T0 + timedelta(minutes=1))
        broken = FakeSender(fail="openclaw_exit_1")
        result = self.env.run(T0 + timedelta(minutes=2), broken)
        self.assertEqual(result["status"], "failed")
        failed = [r for r in self.env.receipts() if r["outcome"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("openclaw_exit_1", failed[0]["reason"])
        working = FakeSender()
        self.env.run(T0 + timedelta(minutes=10), working)
        self.assertEqual(working.sent, [], "retry waits retry_after_minutes")
        self.env.run(T0 + timedelta(minutes=40), working)
        self.assertEqual(len(working.sent), 1)

    def test_a_failed_digest_is_retried_by_the_next_digest(self) -> None:
        self.env.approve()
        self.env.call("call:1", T0)
        self.env.run(T0 + timedelta(minutes=1), FakeSender(fail="openclaw_timeout"))
        sender = FakeSender()
        self.env.run(T0 + timedelta(hours=1), sender)
        self.assertEqual(sender.sent, [])
        self.env.run(T0 + timedelta(hours=6, minutes=2), sender)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("新增 1 件", sender.sent[0][1])


class SenderTests(unittest.TestCase):
    def test_the_host_tool_argv_is_fixed_and_the_message_id_is_read_back(self) -> None:
        calls = []

        def runner(argv, env, timeout):
            calls.append((list(argv), dict(env), timeout))
            return {"payload": {"ok": True, "result": {"messageId": "om_abc"}}}

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            exe = Path(directory) / "openclaw"
            exe.write_text("#!/bin/sh\nexit 1\n")
            exe.chmod(0o700)
            sender = od.OpenClawFeishuSender(executable=str(exe), account="main",
                                             timeout_seconds=30, runner=runner)
            with patch.dict(os.environ, {"SECRET_TOKEN": "x", "OPENCLAW_STATE_DIR": "/s"}):
                self.assertEqual(sender.send(CHAT, "正文"), "om_abc")
        argv, env, timeout = calls[0]
        self.assertEqual(argv, [str(exe), "message", "send", "--channel", "feishu",
                                "--account", "main", "--target", CHAT,
                                "--message", "正文", "--json"])
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertEqual(env.get("OPENCLAW_STATE_DIR"), "/s")
        self.assertEqual(timeout, 30)

    def test_a_refusal_or_a_missing_receipt_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            exe = Path(directory) / "openclaw"
            exe.write_text("")
            exe.chmod(0o700)
            for answer, code in (({"payload": {"ok": False, "error": "no"}}, "openclaw_send_refused"),
                                 ({"payload": {"ok": True}}, "openclaw_send_missing_receipt")):
                sender = od.OpenClawFeishuSender(executable=str(exe), account=None,
                                                 timeout_seconds=5,
                                                 runner=lambda *a, answer=answer: answer)
                with self.assertRaises(od.DeliverySendError) as caught:
                    sender.send(CHAT, "x")
                self.assertEqual(caught.exception.code, code)
        missing = od.OpenClawFeishuSender(executable="/nonexistent/openclaw", account=None,
                                          timeout_seconds=5, runner=lambda *a: {})
        with self.assertRaises(od.DeliverySendError) as caught:
            missing.send(CHAT, "x")
        self.assertEqual(caught.exception.code, "openclaw_executable_unavailable")


class SettingsTests(unittest.TestCase):
    def test_defaults_are_code_and_overrides_are_closed(self) -> None:
        self.assertEqual(od.validate_settings(None), {})
        self.assertEqual(od.validate_settings({"daily_message_cap": 3}), {"daily_message_cap": 3})
        for bad in ({"daily_message_cap": 0}, {"chat_id": CHAT}, {"digest_interval_hours": "6"},
                    {"cockpit_base_url": "ftp://x"}, {"day_timezone": "Mars/Base"}):
            with self.assertRaises(od.OwnerDeliveryError, msg=bad):
                od.validate_settings(bad)

    def test_the_service_config_supplies_overrides_and_the_cockpit_link(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            config = Path(directory) / "service.json"
            config.write_text(json.dumps({
                "owner_delivery": {"digest_interval_hours": 3},
                "control": {"enabled": True, "config": {"tailscale_host": "mac.ts.net",
                                                        "port": 8793}},
                "outbox": {"enabled": False, "config": {"openclaw_executable": "/opt/oc"}}}))
            settings = od.load_settings(directory, service_config_path=config)
        self.assertEqual(settings["digest_interval_hours"], 3)
        self.assertEqual(settings["daily_message_cap"], od.DEFAULT_SETTINGS["daily_message_cap"])
        self.assertEqual(settings["cockpit_base_url"], "https://mac.ts.net:8793/")
        self.assertEqual(settings["openclaw_executable"], "/opt/oc")

    def test_service_config_refuses_a_bad_block(self) -> None:
        from dalton_core.service import ServiceConfig, ServiceConfigError

        with self.assertRaises(ServiceConfigError):
            ServiceConfig.from_mapping({"owner_delivery": {"chat_id": CHAT}})


class OwnerCommandTests(HermeticCase):
    def test_dry_run_writes_nothing_and_apply_writes_an_approved_record(self) -> None:
        (self.env.state / "core.sqlite").touch()
        od.write_contract(self.env.state, od.build_contract_record())
        path = od.contract_path(self.env.state)
        before = path.read_bytes()
        args = ["approve", "--state-dir", str(self.env.state), "--chat-id", CHAT,
                "--approved-by", OWNER]
        with patch("sys.stdout"):
            self.assertEqual(od.main(args), 0)
        self.assertEqual(path.read_bytes(), before)
        with patch("sys.stdout"):
            self.assertEqual(od.main(args + ["--apply"]), 0)
        self.assertTrue(od.load_contract(self.env.state).approved)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertTrue(list(path.parent.glob(path.name + ".pre-*")))
        with patch("sys.stdout"):
            self.assertEqual(od.main(["revoke", "--state-dir", str(self.env.state), "--apply"]), 0)
        self.assertFalse(od.load_contract(self.env.state).approved)

    def test_the_ops_script_is_parameterised_by_state_dir(self) -> None:
        text = (REPO / "docs/ops/owner-delivery-enable.sh").read_text()
        self.assertIn("--state-dir", text)
        self.assertNotRegex(text, r"/(?:Volumes|Users|home|mnt|media)/[^\s\"']+")
        self.assertNotIn("oc_", text.replace("oc_xxx", "").replace("oc_ 开头", "")
                         .replace("--chat-id oc_...", ""))

    def test_needs_human_names_the_unapproved_channel_quietly(self) -> None:
        from dalton_core.needs_human import governance_records

        od.write_contract(self.env.state, od.build_contract_record())
        items = governance_records(self.env.state / od.GOVERNANCE_DIR_NAME)
        self.assertEqual(len(items), 1)
        self.assertIn("飞书推送还没开通", items[0]["title"])
        self.assertFalse(items[0]["actionable"])
        self.env.approve()
        self.assertEqual(governance_records(self.env.state / od.GOVERNANCE_DIR_NAME), [])


class FakeChild:
    def __init__(self, code=None):
        self.code = code
        self.pid = 4242

    def poll(self):
        return self.code


class LaneTests(HermeticCase):
    def launcher(self, clock):
        from dalton_core.mission_owner_delivery_lane import OwnerDeliveryLauncher

        self.spawned = []

        def popen(argv, **kwargs):
            self.spawned.append((argv, kwargs))
            return FakeChild()

        return OwnerDeliveryLauncher(state_dir=self.env.state, clock=clock, popen=popen,
                                     python="/usr/bin/python3")

    def test_no_approval_no_child(self) -> None:
        od.write_contract(self.env.state, od.build_contract_record())
        launcher = self.launcher(lambda: T0)
        result = launcher.dispatch_once()
        self.assertEqual(result["status"], "not_granted")
        self.assertEqual(self.spawned, [])

    def test_a_pass_is_spawned_when_due_and_never_two_at_once(self) -> None:
        self.env.approve()
        now = [T0]
        launcher = self.launcher(lambda: now[0])
        with patch("dalton_core.owner_delivery.load_settings",
                   return_value=dict(od.DEFAULT_SETTINGS)):
            first = launcher.dispatch_once()
            self.assertEqual(first["status"], "dispatched")
            argv, kwargs = self.spawned[0]
            self.assertEqual(argv[1:4], ["-m", "dalton_core.owner_delivery", "run"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(launcher.dispatch_once()["status"], "running")
            launcher._child.code = 0
            now[0] = T0 + timedelta(minutes=1)
            self.assertEqual(launcher.dispatch_once()["status"], "idle")
            # The fallback seam wakes it at once.
            od.request_wake("model_fallback_notice")
            woke = launcher.dispatch_once()
            self.assertEqual((woke["status"], woke["why"]), ("dispatched", "fallback_notice"))
            launcher._child.code = 0
            launcher.dispatch_once()
            # The owner's lever wakes it at once.
            self.env.lever(1, "emergency_stop", T0 + timedelta(minutes=2))
            launcher._child = None
            woke = launcher.dispatch_once()
            self.assertEqual(woke["why"], "owner_lever")
            launcher._child = None
            now[0] = T0 + timedelta(minutes=30)
            self.assertEqual(launcher.dispatch_once()["why"], "poll_interval")

    def test_the_fallback_seam_only_wakes(self) -> None:
        from dalton_core.model_selection import notice_delivery

        before = od.wake_counter()
        self.assertIsNone(notice_delivery({"id": "n:1", "message": "x"}))
        self.assertEqual(od.wake_counter(), before + 1)

    def test_an_emergency_stop_does_not_silence_delivery_but_a_lane_pause_does(self) -> None:
        from dalton_core.owner_levers import lane_refusal

        stop = {"lever": "emergency_stop", "id": "r1", "reason": "x", "until": None}
        self.assertIsNone(lane_refusal({"emergency": stop, "lanes": {}},
                                       "dispatch_owner_delivery"))
        self.assertIsNotNone(lane_refusal({"emergency": stop, "lanes": {}},
                                          "dispatch_catalog_sync"))
        pause = {"lever": "pause", "id": "r2", "reason": "x", "until": None}
        self.assertIsNotNone(lane_refusal(
            {"emergency": None, "lanes": {"dispatch_owner_delivery": pause}},
            "dispatch_owner_delivery"))

    def test_the_lane_is_on_without_a_seeded_record_and_sends_nothing_unapproved(self) -> None:
        # Batch t: deploy.sh never seeds the contract, so gating the lane on
        # the file kept it off in every environment.  The lane is on wherever
        # the code runs; the approved contract is what lets it send.
        from dalton_core.lane_registry import LaunchAgentContext, lane_for_operation, load_lanes
        from dalton_core.mission_owner_delivery_lane import OwnerDeliveryLauncher, argv_fragment

        load_lanes()
        spec = lane_for_operation("dispatch_owner_delivery")
        self.assertIsNotNone(spec)
        context = LaunchAgentContext(state=self.env.state)
        self.assertFalse(od.contract_path(self.env.state).exists())
        self.assertEqual(argv_fragment(context),
                         ["--owner-delivery-state-dir", str(self.env.state)])
        spawned = []
        launcher = OwnerDeliveryLauncher(state_dir=self.env.state,
                                         popen=lambda *a, **k: spawned.append(a))
        result = launcher.dispatch_once()
        self.assertEqual(result["status"], "not_granted")
        self.assertIn("owner-delivery-enable.sh", result["reason"])
        self.assertEqual(spawned, [])

    def test_the_child_entry_point_runs_a_pass_and_writes_its_summary(self) -> None:
        self.env.approve()
        summary = self.env.state / "run" / "s.json"
        with patch("sys.stdout"):
            code = od.main(["run", "--state-dir", str(self.env.state),
                            "--summary-path", str(summary)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(summary.read_text())["status"], "idle")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
