from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.research_publication_worker import poll_once, run_periodic


MISSION = {"id": "mission-version:1", "industry_ref": "industry:test",
           "universe": [{"company_ref": "company:a"}, {"company_ref": "company:b"}]}


def reader(connection, mission, company):
    products = [{"kind": "dossier", "subject_ref": company,
                 "version_ref": f"dossier-version:{company[-1]}",
                 "status": "available", "sections": [{"title": "结论", "body": company + " 的经营情况已有可复核的研究结论。",
                                                          "gaps": []}]}]
    if company == "company:a":
        products.append({"kind": "industry_framework", "subject_ref": "industry:test",
                         "version_ref": "framework-version:1", "status": "available",
                         "sections": [{"title":"结论","body":"本周研究结论保持稳定，未发现新的经营变化。","gaps":[]}]})
    return {"products": products}


class ResearchPublicationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_completed_hash_is_not_prepared_again_and_new_hash_is(self):
        calls = []
        prepare = lambda product: (calls.append(product["version_ref"]) or {"status": "completed"})
        first = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                          library_reader=reader)
        second = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                           library_reader=reader)
        self.assertEqual(first["completed"], 3)
        self.assertEqual(second["unchanged"], 3)
        self.assertEqual(len(calls), 3)

        def changed_reader(connection, mission, company):
            value = reader(connection, mission, company)
            if company == "company:a": value["products"][0]["sections"][0]["body"] += " changed"
            return value
        third = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                          library_reader=changed_reader)
        self.assertEqual(third["completed"], 1)
        self.assertEqual(len(calls), 4)

    def test_default_reader_explicitly_ignores_existing_localization_attachment(self):
        calls = []
        source_product = reader(None, MISSION, "company:a")["products"][0]
        def current_library(connection, mission, company, *, localize):
            self.assertFalse(localize)
            product = dict(source_product)
            if localize:
                product["localization"] = {"content_hash": "localized"}
            return {"products": [product]}
        with mock.patch(
                "dalton_core.research_publication_worker.research_library",
                side_effect=current_library):
            first = poll_once(object(), {**MISSION, "universe": [{"company_ref": "company:a"}]},
                              state_dir=self.state,
                              prepare=lambda product: (calls.append(product) or {"status": "completed"}))
            second = poll_once(object(), {**MISSION, "universe": [{"company_ref": "company:a"}]},
                               state_dir=self.state,
                               prepare=lambda product: (calls.append(product) or {"status": "completed"}))
        self.assertEqual((first["completed"], second["unchanged"]), (1, 1))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("localization", calls[0])

    def test_a_product_with_an_empty_source_section_is_not_prepared(self):
        # 2026-10-09 (batch am): no Chinese attachment for an empty section.
        calls = []
        def empty_reader(connection, mission, company):
            value = reader(connection, mission, company)
            if company == "company:a":
                value["products"][0]["sections"].append({"title": "估值", "body": "  ", "gaps": []})
            return value
        result = poll_once(object(), MISSION, state_dir=self.state,
                           prepare=lambda product: (calls.append(product["version_ref"]) or {"status": "completed"}),
                           library_reader=empty_reader)
        self.assertNotIn("dossier-version:a", calls)
        self.assertEqual(result["empty_source"], 1)
        refused = [row for row in result["products"] if row["status"] == "skipped"]
        self.assertEqual(refused[0]["empty_sections"], ["估值"])
        self.assertEqual(refused[0]["skip_reason"], "source_section_empty")
        self.assertTrue(refused[0]["parked"])
        self.assertEqual(result["completed"], 2)
        # Batch C2: the skip is recorded (no Chinese attachment, no spend).
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["skipped_products"][0]["empty_sections"], ["估值"])
        states = [json.loads(path.read_text()) for path in self.state.glob("*.json")
                  if "dossier-version:a" in path.read_text()]
        self.assertEqual([row["status"] for row in states], ["skipped"])
        self.assertEqual(states[0]["result"]["skip_reason"], "source_section_empty")
        self.assertNotIn("localization", json.dumps(states[0]))

    def test_an_empty_section_skip_is_never_retried_or_rewritten(self):
        from dalton_core.research_publication_worker import (
            _read_state, automatic_retry_due, awaiting_owner, request_retry)

        calls = []
        def empty_reader(connection, mission, company):
            value = reader(connection, mission, company)
            if company == "company:a":
                value["products"][0]["sections"][0]["body"] = ""
            return value
        prepare = lambda product: (calls.append(product["version_ref"]) or {"status": "completed"})
        poll_once(object(), MISSION, state_dir=self.state, prepare=prepare, library_reader=empty_reader)
        path = next(path for path in self.state.glob("*.json") if "dossier-version:a" in path.read_text())
        before = path.read_bytes()
        again = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                          library_reader=empty_reader)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(again["skipped"], 1)
        self.assertNotIn("dossier-version:a", calls)
        state = _read_state(path)
        self.assertIsNone(automatic_retry_due(state, []))
        self.assertFalse(awaiting_owner(state, []))
        with self.assertRaisesRegex(ValueError, "source_section_empty"):
            request_retry(self.state, path.stem, actor_ref="human:owner", reason="again")
        # A redraft with a body is a new hash and is prepared.
        poll_once(object(), MISSION, state_dir=self.state, prepare=prepare, library_reader=reader)
        self.assertIn("dossier-version:a", calls)

    def test_a_substance_gate_blocker_is_skipped_before_prepare(self):
        # Live 10-09: a contract product with a heading-only section passed
        # the body screen and raised inside ``prepare`` (build_prompt).
        calls = []
        def blockers(product):
            if product.get("version_ref") == "dossier-version:a":
                return [{"code": "heading_only", "title": "结论"}]
            return []
        with mock.patch("dalton_core.research_localization.localization_blockers", side_effect=blockers):
            result = poll_once(object(), MISSION, state_dir=self.state,
                               prepare=lambda product: (calls.append(product["version_ref"])
                                                        or {"status": "completed"}),
                               library_reader=reader)
        self.assertNotIn("dossier-version:a", calls)
        skipped = [row for row in result["products"] if row["status"] == "skipped"]
        self.assertEqual(skipped[0]["blockers"], [{"code": "heading_only", "title": "结论"}])
        self.assertEqual((result["pending"], result["completed"]), (0, 2))

    def test_a_blocked_prepare_is_a_recorded_skip_not_a_pending_failure(self):
        from dalton_core.research_localization import ResearchLocalizationError

        def prepare(product):
            if product["subject_ref"] == "company:a":
                raise ResearchLocalizationError(
                    "a product with a failed section is not localised: 结论（empty_source_section）")
            return {"status": "completed"}
        result = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                           library_reader=reader)
        self.assertEqual((result["pending"], result["skipped"], result["completed"]), (0, 1, 2))
        states = {json.loads(path.read_text())["status"] for path in self.state.glob("*.json")}
        self.assertEqual(states, {"skipped", "completed"})

    def test_a_skipped_product_is_a_redraft_card_without_a_retry_button(self):
        from dalton_core.needs_human import parked_publications
        from dalton_core.research_publication_worker import _hash

        def empty_reader(connection, mission, company):
            value = reader(connection, mission, company)
            if company == "company:a":
                value["products"][0]["sections"].append({"title": "估值", "body": "", "gaps": []})
            return value
        work = self.state / "publication"
        products = work / "products"
        result = poll_once(object(), MISSION, state_dir=products,
                           prepare=lambda product: {"status": "completed"}, library_reader=empty_reader)
        (work / "worker-last-run.json").write_text(json.dumps(result))
        with mock.patch("dalton_core.final_text_state.publication_work_dir", return_value=work):
            cards = parked_publications(self.state, {"company:a": "AAA"})
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertTrue(card["ref"].startswith("publication-skipped:"))
        self.assertIn("源稿有空节", card["title"])
        self.assertIn("估值", card["title"])
        self.assertNotIn("retry", card["detail"])
        self.assertEqual(card["detail"]["skip_reason"], "source_section_empty")
        self.assertEqual(card["detail"]["identity_hash"],
                         _hash(result["skipped_products"][0]["identity"]))

    def test_failure_is_pending_and_does_not_block_other_products(self):
        calls = []
        def prepare(product):
            calls.append(product["subject_ref"])
            if product["subject_ref"] == "company:a": raise RuntimeError("boom")
            return {"status": "completed"}
        result = poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                           library_reader=reader)
        self.assertEqual((result["pending"], result["completed"]), (1, 2))
        states = [json.loads(path.read_text()) for path in self.state.glob("*.json")]
        self.assertEqual({row["status"] for row in states}, {"pending", "completed"})
        poll_once(object(), MISSION, state_dir=self.state, prepare=prepare,
                  library_reader=reader)
        self.assertEqual(len(calls), 3)

    def test_periodic_obeys_max_loops_without_spending_on_unchanged_hashes(self):
        calls = []
        result = run_periodic(
            object(), MISSION, state_dir=self.state,
            prepare=lambda product: (calls.append(1) or {"status": "completed"}),
            stop_event=threading.Event(), interval_seconds=0.001, max_loops=2,
            library_reader=reader)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(calls), 3)

    def test_extra_surface_reader_joins_the_same_hash_state_machine(self):
        calls = []
        extra = lambda connection, mission, company: [{
            "kind": "surface_event_judgement", "subject_ref": company,
            "version_ref": "judgement:1", "status": "available", "sections": [{"title":"结论","body":"本周研究结论保持稳定，未发现新的经营变化。","gaps":[]}]}]
        result = poll_once(
            object(), {**MISSION, "universe": [{"company_ref": "company:a"}]},
            state_dir=self.state,
            prepare=lambda product: (calls.append(product["kind"]) or {"status": "completed"}),
            library_reader=lambda *args: {"products": []}, extra_reader=extra)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(calls, ["surface_event_judgement"])


