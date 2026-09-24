"""2026-09-24: settling the cockpit reservations the settlement bug orphaned."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from unittest.mock import patch

import tests.test_cockpit_model_fallback as fallback
from dalton_core.cockpit_reservation_recovery import (
    apply_plan,
    plan_orphan_settlements,
    purpose_of,
)
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetConflict,
    ThesisImpactBudgetStore,
)


NOW = fallback.NOW


class PlanningAdapter(fallback.ChainAdapter):
    """Plan while the attempt is really in flight: leased and admitted."""

    def __init__(self, test: "OrphanReservationTests") -> None:
        super().__init__({})
        self.test = test
        self.plans: list[dict] = []

    def execute(self, work, route, profile):
        self.plans.append(self.test._plan(now=NOW))
        self.plans.append(self.test._plan(now=NOW + timedelta(days=1)))
        return super().execute(work, route, profile)


class OrphanReservationTests(unittest.TestCase):
    _model = fallback.CockpitChainTests._model

    def setUp(self) -> None:
        fallback.CockpitChainTests.setUp(self)
        # Room in the event pool for several open reservations at once.
        self.mission["budget"] = {"max_daily_paid_calls": 50,
                                  "max_daily_cost_usd": 50.0}

    def _plan(self, *, now, **kwargs):
        return plan_orphan_settlements(
            budget_db=self.root / "budget.sqlite",
            scheduler_db=self.root / "scheduler.sqlite",
            broker_journal=self.root / "journal.json",
            now=now, **kwargs,
        )

    def _orphan(self, request_id: str) -> dict:
        """What the bug left: a completed attempt with an open admission."""

        with patch.object(ThesisImpactBudgetStore, "settle",
                          side_effect=ThesisImpactBudgetConflict("old refusal")), \
                redirect_stderr(io.StringIO()):
            return self._model(
                fallback.ChainAdapter({}), policy_version_ref=self.chain_policy,
            ).call(purpose="event_judgement", request_id=request_id,
                   prompt="judge", mission=self.mission)

    def _journal(self, records: list[dict]) -> None:
        (self.root / "journal.json").write_text(json.dumps(
            {"schemaVersion": "0.1", "records": records}))

    def _admission(self, work_order_ref: str) -> dict:
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            row = ledger.connection.execute(
                "SELECT admission_id,reserved_micros FROM thesis_impact_day_admissions "
                "WHERE work_order_ref=?", (work_order_ref,)).fetchone()
        return dict(row)

    def test_purpose_is_read_from_the_cockpit_work_order_ref(self) -> None:
        self.assertEqual(purpose_of("work:cockpit-event_judgement-ab12"), "event_judgement")
        self.assertEqual(purpose_of("work:cockpit-research_language_check-ff"),
                         "research_language_check")
        self.assertIsNone(purpose_of("work:document-extraction-ab12"))
        self.assertIsNone(purpose_of("work:cockpit-"))

    def test_orphans_settle_at_the_metered_cost_or_the_reservation(self) -> None:
        metered = self._orphan("metered")
        unknown = self._orphan("unknown")
        created_ms = int(NOW.timestamp() * 1000)
        self._journal([
            {"createdAtMs": created_ms, "invocationId": "invocation:metered",
             "state": "completed",
             "response": {"workOrderId": metered["work_order_ref"],
                          "cost": {"available": True, "usd": 0.2739862}}},
            # Outside the attempt's lease window: another attempt's call.
            {"createdAtMs": created_ms - 3_600_000, "invocationId": "invocation:old",
             "state": "completed",
             "response": {"workOrderId": metered["work_order_ref"],
                          "cost": {"available": True, "usd": 9.0}}},
            # Inside the window but without a metered cost: unknown.
            {"createdAtMs": created_ms, "invocationId": "invocation:unmetered",
             "state": "completed",
             "response": {"workOrderId": unknown["work_order_ref"],
                          "cost": {"available": False}}},
        ])
        plan = self._plan(now=NOW + timedelta(minutes=5))
        by_work = {item["work_order_ref"]: item for item in plan["settle"]}
        self.assertEqual(set(by_work), {metered["work_order_ref"],
                                        unknown["work_order_ref"]})
        self.assertEqual(by_work[metered["work_order_ref"]]["actual_micros"], 273_986)
        self.assertEqual(by_work[metered["work_order_ref"]]["source"], "broker_journal")
        self.assertEqual(by_work[metered["work_order_ref"]]["invocation_refs"],
                         ["invocation:metered"])
        reserved = self._admission(unknown["work_order_ref"])["reserved_micros"]
        self.assertEqual(by_work[unknown["work_order_ref"]]["actual_micros"], reserved)
        self.assertEqual(by_work[unknown["work_order_ref"]]["source"],
                         "unknown_cost_reserved")
        zero = self._plan(now=NOW + timedelta(minutes=5), unknown_cost="zero")
        self.assertEqual(
            {i["work_order_ref"]: i["actual_micros"] for i in zero["settle"]}
            [unknown["work_order_ref"]], 0)
        # Other purposes are out of scope unless asked for.
        self.assertEqual(self._plan(now=NOW, purposes=["dossier"])["settle"], [])

        # Planning wrote nothing; applying writes once, and is idempotent.
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            self.assertEqual(ledger.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_settlements").fetchone()[0], 0)
        applied = apply_plan(plan)
        self.assertEqual([r["status"] for r in applied], ["fresh", "fresh"])
        self.assertEqual(self._plan(now=NOW + timedelta(minutes=5))["settle"], [])
        self.assertEqual([r["status"] for r in apply_plan(plan)],
                         ["duplicate", "duplicate"])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            committed = ledger.day_summary(
                policy_version_id=fallback.BUDGET_POLICY,
                day=NOW.date().isoformat())["committed_micros"]
        self.assertEqual(committed, 273_986 + reserved)

    def test_an_attempt_inside_its_lease_is_never_touched(self) -> None:
        self._journal([])
        adapter = PlanningAdapter(self)
        answer = self._model(adapter, policy_version_ref=self.chain_policy).call(
            purpose="event_judgement", request_id="in-flight", prompt="judge",
            mission=self.mission)
        during, after_expiry = adapter.plans[0], adapter.plans[1]
        self.assertEqual(during["settle"], [])
        self.assertEqual([i["reason"] for i in during["skipped"]], ["attempt_in_flight"])
        self.assertEqual([i["work_order_ref"] for i in during["skipped"]],
                         [answer["work_order_ref"]])
        # The same leased attempt, planned after its lease ran out, is over.
        self.assertEqual([i["attempt_state"] for i in after_expiry["settle"]],
                         ["leased"])
        # And once the live call has settled it, it is not an orphan at all.
        self.assertEqual(self._plan(now=NOW)["settle"], [])

    def test_a_settlement_written_since_the_plan_wins(self) -> None:
        orphan = self._orphan("raced")
        self._journal([])
        plan = self._plan(now=NOW + timedelta(minutes=5))
        admission = self._admission(orphan["work_order_ref"])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            ledger.settle(admission["admission_id"], actual_micros=1)
        self.assertEqual([r["status"] for r in apply_plan(plan)], ["refused"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
