"""Original EPS-note authority survives only the verified project lineage."""
import copy
import json
import unittest

from dalton_core.document_research_inventory import financial_note_targets_for_registration
from dalton_core.financial_note_evidence import resolve_financial_note_evidence, FinancialNoteEvidenceError
from dalton_core.store import content_hash, canonical_json
from tests import test_financial_note_history_periods as history_tests
from tests import test_document_mission_lineage as lineage_tests
from tests import test_financial_note_evidence as note_tests


class CurrentMissionNoteCarryTests(unittest.TestCase):
    def fixture(self, company='company:A'):
        source = history_tests.HistoryNotePeriodsTests()
        self.addCleanup(source.doCleanups)
        db, _, reg, _ = source.filing(company=company)
        owner = lineage_tests.OriginalInheritanceTests()
        self.addCleanup(owner.doCleanups)
        original, fixture, _, args, _ = owner.fixture()
        old = copy.deepcopy(fixture.mission)
        owner.rolled_plan(original, fixture, args,
                          budget={**fixture.mission['budget'], 'max_daily_cost_usd': 9.0})
        current = copy.deepcopy(fixture.mission)
        sql = fixture.store.connection.execute("SELECT sql FROM sqlite_master WHERE name='coverage_mission_versions'").fetchone()[0]
        db.execute(sql)
        for mission in (old, current):
            mission.pop('status', None)
            mission['universe'][0]['company_ref'] = company
            mission['content_hash'] = content_hash({k:v for k,v in mission.items() if k != 'content_hash'})
            row = dict(fixture.store.connection.execute('SELECT * FROM coverage_mission_versions WHERE mission_version_id=?', (mission['id'],)).fetchone())
            row['record_json'] = canonical_json(mission); row['content_hash'] = mission['content_hash']
            db.execute('INSERT INTO coverage_mission_versions ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', tuple(row.values()))
        reg['source_authority']['mission_version_ref'] = old['id']
        db.execute('UPDATE coverage_mission_statement_dispatches SET mission_version_ref=?,mission_version_hash=?', (old['id'],old['content_hash']))
        return db, current, reg, old

    def targets(self, data, **kwargs):
        db, mission, reg, _ = data
        return financial_note_targets_for_registration(connection=db, mission=mission,
            company_ref=reg['source_authority']['company_ref'], registration=reg, **kwargs)

    def test_two_companies_inherit_exact_original_and_dispatch_without_writes(self):
        for company in ('company:A','company:B'):
            with self.subTest(company=company):
                data=self.fixture(company);db,current,reg,old=data
                before=[tuple(r)for r in db.execute('SELECT * FROM coverage_mission_statement_dispatches')]
                changes=db.total_changes
                targets=self.targets(data)
                self.assertEqual(len(targets),3)
                self.assertTrue(all(t['schema_version']=='financial-note-target-0.2' for t in targets))
                self.assertEqual(self.targets(data,target_schema_version='financial-note-target-0.1'),[])
                self.assertEqual(db.total_changes,changes)
                self.assertEqual([tuple(r)for r in db.execute('SELECT * FROM coverage_mission_statement_dispatches')],before)

    def test_changed_current_hash_ancestor_body_dispatch_and_foreign_source_refuse(self):
        for kind in ('current_hash','ancestor_body','dispatch_hash','dispatch_mission','foreign_registration'):
            with self.subTest(kind=kind):
                data=self.fixture();db,m,reg,old=data
                if kind=='current_hash':m['content_hash']='0'*64
                elif kind=='ancestor_body':
                    row=db.execute('SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?',(old['id'],)).fetchone()
                    body=json.loads(row[0]);body['title']='changed'
                    db.execute('UPDATE coverage_mission_versions SET record_json=? WHERE mission_version_id=?',(canonical_json(body),old['id']))
                elif kind=='dispatch_hash':db.execute("UPDATE coverage_mission_statement_dispatches SET mission_version_hash=?",('0'*64,))
                elif kind=='dispatch_mission':db.execute('UPDATE coverage_mission_statement_dispatches SET mission_version_ref=?,mission_version_hash=?',(m['id'],m['content_hash']))
                else:reg['source_authority']['mission_version_ref']='foreign:mission'
                self.assertEqual(self.targets(data),[])

    def test_current_company_and_connected_source_are_required(self):
        for kind in ('company','source'):
            data=self.fixture();db,m,_,_=data
            if kind=='company':m['universe'][0]['company_ref']='company:removed'
            else:
                for source in m['source_plan']:
                    if source['source_ref']=='source:sec-edgar':source['status']='not_connected'
            m['content_hash']=content_hash({k:v for k,v in m.items()if k!='content_hash'})
            db.execute('UPDATE coverage_mission_versions SET record_json=?,content_hash=? WHERE mission_version_id=?',(canonical_json(m),m['content_hash'],m['id']))
            self.assertEqual(self.targets(data),[])

    def test_current_admission_promotes_and_reader_replays_original_ancestor(self):
        helper=note_tests.FinancialNoteResolverTests();self.addCleanup(helper.doCleanups)
        fixture,executor,admission,target=helper._completed_typed(target_options={
            'form':'10-Q','start':'2025-07-01','end':'2025-09-30','report_date':'2025-12-31',
            'kind':'quarter','inherit_mission':True})
        self.assertNotEqual(admission['request']['registration']['source_authority']['mission_version_ref'], admission['mission_version_ref'])
        kwargs=dict(core_connection=fixture.store.connection,router_connection=fixture.router.connection,
            staging_connection=executor.staging.connection,registry=executor.registry,admission_ref=admission['id'])
        changes=fixture.store.connection.total_changes
        old_registration=copy.deepcopy(admission['request']['registration'])
        paid_before=[tuple(r) for r in fixture.store.connection.execute('SELECT * FROM model_invocations')]
        result=resolve_financial_note_evidence(**kwargs)
        self.assertEqual(result['periods'],target['periods'])
        self.assertEqual(resolve_financial_note_evidence(**kwargs),result)
        self.assertNotIn('value',result)
        self.assertEqual(fixture.store.connection.total_changes,changes)
        self.assertEqual(admission['request']['registration'],old_registration)
        self.assertEqual([tuple(r) for r in fixture.store.connection.execute('SELECT * FROM model_invocations')],paid_before)


if __name__=='__main__':unittest.main()
