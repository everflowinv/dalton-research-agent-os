"""P14-M: chains fall back on the provider failing, never on it disagreeing."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.contracts import WorkOrder
from dalton_core.cockpit_model import purposes, register_purpose
from dalton_core.model_fallback_chain import (
    FALLBACK_FAILURES,
    HALTING_FAILURES,
    TIERS,
    FallbackChainError,
    execute_chain,
    fallback_chains,
    classify_model_failure,
    may_fall_back,
    purpose_tiers,
    register_purpose_tier,
    routing_overview,
    served_family,
    tier_chain,
    tier_for,
    unmapped_purposes,
)
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from tests.test_openclaw_catalog_reconcile import _config


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


class FakeBroker:
    """The offline broker: it answers, or it fails in a named way.

    ``script`` maps a profile id to what happens when the chain reaches it, so a
    test says "OpenAI is down and Anthropic is up" and nothing has to be mocked
    at the socket.
    """

    def __init__(self, script: dict[str, dict]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def __call__(self, route, profile):
        self.calls.append((profile["id"], route["id"]))
        return self.script.get(
            profile["id"], {"outcome": "served", "value": f"answered by {profile['id']}"}
        )


def _work(work_id: str, capability: str = "research") -> WorkOrder:
    moment = NOW.isoformat(timespec="microseconds")
    return WorkOrder(
        schema_version="0.1",
        id=work_id,
        created_at=moment,
        updated_at=moment,
        question="what should the research work on next?",
        requested_capabilities=(capability,),
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


class TierMapTests(unittest.TestCase):
    def test_every_seeded_purpose_has_a_tier_and_an_unknown_one_is_refused(self) -> None:
        for purpose in ("ask", "goal", "steer", "draft", "plan", "model_spec"):
            self.assertEqual(tier_for(purpose), "brain")
        with self.assertRaisesRegex(FallbackChainError, "no model tier"):
            tier_for("no_such_purpose")

    def test_a_purpose_registered_without_a_tier_is_reported_as_unmapped(self) -> None:
        register_purpose("p14m_untiered_probe")
        self.addCleanup(purposes)
        self.assertIn("p14m_untiered_probe", unmapped_purposes())
        with self.assertRaisesRegex(FallbackChainError, "no model tier"):
            tier_for("p14m_untiered_probe")

    def test_registering_a_tier_registers_the_purpose_and_refuses_a_remap(self) -> None:
        register_purpose_tier("p14m_probe_lane", "cheap")
        self.assertIn("p14m_probe_lane", purposes())
        self.assertEqual(purpose_tiers()["p14m_probe_lane"], "cheap")
        register_purpose_tier("p14m_probe_lane", "cheap")
        with self.assertRaisesRegex(FallbackChainError, "already mapped"):
            register_purpose_tier("p14m_probe_lane", "brain")
        with self.assertRaisesRegex(FallbackChainError, "unknown model tier"):
            register_purpose_tier("p14m_probe_lane_two", "expensive")

    def test_the_chains_are_the_ones_the_owner_named(self) -> None:
        self.assertEqual(TIERS, ("brain", "cheap", "verifier"))
        self.assertEqual(
            tier_chain("brain"), ("profile:gpt-6-astra", "profile:claude-fable-5-1")
        )
        self.assertEqual(
            tier_chain("cheap"),
            (
                "profile:deepseek-v4-flash",
                "profile:zai-glm-5-3-flash",
                "profile:gemini-3-5-flash-lite",
            ),
        )
        self.assertEqual(tier_chain("verifier")[0], "profile:claude-fable-5-1")
        self.assertEqual(set(fallback_chains()), {"tiers"})
        self.assertEqual(set(fallback_chains()["tiers"]), set(TIERS))

    def test_the_map_is_code_and_registering_a_tier_pins_no_policy(self) -> None:
        # Item 5 of the review: the pinned policy carries chains only. If it
        # carried the purpose map, every lane that registered a tier would
        # append a routing-policy version to every pinned policy, with a new
        # hash, for a change routing never reads.
        before = fallback_chains()
        register_purpose_tier("p14m_map_probe", "cheap")
        self.assertEqual(fallback_chains(), before)
        self.assertNotIn("purpose_tiers", fallback_chains())

    def test_every_wave_one_purpose_has_a_tier(self) -> None:
        # The lanes register their purposes at import; the map has to keep up.
        import dalton_core.claim_index_tagging  # noqa: F401
        import dalton_core.research_quality_score  # noqa: F401
        from dalton_core.lane_registry import load_lanes

        load_lanes()
        self.assertEqual(tier_for("claim_index"), "cheap")
        self.assertEqual(tier_for("quality"), "cheap")

    def test_the_classifier_names_every_failure_it_is_shown(self) -> None:
        from dalton_core.openclaw_model_adapter import (
            BrokerBudgetExceeded,
            BrokerConnectionError,
            BrokerIdempotencyConflict,
            BrokerProtocolError,
            BrokerTimeout,
            ModelAdmissionError,
        )

        cases = [
            (BrokerTimeout("late"), "transport_failure"),
            (BrokerConnectionError("no socket"), "transport_failure"),
            (TimeoutError("late"), "transport_failure"),
            (ConnectionResetError("reset"), "transport_failure"),
            (BrokerProtocolError("garbled"), "provider_failure"),
            (BrokerBudgetExceeded("too dear"), "budget_refused"),
            (BrokerIdempotencyConflict("already bound"), "contract_violation"),
            (ModelAdmissionError("bad route"), "contract_violation"),
            ({"code": "MODEL_UNAVAILABLE"}, "model_unavailable"),
            ({"code": "REQUIRED_CONTROLS_UNAVAILABLE"}, "contract_violation"),
            ({"code": "PROVIDER_OVERLOADED"}, "model_unavailable"),
            ({"code": "RATE_LIMITED"}, "provider_failure"),
            ({"code": "UPSTREAM_TIMEOUT"}, "transport_failure"),
            ({"code": "PROVIDER_ERROR"}, "provider_failure"),
            ({"code": "CONTENT_REFUSAL"}, "content_refusal"),
            ({"code": "SAFETY_BLOCKED"}, "content_refusal"),
            ({"code": "PROVIDER_BUDGET_EXCEEDED"}, "budget_refused"),
            ({"code": "INVALID_REQUEST"}, "contract_violation"),
            ({"code": "SOMETHING_NOBODY_HAS_SEEN"}, "unclassified_failure"),
            ({"code": "HTTP_503"}, "provider_failure"),
            ({"code": "HTTP_429"}, "provider_failure"),
            ({"code": "HTTP_418"}, "contract_violation"),
            (429, "provider_failure"),
            (500, "provider_failure"),
            (503, "provider_failure"),
            (400, "contract_violation"),
            (403, "contract_violation"),
            (None, "unclassified_failure"),
            ({}, "unclassified_failure"),
        ]
        for failure, expected in cases:
            with self.subTest(failure=failure):
                self.assertEqual(classify_model_failure(failure), expected)

    def test_the_classifier_never_raises_and_never_invents_a_fallback(self) -> None:
        for failure in (object(), b"bytes", 3.5, ["list"], RuntimeError("boom")):
            outcome = classify_model_failure(failure)
            self.assertIn(outcome, FALLBACK_FAILURES | HALTING_FAILURES)
        # Anything the classifier cannot name halts rather than shopping around.
        self.assertFalse(may_fall_back(classify_model_failure(object())))

    def test_only_a_provider_failing_lets_the_chain_move_on(self) -> None:
        for failure in FALLBACK_FAILURES:
            self.assertTrue(may_fall_back(failure))
        for failure in HALTING_FAILURES:
            self.assertFalse(may_fall_back(failure))
        with self.assertRaisesRegex(FallbackChainError, "unclassified"):
            may_fall_back("something_went_wrong")


class ChainExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.router = ModelRouter(Path(self.directory.name) / "router.sqlite")
        self.addCleanup(self.router.close)
        sync_openclaw_model_catalog(self.router, _config(), checked_at=NOW)
        self.policies = {}
        for tier in TIERS:
            self.policies[tier] = ensure_planner_policy(
                self.router,
                tier=tier,
                now=NOW,
                policy_id=f"model-routing-policy:p14m-test-{tier}",
            )["policy_version_ref"]

    def _run(self, tier, broker, *, purpose="plan", capability="research",
             work_id=None, producer_decision_ref=None, producer_decision_refs=(),
             admit=None):
        work = _work(work_id or f"work:p14m-{tier}", capability=capability)
        return execute_chain(
            self.router,
            work,
            purpose=purpose,
            tier=tier,
            capability=capability,
            attempt_number=1,
            policy_version_ref=self.policies[tier],
            credential_slot_refs=credential_slots_for(
                self.router, list(tier_chain(tier))
            ),
            required_modalities=["text"],
            required_context_tokens=2_000,
            estimated_input_tokens=1_000,
            estimated_output_tokens=500,
            idempotency_prefix=f"p14m:{work.id}",
            call=broker,
            admit=admit,
            producer_decision_ref=producer_decision_ref,
            producer_decision_refs=producer_decision_refs,
        )

    def test_the_first_link_serves_and_the_chain_records_which_one(self) -> None:
        broker = FakeBroker({})
        result = self._run("brain", broker)
        self.assertEqual(result["status"], "served")
        self.assertEqual(result["profile_id"], "profile:gpt-6-astra")
        self.assertEqual(result["chain_position"], 1)
        self.assertEqual(len(broker.calls), 1)
        links = self.router.chain_links(work_order_id="work:p14m-brain")
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0]["served"])
        self.assertIsNone(links[0]["skip_reason"])
        self.assertEqual(links[0]["tier"], "brain")
        self.assertEqual(links[0]["decision_id"], result["route_decision_ref"])

    def test_a_provider_failure_moves_to_the_next_link_as_a_switch(self) -> None:
        broker = FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        })
        result = self._run("brain", broker)
        self.assertEqual(result["status"], "served")
        self.assertEqual(result["profile_id"], "profile:claude-fable-5-1")
        self.assertEqual(result["chain_position"], 2)
        self.assertEqual(
            [profile for profile, _ in broker.calls],
            ["profile:gpt-6-astra", "profile:claude-fable-5-1"],
        )
        links = self.router.chain_links(work_order_id="work:p14m-brain")
        self.assertEqual(
            [(link["chain_position"], link["served"], link["skip_reason"]) for link in links],
            [(1, False, "provider_failure"), (2, True, None)],
        )
        decisions = self.router.list_decisions(work_order_id="work:p14m-brain")
        self.assertEqual([item["decision_kind"] for item in decisions], ["initial", "switch"])
        self.assertEqual(decisions[1]["previous_decision_ref"], decisions[0]["id"])
        self.assertEqual(decisions[1]["attempt_number"], decisions[0]["attempt_number"])
        # The switch decision's own snapshot says why link one was not reused.
        skipped = next(
            item
            for item in decisions[1]["candidate_snapshot"]
            if item["profile_version_ref"] == decisions[0]["selected_profile_version_ref"]
        )
        self.assertIn("already_tried_in_switch_chain", skipped["rejection_reasons"])

    def test_a_content_refusal_stops_the_chain_where_it_stands(self) -> None:
        broker = FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "content_refusal"
            }
        })
        result = self._run("brain", broker)
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["reason"], "content_refusal")
        self.assertIsNone(result["served"])
        self.assertEqual(len(broker.calls), 1)
        links = self.router.chain_links(work_order_id="work:p14m-brain")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["skip_reason"], "content_refusal")

    def test_an_unclassified_failure_fails_closed(self) -> None:
        broker = FakeBroker({
            "profile:gpt-6-astra": {"outcome": "failed", "failure_class": "weird"}
        })
        with self.assertRaisesRegex(FallbackChainError, "unclassified"):
            self._run("brain", broker)

    def test_the_chain_order_beats_the_policys_cheapest_first_preference(self) -> None:
        # glm-5.3-flash is a fifth of deepseek-v4-flash's price and second in
        # the chain. A cost-sorted policy would take it first; the chain says no.
        broker = FakeBroker({})
        result = self._run("cheap", broker, purpose="p14m_cheap_probe")
        self.assertEqual(result["profile_id"], "profile:deepseek-v4-flash")

    def test_a_fallback_is_admitted_against_the_model_that_actually_runs(self) -> None:
        admitted: list[int] = []

        def admit(route, profile, micros):
            admitted.append(micros)
            return {"status": "admitted", "admission_id": f"admission:{profile['id']}"}

        broker = FakeBroker({
            "profile:deepseek-v4-flash": {
                "outcome": "failed", "failure_class": "model_unavailable"
            }
        })
        result = self._run("cheap", broker, purpose="p14m_cheap_probe", admit=admit)
        self.assertEqual(result["profile_id"], "profile:zai-glm-5-3-flash")
        # deepseek: (0.22*1000 + 0.66*500)/1e6 USD; glm-5.3-flash: (0.075*1000 +
        # 0.25*500)/1e6. Each admission charges its own link's rate card.
        self.assertEqual(admitted, [550, 200])

    def test_a_budget_refusal_halts_rather_than_shopping_the_call_around(self) -> None:
        broker = FakeBroker({})
        result = self._run(
            "cheap", broker, purpose="p14m_cheap_probe",
            admit=lambda route, profile, micros: None,
        )
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["reason"], "budget_refused")
        self.assertEqual(broker.calls, [])
        links = self.router.chain_links()
        self.assertEqual([link["skip_reason"] for link in links], ["budget_refused"])

    def test_the_verifier_chain_never_starts_on_the_producers_family(self) -> None:
        producer = self._run("brain", FakeBroker({}))
        self.assertEqual(producer["profile_id"], "profile:gpt-6-astra")
        result = self._run(
            "verifier", FakeBroker({}), purpose="p14m_verify_probe",
            capability="verify", work_id="work:p14m-verify-openai",
            producer_decision_ref=producer["route_decision_ref"],
        )
        self.assertEqual(result["status"], "served")
        self.assertEqual(result["profile_id"], "profile:claude-fable-5-1")
        self.assertNotEqual(result["profile"]["family"], producer["profile"]["family"])

    def test_the_producers_family_is_read_from_its_decision_not_the_caller(self) -> None:
        # The producer fell back to Anthropic. A verifier told "the producer was
        # OpenAI" would happily pick Anthropic and pass its own work.
        producer = self._run("brain", FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }))
        self.assertEqual(producer["profile_id"], "profile:claude-fable-5-1")
        self.assertEqual(
            served_family(self.router, producer["route_decision_ref"]),
            "anthropic-claude-5",
        )
        result = self._run(
            "verifier", FakeBroker({}), purpose="p14m_verify_probe",
            capability="verify", work_id="work:p14m-verify-fellback",
            producer_decision_ref=producer["route_decision_ref"],
        )
        self.assertEqual(result["profile_id"], "profile:zai-glm-5-3")

    def test_a_rejected_producer_decision_cannot_stand_in_for_a_producer(self) -> None:
        rejected = self._run(
            "verifier", FakeBroker({}), purpose="p14m_verify_probe",
            capability="verify", work_id="work:p14m-verify-noproducer",
        )
        self.assertEqual(rejected["status"], "exhausted")
        with self.assertRaisesRegex(FallbackChainError, "produced nothing"):
            self._run(
                "verifier", FakeBroker({}), purpose="p14m_verify_probe",
                capability="verify", work_id="work:p14m-verify-onrejected",
                producer_decision_ref=rejected["route_decision_ref"],
            )

    def test_an_anthropic_producer_pushes_the_verifier_onto_the_next_family(self) -> None:
        producer = self._run("brain", FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }))
        result = self._run(
            "verifier", FakeBroker({}), purpose="p14m_verify_probe",
            capability="verify", work_id="work:p14m-verify-anthropic",
            producer_decision_ref=producer["route_decision_ref"],
        )
        self.assertEqual(result["status"], "served")
        self.assertEqual(result["profile_id"], "profile:zai-glm-5-3")
        self.assertEqual(result["chain_position"], 2)
        # One decision, not two: the router refuses the non-independent link in
        # the same snapshot that selects the next one, and says why.
        decisions = self.router.list_decisions(work_order_id="work:p14m-verify-anthropic")
        self.assertEqual(len(decisions), 1)
        fable = next(
            item
            for item in decisions[0]["candidate_snapshot"]
            if item["profile_version_ref"].startswith(
                "model-profile-version:broker-claude-fable-5-1"
            )
        )
        self.assertIn("model_family_not_independent", fable["rejection_reasons"])

    def test_multiple_producer_families_are_skipped_before_charge_or_call(self) -> None:
        openai = self._run("brain", FakeBroker({}), work_id="work:producer-openai")
        anthropic = self._run("brain", FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }), work_id="work:producer-anthropic")
        called = FakeBroker({})
        admitted = []
        result = self._run(
            "verifier", called, purpose="p14m_verify_probe", capability="verify",
            work_id="work:verify-multiple",
            producer_decision_refs=(openai["route_decision_ref"],
                                    anthropic["route_decision_ref"]),
            admit=lambda route, profile, micros: admitted.append(profile["id"]) or
            {"status": "admitted"},
        )
        self.assertEqual(result["profile_id"], "profile:zai-glm-5-3")
        self.assertEqual(called.calls[0][0], "profile:zai-glm-5-3")
        self.assertEqual(admitted, ["profile:zai-glm-5-3"])

    def test_no_independent_verifier_means_no_admission_or_model_call(self) -> None:
        anthropic = self._run("brain", FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }), work_id="work:producer-anthropic-all")
        glm = self._run("cheap", FakeBroker({
            "profile:deepseek-v4-flash": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }), purpose="p14m_cheap_probe", work_id="work:producer-glm")
        gemini = self._run("cheap", FakeBroker({
            "profile:deepseek-v4-flash": {
                "outcome": "failed", "failure_class": "provider_failure"
            },
            "profile:zai-glm-5-3-flash": {
                "outcome": "failed", "failure_class": "provider_failure"
            },
        }), purpose="p14m_cheap_probe", work_id="work:producer-gemini")
        called = FakeBroker({})
        admitted = []
        result = self._run(
            "verifier", called, purpose="p14m_verify_probe", capability="verify",
            work_id="work:verify-none",
            producer_decision_refs=(anthropic["route_decision_ref"],
                                    glm["route_decision_ref"],
                                    gemini["route_decision_ref"]),
            admit=lambda route, profile, micros: admitted.append(profile["id"]),
        )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "verifier_not_independent")
        self.assertEqual(admitted, [])
        self.assertEqual(called.calls, [])

    def test_a_retired_link_is_skipped_and_the_chain_carries_on(self) -> None:
        dropped = _config()
        entries = dropped["plugins"]["entries"]["dalton-openclaw-model-broker"]["config"]
        entries["profiles"] = [
            profile for profile in entries["profiles"]
            if profile["id"] != "profile:gpt-6-astra"
        ]
        sync_openclaw_model_catalog(self.router, dropped, checked_at=NOW)
        result = self._run("brain", FakeBroker({}))
        self.assertEqual(result["profile_id"], "profile:claude-fable-5-1")
        self.assertEqual(result["chain_position"], 2)
        decisions = self.router.list_decisions(work_order_id="work:p14m-brain")
        astra = next(
            item
            for item in decisions[0]["candidate_snapshot"]
            if item["profile_version_ref"].startswith(
                "model-profile-version:broker-gpt-6-astra"
            )
        )
        self.assertIn("profile_retired", astra["rejection_reasons"])

    def test_chain_links_are_append_only_and_replay_identically(self) -> None:
        broker = FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "transport_failure"
            }
        })
        self._run("brain", broker)
        links = self.router.chain_links()
        with self.assertRaises(Exception):
            self.router.connection.execute(
                "UPDATE model_route_chain_links SET served=0"
            )
        with self.assertRaises(Exception):
            self.router.connection.execute("DELETE FROM model_route_chain_links")
        self.assertEqual(self.router.chain_links(), links)

    def test_a_policy_that_does_not_declare_the_tier_refuses_every_candidate(self) -> None:
        untiered = ensure_planner_policy(
            self.router,
            profile_ids=["profile:gpt-6-astra"],
            now=NOW,
            policy_id="model-routing-policy:p14m-test-untiered",
        )["policy_version_ref"]
        decision = self.router.route(
            _work("work:p14m-untiered"),
            attempt_number=1,
            capability="research",
            policy_version_ref=untiered,
            credential_slot_refs=["credential-slot:openclaw:openai"],
            required_modalities=["text"],
            required_context_tokens=2_000,
            estimated_input_tokens=1_000,
            estimated_output_tokens=500,
            idempotency_key="p14m-untiered:1",
            tier="brain",
        )["decision"]
        self.assertEqual(decision["outcome"], "rejected")
        self.assertIn("tier_not_declared_by_policy", decision["rejection_reasons"])

    def test_the_overview_reads_tiers_chains_last_served_and_catalog_sync(self) -> None:
        self._run("brain", FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }))
        overview = routing_overview(
            self.router, openclaw_config=_config(), checked_at=NOW
        )
        self.assertEqual(overview["purpose_tiers"]["plan"], "brain")
        brain = overview["tiers"]["brain"]
        self.assertEqual(
            [link["profile_id"] for link in brain["chain"]],
            ["profile:gpt-6-astra", "profile:claude-fable-5-1"],
        )
        self.assertTrue(all(link["registered"] for link in brain["chain"]))
        self.assertEqual(brain["last_served"]["profile_id"], "profile:claude-fable-5-1")
        self.assertEqual(brain["last_served"]["chain_position"], 2)
        self.assertEqual(
            [item["skip_reason"] for item in brain["skipped_since_last_served"]],
            ["provider_failure"],
        )
        self.assertTrue(overview["catalog"]["catalog_in_sync"])
        self.assertEqual(overview["catalog"]["not_in_broker_profile_ids"], [])

    def test_replaying_a_chain_returns_the_links_it_already_recorded(self) -> None:
        # The blocker: the link id used to be hashed over the clock, so a replay
        # derived a different id, missed the duplicate check and hit the schema's
        # UNIQUE constraint as a raw sqlite3.IntegrityError.
        broker = FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        })
        first = self._run("brain", broker)
        links = self.router.chain_links(work_order_id="work:p14m-brain")
        decisions = self.router.list_decisions(work_order_id="work:p14m-brain")

        replay = self._run("brain", FakeBroker({
            "profile:gpt-6-astra": {
                "outcome": "failed", "failure_class": "provider_failure"
            }
        }))
        self.assertEqual(replay["status"], "served")
        self.assertEqual(replay["profile_id"], first["profile_id"])
        self.assertEqual(replay["route_decision_ref"], first["route_decision_ref"])
        # Nothing was appended: the same links and the same decisions.
        self.assertEqual(self.router.chain_links(work_order_id="work:p14m-brain"), links)
        self.assertEqual(self.router.list_decisions(work_order_id="work:p14m-brain"), decisions)

    def test_recording_the_same_link_twice_is_a_duplicate_not_a_crash(self) -> None:
        result = self._run("brain", FakeBroker({}))
        link = result["served"]
        again = self.router.record_chain_link(
            work_order_id="work:p14m-brain", capability="research",
            attempt_number=1, purpose="plan", tier="brain", chain_position=1,
            profile_id=link["profile_id"], decision_id=link["decision_id"],
            policy_version_ref=link["policy_version_ref"], served=True,
        )
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["link"], link)

    def test_the_same_chain_position_recorded_differently_is_a_conflict(self) -> None:
        result = self._run("brain", FakeBroker({}))
        link = result["served"]
        clash = self.router.record_chain_link(
            work_order_id="work:p14m-brain", capability="research",
            attempt_number=1, purpose="plan", tier="brain", chain_position=1,
            profile_id=link["profile_id"], decision_id=link["decision_id"],
            policy_version_ref=link["policy_version_ref"], served=False,
            skip_reason="provider_failure",
        )
        self.assertEqual(clash["status"], "conflict")
        self.assertEqual(len(self.router.chain_links()), 1)

    def test_an_admission_that_does_not_say_admitted_refuses_the_call(self) -> None:
        for admission in ({}, {"status": "reserved"}, {"admission_id": "a"}, True, "yes"):
            with self.subTest(admission=admission):
                broker = FakeBroker({})
                outcome = self._run(
                    "cheap", broker, purpose="p14m_cheap_probe",
                    work_id=f"work:p14m-admit-{abs(hash(str(admission)))}",
                    admit=lambda route, profile, micros, value=admission: value,
                )
                self.assertEqual(outcome["status"], "halted")
                self.assertEqual(outcome["reason"], "budget_refused")
                self.assertEqual(broker.calls, [])

    def test_the_pinned_policy_carries_the_chain_it_runs(self) -> None:
        policy = self.router.get_policy(self.policies["brain"])
        self.assertEqual(
            policy["fallback_chains"]["tiers"]["brain"],
            ["profile:gpt-6-astra", "profile:claude-fable-5-1"],
        )
        self.assertEqual(
            policy["filters"]["allowed_profile_ids"],
            ["profile:gpt-6-astra", "profile:claude-fable-5-1"],
        )
        # The verifier policy is the one that needs the independence filter on.
        verifier = self.router.get_policy(self.policies["verifier"])
        self.assertEqual(
            verifier["filters"]["family_independence_capabilities"],
            ["verify", "adjudicate"],
        )
        # Re-running the setup with the same tier appends nothing.
        again = ensure_planner_policy(
            self.router, tier="brain", now=NOW,
            policy_id="model-routing-policy:p14m-test-brain",
        )
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["policy_version_ref"], self.policies["brain"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
