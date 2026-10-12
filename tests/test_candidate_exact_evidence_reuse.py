"""A verified new claim may reuse only the latest identical evidence version."""
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_document_extraction as extraction
from tests import test_figure_admission as figures
from tests.test_mission_source_discovery import ACN, CTSH
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver, build_figure_candidate, promote_figure
from dalton_core.research_verification import ResearchVerificationConflict
from dalton_core.store import canonical_json, content_hash


class LegacyPeriodResolver(MissionFigureAuthorityResolver):
    def identity(self, figure):
        return {"version":"hermetic-legacy-period:0.1", "label":figure['as_reported_label'],
                "period":figure['period'], "span":None}


class ExactEvidenceReuseTests(unittest.TestCase):
    def fixture(self, company=ACN):
        # Both companies are in the fixture's normal signed mission scope.
        # The original and all connector/model handles remain hermetic.
        with patch.object(extraction,'ACN',company),patch.object(figures,'ACN',company):
            harness=figures.FigureHarness()
            figure=harness.record_figure(period='Three months ended June 30, 2025')
        self.addCleanup(harness.close)
        staging=harness.staging();self.addCleanup(staging.close)
        return harness,figure,staging

    @staticmethod
    def rows(staging):
        return {table:[tuple(row)for row in staging.connection.execute('SELECT * FROM '+table+' ORDER BY rowid')]
            for table in staging.counts()}

    def old_stage(self,harness,figure,staging):
        return promote_figure(harness.core,staging,figure=figure,actor_ref=figures.ACTOR,
            idempotency_key='old-period',resolver=LegacyPeriodResolver(harness.core.connection))

    def new_stage(self,harness,figure,staging,key='current-period'):
        return promote_figure(harness.core,staging,figure=figure,actor_ref=figures.ACTOR,
            idempotency_key=key,resolver=MissionFigureAuthorityResolver(harness.core.connection))

    def test_two_companies_new_claim_reuses_exact_latest_evidence_and_is_idempotent(self):
        for company in (ACN,CTSH):
            with self.subTest(company=company):
                harness,figure,staging=self.fixture(company)
                old=self.old_stage(harness,figure,staging);before=self.rows(staging)
                new=self.new_stage(harness,figure,staging)
                self.assertEqual(new['evidence'],old['evidence'])
                self.assertNotEqual(new['claim']['candidate_claim_ref'],old['claim']['candidate_claim_ref'])
                self.assertEqual(new['write_status'],'fresh')
                self.assertEqual(new['claim']['period'],'2025-04-01..2025-06-30')
                self.assertEqual(self.rows(staging)['candidate_evidence_versions'],before['candidate_evidence_versions'])
                self.assertEqual(staging.counts()['candidate_claim_versions'],2)
                self.assertEqual(staging.counts()['candidate_stage_requests'],2)
                after=self.rows(staging);duplicate=self.new_stage(harness,figure,staging)
                self.assertEqual(duplicate['write_status'],'duplicate');self.assertEqual(self.rows(staging),after)

    def test_same_evidence_with_existing_claim_new_key_does_not_skip_claim_chain(self):
        h,figure,staging=self.fixture();self.new_stage(h,figure,staging);before=self.rows(staging)
        with self.assertRaisesRegex(ResearchVerificationConflict,'version chain mismatch'):
            self.new_stage(h,figure,staging,key='another-key')
        self.assertEqual(self.rows(staging),before)

    def test_existing_evidence_columns_body_and_hash_must_all_match(self):
        changes={'content_hash':'0'*64,'record_json':'{}','candidate_evidence_ref':'candidate-evidence:foreign',
            'version_number':7,'prior_version_id':'candidate-evidence-version:foreign','created_at':'1999-01-01T00:00:00Z'}
        for field,value in changes.items():
            with self.subTest(field=field):
                h,figure,staging=self.fixture();self.old_stage(h,figure,staging)
                # Corruption is confined to this disposable fixture, not live
                # append-only authority. Preserve the rejection path itself.
                for row in staging.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='candidate_evidence_versions'").fetchall():
                    staging.connection.execute('DROP TRIGGER '+row[0])
                staging.connection.execute('PRAGMA foreign_keys=OFF')
                staging.connection.execute('UPDATE candidate_evidence_versions SET '+field+'=?',(value,))
                before=self.rows(staging)
                with self.assertRaises(ResearchVerificationConflict):self.new_stage(h,figure,staging)
                self.assertEqual(self.rows(staging),before)

    def test_superseded_historical_evidence_cannot_be_reused(self):
        h,figure,staging=self.fixture();old=self.old_stage(h,figure,staging);wire=copy.deepcopy(old['evidence'])
        wire.update(id='candidate-evidence-version:later',version=2,prior_version_ref=old['evidence']['id'])
        wire['content_hash']=content_hash({k:v for k,v in wire.items()if k!='content_hash'})
        staging.connection.execute('INSERT INTO candidate_evidence_versions VALUES(?,?,?,?,?,?,?)',
            (wire['id'],wire['candidate_evidence_ref'],2,wire['prior_version_ref'],canonical_json(wire),wire['content_hash'],wire['created_at']))
        before=self.rows(staging)
        with self.assertRaisesRegex(ResearchVerificationConflict,'latest exact version'):self.new_stage(h,figure,staging)
        self.assertEqual(self.rows(staging),before)

    def test_value_or_evidence_reference_drift_is_rejected_before_reuse(self):
        for changed in ('value','evidence_ref'):
            h,figure,staging=self.fixture();self.old_stage(h,figure,staging)
            resolver=MissionFigureAuthorityResolver(h.core.connection)
            bundle=build_figure_candidate(h.core.connection,figure=figure,actor_ref=figures.ACTOR,resolver=resolver)
            claim=copy.deepcopy(bundle['claim'])
            if changed=='value':claim['value']='99.9'
            else:claim['candidate_evidence_refs'][0]['ref']='candidate-evidence-version:foreign'
            claim['content_hash']=content_hash({k:v for k,v in claim.items()if k!='content_hash'})
            before=self.rows(staging)
            with self.assertRaisesRegex(ResearchVerificationConflict,'drifted from verified inputs'):
                staging.stage(material=bundle['material'],source_verification=bundle['source_verification'],
                    evidence=bundle['evidence'],claim=claim,idempotency_key='drift',
                    verification_mode='mission_figure_authority',figure_admission_policy='verified_figure',
                    verified_figure=figure,figure_resolver=resolver)
            self.assertEqual(self.rows(staging),before)

    def test_reused_evidence_transaction_rolls_back_and_after_commit_replay_is_exact(self):
        h,figure,staging=self.fixture();self.old_stage(h,figure,staging);before=self.rows(staging)
        def crash(stage,_result):
            if stage=='before_commit':raise RuntimeError('isolated transaction fault')
        staging._fault_hook=crash
        with self.assertRaisesRegex(RuntimeError,'transaction fault'):self.new_stage(h,figure,staging)
        self.assertEqual(self.rows(staging),before)
        def after(stage,_result):
            if stage=='after_commit':raise RuntimeError('isolated response fault')
        staging._fault_hook=after
        with self.assertRaisesRegex(RuntimeError,'response fault'):self.new_stage(h,figure,staging)
        committed=self.rows(staging);staging._fault_hook=None
        self.assertEqual(self.new_stage(h,figure,staging)['write_status'],'duplicate')
        self.assertEqual(self.rows(staging),committed)
