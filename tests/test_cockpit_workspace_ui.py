from pathlib import Path
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
            "已连接的模型和资料来源可在新环境中继续使用",
            "研究目标、公司、任务、资料和审批只属于各自的环境",
            "这是一个空白研究环境",
        ):
            self.assertIn(phrase, self.source)
        self.assertNotIn("复制研究内容", self.source)

    def test_blank_workspace_keeps_first_goal_form_visible(self):
        self.assertIn('id="goal-create-card"', self.source)
        self.assertIn(
            '#view-goal > :not(#goal-hero):not(#goal-create-card)',
            self.source,
        )
        self.assertIn("空白研究环境 · 等待研究目标", self.source)

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
