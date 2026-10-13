from __future__ import annotations
import sqlite3
import unittest
from dalton_core.bootstrap import SCHEMA_DATABASES,apply_packaged_schemas
from tests import test_workspace_lifecycle as fixtures

class SourceSignProjectionBootstrapTests(unittest.TestCase):
    def test_new_workspace_core_owns_projection_schema_and_repeat_does_not_relax_owner(self):
        self.assertEqual([row for row in SCHEMA_DATABASES
                          if row[0]=='model_spec_source_sign_projection_schema.sql'],
                         [('model_spec_source_sign_projection_schema.sql',None)])
        fixture=fixtures.WorkspaceLifecycleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        state=fixture.workspace.state_dir
        def inspect():
            with sqlite3.connect(state/'core.sqlite') as c:
                table=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND "
                    "name='company_model_spec_source_sign_projections'").fetchone()
                self.assertIsNotNone(table)
                trigger=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                    "name='model_source_sign_projection_owner'").fetchone()
                self.assertIsNotNone(trigger)
                c.create_function('dalton_coverage_mission_authorized',0,lambda:0)
                with self.assertRaisesRegex(sqlite3.IntegrityError,'requires owner authority'):
                    c.execute('INSERT INTO company_model_spec_source_sign_projections VALUES(?,?,?,?,?,?,?)',
                        ('projection:test','work:root','work:source','company:test','{}','a'*64,'now'))
                self.assertEqual(c.execute('SELECT COUNT(*) FROM company_model_spec_source_sign_projections').fetchone()[0],0)
                return table[0],trigger[0]
        original=inspect()
        apply_packaged_schemas(state)
        self.assertEqual(inspect(),original)
