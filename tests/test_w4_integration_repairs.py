"""Regressions at the boundaries between independently delivered W4 slices."""
import ast
import copy
import json
import unittest
from pathlib import Path

from dalton_core.answer_routing import _route_budget, AnswerRoutingValidationError
from dalton_core.company_dossier import CompanyDossierAuthority, SECTIONS
from dalton_core.event_judgement import derived_context
from dalton_core.market_price_adapter import daily_prices_wire
from dalton_core.research_event import PAYLOAD_FIELDS, validate_payload, ResearchEventValidationError
from scripts.build_mission_v2_params import build_next_version_params
from tests.test_dossier_lane import Harness, FakeModel, ACN
from tests.test_hkex_filings_adapter import capture, COMPANY
from dalton_core.hkex_filings_adapter import parse_capture, buyback_events
from dalton_core.hkex_filings_core import NEXT_DAY_DISCLOSURE_OPERATION
from tests.test_market_price_authority import AuthorityTestCase
from tests.test_market_price_adapter import prices_fixture, SINK

ROOT = Path(__file__).resolve().parents[1]


class SharedEventTests(unittest.TestCase):
    def test_event_contract_has_no_shadowed_dictionary_keys(self):
        tree = ast.parse((ROOT / 'src/dalton_core/research_event.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
                self.assertEqual(len(keys), len(set(keys)), keys)

    def test_cumulative_counts_cannot_lose_their_basis(self):
        for payload in ({'cumulative_shares': '5'}, {'cumulative_basis': 'since_mandate'},
                        {'cumulative_shares': '5', 'cumulative_basis': 'guess'}):
            with self.subTest(payload=payload), self.assertRaises(ResearchEventValidationError):
                validate_payload('buyback_disclosure', payload)
        for basis in ('calendar_year', 'since_mandate', 'fiscal_year'):
            self.assertEqual(validate_payload('buyback_disclosure', {
                'cumulative_shares': '5', 'cumulative_basis': basis,
            })['cumulative_basis'], basis)

    def test_hk_price_comparison_reaches_the_judgement_prompt(self):
        import sqlite3
        wire = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION,
                             capture('next-day-disclosure-00700-20260909.json'),
                             ticker='00700', artifact_hash='a' * 64)
        event = buyback_events(wire, company_ref=COMPANY, invocation_ref='inv:hk',
                              artifact_hash='a' * 64)[0]
        event['id'] = 'research-event:hk'
        with sqlite3.connect(':memory:') as connection:
            block = derived_context(event, connection=connection, price={
                'close': '400.00', 'currency': 'HKD', 'version_ref': 'price:hk:1'})
            self.assertEqual(block['context']['price_vs_current']['premium_to_current'], '0.091770')
            self.assertIn('price:hk:1', block['refs'])
            self.assertIn('since mandate', block['lines'][0])
            wrong_currency = derived_context(event, connection=connection, price={
                'close': '400.00', 'currency': 'USD', 'version_ref': 'price:wrong'})
            self.assertEqual(wrong_currency['context']['price_vs_current']['status'], 'unavailable')
            self.assertNotIn('price:wrong', wrong_currency['refs'])

    def test_published_payload_contract_matches_the_ledger(self):
        schema = json.loads((ROOT / 'contracts/buyback-disclosure-payload.schema.json').read_text())
        self.assertEqual(set(schema['properties']), set(PAYLOAD_FIELDS['buyback_disclosure']))
        self.assertFalse(schema['additionalProperties'])


class DossierOrderTests(unittest.TestCase):
    def test_first_dossier_uses_its_new_classification_without_reordering_sections(self):
        harness = Harness()
        self.addCleanup(harness.close)
        model = FakeModel()
        summary = harness.run(max_units=12, model_factory=lambda: model)
        self.assertEqual(summary['dossier_status'], 'published')
        classification = next(i for i, p in enumerate(model.prompts) if 'Part: industry_classification' in p)
        demand = next(i for i, p in enumerate(model.prompts) if 'Part: demand_drivers' in p)
        self.assertLess(classification, demand)
        self.assertIn('DRIVER TEMPLATE (contract_compounder)', model.prompts[demand])
        record = CompanyDossierAuthority(harness.store).latest(ACN)
        self.assertEqual([s['aspect'] for s in record['sections']], list(SECTIONS))
        old_hash = record['content_hash']
        harness.run(max_units=12)
        self.assertEqual(CompanyDossierAuthority(harness.store).dossier(record['id'])['content_hash'], old_hash)


class MissionSourceTests(unittest.TestCase):
    def test_five_inventory_sources_append_without_changing_the_active_mission(self):
        harness = Harness()
        self.addCleanup(harness.close)
        active = harness.mission
        before = copy.deepcopy(active)
        sources = ['source:sales-notes', 'source:company-wiki', 'source:xueqiu', 'source:x', 'source:blind']
        params = build_next_version_params(active, add_scopes=[], source_statuses={s: 'connected' for s in sources})
        self.assertEqual(active, before)
        self.assertEqual(params['source_plan'][:len(active['source_plan'])], active['source_plan'])
        for source in sources:
            rows = [r for r in params['source_plan'] if r['source_ref'] == source]
            self.assertEqual(len(rows), 1)
            self.assertIn('created_by=set-source-status', rows[0]['role'])
        # Real writer validation must accept the generated closed shape.
        params['actor_ref'] = 'human:lumos'
        next_mission = harness.missions.create_mission(params.pop('mission_ref'), **params)
        again = build_next_version_params(next_mission, add_scopes=[], source_statuses={s: 'connected' for s in sources})
        self.assertEqual(again['source_plan'], next_mission['source_plan'])
        with self.assertRaisesRegex(ValueError, 'inventory'):
            build_next_version_params(active, add_scopes=[], source_statuses={'source:invented': 'connected'})


class AskPolicyTests(unittest.TestCase):
    def test_enabled_and_disabled_routes_match_the_published_budget_contract(self):
        schema = json.loads((ROOT / 'contracts/answer-sufficiency-policy-version.schema.json').read_text())
        route_schema = schema['properties']['adhoc_research_route']
        self.assertEqual(route_schema['properties']['enabled'], {'type': 'boolean', 'default': False})
        for enabled, cost, rounds in ((False, 0, 0), (True, 5, 2)):
            value = dict(enabled=enabled, max_cost_units=cost, max_rounds=rounds)
            self.assertEqual(_route_budget(value, 'adhoc'), value)
        for enabled, cost, rounds in ((True, 0, 2), (True, 5, 0), (False, 5, 2)):
            with self.assertRaises(AnswerRoutingValidationError):
                _route_budget(dict(enabled=enabled, max_cost_units=cost, max_rounds=rounds), 'adhoc')


class HKPriceIntegrationTests(AuthorityTestCase):
    def test_one_hkd_day_passes_adapter_and_price_authority(self):
        raw = prices_fixture(ticker='0700.HK')
        raw['rows'] = raw['rows'][:1]
        raw['metadata']['currency'] = 'HKD'
        wire = daily_prices_wire(raw, source_record_refs=[SINK])
        published = self.publish(wire['bars'], company_ref=COMPANY, ticker='0700.HK', currency='HKD')
        close = self.authority.latest_close(COMPANY)
        self.assertEqual(close['currency'], 'HKD')
        self.assertEqual(close['version_ref'], published['id'])


from tests.test_cockpit_int2 import Int2Case


class ZeroBaseCockpitIntegrationTests(Int2Case):
    def test_zero_base_sibling_candidate_appears_with_its_review_until_decided(self):
        # Projection fixture deliberately uses only the columns the read path
        # needs; authority/candidate writes have separate full-schema tests.
        core = self.store.connection
        core.executescript('''
            CREATE TABLE zero_base_review_versions(version_id TEXT PRIMARY KEY, record_json TEXT);
            CREATE TABLE zero_base_revision_candidates(candidate_id TEXT PRIMARY KEY,
                content_hash TEXT, created_at TEXT, record_json TEXT);
            CREATE TABLE thesis_revision_decisions(candidate_ref TEXT, terminal INTEGER);
        ''')
        narrative = {'title': '从零复盘', 'sections': [
            {'heading': '四、下一个验证点与日期', 'body': '2026-10-01：业绩'}]}
        core.execute('INSERT INTO zero_base_review_versions VALUES (?,?)',
                     ('zero:1', json.dumps({'narrative': narrative})))
        core.execute('INSERT INTO zero_base_revision_candidates VALUES (?,?,?,?)', (
            'candidate:zero:1', 'a' * 64, '2026-09-10T00:00:00+00:00', json.dumps({
                'review_version_ref': 'zero:1', 'company_ref': ACN,
                'decision': 'THESIS_WEAKENED', 'because': '重新审视后需要改写'})))
        core.commit()
        item = next(row for row in self.plane.approvals()['items'] if row['ref'] == 'candidate:zero:1')
        self.assertEqual(item['kind'], 'thesis_revision_candidate')
        self.assertEqual(item['zero_base_review'], narrative)
        self.assertEqual({a['decision'] for a in item['actions']}, {'accept', 'reject', 'defer'})
        core.execute('INSERT INTO thesis_revision_decisions VALUES (?,?)', ('candidate:zero:1', 1))
        core.commit()
        self.assertNotIn('candidate:zero:1', [r['ref'] for r in self.plane.approvals()['items']])


class RehearsalResultTests(unittest.TestCase):
    def test_an_escaped_lane_makes_the_rehearsal_step_fail(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from scripts.rehearse_deploy import Rehearsal
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rehearsal = Rehearsal(root / 'source', root / 'temp',
                                  openclaw_config=root / 'catalog.json', log=lambda _: None)
            rehearsal.temp_config.parent.mkdir(parents=True)
            rehearsal.temp_home.mkdir(parents=True)
            rehearsal.temp_config.write_text(json.dumps({'bounded_planner': {'config': {}}}))
            rehearsal.confined = True
            with patch('dalton_core.service.ServiceConfig.from_file', return_value=SimpleNamespace(tick_seconds=5)), \
                 patch('dalton_core.bounded_planner_driver.BoundedPlannerDriverConfig.from_mapping'), \
                 patch('dalton_core.bounded_planner_driver.BoundedPlannerDriver') as driver:
                driver.return_value.run_once.return_value = {'event_judgement': {'status': 'unavailable:RuntimeError'}}
                result = rehearsal.step('one controller tick', rehearsal.run_tick)
            self.assertFalse(result.ok)
            self.assertIn('event_judgement', result.detail)
            self.assertIn('escaped', result.detail)
