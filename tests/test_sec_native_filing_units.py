"""Native annual original unit facts are separate from domestic inline rules."""
import copy
import unittest

from dalton_core.sec_native_filing_units import parse_instance_units, _match_documents
from dalton_core.sec_filing_units import FilingUnitError, match_row_units
from dalton_core.sec_native_annual import build_document_set


def instance(issuer='0001725964', currency='USD', value='2.5', unit='opaque-eps'):
    return f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
        xmlns:iso="http://www.xbrl.org/2003/iso4217" xmlns:ifrs="http://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full"
        xmlns:d="http://issuer.example/dimensions" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      <xbrli:context id="year"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{issuer}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="{unit}"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso:{currency}</xbrli:measure></xbrli:unitNumerator>
        <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
      <ifrs:DilutedEarningsLossPerShare contextRef="year" unitRef="{unit}" decimals="2">{value}</ifrs:DilutedEarningsLossPerShare>
    </xbrli:xbrl>'''.encode()


def row(unit='opaque-eps'):
    return {'concept':'ifrs:DilutedEarningsLossPerShare', 'period_start':'2025-01-01',
        'period_end':'2025-12-31', 'value':'2.5', 'unit':unit, 'dimensions':{}}


class NativeInstanceUnitTests(unittest.TestCase):
    def test_two_issuers_currency_per_share_preserves_taxonomy_and_annual_duration(self):
        for issuer,currency in [('0001725964','USD'),('0000000123','CAD')]:
            with self.subTest(issuer=issuer):
                parsed=parse_instance_units(instance(issuer,currency))
                lines,proof=match_row_units(parsed,[row()],cik=issuer)
                self.assertEqual(lines[0]['unit'],currency+'PerShare')
                self.assertEqual(lines[0]['concept'],row()['concept'])
                self.assertEqual(lines[0]['period_start'],'2025-01-01')
                self.assertEqual(proof[0]['source_unit_ref'],'opaque-eps')

    def test_wrong_issuer_period_value_dimension_and_unbound_measure_do_not_match(self):
        parsed=parse_instance_units(instance())
        for field,value in [('period_end','2025-09-30'),('value','2.6'),('dimensions',{'d:Axis':'d:Member'})]:
            changed={**row(),field:value}
            self.assertEqual(match_row_units(parsed,[changed],cik='0001725964')[1],[])
        self.assertEqual(match_row_units(parsed,[row()],cik='0000000123')[1],[])
        parsed=parse_instance_units(instance().replace(b'iso:USD',b'other:USD'))
        self.assertEqual(match_row_units(parsed,[row()],cik='0001725964')[1],[])

    def test_duplicate_conflicting_fact_or_unit_ids_and_bad_period_refuse(self):
        raw=instance()
        cases=[raw.replace(b'</xbrli:xbrl>',b'<ifrs:DilutedEarningsLossPerShare contextRef="year" unitRef="opaque-eps">2.6</ifrs:DilutedEarningsLossPerShare></xbrli:xbrl>'),
            raw.replace(b'</xbrli:xbrl>',b'<xbrli:unit id="opaque-eps"><xbrli:measure>iso:CAD</xbrli:measure></xbrli:unit></xbrli:xbrl>'),
            raw.replace(b'<xbrli:endDate>2025-12-31</xbrli:endDate>',b'<xbrli:instant>2025-12-31</xbrli:instant>')]
        for altered in cases:
            with self.assertRaises(ValueError):parse_instance_units(altered)

    def docset(self, form='40-F', issuer='0001725964', second=None):
        filing={'accession':'0001193125-26-081326','form':form,'filed':'2026-02-27',
            'report_date':'2025-12-31','primary_document':'annual.htm','is_xbrl':True}
        names=[('annual.htm',form),('facts.xml','EX-101.INS')]
        originals={'annual.htm':b'<html>annual cover</html>','facts.xml':instance(issuer)}
        if second is not None:names.append(('second.xml','EX-101.INS'));originals['second.xml']=second
        url=f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/000119312526081326/'
        members=[{'document':n,'document_type':t,'sequence':str(i+1),'description':t,'url':url+n}for i,(n,t)in enumerate(names)]
        return build_document_set(issuer=issuer,filing=filing,reporting_profile_hash='a'*64,members=members,originals=originals)

    def test_complete_original_member_fact_binding_across_both_annual_forms(self):
        for form,issuer in [('40-F','0001725964'),('20-F','0000000123'),
                            ('40-F/A','0001725964'),('20-F/A','0000000123')]:
            lines,proof=_match_documents(self.docset(form,issuer),[row()])
            self.assertEqual(lines[0]['unit'],'USDPerShare')
            self.assertEqual(proof[0]['original_document'],'facts.xml')
            self.assertEqual(lines[0]['unit_definition_hash'],proof[0]['content_hash'])

    def test_members_cannot_splice_local_ids_or_override_conflicting_currency(self):
        docset=self.docset(second=instance(currency='CAD'))
        with self.assertRaisesRegex(FilingUnitError,'disagree'):_match_documents(docset,[row()])
        docset=self.docset(second=instance(unit='other-unit'))
        self.assertEqual(_match_documents(docset,[row()])[0][0]['unit'],'USDPerShare')
        with self.assertRaisesRegex(FilingUnitError,'disagree'):
            _match_documents(self.docset(second=instance(value='2.6')),[row()])

    def test_corrupt_original_bytes_refuse_before_unit_match(self):
        value=self.docset();changed=copy.deepcopy(value)
        changed['originals'][0]['sha256']='f'*64
        with self.assertRaisesRegex(FilingUnitError,'bytes'):_match_documents(changed,[row()])

    def test_declared_instance_cannot_be_ignored_beside_another_valid_instance(self):
        for damaged in [b'not XML',b'<html>not an instance</html>',instance()[:-20]]:
            with self.subTest(damaged=damaged[:20]):
                with self.assertRaises(ValueError):
                    _match_documents(self.docset(second=damaged),[row()])

    def test_bounded_instance_bytes_and_unsupported_context_keep_no_authority(self):
        import gzip
        from unittest.mock import patch
        raw=instance()
        with patch('dalton_core.sec_native_filing_units.MAX_BYTES',200):
            for value in [raw,gzip.compress(raw)]:
                with self.assertRaisesRegex(FilingUnitError,'byte'):
                    parse_instance_units(value)
        typed=raw.replace(b'<xbrli:period>',b'<xbrli:segment><xbrldi:typedMember dimension="d:Axis"><d:Member>opaque</d:Member></xbrldi:typedMember></xbrli:segment><xbrli:period>')
        self.assertEqual(match_row_units(parse_instance_units(typed),[row()],cik='0001725964')[1],[])
        wrong=raw.replace(b'<xbrli:measure>iso:USD</xbrli:measure>',b'<d:measure>iso:USD</d:measure>')
        self.assertEqual(match_row_units(parse_instance_units(wrong),[row()],cik='0001725964')[1],[])

    def test_native_inline_member_uses_existing_numeric_transform_rule(self):
        from tests.test_sec_filing_unit_context import XML,row as domestic_row
        value=self.docset(issuer='0000000012')
        originals={'annual.htm':XML.replace('2026-01-01','2025-01-01').replace('2026-03-31','2025-12-31').encode(),
                   'facts.xml':instance(issuer='0000000012')}
        value=build_document_set(issuer=value['issuer'],filing=value['filing'],
            reporting_profile_hash=value['reporting_profile_hash'],members=value['members'],originals=originals)
        annual={**domestic_row(),'period_start':'2025-01-01','period_end':'2025-12-31'}
        lines,proof=_match_documents(value,[annual])
        self.assertEqual(lines[0]['unit'],'USD')
        self.assertEqual(lines[0]['value'],annual['value'])
        self.assertEqual(proof[0]['original_document'],'annual.htm')
        from dalton_core.sec_filing_units import NUMERIC_RULE_REF
        self.assertEqual(proof[0]['numeric_parse_rule_ref'],NUMERIC_RULE_REF)

    def test_zero_and_nonzero_expansion_exponents_share_the_same_bound(self):
        for token in ['0e-1001','0e1001','1e-1001','1e1001']:
            with self.subTest(token=token):
                self.assertEqual(parse_instance_units(instance(value=token))['facts'],[])
        parsed=parse_instance_units(instance(value='0e-1000'))
        self.assertEqual(len(parsed['facts'][0]['value']),1002)
        lines,proof=match_row_units(parsed,[{**row(),'value':'0'}],cik='0001725964')
        self.assertEqual(lines[0]['unit'],'USDPerShare')
        self.assertEqual(len(proof),1)


if __name__=='__main__':unittest.main()


from tests import test_sec_native_annual_pipeline as pipeline_fixture


class NativeUnitPipelineTests(unittest.TestCase):
    setUp=pipeline_fixture.NativeAnnualPipelineTests.setUp
    run_once=pipeline_fixture.NativeAnnualPipelineTests.run_once
    adapter=pipeline_fixture.NativeAnnualPipelineTests.adapter
    root=pipeline_fixture.NativeAnnualPipelineTests.root
    profile=pipeline_fixture.NativeAnnualPipelineTests.profile
    body=pipeline_fixture.NativeAnnualPipelineTests.body
    admitted=pipeline_fixture.NativeAnnualPipelineTests.admitted
    captured=pipeline_fixture.NativeAnnualPipelineTests.captured
    summary=pipeline_fixture.NativeAnnualPipelineTests.summary

    def captured_opaque(self, request, *, missing_fact=False):
        from dalton_core.sec_native_annual import build_document_set
        raw=self.captured(request);parsed=raw['filings'][0]
        issuer=request['reporting_profile']['issuer']
        for role, statement in parsed['statements'].items():
            statement['facts'][0]['unit_ref']='opaque-money'
        contexts=f'''<xbrli:context id="duration"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{issuer}</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
          <xbrli:context id="instant"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{issuer}</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>'''
        facts=''.join(f'<{statement["facts"][0]["concept"]} contextRef="'+('instant'if role=='balance'else'duration')+'" unitRef="opaque-money">100</'+statement['facts'][0]['concept']+'>'
                      for role, statement in parsed['statements'].items() if not (missing_fact and role=='cash'))
        xml=f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:iso="http://www.xbrl.org/2003/iso4217" xmlns:ifrs-full="http://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full">
          {contexts}<xbrli:unit id="opaque-money"><xbrli:measure>iso:USD</xbrli:measure></xbrli:unit>{facts}</xbrli:xbrl>'''.encode()
        docset=parsed['native_document_set']
        parsed['native_document_set']=build_document_set(issuer=issuer,filing=request['filings'][0],
            reporting_profile_hash=request['reporting_profile']['content_hash'],members=docset['members'],
            originals={'annual.htm':b'<html>Annual statements</html>','facts.xml':xml})
        return raw

    def stored(self, annual='40-F', *, missing_fact=False):
        m,r,d,g,gp,p=self.admitted(annual)
        s=self.summary(r,gp,p,self.captured_opaque(r,missing_fact=missing_fact))
        self.assertEqual(s['status'],'succeeded',s.get('failure_reason'))
        filing=m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=s['observation'],
            governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)['filings'][0]
        return m,filing

    def test_native_annual_durable_source_context_read_and_model_units_without_quarters(self):
        from dalton_core.statement_context import StatementContextAuthority,context_record,normalizer_hash
        from dalton_core.sec_native_filing_units import prepare_model_unit_inputs
        from dalton_core.sec_filing_units import unresolved_unit_refs
        m,filing=self.stored()
        before=normalizer_hash()
        context=StatementContextAuthority(self.core).backfill(self.state,filing['ingest_id'],discover_companyfacts=False)
        self.assertEqual(context['status'],'available',context.get('reason'))
        self.assertEqual(context['kind'],'complete_snapshot')
        self.assertEqual(len(context['supplementary_native_units']['evidence']),3)
        self.assertEqual(context_record(m.connection,context['version_id']),context)
        self.assertEqual(unresolved_unit_refs(m.statement_lines(filing['ingest_id'])),[])
        self.assertEqual(prepare_model_unit_inputs(m,self.state,filing['company_ref']),{'refreshed':False,'unresolved':[]})
        self.assertEqual(normalizer_hash(),before)
        self.assertEqual({x['period_start']for x in m.statement_lines(filing['ingest_id'])},{None,'2025-01-01'})
        from dalton_core.company_model_inputs import build_model_inputs
        from tests.test_company_model_inputs import _spec
        spec=_spec(drivers=[{'ref':'native-revenue','label':'Native revenue','kind':'mix',
            'basis_concept':'ifrs-full:Revenue','unit':'USD','because':'Exact original native concept'}],expenses=[])
        spec['schema_version']='0.4'
        spec['financial_statement_structure']={'schema_version':'0.4','lines':[
            {'ref':'native-'+role,'kind':'filed','statement':role,'concept':concept}
            for role,concept in [('income','ifrs-full:Revenue'),('balance','ifrs-full:Assets'),
                                 ('cash','ifrs-full:CashFlowsFromUsedInOperatingActivities')]],'formulas':[]}
        table=build_model_inputs(m,spec)
        filed={x['concept']:x for x in table['filed_lines']}
        self.assertEqual(set(filed),{x['concept'] for x in spec['financial_statement_structure']['lines']})
        self.assertTrue(filed['ifrs-full:Revenue']['duration_facts'])
        self.assertFalse(filed['ifrs-full:Revenue']['cells'])
        self.assertEqual(filed['ifrs-full:Revenue']['source_evidence']['rows'][0]['accession'],filing['accession'])

    def _request_after_normal_context_refresh(self,annual):
        from unittest.mock import patch
        from dalton_core.statement_context import StatementContextAuthority
        from dalton_core.sec_native_filing_units import unit_requirement_status
        from dalton_core import material_requests as mr
        # A durable opaque context, followed by the normal unit proof
        # refresh, uses the same original annual evidence and ingest.
        m,filing=self.stored(annual)
        authority=StatementContextAuthority(self.core)
        with patch('dalton_core.sec_native_filing_units.supplemental_native_unit_lines',
                   side_effect=lambda connection,state,filing,rows:([dict(x)for x in rows],None)):
            old=authority.backfill(self.state,filing['ingest_id'],discover_companyfacts=False)
        request=next(x for x in mr.open_requests(self.state)
                     if x['acceptance'].get('accession')==filing['accession'])
        self.assertFalse(unit_requirement_status(self.state,request)['met'])
        fresh=authority.backfill(self.state,filing['ingest_id'],discover_companyfacts=False)
        self.assertNotEqual(old['version_id'],fresh['version_id'])
        result=unit_requirement_status(self.state,request)
        self.assertTrue(result['met'],result['reason'])
        self.assertEqual(result['items'][0]['statement_context_ref'],fresh['version_id'])
        self.assertNotIn('source_manifest_hash',result['items'][0])
        for field,value in [('cik','0000000123'),('accession','0001193125-26-081327'),
                            ('document_ref','sec:filing:0001193125-26-081327')]:
            changed=copy.deepcopy(request);changed['acceptance'][field]=value
            self.assertFalse(unit_requirement_status(self.state,changed)['met'])

    def test_missing_original_fact_keeps_exact_foreign_annual_request_and_strong_priority(self):
        from dalton_core.statement_context import StatementContextAuthority
        from dalton_core import material_requests as mr
        from dalton_core.material_acquisition import TargetedSourceRung
        from dalton_core.material_model_unit_priority import unit_input_proof,unit_input_still_current
        from dalton_core.material_current_gap_priority import CONTRACT_REF
        m,filing=self.stored('20-F',missing_fact=True)
        context=StatementContextAuthority(self.core).backfill(self.state,filing['ingest_id'],discover_companyfacts=False)
        self.assertEqual(context['status'],'available',context.get('reason'))
        self.assertEqual(context['unresolved_unit_refs'],['opaque-money'])
        requests=mr.open_requests(self.state)
        request=next(x for x in requests if x['acceptance'].get('accession')==filing['accession'])
        self.assertEqual(request['need']['doc_kinds'],['sec_20f'])
        self.assertEqual(TargetedSourceRung('sec_edgar').spec_for(request),'annual-report-20f')
        import json
        mission=json.loads(m.connection.execute('SELECT record_json FROM coverage_mission_versions ORDER BY version_number DESC LIMIT 1').fetchone()[0])
        proof=unit_input_proof(m.connection,request,mission,contract_ref=CONTRACT_REF,cache={})
        self.assertIsNotNone(proof)
        self.assertTrue(unit_input_still_current(m.connection,proof))

    def test_20f_original_units_close_only_exact_request_after_normal_context_refresh(self):
        self._request_after_normal_context_refresh("20-F")

    def test_40f_original_units_close_only_exact_request_after_normal_context_refresh(self):
        self._request_after_normal_context_refresh("40-F")

    def test_rehashed_overlay_or_wrong_original_issuer_cannot_replace_kept_native_source(self):
        from dalton_core.statement_context import StatementContextAuthority
        from dalton_core.sec_native_filing_units import validate_native_unit_supplement
        from dalton_core.store import content_hash
        m,filing=self.stored()
        context=StatementContextAuthority(self.core).backfill(self.state,filing['ingest_id'],discover_companyfacts=False)
        changed=copy.deepcopy(context);proof=changed['supplementary_native_units']
        match=proof['evidence'][0];match['unit']='CAD';match['content_hash']=content_hash({k:v for k,v in match.items() if k!='content_hash'})
        changed['lines'][match['ordinal']].update(unit='CAD',unit_definition_hash=match['content_hash'])
        proof['evidence_hash']=content_hash(proof['evidence']);proof['content_hash']=content_hash({k:v for k,v in proof.items() if k!='content_hash'})
        with self.assertRaisesRegex(FilingUnitError,'replay'):
            validate_native_unit_supplement(m.connection,changed,filing)
        with self.assertRaises(ValueError):
            validate_native_unit_supplement(m.connection,context,{**filing,'cik':'0000000123'})
        with self.assertRaisesRegex(FilingUnitError,'hash or kind'):
            validate_native_unit_supplement(m.connection,{**context,'native_unit_contract':'wrong-contract'},filing)
