"""Lossless research obligations with bounded followup audit metadata."""
import copy
import unittest
from dalton_core.research_planner import (
    build_prompt, followup_context_matches_prompt, project_followup_context,
)
from dalton_core.store import canonical_json, content_hash
from tests import test_reflection_planner_followup as reflection_fixture


class FollowupContextProjectionTests(unittest.TestCase):
    def context(self):
        grant = {'mission_version_ref': 'mission:synthetic:1', 'may_write': ['research_task'],
                 'budget': {'max_daily_cost_usd': 3, 'max_daily_paid_calls': 2}}
        scope = {'company_ref': 'company:synthetic', 'window': {'start': '2026-10-01', 'end': '2026-10-08'}}
        candidate = {'question': 'Which filed obligation needs verification?',
                     'because': 'An existing exact question is unanswered.', 'refs': ['question:1']}
        suggestions = [{'candidate': copy.deepcopy(candidate), 'backlog_question_refs': ['question:1'],
                        'request_ref': f'request:{i}', 'request_hash': content_hash(['request', i]),
                        'reflection_ref': f'reflection:{i}', 'reflection_hash': content_hash(['reflection', i])}
                       for i in range(9)]
        groups = [{'group': {'group_ref': f'group:{n}', 'content_hash': content_hash(['group', n]),
                            'company_ref': 'company:synthetic', 'route': 'research_dispatch', 'scope': scope,
                            'execution_grant': grant, 'requests': [{'request_ref': f'request:{i}', 'hash': content_hash(i)} for i in range(9)]},
                   'suggestions': copy.deepcopy(suggestions)} for n in range(2)]
        question = {'question_ref': 'question:1', 'state': 'open', 'status': 'held',
                    'head': {'id': 'question-version:1', 'content_hash': 'a' * 64,
                             'company_ref': 'company:synthetic', 'question': candidate['question'],
                             'answer_criteria': 'Exact filed amount, period and currency.', 'source_refs': ['source:sec-edgar']},
                    'answer_bindings': [], 'waiting_material_request': {
                        'request_ref': 'material-request:1', 'reason': 'Wait for source evidence.',
                        'content_hash': 'b' * 64, 'question_version_ref': 'question-version:1', 'question_version_hash': 'a' * 64}}
        body = {'groups': groups, 'backlog_questions': [question], 'mission_ref': 'mission:synthetic:1'}
        return {**body, 'content_hash': content_hash(body)}

    def test_duplicates_merge_only_exact_candidate_and_question_refs_and_keep_all_sources(self):
        context = self.context(); before = copy.deepcopy(context)
        context['groups'][0]['suggestions'].append({**context['groups'][0]['suggestions'][0], 'backlog_question_refs': ['question:other']})
        projected = project_followup_context(context)
        rows = projected['groups'][0]['suggestions']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['candidate'], before['groups'][0]['suggestions'][0]['candidate'])
        self.assertEqual(len(rows[0]['source_bindings']), 9)
        self.assertEqual({r['request_ref'] for r in rows[0]['source_bindings']}, {f'request:{n}' for n in range(9)})
        self.assertEqual(rows[1]['backlog_question_refs'], ['question:other'])
        self.assertTrue(followup_context_matches_prompt(projected, context))

    def test_shared_permissions_scope_and_all_open_question_semantics_are_preserved(self):
        context = self.context(); projected = project_followup_context(context, summarize_sources=True)
        self.assertEqual(len(projected['execution_grants']), 1)
        self.assertEqual(len(projected['scopes']), 1)
        for original, new in zip(context['groups'], projected['groups']):
            self.assertEqual(projected['execution_grants'][new['group']['execution_grant_ref']], original['group']['execution_grant'])
            self.assertEqual(projected['scopes'][new['group']['scope_ref']], original['group']['scope'])
            self.assertEqual(new['suggestions'][0]['sources_summary']['count'], 9)
        original, question = context['backlog_questions'][0], projected['backlog_questions'][0]
        for key in ('head', 'state', 'status', 'answer_bindings', 'question_ref'):
            self.assertEqual(question[key], original[key])
        self.assertEqual(question['waiting_material_request']['request_ref'], original['waiting_material_request']['request_ref'])
        self.assertEqual(question['waiting_material_request']['reason'], original['waiting_material_request']['reason'])
        self.assertEqual(question['waiting_material_request']['question_binding'], 'head')
        self.assertNotIn('content_hash', projected)
        self.assertEqual(projected['projection']['full_context_hash'], content_hash(context))
        self.assertTrue(followup_context_matches_prompt(context, context))
        self.assertTrue(followup_context_matches_prompt(projected, context))

    def test_prompt_material_is_independent_of_immutable_full_context(self):
        context = self.context(); before = copy.deepcopy(context)
        projected = project_followup_context(context)
        projected['groups'][0]['suggestions'][0]['candidate']['question'] = 'Changed prompt only'
        next(iter(projected['execution_grants'].values()))['budget']['max_daily_cost_usd'] = 999
        projected['backlog_questions'][0]['head']['question'] = 'Changed prompt head'
        self.assertEqual(context, before)

    def test_foreign_version_or_modified_target_grant_or_either_projection_hash_refuses(self):
        context = self.context()
        for mutation in ('full_hash', 'projected_hash', 'candidate', 'grant', 'scope', 'question'):
            projected = project_followup_context(context)
            if mutation in ('full_hash', 'projected_hash'):
                projected['projection']['full_context_hash' if mutation == 'full_hash' else 'projected_content_hash'] = 'f' * 64
            elif mutation == 'candidate': projected['groups'][0]['suggestions'][0]['candidate']['question'] = 'Unbound proposal'
            elif mutation == 'grant': next(iter(projected['execution_grants'].values()))['may_write'].append('ungranted_write')
            elif mutation == 'scope': next(iter(projected['scopes'].values()))['company_ref'] = 'company:foreign'
            else: projected['backlog_questions'][0]['head']['question'] = 'Different unanswered question'
            with self.subTest(mutation=mutation): self.assertFalse(followup_context_matches_prompt(projected, context))


