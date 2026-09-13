import copy
import unittest
from dalton_core.company_model_report import render_forecast_model
from dalton_core.cockpit_model_display import (
    field_label, model_metadata_text, native_chinese_model_text,
    present_invariants, readiness_labels,
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

    def test_unavailable_and_unknown_unperformed_checks_do_not_invent_failure_or_cause(self):
        records = {'company': {'forecast_model_version': {
            'status': 'unavailable', 'reasons': ['opaque-new-reason'],
            'not_checked': [{'invariant': 'new_check', 'findings': ['unknown-cause']}],
        }}}
        shown = present_invariants(records)['company']['forecast_model_version']
        self.assertEqual(['模型检查暂无结论，具体原因保存在技术详情中。'],
                         shown['display_reasons'])
        self.assertEqual(['此项检查未完成，原始说明见技术详情。'],
                         shown['display_not_checked'][0]['findings'])

    def test_native_chinese_gate_rejects_unreviewed_english_prose_but_allows_financial_acronyms(self):
        self.assertEqual('营业收入按 GAAP 口径列示。',
                         native_chinese_model_text('营业收入按 GAAP 口径列示。'))
        self.assertEqual('本季度对 营业利润 没有假设。',
                         native_chinese_model_text('本季度对 operating-income 没有假设。'))
        self.assertIsNone(native_chinese_model_text('营业收入 uses an untranslated assumption.'))

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

    def test_fixed_driver_notes_and_units_are_readable_without_changing_values(self):
        record = {'forecast_periods': [{'end': '2027-12-31'}],
                  'drivers': [{'ref': 'driver:revenue', 'label': 'revenue',
                    'status': 'unavailable',
                    'note': 'the specification names no filed counterpart for this line'},
                    {'ref': 'driver:cost_of_revenue', 'label': 'cost_of_revenue',
                     'status': 'unavailable',
                     'note': '5 specification rows split this filed line; the total is forecast and the split is not filed'},
                    {'ref': 'driver:other', 'label': 'other', 'status': 'unavailable',
                     'note': 'this concept holds no frozen model role, so nothing is computed from it'}],
                  'assumptions': [{'driver_ref': 'driver:revenue', 'kind': 'estimate',
                    'measure': 'growth', 'period': {'end': '2027-12-31'},
                    'value': '0.12345', 'because': '已披露趋势'}], 'results': []}
        before = copy.deepcopy(record)
        shown = render_forecast_model(record, include_technical=False)
        self.assertIn('尚未关联这项指标的历史披露数据', shown)
        self.assertIn('模型将该科目拆成5项；预测按合计数计算，尚无各分项的披露数据', shown)
        self.assertIn('这项披露数据暂未用于预测计算', shown)
        self.assertIn('金额单位为百万美元；股数单位为百万股', shown)
        self.assertIn('12.3%', shown)
        self.assertNotIn('12.35%', shown)
        self.assertEqual(before, record)

    def test_closed_forecast_unavailable_templates_are_chinese_without_changing_reasons(self):
        reasons = [
            'statement line result:revenue is explicitly unavailable for forecast',
            'formula terms unavailable for this quarter: result:revenue, result:cost',
            'forecast base result:revenue is unavailable for this quarter',
            'no growth assumption for us-gaap:Revenues in this quarter',
            '2 operating expense lines are not available for this quarter',
            'operating cash flow or capital expenditure is not available for this quarter',
            'no cash-flow share assumption for us-gaap:OperatingCashFlow in this quarter',
            'the forecast base would make positive-outflow capital expenditure negative',
        ]
        record = {'forecast_periods': [{'end': '2027-12-31'}], 'drivers': [],
                  'results': [{'ref': f'result:r{i}', 'label': f'项目{i}',
                               'status': 'unavailable', 'reason': reason,
                               'unit': 'USD', 'cells': []}
                              for i, reason in enumerate(reasons)]}
        before = copy.deepcopy(record)
        shown = render_forecast_model(record, include_technical=False)
        for expected in ('该报表项目未提供预测值', '本季度缺少公式所需项目',
                         '本季度缺少预测基准', '本季度缺少增长假设',
                         '本季度缺少 2 项营业费用', '本季度缺少经营现金流或资本支出'):
            self.assertIn(expected, shown)
        self.assertIn('本季度缺少现金流占比假设', shown)
        self.assertIn('预测基准会使正向列示的资本支出变为负数，因此未计算', shown)
        for reason in reasons:
            self.assertNotIn(reason, shown)
        self.assertEqual(before, record)

if __name__ == '__main__': unittest.main()
