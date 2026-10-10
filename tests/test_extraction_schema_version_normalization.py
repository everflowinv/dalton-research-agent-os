"""Only the exact known live version typo is normalized; raw proof stays raw."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from dalton_core.document_extraction import parse_suggestions
from dalton_core.research_verification import ResearchVerificationError
from dalton_core.store import content_hash
from tests import test_document_extraction as fixture


class ExtractionVersionNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.h=fixture.ExtractionHarness(Path(self.tmp.name));self.addCleanup(self.h.close)
        self.h.enable_fixture();self.context=self.h.context()
        self.good=json.loads(self.h.adapter.output_text)

    def test_only_tolerant_exact_numeric_version_is_accepted(self):
        wire={**self.good,'schema_version':0.1};text=json.dumps(wire)
        with self.assertRaisesRegex(ResearchVerificationError,'closed shape'):
            parse_suggestions(text,self.context)
        normalized=parse_suggestions(text,self.context,tolerant=True)
        self.assertEqual(normalized['schema_version'],'0.1')
        self.assertEqual(normalized,parse_suggestions(json.dumps(self.good),self.context,tolerant=True))
        self.assertEqual(json.loads(text),wire)

    def test_wrong_numbers_booleans_null_and_unsupported_strings_refused(self):
        for version in (0.2,0,1,True,False,None,'0.2','0.10','0.1 '):
            with self.subTest(version=version),self.assertRaises(ResearchVerificationError):
                parse_suggestions(json.dumps({**self.good,'schema_version':version}),self.context,tolerant=True)

    def test_envelope_closed_shape_duplicate_keys_and_item_guards_remain(self):
        for wire in ({**self.good,'schema_version':0.1,'actor_ref':'human:owner'},
                     {'schema_version':0.1,'suggestions':{}},
                     {'schema_version':0.1,'suggestions':self.good['suggestions']*6}):
            with self.subTest(wire=wire),self.assertRaises(ResearchVerificationError):
                parse_suggestions(json.dumps(wire),self.context,tolerant=True)
        with self.assertRaisesRegex(ResearchVerificationError,'strict JSON'):
            parse_suggestions('{"schema_version":0.1,"schema_version":0.1,"suggestions":[]}',self.context,tolerant=True)
        for changes in ({'quote_id':'quote:foreign'},{'value':'3'},
                        {'normalized_statement':'Revenue rose 30%'}):
            wire=copy.deepcopy(self.good);wire['schema_version']=0.1;wire['suggestions'][0].update(changes)
            result=parse_suggestions(json.dumps(wire),self.context,tolerant=True)
            self.assertEqual(result['suggestions'],[])
            self.assertEqual(len(result['dropped']),1)

    def test_real_worker_preserves_raw_numeric_version_and_replays_without_model_or_claim(self):
        wire={**self.good,'schema_version':0.1}
        self.h.enable_fixture(wire)
        before=self.h.counts();first=self.h.generate()
        self.assertEqual(first['status'],'succeeded',first)
        self.assertEqual(len(first['suggestions']),1)
        self.assertEqual(self.h.counts(),before)
        saved=list(self.h.adapter.saved.values())
        self.assertEqual(len(saved),1)
        invocation,result=saved[0]
        self.assertIs(type(json.loads(result.outputs['text'])['schema_version']),float)
        self.assertEqual(json.loads(result.outputs['text'])['schema_version'],0.1)
        old_inv=content_hash(invocation.to_dict());old_result=content_hash(result.to_dict())
        self.assertEqual(self.h.generate(),first)
        self.assertEqual(self.h.adapter.calls,1)
        replay=list(self.h.adapter.saved.values())[0]
        self.assertEqual(content_hash(replay[0].to_dict()),old_inv)
        self.assertEqual(content_hash(replay[1].to_dict()),old_result)

if __name__=='__main__':unittest.main()
