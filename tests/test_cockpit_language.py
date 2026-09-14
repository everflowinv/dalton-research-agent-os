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

    def test_long_approval_evidence_is_progressively_disclosed(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('node("details",null,"memo-review")', text)
        self.assertIn('node("summary","展开投资备忘录核验依据")', text)
        self.assertIn('node("summary","展开完整复盘依据")', text)
        self.assertIn('d.append(review)', text)

    def test_dynamic_lane_snapshot_uses_research_language(self) -> None:
        from dalton_core.cockpit_plane import (
            CHANGE_REASON_LABELS, CHECKPOINT_TITLES, PLAN_ACTION_LABELS,
            REGISTRY_LANE_LABELS,
        )
        from dalton_core.model_selection import PURPOSE_LABELS

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
        visible_metadata = "\n".join((
            *PURPOSE_LABELS.values(), *PLAN_ACTION_LABELS.values(),
            *CHANGE_REASON_LABELS.values(), *CHECKPOINT_TITLES.values(),
        ))
        for stale in ("给产出打分", "整理市场在吵什么", "值得下注", "去抓数字",
                      "证据变厚了", "人改的", "过掉的闸"):
            self.assertNotIn(stale, visible_metadata)
        for expected in ("评估研究产出质量", "梳理市场争议", "形成投资判断",
                         "提取财务数字", "支撑论据已补充更新", "人工审阅后修订"):
            self.assertIn(expected, visible_metadata)

    def test_approval_business_details_are_visible_and_raw_technical_values_remain_expandable(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('if(label&&typeof v!=="object")', text)
        self.assertIn('if(looksTechnical(String(v)))technical[k]=v', text)
        self.assertIn('technicalDetails({kind:it.kind,ref:it.ref,hash:it.hash,details:technical})', text)

    def test_owner_erratum_uses_its_exact_correction_as_the_visible_summary(self) -> None:
        from dalton_core.cockpit_plane import _gate_reopen_view
        summary, details = _gate_reopen_view({
            "change_reason": "human_revision",
            "policy_ref": "owner-directed-factual-erratum:0.1",
            "erratum": {"substitutions": [{"before": "2025Q2", "after": "2026Q2"}]},
        }, "")
        self.assertEqual(summary, "根据已核验来源纠正事实：2025Q2 → 2026Q2")
        self.assertEqual(details["修订原因"], "根据已核验来源纠正事实")

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

    def test_deliverable_reader_hides_unreviewed_prose_and_uses_plain_gate_copy(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('if(doc.publication_status==="pending_language_review")', text)
        self.assertIn('doc.display_reason||"正文正在进行语言检查', text)
        self.assertIn('(s.display_gaps||s.gaps||[]).forEach', text)
        self.assertIn('finalResearchText(doc.summary)', text)
        self.assertIn('"公司最新阶段评审：通过"', text)
        self.assertNotIn('"出口门：通过"', text)

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

    def test_model_and_status_copy_uses_reviewed_business_terms(self) -> None:
        from dalton_core.cockpit_plane import (
            CHECKPOINT_ACTIONS,
            MODEL_SELECTION_MODE_LABELS,
            MODEL_TIER_LABELS,
        )

        text = HTML.read_text(encoding="utf-8")
        self.assertEqual("手动指定模型", MODEL_SELECTION_MODE_LABELS["explicit"])
        self.assertEqual("高阶推理与规划", MODEL_TIER_LABELS["brain"])
        self.assertEqual("暂不处理", CHECKPOINT_ACTIONS["thesis_revision_candidate"][2]["label"])
        for stale in ("还没装上", "等你批准", "还没跑过", "网关上有什么", "写进 broker"):
            self.assertNotIn(stale, text)
        for expected in ("尚未配置", "等待批准", "尚未运行", "模型网关目录"):
            self.assertIn(expected, text)

    def test_unsubmitted_actions_avoid_internal_or_false_promising_copy(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        for expected in (
            "允许使用", "已允许使用", "模型信息已保存并生效",
            "多数修改会在下一次调用生效", "历史修订申请",
            "本次没有形成可发布的调整",
        ):
            self.assertIn(expected, text)
        for stale in (
            'node("button","放行"', 'out.reload_instruction||"已放行"',
            "元数据已发布并生效", "可热加载的环节",
            "我会记下来作为下一步开发", "历史重开事项",
        ):
            self.assertNotIn(stale, text)

    def test_approval_and_model_actions_avoid_internal_language(self) -> None:
        frontend = HTML.read_text(encoding="utf-8")
        backend = Path(__import__("dalton_core.cockpit_plane", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        self.assertIn("正在保存模型选择…", frontend)
        for expected in ("投资备忘录：是否批准进入持续覆盖",
                         "等待正式研究审批流程接入；本页暂不能提交决定",
                         "暂缓决定论点修订", "重新出具初步筛查报告"):
            self.assertIn(expected, backend)
        self.assertNotIn('+ f"：{ref', backend)
        for stale in ("正在发布新的模型选择", "memo verification contract failed",
                      "Investment Memo：", "写者操作 decide_conviction_call",
                      "把论点修订放了放", "重出 Initial Screen"):
            self.assertNotIn(stale, frontend + backend)

    def test_legacy_log_rows_hide_refs_and_machine_errors(self) -> None:
        from dalton_core.cockpit_plane import CockpitPlane
        plane = object.__new__(CockpitPlane)
        approval = plane._journal_event_view({
            "kind": "approval", "title": "批准了投资备忘录：memo-version:raw",
            "detail": "同意", "refs_json": '{"decision":"approve","ref":"memo-version:raw"}',
        })
        self.assertEqual(approval["title"], "批准了研究决定")
        self.assertNotIn("raw", approval["title"] + approval["detail"])
        self.assertEqual(approval["technical"]["refs"]["ref"], "memo-version:raw")
        budget = plane._journal_event_view({
            "kind": "model_budget", "title": "已调整「research_localization」的调用预算",
            "detail": '{"max_cost_usd":1}',
            "refs_json": '{"purpose":"research_localization","revision":"raw-revision"}',
        })
        self.assertNotIn("research_localization", budget["title"] + budget["detail"])
        self.assertEqual(budget["technical"]["purpose"], "research_localization")
        frontend = HTML.read_text(encoding="utf-8")
        self.assertIn("technicalDetails(e.technical)", frontend)
        self.assertIn('_runtime_error_display(raw_error)', Path(__import__("dalton_core.cockpit_plane", fromlist=["x"]).__file__).read_text())

    def test_model_action_journal_uses_readable_copy(self) -> None:
        from dalton_core import cockpit_plane

        source = Path(cockpit_plane.__file__).read_text(encoding="utf-8")
        for expected in (
            "你已允许使用一个模型", "模型信息必须是一个对象",
            "模型信息没有保存", "你补充了模型信息",
            "self._model_family_label(family)",
            "self._model_capability_labels(capabilities)",
        ):
            self.assertIn(expected, source)
        for stale in ("模型元数据必须", "模型元数据没有发布", "你声明了 {profile_id}"):
            self.assertNotIn(stale, source)

    def test_dynamic_planner_fields_are_mapped_before_composition(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('${displayText(q.subject)}：${displayText(q.question)}', text)
        self.assertIn('需要核实：${displayText(q.wants)}', text)
        self.assertIn('${displayText(d.subject)} · ${displayText(d.item)}', text)

    def test_event_and_cadence_prose_is_mapped_before_composition(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('node("span",x.summary||"—")', text)
        self.assertNotIn('`${x.kind_label}：${x.summary||"—"}`', text)
        self.assertIn('node("span",r.because,"hint")', text)
        self.assertNotIn('` ${r.because}`', text)

    def test_approval_text_refreshes_when_overview_mapping_arrives(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('const fingerprint=JSON.stringify([it,UI_TEXT_REVISION])', text)
        self.assertIn('if(approvalTextChanged&&approvalCards.size)loadApprovals()', text)
        self.assertIn('existing.fingerprint===fingerprint||existing.pending', text)
        self.assertIn('node("span",displayText(String(v)))', text)
        self.assertIn('finalResearchText(it.summary):displayText(it.summary),summary=approvalSummary(localized)', text)

    def test_final_research_prose_waits_for_exact_reviewed_text(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('FINAL_RESEARCH_REQUIRED&&!Object.prototype.hasOwnProperty.call(UI_TEXT,value)', text)
        self.assertIn('正文正在检查文字表达，完成后会显示。', text)
        for field in (
            'finalResearchText(x.because)',
            'finalResearchText(r.what_we_expected)', 'finalResearchText(r.what_happened)',
        ):
            self.assertIn(field, text)
        self.assertIn('const finalFields=[r.title,r.prose', text)
        self.assertIn('if(!finalResearchReady(finalFields))', text)
        self.assertIn('technicalDetails({original_reflection:r})', text)
        self.assertIn('const answer=r.display_answer?displayText(r.display_answer):finalResearchText(r.answer)', text)
        self.assertIn('technicalDetails({original_answer:r.answer,original_gaps:r.gaps})', text)
        self.assertIn('fields=[z.title,...(z.sections||[]).flatMap', text)
        self.assertIn('technicalDetails({original_review:z})', text)

    def test_missing_model_items_are_rendered_as_readable_lists(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('readinessGroup("当前版本暂无结果的项目"', text)
        self.assertIn('labels.forEach(label=>list.append(node("li",displayText(label))))', text)
        self.assertNotIn('当前版本暂无结果的项目：${(r.results_unavailable_labels||[]).join', text)


if __name__ == "__main__":
    unittest.main()
