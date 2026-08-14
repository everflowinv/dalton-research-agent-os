"""End-to-end slice 2 acceptance through the writer boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import (
    ADJUDICATOR_OPERATIONS,
    CAPABILITY_BUILDER_OPERATIONS,
    CAPABILITY_EVALUATOR_OPERATIONS,
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    RESEARCHER_OPERATIONS,
    VERIFIER_OPERATIONS,
    WORKER_OPERATIONS,
    Principal,
    write_token_config,
)


def invocation(identifier: str, family: str, capability: str, work_order: str) -> dict:
    return {
        "schema_version": "0.1", "id": identifier,
        "created_at": "2026-08-14T00:00:00+00:00", "work_order_ref": work_order,
        "profile_ref": f"profile:{identifier}", "granularity": "task",
        "capability": capability, "provider": f"provider:{family}",
        "model": f"model:{identifier}", "model_family": family,
        "input_refs": [], "output_refs": [],
        "started_at": "2026-08-14T00:00:00+00:00", "completed_at": None,
        "usage": {"tokens": 1}, "side_effects": [], "runtime_ref": "runtime:test",
        "actor_ref": f"agent:{identifier}", "parent_ref": None,
    }


class Slice2IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "private" / "dalton.db"
        self.sock = root / "run" / "writer.sock"
        self.tokens = root / "private" / "tokens.json"
        self.values = {
            "core": "token-core", "worker": "token-worker", "verifier": "token-verifier",
            "researcher": "token-researcher", "adjudicator": "token-adjudicator",
            "intruder": "token-intruder", "builder": "token-builder",
            "evaluator": "token-evaluator", "human": "token-human",
        }
        write_token_config(self.tokens, [
            Principal("core", self.values["core"], CORE_OPERATIONS, unrestricted=True),
            Principal("worker", self.values["worker"], WORKER_OPERATIONS,
                      frozenset({"thesis-producer"}), frozenset({"wo:thesis"}), actor_ref="agent:worker"),
            Principal("verifier", self.values["verifier"], VERIFIER_OPERATIONS,
                      frozenset({"thesis-verifier"}), frozenset({"wo:thesis"}), actor_ref="agent:verifier"),
            Principal("researcher", self.values["researcher"], RESEARCHER_OPERATIONS,
                      frozenset({"claim-producer"}), frozenset({"wo:research"}), actor_ref="agent:researcher"),
            Principal("intruder", self.values["intruder"], RESEARCHER_OPERATIONS,
                      frozenset({"intruder-producer"}), frozenset({"wo:intruder"}), actor_ref="agent:intruder"),
            Principal("adjudicator", self.values["adjudicator"], ADJUDICATOR_OPERATIONS,
                      frozenset({"claim-adjudicator"}), frozenset({"wo:research"}), actor_ref="agent:adjudicator"),
            Principal("builder", self.values["builder"], CAPABILITY_BUILDER_OPERATIONS,
                      frozenset({"cap-builder"}), frozenset({"wo:capability"}), actor_ref="agent:builder"),
            Principal("evaluator", self.values["evaluator"], CAPABILITY_EVALUATOR_OPERATIONS,
                      frozenset({"cap-evaluator"}), frozenset({"wo:capability"}), actor_ref="agent:evaluator"),
            Principal("human-governance", self.values["human"], HUMAN_GOVERNANCE_OPERATIONS,
                      actor_ref="human:lumos"),
        ])
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dalton_core.writer_server", "--db", str(self.db),
             "--socket", str(self.sock), "--token-config", str(self.tokens)],
            cwd=str(Path(__file__).parents[1]), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline and not self.sock.exists():
            if self.proc.poll() is not None:
                self.fail(f"writer server exited with {self.proc.returncode}")
            time.sleep(0.02)
        self.assertTrue(self.sock.exists())
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        self.tmp.cleanup()

    def client(self, name: str) -> WriterClient:
        return WriterClient(str(self.sock), self.values[name])

    def register_invocations(self) -> None:
        core = self.client("core")
        for spec in (
            ("claim-producer", "family-a", "research", "wo:research"),
            ("claim-adjudicator", "family-b", "adjudication", "wo:research"),
            ("intruder-producer", "family-c", "research", "wo:intruder"),
            ("thesis-producer", "family-a", "research", "wo:thesis"),
            ("thesis-verifier", "family-b", "verification", "wo:thesis"),
            ("cap-builder", "family-a", "capability-builder", "wo:capability"),
            ("cap-evaluator", "family-b", "capability-evaluator", "wo:capability"),
        ):
            core.register_invocation(invocation(*spec))

    def test_research_ledger_to_thesis_commit_over_scoped_writer(self) -> None:
        self.register_invocations()
        researcher = self.client("researcher")
        evidence = researcher.register_evidence(evidence={
            "evidence_ref": "evidence:filing", "source_type": "filing",
            "source_ref": "sec:accession", "retrieved_at": "2026-08-14T00:00:00+00:00",
            "source_lineage": ["sec:accession"], "independence_group": "sec:accession",
            "artifact_refs": ["artifact:filing"],
        })
        claim = researcher.register_claim(claim={
            "claim_ref": "claim:revenue", "subject_ref": "company:test",
            "metric_or_aspect": "revenue", "period": "2026Q2", "basis": "reported",
            "normalized_statement": "Revenue was 100", "claim_kind": "quantitative",
            "value": 100, "unit": "USDm",
        }, producer_invocation_refs=["claim-producer"])
        relation_payload = {
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        }
        self.assertEqual(researcher.relate_evidence(relation=relation_payload)["status"], "fresh")
        self.assertEqual(researcher.relate_evidence(relation=relation_payload)["status"], "duplicate")
        adjudicated = self.client("adjudicator").adjudicate_claim(adjudication={
            "claim_version_ref": claim["claim_version_id"], "adjudicated_status": "corroborated",
            "rationale": "independent check", "findings": [],
        }, adjudicator_invocation_ref="claim-adjudicator")
        self.assertEqual(adjudicated["status"], "corroborated")

        content = {
            "statement": "Revenue is growing", "mechanism": "volume", "confidence": 0.6,
            "implied_expectation": "continued growth", "claim_refs": [claim["claim_version_id"]],
            "catalyst_refs": [], "falsifier_refs": [], "change_reason": "new filing",
        }
        self.client("worker").stage_change(
            change_id="change:thesis", thesis_id="thesis:test", content=content,
            producer_invocation_id="thesis-producer",
        )
        self.client("verifier").verify_change(
            change_id="change:thesis", verification_id="verify:thesis",
            verifier_invocation_id="thesis-verifier", verdict="pass", findings=[],
        )
        committed = self.client("core").commit(
            change_id="change:thesis", verification_id="verify:thesis",
            idempotency_key="commit:thesis",
        )
        self.assertEqual(committed["status"], "fresh")

        with self.assertRaises(RemoteAuthorizationError):
            researcher.register_claim(claim={
                "claim_ref": "claim:forged", "subject_ref": "company:test",
                "metric_or_aspect": "x", "period": "p", "basis": "b",
                "normalized_statement": "forged", "claim_kind": "qualitative",
            }, producer_invocation_refs=["thesis-verifier"])
        with self.assertRaises(RemoteAuthorizationError):
            self.client("intruder").register_claim(claim={
                "claim_ref": "claim:revenue", "subject_ref": "company:test",
                "metric_or_aspect": "revenue", "period": "2026Q2", "basis": "reported",
                "normalized_statement": "hijacked revision", "claim_kind": "quantitative",
                "value": 999, "unit": "USDm",
            }, producer_invocation_refs=["intruder-producer"])
        with self.assertRaises(RemoteAuthorizationError):
            self.client("intruder").relate_evidence(relation={
                "evidence_version_ref": evidence["evidence_version_id"],
                "claim_version_ref": claim["claim_version_id"], "relation": "contradicts",
            })

    def test_capability_proposal_eval_human_promotion_over_writer(self) -> None:
        self.register_invocations()
        builder = self.client("builder")
        proposal = {
            "schema_version": "0.1", "id": "capability:v1",
            "created_at": "2026-08-14T00:00:00+00:00",
            "updated_at": "2026-08-14T00:00:00+00:00",
            "title": "Normalize vendor data", "kind": "deterministic_tool",
            "gap": {"reason": "repeated formatting"}, "expected_benefit": {"minutes": 10},
            "contract": {"input": "json", "output": "json"},
            "permissions": {"network": False, "filesystem": {"read": True, "write": False}},
            "fixtures": ["fixture:vendor"], "fixture_manifest_hash": "f" * 64,
            "participants": {}, "status": "proposed",
            "artifact_refs": ["artifact:tool-v1"], "prior_capability_ref": None,
        }
        submitted = builder.submit_capability_proposal(
            proposal=proposal, builder_invocation_ref="cap-builder", idempotency_key="proposal:v1",
        )
        evaluated = self.client("evaluator").record_capability_evaluation(
            proposal_ref=submitted["revision_id"], evaluation_id="evaluation:v1",
            fixtures=["fixture:vendor"], baseline={"passed": False},
            results={"passed": True}, environment_hash="e" * 64,
            evaluator_invocation_ref="cap-evaluator", idempotency_key="evaluation:v1",
        )
        promoted = self.client("human").decide_capability_promotion(
            proposal_ref=submitted["revision_id"], decision="approve",
            evaluation_id=evaluated["evaluation_id"], rationale="fixtures passed",
            idempotency_key="promotion:v1",
        )
        self.assertEqual(promoted["active_revision_id"], submitted["revision_id"])
        self.assertEqual(
            self.client("core").active_capability(submitted["capability_ref"])["revision_id"],
            submitted["revision_id"],
        )
        with self.assertRaises(RemoteAuthorizationError):
            builder.decide_capability_promotion(
                proposal_ref=submitted["revision_id"], decision="approve",
                evaluation_id=evaluated["evaluation_id"], rationale="self approve",
            )
        with self.assertRaises(RemoteError) as ctx:
            self.client("human").call("decide_capability_promotion", {
                "proposal_ref": submitted["revision_id"], "decision": "approve",
                "evaluation_id": evaluated["evaluation_id"], "rationale": "spoof actor",
                "actor_ref": "human:someone-else",
            })
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(RemoteAuthorizationError):
            self.client("core").create_policy(
                policy={"allowed_verdicts": ["pass"]}, version_number=2,
            )
        policy = self.client("human").create_policy(
            policy={"allowed_verdicts": ["pass"]}, version_number=2,
        )
        self.assertEqual(policy["actor_ref"], "human:lumos")


if __name__ == "__main__":
    unittest.main()
