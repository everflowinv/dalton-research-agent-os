from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.document_research_feedback import read_document_research_feedback
from dalton_core.research_planner import build_prompt
from tests.test_research_planner import ACN, state


class DocumentFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.mission = {"id": "mission:1", "universe": [{"company_ref": ACN}]}
        self.core = SimpleNamespace(connection=object())

    def test_query_miss_changes_the_plan_state_without_any_new_claim(self):
        miss = {"id": "feedback:1", "content_hash": "a" * 64,
                "mission_version_ref": "mission:1", "company_ref": ACN,
                "outcome": "query_miss", "tried_query_terms": ["收入确认"],
                "question": "How is revenue recognized?", "document_authority_hash": "b" * 64,
                "meaning": "This query found no matching excerpt, not proof the text has no answer."}
        with patch("dalton_core.document_research_feedback._read_observations", return_value=[]):
            before = read_document_research_feedback(self.core, self.mission)
        with patch("dalton_core.document_research_feedback._read_observations", return_value=[miss]):
            after = read_document_research_feedback(self.core, self.mission)
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        prior_state = state(document_research_feedback=before)
        next_state = state(document_research_feedback=after)
        self.assertNotEqual(prior_state["content_hash"], next_state["content_hash"])
        company = next(c for c in next_state["companies"] if c["company_ref"] == ACN)
        self.assertEqual(company["document_research_feedback"][0]["tried_query_terms"], ["收入确认"])
        self.assertEqual(company["figures"], next(c for c in prior_state["companies"] if c["company_ref"] == ACN)["figures"])
        self.assertIn("not a finding that the source contains nothing", build_prompt(next_state))

    def test_foreign_or_unverifiable_feedback_is_disclosed_without_invented_observations(self):
        cases = [ValueError("formal proof drift"), [{"mission_version_ref": "foreign", "company_ref": ACN}]]
        for value in cases:
            kwargs = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            with self.subTest(value=value), patch("dalton_core.document_research_feedback._read_observations", **kwargs):
                feedback = read_document_research_feedback(self.core, self.mission)
            self.assertEqual(feedback["status"], "unavailable")
            self.assertEqual(feedback["by_company"], {})
