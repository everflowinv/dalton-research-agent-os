"""Cockpit → real writer operation → immutable thesis admission and read-back."""
from pathlib import Path
import tempfile
import unittest
from dalton_core.agenda import AgendaStore
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.cockpit_plane import CockpitConflict
from tests.test_cockpit_plane import CockpitHarness
from tests.test_coverage_admission import thesis_content
from tests.thesis_question_fixtures import formal_content


class RealThesisDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.h=CockpitHarness(Path(self.temp.name));self.addCleanup(self.h.close)
        self.store=self.h.h.h.core;self.authority=CoverageAdmissionAuthority(self.store)
        self.login="owner@example.com"
        self.mandate=AgendaStore(self.store).create_mandate("mandate:cockpit-real",objective="Synthetic real admission",
            scope_refs=["company:sec-cik:0001467373","industry:us-it-services"],constraints={},success_criteria={},
            effective_from="2020-01-01T00:00:00+00:00",effective_until=None,actor_ref="human:owner",activate=True,
            version_id="mandate-version:cockpit-real:1",idempotency_key="mandate:cockpit-real:1")

    def candidate(self,suffix):
        pack=self.h.h.state["pack"]
        content=formal_content(self.store,thesis_content(),question="question:real-cockpit:"+suffix)
        content["falsifier_refs"]=["falsifier:x"]
        return self.authority.propose_thesis_admission(candidate_id="candidate:real-cockpit:"+suffix,
            thesis_ref="thesis:real-cockpit:"+suffix,company_ref="company:sec-cik:0001467373",industry_ref="industry:us-it-services",
            template_ref="template:x",driver_refs=["driver:d"],mandate_version_ref=self.mandate["id"],
            mandate_version_hash=self.mandate["content_hash"],driver_pack_version_ref=pack["id"],driver_pack_version_hash=pack["content_hash"],
            content=content,actor_ref="human:owner",idempotency_key="candidate:real-cockpit:"+suffix)

    def value(self,candidate,verdict):
        return {"kind":"thesis","ref":candidate["id"],"hash":candidate["content_hash"],"decision":verdict,
                "rationale":"已核对原始依据与带期限的证伪条件。","request_id":"real-decision-"+verdict}

    def test_admit_is_idempotent_has_real_receipt_and_leaves_pending_list(self):
        candidate=self.candidate("admit");value=self.value(candidate,"admit")
        first=self.h.plane.decide(self.login,value);repeat=self.h.plane.decide(self.login,value)
        self.assertEqual(first["status"],"decided")
        saved=self.authority.decision(first["result"]["decision"]["id"])
        self.assertEqual(saved,first["result"]["decision"])
        self.assertEqual(repeat["result"]["status"],"duplicate")
        self.assertTrue(saved["resulting_thesis_version_ref"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM thesis_admission_decisions WHERE candidate_id=?",(candidate["id"],)).fetchone()[0],1)
        self.assertFalse(any(x["ref"]==candidate["id"] for x in self.h.plane.approvals()["items"]))
        self.assertTrue(self.h.plane.decision_status(value)["decided"])
        self.assertFalse(self.h.plane.decision_status({**value,"request_id":"different"})["decided"])
        sent=[p for op,p in self.h.calls if op=="decide_thesis_admission"]
        self.assertEqual(sent[0]["idempotency_key"],sent[1]["idempotency_key"])

    def test_reject_has_readable_receipt_without_publishing_a_thesis(self):
        candidate=self.candidate("reject");value=self.value(candidate,"reject");value["rationale"]=""
        outcome=self.h.plane.decide(self.login,value)
        saved=self.authority.decision(outcome["result"]["decision"]["id"])
        self.assertEqual(saved["verdict"],"reject")
        self.assertIsNone(saved["resulting_thesis_version_ref"])
        self.assertEqual(saved["rationale"],"系统记录：用户未填写补充说明")
        self.assertFalse(any(x["ref"]==candidate["id"] for x in self.h.plane.approvals()["items"]))
        receipt=self.h.plane.decision_status(value)
        self.assertTrue(receipt["decided"])
        self.assertIsNone(receipt["resulting_version_ref"])

    def test_an_unsaved_success_callback_cannot_report_success(self):
        candidate=self.candidate("not-saved")
        self.h.plane.governance_call=lambda *a,**k:{"status":"fresh","decision":{"id":k["params"]["decision_id"]}}
        with self.assertRaisesRegex(CockpitConflict,"尚未读回"):
            self.h.plane.decide(self.login,self.value(candidate,"admit"))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM thesis_admission_decisions WHERE candidate_id=?",(candidate["id"],)).fetchone()[0],0)
