"""Repair identity fits the existing prior-body bound without losing requests."""
import copy
import json
import unittest
from dalton_core.research_followup_product_repair import MARKER, input_note, prior_body
from dalton_core.store import canonical_json, content_hash

SUFFIX='\nRepair only these held units from the supplied evidence. Date historical quantities explicitly; absent current evidence stays a gap.\n'

def items(count=3):
    result=[]
    for n in range(count):
        result.append({'group':{'group_ref':'followup-group:synthetic-framework-'+str(n)+'x'*36,
                                'content_hash':content_hash(['group',n])},
                       'source_ref':'industry-framework-version:synthetic-industry:'+str(n)+'x'*26,
                       'source_hash':content_hash(['source',n]),
                       'instructions':[{'ref':'research-followup:synthetic-request:'+str(n)+'x'*30,
                                        'hash':content_hash(['request',n]),'units':['causal_chain:0'],
                                        'reason':'Date the held historical quantity; do not invent current evidence.',
                                        'evidence':[{'ref':'source:'+str(n),'hash':content_hash(n)}]}]})
    return result

def old_note(data):
    compact=[{'group_ref':x['group']['group_ref'],'group_hash':x['group']['content_hash'],
              'source_ref':x['source_ref'],'source_hash':x['source_hash'],
              'instructions':[{'ref':i['ref'],'hash':i['hash'],'units':i['units'],'reason':i['reason'][:120]}
                              for i in x['instructions']]} for x in data]
    return MARKER+canonical_json(compact)+SUFFIX

class RepairNoteCompactionTests(unittest.TestCase):
    def test_existing_fitting_prompt_is_byte_identical(self):
        data=items(1)
        self.assertLess(len(old_note(data)),1800)
        self.assertEqual(input_note(data),old_note(data))
        self.assertEqual(prior_body('historical prose',data),old_note(data)+'historical prose')

    def test_three_exact_instructions_fit_without_raising_input_budget(self):
        data=items()
        self.assertGreater(len(old_note(data)),1800)
        note=input_note(data);self.assertLessEqual(len(note),1800)
        encoded=json.loads(note[len(MARKER):-len(SUFFIX)])
        self.assertEqual(encoded['context_hash'],content_hash(data))
        self.assertEqual(encoded['source_bindings'],[{'ref':x['source_ref'],'hash':x['source_hash']} for x in data])
        self.assertEqual(encoded['instructions'],[{'ref':i['ref'],'hash':i['hash'],'units':i['units'],'reason':i['reason'],'source_indices':[index]} for index,x in enumerate(data) for i in x['instructions']])
        bounded=prior_body('x'*8000,data)
        self.assertEqual(len(bounded),2000)
        self.assertTrue(bounded.startswith(note))

    def test_context_binding_detects_changes_outside_display_excerpt(self):
        data=items();note=input_note(data)
        for path in ['evidence','group']:
            changed=copy.deepcopy(data)
            if path=='evidence':changed[0]['instructions'][0]['evidence'][0]['hash']='f'*64
            else:changed[0]['group']['content_hash']='f'*64
            self.assertNotEqual(input_note(changed),note)
            self.assertFalse(prior_body('',data).startswith(input_note(changed)))

    def test_conflicting_request_identity_and_genuinely_oversized_context_refuse(self):
        data=items();data[1]['instructions'][0]['ref']=data[0]['instructions'][0]['ref']
        with self.assertRaisesRegex(ValueError,'conflicting prompt identities'):input_note(data)
        with self.assertRaisesRegex(ValueError,'existing bounded prior input'):input_note(items(20))
