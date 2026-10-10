"""Persisted selection order is real route order; capacity remains fail closed."""
import copy
import unittest
from tests import test_long_context_routing_budget as long_fixture
from tests import test_model_router as fixture
from dalton_core.model_router import resolve_chain

class SelectionOrderResolutionTests(unittest.TestCase):
    def setUp(self):
        self.profiles=copy.deepcopy(long_fixture.LongContextChainTest.PROFILES)
        self.order=['profile:muse','profile:claude-opus-5','profile:zai-glm-5-3']
        self.policy={'fallback_chains':{'tiers':{'brain':self.order}}}
    def resolve(self):
        return resolve_chain(self.policy,tier='brain',purpose='investment_memo',profiles=self.profiles)
    def test_explicit_order_without_actor_metadata_remains_exact(self):
        self.policy['purpose_overrides']={'investment_memo':{'mode':'explicit','chain':self.order}}
        self.assertEqual(self.resolve()['chain'],tuple(self.order))
        self.assertNotIn('long_context_preferred',self.resolve())
    def test_saved_default_exact_order_remains_exact(self):
        self.policy['fallback_chains']['tiers']['saved_default_brain']=list(self.order)
        self.assertEqual(self.resolve()['chain'],tuple(self.order))
    def test_unselected_old_default_keeps_its_existing_preference(self):
        self.assertEqual(self.resolve()['chain'],('profile:claude-opus-5','profile:zai-glm-5-3','profile:muse'))
    def test_wrong_marker_does_not_suppress_legacy_preference(self):
        self.policy['fallback_chains']['tiers']['saved_default_brain']=list(reversed(self.order))
        self.assertEqual(self.resolve()['chain'],('profile:claude-opus-5','profile:zai-glm-5-3','profile:muse'))
    def test_all_retired_explicit_uses_live_default_preference(self):
        self.profiles['profile:retired']={'status':'retired'}
        self.policy['purpose_overrides']={'investment_memo':{'mode':'explicit','chain':['profile:retired']}}
        resolved=self.resolve()
        self.assertEqual(resolved['mode'],'tier_after_retirement')
        self.assertEqual(resolved['chain'],('profile:claude-opus-5','profile:zai-glm-5-3','profile:muse'))
        self.assertEqual(resolved['superseded_chain'],['profile:retired'])
    def test_retired_selection_falls_to_exact_saved_default(self):
        self.profiles['profile:retired']={'status':'retired'}
        self.policy['purpose_overrides']={'investment_memo':{'mode':'explicit','chain':['profile:retired']}}
        self.policy['fallback_chains']['tiers']['saved_default_brain']=list(self.order)
        self.assertEqual(self.resolve()['chain'],tuple(self.order))

class ActualSelectionOrderTests(unittest.TestCase):
    def setUp(self):
        fixture.ModelRouterTests.setUp(self)
        small=fixture.profile('grok-first',model='grok-4.7',max_context=100000)
        large=fixture.profile('long-second',model='long-context-second',max_context=1000000)
        large['limits'].update(max_input_tokens=800000,max_output_tokens=20000,max_total_tokens=820000)
        large['context']['max_output_tokens']=20000
        self.router.register_profile(small);self.router.register_profile(large)
        self.order=[small['id'],large['id']]
    def tearDown(self):
        fixture.ModelRouterTests.tearDown(self)
    def route(self,*,explicit,input_tokens,key,budget=None):
        policy=fixture.policy();policy['fallback_chains']={'tiers':{'brain':self.order}}
        if explicit:policy['purpose_overrides']={'investment_memo':{'mode':'explicit','chain':self.order}}
        else:policy['fallback_chains']['tiers']['saved_default_brain']=self.order
        self.router.register_policy(policy)
        work=fixture.work_order(work_id='work:'+key,budget=budget or {'max_input_tokens':200000,'max_output_tokens':20000,'max_total_tokens':220000,'max_cost_usd':10})
        return self.router.route(work,**fixture.route_args(purpose='investment_memo',tier='brain',estimated_input_tokens=input_tokens,estimated_output_tokens=500,required_context_tokens=input_tokens+500,idempotency_key=key))['decision']
    def test_explicit_small_prompt_uses_first_choice_without_actor(self):
        decision=self.route(explicit=True,input_tokens=1000,key='explicit-small')
        self.assertEqual(decision['selected_endpoint']['model'],'grok-4.7')
    def test_explicit_large_prompt_keeps_order_but_skips_insufficient_first(self):
        decision=self.route(explicit=True,input_tokens=90000,key='explicit-large')
        self.assertEqual(decision['selected_endpoint']['model'],'long-context-second')
        first=next(item for item in decision['candidate_snapshot'] if item['profile_version_ref']=='model-profile-version:grok-first:1')
        self.assertIn('profile_input_limit_exceeded',first['rejection_reasons'])
    def test_saved_default_small_prompt_uses_first_choice(self):
        decision=self.route(explicit=False,input_tokens=1000,key='default-small')
        self.assertEqual(decision['selected_endpoint']['model'],'grok-4.7')
    def test_saved_order_does_not_override_work_cost_cap(self):
        decision=self.route(explicit=True,input_tokens=1000,key='budget-low',budget={'max_input_tokens':200000,'max_output_tokens':20000,'max_total_tokens':220000,'max_cost_usd':0.000001})
        self.assertEqual(decision['outcome'],'rejected')
