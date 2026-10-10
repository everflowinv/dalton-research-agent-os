"""Synthetic semantic boundaries; no paid output or private company data."""
import copy
import unittest
from dalton_core import research_quality_score as score
from dalton_core.mission_deliverable import number_display


def number(text='14.71', **kw):
    return {'text':text,'number_ref':'pack-json:/our_model/years/0/eps/value',
            'metric':'稀释EPS','period':'FY2027','unit':'美元/股',**kw}


def section(body, numbers=(), title='结论与观点', **kw):
    return {'title':title,'body':body,'numbers':list(numbers),'claim_refs':[],**kw}


def art(*sections):
    return score.artefact(artefact_kind='initial_screen',ref='synthetic:semantic',hash='a'*64,
                         sections=sections,subject_ref='company:synthetic')


class ForwardIdentityTests(unittest.TestCase):
    def test_filed_fy2026_is_not_inferred_forecast_by_calendar(self):
        a=art(section('FY2026 收入 742 亿美元。',[number('742',period='FY2026',period_kind='actual',metric='收入',unit='亿美元',number_ref='pack-json:/numbers/filed/revenue')]))
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'pass')

    def test_unbound_calendar_year_does_not_establish_kind(self):
        self.assertEqual(score.check_actual_vs_estimate_labelling(art(section('FY2035 收入 742 亿美元。')), {})['status'],'pass')

    def test_explicit_estimate_suffix_requires_attribution_in_any_year(self):
        self.assertEqual(score.check_actual_vs_estimate_labelling(art(section('FY2021E EPS 14.71 美元。')), {})['status'],'fail')

    def test_bound_forecast_without_suffix_still_requires_attribution(self):
        a=art(section('FY2027 EPS 14.71 美元。',[number(estimate={'assumption_ref':'pack-json:/our_model/years/0/eps/value'},number_ref=None)]))
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'fail')
        a['sections'][0]['body']='我们预测 FY2027 EPS 14.71 美元。'
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'pass')

    def test_historical_beat_is_not_a_forecast_marker(self):
        self.assertEqual(score.check_actual_vs_estimate_labelling(art(section('股价上涨6%，订单超预期。')), {})['status'],'pass')

    def test_an_equation_multiple_does_not_make_its_price_operand_a_forecast(self):
        n=number('观察价220.65美元 =15.0倍×14.71美元',number_ref=None,
                 estimate={'assumption_ref':'assumption:multiple'},metric='目标市盈率',unit='倍')
        a=art(section('现价209.06美元，相对观察价220.65美元的空间约5.5%。',[n]))
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'pass')


class NumericProofTests(unittest.TestCase):
    def values(self):
        return [number('220.65',metric='观察价',unit='美元',number_ref='pack-json:/watch'),
                number('209.06',metric='现价',unit='美元',number_ref='pack-json:/price'),
                {'text':'5.5%','derivation':{'expression':'(220.65/209.06-1)*100','result':'5.5'},'metric':'隐含空间','period':'截至2026-10-10','unit':'%'}]

    def test_same_sentence_equation_constant_is_sourced(self):
        s=section('隐含空间 =(220.65/209.06−1)×100，约 5.5%。',self.values())
        self.assertEqual(score._sourced_sentences(s),[s['body']])

    def test_constant_does_not_source_another_sentence(self):
        s=section('隐含空间 =(220.65/209.06−1)×100，约 5.5%。收入100亿美元。',self.values())
        self.assertEqual(len(score._sourced_sentences(s)),1)

    def test_wrong_equation_is_not_proof(self):
        s=section('隐含空间 =(220.65/209.06−1)×100，约 7.5%。',self.values())
        self.assertEqual(score._sourced_sentences(s),[])

    def test_circular_or_unproved_peer_values_are_not_sources(self):
        s=section('预测收入 550。',[{'text':'550','derivation':{'expression':'500*1.1','result':'550'}}])
        self.assertEqual(score._sourced_sentences(s),[])

    def test_display_projection_and_replay(self):
        n=number('16.4',metric='市盈率',unit='倍',number_ref='pack-json:/valuation/pe',source_text='16.4232',display=number_display('16.4232','16.4'))
        a=art(section('市盈率16.4倍。',[n]))
        self.assertIn('display',a['sections'][0]['numbers'][0])
        self.assertIn('source_text',a['sections'][0]['numbers'][0])
        self.assertEqual(len(score._sourced_sentences(a['sections'][0])),1)
        a['sections'][0]['numbers'][0]['display']['source_value']='99'
        self.assertEqual(score._sourced_sentences(a['sections'][0]),[])

    def test_uncited_value_is_not_sourced_by_another_citation(self):
        s=section('市盈率16.4倍。收入99亿美元。',[number('16.4',unit='倍',metric='市盈率')])
        self.assertEqual(len(score._sourced_sentences(s)),1)

    def test_long_implied_equation_is_same_sentence_not_distance_limited(self):
        body='市盈率15倍，EPS14.71；观察价220.65，现价209.06。隐含空间 =(220.65/209.06−1)×100，约5.5%。历史市盈率中位区间。'
        a=art(section(body,self.values(),title='估值与观察价'))
        result=score.check_valuation_six_elements(a,{})
        self.assertNotIn('missing_implied_return',[x['code'] for x in result['findings']])

    def test_unrelated_sentence_or_invalid_result_cannot_supply_return(self):
        for body in ('隐含空间仍待观察。另一项收益5.5%。','隐含空间 =(220.65/209.06−1)×100，约7.5%。'):
            with self.subTest(body=body):
                self.assertFalse(score._bound_implied_return(section(body,self.values())))

    def test_return_with_wrong_unit_is_not_a_percent_proof(self):
        values=self.values();values[-1]['unit']='美元'
        self.assertFalse(score._bound_implied_return(section('隐含空间=(220.65/209.06-1)*100，约5.5%。',values)))


