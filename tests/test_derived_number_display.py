"""Display proofs preserve the original arithmetic, raw answer and spent slot."""
import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import derived_number_display as display
from dalton_core import mission_deliverable as md
from dalton_core.investment_memo_draft import build_group_prompt, GROUPS
from dalton_core.investment_memo_cli import run_memo
from tests import test_investment_memo_group_maintenance as fixtures


def number(expression='(6.14-6.01229)/6.01229*100', result='2.12415', text='2.12415%，按一位小数为 2.1%'):
    return {'text': text, 'period': '2026Q1', 'unit': '%',
            'derivation': {'expression': expression, 'result': result}}


class DerivedDisplayTests(unittest.TestCase):
    cited = [{'text': '6.14'}, {'text': '6.01229'}, {'text': '65.904'}, {'text': '59.875'}]

    def test_two_independent_expressions_explicit_display_and_raw_unchanged(self):
        for item in (number(), number('(65.904-59.875)/59.875*100', '10.0693', '10.0693%，按一位小数为 10.1%')):
            raw = copy.deepcopy(item)
            checked = md.validate_derived_number(item, self.cited)
            self.assertEqual(checked['derivation'], item['derivation'])
            self.assertEqual(checked['text'], item['text'])
            self.assertEqual(item, raw)
            proofs = display.prove_displays(item['text'], expression=item['derivation']['expression'],
                                            result=item['derivation']['result'], period=item['period'], unit=item['unit'])
            self.assertEqual(proofs[0]['contract_ref'], display.CONTRACT_REF)
            self.assertEqual(proofs[0]['rounding'], 'ROUND_HALF_UP')

    def test_wrong_result_still_refuses_before_display(self):
        for item in (number(result='2.124050', text='2.124050%，按一位小数为 2.1%'),
                     number('(65.904-59.875)/59.875*100', '10.069394', '10.069394%，按一位小数为 10.1%')):
            with self.assertRaisesRegex(md.MissionDeliverableConflict, 'does not compute'):
                md.validate_derived_number(item, self.cited)

    def test_same_value_elsewhere_wrong_unit_undeclared_or_inexact_display_refuses(self):
        for text in ('2.12415%，按一位小数为 2.1%，利润另为2.1。',
                     '2.12415%，按一位小数为 $2.1', '2.12415%，按一位小数为 2.2%',
                     '2.12415%，约 2.1%', '2.12415%，按三位小数为 2.124%',
                     '2.12415%，按一位小数为 2.1%，新增利润99.77'):
            with self.subTest(text=text), self.assertRaises(md.MissionDeliverableConflict):
                md.validate_derived_number(number(text=text), self.cited)

    def test_explicit_same_currency_magnitude_words_and_unknown_unit_boundaries(self):
        item=number('2009217014/100000000','20.09217014','市值 20.09217014 亿美元，显示为 20.1 亿美元')
        cited=[{'text':'市值2009217014美元'}]
        md.validate_derived_number(item,cited)
        for text in ('市值20.09217014亿美元，显示为20.1亿欧元',
                     '市值20.09217014亿美元，显示为20.1百万美元',
                     '市值20.09217014 BTC，显示为20.1 ETH',
                     '市值20.09217014亿美元，显示为20.092亿美元'):
            with self.subTest(text=text),self.assertRaises(md.MissionDeliverableConflict):
                md.validate_derived_number({**item,'text':text},cited)

    def test_arithmetic_uses_exact_expression_not_twice_rounded_result(self):
        # The accepted five-decimal result rounds up; the exact expression is
        # below the two-decimal midpoint and must display 2.12, not 2.13.
        item = number('2.12499999*1', '2.12500', '2.12500，按二位小数为2.12')
        md.validate_derived_number(item, [{'text': '2.12499999'}])
        item['text'] = '2.12500，按二位小数为2.13'
        with self.assertRaisesRegex(md.MissionDeliverableConflict, 'ROUND_HALF_UP'):
            md.validate_derived_number(item, [{'text': '2.12499999'}])
        item = number('-2.125*1', '-2.125', '-2.125，按二位小数为-2.13')
        md.validate_derived_number(item, [{'text': '2.125'}])

    def test_missing_operand_ambiguous_anchor_expression_extension_and_forged_span_refuse(self):
        with self.assertRaisesRegex(md.MissionDeliverableConflict, 'no other figure'):
            md.validate_derived_number(number(), [{'text':'6.14'}])
        with self.assertRaisesRegex(md.MissionDeliverableConflict, 'unambiguous|unsupported unit'):
            md.validate_derived_number(number(text='2.12415%和$2.12415，按一位小数为2.1%'), self.cited)
        item=number(text='2.12415%，按一位小数为2.1% + 19.31')
        with self.assertRaises(md.MissionDeliverableConflict): md.validate_derived_number(item,self.cited)
        proofs=display.prove_displays(number()['text'],expression=number()['derivation']['expression'],result='2.12415')
        bad=copy.deepcopy(proofs);bad[0]['span'][1]+=1
        with self.assertRaisesRegex(ValueError,'binding'):display.without_proved_displays(number()['text'],bad)

    def test_explicit_display_proof_is_bound_to_period_unit_and_bounded_expression(self):
        item=number();proof=display.prove_displays(item['text'],expression=item['derivation']['expression'],result='2.12415',period='2026Q1',unit='%')[0]
        self.assertEqual(proof['period'],'2026Q1');self.assertEqual(proof['unit'],'%')
        with patch.object(display,'MAX_NODES',1),self.assertRaisesRegex(ValueError,'node bound'):
            display.prove_displays(item['text'],expression=item['derivation']['expression'],result='2.12415')

    def test_full_section_current_sources_and_exact_period_still_checked(self):
        item=number()
        source={'left':'6.14','right':'6.01229'}
        section={'title':'Expected growth','body':item['text'],'numbers':[
            {'text':'6.14','number_ref':'left','period':'2026Q1'},
            {'text':'6.01229','number_ref':'right','period':'2026Q1'},item]}
        checked=md.validate_section(section,resolve_number=source.get)
        self.assertEqual(checked['body'],section['body'])
        source['right']='6.01230'
        with self.assertRaises(md.MissionDeliverableConflict):md.validate_section(section,resolve_number=source.get)
        source.clear()
        with self.assertRaises(md.MissionDeliverableConflict):md.validate_section(section,resolve_number=source.get)

    def test_real_lane_new_validation_marker_wakes_old_hold_once_without_changing_prompts(self):
        from tests import test_investment_memo_lane as lane
        f=lane.MemoFairnessTests();f.setUp()
        try:
            with patch.object(display,'CONTRACT_REF','derived-display:old'):
                original_signature=lane.company_signature(f.inputs['company:A'],f.launcher)
                previous=f.coordinator()
                previous.dispatch_once()
                previous.dispatch_once()
            current_signature=lane.company_signature(f.inputs['company:A'],f.launcher)
            self.assertNotEqual(original_signature,current_signature)
            current=f.coordinator()
            for _ in range(3):current.dispatch_once()
            f.coordinator().dispatch_once()
            self.assertEqual(sum(x['company_ref']=='company:A' for x in f.launcher.started),2)
            self.assertEqual(lane.company_signature(f.inputs['company:A'],f.launcher),current_signature)
        finally:f.doCleanups()

    def test_paid_maintenance_restart_free_acceptance_remaining_groups_and_original_verifier(self):
        for company in ('company:display-a','company:display-b'):
            with self.subTest(company=company):
                f=fixtures.MemoGroupMaintenanceTests();f.setUp()
                try:
                    f.contexts=f.make_contexts(company)
                    for context in f.contexts:
                        context['material'] += [{'ref':'left','kind':'pack_number','text':'6.14'},
                                                {'ref':'right','kind':'pack_number','text':'6.01229'}]
                    f.prompts=[build_group_prompt(**c) for c in f.contexts]
                    execute=f.adapter.execute
                    def answer(work,route,profile):
                        invocation,envelope=execute(work,route,profile)
                        if envelope.status=='succeeded' and '\nMaintenance contract: ' in work.question:
                            payload=json.loads(envelope.outputs['text'])
                            section=payload['sections'][0]
                            item=number()
                            section['numbers']=[{'text':'6.14','number_ref':'left','period':'2026Q1'},
                                                {'text':'6.01229','number_ref':'right','period':'2026Q1'},item]
                            section['body']+=' '+item['text']
                            envelope=replace(envelope,outputs={'text':json.dumps(payload,ensure_ascii=False)})
                        return invocation,envelope
                    f.adapter.execute=answer
                    wrapper=f.wrapper();f.outcome(wrapper,0);f.old_repair(wrapper);bad=f.outcome(wrapper,1)
                    f.outcome(wrapper,2);f.outcome(wrapper,3)
                    with patch.object(display,'prove_displays',return_value=[]):
                        permission,outcome=f.purchase(wrapper,bad)
                    self.assertEqual(outcome['status'],'refused')
                    fees=f.fees();calls=len(f.adapter.served)
                    fresh=f.wrapper()
                    self.assertEqual(f.outcome(fresh,1)['status'],'drafted')
                    self.assertEqual(len(f.adapter.served),calls);self.assertEqual(f.fees(),fees)
                    result,full_wrapper=f.full_run()
                    self.assertEqual(result['status'],'succeeded',result)
                    # Only the original unconsumed verifier may run; drafting
                    # and all consumed semantic/maintenance steps are replayed.
                    self.assertEqual(len(f.adapter.served),calls+1)
                    after=f.fees();self.assertEqual(after[0][:len(fees[0])],fees[0]);self.assertEqual(after[1][:len(fees[1])],fees[1])
                    again=f.wrapper();self.assertEqual(f.outcome(again,1)['status'],'drafted')
                    self.assertEqual(len(f.adapter.served),calls+1);self.assertEqual(f.fees(),after)
                finally:f.doCleanups()
