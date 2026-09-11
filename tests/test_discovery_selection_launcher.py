import json,tempfile,unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from dalton_core.discovery_selection_launcher import DiscoverySelectionLauncher
from dalton_core.store import content_hash
from dalton_core.cockpit_model import build_work
from dalton_core.contracts import ResultEnvelope
from dalton_core.scheduler import Scheduler
from dalton_core.discovery_selection_launcher import _formal_selection_valid
from dalton_core.discovery_candidate_selection import PURPOSE
from dalton_core.discovery_candidate_selection import CONTRACT_REF, selection_prompt

class Process:
    pid=43210
    def __init__(self): self.code=None
    def poll(self): return self.code

class DiscoverySelectionLauncherTests(unittest.TestCase):
    def setUp(self):
        self.formal = patch('dalton_core.discovery_selection_launcher._formal_selection_valid',
                            return_value=True)
        self.formal.start()
        self.addCleanup(self.formal.stop)
    def test_ticket_is_durable_and_empty_completion_advances_page(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); config=root/'model.json'; config.write_text('{}')
            process=Process()
            launcher=DiscoverySelectionLauncher(state_dir=root,model_config_path=config,
                                                  scheduler_db=root/'scheduler.sqlite')
            base={"schema_version":"0.1","contract_ref":"x","source_envelope_ref":"e",
                  "source_envelope_hash":"a"*64,"candidates":[]}
            view={**base,"content_hash":content_hash(base)}
            with patch('dalton_core.discovery_selection_launcher.subprocess.Popen',return_value=process):
                ticket=launcher.start(discovery_ref='discovery:one',view=view,mission_ref='mission:v1',
                    company={"company_ref":"c","name":"IBM","ticker":"IBM","aliases":[]},missing_periods=[])
            summary=launcher.root/ticket['id'].split(':')[1]/'summary.json'
            selection_base={"schema_version":"0.1","contract_ref":"discovery-candidate-selection-contract:0.1",
                            "candidate_view_hash":view["content_hash"],"selected":[]}
            selection={**selection_base,"content_hash":content_hash(selection_base),
                       "work_order_ref":"work-order:test","replayed":False,
                       "result_envelope_ref":"result-envelope:test",
                       "invocation_ref":"invocation:test","route_decision_ref":"route:test",
                       "config_hash":"a"*64,"recovery_epoch":0}
            summary.write_text(json.dumps({"status":"succeeded","identity_hash":ticket["identity_hash"],
                                           "selection":selection}))
            process.code=0
            self.assertEqual(launcher.status(ticket['id'])['status'],'succeeded')
            self.assertEqual(launcher.completed_empty_discoveries(),['discovery:one'])
            settled=launcher.status(ticket['id']); launcher.mark_consumed(settled)
            periods={"c":[]}
            self.assertEqual(launcher.currently_consumed(
                mission_ref='mission:v1',missing_periods_by_company=periods),['discovery:one'])
            self.assertEqual(launcher.currently_consumed(
                mission_ref='mission:v1',missing_periods_by_company={"c":["2026-Q2"]}),[])
            config.write_text('{"call_budget":{"max_input_tokens":1}}')
            self.assertEqual(launcher.currently_consumed(
                mission_ref='mission:v1',missing_periods_by_company=periods),[])

    def test_failed_selection_has_bounded_cooldown_and_new_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); config=root/'model.json'
            config.write_text(json.dumps({"capacity_retry": {
                "max_recovery_epochs": 1, "cooldown_seconds": 60,
                "scheduler_max_attempts": 1}}))
            first=Process(); second=Process()
            launcher=DiscoverySelectionLauncher(state_dir=root,model_config_path=config,
                                                  scheduler_db=root/'scheduler.sqlite')
            base={"schema_version":"0.1","contract_ref":"x","source_envelope_ref":"e",
                  "source_envelope_hash":"a"*64,"candidates":[]}
            view={**base,"content_hash":content_hash(base)}
            args=dict(discovery_ref='discovery:one',view=view,mission_ref='mission:v1',
                      company={"company_ref":"c","name":"IBM","ticker":"IBM","aliases":[]},
                      missing_periods=[])
            with patch('dalton_core.discovery_selection_launcher.subprocess.Popen',side_effect=[first,second]):
                ticket=launcher.start(**args)
                first.code=1
                failed=launcher.status(ticket['id'])
                self.assertEqual(failed['status'],'failed')
                self.assertEqual(launcher.start(**args)['status'],'cooldown')
                launcher._now_datetime=lambda: datetime.fromisoformat(failed['completed_at'])+timedelta(seconds=61)
                retry=launcher.start(**args)
            self.assertEqual(retry['recovery_epoch'],1)
            self.assertNotEqual(retry['identity_hash'],ticket['identity_hash'])
            second.code=1
            launcher.status(retry['id'])
            self.assertEqual(launcher.start(**args)['status'],'exhausted')

    def test_sell_side_consumption_does_not_inherit_earnings_period_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);config=root/'model.json';config.write_text('{}');process=Process()
            launcher=DiscoverySelectionLauncher(state_dir=root,model_config_path=config,
                                                  scheduler_db=root/'scheduler.sqlite')
            base={"schema_version":"0.1","contract_ref":CONTRACT_REF,
                  "source_envelope_ref":"e","source_envelope_hash":"a"*64,"candidates":[]}
            view={**base,"content_hash":content_hash(base)}
            context={"research_purpose":"sell_side_research",
                     "research_question":"Assess IBM demand and competitive positioning."}
            with patch('dalton_core.discovery_selection_launcher.subprocess.Popen',return_value=process):
                ticket=launcher.start(discovery_ref='discovery:sell-side',view=view,
                    mission_ref='mission:v1',company={"company_ref":"c","name":"IBM",
                    "ticker":"IBM","aliases":[]},missing_periods=[],selection_context=context)
            selection_base={"schema_version":"0.1","contract_ref":CONTRACT_REF,
                            "candidate_view_hash":view["content_hash"],"selected":[]}
            selection={**selection_base,"content_hash":content_hash(selection_base),
                       "work_order_ref":"work-order:test","replayed":False,
                       "result_envelope_ref":"result-envelope:test","invocation_ref":"invocation:test",
                       "route_decision_ref":"route:test","config_hash":"a"*64,"recovery_epoch":0}
            directory=launcher.root/ticket['id'].split(':')[1]
            (directory/'summary.json').write_text(json.dumps({"status":"succeeded",
                "identity_hash":ticket["identity_hash"],"selection":selection}));process.code=0
            settled=launcher.status(ticket['id']);launcher.mark_consumed(settled)
            self.assertEqual(launcher.currently_consumed(mission_ref='mission:v1',
                missing_periods_by_company={"c":["2026-Q2"]}),['discovery:sell-side'])

    def test_tampered_success_summary_is_failed_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); config=root/'model.json'; config.write_text('{}')
            process=Process(); launcher=DiscoverySelectionLauncher(
                state_dir=root,model_config_path=config,scheduler_db=root/'scheduler.sqlite')
            base={"schema_version":"0.1","contract_ref":"x","source_envelope_ref":"e",
                  "source_envelope_hash":"a"*64,"candidates":[]}
            view={**base,"content_hash":content_hash(base)}
            with patch('dalton_core.discovery_selection_launcher.subprocess.Popen',return_value=process):
                ticket=launcher.start(discovery_ref='discovery:one',view=view,mission_ref='mission:v1',
                    company={"company_ref":"c","name":"IBM","ticker":"IBM","aliases":[]},missing_periods=[])
            directory=launcher.root/ticket['id'].split(':')[1]
            (directory/'summary.json').write_text(json.dumps({"status":"succeeded",
                "identity_hash":ticket['identity_hash'],"selection":{"selected":[]}}))
            process.code=0
            self.assertEqual(launcher.status(ticket['id'])['status'],'failed')
            persisted=json.loads((directory/'ticket.json').read_text())
            self.assertEqual(persisted['status'],'failed')
            self.assertIsNotNone(persisted['completed_at'])

    def test_restart_refuses_second_child_while_exact_recorded_process_runs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); config=root/'model.json'; config.write_text('{}')
            process=Process(); first=DiscoverySelectionLauncher(
                state_dir=root,model_config_path=config,scheduler_db=root/'scheduler.sqlite')
            base={"schema_version":"0.1","contract_ref":"x","source_envelope_ref":"e",
                  "source_envelope_hash":"a"*64,"candidates":[]}
            view={**base,"content_hash":content_hash(base)}
            with patch('dalton_core.discovery_selection_launcher.subprocess.Popen',return_value=process):
                first.start(discovery_ref='discovery:one',view=view,mission_ref='mission:v1',
                    company={"company_ref":"c","name":"IBM","ticker":"IBM","aliases":[]},missing_periods=[])
            restarted=DiscoverySelectionLauncher(state_dir=root,model_config_path=config,
                                                  scheduler_db=root/'scheduler.sqlite')
            with patch('dalton_core.discovery_selection_launcher.process_matches',return_value=True), \
                 patch('dalton_core.discovery_selection_launcher.subprocess.Popen') as popen:
                result=restarted.start(discovery_ref='discovery:two',view=view,mission_ref='mission:v1',
                    company={"company_ref":"c","name":"IBM","ticker":"IBM","aliases":[]},missing_periods=[])
            self.assertEqual(result['status'],'busy')
            popen.assert_not_called()

