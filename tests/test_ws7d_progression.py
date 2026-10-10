"""2026-10-08 (ws-7d): why the hyperscaler companies were stuck, as tests.

1. A transcript search that found its quarter moves on to the next missing
   quarter at once; a search whose quarter is still missing waits the retry
   interval, and the tick reason names the quarters still missing.
2. The industry framework redrafts when the model-stage gate names a
   deficiency a redraft can fix *and* its inputs moved; the comparison covers
   every company with filed statements; the gap list always names the
   high-frequency substitute.
3. A debate map whose verifier faults one debate publishes the others, and a
   held run that never called a model does not count as a failed draft.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dalton_core.mission_source_discovery import (
    MissionSourceDiscoveryCoordinator,
    build_discovery_plan,
    missing_periods_note,
    search_blocked_reason,
)
from tests.test_mission_source_discovery import (
    NOW,
    CoordinatorTests,
    FakeSearchLauncher,
)

TRANSCRIPTS = "earnings-call-transcripts"


class _Selection:
    consumed: list = []

    def currently_consumed(self, **kwargs):
        return list(self.consumed)


class TranscriptNextQuarterTests(unittest.TestCase):
    setUp = CoordinatorTests.setUp
    create_mission = CoordinatorTests.create_mission
    mission_v2 = CoordinatorTests.mission_v2

    def coordinator_for(self, company_ref):
        plan = build_discovery_plan(
            plan_id="discovery-plan:quarters:test",
            created_at=NOW.isoformat(timespec="microseconds"),
            mission_ref=self.plan["mission_ref"], source_ref="source:alphaengine",
            max_calls_24h=30,
            companies={company_ref: {"name": "Alphabet", "ticker": "GOOGL",
                                     "aliases": ["Alphabet", "Google"]}},
            specs=[{"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes",
                    "query_variants": [{"query_template": "{name} {quarter_label}",
                                        "filters": {}},
                                       {"query_template": "{aliases}", "filters": {}}],
                    "lookback_days": 400, "rediscovery_interval_days": 7,
                    "retry_interval_days": 1}])
        search = FakeSearchLauncher(self.h, self.missions, plan)
        coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=plan,
            search_launcher=search, acquisition_launcher=None, clock=self.clock,
            selection_launcher=_Selection())
        return coordinator, search

    def run_with(self, coordinator, state, call):
        def item(mission, company_ref, spec):
            return {"missing_periods": list(state["missing"]),
                    "candidate_periods": list(state.get("candidates", ())),
                    "required_periods": ["FY2025-Q3", "FY2025-Q4", "FY2026-Q1", "FY2026-Q2"]}
        with patch.object(coordinator, "_period_item", side_effect=item), \
                patch.object(coordinator, "_satisfied_block", return_value=None):
            return call()

    def transcript_skip(self, result, company_ref):
        return next(row["reason"] for row in result["skipped"]
                    if row.get("company_ref") == company_ref
                    and row.get("spec_ref") == TRANSCRIPTS)

    def test_a_found_quarter_moves_the_search_to_the_next_missing_quarter_at_once(self):
        mission = self.mission_v2(self.create_mission())
        company = mission["universe"][0]["company_ref"]
        coordinator, search = self.coordinator_for(company)
        state = {"missing": ["FY2026-Q1", "FY2026-Q2"]}

        first = self.run_with(coordinator, state, coordinator.launch_discovery)
        self.assertEqual(first["status"], "launched", first)
        self.assertEqual(search.starts[-1]["missing_periods"], ["FY2026-Q1", "FY2026-Q2"])
        coordinator.settle_dispatches()

        # Same quarter still missing: the shortfall retry holds it, and the
        # reason names what is still missing and what the next search asks.
        held = self.run_with(coordinator, state, coordinator.launch_discovery)
        self.assertEqual(held["status"], "idle", held)
        reason = self.transcript_skip(held, company)
        self.assertIn("shortfall retry", reason)
        self.assertIn("interval 1d", reason)
        self.assertIn("仍缺 FY2026-Q1、FY2026-Q2", reason)
        self.assertIn("下一次搜 Q1 2026", reason)

        # Q1 was acquired: the next quarter is a new question, due now.
        state["missing"] = ["FY2026-Q2"]
        moved = self.run_with(coordinator, state, coordinator.launch_discovery)
        self.assertEqual(moved["status"], "launched", moved)
        self.assertEqual(search.starts[-1]["missing_periods"], ["FY2026-Q2"])
        self.assertEqual(search.starts[-1]["variant_index"], 0)
        coordinator.settle_dispatches()

        # Q2 not found by that search: the retry interval, never the week.
        held = self.run_with(coordinator, state, coordinator.launch_discovery)
        self.assertEqual(held["status"], "idle", held)
        reason = self.transcript_skip(held, company)
        self.assertIn("rediscovered (shortfall retry)", reason)
        self.assertNotIn("interval 7d", reason)
        self.clock.advance(days=1, minutes=5)
        again = self.run_with(coordinator, state, coordinator.launch_discovery)
        self.assertEqual(again["status"], "launched", again)

    def test_a_missing_quarter_with_a_found_candidate_waits_for_selection(self):
        mission = self.mission_v2(self.create_mission())
        company = mission["universe"][0]["company_ref"]
        coordinator, search = self.coordinator_for(company)
        state = {"missing": ["FY2026-Q1", "FY2026-Q2"]}
        self.run_with(coordinator, state, coordinator.launch_discovery)
        coordinator.settle_dispatches()
        state.update(missing=["FY2026-Q2"], candidates=["FY2026-Q2"])
        held = self.run_with(coordinator, state, coordinator.launch_discovery)
        self.assertEqual(held["status"], "idle", held)
        reason = self.transcript_skip(held, company)
        self.assertIn("FY2026-Q2 已找到候选", reason)
        self.assertEqual(len(search.starts), 1)

    def test_an_unreadable_checklist_is_not_a_missing_quarter(self):
        mission = self.mission_v2(self.create_mission())
        company = mission["universe"][0]["company_ref"]
        coordinator, _search = self.coordinator_for(company)
        spec = coordinator.plan["specs"][0]
        with patch.object(coordinator, "_period_item", side_effect=RuntimeError("locked")):
            self.assertEqual(coordinator._period_state(mission, company, spec),
                             ((), frozenset()))


class BlockedReasonTests(unittest.TestCase):
    def test_the_note_names_the_missing_quarters(self):
        self.assertEqual(missing_periods_note([]), "")
        self.assertEqual(missing_periods_note(["FY2026-Q4"]),
                         "；仍缺 FY2026-Q4，下一次搜 Q4 2026")

    def test_entries_with_a_missing_quarter_lead_the_200_character_line(self):
        skipped = [
            {"company_ref": "company:ticker:amzn", "spec_ref": "sell-side-reports",
             "reason": "broker_research already holds 45 of 3 required; not searching past 6"},
            {"company_ref": "company:ticker:amzn", "spec_ref": TRANSCRIPTS,
             "reason": "rediscovered 0d ago; interval 7d"},
            {"company_ref": "company:ticker:msft", "spec_ref": TRANSCRIPTS,
             "reason": "rediscovered (shortfall retry) 0d ago; interval 1d；仍缺 FY2026-Q4"},
        ]
        line = search_blocked_reason({"status": "idle", "skipped": skipped})
        self.assertTrue(line.startswith("msft/earnings-call-transcripts"), line)


# ---------------------------------------------------------------------------
# 2. the industry framework
# ---------------------------------------------------------------------------

from dalton_core.industry_framework import assess_gaps, build_comparison  # noqa: E402
from dalton_core.industry_framework_cli import stale_units  # noqa: E402
from dalton_core.model_stage_readiness import industry_model_readiness  # noqa: E402


class FrameworkGapTests(unittest.TestCase):
    POLICY = {
        "schema_version": "0.1", "policy_ref": "industry-framework-policy:x:v1",
        "causal_chain_titles": [], "driver_horizons": [], "output_rubric_bindings": [],
        "gap_checklist": [{
            "gap_ref": "gap:x:mission-specific-evidence", "label": "Mission evidence",
            "what_is_missing": "Evidence not collected.", "content_kind": "primary_source",
            "driver_refs": [], "cost_note": "Authorized sources only.", "blocks_links": [0],
        }],
    }

    def test_a_policy_without_a_high_frequency_gap_gets_the_standard_one(self):
        gaps = assess_gaps(self.POLICY)
        refs = [gap["gap_ref"] for gap in gaps]
        self.assertEqual(refs, ["gap:x:mission-specific-evidence",
                                "gap:high-frequency-demand"])
        hf = gaps[-1]
        self.assertEqual(hf["status"], "open")
        self.assertTrue(hf["candidate_sources"])
        self.assertIn("替代", hf["cost_note"])

    def test_a_policy_with_its_own_high_frequency_gap_keeps_it(self):
        policy = dict(self.POLICY)
        policy["gap_checklist"] = [dict(self.POLICY["gap_checklist"][0],
                                        gap_ref="gap:high-frequency-demand",
                                        label="Own", content_kind="web_page")]
        gaps = assess_gaps(policy)
        self.assertEqual([gap["label"] for gap in gaps], ["Own"])


def _table(company_ref, *, revenue, cost=None, spec_ref=None):
    def line(concept, values):
        return {"concept": concept, "status": "filed", "period_basis": "duration",
                "cells": {end: {"value": value, "unit": "USD", "basis": "reported",
                                "source_accessions": [f"acc-{company_ref}-{end}"]}
                          for end, value in values.items()}}
    lines = [line("us-gaap:Revenues", revenue)]
    if cost:
        lines.append(line("us-gaap:CostOfRevenue", cost))
    return {"company_ref": company_ref, "spec_ref": spec_ref, "state_hash": None,
            "filed_lines": lines}


class ComparisonCoverageTests(unittest.TestCase):
    ENDS = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
            "2026-03-31", "2026-06-30"]

    def test_statement_only_companies_complete_the_peer_comparison(self):
        from dalton_core.industry_framework_cli import model_input_tables

        class Missions:
            def latest_company_model_spec(self, company_ref):
                return None

        universe = [{"company_ref": "company:ticker:googl", "ticker": "GOOGL"},
                    {"company_ref": "company:ticker:msft", "ticker": "MSFT"}]
        revenue = {end: 100 + index for index, end in enumerate(self.ENDS)}
        cost = {end: 40 for end in self.ENDS}

        def statement_table(missions, company_ref):
            return _table(company_ref, revenue=revenue, cost=cost)

        with patch("dalton_core.industry_framework_cli.statement_comparison_table",
                   side_effect=statement_table):
            tables = model_input_tables(Missions(), {"universe": universe})
        self.assertEqual([table["company_ref"] for table in tables],
                         ["company:ticker:googl", "company:ticker:msft"])
        comparison = build_comparison(tables, universe=universe)
        companies = {row["company_ref"] for row in comparison["companies"]}
        self.assertEqual(companies, {"company:ticker:googl", "company:ticker:msft"})
        self.assertTrue(any("filed statements" in note
                            for note in comparison["comparability_notes"]))
        framework = {
            "id": "f", "industry_ref": "industry:x", "evidence_refs": [{"ref": "r"}],
            "sections": [], "bindings": {"mission_version_ref": "m:1"},
            "industry_characteristics": {"status": "drafted", "sources": [1]},
            "long_term_drivers": {"status": "drafted", "sources": [1]},
            "short_term_drivers": {"status": "drafted", "sources": [1]},
            "cross_company_comparison": comparison,
            "gaps": assess_gaps(FrameworkGapTests.POLICY),
        }
        verdict = industry_model_readiness(
            framework, mission={"id": "m:1", "industry_ref": "industry:x",
                                "universe": universe})
        self.assertNotIn("five_company_revenue_growth_margin_comparison",
                         verdict["reasons"])
        self.assertNotIn("unconnected_inputs_explicitly_deferred", verdict["reasons"])

    def test_statement_comparison_table_reads_filed_lines(self):
        from dalton_core.industry_framework_cli import statement_comparison_table

        rows = {"us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": [
            {"statement": "income", "concept":
             "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "label": "Revenue", "period_start": "2026-04-01", "period_end": "2026-06-30",
             "value": "1000", "unit": "USD", "is_breakdown": 0, "dimension_axis": None,
             "dimension_member": None, "dimension_count": 0, "filed": "2026-07-30",
             "accession": "0001-26-000001", "report_date": "2026-06-30",
             "filing_form": "10-Q", "balance": None, "level": 0,
             "parent_concept": None, "ordinal": 1}]}

        class Missions:
            def statement_series_lines(self, company_ref, concept):
                return rows.get(concept, [])

        table = statement_comparison_table(Missions(), "company:ticker:msft")
        self.assertEqual(table["company_ref"], "company:ticker:msft")
        self.assertIsNone(table["spec_ref"])
        concepts = [line["concept"] for line in table["filed_lines"]]
        self.assertEqual(concepts,
                         ["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"])
        self.assertIn("2026-06-30", table["filed_lines"][0]["cells"])
        self.assertIsNone(statement_comparison_table(type("M", (), {
            "statement_series_lines": lambda self, c, k: []})(), "company:ticker:x"))


class StaleUnitOrderTests(unittest.TestCase):
    def test_a_never_drafted_unit_outranks_a_refresh(self):
        plan = {
            "causal_chain:0": {"status": "draftable", "stale": True, "new_refs": 40,
                               "drafted": True},
            "characteristics": {"status": "draftable", "stale": True, "new_refs": 30,
                                "drafted": True},
            "long_term_drivers": {"status": "draftable", "stale": True, "new_refs": 20,
                                  "drafted": True},
            "short_term_drivers": {"status": "draftable", "stale": True, "new_refs": 5,
                                   "drafted": False},
        }
        self.assertEqual(stale_units(plan, limit=3)[0], "short_term_drivers")


# ---------------------------------------------------------------------------
# 2b. the framework lane's gate trigger
# ---------------------------------------------------------------------------

from dalton_core.mission_industry_framework_lane import (  # noqa: E402
    GATE_REDRAFT_SECONDS,
    MissionIndustryFrameworkLaneCoordinator,
)


class _Launcher:
    def __init__(self):
        self.started = []
        self.tickets = {}

    def start(self, *, signature, industry_ref=None):
        ticket = {"id": f"t:{len(self.started) + 1}", "signature": signature}
        self.started.append(signature)
        self.tickets[ticket["id"]] = {"status": "running", "signature": signature}
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]


class GateTriggerTests(unittest.TestCase):
    VERSION_AT = datetime(2026, 10, 7, 9, 53, tzinfo=timezone.utc)

    def coordinator(self, trigger, *, now):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE claim_versions (claim_version_id TEXT, created_at TEXT)")
        connection.execute("CREATE TABLE industry_framework_versions "
                           "(framework_ref TEXT, version_number INTEGER, created_at TEXT)")
        connection.execute("INSERT INTO industry_framework_versions VALUES ('f', 1, ?)",
                           (self.VERSION_AT.isoformat(),))
        launcher = _Launcher()
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=connection, launcher=launcher, clock=lambda: now.timestamp())
        patcher = patch("dalton_core.mission_industry_framework_lane.gate_redraft_trigger",
                        return_value=trigger)
        patcher.start()
        self.addCleanup(patcher.stop)
        return coordinator, launcher

    TRIGGER = {"deficiencies": ["行业框架有输入没有来源", "同业收入/增速/毛利率对比不完整"],
               "changes": ["新纳入同业：AMZN、MSFT"]}

    def test_gate_deficiencies_with_moved_inputs_redraft_inside_the_week(self):
        now = self.VERSION_AT + timedelta(hours=7)
        coordinator, launcher = self.coordinator(self.TRIGGER, now=now)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched", result)
        self.assertEqual(result["trigger"], "gate")
        self.assertIn("模型阶段门缺", result["reason"])
        self.assertIn("新纳入同业：AMZN、MSFT", result["reason"])

    def test_gate_redrafts_are_spaced(self):
        now = self.VERSION_AT + timedelta(hours=1)
        coordinator, launcher = self.coordinator(self.TRIGGER, now=now)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "waiting", result)
        self.assertEqual(result["trigger"], "gate")
        self.assertIn("秒后", result["reason"])
        self.assertEqual(launcher.started, [])
        self.assertLess(GATE_REDRAFT_SECONDS, 7 * 24 * 3600)

    def test_without_a_gate_trigger_the_week_holds_and_says_so(self):
        now = self.VERSION_AT + timedelta(hours=7)
        coordinator, launcher = self.coordinator(None, now=now)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["trigger"], "weekly")
        self.assertIn("weekly deliverable", result["reason"])
        self.assertEqual(launcher.started, [])

    def test_after_the_week_the_launch_names_the_weekly_trigger(self):
        now = self.VERSION_AT + timedelta(days=8)
        coordinator, launcher = self.coordinator(None, now=now)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["trigger"], "weekly")
        self.assertIn("每周", result["reason"])


class GateTriggerReadTests(unittest.TestCase):
    """``gate_redraft_trigger`` against a framework the gate faults."""

    def test_deficiency_without_moved_inputs_is_no_trigger(self):
        from dalton_core.mission_industry_framework_lane import redraft_changes

        framework = {"created_at": "2026-10-07T09:53:00+00:00",
                     "cross_company_comparison": {"companies": [
                         {"company_ref": "company:ticker:googl"}]},
                     "gaps": []}
        mission = {"universe": [{"company_ref": "company:ticker:googl", "ticker": "GOOGL"}]}
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE claim_versions (claim_version_id TEXT, created_at TEXT)")
        connection.execute("CREATE TABLE coverage_mission_statement_filings "
                           "(company_ref TEXT, recorded_at TEXT)")
        self.assertEqual(redraft_changes(connection, framework, mission, expected_gaps=None), [])
        connection.execute("INSERT INTO coverage_mission_statement_filings VALUES "
                           "('company:ticker:googl', '2026-10-08T00:00:00+00:00')")
        connection.execute("INSERT INTO claim_versions VALUES ('c', '2026-10-08T00:00:00+00:00')")
        changes = redraft_changes(connection, framework, mission, expected_gaps=None)
        self.assertEqual(len(changes), 2, changes)

    def test_a_screened_company_missing_from_the_comparison_is_a_change(self):
        from dalton_core.mission_industry_framework_lane import redraft_changes

        framework = {"created_at": "2026-10-07T09:53:00+00:00",
                     "cross_company_comparison": {"companies": [
                         {"company_ref": "company:ticker:googl"}]},
                     "gaps": []}
        mission = {"universe": [{"company_ref": "company:ticker:googl", "ticker": "GOOGL"},
                                {"company_ref": "company:ticker:amzn", "ticker": "AMZN"}]}
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE coverage_mission_statement_filings "
                           "(company_ref TEXT, recorded_at TEXT)")
        connection.execute("INSERT INTO coverage_mission_statement_filings VALUES "
                           "('company:ticker:amzn', '2026-09-01T00:00:00+00:00')")
        changes = redraft_changes(connection, framework, mission, expected_gaps=None)
        self.assertEqual(changes, ["新纳入同业：AMZN"])

    def test_a_changed_gap_rule_is_a_change(self):
        from dalton_core.mission_industry_framework_lane import redraft_changes

        framework = {"created_at": "2026-10-07T09:53:00+00:00",
                     "cross_company_comparison": {"companies": []},
                     "gaps": assess_gaps(dict(FrameworkGapTests.POLICY))[:1]}
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        changes = redraft_changes(connection, framework, {"universe": []},
                                  expected_gaps=assess_gaps(FrameworkGapTests.POLICY))
        self.assertEqual(len(changes), 1)
        self.assertIn("缺口", changes[0])


# ---------------------------------------------------------------------------
# 3. the debate map
# ---------------------------------------------------------------------------

class DebateVerifierPartialTests(unittest.TestCase):
    def test_findings_drop_only_the_named_debates(self):
        from dalton_core.debate_map_draft import split_by_findings

        debates = [{"debate_ref": "debate:a"}, {"debate_ref": "debate:b"},
                   {"debate_ref": "debate:c"}]
        kept, dropped = split_by_findings(debates, [
            {"debate_ref": "debate:b", "code": "position_not_supported_by_cited_claims",
             "detail": "x"}])
        self.assertEqual([row["debate_ref"] for row in kept], ["debate:a", "debate:c"])
        self.assertEqual([row["debate_ref"] for row in dropped], ["debate:b"])


class DebatePacingTests(unittest.TestCase):
    def coordinator(self, tmp):
        from pathlib import Path

        from dalton_core.mission_debate_map_lane import MissionDebateMapLaneCoordinator

        clock = {"now": datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)}
        coordinator = MissionDebateMapLaneCoordinator(
            store=None, launcher=None, mission=lambda: None,
            failure_ledger_dir=Path(tmp), clock=lambda: clock["now"])
        return coordinator, clock

    def test_a_held_input_unchanged_run_is_not_a_failed_draft(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            coordinator, _clock = self.coordinator(tmp)
            coordinator._pace_launched("company:ticker:googl", None)
            coordinator._pace_settled("company:ticker:googl", published=False,
                                      transient=False, outcome="input_unchanged")
            entry = coordinator.pacing.get("company:ticker:googl")
            self.assertEqual(int(entry.get("failures") or 0), 0)
            self.assertIsNone(coordinator._paced("company:ticker:googl", None))

    def test_a_backoff_names_the_last_outcome_in_chinese(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            coordinator, _clock = self.coordinator(tmp)
            coordinator._pace_launched("company:ticker:meta", None)
            coordinator._pace_settled(
                "company:ticker:meta", published=False, transient=False,
                outcome="verifier_rejected",
                detail="debate:eb55: position_not_supported_by_cited_claims")
            reason = coordinator._paced("company:ticker:meta", None)
            self.assertIn("没有发布", reason)
            self.assertIn("verifier_rejected", reason)
            self.assertIn("position_not_supported_by_cited_claims", reason)


if __name__ == "__main__":
    unittest.main()
