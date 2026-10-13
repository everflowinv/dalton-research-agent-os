"""Discover exact amended documents without inventing replacement authority."""
import copy
import json
import unittest

from dalton_core.sec_public_adapter import normalize_sec_submissions, SecPublicAdapterError
from dalton_core.sec_filings_index import build_filing_url_authorities, filings_index_adapter_hash
from dalton_core.store import content_hash
from tests.test_sec_public_adapter import payload, PARAMETERS


class IndependentAmendmentIndexTests(unittest.TestCase):
    def test_two_issuers_preserve_original_amendment_identity_and_no_replacement_edge(self):
        for issuer in ('0000007084', '0000315189'):
            with self.subTest(issuer=issuer):
                body=payload();body['cik']=issuer
                del body['filings']['recent']['amendmentOf']
                parameters={**PARAMETERS,'issuer':issuer}
                output=normalize_sec_submissions(body,parameters,provider_status=200)
                amendment=output['records'][1]
                self.assertEqual(amendment['record_ref'],'sec:filing:0000000003-25-000003')
                self.assertIsNone(amendment['revision_of_ref'])
                self.assertEqual(amendment['record_hash'],content_hash(dict(accession='0000000003-25-000003',
                    form='10-Q/A',filing_date='2025-04-30',primary_document='q2a.htm',revision_of=None)))
                urls=build_filing_url_authorities(json.dumps(body).encode(),parameters)
                self.assertEqual(urls[1]['form'],'10-Q/A')
                self.assertEqual(urls[1]['accession'],'0000000003-25-000003')
                self.assertEqual(urls[1]['canonical_url'],f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/000000000325000003/q2a.htm')

    def test_explicit_revision_and_other_form_records_keep_old_exact_wire(self):
        result=normalize_sec_submissions(payload(),PARAMETERS,provider_status=200)
        records=[]
        for accession,form,date,doc,parent in [('0000000002-25-000002','10-Q','2025-01-29','q1.htm',None),
                ('0000000003-25-000003','10-Q/A','2025-04-30','q2a.htm','0000000002-25-000002'),
                ('0000000004-25-000004','10-Q','2025-10-29','q3.htm',None)]:
            records.append(dict(record_ref='sec:filing:'+accession,revision_of_ref=None if parent is None else 'sec:filing:'+parent,
                record_hash=content_hash(dict(accession=accession,form=form,filing_date=date,primary_document=doc,revision_of=parent))))
        self.assertEqual(result,dict(records=records,source_record_refs=[r['record_ref']for r in records],
            request_cursor=None,next_cursor=None,page_ordinal=1,provider_status=200))
        # Existing profile identities bind the frozen transport contract,
        # not the implementation file. No old profile is rewritten.
        self.assertEqual(filings_index_adapter_hash(),'7515fbfa35c4f648e25406508f4d33428a62f72718a29a31b221d0d8210768a6')

    def test_amendments_respect_exact_form_date_window_and_complete_limit(self):
        body=payload();del body['filings']['recent']['amendmentOf']
        amended=normalize_sec_submissions(body,{**PARAMETERS,'form':'10-Q/A'},provider_status=200)
        self.assertEqual(amended['source_record_refs'],['sec:filing:0000000003-25-000003'])
        outside=normalize_sec_submissions(body,{**PARAMETERS,'date_from':'2025-10-01'},provider_status=200)
        self.assertEqual(outside['source_record_refs'],['sec:filing:0000000004-25-000004'])
        with self.assertRaisesRegex(SecPublicAdapterError,'exceeds the declared limit'):
            normalize_sec_submissions(body,{**PARAMETERS,'limit':2},provider_status=200)

    def test_missing_revision_does_not_relax_issuer_path_duplicate_or_malformed_revision(self):
        for kind in ('issuer','unsafe_path','duplicate','bad_revision','bad_revision_column'):
            with self.subTest(kind=kind):
                body=copy.deepcopy(payload());recent=body['filings']['recent']
                if kind=='issuer':body['cik']='0000007084'
                elif kind=='unsafe_path':recent['primaryDocument'][2]='../q2a.htm'
                elif kind=='duplicate':recent['accessionNumber'][0]=recent['accessionNumber'][2]
                elif kind=='bad_revision':recent['amendmentOf'][2]='unknown-original'
                else:recent['amendmentOf']=[None]
                with self.assertRaises(SecPublicAdapterError):normalize_sec_submissions(body,PARAMETERS,provider_status=200)

    def test_independent_amendment_hash_changes_with_original_document_not_inferred_parent(self):
        body=payload();del body['filings']['recent']['amendmentOf']
        first=normalize_sec_submissions(body,PARAMETERS,provider_status=200)
        body['filings']['recent']['primaryDocument'][2]='changed.htm'
        second=normalize_sec_submissions(body,PARAMETERS,provider_status=200)
        self.assertNotEqual(first['records'][1]['record_hash'],second['records'][1]['record_hash'])
        self.assertIsNone(second['records'][1]['revision_of_ref'])
