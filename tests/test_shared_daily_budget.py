"""One day's money for the whole host.

The thing being pinned down is the one sentence the owner said: money spent in
*one* research environment has to count against *every* environment's day.
Four environments each obeying a $500 cap were between them obeying nothing,
and every page said so truthfully, which is what made it hard to see.

So the load-bearing test is the third one: two environments, a ledger in each,
spend in the first, and the second refuses. Around it sit the things that have
to be true for that to be safe -- a closed, hashed policy file; an unbound
environment behaving exactly as it did before; an unreadable environment
counting as zero and being named rather than stopping the host.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.shared_daily_budget import (
    BINDING_FILENAME,
    SharedDailyBudgetError,
    binding_wire,
    cross_environment_spend,
    ledger_day_spend,
    load_shared_daily_budget_policy,
    policy_wire,
    resolve_shared_gate,
    shared_day_caps,
    shared_day_view,
    validate_shared_daily_budget_policy,
)
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetStore,
    ThesisImpactBudgetValidationError,
    ThesisImpactDayBudgetExceeded,
)

DAY = "2026-09-17"
NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
MISSION = {
    "mission_ref": "coverage-mission:us-it-services",
    "mission_version_ref": "coverage-mission-version:us-it-services:4",
    "mission_version_hash": "f" * 64,
    "max_daily_paid_calls": 100,
    "max_daily_cost_micros": 100_000_000,
}


def _policy(path: Path, *, cost_usd: float = 500.0, calls: int = 100000) -> dict:
    wire = policy_wire(
        max_daily_cost_usd=cost_usd, max_daily_paid_calls=calls,
        max_alphaengine_calls_24h=130, revision=1, prior_hash=None,
        updated_at=NOW.isoformat(timespec="microseconds"), actor_ref="human:owner")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wire, sort_keys=True, indent=2), encoding="utf-8")
    return wire


class PolicyFileTests(unittest.TestCase):
    """A number that binds every environment has to be a closed, signed record."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_a_written_policy_reads_back(self) -> None:
        path = self.root / "shared-daily-budget.json"
        written = _policy(path)
        self.assertEqual(load_shared_daily_budget_policy(path), written)
        self.assertEqual(shared_day_caps(written),
                         {"max_daily_paid_calls": 100000,
                          "max_daily_cost_micros": 500_000_000})

    def test_an_extra_key_or_a_wrong_hash_is_refused(self) -> None:
        wire = dict(_policy(self.root / "p.json"))
        with self.assertRaises(SharedDailyBudgetError):
            validate_shared_daily_budget_policy({**wire, "note": "hello"})
        with self.assertRaises(SharedDailyBudgetError):
            validate_shared_daily_budget_policy({**wire, "max_daily_cost_usd": 9000.0})

    def test_only_a_person_may_sign_it(self) -> None:
        wire = {
            "schema_version": "dalton-shared-daily-budget-policy-0.1",
            "max_daily_cost_usd": 500.0, "max_daily_paid_calls": 10,
            "max_alphaengine_calls_24h": 130, "revision": 1, "prior_hash": None,
            "updated_at": NOW.isoformat(), "actor_ref": "agent:controller",
        }
        wire["content_hash"] = content_hash(dict(wire))
        with self.assertRaisesRegex(SharedDailyBudgetError, "actor_ref"):
            validate_shared_daily_budget_policy(wire)

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(SharedDailyBudgetError):
            load_shared_daily_budget_policy("shared-daily-budget.json")


