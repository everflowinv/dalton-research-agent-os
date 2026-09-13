import copy
import unittest
from dalton_core.company_model_report import render_forecast_model
from dalton_core.cockpit_model_display import (
    field_label, model_metadata_text, present_invariants, readiness_labels,
)

class ModelDisplayTest(unittest.TestCase):
    def test_reader_preserves_values_and_keeps_machine_formula_in_technical_view(self):
        record = {'id': 'forecast-model-version:abc', 'version': 3,
                  'generator_ref': 'rule:structured-positive-trailing-carry-forward:2',
                  'forecast_periods': [{'end': '2027-12-31'}],
                  'drivers': [{'ref': 'row:revenue', 'label': 'Revenue',
                               'status': 'unavailable', 'note': 'legacy filed concept'}],
                  'results': [{'ref': 'result:net_income', 'label': 'Net Income',
                    'status': 'computed', 'unit': 'USD', 'formula': 'net_income[k] = revenue[k]',
                    'cells': [{'period': {'end': '2027-12-31'}, 'value': 15623445,
                               'status': 'computed', 'unit': 'USD'}]}]}
        before = copy.deepcopy(record)
        normal = render_forecast_model(record, entity_name='IBM', include_technical=False)
        technical = render_forecast_model(record, entity_name='IBM')
        self.assertIn('15.6', normal)
        self.assertIn('营业收入', normal)
        self.assertIn('历史披露科目', normal)
        for raw in ('row:revenue', 'net_income[k]', 'forecast-model-version:', 'rule:'):
            self.assertNotIn(raw, normal)
            self.assertIn(raw, technical)
        self.assertEqual(before, record)
        labels = readiness_labels(record, {'results_unavailable': ['result:net_income']}, lambda x:x)
        self.assertEqual(['净利润'], labels['results_unavailable_labels'])
        self.assertEqual(['result:net_income'], labels['results_unavailable'])

    def test_unperformed_checks_keep_the_specific_reason_and_original_evidence(self):
        raw = 'no two adjacent quarters both computed with the cost ratios held'
        records = {'company': {'forecast_model_version': {
            'status': 'available', 'output_ref': 'model:original',
            'not_checked': [{'invariant': 'direction_consistency', 'findings': [raw]}]}}}
        shown = present_invariants(records)['company']['forecast_model_version']
        self.assertIn('相邻季度', shown['display_not_checked'][0]['findings'][0])
        self.assertEqual(raw, shown['technical_details']['not_checked'][0]['findings'][0])
        self.assertEqual('available', shown['status'])

    def test_registered_concepts_and_roles_are_readable_without_changing_numbers(self):
        text = ('本季度对 us-gaap:NetCashProvidedByUsedInOperatingActivities 没有假设；'
                '公式所需 operating-income、fx-result 与 interest-other 不可用；数值 -12.5。')
        shown = model_metadata_text(text)
        self.assertEqual(
            '本季度对 经营活动产生的现金流量净额 没有假设；'
            '公式所需 营业利润、汇兑损益 与 利息及其他收益净额 不可用；数值 -12.5。',
            shown,
        )
        self.assertEqual('购建固定资产支付',
                         field_label('us-gaap:PaymentsToAcquirePropertyPlantAndEquipment'))
        self.assertEqual('季度环比增速', field_label('quarterly_growth'))
        self.assertEqual('预测假设复核', field_label('assumption_review'))

    def test_long_bilingual_label_is_complete_and_not_cut_mid_alias(self):
        record = {'forecast_periods': [{'end': '2027-12-31'}], 'drivers': [],
                  'results': [{'ref': 'result:diluted_weighted_average_shares',
                    'label': '摊薄加权平均股数（Diluted weighted-average shares）',
                    'status': 'computed', 'unit': 'shares', 'formula': 'shares[k]',
                    'cells': [{'period': {'end': '2027-12-31'}, 'value': 55800000,
                               'status': 'computed', 'unit': 'shares'}]}]}
        shown = render_forecast_model(record, include_technical=False)
        self.assertIn('摊薄加权平均股数', shown)
        self.assertNotIn('Diluted weighted-average sh', shown)
        self.assertIn('55.8', shown)

if __name__ == '__main__': unittest.main()
