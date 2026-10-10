import copy
import shutil
import unittest
from tests.test_cockpit_v2_migration import fixtures, run, H1

@unittest.skipIf(shutil.which('node') is None, 'node unavailable')
class WorkspaceGoalDisplayTests(unittest.TestCase):
    def test_legacy_generated_draft_is_localized_without_changing_publish_authority(self):
        draft={'draft_id':'draft:legacy','draft_hash':H1,'draft':{'schema_version':'workspace-first-mission-draft-0.1',
            'setup_state':'ready_for_confirmation','mission_body':{'title':'研究设备','objective':'比较公司',
            'universe':[{'ticker':'ASML'}],'budget':{'max_daily_cost_usd':3},
            'source_plan':[{'source_ref':'source:prior-research'}],
            'research_questions':['What evidence would confirm or reject this objective: 研究设备',
                'Which causal drivers explain the differences across the selected companies?',
                'What observable events would falsify the leading view?','Owner asks: Which customer has pricing power?']}}}
        original=copy.deepcopy(draft)
        out=run({'s':'#/settings'},drafts={'goal:draft':draft},fixtures=fixtures())
        text=out['results']['s']['text']
        self.assertIn('哪些证据能够支持或推翻这一研究目标：研究设备',text)
        self.assertIn('哪些业务驱动因素能够解释所选公司的差异？',text)
        self.assertIn('哪些可观察事件会推翻当前主要判断？',text)
        self.assertIn('历史研究资料',text)
        self.assertNotIn('source:prior-research',text)
        self.assertNotIn('What evidence would confirm',text)
        self.assertIn('Owner asks: Which customer has pricing power?',text)
        self.assertEqual(draft,original)
        self.assertEqual(out['jobs'],[])
