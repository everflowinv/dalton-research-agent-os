import copy,json,unittest
from unittest.mock import patch
from tests import test_candidate_exact_evidence_reuse as prior
from tests import test_figure_admission as figures
from tests.test_mission_source_discovery import ACN,CTSH
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver,build_figure_candidate,promote_figure
from dalton_core.research_verification import MISSION_VERIFIED_FIGURE_RULE_REF
from dalton_core.directed_numeric_promotion_replay import replay
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.store import canonical_json,content_hash

class PromotionReplayTests(unittest.TestCase):
    def fixture(self,company=ACN):
        f=prior.ExactEvidenceReuseTests('runTest');self.addCleanup(f.doCleanups)
        h,figure,staging=f.fixture(company)
        body=copy.deepcopy(h.core.active_policy()['policy'])
        body['research_candidate_auto_commit']={'enabled':True,'rules':[MISSION_VERIFIED_FIGURE_RULE_REF],'max_records':10}
        h.core.create_policy(body,actor_ref=figures.REVIEWER)
        def raw(self,held):
            return {'version':'hermetic-old-raw-period:0.1','label':held['as_reported_label'],'period':held['period'],'span':None}
        with patch.object(MissionFigureAuthorityResolver,'identity',raw):
            old=promote_figure(h.core,staging,figure=figure,actor_ref=figures.ACTOR,idempotency_key='old-figure')
            result=h.core.commit_policy_candidate(evidence=old['evidence'],claim=old['claim'],material=old['material'],
                source_verification=old['source_verification'],numeric_spec=None,numeric_verification=old['numeric_verification'],idempotency_key='old-promotion')
        bundle=build_figure_candidate(h.core.connection,figure=figure,actor_ref=figures.ACTOR)
        return h,figure,bundle,result

    def rows(self,c):
        return {t:[tuple(r)for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in
                ('claim_versions','evidence_versions','reviewed_candidate_commits','evidence_relations')}

    def test_two_companies_exact_legacy_period_promotion_read_only_and_repeat(self):
        for company in (ACN,CTSH):
            with self.subTest(company=company):
                h,figure,bundle,original=self.fixture(company);before=self.rows(h.core.connection)
                done=replay(h.core,bundle=bundle)
                self.assertEqual(done['result']['claim_version_ref'],original['claim_version_ref'])
                self.assertNotEqual(done['bundle']['claim']['id'],bundle['claim']['id'])
                self.assertEqual(done['bundle']['claim']['period'],figure['period'])
                self.assertEqual(done['mapping']['source_period_identity']['period'],'2025-04-01..2025-06-30')
                again=replay(h.core,bundle=bundle)
                self.assertEqual(done['mapping'],again['mapping']);self.assertEqual(done['result'],again['result']);self.assertEqual(before,self.rows(h.core.connection))

    def test_current_bundle_value_label_or_original_cannot_be_substituted(self):
        h,figure,bundle,_=self.fixture();before=self.rows(h.core.connection)
        for field,value in (('value','1'),('as_reported_label','Other label'),('source_manifest_hash','f'*64)):
            offered={k:copy.deepcopy(v) for k,v in bundle.items() if k!='resolver'};offered['figure'][field]=value
            offered['figure']['content_hash']=content_hash({k:v for k,v in offered['figure'].items()if k!='content_hash'})
            with self.assertRaises(Exception):replay(h.core,bundle=offered)
        self.assertEqual(before,self.rows(h.core.connection))

    def test_ambiguous_period_or_changed_label_cannot_reuse_old_claim(self):
        h,figure,bundle,_=self.fixture();real=MissionFigureAuthorityResolver.identity
        for field,value in (('period','FY2025'),('label','Other revenues')):
            def changed(self,held):return {**real(self,held),field:value}
            # Current bundle is derived using exactly the offered current
            # contract, so rejection is about compatibility, not a bad hash.
            with patch.object(MissionFigureAuthorityResolver,'identity',changed):
                candidate=build_figure_candidate(h.core.connection,figure=figure,actor_ref=figures.ACTOR)
                with self.assertRaises(DirectedNumericError):replay(h.core,bundle=candidate)

    def test_tampered_review_canonical_record_and_superseded_version_refused(self):
        for table,column in (('reviewed_candidate_commits','decision_json'),('claim_versions','claim_json'),('evidence_versions','evidence_json')):
            h,figure,bundle,_=self.fixture()
            for r in h.core.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall():
                h.core.connection.execute('DROP TRIGGER '+r[0])
            h.core.connection.execute('UPDATE '+table+' SET '+column+"='{}'")
            with self.assertRaises(Exception):replay(h.core,bundle=bundle)

    def test_retracted_source_superseded_canonical_and_changed_value_are_not_compatibility(self):
        h,figure,bundle,original=self.fixture()
        h.missions.retract_document_figure(figure['figure_id'],reason='synthetic withdrawn original',retracted_by='automation:test')
        with self.assertRaises(Exception):replay(h.core,bundle=bundle)
        h,figure,bundle,original=self.fixture()
        row=h.core.connection.execute('SELECT claim_json FROM claim_versions WHERE claim_version_id=?',(original['claim_version_ref'],)).fetchone()
        later=json.loads(row[0]);later.update(id='claim-version:later',version=2,prior_version_ref=original['claim_version_ref'])
        later['content_hash']=content_hash({k:v for k,v in later.items() if k!='content_hash'})
        with h.core._transaction() as cur:
            cur.execute('INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)',(later['id'],later['claim_ref'],2,canonical_json(later),later['content_hash'],later['prior_version_ref'],later['created_at']))
        with self.assertRaisesRegex(DirectedNumericError,'superseded'):replay(h.core,bundle=bundle)

    def test_original_event_or_producer_binding_tampering_refused(self):
        for table,column in (('domain_events','event_json'),('connector_invocations','record_json')):
            h,figure,bundle,_=self.fixture()
            for r in h.core.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall():h.core.connection.execute('DROP TRIGGER '+r[0])
            h.core.connection.execute('UPDATE '+table+' SET '+column+"='{}'")
            with self.assertRaises(Exception):replay(h.core,bundle=bundle)
