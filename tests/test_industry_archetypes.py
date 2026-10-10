import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from dalton_core.industry_archetypes import ARCHETYPES, archetypes_for, specialize, dossier_chain_sections
from dalton_core.workspace_runtime_setup import generic_driver_pack_template, CONSTITUTION_METHOD
from dalton_core.workspace_mission_setup import materialize_first_mission_output_policies
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.store import DaltonStore, content_hash


class ArchetypeTests(unittest.TestCase):
    def test_every_declared_archetype_has_four_measurable_mechanisms(self):
        for key, rows in ARCHETYPES.items():
            self.assertEqual({row[0] for row in rows}, {"demand","supply_and_cost","pricing","cash"})
            self.assertTrue(all("→" in row[1] and row[2] and row[3] for row in rows))

    def test_empty_workspace_hyperscalers_has_concrete_supply_pricing_cash(self):
        pack, method, binding = specialize("industry:hyperscalers", generic_driver_pack_template(), CONSTITUTION_METHOD)
        self.assertEqual(binding["status"], "specialized")
        self.assertEqual(len(binding["archetypes"]), 4)
        self.assertEqual(len(pack["drivers"]), 16)
        self.assertFalse(any("generic" in row["driver_ref"] for row in pack["drivers"]))
        self.assertNotIn("mission_defined", {row["unit"] for row in pack["metric_specs"]})
        self.assertIn("supply_and_cost", dossier_chain_sections(method["causal_chain"]))
        store = DaltonStore(":memory:"); self.addCleanup(store.close)
        held = CoverageAdmissionAuthority(store).register_driver_pack("driver-pack:synthetic", industry_ref="industry:hyperscalers",
               title="合成原型", **pack, actor_ref="human:test", version_id="driver-pack-version:synthetic:1", prior_version_ref=None, idempotency_key="synthetic-archetype")
        self.assertEqual(len(held["drivers"]),16)
        self.assertEqual(archetypes_for("industry:美国-Hyperscaler-云计算与-AI-基础设施"),binding["archetypes"])

    def test_unknown_industry_is_explicitly_unspecialized_and_custom_owner_method_survives(self):
        pack = generic_driver_pack_template()
        same, method, binding = specialize("industry:unknown",pack,CONSTITUTION_METHOD)
        self.assertEqual(same,pack)
        self.assertEqual(binding["status"],"unspecialized")
        custom={**pack,"drivers":[{**d,"driver_ref":d["driver_ref"].replace("workspace-generic","owner")} for d in pack["drivers"]]}
        same, method, binding=specialize("industry:hyperscalers",custom,CONSTITUTION_METHOD)
        self.assertEqual(same,custom)
        self.assertEqual(binding["status"],"owner_specialized")

    def test_policy_and_scope_bind_the_actual_specialized_method(self):
        pack, method, binding=specialize("industry:semiconductor-equipment",generic_driver_pack_template(),CONSTITUTION_METHOD)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            mission={"industry_ref":"industry:semiconductor-equipment","mission_ref":"coverage-mission:synthetic","id":"mission:1","content_hash":"a"*64,"created_at":"2026-10-09T00:00:00Z","universe":[{"company_ref":"company:synthetic"}]}
            constitution={"method":method,"constitution_ref":"constitution:synthetic","id":"constitution-version:synthetic:1","content_hash":"b"*64}
            held={**pack,"id":"driver-pack-version:synthetic:1","content_hash":content_hash(pack)}
            materialize_first_mission_output_policies(SimpleNamespace(state_dir=root),mission=mission,constitution=constitution,driver_pack=held)
            dossier=json.loads((root/'p12a-dossier-policy-v1.json').read_text())
            self.assertEqual(dossier['causal_chain_maps'][0]['sections'],['demand_drivers','supply_and_cost','demand_drivers','supply_and_cost'])
            frozen=json.loads((root/'industry-archetype-binding.json').read_text())
            self.assertEqual(frozen['scope'],['company:synthetic'])
            self.assertEqual(frozen['frozen_at'],mission['created_at'])
