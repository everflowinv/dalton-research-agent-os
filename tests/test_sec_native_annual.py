import base64
import copy
import unittest
from dalton_core.sec_native_annual import (NativeAnnualError,build_document_set,verify_document_set,
    validate_annual_lines,compact_document_set,make_request)
from dalton_core.sec_financials_core import sec_financials_output_schema
from dalton_core.store import content_hash


class NativeAnnualTests(unittest.TestCase):
    def fixture(self, issuer='0001725964', form='40-F'):
        f={'accession':'0001193125-26-081326','form':form,'filed':'2026-02-27','report_date':'2025-12-31',
           'primary_document':'annual.htm','is_xbrl':True}
        base=f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/000119312526081326/'
        members=[{'document':n,'document_type':t,'description':t,'sequence':str(i+1),'url':base+n}
          for i,(n,t) in enumerate([('annual.htm',form),('statements.htm','EX-99.1'),('facts.xml','EX-101.INS'),('logo.jpg','GRAPHIC')])]
        originals={'annual.htm':b'<html>annual cover</html>','statements.htm':b'<html>original financial statements</html>',
                   'facts.xml':b'<xbrl>native facts</xbrl>'}
        value=build_document_set(issuer=issuer,filing=f,reporting_profile_hash='a'*64,members=members,originals=originals)
        return issuer,f,members,originals,value

    def test_two_foreign_annual_families_preserve_original_forms_taxonomy_and_exact_source_members(self):
        for issuer,form in [('0001725964','40-F'),('0000000123','20-F')]:
            i,f,m,o,v=self.fixture(issuer,form)
            self.assertEqual(verify_document_set(v,issuer=i,filing=f,reporting_profile_hash='a'*64),v)
            self.assertEqual(v['filing']['form'],form)
            compact=compact_document_set(v)
            self.assertTrue(all('base64' not in x for x in compact['originals']))
            self.assertEqual(compact['raw_document_set_hash'],v['content_hash'])

    def test_missing_member_primary_and_wrong_issuer_accession_url_refused(self):
        i,f,m,o,v=self.fixture()
        for altered in ('missing','primary','issuer','accession','auth','query'):
            mm=copy.deepcopy(m);oo=copy.deepcopy(o)
            if altered=='missing':oo.pop('facts.xml')
            elif altered=='primary':mm=mm[1:];oo.pop('annual.htm')
            elif altered=='issuer':mm[0]['url']=mm[0]['url'].replace('/1725964/','/123/')
            elif altered=='accession':mm[0]['url']=mm[0]['url'].replace('081326','081327')
            elif altered=='auth':mm[0]['url']=mm[0]['url'].replace('www.sec.gov','evil@www.sec.gov')
            elif altered=='query':mm[0]['url']+='?other=1'
            with self.subTest(altered=altered),self.assertRaises(NativeAnnualError):
                build_document_set(issuer=i,filing=f,reporting_profile_hash='a'*64,members=mm,originals=oo)

    def test_rehashed_metadata_and_original_bytes_do_not_replace_frozen_parent_binding(self):
        i,f,m,o,v=self.fixture()
        for key in ('issuer','filing','reporting_profile_hash','originals'):
            x=copy.deepcopy(v)
            if key=='issuer':x[key]='0000000123'
            elif key=='filing':x[key]['report_date']='2024-12-31'
            elif key=='reporting_profile_hash':x[key]='b'*64
            else:x[key][0]['base64']=base64.b64encode(b'changed').decode()
            x['content_hash']=content_hash({k:v for k,v in x.items() if k!='content_hash'})
            with self.subTest(key=key),self.assertRaises((NativeAnnualError,ValueError)):
                verify_document_set(x,issuer=i,filing=f,reporting_profile_hash='a'*64)

    def test_6k_or_non_xbrl_never_enters_annual_admission(self):
        i,f,m,o,v=self.fixture()
        for form,xbrl in [('6-K',True),('6-K/A',True),('40-F',False)]:
            x={**f,'form':form,'is_xbrl':xbrl}
            with self.assertRaises(NativeAnnualError):build_document_set(issuer=i,filing=x,reporting_profile_hash='a'*64,members=m,originals=o)

    def test_complete_actual_annual_roles_required_and_h1_not_renamed_quarter(self):
        lines=[{'statement':s,'period_end':'2025-12-31','period_start':None if s=='balance' else '2025-01-01',
                'dimensions':{},'concept':'ifrs-full:'+s,'unit':'USD','value':'1'} for s in ('income','balance','cash')]
        validate_annual_lines(lines,'2025-12-31')
        for kind in ('missing','h1','wrong_end','segmented'):
            x=copy.deepcopy(lines)
            if kind=='missing':x.pop()
            elif kind=='h1':x[2]['period_start']='2025-07-01'
            elif kind=='wrong_end':x[2]['period_end']='2025-09-30'
            elif kind=='segmented':x[2]['dimensions']={'axis':'member'}
            with self.subTest(kind=kind),self.assertRaises(NativeAnnualError):validate_annual_lines(x,'2025-12-31')

    def test_native_approval_requires_explicit_v6_and_original_effective_window(self):
        from dalton_core.connector_governance import ConnectorGovernance,build_governance_record
        from dalton_core.sec_financials_core import KIND
        from dalton_core.sec_native_annual import verify_native_approval
        for version,at in [(5,'2026-10-13T00:00:00+00:00'),(6,'2025-12-31T23:59:59+00:00')]:
            g=ConnectorGovernance(build_governance_record(KIND,approved_by='human:synthetic-owner',status='approved',
                effective_from='2026-01-01T00:00:00+00:00',version=version))
            with self.assertRaises(NativeAnnualError):verify_native_approval(g,at=at)
        value=build_governance_record(KIND,approved_by='human:synthetic-owner',status='approved',
                effective_from='2026-01-01T00:00:00+00:00',version=6)
        value['effective_until']='2026-10-01T00:00:00+00:00'
        value['content_hash']=content_hash({k:v for k,v in value.items() if k!='content_hash'})
        g=ConnectorGovernance(value)
        self.assertEqual(verify_native_approval(g,at='2026-09-30T23:59:59+00:00'),g)
        with self.assertRaises(NativeAnnualError):verify_native_approval(g,at='2026-10-01T00:00:00+00:00')

    def test_closed_v6_metadata_and_old_contracts_have_no_native_fields(self):
        for v in (2,3,4,5):
            f=sec_financials_output_schema(version=v)['properties']['filings']['items']
            self.assertNotIn('native_document_set',f['properties'])
            self.assertNotIn('40-F',f['properties']['form']['enum'])
        f=sec_financials_output_schema(version=6)['properties']['filings']['items']
        self.assertIn('40-F',f['properties']['form']['enum']);self.assertNotIn('6-K',f['properties']['form']['enum'])
        self.assertFalse(f['properties']['native_document_set']['additionalProperties'])

