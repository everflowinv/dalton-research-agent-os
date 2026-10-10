"""Producer scale spelling matches the strict, unchanged numeric authority."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from dalton_core.directed_numeric_output import (DRAFT_SCHEMA, LEGACY_DRAFT_SCHEMA,
    draft_prompt, legacy_draft_prompt_v0, legacy_draft_prompt_v1, parse_draft)
from dalton_core.directed_numeric_research import CONTRACT_REF, normalize_targets
from dalton_core.document_numeric_claim import _SCALE_WORDS
from dalton_core.store import canonical_json
from tests import test_directed_numeric_workflow as workflow

class NumericScaleProducerContractTests(unittest.TestCase):
    def inputs(self):
        quote='Synthetic issuer FY2025 revenue (in thousands of USD): Financial services $1,316,487.'
        proof={'matches':[{'source_start':0,'source_end':len(quote),
            'excerpt_sha256':hashlib.sha256(quote.encode()).hexdigest(),'excerpt':quote}]}
        targets=normalize_targets([{'target_ref':'fs','metric_ref':'metric:fs-revenue',
            'metric_definition':{'label':'Financial services','meaning':'Financial services revenue for FY2025.'},
            'period':'FY2025','unit':'currency','currency':'USD','basis':'gaap-reported'}])
        args=dict(question='What is financial services revenue?', search_proof=proof,targets=targets,catalog=[])
        qid=next(iter(json.loads(draft_prompt(**args))['quotes']))
        value={'schema_version':CONTRACT_REF,'targets':[{'target_ref':'fs','status':'supported','reason':None,
            'figure':{'quote_id':qid,'metric_ref':'metric:fs-revenue','subject_as_named':'Synthetic issuer',
                'as_reported_label':'Financial services','value':'1316487','unit':'currency',
                'currency':'USD','period':'FY2025','basis':'gaap-reported','scale':'thousand'}}]}
        return args,value,{'numeric_targets':targets,'retrieval_proof':proof}

    def test_canonical_multiplier_keeps_reported_digits_and_source_quote(self):
        args,value,metadata=self.inputs();original=copy.deepcopy(value)
        prompt=json.loads(draft_prompt(**args));self.assertIn('thousand',DRAFT_SCHEMA['properties']['targets']['items']['properties']['figure']['anyOf'][0]['properties']['scale']['enum'])
        self.assertEqual(parse_draft(canonical_json(value),metadata=metadata),original)
        self.assertEqual(value,original)
        self.assertEqual(prompt['quotes'],json.loads(legacy_draft_prompt_v1(**args))['quotes'])
        self.assertIn('in thousands',prompt['scale_instructions'])
        self.assertIn('canonical scale thousand',prompt['scale_instructions'])
        self.assertIn('Preserve the reported digits',prompt['scale_instructions'])
        self.assertEqual(DRAFT_SCHEMA['properties']['targets']['items']['properties']['figure']['anyOf'][0]['properties']['scale']['enum'],[None,*sorted(_SCALE_WORDS)])

    def test_real_plural_failure_is_rejected_by_provider_schema_and_local_parser(self):
        args,value,metadata=self.inputs()
        for spelling in ('thousands','USD','percent','arbitrary'):
            bad=copy.deepcopy(value);bad['targets'][0]['figure']['scale']=spelling
            with self.subTest(scale=spelling):
                self.assertNotIn(spelling,DRAFT_SCHEMA['properties']['targets']['items']['properties']['figure']['anyOf'][0]['properties']['scale']['enum'])
                with self.assertRaisesRegex(ValueError,'scale must be null or one of'):
                    parse_draft(canonical_json(bad),metadata=metadata)
        # The schema narrowing affects new producers only, not historical bytes.
        self.assertEqual(LEGACY_DRAFT_SCHEMA['properties']['targets']['items']['properties']['figure']['anyOf'][0]['properties']['scale'],{'type':['string','null']})
        self.assertNotIn('scale_instructions',json.loads(legacy_draft_prompt_v0(**args)))
        self.assertNotIn('scale_contract_ref',json.loads(legacy_draft_prompt_v1(**args)))
        wrong_digits=copy.deepcopy(value);wrong_digits['targets'][0]['figure']['value']='1316488'
        with self.assertRaises(ValueError):parse_draft(canonical_json(wrong_digits),metadata=metadata)

    def test_published_identity_work_replays_exactly_without_replacing_old_prompt_or_hash(self):
        from dalton_core.mission_document_research_executor import _effective_stage
        harness=workflow.NumericWorkflowTests('runTest');harness.addCleanup=self.addCleanup
        f,authority,admission,executor,producer,verifier=harness.fixture()
        with patch('dalton_core.directed_numeric_executor.draft_prompt',legacy_draft_prompt_v1):
            for _ in range(6):
                executor.run_once(admission['id'])
                rows=executor.scheduler.connection.execute("SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE json_extract(work_order_json,'$.metadata.mission_document_research_admission_ref')=? AND json_extract(work_order_json,'$.metadata.stage')='qualitative_model_draft'",(admission['id'],)).fetchall()
                if rows:break
        self.assertEqual(len(rows),1);old=json.loads(rows[0]['work_order_json']);old_hash=rows[0]['work_order_hash']
        self.assertNotIn('scale_contract_ref',json.loads(old['question']))
        work,links=_effective_stage(authority,executor.scheduler,admission,1,worker=executor.draft_worker)
        self.assertEqual(work,old);self.assertEqual(links,[])
        executor.run_once(admission['id'])
        held=executor.scheduler.work_order_authority(old['id'])
        self.assertEqual(held['work_order'],old);self.assertEqual(held['work_order_hash'],old_hash)
        self.assertEqual(producer.calls,1)
