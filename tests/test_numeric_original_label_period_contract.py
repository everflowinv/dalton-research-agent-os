import copy
import json
import unittest
from unittest.mock import patch
from dalton_core.store import content_hash, canonical_json
from dalton_core.directed_numeric_output import draft_prompt, legacy_draft_prompt_v2, LABEL_PERIOD_CONTRACT_REF
from dalton_core.directed_numeric_recovery import build_numeric_output_repair, replay_numeric_output_repair, _replacement, DRAFT_IDENTITY_CONTRACT
from dalton_core.mission_document_research_executor import _effective_stage
from tests import test_directed_numeric_workflow as fixtures


class OriginalLabelPeriodContractTests(unittest.TestCase):
    def fixture(self):
        h = fixtures.NumericWorkflowTests('runTest'); self.addCleanup(h.doCleanups)
        return h.fixture()

    def test_source_label_and_period_instructions_do_not_change_typed_targets_or_quote(self):
        f,a,admission,e,p,v = self.fixture()
        args = dict(question=admission['planner_inquiry']['question'], search_proof=e.registry.search(admission['request']),
                    targets=admission['numeric_authority']['targets'], catalog=admission['numeric_authority']['metric_definitions'])
        new, old = json.loads(draft_prompt(**args)), json.loads(legacy_draft_prompt_v2(**args))
        self.assertEqual(new['label_period_contract_ref'], LABEL_PERIOD_CONTRACT_REF)
        self.assertIn('verbatim', new['label_period_instructions'])
        self.assertIn('never translate', new['label_period_instructions'])
        self.assertIn('return unavailable', new['label_period_instructions'])
        self.assertIn('never guess', new['label_period_instructions'])
        for key in ('targets','metric_definitions','quotes','identity_templates','output_schema'):
            self.assertEqual(new[key], old[key])
        for key in ('label_period_contract_ref','label_period_instructions'): new.pop(key)
        self.assertEqual(new,old)

    def test_legacy_scale_identity_initial_work_rebuilds_full_bytes_after_new_prompt(self):
        f,a,admission,e,p,v = self.fixture()
        with patch('dalton_core.directed_numeric_executor.draft_prompt', legacy_draft_prompt_v2):
            for _ in range(5):
                result = e.run_once(admission['id'])
                if result.get('stage') == 'qualitative_model_draft' and result['status'] == 'admitted': break
        ref = result['work_order_ref']; old = e.scheduler.work_order_authority(ref)
        before = f.store.connection.total_changes
        rebuilt,_ = _effective_stage(a,e.scheduler,admission,1,worker=e.draft_worker)
        self.assertEqual(rebuilt,old['work_order'])
        self.assertEqual(content_hash(rebuilt),old['work_order_hash'])
        self.assertEqual(f.store.connection.total_changes,before)
        self.assertEqual(p.calls,0)

    def test_real_old_identity_repair_link_keeps_full_work_and_proof_bytes(self):
        from functools import partial
        f,a,admission,e,p,v = self.fixture()
        p.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        with patch('dalton_core.directed_numeric_executor.draft_prompt', legacy_draft_prompt_v2), \
             patch('dalton_core.directed_numeric_recovery.build_numeric_output_repair',
                   partial(build_numeric_output_repair, contract_ref=DRAFT_IDENTITY_CONTRACT)):
            for _ in range(10):
                result = e.run_once(admission['id'])
                if result.get('reason') == 'automatic_bounded_contract_retry': break
        before = f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_recovery_links WHERE admission_ref=?',(admission['id'],)).fetchone()
        wire = tuple(before)
        link = json.loads(wire[0])
        old = e.scheduler.work_order_authority(link['recovery_work_order_ref'])
        rebuilt, links = _effective_stage(a,e.scheduler,admission,1,worker=e.draft_worker)
        self.assertEqual(rebuilt,old['work_order'])
        self.assertEqual(content_hash(rebuilt),old['work_order_hash'])
        after = f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_recovery_links WHERE admission_ref=?',(admission['id'],)).fetchone()
        self.assertEqual(tuple(after),wire)
        self.assertEqual(links,[link])
        self.assertEqual(p.calls,1)

    def test_new_label_diagnostic_repair_and_old_identity_repair_both_replay_exactly(self):
        f,a,admission,e,p,v = self.fixture()
        p.candidate_wire['targets'][0]['figure']['as_reported_label'] = '$12.3 billion'
        for _ in range(10):
            result = e.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry':break
        link = json.loads(f.store.connection.execute('SELECT record_json FROM mission_document_research_recovery_links WHERE admission_ref=?',(admission['id'],)).fetchone()[0])
        failed = e.scheduler.work_order_authority(link['failed_work_order_ref'])['work_order']
        formal = e.scheduler.formal_result(failed['id'])
        new = build_numeric_output_repair(failed,formal)
        prompt,_ = replay_numeric_output_repair(failed,new)
        self.assertEqual(json.loads(prompt)['label_period_contract_ref'],LABEL_PERIOD_CONTRACT_REF)
        self.assertIn('as_reported_label', json.loads(prompt)['previous_output_error'])
        old = copy.deepcopy(new);old['contract_ref'] = DRAFT_IDENTITY_CONTRACT
        old['replacement_prompt'] = _replacement(json.loads(failed['question']),failed['metadata']['numeric_targets'],old['diagnostic'],contract_ref=DRAFT_IDENTITY_CONTRACT)
        old['replacement_prompt_hash'] = content_hash(old['replacement_prompt']);old.pop('content_hash');old['content_hash']=content_hash(old)
        self.assertEqual(replay_numeric_output_repair(failed,old)[0],old['replacement_prompt'])
        self.assertEqual((p.calls,v.calls),(1,0))
