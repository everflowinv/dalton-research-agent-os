from __future__ import annotations
import json
import unittest
from pathlib import Path
from dalton_core.model_router import ModelRouter
from dalton_core.research_planner_setup import ensure_planner_policy, credential_slots_for
from dalton_core.workspace_host_scheme import _configured_credential_slots, _slot_row, align_credential_slots
from dalton_core.workspace_lane_parity import _credential_slot_actions
from tests.test_workspace_host_scheme import HostSchemeCase, HOST_SLOTS, NOW

class PinnedCredentialParityTests(HostSchemeCase):
    def pin(self):
        database = self.workspace / "model-router.sqlite"
        with ModelRouter(database) as router:
            profiles = router.latest_profiles()
            profile = next(p for p in profiles if p["credential_slot_ref"] == HOST_SLOTS[0])
            policy = ensure_planner_policy(router, profile_ids=[profile["id"]],
                policy_id="model-routing-policy:synthetic-paired-street", now=NOW)
            slots = credential_slots_for(router, [profile["id"]], policy_version_ref=policy["policy_version_ref"])
        config = {"model_router_db": str(database), "routing_policy_ref": policy["policy_version_ref"],
            "credential_slot_refs": slots}
        # Eliminate unrelated deliberately-short baseline configs; this leaves
        # the exact paired-config case with a richer unrelated host inventory.
        align_credential_slots(self.workspace, source_state_dir=self.source)
        path = self.workspace / "street-estimate-model-config.json"
        path.write_text(json.dumps(config))
        return path, config

    def test_minimal_valid_pin_is_not_a_gap_and_repair_does_not_expand_it(self):
        path, config = self.pin(); before = path.read_bytes()
        self.assertEqual(_configured_credential_slots(config, HOST_SLOTS), config["credential_slot_refs"])
        self.assertEqual(_slot_row(self.workspace, self.source)["status"], "ok")
        self.assertEqual(_credential_slot_actions(self.workspace, self.source), [])
        align_credential_slots(self.workspace, source_state_dir=self.source)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_actual_pinned_slot_remains_gap_and_only_that_slot_is_added(self):
        path, config = self.pin(); config["credential_slot_refs"] = []; path.write_text(json.dumps(config))
        self.assertEqual(_slot_row(self.workspace, self.source)["status"], "differs")
        self.assertEqual(len(_credential_slot_actions(self.workspace, self.source)), 1)
        align_credential_slots(self.workspace, source_state_dir=self.source)
        self.assertEqual(json.loads(path.read_text())["credential_slot_refs"], [HOST_SLOTS[0]])

    def test_corrupt_or_missing_policy_is_not_treated_as_closed_pin(self):
        path, config = self.pin()
        with ModelRouter(self.workspace / "model-router.sqlite") as router:
            # Deliberate corruption of this isolated fixture only; production
            # append-only triggers remain unchanged by the proposed code.
            router.connection.execute("DROP TRIGGER model_policy_no_update")
            router.connection.execute("UPDATE model_routing_policy_versions SET policy_hash=? WHERE policy_version_ref=?",
                ("0" * 64, config["routing_policy_ref"]))
        self.assertEqual(_configured_credential_slots(config, HOST_SLOTS), HOST_SLOTS)
        self.assertEqual(_slot_row(self.workspace, self.source)["status"], "differs")
        config["routing_policy_ref"] = "model-routing-policy-version:missing:1"
        self.assertEqual(_configured_credential_slots(config, HOST_SLOTS), HOST_SLOTS)

    def test_missing_or_corrupt_profile_is_not_an_exemption(self):
        path, config = self.pin()
        with ModelRouter(self.workspace / "model-router.sqlite") as router:
            policy = router.get_policy(config["routing_policy_ref"])
            router.connection.execute("DROP TRIGGER model_profile_no_update")
            router.connection.execute("UPDATE model_endpoint_profile_versions SET profile_hash=? WHERE profile_id=?",
                ("0" * 64, policy["filters"]["allowed_profile_ids"][0]))
        self.assertEqual(_configured_credential_slots(config, HOST_SLOTS), HOST_SLOTS)
        self.assertEqual(_slot_row(self.workspace, self.source)["status"], "differs")

    def test_unknown_legacy_and_tier_configs_keep_host_check(self):
        self.assertEqual(_configured_credential_slots({}, HOST_SLOTS), HOST_SLOTS)
        config = json.loads((self.workspace / "research-planner-model-config.json").read_text())
        self.assertEqual(_configured_credential_slots(config, HOST_SLOTS), HOST_SLOTS)
        self.assertEqual(_slot_row(self.workspace, self.source)["status"], "differs")
