"""ADR-0011: the Investment Memo drafts from a bounded view of its authorities."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from dalton_core.investment_memo_cli import anchor_bindings, collect_frozen_input
from dalton_core.investment_memo_draft import MAX_INPUT_TOKENS, build_group_prompt
from dalton_core.investment_memo_material import (
    MATERIAL_BUDGET_BYTES, bound_material, material_bytes,
)
from dalton_core.store import DaltonStore, content_hash

COMPANY = "company:sec-cik:0000000001"


def row(ref, kind, record):
    return {"ref": ref, "hash": content_hash(record), "kind": kind,
            "text": json.dumps(record, ensure_ascii=False, sort_keys=True)}


def live_sized_rows(claims=2900, events=1500):
    """Roughly the live ACN collection: megabytes, not kilobytes."""

    rows = []
    for n in range(claims):
        quantitative = n % 10 == 0
        rows.append(row(f"claim-version:{n:05d}", "claim", {
            "id": f"claim-version:{n:05d}", "subject_ref": COMPANY,
            "claim_kind": "quantitative" if quantitative else "qualitative",
            "normalized_statement": f"Statement {n} " + "detail " * 60,
            "metric_or_aspect": "quarterly_revenue_yoy_growth" if quantitative else "demand",
            "period": "2026-03-01..2026-05-31", "value": "7.2" if quantitative else None,
            "created_at": f"2026-09-{1 + n % 28:02d}T00:00:{n % 60:02d}+00:00",
            "evidence": ["x" * 400]}))
    for n in range(events):
        rows.append(row(f"event:{n}", "research_events", {
            "event_id": f"event:{n}", "occurred_at": f"2026-09-{1 + n % 28:02d}",
            "title": "event " * 40}))
    big = {"sections": [{"title": f"s{n}", "body": "body " * 2000} for n in range(30)]}
    rows.append(row("dossier:v1", "company_dossier_versions", {"id": "dossier:v1", **big}))
    rows.append(row("gate:v2", "deep_insight_gate_versions", {
        "id": "gate:v2", "answers": [{"question_ref": f"q{n}", "judgement": "j " * 900}
                                     for n in range(12)]}))
    rows.append(row("model:v7", "forecast_model_versions", {
        "id": "model:v7", "results": [{"cells": [{"v": "1" * 50}] * 40}] * 20,
        "assumptions": [{"because": "b " * 100, "refs": ["r"]}] * 30}))
    for n in range(160):
        ref = (f"result:revenue@2027-{n:03d}:estimate" if n < 8 else
               f"result:operating_expense:x@{n}")
        rows.append(row(ref, "forecast_cell",
                        {"ref": ref, "value": "123.4", "kind": "estimate"}))
    rows.append(row("sens:v1", "sensitivity_projections", {"id": "sens:v1",
                                                          "drivers": [{"x": "y" * 900}] * 40}))
    rows.append(row("val:v1", "valuation_snapshot_versions", {
        "id": "val:v1", "fundamental_windows": ["w" * 5000] * 10,
        "metrics": [{"metric": "trailing_pe", "status": "available", "value": "46"}]}))
    rows.append(row("framework:v30", "industry_framework", {
        "id": "framework:v30", "sections": [{"body": "b " * 3000}] * 10,
        "cross_company_comparison": {"cells": [
            {"company_ref": ref, "metric": "revenue", "quarter": "2026Q2", "value": "1",
             "status": "computed", "inputs": ["i" * 300]}
            for ref in (COMPANY, "company:other")]}}))
    return rows


class BoundMaterialTests(unittest.TestCase):
    def test_live_sized_collection_fits_one_call_and_keeps_what_the_memo_needs(self):
        rows = live_sized_rows()
        self.assertGreater(material_bytes(rows), 3_000_000)
        bounded = bound_material(rows, company_ref=COMPANY)
        self.assertLessEqual(material_bytes(bounded), MATERIAL_BUDGET_BYTES + 2_000)
        kinds = {item["kind"] for item in bounded}
        for kind in ("company_dossier_versions", "deep_insight_gate_versions",
                     "forecast_model_versions", "sensitivity_projections",
                     "valuation_snapshot_versions", "industry_framework"):
            self.assertIn(kind, kinds)
        # A producer prompt over the bounded material is under the call bound.
        prompt = build_group_prompt(group="view", section_titles=["S3"], questions=[],
                                    material=bounded, company={"company_ref": COMPANY})
        self.assertLess(len(prompt.encode("utf-8")), MAX_INPUT_TOKENS)
        # Every row keeps the exact ref and the full record's hash.
        originals = {item["ref"]: item["hash"] for item in rows}
        for item in bounded:
            self.assertEqual(item["hash"], originals[item["ref"]])
        claims = [item for item in bounded if item["kind"] == "claim"]
        self.assertGreater(len(claims), 30)
        self.assertEqual(json.loads(claims[0]["text"]).get("claim_kind"), "quantitative")
        # Only the key result lines' forecast cells, and only eight events.
        cells = [item["ref"] for item in bounded if item["kind"] == "forecast_cell"]
        self.assertTrue(cells and all(ref.startswith("result:revenue@") for ref in cells))
        self.assertLessEqual(sum(1 for item in bounded if item["kind"] == "research_events"), 8)
        framework = json.loads(next(item["text"] for item in bounded
                                    if item["kind"] == "industry_framework"))
        self.assertEqual({cell["company_ref"] for cell in
                          framework["cross_company_comparison"]["cells"]}, {COMPANY})
        # Deterministic.
        self.assertEqual(bound_material(rows, company_ref=COMPANY), bounded)

    def test_small_collection_is_kept_whole(self):
        rows = [row("claim-version:1", "claim", {"id": "claim-version:1",
                                                 "normalized_statement": "Revenue rose 12%.",
                                                 "context": "constant currency"})]
        bounded = bound_material(rows, company_ref=COMPANY)
        self.assertEqual([item["ref"] for item in bounded], ["claim-version:1"])
        self.assertEqual(json.loads(bounded[0]["text"])["context"], "constant currency")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.mission = {
            "id": "mission:v2", "mission_ref": "mission:coverage", "content_hash": "a" * 64,
            "industry_ref": "industry:it",
            "bindings": {"playbook_version": {"ref": "playbook:v1", "hash": "b" * 64}},
            "deliverables": ["investment_memo"],
            "autonomy": {"may_write": ["deliverable"], "human_checkpoints": ["investment_memo"]},
            "universe": [{"company_ref": COMPANY, "ticker": "AAA"},
                         {"company_ref": "company:b", "ticker": "BBB"}]}
        with self.store._transaction() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS coverage_mission_pointer(mission_ref TEXT)")
            cur.execute("INSERT INTO coverage_mission_pointer VALUES('mission:coverage')")
            cur.execute("CREATE TABLE IF NOT EXISTS coverage_mission_versions("
                        "mission_version_id TEXT, mission_ref TEXT)")
            for version in ("mission:v1", "mission:v2"):
                cur.execute("INSERT INTO coverage_mission_versions(mission_version_id,mission_ref) "
                            "VALUES(?,?)", (version, "mission:coverage"))
            cur.execute("CREATE TABLE deep_insight_gate_versions(version_id TEXT,company_ref TEXT,"
                        "version_number INTEGER,record_json TEXT,content_hash TEXT)")
            cur.execute("CREATE TABLE deep_insight_gate_decisions(gate_version_ref TEXT,decision TEXT)")
            # The real Deep Insight Gate record shape: the mission version is
            # under ``bindings``, and here it is an *older* version.
            gate = {"id": "gate:aaa:2", "bindings": {"mission_version_ref": "mission:v1"}}
            cur.execute("INSERT INTO deep_insight_gate_versions VALUES(?,?,?,?,?)",
                        (gate["id"], COMPANY, 2, json.dumps(gate), content_hash(gate)))
            cur.execute("INSERT INTO deep_insight_gate_decisions VALUES(?, 'approve')", (gate["id"],))
        self.states = {COMPANY: {"current_stage": "company_model", "current_status": "gate_passed",
                                 "next_stage": "investment_memo"},
                       "company:b": {"current_stage": "company_model", "current_status": "entered",
                                     "next_stage": "company_model"}}
        self.latest_memo = None

    def collect(self, **kwargs):
        mission, states, test = self.mission, self.states, self

        class Missions:
            def active_mission(self, _ref): return mission
            def current_stage_state(self, _ref, company): return states[company]

        class Playbooks:
            def playbook(self, _ref): return {"id": "playbook:v1", "content_hash": "b" * 64}

        class Deliverables:
            def live_claim_version_refs(self): return set()
            def deliverables(self, _ref): return []
            def latest(self, _ref): return test.latest_memo

        def reader(record):
            return type("Reader", (), {"latest": lambda self, c: record,
                                       "latest_version": lambda self, c: record})()

        model = {"id": "model:v7", "company_ref": COMPANY, "content_hash": "e" * 64,
                 "inputs_hash": "f" * 64, "results": []}
        projection = {"id": "sens:v1", "company_ref": COMPANY, "content_hash": "2" * 64,
                      "model_version_ref": "model:v7", "model_version_hash": "e" * 64,
                      "inputs_hash": "f" * 64}
        framework = {"id": "framework:v30", "content_hash": "3" * 64,
                     "industry_ref": "industry:it",
                     "bindings": {"mission_version_ref": "mission:v1"}}
        patches = [
            patch("dalton_core.investment_memo_cli.CoverageMissionAuthority", return_value=Missions()),
            patch("dalton_core.investment_memo_cli.ResearchPlaybookAuthority", return_value=Playbooks()),
            patch("dalton_core.investment_memo_cli.MissionDeliverableAuthority", return_value=Deliverables()),
            patch("dalton_core.company_dossier.CompanyDossierAuthority",
                  return_value=reader({"id": "dossier:v1", "content_hash": "d" * 64})),
            patch("dalton_core.model_forecast_driver.ForecastModelAuthority", return_value=reader(model)),
            patch("dalton_core.valuation_snapshot.ValuationSnapshotAuthority",
                  return_value=reader({"id": "val:v1", "content_hash": "1" * 64})),
            patch("dalton_core.forecast_sensitivity.SensitivityProjectionAuthority",
                  return_value=reader(projection)),
            patch("dalton_core.investment_memo_cli.latest_industry_framework",
                  return_value=framework),
        ]
        for item in patches:
            item.start()
        try:
            return collect_frozen_input(self.store, **kwargs)
        finally:
            for item in patches:
                item.stop()

    def test_real_gate_shape_and_framework_authority_make_the_memo_ready(self):
        frozen = self.collect()
        self.assertEqual(frozen["status"], "ready", frozen)
        kinds = {item["kind"] for item in frozen["input_bindings"]}
        self.assertIn("industry_framework", kinds)
        self.assertIn("deep_insight_gate_versions", kinds)

    def test_an_unchanged_anchor_is_not_redrafted_when_claims_move(self):
        frozen = self.collect()
        stale_claims = [*frozen["input_bindings"],
                        {"kind": "claim", "ref": "claim-version:old", "hash": "0" * 64}]
        self.latest_memo = {"mission_version_ref": "mission:v1",
                            "gate": {"input_bindings": stale_claims}}
        again = self.collect(company_ref=COMPANY)
        self.assertEqual(again.get("memo_status"), "nothing_new", again)
        self.assertEqual(anchor_bindings(stale_claims), anchor_bindings(frozen["input_bindings"]))

    def test_waiting_companies_say_where_they_are_in_chinese(self):
        waiting = self.collect(company_ref="company:b")
        self.assertEqual(waiting["status"], "idle")
        self.assertEqual(waiting["reason"], "BBB 停在深度模型构建（进行中），等深度模型构建通过审核")
        with self.store._transaction() as cur:
            cur.execute("DELETE FROM deep_insight_gate_decisions")
        held = self.collect(company_ref=COMPANY)
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "AAA：缺少你已批准的深度认知门（当前研究任务下）")


class LaneReasonTests(unittest.TestCase):
    def test_idle_reason_names_what_each_company_waits_on(self):
        import tempfile
        from pathlib import Path

        from dalton_core.mission_investment_memo_lane import MissionInvestmentMemoLaneCoordinator

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launcher = type("L", (), {"state_dir": root,
                                      "model_config_path": root / "p.json",
                                      "verifier_model_config_path": root / "v.json"})()
            reasons = {"company:a": {"status": "idle",
                                     "reason": "AAA 停在行业模型（进行中），等深度模型构建通过审核"},
                       "company:b": {"status": "held",
                                     "reason": "BBB：缺少你已批准的深度认知门（当前研究任务下）"}}
            coordinator = MissionInvestmentMemoLaneCoordinator(
                connection=None, launcher=launcher, companies=lambda: list(reasons),
                frozen_input=lambda company: reasons[company])
            result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"],
                         "投资备忘录等待：AAA 停在行业模型（进行中），等深度模型构建通过审核；"
                         "BBB：缺少你已批准的深度认知门（当前研究任务下）")


if __name__ == "__main__":
    unittest.main()
