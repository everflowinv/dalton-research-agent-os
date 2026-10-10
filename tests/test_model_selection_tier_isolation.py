"""Saved defaults must not change another role's pin or explicit override."""
import json,unittest
from tests import test_model_selection as fixture
from dalton_core.model_selection import set_tier_selection,publish_tier_selection,publish_selection,purpose_policy_bindings
from dalton_core.model_fallback_chain import effective_chain
from dalton_core.research_planner_setup import ensure_planner_policy,credential_slots_for
from dalton_core.model_router import declared_tier_chain,_policy_wire

class TierIsolationTests(fixture.StateDirectoryCase):
    def legacy(self,name,profiles):
        ref=ensure_planner_policy(self.router,profile_ids=profiles,policy_id='model-routing-policy:'+name,now=fixture.NOW)['policy_version_ref']
        config={**self.model_config,'routing_policy_ref':ref,'credential_slot_refs':credential_slots_for(self.router,profiles)}
        path=self.root/(name+'-model-config.json');path.write_text(json.dumps(config));return path,ref
    def route(self,ref,purpose,capability,profiles,tier=None,key='isolation'):
        return self.router.route(fixture._work('work:'+key,capability),attempt_number=1,capability=capability,
            policy_version_ref=ref,credential_slot_refs=credential_slots_for(self.router,profiles),
            required_modalities=['text'],required_context_tokens=2000,estimated_input_tokens=1000,
            estimated_output_tokens=500,idempotency_key=key,tier=tier,purpose=purpose)['decision']
    def test_brain_save_keeps_street_verifier_and_original_bytes(self):
        profiles=['profile:gemini-3-8-flash','profile:gemini-3-1-pro-preview']
        path,old=self.legacy('street-estimate-verifier',profiles);original=self.router.get_policy(old)
        before=self.route(old,'street_estimate_verifier','provider-controlled-verify',profiles,key='before')
        set_tier_selection(self.root,tier='brain',mode='explicit',chain=['profile:claude-fable-5-1'],now=fixture.NOW)
        pin=json.loads(path.read_text())['routing_policy_ref'];new=self.router.get_policy(pin)
        self.assertEqual(new['filters'],original['filters'])
        self.assertEqual(effective_chain(new,'street_estimate_verifier')['chain'],profiles)
        after=self.route(pin,'street_estimate_verifier','provider-controlled-verify',profiles,key='after')
        self.assertEqual(before['outcome'],'selected');self.assertEqual(after['selected_profile_version_ref'],before['selected_profile_version_ref'])
        self.assertEqual(self.router.get_policy(old),original)
        self.assertIsNone(declared_tier_chain(new,'deliverable'))
        selected=self.route(pin,'plan','research',profiles+['profile:claude-fable-5-1'],tier='brain',key='new-brain')
        self.assertEqual(selected['selected_endpoint']['model'],'claude-fable-5-1')
        self.assertEqual(_policy_wire(new),new)
    def test_verifier_save_does_not_expand_draft_pin(self):
        profiles=['profile:claude-fable-5-1']
        path,old=self.legacy('initial-screen',profiles);original=self.router.get_policy(old)
        set_tier_selection(self.root,tier='verifier',mode='explicit',chain=['profile:gemini-3-8-flash'],now=fixture.NOW)
        pin=json.loads(path.read_text())['routing_policy_ref'];new=self.router.get_policy(pin)
        self.assertEqual(new['filters'],original['filters'])
        self.assertEqual(effective_chain(new,'draft')['chain'],profiles)
        d=self.route(pin,'draft','research',profiles,key='draft-still')
        self.assertEqual(d['selected_endpoint']['model'],'claude-fable-5-1')
        v=self.route(pin,'initial_screen_verifier','provider-controlled-verify',profiles+['profile:gemini-3-8-flash'],tier='verifier',key='new-verify')
        self.assertEqual(v['selected_endpoint']['model'],'gemini-3.8-flash')
    def test_explicit_override_keeps_priority_over_saved_default_and_duplicate(self):
        chosen=publish_selection(self.router,policy_version_ref=self.policies['brain'],purpose='ask',mode='explicit',chain=['profile:claude-fable-5-1'],now=fixture.NOW)
        first=publish_tier_selection(self.router,policy_version_ref=chosen['policy_version_ref'],tier='brain',mode='explicit',chain=['profile:gpt-6-astra'],now=fixture.NOW)
        new=self.router.get_policy(first['policy_version_ref'])
        self.assertEqual(effective_chain(new,'ask')['chain'],['profile:claude-fable-5-1'])
        self.assertEqual(effective_chain(new,'plan')['chain'],['profile:gpt-6-astra'])
        d=self.route(first['policy_version_ref'],'ask','research',['profile:claude-fable-5-1'],tier='brain',key='override')
        self.assertEqual(d['selected_endpoint']['model'],'claude-fable-5-1')
        again=publish_tier_selection(self.router,policy_version_ref=first['policy_version_ref'],tier='brain',mode='explicit',chain=['profile:gpt-6-astra'],now=fixture.NOW)
        self.assertEqual(again['status'],'duplicate')
    def test_declared_chain_does_not_bypass_credentials_or_capabilities(self):
        path,old=self.legacy('street-estimate-verifier',['profile:gemini-3-8-flash'])
        out=publish_tier_selection(self.router,policy_version_ref=old,tier='brain',mode='explicit',chain=['profile:claude-fable-5-1'],now=fixture.NOW)
        d=self.route(out['policy_version_ref'],'plan','research',['profile:gemini-3-8-flash'],tier='brain',key='no-slot')
        self.assertEqual(d['outcome'],'rejected')
        d=self.route(out['policy_version_ref'],'plan','provider-controlled-verify',['profile:claude-fable-5-1'],tier='brain',key='no-controls')
        self.assertEqual(d['outcome'],'rejected')

    def test_historical_tier_still_intersects_global_allow_list(self):
        original = self.router.get_policy(self.policies['brain'])
        wire = {key: value for key, value in original.items()
                if key not in ('content_hash', 'policy_version_ref')}
        wire['id'] = 'model-routing-policy:legacy-intersection'
        wire['policy_version_ref'] = wire['id'].replace('model-routing-policy:', 'model-routing-policy-version:') + ':1'
        wire['version'] = 1
        wire['prior_version_ref'] = None
        wire['filters'] = {**wire['filters'], 'allowed_profile_ids': ['profile:gpt-6-astra']}
        wire['fallback_chains'] = {'tiers': {'brain': ['profile:gpt-6-astra', 'profile:claude-fable-5-1']}}
        held = self.router.register_policy(wire)
        d = self.route(wire['policy_version_ref'], 'plan', 'research',
                       ['profile:claude-fable-5-1'], tier='brain', key='old-intersection')
        self.assertEqual(d['outcome'], 'rejected')
        self.assertTrue(any('profile_not_allowed' in item['rejection_reasons']
                            for item in d['candidate_snapshot']))

    def test_mismatched_saved_marker_does_not_authorize_other_chain(self):
        original = self.router.get_policy(self.policies['brain'])
        wire = {key: value for key, value in original.items()
                if key not in ('content_hash', 'policy_version_ref')}
        wire['id'] = 'model-routing-policy:mismatched-marker'
        wire['policy_version_ref'] = wire['id'].replace('model-routing-policy:', 'model-routing-policy-version:') + ':1'
        wire['version'] = 1
        wire['prior_version_ref'] = None
        wire['filters'] = {**wire['filters'], 'allowed_profile_ids': ['profile:gpt-6-astra']}
        wire['fallback_chains'] = {'tiers': {
            'brain': ['profile:claude-fable-5-1'],
            'saved_default_brain': ['profile:gpt-6-astra']}}
        held = self.router.register_policy(wire)
        d = self.route(wire['policy_version_ref'], 'plan', 'research',
                       ['profile:claude-fable-5-1'], tier='brain', key='marker-mismatch')
        self.assertEqual(d['outcome'], 'rejected')

    def test_retired_purpose_refuses_before_any_policy_or_config_write(self):
        from dalton_core.model_selection import set_model_selection, RETIRED_PURPOSES, ModelSelectionError
        before = self.config_path.read_bytes()
        count = self.router.connection.execute('SELECT COUNT(*) FROM model_routing_policy_versions').fetchone()[0]
        for purpose in RETIRED_PURPOSES:
            with self.assertRaises(ModelSelectionError):
                set_model_selection(self.root, purpose=purpose, mode='explicit',
                                    chain=['profile:claude-fable-5-1'], now=fixture.NOW)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.router.connection.execute('SELECT COUNT(*) FROM model_routing_policy_versions').fetchone()[0], count)
