"""Paid invalid candidates terminate this held-note reading; no real text/calls."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
from dalton_core.store import DaltonStore,content_hash
from dalton_core.street_estimate_table_model import _candidates,extract_verified,TableModelRefused,MODEL_CONFIG,VERIFIER_CONFIG,EXTRACTOR_REF
from dalton_core.street_estimate_table_cli import run_table,pending_notes
from dalton_core.street_estimate_tables import StreetEstimateFigureStore
from dalton_core.street_estimate_extraction import OUTPUT_SCHEMA
from tests.test_street_estimate_table_model import request,response,Model,MISSION

class InvalidCandidateTerminalTests(unittest.TestCase):
 def test_producer_schema_allows_only_the_existing_numeric_scale_contract(self):
  self.assertEqual(OUTPUT_SCHEMA['properties']['estimates']['items']['properties']['scale']['enum'],[None,'thousand','million','billion','trillion'])
 def test_invalid_scale_is_refused_before_verifier_without_reinterpreting_output(self):
  req=request();candidate=response(req);candidate['estimates'][0]['scale']='one';original=copy.deepcopy(candidate);p,v=Model(candidate),Model()
  with self.assertRaisesRegex(TableModelRefused,'scale must be null'):
   extract_verified(req,producer=p,verifier=v,mission=MISSION,family_of=lambda r:{'producer':'a','verifier':'b'}.get(r))
  self.assertEqual(candidate,original);self.assertEqual(len(p.calls),1);self.assertEqual(v.calls,[])
  self.assertIn('never use one',p.calls[0]['prompt'])
 def test_other_invalid_numeric_candidates_are_terminal_not_route_failures(self):
  for changes in [{'scale':'units'},{'currency':'US$'},{'unit':'per_share'}]:
   candidate=response(request());candidate['estimates'][0].update(changes)
   with self.subTest(changes=changes),self.assertRaises(TableModelRefused):_candidates(candidate,request())
 def test_valid_null_eps_and_printed_scaled_revenue_keep_exact_values(self):
  figures=_candidates(response(request()),request());self.assertEqual([f['value'] for f in figures],['3.25','125000000'])
 def test_paid_invalid_output_is_append_only_and_not_reoffered_after_restart_or_new_day(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp)
   with DaltonStore(str(state/'core.sqlite')) as core:
    core.connection.execute('CREATE TABLE coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT)');core.connection.execute("INSERT INTO coverage_mission_pointer VALUES ('m','m:1')")
    core.connection.execute('CREATE TABLE street_estimates (company_ref TEXT, document_ref TEXT, broker TEXT, published_on TEXT, source_manifest_hash TEXT)')
    core.connection.execute("INSERT INTO street_estimates VALUES ('company:example','doc:example','broker','2026-10-10','x')")
    figures=StreetEstimateFigureStore(core);figures.record_document(company_ref='company:example',document_ref='doc:example',house='broker',published_on='2026-10-10',figures=[],outcome='needs_model')
    old=core.connection.execute("SELECT record_json FROM street_estimate_figure_scans WHERE outcome='needs_model'").fetchone()[0]
   cfg={'routing_policy_ref':'policy:1','credential_slot_refs':['slot:1'],'model_router_db':str(state/'router.sqlite'),'broker_socket':str(state/'broker.sock'),'broker_auth_key':str(state/'key'),'broker_client_id':'client:dalton-core','expected_agent_id':'chem','budget_db':str(state/'budget.sqlite'),'budget_policy_ref':'budget:1'}
   for fn in [MODEL_CONFIG,VERIFIER_CONFIG]:(state/fn).write_text(json.dumps(cfg))
   invalid=response(request());invalid['estimates'][0]['scale']='one';models=[Model(invalid),Model()];made=[]
   for model in models:model.budget_for=lambda purpose:{'max_cost_usd':.25,'max_input_tokens':24000,'max_output_tokens':3000}
   def factory(*a,**kw):model=models[len(made)];made.append(model);return model
   with patch('dalton_core.coverage_mission.CoverageMissionAuthority.mission',return_value=MISSION),patch('dalton_core.street_estimate_table_cli.request_for',return_value=request()):
    result=run_table(state_dir=state,company_ref='company:example',document_ref='doc:example',expected_request_hash=content_hash(request()),model_factory=factory,family_of=lambda r:{'producer':'a','verifier':'b'}.get(r))
   self.assertEqual(result['status'],'refused');self.assertIn('TableModelRefused',result['reason']);self.assertEqual(len(models[0].calls),1);self.assertEqual(models[1].calls,[])
   with DaltonStore(str(state/'core.sqlite')) as core:
    self.assertEqual(pending_notes(core.connection,'company:example'),[])
    self.assertEqual(core.connection.execute("SELECT record_json FROM street_estimate_figure_scans WHERE outcome='needs_model'").fetchone()[0],old)
    done=core.connection.execute('SELECT outcome FROM street_estimate_figure_scans WHERE extractor_ref=?',(EXTRACTOR_REF,)).fetchone();self.assertEqual(done[0],'refused')
    from dalton_core.mission_consensus_lane import _table_scanner
    launcher=SimpleNamespace(configured=True,configuration_identity=lambda:'config',start=Mock())
    with patch('dalton_core.street_estimate_table_launcher.StreetEstimateTableLauncher',return_value=launcher),patch('dalton_core.financial_period.fiscal_year_end',return_value=None):
     for _ in range(2):
      scanner=_table_scanner(SimpleNamespace(state_dir=state,store=core));self.assertIsNone(scanner([{'company_ref':'company:example','ticker':'EX'}]))
    launcher.start.assert_not_called()

if __name__=='__main__':unittest.main()
