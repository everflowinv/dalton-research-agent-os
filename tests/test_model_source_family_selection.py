"""Real source families precede stale lone adjustments without changing authority."""
import copy
import unittest
from dalton_core.company_model_identity_catalog import source_family_revenue_groups,coherent_revenue_groups,SOURCE_FAMILY_SELECTION_REF
from dalton_core.model_revenue_group_candidates import candidates
from dalton_core.store import content_hash
from tests import test_model_revenue_group_candidates as fixture
from tests import test_company_model_state as state_fixture
from dalton_core.company_model_state import build_company_model_state
from dalton_core.company_model_spec import build_prompt


def sources():
    current=[fixture.cell('latest-revenue',120),fixture.cell('latest-cost',80,concept='us-gaap:CostOfRevenue'),
             fixture.cell('latest-net',20,concept='us-gaap:NetIncomeLoss')]
    old=[fixture.cell('old-total',100,period_start='2025-01-01',period_end='2025-03-31'),
         fixture.cell('old-a',40,{'segments':'a'},period_start='2025-01-01',period_end='2025-03-31'),
         fixture.cell('old-b',59,{'segments':'b'},period_start='2025-01-01',period_end='2025-03-31')]
    adjustments=[]
    for n in range(5):
        adjustments.extend([fixture.cell(f'hedge{n}',1,{'risk':'fx','equity':'aoci'},period_end=f'2026-0{5-n}-28'),
                            fixture.cell(f'total{n}',120,period_end=f'2026-0{5-n}-28')])
    return current,old,adjustments


class SourceFamilySelectionTests(unittest.TestCase):
    def test_latest_income_and_whole_disclosure_family_beat_many_adjustment_windows(self):
        current,old,adjustments=sources();groups=[[c]for c in [*adjustments,*old,*current]]
        result=source_family_revenue_groups(groups)
        self.assertEqual({c['line_id']for c in result[0]},{c['line_id']for c in current})
        self.assertEqual({c['line_id']for c in result[1]},{c['line_id']for c in old})
        # The legacy ordering places all newer dated hedge packages before this family.
        prior=coherent_revenue_groups(groups)
        self.assertNotIn('old-a',{c['line_id']for g in prior[:2]for c in g})
        self.assertEqual({content_hash(c)for g in result for c in g},{content_hash(c)for g in groups for c in g})
        self.assertEqual(sum(map(len,result)),sum(map(len,groups)))
        selection=candidates([c for g in result[:2]for c in g])['groups'][0]
        self.assertEqual(selection['members_to_group'],'not_closed')
        self.assertEqual((selection['member_sum'],selection['reported_group_total']),('99','100'))

    def test_current_family_is_atomic_with_its_full_income_chain(self):
        current,old,_=sources()
        for cell in old:cell.update(period_start='2026-04-01',period_end='2026-06-30')
        # The reported same-concept total must be exact, not a foreign amount.
        old=old[1:];old[1]['value']='80'
        result=source_family_revenue_groups([[c]for c in [*current,*old]])
        self.assertEqual(len(result),1)
        self.assertEqual({c['line_id']for c in result[0]}, {c['line_id']for c in [*current,*old]})

    def test_no_reported_source_group_keeps_legacy_atomic_fallback(self):
        _,old,adjustments=sources()
        for mutated in [old[1:], [old[0],old[1],{**old[2],'statement_context_ref':'foreign'}],
                        [old[0],old[1],{**old[2],'unit':'EUR'}],
                        [old[0],old[1],{**old[2],'status':'ambiguous'}]]:
            groups=[[c]for c in [*adjustments,*mutated]]
            self.assertEqual(source_family_revenue_groups(groups),coherent_revenue_groups(groups))

    def test_multiple_derived_axes_share_source_once_without_inventing_residuals(self):
        current,old,_=sources()
        old.extend([fixture.cell('old-east',50,{'geo':'east'},period_start='2025-01-01',period_end='2025-03-31'),
                    fixture.cell('old-west',50,{'geo':'west'},period_start='2025-01-01',period_end='2025-03-31')])
        result=source_family_revenue_groups([[c]for c in [*current,*old]])
        self.assertEqual(len(result[1]),5);self.assertEqual(sum(map(len,result)),8)
        self.assertEqual([g['members_to_group']for g in candidates(result[1])['groups']],['not_closed','exact'])

    def test_actual_state_cell_and_byte_caps_preserve_latest_income_and_complete_old_family(self):
        f=state_fixture.CompanyModelStateTests();f.setUp();self.addCleanup(f.doCleanups)
        line=state_fixture._line;rows=[]
        periods=[('2026-04-01','2026-06-30'),('2026-01-01','2026-03-31'),('2025-10-01','2025-12-31'),
                 ('2025-07-01','2025-09-30'),('2025-04-01','2025-06-30'),('2024-10-01','2024-12-31'),
                 ('2024-07-01','2024-09-30'),('2024-04-01','2024-06-30')]
        for start,end in periods:
            rows.extend([line('us-gaap:Revenues',value='100',period_start=start,period_end=end),
                         line('us-gaap:NetIncomeLoss',value='20',period_start=start,period_end=end),
                         line('us-gaap:Revenues',value='1',period_start=start,period_end=end,is_breakdown=True,
                              dimension_axis='risk:Axis',dimension_member='risk:FX')])
        old_revenue='us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'
        rows.extend([line(old_revenue,value='100',period_start='2025-01-01',period_end='2025-03-31'),
            line(old_revenue,value='40',period_start='2025-01-01',period_end='2025-03-31',is_breakdown=True,dimension_axis='segment:Axis',dimension_member='segment:A'),
            line(old_revenue,value='59',period_start='2025-01-01',period_end='2025-03-31',is_breakdown=True,dimension_axis='segment:Axis',dimension_member='segment:B')])
        f.ingest('0001467373-26-000031',rows)
        # This fixture models the admitted full-context reader (the legacy SQL
        # fixture itself only has first-axis columns). Production uses the
        # already hash-verified StatementContextAuthority, exercised separately
        # by the actual-source replay receipt.
        filings=f.missions.statement_filings(state_fixture.ACN)
        by_ingest={item['ingest_id']:f.missions.statement_lines(item['ingest_id'])for item in filings}
        for held in by_ingest.values():
            for row in held:
                row['dimensions']={row['dimension_axis']:row['dimension_member']} if row['dimension_axis'] else {}
                row['statement_context_ref']='statement-context:synthetic-complete'
        class Frozen:
            def statement_filings(self,company):return filings
            def statement_lines(self,ingest):return by_ingest[ingest]
        state=build_company_model_state(Frozen(),state_fixture.ACN,numeric_context_policy={'max_periods_per_series':8,'max_total_cells':5})
        cells=state['numeric_context']['cells'];self.assertEqual(len(cells),5)
        self.assertEqual({c['period_end']for c in cells},{'2025-03-31','2026-06-30'})
        self.assertEqual(state['numeric_context']['selection_contract'],SOURCE_FAMILY_SELECTION_REF)
        self.assertEqual(state['numeric_context']['revenue_group_candidates']['groups'][0]['members_to_group'],'not_closed')
        self.assertEqual(state['numeric_context']['prompt_bytes'],len(build_prompt(state).encode()))
        self.assertTrue(state['numeric_context']['truncated'])
