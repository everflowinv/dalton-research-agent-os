"""2026-10-09: every reason a research task can be held with has a card, a
运行控制 entry and an honest set of doors.

Live legacy: the needs-human group card said 「基于已获取原文开展专题研究」
had 2 research tasks waiting, 管理员设置 → 运行控制 said none were, and no
per-task card existed -- the two EPAM holds carried
``fresh_work_recovery_exhausted``, a reason neither the card builder nor the
admin list knew.  These tests enumerate every reason the executor and the lane
can hold with (grep plus the exported list), and a reason nobody knows, and
assert the card builder and ``held_tasks`` handle each one.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from pathlib import Path

from dalton_core import mission_document_research_executor as executor
from dalton_core import mission_document_research_lane as lane
from dalton_core.cockpit_v2.admin.data import held_tasks
from dalton_core.needs_human import completion_entry, held_lanes, hold_reason_label
from dalton_core.terminology import banned_hits

ROOT = Path(__file__).resolve().parents[1] / "src" / "dalton_core"
ADMISSION = "mission-document-research-admission:"
LIVE_EPAM = {
    ADMISSION + "09b4bb825ae8ef44b3249de067ca4f76": {
        "admission_hash": "d65c0ca0dc31ef96a96390840b95831f67b1ab5d49ee0295f146654448a481c9",
        "disposition": "recovery_required", "reason": "fresh_work_recovery_exhausted",
        "retry_at": None, "ticket_ref": "mission-document-research:89de31311923c773e77b10f5"},
    ADMISSION + "3c49a49c8c4d70c73eff22b5a9da6cba": {
        "admission_hash": "8dfbb5cad1128806bbd35fa6cfca423d526dcbaac2e79bf9e6ead353ddac1f84",
        "disposition": "recovery_required", "reason": "fresh_work_recovery_exhausted",
        "retry_at": None, "ticket_ref": "mission-document-research:5c2f123876e0d2cb44490eea"},
    # A terminal hold is not waiting on anybody and gets no card.
    ADMISSION + "0746fe99e6f6156b0f3364678512ed98": {
        "admission_hash": "6657b968abe41f10636792f444eac5bd0f69ad9cb252935941ae078aa4399f67",
        "disposition": "terminal_hold", "reason": "withdrawn_by_owner",
        "retry_at": None, "ticket_ref": "mission-document-research:b7990a33924e73e0c32c7213"},
}


def _constant_values(module_path: Path) -> dict[str, str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _resolve(token: str, constants: dict[str, str]) -> str | None:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return constants.get(token)


def executor_stop_reasons() -> set[str]:
    """Every reason the executor writes on a ``stopped`` recovery, by grep."""

    path = ROOT / "mission_document_research_executor.py"
    text = path.read_text(encoding="utf-8")
    constants = _constant_values(path)
    found: set[str] = set()
    patterns = (
        r'state, reason = "stopped", ([A-Z_]+|"[a-z_]+")',
        r'"status": "stopped", "reason": ([A-Z_]+|"[a-z_]+")',
        r'answer\("stopped", ([A-Z_]+|"[a-z_]+")\)',
    )
    for pattern in patterns:
        for token in re.findall(pattern, text):
            value = _resolve(token, constants)
            if value is not None:
                found.add(value)
    # ``spent`` comes from the two dispositions and the owner-retry constant.
    for name in ("CONTRACT_FAILED_AFTER_OWNER_RETRY", "CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY",
                 "UNPROVED_SEND_FAILED_AFTER_AUTOMATIC_RETRY"):
        found.add(constants[name])
    # ``reason = (A if ... else B)`` for the deadline stop.
    for token in re.findall(r'^\s+(?:else )?(FRESH_WORK_RECOVERY_[A-Z_]+)$', text, re.M):
        found.add(constants[token])
    return found


def lane_hold_reasons() -> set[str]:
    """Every reason the lane holds an admission ``recovery_required`` with, by grep."""

    path = ROOT / "mission_document_research_lane.py"
    text = path.read_text(encoding="utf-8")
    constants = _constant_values(path)
    found = set()
    for literal in re.findall(r'reason="([a-z_]+)"', text):
        found.add(literal)
    for name in ("REENTRY_ESCALATED_REASON", "DISPATCH_ERROR_REASON", "HINT_DRIFT_HOLD_REASON"):
        found.add(constants[name])
    found.add("controlled_reentry_unavailable")
    found.add("budget_or_pool_refused")
    return found


def _ledger(directory: Path, holds: dict) -> Path:
    runs = directory / "mission-document-research-runs"
    runs.mkdir(parents=True, exist_ok=True)
    from dalton_core.mission_document_research_lane import _write_holds

    _write_holds(runs / "holds.json", holds)
    return runs / "holds.json"


def _hold(reason: str, digit: str) -> dict:
    return {"admission_hash": digit * 64, "ticket_ref": None, "reason": reason,
            "disposition": "recovery_required", "retry_at": None}


def _visible(item: dict) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "why_blocked", "action",
                                                          "consequence"))


class ReasonRegistryTests(unittest.TestCase):
    def test_every_executor_stop_reason_is_in_the_exported_list(self):
        found = executor_stop_reasons()
        self.assertIn("fresh_work_recovery_exhausted", found)
        self.assertIn("fresh_work_recovery_deadline_exceeded", found)
        self.assertIn("fresh_work_recovery_day_window_exceeded", found)
        missing = found - set(executor.STOPPED_REASONS)
        self.assertEqual(missing, set(), "executor stop reasons missing from STOPPED_REASONS")

    def test_every_lane_hold_reason_is_in_the_exported_list(self):
        found = lane_hold_reasons()
        missing = {reason for reason in found
                   if executor.hold_reason_code(reason) not in executor.HOLD_REASONS}
        # ``terminal_hold`` reasons (``failed``, a child's own status) are not
        # waiting on a person; only recovery_required ones matter here.
        self.assertEqual(missing - {"failed"}, set())

    def test_every_reason_has_registry_words(self):
        from dalton_core.terminology import LABELS

        for reason in executor.HOLD_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, LABELS["hold_reason"])
                self.assertNotIn("其他原因", hold_reason_label(reason))
        self.assertEqual(hold_reason_label("some_new_reason:detail"), "其他原因：some_new_reason")

    def test_available_doors_always_offer_withdrawal(self):
        for reason in (*executor.HOLD_REASONS, "some_new_reason", ""):
            with self.subTest(reason=reason):
                doors = [door["door"] for door in executor.available_doors({"reason": reason})]
                self.assertIn("withdraw", doors)
                self.assertEqual(doors.count("withdraw"), 1)
        unknown = [door["door"] for door in executor.available_doors({"reason": "x_y"})]
        self.assertEqual(unknown, ["withdraw", "technical_help"])
        policy_cap = executor.available_doors({"reason": "fresh_work_recovery_exhausted"})
        self.assertEqual([door["door"] for door in policy_cap], ["retry_round", "withdraw"])
        self.assertEqual(policy_cap[0]["cost"], "within_budget")
        self.assertEqual(policy_cap[0]["operation"], "authorize_mission_document_reentry")
        waiting = executor.available_doors({
            "reason": "fresh_work_recovery_exhausted:all_models_unavailable:"
                      "awaiting_model_availability"})
        self.assertEqual([door["door"] for door in waiting], ["withdraw"])
        self.assertTrue(waiting[0]["automatic_retry"])


class EveryReasonHasACardTests(unittest.TestCase):
    REASONS = (
        *executor.HOLD_REASONS,
        "unproved_send_failed_after_automatic_retry:INVALID_HOST_RESULT:empty_or_invalid_reply",
        "provider_budget_exceeded_after_corrected_retry:input=70000>64000:need=88000",
        "reentry_failed_after_automatic_rebind:controlled_reentry_unavailable:ticket missing",
        "fresh_work_recovery_exhausted:route_rejected_definitely_not_sent",
        "fresh_work_recovery_exhausted:all_models_unavailable:awaiting_model_availability",
        "fresh_work_recovery_exhausted:all_models_unavailable:automatic_round_used_today:"
        "next=2026-10-10T00:00:00+00:00",
        "fresh_work_recovery_deadline_exceeded:atomic_day_budget_refusal",
        "dispatch_error:RuntimeError:boom",
        "a_reason_this_release_has_never_heard_of:with detail",
    )

    def test_one_card_and_one_admin_entry_per_held_task(self):
        holds = {}
        for index, reason in enumerate(self.REASONS):
            holds[ADMISSION + f"{index:032x}"] = _hold(reason, "%x" % (index % 16))
        with tempfile.TemporaryDirectory() as name:
            _ledger(Path(name), holds)
            items = held_lanes(None, state_dir=Path(name))
        per_task = [item for item in items if item["ref"].count(":") >= 2]
        group = [item for item in items if item["ref"] == "lane:mission_document_research"]
        self.assertEqual(len(per_task), len(self.REASONS))
        self.assertEqual(len(group), 1)
        # The group card counts exactly the cards it rendered.
        self.assertEqual(group[0]["detail"]["held"], len(per_task))
        self.assertIn(f"有 {len(per_task)} 项研究任务", group[0]["title"])
        self.assertEqual(group[0]["detail"]["unrendered"], [])
        listed = held_tasks({"items": items})
        self.assertEqual(len(listed), len(self.REASONS))
        for item, entry in zip(per_task, listed):
            reason = item["detail"]["reason"]
            with self.subTest(reason=reason):
                doors = [door["door"] for door in entry["doors"]]
                self.assertIn("withdraw", doors)
                self.assertTrue(entry["reason_label"])
                for door in entry["doors"]:
                    self.assertIn(door["cost"], {"free", "within_budget", "paid"})
                    self.assertTrue(door["cost_label"])
                self.assertEqual(banned_hits(_visible(item)), [], _visible(item))
                card_entry = completion_entry(item)
                self.assertEqual(card_entry["mode"], "admin")
                self.assertEqual([door["door"] for door in card_entry["actions"]], doors)
                if not item["detail"]["reason_label"].startswith("其他原因"):
                    continue
                self.assertIsNotNone(item["detail"]["handoff"])
        unknown = next(entry for entry in per_task
                       if entry["detail"]["reason"].startswith("a_reason_this_release"))
        self.assertEqual(unknown["detail"]["reason_label"],
                         "其他原因：a_reason_this_release_has_never_heard_of")
        self.assertIn("交技术协助", unknown["action"])

    def test_a_card_that_cannot_be_built_is_named_on_the_group_card(self):
        from unittest import mock

        holds = {ADMISSION + "1" * 32: _hold("fresh_work_recovery_exhausted", "1"),
                 ADMISSION + "2" * 32: _hold("contract_failed_after_automatic_retry", "2")}
        import dalton_core.needs_human as needs_human

        real = needs_human._escalated_hold_item

        def flaky(lane_name, hold, **kwargs):
            if hold["admission_ref"].endswith("1" * 32):
                raise RuntimeError("subject unreadable")
            return real(lane_name, hold, **kwargs)

        with tempfile.TemporaryDirectory() as name, \
                mock.patch.object(needs_human, "_escalated_hold_item", flaky):
            _ledger(Path(name), holds)
            items = held_lanes(None, state_dir=Path(name))
        group = items[-1]
        self.assertEqual(group["detail"]["held"], 1)
        self.assertIn("有 1 项研究任务", group["title"])
        self.assertIn("另有 1 项无法生成单独的卡片", group["why_blocked"])
        self.assertEqual(len(group["detail"]["unrendered"]), 1)


class LiveEpamShapeTests(unittest.TestCase):
    """The live legacy ledger of 2026-10-09, read by this release."""

    def test_both_epam_holds_get_a_card_a_door_and_an_admin_entry(self):
        with tempfile.TemporaryDirectory() as name:
            _ledger(Path(name), LIVE_EPAM)
            items = held_lanes(None, state_dir=Path(name))
        per_task = [item for item in items if item["ref"].count(":") >= 2]
        self.assertEqual(len(per_task), 2)
        group = items[-1]
        self.assertIn("有 2 项研究任务", group["title"])
        for item in per_task:
            self.assertIn("自动重试的次数已用完", item["why_blocked"])
            doors = [door["door"] for door in item["detail"]["owner_doors"]]
            self.assertEqual(doors, ["retry_round", "withdraw"])
            self.assertIn("在当前模型设置下再试一次", item["action"])
            self.assertEqual(banned_hits(_visible(item)), [])
        listed = held_tasks({"items": items})
        self.assertEqual([entry["admission_ref"] for entry in listed],
                         [item["detail"]["admission_ref"] for item in per_task])
        self.assertEqual(listed[0]["cost"], "within_budget")

    def test_after_the_lane_re_reads_them_they_wait_for_a_model_without_a_click(self):
        holds = {ref: {**value, "reason": (
            "fresh_work_recovery_exhausted:all_models_unavailable:awaiting_model_availability")}
            for ref, value in LIVE_EPAM.items() if value["disposition"] == "recovery_required"}
        with tempfile.TemporaryDirectory() as name:
            _ledger(Path(name), holds)
            items = held_lanes(None, state_dir=Path(name))
        per_task = [item for item in items if item["ref"].count(":") >= 2]
        for item in per_task:
            self.assertTrue(item["title"].startswith("系统将自动重试"))
            self.assertIn("不需要你操作", item["action"])
            self.assertEqual([door["door"] for door in item["detail"]["owner_doors"]],
                             ["withdraw"])
        group = items[-1]
        self.assertEqual(len(per_task), 2)
        self.assertEqual(group["detail"]["automatic_retry"], 2)
        self.assertFalse(group["actionable"])
        self.assertEqual(group["responsibility"], "background")
        self.assertEqual(group["status"], "recovery_summary")
        self.assertIn("系统继续处理", group["why_blocked"])
        self.assertNotIn("等你决定", group["why_blocked"])



class RetryRoundDoorTests(unittest.TestCase):
    def test_the_cockpit_door_is_the_reentry_writer_operation_with_its_own_words(self):
        from unittest import mock

        from dalton_core.cockpit_owner_doors import DOOR_LABELS, owner_door

        admission = ADMISSION + "09b4bb825ae8ef44b3249de067ca4f76"
        plane = mock.Mock()
        plane._governance.return_value = {"status": "granted", "kind": "recovery_round"}
        out = owner_door(plane, "owner@example", {"door": "retry_round", "admission_ref": admission,
                                                  "reason": "模型已恢复"})
        plane._governance.assert_called_with(
            "owner@example", "authorize_mission_document_reentry", {"admission_ref": admission},
            failure=mock.ANY)
        self.assertEqual(DOOR_LABELS["retry_round"], "在当前模型设置下再试一次")
        self.assertIn("再安排一轮重试", out["message"])
        self.assertEqual(banned_hits(out["message"]), [])
        refs = plane.journal.record_event.call_args.kwargs["refs"]
        self.assertEqual(refs["door"], "retry_round")


if __name__ == "__main__":
    unittest.main()
