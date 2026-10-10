"""Cockpit v2 financial charts: payloads on fixture data and an empty
workspace, the route adapter, and the renderers run under node."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core.cockpit_v2 import charts_api
from dalton_core.cockpit_v2.charts import routes as chart_routes
from dalton_core.cockpit_v2.charts_api import ChartReader, ChartRequestError, chart_payload
from tests.p14a_fixtures import ACN, AUTOMATION, EPAM, OWNER, bar

CHARTS_DIR = Path(charts_api.__file__).resolve().parent / "charts"
COMPANY_KINDS = ("price", "model", "scenarios", "returns", "bridge")
NODE = shutil.which("node")


def _model_record(company_ref: str) -> dict:
    """A small forecast model: two filed quarters | two forecast quarters."""

    def period(start, end):
        return {"calendar": "company:fiscal", "kind": "quarter", "start": start, "end": end}

    hist = [("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
    fc = [period("2026-07-01", "2026-09-30"), period("2026-10-01", "2026-12-31")]

    def history(values, basis=("reported", "reported")):
        return [{"period_start": s, "period_end": e, "value": v, "basis": b, "accessions": ["acc-q"]}
                for (s, e), v, b in zip(hist, values, basis)]

    def cells(ref, values, status="computed", reason=None):
        return [{"ref": f"{ref}@{p['end']}", "period": p, "kind": "estimate", "status": status,
                 "value": v, "reason": reason, "superseded_by": None} for p, v in zip(fc, values)]

    return {
        "id": "forecast-model-version:test:3", "version": 3, "company_ref": company_ref,
        "created_at": "2026-10-01T00:00:00+00:00", "currency": "USD", "unit": "usd",
        "history_periods": [e for _, e in hist], "realised_periods": [], "forecast_periods": fc,
        "drivers": [
            {"ref": "concept:us-gaap:Revenues", "label": "Revenues", "role": "revenue",
             "structure_line_ref": "revenue", "concept": "us-gaap:Revenues", "unit": "usd",
             "statement": "income", "history": history(["10000000000", "11000000000"])},
            {"ref": "concept:us-gaap:CostOfRevenue", "label": "Cost of revenue", "role": "cost_of_revenue",
             "structure_line_ref": "cost", "concept": "us-gaap:CostOfRevenue", "unit": "usd",
             "statement": "income", "history": history(["6000000000", "6500000000"], ("reported", "derived_from_cumulative"))},
        ],
        "results": [
            {"ref": "result:revenue", "role": "revenue", "label": "Revenues", "unit": "usd", "status": "computed",
             "cells": cells("result:revenue", ["11500000000", "12000000000"])},
            {"ref": "result:cost_of_revenue", "role": "cost_of_revenue", "label": "Cost of revenue", "unit": "usd",
             "status": "computed", "cells": cells("result:cost_of_revenue", ["6800000000", "7000000000"])},
            {"ref": "result:operating_income", "role": "operating_income", "label": "Operating income",
             "unit": "usd", "status": "unavailable",
             "reason": "formula terms unavailable for this quarter: cost, opex",
             "cells": cells("result:operating_income", [None, None], "unavailable",
                            "formula terms unavailable for this quarter: cost, opex")},
        ],
        "financial_statement_structure": {"lines": [], "formulas": [
            {"operator": "sum", "output_ref": "gross-profit",
             "terms": [{"coefficient": "1", "line_ref": "revenue"}, {"coefficient": "-1", "line_ref": "cost"}]}]},
        "financial_statement_structure_replay": {"formulas": [{"output_ref": "gross-profit", "status": "validated"}]},
    }


def _price_record(company_ref: str, closes: list[str]) -> dict:
    days = [f"2026-09-{day:02d}" for day in range(1, len(closes) + 1)]
    bars = [bar(day, close, captured_at="2026-09-30T23:30:00+00:00") for day, close in zip(days, closes)]
    return {"id": "market-price-series-version:test:1", "company_ref": company_ref, "currency": "USD",
            "first_bar_date": days[0], "last_bar_date": days[-1], "bars": bars, "version": 1}


MISSION = {"id": "coverage-mission-version:test:1", "mission_ref": "coverage-mission:test", "version": 1,
           "content_hash": "e" * 64,
           "industry_ref": "industry:test",
           "autonomy": {"automation_principal": AUTOMATION, "may_write": ["valuation", "dossier"]},
           "universe": [{"company_ref": ACN, "ticker": "ACN", "name": "Accenture"},
                        {"company_ref": EPAM, "ticker": "EPAM"}]}

STAND_INS = """
CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY, mission_ref TEXT,
    version_number INTEGER, record_json TEXT);
