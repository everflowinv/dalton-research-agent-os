import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.cockpit_plane import (
    _latest_initial_screen_failures,
    _stage_readiness_labels,
)


class StageReadinessProjectionTests(unittest.TestCase):
    def projection(self, stage, status, ready):
        return _stage_readiness_labels({
            "stage": stage, "stage_status": status,
            "stage_status_label": {None: "还没开始", "gate_passed": "已通过",
                                   "gate_failed": "未通过，正在补"}.get(status, "进行中"),
            "source_base_ready": ready,
            "gaps": [] if ready else ["annual_report"], "blocked_on": [],
        })

    def test_passed_initial_screen_with_current_gaps_keeps_both_truths(self):
        value = self.projection("initial_screen", "gate_passed", False)
        self.assertEqual(value["journey_status"], "历史检查已通过 · 当前资料待补齐")
        self.assertEqual(value["gate_decision"]["status"], "gate_passed")
        self.assertEqual(value["source_readiness"]["gaps"], ["annual_report"])

    def test_passed_initial_screen_with_ready_sources_is_explicit(self):
        value = self.projection("initial_screen", "gate_passed", True)
        self.assertEqual(value["journey_status"], "历史检查已通过 · 当前资料已齐")
        self.assertTrue(value["source_readiness"]["ready"])

    def test_failed_gate_is_not_rewritten_by_current_readiness(self):
        value = self.projection("initial_screen", "gate_failed", True)
        self.assertEqual(value["journey_status"], "历史检查未通过 · 当前资料已齐")
        self.assertEqual(value["gate_decision"]["status"], "gate_failed")

    def test_not_entered_is_not_presented_as_a_gate_result(self):
        value = self.projection(None, None, False)
        self.assertEqual(value["journey_status"], "尚无审批记录 · 当前资料待补齐")
        self.assertIsNone(value["gate_decision"]["status"])

    def test_later_stage_does_not_imply_initial_screen_gaps_invalidate_it(self):
        value = self.projection("company_model", "gate_passed", False)
        self.assertEqual(value["journey_status"], "历史检查已通过")
        self.assertEqual(value["source_readiness"]["label"], "当前资料待补齐")

    def test_latest_generation_failure_is_separate_from_historical_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "initial-screens" / "run-1"
            run.mkdir(parents=True)
            (run / "ticket.json").write_text(json.dumps({
                "status": "failed", "completed_at": "2026-09-14T15:57:48Z",
            }))
            (run / "summary.json").write_text(json.dumps({
                "status": "failed",
                "drafted": {"company_ref": "company:ctsh"},
                "failure_reason": "没有任何一节写出来，不发布空壳",
                "sections": [{"status": "model_unavailable",
                              "reason": "no model route is available right now"}],
            }))
            failures = _latest_initial_screen_failures(Path(tmp))
        self.assertEqual(
            failures["company:ctsh"],
            "最新一次报告生成失败：模型服务或当时可用运行额度未能承接请求",
        )
        html = (Path(__file__).resolve().parents[1] / "src" / "dalton_core"
                / "cockpit_control.html").read_text(encoding="utf-8")
        self.assertIn("c.latest_generation_failure", html)

    @staticmethod
    def write_run(root, name, *, status, at, company="company:epam", reason=None):
        run = Path(root) / "initial-screens" / name
        run.mkdir(parents=True)
        (run / "ticket.json").write_text(json.dumps({"status": status, "completed_at": at}))
        summary = {"status": status, "drafted": {"company_ref": company}, "sections": []}
        if reason is not None:
            summary["failure_reason"] = reason
        (run / "summary.json").write_text(json.dumps(summary))

    def test_later_success_clears_an_older_failure(self):
        # P0-1: live EPAM failed at 10:13 and succeeded at 10:28 and on 09-14;
        # the card kept saying the latest generation failed.
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, "a", status="failed", at="2026-09-09T10:13:10+00:00",
                           reason="MissionDeliverableValidationError: number.text must be "
                                  "text of 1..200 characters")
            self.write_run(tmp, "b", status="succeeded", at="2026-09-09T10:28:56+00:00")
            self.write_run(tmp, "c", status="succeeded", at="2026-09-14T19:18:00+00:00")
            failures = _latest_initial_screen_failures(Path(tmp))
        self.assertNotIn("company:epam", failures)
        self.assertEqual(failures, {})

    def test_failure_after_success_is_kept_and_translated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, "a", status="succeeded", at="2026-09-09T10:28:56+00:00")
            self.write_run(tmp, "b", status="failed", at="2026-09-14T19:18:00+00:00",
                           reason="MissionDeliverableValidationError: number.text must be "
                                  "text of 1..200 characters")
            self.write_run(tmp, "c", status="succeeded", at="2026-09-14T19:18:00+00:00",
                           company="company:acn")
            failures = _latest_initial_screen_failures(Path(tmp))
        label = failures["company:epam"]
        self.assertTrue(label.startswith("最新一次报告生成失败："))
        self.assertNotIn("MissionDeliverableValidationError", label)
        self.assertNotIn("number.text", label)
        self.assertIn("格式校验", label)
        self.assertNotIn("company:acn", failures)

    def test_chinese_failure_reason_is_shown_as_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, "a", status="failed", at="2026-09-14T11:52:07+00:00",
                           reason="没有任何一节写出来，不发布空壳")
            failures = _latest_initial_screen_failures(Path(tmp))
        self.assertEqual(failures["company:epam"], "最新一次报告生成失败：没有任何一节写出来，不发布空壳")

    def test_an_idle_ticket_without_a_company_does_not_clear_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, "a", status="failed", at="2026-09-14T11:52:07+00:00",
                           reason="没有任何一节写出来，不发布空壳")
            run = Path(tmp) / "initial-screens" / "idle"
            run.mkdir()
            (run / "ticket.json").write_text(json.dumps(
                {"status": "succeeded", "completed_at": "2026-09-30T00:00:00+00:00"}))
            (run / "summary.json").write_text(json.dumps({"status": "idle", "drafted": None}))
            failures = _latest_initial_screen_failures(Path(tmp))
        self.assertIn("company:epam", failures)


if __name__ == "__main__":
    unittest.main()
