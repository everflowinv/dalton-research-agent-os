"""P14b / P14d at the two edges: the writer's ops and the cockpit's approvals."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.cockpit_plane import (
    CHANGE_REASON_LABELS,
    JUDGEMENT_DECISION_LABELS,
    CockpitError,
)
from dalton_core.deliverable_reopen import GateReopenAuthority, reopen_assessment
from dalton_core.governance_cli import ephemeral_call
from dalton_core.thesis_revision import ThesisRevisionAuthority
from dalton_core.writer_server import (
    CORE_DISCOVERY_OPERATIONS,
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    OPERATION_ACTOR_FIELDS,
    OPERATION_FIELDS,
    Principal,
    WriterServer,
)
from tests.p14a_fixtures import ACN, AUTOMATION, OWNER
from tests.test_cockpit_plane import CockpitHarness
from tests.test_deliverable_reopen import ReopenHarness
from tests.test_thesis_revision import RevisionHarness

OPS = ("decide_thesis_revision_candidate", "decide_gate_reopen")


class OperationContractTests(unittest.TestCase):
    def test_both_operations_are_human_governance_with_closed_fields(self):
        for operation in OPS:
            self.assertIn(operation, HUMAN_GOVERNANCE_OPERATIONS)
            self.assertIn(operation, OPERATION_FIELDS)
            self.assertEqual(OPERATION_ACTOR_FIELDS[operation], "actor_ref")
            # Not a tick operation: there is no principal a lane could
            # authenticate as that would let it decide either of these.
            self.assertNotIn(operation, CORE_OPERATIONS)
            self.assertNotIn(operation, CORE_DISCOVERY_OPERATIONS)
        self.assertEqual(
            OPERATION_FIELDS["decide_thesis_revision_candidate"],
            frozenset({"candidate_ref", "candidate_hash", "verdict", "reason",
                       "content", "actor_ref"}),
        )
        self.assertEqual(
            OPERATION_FIELDS["decide_gate_reopen"],
            frozenset({"proposal_ref", "proposal_hash", "verdict", "reason",
                       "actor_ref"}),
        )

    def test_the_ephemeral_governance_path_will_carry_them(self):
        # governance_cli mints a principal whose operations are exactly
        # HUMAN_GOVERNANCE_OPERATIONS, so membership is the whole grant.
        for operation in OPS:
            self.assertIn(operation, HUMAN_GOVERNANCE_OPERATIONS)


class WriterDecisionTests(RevisionHarness):
    """The candidate decision, through a real writer, as a real principal."""

    def writer(self):
        server = WriterServer(
            str(self.state_dir / "core.sqlite"),
            str(self.state_dir / "writer.sock"),
            {"owner": self.principal(OWNER)},
        )
        # A real writer with a real Core on the same file, opened the way the
        # process opens it, so the two operations are exercised through the
        # authorities the writer actually builds rather than through stubs.
        server._open_store()
        self.addCleanup(server._store.close)
        return server

    def principal(self, actor_ref):
        return Principal(
            principal_id="owner", token="owner-token",
            operations=frozenset(HUMAN_GOVERNANCE_OPERATIONS),
            actor_ref=actor_ref,
        )

    def test_the_writer_binds_the_actor_and_refuses_a_spoof(self):
        candidate = self.candidate()
        server = self.writer()
        owner = self.principal(OWNER)
        bound = server._authorized_params(
            owner, "decide_thesis_revision_candidate",
            {"candidate_ref": candidate["id"], "candidate_hash": candidate["content_hash"],
             "verdict": "reject", "reason": "Noise."},
        )
        self.assertEqual(bound["actor_ref"], OWNER)
        with self.assertRaises(PermissionError):
            server._authorized_params(
                owner, "decide_thesis_revision_candidate",
                {"candidate_ref": candidate["id"], "actor_ref": "human:someone-else"},
            )

    def test_an_automation_principal_is_refused_before_the_handler_runs(self):
        candidate = self.candidate()
        server = self.writer()
        request = type("R", (), {
            "auth_token": None, "operation": "decide_thesis_revision_candidate",
            "params": {"candidate_ref": candidate["id"],
                       "candidate_hash": candidate["content_hash"],
                       "verdict": "accept", "reason": "go"},
        })()
        server._principal = lambda token: self.principal(AUTOMATION)  # noqa: ARG005
        with self.assertRaisesRegex(PermissionError, "authenticated human"):
            server._handle(request)
        self.assertEqual(len(self.revisions.version_chain("thesis:acn:ai-reinvention-growth")), 1)

    def test_an_unknown_parameter_never_reaches_the_authority(self):
        candidate = self.candidate()
        server = self.writer()
        request = type("R", (), {
            "auth_token": None, "operation": "decide_thesis_revision_candidate",
            "params": {"candidate_ref": candidate["id"],
                       "candidate_hash": candidate["content_hash"],
                       "verdict": "accept", "reason": "go", "force": True},
        })()
        server._principal = lambda token: self.principal(OWNER)  # noqa: ARG005
        from dalton_core.writer_server import ProtocolError

        with self.assertRaises(ProtocolError):
            server._handle(request)

    def test_the_operation_decides_the_candidate_end_to_end(self):
        candidate = self.candidate()
        server = self.writer()
        request = type("R", (), {
            "auth_token": None, "operation": "decide_thesis_revision_candidate",
            "params": {"candidate_ref": candidate["id"],
                       "candidate_hash": candidate["content_hash"],
                       "verdict": "accept",
                       "reason": "Bookings fired the falsifier we named."},
        })()
        server._principal = lambda token: self.principal(OWNER)  # noqa: ARG005
        result = server._handle(request)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["reviewer_ref"], OWNER)
        self.assertIsNotNone(result["resulting_thesis_version_ref"])
        chain = ThesisRevisionAuthority(self.store).version_chain(
            "thesis:acn:ai-reinvention-growth")
        self.assertEqual([item["version_number"] for item in chain], [1, 2])


class WriterReopenTests(ReopenHarness):
    def setUp(self):
        super().setUp()
        self.grant(checkpoints=("gate_reopen",))
        version = self.publish()
        self.pass_gate(version)
        self.thicken(lines=250)
        self.proposal = self.reopens.propose(
            assessment=reopen_assessment(self.store.connection, company_ref=ACN),
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def test_the_reopen_operation_is_human_only_and_records_the_approval(self):
        principal = Principal(
            principal_id="owner", token="owner-token",
            operations=frozenset(HUMAN_GOVERNANCE_OPERATIONS), actor_ref=OWNER,
        )
        server = WriterServer(
            str(self.state_dir / "core.sqlite"),
            str(self.state_dir / "writer.sock"), {"owner": principal},
        )
        server._open_store()
        self.addCleanup(server._store.close)
        server._principal = lambda token: principal  # noqa: ARG005
        request = type("R", (), {
            "auth_token": None, "operation": "decide_gate_reopen",
            "params": {"proposal_ref": self.proposal["id"],
                       "proposal_hash": self.proposal["content_hash"],
                       "verdict": "approve",
                       "reason": "十份财报进来了，这份筛选是在没有报表的时候写的。"},
        })()
        result = server._handle(request)
        self.assertEqual(result["verdict"], "approve")
        self.assertEqual(result["reviewer_ref"], OWNER)
        self.assertEqual(
            GateReopenAuthority(self.store).decision_for(self.proposal["id"])["id"],
            result["id"],
        )


class CockpitApprovalTests(unittest.TestCase):
    """The two rows on the approvals page, and the two buttons underneath."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = CockpitHarness(Path(self.temp.name))
        self.addCleanup(self.c.close)
        self.login = "owner@example.com"
        self.seen: list[tuple[str, dict]] = []

        def governance(token_config, socket, *, actor_ref, operation, params):
            self.seen.append((operation, dict(params), actor_ref))
            return {"status": "fresh"}

        self.c.plane.governance_call = governance

    def write(self, sql, params=()):
        import sqlite3

        connection = sqlite3.connect(str(self.c.core_path))
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def seed_candidate(self):
        # The rows the cockpit reads are a projection; writing them directly
        # is how this test stays about the *view* rather than about the
        # judgement lane's ability to produce one.
        self.write(
            "CREATE TABLE IF NOT EXISTS thesis_revision_candidates ("
            "candidate_id TEXT PRIMARY KEY, judgement_ref TEXT, thesis_version_ref TEXT,"
            "thesis_version_hash TEXT, company_ref TEXT, decision TEXT,"
            "checkpoint_kind TEXT, record_json TEXT, content_hash TEXT, actor_ref TEXT,"
            "created_at TEXT)")
        self.write(
            "CREATE TABLE IF NOT EXISTS thesis_revision_decisions ("
            "decision_id TEXT PRIMARY KEY, candidate_ref TEXT, terminal INTEGER)")
        self.write(
            "CREATE TABLE IF NOT EXISTS thesis_reflections ("
            "reflection_id TEXT PRIMARY KEY, judgement_ref TEXT, company_ref TEXT,"
            "record_json TEXT, content_hash TEXT, created_at TEXT)")
        reflection = {"id": "thesis-reflection:r", "content_hash": "b" * 64,
                      "trigger_kind": "revision",
                      "what_we_expected": "Bookings convert within two quarters.",
                      "what_happened": "They fell.", "why": "We misread a pipeline comment.",
                      "missed_debates": [{"question": "Federal exposure?", "refs": []}]}
        self.write("INSERT INTO thesis_reflections VALUES(?,?,?,?,?,?)",
                   ("thesis-reflection:r", "event-judgement:j", ACN,
                    json.dumps(reflection), "b" * 64, "2026-09-20T00:00:00+00:00"))
        record = {
            "id": "thesis-revision-candidate:c1", "content_hash": "a" * 64,
            "created_at": "2026-09-20T00:00:00+00:00",
            "company_ref": self.c.h.mission["universe"][0]["company_ref"],
            "decision": "THESIS_WEAKENED", "thesis_version_ref": "thesis-version:1",
            "judgement_ref": "event-judgement:j",
            "proposed_statement": "Reinvention demand is weaker than we said.",
            "proposed_confidence": "low", "because": "Bookings fell six percent.",
            "evidence_refs": ["claim-version:1"], "reflection_ref": "thesis-reflection:r",
        }
        self.write(
            "INSERT INTO thesis_revision_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("thesis-revision-candidate:c1", "event-judgement:j", "thesis-version:1",
             "f" * 64, record["company_ref"], "THESIS_WEAKENED",
             "thesis_revision_candidate", json.dumps(record), "a" * 64,
             AUTOMATION, record["created_at"]),
        )
        return record

    def seed_reopen(self):
        self.write(
            "CREATE TABLE IF NOT EXISTS gate_reopen_proposals ("
            "proposal_id TEXT PRIMARY KEY, company_ref TEXT, record_json TEXT,"
            "content_hash TEXT, created_at TEXT)")
        self.write(
            "CREATE TABLE IF NOT EXISTS gate_reopen_decisions ("
            "decision_id TEXT PRIMARY KEY, proposal_ref TEXT)")
        record = {
            "id": "gate-reopen-proposal:p1", "content_hash": "c" * 64,
            "created_at": "2026-09-21T00:00:00+00:00",
            "company_ref": self.c.h.mission["universe"][0]["company_ref"],
            "passed_version_ref": "mission-deliverable-version:old",
            "passed_version_number": 1, "passed_at": "2026-09-09T09:13:27+00:00",
            "change_reason": "evidence_thicker", "flipped": ["statements"],
            "regressed": [], "evidence_refs": ["statement-ingest:1"],
            "diff": [{"item_ref": "statements", "label": "已入库的财报报表行",
                      "was": {"value": 0, "present": False, "mark": "缺"},
                      "now": {"value": 2926, "present": True, "mark": "有"},
                      "flipped": True, "regressed": False, "refs": [], "note": ""}],
        }
        self.write("INSERT INTO gate_reopen_proposals VALUES(?,?,?,?,?)",
                   ("gate-reopen-proposal:p1", record["company_ref"],
                    json.dumps(record), "c" * 64, record["created_at"]))
        return record

    def test_a_candidate_appears_with_a_plain_title_and_its_reflection(self):
        self.seed_candidate()
        items = self.c.plane.approvals()["items"]
        row = next(item for item in items if item["kind"] == "thesis_revision_candidate")
        self.assertEqual(row["title"], "有事情发生，可能要改我们对这家公司的判断")
        self.assertEqual(row["ref"], "thesis-revision-candidate:c1")
        self.assertEqual(row["hash"], "a" * 64)
        self.assertEqual(row["details"]["大脑的判断"],
                         JUDGEMENT_DECISION_LABELS["THESIS_WEAKENED"])
        self.assertEqual(row["reflection"]["what_happened"], "They fell.")
        self.assertEqual([a["decision"] for a in row["actions"]],
                         ["accept", "reject", "defer"])
        self.assertTrue(row["needs_rationale"])
        self.assertNotIn("thesis-version:1", row["title"])

    def test_a_reopen_appears_with_the_diff_that_argues_for_it(self):
        self.seed_reopen()
        items = self.c.plane.approvals()["items"]
        row = next(item for item in items if item["kind"] == "gate_reopen")
        self.assertEqual(row["title"], "一道已经过掉的闸，现在有证据说可以重开")
        self.assertIn("缺（0）", row["summary"])
        self.assertIn("有（2926）", row["summary"])
        self.assertEqual(row["details"]["改版理由"],
                         CHANGE_REASON_LABELS["evidence_thicker"])
        self.assertEqual([a["decision"] for a in row["actions"]],
                         ["approve", "decline"])

    def test_each_button_routes_to_its_own_human_governance_operation(self):
        self.seed_candidate()
        self.seed_reopen()
        out = self.c.plane.decide(self.login, {
            "kind": "thesis_revision_candidate", "ref": "thesis-revision-candidate:c1",
            "hash": "a" * 64, "decision": "defer",
            "rationale": "再看一个季度。", "request_id": "r1"})
        self.assertEqual(out["status"], "decided")
        operation, params, actor = self.seen[-1]
        self.assertEqual(operation, "decide_thesis_revision_candidate")
        self.assertEqual(params["candidate_hash"], "a" * 64)
        self.assertEqual(params["verdict"], "defer")
        self.assertTrue(actor.startswith("human:"))
        self.assertNotIn(self.login, json.dumps(params))

        out = self.c.plane.decide(self.login, {
            "kind": "gate_reopen", "ref": "gate-reopen-proposal:p1", "hash": "c" * 64,
            "decision": "approve", "rationale": "报表进来了。", "request_id": "r2"})
        self.assertEqual(out["status"], "decided")
        operation, params, _actor = self.seen[-1]
        self.assertEqual(operation, "decide_gate_reopen")
        self.assertEqual(params["proposal_hash"], "c" * 64)
        self.assertEqual(params["verdict"], "approve")

    def test_a_word_outside_the_vocabulary_never_leaves_the_cockpit(self):
        self.seed_candidate()
        self.seed_reopen()
        for value in (
            {"kind": "thesis_revision_candidate", "ref": "thesis-revision-candidate:c1",
             "hash": "a" * 64, "decision": "admit", "rationale": "x", "request_id": "r"},
            {"kind": "thesis_revision_candidate", "ref": "thesis-revision-candidate:c1",
             "hash": "a" * 64, "decision": "accept", "rationale": "  ", "request_id": "r"},
            {"kind": "gate_reopen", "ref": "gate-reopen-proposal:p1", "hash": "c" * 64,
             "decision": "retired", "rationale": "x", "request_id": "r"},
            {"kind": "gate_reopen", "ref": "gate-reopen-proposal:p1", "hash": "c" * 64,
             "decision": "approve", "rationale": "", "request_id": "r"},
        ):
            with self.assertRaises(CockpitError):
                self.c.plane.decide(self.login, value)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
