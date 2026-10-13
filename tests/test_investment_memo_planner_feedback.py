"""Real immutable producer transport to planning, without calls during reads."""
import copy
import json
import sqlite3
import unittest
from dataclasses import replace

from dalton_core.investment_memo_planner_feedback import _read, signature
from tests import test_investment_memo_group_maintenance as transport
from tests import test_investment_memo_draft as payloads


class MemoPlannerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.h = transport.MemoGroupMaintenanceTests('test_later_group_after_original_repair_has_one_fixed_permission_and_free_restart')
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.h.bad_groups = set()
        self.unknown = {'memo_q04', 'memo_q11'}
        original = self.h.adapter.execute
        def execute(work, route, profile):
            invocation, envelope = original(work, route, profile)
            if envelope.status == 'succeeded' and work.metadata['purpose'] == 'investment_memo':
                body = json.loads(envelope.outputs['text'])
                for question in body['key_questions']:
                    if question['question_ref'] in self.unknown:
                        question.update(unknown=True, answer='', refs=[], falsifier='读取过去三年年度报告中的回购金额，并核对期间。')
                envelope = replace(envelope, outputs={'text': json.dumps(body, ensure_ascii=False)})
            return invocation, envelope
        self.h.adapter.execute = execute
        self.playbook = {'id': 'playbook:one', 'content_hash': 'a'*64,
            'key_questions': payloads.QUESTIONS,
            'deliverable_templates': {'investment_memo': payloads.TITLES}}
        self.wrapper = self.h.wrapper()

    def produce(self, count=4):
        for i in range(count):
            self.h.outcome(self.wrapper, i)

    def read(self, **overrides):
        options = dict(state_dir=self.h.root, mission=self.h.mission,
            company_ref='company:synthetic-a', config=self.h.model.config, playbook=self.playbook)
        options.update(overrides)
        return _read(None, **options)

    def test_four_formal_groups_produce_exact_unknowns_without_calls_or_fee_writes(self):
        before_signature = signature(self.h.root)
        self.produce()
        self.assertNotEqual(signature(self.h.root), before_signature)
        fees, calls = self.h.fees(), len(self.h.adapter.served)
        result = self.read()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([q['question_ref'] for q in result['items']], ['memo_q04', 'memo_q11'])
        self.assertTrue(all(q['unknown'] and q['answer'] == '' and q['refs'] == [] for q in result['items']))
        self.assertFalse(result['financial_evidence'])
        self.assertFalse(result['retry_authority'])
        self.assertEqual(self.read(), result)
        self.assertEqual(self.h.fees(), fees)
        self.assertEqual(len(self.h.adapter.served), calls)

    def test_partial_macro_is_not_claimed_to_have_passed_all_questions(self):
        self.produce(3)
        self.assertEqual(self.read(), {'status': 'awaiting_groups', 'items': []})

    def test_company_mission_and_playbook_boundaries(self):
        self.produce()
        self.assertEqual(self.read(company_ref='company:synthetic-b')['status'], 'not_recorded')
        self.assertEqual(self.read(mission={**self.h.mission, 'id': 'mission:other'})['status'], 'previous_mission')
        book = copy.deepcopy(self.playbook); book['key_questions'][3] = 'different question'
        with self.assertRaisesRegex(ValueError, 'playbook'):
            self.read(playbook=book)

    def test_formal_hash_mutation_refuses_feedback(self):
        self.produce()
        with sqlite3.connect(self.h.model.scheduler_db) as c:
            names = c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall()
            for name, in names:
                c.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
            c.execute("UPDATE scheduler_formal_results SET content_hash=?", ('0'*64,))
        with self.assertRaises(Exception):
            self.read()

    def test_latest_refused_content_does_not_become_a_verified_gap(self):
        self.produce()
        self.h.bad_old_repair = True
        self.h.old_repair(self.wrapper, 1)
        self.assertEqual(self.read()['status'], 'content_or_source_refused')

    def test_all_answered_has_no_research_gap(self):
        self.unknown = set()
        self.produce()
        self.assertEqual(self.read(), {'status': 'questions_answered', 'items': []})

    def test_gap_survives_company_projection_and_only_semantic_changes_replan(self):
        from dalton_core.research_state import company_state, planning_hash
        from dalton_core.research_planner import _COMPANY_CORE_FIELDS
        self.produce(); result = self.read()
        company = company_state({'company_ref': 'company:synthetic-a'}, memo_question_feedback=result)
        self.assertEqual(company['memo_question_feedback'], result)
        self.assertIn('memo_question_feedback', _COMPANY_CORE_FIELDS)
        state = {'companies': [company]}
        changed = copy.deepcopy(state)
        changed['companies'][0]['memo_question_feedback']['proof_hash'] = 'b'*64
        self.assertEqual(planning_hash(state), planning_hash(changed))
        changed['companies'][0]['memo_question_feedback']['semantic_hash'] = 'b'*64
        self.assertNotEqual(planning_hash(state), planning_hash(changed))
