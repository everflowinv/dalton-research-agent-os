import copy
import unittest
from unittest.mock import patch

from dalton_core.forecast_revenue_timing_partition import AXIS, MEMBERS, CONTRACT_REF
from dalton_core.forecast_execution_statement_scope import execution_scope, evaluate_execution, RULE_REF, TIMING_RULE_REF
from dalton_core.forecast_consumed_source_groups import select_rows
from dalton_core.forecast_statement_scope import statement_scope
from dalton_core.model_forecast_driver import ForecastModelValidationError
from dalton_core.store import content_hash


class RevenueTimingPartitionTests(unittest.TestCase):
    def fixture(self, company='company:manufacturer'):
        from tests.test_forecast_execution_statement_scope import ExecutionStatementScopeTests
        record, filings, sources = ExecutionStatementScopeTests('runTest').fixture(company)
        mapping = dict(zip(('a', 'b'), sorted(MEMBERS)))
        for d in record['drivers']:
            d['concept'] = 'us-gaap:Revenues'
            for c in d['history']:
                c['concept'] = 'us-gaap:Revenues'
            if d.get('statement_identity'):
                old = next(iter(d['statement_identity']['dimensions'].values()))
                d['statement_identity']['dimensions'] = {AXIS: mapping[old]}
        spec = record['execution_contract']['specification']
        for d in spec['revenue_drivers']:
            old = next(iter(d['basis_identity']['dimensions'].values()))
            d['basis_concept'] = 'us-gaap:Revenues'
            d['basis_identity']['dimensions'] = {AXIS: mapping[old]}
        record['financial_statement_structure']['lines'][0]['concept'] = 'us-gaap:Revenues'
        for rows in sources.values():
            for r in rows:
                r['concept'] = 'us-gaap:Revenues'
                if r['dimensions']:
                    r.update(dimensions={AXIS: mapping[r['dimension_member']]}, dimension_axis=AXIS,
                             dimension_member=mapping[r['dimension_member']])
        return record, filings, sources

    def run_scope(self, record, filings, sources):
        rows, groups = select_rows(record, filings, sources)
        kept, scope = statement_scope(record, rows, source_groups=groups)
        return kept, scope, evaluate_execution(record, statement_rows=kept, scope=scope)

    def test_two_companies_closed_taxonomy_and_original_history(self):
        for company in ('company:manufacturer', 'company:software'):
            record, filings, sources = self.fixture(company)
            before = copy.deepcopy((record, sources))
            rows, scope, report = self.run_scope(record, filings, sources)
            self.assertEqual(report.status, 'available', report.reasons)
            self.assertEqual(scope['rule_ref'], TIMING_RULE_REF)
            self.assertEqual(len(scope['arithmetic_partitions']), 4)
            self.assertEqual((record, sources), before)
            for item in scope['arithmetic_partitions']:
                self.assertEqual(item['classification'], {'contract_ref': CONTRACT_REF, 'axis': AXIS, 'members': sorted(MEMBERS)})
                self.assertEqual({p['member'] for p in item['group']['parts']}, MEMBERS)
                self.assertEqual(item['source_identity'][0], 'ingest:old')

    def test_legacy_rule_stays_refused_and_non_timing_rule_cannot_be_relabeled(self):
        record, filings, sources = self.fixture()
        rows, groups = select_rows(record, filings, sources)
        for rule in (RULE_REF, 'rule:forecast-execution-statement-scope:2', 'rule:forecast-execution-statement-scope:4'):
            with self.assertRaisesRegex(ForecastModelValidationError, 'no unique additive axis'):
                execution_scope(record, rows, groups, rule_ref=rule)
        from tests.test_forecast_execution_statement_scope import ExecutionStatementScopeTests
        old, fs, ss = ExecutionStatementScopeTests('runTest').fixture()
        rs, gs = select_rows(old, fs, ss)
        with self.assertRaisesRegex(ForecastModelValidationError, 'explicitly declared timing root'):
            execution_scope(old, rs, gs, rule_ref=TIMING_RULE_REF)

    def test_missing_member_total_or_full_history_cannot_borrow_latest(self):
        for choice in ('member', 'total', 'history'):
            record, filings, sources = self.fixture()
            if choice == 'history':
                record['drivers'][0]['history'].pop(0)
            else:
                row = next(r for r in sources['ingest:old'] if bool(r['dimensions']) == (choice == 'member'))
                sources['ingest:old'].remove(row)
            with self.assertRaises(ForecastModelValidationError):
                self.run_scope(record, filings, sources)

    def test_custom_member_nonrevenue_fixed_and_two_axes_refused(self):
        for change in ('member', 'concept', 'fixed', 'two_axes'):
            record, filings, sources = self.fixture()
            if change == 'concept':
                for d in record['drivers']:
                    d['concept'] = 'issuer:RevenueSoundingButUnknown'
                    for c in d['history']: c['concept'] = d['concept']
                for d in record['execution_contract']['specification']['revenue_drivers']:
                    d['basis_concept'] = 'issuer:RevenueSoundingButUnknown'
                for rs in sources.values():
                    for r in rs: r['concept'] = 'issuer:RevenueSoundingButUnknown'
                record['financial_statement_structure']['lines'][0]['concept'] = 'issuer:RevenueSoundingButUnknown'
            else:
                extra = 'srt:ProductOrServiceAxis' if change == 'two_axes' else 'issuer:OtherAxis'
                for d in record['drivers']:
                    if d.get('statement_identity'):
                        if change == 'member': d['statement_identity']['dimensions'][AXIS] = 'issuer:OtherMember'
                        else: d['statement_identity']['dimensions'][extra] = 'issuer:FixedMember'
                for d in record['execution_contract']['specification']['revenue_drivers']:
                    if change == 'member': d['basis_identity']['dimensions'][AXIS] = 'issuer:OtherMember'
                    else: d['basis_identity']['dimensions'][extra] = 'issuer:FixedMember'
                for rs in sources.values():
                    for r in rs:
                        if r['dimensions']:
                            if change == 'member':
                                r['dimensions'][AXIS] = r['dimension_member'] = 'issuer:OtherMember'
                            else:
                                r['dimensions'][extra] = 'issuer:FixedMember'
                                r['dimension_count'] = 2
            with self.assertRaises(ForecastModelValidationError):
                self.run_scope(record, filings, sources)

    def test_duplicate_origin_refs_kept_and_conflict_unit_nonclosure_refused(self):
        record, filings, sources = self.fixture()
        for rs in sources.values():
            for r in list(rs):
                duplicate = copy.deepcopy(r); duplicate['line_id'] += ':original-duplicate'; duplicate['ordinal'] += 100
                rs.append(duplicate)
        _, scope, report = self.run_scope(record, filings, sources)
        self.assertEqual(report.status, 'available')
        self.assertTrue(all(len(o['statement_line_refs']) == 2 for p in scope['arithmetic_partitions'] for o in p['origins']))
        for field, value in (('value','101'), ('unit','EUR')):
            altered = copy.deepcopy(sources)
            altered['ingest:old'][-1][field] = value
            with self.assertRaises(ForecastModelValidationError): self.run_scope(record, filings, altered)
        record, filings, sources = self.fixture()
        sources['ingest:old'][0]['value'] = '101'
        with self.assertRaisesRegex(ForecastModelValidationError, 'do not equal'):
            self.run_scope(record, filings, sources)
        record, filings, sources = self.fixture()
        for d in record['drivers']: d['unit'] = 'shares'
        for rs in sources.values():
            for r in rs: r['unit'] = 'shares'
        with self.assertRaisesRegex(ForecastModelValidationError, 'original units'):
            self.run_scope(record, filings, sources)

    def test_free_forecast_digest_changes_only_declared_timing_and_preserves_spec(self):
        from tests.test_company_model_forecast import LaneStateTests, ACN
        from dalton_core.company_model_forecast import model_digest
        from dalton_core.company_model_inputs import build_model_inputs
        from dalton_core.forecast_revenue_timing_partition import declared
        case = LaneStateTests('runTest'); case.setUp(); self.addCleanup(case.doCleanups)
        spec = case.missions.latest_company_model_spec(ACN)
        table = build_model_inputs(case.missions, spec)
        untouched = model_digest(spec, table)
        with patch('dalton_core.forecast_revenue_timing_partition.declared', return_value=False):
            self.assertEqual(model_digest(spec, table), untouched)
        before = copy.deepcopy(spec)
        # No paid spec identity is altered; only a timing declaration receives
        # the corrected free forecast run epoch.
        with patch('dalton_core.forecast_revenue_timing_partition.declared', return_value=True):
            timing_digest = model_digest(spec, table)
        self.assertNotEqual(timing_digest, untouched)
        self.assertEqual(spec, before)
        self.assertFalse(declared(spec))

    def test_zero_supported_axes_is_distinct_from_multiple_axes_in_new_rule(self):
        record, filings, sources = self.fixture()
        second = sorted(MEMBERS)[1]
        for d in record['drivers']:
            if d.get('statement_identity',{}).get('dimensions',{}).get(AXIS) == second:
                d['statement_identity']['dimensions'] = {'issuer:UnsupportedAxis':second}
        for d in record['execution_contract']['specification']['revenue_drivers']:
            if d['basis_identity']['dimensions'][AXIS] == second:
                d['basis_identity']['dimensions'] = {'issuer:UnsupportedAxis':second}
        for rs in sources.values():
            for r in rs:
                if (r.get('dimensions') or {}).get(AXIS) == second:
                    r.update(dimensions={'issuer:UnsupportedAxis':second}, dimension_axis='issuer:UnsupportedAxis')
        rows, groups = select_rows(record, filings, sources)
        with self.assertRaisesRegex(ForecastModelValidationError,'unsupported_additive_axis: no supported axis'):
            execution_scope(record, rows, groups, rule_ref=TIMING_RULE_REF)

    def test_rehashed_scope_classification_origin_and_rule_do_not_bypass_replay(self):
        record, filings, sources = self.fixture()
        rows, scope, _ = self.run_scope(record, filings, sources)
        for choice in ('classification', 'origin', 'rule'):
            altered = copy.deepcopy(scope)
            entry = altered['arithmetic_partitions'][0]
            if choice == 'classification': entry['classification']['members'] = ['issuer:Fake']
            elif choice == 'origin': entry['origins'][0]['statement_line_refs'] = ['foreign-original']
            else: altered['rule_ref'] = RULE_REF
            entry['content_hash'] = content_hash({k:v for k,v in entry.items() if k != 'content_hash'})
            altered['content_hash'] = content_hash({k:v for k,v in altered.items() if k != 'content_hash'})
            with self.assertRaises(ForecastModelValidationError):
                evaluate_execution(record, statement_rows=rows, scope=altered)

    def test_normal_owner_publishes_replays_and_next_tick_is_nothing_to_model(self):
        from tests import test_period_source_family_execution as f
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.company_model_spec import spec_from_response
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        from tests.test_company_model_forecast import REVENUE_CONCEPT, ACN
        mapping = dict(zip(('issuer:FirstMember', 'issuer:SecondMember'), sorted(MEMBERS)))
        original = CoverageMissionAuthority.record_statement_observation
        def observation(owner, *, observation, **kw):
            observation = copy.deepcopy(observation)
            for filing in observation['filings']:
                for r in filing['lines']:
                    if AXIS in (r.get('dimensions') or {}):
                        r['dimensions'][AXIS] = mapping[r['dimensions'][AXIS]]
                        r['dimension_member'] = r['dimensions'][AXIS]
            return original(owner, observation=observation, **kw)
        def response(state, body, **kw):
            body = copy.deepcopy(body)
            body['revenue_drivers'] = [{'ref':ref, 'label':ref, 'kind':'segment', 'basis_concept':REVENUE_CONCEPT,
                'unit':'USD', 'because':'Original recognition timing classification.',
                'basis_identity':{'statement':'income','dimensions':{AXIS:member}}}
                for ref,member in zip(('first','second'),sorted(MEMBERS))]
            body['driver_formulas'] = [{'output_ref':'revenue','operator':'sum','driver_refs':['first','second'],
                                       'because':'Both original classifications exhaust reported revenue.'}]
            return spec_from_response(state, body, **kw)
        helper = f.PeriodFamilyExecutionTests('runTest'); self.addCleanup(helper.doCleanups)
        with patch.object(f,'AXIS',AXIS), patch.object(CoverageMissionAuthority,'record_statement_observation',observation), \
             patch.object(f,'spec_from_response',response):
            case = helper.fixture(static=True)
        result = case.child()
        self.assertEqual(result['forecast_status'],'published', result)
        authority = ForecastModelAuthority(case.store)
        model = authority.model(result['model_version_ref'])
        proof = authority.filing_proof(model['id'])
        self.assertEqual(proof['model_statement_scope']['rule_ref'],TIMING_RULE_REF)
        self.assertEqual(authority._filing_proof(model['id'],require_replay=True),proof)
        self.assertEqual(case.child()['forecast_status'],'nothing_to_model')
        self.assertEqual(case.missions.latest_company_model_spec(ACN)['spec_id'],model['spec_ref'])
