"""Selected admission reads preserve exact pending historical originals."""
import json
import os
import unittest
from unittest.mock import patch
from dalton_core import document_research_inventory as inventory
from dalton_core.mission_document_research_admission import open_mission_document_admission_authority
from dalton_core.mission_document_research import PURPOSE
from dalton_core.mission_document_model_authority import DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME
from dalton_core.annual_report_runtime import DRAFT_MODEL_CONFIG_NAME as ANNUAL_DRAFT, VERIFIER_MODEL_CONFIG_NAME as ANNUAL_VERIFIER
from dalton_core.store import content_hash, canonical_json
from tests import test_numeric_required_original_inventory as originals
from tests import test_document_research_inventory as inventory_fixture

class SelectedAdmissionInventoryTests(unittest.TestCase):
    def fixture(self):
        helper=originals.NumericOriginalInventoryTests(methodName='runTest');self.addCleanup(helper.doCleanups)
        f,a,admission,_,_,_=helper.fixture(old_rejection=True)
        current=helper.current_rendering(admission)
        current['id']='registered-document:sha256:'+content_hash({k:v for k,v in current.items() if k not in {'id','content_hash'}})
        current['content_hash']=content_hash({k:v for k,v in current.items() if k!='content_hash'})
        return f,a,admission,current

    def test_selected_exact_originals_equal_full_and_filter_before_source_replay(self):
        f,a,admission,current=self.fixture();old=admission['request']['registration'];ref=old['document_ref']
        args=dict(core=f.store,mission=f.mission,registry=a.registry,purpose=PURPOSE)
        full,_=inventory._required_numeric_originals(**args)
        before=f.store.connection.total_changes
        with patch.object(a.registry,'_reresolve',wraps=a.registry._reresolve) as replay:
            selected,_=inventory._required_numeric_originals(**args,selected_document_refs=(ref,))
            self.assertEqual(selected,full);self.assertEqual(replay.call_count,1)
        with patch.object(a.registry,'_reresolve',side_effect=AssertionError('unselected original read')):
            self.assertEqual(inventory._required_numeric_originals(**args,selected_document_refs=('document:other',)),([],[]))
            self.assertEqual(inventory._required_numeric_originals(**args,selected_document_refs=()),([],[]))
        self.assertEqual(f.store.connection.total_changes,before)

    def test_selected_host_retains_pending_old_version_and_exact_configured_purpose(self):
        f,a,admission,current=self.fixture();old=admission['request']['registration']
        config={'schema_version':'document-research-config-0.1','purpose':PURPOSE,'inventory_preview_chars':600,
            'spool_dir':str(f.state),'enabled_sources':[old['source_ref']],'policy':a.registry.policy,
            'source_reading_limits':{'alphaengine_max_document_chars':10000000,'public_web_max_source_chars':9000000,
                'public_web_max_pdf_pages':2000,'public_web_max_decompressed_bytes':80000000}}
        config=inventory.validate_inventory_config(config)
        (f.state/inventory.CONFIG_FILENAME).write_text(canonical_json(config))
        for source,target in ((ANNUAL_DRAFT,DRAFT_MODEL_CONFIG_NAME),(ANNUAL_VERIFIER,VERIFIER_MODEL_CONFIG_NAME)):
            (f.state/target).write_bytes((f.state/source).read_bytes());os.chmod(f.state/target,0o600)
        response={'registry':a.registry,'registration_by_hash':{current['content_hash']:current},'config_hash':content_hash(config)}
        scheduler=f.harness.scheduler().connection.execute('PRAGMA database_list').fetchone()[2]
        original_read=inventory._required_numeric_originals
        with patch('dalton_core.mission_document_research_admission.load_document_inventory_authority',return_value=response) as selected_loader,\
             patch.object(inventory,'_required_numeric_originals',wraps=original_read) as required:
            with open_mission_document_admission_authority(store=f.store,state_dir=f.state,mission=f.mission,
                planner_scheduler_db=scheduler,planner_model_config_path=f.state/ANNUAL_DRAFT,
                clock=f.harness.clock,selected_document_refs=(old['document_ref'],)) as (authority,registrations):
                self.assertEqual({r['content_hash'] for r in registrations.values()},{old['content_hash'],current['content_hash']})
                self.assertEqual(registrations[old['id']],old)
            self.assertEqual(selected_loader.call_args.kwargs['selected_document_refs'],(old['document_ref'],))
            self.assertEqual(required.call_args.kwargs['purpose'],config['purpose'])

    def test_selected_sql_excludes_unrequested_document_before_materialization(self):
        h=inventory_fixture.DocumentInventoryTests(methodName='runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.insert();h.insert('record:other')
        h.db.execute("UPDATE coverage_mission_discovered_documents SET document_ref='document:other' WHERE record_id='record:other'")
        registry,registration=h.registry()
        with patch.object(inventory,'_required_numeric_originals',side_effect=AssertionError('selected raw inventory must stay closed')):
            result=inventory.inventory_with_registry(core=h.core,mission=h.mission,registry=registry,purpose='directed_research',selected_document_refs=('sales-note:1',))
        registry.inspect_acquired_document.assert_called_once_with(record_id='record:1',purpose='directed_research')
        self.assertEqual(result['registration_by_hash'],{registration['content_hash']:registration})
