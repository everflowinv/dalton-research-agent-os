import unittest
from dalton_core.driver_template import COST_DRIVER_TEMPLATES
from dalton_core.research_gap_display import display_metadata_text, gap_display_text

class GapDisplayTests(unittest.TestCase):
 def test_real_ibm_cost_gap_is_fully_chinese(self):
  raw='档案 supply_and_cost 未覆盖成本模板的「人均产出」（revenue_per_employee）：What revenue and delivery cost does each employee support? 期望证据种类：company_figure, market_proxy。'
  self.assertEqual(gap_display_text(raw),'公司档案的“供给与成本”章节尚未覆盖“人均产出”：每名员工对应多少收入和交付成本？需要的证据：公司财务数据、市场替代指标。')
 def test_every_registry_key_and_question_has_a_closed_translation(self):
  for template in COST_DRIVER_TEMPLATES['templates']:
   for slot in template['cost_slots']:
    raw=f"档案 supply_and_cost 未覆盖成本模板的「{slot['label']}」（{slot['slot_id']}）：{slot['question']} 期望证据种类：{', '.join(slot['evidence_kinds'])}。"
    shown=gap_display_text(raw)
    self.assertNotIn(slot['slot_id'],shown);self.assertNotIn(slot['question'],shown)
 def test_prefixes_and_known_internal_tag_error_are_readable(self):
  self.assertEqual(gap_display_text('delivery_cost：缺少拆分'),'交付成本：缺少拆分')
  self.assertEqual(gap_display_text('revenue_per_employee / 暂无数据'),'人均产出 / 暂无数据')
  self.assertEqual(gap_display_text('模型引用了不存在的标签：N1、N2'),'模型引用了未纳入依据的内部标签：N1、N2。')
 def test_registered_metadata_tokens_are_replaced_inside_prose(self):
  shown=display_metadata_text('fixed_cost与variable_cost缺数据；分类insufficient_data；参考market_proxy；普通 IBM GenAI 不变。')
  self.assertEqual(shown,'固定成本与变动成本缺数据；分类资料不足；参考市场替代指标；普通 IBM GenAI 不变。')
  for template in COST_DRIVER_TEMPLATES['templates']:
   for slot in template['cost_slots']:
    self.assertNotIn(slot['slot_id'],display_metadata_text('观察 '+slot['slot_id']+' 的变化'))
 def test_unknown_and_existing_chinese_are_preserved_exactly(self):
  for raw in ('仍需管理层说明。','IBM-specific proper name','unknown_metric: keep raw','T. Rowe Price','管理层说："Revenue and price improved."',None):
   self.assertEqual(gap_display_text(raw),'' if raw is None else raw)