class FactIdentityTests(unittest.TestCase):
    def test_same_bare_digits_different_unit_period_metric_are_distinct(self):
        rows=[section('过去3个月。',[number('3',metric='持有期',unit='月',period='历史',number_ref='pack-json:/holding')],title='A'),
              section('增长3%。',[number('3%',metric='收入增长',unit='%',number_ref='pack-json:/growth')],title='B'),
              section('FY2028利润率3%。',[number('3%',metric='利润率',unit='%',period='FY2028',number_ref='pack-json:/margin')],title='C')]
        self.assertEqual(score.check_fact_repetition(art(*rows),{})['status'],'pass')

    def test_exact_model_eps_in_three_sections_still_fails(self):
        a=art(*(section('FY2027 EPS14.71美元。',[number()],title=t) for t in ('A','B','C')))
        r=score.check_fact_repetition(a,{})
        self.assertEqual(r['status'],'fail');self.assertEqual(r['count'],1)

    def test_same_binding_different_label_or_rounding_does_not_evade_limit(self):
        displayed=number('14.7',metric='按一位小数显示的EPS',source_text='14.71',display=number_display('14.71','14.7'))
        a=art(section('EPS14.71美元。',[number()],title='A'),section('EPS14.71美元。',[number()],title='B'),section('EPS14.7美元。',[displayed],title='C'))
        self.assertEqual(score.check_fact_repetition(a,{})['status'],'fail')

    def test_equal_unknown_digits_do_not_prove_equal_facts(self):
        a=art(*(section('收入3%。',[{'text':'3%','passage_ref':'doc-passage:synthetic'}],title=t) for t in ('A','B','C')))
        self.assertEqual(score.check_fact_repetition(a,{})['status'],'pass')
        self.assertEqual(score.check_number_metadata(a,{})['status'],'fail')

    def test_one_passage_with_multiple_metrics_is_not_one_fact(self):
        a=art(*(section(body,[number(text,number_ref=None,passage_ref='doc-passage:synthetic',metric=metric,unit=unit)],title=title)
               for title,body,text,metric,unit in [('A','收入3亿美元。','3','收入','亿美元'),('B','利润率3%。','3%','利润率','%'),('C','增长3%。','3%','增长','%')]))
        self.assertEqual(score.check_fact_repetition(a,{})['status'],'pass')


