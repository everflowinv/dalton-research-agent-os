"""WP-A: a failing endpoint must stop being chosen, and stop being charged for.

2026-09-16. ``profile:gpt-6-astra`` answered HTTP 429 to every brain-tier call
from 2026-09-14T19:58 onwards and stayed first in five routing policies' brain
chains. Six hours of it moved 286 USD through the day ledger against zero served
calls, emptied the pools and left every other lane refused. Three separate
things had to be true at once for that to happen, and there is a test here for
each of them:

A1  a rate-limited failure was settled at the chain's reserved ceiling;
A2  nothing stopped the router offering the same dead endpoint next time;
A3  a chain link whose transport cannot carry the prompt was still dispatched to.

Plus A4: the tier save path accepted a profile id that resolves to nothing
routable, which is how ``model-profile:claude-opus-5`` sat in position four of
every brain chain for a month.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.cockpit_model import no_charge_reason
from dalton_core.contracts import WorkOrder
from dalton_core.cockpit_model_display import (
    chain_link_note,
    cooldown_index,
    cooldown_note,
    purpose_cooldown_note,
)
from dalton_core.model_fallback_chain import (
    FallbackChainError,
    execute_chain,
    profile_transport,
    profiles_serving_transport,
    register_purpose_transport,
    required_transport,
    routing_overview,
    tier_chain,
    validate_selection,
)
from dalton_core.model_profile_bounds import (
    effective_input_bound,
    exceeds_input_bound,
)
from dalton_core.model_profile_health import (
    BASE_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    cooldown_seconds,
    is_immediate_cooldown_code,
)
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from tests.test_openclaw_catalog_reconcile import _config

NOW = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
ASTRA = "profile:gpt-6-astra"
FABLE = "profile:claude-fable-5-1"
ANTIGRAVITY = "profile:gemini-3-8-flash-antigravity"


class _Envelope:
    """The shape ``no_charge_reason`` reads: a failed broker result envelope."""

    def __init__(self, code: str, *, proof: dict | None = None) -> None:
        self.status = "failed"
        self.error = {"code": code, "message": "no"}
        self.metadata = {} if proof is None else {"dispatch_proof": proof}


class _Invocation:
    def __init__(self, usage: dict | None = None) -> None:
        self.usage = usage or {}


PROVIDER_COMPLETED = {
    "authority": "openclaw-model-adapter",
    "state": "provider_completed_failure",
    "version": "0.1",
}
POST_SEND_UNKNOWN = {
    "authority": "openclaw-model-adapter",
    "state": "post_send_result_unknown",
    "version": "0.1",
}


def _work(work_id: str) -> WorkOrder:
    moment = NOW.isoformat(timespec="microseconds")
    return WorkOrder(
        schema_version="0.1",
        id=work_id,
        created_at=moment,
        updated_at=moment,
        question="what should the research work on next?",
        requested_capabilities=("research",),
        runtime_profile_ref="runtime-profile:dalton-model-broker:0.1",
        budget={
            "max_input_tokens": 200_000,
            "max_output_tokens": 20_000,
            "max_total_tokens": 220_000,
            "max_cost_usd": 10.0,
            "max_seconds": 120,
        },
        idempotency_key=f"{work_id}:1",
        declared_side_effects=(),
        status="ready",
        input_refs=(),
        metadata={},
    )


class ZeroSettlementProofTests(unittest.TestCase):
    """A1: which failures are provably free, and which are merely unmeasured."""

    def test_a_rate_limit_in_any_of_its_spellings_is_free(self) -> None:
        for code in ("RATE_LIMITED", "PROVIDER_RATE_LIMITED", "TOO_MANY_REQUESTS",
                     "THROTTLED", "HTTP_429", "QUOTA_EXCEEDED"):
            with self.subTest(code=code):
                self.assertEqual(
                    no_charge_reason(_Invocation(), _Envelope(code)),
                    "provider_rate_limited",
                )

    def test_a_provider_completed_failure_with_no_usage_is_free(self) -> None:
        # The broker's protocol refuses a failed response that claims usage or
        # an available cost, so "the provider completed a failure" and "the
        # provider metered nothing" are one statement.
        self.assertEqual(
            no_charge_reason(
                _Invocation({"input_tokens": None, "output_tokens": None,
                             "raw_provider_telemetry": {
                                 "cost": {"available": False, "usd": None}}}),
                _Envelope("PROVIDER_INTERNAL_ERROR", proof=PROVIDER_COMPLETED),
            ),
            "provider_completed_without_usage",
        )

    def test_a_failure_that_may_have_been_billed_keeps_its_reservation(self) -> None:
        # The conservative rule, unchanged. A transport failure after the bytes
        # left, and a provider-completed failure that *did* report tokens, are
        # both "we cannot prove this was free".
        self.assertIsNone(no_charge_reason(
            _Invocation(), _Envelope("INVALID_HOST_RESULT")))
        self.assertIsNone(no_charge_reason(
            _Invocation(), _Envelope("UPSTREAM_TIMEOUT", proof=POST_SEND_UNKNOWN)))
        self.assertIsNone(no_charge_reason(
            _Invocation({"input_tokens": 4_000, "output_tokens": 0}),
            _Envelope("PROVIDER_INTERNAL_ERROR", proof=PROVIDER_COMPLETED)))
        self.assertIsNone(no_charge_reason(
            _Invocation({"raw_provider_telemetry": {
                "cost": {"available": True, "usd": 0.4}}}),
            _Envelope("PROVIDER_INTERNAL_ERROR", proof=PROVIDER_COMPLETED)))


class CooldownArithmeticTests(unittest.TestCase):
    """A2: the backoff, on its own."""

    def test_it_doubles_and_then_stops_doubling(self) -> None:
        self.assertEqual(cooldown_seconds(1), BASE_COOLDOWN_SECONDS)
        self.assertEqual(cooldown_seconds(2), BASE_COOLDOWN_SECONDS * 2)
        self.assertEqual(cooldown_seconds(3), BASE_COOLDOWN_SECONDS * 4)
        self.assertEqual(cooldown_seconds(99), MAX_COOLDOWN_SECONDS)
        self.assertLessEqual(cooldown_seconds(4), MAX_COOLDOWN_SECONDS)

    def test_only_a_gate_refusal_trips_it_on_its_own(self) -> None:
        for code in ("RATE_LIMITED", "TOO_MANY_REQUESTS", "THROTTLED",
                     "RESOURCE_EXHAUSTED", "HTTP_429"):
            self.assertTrue(is_immediate_cooldown_code(code), code)
        for code in ("PROVIDER_INTERNAL_ERROR", "HTTP_503", "TIMEOUT", "", None):
            self.assertFalse(is_immediate_cooldown_code(code), code)


class _RouterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = NOW
        self.router = ModelRouter(
            Path(self.directory.name) / "router.sqlite",
            clock=lambda: self.clock,
        )
        self.addCleanup(self.router.close)
        sync_openclaw_model_catalog(
            self.router, _config(), checked_at=NOW,
            availability_ttl=timedelta(days=3650))
        self.policy = ensure_planner_policy(
            self.router, tier="brain", now=NOW,
            policy_id="model-routing-policy:wpa-test-brain",
        )["policy_version_ref"]

    def _route(self, work_id: str, *, estimated_input: int = 1_000,
               required_profile_version_ref: str | None = None) -> dict:
        return self.router.route(
            _work(work_id),
            attempt_number=1,
            capability="research",
            policy_version_ref=self.policy,
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain")) + [ANTIGRAVITY]),
            required_modalities=["text"],
            required_context_tokens=estimated_input + 500,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=500,
            idempotency_key=f"wpa:{work_id}",
            tier="brain",
            purpose="plan",
            required_profile_version_ref=required_profile_version_ref,
        )["decision"]

    def _reason_for(self, decision: dict, profile_id: str) -> list[str]:
        for item in decision["candidate_snapshot"]:
            profile = self.router.get_profile(item["profile_version_ref"])
            if profile["id"] == profile_id:
                return item["rejection_reasons"]
        return []


class CooldownPersistenceTests(_RouterCase):
    """A2: one 429 is enough, five ordinary failures are the alternative."""

    def test_one_rate_limit_cools_the_profile_and_routing_stops_offering_it(self) -> None:
        outcome = self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure", failure_code="RATE_LIMITED")
        self.assertEqual(outcome["status"], "cooled")
        self.assertEqual(outcome["cooldown"]["reason"], "provider_rate_limited")
        self.assertEqual(outcome["cooldown"]["streak"], 1)
        self.assertIn(ASTRA, self.router.active_cooldowns())

        decision = self._route("work:wpa-cooled")
        self.assertIn("provider_cooldown", self._reason_for(decision, ASTRA))
        self.assertEqual(decision["outcome"], "selected")
        selected = self.router.get_profile(decision["selected_profile_version_ref"])
        self.assertNotEqual(selected["id"], ASTRA)

    def test_an_ordinary_provider_failure_needs_a_streak(self) -> None:
        for index in range(4):
            outcome = self.router.record_provider_outcome(
                profile_id=ASTRA, outcome="provider_failure",
                failure_code="PROVIDER_INTERNAL_ERROR")
            self.assertIsNone(outcome["cooldown"], index)
        fifth = self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure",
            failure_code="PROVIDER_INTERNAL_ERROR")
        self.assertEqual(fifth["cooldown"]["reason"], "provider_failure_streak")
        self.assertEqual(fifth["cooldown"]["failure_count"], 5)

    def test_a_served_call_clears_the_streak_and_the_cooldown(self) -> None:
        for _ in range(4):
            self.router.record_provider_outcome(
                profile_id=ASTRA, outcome="provider_failure",
                failure_code="PROVIDER_INTERNAL_ERROR")
        self.router.record_provider_outcome(profile_id=ASTRA, outcome="served")
        self.assertIsNone(self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure",
            failure_code="PROVIDER_INTERNAL_ERROR")["cooldown"])

    def test_the_cooldown_expires_and_the_probe_that_fails_doubles_it(self) -> None:
        first = self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure",
            failure_code="RATE_LIMITED")
        self.assertEqual(first["cooldown"]["cooldown_seconds"], BASE_COOLDOWN_SECONDS)

        # While it is running, routing refuses the profile and a late failure
        # does not extend the window.
        self.assertIn(ASTRA, self.router.active_cooldowns())
        self.assertEqual(self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure",
            failure_code="RATE_LIMITED")["status"], "already_cooling")

        # Past the window the profile is selectable again: that is the probe.
        self.clock = NOW + timedelta(seconds=BASE_COOLDOWN_SECONDS + 1)
        self.assertEqual(self.router.active_cooldowns(), {})
        self.assertNotIn("provider_cooldown",
                         self._reason_for(self._route("work:wpa-probe"), ASTRA))

        # And a probe that fails is cooled again for twice as long, without
        # waiting for another streak.
        second = self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure",
            failure_code="PROVIDER_INTERNAL_ERROR")
        self.assertEqual(second["cooldown"]["reason"], "probe_failed")
        self.assertEqual(second["cooldown"]["streak"], 2)
        self.assertEqual(second["cooldown"]["cooldown_seconds"],
                         BASE_COOLDOWN_SECONDS * 2)

    def test_an_exact_version_paid_retry_is_the_probe_and_is_not_refused(self) -> None:
        # The bounded provider retry pins one profile *version*. That is not a
        # selection -- refusing it would not save a call, only strand an
        # owner-configured retry.
        self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure", failure_code="RATE_LIMITED")
        pinned = next(profile["profile_version_ref"]
                      for profile in self.router.latest_profiles()
                      if profile["id"] == ASTRA)
        decision = self._route("work:wpa-pinned",
                               required_profile_version_ref=pinned)
        self.assertEqual(decision["outcome"], "selected")
        self.assertEqual(decision["selected_profile_version_ref"], pinned)

    def test_the_cooldown_is_readable_as_an_audit_row_and_on_the_model_page(self) -> None:
        self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure", failure_code="RATE_LIMITED",
            route_decision_ref="route-decision:abc")
        recorded = self.router.profile_cooldowns()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["profile_id"], ASTRA)
        self.assertEqual(recorded[0]["route_decision_ref"], "route-decision:abc")
        self.assertEqual(self.router.profile_health_events()[0]["failure_code"],
                         "RATE_LIMITED")
        overview = routing_overview(self.router)
        self.assertEqual([item["profile_id"]
                          for item in overview["cooldowns"]["active"]], [ASTRA])
        self.assertIn("429", overview["cooldowns"]["active"][0]["message"])
        self.assertEqual(overview["cooldowns"]["policy"]["failure_threshold"], 5)


class CooldownInTheChainTests(_RouterCase):
    """A2: the walk skips the cooled link, and says so in its own record."""

    def test_the_chain_skips_a_cooled_link_without_calling_or_admitting_it(self) -> None:
        self.router.record_provider_outcome(
            profile_id=ASTRA, outcome="provider_failure", failure_code="RATE_LIMITED")
        called: list[str] = []
        admitted: list[str] = []

        def call(route, profile):
            called.append(profile["id"])
            return {"outcome": "served", "value": "ok"}

        def admit(route, profile, micros):
            admitted.append(profile["id"])
            return {"status": "admitted"}

        work = _work("work:wpa-chain")
        result = execute_chain(
            self.router, work, purpose="plan", tier="brain", capability="research",
            attempt_number=1, policy_version_ref=self.policy,
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_prefix="wpa:chain", call=call, admit=admit,
        )
        self.assertEqual(result["status"], "served")
        self.assertEqual(result["profile_id"], FABLE)
        # The cooled link cost nothing: it was neither called nor admitted.
        self.assertEqual(called, [FABLE])
        self.assertEqual(admitted, [FABLE])
        links = {link["profile_id"]: link
                 for link in self.router.chain_links(work_order_id=work.id)}
        self.assertEqual(links[ASTRA]["skip_reason"], "provider_cooldown")
        self.assertFalse(links[ASTRA]["served"])
        self.assertTrue(links[FABLE]["served"])

    def test_a_rate_limited_call_cools_the_profile_for_the_next_work_order(self) -> None:
        def call(route, profile):
            if profile["id"] == ASTRA:
                return {"outcome": "failed", "failure_class": "provider_failure",
                        "error_code": "RATE_LIMITED", "reason": "429"}
            return {"outcome": "served", "value": "ok"}

        common = dict(
            purpose="plan", tier="brain", capability="research", attempt_number=1,
            policy_version_ref=self.policy,
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain("brain"))),
            required_modalities=["text"], required_context_tokens=2_000,
            estimated_input_tokens=1_000, estimated_output_tokens=500,
            call=call,
        )
        execute_chain(self.router, _work("work:wpa-first"),
                      idempotency_prefix="wpa:first", **common)
        self.assertIn(ASTRA, self.router.active_cooldowns())

        second = _work("work:wpa-second")
        result = execute_chain(self.router, second,
                               idempotency_prefix="wpa:second", **common)
        self.assertEqual(result["status"], "served")
        links = {link["profile_id"]: link
                 for link in self.router.chain_links(work_order_id=second.id)}
        self.assertEqual(links[ASTRA]["skip_reason"], "provider_cooldown")


class InputBoundTests(_RouterCase):
    """A3: a prompt the transport cannot carry does not become a paid failure."""

    def test_the_measured_ceiling_wins_over_a_catalog_that_reopened_it(self) -> None:
        profile = next(item for item in self.router.latest_profiles()
                       if item["id"] == ANTIGRAVITY)
        resolved = effective_input_bound(profile)
        self.assertEqual(resolved["source"], "measured")
        self.assertEqual(resolved["bound"], 30_000)
        self.assertGreater(resolved["declared"], 30_000)
        self.assertTrue(exceeds_input_bound(profile, 30_796))
        self.assertFalse(exceeds_input_bound(profile, 29_434))

    def test_routing_refuses_an_oversized_prompt_before_anything_is_dispatched(self) -> None:
        small = self._route("work:wpa-small", estimated_input=20_000)
        self.assertNotIn("input_bound_exceeded",
                         self._reason_for(small, ANTIGRAVITY))
        big = self._route("work:wpa-big", estimated_input=30_796)
        self.assertIn("input_bound_exceeded", self._reason_for(big, ANTIGRAVITY))
        # And the call still routes: the point is to skip one link, not to
        # refuse the work.
        self.assertEqual(big["outcome"], "selected")
        self.assertNotEqual(
            self.router.get_profile(big["selected_profile_version_ref"])["id"],
            ANTIGRAVITY)


class SelectionValidationTests(_RouterCase):
    """A4: the save path refuses an id that resolves to nothing routable."""

    def _register_expired_twin(self) -> None:
        live = next(item for item in self.router.latest_profiles()
                    if item["id"] == "profile:claude-opus-5")
        stale = {key: value for key, value in live.items()
                 if key not in {"content_hash", "profile_version_ref", "id",
                                "version", "prior_version_ref", "availability",
                                "created_at"}}
        self.router.register_profile({
            **stale,
            "profile_version_ref": "model-profile-version:claude-opus-5:1",
            "id": "model-profile:claude-opus-5",
            "version": 1,
            "prior_version_ref": None,
            "created_at": (NOW - timedelta(days=33)).isoformat(
                timespec="microseconds"),
            "availability": {
                "state": "available",
                "checked_at": (NOW - timedelta(days=33)).isoformat(
                    timespec="microseconds"),
                "valid_until": (NOW - timedelta(days=32)).isoformat(
                    timespec="microseconds"),
            },
        })

    def test_an_expired_id_is_refused_and_the_live_one_is_named(self) -> None:
        self._register_expired_twin()
        with self.assertRaises(FallbackChainError) as raised:
            validate_selection(
                self.router, purpose="plan", mode="explicit",
                chain=["profile:deepseek-v4-flash", "model-profile:claude-opus-5"],
                now=NOW,
            )
        message = str(raised.exception)
        self.assertIn("已过期", message)
        self.assertIn("profile:claude-opus-5", message)

    def test_an_id_nobody_holds_is_refused_in_the_owner_s_language(self) -> None:
        with self.assertRaises(FallbackChainError) as raised:
            validate_selection(self.router, purpose="plan", mode="explicit",
                               chain=["profile:no-such-model"], now=NOW)
        self.assertIn("没有 profile:no-such-model 的模型档案", str(raised.exception))

    def test_a_chain_of_live_profiles_is_accepted_unchanged(self) -> None:
        self._register_expired_twin()
        checked = validate_selection(
            self.router, purpose="plan", mode="explicit",
            chain=["profile:deepseek-v4-flash", "profile:claude-opus-5"], now=NOW)
        self.assertEqual(checked["chain"],
                         ["profile:deepseek-v4-flash", "profile:claude-opus-5"])


class TransportContractTests(_RouterCase):
    """A4b: a stage whose served endpoint is asserted on cannot be saved wrong."""

    CHECKER = "research_language_check"

    def test_the_declared_contract_is_the_one_publication_actually_checks(self) -> None:
        # Kept in step by this test rather than by an import, because
        # research_language_review imports cockpit_model, which imports the
        # chain module the contract is declared in.
        from dalton_core import research_language_review as review

        self.assertEqual(required_transport(self.CHECKER), {
            "provider": review.CHECKER_PROVIDER, "model": review.CHECKER_MODEL})

    def test_the_cheapest_link_is_refused_and_the_ones_that_serve_are_named(self) -> None:
        # The live failure: the checker's config pinned a policy with no
        # override, the cheap tier's ordered preferences picked
        # profile:deepseek-v4-flash, and every publication raised "language
        # checker served an unexpected transport or model" -- 849 of 1,176.
        with self.assertRaises(FallbackChainError) as raised:
            validate_selection(
                self.router, purpose=self.CHECKER, mode="explicit",
                chain=["profile:deepseek-v4-flash", ANTIGRAVITY], now=NOW)
        message = str(raised.exception)
        self.assertIn("antigravity-cli-gateway/gemini-3.8-flash", message)
        self.assertIn("链首不能是 profile:deepseek-v4-flash", message)
        self.assertIn(ANTIGRAVITY, message)

    def test_the_contracted_endpoint_at_the_head_is_accepted(self) -> None:
        checked = validate_selection(
            self.router, purpose=self.CHECKER, mode="explicit",
            chain=[ANTIGRAVITY, "profile:deepseek-v4-flash"], now=NOW)
        self.assertEqual(checked["chain"][0], ANTIGRAVITY)

    def test_a_stage_with_no_contract_is_left_alone(self) -> None:
        checked = validate_selection(
            self.router, purpose="plan", mode="explicit",
            chain=["profile:deepseek-v4-flash"], now=NOW)
        self.assertEqual(checked["chain"], ["profile:deepseek-v4-flash"])

    def test_the_catalog_lookup_composes_the_identity_the_worker_reads_back(self) -> None:
        profiles = {item["id"]: item for item in self.router.latest_profiles()}
        self.assertEqual(profile_transport(profiles[ANTIGRAVITY]), {
            "provider": "antigravity-cli-gateway",
            "model": "antigravity-cli-gateway/gemini-3.8-flash"})
        serving = profiles_serving_transport(
            profiles, required_transport(self.CHECKER))
        self.assertIn(ANTIGRAVITY, serving)
        self.assertNotIn("profile:deepseek-v4-flash", serving)

    def test_a_tier_save_drops_preferences_but_never_a_contract_pin(self) -> None:
        # A tier edit means "all of this tier's stages follow the new chain",
        # which is right for a preference and wrong for a contract: dropping
        # the checker's pin would send every publication straight back to
        # "language checker served an unexpected transport or model" on the
        # next call, silently.
        from dalton_core.model_selection import publish_selection, publish_tier_selection
        from dalton_core.research_planner_setup import ensure_planner_policy

        policy = ensure_planner_policy(
            self.router, tier="cheap", now=NOW,
            policy_id="model-routing-policy:wpa-contract",
        )["policy_version_ref"]
        policy = publish_selection(
            self.router, policy_version_ref=policy, purpose=self.CHECKER,
            mode="explicit", chain=[ANTIGRAVITY], actor_ref="human:owner", now=NOW,
        )["policy_version_ref"]
        policy = publish_selection(
            self.router, policy_version_ref=policy, purpose="claim_index",
            mode="explicit", chain=["profile:deepseek-v4-flash"],
            actor_ref="human:owner", now=NOW,
        )["policy_version_ref"]

        saved = publish_tier_selection(
            self.router, policy_version_ref=policy, tier="cheap", mode="explicit",
            chain=["profile:deepseek-v4-flash", "profile:zai-glm-5-3-flash"],
            actor_ref="human:owner", now=NOW,
        )
        overrides = self.router.get_policy(
            saved["policy_version_ref"]).get("purpose_overrides") or {}
        self.assertEqual(overrides[self.CHECKER]["chain"], [ANTIGRAVITY])
        self.assertNotIn("claim_index", overrides)

    def test_two_callers_cannot_require_two_endpoints_for_one_stage(self) -> None:
        from dalton_core.model_fallback_chain import _PURPOSE_TRANSPORTS

        # The registry is process-global, like every other purpose registry
        # here. Put it back, or the next test module planning a repair sees a
        # stage this one invented.
        self.addCleanup(_PURPOSE_TRANSPORTS.pop, "wpa_probe_stage", None)
        register_purpose_transport(
            "wpa_probe_stage", provider="p", model="p/m")
        register_purpose_transport(
            "wpa_probe_stage", provider="p", model="p/m")
        with self.assertRaisesRegex(FallbackChainError, "two different endpoints"):
            register_purpose_transport(
                "wpa_probe_stage", provider="p", model="p/other")


class CooldownDisplayTests(unittest.TestCase):
    """A2 display: the model page says which model is held back, and why."""

    COOLING = {
        "profile_id": ASTRA, "reason": "provider_rate_limited",
        "until": "2026-09-16T06:30:00+00:00", "streak": 2,
    }

    def test_one_line_names_the_time_the_reason_and_what_it_costs(self) -> None:
        note = cooldown_note(self.COOLING)
        self.assertIn("供应商冷却中", note)
        self.assertIn("至 06:30（UTC）", note)
        self.assertIn("连续限流（HTTP 429）", note)
        self.assertIn("第 2 次", note)
        self.assertIn("不会为它预留预算", note)
        self.assertIsNone(cooldown_note(None))
        self.assertIsNone(cooldown_note({}))

    def test_an_unreadable_or_unknown_field_degrades_instead_of_failing(self) -> None:
        note = cooldown_note({"reason": "something_new", "until": "not a time"})
        self.assertIn("供应商冷却中", note)
        self.assertNotIn("至 ", note)
        self.assertIn("供应商连续失败", note)

    def test_it_folds_into_the_note_a_link_already_carried(self) -> None:
        self.assertEqual(chain_link_note(None, None), None)
        self.assertEqual(chain_link_note("未定价：只能当最后的回退", None),
                         "未定价：只能当最后的回退")
        folded = chain_link_note("未定价：只能当最后的回退", self.COOLING)
        self.assertTrue(folded.startswith("未定价：只能当最后的回退；"))
        self.assertIn("供应商冷却中", folded)

    def test_the_index_takes_either_shape_the_overview_hands_it(self) -> None:
        self.assertEqual(cooldown_index({"active": [self.COOLING]}),
                         {ASTRA: self.COOLING})
        self.assertEqual(cooldown_index([self.COOLING]), {ASTRA: self.COOLING})
        self.assertEqual(cooldown_index(None), {})
        self.assertEqual(cooldown_index({"active": "nonsense"}), {})

    def test_the_row_says_what_picks_up_the_work_or_that_nothing_does(self) -> None:
        chain = [{"profile_id": ASTRA}, {"profile_id": FABLE}]
        index = {ASTRA: self.COOLING}
        note = purpose_cooldown_note(chain, index,
                                     display_name=lambda item: item.split(":")[1])
        self.assertIn("gpt-6-astra 正在供应商冷却中", note)
        self.assertIn("暂时由 claude-fable-5-1 承接", note)

        everything = {ASTRA: self.COOLING,
                      FABLE: {**self.COOLING, "profile_id": FABLE}}
        dark = purpose_cooldown_note(chain, everything)
        self.assertIn("这一环的模型都在供应商冷却中", dark)
        self.assertIn("冷却结束后会自动恢复", dark)
        self.assertIsNone(purpose_cooldown_note(chain, {}))
        self.assertIsNone(purpose_cooldown_note([], index))


if __name__ == "__main__":
    unittest.main()
