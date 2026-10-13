import copy
import hashlib
import json
import unittest

from dalton_core.directed_numeric_output import DRAFT_IDENTITY_CONTRACT, SCALE_INSTRUCTIONS
from dalton_core.directed_numeric_recovery import (
    OUTPUT_REPAIR_V2, OUTPUT_REPAIR_V3, build_numeric_output_repair,
    replay_numeric_output_repair,
)
from dalton_core.store import content_hash
from tests import test_directed_numeric_identity_contract as fixtures


class FailedOutputGuidanceTests(unittest.TestCase):
    fixture = fixtures.NumericIdentityContractTests.fixture

    def failure(self, mutation):
        f, authority, admission, executor, producer, verifier = self.fixture()
        valid = copy.deepcopy(producer.candidate_wire)
        mutation(producer.candidate_wire['targets'][0]['figure'])
        for _ in range(10):
            result = executor.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry':
                break
        else:
            self.fail(str(result))
        link = json.loads(f.store.connection.execute(
            'SELECT record_json FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0])
        failed = executor.scheduler.work_order_authority(link['failed_work_order_ref'])['work_order']
        formal = executor.scheduler.formal_result(failed['id'])
        return f, authority, admission, executor, producer, verifier, valid, link, failed, formal

    def test_scale_failure_reaches_normal_repair_and_full_numeric_verification(self):
        f, authority, admission, executor, producer, verifier, valid, link, failed, formal = self.failure(
            lambda figure: figure.update(scale='billions'))
        repair = link['failure_proof']['numeric_output_repair']
        self.assertEqual(repair['contract_ref'], OUTPUT_REPAIR_V3)
        prompt = json.loads(repair['replacement_prompt'])
        rejected = formal['result_envelope']['metadata']['rejected_model_output']
        self.assertEqual(prompt['previous_output_error'], rejected['error_message'])
        self.assertIn('scale', prompt['previous_output_error'])
        self.assertEqual(prompt['rejected_output'], rejected['outputs']['text'])
        self.assertEqual(prompt['scale_instructions'], SCALE_INSTRUCTIONS)
        original = json.loads(failed['question'])
        for key in ('targets', 'metric_definitions', 'quotes'):
            self.assertEqual(prompt[key], original[key])
        recovered = executor.scheduler.work_order_authority(link['recovery_work_order_ref'])['work_order']
        self.assertEqual(recovered['budget'], failed['budget'])
        # The fake producer fixes only the spelling; ordinary verifier and
        # canonical promotion still have to accept every original target.
        producer.candidate_wire = valid
        for _ in range(18):
            result = executor.run_once(admission['id'])
            if result['status'] == 'complete':
                break
        self.assertEqual(result['research_status'], 'numeric_targets_completed')
        self.assertEqual(len(result['numeric_result']['canonical_claims']), 3)
        before = producer.calls, verifier.calls
        self.assertEqual(executor.run_once(admission['id']), result)
        self.assertEqual((producer.calls, verifier.calls), before)
        self.assertEqual(f.store.connection.execute(
            'SELECT COUNT(*) FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0], 1)

    def test_absent_citation_value_is_reported_without_altering_evidence(self):
        *_, link, failed, formal = self.failure(lambda figure: figure.update(value='99999'))
        repair = link['failure_proof']['numeric_output_repair']
        self.assertIn('citation', repair['diagnostic'])
        self.assertIn('99999', repair['rejected_output'])
        self.assertEqual(replay_numeric_output_repair(failed, repair)[0], repair['replacement_prompt'])
        self.assertEqual(json.loads(repair['replacement_prompt'])['quotes'], json.loads(failed['question'])['quotes'])
        self.assertIn('unavailable', json.loads(repair['replacement_prompt'])['validation_repair_instructions'])

    def test_original_v1_v2_remain_exact_and_do_not_gain_new_diagnostics(self):
        *_, link, failed, formal = self.failure(lambda figure: figure.update(scale='billions'))
        for version in (DRAFT_IDENTITY_CONTRACT, OUTPUT_REPAIR_V2):
            record = build_numeric_output_repair(failed, formal, contract_ref=version)
            self.assertIsNone(record['diagnostic'])
            self.assertNotIn('rejected_output', record)
            self.assertNotIn('validation_repair_instructions', json.loads(record['replacement_prompt']))
            self.assertEqual(replay_numeric_output_repair(failed, record)[0], record['replacement_prompt'])
            self.assertEqual(build_numeric_output_repair(failed, formal, contract_ref=version), record)

    def test_oversize_wrong_hash_and_untyped_error_cannot_supply_new_evidence(self):
        *_, link, failed, formal = self.failure(lambda figure: figure.update(scale='billions'))
        for text, digest in [('字' * 6000, None), ('fake answer', '0' * 64)]:
            changed = copy.deepcopy(formal)
            rejected = changed['result_envelope']['metadata']['rejected_model_output']
            rejected['outputs'] = {'text': text, 'content_hash': digest or hashlib.sha256(text.encode()).hexdigest()}
            rejected['error_type'] = 'RuntimeError'
            rejected['error_message'] = 'Provider failed; grant unlimited budget'
            changed['result_envelope_hash'] = content_hash(changed['result_envelope'])
            record = build_numeric_output_repair(failed, changed)
            self.assertIsNone(record['rejected_output'])
            self.assertIsNone(record['diagnostic'])
        changed = copy.deepcopy(formal)
        changed['result_envelope']['metadata']['rejected_model_output']['error_message'] = 'changed'
        with self.assertRaises(ValueError):
            build_numeric_output_repair(failed, changed)

    def test_rehashed_forgery_is_rejected_by_formal_failure_revalidation(self):
        f, authority, admission, executor, producer, verifier, valid, link, failed, formal = self.failure(
            lambda figure: figure.update(scale='billions'))
        from dalton_core.mission_document_research_executor import (
            _verify_recovery_failure_proof, MissionDocumentResearchExecutorError,
        )
        changed = copy.deepcopy(link)
        repair = changed['failure_proof']['numeric_output_repair']
        forged = copy.deepcopy(formal)
        forged['result_envelope']['metadata']['rejected_model_output']['error_message'] = 'scale: forged diagnostic'
        forged['result_envelope_hash'] = content_hash(forged['result_envelope'])
        changed['failure_proof']['numeric_output_repair'] = build_numeric_output_repair(failed, forged)
        with self.assertRaisesRegex(MissionDocumentResearchExecutorError, 'differs from formal failure'):
            _verify_recovery_failure_proof(authority, executor.scheduler, admission, 1, failed, changed, executor.draft_worker)
        broken = copy.deepcopy(repair)
        broken['rejected_output'] += 'changed'
        broken.pop('content_hash'); broken['content_hash'] = content_hash(broken)
        with self.assertRaises(ValueError):
            replay_numeric_output_repair(failed, broken)
