"""A presentation envelope cannot change numeric or evidence authority."""
import copy
import hashlib
import json
import unittest
from dataclasses import replace

from dalton_core.directed_numeric_output import _json, parse_draft, parse_verifier
from dalton_core.store import canonical_json, content_hash
from tests import test_directed_numeric_scale_contract as scale_fixtures
from tests import test_directed_numeric_workflow as workflow


class NumericJsonEnvelopeTests(unittest.TestCase):
    def fixture(self):
        return scale_fixtures.NumericScaleProducerContractTests('runTest').inputs()

    def test_one_complete_fence_preserves_exact_numeric_and_quote_bindings(self):
        _, value, metadata = self.fixture(); raw = canonical_json(value)
        for opening, newline in [('```json','\n'),('```','\n'),('```JSON','\r\n')]:
            with self.subTest(opening=opening):
                wrapped=opening+newline+raw+newline+'```'
                self.assertEqual(parse_draft(wrapped,metadata=metadata),value)
                self.assertNotEqual(hashlib.sha256(wrapped.encode()).hexdigest(),hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(parse_draft(raw,metadata=metadata),value)

    def test_duplicate_nonfinite_prose_multiple_incomplete_and_wrong_language_rejected(self):
        for text in ['```json\n{"x":1,"x":2}\n```','```json\n{"x":NaN}\n```',
                     'preface\n```json\n{}\n```','```json\n{}\n```\nafter',
                     '```json\n{}\n```\n```json\n{}\n```','```json\n{}',
                     '```python\n{}\n```']:
            with self.subTest(text=text),self.assertRaises(ValueError):_json(text)
        # An escaped newline or a legal Unicode line separator inside a string
        # is not split/reencoded while removing the outer presentation fence.
        raw='{"x":"line\\nnext\u2028last"}'
        self.assertEqual(_json('```json\n'+raw+'\n```'),json.loads(raw))

    def test_next_semantic_error_is_not_repaired_or_ignored(self):
        _, value, metadata=self.fixture()
        for field, wrong in [('value','$ 1,316,487 thousand'),('value','1316488'),
                             ('period','FY2024'),('scale','thousands'),('quote_id','quote:foreign')]:
            changed=copy.deepcopy(value);changed['targets'][0]['figure'][field]=wrong
            with self.subTest(field=field),self.assertRaises(ValueError):
                parse_draft('```json\n'+canonical_json(changed)+'\n```',metadata=metadata)
        extra={**value,'unrequested':True}
        with self.assertRaises(ValueError):parse_draft('```json\n'+canonical_json(extra)+'\n```',metadata=metadata)

    def test_verifier_closed_shape_and_candidate_binding_are_preserved(self):
        _, draft, _=self.fixture()
        verdict={'schema_version':'0.1','candidate_ref':'directed-numeric-candidate:'+content_hash(draft),
                 'verdict':'pass','targets':[{'target_ref':'fs','verdict':'accept','reason':None}]}
        metadata={'draft':draft}
        self.assertEqual(parse_verifier('```json\n'+canonical_json(verdict)+'\n```',metadata=metadata),verdict)
        wrong={**verdict,'candidate_ref':'candidate:foreign'}
        with self.assertRaises(ValueError):parse_verifier('```json\n'+canonical_json(wrong)+'\n```',metadata=metadata)

    def test_normal_workflow_keeps_work_identity_and_replays_without_extra_call(self):
        harness=workflow.NumericWorkflowTests('runTest');self.addCleanup(harness.doCleanups)
        f,authority,admission,executor,producer,verifier=harness.fixture()
        wrapped=[]
        def decorate(adapter):
            original=adapter.execute
            def execute(work,route,profile):
                invocation,envelope=original(work,route,profile)
                raw='```json\n'+envelope.outputs['text']+'\n```'
                wrapped.append((work.id,raw))
                return invocation,replace(envelope,outputs={**envelope.outputs,'text':raw,
                    'content_hash':hashlib.sha256(raw.encode()).hexdigest()})
            adapter.execute=execute
        decorate(producer);decorate(verifier)
        result=harness.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_targets_completed')
        self.assertEqual(len(result['numeric_result']['canonical_claims']),3)
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        for ref,raw in wrapped:
            formal=executor.scheduler.formal_result(ref)
            self.assertEqual(formal['terminal_state'],'succeeded')
            proof=formal['result_envelope']['outputs']
            self.assertEqual(proof['output_hash'],content_hash(proof['output']))
            self.assertEqual(proof['output'],json.loads(raw[len('```json\n'):-len('\n```')]))
            self.assertEqual(proof['work_order_ref'],ref)
        self.assertEqual(f.store.connection.execute(
            'SELECT COUNT(*) FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0],0)
        self.assertEqual(executor.run_once(admission['id']),result)
        self.assertEqual((producer.calls,verifier.calls),(1,1))
