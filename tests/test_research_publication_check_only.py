"""Low-value publication products never buy an Opus revision.

Live 2026-09-24 12:30-16:40 UTC: 63 of 181 research_language_revision calls
($16.90) revised NO_CHANGE event judgements and 29 ($9.90) the cycle
reflection.  After dd2b7105 a NO_CHANGE no longer mints an event_note, but the
judgement is still listed on the company card, so it is still prepared -- with
the cheap draft, the cheap language check, a deterministic revision and the
independent semantic verifier, and nothing else.
"""

from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import research_output_preparation as prep
from dalton_core import research_publication_spend as spend
from dalton_core.research_language_review import deterministic_revision

from tests.test_research_output_preparation import CHINESE, REVISION, SOURCE, STYLE, VERDICT

JUDGEMENT = {"kind": "surface_event_judgement", "version_ref": "judgement:1",
             "status": "available", "subject_ref": "company:a",
             "sections": [{"title": "事件研判", "body": "Revenue was 123 USD.", "gaps": []}]}
FAILED = {**VERDICT, "verdict": "fail", "faithful": False, "findings": ["语气改变"]}


class DeterministicRevisionTests(unittest.TestCase):
    def draft(self, body):
        return {"kind": "x", "version_ref": "v", "sections": [
            {"title": "收入", "body": body, "gaps": []}]}

    def review(self, *pairs):
        return {"overall": "可", "suggestions": [
            {"section_index": 0, "quote": quote, "assessment": "拗口", "suggestion": suggestion}
            for quote, suggestion in pairs]}

    def test_a_unique_quote_is_replaced_and_everything_else_is_rejected_with_a_reason(self):
        draft = self.draft("收入为 123 USD。收入为 123 USD。另有说明。")
        source = {"kind": "x", "version_ref": "v", "sections": [
            {"title": "Revenue", "body": "Revenue was 123 USD. Revenue was 123 USD. Note.",
             "gaps": []}]}
        result = deterministic_revision(draft, self.review(
            ("收入为 123 USD。", "收入为 123 美元。"),      # twice in the body
            ("另有说明。", "另附说明。"),                    # unique: adopted
            ("另有…", "另附"),                              # an excerpt
            ("另附说明。", "另附 456 项说明。"),             # adds a number
        ), numeric_source_product=source)
        self.assertEqual([row["decision"] for row in result["decisions"]],
                         ["reject", "adopt", "reject", "reject"])
        self.assertTrue(all(row["reason"] for row in result["decisions"]))
        self.assertEqual(result["sections"][0]["body"], "收入为 123 USD。收入为 123 USD。另附说明。")
        self.assertEqual(result, deterministic_revision(draft, self.review(
            ("收入为 123 USD。", "收入为 123 美元。"), ("另有说明。", "另附说明。"),
            ("另有…", "另附"), ("另附说明。", "另附 456 项说明。")),
            numeric_source_product=source))

    def test_the_fallback_keeps_the_draft_and_rejects_every_suggestion(self):
        draft = self.draft("收入为 123 USD。")
        result = deterministic_revision(draft, self.review(("收入为 123 USD。", "收入为 123 美元。")),
                                        apply_suggestions=False)
        self.assertEqual(result["sections"][0]["body"], "收入为 123 USD。")
        self.assertEqual(result["decisions"][0]["decision"], "reject")


