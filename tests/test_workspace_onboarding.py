from __future__ import annotations
import tempfile, unittest
from pathlib import Path
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_onboarding import REQUIRED_FIELDS, WorkspaceOnboardingError, initial_goal_draft, validate_initial_goal_draft

class WorkspaceOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); release = root / "release"; release.mkdir()
        self.workspace = create_workspace_manifest(root / "fleet", "blank", 18794,
            "release:sha256:" + "a" * 64, release, shared_readonly_paths=[release])

    def test_draft_is_workspace_bound_and_grants_nothing(self):
        draft = initial_goal_draft(self.workspace, title="  AI pricing power ", objective=" Understand the change. ")
        self.assertEqual(draft["title"], "AI pricing power"); self.assertEqual(draft["companies"], [])
        self.assertIsNone(draft["budget"]); self.assertEqual(draft["setup_state"], "setup_required")
        self.assertEqual(draft["required_fields"], list(REQUIRED_FIELDS)); self.assertFalse(draft["mission_published"])
        self.assertFalse(draft["research_authorized"]); self.assertEqual(validate_initial_goal_draft(draft, self.workspace), draft)

    def test_rejects_cross_workspace_tampering_and_activation_claims(self):
        draft = initial_goal_draft(self.workspace, title="T", objective="O")
        release = Path(self.temp.name) / "release"
        other = create_workspace_manifest(Path(self.temp.name) / "fleet", "other", 18795,
            "release:sha256:" + "a" * 64, release, shared_readonly_paths=[release])
        with self.assertRaisesRegex(WorkspaceOnboardingError, "another workspace"):
            validate_initial_goal_draft(draft, other)
        for field, value in (("companies", [{"company_ref": "company:x"}]), ("budget", {}),
                             ("mission_published", True), ("research_authorized", True)):
            with self.assertRaises(WorkspaceOnboardingError):
                validate_initial_goal_draft({**draft, field: value}, self.workspace)

    def test_requires_user_supplied_title_and_objective(self):
        for title, objective in (("", "O"), ("T", "  ")):
            with self.assertRaises(WorkspaceOnboardingError):
                initial_goal_draft(self.workspace, title=title, objective=objective)

if __name__ == "__main__": unittest.main()
