"""Unit batching and exact verified-read bookkeeping, without paid calls."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core import evidence_index as ei, redraft_policy as policy
from dalton_core import research_product_consumption as consumption
from dalton_core.store import DaltonStore
from tests.test_material_pack import Corpus, FILL, SYNW


class ProductBatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.core = DaltonStore(self.state / 'core.sqlite'); self.addCleanup(self.core.close)
        self.corpus = Corpus(self.state)
        self.now = datetime.now(timezone.utc)
        self.stamp = (self.now - timedelta(days=1)).isoformat()
        self.prior = {'id': 'dossier-version:1', 'drafted_at': {'economics': self.stamp, 'risks': self.stamp},
                      'sections': [{'aspect': 'economics', 'status': 'drafted', 'sources': []},
                                   {'aspect': 'risks', 'status': 'drafted', 'sources': []}]}
        self.binding = consumption.scope('dossier', SYNW, 'economics', [SYNW])

    def arrive(self, letter, *, now=None):
        return self.corpus.add('broker_report', 'Synthetic Widgets '+letter,
            'Synthetic Widgets SYNW '+FILL*12, date=self.now.date().isoformat(),
            now=(now or self.now-timedelta(minutes=1)).isoformat())

    def snapshot(self, refs=(), binding=None, prior_body=''):
        material = [{'ref': ei.passages_for_document(self.state, ref)[0]['passage_ref'],
                     'text': 'Actual bounded original passage'} for ref in refs]
        return consumption.snapshot(self.state, self.core.connection, binding or self.binding,
            material=material, input_data={'prompt_sha': 'derived:'+prior_body,
                'parse_input': {'material': material, 'prior_body': prior_body}})

    def gate(self, captured, *, unit='economics', now=None, **kwargs):
        # This corpus has no consensus/model/conflict authorities. Their
        # absence does not invent a reason to rewrite.
        return policy.unit_gate(self.state, self.core.connection, product='dossier', target=SYNW,
            company_refs=[SYNW], unit=unit, prior=self.prior, input_snapshot=captured,
            now=now or self.now, **kwargs)

    def save(self, snap, binding=None, **kwargs):
        args = {'result': 'verified_no_new_evidence', 'producer_refs': ['route:producer'],
                'verifier_ref': 'route:independent', 'independent': True, 'verified': True}
        args.update(kwargs)
        return consumption.record(self.state, binding or self.binding, snap, **args)

    def test_one_waits_three_documents_accumulate_across_ticks(self):
        a=self.arrive('a'); first=self.gate(self.snapshot([a]))
        self.assertFalse(first['allowed']); self.assertEqual(first['pending_count'], 1)
        self.arrive('b'); self.assertFalse(self.gate(self.snapshot([a]))['allowed'])
        self.arrive('c'); result=self.gate(self.snapshot([a]))
        self.assertTrue(result['allowed']); self.assertIn('broker_batch', [r['code'] for r in result['reasons'] if r['fired']])

    def test_input_only_and_late_arrivals_are_not_consumed_by_published_time(self):
        a=self.arrive('a'); b=self.arrive('b'); snap=self.snapshot([a])
        self.arrive('during-call', now=self.now+timedelta(seconds=1)); self.save(snap)
        rows=ei.material_arrivals(self.state, company_ref=SYNW, include_backfill=True)
        self.assertEqual([x['document_ref'] for x in consumption.pending_arrivals(self.state, self.binding, rows)], [b, rows[-1]['document_ref']])

    def test_no_new_evidence_read_is_idempotent_and_prior_prose_is_not_new_input(self):
        a=self.arrive('a'); first=self.snapshot([a], prior_body='first prose'); self.save(first)
        next_input=self.snapshot([a], prior_body='changed generated prose')
        self.assertEqual(first['input_hash'], next_input['input_hash'])
        self.assertNotEqual(first['exact_input_hash'], next_input['exact_input_hash'])
        self.assertFalse(self.gate(next_input)['allowed'])
        self.assertEqual(self.save(first), consumption.latest(self.state, self.binding))

    def test_unit_a_does_not_clear_b_and_failed_reads_consume_nothing(self):
        a=self.arrive('a'); snap=self.snapshot([a]); other=consumption.scope('dossier', SYNW, 'risks', [SYNW])
        for args in ({'verified': False}, {'independent': False}, {'result':'carry'}):
            with self.assertRaises(ValueError): self.save(snap, **args)
        rows=ei.material_arrivals(self.state, company_ref=SYNW, include_backfill=True)
        self.assertEqual(consumption.pending_arrivals(self.state, self.binding, rows), rows)
        self.save(snap)
        self.assertEqual(consumption.pending_arrivals(self.state, other, rows), rows)
        self.assertEqual(self.gate(snap, unit='risks')['pending_count'], 1)

    def test_first_explicit_revision_retirement_and_binding_change_are_not_blocked(self):
        snap=self.snapshot()
        for key in ('first','revise','correction','binding_changed'):
            self.assertTrue(self.gate(snap, **{key:True})['allowed'])

    def test_missing_unit_time_is_not_guessed_from_product_creation(self):
        self.prior['drafted_at']={}; self.prior['created_at']=self.stamp
        result=self.gate(self.snapshot())
        self.assertFalse(result['allowed']); self.assertEqual(result['reasons'][0]['code'], 'unit_time_unproved')

    def test_legacy_time_is_age_not_assertion_of_all_inputs_read(self):
        # An arrival during an old unknown model pass precedes publication.
        self.prior['drafted_at']['economics']=self.now.isoformat()
        self.arrive('between-read-and-publication', now=self.now-timedelta(minutes=1))
        self.assertEqual(self.gate(self.snapshot())['pending_count'], 1)

    def test_actual_prompt_material_cannot_be_broader_than_frozen_material(self):
        with self.assertRaisesRegex(ValueError, 'frozen input'):
            consumption.snapshot(self.state,self.core.connection,self.binding,
                material=[{'ref':'claim:unread'}], input_data={'parse_input':{'material':[]}})

    def test_raw_claim_lineage_cannot_consume_an_unbound_index_original(self):
        a=self.arrive('a')
        # No passage identity enters this actual prompt, regardless of whether
        # a bare source's present index happens to have one body.
        material=[{'ref':'claim:historical','text':'old observation'}]
        snap=consumption.snapshot(self.state,self.core.connection,self.binding,
            material=material,input_data={'parse_input':{'material':material}})
        self.assertEqual(snap['arrivals'],[])

    def test_concurrent_same_unit_append_preserves_both_read_sets(self):
        a=self.arrive('a'); b=self.arrive('b'); snapshots=[self.snapshot([a]),self.snapshot([b])]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(self.save,snapshots))
        rows=ei.material_arrivals(self.state, company_ref=SYNW, include_backfill=True)
        self.assertEqual(consumption.pending_arrivals(self.state,self.binding,rows),[])
        db=sqlite3.connect(self.state/consumption.JOURNAL); self.addCleanup(db.close)
        with self.assertRaises(sqlite3.DatabaseError): db.execute('DELETE FROM unit_input_receipts')

    def test_light_probe_changes_once_when_only_clock_crosses_fourteen_days(self):
        a=self.arrive('a'); age=self.now-timedelta(days=13)
        self.prior['drafted_at']={key:age.isoformat() for key in self.prior['drafted_at']}
        def probe(moment):
            return policy.lane_probe(self.state,self.core.connection,product='dossier',target=SYNW,
                company_refs=[SYNW],prior=self.prior,now=moment,light_only=True)
        before=probe(self.now); same=probe(self.now+timedelta(hours=1)); due=probe(self.now+timedelta(days=1))
        self.assertEqual(before['fingerprint'],same['fingerprint']); self.assertNotEqual(before['fingerprint'],due['fingerprint'])
        self.assertEqual(due['fingerprint'],probe(self.now+timedelta(days=2))['fingerprint'])
        self.assertFalse(due['urgent'])

    def test_verified_first_read_without_a_new_formal_version_is_not_paid_again(self):
        snap=self.snapshot([self.arrive('a')]); self.save(snap)
        self.prior=None
        self.assertFalse(self.gate(snap, first=True)['allowed'])
        self.assertTrue(self.gate(snap, revise=True)['allowed'])

    def test_verified_binding_change_is_consumed_but_a_later_binding_is_new(self):
        # Use the actual producer-input builder, rather than inventing a hash:
        # governance and method structure both participate in semantic identity.
        from dalton_core.company_dossier_cli import build_dossier_input
        from tests.test_dossier_lane import policy_document
        def captured(version, structure=()):
            bound_policy=policy_document()
            bound_policy['policy_ref']='policy:'+version
            value=build_dossier_input(unit='economics',structure=structure,material=[],
                company={'company_ref':SYNW,'ticker':'SYNW'},
                mission={'id':'mission:'+version,'content_hash':version*64},
                constitution={'id':'constitution:'+version,'content_hash':version*64},
                policy=bound_policy)
            return consumption.snapshot(self.state,self.core.connection,self.binding,
                material=value['parse_input']['material'],input_data=value)
        first=captured('a'); self.save(first)
        changed=captured('b')
        self.assertNotEqual(first['input_hash'],changed['input_hash'])
        self.assertTrue(self.gate(changed,binding_changed=True)['allowed'])
        receipt=self.save(changed)
        raw=(self.state/consumption.JOURNAL).read_bytes()
        self.assertFalse(self.gate(changed,binding_changed=True)['allowed'])
        self.assertEqual(consumption.latest(self.state,self.binding),receipt)
        self.assertEqual((self.state/consumption.JOURNAL).read_bytes(),raw)
        self.assertEqual(receipt['schema_version'],'verified-unit-input-consumption:0.1')
        self.assertTrue(self.gate(changed,revise=True,binding_changed=True)['allowed'])
        self.assertTrue(self.gate(changed,correction=True,binding_changed=True)['allowed'])
        later=captured('c')
        self.assertNotEqual(changed['input_hash'],later['input_hash'])
        self.assertTrue(self.gate(later,binding_changed=True)['allowed'])
        method=captured('b',[{'slot_id':'economics','prompt':'Updated method'}])
        self.assertNotEqual(changed['input_hash'],method['input_hash'])
        self.assertTrue(self.gate(method,binding_changed=True)['allowed'])

    def test_actual_dossier_child_waits_then_consumes_verified_no_new_input(self):
        from tests.test_dossier_lane import Harness, FakeModel, ACN
        from dalton_core import company_dossier_cli as cli
        h=Harness(); self.addCleanup(h.close)
        h.run(max_units=12)
        h.tag('new-business','business_model',statement='又一项关于合同模式的新观察。')
        original=cli.plan_units
        def one_unit(**kwargs):
            plan=original(**kwargs)
            for key,entry in plan.items():
                if key!='business_model':entry['status']='unavailable'
            return plan
        with patch.object(cli,'plan_units',one_unit):
            refused_factory=lambda: self.fail('a single new Claim admitted a model')
            wait=h.run(model_factory=refused_factory)
            self.assertEqual(wait['dossier_status'],'nothing_new')
            corpus=Corpus(h.state_dir)
            for index in range(3):
                corpus.add('broker_report',f'Accenture ACN {index}', 'Accenture ACN '+FILL*12,
                    company=ACN,date=self.now.date().isoformat(),now=(self.now-timedelta(minutes=1)).isoformat())
            model=FakeModel(); verifier=FakeModel(route='route:verify')
            result=h.run(model_factory=lambda:model,verifier_model_factory=lambda:verifier)
            self.assertEqual(result['dossier_status'],'no_new_evidence')
            self.assertEqual(set(result['input_consumption']),{'business_model'})
            self.assertEqual(len(model.prompts)+len(verifier.prompts),2)
            again=h.run(model_factory=refused_factory)
            self.assertEqual(again['dossier_status'],'nothing_new')

    def test_model_eps_baseline_requires_comparable_identity_and_new_actual_input(self):
        old={'company_ref':SYNW,'version_ref':'model:old','content_hash':'a'*64,
             'ntm_eps':'10','quarters':['2026-12-31','2027-03-31','2027-06-30','2027-09-30'],
             'method':'ntm_common_income_over_day_weighted_diluted_shares','unit':'usd_per_share'}
        with patch.object(consumption,'model_baselines',return_value={SYNW:old}):
            captured=self.snapshot(); self.save(captured)
        current={**old,'version_ref':'model:new','content_hash':'b'*64,'ntm_eps':'11'}
        with patch.object(consumption,'model_baselines',return_value={SYNW:current}):
            self.assertFalse(self.gate(captured)['allowed'])  # EPS not yet present in new prompt
            changed={**captured,'input_hash':'c'*64}
            result=self.gate(changed)
            self.assertTrue(result['allowed'])
            reason=next(r for r in result['reasons'] if r['code']=='model_eps')
            self.assertEqual(reason['refs'],['model:old','model:new']);self.assertEqual(reason['pct'],'10.00')
            same_value={**current,'ntm_eps':'10.01'}
        with patch.object(consumption,'model_baselines',return_value={SYNW:same_value}):
            self.assertFalse(self.gate(changed)['allowed'])
        different_period={**current,'quarters':['2027-03-31','2027-06-30','2027-09-30','2027-12-31']}
        with patch.object(consumption,'model_baselines',return_value={SYNW:different_period}):
            reason=next(r for r in self.gate(changed)['reasons'] if r['code']=='model_eps')
            self.assertEqual(reason['comparison_kind'],'model_basis_changed')
            self.assertNotIn('pct',reason)

    def test_same_fired_policy_code_retains_distinct_stable_delta_evidence(self):
        def probe():
            return policy.lane_probe(self.state,self.core.connection,product='dossier',target=SYNW,
                company_refs=[SYNW],prior=self.prior,now=self.now)
        first={'code':'consensus_material','fired':True,'refs':['consensus:1'],
               'detail':'display text','changes':[{'metric':'eps','from':'10','to':'11'}]}
        second={**first,'refs':['consensus:2'],'changes':[{'metric':'eps','from':'10','to':'12'}]}
        with patch.object(policy,'_consensus_reason',return_value=first): a=probe()
        with patch.object(policy,'_consensus_reason',return_value=second): b=probe()
        self.assertNotEqual(a['fingerprint'],b['fingerprint']);self.assertTrue(a['urgent']);self.assertTrue(b['urgent'])

    def test_scope_company_product_unit_and_industry_isolation(self):
        snap=self.snapshot([self.arrive('a')]); self.save(snap)
        rows=ei.material_arrivals(self.state,company_ref=SYNW,include_backfill=True)
        for binding in (consumption.scope('dossier','company:other','economics',['company:other']),
                        consumption.scope('industry_framework','industry:other','economics',[SYNW]),
                        consumption.scope('dossier',SYNW,'risks',[SYNW])):
            self.assertEqual(consumption.pending_arrivals(self.state,binding,rows),rows)


if __name__=='__main__': unittest.main()
