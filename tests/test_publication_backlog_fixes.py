"""2026-10-02: the legacy publication backlog was misreported and partly stuck.

* 287 UI-text batches read ``blocked``; all were sealed 09-21..09-24, before the
  fixes that removed their cause (163 "display attachment exceeds the size
  limit"), and ~224 of them were superseded batches nobody can see.
* ``attempted 1`` meant one prepare that met the day's spend ceiling.
* A dossier failed on "database is locked" twice and on provider cooldown and
  was parked for good; a check-only product with an English source was
  ``exhausted`` after one cheap draft.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import research_output_preparation as prep
from dalton_core import research_publication_worker as worker
from dalton_core import ui_text_discovery as ui
from dalton_core.research_publication_worker import (
    AUTO_RETRY_ROUND, EXHAUSTED_STATUS, poll_once, retry_chain, retry_class)
from dalton_core.store import canonical_json, content_hash
from dalton_core.ui_text_discovery import DEFAULT_MAX_ATTEMPTS, poll_ui_texts

from tests.test_research_publication_check_only import (
    FAILED, JUDGEMENT, CheckOnlyPreparationTests)
from tests.test_research_output_preparation import CHINESE, VERDICT

ENGLISH = ("Management expects enterprise demand to improve during the next "
           "fiscal year across all major markets.")


def failed(error):
    return {"status": "pending", "receipt": {"status": "incomplete", "failures": [
        {"product": 0, "section_start": 0, "error": error}]}}


class UiTextBacklogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.db = sqlite3.connect(":memory:"); self.addCleanup(self.db.close)
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
        self.claim("claim:1", ENGLISH)
        self.calls = 0
        self.outcome = failed("ValueError: display attachment exceeds the size limit")

    def claim(self, ref, text, version=1):
        wire = {"id": f"{ref}:{version}", "claim_ref": ref, "subject_ref": "company:A",
                "normalized_statement": text}
        self.db.execute("INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)", (
            f"{ref}:{version}", ref, version, canonical_json(wire), content_hash(wire), None,
            "2026-09-01T00:00:00Z"))
        self.db.commit()

    def poll(self, **kwargs):
        def prepare(_product, redraft_generation=0):
            self.calls += 1
            return dict(self.outcome)
        library = {"products": [{"status": "available", "sections": [{"sources": [
            {"kind": "claim", "ref": "claim:1:1"}]}]}]}
        with patch("dalton_core.ui_text_discovery.research_library", return_value=library):
            return poll_ui_texts(self.db, self.mission, state_dir=self.state, mapping={},
                                 prepare=prepare, **kwargs)

    def block(self):
        with patch.object(ui, "AUTO_RETRY_CLASSES", ()):
            for _ in range(DEFAULT_MAX_ATTEMPTS):
                result = self.poll()
        self.assertEqual(result["blocked"], 1)
        return result

    def test_a_batch_blocked_on_a_fixed_failure_is_retried_once_per_round(self):
        self.block()
        spent = self.calls
        self.outcome = {"status": "completed"}
        result = self.poll()
        self.assertEqual((result["auto_retried"], result["attempted"]), (1, 1))
        self.assertEqual(self.calls, spent + 1)
        [record] = ui.retry_records(self.state, result["batches"][0]["batch_ref"])
        self.assertEqual(record["trigger"], "automatic_after_fix")
        self.assertTrue(record["reason"].startswith(f"[auto-retry {ui.AUTO_RETRY_ROUND}]"))
        self.assertFalse(record["redraft"])

    def test_the_automatic_retry_is_not_repeated_after_it_fails_again(self):
        self.block()
        for _ in range(DEFAULT_MAX_ATTEMPTS):
            self.poll()
        spent = self.calls
        for _ in range(3):
            result = self.poll()
            self.assertEqual((result["auto_retried"], result["attempted"]), (0, 0))
        self.assertEqual(self.calls, spent)
        self.assertEqual(result["blocked"], 1)

    def test_a_content_failure_stays_blocked_and_is_counted_by_class(self):
        # Batch w: a changed number now earns one retry in its own round (the
        # rule was relaxed for stated unit conversions); a changed section
        # count is still the owner's.
        self.outcome = failed('localized sections must match source section count')
        self.block()
        result = self.poll()
        self.assertEqual((result["auto_retried"], result["blocked"]), (0, 1))
        self.assertEqual(result["blocked_reason_classes"], {"section_count_changed": 1})

    def test_a_superseded_blocked_batch_is_ineligible_not_blocked(self):
        self.block()
        # The claim text changes: the old batch can no longer be shown.
        self.claim("claim:1", ENGLISH + " Updated guidance.", version=2)
        library = {"products": [{"status": "available", "sections": [{"sources": [
            {"kind": "claim", "ref": "claim:1:2"}]}]}]}
        with patch("dalton_core.ui_text_discovery.research_library", return_value=library):
            result = poll_ui_texts(self.db, self.mission, state_dir=self.state, mapping={},
                                   prepare=lambda product, redraft_generation=0: {
                                       "status": "completed"})
        statuses = sorted(row["status"] for row in result["batches"])
        self.assertEqual(statuses, ["completed", "ineligible"])
        self.assertEqual((result["blocked"], result["auto_retried"]), (0, 0))
        self.assertEqual(result["blocked_reasons"], [])

    def test_a_ceiling_deferral_is_not_an_attempt(self):
        self.outcome = {"status": "deferred", "receipt": {"status": "deferred", "failures": [
            {"error": "purpose_daily_cap_reached: research_language_revision has committed "
                      "32689700 micros", "deferred": True}]}}
        result = self.poll()
        self.assertEqual((result["attempted"], result["ceiling_deferred"]), (0, 1))
        self.assertIn("32689700", result["ceiling_reason"])
        self.assertEqual(result["translation_unmapped"], 1)

    def test_reason_classes(self):
        cases = {
            "no model route is available right now": "provider_unavailable",
            "database is locked；no model route is available right now": "database_locked",
            "independent localization verifier did not pass cleanly": "verifier_rejected",
            "brain suggestion decision index is invalid": "brain_decisions_invalid",
            "something new": "other",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertEqual(ui.reason_class(failed(error)), expected)
        self.assertEqual(ui.auto_retry_class(failed("database is locked")), "database_locked")
        self.assertIsNone(ui.auto_retry_class(failed("independent localization verifier "
                                                     "did not pass cleanly")))


MISSION = {"universe": [{"company_ref": "company:a"}]}
PRODUCT = {"kind": "dossier", "subject_ref": "company:a", "version_ref": "dossier:31",
           "status": "available", "sections": [{"title": "结论", "body": "研究报告具有完整且可复核的经营结论。", "gaps": []}]}


class ProductRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "products"
        self.outcomes, self.prepared = [], 0
        self.now = "2026-10-01T09:57:29+00:00"
        self.enterContext(patch.object(worker, "_now", lambda: self.now))

    def prepare(self, product):
        self.prepared += 1
        return self.outcomes.pop(0)

    def poll(self, product=PRODUCT):
        return poll_once(None, MISSION, state_dir=self.state, prepare=self.prepare,
                         library_reader=lambda c, m, s: {"products": [product]},
                         clock=lambda:__import__("datetime").datetime.fromisoformat(self.now).timestamp())

    def test_a_transient_failure_is_retried_once_a_utc_day(self):
        # 2026-10-07: a spent budget; a lock is retried with back-off instead.
        refused = "research budget refused the call (mission_budget_exceeded)"
        self.outcomes = [failed(refused)] * 3 + [{"status": "completed"}]
        self.poll()
        self.assertEqual(self.poll()["retried"], 1)
        again = self.poll()
        self.assertEqual((again["retried"], again["parked"]), (0, 1))
        self.assertEqual(self.prepared, 2)
        self.now = "2026-10-02T00:05:00+00:00"
        self.assertEqual(self.poll()["retried"], 1)
        self.assertEqual(self.poll()["parked"], 1)
        self.now = "2026-10-03T00:05:00+00:00"
        self.assertEqual(self.poll()["pending"], 1)
        self.assertEqual(self.poll()["completed"], 1)
        self.assertEqual(self.prepared, 4)

    def test_provider_cooldown_is_transient(self):
        error = ("every model in the cheap chain failed: profile:gemini-3-8-flash-antigravity "
                 "(provider_cooldown), profile:gemini-3-8-flash-antigravity-high "
                 "(provider_cooldown)")
        self.assertEqual(retry_class(failed(error)), "provider_unavailable")
        self.assertIn("provider_unavailable", worker.TRANSIENT_RETRY_CLASSES)

    def test_a_check_only_product_exhausted_for_a_non_chinese_source_is_retried_once(self):
        error = ("check-only fallback unavailable (the source text is not predominantly "
                 "Simplified Chinese); first failure: independent localization verifier "
                 "did not pass cleanly")
        exhausted = {"status": EXHAUSTED_STATUS, "receipt": {"status": "exhausted",
                     "failures": [{"error": error, "exhausted": True}]}}
        self.assertEqual(retry_class(exhausted), "check_only_not_chinese")
        self.outcomes = [exhausted, exhausted]
        self.poll()
        self.assertEqual(self.poll()["retried"], 1)
        [record] = retry_chain(self.state, worker._hash(worker._identity(PRODUCT)))
        self.assertEqual(record["auto_round"], "2026-10-02a")
        self.now = "2026-10-05T00:00:00+00:00"
        self.assertEqual(self.poll()["retried"], 0)
        self.assertEqual(self.prepared, 2)

    def test_other_exhausted_products_stay_exhausted(self):
        exhausted = {"status": EXHAUSTED_STATUS, "receipt": {"status": "exhausted",
                     "failures": [{"error": "check-only fallback was refused by the verifier "
                                   "(x); first failure: database is locked",
                                   "exhausted": True}]}}
        self.outcomes = [exhausted]
        self.poll(); self.poll()
        self.assertEqual(self.prepared, 1)

    def test_fixed_classes_keep_their_round(self):
        self.assertEqual(worker.retry_round("attachment_size"), AUTO_RETRY_ROUND)
        self.assertEqual(worker.retry_round("check_only_not_chinese+database_locked"),
                         "2026-10-02a")


class FreshDraftFallbackTests(unittest.TestCase):
    run_chunk = CheckOnlyPreparationTests.run_chunk

    def setUp(self):
        CheckOnlyPreparationTests.setUp(self)

    def test_fresh_fallback_cannot_exceed_six_calls(self):
        self.responses["research_localization_verifier"]=[FAILED,FAILED,VERDICT]
        with self.assertRaises(prep.PublicationParked):self.run_chunk(product=JUDGEMENT)
        self.assertEqual(len(self.calls),6)
        self.assertNotIn(prep.BRAIN_PURPOSE,self.calls)
        self.assertFalse(list(Path(self.temp.name).glob('chunks/*.json')))

    def test_the_fresh_draft_is_only_for_check_only(self):
        with self.assertRaisesRegex(ValueError, "only for check-only"):
            prep._run_chunk((0, 0, JUDGEMENT), mission={}, draft_config={},
                verifier_config={}, checker_config={}, brain_config={},
                scheduler_db=Path("x"), work_dir=Path(self.temp.name), max_cost=.2,
                attempts=1, fresh_draft=True)

    def test_unavailable_reason(self):
        self.assertIn("not predominantly", prep._source_kept_unavailable(JUDGEMENT))


class StdoutTests(unittest.TestCase):
    def test_reason_classes_are_printed_whole(self):
        summary = prep.worker_stdout_summary({
            "status": "pending", "blocked": 3, "blocked_reasons": ["a", "b"],
            "blocked_reason_classes": {"attachment_size": 2, "other": 1},
            "ui_texts": {"blocked_reason_classes": {"attachment_size": 2, "other": 1},
                         "ceiling_reason": "purpose_daily_cap_reached: x"}},
            Path("/w/c.json"))
        self.assertEqual(summary["blocked_reason_classes"], {"attachment_size": 2, "other": 1})
        self.assertEqual(summary["ui_texts"]["blocked_reason_classes"]["attachment_size"], 2)
        self.assertEqual(summary["blocked_reasons_count"], 2)


if __name__ == "__main__":
    unittest.main()
