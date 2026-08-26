from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dalton_core.alphaengine_acquisition_launcher import (
    AcquisitionLaunchConflict,
    AcquisitionLaunchRejected,
    AcquisitionTicketNotFound,
    AlphaEngineAcquisitionLauncher,
)
from dalton_core.alphaengine_core_acquisition import (
    StaticConnectorGovernance,
    build_governance_record,
    write_governance_proposal,
)
from dalton_core.connector_governance_cli import (
    GovernanceCliError,
    approve_governance_record,
    main as governance_main,
)
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    DASHBOARD_CONTROL_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    write_token_config,
)


OWNER = "human:lumos"
DOCUMENT_REF = "alphaengine-doc:130000095976806"
PAGE_ONE = (
    "Operator: Good afternoon. New bookings were $19.3 billion for the quarter, "
    "a 2% decrease in US dollars and 3% in local currency. "
) * 300
PAGE_TWO = "Analyst question about managed services pipeline timing. " * 120
DOCUMENT = PAGE_ONE + PAGE_TWO
DIGEST = hashlib.sha256(DOCUMENT.encode("utf-8")).hexdigest()


def write_approved(path: Path) -> dict:
    record = build_governance_record(approved_by=OWNER, status="approved")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return record


class GovernanceApprovalCliTests(unittest.TestCase):
    def test_approve_flips_proposed_record_and_rebinds_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.json"
            proposed = write_governance_proposal(path, approved_by=OWNER)
            self.assertEqual(proposed["status"], "proposed")
            with self.assertRaises(Exception):
                StaticConnectorGovernance.load(path).approval({})
            approved = approve_governance_record(path, approved_by=OWNER)
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["approved_by"], OWNER)
            body = {k: v for k, v in approved.items() if k != "content_hash"}
            self.assertEqual(approved["content_hash"], content_hash(body))
            self.assertEqual(approved["expected_schema_hash"], proposed["expected_schema_hash"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = StaticConnectorGovernance.load(path)
            self.assertTrue(loaded.approved)
            # Idempotent for the same principal, refused for another.
            self.assertEqual(approve_governance_record(path, approved_by=OWNER), approved)
            with self.assertRaises(GovernanceCliError):
                approve_governance_record(path, approved_by="human:someone-else")

    def test_approve_refuses_tampered_or_non_human_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.json"
            write_governance_proposal(path, approved_by=OWNER)
            with self.assertRaises(GovernanceCliError):
                approve_governance_record(path, approved_by="bridge:automation")
            record = json.loads(path.read_text())
            record["max_lease_seconds"] = 100_000
            path.write_text(canonical_json(record) + "\n")
            with self.assertRaises(GovernanceCliError):
                approve_governance_record(path, approved_by=OWNER)
            # Re-hashed but no longer the packaged proposal shape.
            body = {k: v for k, v in record.items() if k != "content_hash"}
            body["allowed_permissions"] = dict(body["allowed_permissions"], network=True)
            path.write_text(canonical_json({**body, "content_hash": content_hash(body)}) + "\n")
            with self.assertRaises(GovernanceCliError):
                approve_governance_record(path, approved_by=OWNER)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(governance_main(["approve", "--path", str(path), "--approved-by", OWNER]), 1)

    def test_cli_show_and_approve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.json"
            write_governance_proposal(path, approved_by=OWNER)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(governance_main(["show", "--path", str(path)]), 0)
                self.assertEqual(governance_main(["approve", "--path", str(path), "--approved-by", OWNER]), 0)
            self.assertIn('"status": "approved"', out.getvalue())
            self.assertTrue(StaticConnectorGovernance.load(path).approved)


class LauncherUnitTests(unittest.TestCase):
    def test_launcher_refuses_before_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            DaltonStore(str(state / "core.sqlite")).close()
            gov = root / "gov.json"
            write_governance_proposal(gov, approved_by=OWNER)
            document = root / "doc.txt"
            document.write_text(DOCUMENT)
            launcher = AlphaEngineAcquisitionLauncher(
                state_dir=state, governance_path=gov,
                mode_args=("--fake-document-file", str(document)),
            )
            self.addCleanup(launcher.close)
            with self.assertRaises(AcquisitionLaunchRejected):
                launcher.start(document_ref=DOCUMENT_REF, actor_ref=OWNER)
            write_approved(gov)
            with self.assertRaises(AcquisitionLaunchRejected):
                launcher.start(document_ref="alphaengine-doc:../x", actor_ref=OWNER)
            with self.assertRaises(AcquisitionLaunchRejected):
                launcher.start(document_ref=DOCUMENT_REF, actor_ref="bridge:tailscale-dashboard")
            with self.assertRaises(AcquisitionLaunchRejected):
                launcher.start(document_ref=DOCUMENT_REF, actor_ref=OWNER, expected_content_sha256="abc")
            with self.assertRaises(AcquisitionLaunchRejected):
                launcher.start(document_ref=DOCUMENT_REF, actor_ref=OWNER, max_pages=0)
            with self.assertRaises(AcquisitionTicketNotFound):
                launcher.status("alphaengine-acquisition:" + "0" * 24)
            self.assertEqual(list((state / "acquisitions").iterdir()), [])
            # Approval by a non-human principal is refused even if the file says approved.
            record = build_governance_record(approved_by="automation:bot", status="approved")
            gov.write_text(canonical_json(record) + "\n")
            with self.assertRaises(AcquisitionLaunchRejected):
                launcher.start(document_ref=DOCUMENT_REF, actor_ref=OWNER)

    def test_rehearsal_acquisition_lands_in_core_and_single_slot_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            # Keep a writer-style connection open on the same Core while the
            # child process writes: this is the live topology.
            holder = DaltonStore(str(state / "core.sqlite"))
            self.addCleanup(holder.close)
            gov = root / "gov.json"
            write_approved(gov)
            document = root / "doc.txt"
            document.write_text(DOCUMENT)
            launcher = AlphaEngineAcquisitionLauncher(
                state_dir=state, governance_path=gov,
                mode_args=("--fake-document-file", str(document), "--governance-approved-by", OWNER),
            )
            self.addCleanup(launcher.close)
            ticket = launcher.start(
                document_ref=DOCUMENT_REF, actor_ref=OWNER, expected_content_sha256=DIGEST,
            )
            self.assertEqual(ticket["status"], "running")
            self.assertEqual(ticket["transport"], "rehearsal")
            with self.assertRaises(AcquisitionLaunchConflict):
                launcher.start(document_ref=DOCUMENT_REF, actor_ref=OWNER)
            self.assertEqual(launcher.wait(timeout=120), 0)
            status = launcher.status(ticket["id"])
            self.assertEqual(status["status"], "succeeded")
            self.assertEqual(status["exit_code"], 0)
            summary = status["summary"]
            self.assertEqual(summary["transport"], "fake")
            self.assertEqual(summary["page_count"], 2)
            self.assertEqual(summary["assembled_content_sha256"], DIGEST)
            self.assertTrue(summary["expected_digest_match"])
            self.assertTrue(summary["transcript_authority_probe"]["ok"])
            self.assertEqual(summary["core_counts"]["connector_source_envelopes"], 2)
            self.assertEqual(summary["core_counts"]["claim_versions"], 0)
            conn = sqlite3.connect(state / "core.sqlite")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM connector_source_envelopes").fetchone()[0], 2)
            conn.close()
            ticket_dir = state / "acquisitions" / ticket["id"].split(":", 1)[1]
            for name in ("ticket.json", "summary.json", "manifest.json", "run.log"):
                self.assertEqual((ticket_dir / name).stat().st_mode & 0o777, 0o600, name)
            # A second launch is a new plan (new created_at, new WorkOrder ids),
            # so the journal does not replay across processes: the provider is
            # called again and a second document unit is consumed.  Callers
            # must not re-launch the same document casually.
            again = launcher.start(document_ref=DOCUMENT_REF, actor_ref=OWNER)
            self.assertEqual(launcher.wait(timeout=120), 0)
            replay = launcher.status(again["id"])["summary"]
            self.assertEqual(replay["provider_calls"], 2)
            self.assertEqual(replay["replayed_pages"], 0)
            self.assertEqual(replay["assembled_content_sha256"], DIGEST)
            self.assertEqual(replay["core_counts"]["connector_source_envelopes"], 4)


class WriterAcquisitionOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        self.db = self.state / "core.sqlite"
        self.sock = root / "run" / "writer.sock"
        self.tokens = root / "private" / "writer-tokens.json"
        self.gov = root / "gov.json"
        write_governance_proposal(self.gov, approved_by=OWNER)
        self.document = root / "doc.txt"
        self.document.write_text(DOCUMENT)
        self.governance_token = "governance-token-s7c"
        self.dashboard_token = "dashboard-token-s7c"
        self.core_token = "core-token-s7c"
        write_token_config(self.tokens, [
            Principal("core", self.core_token, CORE_OPERATIONS, unrestricted=True),
            Principal("coverage-governance", self.governance_token, HUMAN_GOVERNANCE_OPERATIONS, actor_ref=OWNER),
            Principal("dashboard-control", self.dashboard_token, DASHBOARD_CONTROL_OPERATIONS, actor_ref="bridge:tailscale-dashboard"),
        ])
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        self.proc = subprocess.Popen([
            sys.executable, "-m", "dalton_core.writer_server", "--db", str(self.db),
            "--scheduler", str(self.state / "scheduler.sqlite"),
            "--socket", str(self.sock), "--token-config", str(self.tokens),
            "--connector-governance", str(self.gov),
            "--acquisition-rehearsal-document", str(self.document),
            "--acquisition-rehearsal-approved-by", OWNER,
        ], cwd=str(Path(__file__).parents[1]), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.sock.exists():
                return
            if self.proc.poll() is not None:
                self.fail(f"writer exited with {self.proc.returncode}")
            time.sleep(0.02)
        self.fail("writer did not create socket")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def test_human_governance_launch_and_status_through_writer(self):
        governance = WriterClient(str(self.sock), self.governance_token)
        dashboard = WriterClient(str(self.sock), self.dashboard_token)
        # Not a governance operation for the dashboard principal.
        with self.assertRaises(RemoteAuthorizationError):
            dashboard.call("acquire_alphaengine_document", {"document_ref": DOCUMENT_REF})
        # Proposed record: rejected before any process starts.
        with self.assertRaises(RemoteError) as rejected:
            governance.call("acquire_alphaengine_document", {"document_ref": DOCUMENT_REF})
        self.assertEqual(rejected.exception.code, "rejected")
        self.assertFalse(any((self.state / "acquisitions").iterdir()))
        approve_governance_record(self.gov, approved_by=OWNER)
        ticket = governance.call(
            "acquire_alphaengine_document",
            {"document_ref": DOCUMENT_REF, "expected_content_sha256": DIGEST},
        )
        self.assertEqual(ticket["status"], "running")
        self.assertEqual(ticket["actor_ref"], OWNER)
        self.assertEqual(ticket["governance_ref"], "connector-governance:alphaengine-get-document:v1")
        with self.assertRaises(RemoteError) as conflict:
            governance.call("acquire_alphaengine_document", {"document_ref": DOCUMENT_REF})
        self.assertEqual(conflict.exception.code, "conflict")
        deadline = time.monotonic() + 120
        status = None
        while time.monotonic() < deadline:
            status = governance.call("alphaengine_acquisition_status", {"ticket_ref": ticket["id"]})
            if status["status"] != "running":
                break
            time.sleep(0.5)
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "succeeded", status)
        self.assertTrue(status["summary"]["transcript_authority_probe"]["ok"])
        self.assertEqual(status["summary"]["assembled_content_sha256"], DIGEST)
        # The writer's own Core connection sees the child's rows.
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM connector_source_envelopes").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0], 0)
        conn.close()
        with self.assertRaises(RemoteError) as missing:
            governance.call("alphaengine_acquisition_status", {"ticket_ref": "alphaengine-acquisition:" + "f" * 24})
        self.assertEqual(missing.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
