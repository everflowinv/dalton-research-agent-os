"""Native columns certify units/windows, never a metric's economic meaning."""
import copy
import hashlib
import unittest
from dalton_core.store import content_hash
from dalton_core.public_web_extraction_source import render_public_web_text
from dalton_core.operating_source_columns import table_measurements
from dalton_core.operating_source_measurements import native_measurements, matching_measurements, RULE_REF
from dalton_core.document_numeric_claim import verify_numeric_candidate
from dalton_core.operating_driver_units import CONTRACT_REF

PERIOD='2026-04-01/2026-06-30'

def original(*, currencies=('USD',), fiscal='--12-31', caption='Dollars per ton', body=None):
    units=''.join(f'<xbrli:unit id="u{i}"><xbrli:measure>iso4217:{currency}</xbrli:measure></xbrli:unit>' for i,currency in enumerate(currencies))
    rows=body or '''<table><tr><td colspan="5">Dollars per ton</td></tr>
<tr><td></td><td colspan="3">Three months ended June 30,</td><td>Six months ended June 30,</td></tr>
<tr><td></td><td>2026</td><td>2025</td><td>2026 vs 2025</td><td>2026</td></tr>
<tr><td>Units sold (tons) (000s)</td><td>40</td><td>38</td><td>2</td><td>79</td></tr>
<tr><td>Average selling price per ton</td><td>12</td><td>11</td><td>1</td><td>11.5</td></tr></table>'''.replace('Dollars per ton',caption)
    raw=f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:dei="http://xbrl.sec.gov/dei/2026">
<body><ix:header>{units}</ix:header><ix:nonNumeric name="dei:CurrentFiscalYearEndDate">{fiscal}</ix:nonNumeric>{rows}</body></html>'''.encode()
    return raw,render_public_web_text(raw,raw_media_type='application/xhtml+xml')['text']

def proof_for(raw,text,*,terms=('Units sold','Average selling price'),period=PERIOD):
    sha=hashlib.sha256(text.encode()).hexdigest()
    quote={'source_start':0,'source_end':len(text),'excerpt':text,'excerpt_sha256':sha}
    source=native_measurements(raw,original_text=text,matches=[quote],periods=[period],query_terms=list(terms))
    body={'rule_ref':RULE_REF,'registration_ref':'registered:synthetic','registration_hash':'a'*64,
          'manifest_hash':'b'*64,'search_proof_hash':'c'*64,'sources':[source]}
    return {**body,'content_hash':content_hash(body)}

def candidate(row):
    return {'quote_id':row['quote_id'],'metric_ref':'metric:operating-test','subject_as_named':'Synthetic issuer',
            'as_reported_label':row.get('native_label','Selling price'),'value':row['value'],
            'period':row['period'],'unit':row['unit'],'currency':row['currency'],'scale':row['scale'],'basis':'management-reported'}

class OperatingColumnProofTests(unittest.TestCase):
    def test_accounting_sign_whole_and_split_cells_keep_the_complete_amount_span(self):
        for cells, expected, count in [
                ('<td colspan="3">(12)</td>', '-12', 1),
                ('<td>(</td><td>12</td><td>)</td>', '-12', 3),
                ('<td>$</td><td colspan="2">12</td>', '12', 2),
                ('<td colspan="3">-12</td>', '-12', 1),
                ('<td>-</td><td colspan="2">12</td>', '-12', 2),
                ('<td colspan="3">12</td>', '12', 1)]:
            table = '<table><tr><td colspan="4">Dollars per ton</td></tr>'\
                '<tr><td></td><td colspan="3">Three months ended June 30,</td></tr>'\
                '<tr><td></td><td colspan="3">2026</td></tr>'\
                '<tr><td>Average selling price per ton</td>' + cells + '</tr></table>'
            raw, text = original(body=table)
            rows = matching_measurements(proof_for(raw, text), period=PERIOD, terms=['Average selling price'])
            with self.subTest(cells=cells):
                self.assertEqual([r['value'] for r in rows], [expected])
                self.assertEqual(len(rows[0]['amount_cells']), count)
                self.assertEqual((rows[0]['column_start'], rows[0]['column_end']), (1, 4))
                self.assertTrue(all(c['cell_hash'] for c in rows[0]['amount_cells']))
                checked = verify_numeric_candidate(candidate(rows[0]), {rows[0]['quote_id']: text},
                    numeric_contract_ref=CONTRACT_REF, operating_source_proof=proof_for(raw, text))
                self.assertEqual(checked['value'], expected)

    def test_double_malformed_or_cross_period_signs_cannot_expose_a_positive_core(self):
        for cells in ('<td colspan="3">(-12)</td>', '<td colspan="3">(+12)</td>',
                '<td>(</td><td>-12</td><td>)</td>', '<td>(</td><td>12</td><td></td>',
                '<td></td><td>12</td><td>)</td>', '<td>(</td><td>12</td><td>))</td>',
                '<td>(</td><td>+12</td><td>)</td>', '<td>((</td><td>12</td><td></td>',
                '<td>)</td><td colspan="2">12</td>'):
            table = '<table><tr><td colspan="4">Dollars per ton</td></tr>'\
                '<tr><td></td><td colspan="3">Three months ended June 30,</td></tr>'\
                '<tr><td></td><td colspan="3">2026</td></tr>'\
                '<tr><td>Average selling price per ton</td>' + cells + '</tr></table>'
            raw, text = original(body=table)
            with self.subTest(cells=cells):
                self.assertEqual(matching_measurements(proof_for(raw, text), period=PERIOD,
                    terms=['Average selling price']), [])
        table = '<table><tr><td colspan="4">Dollars per ton</td></tr>'\
            '<tr><td></td><td colspan="3">Three months ended June 30,</td></tr>'\
            '<tr><td></td><td>2025</td><td colspan="2">2026</td></tr>'\
            '<tr><td>Average selling price per ton</td><td>(</td><td>12</td><td>)</td></tr></table>'
        raw, text = original(body=table)
        self.assertEqual(matching_measurements(proof_for(raw, text), period=PERIOD,
            terms=['Average selling price']), [])

    def test_shared_header_current_comparative_variance_and_cumulative_are_distinct(self):
        raw,text=original();cols=table_measurements(raw,original_text=text)
        self.assertEqual({r['value'] for r in cols['measurements']},{'40','38','79','12','11','11.5'})
        proof=proof_for(raw,text)
        rows=matching_measurements(proof,period=PERIOD,terms=['Units sold','Average selling price'])
        self.assertEqual({r['value'] for r in rows},{'40','12'})
        self.assertEqual({r['scale'] for r in rows if r['unit']=='tons'},{'thousand'})
        for row in rows:
            checked=verify_numeric_candidate(candidate(row),{row['quote_id']:text},numeric_contract_ref=CONTRACT_REF,operating_source_proof=proof)
            self.assertEqual(checked['value'],row['value'])
        row=next(r for r in rows if r['unit']=='tons')
        for field,value in [('value','38'),('value','2'),('value','79'),('scale',None),('unit','currency_per_mmbtu'),('period','2026-01-01/2026-06-30')]:
            changed=candidate(row);changed[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                verify_numeric_candidate(changed,{row['quote_id']:text},numeric_contract_ref=CONTRACT_REF,operating_source_proof=proof)

    def test_prior_gas_caption_cannot_relabel_explicit_later_tonnage(self):
        raw,text=original(body='''<table><tr><td colspan="2">Dollars</td></tr><tr><td></td><td>Three months ended June 30,</td></tr>
