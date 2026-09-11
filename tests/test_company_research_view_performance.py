"""Bounded evidence projection: preserve bytes without an N+1 table scan."""

import json
import unittest

from dalton_core.company_research_view import query_company_research
from tests.test_company_research_view import ACN, CompanyResearchViewTests


class CompanyResearchEvidenceBatchTests(unittest.TestCase):
    def setUp(self):
        fixture=CompanyResearchViewTests(methodName="test_query_filters_and_validation")
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.store=fixture.store

    def test_evidence_metadata_is_equivalent_and_fetched_once(self):
        statements=[]
        self.store.connection.set_trace_callback(statements.append)
        try:
            rows=query_company_research(self.store,company_ref=ACN)
        finally:
            self.store.connection.set_trace_callback(None)
        evidence_queries=[sql for sql in statements
                          if "FROM evidence_relations r" in sql]
        self.assertEqual(1,len(evidence_queries))
        for row in rows:
            evidence=self.store.connection.execute(
                "SELECT e.evidence_json FROM evidence_relations r "
                "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
                "WHERE r.claim_version_id=?",(row["claim_version_ref"],)).fetchall()
            wires=[json.loads(item[0]) for item in evidence]
            expected_types=sorted({wire["source_type"] for wire in wires
                                   if isinstance(wire.get("source_type"),str)})
            retrieved=[wire["retrieved_at"] for wire in wires
                       if isinstance(wire.get("retrieved_at"),str)]
            self.assertEqual(expected_types,row["source_types"])
            self.assertEqual(max(retrieved) if retrieved else None,
                             row["latest_evidence_retrieved_at"])


if __name__ == "__main__":
    unittest.main()
