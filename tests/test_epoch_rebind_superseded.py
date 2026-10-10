"""2026-10-09: a prior-epoch rebind whose rebound Work was refused, then a new epoch.

Live legacy, EPAM 09b4bb82 / 3c49a49c.  On 10-07 the owner's door rebound
each draft stage under the then-current model authority; the rebound Work's
route was *rejected* (no link eligible, nothing sent).  The owner then
changed the model chains again, and every controlled re-entry since died on
"model authority recovery is bound to an unresolved prior epoch" -- the hold
went back to ``reentry_failed_after_automatic_rebind`` and the card came back
however often the owner pressed 「放行一次重新进入」.

The fixture has that exact shape: one recovery link with its epoch rebind
under authority v2, the rebound Work failed on a rejected route, authority v3
now current.  The next pass must classify the rebound Work's failure, record
the supersession, rebind the fresh Work under v3 and route it.
"""

from __future__ import annotations

import copy
import json
import unittest

from tests import test_mission_document_research as base

_Helpers = type("_Helpers", (unittest.TestCase,), {
    key: value for key, value in vars(base.MissionDocumentResearchTests).items()
    if not key.startswith("test") and key not in ("__dict__", "__weakref__")})

STATEMENT = "Managed services revenue is recognized over time."
GOOD = {"schema_version": "0.1", "status": "answered", "answer": STATEMENT,
        "candidate": {"normalized_statement": STATEMENT, "metric_or_aspect": "revenue",
                      "period": "current", "basis": "reported", "cited_match_indexes": [0]},
        "missing": []}


