import json
import tempfile
import time
import unittest
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.discovery_candidate_selection import candidate_view
from dalton_core.discovery_selection_launcher import DiscoverySelectionLauncher
from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator
from dalton_core.model_router import ModelRouter
from dalton_core.mission_stage import evaluate_mission
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_mission_source_discovery import (FakeAcquisitionLauncher, FakeSearchLauncher,
    SearchHarness, plan_for_tests)
from tests.test_openclaw_model_adapter import AUTH_SECRET, FakeBroker, seal, success_response
from tests.test_transcript_polish_model_worker import policy, profile


class DiscoverySelectionUDSTests(unittest.TestCase):
    def test_real_child_uses_broker_and_persists_formal_selected_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = "alphaengine-doc:other"
            chosen = "alphaengine-doc:ibm-q2"
            harness = SearchHarness(root, [
                {"doc_id": "other", "title": "International Foods quarterly call"},
                {"doc_id": "ibm-q2", "title": "IBM Q2 earnings call transcript"},
            ])
            self.addCleanup(harness.close)
            state = bootstrap_method_authorities(harness.core)
            missions = CoverageMissionAuthority(harness.core)
            params = mission_params(state)
            for source in params["source_plan"]:
                if source["source_ref"] == "source:alphaengine":
                    source["status"] = "connected"
            params["autonomy"]["may_write"] = [*params["autonomy"]["may_write"], "source_discovery"]
            mission = missions.create_mission(params.pop("mission_ref"), **params)
            plan = plan_for_tests()
            search = FakeSearchLauncher(harness, missions, plan)
            authorization = missions.authorize_source_discovery(
                company_ref=mission["universe"][0]["company_ref"], source_ref="source:alphaengine",
                requested_by="human:coverage-owner", mission_version_ref=mission["id"])
            search.start(authorization=authorization, spec_ref="earnings-call-transcripts",
                         as_of=harness.clock().date())
            discovery = missions.source_discoveries(mission["id"])[0]
            envelope_row = harness.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                (discovery["source_envelope_ref"],)).fetchone()
            envelope = json.loads(envelope_row[0])
            view = candidate_view(harness.spool.read_object(envelope["raw_response_hash"]), envelope)

            router_path = root / "router.sqlite"
            with ModelRouter(router_path, clock=harness.clock) as router:
                selected_profile = profile()
                selected_profile["availability"]["checked_at"] = harness.clock().isoformat()
                selected_profile["availability"]["valid_until"] = "2099-01-01T00:00:00+00:00"
                router.register_profile(selected_profile)
                router.register_policy(policy())
            budget_path = root / "budget.sqlite"
            budget = ThesisImpactBudgetStore(budget_path)
            budget.register_policy(policy_version_id="budget:selection:test", day_cap_micros=1_000_000)
            self.addCleanup(budget.close)  # retains WAL sidecars for the child
            key_path = root / "broker.key"
            key_path.write_bytes(AUTH_SECRET); key_path.chmod(0o600)

            def respond(request):
                wire = success_response(request, text=json.dumps({"selected": [{
                    "document_ref": chosen, "reason": "Exact issuer and quarter"
                }]}), cost={"available": True, "usd": 0.001})
                wire.pop("contentHash")
                wire.update(provider=selected_profile["provider"], model=selected_profile["model"],
                            canonicalModel=f"{selected_profile['provider']}/{selected_profile['model']}",
                            agentId="chem")
                return seal(wire)
            broker = FakeBroker(root, respond)
            self.addCleanup(broker.close)
            config = {
                "routing_policy_ref": policy()["policy_version_ref"],
                "credential_slot_refs": [selected_profile["credential_slot_ref"]],
                "model_router_db": str(router_path), "broker_socket": str(broker.path),
                "broker_auth_key": str(key_path), "broker_client_id": "client:dalton-discovery-selection",
                "expected_agent_id": "chem", "budget_db": str(budget_path),
                "budget_policy_ref": "budget:selection:test",
            }
            config_path = root / "selection-config.json"
            config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
            launcher = DiscoverySelectionLauncher(state_dir=root, model_config_path=config_path,
                                                   scheduler_db=root / "scheduler.sqlite")
            company_ref = mission["universe"][0]["company_ref"]
            member = mission["universe"][0]
            stage = next(row for row in evaluate_mission(harness.core.connection, mission)
                         if row["company_ref"] == company_ref)
            periods = list(next(row for row in stage["items"]
                                if row["item_ref"] == "earnings_calls").get("missing_periods") or ())
            ticket = launcher.start(discovery_ref=discovery["id"], view=view,
                mission_ref=mission["id"], company={"company_ref": company_ref,
                "name": plan["companies"][company_ref]["search_terms"], "ticker": member["ticker"],
                "aliases": [member["ticker"]]}, missing_periods=periods)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = launcher.status(ticket["id"])
                if result["status"] != "running":
                    break
                time.sleep(.05)
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual([chosen], [row["document_ref"] for row in result["summary"]["selection"]["selected"]])
            self.assertEqual(len(broker.requests), 1)
            formal = harness.scheduler.formal_result(result["summary"]["selection"]["work_order_ref"])
            self.assertEqual(formal["terminal_state"], "succeeded")
            acquisition = FakeAcquisitionLauncher(harness)
            coordinator = MissionSourceDiscoveryCoordinator(
                store=harness.core, missions=missions, plan=plan, search_launcher=search,
                acquisition_launcher=acquisition, clock=harness.clock,
                spool_dir=root / "spool", selection_launcher=launcher)
            outcome = coordinator.launch_acquisition()
            self.assertEqual(outcome["status"], "launched", outcome)
            self.assertEqual([chosen], [call["document_ref"] for call in acquisition.calls])
            self.assertNotIn(other, [call["document_ref"] for call in acquisition.calls])


if __name__ == "__main__":
    unittest.main()
