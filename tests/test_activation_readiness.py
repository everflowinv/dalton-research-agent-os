import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.activation_readiness import audit, markdown, open_readonly
from dalton_core.store import content_hash


def sealed(body):
    body = dict(body)
    body["content_hash"] = content_hash(body)
    return body


class ActivationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.db = self.state / "core.sqlite"
        c = sqlite3.connect(self.db)
        c.executescript("""
        CREATE TABLE coverage_mission_pointer(mission_ref TEXT,mission_version_id TEXT,content_hash TEXT);
        CREATE TABLE coverage_mission_versions(mission_version_id TEXT,record_json TEXT,content_hash TEXT);
        CREATE TABLE claim_versions(claim_version_id TEXT,claim_json TEXT,created_at TEXT,claim_ref TEXT,version_number INTEGER);
        CREATE TABLE company_dossier_versions(version_id TEXT,company_ref TEXT,version_number INTEGER,record_json TEXT,content_hash TEXT);
        CREATE TABLE debate_map_versions(version_id TEXT,subject_ref TEXT,subject_kind TEXT,version_number INTEGER,record_json TEXT,content_hash TEXT);
        CREATE TABLE research_events(event_id TEXT,company_ref TEXT,content_hash TEXT);
        CREATE TABLE event_judgements(judgement_id TEXT,event_ref TEXT,company_ref TEXT,created_at TEXT,record_json TEXT,content_hash TEXT);
        """)
        self.company = "company:one"
        mission = sealed({"id": "mission-version:2", "mission_ref": "mission:x",
                   "universe": [{"company_ref": self.company, "ticker": "ONE"}],
                   "autonomy": {"may_write": ["dossier", "debate_map", "deliverable"]}})
        c.execute("INSERT INTO coverage_mission_pointer VALUES(?,?,?)", ("mission:x", mission["id"], mission["content_hash"]))
        c.execute("INSERT INTO coverage_mission_versions VALUES(?,?,?)", (mission["id"], json.dumps(mission), mission["content_hash"]))
        c.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?)", ("claim:1", json.dumps({"subject_ref": self.company}), "2026-09-10", "claim:one", 1))
        self.c = c

    def tearDown(self):
        self.c.close(); self.tmp.cleanup()

    def configs(self):
        router = sqlite3.connect(self.state / "model-router.sqlite")
        router.execute("CREATE TABLE model_routing_policy_versions(policy_version_ref TEXT)")
        for name in ("dossier-model-config.json", "company-dossier-verifier-model-config.json",
                     "initial-screen-model-config.json",
                     "event-judgement-model-config.json", "event-verifier-model-config.json"):
            ref = "route:" + name
            (self.state / name).write_text(json.dumps({"routing_policy_ref": ref}))
            router.execute("INSERT INTO model_routing_policy_versions VALUES(?)", (ref,))
        router.commit(); router.close()
        (self.state / "p12a-dossier-policy-v1.json").write_text("{}")

    def test_missing_outputs_name_config_before_claim_work(self):
        self.c.commit()
        report = audit(core_db=self.db, state_dir=self.state)
        products = report["companies"][0]["products"]
        self.assertEqual(products["company_dossier"]["status"], "missing")
        self.assertTrue(products["company_dossier"]["reason"].startswith("missing_config:"))
        self.assertTrue(products["event_judgement"]["reason"].startswith("missing_config:"))
        self.configs()
        report = audit(core_db=self.db, state_dir=self.state)
        self.assertIn("no_unjudged_company_events",
                      report["companies"][0]["products"]["event_judgement"]["reason"])

    def test_exact_refs_hashes_and_freshness_are_per_company(self):
        self.configs()
        dossier = sealed({"id": "dossier:v1", "version": 1, "company_ref": self.company,
                   "created_at": "2026-09-10T01:00:00+00:00",
                   "bindings": {"mission_version_ref": "mission-version:2"},
                   "evidence_refs": [{"kind": "claim", "ref": "claim:1"}]})
        fingerprint = __import__("hashlib").sha256(
            json.dumps({"claim_version_refs": ["claim:1"]}, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        debate = sealed({"id": "debate:v1", "version": 1, "subject_ref": self.company,
                  "created_at": "2026-09-10T02:00:00+00:00",
                  "evidence_fingerprint": fingerprint})
        event = sealed({"id": "judgement:1", "company_ref": self.company,
                 "created_at": "2026-09-10T03:00:00+00:00",
                 "event_ref": "event:1", "event_hash": "e"*64,
                 "mission_version_ref": "mission-version:2"})
        self.c.execute("INSERT INTO company_dossier_versions VALUES(?,?,?,?,?)", ("dossier:v1", self.company, 1, json.dumps(dossier), dossier["content_hash"]))
        self.c.execute("INSERT INTO debate_map_versions VALUES(?,?,?,?,?,?)", ("debate:v1", self.company, "company", 1, json.dumps(debate), debate["content_hash"]))
        self.c.execute("INSERT INTO research_events VALUES(?,?,?)", ("event:1", self.company, "e"*64))
        self.c.execute("INSERT INTO event_judgements VALUES(?,?,?,?,?,?)", ("judgement:1", "event:1", self.company, event["created_at"], json.dumps(event), event["content_hash"]))
        self.c.commit()
        report = audit(core_db=self.db, state_dir=self.state)
        products = report["companies"][0]["products"]
        for item in products.values():
            self.assertEqual(item["status"], "present")
        self.assertIsNone(products["company_dossier"]["input_binding"]["fresh"])
        self.assertTrue(products["company_dossier"]["input_binding"]["bound_refs_valid"])
        self.assertTrue(products["debate_map"]["input_binding"]["fresh"])
        self.assertTrue(products["event_judgement"]["input_binding"]["fresh"])
        self.assertEqual(products["company_dossier"]["hash"], dossier["content_hash"])
        self.assertIsNone(products["debate_map"]["mission_binding"]["fresh"])
        self.assertIn("DebateMapVersion has no mission", products["debate_map"]["mission_binding"]["reason"])
        self.assertIn("| ONE | company_dossier | present | dossier:v1 |", markdown(report))

    def test_database_handle_is_physically_read_only(self):
        self.c.commit()
        with open_readonly(self.db) as ro:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("CREATE TABLE forbidden(x)")

    def test_claim_input_uses_latest_versions_and_producer_limit(self):
        from dalton_core.activation_readiness import _claims
        self.c.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?)", (
            "claim:2", json.dumps({"subject_ref": self.company}), "2026-09-11", "claim:one", 2))
        self.c.commit()
        with open_readonly(self.db) as ro:
            self.assertEqual(_claims(ro, self.company), ["claim:2"])
        self.c.executemany("INSERT INTO claim_versions VALUES(?,?,?,?,?)", [
            (f"claim:extra:{i:04}", json.dumps({"subject_ref": self.company}),
             "2026-09-11", f"extra:{i:04}", 1) for i in range(1001)])
        self.c.commit()
        with open_readonly(self.db) as ro:
            refs = _claims(ro, self.company)
            self.assertEqual(len(refs), 1000)
            self.assertNotIn("claim:extra:1000", refs)

    def test_actual_ledger_projection_matches_audit_claim_selection(self):
        from dalton_core.activation_readiness import _claims
        from dalton_core.debate_map_draft import subject_claim_refs
        from tests.test_claim_index_authority import ClaimIndexAuthorityTests
        fixture = ClaimIndexAuthorityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.add_claim("claim:parity")
        self.assertEqual(_claims(fixture.store.connection, "company:fixture"),
                         subject_claim_refs(fixture.store, "company:fixture"))

    def test_superseded_dossier_evidence_remains_a_valid_immutable_ref(self):
        self.configs()
        dossier = sealed({"id": "dossier:v1", "version": 1, "company_ref": self.company,
                          "created_at": "2026-09-10T01:00:00+00:00",
                          "bindings": {"mission_version_ref": "mission-version:2"},
                          "evidence_refs": [{"kind": "claim", "ref": "claim:1"}]})
        self.c.execute("INSERT INTO company_dossier_versions VALUES(?,?,?,?,?)",
                       (dossier["id"], self.company, 1, json.dumps(dossier), dossier["content_hash"]))
        self.c.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?)",
                       ("claim:2", json.dumps({"subject_ref": self.company}),
                        "2026-09-11", "claim:one", 2))
        self.c.commit()
        item = audit(core_db=self.db, state_dir=self.state)["companies"][0]["products"]["company_dossier"]
        self.assertTrue(item["input_binding"]["bound_refs_valid"])

    def test_corrupt_latest_product_is_not_reported_present(self):
        bad = {"id": "dossier:wrong", "company_ref": self.company,
               "created_at": "2026-09-10T01:00:00+00:00", "content_hash": "x" * 64}
        self.c.execute("INSERT INTO company_dossier_versions VALUES(?,?,?,?,?)",
                       ("dossier:v1", self.company, 1, json.dumps(bad), "d" * 64))
        self.c.commit()
        item = audit(core_db=self.db, state_dir=self.state)["companies"][0]["products"]["company_dossier"]
        self.assertEqual(item["status"], "corrupt")
        self.assertIn("record_id_drift", item["blockers"])
        self.assertIn("content_hash_drift", item["blockers"])

    def test_output_refuses_core_state_symlinks_and_duplicate_targets(self):
        from dalton_core.activation_readiness import main
        self.c.commit()
        before = self.db.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            alias = Path(tmp) / "alias.json"
            alias.symlink_to(self.db)
            for output in (self.db, self.state / "config.json", alias):
                with self.subTest(output=output), self.assertRaises(SystemExit):
                    main(["--state-dir", str(self.state), "--json-output", str(output)])
            output = Path(tmp) / "report.json"
            with self.assertRaises(SystemExit):
                main(["--state-dir", str(self.state), "--json-output", str(output),
                      "--markdown-output", str(output)])
        self.assertEqual(self.db.read_bytes(), before)

    def test_readonly_uri_escapes_filename_and_closes_connection(self):
        odd = self.state / "core?#.sqlite"
        sqlite3.connect(odd).close()
        with open_readonly(odd) as ro:
            self.assertEqual(ro.execute("SELECT 1").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            ro.execute("SELECT 1")

    def test_dossier_reports_invalid_policy_and_missing_input_gates(self):
        self.configs()
        self.c.commit()
        report = audit(core_db=self.db, state_dir=self.state)
        blockers = report["companies"][0]["products"]["company_dossier"]["blockers"]
        self.assertIn("invalid_config:p12a-dossier-policy-v1.json", blockers)
        self.assertIn("no_eligible_input:no_claim_index", blockers)
        self.assertIn("no_eligible_input:initial_screen_not_passed", blockers)

    def test_dossier_refs_resolve_their_own_authorities_and_do_not_guess_cells(self):
        from dalton_core.activation_readiness import _dossier_evidence
        self.c.executescript("""
        CREATE TABLE coverage_mission_statement_lines(line_id TEXT);
        CREATE TABLE coverage_mission_document_figures(figure_id TEXT);
        INSERT INTO coverage_mission_statement_lines VALUES('filed:one');
        INSERT INTO coverage_mission_document_figures VALUES('mission-document-figure:one');
        """)
        refs = [{"kind": "claim", "ref": "claim:1"},
                {"kind": "figure", "ref": "statement-line:filed:one"},
                {"kind": "figure", "ref": "mission-document-figure:one"}]
        self.assertTrue(_dossier_evidence(self.c, {"evidence_refs": refs})["bound_refs_valid"])
        refs.append({"kind": "forecast_cell", "ref": "cell:unversioned"})
        self.assertIsNone(_dossier_evidence(self.c, {"evidence_refs": refs})["bound_refs_valid"])
        refs.append({"kind": "figure", "ref": "statement-line:missing"})
        self.assertFalse(_dossier_evidence(self.c, {"evidence_refs": refs})["bound_refs_valid"])


if __name__ == "__main__": unittest.main()
