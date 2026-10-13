"""Actual Filing API shape has period_of_report, not report_date."""
import sys
import types
import unittest
from datetime import date
from unittest.mock import Mock,patch

from dalton_core.sec_financials_cli import parse_live,_filing_report_date,SecFinancialsRunError
from dalton_core.sec_native_annual import verify_document_set,validate_annual_lines
from dalton_core.sec_financials_normalise import normalise_filing


class Frame:
    def __init__(self, rows):self.rows=rows
    def __getitem__(self, key):
        if key=='statement_type':return types.SimpleNamespace(__eq__=lambda other:other)
        return Frame([x for x in self.rows if x['statement_type']==key])
    def to_dict(self, orient):return self.rows


class Column:
    def __eq__(self,other):return other


class FactsFrame(Frame):
    def __getitem__(self,key):return Column()if key=='statement_type'else super().__getitem__(key)


def xbrl():
    roles={'IncomeStatement':('income','ifrs-full:Revenue'),
           'BalanceSheet':('balance','ifrs-full:Assets'),
           'CashFlowStatement':('cash','ifrs-full:CashFlowsFromUsedInOperatingActivities')}
    facts=[{'statement_type':kind,'concept':concept,'period_start':None if role=='balance'else'2025-01-01',
            'period_end':'2025-12-31','value':'100','unit_ref':'USD','dim_example:UnusedAxis':None}
           for kind,(role,concept)in roles.items()]
    accessors={}
    for role,concept in roles.values():
        frame=Frame([{'concept':concept,'label':concept,'level':0,'abstract':False,'parent_concept':None,
                      'is_breakdown':False,'dimension_axis':None,'dimension_member':None,'balance':'credit'}])
        accessors[role]=lambda f=frame:types.SimpleNamespace(to_dataframe=lambda:f)
    query=types.SimpleNamespace(with_dimensions=lambda:types.SimpleNamespace(to_dataframe=lambda:FactsFrame(facts)))
    return types.SimpleNamespace(query=lambda:query,statements=types.SimpleNamespace(
        income_statement=accessors['income'],balance_sheet=accessors['balance'],cashflow_statement=accessors['cash']))


class FilingApiTests(unittest.TestCase):
    def modules(self, *, issuer='0001725964', form='40-F', report='2025-12-31'):
        class Filing:
            def __init__(self, *, cik, company, form, filing_date, accession_no):
                self.cik=cik;self.company=company;self.form=form;self.filing_date=filing_date;self.accession_no=accession_no
            @property
            def period_of_report(self):return report
            @property
            def attachments(self):
                base=f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{self.accession_no.replace("-","")}/'
                return [types.SimpleNamespace(document='annual.htm',document_type=self.form,sequence_number='1',description='Annual',url=base+'annual.htm')]
            def xbrl(self):return xbrl()
        company=types.SimpleNamespace(cik=int(issuer),name='Synthetic foreign issuer')
        edgar=types.ModuleType('edgar');edgar.Company=lambda ref:company;edgar.Filing=Filing
        transport=types.ModuleType('edgar.httprequests')
        transport.get_with_retry=Mock(side_effect=lambda *,url:types.SimpleNamespace(url=url,content=b'<html>Original annual</html>'))
        transport.inspect_response=lambda response:None
        return edgar,transport,Filing

    def request(self, issuer='0001725964', form='40-F'):
        return {'reporting_profile':{'issuer':issuer,'content_hash':'a'*64},'filings':[
            {'accession':'0001193125-26-081326','form':form,'filed':'2026-02-27','report_date':'2025-12-31',
             'primary_document':'annual.htm','is_xbrl':True}]}

    def test_native_two_annual_families_actual_api_shape_full_parse_and_original_period_proof(self):
        for issuer,form in [('0001725964','40-F'),('0000000123','20-F')]:
            with self.subTest(form=form):
                edgar,transport,_=self.modules(issuer=issuer,form=form);request=self.request(issuer,form)
                with patch.dict(sys.modules,{'edgar':edgar,'edgar.httprequests':transport}):
                    raw=parse_live(None,issuer,form=form,limit=1,user_agent='Synthetic test contact test@example.invalid',native_request=request)
                self.assertEqual(raw['filings'][0]['report_date'],'2025-12-31')
                filing=raw['filings'][0]
                verify_document_set(filing['native_document_set'],issuer=issuer,filing=request['filings'][0],reporting_profile_hash='a'*64)
                lines=normalise_filing(**{k:filing[k]for k in ('accession','form','filed','report_date','statements')})['lines']
                validate_annual_lines(lines,'2025-12-31')
                self.assertEqual(transport.get_with_retry.call_count,1)

    def test_native_mismatched_missing_and_invalid_report_date_refuse_before_attachments(self):
        for report in ('2024-12-31',None,'2025-02-30','20251231'):
            with self.subTest(report=report):
                edgar,transport,_=self.modules(report=report)
                with patch.dict(sys.modules,{'edgar':edgar,'edgar.httprequests':transport}),self.assertRaises(SecFinancialsRunError):
                    parse_live(None,'0001725964',form='40-F',limit=1,user_agent='Synthetic test contact test@example.invalid',native_request=self.request())
                transport.get_with_retry.assert_not_called()

    def test_legacy_explicit_report_date_remains_exact_and_never_reads_another_period(self):
        class Legacy:
            report_date=date(2025,12,31)
            @property
            def period_of_report(self):raise AssertionError('legacy date must remain authoritative')
        self.assertEqual(_filing_report_date(Legacy()),'2025-12-31')

    def test_standard_domestic_actual_api_without_report_date_uses_original_period(self):
        edgar,transport,Filing=self.modules(form='10-K')
        filing=Filing(cik=1725964,company='Synthetic',form='10-K',filing_date='2026-02-27',accession_no='0001193125-26-081326')
        edgar.Company=lambda ref:types.SimpleNamespace(cik=1725964,name='Synthetic',
            get_filings=lambda **kw:types.SimpleNamespace(latest=lambda limit:filing))
        with patch.dict(sys.modules,{'edgar':edgar,'edgar.httprequests':transport}):
            raw=parse_live('SYN',None,form='10-K',limit=1,user_agent='Synthetic test contact test@example.invalid')
        self.assertEqual(raw['filings'][0]['report_date'],'2025-12-31')
        self.assertNotIn('native_document_set',raw['filings'][0])
        transport.get_with_retry.assert_not_called()

    def test_domestic_without_xbrl_keeps_existing_skip_without_reading_report_period(self):
        class NoXbrl:
            def xbrl(self):return None
            @property
            def period_of_report(self):raise AssertionError('no report fetch for skipped filing')
        edgar=types.ModuleType('edgar')
        edgar.Company=lambda ref:types.SimpleNamespace(cik=1725964,name='Synthetic',
            get_filings=lambda **kw:types.SimpleNamespace(latest=lambda limit:NoXbrl()))
        with patch.dict(sys.modules,{'edgar':edgar}):
            self.assertEqual(parse_live('SYN',None,form='10-K',limit=1,
                user_agent='Synthetic test contact test@example.invalid')['filings'],[])
