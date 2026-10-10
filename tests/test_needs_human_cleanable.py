"""2026-10-09: every 待我处理 card can be finished from the cockpit.

Owner, 2026-10-08: 「看下几个环境下"待我处理"的项目，我根本不知道该怎么处理，
没办法消除掉这个通知」.  The fixtures below are shaped like the live cards of
that day:

1. legacy ``publication_route_blocked`` -- eight products, seven of them
   versions since replaced, the eighth (IBM's initial screen) parked on a
   joined ``numeric_tokens_changed+route_refused`` class, and every retry
   judging the draft Gemini wrote before the owner moved the drafting chain;
   plus five UI-text batches blocked after six identical replays;
2. legacy ``controlled_recovery`` (EPAM) -- a rebound Work whose route was
   *rejected*, raising "failed route authority drifted" on every pass;
3. ws-7d ``controlled_recovery`` (MSFT) -- a corrected budget sized from
   glm's count, served by opus, which counted 1.8x more; two admissions of
   one question differing by a 的;
4. legacy ``earnings_release_unread`` (ACN) -- the press release typed
   ``EX-99`` under a non-standard file name;
5. informational cards with nothing to press -- acknowledgement.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

FIXTURES = Path(__file__).parent / "fixtures" / "sec-earnings-release"
NOW = datetime(2026, 10, 9, 3, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. publication: the route-refused card, parked products, UI batches
# ---------------------------------------------------------------------------


REFUSAL = ("独立核验找不到可用模型：环节 research_localization_verifier 的模型链（gemini-3-8-flash、"
           "gemini-3-1-pro-preview）与产出这份内容的模型同属 google-gemini-3 家族，按规则不能自己核验"
           "自己。需要在「研究目标与计划」页「模型与预算」改模型选择。（verifier_not_independent）"
           "（预检拦下：语言检查和修订没有花钱）")


def _identity(kind, subject, version):
    return {"kind": kind, "subject_ref": subject, "version_ref": version}


class PublicationRouteCardTests(unittest.TestCase):
    def setUp(self):
        from dalton_core.research_publication_worker import _hash

        self._hash = _hash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.work = self.state / "research-publication-work"
        self.products = self.work / "products"
        self.products.mkdir(parents=True)

    def _state(self, identity, product_hash, *, status="pending", failures=(REFUSAL,),
               failed_at="2026-10-08T02:50:00+00:00"):
        from dalton_core.research_publication_worker import SCHEMA_VERSION

        path = self.products / f"{self._hash(identity)}.json"
        body = {
            "schema_version": SCHEMA_VERSION,
            "status": status, "identity": identity, "product_hash": product_hash,
            "result": {"failed_at": failed_at, "status": status,
                       "receipt": {"failures": [{"error": e, "product": 0, "section_start": 0}
                                                for e in failures]}}}
        path.write_text(json.dumps({**body, "content_hash": self._hash(body)}))
        return path

    def test_only_current_products_count_toward_the_card(self):
        from dalton_core.needs_human import publication_route_blocks

        current = _identity("initial_screen", "company:sec-cik:0000051143",
                            "mission-deliverable-version:bce92d06")
        self._state(current, "a" * 64)
        for number in range(7):  # replaced event judgements / reflections
            self._state(_identity("surface_event_judgement", "company:sec-cik:0001352010",
                                  f"event-judgement:{number}"), "b" * 64)
        # No checkpoint: every state counts, as before.
        self.assertEqual(publication_route_blocks(self.state)[0]["detail"]["products"], 8)
        (self.work / "worker-last-run.json").write_text(json.dumps({"products": [
            {"identity": current, "product_hash": "a" * 64, "status": "pending", "parked": True}]}))
        items = publication_route_blocks(self.state)
        self.assertEqual(items[0]["detail"]["products"], 1)
        self.assertIn("1 份", items[0]["title"])
        # A newer source hash of the same identity is not the parked one.
        (self.work / "worker-last-run.json").write_text(json.dumps({"products": [
            {"identity": current, "product_hash": "c" * 64, "status": "pending"}]}))
        self.assertEqual(publication_route_blocks(self.state), [])

    def test_once_the_chain_is_fixed_the_card_says_nothing_is_needed(self):
        from dalton_core.needs_human import publication_route_blocks

        self._state(_identity("initial_screen", "company:sec-cik:0000051143", "v1"), "a" * 64)
        with mock.patch("dalton_core.needs_human.drafting_chain_now_independent",
                        return_value=True):
            item = publication_route_blocks(self.state)[0]
        self.assertFalse(item["actionable"])
        self.assertIn("模型选择已改好", item["title"])
        self.assertIn("不需要再改", item["action"])
        self.assertTrue(item["detail"]["chain_now_independent"])

    def test_a_ui_batch_retry_names_a_real_batch(self):
        from dalton_core.writer_server import ValidationError, WriterServer

        fake = mock.Mock(state_dir=self.state)
        for params in ({"batch_ref": "ui-text-batch:xyz", "reason": "x"},
                       {"batch_ref": "ui-text-batch:" + "4" * 64, "reason": " "},
                       {"batch_ref": "ui-text-batch:" + "4" * 64, "reason": "x",
                        "identity_hash": "a" * 64}):
            with self.subTest(params=params), self.assertRaises(ValidationError):
                WriterServer._retry_ui_text_language_review(fake, params, "human:owner")

    def test_a_joined_route_class_is_retried_once_a_day(self):
        from dalton_core.research_publication_worker import (
            automatic_retry_due, awaiting_owner, retry_class)

        prior = {"status": "pending", "product_hash": "a" * 64, "result": {"receipt": {"failures": [
            {"error": REFUSAL},
            {"error": 'localized section changed financial number tokens: {"missing_tokens": ["57.73%"]}'}]}}}
        self.assertEqual(retry_class(prior["result"]), "numeric_tokens_changed+route_refused")
        # Live: the numeric round's retry was already spent on 10-08.
        chain = [{"trigger": "automatic", "product_hash": "a" * 64, "auto_round": "2026-10-08w",
                  "created_at": "2026-10-08T02:47:09+00:00", "sequence": 2}]
        self.assertEqual(automatic_retry_due(prior, chain, now="2026-10-09T00:10:00+00:00"),
                         "numeric_tokens_changed+route_refused")
        self.assertIsNone(automatic_retry_due(prior, chain, now="2026-10-08T23:00:00+00:00"))
        self.assertFalse(awaiting_owner(prior, chain))

    def test_transport_failures_are_transient_and_unknown_ones_await_the_owner(self):
        from dalton_core.research_publication_worker import awaiting_owner, retry_class

        transport = {"status": "pending", "product_hash": "a" * 64, "result": {"receipt": {"failures": [
            {"error": "every model in the cheap chain failed: profile:zai-glm-5-3-flash "
                      "(transport_failure), profile:deepseek-v4-flash (transport_failure)"}]}}}
        self.assertEqual(retry_class(transport["result"]), "provider_unavailable")
        self.assertFalse(awaiting_owner(transport, []))
        unknown = {"status": "pending", "product_hash": "a" * 64,
                   "result": {"reason": "something nobody classified"}}
        self.assertTrue(awaiting_owner(unknown, []))
        self.assertFalse(awaiting_owner({**unknown, "status": "completed"}, []))

    def test_parked_products_and_blocked_ui_batches_are_cards_with_a_retry_button(self):
        from dalton_core.needs_human import collect, parked_publications

        parked = _identity("debate_map", "company:sec-cik:0001467373", "debate-map-version:f1")
        self._state(parked, "d" * 64, failures=("verifier said no",))
        route_only = _identity("dossier", "company:sec-cik:001688568", "dossier:21")
        self._state(route_only, "e" * 64)
        batch = "ui-text-batch:" + "4" * 64
        (self.work / "worker-last-run.json").write_text(json.dumps({
            "checked_at": "2026-10-09T03:05:43+00:00",
            "products": [
                {"identity": parked, "product_hash": "d" * 64, "status": "pending", "parked": True},
                {"identity": route_only, "product_hash": "e" * 64, "status": "pending", "parked": True}],
            "ui_texts": {"batches": [{"batch_ref": batch, "status": "blocked", "attempts": 6,
                                      "reason_class": "verifier_rejected",
                                      "reason": "已连续失败 6 次（上限 6），停止重试以免继续消耗预算；"
                                                "最后一次失败原因：independent localization verifier "
                                                "did not pass cleanly"}]}}))
        cards = parked_publications(self.state, {"company:sec-cik:0001467373": "ACN · Accenture"})
        self.assertEqual([card["kind"] for card in cards], ["publication_parked"] * 2)
        product, ui = cards
        self.assertIn("ACN · Accenture 的多空核心分歧议题", product["title"])
        self.assertEqual(product["detail"]["retry"], {
            "action": "language_retry",
            "payload": {"identity_hash": self._hash(parked), "product_hash": "d" * 64}})
        self.assertEqual(ui["detail"]["retry"]["payload"], {"batch_ref": batch})
        self.assertFalse(ui['actionable'])
        self.assertEqual(ui['responsibility'],'administrator')
        self.assertFalse(product['actionable'])
        self.assertEqual(product['responsibility'],'administrator')

        # 2026-10-09 (G10, financial 痛点5): the English failure stays in the
        # detail for the administrator, never in the analyst's sentence.
        self.assertIn("independent localization verifier", ui["detail"]["reason"])
        self.assertIn("independent localization verifier", ui["detail"]["last_failure"])
        self.assertNotIn("independent localization verifier", ui["why_blocked"])
        self.assertNotIn("independent localization verifier", ui["title"])
        listed = collect(state_dir=self.state, clock=lambda: NOW)
        entries = {item["ref"]: item["entry"] for item in listed["items"]}
        entry = entries[f"publication-parked:{batch}"]
        self.assertEqual(entry["mode"], "action")
        self.assertEqual(entry["action"], "language_retry")
        self.assertEqual(entry["label"], "重新生成中文定稿")
        self.assertEqual([c["choice"] for c in entry["acknowledge"]["choices"]], ["noted", "snooze"])

    def test_an_owner_retry_prepares_from_a_fresh_draft(self):
        from dalton_core import research_publication_worker as worker

        identity = _identity("dossier", "company:x", "dossier:1")
        product = {**identity, "status": "available", "sections": [{"title": "判断", "body": "本季收入增长来自已交付项目，后续增长仍需新增订单支持。", "citations": []}]}
        product_hash = worker._hash(product)
        self._state(identity, product_hash, failures=("verifier said no",))
        seen = []

        def prepare(item, redraft_generation=0):
            seen.append(redraft_generation)
            return {"status": "pending", "receipt": {"failures": [{"error": "verifier said no"}]}}

        mission = {"universe": [{"company_ref": "company:x"}]}
        reader = lambda _c, _m, _company: {"products": [product]}  # noqa: E731
        worker.request_retry(self.products, worker._hash(identity), actor_ref="human:owner",
                             reason="再试一次", apply=True)
        worker.poll_once(None, mission, state_dir=self.products, prepare=prepare,
                         library_reader=reader)
        worker.request_retry(self.products, worker._hash(identity), actor_ref="human:owner",
                             reason="再试一次", apply=True)
        worker.poll_once(None, mission, state_dir=self.products, prepare=prepare,
                         library_reader=reader)
        self.assertEqual(seen, [1, 2])


class LocalizationFixTests(unittest.TestCase):
    def test_a_hyphen_is_not_a_minus_sign(self):
        from dalton_core.research_localization import _number_differences

        source = ("Guggenheim's implied expectation for CTSH shifted materially downward through "
                  "mid-2026, with the July 2026 target the lowest.")
        target = "Guggenheim 对 CTSH 的隐含预期在 2026 年中前明显下移，其中 2026 年七月的目标价最低。"
        self.assertEqual(_number_differences(("t", source), ("t", target)), ([], []))
        # A real negative number still has to survive.
        self.assertNotEqual(_number_differences(("t", "result was -12 USD"), ("t", "结果为12美元")),
                            ([], []))

    def test_a_minority_of_malformed_suggestions_is_set_aside(self):
        from dalton_core.research_language_review import (
            ResearchLanguageReviewError, validate_checker_output)

        sections = [{"title": "界面文字", "body": f"第 {i} 句：原文内容在这里。", "gaps": []}
                    for i in range(10)]
        good = [{"section_index": i, "quote": f"第 {i} 句", "assessment": "生硬",
                 "suggestion": f"第{i}句"} for i in range(9)]
        bad = {"section_index": 9, "quote": "第 9 句", "body": "x", "assessment": "生硬",
               "suggestion": "y"}  # live: both ``body`` and ``assessment``
        result = validate_checker_output({"overall": "ok", "suggestions": [*good, bad]},
                                         sections=sections)
        self.assertEqual(len(result["suggestions"]), 9)
        self.assertEqual(result["malformed_suggestions"], [bad])
        with self.assertRaisesRegex(ResearchLanguageReviewError, "invalid shape"):
            validate_checker_output({"overall": "ok", "suggestions": [good[0], bad, bad]},
                                    sections=sections)

    def test_malformed_suggestions_get_one_automatic_retry_round(self):
        from dalton_core import ui_text_discovery as ui

        result = {"receipt": {"failures": [
            {"error": "language checker suggestion has an invalid shape"}]}}
        self.assertEqual(ui.auto_retry_class(result), "checker_suggestion_malformed")
        self.assertEqual(ui._auto_retry_round("checker_suggestion_malformed"), "2026-10-09ag")


class RouteRedraftTests(unittest.TestCase):
    """The cached draft Gemini wrote before the owner moved the drafting chain."""

    PRODUCT = {"kind": "surface_event_judgement", "subject_ref": "company:sec-cik:0001352010",
               "version_ref": "event-judgement:1", "status": "available",
               "sections": [{"title": "Judgement", "body": "Revenue grew 5% in the quarter.",
                             "gaps": []}]}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.configs = {name: {"model_router_db": "/nowhere", "routing_policy_ref": f"p:{name}"}
                        for name in ("draft", "checker", "brain", "verifier")}

    def _run(self, *, current_family):
        from dalton_core import research_output_preparation as prep

        identity = prep.style_stage_identity(
            self.PRODUCT, draft_config=self.configs["draft"],
            checker_config=self.configs["checker"], brain_config=self.configs["brain"])
        (self.work / "stages").mkdir(exist_ok=True)
        (self.work / "stages" / f"{identity}.json").write_text(json.dumps({
            "draft": {"route_decision_ref": "route:gemini", "text": "{}"},
            "draft_localized": {"sections": [{"index": 0, "title": "判断", "body": "收入增长 5%。",
                                              "gaps": []}]}}))
        requests = []

        class Stop(Exception):
            pass

        class Model:
            def __init__(self, *args, **kwargs):
                pass

            def call(self, **kwargs):
                requests.append(kwargs["request_id"])
                raise Stop()

        def preflight(_v, _b, *, draft_route_ref, check_only, producer_family=None):
            family = producer_family or "google-gemini-3"
            if family == "google-gemini-3":
                raise prep.VerifierNotIndependent("（verifier_not_independent）")
            return None

        with mock.patch.object(prep, "verifier_preflight", side_effect=preflight), \
                mock.patch.object(prep, "drafting_family", return_value=current_family), \
                mock.patch.object(prep, "CockpitModel", Model):
            try:
                prep.run_chunk((0, 0, self.PRODUCT), mission={}, draft_config=self.configs["draft"],
                               verifier_config=self.configs["verifier"],
                               checker_config=self.configs["checker"],
                               brain_config=self.configs["brain"], scheduler_db=None,
                               work_dir=self.work, max_cost=1, attempts=1)
            except Stop:
                return "redrafted", identity, requests
            except prep.VerifierNotIndependent:
                return "refused", identity, requests
        return "passed", identity, requests

    def test_a_changed_drafting_chain_redrafts_instead_of_refusing_forever(self):
        from dalton_core.research_output_preparation import route_redraft_identity

        outcome, identity, requests = self._run(current_family="zhipu-glm-5")
        self.assertEqual(outcome, "redrafted")
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].startswith("zh-draft-"))
        # A new stage identity, so a new request id the scheduler cannot replay.
        self.assertNotEqual(route_redraft_identity(identity, "zhipu-glm-5"), identity)

    def test_an_unchanged_chain_still_refuses_with_the_owners_words(self):
        outcome, _identity, requests = self._run(current_family="google-gemini-3")
        self.assertEqual(outcome, "refused")
        self.assertEqual(requests, [])


# ---------------------------------------------------------------------------
# 2 / 3. held research tasks: route rejection, budget correction, dedupe
# ---------------------------------------------------------------------------


def _executor_helpers():
    from tests import test_mission_document_research as base

    return type("_Helpers", (unittest.TestCase,), {
        key: value for key, value in vars(base.MissionDocumentResearchTests).items()
        if not key.startswith("test") and key not in ("__dict__", "__weakref__")})


_Helpers = _executor_helpers()


class HeldResearchTaskTests(_Helpers):
    def _reject_routes(self, executor):
        router = executor.draft_worker.router
        original = router.route
        router.route = lambda work, **kw: original(
            work, **{**kw, "credential_slot_refs": ("credential-slot:nobody",)})
        return lambda: router.__dict__.pop("route", None)

    def test_a_rejected_route_is_recovered_under_current_authority_even_days_later(self):
        """Legacy EPAM 10-07: the rebound Work's route was rejected; nothing was sent."""

        from dalton_core.mission_document_research_executor import (
            ROUTE_REJECTED_NO_SEND_CLASSIFICATION)

        fixture, authority, args, _r, _l = self._fixture()
        self._enable_recovery(fixture, maximum=1, elapsed=7200)
        admission = authority.admit_from_plan(**args)
        executor, draft, _verifier = self._executor(fixture, authority)
        restore = self._reject_routes(executor)
        failed = self._run_until(executor, admission, lambda item: item.get("status") == "failed")
        restore()
        self.assertEqual(draft.calls, 0)
        # The owner re-enters it two days later -- far past the 2 h window.
        fixture.harness.clock.value += timedelta(days=2)
        final = self._run_until(
            executor, admission, lambda item: item.get("research_status") == "candidate_staged")
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(draft.calls, 1)
        proofs = [json.loads(row[0])["failure_proof"]["classification"]
                  for row in fixture.store.connection.execute(
                      "SELECT record_json FROM mission_document_research_recovery_links")]
        self.assertEqual(proofs, [ROUTE_REJECTED_NO_SEND_CLASSIFICATION])
        self.assertIsNotNone(failed)

    def test_a_rejected_route_of_another_policy_still_drifts(self):
        from dalton_core.mission_document_research_executor import (
            MissionDocumentResearchExecutorError)

        fixture, authority, args, _r, _l = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        router = executor.draft_worker.router
        original = router.route
        router.route = lambda work, **kw: original(
            work, **{**kw, "credential_slot_refs": ("credential-slot:nobody",),
                     "policy_version_ref": kw.get("policy_version_ref")})
        self._run_until(executor, admission, lambda item: item.get("status") == "failed")
        del router.route
        # Forge the decision's policy: the binding must still be exact.
        row = router.connection.execute(
            "SELECT decision_id, decision_json FROM model_route_decisions "
            "ORDER BY rowid DESC LIMIT 1").fetchone()
        forged = json.loads(row[1])
        self.assertEqual(forged["outcome"], "rejected")
        with mock.patch("dalton_core.mission_document_research_executor.json.loads",
                        side_effect=lambda text, *a, **k: (
                            {**json.JSONDecoder().decode(text), "policy_version_ref": "p:other"}
                            if text == row[1] else json.JSONDecoder().decode(text))):
            with self.assertRaisesRegex(MissionDocumentResearchExecutorError,
                                        "route authority drifted"):
                executor.run_once(admission["id"])

    def test_a_second_correction_when_another_model_served_the_corrected_call(self):
        """ws-7d MSFT: glm counted 64,341; opus served the corrected Work and counted 115,500."""

        from dalton_core import mission_document_research_executor as ex
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
        from tests.test_mission_document_research import DRAFT_REPLY, InputOverrunAdapter

        class TwiceOverrun(InputOverrunAdapter):
            def __init__(self, wire):
                super().__init__(wire, always=True)

            def execute(self, work, route, selected):
                self.always = self.calls < 2
                if not self.always:
                    self.overrun_work_id = None
                    self.calls += 1
                    self.budgets.append(dict(work.budget))
                    from tests.test_transcript_polish_model_worker import FakeAdapter
                    return FakeAdapter.execute(self, work, route, selected)
                return super().execute(work, route, selected)

        fixture, authority, args, _r, _l = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = TwiceOverrun(DRAFT_REPLY)
        executor, _draft, _verifier = self._executor(fixture, authority, draft_adapter=adapter)
        # A release before this one stopped it with the plain reason.
        with mock.patch.object(ex, "PROVIDER_BUDGET_EXCEEDED_AFTER_FINAL_CORRECTION",
                               ex.PROVIDER_BUDGET_EXCEEDED_AFTER_CORRECTION), \
                mock.patch.object(ex.MissionDocumentResearchExecutor,
                                  "_served_by_another_model", return_value=False):
            stopped = self._run_until(
                executor, admission, lambda item: item.get("status") == "stopped")
        self.assertEqual(stopped["reason"], ex.PROVIDER_BUDGET_EXCEEDED_AFTER_CORRECTION)
        lane = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=None, clock=fixture.harness.clock)
        state = lane._typed_recovery_state(admission, stopped["work_order_ref"])
        self.assertEqual(state["action"], "resume")
        self.assertEqual(state["reason"], "automatic_budget_second_correction_available")
        with mock.patch.object(ex.MissionDocumentResearchExecutor,
                               "_served_by_another_model", return_value=True):
            final = self._run_until(
                executor, admission, lambda item: item.get("research_status") == "candidate_staged")
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(len(adapter.budgets), 3)
        second = adapter.budgets[1]["max_input_tokens"] + InputOverrunAdapter.OVERRUN
        self.assertEqual(adapter.budgets[2]["max_input_tokens"],
                         -(-(-(-second * 5 // 4)) // 1000) * 1000)
        self.assertEqual(adapter.budgets[2]["max_cost_usd"], adapter.budgets[0]["max_cost_usd"])
        kinds = [json.loads(row[0])["kind"] for row in fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_controlled_recovery_authorizations")]
        self.assertEqual(kinds, [ex.AUTOMATIC_BUDGET_CORRECTION_CLASSIFICATION] * 2)

    def test_served_by_another_model_reads_both_route_decisions(self):
        from dalton_core.mission_document_research_executor import MissionDocumentResearchExecutor

        decisions = {
            "route:glm": {"outcome": "selected",
                          "selected_profile_version_ref": "model-profile-version:zai-glm-5-3:4",
                          "selected_endpoint": {"family": "zhipu-glm-5"}},
            "route:glm-again": {"outcome": "selected",
                                "selected_profile_version_ref": "model-profile-version:zai-glm-5-3:5",
                                "selected_endpoint": {"family": "zhipu-glm-5"}},
            "route:opus": {"outcome": "selected",
                           "selected_profile_version_ref":
                               "model-profile-version:broker-claude-opus-5-738752cd053d9ebf:5",
                           "selected_endpoint": {"family": "anthropic-claude-5"}},
        }
        router = mock.Mock()
        router.get_decision.side_effect = lambda ref: decisions[ref]
        fake = mock.Mock(draft_worker=mock.Mock(router=router))
        link = {"failure_proof": {"route_decision_ref": "route:glm"}}

        def formal(route):
            from dalton_core.contracts import ResultEnvelope

            envelope = ResultEnvelope(
                schema_version="0.1", id="result:x", created_at=NOW.isoformat(),
                work_order_ref="work:x", invocation_ref="invocation:x", status="failed",
                outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
                error={"code": "PROVIDER_BUDGET_EXCEEDED"},
                metadata={"route_decision_ref": route}).to_dict()
            return {"result_envelope": envelope}

        method = MissionDocumentResearchExecutor._served_by_another_model
        self.assertTrue(method(fake, 1, link, formal("route:opus")))
        self.assertFalse(method(fake, 1, link, formal("route:glm-again")))
        self.assertFalse(method(fake, 1, {"failure_proof": {}}, formal("route:opus")))


class QuestionDedupeTests(unittest.TestCase):
    def test_a_particle_does_not_make_a_new_question(self):
        from dalton_core.document_inquiry_equivalence import (
            inquiry_signature, signatures_equivalent)

        first = inquiry_signature(
            "company:msft", "MSFT FY2025 末融资租赁负债有多少？按年份怎样到期？长期债务余额是多少？", None)
        second = inquiry_signature(
            "company:msft", "MSFT FY2025 末的融资租赁负债有多少？按年份怎样到期？长期债务余额是多少？", None)
        self.assertEqual(signatures_equivalent(first, second), "question")
        other = inquiry_signature("company:msft", "MSFT FY2025 按地区的收入怎么拆分？", None)
        self.assertIsNone(signatures_equivalent(first, other))
        elsewhere = inquiry_signature(
            "company:aapl", "MSFT FY2025 末融资租赁负债有多少？按年份怎样到期？长期债务余额是多少？", None)
        self.assertIsNone(signatures_equivalent(first, elsewhere))


class RecoveryCardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        runs = self.state / "mission-document-research-runs"
        runs.mkdir()
        (runs / "holds.json").write_text(json.dumps({"holds": {
            "mission-document-research-admission:09b4bb825ae8ef44b3249de067ca4f76": {
                "disposition": "recovery_required",
                "reason": "reentry_failed_after_automatic_rebind:controlled reentry was already attempted"},
            "mission-document-research-admission:5a2365a22059a0000000000000000000": {
                "disposition": "recovery_required",
                "reason": "provider_budget_exceeded_after_corrected_retry:input=115500>81000:need=145000"},
        }}))

    def test_held_tasks_carry_their_doors_and_open_the_run_control(self):
        from dalton_core.needs_human import collect

        listed = collect(state_dir=self.state, clock=lambda: NOW)
        cards = {item["detail"].get("admission_ref"): item for item in listed["items"]
                 if item["kind"] == "controlled_recovery" and item["detail"].get("admission_ref")}
        epam = cards["mission-document-research-admission:09b4bb825ae8ef44b3249de067ca4f76"]
        self.assertEqual(epam["entry"]["mode"], "admin")
        self.assertEqual(epam["entry"]["href"], "/admin#run")
        self.assertEqual([a["door"] for a in epam["entry"]["actions"]], ["reentry", "withdraw"])
        self.assertIn("恢复专项课题研究", epam["action"])
        self.assertNotIn("待决事项", json.dumps(epam["entry"], ensure_ascii=False))
        msft = cards["mission-document-research-admission:5a2365a22059a0000000000000000000"]
        self.assertEqual([a["door"] for a in msft["entry"]["actions"]], ["withdraw"])
        # The CLI stays in 技术详情.
        self.assertTrue(any("document_recovery_cli" in (c.get("apply") or c.get("preview") or "")
                            for c in msft["detail"]["commands"]))
        aggregate = next(item for item in listed["items"] if item["ref"] == "lane:mission_document_research")
        self.assertEqual(aggregate["entry"]["mode"], "admin")
        for item in listed["items"]:
            self.assertNotEqual(item["entry"].get("label"), "到下方「待决事项」处理")

    def test_a_held_duplicate_question_says_so(self):
        from dalton_core.needs_human import _open, held_lanes

        runs = self.state / "mission-document-research-runs"
        first = "mission-document-research-admission:" + "5a" * 16
        second = "mission-document-research-admission:" + "5f" * 16
        reason = "provider_budget_exceeded_after_corrected_retry:input=115500>81000:need=145000"
        (runs / "holds.json").write_text(json.dumps({"holds": {
            first: {"disposition": "recovery_required", "reason": reason},
            second: {"disposition": "recovery_required", "reason": reason}}}))
        connection = sqlite3.connect(self.state / "core.sqlite")
        connection.executescript(
            "CREATE TABLE mission_document_research_admissions("
            "admission_id TEXT, company_ref TEXT, question_version_ref TEXT);"
            "CREATE TABLE backlog_question_versions(version_id TEXT, question TEXT);")
        connection.executemany("INSERT INTO mission_document_research_admissions VALUES(?,?,?)", [
            (first, "company:sec-cik:0000789019", "q:1"), (second, "company:sec-cik:0000789019", "q:2")])
        connection.executemany("INSERT INTO backlog_question_versions VALUES(?,?)", [
            ("q:1", "MSFT FY2025 末融资租赁负债有多少？按年份怎样到期？长期债务余额是多少？"),
            ("q:2", "MSFT FY2025 末的融资租赁负债有多少？按年份怎样到期？长期债务余额是多少？")])
        connection.commit()
        connection.close()
        core = _open(self.state / "core.sqlite")
        self.addCleanup(core.close)
        items = {item["detail"].get("admission_ref"): item
                 for item in held_lanes(None, state_dir=self.state, core=core)}
        self.assertNotIn("duplicate_of", items[first]["detail"])
        self.assertEqual(items[second]["detail"]["duplicate_of"], first)
        self.assertIn("同一个问题", items[second]["action"])

    def test_the_admin_run_section_lists_held_tasks(self):
        from dalton_core.cockpit_v2.admin.data import held_tasks
        from dalton_core.needs_human import collect

        tasks = held_tasks(collect(state_dir=self.state, clock=lambda: NOW))
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task["doors"] for task in tasks))


class CockpitOwnerDoorTests(unittest.TestCase):
    def _plane(self):
        plane = mock.Mock()
        plane._governance.return_value = {"status": "granted"}
        return plane

    def test_each_door_is_its_writer_operation_with_the_reason(self):
        from dalton_core.cockpit_owner_doors import owner_door

        admission = "mission-document-research-admission:09b4bb825ae8ef44b3249de067ca4f76"
        plane = self._plane()
        out = owner_door(plane, "owner@example", {"door": "reentry", "admission_ref": admission,
                                                  "reason": "路由已恢复"})
        plane._governance.assert_called_with(
            "owner@example", "authorize_mission_document_reentry", {"admission_ref": admission},
            failure=mock.ANY)
        self.assertIn("已恢复专项课题研究", out["message"])
        self.assertNotIn("不产生费用", out["message"])           # batch am: no petty-billing words
        owner_door(plane, "owner@example", {"door": "withdraw", "admission_ref": admission,
                                            "reason": "不再需要"})
        self.assertEqual(plane._governance.call_args.args[2],
                         {"admission_ref": admission, "reason": "不再需要"})
        owner_door(plane, "owner@example", {"door": "paid", "admission_ref": admission,
                                            "reason": "值得", "max_cost_usd": "0.8"})
        self.assertEqual(plane._governance.call_args.args[1:3], (
            "authorize_mission_document_paid_recovery",
            {"admission_ref": admission, "max_cost_usd": 0.8}))

    def test_bad_requests_are_refused_before_the_writer(self):
        from dalton_core.cockpit_owner_doors import owner_door
        from dalton_core.cockpit_plane import CockpitError

        admission = "mission-document-research-admission:09b4bb825ae8ef44b3249de067ca4f76"
        plane = self._plane()
        for value in ({"door": "nuke", "admission_ref": admission, "reason": "x"},
                      {"door": "reentry", "admission_ref": "lane:x", "reason": "x"},
                      {"door": "reentry", "admission_ref": admission, "reason": " "},
                      {"door": "paid", "admission_ref": admission, "reason": "x"},
                      {"door": "paid", "admission_ref": admission, "reason": "x",
                       "max_cost_usd": "500"}):
            with self.subTest(value=value), self.assertRaises(CockpitError):
                owner_door(plane, "owner@example", value)
        plane._governance.assert_not_called()

    def test_the_admin_whitelist_and_the_cockpit_dispatch_know_the_new_actions(self):
        from dalton_core.cockpit_v2.admin.routes import ACTIONS
        from dalton_core.writer_server import HUMAN_GOVERNANCE_OPERATIONS, OPERATION_FIELDS

        self.assertTrue(ACTIONS["owner_door"]["native_reason"])
        self.assertTrue(ACTIONS["owner_door"]["danger"])
        self.assertTrue(ACTIONS["needs_human_ack"]["native_reason"])
        self.assertIn("acknowledge_needs_human_card", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertIn("batch_ref", OPERATION_FIELDS["retry_publication_language_review"])
        source = (Path(__file__).parents[1] / "src/dalton_core/agenda_control.py").read_text()
        self.assertIn('action == "owner_door"', source)
        self.assertIn('action == "needs_human_ack"', source)


# ---------------------------------------------------------------------------
# 4. the ACN press release, typed EX-99 under a non-standard name
# ---------------------------------------------------------------------------


class EarningsExhibitTests(unittest.TestCase):
    INDEX = FIXTURES / "0001467373-26-000037-index.html"
    PRIMARY = FIXTURES / "acn-20261001.htm"

    def test_the_filing_index_names_the_ex99_document(self):
        from dalton_core.earnings_release_intake import exhibit_name_from_filing_index

        self.assertEqual(exhibit_name_from_filing_index(self.INDEX.read_bytes()),
                         "q4fy26earnings8-kexhibit.htm")

    def test_ex99_1_wins_and_two_unlabelled_ex99s_are_ambiguous(self):
        from dalton_core.earnings_release_intake import exhibit_name_from_filing_index

        def table(*rows):
            body = "".join(
                f'<tr><td>{i}</td><td>{desc}</td><td><a href="/Archives/edgar/data/1/0001/{name}">'
                f'{name}</a></td><td>{kind}</td><td>1</td></tr>' for i, (desc, name, kind) in enumerate(rows))
            return f'<table class="tableFile" summary="Document Format Files">{body}</table>'

        self.assertEqual(exhibit_name_from_filing_index(table(
            ("SLIDES", "slides.htm", "EX-99.2"), ("RELEASE", "pr.htm", "EX-99.1"))), "pr.htm")
        self.assertIsNone(exhibit_name_from_filing_index(table(
            ("A", "a.htm", "EX-99.2"), ("B", "b.htm", "EX-99.3"))))
        self.assertEqual(exhibit_name_from_filing_index(table(
            ("PRESS RELEASE DATED", "a.htm", "EX-99.2"), ("SLIDES", "b.htm", "EX-99.3"))), "a.htm")
        self.assertIsNone(exhibit_name_from_filing_index(table(("COVER", "c.htm", "GRAPHIC"))))

    def test_the_8ks_own_exhibit_index_numbered_99_is_read_too(self):
        from dalton_core.earnings_release_intake import press_release_exhibit_name

        self.assertEqual(press_release_exhibit_name(self.PRIMARY.read_bytes()),
                         "q4fy26earnings8-kexhibit.htm")

    def _authority(self):
        from dalton_core.public_web_connector import (
            public_web_url_ref, validate_public_web_url_authority)
        from dalton_core.store import content_hash

        url = "https://www.sec.gov/Archives/edgar/data/1467373/000146737326000037/acn-20261001.htm"
        url_ref = public_web_url_ref(url)
        base = {"schema_version": "0.1",
                "id": "public-web-url-authority:" + content_hash(
                    {"url_ref": url_ref, "source_envelope_hash": "b" * 64}),
                "created_at": "2026-10-01T00:00:00.000000+00:00", "url_ref": url_ref,
                "canonical_url": url, "url_hash": url_ref.removeprefix("public-web-url:sha256:"),
                "host": "www.sec.gov", "discovery_source_envelope_ref": "source-envelope:x",
                "discovery_source_envelope_hash": "b" * 64,
                "discovery_raw_artifact_version_ref": "artifact-version:x", "search_record_ref": url_ref}
        return validate_public_web_url_authority({**base, "content_hash": content_hash(base)})

    def test_the_fetch_child_reads_the_document_table_first(self):
        from dalton_core.public_web_fetch_cli import fetch_press_release_exhibit

        bodies = {"primary": b"<html>Item 2.02 Exhibit 99 News Release</html>",
                  "index": self.INDEX.read_bytes()}
        requested = []

        class Spool:
            def read_object(self, digest):
                return bodies[digest]

        class Fetch:
            def build_request(self, authority):
                requested.append(authority["canonical_url"].rsplit("/", 1)[-1])
                return authority

            def fetch(self, request):
                name = request["canonical_url"].rsplit("/", 1)[-1]
                return {"outcome": "succeeded", "document_ref": name, "provider_calls": 1,
                        "raw_response_hash": "index" if name.endswith("-index.html") else "x"}

            def manifest(self, receipt):
                return {"id": "public-web-fetch-manifest:m", "content_hash": "h" * 64,
                        "body_sha256": "a" * 64, "body_bytes": 639970}

        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_press_release_exhibit(
                Fetch(), Spool(), primary_authority=self._authority(),
                primary_receipt={"raw_response_hash": "primary"}, out=Path(tmp))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["detected_by"], "filing_index")
        self.assertEqual(requested, ["0001467373-26-000037-index.html",
                                     "q4fy26earnings8-kexhibit.htm"])

    def test_a_detection_failure_is_redetected_once_and_then_read(self):
        from dalton_core.earnings_release_intake import (
            EXHIBIT_DETECTOR_VERSION, EXHIBIT_NOT_FOUND_REASON, exhibit_for_ticket,
            exhibit_redetect_due)

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            ticket = "public-web-fetch:" + "a" * 24
            directory = state / "fetches" / ("a" * 24)
            directory.mkdir(parents=True)
            summary = {"status": "succeeded", "url_ref": "sec:filing:0001467373-26-000037",
                       "exhibit": {"status": "failed", "failure_reason": EXHIBIT_NOT_FOUND_REASON}}
            path = directory / "summary.json"
            path.write_text(json.dumps(summary))
            path.chmod(0o600)
            self.assertTrue(exhibit_redetect_due(state, ticket))
            redetect = directory / "exhibit-redetect.json"
            redetect.write_text(json.dumps({"ticket_ref": ticket,
                                            "detector_version": EXHIBIT_DETECTOR_VERSION,
                                            "exhibit": {"status": "failed",
                                                        "failure_reason": "fetch outcome failed"}}))
            redetect.chmod(0o600)
            self.assertFalse(exhibit_redetect_due(state, ticket))
            read = exhibit_for_ticket(state, ticket, "sec:filing:0001467373-26-000037")
            self.assertEqual(read["reason"], "fetch outcome failed")
            # A network failure is not a detection failure: never redetected.
            summary["exhibit"]["failure_reason"] = "fetch outcome failed"
            path.write_text(json.dumps(summary))
            redetect.unlink()
            self.assertFalse(exhibit_redetect_due(state, ticket))

    def test_the_launcher_starts_a_redetect_child_in_its_own_slot(self):
        from dalton_core.public_web_fetch_launcher import PublicWebFetchLauncher

        with tempfile.TemporaryDirectory() as tmp:
            launcher = PublicWebFetchLauncher.__new__(PublicWebFetchLauncher)
            command = PublicWebFetchLauncher._command(
                mock.Mock(python_executable="python", state_dir=Path(tmp), governance_path=Path(tmp),
                          catalog_db=Path(tmp), user_agent="ua", spool_dir=None, mode_args=()),
                url_ref=None, requested_by="automation:x", ticket_dir=Path(tmp),
                exhibit_ticket_ref="public-web-fetch:" + "a" * 24)
            self.assertIn("--exhibit-for-ticket", command)
            self.assertNotIn("--url-ref", command)
            del launcher


# ---------------------------------------------------------------------------
# 5. acknowledgement
# ---------------------------------------------------------------------------


class AcknowledgementTests(unittest.TestCase):
    def setUp(self):
        from dalton_core.store import DaltonStore

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)

    def _card(self, failures=32):
        return {"kind": "fetch_failure_capped", "ref": "fetch-cap:public-web-url:sha256:" + "f" * 64,
                "title": "ACN · Accenture 的一份资料连续 32 次没取回来",
                "detail": {"document_ref": "public-web-url:sha256:" + "f" * 64,
                           "consecutive_failures": failures}}

    def test_noted_hides_until_the_condition_changes(self):
        from dalton_core.needs_human_ack import (
            NeedsHumanAckAuthority, condition_hash, latest_acknowledgements, split_acknowledged)

        card = self._card()
        authority = NeedsHumanAckAuthority(self.store)
        receipt = authority.record(card_ref=card["ref"], kind=card["kind"], choice="noted",
                                   condition_hash=condition_hash(card), reason="已交代",
                                   actor_ref="human:owner", idempotency_key="k1", now=NOW)
        self.assertEqual(receipt["status"], "recorded")
        replay = authority.record(card_ref=card["ref"], kind=card["kind"], choice="noted",
                                  condition_hash=condition_hash(card), reason="已交代",
                                  actor_ref="human:owner", idempotency_key="k1", now=NOW)
        self.assertEqual(replay["status"], "replayed")
        acks = latest_acknowledgements(self.store.connection)
        shown, hidden = split_acknowledged([card], acks, now=NOW)
        self.assertEqual((shown, [h["choice"] for h in hidden]), ([], ["noted"]))
        shown, hidden = split_acknowledged([self._card(failures=33)], acks, now=NOW)
        self.assertEqual(len(shown), 1)

    def test_a_snooze_expires_and_bad_answers_are_refused(self):
        from dalton_core.needs_human_ack import (
            NeedsHumanAckAuthority, NeedsHumanAckError, condition_hash, hides,
            latest_acknowledgements)

        card = self._card()
        authority = NeedsHumanAckAuthority(self.store)
        authority.record(card_ref=card["ref"], kind=card["kind"], choice="snooze",
                         condition_hash=condition_hash(card), reason="一周后再看",
                         actor_ref="human:owner", snooze_days=7, now=NOW)
        ack = latest_acknowledgements(self.store.connection)[card["ref"]]
        self.assertTrue(hides(card, ack, now=NOW + timedelta(days=6)))
        self.assertFalse(hides(card, ack, now=NOW + timedelta(days=8)))
        for kwargs in ({"actor_ref": "automation:x"}, {"reason": " "},
                       {"choice": "abandon_document", "card_ref": "raw-spool:capacity",
                        "kind": "raw_spool_capacity"},
                       {"kind": "gate_decision"}, {"snooze_days": 99}):
            values = {"card_ref": card["ref"], "kind": card["kind"], "choice": "snooze",
                      "condition_hash": condition_hash(card), "reason": "x",
                      "actor_ref": "human:owner", "snooze_days": 7, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(NeedsHumanAckError):
                authority.record(**values, now=NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM needs_human_acknowledgements")

    def test_abandoning_a_document_retires_it_for_the_coordinator(self):
        from dalton_core.needs_human_ack import (
            NeedsHumanAckAuthority, NeedsHumanAckError, abandoned_document_refs, condition_hash)

        card = self._card()
        authority = NeedsHumanAckAuthority(self.store)
        with self.assertRaisesRegex(NeedsHumanAckError, "no discovered document"):
            authority.record(card_ref=card["ref"], kind=card["kind"], choice="abandon_document",
                             condition_hash=condition_hash(card), reason="来源不提供",
                             actor_ref="human:owner", now=NOW)
        self.store.connection.execute(
            "CREATE TABLE coverage_mission_discovered_documents(document_ref TEXT)")
        self.store.connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?)",
            (card["detail"]["document_ref"],))
        authority.record(card_ref=card["ref"], kind=card["kind"], choice="abandon_document",
                         condition_hash=condition_hash(card), reason="来源不提供",
                         actor_ref="human:owner", now=NOW)
        self.assertEqual(abandoned_document_refs(self.store.connection),
                         (card["detail"]["document_ref"],))

    def test_spool_bands_bring_the_card_back_when_crossing_higher(self):
        from dalton_core.needs_human_ack import condition_hash

        def spool(percent, over=False):
            return {"kind": "raw_spool_capacity", "ref": "raw-spool:capacity",
                    "detail": {"percent": percent, "over_ceiling": over, "max_total_bytes": 4}}

        self.assertEqual(condition_hash(spool(98)), condition_hash(spool(99)))
        self.assertNotEqual(condition_hash(spool(94)), condition_hash(spool(96)))
        self.assertNotEqual(condition_hash(spool(99)), condition_hash(spool(101, True)))

    def test_collect_hides_acknowledged_cards_and_lists_them(self):
        from dalton_core.needs_human import collect, completion_entry
        from dalton_core.needs_human_ack import NeedsHumanAckAuthority

        card = {"kind": "raw_spool_capacity", "ref": "raw-spool:capacity", "at": NOW.isoformat(),
                "urgency": 2, "urgency_label": "x", "title": "连接器原始资料存储已用到上限的 98%",
                "why_blocked": "", "action": "", "consequence": "", "actionable": True,
                "where": "交给技术管理员在本机处理（命令见技术详情）",
                "detail": {"percent": 98, "over_ceiling": False, "max_total_bytes": 4_000_000_000}}
        entry = completion_entry(card)
        self.assertEqual(entry["mode"], "acknowledge")
        self.assertEqual([c["choice"] for c in entry["choices"]], ["noted", "snooze"])
        NeedsHumanAckAuthority(self.store).record(
            card_ref=entry["card_ref"], kind=entry["kind"], choice="noted",
            condition_hash=entry["condition_hash"], reason="已安排归档",
            actor_ref="human:owner", now=NOW)
        with mock.patch("dalton_core.needs_human.raw_spool_capacity", return_value=[card]):
            listed = collect(state_dir=self.state, clock=lambda: NOW)
        self.assertEqual(listed["items"], [])
        self.assertEqual(listed["acknowledged"][0]["choice_label"], "已阅知悉")

    def test_informational_cards_offer_their_choices(self):
        from dalton_core.needs_human import completion_entry

        capped = completion_entry({**self._card(), "where": "资料来源", "where_view": "goal",
                                   "where_open": "sources"})
        self.assertEqual(capped["mode"], "acknowledge")
        self.assertEqual([c["choice"] for c in capped["choices"]],
                         ["noted", "snooze", "abandon_document"])
        self.assertTrue(capped["choices"][2]["danger"])
        release = completion_entry({
            "kind": "earnings_release_unread", "ref": "earnings-release:x", "where": "SEC",
            "detail": {"accession": "0001467373-26-000037", "status": "exhibit_unavailable",
                       "exhibit_url": "https://www.sec.gov/x.htm"}})
        self.assertEqual(release["mode"], "acknowledge")
        self.assertEqual(release["secondary"]["mode"], "link")
        gate = completion_entry({"kind": "gate_decision", "ref": "g", "where": "待办审批",
                                 "where_view": "approve", "detail": {}})
        self.assertEqual(gate, {"mode": "approvals", "label": "到「需要你确认的研究决定」处理"})

    def test_the_writer_op_records_through_the_authority(self):
        from dalton_core.needs_human_ack import condition_hash
        from dalton_core.writer_server import WriterServer

        card = self._card()
        fake = mock.Mock(store=self.store, _now=lambda: NOW)
        fake._needs_human_ack_authority = None
        out = WriterServer._op_acknowledge_needs_human_card(fake, {
            "card_ref": card["ref"], "kind": card["kind"], "choice": "noted",
            "condition_hash": condition_hash(card), "reason": "知道了", "actor_ref": "human:owner"})
        self.assertEqual(out["status"], "recorded")


# ---------------------------------------------------------------------------
# the v2 待我处理 page renders the entries
# ---------------------------------------------------------------------------


class TasksPageEntryTests(unittest.TestCase):
    def test_server_entries_render_as_buttons(self):
        import shutil

        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        from tests.test_cockpit_v2_shell import FIXTURES as PAGE_FIXTURES, node

        fixtures = json.loads(json.dumps(PAGE_FIXTURES, ensure_ascii=False))
        fixtures["/v1/cockpit/needs-human"] = {"as_of": NOW.isoformat(), "items": [
            {"kind": "controlled_recovery", "ref": "lane:x:a", "at": NOW.isoformat(), "urgency": 2,
             "urgency_label": "有一整个环节停着", "title": "等你决定：EPAM · 收入怎么拆分？",
             "why_blocked": "", "action": "", "consequence": "", "actionable": True,
             "detail": {"admission_ref": "mission-document-research-admission:09b4", "lane": "x"},
             "entry": {"mode": "admin", "href": "/admin#run", "label": "在「管理员设置 → 运行控制」处理",
                       "actions": [{"door": "reentry", "label": "恢复研究任务（不产生费用）",
                                    "admission_ref": "mission-document-research-admission:09b4"},
                                   {"door": "withdraw", "label": "停止这项研究（已有成果保留）",
                                    "admission_ref": "mission-document-research-admission:09b4",
                                    "danger": True}]}},
            {"kind": "publication_parked", "ref": "publication-parked:x", "at": NOW.isoformat(),
             "urgency": 4, "urgency_label": "积压的待办", "title": "一批界面文字的中文翻译停下了",
             "why_blocked": "", "action": "", "consequence": "", "actionable": True, "detail": {},
             "entry": {"mode": "action", "action": "language_retry", "label": "重新进行语言质检",
                       "payload": {"batch_ref": "ui-text-batch:" + "4" * 64},
                       "acknowledge": {"card_ref": "publication-parked:x", "kind": "publication_parked",
                                       "condition_hash": "0" * 64,
                                       "choices": [{"choice": "noted", "label": "已知悉"}]}}},
            {"kind": "fetch_failure_capped", "ref": "fetch-cap:x", "at": NOW.isoformat(), "urgency": 4,
             "urgency_label": "积压的待办", "title": "ACN · Accenture 的一份资料连续 32 次没取回来",
             "why_blocked": "", "action": "", "consequence": "", "actionable": True, "detail": {},
             "entry": {"mode": "acknowledge", "card_ref": "fetch-cap:x", "kind": "fetch_failure_capped",
                       "condition_hash": "0" * 64, "choices": [
                           {"choice": "noted", "label": "已知悉"},
                           {"choice": "snooze", "label": "暂不提醒（选天数）", "days": [1, 7]},
                           {"choice": "abandon_document", "label": "放弃这份资料", "danger": True}]}},
        ]}
        out = node("pages", {"fixtures": fixtures, "render": {"tasks": "#/tasks"}})
        buttons = out["results"]["tasks"]["buttons"]
        # 2026-10-09: the business words (old labels from an older server are
        # relabelled by copy(): 放行/再买/撤回 never reach the page).
        # Batch am: the labels an older server sent are relabelled by copy()
        # into the terminology registry's words.
        # The doors themselves read the registry's labels by door name.
        for label in ("恢复专项课题研究", "暂缓当前专项课题（保留现有底稿）",
                      "重新生成中文定稿", "已阅知悉", "静音该提示", "忽略该缺失资料（采用现有数据推导）"):
            self.assertIn(label, buttons)
        self.assertNotIn("复制给管理员的摘要", buttons)


if __name__ == "__main__":
    unittest.main()
