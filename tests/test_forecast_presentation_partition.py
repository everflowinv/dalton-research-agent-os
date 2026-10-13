import copy
import unittest
from dalton_core.forecast_presentation_partition import _partition
from dalton_core.sec_filing_units import FilingUnitError
from dalton_core.store import content_hash
from unittest.mock import patch
import hashlib
from dalton_core.forecast_presentation_partition import presentation_partition
from dalton_core.company_model_forecast import model_digest
from tests import test_mission_model_forecast_lane as lane_fixture
from tests import test_company_financial_statement_structure as structure_fixture
from tests import test_financial_structure_forecast_consumer as consumer_fixture


class PresentationPartitionTests(unittest.TestCase):
    def fixture(self, *, issuer='42', prefix='issuer'):
        ns='http://example.test/'+prefix
        head=f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:iso="http://www.xbrl.org/2003/iso4217" xmlns:gaap="http://example.test/gaap" xmlns:{prefix}="{ns}"><body>'
        contexts=[];tables=[];rows=[]
        for group,members in [('details',['East','West']),('summary',['Product','Service'])]:
            cells=[]
            for n,member in enumerate(members):
                cref=group+member
                contexts.append(f'<xbrli:context id="{cref}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{issuer}</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="gaap:RevenueAxis">{prefix}:{member}</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-10-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>')
                cells.append(f'<td><ix:nonFraction name="gaap:Revenue" contextRef="{cref}" unitRef="dollars">{40+20*n}</ix:nonFraction></td>')
                rows.append({'line_id':cref,'concept':'gaap:Revenue','dimensions':{'gaap:RevenueAxis':prefix+':'+member},'period_start':'2025-10-01','period_end':'2025-12-31','unit':'USD','value':str(40+20*n)})
            tables.append('<table><tr>'+''.join(cells)+'</tr></table>')
        raw=(head+''.join(contexts)+'<xbrli:unit id="dollars"><xbrli:measure>iso:USD</xbrli:measure></xbrli:unit>'+''.join(tables)+'</body></html>').encode()
        return raw,{'cik':issuer},rows,{prefix+':East',prefix+':West'}

    def check(self, raw, filing, rows, expected):
        return _partition(raw,filing,rows,expected,concept='gaap:Revenue',axis='gaap:RevenueAxis',fixed={})

    def test_multiple_issuers_prove_complete_tables_without_amount_partition_selection(self):
        for issuer,prefix in [('42','issuer'),('43','other')]:
            with self.subTest(issuer=issuer):
                raw,f,rows,members=self.fixture(issuer=issuer,prefix=prefix)
                proof=self.check(raw,f,rows,members)
                self.assertEqual(len(proof['other_table_hashes']),1)
                self.assertNotEqual(proof['selected_table_hash'],proof['other_table_hashes'][0])
                self.assertEqual(len(proof['current_row_bindings']),4)
                self.assertEqual(proof['content_hash'],content_hash({k:v for k,v in proof.items()if k!='content_hash'}))

    def test_same_table_hidden_member_or_mixed_partition_cannot_be_filtered_out(self):
        raw,f,rows,members=self.fixture()
        # All four equal-total members in one table do not become an allowed
        # partition just because the model named two of them.
        raw=raw.replace(b'</tr></table><table><tr>',b'')
        with self.assertRaisesRegex(FilingUnitError,'complete source table'):
            self.check(raw,f,rows,members)

    def test_ambiguous_duplicate_different_tables_refused(self):
        raw,f,rows,members=self.fixture()
        table=raw.split(b'<table>',1)[1].split(b'</table>',1)[0]
        raw=raw.replace(b'</body>',b'<table id="other">'+table+b'</table></body>')
        with self.assertRaisesRegex(FilingUnitError,'one complete'):
            self.check(raw,f,rows,members)

    def test_wrong_issuer_unit_value_period_or_fixed_dimension_refused(self):
        raw,f,rows,members=self.fixture()
        for field,value in [('unit','EUR'),('value','41'),('period_start','2025-09-01'),('dimensions',{'gaap:RevenueAxis':'issuer:East','gaap:ExtraAxis':'issuer:East'})]:
            with self.subTest(field=field):
                altered=copy.deepcopy(rows);altered[0][field]=value
                with self.assertRaises(FilingUnitError):self.check(raw,f,altered,members)
        with self.assertRaises(FilingUnitError):self.check(raw,{'cik':'999'},rows,members)

    def test_missing_other_member_original_proof_refused(self):
        raw,f,rows,members=self.fixture()
        raw=raw.replace(b'contextRef="summaryProduct"',b'contextRef="unheld"')
        with self.assertRaisesRegex(FilingUnitError,'unbound target fact context'):
            self.check(raw,f,rows,members)

    def test_partial_period_membership_or_unsupported_fact_refused(self):
        raw,f,rows,members=self.fixture()
        raw=raw.replace(b'<ix:nonFraction name="gaap:Revenue" contextRef="detailsWest" unitRef="dollars">60',b'<ix:nonFraction name="gaap:Revenue" contextRef="detailsWest" unitRef="dollars" format="gaap:unsupported">60')
        with self.assertRaisesRegex(FilingUnitError,'unsupported numeric'):
            self.check(raw,f,rows,members)
        raw,f,rows,members=self.fixture()
        raw=raw.replace(b'<xbrli:unit id="dollars">',b'<xbrli:unit id="euro"><xbrli:measure>iso:EUR</xbrli:measure></xbrli:unit><xbrli:unit id="dollars">').replace(b'contextRef="detailsWest" unitRef="dollars"',b'contextRef="detailsWest" unitRef="euro"')
        with self.assertRaisesRegex(FilingUnitError,'incomplete period/unit'):
            self.check(raw,f,rows,members)
    def test_hidden_unsupported_context_unknown_unit_or_missing_context_never_disappears(self):
        for failure in ['typed', 'unit', 'context']:
            with self.subTest(failure=failure):
                raw,f,rows,members=self.fixture()
                start=raw.index(b'<xbrli:context id="detailsEast">');end=raw.index(b'</xbrli:context>',start)+len(b'</xbrli:context>')
                context=raw[start:end].replace(b'detailsEast',b'hidden').replace(b'issuer:East',b'issuer:Hidden')
                if failure=='typed':
                    context=context.replace(b'</xbrli:segment>',b'<xbrldi:typedMember dimension="gaap:UnknownAxis"><gaap:Typed>hidden</gaap:Typed></xbrldi:typedMember></xbrli:segment>')
                raw=raw.replace(b'<xbrli:unit id="dollars">',context+b'<xbrli:unit id="dollars">')
                cref='unbound' if failure=='context' else 'hidden'
                unit='unknown' if failure=='unit' else 'dollars'
                fact=f'<td><ix:nonFraction name="gaap:Revenue" contextRef="{cref}" unitRef="{unit}">0</ix:nonFraction></td>'.encode()
                raw=raw.replace(b'</tr>',fact+b'</tr>',1)
                with self.assertRaises(FilingUnitError):self.check(raw,f,rows,members)

    def test_replay_pins_verified_original_after_new_source_and_rejects_tamper_or_fake_receipt(self):
        raw,f,rows,members=self.fixture()
        old={'record_id':'source:old','manifest':{'body_sha256':hashlib.sha256(raw).hexdigest()}}
        new={'record_id':'source:new','manifest':{'body_sha256':hashlib.sha256(raw).hexdigest()}}
        class Connection:
            def execute(self,sql):return [(0,'main','/synthetic/state/core.sqlite')]
        current=[old];objects={'source:old':raw,'source:new':raw}
        def source_reader(connection,state,filing,*,source=None):
            selected=source or current[0]
            if selected not in [old,new]:raise FilingUnitError('source receipt binding drifted')
            return selected,objects[selected['record_id']]
        def read(source=None):
            return presentation_partition(Connection(),f,rows,members,concept='gaap:Revenue',axis='gaap:RevenueAxis',fixed={},source=source)
        with patch('dalton_core.forecast_presentation_partition._source',side_effect=source_reader):
            published=read();current[0]=new
            self.assertNotEqual(read()['content_hash'],published['content_hash'])
            self.assertEqual(read(published['source']),published)
            fake=copy.deepcopy(published['source']);fake['manifest']['body_sha256']='0'*64
            with self.assertRaisesRegex(FilingUnitError,'binding drifted'):read(fake)
            objects['source:old']=raw.replace(b'>40</ix:nonFraction>',b'>41</ix:nonFraction>',1)
            with self.assertRaisesRegex(FilingUnitError,'original bytes differ'):read(published['source'])

    def test_new_rule_naturally_recomputes_once_and_keeps_original_failed_ticket(self):
        fixture=lane_fixture.LaneTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        table=consumer_fixture.financial_inputs();table['company_ref']=lane_fixture.ACN
        spec=structure_fixture.company_spec();spec['company_ref']=lane_fixture.ACN;spec['decided_by']='automation:test'
        spec['financial_statement_structure']=consumer_fixture.forecastable_proposal(table)
        before=copy.deepcopy(spec)
        def old_hash(body):
            if isinstance(body,dict) and 'filed_presentation_partition_rule' in body:
                body={k:v for k,v in body.items()if k!='filed_presentation_partition_rule'}
            return content_hash(body)
        with patch('dalton_core.mission_model_forecast_lane.pending_companies',return_value=[(lane_fixture.ACN,spec,table)]), patch('dalton_core.mission_model_forecast_lane.forecast_reporting_boundary',return_value=None):
            with patch('dalton_core.company_model_forecast.content_hash',side_effect=old_hash):
                old_digest=model_digest(spec,table);first=fixture.lane.dispatch_once()
            fixture.launcher.finish(first['ticket_ref'],status='failed',summary={'failure_reason':'ForecastModelValidationError: segment scope must declare every current filed member'})
            old_ticket=copy.deepcopy(fixture.launcher.tickets[first['ticket_ref']])
            new=fixture.lane.dispatch_once();self.assertEqual(new['status'],'launched')
            self.assertNotEqual(new['model_digest'],old_digest)
            self.assertEqual(fixture.launcher.tickets[first['ticket_ref']],old_ticket)
            fixture.launcher.finish(new['ticket_ref'],status='failed',summary={'forecast_status':'unavailable:economic_invariants','failure_reason':'segment_sum: genuine current source refusal'})
            self.assertEqual(fixture.lane.dispatch_once()['status'],'held')
            self.assertEqual(len(fixture.launcher.started),2)
        self.assertEqual(spec,before)



if __name__=='__main__':unittest.main()