CREATE TABLE coverage_mission_pointer(mission_ref TEXT PRIMARY KEY, mission_version_id TEXT,
    updated_at TEXT);
CREATE TABLE coverage_mission_stage_records(record_id TEXT PRIMARY KEY, mission_version_ref TEXT,
    company_ref TEXT, stage_ref TEXT, status TEXT, created_at TEXT);
CREATE TABLE forecast_model_versions(version_id TEXT PRIMARY KEY, model_ref TEXT, version_number INTEGER,
    company_ref TEXT, record_json TEXT, created_at TEXT);
CREATE TABLE market_price_series_versions(version_id TEXT PRIMARY KEY, series_ref TEXT,
    version_number INTEGER, company_ref TEXT, currency TEXT, record_json TEXT);
"""


class FixtureCore(unittest.TestCase):
    """A Core on disk: real ADR-0012/0014 authorities, stand-ins for the
    tables whose writers need their lanes' authorization functions."""

    def setUp(self) -> None:
        # Explicit old records exercise historical chart readability; new publication is tested separately.
        from tests.test_company_scenario import legacy_scenario_fixture
        legacy_scenario_fixture(self)
        from dalton_core.company_scenario import CompanyScenarioAuthority
        from dalton_core.return_attribution import refresh_company
        from dalton_core.store import DaltonStore
        from tests.test_company_scenario import scenario
        from tests.test_return_attribution import calendar_company_filings, install_filings

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = Path(self._dir.name) / "core.sqlite"
        self.store = DaltonStore(str(path))
        self.addCleanup(self.store.close)
        self.reader = ChartReader(path)
        connection = self.store.connection
        connection.executescript(STAND_INS)
        connection.execute("INSERT INTO coverage_mission_versions VALUES(?,?,?,?)",
                           (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION)))
        connection.execute("INSERT INTO coverage_mission_pointer VALUES(?,?,?)",
                           (MISSION["mission_ref"], MISSION["id"], "2026-10-01T00:00:00+00:00"))
        connection.execute("INSERT INTO coverage_mission_stage_records VALUES(?,?,?,?,?,?)",
                           ("stage:1", MISSION["id"], ACN, "company_model", "entered", "2026-10-01T00:00:00+00:00"))
        record = _model_record(ACN)
        connection.execute("INSERT INTO forecast_model_versions VALUES(?,?,?,?,?,?)",
                           (record["id"], f"forecast-model:{ACN}", 3, ACN, json.dumps(record), record["created_at"]))
        price = _price_record(ACN, [str(100 + i) for i in range(12)])
        connection.execute("INSERT INTO market_price_series_versions VALUES(?,?,?,?,?,?)",
                           (price["id"], f"market-price-series:{ACN}", 1, ACN, "USD", json.dumps(price)))
        connection.commit()
        install_filings(connection, ACN, calendar_company_filings())
        refresh_company(self.store, MISSION, ACN)
        authority = CompanyScenarioAuthority(self.store)
        proposal = authority.propose_set(
            company_ref=ACN, scenarios=[scenario("base", "0.6"), scenario("bear", "0.4")],
            anchors={"dossier_version_ref": "company-dossier-version:test:1"},
            proposer={"kind": "model", "purpose": "scenario_proposal"},
            mission=MISSION, actor_ref=AUTOMATION)
        authority.decide_set(set_ref=proposal["id"], set_hash=proposal["content_hash"],
                             decision="accept", reason="ok", actor_ref=OWNER)

    def payload(self, kind: str, **query: str) -> dict:
        value = chart_payload(kind, query, self.reader)
        json.dumps(value)  # every payload is JSON as it stands
        return value