class SupersededEpochRebindTests(_Helpers):
    def _live_shape(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = base.ContractRejectOnceAdapter(
            GOOD, {"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        self._run_until(executor, admission, lambda item: item.get("status") == "failed")
        execution = copy.deepcopy(admission["model_execution"])
        v2 = copy.deepcopy(admission["model_authority"])
        v2["draft"]["routing_policy_hash"] = "e" * 64
        authority.model_execution_resolver = lambda: (execution, v2)
        admitted = executor.run_once(admission["id"])
        self.assertEqual(admitted["reason"], "automatic_bounded_contract_retry")
        # The rebound Work's route is rejected: nothing eligible, nothing sent.
        router = executor.draft_worker.router
        original = router.route
        router.route = lambda work, **kw: original(
            work, **{**kw, "credential_slot_refs": ("credential-slot:nobody",)})
        failed = self._run_until(executor, admission, lambda item: item.get("status") == "failed")
        del router.route
        rebinds = fixture.store.connection.execute(
            "SELECT * FROM mission_document_research_model_authority_epoch_rebinds").fetchall()
        self.assertEqual(len(rebinds), 1)
        self.assertEqual(failed["work_order_ref"], rebinds[0]["rebound_work_order_ref"])
        # The owner changes the model chains again.
        v3 = copy.deepcopy(v2)
        v3["draft"]["routing_policy_hash"] = "f" * 64
        authority.model_execution_resolver = lambda: (execution, v3)
        return fixture, authority, admission, executor, adapter, rebinds[0]

    def test_the_superseded_rebind_is_recorded_and_the_fresh_work_rebound_and_routed(self):
        from dalton_core.mission_document_research_executor import (
            ROUTE_REJECTED_NO_SEND_CLASSIFICATION)

        fixture, _authority, admission, executor, adapter, old = self._live_shape()
        calls = adapter.calls
        recovered = executor.run_once(admission["id"])
        self.assertEqual(recovered["reason"], "fresh_work_recovery")
        connection = fixture.store.connection
        rebinds = connection.execute(
            "SELECT * FROM mission_document_research_model_authority_epoch_rebinds "
            "ORDER BY created_at").fetchall()
        self.assertEqual(len(rebinds), 2)
        new = next(row for row in rebinds if row["rebind_id"] != old["rebind_id"])
        self.assertNotEqual(new["current_base_work_ref"], old["current_base_work_ref"])
        self.assertEqual(recovered["work_order_ref"], new["rebound_work_order_ref"])
        superseded = connection.execute(
            "SELECT * FROM "
            "mission_document_research_model_authority_epoch_rebind_supersessions").fetchall()
        self.assertEqual([row["rebind_ref"] for row in superseded], [old["rebind_id"]])
        links = [json.loads(row[0]) for row in connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links "
            "ORDER BY recovery_number")]
        self.assertEqual(links[-1]["failed_work_order_ref"], old["rebound_work_order_ref"])
        self.assertEqual(links[-1]["failure_proof"]["classification"],
                         ROUTE_REJECTED_NO_SEND_CLASSIFICATION)
        self.assertEqual(adapter.calls, calls)  # classification and rebind buy nothing
        final = self._run_until(
            executor, admission, lambda item: item.get("research_status") == "candidate_staged")
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(adapter.calls, calls + 1)
        # Re-running converges on the same records.
        self.assertEqual(len(connection.execute(
            "SELECT 1 FROM mission_document_research_model_authority_epoch_rebinds").fetchall()), 2)

    def test_a_tampered_superseded_rebind_still_drifts(self):
        from dalton_core.mission_document_research_executor import (
            MissionDocumentResearchExecutorError)

        fixture, _authority, admission, executor, _adapter, old = self._live_shape()
        connection = fixture.store.connection
        connection.execute(
            "DROP TRIGGER mission_document_research_model_authority_epoch_rebinds_no_update")
        wire = json.loads(old["record_json"])
        wire["tampered"] = True
        connection.execute(
            "UPDATE mission_document_research_model_authority_epoch_rebinds SET record_json=? "
            "WHERE rebind_id=?", (json.dumps(wire, sort_keys=True, separators=(",", ":")),
                                  old["rebind_id"]))
        with self.assertRaisesRegex(MissionDocumentResearchExecutorError, "drifted"):
            executor.run_once(admission["id"])
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebind_supersessions").fetchone()[0], 0)


class OwnerReentryVisibilityTests(unittest.TestCase):
    """The owner's presses and what each one bought, on the card and the list."""

    ADMISSION = "mission-document-research-admission:09b4bb825ae8ef44b3249de067ca4f76"

    def setUp(self):
        import hashlib
        import sqlite3
        import tempfile
        from pathlib import Path

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.runs = self.state / "mission-document-research-runs"
        self.runs.mkdir()
        (self.runs / "holds.json").write_text(json.dumps({"holds": {self.ADMISSION: {
            "disposition": "recovery_required",
            "reason": "reentry_failed_after_automatic_rebind:controlled reentry was already attempted"}}}))
        self.stem = ("controlled-reentry-grant-"
                     + hashlib.sha256(self.ADMISSION.encode()).hexdigest()[:24])
        marker = "c" * 24
        (self.runs / f"{self.stem}-used-{marker}.json").write_text(json.dumps({
            "admission_ref": self.ADMISSION, "granted_at": "2026-10-09T11:49:17+00:00",
            "consumed_at": "2026-10-09T11:50:01+00:00"}))
        run = self.runs / "89de31311923c773e77b10f5"
        run.mkdir()
        (run / f"controlled-reentry-{marker}.json").write_text("{}")
        (run / "summary.json").write_text(json.dumps({
            "admission_ref": self.ADMISSION, "status": "failed",
            "created_at": "2026-10-09T11:53:10+00:00",
            "error": "MissionDocumentResearchExecutorError: model authority recovery is "
                     "bound to an unresolved prior epoch"}))
        self.journal = sqlite3.connect(":memory:")
        self.journal.row_factory = sqlite3.Row
        self.journal.execute("CREATE TABLE cockpit_events(event_id INTEGER PRIMARY KEY, at TEXT, "
                             "kind TEXT, title TEXT, detail TEXT, login TEXT, refs_json TEXT)")
        self.journal.execute(
            "INSERT INTO cockpit_events(at,kind,title,refs_json) VALUES(?,?,?,?)",
            ("2026-10-09T11:49:17+00:00", "lever", "放行了一次重新进入", json.dumps({
                "admission_ref": self.ADMISSION, "job_ref": None,
                "operation": "authorize_mission_document_reentry", "status": "granted"},
                ensure_ascii=False, sort_keys=True)))

    def test_technical_exhaustion_keeps_failure_proof_without_owner_action(self):
        from dalton_core.needs_human import held_lanes

        card = next(item for item in held_lanes(None, state_dir=self.state)
                    if item["detail"].get("admission_ref") == self.ADMISSION)
        self.assertFalse(card["actionable"])
        self.assertEqual(card["responsibility"], "administrator")
        self.assertIn("管理员检查执行凭证", card["action"])
        self.assertIn("原调用与费用记录保留", card["action"])
        last = card["detail"]["last_reentry"]
        self.assertEqual(last["run_status"], "failed")
        self.assertIn("unresolved prior epoch", last["run_error"])

    def test_a_waiting_grant_remains_technical_and_offers_no_second_press(self):
        from dalton_core.needs_human import held_lanes

        (self.runs / f"{self.stem}.json").write_text(json.dumps({
            "admission_ref": self.ADMISSION, "granted_at": "2026-10-09T12:00:00+00:00"}))
        card = next(item for item in held_lanes(None, state_dir=self.state)
                    if item["detail"].get("admission_ref") == self.ADMISSION)
        self.assertFalse(card["actionable"])
        self.assertEqual(card["responsibility"], "administrator")
        self.assertEqual([door["door"] for door in card["detail"]["owner_doors"]], ["withdraw"])

    def test_the_admin_list_shows_cockpit_presses_with_their_result(self):
        from types import SimpleNamespace

        from dalton_core.cockpit_v2.admin.data import cockpit_owner_door_requests

        plane = SimpleNamespace(
            journal=SimpleNamespace(rows=lambda sql, params=(): self.journal.execute(
                sql, params).fetchall()),
            config=SimpleNamespace(state_dir=self.state))
        items = cockpit_owner_door_requests(plane)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["state"], items[0]["state_label"]), ("failed", "重新进入失败"))
        self.assertIn("unresolved prior epoch", items[0]["error"])
        self.assertEqual(items[0]["operation_label"], "放行一次重新进入")


if __name__ == "__main__":
    unittest.main()
