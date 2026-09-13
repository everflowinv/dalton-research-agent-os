from __future__ import annotations

import subprocess
import unittest
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"


class CockpitLanguageTests(unittest.TestCase):
    def test_frontend_language_contract(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "check_cockpit_language.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_javascript_parses(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        javascript = text.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            ["node", "--check", "-"],
            input=javascript,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_unknown_backend_text_is_not_written_as_a_primary_error(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertNotIn("textContent=e.message", text)
        self.assertNotIn("node(\"div\",e.message", text)
        self.assertIn("technicalDetails(p.technical)", text)

    def test_pagination_keeps_legacy_response_compatible(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn("r.returned_count??r.items.length", text)
        self.assertIn("if(r.next_cursor!=null||claimPages.length)", text)

    def test_real_approval_snapshot_folds_layout_without_merging_actions(self) -> None:
        items = json.loads(
            (ROOT / "tests/fixtures/cockpit_approvals_49_routing.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(49, len(items))
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for item in items:
            key = (item["kind"], item["who"]) if item["kind"] == "gate_reopen" else (
                "item", item["ref"]
            )
            groups[key].append(item)
        self.assertEqual(11, len(groups))  # four folded gate groups + seven other cards
        routed = [
            (item["ref"], action["decision"], action["label"])
            for entries in groups.values()
            for item in entries
            for action in item["actions"]
        ]
        self.assertEqual(sum(len(x["actions"]) for x in items), len(routed))
        self.assertEqual(len(routed), len(set(routed)))
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('it.kind==="gate_reopen"', text)
        self.assertIn("group.entries.forEach(card=>d.append(card))", text)

    def test_dynamic_lane_snapshot_uses_research_language(self) -> None:
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS

        self.assertGreaterEqual(len(REGISTRY_LANE_LABELS), 41)
        snapshot = "\n".join(REGISTRY_LANE_LABELS.values())
        for stale in (
            "取三张报表", "记下公司下次开口的日子", "看街上预期什么",
            "算预测行", "市场在吵什么", "值得下注", "读 sales note",
        ):
            self.assertNotIn(stale, snapshot)
        for expected in (
            "获取完整财务三张表", "同步卖方一致预期",
            "核心假设敏感性分析", "公司深度投研档案",
        ):
            self.assertIn(expected, snapshot)

    def test_goal_and_steer_prompts_share_the_final_text_contract(self) -> None:
        from dalton_core.cockpit_plane import CockpitPlane
        from dalton_core.final_text_contract import final_text_instructions

        plane = object.__new__(CockpitPlane)
        mission = {
            "title": "测试目标", "objective": "核实事实", "research_questions": ["收入如何变化？"],
            "universe": [], "source_plan": [],
        }
        for prompt in (
            plane._goal_prompt("更新目标", mission, {}),
            plane._steer_prompt("增加成本分析", mission, {}),
        ):
            for rule in final_text_instructions():
                self.assertIn(rule, prompt)

    def test_pending_language_review_never_renders_product_sections(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        guard = 'if(p.publication_status==="pending_language_review")'
        self.assertIn(guard, text)
        guarded = text[text.index(guard):text.index('d.append(node("h3",title)', text.index(guard)) + 300]
        self.assertIn("display_reason", guarded)
        self.assertIn("return d", guarded)

    def test_reported_stale_cockpit_phrases_are_removed(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        for stale in (
            "质量评分 · ${q.rubric}", "条结果行", "格换成了实际数",
            "估计被取代但保留着", "这一版为什么存在", "每一池花了多少",
            "当时的判断，后来怎样", "给你的几句建议", "流水线在跑",
            "还没填上的来源缺口", "挂起 / 不再重试的工作",
            'rawNode("div",x.statement)',
        ):
            self.assertNotIn(stale, text)
        self.assertIn('versionLabel(version)', text)
        self.assertIn('technicalDetails({rubric:q.rubric})', text)

    def test_dynamic_planner_fields_are_mapped_before_composition(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('${displayText(q.subject)}：${displayText(q.question)}', text)
        self.assertIn('需要核实：${displayText(q.wants)}', text)
        self.assertIn('${displayText(d.subject)} · ${displayText(d.item)}', text)


if __name__ == "__main__":
    unittest.main()