class PayloadTests(FixtureCore):

    def test_every_payload_carries_as_of_currency_unit_period(self) -> None:
        for kind in COMPANY_KINDS:
            value = self.payload(kind, company=ACN)
            self.assertEqual(value["schema_version"], charts_api.SCHEMA_VERSION)
            self.assertIn(value["status"], {"ok", "empty"}, kind)
            if value["status"] == "ok":
                self.assertTrue(value["data_as_of"], kind)
                self.assertTrue(value["unit"], kind)
                self.assertTrue((value["period"] or {}).get("label"), kind)

    def test_price_matches_the_old_cockpit_changes_and_marks_filings(self) -> None:
        value = self.payload("price", company=ACN)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["data_as_of"], "2026-09-12")
        self.assertEqual(value["currency"], "USD")
        self.assertEqual(len(value["points"]), 12)
        five = next(c for c in value["changes"] if c["days"] == 5)
        # CockpitPlane._market: 111 vs 106 -> +4.7%, sign printed
        self.assertEqual(five["display"], "+4.7%")
        self.assertEqual(next(c for c in value["changes"] if c["days"] == 30)["status"], "missing")
        self.assertIn("历史收盘数据不足", next(c for c in value["changes"] if c["days"] == 30)["missing_reason"])
        self.assertEqual(value["metrics"], [])
        self.assertIn("估值快照", value["metrics_missing_reason"])
        with self.assertRaises(ChartRequestError):
            self.payload("price", company=ACN, range="10y")

    def test_model_separates_history_from_forecast_and_names_every_gap(self) -> None:
        value = self.payload("model", company=ACN)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["boundary_index"], 2)
        self.assertEqual([c["kind"] for c in value["columns"]], ["history", "history", "forecast", "forecast"])
        self.assertEqual(value["unit"], "亿美元")
        revenue = next(r for r in value["rows"] if r["role"] == "revenue")
        self.assertEqual(revenue["label"], "营业收入")
        self.assertEqual([c["display"] for c in revenue["cells"]], ["100.00", "110.00", "115.00", "120.00"])
        self.assertEqual([c["basis"] for c in revenue["cells"]], ["reported", "reported", "estimate", "estimate"])
        cost = next(r for r in value["rows"] if r["role"] == "cost_of_revenue")
        self.assertEqual(cost["cells"][1]["basis"], "derived")
        operating = next(r for r in value["rows"] if r["role"] == "operating_income")
        self.assertEqual([c["status"] for c in operating["cells"]], ["missing"] * 4)
        self.assertIn("本季度缺少公式所需项目", operating["cells"][2]["reason"])
        self.assertTrue(all(c["reason"] for c in operating["cells"]))
        gross = next(m for m in value["series"]["margins"] if m["key"] == "gross_margin")
        self.assertEqual([p["display"] for p in gross["points"]], ["40.0%", "40.9%", "40.9%", "41.7%"])
        self.assertEqual([p["basis"] for p in gross["points"]], ["reported", "derived", "estimate", "estimate"])
        self.assertEqual(value["completeness"]["label"], "利润表 2/3")
        self.assertEqual(value["completeness"]["results_computed"], 2)
        # the revenue series is plotted in the table's unit, never raw dollars
        self.assertEqual(value["series"]["revenue"]["points"][0]["plot_value"], "100.0000")

    def test_scenarios_order_probabilities_and_show_the_sum(self) -> None:
        value = self.payload("scenarios", company=ACN)
        self.assertEqual(value["status"], "ok")
        self.assertEqual([s["key"] for s in value["scenarios"]], ["bear", "base"])
        self.assertEqual([s["probability_display"] for s in value["scenarios"]], ["40%", "60%"])
        self.assertEqual(value["probability_sum_display"], "100%")
        self.assertTrue(value["probability_sum_ok"])
        self.assertEqual(value["state"], "in_force")
        for s in value["scenarios"]:
            self.assertIsNone(s["target_label"])
            self.assertIn("尚无目标价", s["target_missing_reason"])

    def test_bridge_binds_each_bar_to_its_statement_line_and_one_basis(self) -> None:
        value = self.payload("bridge", company=ACN)
        self.assertEqual(value["status"], "ok")
        bridge = value["bridge"]
        self.assertEqual(bridge["semantics"], "quarterly")  # 单季 by default
        anchors = [s for s in bridge["steps"] if s["type"] == "anchor"]
        self.assertTrue(anchors and all(s["statement"]["accession"] for s in anchors))
        self.assertTrue(all(s["statement"]["ref"].startswith("statement-line:") for s in anchors))
        for step in bridge["steps"]:
            if step["type"] == "step" and Decimal(step["value"]) != 0:
                self.assertRegex(step["display"], r"^[+−]")
        annual = next(o for o in value["options"] if o["semantics"] == "annual")
        again = self.payload("bridge", company=ACN, bridge=annual["ref"])
        self.assertEqual(again["bridge"]["semantics"], "annual")
        self.assertIn("完整财年", again["period"]["label"])

    def test_returns_without_inputs_is_an_honest_empty_state(self) -> None:
        value = self.payload("returns", company=ACN)
        self.assertEqual(value["status"], "empty")
        self.assertIn("还没有估值快照", value["empty_reason"])

    def test_return_waterfall_steps_add_up_in_percentage_points(self) -> None:
        leg = charts_api._leg({
            "leg": "expected_driver_model", "label": "预期：我们的驱动模型", "status": "available",
            "window": {"start": "2026-07-01", "end": "2027-06-30"}, "price_return_percent": "6.18",
            "total_return_percent": "9.42", "expectation_source_kind": "market_implied",
            "earnings_basis": "driver_model_revenue_x_trailing_net_margin", "share_basis": "held_flat",
            "contributions_pp": {"earnings_growth": "6.18", "share_count": "0.00",
                                 "multiple_change": "0.00", "dividends": "3.24"}})
        self.assertEqual([s["key"] for s in leg["steps"]],
                         ["earnings_growth", "share_count", "multiple_change", "price_return", "dividends", "total_return"])
        self.assertEqual(leg["steps"][-2]["end_pp"], "9.42")
        self.assertEqual(leg["steps"][0]["display"], "+6.18 pp")
        self.assertIn("粗算估算", leg["basis_label"])
        self.assertIn("代理口径", leg["basis_label"])
        self.assertTrue(leg["estimated"])
        realised = charts_api._leg({"leg": "realised_trailing_year", "label": "已实现", "status": "available",
                                    "price_return_percent": "-21.73", "total_return_percent": "-19.19",
                                    "contributions_pp": {"earnings_growth": "0.69", "share_count": "1.97",
                                                         "multiple_change": "-24.39", "dividends": "2.54"}})
        self.assertEqual(realised["group"], "realised")
        self.assertEqual(realised["steps"][2]["display"], "−24.39 pp")
        self.assertEqual(realised["total_display"], "−19.19%")

    def test_comparison_ranks_only_one_basis_and_says_incomparable(self) -> None:
        value = self.payload("comparison")
        self.assertEqual(value["status"], "ok")
        tickers = {row["company_ref"] for row in value["rows"]}
        self.assertIn(ACN, tickers)
        rows = [{"ticker": "A", "cells": {"x": {"value": "1", "basis_key": "w1", "basis_label": "窗口一"}}},
                {"ticker": "B", "cells": {"x": {"value": "2", "basis_key": "w1", "basis_label": "窗口一"}}},
                {"ticker": "C", "cells": {"x": {"value": "9", "basis_key": "w2", "basis_label": "窗口二"}}}]
        basis = charts_api._basis_column(rows, "x")
        self.assertEqual(basis["basis_key"], "w1")
        self.assertFalse(rows[2]["cells"]["x"]["comparable"])
        self.assertIn("口径暂不可比（预测周期不一致）", rows[2]["cells"]["x"]["incomparable_reason"])
        self.assertIn("窗口一", rows[2]["cells"]["x"]["incomparable_reason"])
        with self.assertRaises(ChartRequestError):
            self.payload("comparison", sort="beauty")
        with self.assertRaises(ChartRequestError):
            self.payload("comparison", dir="sideways")

    def test_comparison_rule_is_one_and_ties_take_the_latest_window(self) -> None:
        value = self.payload("comparison")
        self.assertEqual(value["rule"]["id"], "same-basis-same-window-v1")
        self.assertEqual(value["dir"], "desc")
        rows = [{"ticker": "A", "cells": {"x": {"value": "1", "basis_key": "2026-06-01..2027-05-31", "basis_label": "早"}}},
                {"ticker": "B", "cells": {"x": {"value": "2", "basis_key": "2026-07-01..2027-06-30", "basis_label": "晚"}}}]
        self.assertEqual(charts_api._basis_column(rows, "x")["basis_key"], "2026-07-01..2027-06-30")
        # rank_rows: comparable by value, then 不可比较, then missing; asc flips only the first tier
        ranked = [{"ticker": t, "cells": {"expected": c}} for t, c in (
            ("M", {"value": None, "comparable": False}), ("I", {"value": "50", "comparable": False}),
            ("L", {"value": "1", "comparable": True}), ("H", {"value": "9", "comparable": True}))]
        self.assertEqual([r["ticker"] for r in charts_api.rank_rows(ranked, "return")], ["H", "L", "I", "M"])
        self.assertEqual([r["ticker"] for r in charts_api.rank_rows(ranked, "return", "asc")], ["L", "H", "I", "M"])

    def test_forecast_flags_say_started_expired_and_simplified(self) -> None:
        leg = {"leg": "expected_driver_model", "window": {"start": "2026-06-01", "end": "2027-05-31"},
               "earnings_basis": "driver_model_revenue_x_trailing_net_margin", "share_basis": "held_flat"}
        flags = charts_api._forecast_flags(leg, "2026-10-08", "2026-10-07T10:00:00+00:00")
        texts = [b["text"] for b in flags["badges"]]
        self.assertEqual(texts, ["预测期已开始", "粗算估算", "预测生成于2026年10月7日"])
        self.assertIn("股数按最近一期持平", flags["simplified_label"])
        expired = charts_api._forecast_flags({**leg, "window": {"start": "2025-06-01", "end": "2026-05-31"}}, "2026-10-08", None)
        self.assertEqual(expired["badges"][0]["text"], "已到期未更新")

    def test_a_company_without_data_reads_the_stage_ledger(self) -> None:
        value = self.payload("model", company=EPAM)
        self.assertEqual(value["status"], "empty")
        self.assertIn("还没有预测模型", value["empty_reason"])
        self.assertIn("研究还没有开始", value["empty_reason"])
        value = self.payload("returns", company=ACN)
        self.assertEqual(value["status"], "empty")
        value = self.payload("scenarios", company=ACN.replace("1467373", "0000000"))
        self.assertEqual(value["status"], "empty")

    def test_a_cockpit_plane_is_a_valid_context(self) -> None:
        from datetime import datetime, timezone
        from types import SimpleNamespace

        plane = SimpleNamespace(
            config=SimpleNamespace(core_db=self.reader.core_db, mission_ref=MISSION["mission_ref"]),
            clock=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc),
            _mission_company_names=lambda: {EPAM: "EPAM Systems"})
        value = chart_payload("comparison", {}, plane)
        self.assertEqual(value["generated_at"], "2026-10-08T00:00:00Z")
        # batch ah: "Name TICKER", the same as the company page header.
        self.assertIn("EPAM Systems EPAM", [row["company"] for row in value["rows"]])
        status, _, body = chart_routes.handle_chart_request("/v2/data/charts/model", {"company": ACN},
                                                             {"plane": plane})
        self.assertEqual((status, json.loads(body)["status"]), (200, "ok"))

    def test_reader_is_read_only(self) -> None:
        with self.reader.core() as core:
            with self.assertRaises(Exception):
                core.execute("CREATE TABLE should_fail(x)")


