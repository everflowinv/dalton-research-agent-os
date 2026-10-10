"""2026-10-08: AlphaEngine login lapse -- provider text kept, source held, owner told.

Both environments, 08:03-08:09Z: every AlphaEngine search failed with
``E_UPSTREAM_API: 用户状态发生变更，刷新 token`` while ``auth_status`` said
ready.  The ticket said only "search outcome retryable", the cadence waited a
day and nobody was told.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dalton_core.alphaengine_core_search import (
    SearchConnectorGovernance,
    build_search_governance_record,
)
from dalton_core.alphaengine_search_cli import run_discovery
from dalton_core.connector_health import (
    HELD,
    RECOVERED,
    ConnectorHealth,
    held_records,
    read_record,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.live_mcp_connector import AlphaEngineLiveAdapter
from dalton_core.mission_source_discovery import (
    AlphaEngineSearchLauncher,
    MissionSourceDiscoveryCoordinator,
    discovery_coordinator_state,
)
from dalton_core.needs_human import collect, connector_login_lapses
from dalton_core.openclaw_connector_bridge import HostToolInvocationResult
from dalton_core.provider_failure import (
    PROVIDER_AUTH,
    PROVIDER_ERROR,
    TRANSPORT,
    classify_provider_failure,
    classify_reason,
    failure_detail,
    failure_reason_text,
    journal_failure,
)
from dalton_core.store import DaltonStore, canonical_json
from dalton_core.workspace_health_parity import Environment, connector_health_rows
from tests import test_mission_source_discovery as _discovery_tests
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_live_mcp_connector import MemorySink, adapter_request, search_parameters
from tests.test_mission_source_discovery import (
    ACN,
    CTSH,
    OWNER,
    Clock,
    FakeAcquisitionLauncher,
    FakeSearchLauncher,
    SearchHarness,
    plan_for_tests,
)

UPSTREAM = "E_UPSTREAM_API: 用户状态发生变更，刷新 token"


def lapsed_result() -> dict:
    return {"isError": True, "content": [{"type": "text", "text": UPSTREAM}]}


class LapsedHandle:
    """The AlphaEngine host tool as it answered on 2026-10-08."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        del tool_name, arguments, call_ref, deadline_at, max_response_bytes
        self.calls += 1
        request_id = f"provider-request:lapsed:{self.calls}"
        result = lapsed_result()
        raw = canonical_json({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()
        return HostToolInvocationResult(request_id=request_id, raw_response=raw, result=result)


class ClassificationTests(unittest.TestCase):
    def test_session_and_token_refusals_are_provider_auth(self) -> None:
        for code, message in (
            ("source_error", UPSTREAM),
            (None, "token_expired"),
            (None, "HTTP 401 Unauthorized"),
            (None, "status_code=403"),
            ("source_error", "用户状态发生变更"),
            ("provider_auth", "anything"),
            ("permission_denied", "MCP endpoint rejected host authorization"),
        ):
            self.assertEqual(classify_provider_failure(code, message), PROVIDER_AUTH, message)

    def test_transport_and_other_errors_stay_distinct(self) -> None:
        self.assertEqual(classify_provider_failure("adapter_failure",
                                                   "OpenClawConnectorBridgeError: MCP endpoint is unavailable"),
                         TRANSPORT)
        self.assertEqual(classify_provider_failure("deadline_exceeded", "x"), TRANSPORT)
        self.assertEqual(classify_provider_failure("source_error", "E_UPSTREAM_API: 服务繁忙"),
                         PROVIDER_ERROR)
        # A doc id that merely contains 401 is not an auth failure.
        self.assertEqual(classify_provider_failure("source_error", "doc 1401 not found"),
                         PROVIDER_ERROR)

    def test_reason_text_carries_the_provider_words_and_the_class_tag(self) -> None:
        detail = failure_detail({"code": "provider_auth", "message": UPSTREAM, "retryable": False})
        reason = failure_reason_text("search outcome failed", detail)
        self.assertIn(UPSTREAM, reason)
        self.assertIn("[provider_auth]", reason)
        self.assertEqual(classify_reason(reason), PROVIDER_AUTH)
        self.assertIsNone(classify_reason("search outcome retryable"))
        self.assertEqual(failure_reason_text("search outcome failed", None), "search outcome failed")

    def test_adapter_reports_provider_auth_not_retryable_source_error(self) -> None:
        request, _ = adapter_request("search_library", search_parameters())

        class Handle:
            def invoke(self, *args, **kwargs):
                return LapsedHandle().invoke(*args, **kwargs)

        sink = MemorySink()
        observation = AlphaEngineLiveAdapter()(request, sink, Handle())
        self.assertEqual(observation["outcome"], "failed")
        self.assertEqual(observation["error"]["code"], "provider_auth")
        self.assertEqual(observation["error"]["message"], UPSTREAM)
        self.assertFalse(observation["error"]["retryable"])
        self.assertEqual(sink.data, b"")


class ErrorTextSurvivesTests(unittest.TestCase):
    """The provider text reaches the attempt record, the summary and the dispatch reason."""

    def test_governed_search_keeps_provider_text_in_attempt_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = SearchHarness(Path(temp), [])
            try:
                lapsed = LapsedHandle()
                from tests.test_mission_source_discovery import approved_governance
                from dalton_core.alphaengine_core_search import AlphaEngineCoreSearch

                search = AlphaEngineCoreSearch(
                    store=harness.core, connectors=harness.connectors,
                    observability=harness.observability, journal=harness.journal,
                    scheduler=harness.scheduler, catalog=harness.catalog, spool=harness.spool,
                    governance=approved_governance(), mcp_handle=lapsed, clock=harness.clock,
                )
                parameters = {"query": "Alphabet Q2 2026", "cursor": None,
                              "filters": {"document_type": "meeting_minutes",
                                          "date_from": "2025-09-03", "date_to": "2026-10-08"}}
                receipt = search.search(search.build_request(parameters))
                self.assertNotEqual(receipt["outcome"], "succeeded")
                self.assertEqual(lapsed.calls, 1)
                detail = journal_failure(harness.core.connection, receipt["connector_invocation_ref"])
                self.assertEqual(detail["class"], PROVIDER_AUTH)
                self.assertEqual(detail["message"], UPSTREAM)
                row = harness.core.connection.execute(
                    "SELECT record_json FROM connector_physical_attempts "
                    "WHERE connector_invocation_ref=?",
                    (receipt["connector_invocation_ref"],)).fetchone()
                attempt = json.loads(row[0])
                self.assertEqual(attempt["outcome"], "failed")
                self.assertEqual(attempt["error"]["code"], "provider_auth")
                self.assertEqual(attempt["error"]["message"], UPSTREAM)
            finally:
                harness.close()

    def test_run_discovery_summary_names_the_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            state.mkdir(mode=0o700)
            core = DaltonStore(str(state / "core.sqlite"))
            method = bootstrap_method_authorities(core)
            params = mission_params(method)
            ref = params.pop("mission_ref")
            mission = CoverageMissionAuthority(core).create_mission(ref, **params)
            core.close()
            governance = SearchConnectorGovernance(
                build_search_governance_record(approved_by="human:lumos", status="approved"))
            plan = plan_for_tests()
            summary = run_discovery(
                state_dir=state, governance=governance, plan=plan, company_ref=ACN,
                spec_ref="earnings-call-transcripts", requested_by=OWNER,
                mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
                as_of=date(2026, 10, 8), handle=LapsedHandle(), transport="fake",
                summary_dir=state / "out",
            )
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["search"]["document_refs"], [])
            self.assertEqual(summary["failure_detail"]["class"], PROVIDER_AUTH)
            self.assertEqual(summary["failure_detail"]["message"], UPSTREAM)
            self.assertIn(UPSTREAM, summary["failure_reason"])
            self.assertIn("[provider_auth]", summary["failure_reason"])
            written = json.loads((state / "out" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(written["failure_detail"]["message"], UPSTREAM)


class AuthFailingSearchLauncher(FakeSearchLauncher):
    """The child as it now reports a lapsed login, plus a scripted probe."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lapsed = True
        self.probe_ok: list[bool] = []
        self.probes = 0

    def start(self, *, authorization, spec_ref, as_of=None, cursor=None,
              variant_index=None, missing_periods=()):
        if not self.lapsed:
            return super().start(authorization=authorization, spec_ref=spec_ref, as_of=as_of,
                                 cursor=cursor, variant_index=variant_index,
                                 missing_periods=missing_periods)
        self.starts.append({"authorization": dict(authorization), "spec_ref": spec_ref,
                            "cursor": cursor, "as_of": as_of})
        ticket_id = f"alphaengine-discovery:{len(self.starts):024x}"
        detail = failure_detail({"code": "provider_auth", "message": UPSTREAM, "retryable": False})
        summary = {"discovery_ref": None, "new_document_count": 0,
                   "failure_detail": detail,
                   "failure_reason": failure_reason_text("search outcome failed", detail)}
        self.tickets[ticket_id] = {"id": ticket_id, "status": "failed", "exit_code": 1,
                                   "summary": summary}
        return {"id": ticket_id, "status": "running"}

    def probe_health(self):
        self.probes += 1
        ok = self.probe_ok.pop(0) if self.probe_ok else False
        return {"ok": ok, "method": "list_filters",
                "message": "list_filters answered" if ok else UPSTREAM}


class HoldProbeRetryTests(unittest.TestCase):
    """The coordinator fixture of test_mission_source_discovery: ACN and CTSH, retry 1 day."""

    create_mission = _discovery_tests.CoordinatorTests.create_mission
    mission_v2 = _discovery_tests.CoordinatorTests.mission_v2

    def setUp(self) -> None:
        _discovery_tests.CoordinatorTests.setUp(self)
        self.health_dir = Path(self.temp.name) / "health-state"
        self.health_dir.mkdir()
        self.health = ConnectorHealth(self.health_dir, clock=self.clock)
        self.search_launcher = AuthFailingSearchLauncher(self.h, self.missions, self.plan)
        self.acquisition_launcher = FakeAcquisitionLauncher(self.h)
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=self.search_launcher,
            acquisition_launcher=self.acquisition_launcher,
            clock=self.clock, health=self.health,
        )

    def test_auth_failure_holds_source_probes_then_resumes_without_daily_wait(self) -> None:
        self.mission_v2(self.create_mission())
        first = self.coordinator.dispatch_once()
        self.assertEqual(first["discovery"]["status"], "launched")
        self.assertEqual(first["discovery"]["company_ref"], ACN)

        # The failure settles: the dispatch keeps the provider's words, the
        # source goes on hold, and nothing else is launched (not CTSH either).
        second = self.coordinator.dispatch_once()
        settled = second["settled_dispatches"][0]
        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["failure_class"], PROVIDER_AUTH)
        self.assertIn(UPSTREAM, settled["reason"])
        row = self.missions.discovery_dispatches(
            self.missions.active_mission(self.plan["mission_ref"])["id"],
            company_ref=ACN, spec_ref="earnings-call-transcripts", limit=1,
            across_versions=True)[0]
        self.assertIn(UPSTREAM, row["failure_reason"])
        self.assertEqual(second["discovery"]["status"], "health_hold")
        self.assertEqual(second["acquisition"]["status"], "health_hold")
        self.assertEqual(len(self.search_launcher.starts), 1)
        self.assertEqual(self.search_launcher.probes, 0)
        state = discovery_coordinator_state(second)
        self.assertEqual(state["connector_health"], "provider_auth_hold")
        record = read_record(self.health_dir, "source:alphaengine")
        self.assertEqual(record["status"], HELD)
        self.assertEqual(record["blocked"]["search"], 1)

        # Inside the hold: no probe, no search.
        self.clock.advance(minutes=5)
        third = self.coordinator.dispatch_once()
        self.assertEqual(third["discovery"]["status"], "health_hold")
        self.assertEqual(self.search_launcher.probes, 0)

        # Hold lapses, probe still refused: hold extended, still no search.
        self.clock.advance(minutes=11)
        fourth = self.coordinator.dispatch_once()
        self.assertEqual(fourth["discovery"]["status"], "health_hold")
        self.assertEqual(self.search_launcher.probes, 1)
        self.assertEqual(len(self.search_launcher.starts), 1)

        # The owner re-logs in; the next probe passes and the ACN search that
        # failed with provider_auth runs on this tick -- not a day later.
        self.search_launcher.lapsed = False
        self.search_launcher.probe_ok = [True]
        self.clock.advance(minutes=16)
        fifth = self.coordinator.dispatch_once()
        self.assertEqual(self.search_launcher.probes, 2)
        self.assertEqual(fifth["discovery"]["status"], "launched", fifth["discovery"])
        self.assertEqual(fifth["discovery"]["company_ref"], ACN)
        self.assertEqual(read_record(self.health_dir, "source:alphaengine")["status"], RECOVERED)

    def test_non_auth_failure_still_waits_retry_interval(self) -> None:
        self.mission_v2(self.create_mission())
        self.search_launcher.lapsed = False
        self.search_launcher.fail_next = True
        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["settled_dispatches"][0]["status"], "failed")
        self.assertNotIn("failure_class", tick["settled_dispatches"][0])
        self.assertEqual(tick["discovery"]["company_ref"], CTSH)
        self.assertIsNone(read_record(self.health_dir, "source:alphaengine"))
        self.coordinator.settle_dispatches()
        later = self.coordinator.launch_discovery()
        self.assertTrue(any("retry interval" in item["reason"]
                            for item in later.get("skipped", [])), later)

    def test_dispatch_settled_before_the_fix_is_classified_from_the_journal(self) -> None:
        """Rows that say only "search outcome retryable" (live, 2026-10-08 08:03Z)."""

        from dalton_core.alphaengine_core_search import AlphaEngineCoreSearch
        from tests.test_mission_source_discovery import approved_governance

        search = AlphaEngineCoreSearch(
            store=self.h.core, connectors=self.h.connectors, observability=self.h.observability,
            journal=self.h.journal, scheduler=self.h.scheduler, catalog=self.h.catalog,
            spool=self.h.spool, governance=approved_governance(), mcp_handle=LapsedHandle(),
            clock=self.clock,
        )
        receipt = search.search(search.build_request(
            {"query": "Microsoft Q4 2026", "cursor": None,
             "filters": {"document_type": "meeting_minutes"}}))
        ticket = "alphaengine-discovery:" + "e" * 24
        self.search_launcher.tickets[ticket] = {
            "id": ticket, "status": "failed", "exit_code": 1,
            "summary": {"failure_reason": "search outcome retryable",
                        "search": {"connector_invocation_ref": receipt["connector_invocation_ref"]}}}
        row = {"dispatch_id": "d-legacy", "ticket_ref": ticket, "status": "failed",
               "failure_reason": "search outcome retryable"}
        self.assertEqual(self.coordinator._dispatch_failure_class(row), PROVIDER_AUTH)
        other = {"dispatch_id": "d-other", "ticket_ref": ticket, "status": "failed",
                 "failure_reason": "rehearsed failure"}
        self.assertIsNone(self.coordinator._dispatch_failure_class(other))

    # The four rows the live ledgers held on 2026-10-08 (ws-7d x3, legacy x1).
    LIVE_ROWS = (
        ("mission-discovery-dispatch:7337edc6b5bcb68296c1d3ae1feaa2a3", "company:ticker:googl",
         "earnings-call-transcripts", "alphaengine-discovery:db582c21b569a4278cc8a951",
         "2026-10-08T08:03:09.471885+00:00", "2026-10-08T08:08:05.767316+00:00"),
        ("mission-discovery-dispatch:741e217b80b201daeb2a459e024f0e23", "company:ticker:msft",
         "earnings-call-transcripts", "alphaengine-discovery:eed2391e2a555d0ee1984af7",
         "2026-10-08T08:08:11.407065+00:00", "2026-10-08T08:13:05.894368+00:00"),
        ("mission-discovery-dispatch:74140e11590536bfd43440d9f9e8bcfc", "company:ticker:msft",
         "sell-side-reports", "alphaengine-discovery:cd546a54b52343d85ae1146c",
         "2026-10-08T08:33:24.402505+00:00", "2026-10-08T08:38:17.459060+00:00"),
        ("mission-discovery-dispatch:ea615ce4bd4da54102fcd4b0f4387d2e", "company:sec-cik:0001467373",
         "earnings-call-transcripts", "alphaengine-discovery:26d1b57f587b231d5228a01f",
         "2026-10-08T08:09:00.714137+00:00", "2026-10-08T08:13:53.924456+00:00"),
    )

    def live_row(self, entry, reason="search outcome retryable"):
        dispatch_id, company_ref, spec_ref, ticket, created, updated = entry
        return {"dispatch_id": dispatch_id, "company_ref": company_ref, "spec_ref": spec_ref,
                "source_ref": "source:alphaengine", "status": "failed",
                "failure_reason": reason, "ticket_ref": ticket,
                "created_at": created, "updated_at": updated}

    def block_for(self, row):
        from unittest.mock import patch

        spec = dict(self.plan["specs"][0], spec_ref=row["spec_ref"])
        with patch.object(self.missions, "discovery_dispatches", return_value=[row]):
            return self.coordinator._cadence_block("coverage-mission-version:x", row["company_ref"], spec)

    def test_live_prefix_rows_with_journal_are_due_on_the_first_tick(self) -> None:
        """Each live row's journal says E_UPSTREAM_API...: provider_auth, due now."""

        from dalton_core.alphaengine_core_search import AlphaEngineCoreSearch
        from tests.test_mission_source_discovery import approved_governance

        self.clock.value = datetime(2026, 10, 8, 8, 45, tzinfo=timezone.utc)
        search = AlphaEngineCoreSearch(
            store=self.h.core, connectors=self.h.connectors, observability=self.h.observability,
            journal=self.h.journal, scheduler=self.h.scheduler, catalog=self.h.catalog,
            spool=self.h.spool, governance=approved_governance(), mcp_handle=LapsedHandle(),
            clock=self.clock,
        )
        for index, entry in enumerate(self.LIVE_ROWS):
            receipt = search.search(search.build_request(
                {"query": f"live row {index}", "cursor": None,
                 "filters": {"document_type": "meeting_minutes"}}))
            self.search_launcher.tickets[entry[3]] = {
                "id": entry[3], "status": "failed", "exit_code": 1,
                "summary": {"failure_reason": "search outcome retryable",
                            "search": {"connector_invocation_ref": receipt["connector_invocation_ref"]}}}
            self.assertIsNone(self.block_for(self.live_row(entry)), entry)
        self.assertEqual(held_records(self.health_dir), [])

    def test_live_prefix_rows_without_detail_get_short_retry_and_probe_reset(self) -> None:
        """The same rows with no recoverable provider words: short retry, not a day."""

        # 08:45Z: GOOGL (08:08Z), MSFT transcripts (08:13Z), ACN (08:13Z) are
        # past the short retry; MSFT sell-side (08:38Z) is seven minutes in.
        self.clock.value = datetime(2026, 10, 8, 8, 45, tzinfo=timezone.utc)
        verdicts = [self.block_for(self.live_row(entry)) for entry in self.LIVE_ROWS]
        self.assertIsNone(verdicts[0])
        self.assertIsNone(verdicts[1])
        self.assertIsNone(verdicts[3])
        self.assertIn("short retry 15m", verdicts[2])
        self.assertNotIn("retry interval 1d", verdicts[2])
        # A passed health probe after the failure resets it at once.
        self.health.record_auth_failure("source:alphaengine", kind="search", ref="x",
                                        detail={"message": UPSTREAM})
        self.health.record_probe("source:alphaengine", ok=True, detail={"method": "list_filters"})
        self.assertIsNone(self.block_for(self.live_row(self.LIVE_ROWS[2])))
        # A failure that is not this shape keeps the plan's retry interval.
        other = self.block_for(self.live_row(self.LIVE_ROWS[0], reason="rehearsed failure"))
        self.assertIn("retry interval 1d", other)
        tagged = self.block_for(self.live_row(
            self.LIVE_ROWS[0], reason="search outcome failed [provider_error] source_error: busy"))
        self.assertIn("retry interval 1d", tagged)

    def test_web_search_source_never_holds(self) -> None:
        self.assertIsNone(MissionSourceDiscoveryCoordinator._default_health(
            type("C", (), {"source_ref": "source:web-search", "search_launcher": None,
                           "acquisition_launcher": None, "clock": self.clock})()))


class OwnerVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.clock = Clock(datetime(2026, 10, 8, 8, 8, 5, tzinfo=timezone.utc))
        self.health = ConnectorHealth(self.state, clock=self.clock)
        detail = {"code": "provider_auth", "message": UPSTREAM}
        self.health.record_auth_failure("source:alphaengine", kind="search", ref="d1",
                                        detail=detail, at="2026-10-08T08:03:31+00:00")
        self.health.record_auth_failure("source:alphaengine", kind="search", ref="d2",
                                        detail=detail)
        self.health.record_auth_failure("source:alphaengine", kind="acquisition", ref="r1",
                                        detail=detail)

    def test_needs_you_card_names_the_login_lapse_since_and_counts(self) -> None:
        items = connector_login_lapses(self.state, now=self.clock())
        self.assertEqual(len(items), 1)
        card = items[0]
        self.assertEqual(card["kind"], "connector_login_expired")
        self.assertEqual(
            f"{card['title']}；{card['action'].split('（')[0]}",
            "AlphaEngine 登录状态失效：服务器要求刷新 token（desktop 显示已就绪但请求被拒）；"
            "请在 AlphaEngine Desktop 重新登录/刷新，恢复后系统自动继续")
        self.assertIn("2026年10月8日 16:03", card["why_blocked"])
        self.assertIn("2 次资料检索", card["why_blocked"])
        self.assertIn("1 次原文获取", card["why_blocked"])
        self.assertIn(UPSTREAM, card["why_blocked"])
        self.assertEqual(card["detail"]["blocked_searches"], 2)
        self.assertEqual(card["detail"]["blocked_acquisitions"], 1)
        self.assertTrue(card["actionable"])
        listed = collect(state_dir=self.state, clock=self.clock)
        self.assertIn("connector_login_expired", listed["by_kind"])

    def test_card_disappears_after_probe_passes(self) -> None:
        self.clock.advance(minutes=20)
        self.health.record_probe("source:alphaengine", ok=True, detail={"method": "list_filters"})
        self.assertEqual(held_records(self.state), [])
        self.assertEqual(connector_login_lapses(self.state, now=self.clock()), [])

    def test_failed_probe_extends_hold(self) -> None:
        before = read_record(self.state, "source:alphaengine")["hold_until"]
        self.clock.advance(minutes=20)
        self.assertTrue(self.health.probe_due("source:alphaengine"))
        after = self.health.record_probe("source:alphaengine", ok=False,
                                         detail={"message": UPSTREAM})
        self.assertGreater(after["hold_until"], before)
        self.assertFalse(self.health.probe_due("source:alphaengine"))
        self.assertEqual(after["probes"], 1)

    def test_parity_warn_row(self) -> None:
        env = Environment("ws-test", self.state)
        try:
            rows = connector_health_rows(env)
        finally:
            env.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "warn")
        self.assertEqual(rows[0]["check"], "connector_health.alphaengine.login")
        self.assertIn("provider_auth", rows[0]["detail"])
        self.assertIn("AlphaEngine Desktop", rows[0]["fix"])
        self.health.record_probe("source:alphaengine", ok=True)
        env = Environment("ws-test", self.state)
        try:
            self.assertEqual(connector_health_rows(env)[0]["status"], "ok")
        finally:
            env.close()

    def test_cockpit_sources_row_reads_login_expired(self) -> None:
        from types import SimpleNamespace

        from dalton_core.cockpit_plane import CockpitPlane

        rows = [{"source_ref": "source:alphaengine", "slug": "alphaengine",
                 "connector": {"status": "installed", "status_label": "本机连接器目录已登记"},
                 "status_label": "已连接"},
                {"source_ref": "source:sec-edgar", "slug": "sec-edgar",
                 "connector": {"status": "installed"}, "status_label": "已连接"}]
        fake = SimpleNamespace(config=SimpleNamespace(state_dir=self.state))
        CockpitPlane._mark_login_lapses(fake, rows)
        self.assertEqual(rows[0]["connector"]["status"], "login_expired")
        self.assertEqual(rows[0]["connector"]["status_label"], "登录失效")
        self.assertEqual(rows[0]["health"]["blocked_searches"], 2)
        self.assertIn("AlphaEngine Desktop", rows[0]["health_note"])
        self.assertNotIn("health", rows[1])

    def test_probe_is_rehearsal_safe_without_network(self) -> None:
        state = self.state / "launcher-state"
        state.mkdir()
        plan = self.state / "plan.json"
        plan.write_text(json.dumps(plan_for_tests()), encoding="utf-8")
        launcher = AlphaEngineSearchLauncher(
            state_dir=state, governance_path=self.state / "gov.json", plan_path=plan,
            mode_args=("--fake-search-file", str(self.state / "r.json")))
        self.assertTrue(launcher.probe_health()["ok"])
        self.assertEqual(AlphaEngineSearchLauncher.PROBE_TOOL, "list_filters")


if __name__ == "__main__":
    unittest.main()
