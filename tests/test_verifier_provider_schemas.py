import json
import unittest
from importlib import resources

from dalton_core.company_dossier_draft import VERIFIER_FINDING_CODES as DOSSIER_CODES, validate_verifier_output as validate_dossier
from dalton_core.deep_insight_gate_draft import VERIFIER_FINDING_CODES as GATE_CODES, validate_verifier_output as validate_gate


class VerifierProviderSchemaTests(unittest.TestCase):
    def schema(self, name):
        return json.loads(resources.files("dalton_core").joinpath(name).read_text("utf-8"))

    def test_dossier_and_industry_golden_payload_matches_semantic_validator(self):
        schema = self.schema("dossier-verifier-provider-output-v0.1.schema.json")
        payload = {"verdict": "reject", "findings": [{"unit": "economics",
                   "code": DOSSIER_CODES[0], "detail": "The row does not support the sentence."}]}
        self.assertEqual(validate_dossier(payload), payload)
        item = schema["properties"]["findings"]["items"]
        self.assertEqual(set(item["required"]), set(payload["findings"][0]))
        self.assertEqual(set(item["properties"]["code"]["enum"]), set(DOSSIER_CODES))
        self.assertFalse(schema["additionalProperties"])

    def test_deep_insight_golden_payload_matches_semantic_validator(self):
        schema = self.schema("deep-insight-gate-verifier-provider-output-v0.1.schema.json")
        payload = {"verdict": "reject", "findings": [{"question_ref": "q1",
                   "code": GATE_CODES[0], "detail": "The cited row does not support this answer."}]}
        self.assertEqual(validate_gate(payload), payload)
        item = schema["properties"]["findings"]["items"]
        self.assertEqual(set(item["required"]), set(payload["findings"][0]))
        self.assertEqual(set(item["properties"]["code"]["enum"]), set(GATE_CODES))
