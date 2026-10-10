"""Number-entry proofs are stricter than prose enumeration, without losing precision."""
import copy
import unittest

from dalton_core.initial_screen import (
    InitialScreenContractError, build_worksheet_prompt, parse_document_output, validate_worksheet,
)
from dalton_core.mission_deliverable import (
    MissionDeliverableError, number_display, validate_pack_number,
    validate_passage_number, validate_derived_number, validate_section, value_tokens,
)
from tests.synthetic_company import material_pack, strl_document, strl_worksheet, COMPANY


class ExplicitNumbersTests(unittest.TestCase):
    def test_ten_thousand_unit_conversion_needs_a_declared_replayed_formula(self):
        source={'text':'$12500','number_ref':'pack-json:/amount','unit':'USD'}
        result={'text':'1.25万美元','derivation':{'expression':'12500/10000','result':'1.25'},'unit':'USDten-thousand'}
        validate_section({'title':'synthetic','body':'1.25万美元','numbers':[source,result]},resolve_number=lambda ref:'$12500')
        for expression in ['12500/9999','12500*1.37']:
            bad={**result,'derivation':{'expression':expression,'result':'1.25'}}
            with self.assertRaises(MissionDeliverableError):
                validate_section({'title':'synthetic','body':'','numbers':[source,bad]},resolve_number=lambda ref:'$12500')
        for body in ['收入1.25万美元','收入¥12500','客户10000家']:
            with self.assertRaises(MissionDeliverableError):
                validate_section({'title':'synthetic','body':body,'numbers':[source]},resolve_number=lambda ref:'$12500')
        with self.assertRaises(MissionDeliverableError):number_display('$12500','¥1.25')

    def test_body_equation_constants_require_a_replayed_derivation(self):
        entries=[{'text':'15.0','number_ref':'pack-json:/multiple'},
                 {'text':'12.7','number_ref':'pack-json:/base'},
                 {'text':'18.1%','derivation':{'expression':'(15.0/12.7-1)*100','result':'18.1'}}]
        sources={'pack-json:/multiple':'15.0','pack-json:/base':'12.7'}
        validate_section({'title':'synthetic','body':'倍数15；差异=(15/12.7−1)×100，为18.1%。','numbers':entries},resolve_number=sources.get)
        for body in ['客户100家','倍数15.04','差异=(15/12.8−1)×100，为18.1%','日期10月14日']:
            with self.subTest(body=body),self.assertRaises(MissionDeliverableError):
                validate_section({'title':'synthetic','body':body,'numbers':entries},resolve_number=sources.get)

    def test_small_measurement_is_checked_but_body_enumeration_stays_compatible(self):
        self.assertEqual(value_tokens('2 SD and 10 dollars'), [])
        self.assertEqual(value_tokens('2 SD and 10 dollars', include_small=True), ['2', '10'])
        item={'text':'2 SD','passage_ref':'doc-passage:synthetic'}
        self.assertEqual(validate_passage_number(item,lambda ref:'trades at 2 SD')['text'],'2 SD')
        with self.assertRaises(MissionDeliverableError):
            validate_passage_number(item,lambda ref:'trades at 3 SD')
        validate_derived_number({'text':'年化约10亿美元','derivation':{'expression':'100/10','result':'10'}},
                                [{'text':'100'}])

    def test_signed_rounding_source_and_precision_are_replayed(self):
        for raw,shown in [('15.90406','15.90'),('16.4232','16.4'),('-15.90406','-15.90'),('$15.905','$15.91')]:
            with self.subTest(raw=raw):
                display=number_display(raw,shown)
                out=validate_pack_number({'text':shown,'number_ref':'pack-json:/n','display':display},lambda ref:raw)
                self.assertEqual(out['source_text'],raw)
                self.assertEqual(out['display']['source_value'],raw)
                self.assertEqual(out['display']['display_value'],shown)
                with self.assertRaises(MissionDeliverableError):
                    validate_pack_number({'text':shown,'number_ref':'pack-json:/n','display':display},lambda ref:raw+'1')
        for raw,shown in [('15.90406','15.91'),('-15.90406','15.90'),('15.90406','$15.90'),
                          ('15.90406','15.90%'),('1590.406','15.90'),('15.90406','15.904')]:
            with self.subTest(raw=raw,shown=shown),self.assertRaises(MissionDeliverableError):
                number_display(raw,shown)

    def test_display_cannot_smuggle_metadata_or_an_extra_number(self):
        display=number_display('15.90406','15.90')
        bad=copy.deepcopy(display);bad['source_value']='15.90'
        for text,d in [('15.90',bad),('15.90 and 42',display)]:
            with self.assertRaises(MissionDeliverableError):
                validate_pack_number({'text':text,'number_ref':'pack-json:/n','display':d},lambda ref:'15.90406')
        with self.assertRaises(MissionDeliverableError):
            validate_section({'title':'synthetic','body':'','numbers':[{'text':'15.90','passage_ref':'doc-passage:x','display':display}]},
                             resolve_passage=lambda ref:'15.90406')

    def test_display_is_a_proven_operand_without_mutating_source(self):
        display=number_display('15.90406','15.90')
        section={'title':'synthetic','body':'3.20','numbers':[
            {'text':'15.90','number_ref':'pack-json:/n','display':display},
            {'text':'3.20','derivation':{'expression':'15.90-12.7','result':'3.20'}},
            {'text':'12.7','number_ref':'pack-json:/base'}]}
        out=validate_section(section,resolve_number={'pack-json:/n':'15.90406','pack-json:/base':'12.7'}.get)
        self.assertEqual(out['numbers'][0]['source_text'],'15.90406')
        section['numbers'][1]={'text':'3.20','derivation':{'expression':'15.90406-12.7','result':'3.20'}}
        validate_section(section,resolve_number={'pack-json:/n':'15.90406','pack-json:/base':'12.7'}.get)
        section['numbers'][1]['derivation']['expression']='15.91-12.7'
        with self.assertRaises(MissionDeliverableError):validate_section(section,resolve_number={'pack-json:/n':'15.90406','pack-json:/base':'12.7'}.get)

    def test_both_range_endpoints_must_be_independently_proven(self):
        entries=[{'text':'1%–3.5%','derivation':{'expression':'3-2','result':'1'}},
                 {'text':'1%–3.5%','derivation':{'expression':'6-2.5','result':'3.5'}}]
        source={'text':'2.5','number_ref':'pack-json:/base'}
        for order in (entries,list(reversed(entries))):
            validate_section({'title':'synthetic','body':'1%–3.5%','numbers':[source,*order]},resolve_number=lambda ref:'2.5')
        bad=copy.deepcopy(entries);bad[1]['derivation']['result']='4.5'
        with self.assertRaises(MissionDeliverableError):validate_section({'title':'synthetic','body':'','numbers':[source,*bad]},resolve_number=lambda ref:'2.5')
        # A peer's prose is never used to prove another arithmetic expression.
        bad=[{'text':'13.7 and 42.7','derivation':{'expression':'10+3.7','result':'13.7'}},
             {'text':'43.7','derivation':{'expression':'42.7+1','result':'43.7'}},
             {'text':'3.7','number_ref':'pack-json:/base'}]
        with self.assertRaises(MissionDeliverableError):validate_section({'title':'synthetic','body':'','numbers':bad},resolve_number=lambda ref:'3.7')
        cyclic=[{'text':'42.7','derivation':{'expression':'43.7-1','result':'42.7'}},
                {'text':'43.7','derivation':{'expression':'42.7+1','result':'43.7'}}]
        with self.assertRaises(MissionDeliverableError):validate_section({'title':'synthetic','body':'','numbers':cyclic})

    def test_document_figure_rounds_only_after_binding_the_pack_source(self):
        pack=material_pack();doc=strl_document()
        # Use a fresh precise scalar in the synthetic consensus EPS slot.
        from dalton_core.product_contract import normalise_pack, tag_entry
        wire=normalise_pack(pack)
        tag=next(t for t in wire['tags'] if tag_entry(wire,t)['kind']=='number' and not tag_entry(wire,t).get('cell'))
        entry=tag_entry(wire,tag)
        parts=entry['pointer'].strip('/').split('/');obj=wire['json']
        for part in parts[:-1]:obj=obj[int(part)] if isinstance(obj,list) else obj[part]
        if isinstance(obj,list):obj[int(parts[-1])]='15.90406'
        else:obj[parts[-1]]='15.90406'
        wire['json']['number_tags'][tag]['value']='15.90406';wire['json']['number_tags'][tag]['display']='15.90406'
        doc['sections'][0]['numbers']=[{'tag':tag,'figure':'15.90'}]
        out,find=parse_document_output(doc,pack=wire)
        self.assertEqual(find,[])
        self.assertEqual(out[0]['numbers'][0]['display']['source_value'],'15.90406')
        doc['sections'][0]['numbers'][0]['figure']='15.91'
        _,find=parse_document_output(doc,pack=wire)
        self.assertIn('number_display_invalid',[f['code'] for f in find])

    def test_worksheet_has_its_own_closed_phase_contract(self):
        pack=material_pack();prompt=build_worksheet_prompt(company={'company_ref':COMPANY,'ticker':'SYNW'},pack=pack)
        self.assertNotIn('每个数字在 numbers 数组',prompt)
        sheet=strl_worksheet();sheet['numbers']=[]
        with self.assertRaisesRegex(InitialScreenContractError,"extra=\\['numbers'\\]"):
            validate_worksheet(sheet,pack)
        del sheet['numbers'];del sheet['stance']
        with self.assertRaisesRegex(InitialScreenContractError,"missing=\\['stance'\\]"):
            validate_worksheet(sheet,pack)
