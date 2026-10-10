"""A project revision retains originals without changing their recorded authority."""
import copy
import json
import unittest

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_mission_lineage import document_mission_versions, registration_in_mission
from dalton_core.document_research_inventory import inventory_with_registry, document_inventory_signature
from dalton_core.mission_document_research import PURPOSE, MissionDocumentResearchError
from dalton_core.mission_document_research_admission import admit_directed_inquiry
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.store import content_hash
from tests import test_mission_document_research as research_tests


class OriginalInheritanceTests(unittest.TestCase):
    def fixture(self):
        owner = research_tests.MissionDocumentResearchTests('test_exact_archived_plan_question_and_registration_admit_and_replay')
        self.addCleanup(owner.doCleanups)
        fixture, authority, args, registration, launcher = owner._fixture()
        return owner, fixture, authority, args, registration

    def rolled_plan(self, owner, fixture, args, **roll):
        old = json.loads(fixture.store.connection.execute(
            'SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?', (args['plan_ref'],)).fetchone()[0])
        fixture.mission = owner._roll_mission(fixture, **roll)
        plan = {key: copy.deepcopy(old[key]) for key in (
            'schema_version', 'task_ref', 'created_at', 'assessment', 'directives', 'inquiries', 'sufficiency')}
        plan.update(state_hash='7'*64, mission_version_ref=fixture.mission['id'])
        plan['content_hash'] = content_hash(plan)
        return owner._record_plan(fixture, plan)

    def test_revision_reads_same_original_and_new_plan_admits_under_current_budget(self):
        owner, fixture, authority, args, registration = self.fixture()
        original = copy.deepcopy(registration)
        connection = fixture.store.connection
        old_rows = [tuple(row) for row in connection.execute('SELECT * FROM coverage_mission_discovered_documents')]
        old_signature = document_inventory_signature(fixture.store, fixture.state)
        plan = self.rolled_plan(owner, fixture, args, budget={**fixture.mission['budget'], 'max_daily_cost_usd':9.0})
        inventory = inventory_with_registry(core=fixture.store, mission=fixture.mission,
                                            registry=authority.registry, purpose=PURPOSE)
        self.assertEqual(inventory['registration_by_hash'][registration['content_hash']], original)
        company = plan['inquiries'][0]['company_ref']
        self.assertEqual(inventory['readable_documents_by_company'][company][0]['document_version_hash'], original['content_hash'])
        self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM coverage_mission_discovered_documents')], old_rows)
        self.assertNotEqual(document_inventory_signature(fixture.store, fixture.state), old_signature)
        result = admit_directed_inquiry(authority=authority, registrations={original['id']:original},
            backlog=ResearchQuestionBacklog(fixture.store), mission=fixture.mission,
            plan=plan, inquiry=plan['inquiries'][0])
        self.assertEqual(result['status'], 'fresh')
        resolved = authority.resolve_for_execution(result['admission_ref'])
        self.assertEqual(resolved['mission_version_ref'], fixture.mission['id'])
        self.assertEqual(registration, original)
        duplicate = admit_directed_inquiry(authority=authority, registrations={original['id']:original},
            backlog=ResearchQuestionBacklog(fixture.store), mission=fixture.mission,
            plan=plan, inquiry=plan['inquiries'][0])
        self.assertEqual(duplicate['status'], 'duplicate')

    def test_disconnected_source_is_not_inherited_or_admitted(self):
        owner, fixture, authority, args, registration = self.fixture()
        sources = copy.deepcopy(fixture.mission['source_plan'])
        for source in sources:
            if source['source_ref'] == registration['source_ref']:
                source['status'] = 'not_connected'
        plan = self.rolled_plan(owner, fixture, args, source_plan=sources)
        inventory = inventory_with_registry(core=fixture.store, mission=fixture.mission,
                                            registry=authority.registry, purpose=PURPOSE)
        self.assertEqual(inventory['registration_by_hash'], {})
        with self.assertRaises(MissionDocumentResearchError):
            authority.question_origin_from_plan(plan_ref=plan['plan_id'], inquiry_ref=args['inquiry_ref'],
                                                 document_authority_ref=registration['id'])

    def test_foreign_version_and_drifted_mission_cannot_supply_originals(self):
        owner, fixture, authority, args, registration = self.fixture()
        self.rolled_plan(owner, fixture, args)
        self.assertFalse(registration_in_mission(fixture.store.connection, fixture.mission,
                                                 {'mission_version_ref':'mission:foreign'}))
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            document_mission_versions(fixture.store.connection, {**fixture.mission, 'content_hash':'0'*64})

    def test_newer_unfinished_row_does_not_fall_back_to_older_original(self):
        owner, fixture, authority, args, registration = self.fixture()
        self.rolled_plan(owner, fixture, args)
        coverage = CoverageMissionAuthority(fixture.store)
        # This fixture's original source discovery is a fake receipt too.
        fixture.store.connection.execute('PRAGMA foreign_keys=OFF')
        with coverage._transaction() as cur:
            cur.execute('INSERT INTO coverage_mission_discovered_documents '
                '(record_id,mission_version_ref,company_ref,source_ref,document_ref,discovery_ref,status,created_at,updated_at) '
                'SELECT ?,?,company_ref,source_ref,document_ref,discovery_ref,?,created_at,updated_at '
                'FROM coverage_mission_discovered_documents WHERE record_id=?',
                ('mission-discovered-document:newer',fixture.mission['id'],'discovered',registration['source_authority']['ref']))
        fixture.store.connection.execute('PRAGMA foreign_keys=ON')
        inventory = inventory_with_registry(core=fixture.store, mission=fixture.mission,
                                            registry=authority.registry, purpose=PURPOSE)
        self.assertEqual(inventory['registration_by_hash'], {})

if __name__ == '__main__':
    unittest.main()
