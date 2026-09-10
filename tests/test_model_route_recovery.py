"""Changed admission recovers only proven pre-provider route refusals."""

import unittest
import json
from unittest.mock import patch

from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from tests import test_cockpit_model_fallback as fixture


class RouteRecoveryTests(unittest.TestCase):
    setUp = fixture.CockpitChainTests.setUp
    _model = fixture.CockpitChainTests._model

    def test_ticket_and_failure_key_change_with_consumed_configuration(self):
        from dalton_core.debate_map_launcher import DebateMapLauncher
        from dalton_core.conviction_call_launcher import ConvictionCallLauncher
        from dalton_core.model_route_recovery import configured_business_key
        from dalton_core.lane_child_launcher import LaneChildRejected
        config = self._model(fixture.ChainAdapter({}),
                             policy_version_ref=self.chain_policy).config
        path = self.root / "model.json"
        for cls, entity in ((DebateMapLauncher, "subject_ref"),
                            (ConvictionCallLauncher, "company_ref")):
            with self.subTest(launcher=cls.__name__):
                path.write_text(json.dumps(config))
                launcher = cls(state_dir=self.root, model_config_path=path)
                args = {entity: "company:example", "fingerprint": "same-evidence"}
                with patch.object(launcher, "spawn", side_effect=lambda **kw: kw):
                    original = launcher.start(**args)
                    key = configured_business_key("company|evidence", launcher)
                    path.write_text(json.dumps(config, indent=4, sort_keys=True))
                    self.assertEqual(original["digest"], launcher.start(**args)["digest"])
                    changed = {**config, "credential_slot_refs": ["credential-slot:new"]}
                    path.write_text(json.dumps(changed))
                    revised = launcher.start(**args)
                    self.assertNotEqual(original["digest"], revised["digest"])
                    self.assertNotEqual(key, configured_business_key("company|evidence", launcher))
                    self.assertEqual(revised["digest"], launcher.start(**args)["digest"])
                    path.write_text("{}")
                    with self.assertRaises(LaneChildRejected):
                        launcher.start(**args)

    def test_rejected_successor_stays_bounded_and_slot_order_is_not_a_change(self):
        adapter = fixture.ChainAdapter({})
        args = dict(purpose="plan", request_id="still-no-route", prompt="what next?",
                    mission=self.mission)
        slots = ["credential-slot:missing-a", "credential-slot:missing-b"]
        for current in (slots, list(reversed(slots)), ["credential-slot:missing-c"],
                        ["credential-slot:missing-c"]):
            model = self._model(adapter, policy_version_ref=self.chain_policy, slots=current)
            with self.assertRaises(CockpitModelError):
                model.call(**args)
        self.assertEqual(adapter.served, [])
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            count = scheduler.connection.execute(
                "SELECT COUNT(DISTINCT work_order_id) FROM scheduler_result_envelopes"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_fixed_credentials_run_once_and_keep_original_failure_immutable(self):
        for policy in (self.pinned_policy, self.chain_policy):
            with self.subTest(policy=policy):
                adapter = fixture.ChainAdapter({})
                args = dict(purpose="plan", request_id=policy,
                            prompt="what next?", mission=self.mission)
                broken = self._model(adapter, policy_version_ref=policy,
                                     slots=["credential-slot:unrelated"])
                with self.assertRaises(CockpitModelError):
                    broken.call(**args)
                with Scheduler(self.root / "scheduler.sqlite") as scheduler:
                    row = scheduler.connection.execute(
                        "SELECT work_order_id,result_envelope_json FROM "
                        "scheduler_result_envelopes ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    original_id, original_wire = tuple(row)
                with self.assertRaises(CockpitModelError):
                    broken.call(**args)
                self.assertEqual(adapter.served, [])
                fixed = self._model(adapter, policy_version_ref=policy)
                first = fixed.call(**args)
                self.assertNotEqual(first["work_order_ref"], original_id)
                self.assertFalse(first["replayed"])
                self.assertTrue(fixed.call(**args)["replayed"])
                self.assertEqual(len(adapter.served), 1)
                with Scheduler(self.root / "scheduler.sqlite") as scheduler:
                    kept = scheduler.connection.execute(
                        "SELECT result_envelope_json FROM scheduler_result_envelopes "
                        "WHERE work_order_id=?", (original_id,),
                    ).fetchone()[0]
                self.assertEqual(kept, original_wire)

    def test_changed_policy_recovers_refusal_but_preserves_success_on_switch(self):
        adapter = fixture.ChainAdapter({})
        args = dict(purpose="claim_index", request_id="policy-recovery",
                    prompt="what next?", mission=self.mission)
        broken = self._model(adapter, policy_version_ref=self.pinned_policy,
                             slots=self.cheap_slots)
        with self.assertRaises(CockpitModelError):
            broken.call(**args)
        fixed = self._model(adapter, policy_version_ref=self.cheap_policy,
                            slots=self.cheap_slots)
        recovered = fixed.call(**args)
        self.assertTrue(fixed.call(**args)["replayed"])
        success_args = {**args, "request_id": "successful-original"}
        first = fixed.call(**success_args)
        switched = self._model(adapter, policy_version_ref=self.chain_policy)
        replay = switched.call(**success_args)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["work_order_ref"], replay["work_order_ref"])
        self.assertNotEqual(first["work_order_ref"], recovered["work_order_ref"])

    def test_unknown_provider_failure_does_not_get_new_work_on_config_change(self):
        adapter = fixture.ChainAdapter({"profile:gpt-6-astra": {
            "code": "HOST_COMPLETION_FAILED", "message": "host completion failed"}})
        args = dict(purpose="plan", request_id="uncertain-provider",
                    prompt="what next?", mission=self.mission)
        broken = self._model(adapter, policy_version_ref=self.pinned_policy)
        with self.assertRaises(CockpitModelError):
            broken.call(**args)
        switched = self._model(adapter, policy_version_ref=self.cheap_policy,
                               slots=self.cheap_slots)
        with self.assertRaises(CockpitModelError):
            switched.call(**args)
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])


if __name__ == "__main__":
    unittest.main()
