import os
import sqlite3
import subprocess
import sys
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.errors import BadVerdict, GateRejected, IndependenceViolation, InvocationConflict, ValidationError, VerificationRequired
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.contracts import validate_contract


def invocation(i, family="family", provider=None):
    return {"schema_version": "0.1", "id": i, "created_at": "2026-01-01T00:00:00+00:00", "work_order_ref": "wo", "profile_ref": "profile-" + i,
            "granularity": "task", "capability": "research", "provider": provider or family,
            "model": "model-" + i, "model_family": family, "runtime_ref": "runtime",
            "actor_ref": "actor", "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
            "started_at": "2026-01-01T00:00:00+00:00", "completed_at": None, "side_effects": [], "parent_ref": None}


def thesis_payload(**overrides):
    value = {"statement": "s", "mechanism": "m", "confidence": 0.7,
             "implied_expectation": "e", "claim_refs": [], "catalyst_refs": [],
             "falsifier_refs": [], "change_reason": "test"}
    value.update(overrides)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(delete=False)
        self.db.close()
        self.addCleanup(lambda: os.unlink(self.db.name) if os.path.exists(self.db.name) else None)
        self.s = DaltonStore(self.db.name)
        self.s.stage_change("c1", thesis_id="t1", content=thesis_payload(), producer_invocation=invocation("p", "one"))

    def tearDown(self):
        self.s.close()

    def test_database_file_is_owner_only(self):
        self.assertEqual(os.stat(self.db.name).st_mode & 0o777, 0o600)

    def verify(self, verdict="pass", verifier="v"):
        return self.s.verify_change("c1", verification_id="vr1", verifier_invocation=invocation(verifier, "two"), verdict=verdict, findings=[])

    def test_success_and_chain(self):
        self.verify()
        result = self.s.commit("c1", "vr1", "k1")
        self.assertEqual(result["status"], "fresh")
        pointer = self.s.current_pointer("t1")
        self.assertEqual(pointer["version_id"], result["version_id"])
        self.assertEqual(pointer["version_number"], 1)
        self.assertEqual(pointer["content_hash"], content_hash(thesis_payload()))
        thesis = self.s.get_version(result["version_id"])
        validate_contract("ThesisVersion", json.loads(thesis["content_json"]))
        verification = json.loads(self.s.conn.execute("SELECT verification_json FROM verification_records WHERE verification_id='vr1'").fetchone()[0])
        validate_contract("VerificationRecord", verification)
        policy = json.loads(self.s.conn.execute("SELECT version_json FROM governance_policy_versions WHERE policy_version_id='policy-1'").fetchone()[0])
        validate_contract("GovernancePolicyVersion", policy)
        self.assertEqual(self.s.active_policy_version().to_dict(), policy)
        for row in self.s.list_events(aggregate_id="t1"):
            event = json.loads(row["event_json"])
            validate_contract("DomainEvent", event)
            if event["event_type"] == "committed":
                self.assertEqual(event["version_ref"], event["payload"]["version_id"])
                self.assertEqual(event["payload"]["policy_version_id"], self.s.active_policy()["policy_version_id"])
                self.assertEqual(event["payload"]["policy_content_hash"], self.s.active_policy()["content_hash"])
            elif event["event_type"] == "staged":
                self.assertIsNone(event["version_ref"])
                self.assertEqual(event["change_ref"], "c1")
                self.assertIsNone(event["verification_ref"])
            else:
                self.assertIsNone(event["version_ref"])
                self.assertEqual(event["change_ref"], "c1")
                self.assertEqual(event["verification_ref"], "vr1")
            self.assertTrue(event["actor_ref"])
            self.assertTrue(event["idempotency_key"])
            self.assertTrue(event["correlation_id"])

    def test_same_invocation_missing_and_bad_verdict(self):
        self.s.verify_change("c1", verification_id="vr1", verifier_invocation={"invocation_id": "p"}, verdict="pass")
        with self.assertRaises(IndependenceViolation):
            self.s.commit("c1", "vr1", "k")
        self.s.close()
        self.s = DaltonStore(":memory:")
        self.s.stage_change("c1", thesis_id="t1", content=thesis_payload(), producer_invocation=invocation("p"))
        self.verify(verdict="reject")
        with self.assertRaises(BadVerdict):
            self.s.commit("c1", "vr1", "k")

    def test_missing_verification_and_idempotency(self):
        with self.assertRaises(VerificationRequired):
            self.s.commit("c1", "vr1", "k")
        self.verify()
        first = self.s.commit("c1", "vr1", "k", request={"request": 1})
        self.assertEqual(self.s.commit("c1", "vr1", "k", request={"request": 1})["status"], "duplicate")
        self.assertEqual(self.s.commit("c1", "vr1", "k", request={"request": 2})["status"], "conflict")
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM thesis_versions").fetchone()[0], 1)

    def test_direct_writes_rejected(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("INSERT INTO current_pointers VALUES ('x','y',1,'h','now')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("INSERT INTO thesis_versions VALUES ('v','t',1,'{}','h',NULL,'c','vr',NULL,'now')")
        self.verify()
        self.s.commit("c1", "vr1", "k")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("UPDATE thesis_versions SET content_json='{}'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("DELETE FROM domain_events")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("DELETE FROM current_pointers")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("UPDATE idempotency_keys SET request_hash='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("DELETE FROM idempotency_keys")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.conn.execute("UPDATE staging_changes SET thesis_id='other' WHERE change_id='c1'")

    def test_commit_rechecks_staging_and_request_hash(self):
        self.verify()
        expected_hash = self.s._commit_request_hash("c1", "vr1", None, content_hash(thesis_payload()))
        with self.assertRaises(ValidationError):
            self.s.commit("c1", "vr1", "bad-hash", request_hash="caller-controlled")
        self.assertEqual(self.s.commit("c1", "vr1", "good-hash", request_hash=expected_hash)["status"], "fresh")

        tampered = DaltonStore(":memory:")
        self.addCleanup(tampered.close)
        tampered.stage_change("c", thesis_id="t", content=thesis_payload(), producer_invocation=invocation("tp"))
        tampered.verify_change("c", verification_id="v", verifier_invocation=invocation("tv", "two"), verdict="pass")
        tampered.conn.create_function("dalton_authorized", 0, lambda: 1)
        tampered.conn.execute("UPDATE staging_changes SET thesis_id='evil' WHERE change_id='c'")
        with self.assertRaises(GateRejected):
            tampered.commit("c", "v", "tampered")

    def test_policy_change_requires_reverification(self):
        self.verify()
        self.s.create_policy({"allowed_verdicts": ["pass"]}, version_number=2)
        with self.assertRaises(GateRejected):
            self.s.commit("c1", "vr1", "policy-changed")

    def test_subprocess_crash_has_no_orphan_commit(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            child = subprocess.run(
                [sys.executable, "tests/fault_crash_writer.py", path],
                cwd=os.path.dirname(os.path.dirname(__file__)),
                env={**os.environ, "PYTHONPATH": "src"},
                check=False,
            )
            self.assertEqual(child.returncode, 17)
            reopened = DaltonStore(path)
            self.addCleanup(reopened.close)
            for table in ("thesis_versions", "current_pointers", "idempotency_keys"):
                self.assertEqual(reopened.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM domain_events WHERE event_type='committed'").fetchone()[0], 0)
            self.assertEqual(reopened.conn.execute("SELECT status FROM staging_changes WHERE change_id='crash-change'").fetchone()[0], "verified")
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM domain_events WHERE event_type IN ('staged','verified')").fetchone()[0], 2)
        finally:
            os.unlink(path)

    def test_rollback_after_commit_failure(self):
        self.verify()
        with self.assertRaises(RuntimeError):
            self.s.commit("c1", "vr1", "k", fault_at="after_event")
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM thesis_versions").fetchone()[0], 0)
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM domain_events WHERE event_type='committed'").fetchone()[0], 0)
        self.assertIsNone(self.s.current_pointer("t1"))

    def test_policy_field_predicates_and_rejection(self):
        self.s.create_policy({
            "allowed_verdicts": ["pass"],
            "independence_predicates": [{
                "left_path": "producer.provider",
                "right_path": "verifier.provider",
                "operator": "ne",
            }],
        }, version_number=2)
        self.verify(verifier="v2")
        self.assertEqual(self.s.commit("c1", "vr1", "different-family")["status"], "fresh")

        same = DaltonStore(":memory:")
        self.addCleanup(same.close)
        same.create_policy({
            "allowed_verdicts": ["pass"],
            "independence_predicates": [{
                "left_path": "producer.provider",
                "right_path": "verifier.provider",
                "operator": "ne",
            }],
        }, version_number=2)
        same.stage_change("c", thesis_id="t", content=thesis_payload(), producer_invocation=invocation("p", provider="same"))
        same.verify_change("c", verification_id="v", verifier_invocation=invocation("q", provider="same"), verdict="pass")
        with self.assertRaises(IndependenceViolation):
            same.commit("c", "v", "same-family")
        with self.assertRaises(ValidationError):
            same.create_policy({"allowed_verdicts": ["pass"], "independence_predicates": [{"left_path": "producer.secret", "operator": "ne", "value": "x"}]}, version_number=3)
        with self.assertRaises(ValidationError):
            same.create_policy({"allowed_verdicts": ["pass"], "independence_predicates": [{"field": "provider", "op": "ne"}]}, version_number=3)

        reverse = DaltonStore(":memory:")
        self.addCleanup(reverse.close)
        reverse.create_policy({"allowed_verdicts": ["pass"], "independence_predicates": [{"left_path": "verifier.provider", "operator": "eq", "value": "verifier"}]}, version_number=2)
        reverse.stage_change("r", thesis_id="rt", content=thesis_payload(), producer_invocation=invocation("rp", provider="producer"))
        reverse.verify_change("r", verification_id="rv", verifier_invocation=invocation("rvv", provider="verifier"), verdict="pass")
        self.assertEqual(reverse.commit("r", "rv", "reverse")["status"], "fresh")

    def test_invocation_collision_is_explicit(self):
        with self.assertRaises(InvocationConflict):
            self.s.register_invocation({"invocation_id": "p", "model_family": "different"})

        with self.assertRaises(ValidationError):
            self.s.register_invocation({"invocation_id": "new-only-id"})

    def test_model_invocation_atomically_registers_generic_execution(self):
        saved = self.s.conn.execute(
            "SELECT execution_json,content_hash,kind FROM execution_invocations "
            "WHERE execution_id='p'"
        ).fetchone()
        self.assertIsNotNone(saved)
        wire = json.loads(saved["execution_json"])
        self.assertEqual(wire["kind"], "model")
        self.assertEqual(wire["work_order_ref"], "wo")
        self.assertEqual(saved["content_hash"], content_hash(wire))
        link = self.s.conn.execute(
            "SELECT execution_ref,model_invocation_ref FROM execution_invocation_model_links "
            "WHERE execution_ref='p'"
        ).fetchone()
        self.assertEqual(dict(link), {"execution_ref": "p", "model_invocation_ref": "p"})
        for table in ("execution_invocations", "execution_invocation_model_links"):
            with self.subTest(table=table), self.assertRaises(sqlite3.DatabaseError):
                self.s.conn.execute(f"DELETE FROM {table}")

    def test_legacy_model_invocation_is_backfilled_additively(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.unlink(path) if os.path.exists(path) else None)
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE model_invocations ("
            "invocation_id TEXT PRIMARY KEY, profile_ref TEXT, provider TEXT, model TEXT, "
            "capability TEXT, runtime_ref TEXT, actor_ref TEXT, environment_hash TEXT, "
            "granularity TEXT, work_order_ref TEXT, model_family TEXT, invocation_json TEXT, "
            "created_at TEXT)"
        )
        wire = invocation("legacy")
        old.execute(
            "INSERT INTO model_invocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy", wire["profile_ref"], wire["provider"], wire["model"],
                wire["capability"], wire["runtime_ref"], wire["actor_ref"],
                wire.get("environment_hash"), wire["granularity"], wire["work_order_ref"],
                wire["model_family"], canonical_json(wire), wire["created_at"],
            ),
        )
        old.commit()
        old.close()
        migrated = DaltonStore(path)
        self.addCleanup(migrated.close)
        execution = migrated.conn.execute(
            "SELECT kind,execution_json FROM execution_invocations WHERE execution_id='legacy'"
        ).fetchone()
        self.assertEqual(execution["kind"], "model")
        self.assertEqual(json.loads(execution["execution_json"])["id"], "legacy")
        self.assertEqual(
            migrated.conn.execute(
                "SELECT model_invocation_ref FROM execution_invocation_model_links "
                "WHERE execution_ref='legacy'"
            ).fetchone()[0],
            "legacy",
        )

    def test_conflicting_legacy_execution_backfill_fails_atomically(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.unlink(path) if os.path.exists(path) else None)
        legacy = sqlite3.connect(path)
        legacy.row_factory = sqlite3.Row
        legacy.create_function("dalton_authorized", 0, lambda: 1)
        schema = (Path(__file__).resolve().parents[1] / "src/dalton_core/schema.sql").read_text(
            encoding="utf-8"
        )
        legacy.executescript(schema)
        model_wire = invocation("legacy-conflict")
        legacy.execute(
            "INSERT INTO model_invocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-conflict", model_wire["profile_ref"], model_wire["provider"],
                model_wire["model"], model_wire["capability"], model_wire["runtime_ref"],
                model_wire["actor_ref"], model_wire.get("environment_hash"),
                model_wire["granularity"], model_wire["work_order_ref"],
                model_wire["model_family"], canonical_json(model_wire), model_wire["created_at"],
            ),
        )
        conflicting = {
            "schema_version": "0.1", "id": "legacy-conflict",
            "created_at": model_wire["created_at"], "kind": "connector",
            "work_order_ref": model_wire["work_order_ref"], "profile_ref": "connector:p",
            "capability": "connector", "input_refs": [], "output_refs": [],
            "started_at": model_wire["started_at"], "completed_at": None,
            "side_effects": [], "runtime_ref": "runtime:connector",
            "actor_ref": "agent:connector", "parent_ref": None,
            "environment_hash": None,
        }
        legacy.execute(
            "INSERT INTO execution_invocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conflicting["id"], conflicting["kind"], conflicting["work_order_ref"],
                conflicting["profile_ref"], conflicting["capability"],
                conflicting["runtime_ref"], conflicting["actor_ref"], None, None,
                canonical_json(conflicting), content_hash(conflicting), model_wire["created_at"],
            ),
        )
        legacy.commit()
        legacy.close()

        with self.assertRaises(InvocationConflict):
            DaltonStore(path)
        check = sqlite3.connect(path)
        try:
            self.assertEqual(
                check.execute(
                    "SELECT COUNT(*) FROM execution_invocation_model_links"
                ).fetchone()[0],
                0,
            )
        finally:
            check.close()

    def test_policy_verdict_interval_and_strict_invocation(self):
        with self.assertRaises(ValidationError):
            self.s.create_policy({"allowed_verdicts": ["allow"]}, version_number=2)
        with self.assertRaises(ValidationError):
            self.s.register_invocation({**invocation("mystery"), "mystery": True})
        self.s.create_policy({"allowed_verdicts": ["pass"], "effective_from": "2999-01-01T00:00:00+00:00"}, version_number=2)
        self.verify()
        with self.assertRaises(GateRejected):
            self.s.commit("c1", "vr1", "future")

    def test_stage_requires_exact_semantic_payload(self):
        with self.assertRaises(ValidationError):
            self.s.stage_change("bad", thesis_id="t", content={"statement": "only"}, producer_invocation=invocation("badp"))
        with self.assertRaises(ValidationError):
            self.s.stage_change("bad2", thesis_id="t", content={**thesis_payload(), "unknown": 1}, producer_invocation=invocation("badp2"))
        with self.assertRaises(ValidationError):
            self.s.stage_change("bad3", thesis_id="t", content=thesis_payload(), producer_invocation=invocation("badp3"), actor_id=7)

    def test_idempotency_hash_binds_target(self):
        self.verify()
        self.s.commit("c1", "vr1", "shared", request={"same": True})
        self.s.stage_change("c2", thesis_id="t2", content=thesis_payload(statement="new"), producer_invocation=invocation("p2"))
        self.s.verify_change("c2", verification_id="vr2", verifier_invocation=invocation("v2", "two"), verdict="pass")
        result = self.s.commit("c2", "vr2", "shared", request={"same": True})
        self.assertEqual(result["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
