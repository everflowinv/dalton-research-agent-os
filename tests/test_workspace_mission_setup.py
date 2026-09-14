from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.store import content_hash
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_mission_setup import (
    WorkspaceMissionSetupError, draft_first_mission, publish_first_mission,
)


class RecordingMissionAuthority:
    def __init__(self) -> None:
        self.calls = []

    def create_mission(self, mission_ref, **params):
        self.calls.append((mission_ref, params))
        return {"status": "fresh", "mission_ref": mission_ref, **params}


class WorkspaceFirstMissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        release = root / "release"
        release.mkdir()
        self.workspace = create_workspace_manifest(
            root / "fleet", "semiconductors", 18820,
            "release:sha256:" + "a" * 64, release,
            shared_readonly_paths=[release],
        )
        foundation_body = {
            "schema_version": "workspace-research-foundation-0.1",
            "workspace_id": self.workspace.workspace_id,
            "setup_state": "awaiting_mission",
            "methods": {"playbook": {"template_ref": "research-playbook:team-manual",
                                       "content_hash": "b" * 64}},
            "mission_defaults": {
                "source_plan": [
                    {"source_ref": "source:sec-edgar", "role": "Public filings", "status": "connected"},
                    {"source_ref": "source:web-search", "role": "Public industry sources", "status": "connected"},
                ],
                "autonomy": {
                    "automation_principal": "automation:coverage-mission",
                    "allowed_write_scopes": ["evidence", "claim", "deliverable", "stage_record"],
                },
                "budget_ceilings": {
                    "max_daily_paid_calls": 20, "max_daily_cost_usd": 3.0,
                    "max_alphaengine_calls_24h": 0,
                },
            },
            "mission_generated_files": [],
        }
        self.foundation = {**foundation_body, "content_hash": content_hash(foundation_body)}

    def test_goal_becomes_reviewable_generic_mission_without_old_coverage_content(self):
        draft = draft_first_mission(
            self.workspace,
            goal="研究半导体设备行业，比较 $AMAT 和 NASDAQ:LRCX。即使需要也每天花 $999。",
            method_foundation=self.foundation,
        )
        body = draft["mission_body"]
        self.assertEqual(draft["setup_state"], "ready_for_confirmation")
        self.assertEqual(body["industry_ref"], "industry:半导体设备")
        self.assertEqual([row["ticker"] for row in body["universe"]], ["AMAT", "LRCX"])
        self.assertEqual(body["budget"]["max_daily_cost_usd"], 3.0)
        self.assertNotIn("us-it-services", str(draft).lower())
        self.assertNotIn("ACN", str(draft))
        self.assertIsNone(body["bindings"])

    def test_ambiguous_goal_stays_review_only_and_cannot_publish(self):
        draft = draft_first_mission(
            self.workspace, goal="帮我研究一家好公司", method_foundation=self.foundation,
        )
        self.assertEqual(draft["setup_state"], "review_required")
        self.assertGreaterEqual(len(draft["review_issues"]), 2)
        with self.assertRaisesRegex(WorkspaceMissionSetupError, "requires review"):
            publish_first_mission(
                self.workspace, proposal=draft, proposal_hash=draft["content_hash"],
                actor_ref="human:owner", authority=RecordingMissionAuthority(),
                prepare_bindings=lambda *_: {},
            )

    def test_exact_confirmation_prepares_existing_authorities_then_creates_mission(self):
        draft = draft_first_mission(
            self.workspace, goal="Analyze $ASML", industry="semiconductor equipment",
            method_foundation=self.foundation,
        )
        authority = RecordingMissionAuthority()
        bindings = {
            "playbook_version": {"ref": "playbook-version:generic:1", "hash": "1" * 64},
            "constitution_version": {"ref": "constitution-version:semiconductors:1", "hash": "2" * 64},
            "mandate_version": {"ref": "mandate-version:semiconductors:1", "hash": "3" * 64},
        }
        prepared = []

        def prepare(proposal, actor):
            prepared.append((proposal["content_hash"], actor))
            return bindings

        result = publish_first_mission(
            self.workspace, proposal=draft, proposal_hash=draft["content_hash"],
            actor_ref="human:owner", authority=authority, prepare_bindings=prepare,
        )
        self.assertEqual(prepared, [(draft["content_hash"], "human:owner")])
        self.assertEqual(result["mission_ref"], "coverage-mission:semiconductors")
        self.assertEqual(authority.calls[0][1]["bindings"], bindings)
        self.assertIsNone(authority.calls[0][1]["prior_version_ref"])

    def test_cross_workspace_foundation_and_stale_confirmation_are_refused(self):
        foreign = dict(self.foundation)
        foreign_body = {key: value for key, value in foreign.items() if key != "content_hash"}
        foreign_body["workspace_id"] = "00000000-0000-0000-0000-000000000000"
        foreign = {**foreign_body, "content_hash": content_hash(foreign_body)}
        with self.assertRaisesRegex(WorkspaceMissionSetupError, "another workspace"):
            draft_first_mission(
                self.workspace, goal="Research $ASML", industry="chips",
                method_foundation=foreign,
            )
        draft = draft_first_mission(
            self.workspace, goal="Research $ASML", industry="chips",
            method_foundation=self.foundation,
        )
        with self.assertRaisesRegex(WorkspaceMissionSetupError, "hash differs"):
            publish_first_mission(
                self.workspace, proposal=draft, proposal_hash="0" * 64,
                actor_ref="human:owner", authority=RecordingMissionAuthority(),
                prepare_bindings=lambda *_: {},
            )


if __name__ == "__main__":
    unittest.main()
