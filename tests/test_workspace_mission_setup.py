from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from dalton_core.store import content_hash
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_mission_setup import (
    WorkspaceMissionSetupError, draft_first_mission, plan_first_mission_goal,
    materialize_first_mission_discovery_plans, publish_first_mission,
    publish_first_mission_to_store,
    resolve_sec_ticker,
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
            "setup_planning_budget": {
                "max_model_calls": 6, "max_input_tokens": 120000,
                "max_output_tokens": 24000, "max_cost_usd": 10.0,
            },
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

    def test_model_planner_has_a_pre_mission_budget_context_and_structured_contract(self):
        class Model:
            def __init__(self): self.calls = []
            def call_setup(inner, **kwargs):
                inner.calls.append(kwargs)
                return {"text": '{"summary":"s","title":"Chip tools",'
                        '"objective":"Compare tool vendors",'
                        '"industry":{"name":"semiconductor equipment","reason":"named"},'
                        '"research_questions":["Who gains share?"],"subtasks":["filings"],'
                        '"suggested_companies":[{"ticker":"ASML","name":"ASML","reason":"named"}]}' }
        model = Model()
        draft = plan_first_mission_goal(
            model, self.workspace, goal="Compare ASML", method_foundation=self.foundation,
            request_id="goal-1", created_at="2026-09-14T12:00:00+00:00")
        self.assertEqual(draft["mission_body"]["universe"][0]["ticker"], "ASML")
        context = model.calls[0]["planning_context"]
        self.assertEqual(context["budget"]["max_daily_paid_calls"], 6)
        self.assertEqual(context["foundation_hash"], self.foundation["content_hash"])
        self.assertNotIn("mission", context)

    def test_production_preparer_publishes_real_authority_chain_and_active_mission(self):
        from dalton_core.store import DaltonStore
        from tests.p9a_fixtures import bootstrap_method_authorities, constitution_method

        db = Path(self.temp.name) / "core.sqlite"
        store = DaltonStore(str(db)); self.addCleanup(store.close)
        seeded = bootstrap_method_authorities(store)
        driver_value = {
            "drivers": [{"driver_ref": "driver:volume", "label": "Volume",
                         "mechanism": "Customer demand changes units sold.",
                         "metric_refs": ["metric:revenue"]}],
            "metric_specs": [{"metric_ref": "metric:revenue", "label": "Revenue",
                              "definition": "Reported revenue", "unit": "USD",
                              "periodicity": "quarterly",
                              "preferred_source_refs": ["source:sec-edgar"],
                              "verification_kind": "numeric", "caveats": []}],
            "thesis_templates": [{"template_ref": "template:demand", "statement": "Demand changes revenue.",
                                  "mechanism": "Volume", "driver_refs": ["driver:volume"],
                                  "implied_expectation": "Revenue follows demand.",
                                  "falsifier_refs": ["falsifier:revenue"]}],
        }
        body = {key: value for key, value in self.foundation.items() if key != "content_hash"}
        body["methods"] = {
            "playbook": {"binding": {"ref": seeded["playbook"]["id"],
                                       "hash": seeded["playbook"]["content_hash"]}},
            "driver_pack_template": {"value": driver_value,
                                      "content_hash": content_hash(driver_value)},
            "constitution_method": {"value": constitution_method(),
                                    "content_hash": content_hash(constitution_method())},
        }
        foundation = {**body, "content_hash": content_hash(body)}
        draft = draft_first_mission(
            self.workspace, goal="Research $ASML", industry="semiconductor equipment",
            method_foundation=foundation)
        mission = publish_first_mission_to_store(
            store, self.workspace, proposal=draft, proposal_hash=draft["content_hash"],
            actor_ref="human:owner", method_foundation=foundation)
        from dalton_core.macos_launchagent import _web_discovery_plan
        from dalton_core.mission_source_discovery import load_discovery_plan
        selected_plan = load_discovery_plan(_web_discovery_plan(self.workspace.state_dir))
        self.assertEqual(selected_plan["mission_ref"], mission["mission_ref"])
        self.assertEqual(set(selected_plan["companies"]), {"company:ticker:asml"})
        materialize_first_mission_discovery_plans(
            self.workspace, mission,
            sec_ticker_resolver=lambda ticker: {
                "ticker": ticker, "cik": "1487729", "name": "ASML Holding NV"})
        from dalton_core.macos_launchagent import _sec_discovery_plan
        sec_plan = load_discovery_plan(_sec_discovery_plan(self.workspace.state_dir))
        self.assertEqual(sec_plan["companies"]["company:ticker:asml"]["cik"], "0001487729")
        from dalton_core.coverage_mission import CoverageMissionAuthority
        authority = CoverageMissionAuthority(store)
        self.assertEqual(authority.active_mission(mission["mission_ref"])["id"], mission["id"])
        from dalton_core.mission_stage import MissionStageDriver
        first_tick = MissionStageDriver(authority).run_once()
        self.assertEqual(first_tick["status"], "entered")
        self.assertEqual(first_tick["entered"][0]["company_ref"], "company:ticker:asml")
        progress = authority.mission_progress(mission["mission_ref"])
        self.assertEqual(progress["mission_version_ref"], mission["id"])
        self.assertEqual(progress["companies"][0]["current_stage"], "initial_screen")

    def test_sec_resolution_failure_is_visible_and_uses_workspace_local_process(self):
        draft = draft_first_mission(
            self.workspace, goal="Research $ASML", industry="semiconductor equipment",
            method_foundation=self.foundation)
        mission = {**draft["mission_body"], "mission_ref": "coverage-mission:semiconductors"}
        materialize_first_mission_discovery_plans(
            self.workspace, mission,
            sec_ticker_resolver=lambda ticker: (_ for _ in ()).throw(
                WorkspaceMissionSetupError(f"SEC timeout for {ticker}")))
        status = json.loads((self.workspace.state_dir /
                             "sec-company-resolution-status.json").read_text())
        self.assertEqual(status["resolved_company_refs"], [])
        self.assertIn("SEC timeout", status["pending"]["company:ticker:asml"])
        with patch("dalton_core.workspace_mission_setup.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps(
                {"ticker": "ASML", "cik": "1487729", "name": "ASML Holding NV"})
            run.return_value.stderr = ""
            issuer = resolve_sec_ticker("ASML", state_dir=self.workspace.state_dir)
        self.assertEqual(issuer["cik"], "0001487729")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], str(self.workspace.state_dir.resolve()))
        self.assertEqual(run.call_args.kwargs["timeout"], 15.0)

    def test_writer_refuses_a_self_consistent_foreign_workspace_before_opening_authority(self):
        from dalton_core.writer_server import (
            DASHBOARD_CONTROL_OPERATIONS, Principal, WriterServer,
        )
        release = Path(self.temp.name) / "release"
        foreign = create_workspace_manifest(
            Path(self.temp.name) / "other-fleet", "foreign", 18821,
            "release:sha256:" + "a" * 64, release,
            shared_readonly_paths=[release])
        self.workspace.state_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace.state_dir / "research-foundation.json").write_text(
            json.dumps(self.foundation), encoding="utf-8")
        principal = Principal(
            "dashboard-control", "token", DASHBOARD_CONTROL_OPERATIONS,
            actor_ref="bridge:tailscale-dashboard")
        server = object.__new__(WriterServer)
        server._workspace = self.workspace
        foreign_manifest = json.loads(foreign.manifest_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(PermissionError, "differs from writer workspace"):
            server._op_publish_first_workspace_mission({
                "workspace_manifest": foreign_manifest,
                "method_foundation": self.foundation, "proposal": {},
                "proposal_hash": "0" * 64, "actor_ref": "human:tailscale-owner",
            })
        authorized = server._authorized_params(principal, "publish_first_workspace_mission", {
            "workspace_manifest": {}, "method_foundation": {}, "proposal": {},
            "proposal_hash": "0" * 64, "actor_ref": "human:tailscale-owner",
        })
        self.assertEqual(authorized["actor_ref"], "human:tailscale-owner")


if __name__ == "__main__":
    unittest.main()
