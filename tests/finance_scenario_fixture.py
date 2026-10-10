"""Synthetic isolated CockpitHarness only. Uses the real held-spec/input and model authorities."""
import copy
from dalton_core.store import content_hash
from dalton_core.company_model_state import build_company_model_state
from dalton_core.company_model_spec import spec_from_response
from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure,forecast_structure_binding
from dalton_core.model_forecast_driver import ForecastModelAuthority,build_structured_forecast_model
from tests.test_financial_structure_forecast_consumer import financial_inputs,forecastable_proposal
from tests.test_company_financial_statement_structure import input_line
from tests.test_company_model_spec import _spec


def seed_held_model(store, m, mission, *, company_ref, ticker, cik, entity_name):
 company=company_ref
 accession=cik.zfill(10)+'-26-000001'
 table=financial_inputs(); table['company_ref']=company
 for l in table['filed_lines']:
  if l['concept']=='revenue':l['concept']='us-gaap:Revenues'
 table['filed_lines'] += [input_line('units',(100,110,120,130),unit='count'),input_line('price',(10,)*4,unit='usd_per_unit')]
 auth=m.authorize_sec_lane(company_ref=company,ticker=ticker,actor_ref='automation:coverage-mission',mission_version_ref=mission['id'],mission_version_hash=mission['content_hash'])
 dispatch=m.queue_statement_dispatch(authorization=auth,form='10-K',attempt=0)
 m.mark_statement_dispatch_launched(dispatch['dispatch_id'],'sec-financials-run:synthetic-ui-held')
 lines=[]
 for source in table['filed_lines']:
  for end,c in source['cells'].items():
   lines.append({'statement':source['statement'],'concept':source['concept'],'label':source['label'],'level':0,'parent_concept':None,'is_breakdown':False,'dimension_axis':None,'dimension_member':None,'period_start':c['period_start'],'period_end':end,'value':c['value'],'unit':c['unit'],'balance':'credit'})
 m.record_statement_observation(dispatch_id=dispatch['dispatch_id'],observation={'schema_version':'0.1','cik':cik,'entity_name':entity_name,'filings':[{'accession':accession,'form':'10-K','filed':'2026-01-01','report_date':'2025-12-31','lines':lines}],'source_record_refs':['raw-sink:'+'c'*64],'next_cursor':None,'provider_status':200},governance_ref='g',governance_hash='b'*64)
 m.settle_statement_dispatch(dispatch['dispatch_id'],outcome='succeeded')
 candidate=forecastable_proposal(table)
 for l in candidate['lines']:
  if l.get('concept')=='revenue':l['concept']='us-gaap:Revenues'
 for f in candidate['formulas']:f['evidence_refs']=[accession]
 definition={k:copy.deepcopy(candidate[k]) for k in ('schema_version','lines','formulas')}
 response=_spec(revenue_anchor_concept='us-gaap:Revenues',financial_statement_structure=definition,operating_metrics=[],expense_lines=[{'ref':'cost','label':'营业成本','basis_concept':'cost','behaviour':'variable_with_revenue','driver_ref':None,'because':'Synthetic filed delivery cost'},{'ref':'opex','label':'营业费用','basis_concept':'opex','behaviour':'variable_with_revenue','driver_ref':None,'because':'Synthetic filed operating expense'}],revenue_drivers=[{'ref':'volume','kind':'volume','basis_concept':'units','unit':'count','label':'销量','because':'Synthetic held quantity driver'},{'ref':'price','kind':'price','basis_concept':'price','unit':'usd_per_unit','label':'单价','because':'Synthetic held unit price driver'}],driver_formulas=[{'output_ref':'revenue','operator':'product','driver_refs':['volume','price'],'because':'Synthetic held quantities × price close to consolidated revenue'}])
 state=build_company_model_state(m,company,ticker=ticker)
 spec=m.record_company_model_spec(spec_from_response(state,response,decided_by='human:synthetic-ui-review'),mission_version_ref=mission['id'])
 inputs=build_model_inputs(m,spec); structure,replay=materialize_financial_statement_structure(spec,inputs)
 body=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=forecast_structure_binding(structure,replay,inputs))
 body['mission_version_ref']=mission['id']
 model=ForecastModelAuthority(store).publish(body)
 return model,spec,inputs
