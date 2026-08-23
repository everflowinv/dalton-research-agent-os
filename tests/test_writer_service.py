import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError, decode_frame
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    FEEDBACK_BRIDGE_OPERATIONS,
    MAX_CONNECTIONS,
    RESEARCH_REVIEW_CONTROL_OPERATIONS,
    THESIS_IMPACT_OPERATIONS,
    VERIFIER_OPERATIONS,
    WORKER_OPERATIONS,
    Principal,
    WriterServerError,
    load_principals,
    write_token_config,
)


def invocation(i, family="family", work_order="wo"):
    return {
        "schema_version": "0.1", "id": i, "created_at": "2026-01-01T00:00:00+00:00",
        "work_order_ref": work_order, "profile_ref": "profile-" + i, "granularity": "task",
        "capability": "research", "provider": "provider-" + i, "model": "model-" + i,
        "model_family": family, "runtime_ref": "runtime", "actor_ref": "actor",
        "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
        "started_at": "2026-01-01T00:00:00+00:00", "completed_at": None,
        "side_effects": [], "parent_ref": None,
    }


def thesis_payload(statement="s"):
    return {"statement": statement, "mechanism": "m", "confidence": 0.7,
            "implied_expectation": "e", "claim_refs": [], "catalyst_refs": [],
            "falsifier_refs": [], "change_reason": "test"}


class WriterServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "private" / "coverage.db"
        self.sock = root / "run" / "writer.sock"
        self.tokens = root / "private" / "writer-tokens.json"
        self.worker_token = "worker-token-9f0c"
        self.verifier_token = "verifier-token-9f0c"
        self.core_token = "core-token-9f0c"
        self.review_token = "review-token-9f0c"
        self.thesis_impact_token = "thesis-impact-token-9f0c"
        write_token_config(self.tokens, [
            Principal("worker", self.worker_token, WORKER_OPERATIONS, frozenset({"producer"}), frozenset({"wo"})),
            Principal("verifier", self.verifier_token, VERIFIER_OPERATIONS, frozenset({"verifier"}), frozenset({"wo"})),
            Principal("core", self.core_token, CORE_OPERATIONS, unrestricted=True),
            Principal(
                "research-review-control", self.review_token,
                RESEARCH_REVIEW_CONTROL_OPERATIONS,
                actor_ref="bridge:tailscale-review",
            ),
            Principal(
                "thesis-impact",
                self.thesis_impact_token,
                THESIS_IMPACT_OPERATIONS,
                actor_ref="system:thesis-impact-model-worker",
            ),
        ])
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        self.proc = None
        self.start_server()
        self.addCleanup(self.stop_server)

    def start_server(self):
        self.proc = subprocess.Popen([
            sys.executable, "-m", "dalton_core.writer_server", "--db", str(self.db),
            "--scheduler", str(Path(self.tmp.name) / "scheduler.sqlite"),
            "--socket", str(self.sock), "--token-config", str(self.tokens),
        ], cwd=str(Path(__file__).parents[1]), env=self.env,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Hosted runners can take more than five seconds to import sqlite-heavy
        # authority modules under concurrent matrix load.  Keep the early-exit
        # check, but allow enough time for a healthy server to publish its UDS.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.sock.exists():
                return
            if self.proc.poll() is not None:
                self.fail(f"writer server exited with {self.proc.returncode}")
            time.sleep(0.02)
        self.fail("writer server did not create socket")

    def stop_server(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc = None
        self.tmp.cleanup()

    def stop_process_only(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        self.proc = None

    @property
    def worker(self):
        return WriterClient(str(self.sock), self.worker_token)

    @property
    def verifier(self):
        return WriterClient(str(self.sock), self.verifier_token)

    @property
    def core(self):
        return WriterClient(str(self.sock), self.core_token)

    @property
    def review(self):
        return WriterClient(str(self.sock), self.review_token)

    @property
    def thesis_impact(self):
        return WriterClient(str(self.sock), self.thesis_impact_token)

    def test_permission_matrix_and_unknown_fields(self):
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.register_invocation(invocation("worker-1", "one"))
        self.core.register_invocation(invocation("producer", "one"))
        self.worker.stage_change(change_id="c1", thesis_id="t1", content=thesis_payload(), producer_invocation_id="producer")
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.commit(change_id="c1", verification_id="v1", idempotency_key="k1")
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.stage_change(change_id="c2", thesis_id="t2", content=thesis_payload(), producer_invocation=invocation("producer-2", "one"))
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.create_policy(policy={"allowed_verdicts": ["pass"]})
        with self.assertRaises(RemoteError) as ctx:
            self.worker.call("stage_change", {"unknown": True})
        self.assertEqual(ctx.exception.code, "protocol_error")

    def test_thesis_impact_principal_is_scoped_and_empty_discovery_is_safe(self):
        self.assertEqual(
            self.thesis_impact.thesis_impact_targets({}, limit=10), []
        )
        with self.assertRaises(RemoteAuthorizationError):
            self.thesis_impact.stage_change(
                change_id="forbidden",
                thesis_id="thesis:forbidden",
                content=thesis_payload(),
                producer_invocation_id="missing",
            )
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.thesis_impact_targets({}, limit=10)

    def test_agenda_context_operations_are_core_only_and_closed(self):
        # Materializing an Agenda context reads mandates and perception
        # snapshots.  Only the core principal may ask for it, and neither
        # operation may accept an out-of-contract field.
        for client in (self.worker, self.verifier, self.review):
            with self.assertRaises(RemoteAuthorizationError):
                client.materialize_agenda_context(cycle_id="agenda-cycle:1")
            with self.assertRaises(RemoteAuthorizationError):
                client.register_perception_snapshot(snapshot={}, actor_ref="core")
            with self.assertRaises(RemoteAuthorizationError):
                client.get_agenda_mandate_version(version_id="mandate-version:1")
            with self.assertRaises(RemoteAuthorizationError):
                client.get_agenda_policy_version(version_id="agenda-policy-version:1")
            with self.assertRaises(RemoteAuthorizationError):
                client.get_perception_snapshot(snapshot_id="perception:1")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call("materialize_agenda_context", {"snapshot": {}})
        self.assertEqual(ctx.exception.code, "protocol_error")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call(
                "materialize_agenda_context",
                {
                    "cycle_id": "agenda-cycle:absent",
                    "max_tokens": 100,
                    "max_bytes": 1000,
                    "created_at": "2099-01-01T00:00:00Z",
                },
            )
        self.assertEqual(ctx.exception.code, "protocol_error")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call(
                "get_perception_snapshot",
                {"snapshot_id": "perception:1", "snapshot": {}},
            )
        self.assertEqual(ctx.exception.code, "protocol_error")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call(
                "register_perception_snapshot",
                {"snapshot": {}, "actor_ref": "core", "database_path": "/tmp/x"},
            )
        self.assertEqual(ctx.exception.code, "protocol_error")
        # A caller may name a cycle and a budget; it may not smuggle a body.
        with self.assertRaises(RemoteError) as ctx:
            self.core.materialize_agenda_context(
                cycle_id="agenda-cycle:absent", max_tokens=100, max_bytes=1000
            )
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(RemoteError) as ctx:
            self.core.register_perception_snapshot(
                snapshot={"schema_version": "0.1"}, actor_ref="core"
            )
        self.assertEqual(ctx.exception.code, "rejected")

    def test_stage_verify_commit_uses_separate_scopes(self):
        self.core.register_invocation(invocation("producer", "one"))
        self.core.register_invocation(invocation("verifier", "two"))
        staged = self.worker.stage_change(change_id="c1", thesis_id="t1", content=thesis_payload(), producer_invocation_id="producer")
        self.assertEqual(staged["status"], "staged")
        verified = self.verifier.verify_change(change_id="c1", verification_id="v1", verifier_invocation_id="verifier", verdict="pass", findings=[])
        self.assertEqual(verified["status"], "verified")
        committed = self.core.commit(change_id="c1", verification_id="v1", idempotency_key="k1")
        self.assertEqual(committed["status"], "fresh")
        self.assertEqual(self.core.current_pointer("t1")["version_id"], committed["version_id"])

    def test_scoped_actor_is_injected_and_spoof_is_rejected(self):
        self.core.register_invocation(invocation("producer", "one"))
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(
                change_id="spoof", thesis_id="t1", content=thesis_payload(),
                producer_invocation_id="producer", actor_id="human:owner",
            )
        self.worker.stage_change(
            change_id="c1", thesis_id="t1", content=thesis_payload(),
            producer_invocation_id="producer",
        )
        staged_event = next(event for event in self.core.list_events("t1") if event["event_type"] == "staged")
        self.assertEqual(staged_event["actor_id"], "worker")

        self.core.register_invocation(invocation("verifier", "two"))
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.verify_change(
                change_id="c1", verification_id="spoof-v", verifier_invocation_id="verifier",
                verdict="pass", findings=[], actor_id="human:owner",
            )

    def test_core_reads_and_socket_permissions(self):
        self.assertEqual(stat.S_IMODE(self.sock.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tokens.stat().st_mode), 0o600)
        self.assertEqual(self.core.active_policy()["policy_version_id"], "policy-1")
        client_vars = vars(self.worker)
        self.assertNotIn("db_path", client_vars)
        self.assertNotIn("database", client_vars)
        self.assertNotIn("coverage.db", repr(self.worker))
        self.assertNotIn(self.worker_token, repr(self.worker))

    def test_invalid_token_and_malformed_frame_are_sanitized(self):
        bad = WriterClient(str(self.sock), "wrong-token")
        with self.assertRaises(RemoteAuthorizationError) as ctx:
            bad.active_policy()
        self.assertEqual(ctx.exception.code, "forbidden")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(str(self.sock))
            raw.sendall(b"not-json\n")
            response = decode_frame(raw.makefile("rb").readline())
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "protocol_error")
        self.assertNotIn(str(self.db), json.dumps(response))
        self.assertNotIn("Traceback", json.dumps(response))

    def test_restart_preserves_database_but_not_client_db_access(self):
        self.core.register_invocation(invocation("producer", "one"))
        self.core.register_invocation(invocation("verifier", "two"))
        self.worker.stage_change(change_id="c1", thesis_id="t1", content=thesis_payload(), producer_invocation_id="producer")
        self.verifier.verify_change(change_id="c1", verification_id="v1", verifier_invocation_id="verifier", verdict="pass", findings=[])
        self.core.commit(change_id="c1", verification_id="v1", idempotency_key="k1")
        self.stop_process_only()
        self.start_server()
        self.assertEqual(self.core.current_pointer("t1")["version_number"], 1)

    def test_inline_foreign_and_wrong_work_order_subjects_are_rejected(self):
        self.core.register_invocation(invocation("producer", "one"))
        self.core.register_invocation(invocation("other", "one", work_order="not-assigned"))
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(change_id="inline", thesis_id="t", content=thesis_payload(), producer_invocation_id="producer", producer_invocation=invocation("producer", "attacker"))
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(change_id="foreign", thesis_id="t", content=thesis_payload(), producer_invocation_id="other")
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(change_id="missing", thesis_id="t", content=thesis_payload(), producer_invocation_id="not-registered")

        self.core.register_invocation(invocation("verifier", "two"))
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.verify_change(change_id="missing", verification_id="v-inline", verifier_invocation_id="verifier", verifier_invocation=invocation("verifier", "attacker"), verdict="pass", findings=[])
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.verify_change(change_id="missing", verification_id="v-foreign", verifier_invocation_id="producer", verdict="pass", findings=[])

    def test_token_config_rejects_non_owner_permissions(self):
        os.chmod(self.tokens, 0o644)
        # This test uses the loader directly and does not restart the running
        # server, so it cannot accidentally alter the live writer authority.
        with self.assertRaises(WriterServerError):
            load_principals(self.tokens)

    def test_human_actor_requires_nonempty_normalized_subject(self):
        with self.assertRaises(WriterServerError):
            Principal("governance", "bad", frozenset({"active_policy"}), actor_ref="human:")
        bad_config = Path(self.tmp.name) / "private" / "bad-human.json"
        bad_config.write_text(json.dumps({
            "schema_version": "0.1",
            "principals": [{
                "principal_id": "governance", "token": "bad-human-token",
                "operations": ["active_policy"], "allowed_invocation_refs": [],
                "work_order_refs": [], "unrestricted": False, "actor_ref": "human:",
            }],
        }), encoding="utf-8")
        os.chmod(bad_config, 0o600)
        with self.assertRaises(WriterServerError):
            load_principals(bad_config)

    def test_scoped_feedback_principals_require_exact_actor_and_operations(self):
        for principal_id, actor_ref in (
            ("dashboard-control", "bridge:tailscale-dashboard"),
            ("agenda-timeout", "automation:agenda-timeout"),
        ):
            valid = Path(self.tmp.name) / "private" / f"{principal_id}.json"
            write_token_config(valid, [
                Principal(
                    principal_id, f"{principal_id}-token", FEEDBACK_BRIDGE_OPERATIONS,
                    actor_ref=actor_ref,
                )
            ])
            self.assertIn(principal_id, load_principals(valid))
            invalid = Path(self.tmp.name) / "private" / f"{principal_id}-invalid.json"
            write_token_config(invalid, [
                Principal(
                    principal_id, f"{principal_id}-bad-token", FEEDBACK_BRIDGE_OPERATIONS,
                    actor_ref="bridge:openclaw-discord",
                )
            ])
            with self.assertRaises(WriterServerError):
                load_principals(invalid)

    def test_research_review_principal_is_exact_and_rejects_automation(self):
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.commit_reviewed_candidate(
                decision={}, evidence={}, claim={}, idempotency_key="forbidden"
            )
        with self.assertRaises(RemoteAuthorizationError):
            self.review.commit_reviewed_candidate(
                decision={
                    "reviewer_ref": "automation:timeout", "verdict": "accept",
                    "authorization": "explicit_human_review", "source": "tailscale_review",
                    "source_event_ref": "research-review:bad",
                },
                evidence={}, claim={}, idempotency_key="automation",
            )
        invalid = Path(self.tmp.name) / "private" / "research-review-invalid.json"
        write_token_config(invalid, [
            Principal(
                "research-review-control", "bad-review-token",
                RESEARCH_REVIEW_CONTROL_OPERATIONS,
                actor_ref="bridge:tailscale-dashboard",
            )
        ])
        with self.assertRaises(WriterServerError):
            load_principals(invalid)

    def test_partial_frame_does_not_block_valid_client_and_connection_limit(self):
        partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(partial.close)
        partial.connect(str(self.sock))
        partial.sendall(b'{"protocol_version":"0.1"')
        started = time.monotonic()
        self.assertEqual(self.core.active_policy()["policy_version_id"], "policy-1")
        self.assertLess(time.monotonic() - started, 0.75)
        partial.close()

        blockers = []
        try:
            for _ in range(MAX_CONNECTIONS):
                blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                blocker.settimeout(0.5)
                blocker.connect(str(self.sock))
                blocker.sendall(b"{")
                blockers.append(blocker)
                time.sleep(0.02)
            extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(extra.close)
            extra.settimeout(0.5)
            extra.connect(str(self.sock))
            extra.sendall(b"{")
            try:
                closed = extra.recv(1) == b""
            except (ConnectionResetError, BrokenPipeError):
                closed = True
            self.assertTrue(closed, "connection beyond MAX_CONNECTIONS was not rejected")
        finally:
            for blocker in blockers:
                blocker.close()


if __name__ == "__main__":
    unittest.main()
