from __future__ import annotations
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.final_surface_products import _sections, _product
from dalton_core.cockpit_research_library import _sections as library_sections
from dalton_core.research_localization import localization_blockers, validate_localized_text, ResearchLocalizationError
from dalton_core.research_source_projection import PROJECTION_VERSION, legacy_projection, publication_source_hash
from dalton_core.research_publication_limits import PublicationLimits, publication_scope, current_scope, GovernedPublicationModel, PublicationParked
from dalton_core.research_publication_worker import poll_once, _hash

class TypedProjectionTests(unittest.TestCase):
    def field(self, text="Revenue", kind="surface_model_notes"):
        return _product(kind,"company:synthetic","version:synthetic","a"*64,_sections("模型说明",text))
    def test_short_label_and_explicit_source_gap_are_fields_not_missing_chapters(self):
        self.assertEqual(localization_blockers(self.field()),[])
        self.assertEqual(localization_blockers(self.field("缺少该季度预测假设")),[])
        self.assertEqual(localization_blockers(self.field("暂无可比对账记录")),[])
    def test_true_empty_and_placeholders_still_refuse_typed_fields(self):
        self.assertTrue(localization_blockers(self.field("TBD")))
        product=self.field();product["sections"][0]["body"]=""
        product["content_hash"]=_hash({k:v for k,v in product.items() if k!="content_hash"})
        self.assertIn("empty_source_section",{x["code"] for x in localization_blockers(product)})
    def test_real_narrative_empty_heading_gap_and_placeholder_are_not_exempt(self):
        for text in ["", "估值", "现有材料缺少足够信息。", "TBD"]:
            product=_product("surface_earnings_preview","company:synthetic","version:1","a"*64,
                [{"title":"估值","body":text,"gaps":[],"localization_role":"narrative"}])
            self.assertTrue(localization_blockers(product),text)
    def test_initial_screen_cannot_self_declare_a_display_field(self):
        product={"kind":"initial_screen","status":"available","version_ref":"version:1",
            "projection_contract":PROJECTION_VERSION,"sections":[
                {"title":"S6","body":"现有材料缺少估值资料。","gaps":[],"localization_role":"display_field"}]}
        self.assertTrue(localization_blockers(product))
        product.pop("projection_contract")
        self.assertTrue(localization_blockers(product))
    def test_only_exact_formal_absence_state_is_accepted(self):
        position={"available":False,"lean":None,"statement":None,"refs":[]}
        record={"debates":[{"question":"Synthetic question","market_position":position,
            "our_position":{"state":"none_yet","side":None,"statement":None,"refs":[]},
            "bull_position":{"statement":"正式多方观点包含可复核的经营机制，并据持有的事实作出判断。","claim_refs":["claim:1"]},
            "bear_position":{"statement":"正式空方观点包含可复核的风险机制，并据持有的事实作出判断。","claim_refs":["claim:2"]}}]}
        product={"kind":"debate_map","projection_contract":PROJECTION_VERSION,"sections":library_sections("debate_map",record)}
        self.assertEqual(localization_blockers(product),[])
        bad=copy.deepcopy(product);bad["sections"][2]["position"]["available"]=True
        self.assertTrue(localization_blockers(bad))
        bad=copy.deepcopy(product);bad["sections"][2]["sources"]=["claim:invented"]
        self.assertTrue(localization_blockers(bad))
    def test_role_binding_cannot_relax_numeric_translation_check(self):
        product=self.field("收入 12.5 亿美元。")
        localized={"sections":[{"index":0,"title":"模型说明","body":"收入 99.5 亿美元。","gaps":[]}]}
        with self.assertRaises(ResearchLocalizationError):validate_localized_text(product,localized)
    def test_legacy_key_is_reconstructed_not_accepted_from_caller(self):
        product=self.field();old=legacy_projection(product)
        self.assertEqual(publication_source_hash(product),_hash(old))
        self.assertNotEqual(_hash(product),_hash(old))
        forged=copy.deepcopy(product);forged["publication_source_hash"]="0"*64
        with self.assertRaises(ValueError):publication_source_hash(forged)
        new=_product("surface_model_notes","company:synthetic","version:2","b"*64,_sections("模型说明","Revenue"))
        self.assertNotEqual(publication_source_hash(new),publication_source_hash(product))
    def test_framework_gap_body_migration_preserves_exact_empty_body_ledger_key(self):
        gaps=["gap:1: 缺少行业规模资料","gap:2: 缺少竞争份额资料","gap:3: 缺少现金流可比口径"]
        old={"kind":"industry_framework","content_hash":"a"*64,"sections":[
            {"title":"gaps","body":"","sources":[],"numbers":[],"gaps":gaps}]}
        new=copy.deepcopy(old);new["projection_contract"]=PROJECTION_VERSION
        new["sections"][0].update(body="\n".join(gaps),localization_role="gap_appendix")
        self.assertEqual(legacy_projection(new),old)
        self.assertEqual(publication_source_hash(new),_hash(old))
        self.assertEqual(localization_blockers(new),[])
        empty={"kind":"industry_framework","content_hash":"a"*64,"projection_contract":PROJECTION_VERSION,"sections":[]}
        self.assertEqual(legacy_projection(empty)["sections"],[{"title":"gaps","body":"","sources":[],"numbers":[],"gaps":[]}])
    def test_new_empty_body_guard_preserves_old_gap_only_surface_limits(self):
        gap = "缺来源：consensus_revenue。"
        for kind in ("surface_earnings_preview", "surface_earnings_calibration"):
            old_sections = [{"title":"经营展望","body":gap,"gaps":[]}]
            new_sections = [{"title":"经营展望","body":"","gaps":[],
                "localization_role":"required_empty_body"}] + _sections("经营展望",gap,role="gap_note")
            new = _product(kind,"company:synthetic","version:1","a"*64,new_sections)
            old = legacy_projection(_product(kind,"company:synthetic","version:1","a"*64,
                _sections("经营展望",gap)))
            self.assertEqual(legacy_projection(new),old)
            self.assertEqual(legacy_projection(new)["sections"],old_sections)
            self.assertIn("empty_source_section",{r["code"] for r in localization_blockers(new)})
            for event_kind,limit in (("run",2),("call",6)):
                with tempfile.TemporaryDirectory() as d:
                    ledger=PublicationLimits(Path(d)/"limits.db")
                    for n in range(limit):ledger.admit(_hash(old),f"old{n}",f"stage{n}",kind=event_kind)
                    with self.assertRaises(PublicationParked):
                        ledger.admit(publication_source_hash(new),"new","next",kind=event_kind)
            bad=copy.deepcopy(new);bad["sections"][0]["body"]="收入增长"
            with self.assertRaises(ValueError):publication_source_hash(bad)

    def test_old_two_runs_and_six_calls_are_not_reset_by_roles(self):
        product=self.field();key=_hash(legacy_projection(product))
        for kind,limit in [("run",2),("call",6)]:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                path=Path(d)/"limits.db";ledger=PublicationLimits(path)
                for n in range(limit):ledger.admit(key,f"old{n}",f"stage{n}",kind=kind)
                with self.assertRaisesRegex(PublicationParked,f"publication_{kind}_24h_limit"):
                    ledger.admit(publication_source_hash(product),"new","next",kind=kind)
    def test_models_do_not_dispatch_seventh_call_after_typed_migration(self):
        product=self.field();key=publication_source_hash(product)
        class Model:
            calls=0
            def call(self,**kwargs):self.calls+=1;return {"cost_micros":1}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"limits.db";ledger=PublicationLimits(path)
            for n in range(6):ledger.admit(key,f"old{n}",f"stage{n}",kind="call")
            model=Model()
            with publication_scope(path,publication_source_hash(product)):
                with self.assertRaises(PublicationParked):GovernedPublicationModel(model).call(purpose="research_localization",prompt="原文")
            self.assertEqual(model.calls,0)
    def test_worker_rechecks_previous_false_skip_once_then_reuses_completion(self):
        product=self.field("营业收入");old=legacy_projection(product);mission={"universe":[{"company_ref":"company:synthetic"}]};calls=[]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"products"
            poll_once(None,mission,state_dir=root,prepare=lambda p:calls.append(p),
                library_reader=lambda *args:{"products":[old]})
            self.assertEqual(calls,[])
            for _ in range(2):poll_once(None,mission,state_dir=root,
                prepare=lambda p:(calls.append(p) or {"status":"completed"}),
                library_reader=lambda *args:{"products":[product]})
            self.assertEqual(len(calls),1)
    def test_worker_does_not_prepare_third_run_after_role_change(self):
        product=self.field("营业收入");key=publication_source_hash(product)
        mission={"universe":[{"company_ref":"company:synthetic"}]};calls=[]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"work";ledger=PublicationLimits(root/"publication-limits.sqlite3")
            for n in range(2):ledger.admit(key,f"old{n}","pipeline",kind="run")
            summary=poll_once(None,mission,state_dir=root/"products",
                prepare=lambda p:(calls.append(p) or {"status":"completed"}),
                library_reader=lambda *args:{"products":[product]})
            self.assertEqual(calls,[])
            self.assertEqual(summary["deferred"],1)
    def test_untrusted_body_fields_do_not_set_projection_roles(self):
        record={"sections":[{"title":"S6","body":"缺少材料。","gaps":[],
            "localization_role":"display_field","projection_contract":PROJECTION_VERSION}]}
        projected=library_sections("initial_screen",record)
        self.assertNotIn("localization_role",projected[0])
        self.assertTrue(localization_blockers({"kind":"initial_screen","sections":projected}))
    def test_single_section_and_whole_product_wrappers_use_same_limiter_identity(self):
        from dalton_core.research_output_preparation import run_chunk
        from dalton_core import research_language_runtime as runtime
        product=self.field("营业收入");key=publication_source_hash(product)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch("dalton_core.research_output_preparation._run_governed_chunk",side_effect=lambda *a,**k:current_scope()[1]):
                self.assertEqual(run_chunk((0,0,product),work_dir=root),key)
                self.assertEqual(run_chunk((0,0,product),work_dir=root,artifact_source_hash=key),key)
            with patch("dalton_core.research_language_runtime._run",side_effect=lambda *a,**k:current_scope()[1]):
                actual=runtime.run(product,mission={},request_id="synthetic",checker_config=root/"checker",
                    brain_config=root/"brain",verifier_config=root/"verifier",scheduler_db=root/"scheduler",
                    producer_route_decision_ref="route:synthetic",artifact_dir=root)
                self.assertEqual(actual,key)
