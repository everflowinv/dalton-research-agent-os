"""Provider-only edits reach the actual model picker without a second allow step."""

import json
import unittest

from dalton_core.mission_model_catalog_lane import ModelCatalogSyncCoordinator
from dalton_core.model_router import ModelRouter
from tests import test_model_selection as fixtures


class ProviderCatalogPickerTests(unittest.TestCase):
    def test_follow_mode_hides_legacy_routes_removed_from_provider_catalog(self):
        from dalton_core.model_deployment import openclaw_broker_profiles

        fixture = fixtures.CockpitModelPageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.install()
        plane = fixture.plane(with_model_config=True)
        profile = openclaw_broker_profiles(checked_at=fixtures.NOW)[0]
        profile.update(id="model-profile:legacy-removed",
                       profile_version_ref="model-profile-version:legacy-removed:1",
                       model="legacy-removed")
        profile.pop("content_hash", None)
        with ModelRouter(fixture.router_db) as router:
            router.register_profile(profile)
        self.assertIn(profile["id"], [x["model"] for x in plane.models()["choices"]])
        (fixture.root / "model-catalog-sync.json").write_text(json.dumps({
            "openclaw_config_path": str(fixture.openclaw),
            "model_router_db": str(fixture.router_db),
            "follow_provider_catalog": True,
        }))
        self.assertNotIn(profile["id"], [x["model"] for x in plane.models()["choices"]])
        with ModelRouter(fixture.router_db) as router:
            self.assertIn(profile["id"], [x["id"] for x in router.latest_profiles()])

    def test_provider_add_update_remove_and_revive_reach_picker_without_reselecting_chains(self):
        fixture = fixtures.CockpitModelPageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.install()
        plane = fixture.plane(with_model_config=True)
        selection = fixture.root / "research-planner-model-config.json"
        original_selection = selection.read_bytes()
        switch = fixture.root / "model-catalog-sync.json"
        switch.write_text(json.dumps({
            "openclaw_config_path": str(fixture.openclaw),
            "model_router_db": str(fixture.router_db),
            "follow_provider_catalog": True,
        }))
        coordinator = ModelCatalogSyncCoordinator(
            config_path=switch, clock=lambda: fixtures.NOW)
        coordinator.dispatch_once()
        initial = plane.models()
        original_chains = {x["purpose"]: x["chain"] for x in initial["purposes"]}
        config = json.loads(fixture.openclaw.read_text())
        config["models"]["providers"]["muse-cli-gateway"] = {
            "apiKey": "private-test-key-never-in-picker",
            "models": [{"id": "muse-spark-1.3", "contextWindow": 200000,
                        "maxTokens": 10000, "cost": {"input": 1.25, "output": 4.25}}],
        }
        fixture.openclaw.write_text(json.dumps(config))
        added = coordinator.dispatch_once()
        self.assertTrue(added["catalog_in_sync"])
        view = plane.models()
        choice = next(x for x in view["choices"] if x["model_ref"] == "muse-spark-1.3")
        self.assertEqual(choice["display_name"], "muse-spark-1.3 · Muse 网关")
        self.assertTrue(view["catalog"]["follow_provider_catalog"])
        self.assertIn("自动跟随 OpenClaw", view["catalog"]["sync_note"])
        self.assertNotIn("private-test-key", json.dumps(view))
        self.assertEqual(view["catalog"]["in_openclaw_not_allowed"], [])
        self.assertEqual(view["catalog"]["allowed_without_broker_profile"], [])
        profile_id = choice["model"]
        original_version = choice["profile_version_ref"]
        with ModelRouter(fixture.router_db) as router:
            original_profile = router.connection.execute(
                "SELECT profile_json FROM model_endpoint_profile_versions "
                "WHERE profile_version_ref=?", (original_version,),
            ).fetchone()[0]
        config = json.loads(fixture.openclaw.read_text())
        model = config["models"]["providers"]["muse-cli-gateway"]["models"][0]
        model.update(contextWindow=300000, maxTokens=12000,
                     cost={"input": 1.0, "output": 3.5})
        fixture.openclaw.write_text(json.dumps(config))
        coordinator.dispatch_once()
        updated = next(x for x in plane.models()["choices"] if x["model"] == profile_id)
        self.assertNotEqual(updated["profile_version_ref"], original_version)
        with ModelRouter(fixture.router_db) as router:
            profile = next(x for x in router.latest_profiles() if x["id"] == profile_id)
            self.assertEqual(profile["context"]["max_output_tokens"], 12000)
            self.assertEqual(profile["context"]["max_context_tokens"], 300000)
            self.assertEqual(profile["cost"]["input_per_million_usd"], 1.0)
        config = json.loads(fixture.openclaw.read_text())
        saved = config["models"]["providers"].pop("muse-cli-gateway")
        fixture.openclaw.write_text(json.dumps(config))
        coordinator.dispatch_once()
        self.assertNotIn(profile_id, [x["model"] for x in plane.models()["choices"]])
        config = json.loads(fixture.openclaw.read_text())
        config["models"]["providers"]["muse-cli-gateway"] = saved
        fixture.openclaw.write_text(json.dumps(config))
        coordinator.dispatch_once()
        self.assertIn(profile_id, [x["model"] for x in plane.models()["choices"]])
        self.assertEqual(selection.read_bytes(), original_selection)
        self.assertEqual({x["purpose"]: x["chain"] for x in plane.models()["purposes"]},
                         original_chains)
        with ModelRouter(fixture.router_db) as router:
            preserved = router.connection.execute(
                "SELECT profile_json FROM model_endpoint_profile_versions "
                "WHERE profile_version_ref=?", (original_version,),
            ).fetchone()[0]
        self.assertEqual(preserved, original_profile)
        self.assertFalse(fixture.calls)
