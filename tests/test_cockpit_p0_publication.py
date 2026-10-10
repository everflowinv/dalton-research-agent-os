"""Cockpit inspection 2026-10-07, P0: BUG-01 / BUG-02 and the dossier fallback.

* the final-text gate is the ledger's answer for the exact version
  (``final_text_state``), not membership in the page's UI-text dictionary;
* a product waiting longer than the draft window, or given up on, shows and
  exports its draft under 「初稿（待审核签发）」 with a governed
  「重试语言质检」;
* "database is locked" is a retryable outcome: the worker retries it with a
  bounded, jittered back-off, and the day-ledger admission waits longer and
  holds its write lock for less time.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import research_publication_worker as worker
from dalton_core.final_text_state import (
    DRAFT, DRAFT_LABEL, PENDING, READY, final_text_state, language_policy,
)
from dalton_core.research_publication_worker import (
    BACKOFF_RETRY_BASE_SECONDS, BACKOFF_RETRY_MAX_PER_DAY, BACKOFF_RETRY_MAX_SECONDS,
    backoff_retry_delay, poll_once, retry_chain,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
PRODUCT = {"kind": "dossier", "subject_ref": "company:a", "version_ref": "dossier:21",
           "status": "available", "created_at": "2026-10-07T11:30:00+00:00",
           "sections": [{"title": "结论", "body": "收入为 2707409432.42083940 美元", "gaps": []}]}
MISSION = {"mission_ref": "coverage-mission:t", "universe": [{"company_ref": "company:a"}]}


def failed(error: str) -> dict:
    return {"status": "incomplete", "receipt": {"status": "incomplete",
            "failures": [{"product": 0, "section_start": 0, "error": error}]}}


class _State:
    """A state directory: policy, worker config and worker product states."""

    def __init__(self, test: unittest.TestCase, *, required: bool = True,
                 hours: float | None = None) -> None:
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        policy: dict = {"required": required}
        if hours is not None:
            policy["draft_fallback_after_hours"] = hours
        (self.root / "research-language-policy.json").write_text(json.dumps(policy))
        self.products = self.root / "research-publication-work" / "products"

    def worker_state(self, outcome: dict, product: dict = PRODUCT) -> None:
        poll_once(None, MISSION, state_dir=self.products, prepare=lambda _p: outcome,
                  library_reader=lambda c, m, s: {"products": [product]})


class FinalTextGateTests(unittest.TestCase):
    def test_reviewed_or_not_required_is_ready(self):
        state = _State(self)
        self.assertEqual(final_text_state(state.root, PRODUCT, reviewed=True)["status"], READY)
        loose = _State(self, required=False)
        self.assertEqual(final_text_state(loose.root, PRODUCT, reviewed=False,
                                          since="2020-01-01T00:00:00+00:00")["status"], READY)

    def test_pending_only_while_a_review_can_still_be_under_way(self):
        state = _State(self)
        young = final_text_state(state.root, PRODUCT, reviewed=False,
                                 since="2026-10-07T11:30:00+00:00", now=NOW)
        self.assertEqual(young["status"], PENDING)
        self.assertEqual(young["message"], "正文正在检查文字表达，完成后会显示。")
        # A lock failure is retried automatically: still pending while young.
        state.worker_state(failed("database is locked"))
        self.assertEqual(final_text_state(state.root, PRODUCT, reviewed=False,
                                          since="2026-10-07T11:30:00+00:00", now=NOW)["status"],
                         PENDING)

    def test_overdue_is_a_draft_after_the_configured_window(self):
        state = _State(self)
        old = final_text_state(state.root, PRODUCT, reviewed=False,
                               since="2026-10-07T10:59:00+00:00", now=NOW)
        self.assertEqual((old["status"], old["label"]), (DRAFT, DRAFT_LABEL))
        self.assertEqual(old["draft_after_seconds"], 3600)
        longer = _State(self, hours=3)
        self.assertEqual(language_policy(longer.root)["draft_after_seconds"], 3 * 3600)
        self.assertEqual(final_text_state(longer.root, PRODUCT, reviewed=False,
                                          since="2026-10-07T10:59:00+00:00", now=NOW)["status"],
                         PENDING)

    def test_given_up_is_a_draft_at_once_with_a_retry(self):
        for outcome in ({"status": "exhausted", "receipt": {"status": "exhausted"}},
                        failed("localized sections must match source section count")):
            with self.subTest(outcome=outcome["status"]):
                state = _State(self)
                state.worker_state(outcome)
                shown = final_text_state(state.root, PRODUCT, reviewed=False,
                                         since="2026-10-07T11:59:00+00:00", now=NOW)
                self.assertEqual(shown["status"], DRAFT)
                self.assertEqual(shown["retry"]["identity_hash"],
                                 worker._hash(worker._identity(PRODUCT)))
                self.assertEqual(shown["retry"]["product_hash"], worker._hash(PRODUCT))
                self.assertFalse(shown["retry"]["requested"])

    def test_a_requested_retry_is_pending_again(self):
        state = _State(self)
        state.worker_state(failed("localized section changed financial number tokens: {}"))
        worker.request_retry(state.products, worker._hash(worker._identity(PRODUCT)),
                             actor_ref="human:owner", reason="retry", apply=True)
        shown = final_text_state(state.root, PRODUCT, reviewed=False,
                                 since="2026-10-07T11:59:00+00:00", now=NOW)
        self.assertEqual(shown["status"], PENDING)
        self.assertTrue(shown["retry"]["requested"])

    def test_a_state_for_other_source_bytes_is_ignored(self):
        state = _State(self)
        state.worker_state({"status": "exhausted", "receipt": {}})
        changed = {**PRODUCT, "sections": [{"title": "结论", "body": "新版本", "gaps": []}]}
        self.assertEqual(final_text_state(state.root, changed, reviewed=False,
                                          since="2026-10-07T11:59:00+00:00", now=NOW)["status"],
                         PENDING)


class LibraryAndExportFallbackTests(unittest.TestCase):
    def _library(self, state: _State, product: dict) -> dict:
        from dalton_core.research_localization_store import localize_library

        connection = sqlite3.connect(state.root / "core.sqlite")
        self.addCleanup(connection.close)
        return localize_library(connection, {"company_ref": "company:a", "products": [product]},
                                now=NOW)["products"][0]

    def test_an_overdue_product_keeps_its_draft_sections(self):
        state = _State(self)
        shown = self._library(state, {**PRODUCT, "created_at": "2026-10-05T06:40:00+00:00"})
        self.assertEqual(shown["publication_status"], "draft")
        self.assertEqual(shown["draft_label"], DRAFT_LABEL)
        self.assertEqual(shown["final_text"]["status"], DRAFT)
        self.assertEqual(shown["sections"][0]["body"], PRODUCT["sections"][0]["body"])

    def test_a_young_product_is_still_hidden(self):
        state = _State(self)
        shown = self._library(state, dict(PRODUCT))
        self.assertEqual(shown["publication_status"], "pending_language_review")
        # 2026-10-09: the draft is shown under a badge while the review runs.
        self.assertEqual(shown["sections"][0]["body"], PRODUCT["sections"][0]["body"])
        self.assertEqual(shown["draft_label"], "初稿（待审核签发）")
        self.assertEqual(shown["final_text"]["status"], PENDING)
        self.assertTrue(shown["final_text"]["show_draft"])

    def test_export_shows_the_watermarked_draft_never_an_empty_section(self):
        from dalton_core.research_html_export import render_research_html

        state = _State(self)
        draft = self._library(state, {**PRODUCT, "created_at": "2026-10-05T06:40:00+00:00"})
        page = render_research_html(
            {"company_ref": "company:a", "products": [draft]},
            mission={"id": "m", "content_hash": "h", "title": "t",
                     "universe": [{"company_ref": "company:a", "ticker": "A"}]})
        self.assertIn("初稿（待审核签发）", page)
        self.assertIn('class="watermark"', page)
        self.assertIn("收入为", page)
        self.assertNotIn("正文正在进行语言检查", page)
        pending = self._library(state, dict(PRODUCT))
        page = render_research_html(
            {"company_ref": "company:a", "products": [pending]},
            mission={"id": "m", "content_hash": "h", "title": "t", "universe": []})
        # 2026-10-09: a product under review exports its draft under the
        # review badge instead of an empty section.
        self.assertIn("初稿（待审核签发）", page)
        self.assertIn('class="watermark"', page)
        self.assertIn("收入为", page)
        self.assertNotIn("正文正在进行语言检查", page)


class WorkerBackoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "products"
        self.prepared = 0
        self.now = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
        self.enterContext(patch.object(worker, "_now", lambda: self.now.isoformat()))

    def prepare(self, _product):
        self.prepared += 1
        return failed("database is locked")

    def poll(self):
        return poll_once(None, MISSION, state_dir=self.state, prepare=self.prepare,
                         library_reader=lambda c, m, s: {"products": [PRODUCT]})

    def test_the_delay_doubles_with_jitter_and_is_bounded(self):
        identity = "a" * 64
        for n in range(12):
            base = min(BACKOFF_RETRY_BASE_SECONDS * 2 ** n, BACKOFF_RETRY_MAX_SECONDS)
            delay = backoff_retry_delay(identity, n)
            self.assertGreaterEqual(delay, base / 2)
            self.assertLessEqual(delay, base)
            self.assertEqual(delay, backoff_retry_delay(identity, n))
        self.assertNotEqual(backoff_retry_delay("a" * 64, 0), backoff_retry_delay("b" * 64, 0))

    def test_a_lock_failure_is_retried_after_its_back_off_not_the_next_day(self):
        self.poll()
        self.assertEqual(self.poll()["parked"], 1)        # not yet due
        self.now += timedelta(seconds=BACKOFF_RETRY_BASE_SECONDS)
        self.assertEqual(self.poll()["retried"], 1)       # due within the base delay
        self.assertEqual(self.prepared, 2)
        [record] = retry_chain(self.state, worker._hash(worker._identity(PRODUCT)))
        self.assertEqual((record["trigger"], record["retry_class"]),
                         ("automatic", "database_locked"))
        self.assertIn("back-off", record["reason"])
        # The next one waits about twice as long.
        self.now += timedelta(seconds=BACKOFF_RETRY_BASE_SECONDS / 2)
        self.assertEqual(self.poll()["retried"], 0)
        self.now += timedelta(seconds=BACKOFF_RETRY_BASE_SECONDS * 2)
        self.assertEqual(self.poll()["retried"], 1)

    def test_back_off_retries_are_capped_per_utc_day(self):
        self.poll()
        for _ in range(40):
            self.now += timedelta(seconds=BACKOFF_RETRY_MAX_SECONDS)
            if self.now.date() != datetime(2026, 10, 7).date():
                break
            self.poll()
        chain = retry_chain(self.state, worker._hash(worker._identity(PRODUCT)))
        today = [r for r in chain if r["created_at"].startswith("2026-10-07")]
        self.assertLessEqual(len(today), BACKOFF_RETRY_MAX_PER_DAY)

    def test_a_lock_mixed_with_a_content_failure_waits_for_the_owner(self):
        self.prepare = lambda _p: {"status": "incomplete", "receipt": {"failures": [
            {"error": "database is locked"},
            {"error": "localized sections must match source section count"}]}}
        self.poll()
        self.now += timedelta(days=2)
        self.assertEqual(self.poll()["retried"], 0)


class AdmissionLockTests(unittest.TestCase):
    def test_the_admission_retry_backs_off_with_jitter_inside_its_deadline(self):
        from dalton_core.sqlite_contention import (
            ADMISSION_LOCK_RETRY_SECONDS, equal_jitter, retry_admission_on_sqlite_lock)

        clock = [0.0]
        waits: list[float] = []

        def sleep(seconds):
            waits.append(seconds); clock[0] += seconds

        def locked():
            clock[0] += 1.0
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            retry_admission_on_sqlite_lock(locked, sleep=sleep, monotonic=lambda: clock[0])
        self.assertGreater(len(waits), 5)
        self.assertLessEqual(clock[0], ADMISSION_LOCK_RETRY_SECONDS + 1.0)
        self.assertLessEqual(max(waits), 20.0)
        self.assertGreater(len(set(round(w, 6) for w in waits)), 3)
        for _ in range(50):
            value = equal_jitter(8.0)
            self.assertTrue(4.0 <= value <= 8.0)

    def test_a_lock_that_clears_is_admitted_on_a_later_try(self):
        from dalton_core.sqlite_contention import retry_admission_on_sqlite_lock

        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 4:
                raise sqlite3.OperationalError("database is locked")
            return {"status": "admitted"}

        self.assertEqual(retry_admission_on_sqlite_lock(flaky, sleep=lambda _s: None),
                         {"status": "admitted"})
        self.assertEqual(len(calls), 4)

    def test_the_day_or_open_set_selects_exactly_the_old_rows(self):
        from dalton_core.shared_daily_budget import DAY_OR_OPEN_ADMISSIONS_SQL, _DAY_SPEND_SQL
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        store = ThesisImpactBudgetStore(Path(temp.name) / "budget.sqlite")
        self.addCleanup(store.close)
        db = store.connection
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA defer_foreign_keys=ON")
        days = ["2026-10-05", "2026-10-06", "2026-10-07"]
        for i in range(60):
            admission = f"adm-{i}"
            day = days[i % 3]
            columns = [row[1] for row in db.execute(
                "PRAGMA table_info(thesis_impact_day_admissions)")]
            values = {name: None for name in columns}
            values.update(admission_id=admission, policy_version_id="p", day=day,
                          work_order_ref=f"w{i}", attempt_number=1, phase="assessment",
                          route_decision_ref="r", reserved_micros=100 + i,
                          record_json="{}", content_hash="h", created_at=day)
            for name in [n for n in values if values[n] is None]:
                values.pop(name)
            db.execute(f"INSERT INTO thesis_impact_day_admissions({','.join(values)}) "
                       f"VALUES ({','.join('?' * len(values))})", tuple(values.values()))
            if i % 4:
                db.execute("INSERT INTO thesis_impact_day_settlements(settlement_id,admission_id,"
                           "actual_micros,record_json,content_hash,created_at) VALUES (?,?,?,?,?,?)",
                           (f"s{i}", admission, 50 + i, "{}", "h", day))
            if i % 2:
                db.execute("INSERT INTO model_mission_budget_bindings(admission_id,mission_ref,"
                           "record_json) VALUES (?,?,?)",
                           (admission, "mission:a" if i % 3 else "mission:b", "{}"))
        old_spend = ("SELECT a.reserved_micros,COALESCE(c.corrected_micros,s.actual_micros) AS actual_micros "
                     "FROM thesis_impact_day_admissions a "
                     "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                     "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
                     "WHERE a.day=? OR s.admission_id IS NULL")
        old_mission = ("SELECT a.reserved_micros,COALESCE(c.corrected_micros,s.actual_micros) AS actual_micros "
                       "FROM thesis_impact_day_admissions a "
                       "LEFT JOIN model_mission_budget_bindings b ON a.admission_id=b.admission_id "
                       "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                       "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
                       "WHERE (b.mission_ref=? OR b.admission_id IS NULL) AND (a.day=? OR s.admission_id IS NULL)")
        new_mission = (f"WITH counted(admission_id) AS ({DAY_OR_OPEN_ADMISSIONS_SQL}) "
                       "SELECT a.reserved_micros,COALESCE(c.corrected_micros,s.actual_micros) AS actual_micros "
                       "FROM counted JOIN thesis_impact_day_admissions a ON a.admission_id=counted.admission_id "
                       "LEFT JOIN model_mission_budget_bindings b ON a.admission_id=b.admission_id "
                       "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                       "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
                       "WHERE (b.mission_ref=? OR b.admission_id IS NULL)")
        for day in days + ["2026-10-08"]:
            self.assertEqual(Counter(map(tuple, db.execute(old_spend, (day,)))),
                             Counter(map(tuple, db.execute(_DAY_SPEND_SQL, (day,)))))
            for mission in ("mission:a", "mission:b"):
                self.assertEqual(Counter(map(tuple, db.execute(old_mission, (mission, day)))),
                                 Counter(map(tuple, db.execute(new_mission, (day, mission)))))


class DisplayRoundingTests(unittest.TestCase):
    def test_machine_precision_may_be_shown_rounded(self):
        from dalton_core.research_localization import _number_differences

        self.assertEqual(_number_differences(["收入 2707409432.42083940 美元，2026 财年"],
                                             ["收入 2707409432.42 美元，2026 财年"]), ([], []))
        self.assertEqual(_number_differences(["比率 0.98123"], ["比率 0.98"]), ([], []))
        # A wrong rounding, a stated figure, or a dropped year stays refused.
        self.assertNotEqual(_number_differences(["收入 2707409432.42083940"],
                                                ["收入 2707409432.43"]), ([], []))
        self.assertNotEqual(_number_differences(["利润率 12.345"], ["利润率 12.35"]), ([], []))
        self.assertNotEqual(_number_differences(["2026 财年"], ["本财年"]), ([], []))


class GovernedRetryTests(unittest.TestCase):
    def test_the_retry_is_a_human_door_with_its_fields(self):
        from dalton_core.writer_server import (
            CORE_OPERATIONS, HUMAN_GOVERNANCE_OPERATIONS, OPERATION_ACTOR_FIELDS,
            OPERATION_FIELDS)

        name = "retry_publication_language_review"
        self.assertIn(name, HUMAN_GOVERNANCE_OPERATIONS)
        self.assertNotIn(name, CORE_OPERATIONS)
        self.assertEqual(OPERATION_ACTOR_FIELDS[name], "actor_ref")
        # 2026-10-09: ``batch_ref`` retries a blocked UI-text batch instead.
        self.assertEqual(OPERATION_FIELDS[name], frozenset({
            "identity_hash", "product_hash", "batch_ref", "reason", "idempotency_key",
            "actor_ref"}))

    def test_the_writer_op_writes_the_receipt_bound_to_the_exact_state(self):
        from dalton_core.writer_server import WriterServer

        state = _State(self)
        state.worker_state(failed("localized section changed financial number tokens: {}"))
        identity = worker._hash(worker._identity(PRODUCT))
        server = SimpleNamespace(state_dir=state.root, _now=lambda: NOW)
        op = WriterServer._op_retry_publication_language_review
        params = {"identity_hash": identity, "product_hash": worker._hash(PRODUCT),
                  "reason": "负责人要求重试", "actor_ref": "human:owner"}
        with self.assertRaises(Exception):
            op(server, {**params, "actor_ref": "automation:x"})
        with self.assertRaises(Exception):
            op(server, {**params, "product_hash": "0" * 64})
        out = op(server, params)
        self.assertEqual(out["status"], "requested")
        self.assertEqual(out["receipt"]["actor_ref"], "human:owner")
        self.assertEqual(out["receipt"]["created_at"], NOW.isoformat())
        self.assertEqual(op(server, params)["status"], "already_requested")
        [record] = retry_chain(state.products, identity)
        self.assertEqual((record["trigger"], record["reason"]), ("owner", "负责人要求重试"))


class AnswerStateTests(unittest.TestCase):
    def test_an_answer_is_ready_when_reviewed_and_never_pending(self):
        from dalton_core.cockpit_plane import CockpitPlane

        state = _State(self)
        plane = SimpleNamespace(config=SimpleNamespace(core_db=state.root / "core.sqlite"))
        ready = CockpitPlane._with_answer_state(plane, {"answer": "x", "display_answer": "答"})
        self.assertEqual(ready["final_text"]["status"], READY)
        old = CockpitPlane._with_answer_state(plane, {"answer": "x", "display_answer": None})
        self.assertEqual(old["final_text"]["status"], DRAFT)


class PageRuleTests(unittest.TestCase):
    def test_the_page_keys_every_gate_on_the_server_state(self):
        page = (Path(__file__).resolve().parents[1] / "src" / "dalton_core"
                / "cockpit_control.html").read_text(encoding="utf-8")
        self.assertIn("finalResearchReady(finalFields,finalState(r))", page)
        self.assertIn("finalResearchReady(fields,finalState(it))", page)
        self.assertIn("finalResearchText(doc.summary,finalState(doc))", page)
        self.assertIn("finalResearchText(r.answer,st)", page)
        self.assertIn('"/v1/cockpit/language_retry"', page)
        self.assertIn("重试语言质检", page)
        # Every gate call names a state; none relies on the dictionary alone.
        import re
        for call in re.findall(r"finalResearchReady\(([^()]*(?:\([^()]*\)[^()]*)*)\)", page):
            if call.startswith("values,state"):
                continue
            self.assertIn("finalState(", call)


if __name__ == "__main__":
    unittest.main()
