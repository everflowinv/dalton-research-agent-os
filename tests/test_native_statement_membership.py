import base64
import copy
import unittest
from unittest.mock import patch

from dalton_core.native_statement_membership import candidates, validate, FIELD
from dalton_core.store import content_hash
from tests.test_sec_native_filing_units import NativeUnitPipelineTests


def fixture(concept='issuer:Deposits'):
    row = {'concept': concept, 'label': 'Deposits', 'level': 1,
           'abstract': False, 'parent_concept': 'issuer:Assets',
           'is_breakdown': False, 'dimension_axis': None, 'dimension_member': None,
           '2025-12-31': 37.0}
    fact = {'concept': concept, 'period_start': None, 'period_end': '2025-12-31',
            'unit_ref': 'USD', 'value': '37', 'dim_unused:Axis': None}
    return {'statements': {'balance': {'structure': [row], 'facts': []},
                           'cash': {'structure': [], 'facts': [fact]}}}


class MembershipCandidatesTests(unittest.TestCase):
    def test_explicit_model_statement_selects_only_a_present_source(self):
        from dalton_core.company_financial_statement_structure import _presentation_filed
        source = {'concept':'issuer:Deposits', 'unit':'USD', 'period_kind':'instant'}
        state = {'filings':[{'accession':'a'}], 'statements':{
            'cash':[source], 'balance':[source], 'income':[]}}
        chosen, _ = _presentation_filed(state, statement_hints={'issuer:Deposits':'balance'})
        self.assertEqual(chosen['issuer:Deposits']['statement'], 'balance')
        state['statements']['balance'] = []
        chosen, _ = _presentation_filed(state, statement_hints={'issuer:Deposits':'balance'})
        self.assertEqual(chosen['issuer:Deposits']['statement'], 'cash')

    def test_annotation_repair_does_not_buy_an_unchangeable_source_mismatch(self):
        from tests.test_model_spec_joint_field_diagnostics import case
        from tests.test_model_spec_cash_flow_base_repair import root_call
        from dalton_core import company_model_cli as cli, model_spec_joint_field_diagnostics as joint
        from dalton_core.company_model_spec import CompanyModelSpecError
        state, bad, _ = case()
        # The filed amount belongs to another statement and is not a permitted
        # net-income carry. None of the finite annotation paths can repair it.
        cost = next(r for r in state['statements']['income'] if r['concept']=='us-gaap:CostOfRevenue')
        state['statements']['income'].remove(cost)
        state['statements']['cash'].append(cost)
        class Model:
            scheduler_db = None
            def call(self, **kwargs):
                raise AssertionError('known source mismatch must not buy annotation repair')
        with self.assertRaises(CompanyModelSpecError) as caught:
            cli._validated_spec_with_repair(model=Model(), state=state, mission={},
                original_call=root_call(bad), repair_config={'max_attempts':1}, decided_by='automation:test')
        self.assertEqual(caught.exception.code, joint.PREFLIGHT_CODE)
        self.assertIn('cost needs income/duration; held cash/duration', str(caught.exception))

    def test_two_unrelated_concepts_need_explicit_statement_period_and_value(self):
        for concept in ('issuer:Deposits', 'ifrs-full:CashAndCashEquivalents'):
            parsed = fixture(concept)
            got = candidates(parsed, [])
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]['line']['concept'], concept)
            self.assertEqual(got[0]['line']['statement'], 'balance')
            self.assertEqual(got[0]['line']['period_start'], None)
            self.assertEqual(candidates(parsed, [got[0]['line']]), [])

    def test_wrong_value_period_duration_unknown_or_segment_dimensions_refuse(self):
        for key, value in [('value', '38'), ('period_end', '2024-12-31'),
                           ('period_start', '2025-01-01'), ('dim_unused:Axis', 'issuer:Segment')]:
            parsed = fixture(); parsed['statements']['cash']['facts'][0][key] = value
            self.assertEqual(candidates(parsed, []), [], key)
        parsed = fixture(); del parsed['statements']['cash']['facts'][0]['dim_unused:Axis']
        self.assertEqual(candidates(parsed, []), [])
        parsed = fixture(); parsed['statements']['balance']['structure'][0].pop('2025-12-31')
        self.assertEqual(candidates(parsed, []), [])

    def test_conflicting_fact_unit_or_value_cannot_choose_the_matching_one(self):
        for key, value in [('value', '38'), ('unit_ref', 'CAD')]:
            parsed = fixture(); facts = parsed['statements']['cash']['facts']
            facts.append({**facts[0], key: value})
            self.assertEqual(candidates(parsed, []), [])

    def test_inexact_float_nan_and_bool_presentations_do_not_establish_membership(self):
        for value in (float('nan'), 37.1, float(2 ** 54), True):
            parsed = fixture(); parsed['statements']['balance']['structure'][0]['2025-12-31'] = value
            self.assertEqual(candidates(parsed, []), [])

    def test_no_balance_structure_means_no_inferred_role(self):
        parsed = fixture(); parsed['statements'].pop('balance')
        self.assertEqual(candidates(parsed, []), [])


