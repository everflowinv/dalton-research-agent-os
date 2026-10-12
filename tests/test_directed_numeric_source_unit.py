"""Legacy missing scale never authorizes changing a value or reporting basis."""
import copy
import json
import types
import unittest
from unittest.mock import patch
from tests import test_directed_numeric_local_reconciliation as local
from tests import test_directed_numeric_workflow as workflow
from tests import test_mission_annual_research as mission
from dalton_core.directed_numeric_executor import source_binding, _execution_proof
from dalton_core.directed_numeric_local_reconciliation import _chain, qualification
from dalton_core.directed_numeric_source_unit import support, verify, CONTRACT, PREFIX
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver, _FIGURE_COLUMNS
from dalton_core.store import content_hash
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.directed_numeric_output import quotes


class SourceUnitTests(unittest.TestCase):
    fixture=local.NumericLocalReconciliationTests.fixture
    attempted=local.NumericLocalReconciliationTests.attempted
    call=local.NumericLocalReconciliationTests.call
    def unit_fixture(self, *, company='wanhua', change=None, same_quote=True):
        with patch.object(workflow, 'COMPANY', company), patch.object(mission, 'COMPANY', company):
            h,w,f,a,admission,ex,p,v,works=self.fixture(seed=False)
        proof,draft,verifier,_=_execution_proof(ex,admission,works)
        expected=copy.deepcopy(draft['output']['targets'][0]['figure'])
        legacy={**expected,'scale':None,**(change or {})}
        citation=quotes(proof)[legacy['quote_id']]
        if not same_quote:
            from dalton_core.document_research import READ_REQUEST_SCHEMA_VERSION, READ_OPERATION
            import hashlib
            request={k:proof['request'][k] for k in ('purpose','research_question','registration','policy_ref','policy_hash')}
            read=a.registry.read({**request,'schema_version':READ_REQUEST_SCHEMA_VERSION,'operation':READ_OPERATION,
                'source_start':1,'source_end':len(workflow.TEXT)})
            citation=read['text'];legacy['quote_id']=f"quote:1:{len(workflow.TEXT)}:{hashlib.sha256(citation.encode()).hexdigest()[:16]}"
        source=source_binding(ex.connection,admission['request']['registration'],company)
        a._missions.record_document_figures(company_ref=company,review_ref=source['review_id'],
            document_ref=source['document_ref'],source_manifest_hash=source['source_manifest_hash'],
            source_grade=source['source_grade'],figures=[{**legacy,'citation_text':citation}],
            observed_by=admission['actor_ref'])
        old=a._missions.document_figures(company_ref=company,limit=2000)[0]
        self.attempted(f,ex,works[3],exhaust=True)
        args=dict(connection=ex.connection,registry=ex.registry,admission=admission,
            target=admission['numeric_authority']['targets'][0],figure=old,search_proof=proof,draft=draft,verifier=verifier)
        return h,w,f,a,admission,ex,p,v,works,old,args

    def test_two_companies_append_typed_fact_full_commit_free_restart_and_unchanged_old_ledger(self):
        for company in ('wanhua','synthetic-second-company'):
            with self.subTest(company=company):
                h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture(company=company)
                old_work=ex.scheduler.work_order_authority(works[3]['id'])
                old_history=ex.scheduler.attempt_history(works[3]['id'])
                paid=[h.rows(ex.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')]
                with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no producer')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
                    done=self.call(ex,admission)
                    self.assertEqual(done['research_status'],'numeric_targets_completed')
                    self.assertEqual(done['model_calls'],0)
                    receipt=done['numeric_result']
                    self.assertEqual(len(receipt['canonical_claims']),3)
                    self.assertEqual(receipt['source_unit_clarification_contract'],CONTRACT)
                    mapping=receipt['source_unit_clarifications'][0]['mapping']
                    typed=MissionFigureAuthorityResolver(ex.connection).figure(mapping['figure_ref'])
                    self.assertTrue(typed['figure_id'].startswith(PREFIX))
                    self.assertEqual(typed['scale'],'billion')
                    self.assertEqual(verify(mapping,**{**args,'figure':typed}),mapping)
                    self.assertEqual(self.call(ex,admission),done)
                    self.assertEqual(ex.run_once(admission['id'])['numeric_result'],receipt)
                self.assertEqual(MissionFigureAuthorityResolver(ex.connection).figure(old['figure_id']),old)
                self.assertEqual(ex.scheduler.work_order_authority(works[3]['id']),old_work)
                self.assertEqual(ex.scheduler.attempt_history(works[3]['id']),old_history)
                self.assertEqual([h.rows(ex.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')],paid)
                self.assertEqual((p.calls,v.calls),(1,1))

    def test_basis_value_currency_and_known_scale_are_not_clarifications(self):
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture()
        h.corruptible_copy(ex.connection,'coverage_mission_document_figures')
        for field,value in (('basis','management-reported'),('value','2.5'),('currency','EUR'),('scale','million')):
            with self.subTest(field=field):
                changed={**old,field:value}
                changed['content_hash']=content_hash({k:changed[k] for k in _FIGURE_COLUMNS})
                for k in (*_FIGURE_COLUMNS,'content_hash'):
                    ex.connection.execute('UPDATE coverage_mission_document_figures SET '+k+'=? WHERE figure_id=?',(changed[k],old['figure_id']))
                ex.connection.commit()
                with self.assertRaises(Exception):support(**{**args,'figure':changed})
        self.assertEqual(ex.connection.execute('SELECT COUNT(*) FROM coverage_mission_document_figures WHERE figure_id LIKE ?', (PREFIX+'%',)).fetchone()[0],0)

    def test_different_exact_window_clarifies_without_changing_legacy_quote(self):
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture(same_quote=False)
        done=self.call(ex,admission)
        typed=MissionFigureAuthorityResolver(ex.connection).figure(done['numeric_result']['completed_targets'][0]['figure_ref'])
        self.assertNotEqual(typed['quote_id'],old['quote_id'])
        self.assertEqual(MissionFigureAuthorityResolver(ex.connection).figure(old['figure_id']),old)
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_append_only_authority_rehashed_typed_fact_and_origin_retraction_refuse(self):
        from dalton_core.directed_numeric_source_unit import TABLE
        from dalton_core.store import canonical_json
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture()
        done=self.call(ex,admission);ref=done['numeric_result']['source_unit_clarifications'][0]['mapping']['figure_ref']
        with self.assertRaisesRegex(Exception,'append-only'):ex.connection.execute('DELETE FROM '+TABLE)
        row=ex.connection.execute('SELECT * FROM '+TABLE).fetchone()
        h.corruptible_copy(ex.connection,TABLE)
        bad=json.loads(row['record_json']);bad['value']='100'
        bad['content_hash']=content_hash({k:v for k,v in bad.items() if k!='content_hash'})
        ex.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?',(canonical_json(bad),bad['content_hash']));ex.connection.commit()
        with self.assertRaisesRegex(DirectedNumericError,'authorized typed fact'):MissionFigureAuthorityResolver(ex.connection).figure(ref)
        ex.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?',(row['record_json'],row['content_hash']));ex.connection.commit()
        a._missions.retract_document_figure(old['figure_id'],reason='source disproved',retracted_by='human:synthetic')
        with self.assertRaises(Exception):ex.run_once(admission['id'])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_proof_rehash_foreign_source_and_missing_independent_prefix_refuse(self):
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture()
        done=self.call(ex,admission);mapping=done['numeric_result']['source_unit_clarifications'][0]['mapping']
        typed=MissionFigureAuthorityResolver(ex.connection).figure(mapping['figure_ref'])
        changed=copy.deepcopy(mapping);changed['conflict_proof']['verified_answer']['scale']='million'
        changed['conflict_proof']['content_hash']=content_hash({k:x for k,x in changed['conflict_proof'].items() if k!='content_hash'})
        changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
        with self.assertRaisesRegex(DirectedNumericError,'does not replay'):verify(changed,**{**args,'figure':typed})
        changed=copy.deepcopy(args['search_proof']);changed['matches'][0]['excerpt']+=' forged'
        with self.assertRaises(Exception):support(**{**args,'search_proof':changed})
        bad=copy.deepcopy(args['verifier']);bad['model_family']=args['draft']['model_family']
        with self.assertRaisesRegex(DirectedNumericError,'independent'):support(**{**args,'verifier':bad})

    def test_dispatch_uses_existing_numeric_normalization_and_cannot_merge_equal_basis(self):
        from dalton_core.directed_numeric_source_unit import candidate_figures
        from dalton_core.directed_numeric_output import candidate_ref
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture()
        draft=copy.deepcopy(args['draft']);draft['output']['targets'][0]['figure']['value']=' 12.3 '
        verifier=copy.deepcopy(args['verifier']);verifier['output']['candidate_ref']=candidate_ref(draft['output'])
        self.assertEqual(len(candidate_figures(ex.connection,admission,args['search_proof'],draft,verifier)),1)
        # Same number, label and units do not erase a reporting-basis change.
        draft['output']['targets'][0]['figure']['basis']='management-reported'
        verifier['output']['candidate_ref']=candidate_ref(draft['output'])
        self.assertEqual(candidate_figures(ex.connection,admission,args['search_proof'],draft,verifier),[])

    def test_old_scale_conflict_normal_path_cannot_append_without_local_authority(self):
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture()
        # Exhausted old local Work remains exhausted; qualification is read-only.
        before=h.rows(ex.connection,'coverage_mission_document_figures')
        self.assertTrue(any(x['kind']=='source_unit_clarification' for x in qualification(ex,admission['id'],admission['content_hash'])[2]['repair_evidence']))
        self.assertEqual(h.rows(ex.connection,'coverage_mission_document_figures'),before)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_real_lane_old_reentry_hold_selects_one_zero_model_child_and_retires_after_commit(self):
        from contextlib import nullcontext
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator, _write_holds, _read_holds
        from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
        from dalton_core.mission_document_research_cli import run_admission
        from dalton_core.document_completed_model_replay import UNIT_ERROR, UNIT_CONTRACT
        h,w,f,a,admission,ex,p,v,works,old,args=self.unit_fixture()
        directory=f.state/'source-unit-tickets';directory.mkdir()
        original=directory/'old';original.mkdir()
        body={'status':'failed','error':UNIT_ERROR,'admission_ref':admission['id'],'admission_hash':admission['content_hash']}
        summary={**body,'content_hash':content_hash(body)}
        (original/'summary.json').write_text(json.dumps(summary))
        ticket={**body,'id':'mission-document-research:old','summary':summary}
        launcher=object.__new__(MissionDocumentResearchLauncher);launcher.tickets_dir=directory
        launcher._ticket_path=lambda ref: original/'ticket.json' if ref==ticket['id'] else directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'synthetic':'no physical child'}
        started=[];completed={}
        launcher.status=lambda ref: ticket if ref==ticket['id'] else completed[ref]
        def spawn(**kw):
            self.assertEqual(kw['record']['contract_ref'],UNIT_CONTRACT)
            self.assertTrue(kw['completed_models_only']);started.append(kw)
            runtime=types.SimpleNamespace(admission=admission,executor=ex)
            with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(ex,'run_once',side_effect=AssertionError('no normal model loop')):
                done=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',
                    planner_model_config=f.state/'unused',document_research_config=f.state/'unused',
                    admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],completed_models_only=True)
            self.assertEqual(done['outcomes'][0]['model_calls'],0)
            self.assertEqual(done['outcomes'][0]['research_status'],'numeric_targets_completed')
            ref=launcher.TICKET_PREFIX+':'+kw['digest'];launcher._ticket_path(ref).write_text('{}')
            completed[ref]={'id':ref,'status':'succeeded','summary':done}
            return completed[ref]
        launcher.spawn=spawn
        from dalton_core.mission_document_research_promotion import ensure_promotion_authority
        ensure_promotion_authority(ex.connection)
        c=MissionDocumentResearchCoordinator(store=f.store,launcher=launcher,clock=f.harness.clock)
        _write_holds(c.holds_path,{admission['id']:{'reason':'reentry_failed_after_automatic_rebind:controlled reentry was already attempted',
            'admission_hash':admission['content_hash'],'ticket_ref':ticket['id'],'disposition':'recovery_required','retry_at':None}})
        with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no producer')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
            self.assertEqual(c.dispatch_once()['reason'],'source_unit_local_reconciliation')
            c.dispatch_once()
        self.assertEqual(len(started),1)
        self.assertNotIn(admission['id'],_read_holds(c.holds_path))
        self.assertEqual((p.calls,v.calls),(1,1))
