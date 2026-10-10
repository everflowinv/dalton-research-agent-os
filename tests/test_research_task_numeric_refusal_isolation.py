"""Known numeric admission refusals are local; successful writes remain real."""
import contextlib
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import PublicWebDocumentSourceAdapter
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.research_task_cli import run_admissions
from dalton_core.store import content_hash
from tests import test_directed_numeric_workflow as numeric_workflow
from tests.test_document_research import FakeLauncher
from tests.test_mission_annual_research import COMPANY
from tests.test_research_plan_annual_report import AnnualSourceHarness


class NumericAdmissionRefusalIsolationTests(unittest.TestCase):
    def fixture(self):
        numeric = numeric_workflow.NumericWorkflowTests('runTest')
        numeric.addCleanup = self.addCleanup
        f, authority, prior, executor, producer, verifier = numeric.fixture()
        # A second acquired original uses actual synthetic connector receipts,
        # review and registration, graded as a transcript by its discovery.
        from dalton_core.public_web_core_search import PublicWebCoreSearch
        original_request = PublicWebCoreSearch.build_request
        def distinct_discovery(search, parameters, **kwargs):
            return original_request(search, {**parameters, 'query': 'synthetic earnings transcript'}, **kwargs)
        with patch.object(PublicWebCoreSearch, 'build_request', distinct_discovery):
            source = AnnualSourceHarness(f.harness.planner, accession='0001234567-26-000099')
        self.addCleanup(source.close)
        source.ticket_ref = 'public-web-fetch:' + 'b' * 24
        coverage = CoverageMissionAuthority(f.store)
        envelope = source.reader.get_source_envelope(source.manifest['discovery_source_envelope_ref'])
        invocation = source.reader.get_invocation(envelope['connector_invocation_ref'])
        authorization = coverage.authorize_source_discovery(
            company_ref=COMPANY, source_ref='source:web-search', requested_by='automation:test',
            mission_version_ref=f.mission['id'])
        discovery = coverage.record_source_discovery(
            authorization=authorization, discovery_plan_ref='discovery-plan:synthetic-transcript',
            discovery_plan_hash='b' * 64, spec_ref='earnings-call-transcripts', query_hash='c' * 64,
            parameters={'query': 'annual report', 'date_after': '2025-01-01', 'date_before': '2026-01-01'},
            connector_invocation_ref=invocation['id'], connector_invocation_hash=invocation['content_hash'],
            source_envelope_ref=envelope['id'], source_envelope_hash=envelope['content_hash'],
            document_refs=envelope['source_record_refs'], in_authority_document_refs=[])
        document_ref = source.manifest['url_ref']
        row = next(r for r in coverage.discovered_documents(mission_version_ref=f.mission['id'])
                   if r['discovery_ref'] == discovery['id'] and r['document_ref'] == document_ref)
        coverage.mark_discovered_document_launched(row['record_id'], source.ticket_ref)
        coverage.settle_discovered_document(row['record_id'], status='acquired')
        coverage.register_document_review(row['record_id'], requested_by='automation:test')
        bad_adapter = PublicWebDocumentSourceAdapter(
            source_ref='source:public-web', core=f.store, spool=source.spool, receipt_reader=source.reader,
            launcher=FakeLauncher(source.manifest, source.ticket_ref, lookup_ref=document_ref, state_dir=f.state),
            max_source_chars=100000, max_pdf_pages=100, max_decompressed_bytes=1000000)
        registry = authority.registry
        good_adapter = registry.adapters['source:public-web']

        class ExactSources:
            source_ref = 'source:public-web'
            def materialize(self, **kwargs):
                adapter = bad_adapter if kwargs['document_ref'] == document_ref else good_adapter
                return adapter.materialize(**kwargs)

        registry.adapters['source:public-web'] = ExactSources()
        registry.acquired_document_adapter.adapters['source:public-web'] = registry.adapters['source:public-web']
        bad_registration = registry.register_acquired_document(record_id=row['record_id'], purpose=numeric_workflow.PURPOSE)
        good_registration = prior['request']['registration']
        registrations = {r['id']: r for r in (bad_registration, good_registration)}
        authority.registration_resolver = registrations.get
        inquiries = []
        for rank, name, registration in ((0, 'spoken cash', bad_registration),
                                         (1, 'filed cash', good_registration),
                                         (2, 'filed working capital', good_registration)):
            inquiry = copy.deepcopy(prior['planner_inquiry'])
            inquiry.update(rank=rank, question=f'What is the FY2025 {name} amount?',
                           wants=f'The exact {name} amount in this original.')
            target = copy.deepcopy(inquiry['directed_document']['numeric_targets'][0])
            target.update(target_ref='target:isolated-' + str(rank), metric_ref='metric:isolated-' + str(rank),
                          metric_definition={'label': name, 'meaning': f'Synthetic issuer {name} at FY2025.'})
            target.pop('metric_definition_hash', None)
            from dalton_core.directed_numeric_research import normalize_targets
            inquiry['directed_document'].update(document_ref=registration['document_ref'],
                document_version_hash=registration['content_hash'], numeric_targets=normalize_targets([target]))
            inquiries.append(inquiry)
        plan = {'schema_version': '0.1', 'task_ref': 'task:research-plan-directives:0.1',
                'created_at': f.mission['created_at'], 'state_hash': 'a' * 64,
                'mission_version_ref': f.mission['id'], 'assessment': 'Read distinct exact synthetic facts.',
                'directives': [], 'inquiries': inquiries, 'sufficiency': []}
        plan['content_hash'] = content_hash(plan)
        plan = numeric._record_plan(f, plan)

        @contextlib.contextmanager
        def exact_runtime(**_kwargs):
            yield authority, registrations

        kwargs = dict(state_dir=f.state, summary_dir=f.state/'numeric-admission-summary',
                      max_admissions=1, directed_only=True,
                      planner_scheduler_db=Path('/synthetic/scheduler.sqlite'),
                      planner_model_config_path=Path('/synthetic/planner.json'))
        return f, authority, plan, exact_runtime, kwargs, producer, verifier

    def test_source_grade_refusal_continues_to_filed_admission_without_spending_or_extra_slots(self):
        f, authority, plan, runtime, kwargs, producer, verifier = self.fixture()
        before = f.store.connection.execute('SELECT count(*) FROM mission_document_research_admissions').fetchone()[0]
        with patch('dalton_core.mission_document_research_admission.open_mission_document_admission_authority', runtime):
            first = run_admissions(**kwargs)
            second = run_admissions(**kwargs)
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(first['admitted'], 1)
        self.assertEqual(first['refused'][0]['inquiry_ref'], inquiry_ref_for(inquiry_content_hash(plan['inquiries'][0])))
        self.assertIn('DirectedNumericError: numeric directed formal claims require a company-filed source', first['refused'][0]['reason'])
        self.assertEqual(first['refused'][1]['reason'], 'deferred_to_a_later_tick')
        self.assertEqual(first['tasks'][0]['status'], 'fresh')
        saved = authority.admission(first['tasks'][0]['admission_ref'])
        self.assertEqual(saved['plan_ref'], plan['plan_id'])
        self.assertEqual(saved['planner_inquiry'], plan['inquiries'][1])
        self.assertEqual(second['admitted'], 1)  # duplicate is not a fresh slot
        self.assertEqual(second['tasks'][0]['status'], 'duplicate')
        self.assertEqual(second['tasks'][1]['status'], 'fresh')
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_admissions').fetchone()[0], before+2)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))
        summary = json.loads((kwargs['summary_dir']/'summary.json').read_text())
        self.assertEqual(summary['refused'][0], second['refused'][0])
        self.assertEqual(first['task_budget'], second['task_budget'])

    def test_unknown_error_surfaces_and_failed_summary_preserves_the_exact_plan(self):
        f, authority, plan, runtime, kwargs, producer, verifier = self.fixture()
        before = f.store.connection.execute('SELECT count(*) FROM mission_document_research_admissions').fetchone()[0]
        with patch('dalton_core.mission_document_research_admission.open_mission_document_admission_authority', runtime), \
             patch('dalton_core.mission_document_research_admission.admit_directed_inquiry', side_effect=RuntimeError('unknown authority failure')):
            with self.assertRaisesRegex(RuntimeError, 'unknown authority failure'):
                run_admissions(**kwargs)
        summary = json.loads((kwargs['summary_dir']/'summary.json').read_text())
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['plan_ref'], plan['plan_id'])
        self.assertIn('unexpected RuntimeError: unknown authority failure', summary['failure_reason'])
        self.assertEqual(summary['refused'], [])
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_admissions').fetchone()[0], before)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))
