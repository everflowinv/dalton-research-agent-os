from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.final_surface_products import final_surface_products

COMPANY = "company:a"
MISSION = {"mission_ref": "mission:m", "industry_ref": "industry:i",
           "universe": [{"company_ref": COMPANY}]}


class FinalSurfaceProductsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:"); self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
        CREATE TABLE event_judgements (
          judgement_id TEXT, company_ref TEXT, record_json TEXT, content_hash TEXT, created_at TEXT);
        CREATE TABLE thesis_reflections (
          reflection_id TEXT, judgement_ref TEXT, company_ref TEXT, record_json TEXT,
          content_hash TEXT, created_at TEXT);
        """)

    def insert(self, table, identity, company, body, created):
        column = "judgement_id" if table == "event_judgements" else "reflection_id"
        if table == "event_judgements":
            self.db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?)",
                            (identity, company, json.dumps(body), identity + '-hash', created))
        else:
            self.db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?)",
                            (identity, "judgement:1", company, json.dumps(body),
                             identity + '-hash', created))

    def test_company_filter_latest_order_and_reference_exclusion(self):
        for index in range(7):
            self.insert("event_judgements", f"judgement:{index}", COMPANY,
                        {"because": f"判断 {index}", "note": "说明",
                         "citations": ["claim-version:" + "a" * 64]}, f"2026-09-{index+1:02d}")
        self.insert("event_judgements", "judgement:other", "company:b",
                    {"because": "另一家公司"}, "2026-09-09")
        products = final_surface_products(self.db, MISSION, COMPANY)
        events = [row for row in products if row["kind"] == "surface_event_judgement"]
        self.assertEqual([row["version_ref"] for row in events],
                         ["judgement:6", "judgement:5", "judgement:4", "judgement:3", "judgement:2"])
        rendered = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("另一家公司", rendered)
        self.assertNotIn("claim-version", rendered)

    def test_projection_executes_no_sqlite_write(self):
        self.insert("thesis_reflections", "reflection:1", COMPANY,
                    {"what_we_expected": "需求改善", "what_happened": "需求持平",
                     "why": "预算延后", "citations": ["secret-ref"]}, "2026-09-01")
        writes = []
        def authorizer(action, arg1, arg2, db, trigger):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                writes.append((action, arg1))
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorizer)
        products = final_surface_products(self.db, MISSION, COMPANY)
        self.assertEqual(writes, [])
        self.assertIn("需求改善", products[0]["sections"][0]["body"])
        self.assertNotIn("secret-ref", json.dumps(products, ensure_ascii=False))

    def test_outside_company_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            final_surface_products(self.db, MISSION, "company:outside")
