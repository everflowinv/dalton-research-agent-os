"""Opaque XBRL unit ids are resolved only against exact held XML facts."""
import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.sec_filing_units import parse_filing_units,match_row_units,validate_unit_supplement,FilingUnitError,RULE_REF
from dalton_core.store import content_hash
from dalton_core.company_model_state import _numeric_line_authority,_numeric_context,DEFAULT_NUMERIC_CONTEXT_POLICY
from dalton_core.company_model_spec import _numeric_period_table
from dalton_core.company_model_inputs import _series_for

XML='''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:x="http://www.xbrl.org/2003/instance" xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:currency="http://www.xbrl.org/2003/iso4217" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:g="https://taxonomy.example/gaap" xmlns:t="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"><head/><body><x:unit id="opaque-dollar"><x:measure>currency:USD</x:measure></x:unit><x:unit id="opaque-share"><x:measure>x:shares</x:measure></x:unit><x:unit id="opaque-eps"><x:divide><x:unitNumerator><x:measure>currency:USD</x:measure></x:unitNumerator><x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide></x:unit><x:context id="c"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">12</x:identifier></x:entity><x:period><x:startDate>2026-01-01</x:startDate><x:endDate>2026-03-31</x:endDate></x:period></x:context><ix:nonFraction name="g:Revenue" contextRef="c" unitRef="opaque-dollar" scale="6" format="t:num-dot-decimal">1,234.5</ix:nonFraction><ix:nonFraction name="g:Shares" contextRef="c" unitRef="opaque-share">100</ix:nonFraction></body></html>'''
def row(concept='g:Revenue',unit='opaque-dollar',value='1234500000',**kw):
 return dict(statement='income',concept=concept,label='Revenue',level=0,parent_concept=None,is_breakdown=False,dimension_axis=None,dimension_member=None,dimension_count=0,dimensions={},period_start='2026-01-01',period_end='2026-03-31',value=value,unit=unit,balance=None,**kw)

