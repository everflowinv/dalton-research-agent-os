"""Window labels are periods; amounts and arithmetic still need sources."""
import unittest
from dalton_core.mission_deliverable import (value_tokens, validate_derived_number,
    MissionDeliverableConflict)

class RollingWindowNumberTests(unittest.TestCase):
    def test_chinese_average_windows_across_lengths_and_measures(self):
        for text in ('30 日日均成交额 145.919 亿美元', '60 天日均交易量 223 股',
                     '12 周周均成交额 145.919 美元', '90 日月均价 145.919 美元'):
            with self.subTest(text=text):
                tokens = value_tokens(text, include_small=True)
                self.assertNotIn(text.split()[0], tokens)
                self.assertTrue(tokens)

    def test_derived_volume_window_reuses_exact_sourced_operand(self):
        item={'text':'30 日日均成交额 145.919 亿美元','period':'2026-10-12',
              'derivation':{'expression':'14591.9/100','result':'145.919'}}
        self.assertEqual(validate_derived_number(item,[{'text':'14591.9m美元'}]), item)
        with self.assertRaisesRegex(MissionDeliverableConflict, 'a derivation uses'):
            validate_derived_number(item, [])
        with self.assertRaisesRegex(MissionDeliverableConflict, 'does not compute'):
            validate_derived_number({**item,'derivation':{'expression':'14591.9/100','result':'145.95'}},
                                    [{'text':'14591.9m美元'}])

    def test_window_does_not_source_extra_amount_return_or_operand(self):
        item={'text':'30 日日均成交额 145.919 亿美元；额外利润 30 美元',
              'derivation':{'expression':'14591.9/100','result':'145.919'}}
        with self.assertRaisesRegex(MissionDeliverableConflict, 'neither its result nor cited'):
            validate_derived_number(item,[{'text':'14591.9m美元'}])
        for text in ('30 日内上涨 5%', '30 日增加利润 145.919 亿美元', '30 美元日均成交额'):
            self.assertIn('30',value_tokens(text,include_small=True))
        with self.assertRaisesRegex(MissionDeliverableConflict, 'a derivation uses 30'):
            validate_derived_number({'text':'日均30美元', 'derivation':{'expression':'30*1','result':'30'}},
                                    [{'text':'30 日 ADV 14591.9 美元'}])
