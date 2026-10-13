import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from dalton_core.mission_deliverable import (
    MissionDeliverableConflict, pack_passage_resolver, validate_section, value_tokens,
)
from dalton_core.source_product_identifiers import without_verified_product_identifiers


class SourceProductIdentifierTests(unittest.TestCase):
    def validate(self, body, original, quote='Attainment was 30%.', extra=None):
        ref = 'doc-passage:doc:0-' + str(len(original)) + ':' + hashlib.sha256(original.encode()).hexdigest()[:12]
        section = {'title':'数据跟踪', 'body':body, 'claim_refs':[], 'gaps':[],
                   'numbers':[{'text':quote,'passage_ref':ref,'period':'FY2026-Q2'}, *(extra or [])]}
        return validate_section(section, resolve_passage=pack_passage_resolver(None,{ref:original}))

    def test_same_cited_original_names_product_but_short_quote_does_not(self):
        for name, category in [('z16','mainframe'), ('X100','processor'), ('A320','product')]:
            original = f'The {name} {category} cycle was comparable. Attainment was 30%.'
            result = self.validate(f'跟踪 {name} 周期；达成率30%。', original)
            self.assertIn(name, result['body'])
            self.assertEqual(result['numbers'][0]['text'], 'Attainment was 30%.')

    def test_identifier_needs_exact_name_and_explicit_source_product_context(self):
        for body,source in [('z17 主机', 'The z16 mainframe cycle. Attainment was 30%.'),
                            ('Z16 主机', 'The z16 mainframe cycle. Attainment was 30%.'),
                            ('z16 主机', 'The z16 score was recorded. Attainment was 30%.')]:
            with self.assertRaises(MissionDeliverableConflict):self.validate(body,source)

    def test_unresolved_or_tampered_source_cannot_mask_product_digits(self):
        source='The X100 processor. Attainment was 30%.'
        ref='doc-passage:doc:0-43:000000000000'
        with self.assertRaises(MissionDeliverableConflict):
            validate_section({'title':'数据跟踪','body':'X100','numbers':[{'text':'30%','passage_ref':ref}]},
                             resolve_passage=pack_passage_resolver(None,{ref:source}))

    def test_product_name_is_not_a_financial_value_or_arithmetic_operand(self):
        source='The z16 mainframe. Attainment was 30%.'
        with self.assertRaisesRegex(MissionDeliverableConflict, '16'):
            self.validate('z16 主机价格16 USD',source)
        with self.assertRaisesRegex(MissionDeliverableConflict, 'derivation uses 16'):
            self.validate('z16 主机',source,quote='z16 mainframe. Attainment was 30%.',
                extra=[{'text':'32','derivation':{'expression':'16*2','result':'32'}}])

    def test_periods_amounts_and_physical_metrics_are_not_product_exemptions(self):
        for value in ['FY2027','CO20','NO25','Scope20']:
            text=f'{value} model'
            self.assertEqual(without_verified_product_identifiers(text,[text]),text)
        self.assertEqual(value_tokens('FY2027 3M 支出2027 USD CO2排放30%'), ['2027','30%'])
        with self.assertRaises(MissionDeliverableConflict):
            self.validate('3M 产品支出30 USD','The M30 product. Attainment was 30%.')

    def test_adjacent_values_and_uncited_other_product_remain_refused(self):
        with self.assertRaises(MissionDeliverableConflict):
            self.validate('X100 产品收入45 USD','The X100 processor. Attainment was 30%.')
        with self.assertRaises(MissionDeliverableConflict):
            self.validate('X200 产品','The X100 processor. Attainment was 30%.')

    def test_source_before_name_and_unicode_body_preserve_original_output(self):
        source='The processor X100 was introduced. Attainment was 30%.'
        body='该 X100 处理器进入周期，达成率30%。'
        self.assertEqual(self.validate(body,source)['body'],body)

    def test_paid_old_refusal_revalidates_free_without_new_grant_or_fee(self):
        from tests import test_investment_memo_group_maintenance as fixture
        from dalton_core import mission_deliverable as md, source_product_identifiers as pi
        from dalton_core.investment_memo_draft import build_group_prompt
        f=fixture.MemoGroupMaintenanceTests();self.addCleanup(f.doCleanups);f.setUp()
        f.bad_groups={0}
        original='The X100 processor cycle. Attainment was 30%.'
        ref='doc-passage:doc:0-'+str(len(original))+':'+hashlib.sha256(original.encode()).hexdigest()[:12]
        for context in f.contexts:
            context['material'].append({'ref':ref,'kind':'pack_passage','text':original})
        f.prompts=[build_group_prompt(**context)for context in f.contexts]
        execute=f.adapter.execute
        def answer(work,route,profile):
            invocation,envelope=execute(work,route,profile)
            if '\nGroup: view\n' in work.question:
                value=json.loads(envelope.outputs['text']);section=value['sections'][0]
                section['body']+=' X100 处理器达成率30%。'
                section['numbers'].append({'text':'Attainment was 30%.','passage_ref':ref})
                envelope=replace(envelope,outputs={'text':json.dumps(value,ensure_ascii=False)})
            return invocation,envelope
        f.adapter.execute=answer
        with patch.object(pi,'without_verified_product_identifiers',lambda text,passages:text),\
             patch.object(md,'NUMBER_SOURCE_CONTRACT_VERSION','number-source-contract:0.9'):
            wrapper,outcome=f.seeded()
        self.assertEqual(outcome['status'],'refused');old=f.fees();calls=len(f.adapter.served)
        fresh=f.wrapper();current=f.outcome(fresh,1)
        self.assertEqual(current['status'],'drafted')
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),calls)
        self.assertIsNone(fresh.prepare_maintenance(context=f.contexts[1],outcome=current,remaining_groups=2))
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),calls)