class EmptyWorkspaceTests(unittest.TestCase):

    def test_no_core_and_a_fresh_core_never_raise(self) -> None:
        from dalton_core.store import DaltonStore

        with tempfile.TemporaryDirectory() as tmp:
            readers = [ChartReader(Path(tmp) / "absent.sqlite")]
            store = DaltonStore(str(Path(tmp) / "core.sqlite"))
            store.close()
            readers.append(ChartReader(Path(tmp) / "core.sqlite"))
            for reader in readers:
                for kind in COMPANY_KINDS:
                    value = chart_payload(kind, {"company": ACN}, reader)
                    self.assertEqual(value["status"], "empty", kind)
                    self.assertTrue(value["empty_reason"], kind)
                value = chart_payload("comparison", {}, reader)
                self.assertEqual(value["status"], "empty")
                self.assertIn("研究", value["empty_reason"])

    def test_requests_that_cannot_be_answered_are_400_not_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = {"core_db": str(Path(tmp) / "absent.sqlite")}
            status, _, body = chart_routes.data_handler("model")({}, context)
            self.assertEqual(status, 400)
            self.assertIn("company", json.loads(body)["message"])
            status, kind, body = chart_routes.data_handler("model")({"company": ACN}, context)
            self.assertEqual((status, kind), (200, chart_routes.JSON_TYPE))
            self.assertEqual(json.loads(body)["status"], "empty")
            self.assertEqual(chart_routes.handle_chart_request("/v2/data/charts/nope", {}, context)[0], 404)
            self.assertIsNone(chart_routes.handle_chart_request("/v2/other", {}, context))
            with self.assertRaises(ChartRequestError):
                chart_payload("pie", {}, context)


