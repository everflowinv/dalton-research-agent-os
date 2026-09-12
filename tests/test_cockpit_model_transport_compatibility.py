"""Compatibility boundaries for model-spec Work transport metadata."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import dalton_core.cockpit_model as cockpit_model
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.company_model_cli import (
    _validated_spec_with_repair,
    model_spec_request_id,
    model_spec_request_identity,
)
from dalton_core.contracts import WorkOrder
from dalton_core.scheduler import Scheduler
from tests import test_cockpit_model_fallback as fallback_tests
from tests.test_cockpit_model_fallback import ChainAdapter
from tests.test_company_model_spec import STATE, _spec


TRANSPORT = {
    "max_definitely_not_sent_retries": 0,
    "queue_wait_seconds": 7,
    "retry_backoff_seconds": 0,
}


class ModelSpecTransportCompatibilityTests(TestCase):
    """Use the existing fixture setup without inheriting its full test suite."""

    def setUp(self) -> None:
        self.fixture = fallback_tests.CockpitChainTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.doCleanups()

    def _model(self, adapter):
        return self.fixture._model(
            adapter,
            policy_version_ref=self.fixture.pinned_policy,
            transport_retry=TRANSPORT,
        )

    def test_legacy_initial_and_repair_chain_replay_without_another_provider_call(self):
        initial = _spec(assessment="x" * 1201)
        repaired = copy.deepcopy(initial)
        repaired["assessment"] = "x" * 1199

        class SequenceAdapter(ChainAdapter):
            def __init__(inner):
                super().__init__({})
                inner.outputs = [initial, repaired]

            def execute(inner, work, route, profile):
                invocation, envelope = super().execute(work, route, profile)
                return invocation, replace(
                    envelope,
                    outputs={"text": json.dumps(inner.outputs.pop(0))},
                )

        adapter = SequenceAdapter()
        model = self._model(adapter)
        repair_config = {"max_attempts": 1}
        identity = model_spec_request_identity(
            STATE["state_hash"], repair_config=repair_config,
        )
        request_id = model_spec_request_id(
            STATE["state_hash"], repair_config=repair_config,
        )
        real_build_work = cockpit_model.build_work

        def legacy_build_work(**kwargs):
            kwargs.pop("transport_retry", None)
            return real_build_work(**kwargs)

        # Reproduce the exact historical shape: the request namespace already
        # bound the transport policy, while Work.metadata did not repeat it.
        with patch.object(cockpit_model, "build_work", side_effect=legacy_build_work):
            first = model.call(
                purpose="model_spec",
                request_id=request_id,
                prompt="legacy transport metadata model structure",
                mission=self.fixture.mission,
                _model_spec_request_identity=identity,
            )
            spec, repairs, accepted = _validated_spec_with_repair(
                model=model,
                state=STATE,
                mission=self.fixture.mission,
                original_call=first,
                repair_config=repair_config,
                decided_by="automation:test",
            )
        self.assertEqual(len(adapter.served), 2)
        with Scheduler(self.fixture.root / "scheduler.sqlite") as scheduler:
            for ref in (first["work_order_ref"], accepted["work_order_ref"]):
                authority = scheduler.work_order_authority(ref)
                self.assertNotIn("transport_retry", authority["work_order"]["metadata"])

        replayed_first = model.call(
            purpose="model_spec",
            request_id=request_id,
            prompt="legacy transport metadata model structure",
            mission=self.fixture.mission,
            _model_spec_request_identity=identity,
        )
        replayed_spec, replayed_repairs, replayed_accepted = (
            _validated_spec_with_repair(
                model=model,
                state=STATE,
                mission=self.fixture.mission,
                original_call=replayed_first,
                repair_config=repair_config,
                decided_by="automation:test",
            )
        )
        self.assertEqual(replayed_spec["content_hash"], spec["content_hash"])
        self.assertEqual(replayed_accepted["work_order_ref"], accepted["work_order_ref"])
        self.assertTrue(replayed_first["replayed"])
        self.assertTrue(replayed_repairs[0]["replayed"])
        self.assertEqual(len(adapter.served), 2)

    def test_concurrent_foreign_insert_after_absent_read_conflicts_without_provider(self):
        adapter = ChainAdapter({})
        model = self._model(adapter)
        identity = model_spec_request_identity(STATE["state_hash"])
        captured = {}
        real_build_work = cockpit_model.build_work
        real_authority = Scheduler.work_order_authority
        inserted = False

        def capture_build_work(**kwargs):
            work = real_build_work(**kwargs)
            captured["work"] = work
            return work

        def insert_then_report_absent(scheduler, work_order_id):
            nonlocal inserted
            if not inserted:
                inserted = True
                wire = captured["work"].to_dict()
                wire["metadata"] = {**wire["metadata"], "foreign_race": True}
                self.assertEqual(
                    scheduler.enqueue(WorkOrder.from_dict(wire))["status"],
                    "fresh",
                )
                return None
            return real_authority(scheduler, work_order_id)

        with patch.object(cockpit_model, "build_work", side_effect=capture_build_work), \
                patch.object(Scheduler, "work_order_authority", insert_then_report_absent):
            with self.assertRaisesRegex(CockpitModelError, "different content"):
                model.call(
                    purpose="model_spec",
                    request_id=model_spec_request_id(STATE["state_hash"]),
                    prompt="concurrent transport metadata model structure",
                    mission=self.fixture.mission,
                    _model_spec_request_identity=identity,
                )
        self.assertTrue(inserted)
        self.assertEqual(adapter.served, [])

    def test_corrupt_stored_work_hash_is_refused_before_provider(self):
        adapter = ChainAdapter({})
        model = self._model(adapter)
        identity = model_spec_request_identity(STATE["state_hash"])
        kwargs = {
            "purpose": "model_spec",
            "request_id": model_spec_request_id(STATE["state_hash"]),
            "prompt": "corrupt transport metadata model structure",
            "mission": self.fixture.mission,
            "_model_spec_request_identity": identity,
        }
        first = model.call(**kwargs)
        self.assertEqual(len(adapter.served), 1)
        with Scheduler(self.fixture.root / "scheduler.sqlite") as scheduler:
            # Deliberately corrupt the temporary fixture beneath the immutable
            # authority API, as a damaged file or hostile offline edit would.
            scheduler.connection.execute("DROP TRIGGER scheduler_work_no_update")
            scheduler.connection.execute(
                "UPDATE scheduler_work_orders SET work_order_hash=? "
                "WHERE work_order_id=?",
                ("0" * 64, first["work_order_ref"]),
            )

        with self.assertRaisesRegex(CockpitModelError, "authority drifted"):
            model.call(**kwargs)
        self.assertEqual(len(adapter.served), 1)
