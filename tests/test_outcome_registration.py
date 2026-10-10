import sqlite3
from unittest.mock import patch
from dalton_core.store import content_hash
from dalton_core.outcome_registration import OutcomeRegistrationAuthority, read_registration
from dalton_core.judgement_outcome import build_outcome_checks, JudgementOutcomeAuthority
from tests.test_thesis_revision import RevisionHarness
from tests.p14a_fixtures import ACN, CTSH, EPAM, OWNER
from tests.zero_base_fixtures import add_judgement, series

class RegistrationTests(RevisionHarness):
    def setUp(self):
        super().setUp()
        self.judgement = add_judgement(self.store, judgement_id='event-judgement:prospective', company_ref=ACN,
                                      decision='NO_CHANGE', action='no_change', created_at='2030-07-01T00:00:00+00:00')
        self.authority = OutcomeRegistrationAuthority(self.store, clock=lambda:'2030-07-02T00:00:00+00:00')
        members = sorted([CTSH, EPAM]);digest = content_hash({'basket_members': members})
        self.draft = {'judgement_ref':self.judgement['id'], 'thesis_version_ref':self.version_one['id'],
                      'thesis_hash':self.version_one['content_hash'], 'direction':'up', 'metric_ref':'price_excess_return',
                      'benchmark_version_ref':'outcome-basket:'+digest[:32], 'benchmark_hash':digest, 'basket_members':members,
                      'period_start':'2030-07-02', 'period_end':'2030-07-10', 'target_release_date':'2030-07-10',
                      'information_cutoff':'2030-07-02', 'threshold_percent':'6', 'source_refs':list(self.version_one['claim_refs'])}

    def build(self, end=True):
        dates=['2030-07-02','2030-07-10'] if end else ['2030-07-02']
        mapping={ACN:series(ACN, dates,[100,90] if end else [100]),
                 CTSH:series(CTSH, dates,[100]*len(dates)), EPAM:series(EPAM, dates,[100]*len(dates))}
        return build_outcome_checks(self.store.connection, company_refs=[ACN], series_by_company=mapping, thresholds={}, stances={})

    def test_explicit_registration_preserves_a_pending_then_decidable_chain(self):
        registration=self.authority.record(self.draft,actor_ref=OWNER)
        self.assertEqual(read_registration(self.store.connection,self.judgement['id']),registration)
        pending=self.build(False);settled=self.build()
        self.assertEqual(pending[0]['outcome'],'pending')
        self.assertEqual(settled[0]['outcome'],'should_have_moved')
        self.assertEqual(settled[0]['registration_hash'],registration['content_hash'])
        outcomes=JudgementOutcomeAuthority(self.store)
        outcomes.record_all(pending,actor_ref='automation:coverage-mission')
        outcomes.record_all(settled,actor_ref='automation:coverage-mission')
        self.assertEqual([row['outcome'] for row in outcomes.versions(settled[0]['check_ref'])],['pending','should_have_moved'])

    def test_no_registration_is_unavailable_despite_a_formal_view_and_wrong_prices(self):
        self.assertEqual(self.build()[0]['reason'],'outcome_not_preregistered')

    def test_timestamps_late_results_scope_and_hash_cannot_be_faked(self):
        for change in ({'registered_at':'2020-01-01'}, {'target_release_date':'2030-07-01'},
                       {'thesis_hash':'a'*64}, {'benchmark_hash':'b'*64}):
            with self.assertRaises(ValueError):self.authority.record({**self.draft,**change},actor_ref=OWNER)
        with patch('dalton_core.outcome_registration.actual_publications',return_value=[{'published_on':'2030-07-01','ref':'held-release:1','hash':'a'*64}]):
            with self.assertRaisesRegex(ValueError,'already published'):self.authority.record(self.draft,actor_ref=OWNER)

    def test_target_is_immutable_and_cannot_be_registered_twice_or_as_automation(self):
        with self.assertRaises(ValueError):self.authority.record(self.draft,actor_ref='automation:coverage-mission')
        self.authority.record(self.draft,actor_ref=OWNER)
        with self.assertRaises(ValueError):self.authority.record({**self.draft,'direction':'down'},actor_ref=OWNER)
        for sql in ('UPDATE outcome_registrations SET registered_at="2020-01-01"','DELETE FROM outcome_registrations'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store._transaction() as cur:cur.execute(sql)
