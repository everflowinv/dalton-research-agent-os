import copy
import unittest
from dalton_core.model_revenue_group_candidates import candidates
from dalton_core.company_model_identity_catalog import cell_identity_ref

def cell(ref,value,dimensions=None,**overrides):
    return {"statement":"income","concept":"us-gaap:Revenues","status":"filed","value":str(value),"unit":"USD",
            "filing_content_hash":"a"*64,"accession":"0000000001-26-000001","statement_context_ref":"statement-context:one",
            "period_start":"2026-04-01","period_end":"2026-06-30","dimensions":dimensions or {},"line_id":ref,
            "line_content_hash":ref.ljust(64,'0'),**overrides}

class ModelRevenueGroupTests(unittest.TestCase):
    def test_reported_aggregate_gap_and_alternate_closed_family_are_explicit_choices(self):
        rows=[cell('total',12),cell('ops',9,{'consolidation':'operating'}),
              cell('a',4,{'consolidation':'operating','segments':'a'}),cell('b',5,{'consolidation':'operating','segments':'b'}),
              cell('east',6,{'geography':'east'}),cell('west',6,{'geography':'west'})]
        groups=candidates(rows)['groups'];self.assertEqual(len(groups),2)
        ops=next(g for g in groups if g['axis']=='segments');self.assertEqual(ops['members_to_group'],'exact')
        self.assertEqual(ops['group_to_company'],'not_closed');self.assertIn('不得创造余额',ops['missing_reconciliation'])
        geo=next(g for g in groups if g['axis']=='geography');self.assertEqual(geo['group_to_company'],'exact')
        self.assertEqual({m['identity_ref'] for m in geo['members']},{cell_identity_ref(rows[4]),cell_identity_ref(rows[5])})
        self.assertNotIn('selected_group',candidates(rows))

    def test_same_identity_equal_presentations_not_double_counted_but_all_refs_retained(self):
        total=cell('total',10);a=cell('a',4,{'axis':'a'});alias=copy.deepcopy(a);alias.update(line_id='alias',line_content_hash='b'*64)
        rows=[total,a,alias,cell('b',6,{'axis':'b'})];first=candidates(rows)
        self.assertEqual(first,candidates(list(reversed(rows))))
        member=next(m for m in first['groups'][0]['members']if m['member']=='a');self.assertEqual(len(member['equivalent_refs']),2)
        self.assertEqual(first['groups'][0]['member_sum'],'10')
        alias['value']='5';self.assertEqual(candidates(rows)['groups'],[])

    def test_foreign_period_source_context_and_currency_cannot_complete_group(self):
        for change in ({'period_end':'2026-03-31'},{'statement_context_ref':'statement-context:foreign'},
                       {'filing_content_hash':'f'*64},{'accession':'foreign'},{'unit':'EUR'}):
            rows=[cell('total',10),cell('a',4,{'axis':'a'}),cell('b',6,{'axis':'b'},**change)]
            self.assertEqual(candidates(rows)['groups'],[])

    def test_partial_group_uses_reported_total_not_generated_remainder(self):
        result=candidates([cell('total',100),cell('a',30,{'axis':'a'}),cell('b',20,{'axis':'b'})])
        self.assertEqual(result['groups'][0]['members_to_group'],'not_closed')
        self.assertEqual([m['member']for m in result['groups'][0]['members']],['a','b'])
        self.assertNotIn('residual',result['groups'][0])

    def test_latest_each_family_and_bound_omissions_are_deterministic(self):
        rows=[]
        for axis in range(7):
            for end in ('2025-06-30','2026-06-30'):
                rows += [cell(f'{axis}t{end}',10,period_end=end),cell(f'{axis}a{end}',4,{f'axis{axis}':'a'},period_end=end),cell(f'{axis}b{end}',6,{f'axis{axis}':'b'},period_end=end)]
        result=candidates(rows);self.assertEqual(len(result['groups']),4)
        self.assertEqual(result['omitted_group_count'],3)
        self.assertEqual({g['period_end']for g in result['groups']},{'2026-06-30'})
        self.assertEqual(result,candidates(list(reversed(rows))))
