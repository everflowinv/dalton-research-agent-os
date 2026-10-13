import copy
import hashlib
import json
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import sec_interim_attachments as interim
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector_authority_port import ReadOnlyConnectorReceiptReader
from dalton_core.connector import ConnectorStore
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.observability import ObservabilityStore
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.public_web_core_fetch import PublicWebCoreFetch, PublicWebCoreFetchError
from dalton_core.public_web_fetch_cli import _write_owner_only
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.sec_plan_specs import effective_sec_plan
from dalton_core.store import content_hash
from tests import test_sec_native_interim as roots
from tests import test_sec_filings_index_discovery as fixture
from tests import test_public_web_fetch_lane as fetch_fixture


class InterimAcquisitionTests(unittest.TestCase):
    setUp = roots.InterimSourceTests.setUp
    run_once = roots.InterimSourceTests.run_once
    adapter = roots.InterimSourceTests.adapter
    root = roots.InterimSourceTests.root
    profile = roots.InterimSourceTests.profile
    body = roots.InterimSourceTests.body

    def prepare(self, annual='40-F'):
        profile = self.root(annual)
        result = self.run_once(plan=effective_sec_plan(fixture.sec_plan()),
            spec_ref='foreign-disclosure-6k', as_of=date.today(), reporting_profile=profile,
            adapter=self.adapter(self.body(annual)))
        self.assertEqual(result['status'], 'succeeded', result)
        self.parent_ref = fixture.CIK + '-26-000002'
        self.identity = interim.identity(self.core.connection, self.spool, result['discovery_ref'],
            company_ref=fixture.ACN, parent_ref='sec:filing:' + self.parent_ref)
        self.base = self.identity['primary_authority']['canonical_url'].rsplit('/', 1)[0]
        self.index = (f'<html><table summary="Document Format Files"><tr><th>Seq</th><th>Description</th>'
            '<th>Document</th><th>Type</th><th>Size</th></tr>'
            '<tr><td>1</td><td>Original cover</td><td><a href="disclosure.htm">disclosure.htm</a></td><td>6-K</td><td>100</td></tr>'
            '<tr><td>2</td><td>Interim Financial Statements</td><td><a href="financial.htm">financial.htm</a></td><td>EX-99.1</td><td>300</td></tr>'
            '<tr><td>3</td><td>Logo</td><td><a href="logo.jpg">logo.jpg</a></td><td>GRAPHIC</td><td>50</td></tr>'
            '</table></html>').encode()
        self.pages = {'disclosure.htm': b'<html><p>FORM 6-K original disclosure</p></html>',
            self.parent_ref + '-index.html': self.index,
            'financial.htm': b'<html><p>Six months ended June 30, 2026</p><table><tr><td>Revenue</td><td>123</td></tr></table></html>'}
        self.sent = []
        def exchange(target, *args):
            self.sent.append(target.url)
            return fetch_fixture._Response(self.pages[target.url.rsplit('/', 1)[1]])
        self.raw_spool = RawSpool(str(self.state / 'connector-spool'), max_total_bytes=100000000)
        gov = fetch_fixture.approved_fetch_governance()
        self.catalog = CapabilityCatalog(str(self.state / 'fetch-catalog.sqlite'),
            approval_resolver=gov.approval, policy_resolver=gov.policy)
        self.addCleanup(self.catalog.close)
        self.scheduler = Scheduler(str(self.state / 'fetch-scheduler.sqlite'))
        self.addCleanup(self.scheduler.close)
        self.fetch = PublicWebCoreFetch(store=self.core, connectors=ConnectorStore(self.core),
            observability=ObservabilityStore(self.core), journal=RunnerJournal(self.core),
            scheduler=self.scheduler, catalog=self.catalog, spool=self.raw_spool, governance=gov,
            transport=PublicHttpTransport(resolver=lambda *args: ('93.184.216.34',), exchange=exchange))
        self.primary = self.fetch.fetch(self.fetch.build_request(self.identity['primary_authority']))
        self.assertEqual(self.primary['outcome'], 'succeeded', self.primary)
        self.out = self.state / 'fetches' / ('a'*24); self.out.mkdir(parents=True)
        self.ticket_ref = 'public-web-fetch:' + self.out.name
        _write_owner_only(self.out / 'manifest.json', self.fetch.manifest(self.primary))
        _write_owner_only(self.out / 'summary.json', {'status': 'succeeded', 'url_ref': 'sec:filing:' + self.parent_ref,
            'discovery_ref': result['discovery_ref']})
        return result

    def acquire(self):
        return interim.acquire(self.fetch, self.raw_spool, source_identity=self.identity,
            primary_receipt=self.primary, out=self.out)

    def verify(self):
        return interim.verified_sources(self.core, self.raw_spool, self.state, ticket_ref=self.ticket_ref,
            parent_document_ref='sec:filing:' + self.parent_ref, company_ref=fixture.ACN,
            receipt_reader=ReadOnlyConnectorReceiptReader(self.core.connection))

    def test_normal_fetch_cli_executes_foreign_attachment_hook(self):
        # A normal current mission, with the existing public fetch source grant.
        from dalton_core.store import DaltonStore
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
        core = DaltonStore(str(self.state / 'core.sqlite'))
        try:
            authority = CoverageMissionAuthority(core)
            params = mission_params(bootstrap_method_authorities(core))
            params['autonomy']['may_write'] = list(params['autonomy']['may_write']) + ['source_discovery']
            for source in params['source_plan']:
                if source['source_ref'] in ('source:sec-edgar', 'source:web-search'):
                    source['status'] = 'connected'
            params.update(version_id='coverage-mission-version:us-it-services:2',
                prior_version_ref=self.mission['id'], idempotency_key='coverage-mission:us-it-services:2')
            ref = params.pop('mission_ref')
            self.mission = authority.create_mission(ref, **params)
        finally:
            core.close()
        self.prepare()
        from dalton_core.public_web_fetch_cli import run_fetch
        def exchange(target, *args):
            self.sent.append(target.url)
            return fetch_fixture._Response(self.pages[target.url.rsplit('/', 1)[1]])
        out = self.state / 'fetches' / ('b' * 24)
        summary = run_fetch(state_dir=self.state, governance=fetch_fixture.approved_fetch_governance(),
            url_ref='sec:filing:' + self.parent_ref, requested_by=self.principal,
            transport=PublicHttpTransport(resolver=lambda *a: ('93.184.216.34',), exchange=exchange),
            transport_label='hermetic', summary_dir=out, spool_dir=self.state / 'connector-spool')
        self.assertEqual(summary['status'], 'succeeded', summary)
        self.assertEqual(summary['interim_attachments']['status'], 'succeeded', summary)
        self.assertEqual(summary['provider_calls'], 3)
        self.assertEqual(summary['interim_attachments']['source_identity']['company_ref'], fixture.ACN)
        self.assertFalse(summary['interim_attachments']['financial_periods_inferred'])
        self.assertTrue((out / interim.RECORD_FILE).is_file())

    def test_actual_governed_fetch_complete_set_receipts_and_free_replay(self):
        self.prepare(); outcome = self.acquire()
        self.assertEqual(outcome['status'], 'succeeded', outcome)
        self.assertEqual(len(self.sent), 3)
        exact = self.verify()
        self.assertEqual(len(exact), 2)
        self.assertIn('Six months ended', exact[1]['rendering']['text'])
        self.assertEqual(exact[0]['document_ref'], 'sec:filing:' + self.parent_ref)
        self.assertTrue(exact[1]['document_ref'].startswith('sec:interim:'))
        self.assertFalse(outcome['financial_periods_inferred'])
        self.assertEqual(self.core.connection.execute('SELECT COUNT(*) FROM coverage_mission_statement_filings').fetchone()[0], 0)
        before = len(self.sent)
        self.assertEqual(self.acquire()['provider_calls'], 0)
        self.assertEqual(len(self.sent), before)
        self.assertEqual(self.verify(), exact)

    def test_rehashed_members_or_discovery_or_missing_bytes_cannot_register(self):
        self.prepare(); self.acquire()
        original = json.loads((self.out / interim.RECORD_FILE).read_text())
        for case in ('member', 'source', 'missing', 'index'):
            wire = copy.deepcopy(original)
            if case == 'member': wire['source_manifests'][1]['member']['url'] = self.base + '/not-listed.htm'
            elif case == 'source': wire['source_identity']['company_ref'] = 'company:wrong'
            elif case == 'missing': wire['source_manifests'].pop()
            else: wire['index_manifest']['body_sha256'] = 'e'*64
            wire['content_hash'] = content_hash({k:v for k,v in wire.items() if k != 'content_hash'})
            _write_owner_only(self.out / interim.RECORD_FILE, wire)
            with self.assertRaises((ValueError, KeyError, PublicWebCoreFetchError)): self.verify()
        _write_owner_only(self.out / interim.RECORD_FILE, original)
        self.assertEqual(len(self.verify()), 2)

    def test_cross_issuer_cross_accession_and_query_form_rejected(self):
        result = self.prepare('20-F')
        with self.assertRaises(ValueError):
            interim.identity(self.core.connection, self.spool, result['discovery_ref'], company_ref='company:wrong', parent_ref='sec:filing:' + self.parent_ref)
        for link in ('https://example.com/financial.htm', self.base.replace('/1467373/', '/9999/') + '/financial.htm',
                     self.base.replace('26000002', '26000003') + '/financial.htm', '../financial.htm', 'financial.htm?other=1'):
            raw = self.index.replace(b'href="financial.htm"', f'href="{link}"'.encode())
            with self.assertRaises(ValueError): interim.attachment_index(raw, issuer=fixture.CIK,
                filing=self.identity['filing'], index_url=self.base + '/' + self.parent_ref + '-index.html')

    def test_cover_and_failed_attachment_never_claim_complete_or_quarter(self):
        self.prepare(); self.pages['financial.htm'] = b''
        outcome = self.acquire()
        self.assertEqual(outcome['status'], 'incomplete')
        with self.assertRaises(ValueError): self.verify()
        self.assertEqual(self.core.connection.execute('SELECT COUNT(*) FROM coverage_mission_statement_filings').fetchone()[0], 0)

    def test_index_duplicate_unclosed_or_overbound_rejected_before_member_fetch(self):
        self.prepare()
        for raw in (self.index.replace(b'</table>', b''),
                    self.index.replace(b'logo.jpg', b'disclosure.htm'),
                    self.index.replace(b'</table>', b'<tr><td>9</td></tr></table>')):
            with self.assertRaises(ValueError): interim.attachment_index(raw, issuer=fixture.CIK,
                filing=self.identity['filing'], index_url=self.base + '/' + self.parent_ref + '-index.html')

    def test_normal_registration_and_independent_reader_preserve_attachment_identity(self):
        result = self.prepare(); self.acquire()
        connection = self.core.connection
        missions = CoverageMissionAuthority(self.core)
        with missions._transaction() as cursor:
            cursor.execute("UPDATE coverage_mission_discovered_documents SET status='acquired',ticket_ref=? WHERE document_ref=?",
                (self.ticket_ref, 'sec:filing:' + self.parent_ref))
        parent = connection.execute('SELECT record_id FROM coverage_mission_discovered_documents WHERE document_ref=?',
            ('sec:filing:' + self.parent_ref,)).fetchone()[0]
        missions = CoverageMissionAuthority(self.core)
        reviews = missions.register_acquired_interim_attachments(parent, state_dir=self.state, spool=self.raw_spool,
            receipt_reader=ReadOnlyConnectorReceiptReader(connection), requested_by=self.principal)
        self.assertEqual(len(reviews), 1)
        replay = missions.register_acquired_interim_attachments(parent, state_dir=self.state, spool=self.raw_spool,
            receipt_reader=ReadOnlyConnectorReceiptReader(connection), requested_by=self.principal)
        self.assertEqual([x['source_binding'] for x in replay], [x['source_binding'] for x in reviews])
        self.assertTrue(all(x['review_status'] == 'duplicate' for x in replay))
        from dalton_core.document_research import PublicWebDocumentSourceAdapter
        adapter = PublicWebDocumentSourceAdapter(source_ref='source:sec-edgar', core=self.core, spool=self.raw_spool,
            launcher=SimpleNamespace(state_dir=self.state, read_completed_manifest=lambda *a: self.fail('no guessed manifest'),
                locate_completed_manifest_binding=lambda *a: self.fail('no scan')),
            receipt_reader=ReadOnlyConnectorReceiptReader(connection), max_source_chars=100000,
            max_pdf_pages=10, max_decompressed_bytes=100000)
        registration, text = adapter.materialize(document_ref=reviews[0]['document_ref'], acquisition_ticket_ref=self.ticket_ref)
        self.assertEqual(registration['document_ref'], reviews[0]['document_ref'])
        self.assertIn('Six months ended', text)
        self.assertNotIn('FORM 6-K original', text)
        self.assertEqual(len(self.sent), 3)
        from dalton_core.evidence_corpus import CorpusContext, Candidate, load_document
        with CorpusContext(self.state, core=connection) as context:
            body = load_document(context, Candidate('interim-test', 'fetch_interim_attachment',
                self.out / 'interim-attachment-001-manifest.json'))
            self.assertEqual(body.document_ref, reviews[0]['document_ref'])
            self.assertEqual(body.kind, 'sec_6k')
            self.assertIn('Six months ended', body.text)
            self.assertEqual(body.filed_company_refs, [fixture.ACN])
        from dalton_core import sec_interim_attachments
        after = sec_interim_attachments.register_held(missions, state_dir=self.state,
            spool_dir=self.state / 'connector-spool')
        self.assertEqual(after['registered'], [])
        self.assertEqual(after['refused'], [])
