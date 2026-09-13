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
        self.assertEqual([section["body"] for section in events[0]["sections"]],
                         ["判断 6", "说明"])

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
        self.assertEqual([section["body"] for section in products[0]["sections"]],
                         ["需求改善", "需求持平", "预算延后"])

    def test_current_thesis_uses_updated_chain_head_not_only_version_one(self):
        self.db.executescript("""
        CREATE TABLE thesis_admission_candidates (candidate_id TEXT, company_ref TEXT);
        CREATE TABLE thesis_admission_decisions (decision_id TEXT, candidate_id TEXT);
        CREATE TABLE thesis_versions (version_id TEXT, thesis_id TEXT, version_number INTEGER,
          admission_decision_id TEXT, content_hash TEXT, content_json TEXT);
        CREATE TABLE current_pointers (thesis_id TEXT, version_id TEXT, updated_at TEXT);
        """)
        self.db.execute("INSERT INTO thesis_admission_candidates VALUES ('c1',?)", (COMPANY,))
        self.db.execute("INSERT INTO thesis_admission_decisions VALUES ('d1','c1')")
        self.db.execute("INSERT INTO thesis_versions VALUES "
                        "('v1','thesis:1',1,'d1','h1',?)",
                        (json.dumps({"statement": "旧论点"}),))
        self.db.execute("INSERT INTO thesis_versions VALUES "
                        "('v2','thesis:1',2,NULL,'h2',?)",
                        (json.dumps({"statement": "当前论点"}),))
        self.db.execute("INSERT INTO current_pointers VALUES ('thesis:1','v2','2026-09-13')")
        thesis = next(row for row in final_surface_products(self.db, MISSION, COMPANY)
                      if row["kind"] == "surface_thesis")
        self.assertEqual(thesis["version_ref"], "v2")
        self.assertEqual(thesis["sections"][0]["body"], "当前论点")

    def test_outside_company_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            final_surface_products(self.db, MISSION, "company:outside")

    def test_weekly_renderer_projects_the_exact_full_markdown_as_one_ui_string(self):
        self.db.execute("CREATE TABLE weekly_brief_issue_versions "
                        "(version_id TEXT,content_hash TEXT,record_json TEXT,version_number INTEGER)")
        self.db.execute("INSERT INTO weekly_brief_issue_versions VALUES (?,?,?,1)",
                        ("weekly:1", "weekly-hash", json.dumps({"thesis_bindings": [
                            {"company_ref": COMPANY, "statement": "周观点"}]})))
        exact = "# 每周研究简报\n\n完整正文与原始引用 claim-version:abc 保持原样。\n"
        products = final_surface_products(
            self.db, MISSION, COMPANY,
            weekly_renderer=lambda version: {"body": exact})
        weekly = next(row for row in products if row["kind"] == "surface_weekly_brief")
        self.assertEqual(weekly['subject_ref'], MISSION['mission_ref'])
        self.assertEqual(weekly["sections"], [
            {"title": "每周研究简报", "body": exact, "gaps": []}])

    def test_latest_model_notes_preserve_exact_fields_and_company(self):
        self.db.execute('CREATE TABLE forecast_model_versions '
                        '(company_ref TEXT,record_json TEXT,content_hash TEXT,version_number INTEGER)')
        for company, version, label in [(COMPANY, 1, 'old'), (COMPANY, 2, 'Current driver'),
                                        ('company:b', 3, 'Other company')]:
            model={'id':f'model:{version}', 'drivers':[{'label':label,'note':'Exact note'}],
                   'assumptions':[{'because':'Exact rationale'}],
                   'results':[{'label':label,'reason':'Exact reason','value':12345}]}
            self.db.execute('INSERT INTO forecast_model_versions VALUES (?,?,?,?)',
                            (company,json.dumps(model),f'hash:{version}',version))
        product=next(row for row in final_surface_products(self.db,MISSION,COMPANY)
                     if row['kind']=='surface_model_notes')
        self.assertEqual(product['version_ref'],'model:2')
        self.assertEqual([s['body'] for s in product['sections']],
                         ['Current driver','Exact note','Exact rationale','Exact reason'])
