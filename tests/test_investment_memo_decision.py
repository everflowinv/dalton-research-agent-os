from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.investment_memo_contract import CHECK_REFS, PRODUCER_GROUPS, verified_body_hash
from dalton_core.writer_server import WriterServer, WriterServerError


def memo():
    record = {
        "id": "memo:v1", "content_hash": "d" * 64,
        "deliverable_ref": "mission-deliverable:investment_memo:a",
        "kind": "investment_memo", "subject_ref": "company:a",
        "mission_version_ref": "mission:v14", "mission_version_hash": "a" * 64,
        "playbook_version_ref": "playbook:v1", "playbook_version_hash": "b" * 64,
        "template_ref": "investment_memo", "summary": "memo",
        "sections": [{"title": "S1", "body": "body", "claim_refs": [],
                      "numbers": [], "gaps": []}], "gaps": [],
    }
    calls = [{"group": group, "work_order_ref": f"work:{i}",
              "route_decision_ref": f"route-decision:{i}",
              "result_envelope_ref": f"result:{i}",
              "invocation_ref": f"invocation:{i}"}
             for i, group in enumerate(PRODUCER_GROUPS)]
    record["gate"] = {
        "schema_version": "investment-memo-gate-0.1", "passed": True,
        "checks": [{"check_ref": ref, "status": "pass", "reason": "ok",
                    "evidence_refs": ["claim:1"]} for ref in CHECK_REFS],
        "verified_body_hash": "", "key_questions": [
            {"question_ref": f"memo_q{i:02d}", "question": f"question {i}", "answer": "a",
             "refs": ["claim:1"], "unknown": False, "falsifier": "f"}
            for i in range(1, 13)],
        "input_bindings": [{"ref": "dossier:v1", "hash": "c" * 64,
                            "kind": "company_dossier"}],
        "producer_calls": calls,
        "verifier": {"verdict": "pass", "work_order_ref": "work:v",
                     "route_decision_ref": "route-decision:v",
                     "result_envelope_ref": "result:v", "invocation_ref": "invocation:v",
                     "producer_route_decision_refs": [f"route-decision:{i}" for i in range(4)],
                     "finding_codes": []},
    }
    record["gate"]["verified_body_hash"] = verified_body_hash(record)
    record["model_invocation_refs"] = [f"work:{i}" for i in range(4)] + ["work:v"]
    return record


class Row(dict):
    pass


class Connection:
    def __init__(self, record): self.record = record
    def execute(self, query, params=()):
        if "mission_deliverable_versions WHERE version_id" in query:
            return Result(Row(record_json=__import__("json").dumps(self.record),
                              content_hash=self.record["content_hash"]))
        if "coverage_mission_pointer" in query:
            return Result(Row(mission_version_id="mission:v14"))
        raise AssertionError(query)


class Result:
    def __init__(self, row): self.row = row
    def fetchone(self): return self.row


class Missions:
    def __init__(self):
        self.rows = [{"id": "company-pass", "stage_ref": "company_model",
                      "status": "gate_passed"}]
        self.fail_active_once = False
    def mission(self, ref):
        return {"id": ref, "mission_ref": "mission:coverage", "content_hash": "a" * 64}
    def stage_records(self, mission_ref, company_ref): return list(self.rows)
    def current_stage_state(self, mission_ref, company_ref):
        history = [row for row in self.rows if row["stage_ref"] == "investment_memo"]
        return {"stages": {"investment_memo": {"status": history[-1]["status"]}}} if history else {"stages": {}}
    def record_stage(self, **p):
        if p["stage_ref"] == "active_coverage" and self.fail_active_once:
            self.fail_active_once = False
            raise RuntimeError("crash after memo pass")
        row = {"id": f"stage:{len(self.rows)}", **p}
        self.rows.append(row)
        return row


class Deliverables:
    def __init__(self, record): self.record = record
    def latest(self, ref): return self.record


class Router:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get_decision(self, ref):
        suffix = ref.split(":")[-1]
        work = "work:v" if suffix == "v" else f"work:{suffix}"
        return {"id": ref, "outcome": "selected", "work_order_ref": work}


class Scheduler:
    def __init__(self, record): self.record = record; self.missing = set(); self.bad_verifier = False
    def formal_result(self, work):
        if work in self.missing: return None
        suffix = work.split(":")[-1]
        is_verifier = suffix == "v"
        output = ({"verdict": "pass", "verified_body_hash": self.record["gate"]["verified_body_hash"],
                   "finding_codes": []} if is_verifier else {"draft": True})
        if is_verifier and self.bad_verifier:
            output["verified_body_hash"] = "0" * 64
        route = "route-decision:v" if is_verifier else f"route-decision:{suffix}"
        return {"terminal_state": "succeeded", "result_envelope_id": f"result:{suffix}",
                "result_envelope": {"id": f"result:{suffix}", "status": "succeeded",
                    "work_order_ref": work, "invocation_ref": f"invocation:{suffix}",
                    "outputs": {"text": __import__('json').dumps(output)},
                    "metadata": {"route_decision_ref": route}}}


class Playbooks:
    def playbook(self, ref):
        return {"content_hash": "b" * 64,
                "key_questions": [f"question {i}" for i in range(1, 13)]}


