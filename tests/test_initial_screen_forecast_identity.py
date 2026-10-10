import copy
import unittest
from datetime import date
from decimal import Decimal
from dalton_core.initial_screen import validate_worksheet, build_worksheet_prompt, _pack, _forecast_identity
from dalton_core.valuation_forward import our_eps
from tests.synthetic_company import material_pack, strl_worksheet, COMPANY

def codes(sheet, pack):return {row['code'] for row in validate_worksheet(sheet,pack)[1]}

class IdentityTests(unittest.TestCase):
    def test_eps_requires_a_common_explicit_definition_and_keeps_known_sides(self):
        p=_pack(material_pack());s=strl_worksheet()
        self.assertEqual(codes(s,p),set())
        p['json']['numbers']['consensus']['eps_basis']={'vendor':'adjusted'}
        self.assertIn('forecast_eps_basis_not_comparable',codes(s,p))
        for row in s['forecasts']:
            if row['metric']=='eps':row.update(status='unavailable',diff_pct=None,diff_expression=None,reason='EPS口径不可比：GAAP与调整后定义没有来源绑定的调节桥')
        self.assertEqual(codes(s,p),set())
        s['forecasts'][1]['ours']='999999'
        self.assertIn('forecast_ours_not_in_tag',codes(s,p))
        s=strl_worksheet();p=_pack(material_pack())
        del p['json']['our_model']['eps_basis']
        self.assertIn('forecast_eps_basis_not_comparable',codes(s,p))
        del p['json']['numbers']['consensus']['eps_basis']
        self.assertIn('forecast_eps_basis_not_comparable',codes(s,p))

    def test_adjusted_label_alone_or_unbound_unavailable_is_not_a_comparison(self):
        p=_pack(material_pack());s=strl_worksheet()
        p['json']['our_model']['eps_basis']={'kind':'adjusted'}
        p['json']['numbers']['consensus']['eps_basis']={'vendor':'adjusted'}
        self.assertIn('forecast_eps_basis_not_comparable',codes(s,p))
        for root in [p['json']['our_model'],p['json']['numbers']['consensus']]:root['eps_basis']['definition_ref']='eps-definition:synthetic-common-adjustments'
        self.assertEqual(codes(s,p),set())
        p['json']['numbers']['consensus']['eps_basis']['definition_ref']='eps-definition:other-adjustments'
        self.assertIn('forecast_eps_basis_not_comparable',codes(s,p))
        p=_pack(material_pack())
        s['forecasts'][0].update(status='unavailable',diff_pct=None,diff_expression=None,reason='EPS口径不同')
        self.assertIn('forecast_unavailable_invalid',codes(s,p))

    def test_missing_consensus_preserves_and_verifies_the_known_model_forecast(self):
        s=strl_worksheet()
        s['forecasts'][0].update(status='unavailable', consensus=None,
            consensus_tag=None, diff_pct=None, diff_expression=None,
            reason='数据商尚无该年度一致预期')
        self.assertEqual(codes(s,material_pack()),set())
        s['forecasts'][0]['ours']='999999'
        self.assertIn('forecast_ours_not_in_tag',codes(s,material_pack()))
        s['forecasts'][0]['ours']='5050'
        s['forecasts'][0]['diff_pct']='0'
        self.assertIn('forecast_unavailable_invalid',codes(s,material_pack()))

    def test_comparison_copy_requires_exact_canonical_consensus_value_and_period(self):
        p=_pack(material_pack())
        p['json']['our_model']['years']=[{'label':'FY2027','eps_vs_consensus':{'theirs':'4.50'}}]
        source=p['json']['numbers']['consensus']['periods'][1]
        source.update(status='available');source['eps']['currency']='USD'
        entry={'kind':'number','source_kind':'consensus','period':'FY2027',
               'pointer':'/our_model/years/0/eps_vs_consensus/theirs'}
        identity=_forecast_identity(p,entry,ours=False)
        self.assertEqual((identity['period'],identity['metric'],identity['unit']),('FY2027','eps','USD/shares'))
        self.assertIsNone(_forecast_identity(p,entry,ours=True))
        self.assertIsNone(_forecast_identity(p,{**entry,'period':'FY2027Q1'},ours=False))
        p['json']['our_model']['years'][0]['eps_vs_consensus']['theirs']='4.51'
        self.assertIsNone(_forecast_identity(p,entry,ours=False))

    def test_debate_tag_and_unheld_assumption_never_supply_watch_eps_or_comparison(self):
        p=_pack(material_pack());s=strl_worksheet()
        p['tags']['O2']={'ref':'debate-map-version:synthetic','text':'FY2027 EPS 5.00','source_kind':'ours'}
        for tag in ('O2','assumption:synthetic-eps'):
            s['watch_price']['eps_tag']=tag;s['forecasts'][3]['ours_tag']=tag
            self.assertIn('watch_price_eps_not_bound_to_held_model',codes(s,p))
            self.assertIn('forecast_ours_not_bound_to_model',codes(s,p))

    def test_same_number_in_the_wrong_period_metric_or_unit_is_not_comparable(self):
        p=_pack(material_pack());s=strl_worksheet()
        p['json']['numbers']['consensus']['periods'][1]['label']='FY2027Q1'
        self.assertIn('forecast_consensus_period_or_metric_mismatch',codes(s,p))
        p=_pack(material_pack());s['forecasts'][3]['consensus_tag']='N3'
        self.assertIn('forecast_consensus_period_or_metric_mismatch',codes(s,p))
        p=_pack(material_pack());s=strl_worksheet()
        p['json']['number_tags']['N19']['transform']='÷1e9'
        self.assertIn('forecast_unit_or_scale_mismatch',codes(s,p))
        p=_pack(material_pack());p['json']['our_model']['model']={}
        self.assertIn('forecast_ours_identity_missing',codes(strl_worksheet(),p))

    def test_watch_year_and_percent_formula_are_independently_checked(self):
        s=strl_worksheet();s['watch_price']['multiple_basis']='FY2028 P/E'
        s['forecasts'][0].update(diff_expression='(5050+4950)*100',diff_pct='1000000')
        c=codes(s,material_pack())
        self.assertIn('watch_price_eps_period_mismatch',c)
        self.assertIn('forecast_diff_not_standard_comparison',c)

    def test_missing_forecast_rows_remain_missing_without_fabricated_percent(self):
        s=strl_worksheet();s['watch_price']={'status':'unavailable','reason':'没有完整年度EPS','reference_tags':['N4']}
        for row in s['forecasts']:row.update(status='unavailable',ours=None,diff_pct=None,diff_expression=None,reason='年度对应模型或一致预期尚未取得')
        self.assertEqual(codes(s,material_pack()),set())
        s['forecasts'][0]['diff_pct']='0'
        self.assertIn('forecast_unavailable_invalid',codes(s,material_pack()))
        prompt=build_worksheet_prompt(company={'company_ref':COMPANY,'ticker':'SYNW'},pack=material_pack())
        self.assertIn('Never compare annual with quarterly',prompt)
        self.assertIn('No unsourced assumption EPS',prompt)

    def test_ntm_eps_uses_day_weighted_shares_not_sum_of_quarterly_eps(self):
        ends=['2027-03-31','2027-06-30','2027-09-30','2027-12-31'];starts=['2027-01-01','2027-04-01','2027-07-01','2027-10-01']
        shares=[100,200,100,200]
        p={'status':'ok','period':{'forecast':ends},'columns':[{'start':s,'end':e} for s,e in zip(starts,ends)],'rows':[
            {'role':'diluted_eps','label':'EPS','unit_kind':'per_share','cells':[{'end':e,'value':str(Decimal(100)/n)} for e,n in zip(ends,shares)]},
            {'role':'diluted_eps_numerator','unit_kind':'amount','cells':[{'end':e,'value':'100'} for e in ends]},
            {'role':'diluted_weighted_average_shares','unit_kind':'shares','cells':[{'end':e,'value':str(n)} for e,n in zip(ends,shares)]}]}
        got=our_eps(p,'company:synthetic')
        expected=Decimal(400)/(sum(Decimal(n)*Decimal((date.fromisoformat(e)-date.fromisoformat(s)).days+1) for n,s,e in zip(shares,starts,ends))/Decimal(365))
        self.assertEqual(Decimal(got['value']),expected.quantize(Decimal('.01')))
        self.assertNotEqual(Decimal(got['value']),Decimal(3))
        p['columns'][1]['start']='2027-04-02'
        self.assertEqual(our_eps(p,'company:synthetic')['status'],'unavailable')

    def test_ntm_exact_fiscal_year_uses_the_same_bound_annual_eps(self):
        from dalton_core.store import content_hash
        ends=['2027-03-31','2027-06-30','2027-09-30','2027-12-31']
        p={'status':'ok','version_ref':'forecast-model-version:synthetic:1','period':{'forecast':ends},
           'rows':[{'role':'diluted_eps','unit_kind':'per_share','cells':[{'end':e,'value':'1'} for e in ends]}]}
        annual={'status':'computed','value':'14.71','unit':'usd_per_share',
                'numerator':{'status':'computed','value':'1471'},
                'denominator':{'status':'computed','value':'100','method':'annual_growth_from_filed','as_of':'2025-12-31','steps':2,'growth_rate':'-.01'}}
        projection={'company_ref':'company:synthetic','model_version_ref':p['version_ref'],'projection_ref':'annual-projection:synthetic',
                    'periods':[{'label':'FY2027E','quarter_ends':ends,'forecast_eps':annual}]}
        projection['content_hash']=content_hash(projection);p['annual_projection']=projection
        got=our_eps(p,'company:synthetic')
        self.assertEqual(got['value'],'14.71')
        self.assertEqual(got['method'],'ntm_exact_fiscal_year_annual_projection')
        self.assertEqual(got['share_steps'],2)
        projection['periods'][0]['forecast_eps']['value']='99'
        self.assertEqual(our_eps(p,'company:synthetic')['status'],'unavailable')
