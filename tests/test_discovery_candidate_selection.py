import hashlib
import json
import unittest

from dalton_core.discovery_candidate_selection import (
    CandidateSelectionError, candidate_view, selection_prompt, validate_selection,
)
from dalton_core.store import canonical_json, content_hash


class DiscoveryCandidateSelectionTests(unittest.TestCase):
    def fixture(self):
        results = [
            {"doc_id": "other", "title": "International Foods quarterly call",
             "snippet": "Results for another issuer", "date": "2026-07-20",
             "document_type": "meeting_minutes", "company": "International Foods",
             "tags": ["quarterly"]},
            {"doc_id": "ibm-q2", "title": "IBM Q2 2026 Earnings Call Transcript",
             "snippet": "International Business Machines management discussion",
             "date": "2026-07-23", "document_type": "meeting_minutes",
             "company": "International Business Machines", "tags": ["IBM", "earnings"]},
        ]
        payload = {"results": results, "cursor": None, "has_more": False, "total": 2}
        raw = canonical_json({"jsonrpc": "2.0", "id": "archived", "result": {
            "content": [{"type": "text", "text": canonical_json(payload)}]}}).encode()
        base = {"id": "source-envelope:ibm", "content_hash": "a" * 64,
                "raw_response_hash": hashlib.sha256(raw).hexdigest(),
                "source_record_refs": ["alphaengine-doc:other", "alphaengine-doc:ibm-q2"]}
        return raw, base

    def test_archived_ibm_shape_projects_bounded_metadata_and_selects_only_ibm(self):
        raw, envelope = self.fixture()
        view = candidate_view(raw, envelope)
        self.assertEqual([x["document_ref"] for x in view["candidates"]],
                         ["alphaengine-doc:other", "alphaengine-doc:ibm-q2"])
        prompt = selection_prompt(view, company_ref="company:sec-cik:0000051143",
                                  missing_periods=["2026-Q2"])
        self.assertIn("Tags are hints, not authority", prompt)
        selected = validate_selection(json.dumps({"selected": [{
            "document_ref": "alphaengine-doc:ibm-q2", "reason": "Exact IBM Q2 call"
        }]}), view)
        self.assertEqual(["alphaengine-doc:ibm-q2"],
                         [x["document_ref"] for x in selected["selected"]])

    def test_raw_drift_unknown_ref_and_unclosed_output_refuse(self):
        raw, envelope = self.fixture()
        with self.assertRaises(CandidateSelectionError):
            candidate_view(raw + b" ", envelope)
        view = candidate_view(raw, envelope)
        for output in ({"selected": [{"document_ref": "alphaengine-doc:invented",
                                       "reason": "x"}]},
                       {"selected": [], "extra": True}):
            with self.assertRaises(CandidateSelectionError):
                validate_selection(json.dumps(output), view)

    def test_candidate_unknown_fields_do_not_enter_prompt(self):
        raw, envelope = self.fixture()
        rpc = json.loads(raw)
        payload = json.loads(rpc["result"]["content"][0]["text"])
        payload["results"][1]["private_provider_field"] = "secret"
        rpc["result"]["content"][0]["text"] = canonical_json(payload)
        raw = canonical_json(rpc).encode()
        envelope["raw_response_hash"] = hashlib.sha256(raw).hexdigest()
        view = candidate_view(raw, envelope)
        self.assertNotIn("private_provider_field", canonical_json(view))


if __name__ == "__main__": unittest.main()