class OneOffAndIssuerTests(unittest.TestCase):
    def test_pricing_competition_is_not_accounting_normalisation(self):
        a=art(section('尚不清楚定价是否一次性竞争行为。',title='附录'))
        self.assertEqual(score.check_one_off_normalisation(a,{})['status'],'pass')

    def test_accounting_oneoff_without_amount_still_fails(self):
        for body in ('上一季度有一次性重组费用。','一次性利润推高了盈利。'):
            self.assertEqual(score.check_one_off_normalisation(art(section(body)),{})['status'],'fail')

    def test_unrelated_normalisation_sentence_cannot_excuse_charge(self):
        a=art(section('调整后利润保持稳定。一次性重组费用提高。'))
        self.assertEqual(score.check_one_off_normalisation(a,{})['status'],'fail')
        a=art(section('剔除一次性重组费用后利润保持稳定。'))
        self.assertEqual(score.check_one_off_normalisation(a,{})['status'],'pass')

    def test_normalisation_can_explicitly_continue_in_the_next_sentence(self):
        for body in ('上一季度有一次性重组费用。按调整后口径剔除后，利润率不变。',
                     '本季确认一次性重组费用。该费用已剔除，按正常化营业利润展示。'):
            with self.subTest(body=body):
                self.assertEqual(score.check_one_off_normalisation(art(section(body)),{})['status'],'pass')

    def test_continuation_cannot_cross_paragraph_or_another_item(self):
        for body in ('一次性重组费用推高成本。\n\n按调整后口径剔除后，利润率不变。',
                     '一次性重组费用推高成本。需求保持稳定。按调整后口径剔除后，利润率不变。',
                     '一次性重组费用推高成本。按调整后口径剔除减值后，利润率不变。'):
            with self.subTest(body=body):
                self.assertEqual(score.check_one_off_normalisation(art(section(body)),{})['status'],'fail')

    def test_adjusted_different_metric_in_the_same_sentence_cannot_excuse_gain(self):
        a=art(section('本季一次性证券收益100万美元，同期调整后营收1000万美元。'))
        self.assertEqual(score.check_one_off_normalisation(a,{})['status'],'fail')

    def test_accounting_context_continues_with_an_explicit_anaphoric_subject(self):
        a=art(section('本季利润增长20%。其中一次性项目贡献10个百分点。'))
        self.assertEqual(score.check_one_off_normalisation(a,{})['status'],'fail')

    def test_accounting_disclosure_question_is_not_an_asserted_oneoff(self):
        a=art(section('是否存在一次性税项影响？公司未披露，需获取税务明细。'))
        self.assertEqual(score.check_one_off_normalisation(a,{})['status'],'pass')

    def test_external_multicompany_material_is_unknown_not_false_attribution(self):
        a=art(section('专家讨论行业。',source_refs=[{'ref':'doc:expert','source_kind':'expert_call','company_ref':'company:other','company_refs':['company:synthetic','company:other']}]))
        r=score.check_issuer_attribution(a,{})
        self.assertEqual(r['status'],'fail');self.assertEqual(r['findings'][0]['code'],'issuer_binding_unverified')
        a['sections'][0]['source_refs'][0]['company_ref']='company:synthetic'
        self.assertEqual(score.check_issuer_attribution(a,{})['status'],'fail')
        a['sections'][0]['source_refs'][0].pop('company_ref')
        self.assertEqual(score.check_issuer_attribution(a,{})['status'],'fail')

    def test_foreign_issuer_authority_remains_mismatch(self):
        a=art(section('公司申报利润。',source_refs=[{'ref':'doc:filing','source_kind':'release','company_ref':'company:other'}]))
        r=score.check_issuer_attribution(a,{})
        self.assertEqual(r['status'],'fail');self.assertEqual(r['findings'][0]['code'],'foreign_issuer_material')


class IndependentCounterexampleTests(unittest.TestCase):
    def test_the_exact_forecast_only_annual_reader_path_requires_a_label(self):
        a=art(section('FY2027 EPS14.71美元。',[number()]))
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'fail')
        a['sections'][0]['numbers'][0]['number_ref']='pack-json:/our_model/history/0/eps/value'
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'pass')

    def test_unbound_fiscal_year_stays_unknown_and_the_full_rubric_refuses_numbers(self):
        a=art(section('FY2035收入742亿美元。'))
        self.assertEqual(score.check_actual_vs_estimate_labelling(a,{})['status'],'pass')
        self.assertIn('未凭年份',score.check_actual_vs_estimate_labelling(a,{})['detail'])
        self.assertEqual(score.check_numbers_without_refs(a,{})['status'],'fail')

    def test_unknown_identity_is_not_merged_and_metadata_refuses_it(self):
        a=art(*(section('收入3%。',[{'text':'3%','passage_ref':'doc-passage:synthetic'}],title=t) for t in ('A','B','C')))
        self.assertEqual(score.check_fact_repetition(a,{})['status'],'pass')
        self.assertIn('缺身份',score.check_fact_repetition(a,{})['detail'])
        self.assertEqual(score.check_number_metadata(a,{})['status'],'fail')

    def test_replayed_derivation_does_not_establish_its_period(self):
        n={'text':'15.71','derivation':{'expression':'14.71+1','result':'15.71'},'metric':'EPS加项后','unit':'美元/股'}
        a=art(*(section('EPS加项后15.71美元。',[number(),copy.deepcopy(n)],title=t) for t in ('A','B','C')))
        self.assertEqual(score.check_fact_repetition(a,{})['status'],'pass')
        r=score.check_number_metadata(a,{})
        self.assertEqual(r['status'],'fail');self.assertEqual(r['count'],3)
        self.assertTrue(all(x['code']=='derived_period_unverified' for x in r['findings']))

    def test_distinct_claim_refs_are_not_merged_without_canonical_authority(self):
        a=art(*(section('FY2027收入100亿美元。',[number('100',number_ref=None,claim_version_ref=f'claim-version:synthetic-{i}',metric='收入',unit='亿美元')],title=t) for i,t in enumerate(('A','B','C'))))
        self.assertEqual(score.check_fact_repetition(a,{})['status'],'pass')

if __name__=='__main__':unittest.main()