class RouteTests(unittest.TestCase):

    def test_register_adapts_to_the_shell_route_table(self) -> None:
        table: dict = {}
        routes = chart_routes.register_chart_routes(table)
        for kind in charts_api.KINDS:
            self.assertIn(("GET", f"/v2/data/charts/{kind}"), table)
        self.assertIn(("GET", "/v2/assets/charts/charts.mjs"), table)

        class App:
            def __init__(self) -> None:
                self.seen = []

            def add_get(self, path, handler) -> None:
                self.seen.append(path)

        app = App()
        chart_routes.register_chart_routes(app)
        self.assertEqual(sorted(app.seen), sorted(routes))

    def test_assets_are_served_and_only_shipped_files(self) -> None:
        status, kind, body = chart_routes.asset_bytes("charts.mjs")
        self.assertEqual((status, kind), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"registerWidget", body)
        self.assertEqual(chart_routes.asset_bytes("../charts_api.py")[0], 404)
        for name in chart_routes.ASSETS:
            self.assertTrue((CHARTS_DIR / name).is_file(), name)


class SourceRuleTests(unittest.TestCase):

    def test_no_library_no_network_no_innerhtml(self) -> None:
        for path in CHARTS_DIR.glob("*.mjs"):
            text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
            self.assertFalse("innerHTML" in text, path.name)
            self.assertNotRegex(text, r"https?://(?!www\.w3\.org/2000/svg)", path.name)
            self.assertNotRegex(text, r"import .* from ['\"](?!\./)", path.name)
            self.assertNotIn("animation", text, path.name)

    def test_css_uses_the_validated_palette_and_readable_sizes(self) -> None:
        css = re.sub(r"/\*.*?\*/", "", (CHARTS_DIR / "charts.css").read_text(encoding="utf-8"), flags=re.S)
        # Marks: the dataviz reference steps; surfaces: the cockpit's navy glass
        # composite (dark, the default) and white (light).
        for hexes in ("#2a78d6", "#e34948", "#3987e5", "#e66767", "#ffffff", "#1d243c"):
            self.assertIn(hexes, css)
        default = re.search(r"\.dv2c-root \{([^}]*)\}", css).group(1)
        self.assertIn("color-scheme: dark", default)                     # dark navy by default
        self.assertIn("--dv2c-surface: #1d243c", default)
        self.assertIn("@media (prefers-color-scheme: light)", css)
        self.assertIn('[data-theme="light"]', css)
        sizes = [int(v) for v in re.findall(r"font-size:\s*(\d+)px", css)]
        self.assertTrue(sizes and min(sizes) >= 13, sizes)
        self.assertRegex(css, r"\.dv2c-root \{[^}]*font-size: 15px")
        self.assertIsNone(re.search(r"gradient|box-shadow: 0 \d|animation|transition", css))
        self.assertIn('[data-theme="dark"]', css)

    def test_chart_text_and_marks_contrast_in_both_themes(self) -> None:
        css = re.sub(r"/\*.*?\*/", "", (CHARTS_DIR / "charts.css").read_text(encoding="utf-8"), flags=re.S)
        dark = dict(re.findall(r"--dv2c-([a-z0-9-]+): (#[0-9a-f]{6})", re.search(r"\.dv2c-root \{([^}]*)\}", css).group(1)))
        light = dict(re.findall(r"--dv2c-([a-z0-9-]+): (#[0-9a-f]{6})",
                                re.search(r':root\[data-theme="light"\] \.dv2c-root \{([^}]*)\}', css).group(1)))

        def ratio(a, b):
            def lum(h):
                c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
                c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
                return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
            x, y = lum(a), lum(b)
            return (max(x, y) + 0.05) / (min(x, y) + 0.05)

        for name, tokens in (("dark", dark), ("light", light)):
            for fg in ("ink", "ink-2", "muted"):
                for bg in ("surface", "plane", "forecast-wash"):
                    with self.subTest(theme=name, text=fg, on=bg):
                        self.assertGreaterEqual(ratio(tokens[fg], tokens[bg]), 4.5)
            for mark in ("series-1", "pos", "neg", "total"):
                with self.subTest(theme=name, mark=mark):
                    self.assertGreaterEqual(ratio(tokens[mark], tokens["surface"]), 3.0)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_palette_passes_the_dataviz_validator_when_available(self) -> None:
        validator = os.environ.get("DATAVIZ_VALIDATOR")
        if not validator or not Path(validator).is_file():
            self.skipTest("set DATAVIZ_VALIDATOR to the dataviz skill's validate_palette.js")
        for palette, mode, surface in (("#2a78d6,#e34948", "light", "#ffffff"),
                                       ("#3987e5,#e66767", "dark", "#1d243c"),
                                       ("#3987e5,#e66767", "dark", "#232a41")):
            run = subprocess.run([NODE, validator, palette, "--mode", mode, "--surface", surface],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


DOM_SHIM = r"""
class Node {
  constructor(tag) { this.tagName = tag; this.attrs = {}; this.childNodes = []; this.listeners = {}; this.style = {}; this.parentNode = null; this._text = null;
    const self = this; this.classList = {add(c) { const s = new Set((self.attrs.class || "").split(" ").filter(Boolean)); s.add(c); self.attrs.class = [...s].join(" "); },
      remove(c) { self.attrs.class = (self.attrs.class || "").split(" ").filter((x) => x && x !== c).join(" "); }}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  set hidden(v) { if (v) this.attrs.hidden = ""; else delete this.attrs.hidden; }
  get hidden() { return "hidden" in this.attrs; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.childNodes.push(c); return c; }
  removeChild(c) { this.childNodes = this.childNodes.filter((x) => x !== c); c.parentNode = null; return c; }
  get firstChild() { return this.childNodes[0] || null; }
  set textContent(v) { this.childNodes = [new Text(String(v))]; }
  get textContent() { return this.tagName === "#text" ? this._text : this.childNodes.map((c) => c.textContent).join(""); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatchEvent(e) { let n = this; while (n) { for (const f of n.listeners[e.type] || []) f(e); if (!e.bubbles) break; n = n.parentNode; } return true; }
  get clientWidth() { return Number(this.attrs["data-width"] || 0); }
  getBoundingClientRect() { return {left: 0, top: 0}; }
  all(pred, out = []) { for (const c of this.childNodes) { if (pred(c)) out.push(c); c.all(pred, out); } return out; }
  byClass(c) { return this.all((n) => (n.attrs.class || "").split(" ").includes(c)); }
}
class Text extends Node { constructor(t) { super("#text"); this._text = t; } }
globalThis.document = {createElement: (t) => new Node(t), createElementNS: (ns, t) => new Node(t), createTextNode: (t) => new Text(t), readyState: "complete"};
globalThis.CustomEvent = class { constructor(type, init = {}) { this.type = type; this.bubbles = !!init.bubbles; this.detail = init.detail; } };
globalThis.box = (w = 900) => { const c = new Node("div"); c.setAttribute("data-width", w); return c; };
"""

RENDER_SCRIPT = r"""
const m = await import(process.argv[2]);
const fs = await import("node:fs");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
const text = (n) => n.textContent;
const parts = {
  price: m.buildPriceSummary(data.price, box()), model: m.buildModel(data.model, box()),
  valuation: m.buildValuation(data.scenarios), scenarios: m.buildScenarioStatus(data.scenarios),
  returns: m.buildReturns(data.returns, box()), bridge: m.buildBridge(data.bridge, box(), {reload: () => {}}),
  comparison: m.buildComparison(data.comparison, box()), empty: m.buildModel(data.empty, box()),
  synthetic_returns: m.buildReturns(data.synthetic_returns, box()),
};
for (const [k, n] of Object.entries(parts)) out[k] = {text: text(n), empty: n.byClass("dv2c-empty").length,
  missing: n.byClass("dv2c-missing").length, bars: n.byClass("dv2c-bar").length,
  boundary: n.byClass("dv2c-boundary").length, forecast: n.byClass("dv2c-line-forecast").length,
  hits: n.byClass("dv2c-hit").filter((h) => h.attrs.tabindex === "0").length,
  twins: n.byClass("dv2c-twin").length, fills: n.byClass("dv2c-pbar-fill").map((f) => f.attrs.style)};
// clicking a bridge bar opens its statement line and tells the shell
const host = box(); host.appendChild(parts.bridge); let seen = null;
host.addEventListener("dalton:statement-line", (e) => { seen = e.detail; });
const hit = parts.bridge.byClass("dv2c-hit").find((h) => h.attrs.role === "button" && /营业利润/.test(h.attrs["aria-label"]));
for (const f of hit.listeners.click || []) f({});
out.click = {detail: seen, panel: text(parts.bridge.byClass("dv2c-panel")[0])};
// widgets register through window.DaltonV2 when the shell provides it
const slots = []; m.install({registerWidget: (slot, render) => slots.push([slot, typeof render])});
out.slots = slots;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class RendererTests(FixtureCore):

    def render(self, *, missing_total=False) -> dict:
        payloads = {kind: self.payload(kind, company=ACN) for kind in COMPANY_KINDS}
        payloads["comparison"] = self.payload("comparison")
        payloads["empty"] = self.payload("model", company=EPAM)
        synthetic = {**payloads["returns"], "status": "ok", "data_as_of": "2026-10-07",
                     "unit": "个百分点", "period": {"label": "各段窗口见每张图"},
                     "legs": [charts_api._leg({
                         "leg": "realised_trailing_year", "label": "已实现（过去 12 个月）", "status": "available",
                         "window": {"start": "2025-10-07", "end": "2026-10-07"},
                         "price_return_percent": "-21.73", "total_return_percent": "-19.19",
                         "contributions_pp": {"earnings_growth": "0.69", "share_count": "1.97",
                                              "multiple_change": "-24.39", "dividends": "2.54"}}),
                         charts_api._leg({"leg": "expected_consensus", "label": "预期：市场一致预期",
                                          "status": "unavailable", "reason": "还没有一致预期"})]}
        if missing_total:
            synthetic["legs"][0]["total_display"] = None
            synthetic["legs"][0]["total_return_percent"] = None
            synthetic["legs"][0]["reason"] = "没有完整的总回报输入；已算价格回报保留。"
        payloads["synthetic_returns"] = synthetic
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data.json"
            data.write_text(json.dumps(payloads, ensure_ascii=False), encoding="utf-8")
            script = Path(tmp) / "run.mjs"
            script.write_text(DOM_SHIM + RENDER_SCRIPT, encoding="utf-8")
            run = subprocess.run([NODE, str(script), (CHARTS_DIR / "charts.mjs").as_uri(), str(data)],
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_missing_total_is_not_null_or_zero_and_keeps_actual_reason_and_price_components(self):
        value = self.render(missing_total=True)["synthetic_returns"]["text"]
        self.assertIn("总回报 —", value)
        self.assertNotIn("总回报 null", value)
        self.assertNotIn("总回报 0", value)
        self.assertIn("没有完整的总回报输入；已算价格回报保留。", value)
        self.assertIn("−24.39", value)

    def test_renderers_show_meta_boundaries_gaps_and_identity(self) -> None:
        out = self.render()
        for key in ("price", "model", "valuation", "scenarios", "bridge", "comparison"):
            self.assertEqual(out[key]["empty"], 0, key)
            self.assertIn("数据截至", out[key]["text"], key)
        self.assertIn("币种美元（USD）", out["price"]["text"])
        self.assertIn("+4.7%", out["price"]["text"])
        self.assertGreaterEqual(out["price"]["hits"], 1)  # keyboard crosshair
        # model: a visible history | forecast boundary, dashed forecast, gaps explained
        self.assertGreaterEqual(out["model"]["boundary"], 1)
        self.assertGreaterEqual(out["model"]["forecast"], 1)
        self.assertIn("已披露历史", out["model"]["text"])
        self.assertIn("预测（估算）", out["model"]["text"])
        self.assertGreaterEqual(out["model"]["missing"], 4)
        self.assertIn("缺失说明", out["model"]["text"])
        # scenarios: probability bars sized by probability, sum printed
        self.assertEqual(out["valuation"]["fills"], ["width:40%", "width:60%"])
        self.assertIn("概率合计100% 合计为 100%", out["valuation"]["text"])
        self.assertIn("未给出目标价或回报区间", out["valuation"]["text"])
        self.assertIn("还没观察过", out["scenarios"]["text"])
        # waterfall in percentage points with signs, realised separate from expected
        synthetic = out["synthetic_returns"]
        self.assertIn("−24.39", synthetic["text"])
        self.assertNotIn("个百分点", synthetic["text"].split("增加（+）")[1])   # am 5.8: the unit is declared once
        self.assertIn("总回报 −19.19%", synthetic["text"])
        self.assertIn("事后财务归因", synthetic["text"])
        self.assertIn("暂无：还没有一致预期", synthetic["text"])
        self.assertEqual(synthetic["bars"], 6)
        self.assertGreaterEqual(synthetic["twins"], 1)
        # the honest empty state, with the stage-ledger reason
        self.assertEqual(out["empty"]["empty"], 1)
        self.assertIn("研究还没有开始", out["empty"]["text"])
        self.assertEqual(out["returns"]["empty"], 1)
        # bridge: a click opens the statement line and is announced to the shell
        self.assertTrue(out["click"]["detail"]["ref"].startswith("statement-line:"))
        self.assertIn("申报文件", out["click"]["panel"])
        self.assertIn("单季", out["bridge"]["text"])
        self.assertEqual(sorted(slot for slot, _ in out["slots"]),
                         sorted(["company.summary", "company.model", "company.valuation",
                                 "company.returns", "company.scenarios"]))
        self.assertTrue(all(kind == "function" for _, kind in out["slots"]))


if __name__ == "__main__":
    unittest.main()
