import unittest
from dalton_core.research_reasoning_lint import lint_text


def codes(text):return {x['rule'] for x in lint_text(text)}


class FutureObservationScopeTests(unittest.TestCase):
    def test_order_ratio_and_revenue_are_separate_observation_conditions(self):
        for text in ['如果新产品带来新增收入，而且新签订单出货比维持1.1，我们上调观点。',
                     '收入增速提高，同时新签订单收入比超过1.0，是两项观察条件。']:
            self.assertNotIn('backlog_as_near_term_revenue',codes(text))

    def test_conditional_direct_order_conversion_still_needs_evidence(self):
        for text in ['新签订单增加，所以锁定下季度收入。','如果新签订单增加，就意味着近期收入增长。',
                     '新签订单收入比1.1，因此保障下季度收入。']:
            self.assertIn('backlog_as_near_term_revenue',codes(text))
        self.assertNotIn('backlog_as_near_term_revenue',codes('RPO按历史转化率支撑下季度收入。'))

    def test_future_consensus_movement_is_not_a_current_consensus_claim(self):
        for text in ['如果收入改善，一致预期上修，我们将上调判断。',
                     '若一致预期下修，我们重新审查。','未来一致预期可能上修。']:
            self.assertNotIn('consensus_without_authority',codes(text))

    def test_typed_future_movement_and_falsification_keep_identity_without_claiming_a_level(self):
        for text in ['如果新产品加速，数据商一致预期会上修，我们重新评估。',
                     '证伪中性判断的信号是数据商 FY2028 一致预期 EPS 被上修，同时收入改善。',
                     '观察条件为分析师 FY2027 一致预期收入下修。',
                     '如果销量改善，卖方一致预期FY2028 EPS将上修。']:
            with self.subTest(text=text):self.assertNotIn('consensus_without_authority',codes(text))
        for text in ['数据商 FY2028 一致预期 EPS 已经上修。',
                     '如果收入改善，数据商 FY2028 一致预期 EPS 上修到15。',
                     '证伪信号是当前数据商一致预期 EPS 为15。',
                     '观察条件为一致预期上修幅度达到5%。',
                     '证伪信号是一致预期EPS被上修，但当前一致预期EPS为15。',
                     '数据商FY2028一致预期EPS被上修。']:
            with self.subTest(text=text):self.assertIn('consensus_without_authority',codes(text))

    def test_conditional_clause_does_not_exempt_current_levels_or_facts(self):
        for text in ['如果收入改善，一致预期收入为100亿元。','若销量增长，但当前一致预期已经上修。',
                     '一致预期已经上修。','市场预期收入100亿元，假如收入改善我们看多。']:
            self.assertIn('consensus_without_authority',codes(text))
        self.assertNotIn('consensus_without_authority',{x['rule'] for x in lint_text('当前一致预期收入100亿元。',source_kinds=['consensus'])})

    def test_cross_basis_growth_does_not_prove_share_gain(self):
        self.assertIn('management_share_gain_as_fact',codes('公司内生增长3%，高于服务预算预期1.8%，方向上和份额提升一致。'))
