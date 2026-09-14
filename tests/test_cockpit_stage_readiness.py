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


if __name__ == "__main__":
    unittest.main()