class TierTests(unittest.TestCase):
    def test_no_change_judgements_and_the_reflection_are_check_only(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE event_judgements(judgement_id TEXT, decision TEXT)")
        db.executemany("INSERT INTO event_judgements VALUES(?,?)",
                       [("j:nc", "NO_CHANGE"), ("j:up", "UPDATE_THESIS")])
        cases = {("surface_event_judgement", "j:nc"): "check_only",
                 ("surface_event_judgement", "j:up"): "full",
                 ("surface_cycle_reflection", "r"): "check_only",
                 ("dossier", "d"): "full", ("surface_weekly_brief", "w"): "full",
                 ("ui_text", "u"): "full"}
        for (kind, ref), tier in cases.items():
            self.assertEqual(spend.language_tier(db, {"kind": kind, "version_ref": ref}),
                             tier, kind)

    def test_the_worker_prepares_each_product_at_its_tier(self):
        source = inspect.getsource(prep.run_worker)
        self.assertIn("tiered.language_tier=language_tier(connection,product)", source)
        self.assertIn("language_tier=getattr(args,'language_tier',LANGUAGE_TIER_FULL)",
                      inspect.getsource(prep.build))


class CheckOnlyPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.calls, self.excluded, self.asked = [], [], []
        self.responses = {"research_localization": CHINESE, prep.CHECKER_PURPOSE: STYLE,
                          prep.BRAIN_PURPOSE: REVISION,
                          "research_localization_verifier": VERDICT}
        owner = self

        class Model:
            def __init__(self, *a, **kw): pass

            def call(self, *, purpose, **kw):
                owner.calls.append(purpose)
                response = owner.responses[purpose]
                if isinstance(response, list):
                    response = response.pop(0)
                return {"text": json.dumps(response), "route_decision_ref": purpose,
                        "cost_micros": 1}

        def independent(model, *, producer_route_decision_refs, **kw):
            owner.excluded.append(producer_route_decision_refs)
            return model.call(**kw)
        self.enterContext(patch.object(prep, "CockpitModel", Model))
        self.enterContext(patch.object(prep, "independent_model_call", independent))
        self.enterContext(patch.object(prep, "selected_identity", return_value={
            "provider": prep.CHECKER_PROVIDER, "model": prep.CHECKER_MODEL}))
        self.enterContext(patch.object(prep, "router_family_resolver",
                                       return_value=lambda ref: ref))

    def run_chunk(self, product=JUDGEMENT, tier="check_only"):
        return prep.run_chunk((0, 0, product), mission={}, draft_config={"name": "draft"},
            verifier_config={"name": "verify"}, checker_config={"name": "checker"},
            brain_config={"name": "brain"}, scheduler_db=Path("unused"),
            work_dir=Path(self.temp.name), max_cost=.2, attempts=3, repair_reviewed=True,
            spend_gate=self.asked.append, language_tier=tier)[2]

    def test_check_only_is_draft_check_verifier_and_no_brain(self):
        result = self.run_chunk()
        self.assertEqual(self.calls, ["research_localization", prep.CHECKER_PURPOSE,
                                      "research_localization_verifier"])
        self.assertNotIn(prep.BRAIN_PURPOSE, self.asked)
        # The verifier is independent of the only author there was.
        self.assertEqual(self.excluded, [["research_localization"]])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["language_tier"], "check_only")
        self.assertEqual(result["localized"]["sections"][0]["body"], "收入为 123 美元。")
        self.assertEqual(result["language_review"]["status"], "ready_for_publication")
        self.assertEqual(result["language_review"]["brain_revision"]["decisions"][0]["decision"],
                         "adopt")
        self.assertEqual(self.run_chunk(), result)
        self.assertEqual(len(self.calls), 3)

    def test_a_rejected_applied_suggestion_falls_back_to_the_draft_without_a_model(self):
        self.responses["research_localization_verifier"] = [FAILED, VERDICT]
        result = self.run_chunk()
        self.assertEqual(self.calls, ["research_localization", prep.CHECKER_PURPOSE,
                                      "research_localization_verifier",
                                      "research_localization_verifier"])
        self.assertEqual(result["localized"]["sections"][0]["body"], "收入为 123 USD。")
        self.assertEqual([row["stage"] for row in result["review_history"]],
                         ["semantic", "deterministic_fallback"])
        # Replayed from disk: no new call, same result.
        self.assertEqual(self.run_chunk(), result)
        self.assertEqual(len(self.calls), 4)

    def test_a_rejected_draft_fails_without_buying_a_brain_repair(self):
        self.responses["research_localization_verifier"] = [FAILED, FAILED]
        with self.assertRaises(Exception):
            self.run_chunk()
        self.assertNotIn(prep.BRAIN_PURPOSE, self.calls)
        self.assertEqual(self.calls.count("research_localization_verifier"), 2)

    def test_an_invalid_draft_is_not_repaired_by_the_brain(self):
        bad = {"sections": [{"index": 0, "title": "收入", "body": "收入为 999 美元。", "gaps": []}]}
        self.responses["research_localization"] = bad
        with self.assertRaisesRegex(ValueError, "check-only draft failed validation"):
            self.run_chunk()
        self.assertEqual(self.calls, ["research_localization"] * 3)

    def test_check_only_never_reuses_a_full_stage_and_full_is_unchanged(self):
        self.run_chunk(product=SOURCE, tier="full")
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 1)
        self.run_chunk(product=SOURCE, tier="check_only")
        # A new draft and check under its own identity; still no new brain.
        self.assertEqual(self.calls.count(prep.BRAIN_PURPOSE), 1)
        self.assertEqual(self.calls.count("research_localization"), 2)
        self.assertEqual(len(list(Path(self.temp.name, "stages").glob("*.json"))), 2)

    def test_an_unknown_tier_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_chunk(tier="cheap")

    def test_a_check_only_chunk_publishes_like_any_reviewed_chunk(self):
        from dalton_core.research_localization import build_localization
        from dalton_core.research_localization_store import (has_reviewed_attachment,
                                                             publish_reviewed_attachment)
        saved = self.run_chunk()
        out = Path(self.temp.name) / "published"
        candidate = build_localization(JUDGEMENT, saved["localized"], saved["verifier"])
        publish_reviewed_attachment(out, JUDGEMENT, candidate, [copy.deepcopy(saved)])
        self.assertTrue(has_reviewed_attachment(out, JUDGEMENT))


if __name__ == "__main__":
    unittest.main()
