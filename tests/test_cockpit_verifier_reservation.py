import unittest
from decimal import Decimal
from unittest.mock import patch
from dalton_core.cockpit_model import CockpitModel


class ActualFamilyReservationTests(unittest.TestCase):
    def test_maximum_counts_every_reachable_family_and_no_producer_family(self):
        class Router:
            def latest_profiles(self):return [
                {'id':'profile:a','family':'family:a','status':'active'},
                {'id':'profile:a2','family':'family:a','status':'active'},
                {'id':'profile:b','family':'family:b','status':'active'},
                {'id':'profile:c','family':'family:c','status':'active'},
                {'id':'profile:retired','family':'family:retired','status':'retired'}]
            def get_policy(self,ref):return {'id':ref}
        model=CockpitModel.__new__(CockpitModel);model.config={'routing_policy_ref':'policy:synthetic'}
        costs={'profile:a':Decimal('99'),'profile:a2':Decimal('98'),'profile:b':Decimal('4'),
               'profile:c':Decimal('7'),'profile:retired':Decimal('200')}
        def cost(profile,**kwargs):return costs[profile['id']]
        with patch('dalton_core.model_router.resolve_chain',return_value={'chain':list(costs)}),\
             patch('dalton_core.model_fallback_chain.served_family',side_effect=lambda router,ref:{'route:a':'family:a','route:c':'family:c'}[ref]),\
             patch('dalton_core.cockpit_model.profile_call_ceiling_usd',side_effect=cost):
            options={'purpose':'verify','call_budget':{'max_output_tokens':1000,'max_cost_usd':100}}
            self.assertEqual(model._chain_ceiling(Router(),'verifier',500,**options),99000000)
            self.assertEqual(model._chain_ceiling(Router(),'verifier',500,**options,producer_route_decision_refs=['route:a']),7000000)
            self.assertEqual(model._chain_ceiling(Router(),'verifier',500,**options,producer_route_decision_refs=['route:a','route:c']),4000000)
            with self.assertRaises(KeyError):model._chain_ceiling(Router(),'verifier',500,**options,producer_route_decision_refs=['route:unknown'])