class TwoEnvironmentCase(unittest.TestCase):
    """Two environments on one host, each with its own day ledger."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.host = self.root / "host"
        (self.host / "workspaces").mkdir(parents=True)
        self.policy_path = self.host / "connections" / "shared-daily-budget.json"
        self.states: dict[str, Path] = {}
        for slug in ("ws-one", "ws-two"):
            state = self.host / "workspaces" / slug / "state" / "dalton-core"
            state.mkdir(parents=True)
            (self.host / "workspaces" / slug / "workspace.json").write_text(
                json.dumps({"slug": slug, "workspace_id": slug,
                            "state_dir": str(state)}), encoding="utf-8")
            self.states[slug] = state
        self.manager = self.host / "manager.json"
        self.manager.write_text(json.dumps({"host_root": str(self.host)}),
                                encoding="utf-8")

    def bind(self, *, cost_usd: float = 500.0, calls: int = 100000) -> dict:
        policy = _policy(self.policy_path, cost_usd=cost_usd, calls=calls)
        for slug, state in self.states.items():
            binding = binding_wire(
                policy_path=str(self.policy_path),
                policy_hash=policy["content_hash"],
                manager_config_path=str(self.manager),
                environment_id=slug)
            (state / BINDING_FILENAME).write_text(
                json.dumps(binding, sort_keys=True, indent=2), encoding="utf-8")
        return policy

    def ledger(self, slug: str) -> ThesisImpactBudgetStore:
        store = ThesisImpactBudgetStore(
            self.states[slug] / "thesis-impact-budget.sqlite", clock=lambda: NOW)
        self.addCleanup(store.close)
        store.register_policy(policy_version_id=f"budget-policy:{slug}:1",
                              day_cap_micros=1_000_000_000)
        return store

    def spend(self, store: ThesisImpactBudgetStore, slug: str, *, micros: int,
              work: str) -> dict:
        return store.admit(
            policy_version_id=f"budget-policy:{slug}:1", day=DAY,
            work_order_ref=work, attempt_number=1, phase="assessment",
            route_decision_ref=f"route-decision:{work}:1",
            reserved_micros=micros, mission_binding=dict(MISSION))


class SharedGateTests(TwoEnvironmentCase):
    def test_spend_in_one_environment_refuses_the_call_in_the_other(self) -> None:
        # $10 of the host's day, shared, and one environment takes $9 of it.
        self.bind(cost_usd=10.0)
        first = self.ledger("ws-one")
        self.spend(first, "ws-one", micros=9_000_000, work="work-order:one")
        second = self.ledger("ws-two")
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as raised:
            self.spend(second, "ws-two", micros=2_000_000, work="work-order:two")
        self.assertEqual(raised.exception.rejection["reason"],
                         "shared_daily_budget_exceeded")
        # The mission's own cap was nowhere near passed; only the host's was.
        self.assertLess(2_000_000, MISSION["max_daily_cost_micros"])

    def test_what_still_fits_under_the_shared_cap_is_admitted(self) -> None:
        self.bind(cost_usd=10.0)
        first = self.ledger("ws-one")
        self.spend(first, "ws-one", micros=9_000_000, work="work-order:one")
        second = self.ledger("ws-two")
        admitted = self.spend(second, "ws-two", micros=500_000, work="work-order:two")
        self.assertEqual(admitted["status"], "fresh")

    def test_the_shared_call_count_is_host_wide_too(self) -> None:
        self.bind(cost_usd=1000.0, calls=2)
        first = self.ledger("ws-one")
        self.spend(first, "ws-one", micros=1_000, work="work-order:one")
        self.spend(first, "ws-one", micros=1_000, work="work-order:two")
        second = self.ledger("ws-two")
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as raised:
            self.spend(second, "ws-two", micros=1_000, work="work-order:three")
        self.assertEqual(raised.exception.rejection["reason"],
                         "shared_daily_budget_exceeded")

    def test_an_unbound_environment_is_unchanged(self) -> None:
        # No binding file anywhere: the pre-2026-09-17 behaviour exactly.
        first = self.ledger("ws-one")
        self.spend(first, "ws-one", micros=90_000_000, work="work-order:one")
        second = self.ledger("ws-two")
        admitted = self.spend(second, "ws-two", micros=90_000, work="work-order:two")
        self.assertEqual(admitted["status"], "fresh")
        self.assertIsNone(second.shared_daily_gate())

    def test_a_malformed_binding_refuses_rather_than_quietly_lifting_the_cap(self) -> None:
        self.bind(cost_usd=10.0)
        (self.states["ws-two"] / BINDING_FILENAME).write_text("{oops", encoding="utf-8")
        second = self.ledger("ws-two")
        with self.assertRaises(ThesisImpactBudgetValidationError):
            self.spend(second, "ws-two", micros=1_000, work="work-order:two")

    def test_an_unreadable_peer_counts_as_zero_and_is_named(self) -> None:
        self.bind(cost_usd=10.0)
        # ws-one never opened a ledger, so there is no file to read.
        second = self.ledger("ws-two")
        admitted = self.spend(second, "ws-two", micros=1_000, work="work-order:two")
        self.assertEqual(admitted["status"], "fresh")
        view = shared_day_view(
            self.states["ws-two"] / "thesis-impact-budget.sqlite", DAY)
        self.assertEqual([item["environment_id"] for item in view["unavailable"]],
                         ["ws-one"])
        self.assertIn("读不到", view["note"])


class SharedViewTests(TwoEnvironmentCase):
    """What the 预算 page shows beside the mission's own numbers."""

    def test_the_view_adds_up_both_environments(self) -> None:
        self.bind(cost_usd=10.0)
        first = self.ledger("ws-one")
        self.spend(first, "ws-one", micros=3_000_000, work="work-order:one")
        second = self.ledger("ws-two")
        self.spend(second, "ws-two", micros=1_000_000, work="work-order:two")
        view = shared_day_view(
            self.states["ws-two"] / "thesis-impact-budget.sqlite", DAY)
        self.assertEqual(view["status"], "ok")
        self.assertEqual(view["cost_cap_usd"], 10.0)
        self.assertEqual(view["cost_usd"], 4.0)
        self.assertEqual(view["used"], 2)
        self.assertEqual(view["this_environment"]["cost_usd"], 1.0)
        self.assertEqual([item["environment_id"] for item in view["environments"]],
                         ["ws-one"])
        self.assertIn("所有研究环境", view["note"])

    def test_an_unbound_environment_shows_nothing_rather_than_a_cap_of_zero(self) -> None:
        second = self.ledger("ws-two")
        del second
        self.assertIsNone(shared_day_view(
            self.states["ws-two"] / "thesis-impact-budget.sqlite", DAY))

    def test_the_gate_lists_only_the_other_environments(self) -> None:
        self.bind()
        self.ledger("ws-one")
        self.ledger("ws-two")
        gate = resolve_shared_gate(
            self.states["ws-two"] / "thesis-impact-budget.sqlite")
        self.assertEqual([peer["environment_id"] for peer in gate["peer_ledgers"]],
                         ["ws-one"])
        self.assertEqual(gate["environment_id"], "ws-two")

    def test_a_day_is_read_read_only_and_an_open_reservation_keeps_counting(self) -> None:
        """Deliberately the same conservative window the gate uses.

        An admission that has never settled is money that may still land, so
        it keeps counting after midnight. A shared cap that forgot it would let
        the host overspend on exactly the day a lane crashed -- which is the
        rule the mission's own outer check already follows, and the display has
        to agree with the gate or the owner is reading a different number from
        the one that refused them.
        """

        self.bind()
        first = self.ledger("ws-one")
        self.spend(first, "ws-one", micros=250_000, work="work-order:one")
        path = self.states["ws-one"] / "thesis-impact-budget.sqlite"
        self.assertEqual(ledger_day_spend(path, DAY),
                         {"calls": 1, "cost_micros": 250_000})
        self.assertEqual(ledger_day_spend(path, "2026-09-18"),
                         {"calls": 1, "cost_micros": 250_000})
        # Reading it never made a WAL sidecar or a journal beside it.
        self.assertFalse((path.parent / f"{path.name}-journal").exists())

    def test_a_missing_ledger_raises_for_the_direct_reader_and_not_for_the_sum(self) -> None:
        missing = self.states["ws-one"] / "thesis-impact-budget.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            ledger_day_spend(missing, DAY)
        summed = cross_environment_spend(
            [{"environment_id": "ws-one", "name": "ws-one", "budget_db": str(missing)}],
            DAY)
        self.assertEqual(summed["calls"], 0)
        self.assertEqual([item["environment_id"] for item in summed["unavailable"]],
                         ["ws-one"])


if __name__ == "__main__":
    unittest.main()
