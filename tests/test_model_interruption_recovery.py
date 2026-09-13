import json,tempfile,unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from dalton_core.model_interruption_recovery import recover,ModelInterruptionRecoveryError
from dalton_core.model_router import ModelRouter
from dalton_core.scheduler import Scheduler
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_scheduler import work_order
from tests.test_model_router import profile,policy
from dalton_core.contracts import ResultEnvelope

class RecoveryTests(unittest.TestCase):
 def test_undispatched_recovery_is_exact_and_never_routes_or_spends(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); sched=root/'scheduler.sqlite'; router=root/'router.sqlite'; budget=root/'budget.sqlite'; journal=root/'journal.json'; config=root/'config.json'
   with Scheduler(sched) as s:
    s.enqueue(work_order()); lease=s.claim('worker:a',work_order_id='work-1')
   ModelRouter(router).close(); ThesisImpactBudgetStore(budget).close()
   journal.write_text(json.dumps({'schemaVersion':'0.1','records':[]}));config.write_text('{}')
   plan={'schema_version':'dalton-model-interruption-recovery-plan:0.1','process_pid':25400,
    'process_start':'2026-09-13T13:26:00-04:00','scheduler_db':str(sched),'router_db':str(router),
    'budget_db':str(budget),'broker_journal':str(journal),'entries':[{'work_order_ref':'work-1',
    'attempt_number':1,'lease_revision_ref':lease['lease']['id'],'lease_hash':lease['lease']['content_hash'],
    'work_order_hash':lease['work_order_hash'],'owner_ref':'worker:a','disposition':'undispatched','model_config':str(config),'model_config_sha256':__import__('hashlib').sha256(config.read_bytes()).hexdigest()}]}
   checked=recover(plan,execute=False,process_is_alive=lambda pid:False)
   self.assertEqual(checked['entries'][0]['status'],'validated')
   done=recover(plan,execute=True,process_is_alive=lambda pid:False)
   self.assertEqual(done['entries'][0]['status'],'fresh')
   duplicate=recover(plan,execute=True,process_is_alive=lambda pid:False)
   self.assertEqual(duplicate['entries'][0]['status'],'duplicate')
   with Scheduler(sched) as s:self.assertEqual(s.status('work-1')['attempt_number'],2)

 def test_live_process_is_rejected_before_authority_access(self):
  plan={'schema_version':'dalton-model-interruption-recovery-plan:0.1','process_pid':9,
   'process_start':'x','scheduler_db':'x','router_db':'x','budget_db':'x','broker_journal':'x','entries':[]}
  with self.assertRaisesRegex(ModelInterruptionRecoveryError,'entries must be non-empty'):
   recover(plan,process_is_alive=lambda pid:True)

 def test_durable_replay_settles_original_admission_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); sched=root/'scheduler.sqlite'; routerdb=root/'router.sqlite'; budgetdb=root/'budget.sqlite'; journal=root/'journal.json'; config=root/'config.json'
   work=work_order(); work['requested_capabilities']=['research']; work['metadata']={'purpose':'research_language_revision'}; work['budget']={'max_input_tokens':2000,'max_output_tokens':1000,'max_total_tokens':3000,'max_cost_usd':1.0,'max_seconds':600}
   with Scheduler(sched) as s:s.enqueue(work); lease=s.claim('worker:a',work_order_id='work-1')
   route={'id':'route:1','selected_profile_version_ref':'profile-version:1','work_order_ref':'work-1','attempt_number':1}
   selected={'profile_version_ref':'profile-version:1','cost':{'input_per_million_usd':.1,'output_per_million_usd':.2}}
   with ThesisImpactBudgetStore(budgetdb) as budget:
    budget.register_policy(policy_version_id='budget:1',day_cap_micros=1000000)
    budget.admit(policy_version_id='budget:1',day='2026-09-13',work_order_ref='work-1',attempt_number=1,phase='assessment',route_decision_ref=route['id'],reserved_micros=100000)
   inv='invocation:test'; recorded=int(datetime.fromisoformat(lease['lease']['issued_at']).timestamp()*1000)+1
   record={'invocationId':inv,'state':'completed','createdAtMs':recorded,'requestHash':'a'*64,'response':{'workOrderId':'work-1','invocationId':inv}}
   journal.write_text(json.dumps({'schemaVersion':'0.1','records':[record]}));config.write_text('{}')
   envelope=ResultEnvelope(schema_version='0.1',id='result:test',created_at='2026-01-01T00:00:02+00:00',work_order_ref='work-1',invocation_ref=inv,status='succeeded',outputs={'text':'ok'},actual_side_effects=(),usage_refs=(),artifact_refs=(),error=None,metadata={})
   class FakeAdapter:
    def replay(self,*_):return SimpleNamespace(id=inv,usage={'raw_provider_telemetry':{'cost':{'available':True,'usd':.01}}}),envelope
   class FakeModel:
    def __init__(self,*a,**k):pass
    def budget_for(self,p):return {'timeout_seconds':600}
    def _adapter(self,*a,**k):return FakeAdapter()
   class FakeRouter:
    def __init__(self,*a,**k):pass
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def list_decisions(self,**k):return [route]
    def get_profile(self,*a):return selected
   plan={'schema_version':'dalton-model-interruption-recovery-plan:0.1','process_pid':25400,'process_start':'start','scheduler_db':str(sched),'router_db':str(routerdb),'budget_db':str(budgetdb),'broker_journal':str(journal),'entries':[{'work_order_ref':'work-1','attempt_number':1,'lease_revision_ref':lease['lease']['id'],'lease_hash':lease['lease']['content_hash'],'work_order_hash':lease['work_order_hash'],'owner_ref':'worker:a','disposition':'durable_completion','model_config':str(config),'model_config_sha256':__import__('hashlib').sha256(config.read_bytes()).hexdigest()}]}
   with patch('dalton_core.model_interruption_recovery.CockpitModel',FakeModel),patch('dalton_core.model_interruption_recovery.ModelRouter',FakeRouter):
    route['attempt_number']=2
    with self.assertRaisesRegex(ModelInterruptionRecoveryError,'route/admission attempt'):
     recover(plan,execute=True,process_is_alive=lambda pid:False)
    route['attempt_number']=1
    with patch.object(Scheduler,'reconcile_interrupted_model_attempt',side_effect=RuntimeError('crash after settlement')):
     with self.assertRaisesRegex(RuntimeError,'crash after settlement'):recover(plan,execute=True,process_is_alive=lambda pid:False)
    first=recover(plan,execute=True,process_is_alive=lambda pid:False)
    config.write_text('{"drift":true}')
    with self.assertRaisesRegex(ModelInterruptionRecoveryError,'config hash drifted'):
     recover(plan,execute=True,process_is_alive=lambda pid:False)
    config.write_text('{}')
    journal.write_text(json.dumps({'schemaVersion':'0.1','records':[record,
     {'invocationId':'invocation:unrelated','state':'pending','createdAtMs':recorded+2}]}))
    second=recover(plan,execute=True,process_is_alive=lambda pid:False)
   self.assertEqual(first['entries'][0]['status'],'fresh');self.assertEqual(second['entries'][0]['status'],'duplicate')
   with ThesisImpactBudgetStore(budgetdb) as budget:
    self.assertEqual(budget.admission(work_order_ref='work-1',attempt_number=1,phase='assessment')['settlement']['actual_micros'],10000)

 def test_cas_drift_is_rejected_before_budget_settlement(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);sched=root/'s.sqlite';router=root/'r.sqlite';budgetdb=root/'b.sqlite';journal=root/'j.json';config=root/'c.json'
   with Scheduler(sched) as s:s.enqueue(work_order());lease=s.claim('worker:a',work_order_id='work-1')
   ModelRouter(router).close()
   with ThesisImpactBudgetStore(budgetdb) as b:
    b.register_policy(policy_version_id='budget:1',day_cap_micros=1000000)
    b.admit(policy_version_id='budget:1',day='2026-09-13',work_order_ref='work-1',attempt_number=1,phase='assessment',route_decision_ref='route:1',reserved_micros=100000)
   journal.write_text('{"schemaVersion":"0.1","records":[]}');config.write_text('{}')
   entry={'work_order_ref':'work-1','attempt_number':1,'lease_revision_ref':lease['lease']['id'],'lease_hash':'0'*64,'work_order_hash':lease['work_order_hash'],'owner_ref':'worker:a','disposition':'durable_completion','model_config':str(config),'model_config_sha256':__import__('hashlib').sha256(config.read_bytes()).hexdigest()}
   plan={'schema_version':'dalton-model-interruption-recovery-plan:0.1','process_pid':25400,'process_start':'start','scheduler_db':str(sched),'router_db':str(router),'budget_db':str(budgetdb),'broker_journal':str(journal),'entries':[entry]}
   with self.assertRaisesRegex(Exception,'CAS authority'):
    recover(plan,execute=True,process_is_alive=lambda pid:False)
   with ThesisImpactBudgetStore(budgetdb) as b:self.assertIsNone(b.admission(work_order_ref='work-1',attempt_number=1,phase='assessment')['settlement'])
