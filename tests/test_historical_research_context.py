import copy
import json
import unittest
from dalton_core.historical_research_context import historical_findings, reading_projection
from dalton_core.company_dossier_draft import parse_unit_output, DossierDraftRefused, build_unit_prompt, legacy_unit_prompt_v10

class HistoricalContextTests(unittest.TestCase):
    def test_history_is_an_independent_source_bound_view_and_unknown_is_not_current(self):
        record={'id':'dossier:synthetic:1','content_hash':'a'*64,'sections':[{'aspect':'supply_and_cost',
            'sources':[{'ref':'old','period':'FY2024','text':'Revenue 10 USD'},{'ref':'undated','period':None}],
            'slots':[{'slot_id':'cost','sentences':[{'text':'收入10美元。','refs':['old']},{'text':'利润2美元。','refs':['undated']}]}]}]}
        original=copy.deepcopy(record)
        projected=reading_projection(record,latest_disclosure={'report_date':'2026-06-30','content_hash':'b'*64})
        self.assertEqual(projected['paragraphs'][0]['label'],'历史对照：FY2024')
        self.assertEqual(projected['paragraphs'][1]['state'],'period_unknown')
        self.assertEqual(record,original)

    def test_new_draft_rejects_undated_use_of_an_explicit_older_number_and_legacy_prompt_keeps_old_shape(self):
        material=[{'tag':'N1','kind':'figure','ref':'old','text':'10 USD','period':'FY2024'},
                  {'tag':'N2','kind':'figure','ref':'new','text':'11 USD','period':'FY2026'}]
        structure=[{'slot_id':'cost','prompt':'What drives cost?'}]
        reply={'slots':[{'slot_id':'cost','sentences':[{'text':'收入10美元。','refs':['N1']}]}],'gaps':[]}
        with self.assertRaisesRegex(DossierDraftRefused,'historical_number_without_period'):
            parse_unit_output(json.dumps(reply),unit='business_model',structure=structure,material=material)
        reply['slots'][0]['sentences'][0]['text']='历史对照：FY2024收入10美元。'
        accepted=parse_unit_output(json.dumps(reply),unit='business_model',structure=structure,material=material)
        self.assertEqual(accepted['slots'][0]['sentences'][0]['refs'],['old'])
        args={'unit':'business_model','structure':structure,'material':material,'company':{'ticker':'SYN','company_ref':'company:synthetic'}}
        self.assertIn('Label older quantitative evidence',build_unit_prompt(**args))
        self.assertNotIn('Label older quantitative evidence',legacy_unit_prompt_v10(**args))
