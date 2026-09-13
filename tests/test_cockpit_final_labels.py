from pathlib import Path
import unittest

from dalton_core.cockpit_plane import CHECKPOINT_ACTIONS, MODEL_SELECTION_MODE_LABELS


class CockpitFinalLabelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parents[1] / "src" / "dalton_core" /
                    "cockpit_control.html").read_text("utf-8")

    def test_authority_values_keep_clear_user_labels(self):
        self.assertEqual(MODEL_SELECTION_MODE_LABELS["tier"], "使用系统推荐配置")
        actions = {row["decision"]: row["label"]
                   for row in CHECKPOINT_ACTIONS["gate_reopen"]}
        self.assertEqual(actions, {
            "approve": "批准修订", "decline": "保留当前版本"})

    def test_direction_copy_only_promises_question_and_goal_changes(self):
        self.assertIn("整理为研究问题和目标的调整方案", self.html)
        self.assertIn('id="steer-submit">生成调整方案</button>', self.html)
        self.assertNotIn("翻译成具体改动", self.html)

    def test_log_filter_labels_change_without_changing_filter_values(self):
        expected = {
            "all": "全部动态", "discoveries": "资料检索",
            "fetches,acquisitions,sec-lane-runs": "资料采集",
            "extractions,review": "阅读与提取", "claim": "新增结论",
            "question,goal,steer,approval": "操作记录", "problem": "运行问题",
        }
        for value, label in expected.items():
            self.assertIn(f'data-f="{value}">{label}</span>', self.html)

    def test_claim_index_empty_state_uses_product_language(self):
        self.assertIn("结论索引尚未建立，所以没有主题和来源层级。", self.html)
        self.assertNotIn("这个 Core 还没有给结论建索引", self.html)


if __name__ == "__main__":
    unittest.main()
