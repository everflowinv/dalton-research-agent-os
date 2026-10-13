"""A projection contract wakes one normal decision, not repeated paid retries."""
import copy
import unittest
from datetime import datetime
from dalton_core.research_planner import typed_note_directory_contract,TASK_HASH
from dalton_core.research_planner_cli import build_state,attempt_hold,replan_pacing_hold
from dalton_core.research_state import state_content_hash,planning_core_hash
from dalton_core.store import content_hash
from tests import test_research_planner_cli as planner_fixtures
from tests import test_financial_note_history_periods as note_fixtures

class NoteDirectoryWakeTests(unittest.TestCase):
    def fixture(self):
        p=planner_fixtures.PlannerChildTests();p.setUp();self.addCleanup(p.doCleanups)
        m=p.publish_mission();company=m['universe'][0]['company_ref']
        n=note_fixtures.HistoryNotePeriodsTests();self.addCleanup(n.doCleanups)
        t=n.targets(n.filing())[0]
        doc={'company_ref':company,'document_ref':'sec:filing:'+t['accession'],
             'document_version_hash':'4'*64,'authority_hash':'4'*64,
             'authority_ref':'registered-document:sha256:'+'3'*64,
             'readable':True,'source_ref':'source:sec-edgar','operations':['search_registered_document'],
             'evidence_targets':[t]}
        return p,m,company,doc

    def build(self,p,m,company,docs,asof):
        return build_state(p.store,p.missions,m,plans_dir=p.state/'discovery-plans',as_of=asof,
            document_inventory={'status':'available','readable_documents_by_company':{company:docs},
                'document_research_policy':{'max_query_terms':10,'max_query_term_chars':160}})

    def test_complete_directory_changes_normal_identity_once_and_current_replay_is_stable(self):
        p,m,c,doc=self.fixture()
        prior=self.build(p,m,c,[], '2026-09-11T12:00:00+00:00')
        current=self.build(p,m,c,[doc], '2026-09-11T12:01:00+00:00')
        nexttick=self.build(p,m,c,[doc], '2026-09-11T12:02:00+00:00')
        self.assertEqual(prior['planner_task_hash'],TASK_HASH)
        self.assertNotEqual(current['planner_task_hash'],TASK_HASH)
        self.assertEqual(current['content_hash'],nexttick['content_hash'])
        self.assertEqual(planning_core_hash(current),planning_core_hash(nexttick))
        # This is the actual pre-projection existing-plan/ledger key.
        legacy=copy.deepcopy(current);legacy['planner_task_hash']=TASK_HASH
        legacy['content_hash']=state_content_hash(legacy)
        self.assertNotEqual(current['content_hash'],legacy['content_hash'])
        self.assertNotEqual(planning_core_hash(current),planning_core_hash(legacy))
        self.assertIsNone(attempt_hold({legacy['content_hash']:{'at':'2026-09-11T12:00:00+00:00',
            'plan_status':'succeeded'}},current['content_hash'],now=datetime.fromisoformat('2026-09-11T12:01:00+00:00')))
        self.assertIsNone(replan_pacing_hold({'at':'2026-09-11T12:00:00+00:00',
            'planning_hash':'legacy','planning_core_hash':planning_core_hash(legacy)},'current',
            now=datetime.fromisoformat('2026-09-11T12:01:00+00:00'),planning_core_hash=planning_core_hash(current)))

    def test_no_directory_or_incomplete_capability_keeps_existing_task_contract(self):
        p,m,c,doc=self.fixture()
        for changed in (None,'operation','authority','target'):
            rows=[] if changed is None else [copy.deepcopy(doc)]
            if changed=='operation':rows[0]['operations']=[]
            elif changed=='authority':rows[0]['authority_hash']='5'*64
            elif changed=='target':rows[0]['evidence_targets'].append({'schema_version':'invented'})
            state=self.build(p,m,c,rows,'2026-09-11T12:00:00+00:00')
            self.assertIsNone(typed_note_directory_contract(state))
            self.assertEqual(state['planner_task_hash'],TASK_HASH)

if __name__=='__main__':unittest.main()
