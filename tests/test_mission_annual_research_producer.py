"""The research-task producer step that writes mission annual-research admissions."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.dossier_repair_feedback import read_dossier_repair_feedback
from dalton_core.mission_annual_research_lane import MissionAnnualResearchCoordinator
from dalton_core.mission_annual_research_producer import (
    LEDGER_FILE, MissionAnnualResearchProducer, producer_run_budget,
    query_terms_for_target,
)
from dalton_core.store import content_hash
from tests.test_mission_annual_research import COMPANY, MissionAnnualFixture
from tests.test_mission_annual_research_lane import _Launcher


class QueryTermTests(unittest.TestCase):
    def test_figures_are_searched_as_written_and_as_a_filing_prints_them(self) -> None:
        self.assertEqual(
            query_terms_for_target({"figure": "970000000"}),
            ["970000000", "970,000,000", "970 million"],
        )
        self.assertEqual(
            query_terms_for_target({"figure": "2500000000"}),
            ["2500000000", "2,500,000,000", "2.5 billion", "2500 million"],
        )
        self.assertEqual(query_terms_for_target({"figure": "15.8%"}), ["15.8%"])
        self.assertEqual(query_terms_for_target({"figure": "188.6"}), ["188.6"])

    def test_small_integers_and_chinese_prose_are_not_anchors(self) -> None:
        self.assertEqual(query_terms_for_target({"figure": "14"}), [])
        self.assertEqual(query_terms_for_target({
            "unit": "catalyst_calendar", "code": "missing_evidence",
            "detail": "缺少业绩发布、投资者日、合同续约的确认日期。",
        }), [])

    def test_latin_phrases_in_the_detail_are_copied_not_translated(self) -> None:
        terms = query_terms_for_target({
            "detail": "现有材料提及净推荐值（NPS）、DXC 的 Hogan AI 架构和 FY2026 的 10-K。",
        }, exclude=frozenset({"dxc"}))
        self.assertEqual(terms, ["nps", "hogan ai"])

    def test_packaged_run_budget_bounds_the_producer(self) -> None:
        budget = producer_run_budget()
        self.assertEqual(budget["max_admissions_per_tick"], 1)
        self.assertEqual(budget["max_units"], 3)
        self.assertEqual(budget["max_cost_usd"], 120.0)


class ProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = MissionAnnualFixture(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.factory_calls = 0

    def _target(self) -> dict:
        return read_dossier_repair_feedback(self.fixture.state)[COMPANY]["repair_targets"][0]

    def _inquiry(self, **extra) -> dict:
        target = self._target()
        return {
            "rank": 0, "company_ref": COMPANY,
            "question": "客户留存率的口径是什么？",
            "wants": "年报里对 retention 分子分母的定义",
            "because": "Dossier KPI 字典缺少有出处的定义",
            "repair_target_ref": target["id"],
            "repair_target_hash": target["content_hash"],
            **extra,
        }

    def _plan(self, inquiries: list[dict], state: str = "a") -> dict:
        plan = {
            "state_hash": state * 64, "assessment": "where the research stands",
            "mission_version_ref": self.fixture.mission["id"],
            "directives": [], "inquiries": inquiries, "sufficiency": [],
        }
        plan["content_hash"] = content_hash(plan)
        return CoverageMissionAuthority(self.fixture.store).record_research_plan(
            plan, decided_by="automation:test")

    def _producer(self, *, factory=None, **budget) -> MissionAnnualResearchProducer:
        def counted():
            self.factory_calls += 1
            return nullcontext(self.fixture.authority)

        return MissionAnnualResearchProducer(
            store=self.fixture.store, state_dir=self.fixture.state,
            ledger_dir=self.root / "tickets",
            authority_factory=factory or counted,
            clock=self.fixture.harness.clock,
            run_budget={"max_admissions_per_tick": 1, "max_units": 3,
                        "max_cost_usd": 120.0, **budget},
        )

    def _count(self) -> int:
        return self.fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_admissions").fetchone()[0]

    def test_lane_admits_from_the_planner_inquiry_and_launches_it_in_one_tick(self) -> None:
        plan = self._plan([self._inquiry()])
        launcher = _Launcher(self.root / "tickets")
        lane = MissionAnnualResearchCoordinator(
            store=self.fixture.store, launcher=launcher, producer=self._producer(),
        )
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        [produced] = result["produced"]
        self.assertEqual(produced["status"], "fresh")
        self.assertEqual(produced["plan_ref"], plan["plan_id"])
        self.assertEqual(launcher.started[0][0], produced["admission_ref"])
        self.assertIn("写入年报研究 admission", result["reason"])
        admission = self.fixture.authority.admission(produced["admission_ref"])
        self.assertEqual(admission["actor_ref"], "automation:test")
        self.assertEqual(admission["planner_inquiry"], self._inquiry())
        self.assertEqual(admission["repair_target_ref"], self._target()["id"])
        self.assertEqual(admission["request"]["query_terms"],
                         ["retention numerator and denominator"])
        self.assertIn("No term was translated", admission["query_rationale"])
        # The admission it wrote is the one the executor will re-resolve.
        self.assertEqual(
            self.fixture.authority.resolve_for_execution(admission["id"])["id"],
            admission["id"],
        )

    def test_one_admission_per_company_10k_and_target(self) -> None:
        self._plan([self._inquiry()])
        producer = self._producer()
        self.assertEqual(producer.produce_once()["status"], "admitted")
        # The same target asked again in a later plan, reworded, buys nothing.
        self._plan([self._inquiry(question="客户留存的计算方式？")], state="b")
        again = producer.produce_once()
        self.assertEqual(again["status"], "declined")
        self.assertEqual(again["decline"], "already_admitted")
        self.assertEqual(self._count(), 1)
        self.assertEqual(self.factory_calls, 1)

    def test_daily_cap_and_cost_ceiling_decline_before_any_render(self) -> None:
        self._plan([self._inquiry()])
        capped = self._producer(max_units=0).produce_once()
        self.assertEqual(capped["decline"], "daily_cap_reached")
        ceiling = self._producer(max_cost_usd=0.01).produce_once()
        self.assertEqual(ceiling["decline"], "daily_cost_ceiling_reached")
        self.assertEqual(self._count(), 0)
        self.assertEqual(self.factory_calls, 0)

    def test_missing_inputs_are_named_in_order(self) -> None:
        producer = self._producer()
        self.assertEqual(producer.produce_once()["decline"],
                         "no_planner_inquiry_for_current_target")
        # A directed-document inquiry belongs to the document lane.
        self._plan([self._inquiry(directed_document={"strategy_version": "x"})])
        self.assertEqual(producer.produce_once()["decline"],
                         "no_planner_inquiry_for_current_target")
        # A newer Dossier run with no gaps retires the target.
        self._plan([self._inquiry()], state="b")
        self.fixture._write_feedback("cleared", targets=[])
        cleared = producer.produce_once()
        self.assertEqual(cleared["decline"], "no_registered_repair_target")
        self.assertEqual(cleared["counts"]["registered_repair_targets"], 0)
        self.assertEqual(self._count(), 0)

    def test_target_without_a_literal_anchor_is_declined_by_name(self) -> None:
        self.fixture._write_feedback("chinese", targets=[{
            "unit": "kpi_dictionary", "code": "missing_evidence",
            "detail": "需补充净推荐值的受访客户范围与计算公式。",
        }])
        self._plan([self._inquiry()])
        result = self._producer().produce_once()
        self.assertEqual(result["decline"], "no_literal_anchor")
        self.assertEqual(result["counts"]["no_literal_anchor"], 1)
        self.assertEqual(self.factory_calls, 0)

    def test_authority_refusal_is_remembered_for_the_day(self) -> None:
        self._plan([self._inquiry()])

        @contextmanager
        def refusing():
            self.factory_calls += 1
            raise RuntimeError("installed annual-report draft route has no eligible model")
            yield  # pragma: no cover

        producer = self._producer(factory=refusing)
        first = producer.produce_once()
        self.assertEqual(first["decline"], "authority_refused")
        self.assertIn("no eligible model", first["error"])
        second = producer.produce_once()
        self.assertEqual(second["decline"], "declined_earlier_today")
        self.assertEqual(self.factory_calls, 1)
        ledger = json.loads((self.root / "tickets" / LEDGER_FILE).read_text())
        self.assertEqual(ledger["day"], "2026-09-11")
        self.assertEqual(len(ledger["declined"]), 1)
        self.assertEqual(self._count(), 0)

    def test_idle_lane_reason_names_the_missing_input(self) -> None:
        lane = MissionAnnualResearchCoordinator(
            store=self.fixture.store, launcher=_Launcher(self.root / "tickets"),
            producer=self._producer(),
        )
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["producer"], "no_planner_inquiry_for_current_target")
        self.assertEqual(result["producer_registered_repair_targets"], 1)
        self.assertIn("planner inquiry", result["reason"])
        self.assertNotIn("没有任何 CLI", result["reason"])

    def test_lane_without_a_producer_says_so(self) -> None:
        lane = MissionAnnualResearchCoordinator(
            store=self.fixture.store, launcher=_Launcher(self.root / "tickets"),
        )
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertIn("没有配置 admission 生产步骤", result["reason"])

    def test_a_failing_producer_is_a_visible_stall_not_a_quiet_idle(self) -> None:
        class Broken:
            def produce_once(self):
                raise ValueError("ledger drifted")

        lane = MissionAnnualResearchCoordinator(
            store=self.fixture.store, launcher=_Launcher(self.root / "tickets"),
            producer=Broken(),
        )
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("ledger drifted", result["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