if __name__=='__main__':unittest.main()


class DomesticWireGoldenTests(unittest.TestCase):
    def test_old_schemas_identity_normalizer_hash_and_domestic_wire_are_exact(self):
        import json, hashlib
        from pathlib import Path
        from dalton_core import sec_financials_core, statement_context, sec_financials_normalise, statement_companyfacts_context
        from dalton_core.store import canonical_json
        from tests.test_sec_financials_cli import fixture_parse
        golden=json.loads(Path(__file__).with_name('fixtures').joinpath('sec_native_domestic_wire_golden.json').read_bytes())
        for version in (2,3,4,5):
            self.assertEqual(canonical_json(sec_financials_core.sec_financials_output_schema(version=version)),
                             canonical_json(golden['old_versions'][str(version)]['schema']))
            self.assertEqual(sec_financials_core.sec_financials_identity(version=version),golden['old_versions'][str(version)]['identity'])
        self.assertEqual(statement_context.normalizer_hash(),golden['normalizer_hash'])
        # The unit-source compatibility alias may evolve independently; its
        # semantic identity is covered by the normalizer hash above.
        for module in (sec_financials_normalise,statement_companyfacts_context):
            self.assertEqual(hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
                             golden['normalizer_sources_sha256'][Path(module.__file__).name])
        raw=fixture_parse();f=raw['filings'][0]
        wire=sec_financials_normalise.normalise_filing(accession=f['accession'],form=f['form'],filed=f['filed'],
            report_date=f['report_date'],statements=f['statements'])
        self.assertEqual(canonical_json(wire),canonical_json(golden['domestic_normalised_filing']))