class InvestmentMemoDecisionTests(unittest.TestCase):
    def server(self, record):
        obj = object.__new__(WriterServer)
        obj._store = type("Store", (), {"connection": Connection(record)})()
        obj._coverage_mission = Missions()
        obj._mission_deliverables = Deliverables(record)
        obj._research_playbook = Playbooks()
        obj._scheduler = Scheduler(record)
        obj._model_router_db = lambda: "/tmp/router.sqlite"
        return obj

    @patch("dalton_core.model_router.ModelRouter", return_value=Router())
    @patch("dalton_core.model_fallback_chain.served_family",
           side_effect=lambda router, ref: "verifier" if ref == "route-decision:v" else "producer")
    def test_approve_recovers_after_memo_pass_before_active_entry(self, family, router):
        record = memo()
        server = self.server(record)
        server.coverage_mission.fail_active_once = True
        request = {"memo_version_ref": record["id"],
                   "memo_version_hash": record["content_hash"],
                   "decision": "approve", "reason": "approved", "actor_ref": "human:owner"}
        with self.assertRaisesRegex(RuntimeError, "crash"):
            WriterServer._op_decide_investment_memo(server, request)
        result = WriterServer._op_decide_investment_memo(server, request)
        self.assertEqual(result["status"], "decided")
        self.assertEqual([r["stage_ref"] for r in server.coverage_mission.rows],
                         ["company_model", "investment_memo", "investment_memo",
                          "active_coverage"])

    @patch("dalton_core.model_router.ModelRouter", return_value=Router())
    @patch("dalton_core.model_fallback_chain.served_family", return_value="same")
    def test_same_family_refuses_before_stage_write(self, family, router):
        record = memo(); server = self.server(record)
        with self.assertRaisesRegex(WriterServerError, "not independent"):
            WriterServer._op_decide_investment_memo(server, {
                "memo_version_ref": record["id"], "memo_version_hash": record["content_hash"],
                "decision": "approve", "reason": "yes", "actor_ref": "human:owner"})
        self.assertEqual(len(server.coverage_mission.rows), 1)

    def test_nonhuman_and_tampered_hash_refuse_without_writes(self):
        record = memo(); server = self.server(record)
        for actor, digest in (("automation:mission", record["content_hash"]),
                              ("human:owner", "0" * 64)):
            with self.assertRaises(WriterServerError):
                WriterServer._op_decide_investment_memo(server, {
                    "memo_version_ref": record["id"], "memo_version_hash": digest,
                    "decision": "reject", "reason": "no", "actor_ref": actor})
        self.assertEqual(len(server.coverage_mission.rows), 1)

    @patch("dalton_core.model_router.ModelRouter", return_value=Router())
    @patch("dalton_core.model_fallback_chain.served_family",
           side_effect=lambda router, ref: "verifier" if ref == "route-decision:v" else "producer")
    def test_opposite_human_decision_cannot_replace_settled_verdict(self, family, router):
        record = memo(); server = self.server(record)
        server.coverage_mission.rows.extend([
            {"id": "entered", "stage_ref": "investment_memo", "status": "entered",
             "evidence_refs": [record["id"]]},
            {"id": "rejected", "stage_ref": "investment_memo", "status": "gate_failed",
             "evidence_refs": [record["id"]]},
        ])
        with self.assertRaisesRegex(WriterServerError, "opposite human decision"):
            WriterServer._op_decide_investment_memo(server, {
                "memo_version_ref": record["id"], "memo_version_hash": record["content_hash"],
                "decision": "approve", "reason": "changed mind", "actor_ref": "human:owner"})
        self.assertEqual(len(server.coverage_mission.rows), 3)

    @patch("dalton_core.model_router.ModelRouter", return_value=Router())
    @patch("dalton_core.model_fallback_chain.served_family",
           side_effect=lambda router, ref: "verifier" if ref == "route-decision:v" else "producer")
    def test_old_failed_memo_does_not_poison_new_head(self, family, router):
        record = memo(); server = self.server(record)
        server.coverage_mission.rows.extend([
            {"id": "old-entered", "stage_ref": "investment_memo", "status": "entered",
             "evidence_refs": ["memo:old"]},
            {"id": "old-failed", "stage_ref": "investment_memo", "status": "gate_failed",
             "evidence_refs": ["memo:old"]},
        ])
        result = WriterServer._op_decide_investment_memo(server, {
            "memo_version_ref": record["id"], "memo_version_hash": record["content_hash"],
            "decision": "approve", "reason": "new evidence", "actor_ref": "human:owner"})
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(server.coverage_mission.rows[-2]["evidence_refs"], [record["id"]])

    @patch("dalton_core.model_router.ModelRouter", return_value=Router())
    @patch("dalton_core.model_fallback_chain.served_family",
           side_effect=lambda router, ref: "verifier" if ref == "route-decision:v" else "producer")
    def test_selected_route_without_formal_result_refuses_before_stage_write(self, family, router):
        record = memo(); server = self.server(record)
        server._scheduler.missing.add("work:2")
        with self.assertRaisesRegex(WriterServerError, "no successful formal result"):
            WriterServer._op_decide_investment_memo(server, {
                "memo_version_ref": record["id"], "memo_version_hash": record["content_hash"],
                "decision": "approve", "reason": "yes", "actor_ref": "human:owner"})
        self.assertEqual(len(server.coverage_mission.rows), 1)

    @patch("dalton_core.model_router.ModelRouter", return_value=Router())
    @patch("dalton_core.model_fallback_chain.served_family",
           side_effect=lambda router, ref: "verifier" if ref == "route-decision:v" else "producer")
    def test_formal_verifier_must_match_memo_body_hash(self, family, router):
        record = memo(); server = self.server(record); server._scheduler.bad_verifier = True
        with self.assertRaisesRegex(WriterServerError, "does not match verifier formal output"):
            WriterServer._op_decide_investment_memo(server, {
                "memo_version_ref": record["id"], "memo_version_hash": record["content_hash"],
                "decision": "approve", "reason": "yes", "actor_ref": "human:owner"})
        self.assertEqual(len(server.coverage_mission.rows), 1)


if __name__ == "__main__": unittest.main()
