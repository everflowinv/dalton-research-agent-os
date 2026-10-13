import json
import sqlite3
import unittest
from pathlib import Path

from dalton_core.mission_quality_score_lane import pending_targets, run_digest
from dalton_core.research_product_quality import product_artefact, product_quality
from dalton_core.research_quality_rubrics import rubric
from dalton_core.research_quality_score import QualityScoreAuthority, run_deterministic, SCORER_VERSION
from dalton_core.store import DaltonStore


class ProductQualityTests(unittest.TestCase):
    def test_event_source_numbers_are_bound_to_citations(self):
        record = {"id":"event:synthetic", "content_hash":"a"*64,
                  "because":"公司披露收入 120 万美元，需求变化还需下一季确认。", "citations":["N1"]}
        sources = {"N1":{"ref":"figure:synthetic", "text":"收入 120 万美元"}}
        self.assertTrue(product_quality("event_judgement",record,sources=sources)["passed"])
        record["citations"] = []
        self.assertFalse(product_quality("event_judgement",record,sources=sources)["passed"])

    def test_debate_sides_and_missing_body_are_scored(self):
        record = {"debates":[{"question":"客户需求是否改善？",
                  "bull_position":{"statement":"续约表明客户仍有需求。"},
                  "bear_position":{"statement":"预算审批延后构成相反证据。"},
                  "market_position":{"statement":"未持有可核验的市场预期。"},
                  "our_position":{"statement":""}}]}
        art = product_artefact("debate_map",record)
        self.assertEqual(len(art["sections"]), 4)
        self.assertFalse(product_quality("debate_map",record)["passed"])

    def test_old_score_rows_and_pointers_survive_migration_unchanged(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        core = store.connection
        schema = Path("src/dalton_core/research_quality_schema.sql").read_text()
        old_schema = schema.replace(",\n        'debate_map','investment_memo','event_judgement'", "")
        core.create_function("dalton_research_quality_authorized",0,lambda:1)
        core.executescript(old_schema)
        columns = [row[1] for row in core.execute("PRAGMA table_info(research_quality_score_versions)")]
        row = dict.fromkeys(columns,"synthetic")
        row.update(version_id="old-score",score_ref="score:old",version_number=1,prior_version_ref=None,
                   artefact_kind="initial_screen",record_json='{"historical":"unchanged"}',judge_status=None)
        core.execute("INSERT INTO research_quality_score_versions VALUES("+",".join("?" for _ in columns)+")", [row[c] for c in columns])
        core.execute("INSERT INTO research_quality_score_pointer VALUES(?,?,?,?,?)", ("score:old","old-score",1,"synthetic","2026-10-09"))
        core.commit()
        authority = QualityScoreAuthority(store)
        after = core.execute("SELECT * FROM research_quality_score_versions WHERE version_id='old-score'").fetchone()
        self.assertEqual(dict(after), row)
        self.assertEqual(core.execute("PRAGMA foreign_key_check").fetchall(), [])
        for kind in ("debate_map","investment_memo","event_judgement"):
            standard = rubric(kind)
            art = json.loads((Path("tests/golden")/kind/"baseline.json").read_text())["artefact"]
            deterministic = run_deterministic(art, standard)
            written = authority.record(artefact_kind=kind,target_ref=f"target:{kind}",target_hash="a"*64,
                rubric=standard,deterministic=deterministic,actor_ref="human:synthetic")
            self.assertEqual(written["artefact_kind"],kind)
        with self.assertRaises(sqlite3.IntegrityError):
            core.execute("UPDATE research_quality_score_versions SET content_hash='changed'")

    def test_pending_products_bind_rubric_hash_and_contract(self):
        core = sqlite3.connect(":memory:")
        core.row_factory = sqlite3.Row
        self.addCleanup(core.close)
        core.executescript("""
          CREATE TABLE company_dossier_versions(company_ref TEXT,record_json TEXT,created_at TEXT);
          CREATE TABLE debate_map_versions(subject_ref TEXT,record_json TEXT,created_at TEXT);
          CREATE TABLE event_judgements(company_ref TEXT,record_json TEXT,created_at TEXT);
          CREATE TABLE research_quality_score_versions(target_ref TEXT,target_hash TEXT,rubric_ref TEXT,rubric_hash TEXT,judge_status TEXT,scorer_version TEXT);
        """)
        for table,kind in (("company_dossier_versions","company_dossier"),("debate_map_versions","debate_map"),("event_judgements","event_judgement")):
            record={"id":kind+":v1","content_hash":"a"*64}
            core.execute(f"INSERT INTO {table} VALUES(?,?,?)",("company:SYN",json.dumps(record),"2026-10-09"))
        self.assertEqual({x["rubric"] for x in pending_targets(core)}, {"company_dossier","debate_map","event_judgement"})
        standard = rubric("debate_map")
        core.execute("INSERT INTO research_quality_score_versions VALUES(?,?,?,?,?,?)",("debate_map:v1","a"*64,standard.rubric_ref,"old-rules","scored",SCORER_VERSION))
        self.assertEqual(len(pending_targets(core)),3)
        core.execute("INSERT INTO research_quality_score_versions VALUES(?,?,?,?,?,?)",("debate_map:v1","a"*64,standard.rubric_ref,standard.content_hash,"scored","historical-scorer"))
        pending=pending_targets(core)
        self.assertEqual(len(pending),3)
        self.assertTrue(next(x for x in pending if x['rubric']=='debate_map')['deterministic_refresh'])
        core.execute("INSERT INTO research_quality_score_versions VALUES(?,?,?,?,?,?)",("debate_map:v1","a"*64,standard.rubric_ref,standard.content_hash,"scored",SCORER_VERSION))
        self.assertEqual(len(pending_targets(core)),2)
        self.assertNotEqual(run_digest("same","a"*64,"initial_screen"),run_digest("same","a"*64,"initial_screen_strl"))