class PublicationDebounceTests(unittest.TestCase):
    """2026-10-07: superseded dossier versions are not paid for."""

    NOW = 1_800_000_000.0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def _reader(self, created_at):
        from datetime import datetime, timezone

        stamp = datetime.fromtimestamp(created_at, timezone.utc).isoformat()

        def read(connection, mission, company):
            return {"products": [{
                "kind": "dossier", "subject_ref": company,
                "version_ref": f"dossier-version:{company[-1]}:{int(created_at)}",
                "status": "available", "created_at": stamp, "sections": [{"title":"结论","body":"本周研究结论保持稳定，未发现新的经营变化。","gaps":[]}]}]}
        return read

    def test_a_version_younger_than_the_window_is_settling_not_prepared(self):
        calls = []
        result = poll_once(
            object(), MISSION, state_dir=self.state,
            prepare=lambda product: calls.append(product) or {"status": "completed"},
            library_reader=self._reader(self.NOW - 600),
            stable_window_seconds=7200, clock=lambda: self.NOW)
        self.assertEqual(calls, [])
        self.assertEqual(result["settling"], 2)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(list(self.state.glob("*.json")), [])
        row = result["products"][0]
        self.assertEqual(row["status"], "settling")
        self.assertEqual(row["stable_in_seconds"], 6600)

    def test_a_version_stable_for_the_window_is_prepared(self):
        calls = []
        result = poll_once(
            object(), MISSION, state_dir=self.state,
            prepare=lambda product: calls.append(product) or {"status": "completed"},
            library_reader=self._reader(self.NOW - 7201),
            stable_window_seconds=7200, clock=lambda: self.NOW)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["settling"], 0)

    def test_a_version_superseded_before_its_turn_is_skipped(self):
        calls = []
        result = poll_once(
            object(), MISSION, state_dir=self.state,
            prepare=lambda product: calls.append(product["subject_ref"]) or {
                "status": "completed"},
            library_reader=self._reader(self.NOW - 9000),
            still_current=lambda product, company: company != "company:a",
            stable_window_seconds=7200, clock=lambda: self.NOW)
        self.assertEqual(calls, ["company:b"])
        self.assertEqual(result["superseded"], 1)
        self.assertEqual(result["completed"], 1)

    def test_priority_order_is_kept_among_stable_products(self):
        calls = []
        reader = self._reader(self.NOW - 9000)

        def priority(product):
            return 0 if product["subject_ref"] == "company:b" else 1

        poll_once(object(), MISSION, state_dir=self.state,
                  prepare=lambda product: calls.append(product["subject_ref"]) or {
                      "status": "completed"},
                  library_reader=reader, priority=priority,
                  stable_window_seconds=7200, clock=lambda: self.NOW)
        self.assertEqual(calls, ["company:b", "company:a"])

    def test_the_worker_config_accepts_and_bounds_the_window(self):
        from dalton_core.research_output_preparation import validate_worker_config

        base = {"schema_version": "research-publication-worker-config:0.1",
                "workers": 2, "chunk_chars": 4500, "max_cost_per_call": 1,
                "draft_attempts": 2, "publication_gate": {},
                **{name: "/abs/" + name for name in (
                    "core_db", "scheduler_db", "model_config", "verifier_config",
                    "checker_config", "brain_config", "work_dir", "output_directory")}}
        validate_worker_config({**base, "stable_window_seconds": 7200})
        validate_worker_config(base)
        with self.assertRaises(ValueError):
            validate_worker_config({**base, "stable_window_seconds": -1})
