"""Real Cockpit transport decoration still spends one fixed screen permission."""
import copy
import unittest
from dataclasses import replace
from unittest import mock
from dalton_core import initial_screen_content_maintenance as maintenance
from dalton_core.cockpit_model import _transport_request_id, CockpitModelError
from dalton_core.contracts import WorkOrder
from dalton_core.scheduler import Scheduler
from tests import test_initial_screen_content_maintenance as fixtures


class MaintenanceTransportChildTests(unittest.TestCase):
    def fixture(self, company='syna'):
        owner=fixtures.ContentMaintenanceTests('runTest')
        self.addCleanup(owner.doCleanups)
        f=owner.fixture(company)
        f.model.config.update(capacity_retry={'cooldown_seconds':1800,
            'max_recovery_epochs':1,'scheduler_max_attempts':3},
            transport_retry={'max_definitely_not_sent_retries':0,
                'queue_wait_seconds':7,'retry_backoff_seconds':0})
        return f

    def test_real_two_company_adapter_permission_claim_verifier_and_restart(self):
        for company in ('syna','synb'):
            with self.subTest(company=company):
                f=self.fixture(company);result=f.qualify();before=len(f.f.adapter.served)
                corrected=maintenance.run(result,model=f.model,state=f.state,
                    mission=f.f.mission,authority=f.f.authority)
                self.assertEqual(corrected['record']['findings'],[])
                with Scheduler(f.model.scheduler_db) as s:
                    grant=maintenance._row(s.connection,maintenance.TABLE,maintenance.permission(result))
                    claim=maintenance._row(s.connection,maintenance.CLAIMS,grant['permission_ref'])
                    work=s.work_order_authority(claim['work_order_ref'])['work_order']
                    self.assertEqual(work['metadata']['request_id'],
                        _transport_request_id(grant['request_id'],f.model.config))
                old=copy.deepcopy((grant,claim))
                f.f.adapter.value={'verdict':'pass','findings':[]}
                verdict,call=fixtures.recovery.verifier(corrected,model=f.model,state=f.state,mission=f.f.mission)
                self.assertEqual(verdict['verdict'],'pass')
                self.assertEqual(len(f.f.adapter.served),before+2)
                # Changed current execution policy cannot erase or rebuy an
                # already owner-claimed, fully hashed original paid answer.
                f.model.config['transport_retry']['queue_wait_seconds']=9
                replay=maintenance.run(f.qualify(),model=f.model,state=f.state,
                    mission=f.f.mission,authority=f.f.authority)
                self.assertEqual(replay['record']['findings'],[])
                self.assertEqual(len(f.f.adapter.served),before+2)
                with Scheduler(f.model.scheduler_db) as s:
                    self.assertEqual((maintenance._row(s.connection,maintenance.TABLE,grant['permission_ref']),
                        maintenance._row(s.connection,maintenance.CLAIMS,grant['permission_ref'])),old)

    def test_current_wrong_policy_or_suffix_rejects_before_claim_and_provider(self):
        f=self.fixture();result=f.qualify()
        authority=maintenance.Authority(result=result,model=f.model,state=f.state,
            mission=f.f.mission,authority=f.f.authority)
        grant=authority.issue();_,prompt=authority.verify_record(grant)
        from dalton_core.cockpit_model import build_work
        budget=f.model.budget_for('initial_screen')
        request=_transport_request_id(grant['request_id'],f.model.config)
        work=build_work(purpose='initial_screen',request_id=request,prompt=prompt,
            mission_version_ref=f.f.mission['id'],created_at=f.f.mission['created_at'],
            max_input_tokens=budget['max_input_tokens'],max_output_tokens=budget['max_output_tokens'],
            max_cost_usd=budget['max_cost_usd'],max_seconds=budget['timeout_seconds'])
        before=len(f.f.adapter.served)
        with Scheduler(f.model.scheduler_db) as s:
            for invalid in (grant['request_id'],request+':operator-recovery:'+'a'*16,
                    request.replace(':capacity-policy:',':capacity-policy:bad'),request+':extra'):
                bad=replace(work,metadata={**work.metadata,'request_id':invalid})
                with self.subTest(request=invalid),self.assertRaisesRegex(ValueError,'fixed permission'):
                    authority.verify_child(s,bad)
            self.assertIsNone(maintenance._row(s.connection,maintenance.CLAIMS,grant['permission_ref']))
        self.assertEqual(len(f.f.adapter.served),before)

    def test_changed_policy_new_work_cannot_consume_claim_twice(self):
        f=self.fixture();result=f.qualify()
        maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        authority=maintenance.Authority(result=result,model=f.model,state=f.state,
            mission=f.f.mission,authority=f.f.authority)
        grant=authority.issue();_,prompt=authority.verify_record(grant)
        before=len(f.f.adapter.served)
        f.model.config['transport_retry']['queue_wait_seconds']=10
        f.model._initial_screen_content_maintenance_authority=authority
        with self.assertRaisesRegex(CockpitModelError,'immutable binding differs'):
            f.model.call(purpose='initial_screen',request_id=grant['request_id'],prompt=prompt,mission=f.f.mission)
        self.assertEqual(len(f.f.adapter.served),before)

    def test_canonical_decorator_preserves_legacy_recovery_and_timeout_behavior(self):
        f=self.fixture();config=f.model.config
        from dalton_core.store import content_hash
        suffix=':capacity-policy:'+content_hash(config['capacity_retry'])[:16]
        transport=':transport-policy:'+content_hash(config['transport_retry'])[:16]
        self.assertEqual(_transport_request_id('plain',{}),'plain')
        self.assertEqual(_transport_request_id('plain',config,timeout_recovery={}),'plain')
        self.assertEqual(_transport_request_id('plain'+suffix,config),'plain'+suffix+transport)
        recovered='plain'+suffix+':capacity-recovery:1:'+'a'*16
        self.assertEqual(_transport_request_id(recovered,config),recovered+transport)