class ProjectedContextNormalAdoptionTests(unittest.TestCase):
    def fixture(self):
        fixture = reflection_fixture.ReflectionPlannerConsumptionTests('runTest'); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        projected = copy.deepcopy(fixture.state)
        projected['reflection_followup_context'] = project_followup_context(fixture.context, summarize_sources=True)
        fixture.prompt = build_prompt(projected)
        fixture.work['question'] = fixture.prompt
        fixture.s.execute('UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=?',
                          (canonical_json(fixture.work), content_hash(fixture.work))); fixture.s.commit()
        return fixture, projected

    def test_real_scheduler_core_plan_consumes_projected_context_with_unchanged_full_proofs(self):
        fixture, _ = self.fixture()
        originals = list(fixture.c.execute('SELECT record_json,content_hash FROM research_followup_requests'))
        observed = fixture.observe()
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(x['state'] == 'waiting_evidence' for x in observed))
        self.assertTrue(all(x['evidence']['input_context'] == fixture.context for x in observed))
        self.assertEqual(originals, list(fixture.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
        self.assertEqual(fixture.auto.observe_planner(context=fixture.context, full_state=fixture.state, stored_plan=fixture.stored), observed)

    def test_modified_projected_suggestion_with_valid_work_hash_cannot_adopt_or_close(self):
        fixture, projected = self.fixture()
        projected['reflection_followup_context']['groups'][0]['suggestions'][0]['candidate']['question'] = 'Changed suggestion'
        fixture.prompt = build_prompt(projected); fixture.work['question'] = fixture.prompt
        fixture.s.execute('UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=?',
                          (canonical_json(fixture.work), content_hash(fixture.work))); fixture.s.commit()
        with self.assertRaisesRegex(ValueError, 'exact planner input'): fixture.observe()
        self.assertEqual(fixture.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
