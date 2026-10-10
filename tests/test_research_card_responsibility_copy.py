"""Research cards name the actual handler without creating another decision."""
import json
import shutil
import unittest
from unittest.mock import patch
from dalton_core.needs_human import _numeric_contract_system_item


class ResearchCardCopyTests(unittest.TestCase):
    def card(self, **state):
        with patch('dalton_core.directed_numeric_recovery.read_numeric_contract_hold',
                   return_value={'automatic_window_available':False, **state}):
            return _numeric_contract_system_item(object(), 'mission_document_research',
                {'admission_ref':'mission-document-research-admission:synthetic',
                 'reason':'numeric_maintenance_budget_waiting'},
                at='2026-10-10T00:00:00+00:00',path='<state-dir>/holds.json',subject={'company':'SYN'})

    def test_budget_wait_is_system_owned_and_conditional_not_a_next_day_promise(self):
        c=self.card(budget_waiting=True)
        self.assertEqual(c['responsibility'],'background')
        self.assertFalse(c['actionable'])
        self.assertIn('系统处理',c['title'])
        self.assertIn('下一预算周期',c['action'])
        self.assertIn('额度足够时继续',c['action'])
        self.assertIn('不会增加已设定的费用额度',c['why_blocked'])
        self.assertEqual(c['detail']['owner_doors'],[])
        self.assertFalse(any(x in c['action'] for x in ('明天','次日','已通知')))

    def test_exhausted_format_repair_requires_admin_check_not_an_owner_decision(self):
        c=self.card(technical_schema_candidate=True)
        self.assertEqual(c['responsibility'],'administrator')
        self.assertIn('管理员检查',c['title'])
        self.assertIn('核验接口要求的回复格式',c['why_blocked'])
        self.assertNotIn('契约绑定',c['why_blocked'])
        self.assertFalse(c['actionable'])
        self.assertEqual(c['detail']['commands'],[])

    def test_verified_automatic_window_remains_system_owned(self):
        c=self.card(automatic_window_available=True)
        self.assertEqual(c['responsibility'],'background')
        self.assertIn('系统处理',c['title'])
        self.assertFalse(c['actionable'])
        self.assertEqual(c['detail']['owner_doors'],[])

    @unittest.skipUnless(shutil.which('node'), 'Node unavailable')
    def test_actual_v2_keeps_background_and_admin_outside_owner_decisions(self):
        from tests import test_cockpit_v2_shell as fixture
        fixtures=json.loads(json.dumps(fixture.FIXTURES))
        cards=[self.card(budget_waiting=True),self.card(technical_schema_candidate=True)]
        cards[1]['ref']+='-admin'
        fixtures['/v1/cockpit/needs-human']={'items':cards,'as_of':'2026-10-10T00:00:00+00:00'}
        fixtures['/v1/cockpit/approvals']={'items':[],'count':0}
        out=fixture.node('pages',{'fixtures':fixtures,'render':{'t':'#/tasks'}})
        text=out['results']['t']['text']
        for label in ('后台推进','后台跟进','管理员诊断','管理员维护','后续进展','系统处理','管理员检查'):
            self.assertIn(label,text)
        self.assertNotIn('你要做的',text)
        self.assertNotIn('契约绑定',text)
        self.assertEqual(out['posted'],[])
