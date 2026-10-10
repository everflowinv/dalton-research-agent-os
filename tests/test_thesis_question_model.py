"""TM governance boundaries, including real admission/decision transactions."""
import copy
import json
import unittest
from unittest.mock import patch

from dalton_core.coverage_admission import CoverageAdmissionConflict, CoverageAdmissionValidationError
from dalton_core.thesis_question_model import validate_question_fields, validate_revision_proposal, validate_directions, thesis_payload
from dalton_core.thesis_revision import ThesisRevisionValidationError
from tests.test_coverage_admission import CoverageAdmissionTests, COMPANY_REF, INDUSTRY_REF, thesis_content
from tests.test_thesis_revision import RevisionHarness, OWNER
from tests.thesis_question_fixtures import formal_content


class QuestionGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.h = CoverageAdmissionTests('test_human_admission_is_bound_immutable_and_idempotent')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.content = formal_content(self.h.store, thesis_content(), question='question:demand-bottoming')

    def admit(self, suffix, content):
        h = self.h
        candidate = h.authority.propose_thesis_admission(
            candidate_id='candidate:' + suffix, thesis_ref='thesis:acn:' + suffix,
            company_ref=COMPANY_REF, industry_ref=INDUSTRY_REF,
            template_ref='template:ai-reinvention-growth', driver_refs=['driver:bookings-conversion'],
            mandate_version_ref=h.mandate['id'], mandate_version_hash=h.mandate['content_hash'],
            driver_pack_version_ref=h.pack['id'], driver_pack_version_hash=h.pack['content_hash'],
            content=content, actor_ref='human:owner', idempotency_key='candidate:' + suffix)
        return h.authority.decide_thesis_admission(
            candidate_id=candidate['id'], candidate_hash=candidate['content_hash'], verdict='admit',
            rationale='Synthetic evidence supports this explicitly falsifiable proposition.',
            decision_id='decision:' + suffix, actor_ref='human:owner', idempotency_key='decision:' + suffix)

    def test_one_question_cannot_have_two_current_house_views(self):
        self.admit('first', self.content)
        with self.assertRaisesRegex(CoverageAdmissionConflict, 'one current house'):
            self.admit('second', self.content)
        self.assertEqual(self.h.store.connection.execute('SELECT COUNT(*) FROM thesis_versions').fetchone()[0], 1)

    def test_opposite_counter_requires_exact_disagreement_and_appends_it_atomically(self):
        first = self.admit('first', self.content)['thesis_version']
        counter = {**self.content, 'stance': 'counter', 'proposition_direction': 'contradicts',
                   'statement': 'Demand has not bottomed; declining conversion contradicts a recovery.'}
        with self.assertRaisesRegex(CoverageAdmissionConflict, 'opposite current views'):
            self.admit('unexplained', counter)
        counter['disagreement'] = {'other_thesis_version_ref': first['id'], 'other_thesis_hash': first['content_hash'],
                                   'reason': 'The same booking data permit different conversion assumptions.',
                                   'evidence_refs': self.content['claim_refs'], 'deadline': '2027-03-31', 'arbiter_ref': 'human:owner'}
        published = self.admit('counter', counter)['thesis_version']
        rows = self.h.store.connection.execute("SELECT event_json FROM domain_events WHERE event_type='thesis_disagreement'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn(published['id'], rows[0][0])
        self.assertIn(first['id'], rows[0][0])

    def test_formal_views_cannot_cite_missing_passages_or_claims(self):
        for refs in ({'claim_refs': ['claim-version:missing']}, {'claim_refs': [], 'passage_refs': ['doc-passage:missing']}):
            with self.subTest(refs=refs), self.assertRaisesRegex(CoverageAdmissionConflict, 'does not resolve'):
                self.admit(str(len(refs)) + str(bool(refs.get('passage_refs'))), {**self.content, **refs})

    def test_new_admission_refuses_legacy_empty_metadata(self):
        with self.assertRaises(CoverageAdmissionValidationError):
            self.admit('old', thesis_content())

    def test_quantified_falsifier_requires_finite_threshold_evidence_and_real_date(self):
        for change in ({'threshold': 'NaN'}, {'threshold': True}, {'deadline': '2027-02-30'}, {'evidence_refs': []}):
            bad = copy.deepcopy(self.content)
            bad['falsification_conditions'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_question_fields(bad)

    def test_industry_framework_is_required_and_latest(self):
        from dalton_core.thesis_question_model import require_current_question
        with self.assertRaisesRegex(ValueError, 'latest industry framework'):
            require_current_question(self.h.store.connection, self.content, thesis_ref='thesis:industry:demand',
                                     industry_ref=INDUSTRY_REF, industry_thesis=True)
        from tests.test_industry_framework import record, simple_comparison
        from dalton_core.industry_framework import IndustryFrameworkAuthority
        authority = IndustryFrameworkAuthority(self.h.store)
        first = authority.publish(record(comparison=simple_comparison()))
        bound = {**self.content, 'framework_version_ref': first['id']}
        require_current_question(self.h.store.connection, bound, thesis_ref='thesis:industry:demand', industry_ref=INDUSTRY_REF, industry_thesis=True)
        authority.publish(record(comparison=simple_comparison(), ref='claim-version:new'))
        with self.assertRaisesRegex(ValueError, 'latest industry framework'):
            require_current_question(self.h.store.connection, bound, thesis_ref='thesis:industry:demand', industry_ref=INDUSTRY_REF, industry_thesis=True)

    def test_no_empty_confidence_or_confidence_contradiction(self):
        for statement, confidence in ((None, None), ('', 'low'), ('The case has high confidence', 'low')):
            with self.subTest(statement=statement), self.assertRaises(ValueError):
                validate_revision_proposal(statement, confidence, decision='THESIS_WEAKENED')

    def test_superseded_uses_existing_reopen_threshold_authority(self):
        from types import SimpleNamespace
        from dalton_core.event_judgement import reopen_superseded_thesis
        with patch('dalton_core.deliverable_reopen.GateReopenAuthority') as authority, patch('dalton_core.redraft_policy.auto_reopen', return_value={'status': 'reopened'}) as reopen:
            fake = SimpleNamespace(store=SimpleNamespace(path='/tmp/synthetic-core/core.sqlite'), connection=object())
            self.assertEqual(reopen_superseded_thesis(fake, COMPANY_REF, {'id': 'mission:1'})['status'], 'reopened')
            self.assertEqual(reopen.call_args.args[3], COMPANY_REF)
            self.assertEqual(reopen.call_count, 1)


class DirectionRevisionTests(RevisionHarness):
    def test_unchanged_words_cannot_hide_a_direction_flip(self):
        from dalton_core.thesis_revision import require_revision_direction
        current = {"question_ref": "question:q", "statement": "Demand can recover.", "proposition_direction": "supports"}
        proposed = {**current, "confidence": "low", "proposition_direction": "contradicts", "view_status": "active"}
        with self.assertRaisesRegex(ValueError, "THESIS_BROKEN"):
            require_revision_direction(None, {"decision": "THESIS_WEAKENED", "proposed_confidence": "low"}, current, proposed)

    def test_rejected_independent_review_cannot_authorize_new_words(self):
        from unittest.mock import Mock
        from dalton_core.thesis_revision import require_revision_direction, proposed_content
        candidate = self.candidate(statement="Demand may recover more slowly.")
        row = self.store.connection.execute("SELECT record_json FROM event_judgements WHERE judgement_id=?", (candidate['judgement_ref'],)).fetchone()
        record = json.loads(row[0]); record['verifier']['verdict'] = 'reject'
        core = Mock()
        core.execute.return_value.fetchone.return_value = (json.dumps(record),)
        proposed = proposed_content(current_content=self.version_one, candidate=candidate)
        with self.assertRaisesRegex(ValueError, "passed independent"):
            require_revision_direction(core, candidate, self.version_one, proposed)

    def test_opposite_statement_cannot_be_accepted_as_weakened(self):
        with patch.object(self, 'judgement', wraps=self.judgement) as original:
            # Produce a held independent verifier record with an opposite comparison.
            def judged(suffix, statement, confidence):
                return RevisionHarness.judgement(self, suffix, statement, confidence, relation='opposite')
            original.side_effect = judged
            candidate = self.candidate(statement='Reinvention demand can no longer sustain growth.')
        with self.assertRaisesRegex(ThesisRevisionValidationError, 'THESIS_BROKEN'):
            self.revisions.decide(candidate_ref=candidate['id'], candidate_hash=candidate['content_hash'], verdict='accept', reason='Review.', actor_ref=OWNER)

    def test_broken_revision_remains_visibly_broken(self):
        candidate = self.candidate(decision='THESIS_BROKEN', statement='Reinvention demand can no longer sustain growth.')
        result = self.revisions.decide(candidate_ref=candidate['id'], candidate_hash=candidate['content_hash'], verdict='accept', reason='Falsifier occurred.', actor_ref=OWNER)
        self.assertEqual(result['thesis_version']['view_status'], 'broken')

    def test_unreviewed_replacement_question_is_refused(self):
        candidate = self.candidate()
        proposed = {**thesis_payload(self.version_one), 'confidence': 'low', 'question_ref': 'question:different'}
        with self.assertRaisesRegex(ThesisRevisionValidationError, 'new thesis admission'):
            self.revisions.decide(candidate_ref=candidate['id'], candidate_hash=candidate['content_hash'], verdict='accept', reason='Different question.', actor_ref=OWNER, content=proposed)


class DirectionVerifierTests(unittest.TestCase):
    def test_cheap_independent_event_verifier_binds_exact_proposal(self):
        from dalton_core.event_judgement import verify, VERIFIER_PURPOSE
        from tests.test_event_judgement import FakeModel, JUDGE_ROUTE, VERIFIER_ROUTE
        thesis = {'ref': 'thesis-version:one', 'content_hash': 'a' * 64, 'statement': 'Demand is recovering.', 'confidence': 'medium'}
        context = {'event': {'id': 'event:1', 'kind': 'claim', 'source_refs': [], 'payload': {}, 'evidence_tier': 'company_primary'},
                   'theses': [thesis], 'claims': [], 'drivers': []}
        judgement = {'status': 'judged', 'decision': 'THESIS_WEAKENED', 'action': 'revise_thesis',
                     'because': 'Recovery is slower.', 'citations': ['event:1'], 'driver_refs': [],
                     'thesis_refs': [thesis['ref']], 'proposed_statement': 'Demand recovery is still possible but less certain.',
                     'proposed_confidence': 'low', 'model': {'route_decision_ref': JUDGE_ROUTE}}
        for relation, status in (('same', 'verified'), ('opposite', 'refused'), ('unclear', 'refused')):
            model = FakeModel([{'verdict': 'pass', 'findings': [], 'direction_checks': [{'thesis_version_ref': thesis['ref'], 'relation': relation, 'reason': 'Compare directional proposition.'}]}], route=VERIFIER_ROUTE)
            with self.subTest(relation=relation):
                result = verify(context, judgement, model=model, mission={}, request_id='direction',
                                family_resolver={JUDGE_ROUTE: 'producer', VERIFIER_ROUTE: 'verifier'}.get)
                self.assertEqual(result['status'], status, result)
                self.assertEqual(model.purposes, [VERIFIER_PURPOSE])
                if status == 'verified':
                    from dalton_core.thesis_question_model import direction_binding
                    binding = direction_binding(thesis, judgement['proposed_statement'], 'low')
                    self.assertTrue(all(result['direction_checks'][0][k] == v for k,v in binding.items()))
