import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.store import canonical_json, content_hash
from dalton_core.ui_text_discovery import poll_ui_texts


class UITextDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE claim_versions(
            claim_version_id TEXT PRIMARY KEY, claim_ref TEXT, version_number INTEGER,
            claim_json TEXT, content_hash TEXT, prior_version_id TEXT, created_at TEXT);
          CREATE TABLE claim_retirement_decisions(claim_version_ref TEXT, decision TEXT);
          CREATE TABLE claim_index_entry_versions(
            version_id TEXT,entry_ref TEXT,version_number INTEGER,
            claim_version_ref TEXT,is_canonical INTEGER);
        """)
        self.mission = {"mission_ref": "mission:test", "industry_ref": "industry:test",
                        "universe": [{"company_ref": "company:A"}]}

    def claim(self, ref, text, *, claim_ref=None, version=1, subject="company:A"):
        wire = {"id": ref, "claim_ref": claim_ref or ref, "subject_ref": subject,
                "normalized_statement": text}
        self.db.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)", (
            ref, wire["claim_ref"], version, canonical_json(wire), content_hash(wire),
            None, f"2026-09-{version:02d}T00:00:00Z"))
        self.db.commit()

    @staticmethod
    def library(*refs):
        return {"products": [{"status": "available", "sections": [{
            "sources": list(refs) + [{"kind": "quote", "ref": "quote:1"},
                                     {"kind": "original_quote", "ref": "quote:2"}]}]}]}

    def poll(self, prepare, mapping=None, library_refs=()):
        with patch("dalton_core.ui_text_discovery.research_library",
                   return_value=self.library(*library_refs)):
            return poll_ui_texts(self.db, self.mission, state_dir=self.state,
                                 mapping=mapping or {}, prepare=prepare)

    def test_exact_mapping_makes_no_batch_and_no_call(self):
        self.claim("claim:1", "Revenue rose 10%.")
        calls = []
        result = self.poll(calls.append, {"Revenue rose 10%.": "收入增长10%。"})
        self.assertEqual((result["sealed"], result["attempted"]), (0, 0))
        self.assertEqual(calls, [])

    def test_sealed_batch_is_stable_when_new_claim_sorts_first(self):
        self.claim("claim:1", "Zulu statement")
        products = []
        first = self.poll(lambda product: products.append(product) or {"status": "pending"},
                          library_refs=("claim:1",))
        first_ref = first["batches"][0]["batch_ref"]
        self.claim("claim:2", "Alpha statement")
        second = self.poll(lambda product: {"status": "pending"})
        refs = [row["batch_ref"] for row in second["batches"]]
        self.assertEqual(refs[0], first_ref)
        self.assertEqual(len(refs), 2)
        manifest = json.loads(next((self.state / "manifests").glob("*.json")).read_text())
        source = manifest["entries"][0]["sources"][0]
        self.assertEqual(source["roles"], ["canonical_claim", "library_claim_source"])

    def test_pending_retries_same_product_while_existing_pipeline_owns_stage_cache(self):
        self.claim("claim:1", "A stable statement")
        products = []
        self.poll(lambda product: products.append(product) or {"status": "pending"})
        self.poll(lambda product: products.append(product) or {"status": "completed"})
        self.assertEqual(products[0], products[1])
        self.assertEqual(products[0]["version_ref"], products[1]["version_ref"])
        calls = []
        self.poll(lambda product: calls.append(product), {"A stable statement": "稳定表述"})
        self.assertEqual(calls, [])

    def test_batches_are_bounded_and_only_four_are_attempted(self):
        for number in range(121):
            self.claim(f"claim:{number}", f"Statement {number:03d} " + "x" * 140)
        calls = []
        result = self.poll(lambda product: calls.append(product) or {"status": "pending"})
        self.assertEqual(result["attempted"], 4)
        self.assertEqual(len(calls), 4)
        for product in calls:
            self.assertLessEqual(len(product["sections"]), 30)
            self.assertLessEqual(sum(len(row["body"]) for row in product["sections"]), 4500)

    def test_new_batch_runs_before_retries_so_failures_cannot_starve_it(self):
        for number in range(121):
            self.claim(f"claim:{number}", f"Statement {number:03d} " + "x" * 140)
        first = []
        self.poll(lambda product: first.append(product["version_ref"]) or {"status": "pending"})
        second = []
        self.poll(lambda product: second.append(product["version_ref"]) or {"status": "pending"})
        all_refs = {"ui-text-batch:" + path.stem
                    for path in (self.state / "manifests").glob("*.json")}
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertTrue((all_refs - set(first)).issubset(set(second)))

    def test_latest_current_claim_and_retirement_are_honored(self):
        self.claim("claim:old", "Old", claim_ref="claim:logical", version=1)
        self.claim("claim:new", "New", claim_ref="claim:logical", version=2)
        self.claim("claim:retired", "Retired")
        self.db.execute("INSERT INTO claim_retirement_decisions VALUES(?,?)",
                        ("claim:retired", "retired")); self.db.commit()
        products = []
        self.poll(lambda product: products.append(product) or {"status": "pending"})
        self.assertEqual([row["body"] for row in products[0]["sections"]], ["New"])

    def test_noncanonical_index_duplicate_is_excluded_unless_library_cites_it(self):
        self.claim("claim:canonical", "Canonical")
        self.claim("claim:duplicate", "Duplicate")
        self.db.executemany("INSERT INTO claim_index_entry_versions VALUES(?,?,?,?,?)", [
            ("index:1", "entry:1", 1, "claim:canonical", 1),
            ("index:2", "entry:2", 1, "claim:duplicate", 0),
        ]); self.db.commit()
        products = []
        self.poll(lambda product: products.append(product) or {"status": "pending"})
        self.assertEqual([row["body"] for row in products[0]["sections"]], ["Canonical"])
        other_state = self.state.with_name("library-state")
        with patch("dalton_core.ui_text_discovery.research_library",
                   return_value=self.library("claim:duplicate")):
            cited = []
            poll_ui_texts(self.db, self.mission, state_dir=other_state, mapping={},
                          prepare=lambda product: cited.append(product) or {"status": "pending"})
        self.assertEqual({row["body"] for row in cited[0]["sections"]},
                         {"Canonical", "Duplicate"})

    def test_library_exact_historical_claim_version_remains_discoverable(self):
        self.claim("claim:old", "Historical source", claim_ref="claim:logical", version=1)
        self.claim("claim:new", "Current claim", claim_ref="claim:logical", version=2)
        products = []
        self.poll(lambda product: products.append(product) or {"status": "pending"},
                  library_refs=("claim:old",))
        rows = {row["body"]: row for row in products[0]["sections"]}
        self.assertEqual(set(rows), {"Historical source", "Current claim"})
        self.assertEqual(rows["Historical source"]["sources"][0]["roles"],
                         ["library_claim_source"])

    def test_old_mission_and_removed_subject_batches_are_not_prepared(self):
        self.claim("claim:a", "Company A")
        self.poll(lambda product: {"status": "pending"})
        self.claim("claim:b", "Company B", subject="company:B")
        changed = {"mission_ref": "mission:new", "industry_ref": "industry:test",
                   "universe": [{"company_ref": "company:B"}]}
        calls = []
        with patch("dalton_core.ui_text_discovery.research_library",
                   return_value=self.library()):
            result = poll_ui_texts(self.db, changed, state_dir=self.state, mapping={},
                                   prepare=lambda product: calls.append(product) or {"status": "pending"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["subject_ref"], "mission:new")
        self.assertIn("ineligible", {row["status"] for row in result["batches"]})

    def test_recomputed_manifest_transplant_and_symlink_are_refused(self):
        self.claim("claim:1", "Original")
        self.poll(lambda product: {"status": "pending"})
        path = next((self.state / "manifests").glob("*.json"))
        manifest = json.loads(path.read_text())
        manifest["entries"][0]["text"] = "Forged"
        manifest["entries"][0]["text_sha256"] = __import__("hashlib").sha256(
            b"Forged").hexdigest()
        # Rebind all local hashes; the immutable ClaimVersion still refuses it.
        from dalton_core.ui_text_discovery import _hash, _product
        digest = _hash({"mission_ref": manifest["mission_ref"],
                        "entries": manifest["entries"]})
        manifest["batch_ref"] = "ui-text-batch:" + digest
        manifest["product"] = _product(manifest["mission_ref"], manifest["batch_ref"],
                                       manifest["entries"])
        unsigned = dict(manifest); unsigned.pop("content_hash")
        manifest["content_hash"] = _hash(unsigned)
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(ValueError, "source authority drifted"):
            self.poll(lambda product: {"status": "pending"})

        other = Path(self.tmp.name) / "other"; other.mkdir()
        symlink_state = Path(self.tmp.name) / "linked"; symlink_state.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            poll_ui_texts(self.db, self.mission, state_dir=symlink_state,
                          mapping={}, prepare=lambda product: {})

    def test_manifest_symlink_is_refused(self):
        self.claim("claim:1", "Original")
        self.poll(lambda product: {"status": "pending"})
        path = next((self.state / "manifests").glob("*.json"))
        target = Path(self.tmp.name) / "manifest-copy.json"
        target.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "unsafe UI text state"):
            self.poll(lambda product: {"status": "pending"})

    def test_source_hash_transplant_is_refused_even_with_all_local_hashes_rebound(self):
        self.claim("claim:1", "Original")
        self.poll(lambda product: {"status": "pending"})
        path = next((self.state / "manifests").glob("*.json"))
        manifest = json.loads(path.read_text())
        manifest["entries"][0]["sources"][0]["hash"] = "f" * 64
        from dalton_core.ui_text_discovery import _hash, _product
        digest = _hash({"mission_ref": manifest["mission_ref"],
                        "entries": manifest["entries"]})
        manifest["batch_ref"] = "ui-text-batch:" + digest
        manifest["product"] = _product(manifest["mission_ref"], manifest["batch_ref"],
                                       manifest["entries"])
        unsigned = dict(manifest); unsigned.pop("content_hash")
        manifest["content_hash"] = _hash(unsigned)
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(ValueError, "source authority drifted"):
            self.poll(lambda product: {"status": "pending"})


if __name__ == "__main__":
    unittest.main()
