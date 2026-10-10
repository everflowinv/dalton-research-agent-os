"""Technical upkeep stays visible without becoming an analyst decision."""
import copy
import shutil
import unittest
from tests import test_cockpit_v2_shell as fixture

@unittest.skipUnless(shutil.which('node'),'node unavailable')
class OverviewAnalystResponsibilityTests(unittest.TestCase):
    def test_shared_counts_respect_roles_but_keep_real_spend_decisions(self):
        admin={'kind':'controlled_recovery','actionable':True,'responsibility':'administrator'}
        background={'kind':'gate_decision','actionable':True,'responsibility':'background'}
        optional={'kind':'memo_decision','actionable':True,'responsibility':'optional_feedback'}
        budget={'kind':'material_budget_exceeded','actionable':True,'detail':{'approve_usd':'0.50','approve_calls':3}}
        gate={'kind':'gate_decision','actionable':True}
        r=fixture.node('call',{'module':'pages/task_kinds.js','calls':[
            ['analystAction',admin],['analystAction',background],['analystAction',optional],
            ['analystAction',{**gate,'decision_required':False}],['analystAction',budget],['analystAction',gate],
            ['analystAction',{'kind':'model_cooldown','actionable':True}],
            ['analystAction',{'kind':'new_formal_decision','decision_required':True,'actionable':True}],
            ['actionCount',[admin,background,optional,budget,gate],[{'decision_required':False},{'responsibility':'administrator'},{'kind':'initial_screen'}]],
        ]})
        self.assertEqual([x['ok'] for x in r],[False,False,False,False,True,True,False,True,3])

    def render(self,items,approvals=None):
        data=copy.deepcopy(fixture.FIXTURES)
        data['/v1/cockpit/needs-human']={'as_of':'2026-01-01T00:00:00+00:00','items':items}
        data['/v1/cockpit/approvals']={'count':0,'items':approvals or []}
        return fixture.node('pages',{'fixtures':data,'render':{'o':'#/overview','t':'#/tasks'}})

    def test_background_and_administrator_items_remain_visible_without_decision_headline(self):
        out=self.render([
            {'kind':'controlled_recovery','ref':'synthetic:admin','title':'管理员检查核验接口','actionable':True,'responsibility':'administrator','urgency':2,'detail':{}},
            {'kind':'publication_parked','ref':'synthetic:background','title':'系统继续整理译稿','actionable':False,'responsibility':'background','urgency':4,'detail':{}},
            {'kind':'governance_record','ref':'synthetic:info','title':'已转后台的信息','actionable':True,'decision_required':False,'urgency':4,'detail':{}},
        ])
        overview=out['results']['o']['text'];tasks=out['results']['t']['text']
        self.assertNotIn('重点决策待确认',overview)
        self.assertIn('管理员检查核验接口',tasks)
        self.assertIn('系统继续整理译稿',tasks)
        self.assertIn('管理员诊断',tasks)
        self.assertNotIn('项需要处理',tasks)
        self.assertEqual(out['posted'],[])

    def test_budget_choice_has_correct_action_not_technical_help(self):
        budget={'kind':'material_budget_exceeded','ref':'material-budget:synthetic','title':'材料获取额度待决定',
                'actionable':True,'urgency':3,'detail':{'scope':'request','approve_usd':'0.50','approve_calls':3}}
        out=self.render([budget])
        overview=out['results']['o']['text'];tasks=out['results']['t']['text']
        self.assertIn('1 项重点决策待确认',overview)
        self.assertIn('决定是否追加材料获取额度 0.50 美元',overview)
        self.assertIn('也可以保留现有额度',overview)
        self.assertNotIn('请交给管理员处理',overview)
        self.assertIn('1 项需要处理',tasks)
        self.assertEqual(out['posted'],[])