class FilingUnitTests(unittest.TestCase):
 def setUp(self):self.parsed=parse_filing_units(XML.encode())
 def test_opaque_ids_namespace_measure_and_divide(self):
  self.assertEqual({k:v['unit'] for k,v in self.parsed['units'].items()},{'opaque-dollar':'USD','opaque-share':'shares','opaque-eps':'USDPerShare'})
  lines,proof=match_row_units(self.parsed,[row(),row('g:Shares','opaque-share','100')],cik='12')
  self.assertEqual([x['unit'] for x in lines],['USD','shares']);self.assertEqual([x['source_unit_ref'] for x in lines],['opaque-dollar','opaque-share']);self.assertEqual(proof[1]['ordinal'],1)
 def test_id_that_looks_like_usd_has_no_authority(self):
  xml=XML.replace('currency:USD','x:pure').replace('opaque-dollar','U_USD');p=parse_filing_units(xml.encode());out,proof=match_row_units(p,[row(unit='U_USD')],cik='12');self.assertEqual(out[0]['unit'],'pure')
  out,proof=match_row_units(self.parsed,[row(unit='U_USD')],cik='12');self.assertEqual(proof,[]);self.assertEqual(out[0]['unit'],'U_USD')
 def test_exact_company_period_value_unit_and_complete_dimensions(self):
  for change in ({'value':'1234500001'},{'period_start':'2026-01-02'},{'period_end':'2026-04-01'},{'unit':'opaque-share'},{'dimensions':None},{'dimensions':{'g:Axis':'g:Member'}}):
   with self.subTest(change=change):self.assertEqual(match_row_units(self.parsed,[{**row(),**change}],cik='12')[1],[])
  self.assertEqual(match_row_units(self.parsed,[row()],cik='13')[1],[])
 def test_same_amount_different_dimension_member_not_interchangeable(self):
  xml=XML.replace('<x:period>','<x:segment><d:explicitMember dimension="g:Axis">g:Member</d:explicitMember></x:segment><x:period>');p=parse_filing_units(xml.encode())
  self.assertEqual(match_row_units(p,[row()],cik='12')[1],[])
  self.assertTrue(match_row_units(p,[{**row(),'dimensions':{'g:Axis':'g:Member'}}],cik='12')[1])
 def test_duplicate_units_and_dimension_axes_refused(self):
  with self.assertRaises(FilingUnitError):parse_filing_units(XML.replace('</body>','<x:unit id="opaque-dollar"><x:measure>currency:USD</x:measure></x:unit></body>').encode())
  with self.assertRaises(FilingUnitError):parse_filing_units(XML.replace('<x:period>','<x:segment><d:explicitMember dimension="g:A">g:M</d:explicitMember><d:explicitMember dimension="g:A">g:N</d:explicitMember></x:segment><x:period>').encode())
 def test_namespace_alias_not_local_name_currency_guess(self):
  p=parse_filing_units(XML.replace('http://www.xbrl.org/2003/iso4217','https://fake.example').encode());self.assertIsNone(p['units']['opaque-dollar']['unit']);self.assertEqual(match_row_units(p,[row()],cik='12')[1],[])
 def test_decimal_scaling_and_sign_exact(self):
  xml=XML.replace('1,234.5','123456789012345678901234567890.123456789').replace('scale="6"','scale="9" sign="-"');p=parse_filing_units(xml.encode());self.assertEqual(p['facts'][0]['value'],'-123456789012345678901234567890123456789')
 def test_proof_replay_refuses_same_hash_self_consistent_line_forgery(self):
  lines,ev=match_row_units(self.parsed,[row()],cik='12');proof={'rule_ref':RULE_REF,'source':{'record_id':'held'},'raw_sha256':self.parsed['raw_sha256'],'evidence':ev,'evidence_hash':content_hash(ev)};record={'kind':'complete_snapshot','lines':lines,'supplementary_units':proof}
  with tempfile.TemporaryDirectory() as td:
   c=sqlite3.connect(Path(td)/'core.sqlite')
   with patch('dalton_core.sec_filing_units._source',return_value=(proof['source'],XML.encode())):
    validate_unit_supplement(c,record,{'cik':'12'})
    bad=copy.deepcopy(record);bad['lines'][0]['value']='999';bad['supplementary_units']['evidence'][0]['value']='999';b=bad['supplementary_units']['evidence'][0];b['content_hash']=content_hash({k:v for k,v in b.items() if k!='content_hash'});bad['lines'][0]['unit_definition_hash']=b['content_hash'];bad['supplementary_units']['evidence_hash']=content_hash(bad['supplementary_units']['evidence'])
    with self.assertRaises(FilingUnitError):validate_unit_supplement(c,bad,{'cik':'12'})
   c.close()
 def test_legacy_absence_does_not_add_fields_or_require_source(self):
  validate_unit_supplement(None,{},{});r=row();a=_numeric_line_authority(r);self.assertNotIn('source_unit_ref',a);self.assertNotIn('unit_definition_hash',a)
 def test_numeric_prompt_and_filed_series_use_snapshot_units_and_proof(self):
  lines,proof=match_row_units(self.parsed,[row(),row('g:Shares','opaque-share','100')],cik='12')
  lines=[{**x,'ingest_id':'filing','ordinal':i,'line_id':'filing#ctx#'+str(i),'statement_context_ref':'context:bound'} for i,x in enumerate(lines)]
  filing={'ingest_id':'filing','accession':'0000000012-26-000001','form':'10-Q','filed':'2026-04-10','report_date':'2026-03-31','content_hash':'a'*64,'source_record_refs':['held']}
  context,_=_numeric_context([filing],{'filing':lines},structure_series={('income','g:Revenue'),('income','g:Shares')},policy=DEFAULT_NUMERIC_CONTEXT_POLICY)
  self.assertEqual({x['unit'] for x in context['cells']},{'USD','shares'});self.assertTrue(all(x['source_unit_ref'] and x['unit_definition_hash'] for x in context['cells']))
  prompt=_numeric_period_table({'numeric_context':context});self.assertIn('opaque-dollar',prompt);self.assertIn('unit_definition_hash',prompt)
  class Port:
   def statement_series_lines(self,company,concept):return [{**x,'accession':filing['accession'],'filed':filing['filed'],'filing_form':'10-Q'} for x in lines if x['concept']==concept]
  series=_series_for(Port(),'company','g:Revenue');self.assertEqual(series['source_units'],['USD']);self.assertEqual(series['source_evidence']['rows'][0]['source_unit_ref'],'opaque-dollar')
 def test_scripts_fragments_and_external_entities_are_not_numeric_authority(self):
  with self.assertRaises(FilingUnitError):parse_filing_units(XML[:-7].encode())
  scripted=XML.replace('<ix:nonFraction name="g:Revenue"','<script><ix:nonFraction name="g:Revenue"',1).replace('1,234.5</ix:nonFraction>','1,234.5</ix:nonFraction></script>',1)
  self.assertEqual(match_row_units(parse_filing_units(scripted.encode()),[row()],cik='12')[1],[])
  with self.assertRaises(FilingUnitError):parse_filing_units(('<!DOCTYPE html [<!ENTITY fake "x">]>'+XML).encode())
 def test_non_xml_or_unsupported_fact_transform_keeps_gap(self):
  with self.assertRaises(FilingUnitError):parse_filing_units(b'<html><body><p>broken & unescaped</body></html>')
  p=parse_filing_units(XML.replace('t:num-dot-decimal','g:num-dot-decimal').encode());self.assertEqual(match_row_units(p,[row()],cik='12')[1],[])
