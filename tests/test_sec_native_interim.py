import base64
import copy
import json
import unittest
from datetime import date, timedelta

from dalton_core import sec_native_interim as interim
from dalton_core.store import content_hash
from tests import test_sec_issuer_reporting_profile as profile_fixture
from tests import test_sec_filings_index_discovery as fixture


class InterimSourceTests(unittest.TestCase):
    setUp = profile_fixture.ForeignProfileTests.setUp
    run_once = profile_fixture.ForeignProfileTests.run_once
    adapter = profile_fixture.ForeignProfileTests.adapter
    root = profile_fixture.ForeignProfileTests.root
    profile = profile_fixture.ForeignProfileTests.profile

    def body(self, annual='40-F'):
        value = json.loads(profile_fixture.ForeignProfileTests.body(self, annual))
        r = value['filings']['recent']
        r['reportDate'] = [date.today().isoformat()] * len(r['form'])
        r['isXBRL'] = [1 if form == annual else 0 for form in r['form']]
        return json.dumps(value).encode()

    def select(self, profile, **kw):
        return interim.candidates(self.core.connection, self.spool, profile,
            company_ref=fixture.ACN, issuer=fixture.CIK, as_of=date.today(),
            date_from=date.today()-timedelta(days=90), **kw)

    def test_original_non_xbrl_disclosures_are_material_not_quarters_in_both_families(self):
        for annual in ('40-F', '20-F'):
            p = self.root(annual)
            result = self.select(p, limit=4)
            self.assertEqual([x['form'] for x in result['filings']], ['6-K/A', '6-K'])
            self.assertTrue(all(not x['is_xbrl'] for x in result['filings']))
            self.assertFalse(result['financial_periods_inferred'])
            self.assertTrue(result['complete'])
            self.assertEqual(self.core.connection.execute(
                'SELECT COUNT(*) FROM coverage_mission_statement_filings').fetchone()[0], 0)

    def test_stable_bounded_cursor_covers_same_day_and_amendment_without_revision_guess(self):
        p = self.root()
        one = self.select(p, limit=1)
        two = self.select(p, limit=1, cursor=one['next_cursor'])
        self.assertFalse(one['complete'])
        self.assertTrue(two['complete'])
        self.assertNotEqual(one['filings'], two['filings'])
        self.assertTrue(all('revision_of' not in x for x in one['filings']+two['filings']))
        self.assertEqual(one, self.select(p, limit=1))
        self.assertEqual(two, self.select(p, limit=1, cursor=one['next_cursor']))

    def test_cursor_rehash_does_not_authorize_different_root_or_nonexistent_origin(self):
        p = self.root(); first = self.select(p)
        bad = copy.deepcopy(first['next_cursor']); bad['after'][1] = fixture.CIK+'-26-999999'
        bad['content_hash'] = content_hash({k:v for k,v in bad.items() if k!='content_hash'})
        with self.assertRaises(interim.NativeInterimError): self.select(p, cursor=bad)
        newer = self.root()
        with self.assertRaises(interim.NativeInterimError): self.select(newer, cursor=first['next_cursor'])
        p = self.root('10-K')
        with self.assertRaises(interim.NativeInterimError): self.select(p)

    def test_duplicate_accession_and_malformed_xbrl_flag_not_silently_truncated(self):
        for case in ('duplicate', 'flag'):
            raw = json.loads(self.body()); r = raw['filings']['recent']
            if case == 'duplicate': r['accessionNumber'][2] = r['accessionNumber'][1]
            else: r['isXBRL'][1] = '0'
            if case == 'duplicate':
                # The existing root adapter itself can reject ambiguous filings.
                with self.assertRaises(AssertionError): self.root(raw=json.dumps(raw).encode())
            else:
                p = self.root(raw=json.dumps(raw).encode())
                with self.assertRaises(interim.NativeInterimError): self.select(p)


class InterimDocumentSetTests(unittest.TestCase):
    def fixture(self, issuer='0000000123', form='6-K'):
        f = {'accession':'0000000123-26-000001', 'form':form, 'filed':'2026-08-06',
             'report_date':'2026-08-05', 'primary_document':'cover.htm', 'is_xbrl':False}
        base=f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/000000012326000001/'
        members=[{'document':n,'document_type':t,'sequence':str(i+1),'description':t,'url':base+n}
            for i,(n,t) in enumerate([('cover.htm',form),('report.htm','EX-99.1'),('logo.jpg','GRAPHIC')])]
        originals={'cover.htm':b'<html>cover only</html>',
                   'report.htm':b'<html>Six months ended June 30 2026; financial report</html>'}
        v=interim.build_document_set(issuer=issuer,filing=f,reporting_profile_hash='a'*64,
                                    members=members,originals=originals)
        return issuer,f,members,originals,v

    def test_two_issuers_complete_material_set_preserves_sec_reportdate_not_financial_period(self):
        for issuer,form in [('0000000123','6-K'),('0000000456','6-K/A')]:
            i,f,m,o,v=self.fixture(issuer,form)
            self.assertEqual(interim.verify_document_set(v,issuer=i,filing=f,
                reporting_profile_hash='a'*64,expected_hash=v['content_hash']),v)
            self.assertEqual(v['admission'],'material_only')
            self.assertEqual(v['filing']['report_date'],'2026-08-05')
            self.assertNotIn('period_end',v); self.assertNotIn('lines',v)
            self.assertNotIn('revision_of',v)

    def test_full_attachment_index_and_original_bytes_required_with_exact_url_scope(self):
        i,f,m,o,v=self.fixture()
        for case in ('missing','extra','issuer','accession','redirect','duplicate','empty'):
            mm=copy.deepcopy(m); oo=dict(o)
            if case=='missing':oo.pop('report.htm')
            elif case=='extra':oo['foreign.htm']=b'extra'
            elif case=='issuer':mm[1]['url']=mm[1]['url'].replace('/123/','/456/')
            elif case=='accession':mm[1]['url']=mm[1]['url'].replace('26000001','26000002')
            elif case=='redirect':mm[1]['url']+='?other=1'
            elif case=='duplicate':mm.append(mm[0])
            else:oo['report.htm']=b''
            with self.subTest(case=case),self.assertRaises(ValueError):
                interim.build_document_set(issuer=i,filing=f,reporting_profile_hash='a'*64,
                                            members=mm,originals=oo)

    def test_rehashed_material_cannot_replace_strong_receipt_or_create_quarter_admission(self):
        i,f,m,o,v=self.fixture()
        for case in ('bytes','profile','period','issuer','admission'):
            changed=copy.deepcopy(v)
            if case=='bytes':
                x=changed['originals'][0];raw=b'altered';x.update(base64=base64.b64encode(raw).decode(),
                    size_bytes=len(raw),sha256=__import__('hashlib').sha256(raw).hexdigest())
            elif case=='profile':changed['reporting_profile_hash']='b'*64
            elif case=='period':changed['filing']['report_date']='2026-06-30'
            elif case=='issuer':changed['issuer']='0000000456'
            else:changed['admission']='quarterly_financials'
            changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
            with self.subTest(case=case),self.assertRaises(interim.NativeInterimError):
                interim.verify_document_set(changed,issuer=i,filing=f,
                    reporting_profile_hash='a'*64,expected_hash=v['content_hash'])


if __name__=='__main__':unittest.main()
