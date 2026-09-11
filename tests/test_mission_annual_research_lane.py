"""Writer dispatch for immutable mission annual-research admissions."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_annual_research_lane import (
    LANE,
    LANE_CONFIG,
    MissionAnnualResearchCoordinator,
    MissionAnnualResearchLaneError,
    argv_fragment,
    lane_configuration,
)
from dalton_core.scheduler import Scheduler
from dalton_core.store import canonical_json, content_hash


class _Store:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE mission_annual_research_admissions (
                admission_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE mission_annual_research_outcomes (
                outcome_id TEXT PRIMARY KEY,
                admission_ref TEXT NOT NULL UNIQUE
            );
            CREATE TABLE mission_annual_research_starts (
                start_id TEXT PRIMARY KEY,
                admission_ref TEXT NOT NULL UNIQUE
            );
            """
        )

    def add(self, ordinal: int) -> dict:
        body = {
            "schema_version": "0.1",
            "id": f"mission-annual-research-admission:{ordinal:032x}",
            "created_at": f"2026-09-11T12:00:0{ordinal}.000000+00:00",
            "identity_hash": content_hash({"ordinal": ordinal}),
        }
        wire = {**body, "content_hash": content_hash(body)}
        self.connection.execute(
            "INSERT INTO mission_annual_research_admissions VALUES(?,?,?,?)",
            (wire["id"], canonical_json(wire), wire["content_hash"], wire["created_at"]),
        )
        self.connection.commit()
        return wire

    def started(self, admission_ref: str) -> None:
        self.connection.execute(
            "INSERT INTO mission_annual_research_starts VALUES(?,?)",
            ("start:" + content_hash(admission_ref)[:24], admission_ref),
        )
        self.connection.commit()


class _Launcher:
    def __init__(self, tickets_dir: Path) -> None:
        self.tickets_dir = tickets_dir
        tickets_dir.mkdir(parents=True)
        self.tickets: dict[str, dict] = {}
        self.started: list[tuple[str, str]] = []
        self.resumed: list[dict] = []

    def start(self, *, admission_ref: str, admission_hash: str) -> dict:
        self.started.append((admission_ref, admission_hash))
        ticket = {
            "id": "mission-annual-research:" + f"{len(self.started):024x}",
            "status": "running",
            "summary": None,
        }
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref: str) -> dict:
        return dict(self.tickets[ticket_ref])

    def resume(self, **kwargs) -> dict:
        self.resumed.append(dict(kwargs))
        return {"id": kwargs["prior_ticket_ref"], "status": "running"}


def _write_latest(path: Path, admission: dict, ticket_ref: str) -> None:
    body = {
        "ticket_ref": ticket_ref,
        "admission_ref": admission["id"],
        "admission_hash": admission["content_hash"],
    }
    path.write_text(
        canonical_json({**body, "content_hash": content_hash(body)}) + "\n",
        encoding="utf-8",
    )


class MissionAnnualResearchLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = _Store()
        self.addCleanup(self.store.connection.close)
        self.launcher = _Launcher(self.root / "tickets")
        self.lane = MissionAnnualResearchCoordinator(
            store=self.store, launcher=self.launcher,
        )

    def test_fresh_admission_launches_only_by_exact_ref_and_hash(self) -> None:
        admission = self.store.add(1)
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(
            self.launcher.started,
            [(admission["id"], admission["content_hash"])],
        )
        pointer = json.loads(self.lane.latest_path.read_text(encoding="utf-8"))
        asserted = pointer.pop("content_hash")
        self.assertEqual(asserted, content_hash(pointer))

    def test_failed_admission_is_held_while_next_admission_runs(self) -> None:
        first = self.store.add(1)
        second = self.store.add(2)
        ticket_ref = "mission-annual-research:" + "a" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref,
            "status": "failed",
            "summary": {"status": "failed"},
        }
        _write_latest(self.lane.latest_path, first, ticket_ref)

        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], second["id"])
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertEqual(holds["holds"][first["id"]]["reason"], "failed")
        self.assertEqual(len(self.launcher.started), 1)

    def test_started_without_owned_ticket_is_not_blindly_replayed(self) -> None:
        first = self.store.add(1)
        second = self.store.add(2)
        self.store.started(first["id"])

        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["admission_ref"], second["id"])
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        self.assertEqual(
            holds["holds"][first["id"]]["reason"],
            "started_without_owned_live_ticket",
        )

    def test_tampered_latest_and_hold_authority_fail_closed(self) -> None:
        admission = self.store.add(1)
        ticket_ref = "mission-annual-research:" + "b" * 24
        _write_latest(self.lane.latest_path, admission, ticket_ref)
        latest = json.loads(self.lane.latest_path.read_text(encoding="utf-8"))
        latest["admission_hash"] = "0" * 64
        self.lane.latest_path.write_text(canonical_json(latest) + "\n")
        with self.assertRaisesRegex(MissionAnnualResearchLaneError, "pointer drifted"):
            self.lane.dispatch_once()

        self.lane.latest_path.unlink()
        body = {
            "schema_version": "0.1",
            "holds": {
                admission["id"]: {
                    "admission_hash": admission["content_hash"],
                    "ticket_ref": None,
                    "reason": "orphaned",
                    "disposition": "terminal_hold",
                    "retry_at": None,
                    "extra": True,
                }
            },
        }
        self.lane.holds_path.write_text(
            canonical_json({**body, "content_hash": content_hash(body)}) + "\n"
        )
        with self.assertRaisesRegex(MissionAnnualResearchLaneError, "invalid entry"):
            self.lane.dispatch_once()

    def test_hold_for_same_ref_cannot_hide_changed_admission_hash(self) -> None:
        admission = self.store.add(1)
        body = {
            "schema_version": "0.1",
            "holds": {
                admission["id"]: {
                    "admission_hash": "0" * 64,
                    "ticket_ref": None,
                    "reason": "orphaned",
                    "disposition": "terminal_hold",
                    "retry_at": None,
                }
            },
        }
        self.lane.holds_path.write_text(
            canonical_json({**body, "content_hash": content_hash(body)}) + "\n"
        )
        with self.assertRaisesRegex(MissionAnnualResearchLaneError, "hash drifted"):
            self.lane.dispatch_once()

    def _scheduler_work(self, admission: dict) -> tuple[Scheduler, dict]:
        scheduler = Scheduler(
            connection=self.store.connection, max_attempts=2,
            default_lease_seconds=30, max_lease_seconds=60,
            max_total_lease_seconds=120,
        )
        work_ref = "work:mission-annual-research-" + content_hash({
            "admission_identity_hash": admission["identity_hash"], "ordinal": 1,
        })[:32]
        work = {
            "schema_version": "0.1", "id": work_ref,
            "created_at": admission["created_at"],
            "updated_at": admission["created_at"],
            "question": "retrieve exact registered annual source",
            "requested_capabilities": ["registered_source_retrieval"],
            "runtime_profile_ref": "runtime:registered-source-local",
            "budget": {"max_seconds": 60},
            "idempotency_key": "enqueue:" + work_ref,
            "declared_side_effects": [], "status": "ready",
            "input_refs": [admission["id"]],
            "metadata": {
                "mission_annual_research_admission_ref": admission["id"],
                "mission_annual_research_admission_hash": admission["content_hash"],
                "stage": "registered_source_retrieval",
            },
        }
        scheduler.enqueue(work)
        return scheduler, work

    def test_failed_child_with_ready_exact_work_uses_controlled_reentry(self) -> None:
        admission = self.store.add(1)
        self.store.started(admission["id"])
        self._scheduler_work(admission)
        ticket_ref = "mission-annual-research:" + "c" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "failed",
            "summary": {"status": "incomplete"},
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)

        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(len(self.launcher.resumed), 1)
        authorization = json.loads(self.launcher.resumed[0]["authorization"])
        self.assertEqual(authorization["kind"], "exact_scheduler_replay")
        self.assertEqual(authorization["work_order_ref"], result["last"]["recovery"]["work_order_ref"])

    def test_proved_capacity_terminal_is_visible_as_recovery_required(self) -> None:
        admission = self.store.add(1)
        self.store.started(admission["id"])
        scheduler, work = self._scheduler_work(admission)
        claim = scheduler.claim("worker:test", work_order_id=work["id"])
        assert claim is not None
        result = {
            "schema_version": "0.1",
            "id": "result:capacity:" + "d" * 24,
            "created_at": "2026-09-11T12:00:03.000000+00:00",
            "work_order_ref": work["id"],
            "invocation_ref": "invocation:not-started:" + "d" * 24,
            "status": "failed", "outputs": {},
            "actual_side_effects": [], "usage_refs": [], "artifact_refs": [],
            "error": {"code": "BUSY"},
            "metadata": {"control_plane_failure": True},
        }
        scheduler.complete(
            work["id"], 1, "worker:test", claim["lease_token"], result,
            idempotency_key="capacity-terminal",
        )
        ticket_ref = "mission-annual-research:" + "d" * 24
        self.launcher.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "succeeded",
            "summary": {"status": "blocked"},
        }
        _write_latest(self.lane.latest_path, admission, ticket_ref)

        dispatched = self.lane.dispatch_once()
        self.assertEqual(dispatched["status"], "recovery_required")
        holds = json.loads(self.lane.holds_path.read_text(encoding="utf-8"))
        held = holds["holds"][admission["id"]]
        self.assertEqual(held["disposition"], "recovery_required")
        self.assertEqual(held["reason"], "proved_capacity_not_sent_exhausted")

    def test_lane_is_opt_in_and_registered_once(self) -> None:
        self.assertIs(
            lane_for_operation("dispatch_mission_annual_research"), LANE
        )
        self.assertEqual(LANE.driver_key, "mission_annual_research")
        orders = [item.order for item in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))
        context = type("Context", (), {"state": self.root})()
        self.assertEqual(argv_fragment(context), [])
        config = self.root / LANE_CONFIG
        config.write_text(
            canonical_json({"schema_version": "0.1", "enabled": True}) + "\n"
        )
        self.assertEqual(
            lane_configuration(config), {"schema_version": "0.1", "enabled": True}
        )
        self.assertEqual(
            argv_fragment(context), ["--mission-annual-research-lane", str(config)]
        )


if __name__ == "__main__":
    unittest.main()
