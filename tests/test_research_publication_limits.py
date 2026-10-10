import concurrent.futures
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime,timezone
from pathlib import Path

from dalton_core.research_publication_limits import (PublicationLimits,PublicationParked,
    GovernedPublicationModel,publication_scope,reserve_dispatch,observability,fee_binding)
from dalton_core.research_publication_repair import local_repair_prompt,expand_local_patch


class PublicationLimitsTests(unittest.TestCase):
    def test_h_lint_detects_same_tick_stage_duplication(self):
        from dalton_core.research_publication_limits import audit_limits
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'limits.db';ledger=PublicationLimits(path,clock=lambda:100000)
            ledger.admit('source','tick','stage',kind='call')
            self.assertTrue(audit_limits(path,now=100001)['passed'])
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO publication_events(at,source_hash,tick,stage,kind,reason) VALUES(100000,'source','tick','stage','call','fault_injection')")
            audit=audit_limits(path,now=100001)
            self.assertFalse(audit['passed'])
            self.assertEqual(audit['violations'][0]['code'],'same_tick_stage_duplicate')

    def test_seed_failure_releases_database_lock(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'limits.db'
            with patch.object(PublicationLimits,'_seed_legacy',side_effect=OSError('rotated')):
                with self.assertRaises(OSError):PublicationLimits(path).admit('source','tick','stage',kind='call')
            db=sqlite3.connect(path,timeout=.1)
            try:db.execute('BEGIN IMMEDIATE');db.rollback()
            finally:db.close()
            PublicationLimits(path).admit('source','tick','stage',kind='call')

    def test_fee_binding_is_immutable_and_admin_uses_settled_ledger_cost(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore,ThesisImpactBudgetConflict
        from dalton_core.cockpit_model import admit_day_ledger
        from dalton_core.cockpit_v2.admin.data import publication_pipeline_observability
        from dalton_core.final_text_state import publication_work_dir
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'budget.db';work=publication_work_dir(root)
            with ThesisImpactBudgetStore(path) as budget:
                budget.register_policy(policy_version_id='policy:fee:1',day_cap_micros=1000000)
                class Model:
                    def call(self,**kwargs):
                        record=admit_day_ledger(budget,policy_version_id='policy:fee:1',day=datetime.now(timezone.utc).date().isoformat(),
                            work_order_ref='work:fee',attempt_number=1,phase='assessment',route_decision_ref='route:fee',
                            reserved_micros=500000,mission_binding=None,purpose=kwargs['purpose'],thinking_level='low')['admission']
                        self.binding=record['publication']
                        budget.settle(record['admission_id'],actual_micros=123)
                        return {'cost_micros':1}
                model=Model()
                with publication_scope(work/'publication-limits.sqlite3','source',tick='tick'):
                    GovernedPublicationModel(model).call(purpose='research_localization',request_id='id',prompt='原稿',mission={})
                self.assertEqual(model.binding['prompt_bytes'],6)
                with patch('dalton_core.cockpit_v2.admin.data._model_config',return_value={'budget_db':str(path)}):
                    value=publication_pipeline_observability(SimpleNamespace(config=SimpleNamespace(state_dir=root)))
                self.assertEqual(value['artifacts'][0]['cost_micros'],123)
                self.assertEqual(value['artifacts'][0]['cost_basis'],'day_ledger')
                self.assertEqual(value['artifacts'][0]['calls'],1)

    def test_readonly_replay_counts_unique_receipts_and_does_not_write(self):
        from dalton_core.research_publication_replay import replay
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'stages';root.mkdir()
            for index in range(3):
                path=root/(str(index)+'.json')
                path.write_text(json.dumps({'source_hash':'source','draft':{'work_order_ref':'draft'},
                    'repair_brain_calls':[{'work_order_ref':'repair:'+str(index)+':'+str(n),'cost_micros':10} for n in range(3)]}))
                os.utime(path,(100000+index,100000+index))
            before={path.name:path.read_bytes() for path in root.iterdir()}
            value=replay(root,now=100004)
            self.assertEqual(value['runs'],3)
            self.assertEqual(value['unique_calls'],10)
            self.assertEqual(value['calls_prevented_by_cap'],4)
            self.assertEqual(value['runs_prevented_by_cap'],1)
            self.assertEqual(before,{path.name:path.read_bytes() for path in root.iterdir()})

    def test_rolling_runs_calls_and_durable_park_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[100000.0];path=Path(directory)/'limits.db';ledger=PublicationLimits(path,clock=lambda:now[0])
            ledger.admit('source','tick-1','pipeline',kind='run',reason='new_version')
            ledger.admit('source','tick-1','pipeline',kind='run')
            ledger.admit('source','tick-2','pipeline',kind='run',reason='daily_retry')
            with self.assertRaisesRegex(PublicationParked,'run_24h_limit'):
                PublicationLimits(path,clock=lambda:now[0]).admit('source','tick-3','pipeline',kind='run')
            for index in range(6):ledger.admit('source','tick-1',str(index),kind='call')
            with self.assertRaisesRegex(PublicationParked,'call_24h_limit'):ledger.admit('source','tick-2','seventh',kind='call')
            self.assertTrue(any(row['reason']=='publication_run_24h_limit' for row in observability(path,now=now[0])['rows']))
            now[0]+=86400
            ledger.admit('source','tick-3','pipeline',kind='run')
            ledger.admit('source','tick-3','new',kind='call')

    def test_concurrent_chunks_share_six_dispatch_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'limits.db'
            def reserve(index):
                try:PublicationLimits(path).admit('whole-artifact','tick',str(index),kind='call');return True
                except PublicationParked:return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:outcomes=list(pool.map(reserve,range(12)))
            self.assertEqual(sum(outcomes),6)

    def test_same_stage_different_request_ids_is_a_duplicate(self):
        class Model:
            def call(self,**kwargs):return {'cost_micros':2}
        with tempfile.TemporaryDirectory() as directory,publication_scope(Path(directory)/'limits.db','source',tick='tick'):
            model=GovernedPublicationModel(Model())
            model.call(purpose='research_language_check',request_id='one',prompt='same input',mission={})
            with self.assertRaisesRegex(PublicationParked,'same_tick_stage_duplicate'):
                model.call(purpose='research_language_check',request_id='different',prompt='same input',mission={})

    def test_actual_fallback_dispatches_are_counted_before_execution(self):
        dispatched=[];binding=[]
        class Model:
            def call(self,**kwargs):
                binding.append(fee_binding())
                for index in range(7):reserve_dispatch();dispatched.append(index)
                return {}
        with tempfile.TemporaryDirectory() as directory,publication_scope(Path(directory)/'limits.db','source',tick='tick'):
            with self.assertRaisesRegex(PublicationParked,'call_24h_limit'):
                GovernedPublicationModel(Model()).call(purpose='research_localization',request_id='one',prompt='原稿',mission={})
            self.assertEqual(len(dispatched),6)
            self.assertEqual(binding[0]['prompt_bytes'],len('原稿'.encode()))
            self.assertEqual(binding[0]['source_hash'],'source')

    def test_recent_legacy_receipts_cannot_reset_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'stages').mkdir();path=root/'stages'/'legacy.json'
            value={'source_hash':'source','draft':{'work_order_ref':'draft'},'checker_call':{'work_order_ref':'checker'},
                   'brain_call':{'work_order_ref':'brain'},'repair_brain_calls':[{'work_order_ref':'repair1'},{'work_order_ref':'repair2'},{'work_order_ref':'repair3'}]}
            path.write_text(json.dumps(value));os.utime(path,(100000,100000));before=path.read_bytes()
            with self.assertRaisesRegex(PublicationParked,'call_24h_limit'):
                PublicationLimits(root/'publication-limits.sqlite3',clock=lambda:100001).admit('source','new','draft',kind='call')
            self.assertEqual(before,path.read_bytes())

    def test_quote_bound_local_patch_does_not_send_unaffected_section(self):
        product={'sections':[{'title':'收入','body':'Revenue was 10 USD.','gaps':[]},
                             {'title':'不变','body':'UNCHANGED AUTHORITY CONTEXT','gaps':[]}]}
        candidate=[{'index':0,'title':'收入','body':'收入为 11 美元。','gaps':[]},
                   {'index':1,'title':'不变','body':'未改变的正文。','gaps':[]}]
        feedback={'verifier':{'findings':[{'section_index':0,'source_quote':'Revenue was 10 USD.',
            'candidate_quote':'收入为 11 美元。','code':'changed_number','reason':'10 被改成 11'}]}}
        prompt,indices=local_repair_prompt(product,candidate,feedback)
        self.assertNotIn('UNCHANGED AUTHORITY CONTEXT',prompt)
        patch={'decisions':[],'sections':[dict(candidate[0],body='收入为 10 美元。')]}
        expanded=expand_local_patch(patch,candidate=candidate,indices=indices,decisions=[])
        self.assertEqual(expanded['sections'][1],candidate[1])
        with self.assertRaisesRegex(ValueError,'unrequested'):
            expand_local_patch({'decisions':[],'sections':candidate},candidate=candidate,indices=indices,decisions=[])
        feedback['verifier']['findings'][0]['source_quote']='invented quote'
        with self.assertRaisesRegex(ValueError,'unanchored'):local_repair_prompt(product,candidate,feedback)
        feedback['verifier']['findings'][0].update(source_quote='Revenue was 10 USD.',candidate_quote='收入为 10 美元。')
        candidate[0]['body']='收入为 10 美元。'
        with self.assertRaisesRegex(ValueError,'changed_number_feedback_not_confirmed'):
            local_repair_prompt(product,candidate,feedback)