<tr><td></td><td>2026</td></tr><tr><td>Natural gas costs per MMBtu</td></tr><tr><td>Used for production</td><td>3</td></tr>
<tr><td>Units sold (tons) (000s)</td><td>40</td></tr></table>''')
        proof=proof_for(raw,text,terms=['Units sold'])
        rows=matching_measurements(proof,period=PERIOD,terms=['Units sold'])
        self.assertEqual([(r['unit'],r['currency'],r['scale']) for r in rows],[('tons',None,'thousand')])

    def test_same_original_namespace_currency_required_and_never_unit_id_guess(self):
        for currencies,caption in [((),'Dollars per ton'),(('USD','CAD'),'Dollars per ton'),(('USD',),'Canadian dollars per ton'),(('USD',),'Prices per ton')]:
            raw,text=original(currencies=currencies,caption=caption)
            proof=proof_for(raw,text)
            rows=matching_measurements(proof,period=PERIOD,terms=['Average selling price'])
            with self.subTest(currencies=currencies,caption=caption):self.assertEqual(rows,[])
        raw,text=original();proof=proof_for(raw,text)
        changed=copy.deepcopy(proof);money=changed['sources'][0]['report_currency'];money['raw_sha256']='d'*64
        money['content_hash']=content_hash({k:v for k,v in money.items() if k!='content_hash'})
        for value in [changed['sources'][0],changed]:value['content_hash']=content_hash({k:v for k,v in value.items() if k!='content_hash'})
        with self.assertRaises(ValueError):matching_measurements(changed,period=PERIOD,terms=['Average selling price'])

    def test_per_unit_cannot_silently_discard_shared_millions(self):
        for caption,allowed in [('Dollars in millions',False),('Dollars in millions except per ton data',True)]:
            raw,text=original(caption=caption);proof=proof_for(raw,text)
            rows=matching_measurements(proof,period=PERIOD,terms=['Average selling price'])
            self.assertEqual(bool(rows),allowed)

    def test_narrative_needs_calendar_fiscal_authority_exact_clause_and_currency(self):
        body='<p>Average selling price was $12 per ton in the second quarter of 2026 compared to $11 per ton in the second quarter of 2025.</p>'
        raw,text=original(body=body);proof=proof_for(raw,text,terms=['selling price'])
        rows=matching_measurements(proof,period=PERIOD,terms=['selling price'])
        self.assertEqual([r['value'] for r in rows],['12'])
        row=rows[0];fact=candidate(row);fact['as_reported_label']='Average selling price'
        self.assertEqual(verify_numeric_candidate(fact,{row['quote_id']:text},numeric_contract_ref=CONTRACT_REF,operating_source_proof=proof)['value'],'12')
        for fiscal,currencies in [('--06-30',('USD',)),('--12-31',()),('--12-31',('USD','EUR'))]:
            raw,text=original(body=body,fiscal=fiscal,currencies=currencies)
            self.assertEqual(matching_measurements(proof_for(raw,text,terms=['selling price']),period=PERIOD,terms=['selling price']),[])

    def test_source_wide_cell_expansion_is_bounded_not_just_each_table(self):
        body='<table><tr><td colspan="255" rowspan="390">Bounded layout</td></tr></table>'*6
        raw,text=original(body=body)
        with self.assertRaisesRegex(ValueError,'total occupied'):table_measurements(raw,original_text=text)

    def test_invalid_span_and_foreign_rendering_cannot_make_source_proof(self):
        raw,text=original(body='<table><tr><td colspan="0">Units sold</td></tr></table>')
        with self.assertRaisesRegex(ValueError,'span'):table_measurements(raw,original_text=text)
        raw,text=original();cols=table_measurements(raw,original_text=text+'changed')
        self.assertNotEqual(cols['text_sha256'],table_measurements(raw,original_text=text)['text_sha256'])
        self.assertEqual(table_measurements(raw,original_text='Foreign source table text')['measurements'],[])

if __name__=='__main__':unittest.main()
