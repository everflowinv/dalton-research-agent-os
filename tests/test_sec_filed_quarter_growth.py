from __future__ import annotations
import copy
import unittest
from datetime import datetime, timezone
from tests import test_sec_fy_minus_9m as fixture
from dalton_core.sec_filed_quarter_growth import build_filed_quarter_growth_candidate, stage_filed_quarter_growth_candidate, RULE_REF
from dalton_core.research_auto_commit import ResearchAutoCommitRejected
from dalton_core.quantitative_claim_promotion import QuantitativeClaimPromotionError

Q1='0001018724-26-000016'; Q2='0001018724-26-000031'
def filings():
    def row(*args, **kwargs):
        return fixture.row(*args, dimension_count=0, dimensions={}, **kwargs)
    M=fixture.M
    return {Q1:('10-Q','2026-04-29','2026-03-31',[
        row('2025-01-01','2025-03-31',5115*M),row('2026-01-01','2026-03-31',5413*M)]),
        Q2:('10-Q','2026-07-29','2026-06-30',[
        row('2025-01-01','2025-06-30',10360*M),row('2025-04-01','2025-06-30',5245*M),
        row('2026-01-01','2026-06-30',10894*M),row('2026-04-01','2026-06-30',5481*M)])}

class FiledQuarterGrowthTests(unittest.TestCase):
    def harness(self, fs=None, **kw):
        h=fixture.Harness(filings() if fs is None else fs, **kw);self.addCleanup(h.close);return h
    def ingest(self,h):
        return h.store.connection.execute('SELECT ingest_id FROM coverage_mission_statement_filings WHERE accession=?',(Q2,)).fetchone()[0]
    def bundle(self,h):
        return build_filed_quarter_growth_candidate(h.store.connection,ingest_id=self.ingest(h),actor_ref=fixture.ACTOR)
    def test_same_accession_quarter_pair_full_chain_and_idempotent_formal_commit(self):
        h=self.harness();h.sign(RULE_REF)
        b=stage_filed_quarter_growth_candidate(h.store.connection,h.staging,ingest_id=self.ingest(h),actor_ref=fixture.ACTOR)
        self.assertEqual(b['claim']['value'],'4.5');self.assertEqual(b['claim']['period'],'2026-04-01..2026-06-30')
        self.assertEqual(b['source_verification']['verdict'],'pass');self.assertEqual(b['numeric_verification']['verdict'],'pass')
        first=h.commit(b);second=h.commit(b)
        self.assertEqual(first['status'],'fresh');self.assertEqual(second['status'],'duplicate')
        self.assertEqual(len(h.claims()),1)
    def test_other_company_uses_same_rule_no_ticker_switch(self):
        h=self.harness(company='company:sec-cik:0001058290');self.assertEqual(self.bundle(h)['claim']['subject_ref'],h.company)
    def test_unsigned_statement_line_or_fy_rule_cannot_authorize_quarter_pair(self):
        h=self.harness();h.sign(fixture.SEC_STATEMENT_LINE_RULE_REF,fixture.SEC_FY_MINUS_9M_RULE_REF)
        with self.assertRaisesRegex(ResearchAutoCommitRejected,RULE_REF):h.commit(self.bundle(h))
    def test_tampered_candidate_not_admitted(self):
        h=self.harness();h.sign(RULE_REF);b=self.bundle(h);claim=copy.deepcopy(b['claim']);claim['normalized_statement']+=' altered'
        from dalton_core.store import content_hash
        claim['content_hash']=content_hash({k:v for k,v in claim.items()if k!='content_hash'})
        with self.assertRaises(ResearchAutoCommitRejected):h.commit(b,claim=claim)
    def test_current_concept_unit_and_ytd_inconsistency_refuse(self):
        for mutation in ['concept','unit','ytd']:
            with self.subTest(mutation=mutation):
                fs=filings();r=fs[Q2][3]
                if mutation=='concept':r[-1]['concept']='us-gaap:SalesRevenueNet'
                if mutation=='unit':r[-1]['unit']='unknown-unit'
                if mutation=='ytd':r[2]['value']=str(12000*fixture.M)
                h=self.harness(fs)
                with self.assertRaises(QuantitativeClaimPromotionError):self.bundle(h)
    def test_missing_prior_and_missing_independent_first_quarter_refuse(self):
        for index in [0,1]:
            with self.subTest(index=index):
                fs=filings()
                if index==0:fs[Q2][3].pop(1)
                else:del fs[Q1]
                h=self.harness(fs)
                with self.assertRaises(QuantitativeClaimPromotionError):self.bundle(h)
    def test_source_line_drift_and_dispatch_failure_refuse(self):
        h=self.harness();c=h.store.connection
        for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='coverage_mission_statement_lines'").fetchall():
            c.execute('DROP TRIGGER '+r[0])
        c.execute("UPDATE coverage_mission_statement_lines SET value='1' WHERE ingest_id=? AND ordinal=3",(self.ingest(h),))
        with self.assertRaisesRegex(QuantitativeClaimPromotionError,'hash is invalid'):self.bundle(h)
    def test_normal_lane_commits_primary_before_companyfacts_backoff_no_new_dispatch(self):
        from dalton_core.mission_sec_quarters import MissionSecQuartersCoordinator
        h=self.harness();h.sign(RULE_REF)
        coord=MissionSecQuartersCoordinator(store=h.store,missions=h.missions,state_dir=h.path,
            checklist=lambda:[{'company_ref':h.company,'ticker':'ACN','items':[{'item_ref':'quarterly_financials','have':0,'required':4}]}],
            clock=lambda:datetime(2026,8,1,tzinfo=timezone.utc),governance_check=lambda:None,staging=h.staging)
        entry={'accession':Q2,'form':'10-Q','filed':'2026-07-29','end':'2026-06-30','period':'2026-04-01..2026-06-30','observation_ref':'statement-ingest:'+self.ingest(h)}
        coord._artifact_filings=lambda company:([entry],True)
        coord._attempt_ledger=lambda:{'counted':{},'source_lag':{Q2:[datetime(2026,8,1,tzinfo=timezone.utc)]},'transport':{},'annual_only':set()}
        result=coord.dispatch_once();self.assertEqual(result['status'],'committed',result)
        self.assertEqual(result['queued'],[]);self.assertIn('2026-04-01..2026-06-30',coord._held_periods(h.company))
        before=len(h.claims());again=coord.dispatch_once();self.assertEqual(again['status'],'idle');self.assertEqual(len(h.claims()),before)

    def test_frozen_context_replay_is_exact_and_missing_binding_cannot_authorize(self):
        h=self.harness();b=self.bundle(h)
        bindings=b['material']['normalized_payload']['context_bindings']
        replay=build_filed_quarter_growth_candidate(h.store.connection,ingest_id=self.ingest(h),actor_ref=fixture.ACTOR,context_bindings=bindings)
        self.assertEqual(replay['material'],b['material']);self.assertEqual(replay['claim'],b['claim'])
        with self.assertRaisesRegex(QuantitativeClaimPromotionError,'binding'):
            build_filed_quarter_growth_candidate(h.store.connection,ingest_id=self.ingest(h),actor_ref=fixture.ACTOR,context_bindings={})

    def test_failed_statement_dispatch_cannot_promote_even_if_number_is_present(self):
        h=self.harness();c=h.store.connection
        for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='coverage_mission_statement_dispatches'").fetchall():
            c.execute('DROP TRIGGER '+row[0])
        c.execute("UPDATE coverage_mission_statement_dispatches SET status='failed'")
        with self.assertRaisesRegex(QuantitativeClaimPromotionError,'authority verification rejected'):
            self.bundle(h)

    def test_hidden_or_unknown_dimensions_cannot_be_taken_for_consolidated_revenue(self):
        for extra in ({'dimension_count':1,'dimensions':{'synthetic:HiddenAxis':'synthetic:Member'}},
                      {'dimension_count':None,'dimensions':None}):
            with self.subTest(extra=extra):
                fs=filings();fs[Q2][3][-1].update(extra);h=self.harness(fs)
                with self.assertRaisesRegex(QuantitativeClaimPromotionError,'consolidated quarter'):
                    self.bundle(h)
