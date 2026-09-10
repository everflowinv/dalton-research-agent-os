from __future__ import annotations

import unittest

from dalton_core.bounded_alphaengine_search_probe import (
    BoundedAlphaEngineSearchProbeError,
    BoundedAlphaEngineSearchProbePending,
    execute_alphaengine_search_probe,
)


def work(parameters=None):
    return {
        "id": "work:bounded-alpha-search:test",
        "metadata": {
            "operation": "alphaengine_discovery_refresh",
            "permission_scope": "alphaengine_read",
            "mission_version_ref": "coverage-mission-version:test:1",
            "mission_version_hash": "a" * 64,
            "parameters": parameters or {
                "source_ref": "source:alphaengine",
                "spec_ref": "earnings-call-transcripts",
                "inquiry_hash": "b" * 64,
                "company_ref": "company:sec-cik:0001467373",
                "discovery_plan_ref": "discovery-plan:us-it-services:alphaengine:1",
                "discovery_plan_hash": "c463a3dac1daf95a41c8697eeec7edd3b3da90ccb0265bb800edeb6b7be47d92",
            },
        },
    }


class Client:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.starts = 0

    def call(self, operation, params):
        if operation == "start_bounded_source_discovery":
            self.starts += 1
            return {"id": "alphaengine-search-ticket:" + "1" * 24, "status": "running"}
        assert operation == "mission_source_discovery_status"
        return self.statuses.pop(0)


class SearchProbeTests(unittest.TestCase):
    def test_formal_source_success_carries_actual_invocation_authority(self):
        client = Client([{"status": "succeeded", "summary": {
            "status": "succeeded", "query_hash": "c" * 64, "provider_calls": 1,
            "search": {"outcome": "succeeded", "document_refs": ["document:a"],
                       "connector_invocation_ref": "connector-invocation:a",
                       "connector_invocation_hash": "d" * 64,
                       "source_envelope_ref": "source-envelope:a",
                       "source_envelope_hash": "e" * 64,
                       "raw_artifact_version_ref": "artifact:a"},
        }}])
        result = execute_alphaengine_search_probe(work(), client=client, poll_seconds=0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["invocation_ref"], "connector-invocation:a")
        self.assertEqual(result["metadata"]["provider_calls"], 1)
        self.assertEqual(client.starts, 1)

    def test_exit_zero_without_formal_source_success_is_failed(self):
        client = Client([{"status": "succeeded", "summary": {
            "status": "succeeded", "search": {"outcome": "succeeded"}}}])
        result = execute_alphaengine_search_probe(work(), client=client, poll_seconds=0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "SOURCE_UNAVAILABLE")

    def test_deadline_is_a_pending_child_not_a_free_failed_probe(self):
        client = Client([])
        with self.assertRaises(BoundedAlphaEngineSearchProbePending):
            execute_alphaengine_search_probe(work(), client=client, timeout_seconds=0)
        self.assertEqual(client.starts, 1)

    def test_cross_purpose_parameters_are_closed(self):
        parameters = dict(work()["metadata"]["parameters"])
        parameters["query"] = "invented"
        with self.assertRaises(BoundedAlphaEngineSearchProbeError):
            execute_alphaengine_search_probe(work(parameters), client=Client([]))


if __name__ == "__main__":
    unittest.main()
