"""Adversarial tests for authority-bound research-plan admission."""

from __future__ import annotations

import unittest

from dalton_core.research_coordinator import ResearchCoordinatorStore
from dalton_core.research_plan import (
    ResearchPlanConflict,
    _plan_work_orders,
)
from dalton_core.research_plan_coordinator import (
    ResearchPlanCoordinator,
    ResearchPlanCoordinatorConflict,
    _stage_output_ref,
)
from dalton_core.store import content_hash
from tests import test_research_plan as planner_test_support


NOW = planner_test_support.NOW


class InjectedAdmissionCrash(RuntimeError):
    pass


class ResearchPlanCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the planner's full Agenda/Backlog authority fixture without
        # inheriting its test methods into this module.
        self.planner = planner_test_support.ResearchPlanTests(
            methodName="test_create_plan_is_exact_closed_four_step_tree"
        )
        self.planner.setUp()
        self.addCleanup(self.planner.doCleanups)
        self.connector_records = ResearchCoordinatorStore(":memory:")
        self.addCleanup(self.connector_records.close)
        self.coordinator = ResearchPlanCoordinator(
            plan=self.planner.plans,
            scheduler=self.planner.scheduler,
            connector_records=self.connector_records,
        )

    def _started(self, suffix: str = "coordinator") -> tuple[dict, list[dict]]:
        created = self.planner._create_plan(suffix=suffix)
        self.planner._approve(created, suffix=suffix)
        self.planner._start(created, suffix=suffix)
        plan = self.planner.plans.plan_version(created["plan_version_ref"])
        return plan, _plan_work_orders(plan)

    def _claim(self, work: dict, *, owner: str = "runner:test") -> dict:
        return self.planner.scheduler.claim(owner, work_order_id=work["id"])

    @staticmethod
    def _runner_request(work: dict, claim: dict, *, suffix: str) -> dict:
        base = {
            "schema_version": "0.1",
            "id": f"connector-runner-request:{suffix}",
            "created_at": NOW,
            "connector_invocation_ref": f"connector-invocation:{suffix}",
            "connector_invocation_hash": "1" * 64,
            "execution_ref": f"execution:{suffix}",
            "execution_hash": "2" * 64,
            "work_order_ref": work["id"],
            "work_order_hash": content_hash(work),
            "scheduler_attempt_number": claim["attempt"]["attempt_number"],
            "scheduler_lease_revision_ref": claim["lease"]["id"],
            "scheduler_lease_hash": claim["lease"]["content_hash"],
            "connector_profile_ref": "connector-profile:sec:test",
            "connector_profile_hash": "3" * 64,
            "call_spec_ref": f"connector-call:{suffix}",
            "call_spec_hash": "4" * 64,
            "capability_lease_ref": f"capability-lease:{suffix}",
            "capability_lease_hash": "5" * 64,
            "principal_ref": "principal:test",
            "runner_runtime_ref": "runner-runtime:test",
            "runner_actor_ref": "runner:test",
            "runner_environment_hash": "6" * 64,
            "idempotency_key": f"connector-request:{suffix}",
        }
        return {**base, "content_hash": content_hash(base)}

    def _complete_connector(
        self,
        work: dict,
        *,
        suffix: str = "root",
        store_receipt: bool = True,
        receipt_result_hash: str | None = None,
    ) -> dict:
        claim = self._claim(work)
        request = self._runner_request(work, claim, suffix=suffix)
        self.connector_records.store_runner_request(request)
        source_ref = f"source-envelope:{suffix}"
        artifact_ref = f"artifact-version:{suffix}"
        result = {
            "schema_version": "0.1",
            "id": f"result-envelope:{suffix}",
            "created_at": NOW,
            "work_order_ref": work["id"],
            "invocation_ref": f"execution:{suffix}",
            "status": "succeeded",
            "outputs": {"source_envelope_ref": source_ref},
            "actual_side_effects": ["read:public-http"],
            "usage_refs": [],
            "artifact_refs": [artifact_ref],
            "metadata": {"runner_request_ref": request["id"]},
        }
        result_hash = content_hash(result)
        if store_receipt:
            receipt = {
                "schema_version": "0.1",
                "id": f"connector-completion-receipt:{suffix}",
                "created_at": NOW,
                "runner_request_ref": request["id"],
                "runner_request_hash": request["content_hash"],
                "status": "succeeded",
                "result_ref": result["id"],
                "result_hash": receipt_result_hash or result_hash,
                "source_envelopes": [{"ref": source_ref, "hash": "7" * 64}],
                "artifacts": [{"ref": artifact_ref, "hash": "8" * 64}],
                "next_cursor": None,
                "error_code": None,
                "retry_after_ms": None,
            }
            receipt["content_hash"] = content_hash(receipt)
            self.connector_records.store_completion_receipt(receipt)
        self.planner.scheduler.complete(
            work["id"],
            claim["attempt"]["attempt_number"],
            "runner:test",
            claim["lease_token"],
            result,
            idempotency_key=f"complete:{suffix}",
            result_envelope_hash=result_hash,
        )
        return result

    def _complete_internal(
        self,
        plan: dict,
        work: dict,
        *,
        step_index: int,
        upstream_work: dict,
        suffix: str,
    ) -> dict:
        claim = self._claim(work)
        prior = self.planner.scheduler.formal_result(upstream_work["id"])
        assert prior is not None
        stage = plan["execution_scope"]["steps"][step_index]["stage"]
        record_specs = {
            "authority_resolver": [
                ("authority_resolution", f"authority-resolution:{suffix}")
            ],
            "verifier": [
                ("source_verification", f"verification-bundle:source:{suffix}"),
                ("numeric_verification", f"verification-bundle:numeric:{suffix}"),
            ],
            "candidate_staging": [
                ("candidate_evidence", f"candidate-evidence-version:{suffix}"),
                ("candidate_claim", f"candidate-claim-version:{suffix}"),
            ],
        }
        proof = {
            "schema_version": "0.1",
            "id": _stage_output_ref(
                plan_version_ref=plan["id"],
                step_ref=plan["execution_scope"]["steps"][step_index]["id"],
                upstream_result_ref=prior["result_envelope_id"],
                upstream_result_hash=prior["result_envelope_hash"],
            ),
            "created_at": NOW,
            "plan_version_ref": plan["id"],
            "plan_version_hash": plan["content_hash"],
            "step_ref": plan["execution_scope"]["steps"][step_index]["id"],
            "step_hash": plan["execution_scope"]["steps"][step_index]["content_hash"],
            "stage": stage,
            "operation": plan["execution_scope"]["steps"][step_index]["operation"],
            "output_contract_ref": plan["execution_scope"]["steps"][step_index]["output_contract_ref"],
            "upstream_work_order_ref": upstream_work["id"],
            "upstream_result_ref": prior["result_envelope_id"],
            "upstream_result_hash": prior["result_envelope_hash"],
            "records": [
                {"kind": kind, "ref": ref, "hash": content_hash({"ref": ref})}
                for kind, ref in record_specs[stage]
            ],
        }
        proof["content_hash"] = content_hash(proof)
        result = {
            "schema_version": "0.1",
            "id": f"result-envelope:{suffix}",
            "created_at": NOW,
            "work_order_ref": work["id"],
            "invocation_ref": f"execution:{suffix}",
            "status": "succeeded",
            "outputs": proof,
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "metadata": {},
        }
        self.planner.scheduler.complete(
            work["id"],
            claim["attempt"]["attempt_number"],
            "runner:test",
            claim["lease_token"],
            result,
            idempotency_key=f"complete:{suffix}",
            result_envelope_hash=content_hash(result),
        )
        return result

    def _complete_connector_v2(self, work: dict, *, suffix: str) -> dict:
        from dalton_core.runner_journal import RunnerJournal

        claim = self._claim(work)
        compiled = self._runner_request(work, claim, suffix=f"{suffix}:compiled")
        self.connector_records.store_runner_request(compiled)
        actual_base = {
            key: value for key, value in compiled.items() if key != "content_hash"
        }
        actual_base["id"] = f"connector-runner-request:{suffix}:actual"
        actual_base["idempotency_key"] = f"connector-request:{suffix}:actual"
        actual = {**actual_base, "content_hash": content_hash(actual_base)}
        journal = RunnerJournal(self.planner.store)
        journal.begin_request(actual)
        journal.append(
            actual["id"], "reserved", {"reservation_ref": f"reservation:{suffix}"}
        )
        journal.append(
            actual["id"], "observed", {"reservation_ref": f"reservation:{suffix}"}
        )

        source_ref = f"source-envelope:{suffix}"
        artifact_ref = f"artifact-version:{suffix}"
        result = {
            "schema_version": "0.1",
            "id": f"result-envelope:{suffix}",
            "created_at": NOW,
            "work_order_ref": work["id"],
            "invocation_ref": f"execution:{suffix}",
            "status": "succeeded",
            "outputs": {"source_envelope_ref": source_ref},
            "actual_side_effects": ["read:public-http"],
            "usage_refs": [],
            "artifact_refs": [artifact_ref],
            "metadata": {"runner_request_ref": actual["id"]},
        }
        result_hash = content_hash(result)
        receipt = {
            "schema_version": "0.2",
            "id": f"connector-completion-receipt:{suffix}",
            "created_at": NOW,
            "runner_request_ref": compiled["id"],
            "runner_request_hash": compiled["content_hash"],
            "actual_runner_request_ref": actual["id"],
            "actual_runner_request_hash": actual["content_hash"],
            "status": "succeeded",
            "result_ref": result["id"],
            "result_hash": result_hash,
            "source_envelopes": [{"ref": source_ref, "hash": "a" * 64}],
            "artifacts": [{"ref": artifact_ref, "hash": "b" * 64}],
            "next_cursor": None,
            "error_code": None,
            "retry_after_ms": None,
        }
        receipt["content_hash"] = content_hash(receipt)
        self.connector_records.store_completion_receipt(receipt)
        self.planner.scheduler.complete(
            work["id"], 1, "runner:test", claim["lease_token"], result,
            idempotency_key=f"complete:{suffix}",
            result_envelope_hash=result_hash,
        )
        response = {
            "schema_version": "0.2",
            "id": f"connector-runner-response:{suffix}",
            "created_at": NOW,
            "runner_request_ref": actual["id"],
            "runner_request_hash": actual["content_hash"],
            "idempotency_status": "fresh",
            "connector_invocation_ref": actual["connector_invocation_ref"],
            "connector_invocation_hash": actual["connector_invocation_hash"],
            "physical_attempt_ref": f"physical-attempt:{suffix}",
            "physical_attempt_hash": "c" * 64,
            "usage_entry_ref": f"usage:{suffix}",
            "usage_entry_hash": "d" * 64,
            "cost_entry_ref": f"cost:{suffix}",
            "cost_entry_hash": "e" * 64,
            "quota_settlement_ref": f"settlement:{suffix}",
            "quota_settlement_hash": "f" * 64,
            "raw_artifact_version_ref": artifact_ref,
            "raw_artifact_version_hash": "b" * 64,
            "source_envelope_ref": source_ref,
            "source_envelope_hash": "a" * 64,
            "result_envelope_ref": result["id"],
            "result_envelope_hash": result_hash,
            "outcome": "succeeded",
            "retry_at": None,
        }
        response["content_hash"] = content_hash(response)
        journal.append(
            actual["id"],
            "responded",
            {"reservation_ref": f"reservation:{suffix}", "response": response},
        )
        return result

    def test_root_success_admits_only_immediate_child_and_replay_converges(self) -> None:
        plan, work = self._started()
        self._complete_connector(work[0])
        fresh = self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
        )
        duplicate = self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
        )
        self.assertEqual(fresh["status"], "fresh")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(fresh["admitted_work_order_ref"], work[1]["id"])
        rows = self.planner.store.connection.execute(
            "SELECT work_order_id FROM scheduler_work_orders ORDER BY work_order_id"
        ).fetchall()
        self.assertEqual({row["work_order_id"] for row in rows}, {work[0]["id"], work[1]["id"]})

    def test_v2_receipt_rechecks_actual_request_and_responded_journal(self) -> None:
        plan, work = self._started(suffix="v2")
        self._complete_connector_v2(work[0], suffix="v2")
        admitted = self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
        )
        self.assertEqual(admitted["status"], "fresh")
        self.assertEqual(admitted["admitted_work_order_ref"], work[1]["id"])

    def test_cannot_skip_a_planned_upstream_node(self) -> None:
        plan, work = self._started(suffix="skip")
        self._complete_connector(work[0], suffix="skip")
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=plan["id"], upstream_work_order_ref=work[1]["id"]
            )
        self.assertEqual(
            self.planner.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            1,
        )

    def test_opaque_internal_success_cannot_authorize_next_stage(self) -> None:
        plan, work = self._started(suffix="opaque")
        self._complete_connector(work[0], suffix="opaque")
        self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
        )
        claim = self._claim(work[1])
        opaque = {
            "schema_version": "0.1",
            "id": "result-envelope:opaque-internal",
            "created_at": NOW,
            "work_order_ref": work[1]["id"],
            "invocation_ref": "execution:opaque",
            "status": "succeeded",
            "outputs": {"success": True, "payload": "looks plausible"},
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "metadata": {},
        }
        self.planner.scheduler.complete(
            work[1]["id"], 1, "runner:test", claim["lease_token"], opaque,
            idempotency_key="complete:internal:opaque",
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=plan["id"], upstream_work_order_ref=work[1]["id"]
            )
        self.assertIsNone(
            self.planner.store.connection.execute(
                "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
                (work[2]["id"],),
            ).fetchone()
        )

    def test_full_four_node_admission_is_strictly_one_edge_at_a_time(self) -> None:
        plan, work = self._started(suffix="full")
        self._complete_connector(work[0], suffix="full-root")
        for index, suffix in ((0, "resolver"), (1, "verifier"), (2, "staging")):
            admitted = self.coordinator.admit_next_work_order(
                plan_version_ref=plan["id"],
                upstream_work_order_ref=work[index]["id"],
            )
            self.assertEqual(admitted["admitted_work_order_ref"], work[index + 1]["id"])
            self._complete_internal(
                plan,
                work[index + 1],
                step_index=index + 1,
                upstream_work=work[index],
                suffix=f"full-{suffix}",
            )
        complete = self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[3]["id"]
        )
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(
            self.planner.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            4,
        )
        status = self.coordinator.tree_status(plan["id"])
        self.assertEqual(
            [node["admission_state"] for node in status["nodes"]],
            ["queued", "queued", "queued", "queued"],
        )
        self.assertEqual(
            [node["attempt_state"] for node in status["nodes"]],
            ["succeeded", "succeeded", "succeeded", "succeeded"],
        )

    def test_pending_failed_and_retry_exhausted_upstream_never_admit(self) -> None:
        plan, work = self._started(suffix="pending")
        pending = self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
        )
        self.assertEqual(pending["status"], "pending_upstream")

        failed_plan, failed_work = self._started(suffix="failed")
        claim = self._claim(failed_work[0])
        failed_result = {
            "schema_version": "0.1",
            "id": "result-envelope:failed",
            "created_at": NOW,
            "work_order_ref": failed_work[0]["id"],
            "invocation_ref": "execution:failed",
            "status": "failed",
            "outputs": {},
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "error": {"code": "failed", "message": "fixture failure"},
            "metadata": {},
        }
        self.planner.scheduler.complete(
            failed_work[0]["id"], 1, "runner:test", claim["lease_token"],
            failed_result, idempotency_key="complete:failed",
        )
        blocked = self.coordinator.admit_next_work_order(
            plan_version_ref=failed_plan["id"], upstream_work_order_ref=failed_work[0]["id"]
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNone(
            self.planner.store.connection.execute(
                "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
                (failed_work[1]["id"],),
            ).fetchone()
        )

        exhausted_plan, exhausted_work = self._started(suffix="exhausted")
        for attempt_number in (1, 2):
            claim = self._claim(exhausted_work[0])
            self.assertEqual(claim["attempt"]["attempt_number"], attempt_number)
            retryable = {
                "schema_version": "0.1",
                "id": f"result-envelope:retryable:{attempt_number}",
                "created_at": NOW,
                "work_order_ref": exhausted_work[0]["id"],
                "invocation_ref": f"execution:retryable:{attempt_number}",
                "status": "retryable",
                "outputs": {},
                "actual_side_effects": [],
                "usage_refs": [],
                "artifact_refs": [],
                "error": {
                    "code": "retryable",
                    "message": "fixture retry",
                    "retryable": True,
                },
                "metadata": {},
            }
            self.planner.scheduler.complete(
                exhausted_work[0]["id"], attempt_number, "runner:test",
                claim["lease_token"], retryable,
                idempotency_key=f"complete:retryable:{attempt_number}",
            )
        exhausted = self.coordinator.admit_next_work_order(
            plan_version_ref=exhausted_plan["id"],
            upstream_work_order_ref=exhausted_work[0]["id"],
        )
        self.assertEqual(exhausted["status"], "blocked")
        self.assertEqual(exhausted["upstream_state"], "plan_attempts_exhausted")

    def test_bare_success_or_wrong_receipt_result_cannot_authorize(self) -> None:
        plan, work = self._started(suffix="bare")
        self._complete_connector(work[0], suffix="bare", store_receipt=False)
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
            )

        other_plan, other_work = self._started(suffix="wrong-receipt")
        self._complete_connector(
            other_work[0], suffix="wrong-receipt", receipt_result_hash="9" * 64
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=other_plan["id"],
                upstream_work_order_ref=other_work[0]["id"],
            )

    def test_wrong_plan_upstream_substitution_is_rejected(self) -> None:
        first_plan, first_work = self._started(suffix="first")
        second_plan, _ = self._started(suffix="second")
        self._complete_connector(first_work[0], suffix="first")
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=second_plan["id"],
                upstream_work_order_ref=first_work[0]["id"],
            )

    def test_receipt_scheduler_result_and_child_tamper_fail_closed(self) -> None:
        plan, work = self._started(suffix="tamper")
        self._complete_connector(work[0], suffix="tamper")
        self.connector_records.connection.execute(
            "DROP TRIGGER research_completion_receipts_no_update"
        )
        self.connector_records.connection.execute(
            "UPDATE research_completion_receipts SET content_hash=?",
            ("0" * 64,),
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
            )

        clean_plan, clean_work = self._started(suffix="child-tamper")
        self._complete_connector(clean_work[0], suffix="child-tamper")
        self.coordinator.admit_next_work_order(
            plan_version_ref=clean_plan["id"],
            upstream_work_order_ref=clean_work[0]["id"],
        )
        self.planner.store.connection.execute("DROP TRIGGER scheduler_work_no_update")
        self.planner.store.connection.execute(
            "UPDATE scheduler_work_orders SET work_order_hash=? WHERE work_order_id=?",
            ("0" * 64, clean_work[1]["id"]),
        )
        with self.assertRaises((ResearchPlanConflict, ResearchPlanCoordinatorConflict)):
            self.coordinator.admit_next_work_order(
                plan_version_ref=clean_plan["id"],
                upstream_work_order_ref=clean_work[0]["id"],
            )

    def test_attempt_event_and_formal_result_tamper_fail_closed(self) -> None:
        plan, work = self._started(suffix="attempt-tamper")
        self._complete_connector(work[0], suffix="attempt-tamper")
        self.planner.store.connection.execute("DROP TRIGGER scheduler_attempt_no_update")
        self.planner.store.connection.execute(
            "UPDATE scheduler_attempt_events SET content_hash=? "
            "WHERE work_order_id=? AND state='succeeded'",
            ("0" * 64, work[0]["id"]),
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
            )

        other_plan, other_work = self._started(suffix="formal-tamper")
        self._complete_connector(other_work[0], suffix="formal-tamper")
        self.planner.store.connection.execute("DROP TRIGGER scheduler_result_no_update")
        self.planner.store.connection.execute(
            "UPDATE scheduler_formal_results SET result_envelope_hash=? "
            "WHERE work_order_id=?",
            ("0" * 64, other_work[0]["id"]),
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=other_plan["id"],
                upstream_work_order_ref=other_work[0]["id"],
            )

        result_plan, result_work = self._started(suffix="result-tamper")
        self._complete_connector(result_work[0], suffix="result-tamper")
        self.planner.store.connection.execute(
            "DROP TRIGGER scheduler_result_envelope_no_update"
        )
        self.planner.store.connection.execute(
            "UPDATE scheduler_result_envelopes SET content_hash=? "
            "WHERE work_order_id=?",
            ("0" * 64, result_work[0]["id"]),
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.admit_next_work_order(
                plan_version_ref=result_plan["id"],
                upstream_work_order_ref=result_work[0]["id"],
            )

    def test_crash_after_enqueue_replays_to_one_child(self) -> None:
        plan, work = self._started(suffix="crash")
        self._complete_connector(work[0], suffix="crash")

        def fail(seam: str) -> None:
            if seam == "after_enqueue":
                raise InjectedAdmissionCrash(seam)

        crashing = ResearchPlanCoordinator(
            plan=self.planner.plans,
            scheduler=self.planner.scheduler,
            connector_records=self.connector_records,
            fault_injector=fail,
        )
        with self.assertRaises(InjectedAdmissionCrash):
            crashing.admit_next_work_order(
                plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
            )
        recovered = self.coordinator.admit_next_work_order(
            plan_version_ref=plan["id"], upstream_work_order_ref=work[0]["id"]
        )
        self.assertEqual(recovered["status"], "duplicate")
        self.assertEqual(
            self.planner.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders WHERE work_order_id=?",
                (work[1]["id"],),
            ).fetchone()[0],
            1,
        )

    def test_tree_status_rejects_missing_or_tampered_attempt_history(self) -> None:
        plan, work = self._started(suffix="status")
        status = self.coordinator.tree_status(plan["id"])
        self.assertEqual(
            [node["admission_state"] for node in status["nodes"]],
            ["queued", "planned", "planned", "planned"],
        )
        self.planner.store.connection.execute("DROP TRIGGER scheduler_attempt_no_delete")
        self.planner.store.connection.execute(
            "DELETE FROM scheduler_attempt_events WHERE work_order_id=?",
            (work[0]["id"],),
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            self.coordinator.tree_status(plan["id"])


if __name__ == "__main__":
    unittest.main()
