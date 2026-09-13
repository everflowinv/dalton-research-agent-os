from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.research_publication_worker import poll_once, run_periodic


MISSION = {"id": "mission-version:1", "industry_ref": "industry:test",
           "universe": [{"company_ref": "company:a"}, {"company_ref": "company:b"}]}


def reader(connection, mission, company):
    products = [{"kind": "dossier", "subject_ref": company,
                 "version_ref": f"dossier-version:{company[-1]}",
                 "status": "available", "sections": [{"title": "结论", "body": company,
                                                          "gaps": []}]}]
    if company == "company:a":
        products.append({"kind": "industry_framework", "subject_ref": "industry:test",
                         "version_ref": "framework-version:1", "status": "available",
                         "sections": []})
    return {"products": products}


class ResearchPublicationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_completed_hash_is_not_prepared_again_and_new_hash_is(self):
        calls = []
        prepare = lambda product: (calls.append(product["version_ref"]) or {"status": "completed"})
        first = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                          library_reader=reader)
        second = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                           library_reader=reader)
        self.assertEqual(first["completed"], 3)
        self.assertEqual(second["unchanged"], 3)
        self.assertEqual(len(calls), 3)

        def changed_reader(connection, mission, company):
            value = reader(connection, mission, company)
            if company == "company:a": value["products"][0]["sections"][0]["body"] += " changed"
            return value
        third = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                          library_reader=changed_reader)
        self.assertEqual(third["completed"], 1)
        self.assertEqual(len(calls), 4)

    def test_failure_is_pending_and_does_not_block_other_products(self):
        calls = []
        def prepare(product):
            calls.append(product["subject_ref"])
            if product["subject_ref"] == "company:a": raise RuntimeError("boom")
            return {"status": "completed"}
        result = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                           library_reader=reader)
        self.assertEqual((result["pending"], result["completed"]), (1, 2))
        states = [json.loads(path.read_text()) for path in self.state.glob("*.json")]
        self.assertEqual({row["status"] for row in states}, {"pending", "completed"})
        poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                  library_reader=reader)
        self.assertEqual(len(calls), 3)

    def test_periodic_obeys_max_loops_without_spending_on_unchanged_hashes(self):
        calls = []
        result = run_periodic(
            object(), MISSION, state_dir=self.state,
            prepare=lambda product: (calls.append(1) or {"status": "completed"}),
            stop_event=threading.Event(), interval_seconds=0.001, max_loops=2,
            library_reader=reader)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(calls), 3)
