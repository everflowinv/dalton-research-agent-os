"""2026-10-02 (ws-7d): a data foundation that never arrives is a hold the owner sees.

Four hyperscalers waited fifteen days in ``initial_screen entered`` while the
drafting lane said ``idle: nothing to draft`` every hour.  These tests pin the
pieces that turn that into something a person can act on: the checklist knows
which missing quarters were already found, the lane holds with the reason, the
owner's list and the parity check escalate a long wait, the SEC host can fetch
a 10-K bigger than a web page, and a first mission searches AlphaEngine by the
issuer's name instead of its ticker.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import data_foundation
from dalton_core.data_foundation import (
    REASON_CODE,
    diagnose_item,
    foundation_blockers,
    hold_sentence,
    latest_summary,
    overdue,
)
from dalton_core.mission_stage import MissionStageDriver
from tests.test_mission_stage import ACN, PLANNED, REPORTS, TRANSCRIPTS, StageHarness

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
ENTERED = "2026-09-17T15:07:30+00:00"


def calls_item(**overrides):
    item = {"item_ref": "earnings_calls", "label": "过去 4 个季度的电话会纪要", "status": "partial",
            "have": 1, "required": 4, "candidate_count": 28, "pending": 0, "failed": 0,
            "missing_periods": ["FY2025-Q3", "FY2025-Q4", "FY2026-Q1"],
            "candidate_periods": [], "spec_refs": [TRANSCRIPTS], "source_ref": "source:alphaengine"}
    item.update(overrides)
    return item


class CandidatePeriodTests(StageHarness):
    """The checklist says which missing quarters are already found, by the counting rules."""

    def test_found_quarters_are_reported_and_junk_candidates_are_not(self) -> None:
        self.document(ACN, TRANSCRIPTS, "acquired", title="ACN Q2 2026")
        self.document(ACN, TRANSCRIPTS, "discovered", title="ACN Q1 2026")
        self.document(ACN, TRANSCRIPTS, "discovered", title="ACN Q4 2025")
        # A different issuer's call and a conference appearance are candidates
        # in the queue but cover no quarter of this company.
        self.document(ACN, TRANSCRIPTS, "discovered", title="Volvo Cars Pre-close call",
                      named_companies=["VOLVO CAR AB"])
        self.document(ACN, TRANSCRIPTS, "discovered", title="ACN Q3 2025 Citi Global TMT Conference")
        calls = self.item(self.evaluate(), ACN, "earnings_calls")
        self.assertEqual(calls["have"], 1)
        self.assertEqual(calls["missing_periods"], ["FY2025-Q3", "FY2025-Q4", "FY2026-Q1"])
        self.assertEqual(calls["candidate_periods"], ["FY2025-Q4", "FY2026-Q1"])
        self.assertEqual(calls["candidate_count"], 4)
        diagnosis = diagnose_item(calls, ticker="ACN")
        # One quarter was never returned: that is a search problem, and it
        # still says the other two are on their way.
        self.assertEqual(diagnosis["cause"], "not_returned_by_search")
        self.assertFalse(diagnosis["automatic"])
        self.assertIn("FY2025-Q3", diagnosis["why"])
        self.assertIn("FY2025-Q4、FY2026-Q1 已找到", diagnosis["why"])

    def test_every_missing_quarter_found_is_an_automatic_wait(self) -> None:
        self.document(ACN, TRANSCRIPTS, "acquired", title="ACN Q2 2026")
        for title in ("ACN Q3 2025", "ACN Q4 2025", "ACN Q1 2026"):
            self.document(ACN, TRANSCRIPTS, "discovered", title=title)
        calls = self.item(self.evaluate(), ACN, "earnings_calls")
        self.assertEqual(calls["candidate_periods"], ["FY2025-Q3", "FY2025-Q4", "FY2026-Q1"])
        diagnosis = diagnose_item(calls, ticker="ACN")
        self.assertEqual(diagnosis["cause"], "found_waiting_acquisition")
        self.assertTrue(diagnosis["automatic"])

    def test_an_acquired_quarter_is_not_reported_again_as_a_candidate(self) -> None:
        self.document(ACN, TRANSCRIPTS, "acquired", title="ACN Q2 2026")
        self.document(ACN, TRANSCRIPTS, "discovered", title="ACN Q2 2026 Earnings Call")
        calls = self.item(self.evaluate(), ACN, "earnings_calls")
        self.assertEqual(calls["candidate_periods"], [])


class DiagnosisTests(unittest.TestCase):
    """One missing item → one cause, in the words of who has to move."""

    def test_no_transcript_ever_returned_names_the_search_plan(self) -> None:
        item = calls_item(status="missing", have=0, candidate_count=2, missing_periods=[])
        diagnosis = diagnose_item(item, ticker="GOOGL")
        self.assertEqual(diagnosis["cause"], "not_returned_by_search")
        self.assertIn("最近 4 个季度", diagnosis["why"])
        self.assertIn("GOOGL", diagnosis["action"])
        self.assertIn("检索计划", diagnosis["action"])

    def test_a_10k_too_large_for_the_fetch_is_named_as_a_code_limit(self) -> None:
        item = {"item_ref": "annual_report", "label": "最新年报（10-K）正文", "status": "missing",
                "have": 0, "required": 1, "candidate_count": 0, "pending": 0, "failed": 2,
                "spec_refs": ["annual-report-10k"], "source_ref": "source:sec-edgar"}
        diagnosis = diagnose_item(
            item, ticker="MSFT",
            failure_reason=("fetch outcome retryable; PublicTransportResponseTooLarge: "
                            "response body exceeds the configured limit"))
        self.assertEqual(diagnosis["cause"], "fetch_too_large")
        self.assertFalse(diagnosis["automatic"])
        self.assertIn("大小上限", diagnosis["action"])
        other = diagnose_item(item, ticker="MSFT", failure_reason="HTTP 404")
        self.assertEqual(other["cause"], "fetch_failed")

    def test_broker_candidates_waiting_for_selection_are_automatic(self) -> None:
        item = {"item_ref": "broker_research", "label": "近 6 个月的多空券商观点",
                "status": "missing", "have": 0, "required": 3, "candidate_count": 638,
                "pending": 0, "failed": 0, "spec_refs": [REPORTS], "source_ref": "source:alphaengine"}
        diagnosis = diagnose_item(item, ticker="MSFT")
        self.assertEqual(diagnosis["cause"], "found_waiting_acquisition")
        self.assertIn("638", diagnosis["why"])
        none = diagnose_item({**item, "candidate_count": 0}, ticker="MSFT")
        self.assertEqual(none["cause"], "not_returned_by_search")

    def test_blockers_carry_days_and_whether_the_owner_is_needed(self) -> None:
        rows = [
            {"company_ref": "company:ticker:meta", "ticker": "META", "priority": "P1",
             "stage": "initial_screen", "stage_status": "entered",
             "items": [calls_item(candidate_periods=["FY2025-Q3", "FY2025-Q4", "FY2026-Q1"])]},
            {"company_ref": "company:ticker:msft", "ticker": "MSFT", "priority": "P1",
             "stage": "initial_screen", "stage_status": "entered",
             "items": [calls_item(status="missing", have=0, missing_periods=[], candidate_count=4)]},
            # Passed, or not in the first stage: nothing to say.
            {"company_ref": "company:ticker:amzn", "ticker": "AMZN", "stage": "initial_screen",
             "stage_status": "gate_passed", "items": [calls_item()]},
        ]
        blockers = foundation_blockers(
            rows, waiting={"company:ticker:meta": ENTERED, "company:ticker:msft": ENTERED},
            now=NOW)
        self.assertEqual([block["ticker"] for block in blockers], ["META", "MSFT"])
        self.assertEqual(blockers[0]["days_waiting"], 14)
        self.assertFalse(blockers[0]["needs_owner"])
        self.assertTrue(blockers[1]["needs_owner"])
        sentence = hold_sentence(blockers)
        self.assertIn("2 家公司", sentence)
        self.assertIn("META（已等 14 天）", sentence)
        self.assertIn("检索没有找到", sentence)


class SummaryReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def write(self, name: str, summary: dict, *, latest: bool = True) -> None:
        directory = self.state / "initial-screens" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        if latest:
            (self.state / "initial-screens" / "latest.json").write_text(
                json.dumps({"ticket_ref": f"initial-screen:{name}"}), encoding="utf-8")

    def held(self, since: str = ENTERED) -> dict:
        return {"status": "held", "reason_code": REASON_CODE, "created_at": NOW.isoformat(),
                "failure_reason": "资料底座还没齐",
                "foundation": {"companies": [{
                    "company_ref": "company:ticker:msft", "ticker": "MSFT", "waiting_since": since,
                    "needs_owner": True, "missing": ["过去 4 个季度的电话会纪要"],
                    "items": [diagnose_item(calls_item(status="missing", have=0, missing_periods=[]),
                                            ticker="MSFT")]}]}}

    def test_latest_summary_falls_back_while_the_newest_ticket_runs(self) -> None:
        self.assertIsNone(latest_summary(self.state))
        self.write("a" * 24, self.held())
        (self.state / "initial-screens" / "latest.json").write_text(
            json.dumps({"ticket_ref": "initial-screen:" + "b" * 24}), encoding="utf-8")
        self.assertEqual(latest_summary(self.state)["reason_code"], REASON_CODE)

    def test_only_a_long_wait_is_overdue(self) -> None:
        self.assertEqual(overdue(self.held(), now=NOW)[0]["days_waiting"], 14)
        fresh = (NOW - timedelta(days=1)).isoformat()
        self.assertEqual(overdue(self.held(fresh), now=NOW), [])
        self.assertEqual(overdue({"status": "idle"}, now=NOW), [])

    def test_the_owner_list_carries_one_item_per_waiting_company(self) -> None:
        from dalton_core.needs_human import KINDS, URGENCY, collect, stalled_foundations

        self.assertIn("data_foundation_stalled", KINDS)
        self.assertEqual(URGENCY["data_foundation_stalled"], 1)
        self.write("a" * 24, self.held())
        items = stalled_foundations(self.state, now=NOW)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["ref"], "foundation:company:ticker:msft")
        self.assertIn("MSFT 的初筛已经等了 14 天", item["title"])
        self.assertIn("检索计划", item["action"])
        self.assertTrue(item["actionable"])
        listed = collect(state_dir=self.state, clock=lambda: NOW)
        self.assertIn("foundation:company:ticker:msft", [row["ref"] for row in listed["items"]])

    def test_a_wait_that_closes_by_itself_is_a_notice_not_a_todo(self) -> None:
        from dalton_core.needs_human import stalled_foundations

        summary = self.held()
        block = summary["foundation"]["companies"][0]
        block["items"] = [diagnose_item(
            calls_item(candidate_periods=["FY2025-Q3", "FY2025-Q4", "FY2026-Q1"]), ticker="META")]
        block["needs_owner"] = False
        self.write("a" * 24, summary)
        item = stalled_foundations(self.state, now=NOW)[0]
        self.assertFalse(item["actionable"])
        self.assertIn("不用你处理", item["action"])

    def test_parity_warns_with_the_concrete_items(self) -> None:
        from dalton_core.workspace_health_parity import WARN, _initial_screen_foundation_rows

        env = SimpleNamespace(state=self.state, mission={"id": "m"})
        self.assertEqual(_initial_screen_foundation_rows(env, now=NOW), [])
        self.write("a" * 24, self.held())
        rows = _initial_screen_foundation_rows(env, now=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["check"], rows[0]["status"]), ("initial_screen_foundation", WARN))
        self.assertIn("MSFT 等了 14 天", rows[0]["detail"])
        self.assertIn("检索没有找到", rows[0]["detail"])
        self.assertIn("MSFT", rows[0]["fix"])


class LaneHoldTests(StageHarness):
    """The child holds with the reason; the coordinator repeats it every tick."""

    def setUp(self) -> None:
        super().setUp()
        params = dict(self.params)
        params["autonomy"] = {**params["autonomy"],
                              "may_write": list(params["autonomy"]["may_write"]) + ["deliverable"]}
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.mission = self.missions.create_mission(self.mission_ref, **params)
        MissionStageDriver(self.missions, planned_specs=PLANNED).run_once()

    def claim(self) -> None:
        from dalton_core.store import content_hash

        claim = {
            "schema_version": "0.2", "id": "claim-version:" + "1" * 64, "claim_ref": "claim:test:1",
            "version": 1, "subject_ref": ACN, "metric_or_aspect": "aspect:test", "period": "2026Q2",
            "basis": "fixture", "normalized_statement": "管理层说需求在改善。",
            "claim_kind": "qualitative", "value": None, "unit": None, "currency": None,
            "scale": None, "producer_execution_refs": [], "semantic_review_ref": None,
            "semantic_review_hash": None, "candidate_origin_ref": None,
            "candidate_origin_hash": None, "actor_ref": "system:research-auto-commit",
            "prior_version_ref": None, "created_at": "2026-09-02T00:00:00+00:00",
        }
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1, json.dumps(claim, sort_keys=True),
                 claim["content_hash"], claim["created_at"]))

    def test_the_child_holds_with_the_missing_items_instead_of_idling(self) -> None:
        from dalton_core import initial_screen_cli

        self.claim()
        self.document(ACN, TRANSCRIPTS, "acquired", title="ACN Q2 2026")
        self.document(ACN, TRANSCRIPTS, "discovered", title="Volvo Cars Pre-close call",
                      named_companies=["VOLVO CAR AB"])
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "discovery-plans").mkdir()
        (root / "discovery-plans" / "plan.json").write_text(json.dumps({"specs": [
            {"spec_ref": TRANSCRIPTS}, {"spec_ref": REPORTS}, {"spec_ref": "annual-report-10k"}]}))
        self.store.connection.commit()
        destination = sqlite3.connect(str(root / "core.sqlite"))
        self.store.connection.backup(destination)
        destination.close()
        args = argparse.Namespace(
            state_dir=str(root), model_config=None, summary_dir=str(root),
            scheduler_db=None, timeout_seconds=30, dry_run=True, quiet=True)
        summary = initial_screen_cli.run(args)
        self.assertEqual(summary["status"], "held", summary)
        self.assertEqual(summary["reason_code"], REASON_CODE)
        self.assertIn("资料底座还没齐", summary["failure_reason"])
        companies = summary["foundation"]["companies"]
        self.assertEqual([block["company_ref"] for block in companies], [ACN])
        causes = {item["item_ref"]: item["cause"] for item in companies[0]["items"]}
        self.assertEqual(causes["earnings_calls"], "not_returned_by_search")
        self.assertIsNotNone(companies[0]["waiting_since"])
        # Companies with nothing to write from are still skipped for that reason.
        self.assertIn("no live Claim to write from",
                      {row["reason"] for row in summary["skipped"]})

    def test_the_coordinator_repeats_the_foundation_reason_while_held(self) -> None:
        from dalton_core.initial_screen_launcher import (
            InitialScreenCoordinator,
            InitialScreenLauncher,
        )

        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / "model.json"; config.write_text("{}", encoding="utf-8")
        launcher = InitialScreenLauncher(state_dir=root, model_config_path=config)
        coordinator = InitialScreenCoordinator(store=self.store, launcher=launcher,
                                               clock=lambda: NOW)
        name = "e" * 24
        directory = launcher.tickets_dir / name
        directory.mkdir(parents=True)
        (directory / "ticket.json").write_text(json.dumps({
            "schema_version": "0.1", "id": f"initial-screen:{name}", "status": "succeeded",
            "pid": 1, "started_at": "2026-10-02T08:30:00+00:00", "exit_code": 0,
            "completed_at": "2026-10-02T08:31:00+00:00"}), encoding="utf-8")
        (directory / "summary.json").write_text(json.dumps({
            "status": "held", "reason_code": REASON_CODE,
            "failure_reason": "资料底座还没齐，1 家公司的初筛都在等：MSFT 电话会纪要：检索没有找到"}),
            encoding="utf-8")
        coordinator._latest_path().write_text(json.dumps({
            "ticket_ref": f"initial-screen:{name}", "started_at": "2026-10-02T08:30:00+00:00",
            "idle_signature": coordinator._signature(),
            "idle_at": "2026-10-02T08:30:00+00:00"}), encoding="utf-8")
        result = coordinator.dispatch_once()
        self.assertEqual((result["status"], result["reason_code"]), ("held", REASON_CODE))
        self.assertIn("MSFT 电话会纪要：检索没有找到", result["reason"])


class SecFetchCeilingTests(unittest.TestCase):
    """www.sec.gov gets a bigger body ceiling as a new profile generation."""

    def test_only_the_sec_host_changes_generation(self) -> None:
        from dalton_core.public_web_core_fetch import (
            FETCH_MAX_RESPONSE_BYTES,
            HOST_SCOPED_GENERATION,
            host_generation,
            host_max_response_bytes,
        )

        self.assertEqual(host_max_response_bytes("example.com"), FETCH_MAX_RESPONSE_BYTES)
        self.assertEqual(host_generation("example.com"), HOST_SCOPED_GENERATION)
        self.assertGreaterEqual(host_max_response_bytes("www.sec.gov"), 16_000_000)
        self.assertNotEqual(host_generation("www.sec.gov"), HOST_SCOPED_GENERATION)

    def test_an_existing_4mb_sec_profile_is_superseded_on_the_same_chain(self) -> None:
        from dalton_core import public_web_core_fetch as module
        from tests.test_public_web_fetch_lane import FetchHarness

        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        harness = FetchHarness(Path(temp.name))
        self.addCleanup(harness.close)
        # What a Core deployed before this change registered for the host.
        with patch.object(module, "HOST_MAX_RESPONSE_BYTES", {}):
            old = harness.fetch.ensure_host_authorities("www.sec.gov")
        self.assertEqual(old["profile"]["max_response_bytes"], module.FETCH_MAX_RESPONSE_BYTES)
        harness.fetch._hosts.clear()
        new = harness.fetch.ensure_host_authorities("www.sec.gov")
        self.assertEqual(new["profile"]["max_response_bytes"],
                         module.host_max_response_bytes("www.sec.gov"))
        self.assertNotEqual(new["profile"]["id"], old["profile"]["id"])
        self.assertEqual(new["profile"]["prior_version_ref"], old["profile"]["id"])
        self.assertIn(module.host_generation("www.sec.gov"), new["profile"]["id"])
        # Same host-scoped quota, larger byte allowance.
        self.assertEqual(new["rate_policy"]["quota_scope_ref"], old["rate_policy"]["quota_scope_ref"])
        self.assertGreater(new["rate_policy"]["limits"]["bytes"], old["rate_policy"]["limits"]["bytes"])
        # Re-running is idempotent, and other hosts keep their generation.
        harness.fetch._hosts.clear()
        self.assertEqual(harness.fetch.ensure_host_authorities("www.sec.gov")["profile"]["id"],
                         new["profile"]["id"])
        other = harness.fetch.ensure_host_authorities("example.com")
        self.assertTrue(other["profile"]["id"].endswith(f":{module.HOST_SCOPED_GENERATION}:v1"))


class FirstMissionAlphaEnginePlanTests(unittest.TestCase):
    """A first mission searches AlphaEngine by the issuer's name, not its ticker."""

    def test_names_come_from_the_name_table_then_the_sec_registration(self) -> None:
        from dalton_core.workspace_mission_setup import (
            alphaengine_plan_companies,
            issuer_short_name,
        )

        self.assertEqual(issuer_short_name("GOOGL"), "Alphabet")
        self.assertEqual(issuer_short_name("MSFT", ["MICROSOFT CORP"]), "Microsoft")
        self.assertEqual(issuer_short_name("NVDA", ["NVIDIA CORP"]), "NVIDIA")
        self.assertEqual(issuer_short_name("XYZ", ["Block, Inc."]), "Block")
        self.assertEqual(issuer_short_name("ZZZZ"), "ZZZZ")
        companies = alphaengine_plan_companies({"company:ticker:meta": "META"})
        self.assertEqual(companies["company:ticker:meta"],
                         {"name": "Meta Platforms", "ticker": "META", "aliases": ["Meta Platforms"]})

    def test_transcript_queries_name_the_issuer(self) -> None:
        from datetime import date

        from dalton_core.mission_source_discovery import (
            build_discovery_parameters,
            build_discovery_plan,
        )
        from dalton_core.workspace_mission_setup import (
            alphaengine_plan_companies,
            alphaengine_plan_specs,
        )

        plan = build_discovery_plan(
            plan_id="discovery-plan:ws-test:alphaengine:1",
            created_at="1970-01-01T00:00:00.000000+00:00",
            mission_ref="coverage-mission:ws-test",
            companies=alphaengine_plan_companies({"company:ticker:googl": "GOOGL"}),
            max_calls_24h=130, specs=alphaengine_plan_specs())
        # 2026-10-06: the plan holds governed fields only; the forms are the code's.
        from dalton_core.alphaengine_query_forms import effective_discovery_plan
        self.assertEqual(plan["schema_version"], "0.7")
        plan = effective_discovery_plan(plan)
        spec = next(item for item in plan["specs"] if item["spec_ref"] == TRANSCRIPTS)
        queries = [build_discovery_parameters(
            plan, spec_ref=TRANSCRIPTS, company_ref="company:ticker:googl",
            as_of=date(2026, 10, 2), variant_index=index,
            missing_periods=["FY2025-Q3"])["query"]
            for index in range(len(spec["query_variants"]))]
        # 2026-10-03: AlphaEngine titles the call "Alphabet Q3 2025"; that
        # exact form is asked first, the longer phrasings follow it.
        self.assertEqual(queries[0], "Alphabet Q3 2025")
        self.assertEqual(queries[1], "Alphabet")
        self.assertEqual(queries[2], "Alphabet FY2025-Q3 earnings call transcript")
        self.assertTrue(all("Alphabet" in query or "GOOGL" in query for query in queries))
        self.assertGreater(len(queries), 1)
        # Without a missing quarter the label leaves the bare name, not a gap.
        bare = build_discovery_parameters(
            plan, spec_ref=TRANSCRIPTS, company_ref="company:ticker:googl",
            as_of=date(2026, 10, 2), variant_index=0)["query"]
        self.assertEqual(bare, "Alphabet")

    def test_materialized_plan_uses_registered_names(self) -> None:
        from dalton_core.workspace import WorkspacePaths
        from dalton_core.workspace_mission_setup import materialize_first_mission_discovery_plans

        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        state = Path(temp.name) / "state"
        (state / "discovery-plans").mkdir(parents=True)
        workspace = SimpleNamespace(state_dir=state, slug="ws-test")
        mission = {
            "mission_ref": "coverage-mission:ws-test",
            "source_plan": [{"source_ref": "source:alphaengine", "status": "connected"},
                            {"source_ref": "source:sec-edgar", "status": "connected"}],
            "universe": [{"company_ref": "company:ticker:nvda", "ticker": "NVDA"}],
            "budget": {"max_daily_paid_calls": 0, "max_alphaengine_calls_24h": 130},
        }
        written = materialize_first_mission_discovery_plans(
            workspace, mission,
            sec_ticker_resolver=lambda ticker: {"cik": "1045810", "name": "NVIDIA CORP"})
        plan = json.loads(Path(written["source:alphaengine"]).read_text())
        self.assertEqual(plan["companies"]["company:ticker:nvda"]["name"], "NVIDIA")
        self.assertIsInstance(WorkspacePaths, type)


if __name__ == "__main__":
    unittest.main()