if __name__=='__main__': unittest.main()

class DiscoverySelectionFormalAuthorityTests(unittest.TestCase):
    def test_exact_success_is_accepted_and_route_tamper_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'scheduler.sqlite'
            scheduler=Scheduler(path)
            view_base={"schema_version":"0.1","contract_ref":CONTRACT_REF,
                       "source_envelope_ref":"source-envelope:test","source_envelope_hash":"a"*64,
                       "candidates":[]}
            view={**view_base,"content_hash":content_hash(view_base)}
            company={"company_ref":"company:test","name":"Test Company","ticker":"TEST","aliases":[]}
            source={"view":view,"company":company,"missing_periods":[],"mission_ref":"mission-version:test"}
            work=build_work(purpose='discovery_selection',request_id='selection:test',
                prompt=selection_prompt(view,company=company,missing_periods=[]),
                mission_version_ref='mission-version:test',max_input_tokens=10000,max_output_tokens=100,
                max_cost_usd=1,max_seconds=30,created_at='2026-09-11T00:00:00+00:00')
            scheduler.enqueue(work); lease=scheduler.claim('worker:test',work_order_id=work.id)
            envelope=ResultEnvelope(schema_version='0.1',id='result:selection:test',
                created_at='2026-09-11T00:00:01+00:00',work_order_ref=work.id,
                invocation_ref='invocation:selection:test',status='succeeded',
                outputs={'text':'{"selected":[]}'},actual_side_effects=(),usage_refs=(),artifact_refs=(),
                error=None,metadata={'route_decision_ref':'route-decision:selection:test'})
            scheduler.complete(work.id,1,'worker:test',lease['lease_token'],envelope,
                               idempotency_key='complete:selection:test')
            selection_base={"schema_version":"0.1","contract_ref":CONTRACT_REF,
                            "candidate_view_hash":view["content_hash"],"selected":[]}
            selection={**selection_base,"content_hash":content_hash(selection_base),
                       'work_order_ref':work.id,'result_envelope_ref':envelope.id,
                       'invocation_ref':envelope.invocation_ref,
                       'route_decision_ref':'route-decision:selection:test'}
            self.assertTrue(_formal_selection_valid(path,selection,source))
            self.assertFalse(_formal_selection_valid(path,{**selection,'route_decision_ref':'route:tampered'},source))
            scheduler.close()