class MembershipPipelineTests(unittest.TestCase):
    setUp = NativeUnitPipelineTests.setUp
    run_once = NativeUnitPipelineTests.run_once
    adapter = NativeUnitPipelineTests.adapter
    root = NativeUnitPipelineTests.root
    profile = NativeUnitPipelineTests.profile
    body = NativeUnitPipelineTests.body
    admitted = NativeUnitPipelineTests.admitted
    captured = NativeUnitPipelineTests.captured
    captured_opaque = NativeUnitPipelineTests.captured_opaque
    summary = NativeUnitPipelineTests.summary

    def stored(self, *, missing_xml=False):
        from dalton_core.sec_native_annual import build_document_set
        m, request, dispatch, governance, gp, path = self.admitted()
        raw = self.captured_opaque(request)
        parsed = raw['filings'][0]
        view = fixture('ifrs-full:CashAndCashEquivalents')
        parsed['statements']['balance']['structure'].extend(view['statements']['balance']['structure'])
        fact = view['statements']['cash']['facts'][0]
        fact['unit_ref'] = 'opaque-money'
        parsed['statements']['cash']['facts'].append(fact)
        # The cash statement presents this instant as well; the original
        # parser classifies the common fact under cash only.
        parsed['statements']['cash']['structure'].append({
            **view['statements']['balance']['structure'][0], 'label': 'Ending deposits'})
        original = parsed['native_document_set']
        bodies = {x['document']: base64.b64decode(x['base64']) for x in original['originals']}
        if not missing_xml:
            bodies['facts.xml'] = bodies['facts.xml'].replace(b'</xbrli:xbrl>',
                b'<ifrs-full:CashAndCashEquivalents contextRef="instant" unitRef="opaque-money">37</ifrs-full:CashAndCashEquivalents></xbrli:xbrl>')
        parsed['native_document_set'] = build_document_set(issuer=original['issuer'],
            filing=original['filing'], reporting_profile_hash=original['reporting_profile_hash'],
            members=original['members'], originals=bodies)
        summary = self.summary(request, gp, path, raw)
        self.assertEqual(summary['status'], 'succeeded', summary.get('failure_reason'))
        filing = m.record_statement_observation(dispatch_id=dispatch['dispatch_id'],
            observation=summary['observation'], governance_ref=governance['id'],
            governance_hash=governance['content_hash'], governance_proof=governance)['filings'][0]
        return m, filing

    def test_normal_owner_refresh_preserves_original_then_model_reads_new_balance(self):
        from dalton_core.statement_context import (StatementContextAuthority, context_record,
            base_statement_rows, refresh_company_contexts)
        from dalton_core.company_model_inputs import build_model_inputs
        from tests.test_company_model_inputs import _spec
        m, filing = self.stored()
        old = copy.deepcopy(base_statement_rows(m.connection, filing['ingest_id']))
        authority = StatementContextAuthority(self.core)
        with patch('dalton_core.native_statement_membership.supplement', side_effect=lambda c,s,f,p,rows:(rows,None)):
            earlier = authority.backfill(self.state, filing['ingest_id'], discover_companyfacts=False)
        fresh = authority.backfill(self.state, filing['ingest_id'], discover_companyfacts=False)
        self.assertEqual(fresh['status'], 'available', fresh.get('reason'))
        self.assertEqual(len(fresh[FIELD]['entries']), 1)
        self.assertEqual(context_record(m.connection, fresh['version_id']), fresh)
        self.assertEqual(context_record(m.connection, earlier['version_id']), earlier)
        self.assertEqual(base_statement_rows(m.connection, filing['ingest_id']), old)
        self.assertTrue(refresh_company_contexts(self.core, self.state, filing['company_ref'])[0]['reused'])
        spec = _spec(drivers=[{'ref':'revenue', 'label':'Revenue', 'kind':'mix',
            'basis_concept':'ifrs-full:Revenue', 'unit':'USD', 'because':'Filed'}], expenses=[])
        spec['schema_version'] = '0.4'
        spec['company_ref'] = filing['company_ref']
        spec['financial_statement_structure'] = {'schema_version':'0.7', 'lines':[
            {'ref':'cash', 'kind':'filed', 'statement':'balance',
             'concept':'ifrs-full:CashAndCashEquivalents'}], 'formulas':[]}
        inputs = build_model_inputs(m, spec)
        line = next(x for x in inputs['filed_lines'] if x['concept'] == 'ifrs-full:CashAndCashEquivalents')
        self.assertEqual(line['statement'], 'balance')
        self.assertEqual(line['cells']['2025-12-31']['value'], '37')
        self.assertTrue(all(r['statement_context_ref'] == fresh['version_id'] for r in line['source_evidence']['rows']))
        forged = copy.deepcopy(fresh)
        forged[FIELD]['entries'][0]['structure_hash'] = '0' * 64
        forged[FIELD]['content_hash'] = content_hash({k:v for k,v in forged[FIELD].items() if k != 'content_hash'})
        with self.assertRaisesRegex(ValueError, 'replay'):
            validate(m.connection, forged, filing)

    def test_missing_xml_fact_refuses_without_borrowing_parser_number(self):
        from dalton_core.statement_context import prepare_context
        m, filing = self.stored(missing_xml=True)
        result = prepare_context(m.connection, self.state, filing['ingest_id'], discover_companyfacts=False)
        self.assertEqual(result['status'], 'refused')
        self.assertIn('original fact proof', result['reason'])
