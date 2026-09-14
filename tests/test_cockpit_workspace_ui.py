from pathlib import Path
import json
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"


class CockpitWorkspaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_workspace_has_a_first_class_navigation_and_blank_creation_flow(self):
        self.assertIn('data-view="workspace"', self.source)
        self.assertIn('id="view-workspace"', self.source)
        self.assertIn('id="workspace-input"', self.source)
        self.assertIn('id="workspace-create"', self.source)
        self.assertIn("新建空白研究环境", self.source)
        self.assertIn(
            'postJson("/v1/cockpit/workspace_create",{name,request_id:workspaceCreateId},{timeoutMs:180000})',
            self.source,
        )

    def test_workspace_explains_shared_connections_and_isolated_research(self):
        for phrase in (
            "模型和资料来源的连接配置已登记；实际研究调用需在本环境中配置并确认",
            "研究目标、公司、任务、资料和审批只属于各自的环境",
            "这是一个空白研究环境",
        ):
            self.assertIn(phrase, self.source)
        self.assertNotIn("复制研究内容", self.source)

    def test_real_shared_connection_counts_are_visible(self):
        function = re.search(
            r"function workspaceConnectionText\([^\n]+\)\{.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(function)
        payload = {"models": 24, "sources": 9, "available": True}
        result = subprocess.run(
            ["node", "-e", function.group(0) +
             f"\nconsole.log(workspaceConnectionText({json.dumps(payload)}));"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(
            "已登记 24 个模型和 9 个资料来源的连接配置；实际研究调用需在本环境中配置并确认。",
            result.stdout.strip(),
        )

    def test_favicon_is_inline_and_needs_no_extra_request(self):
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', self.source)

    def test_blank_workspace_can_inspect_shared_models_and_sources(self):
        self.assertIn('id="workspace-open-models"', self.source)
        self.assertIn('id="workspace-open-sources"', self.source)
        self.assertIn('$("workspace-open-models").onclick=openModels', self.source)
        self.assertIn('$("workspace-open-sources").onclick=openSources', self.source)
        self.assertIn("连接配置已登记；研究调用需在本环境配置", self.source)
        self.assertIn('row.append(node("span",null,"st wait"))', self.source)
        shared_before_unavailable = self.source.index("const sharedModels=r.shared_catalog")
        unavailable = self.source.index("if(!r.available)", shared_before_unavailable)
        self.assertLess(shared_before_unavailable, unavailable)

    def test_blank_workspace_keeps_first_goal_form_visible(self):
        self.assertIn('id="goal-create-card"', self.source)
        self.assertIn(
            '#view-goal > :not(#goal-hero):not(#goal-create-card)',
            self.source,
        )
        self.assertIn("空白研究环境 · 等待研究目标", self.source)
        self.assertIn('$("goal-submit").textContent="保存目标草稿"', self.source)

    def test_initial_goal_is_saved_without_a_false_publish_action(self):
        marker = 'if(d.schema_version==="workspace-initial-goal-draft-0.1")'
        self.assertIn(marker, self.source)
        branch = self.source[self.source.index(marker):]
        branch = branch[:branch.index("return}")]
        for phrase in ("目标草稿已保存", "启动前还需设置", "研究公司", "资料范围", "研究预算", "研究规则", "保存草稿不会启动研究"):
            self.assertIn(phrase, branch)
        self.assertNotIn("draftActions", branch)

    def test_saved_initial_goal_returns_without_overwriting_user_edits(self):
        guard = (
            'o.initial_goal?.status==="saved"'
            '&&!$("goal-draft").childNodes.length'
            '&&!$("goal-input").value.trim()'
        )
        self.assertIn(guard, self.source)
        self.assertIn("renderGoalDraft(o.initial_goal)", self.source)
        self.assertIn("已恢复上次保存的目标草稿。", self.source)

    def test_creation_requires_a_name_and_only_enters_a_real_url(self):
        self.assertIn("if(!name){status.className=\"status err\"", self.source)
        self.assertIn("enter.disabled=item.current||!item.url", self.source)
        self.assertIn("if(workspace.url){status.replaceChildren", self.source)
        self.assertIn("研究环境已建立，正在准备进入入口", self.source)

    def test_request_id_is_reused_after_failure_and_reset_after_input_change(self):
        self.assertIn("if(!workspaceCreateId)workspaceCreateId=rid()", self.source)
        failure = self.source.index('catch(e){status.className="status err"', self.source.index('$("workspace-create").onclick'))
        reset = self.source.index("workspaceCreateId=null", self.source.index('$("workspace-create").onclick'))
        self.assertLess(reset, failure)
        self.assertIn("if(name!==workspaceLastName){workspaceCreateId=null", self.source)

    def test_javascript_parses(self):
        javascript = self.source.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            ["node", "--check", "-"], input=javascript, text=True,
            cwd=ROOT, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_workspace_display_name_is_used_in_the_cockpit_header(self):
        self.assertIn("workspace&&(workspace.name||workspace.slug)", self.source)


if __name__ == "__main__":
    unittest.main()
