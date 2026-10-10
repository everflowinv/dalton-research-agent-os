"""Batch w: the publication pipeline's two live blockers of 2026-10-07/08.

1. 235 localization-verifier calls were refused before any route decision --
   the verifier chain held only the drafting model's family -- and the refusal
   was recorded nowhere but as a bare ``MODEL_ROUTE_REJECTED`` that no policy
   change could ever recover.
2. 158 localizations on 10-07 (and 34 blocked UI-text batches) failed the
   numeric check on a faithful unit conversion: 104863000 shown as
   1.04863 亿美元.

The numeric cases below are the live texts, shortened only where the rest of
the sentence carries no number.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.research_localization import (
    ResearchLocalizationError, _number_differences, validate_localized_text)


def diff(source: str, target: str) -> tuple[list[str], list[str]]:
    return _number_differences(("界面文字", source), ("界面文字", target))


UI_SOURCE = ("EPAM Systems, Inc.在10-Q（accession 0001352010-26-000029）中列报的营业收入"
             "（分部：United Kingdom），期间 2025-01-01..2025-03-31，为 USD {value}。"
             "该数字直接取自申报的 XBRL 行 us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
             "（United Kingdom，第 321 行），未经任何换算。")
UI_TARGET = ("EPAM Systems, Inc.在10-Q（accession 0001352010-26-000029）中列报的营业收入"
             "（分部：United Kingdom），期间 2025-01-01 至 2025-03-31，为 {shown}。"
             "该数字直接取自申报的 XBRL 行 us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
             "（United Kingdom，第 321 行），未经任何换算。")


class UnitConversionTests(unittest.TestCase):
    def assertPasses(self, source, target):
        self.assertEqual(diff(source, target), ([], []))

    def assertFails(self, source, target):
        missing, added = diff(source, target)
        self.assertTrue(missing or added, (missing, added))

    def test_live_yi_conversions_with_the_unit_stated_pass(self):
        source = UI_SOURCE.format(value="104863000")
        for shown in ("1.04863 亿美元", "1.05 亿美元", "1.0486亿美元", "10486.3万美元",
                      "10486.3 万美元", "104,863,000 美元"):
            with self.subTest(shown=shown):
                self.assertPasses(source, UI_TARGET.format(shown=shown))

    def test_machine_precision_value_restated_in_yi(self):
        self.assertPasses("DXC 当期为 USD 2707409432.42。", "DXC 当期为 27.07 亿美元。")
        self.assertPasses("DXC 当期为 USD 2707409432.42。", "DXC 当期为 27.074 亿美元。")

    def test_a_changed_number_is_still_refused(self):
        source = UI_SOURCE.format(value="104863000")
        for shown in ("1.06 亿美元", "1.04 亿美元", "10.49 亿美元", "1 亿美元", "1.1 亿美元"):
            with self.subTest(shown=shown):
                self.assertFails(source, UI_TARGET.format(shown=shown))

    def test_the_unit_must_be_stated(self):
        source = UI_SOURCE.format(value="104863000")
        self.assertFails(source, UI_TARGET.format(shown="1.04863 美元"))
        self.assertFails(source, UI_TARGET.format(shown="1.04863"))
        self.assertFails("净利润为 USD 2707409432.42。", "净利润为 27.07。")

    def test_live_wrong_magnitudes_stay_refused(self):
        # 10-07 drafts that really changed the value: 31.12 亿 written 3.112 亿,
        # 700100 万 written 7001 万, 403432.1 万 written 403433 万.
        self.assertFails(UI_SOURCE.format(value="3112000000"),
                         UI_TARGET.format(shown="3.112亿美元"))
        self.assertFails(UI_SOURCE.format(value="7001000000"),
                         UI_TARGET.format(shown="7001万美元"))
        self.assertFails(UI_SOURCE.format(value="4034321000"),
                         UI_TARGET.format(shown="403433 万美元"))
        self.assertFails(UI_SOURCE.format(value="11452914000"),
                         UI_TARGET.format(shown="11.45亿美元"))

    def test_restated_display_beside_the_kept_value(self):
        source = UI_SOURCE.format(value="65543000")
        self.assertPasses(source, UI_TARGET.format(shown="65543000美元（约合6554.3万美元）"))
        self.assertFails(source, UI_TARGET.format(shown="65543000美元（约合6554.9万美元）"))

    def test_scaled_number_the_source_happens_to_contain_elsewhere(self):
        source = ("IBM在10-Q中列报的资本开支，期间 2023-01-01..2023-03-31，为 USD 300000000。")
        self.assertPasses(source, "IBM在10-Q中列报的资本开支，期间 2023-01-01 至 2023-03-31，为 3 亿美元。")

    def test_table_that_declares_its_unit_once(self):
        source = "ACN revenue 16405819000 17689545000 CTSH revenue 5044000000"
        self.assertPasses(source, "营业收入（单位：亿美元） ACN 164.06 176.90 CTSH 50.44")
        self.assertPasses(source, "营业收入（亿美元） ACN 164.06 176.90 CTSH 50.44")
        self.assertFails(source, "营业收入 ACN 164.06 176.90 CTSH 50.44")
        self.assertFails(source, "营业收入（单位：亿美元） ACN 164.06 176.91 CTSH 50.44")

    def test_source_with_its_own_scale_word(self):
        source = "we invested $1.5 billion across 23 strategic acquisitions."
        self.assertPasses(source, "公司投入15亿美元完成23项战略收购。")
        self.assertFails(source, "公司投入16亿美元完成23项战略收购。")

    def test_generated_restatement_may_be_said_once(self):
        source = ("we invested $1.5 billion across 23 strategic acquisitions for fiscal 2025 "
                  "was USD 1.5 billion (currency), as published by the company in this document.")
        self.assertPasses(source, "公司在本文档中披露，2025财年投入15亿美元完成23项战略收购。")
        percent = ("Adjusted Diluted EPS up $0.53 or 11.2% from 2024 for year ended "
                   "December 31, 2025 was 11.2 (percent), as published by the company.")
        self.assertPasses(percent, "截至2025年12月31日止年度，调整后摊薄每股收益较2024年增加"
                                   "0.53美元，增幅为11.2%。")
        # Two different facts that share a value are not a restatement.
        self.assertFails("cost 100 and capex 100 in the quarter.", "成本为100。")

    def test_percent_written_in_words(self):
        self.assertPasses("corresponding margin for fiscal 2026 was 7.7 (percent), as published.",
                          "公司公布的 2026 财年对应利润率为 7.7%。")
        self.assertFails("corresponding margin for fiscal 2026 was 7.7 (percent), as published.",
                         "公司公布的 2026 财年对应利润率为 7.9%。")

    def test_accession_number_and_xbrl_row_are_locators(self):
        source = UI_SOURCE.format(value="104863000")
        dropped = ("EPAM Systems, Inc. 在 10-Q 中列报英国分部营业收入，期间 2025-01-01 至 "
                   "2025-03-31，为 1.04863 亿美元。该数字取自 XBRL 行 "
                   "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax。")
        self.assertPasses(source, dropped)
        changed_row = UI_TARGET.format(shown="1.04863 亿美元").replace("第 321 行", "第 322 行")
        self.assertFails(source, changed_row)
        dropped_period = ("EPAM Systems, Inc. 在 10-Q 中列报英国分部营业收入为 1.04863 亿美元。")
        self.assertFails(source, dropped_period)

    def test_mid_year_is_not_a_negative_year(self):
        self.assertPasses("Cognizant reported remaining performance obligations as of mid-2026.",
                          "Cognizant报告了2026年中的剩余履约义务。")

    def test_validate_localized_text_uses_the_rule(self):
        product = {"sections": [{"title": "界面文字", "body": UI_SOURCE.format(value="104863000"),
                                 "gaps": []}]}
        good = {"sections": [{"index": 0, "title": "界面文字",
                              "body": UI_TARGET.format(shown="1.05 亿美元"), "gaps": []}]}
        self.assertEqual(len(validate_localized_text(product, good)), 1)
        bad = {"sections": [{"index": 0, "title": "界面文字",
                             "body": UI_TARGET.format(shown="1.04863"), "gaps": []}]}
        with self.assertRaisesRegex(ResearchLocalizationError, "financial number tokens"):
            validate_localized_text(product, bad)


class WorkerRetryClassTests(unittest.TestCase):
    def test_classes(self):
        from dalton_core.research_publication_worker import (
            TRANSIENT_RETRY_CLASSES, automatic_retry_due, retry_class, retry_round)

        def state(error, status="pending"):
            return {"status": status, "product_hash": "h", "identity": {"kind": "k"},
                    "result": {"receipt": {"failures": [{"error": error}]}}}

        numeric = state('localized section changed financial number tokens: {"added_tokens": '
                        '["1.04863"], "missing_tokens": ["104863000"], "section_index": 0}')
        self.assertEqual(retry_class(numeric["result"]), "numeric_tokens_changed")
        self.assertEqual(retry_round("numeric_tokens_changed"), "2026-10-08w")
        self.assertEqual(automatic_retry_due(numeric, [], now="2026-10-08T02:00:00+00:00"),
                         "numeric_tokens_changed")
        once = [{"trigger": "automatic", "product_hash": "h", "auto_round": "2026-10-08w",
                 "created_at": "2026-10-08T02:00:00+00:00"}]
        self.assertIsNone(automatic_retry_due(numeric, once, now="2026-10-09T02:00:00+00:00"))

        refused = state("独立核验找不到可用模型：……（verifier_not_independent）")
        self.assertEqual(retry_class(refused["result"]), "route_refused")
        self.assertIn("route_refused", TRANSIENT_RETRY_CLASSES)
        legacy = state("the model call did not succeed (MODEL_ROUTE_REJECTED)")
        self.assertEqual(retry_class(legacy["result"]), "route_refused")
        today = [{"trigger": "automatic", "product_hash": "h", "auto_round": "x",
                  "created_at": "2026-10-08T01:00:00+00:00"}]
        self.assertIsNone(automatic_retry_due(refused, today, now="2026-10-08T05:00:00+00:00"))
        self.assertEqual(automatic_retry_due(refused, today, now="2026-10-09T05:00:00+00:00"),
                         "route_refused")

        exhausted = state("check-only fresh draft failed too (check-only draft failed "
                          "validation: localized section changed financial number tokens: "
                          "{})", status="exhausted")
        self.assertEqual(retry_class(exhausted["result"]), "check_only_numeric")
        self.assertEqual(automatic_retry_due(exhausted, [], now="2026-10-08T02:00:00+00:00"),
                         "check_only_numeric")

    def test_ui_text_numeric_block_gets_one_retry_per_its_round(self):
        from dalton_core import ui_text_discovery as ui

        result = {"receipt": {"failures": [
            {"error": "localized section changed financial number tokens: {}"}]}}
        self.assertEqual(ui.auto_retry_class(result), "numeric_tokens_changed")
        self.assertEqual(ui._auto_retry_mark("numeric_tokens_changed"),
                         "[auto-retry 2026-10-08w]")
        self.assertEqual(ui._auto_retry_mark("database_locked"),
                         f"[auto-retry {ui.AUTO_RETRY_ROUND}]")


class NeedsHumanCardTests(unittest.TestCase):
    def test_one_card_for_every_product_on_a_refused_verifier(self):
        from dalton_core.needs_human import KINDS, URGENCY, publication_route_blocks

        self.assertIn("publication_route_blocked", KINDS)
        self.assertIn("publication_route_blocked", URGENCY)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(publication_route_blocks(state), [])
            products = state / "research-publication-work" / "products"
            products.mkdir(parents=True)
            reason = ("独立核验找不到可用模型：环节 research_localization_verifier 的模型链"
                      "（gemini-3-8-flash、gemini-3-1-pro-preview）与产出这份内容的模型同属 "
                      "google-gemini-3 家族（verifier_not_independent）")
            for number in range(3):
                (products / f"{number}.json").write_text(json.dumps({
                    "status": "pending", "result": {
                        "failed_at": f"2026-10-08T0{number}:00:00+00:00",
                        "receipt": {"failures": [{"error": reason}]}}}))
            (products / "other.json").write_text(json.dumps({
                "status": "pending", "result": {"reason": "database is locked"}}))
            (products / "done.json").write_text(json.dumps({
                "status": "completed", "result": {"reason": reason}}))
            items = publication_route_blocks(state)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["kind"], "publication_route_blocked")
        self.assertEqual(item["detail"]["products"], 3)
        self.assertEqual(item["at"], "2026-10-08T00:00:00+00:00")
        self.assertIn("3 份", item["title"])
        self.assertIn("gemini-3-8-flash", item["why_blocked"])
        self.assertIn("整理研究内容的中文初稿", item["action"])


class ChainRefusalTests(unittest.TestCase):
    """The refusal itself, through the real cockpit, router and scheduler."""

    def setUp(self):
        from tests import test_cockpit_model_fallback as fixture

        self.fixture = fixture
        fixture.CockpitChainTests.setUp(self)

    _model = None  # bound in each test from the fixture

    def _cockpit(self, slots=None):
        return self.fixture.CockpitChainTests._model(
            self, self.fixture.ChainAdapter({}), policy_version_ref=self.verifier_policy,
            slots=slots if slots is not None else self.verifier_slots)

    def _families(self):
        from dalton_core.model_fallback_chain import profile_families, tier_chain
        from dalton_core.model_router import ModelRouter

        with ModelRouter(self.router_db, read_only=True) as router:
            held = profile_families(router)
        return {f"route:{index}": held[profile]["family"]
                for index, profile in enumerate(tier_chain("verifier"))}

    def test_refusal_is_recorded_named_in_chinese_and_recovered_on_a_new_scope(self):
        from dalton_core.cockpit_model import CockpitModelRouteUnavailable
        from dalton_core.scheduler import Scheduler

        families = self._families()
        args = dict(purpose="p14m_route_verify", request_id="same-family", prompt="verify",
                    mission=self.mission, producer_route_decision_refs=sorted(families))
        with patch("dalton_core.model_fallback_chain.served_family",
                   side_effect=lambda router, ref: families[ref]):
            with self.assertRaises(CockpitModelRouteUnavailable) as first:
                self._cockpit().call(**args)
            self.assertIn("verifier_not_independent", str(first.exception))
            self.assertIn("独立核验找不到可用模型", str(first.exception))
            with self.assertRaises(CockpitModelRouteUnavailable) as replay:
                self._cockpit().call(**args)
            self.assertIn("MODEL_ROUTE_REJECTED", str(replay.exception))
            self.assertIn("独立核验找不到可用模型", str(replay.exception))
            with Scheduler(self.root / "scheduler.sqlite") as scheduler:
                rows = scheduler.connection.execute(
                    "SELECT result_envelope_json FROM scheduler_result_envelopes").fetchall()
            self.assertEqual(len(rows), 1)
            envelope = json.loads(rows[0][0])
            refusal = envelope["metadata"]["chain_refusal"]
            self.assertEqual(refusal["reason"], "verifier_not_independent")
            self.assertEqual(refusal["purpose"], "p14m_route_verify")
            self.assertEqual(sorted(refusal["producer_families"]), sorted(set(families.values())))
            self.assertTrue(refusal["policy_hash"])
            # A changed credential scope (as a changed policy would) earns
            # exactly one successor request; asking again under it replays.
            changed = [*self.verifier_slots, "credential-slot:openclaw:new"]
            for _ in range(2):
                with self.assertRaises(CockpitModelRouteUnavailable):
                    self._cockpit(slots=changed).call(**args)
            with Scheduler(self.root / "scheduler.sqlite") as scheduler:
                count = scheduler.connection.execute(
                    "SELECT COUNT(DISTINCT work_order_id) FROM scheduler_result_envelopes"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_legacy_refusal_without_a_record_recovers_once_the_policy_is_newer(self):
        from dalton_core.model_route_recovery import route_recovery_request

        config = self._cockpit().config
        legacy = {"terminal_state": "failed", "created_at": "2026-09-01T00:00:00+00:00",
                  "result_envelope": {"error": {"code": "MODEL_ROUTE_REJECTED"},
                                      "metadata": {"control_plane_failure": True,
                                                   "route_decision_ref": None,
                                                   "chain_failures": []}}}
        successor = route_recovery_request(legacy, work_order_ref="work:x",
                                           request_id="zh-verify-1", config=config)
        self.assertTrue(successor.startswith("zh-verify-1:route-admission:"))
        self.assertIsNone(route_recovery_request(legacy, work_order_ref="work:x",
                                                 request_id=successor, config=config))
        newer = {**legacy, "created_at": "2099-01-01T00:00:00+00:00"}
        self.assertIsNone(route_recovery_request(newer, work_order_ref="work:x",
                                                 request_id="zh-verify-1", config=config))
        tried = {**legacy, "result_envelope": {**legacy["result_envelope"], "metadata": {
            **legacy["result_envelope"]["metadata"],
            "chain_failures": [{"profile_id": "p", "code": "X", "message": "m"}]}}}
        self.assertIsNone(route_recovery_request(tried, work_order_ref="work:x",
                                                 request_id="zh-verify-1", config=config))

    def test_preflight_stops_before_the_paid_stages(self):
        from dalton_core.research_output_preparation import (
            VerifierNotIndependent, verifier_preflight)
        from dalton_core.model_fallback_chain import profile_families, tier_chain
        from dalton_core.model_router import ModelRouter

        config = {"model_router_db": str(self.router_db),
                  "routing_policy_ref": self.verifier_policy,
                  "credential_slot_refs": self.verifier_slots}
        with ModelRouter(self.router_db, read_only=True) as router:
            held = profile_families(router)
        every = {held[profile]["family"] for profile in tier_chain("verifier")}

        def families(chosen):
            return patch("dalton_core.model_fallback_chain.served_family",
                         side_effect=lambda router, ref: chosen)

        with patch("dalton_core.model_fallback_chain.served_family",
                   return_value="family:nobody-else"):
            self.assertIsNone(verifier_preflight(config, config, draft_route_ref="route:x",
                                                 check_only=True))
        self.assertIsNone(verifier_preflight(config, config, draft_route_ref=None,
                                             check_only=True))
        # Every capable verifier link shares the producer's family: refused,
        # in the cockpit's own words, before checker or revision is bought.
        capable = {held[profile]["family"] for profile in tier_chain("verifier")
                   if "provider-controlled-verify" in (held[profile].get("capabilities") or ())}
        # The fixture, like legacy, has one family with provider-controlled-verify.
        self.assertEqual(capable, {"google-gemini-3"})
        self.assertGreater(len(every), 1)
        with families("google-gemini-3"):
            with self.assertRaisesRegex(VerifierNotIndependent, "verifier_not_independent"):
                verifier_preflight(config, config, draft_route_ref="route:x",
                                   check_only=True)
        with families("anthropic-claude-5"):
            self.assertIsNone(verifier_preflight(config, config, draft_route_ref="route:x",
                                                 check_only=True))
        # Unreadable configuration: the call itself decides, as before.
        self.assertIsNone(verifier_preflight({}, {}, draft_route_ref="route:x",
                                             check_only=False))


class SelectScriptTests(unittest.TestCase):
    def test_dry_run_and_apply_use_the_writer_door_only(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "select_model_chain.py"
        spec = importlib.util.spec_from_file_location("select_model_chain", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Env:
            def __init__(self, ident, running):
                self.environment_id = ident; self.name = ident
                self.state_dir = Path("/nowhere") / ident
                self.router_db = Path("/nowhere") / ident / "model-router.sqlite"
                self.is_running = running

        environments = [Env("legacy", True), Env("ws", False), Env("ws2", True)]
        rows = [{"environment": "legacy", "status": "to_publish"},
                {"environment": "ws", "status": "to_publish"},
                {"environment": "ws2", "status": "selected"}]
        calls = []
        out = module.apply(rows, environments, purpose="research_localization",
                           chain=["profile:a"], actor_ref="human:owner",
                           writer_call=lambda env, **kw: calls.append((env.environment_id, kw))
                           or {"status": "published"})
        self.assertEqual([row["status"] for row in out],
                         ["published", "writer_not_running", "selected"])
        self.assertEqual(calls, [("legacy", {"actor_ref": "human:owner", "params": {
            "mode": "explicit", "chain": ["profile:a"], "purpose": "research_localization"}})])
        planned = module.plan([Env("gone", False)], purpose="research_localization",
                              chain=["profile:a"])
        self.assertEqual(planned[0]["status"], "no_router")


if __name__ == "__main__":
    unittest.main()
