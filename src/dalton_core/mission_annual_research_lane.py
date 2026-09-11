"""Writer lane for already-admitted directed document research.

The planner/research-task producer writes the immutable admission.  This lane
only selects an unstarted admission and launches its admission-ref-only child.
An orphaned or failed child is held and skipped so the lane never guesses that
another paid call is safe, while later independent admissions can still run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    write_owner_only,
)
from .lane_registry import LaneSpec, register_lane
from .store import canonical_json, content_hash


LANE_CONFIG = "mission-annual-research-lane.json"
LAUNCHER_KWARG = "mission_annual_research_launcher"
DRIVER_KEY = "mission_annual_research"
LATEST_FILE = "latest.json"
HOLDS_FILE = "holds.json"


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class MissionAnnualResearchLaneError(RuntimeError):
    pass


def _read_holds(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionAnnualResearchLaneError("annual research hold ledger is invalid") from exc
    body = dict(record)
    asserted = body.pop("content_hash", None)
    if (
        set(record) != {"schema_version", "holds", "content_hash"}
        or record.get("schema_version") != "0.1"
        or not isinstance(record.get("holds"), dict)
        or asserted != content_hash(body)
    ):
        raise MissionAnnualResearchLaneError("annual research hold ledger drifted")
    holds: dict[str, dict[str, Any]] = {}
    for admission_ref, value in record["holds"].items():
        if (
            not isinstance(admission_ref, str)
            or not admission_ref.startswith("mission-annual-research-admission:")
            or not isinstance(value, Mapping)
            or set(value) != {"admission_hash", "ticket_ref", "reason"}
            or not _sha256(value.get("admission_hash"))
            or (
                value.get("ticket_ref") is not None
                and (
                    not isinstance(value.get("ticket_ref"), str)
                    or not value["ticket_ref"].startswith(
                        "mission-annual-research:"
                    )
                )
            )
            or not isinstance(value.get("reason"), str)
            or not value["reason"]
        ):
            raise MissionAnnualResearchLaneError(
                "annual research hold ledger has an invalid entry"
            )
        holds[admission_ref] = dict(value)
    return holds


def _write_holds(path: Path, holds: Mapping[str, Mapping[str, Any]]) -> None:
    body = {
        "schema_version": "0.1",
        "holds": {key: dict(holds[key]) for key in sorted(holds)},
    }
    write_owner_only(path, {**body, "content_hash": content_hash(body)})


class MissionAnnualResearchCoordinator:
    def __init__(self, *, store: Any, launcher: Any | None) -> None:
        self.store = store
        self.launcher = launcher

    @property
    def latest_path(self) -> Path:
        return self.launcher.tickets_dir / LATEST_FILE

    @property
    def holds_path(self) -> Path:
        return self.launcher.tickets_dir / HOLDS_FILE

    def _latest(self) -> dict[str, Any] | None:
        if not self.latest_path.is_file():
            return None
        try:
            value = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MissionAnnualResearchLaneError(
                "annual research latest ticket pointer is invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise MissionAnnualResearchLaneError(
                "annual research latest ticket pointer has an invalid shape"
            )
        body = dict(value)
        asserted = body.pop("content_hash", None)
        if (
            set(value) != {
                "ticket_ref", "admission_ref", "admission_hash", "content_hash",
            }
            or asserted != content_hash(body)
            or not isinstance(value.get("ticket_ref"), str)
            or not value["ticket_ref"].startswith("mission-annual-research:")
            or not isinstance(value.get("admission_ref"), str)
            or not value["admission_ref"].startswith(
                "mission-annual-research-admission:"
            )
            or not _sha256(value.get("admission_hash"))
        ):
            raise MissionAnnualResearchLaneError(
                "annual research latest ticket pointer drifted"
            )
        return dict(value)

    def _admissions(self) -> list[dict[str, Any]]:
        try:
            rows = self.store.connection.execute(
                "SELECT a.* FROM mission_annual_research_admissions a "
                "LEFT JOIN mission_annual_research_outcomes o "
                "ON o.admission_ref=a.admission_id "
                "WHERE o.outcome_id IS NULL ORDER BY a.created_at,a.admission_id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        result = []
        for row in rows:
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionAnnualResearchLaneError(
                    "annual research admission record is invalid"
                ) from exc
            body = dict(wire)
            asserted = body.pop("content_hash", None)
            if (
                canonical_json(wire) != row["record_json"]
                or wire.get("id") != row["admission_id"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
            ):
                raise MissionAnnualResearchLaneError(
                    "annual research admission authority drifted"
                )
            result.append(wire)
        return result

    def _started(self, admission_ref: str) -> bool:
        try:
            return self.store.connection.execute(
                "SELECT 1 FROM mission_annual_research_starts WHERE admission_ref=?",
                (admission_ref,),
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return False
            raise

    def _hold(
        self, holds: dict[str, dict[str, Any]], admission: Mapping[str, Any],
        *, reason: str, ticket_ref: str | None,
    ) -> None:
        holds[admission["id"]] = {
            "admission_hash": admission["content_hash"],
            "ticket_ref": ticket_ref,
            "reason": reason,
        }
        _write_holds(self.holds_path, holds)

    def dispatch_once(self) -> dict[str, Any]:
        if self.launcher is None:
            return {"status": "unconfigured", "reason": "annual research lane is absent"}
        holds = _read_holds(self.holds_path)
        admissions = self._admissions()
        by_ref = {item["id"]: item for item in admissions}
        latest = self._latest()
        settled = None
        if latest is not None and latest["admission_ref"] in by_ref:
            admission = by_ref[latest["admission_ref"]]
            if latest["admission_hash"] != admission["content_hash"]:
                raise MissionAnnualResearchLaneError("latest admission hash drifted")
            ticket = self.launcher.status(latest["ticket_ref"])
            if ticket["status"] == "running":
                return {
                    "status": "busy", "ticket_ref": ticket["id"],
                    "admission_ref": admission["id"],
                }
            summary = ticket.get("summary")
            child_status = None if summary is None else summary.get("status")
            settled = {
                "ticket_ref": ticket["id"],
                "admission_ref": admission["id"],
                "status": child_status or ticket["status"],
            }
            if child_status != "complete":
                self._hold(
                    holds, admission,
                    reason=(child_status or ticket["status"]),
                    ticket_ref=ticket["id"],
                )

        for admission in admissions:
            held = holds.get(admission["id"])
            if held is not None:
                if held["admission_hash"] != admission["content_hash"]:
                    raise MissionAnnualResearchLaneError(
                        "annual research hold admission hash drifted"
                    )
                continue
            if self._started(admission["id"]):
                self._hold(
                    holds, admission,
                    reason="started_without_owned_live_ticket",
                    ticket_ref=None,
                )
                continue
            try:
                ticket = self.launcher.start(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "reason": str(exc), "last": settled}
            except LaneChildRejected as exc:
                self._hold(
                    holds, admission, reason=str(exc), ticket_ref=None
                )
                continue
            pointer = {
                "ticket_ref": ticket["id"],
                "admission_ref": admission["id"],
                "admission_hash": admission["content_hash"],
            }
            write_owner_only(
                self.latest_path,
                {**pointer, "content_hash": content_hash(pointer)},
            )
            return {
                "status": "launched", "ticket_ref": ticket["id"],
                "admission_ref": admission["id"], "last": settled,
            }
        return {
            "status": "idle", "reason": "no unstarted annual research admission",
            "held": len(holds), "last": settled,
        }


def dispatch(server: Any, _params: Mapping[str, Any]) -> dict[str, Any]:
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionAnnualResearchCoordinator(
            store=server.store, launcher=launcher
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--mission-annual-research-lane", type=Path, default=None,
        help="Enable admission-ref-only annual research execution from this closed config.",
    )


def lane_configuration(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionAnnualResearchLaneError(
            "mission annual research lane config cannot be read"
        ) from exc
    if value != {"schema_version": "0.1", "enabled": True}:
        raise MissionAnnualResearchLaneError(
            "mission annual research lane config has an invalid closed shape"
        )
    return dict(value)


def build_launcher(args: Any) -> Any | None:
    path = getattr(args, "mission_annual_research_lane", None)
    if path is None:
        return None
    lane_configuration(path)
    required = {
        "candidate staging": getattr(args, "candidate_staging", None),
        "web fetch governance": getattr(args, "web_fetch_governance", None),
        "transcript spool": getattr(args, "transcript_spool_dir", None),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise MissionAnnualResearchLaneError(
            "mission annual research lane requires " + ", ".join(missing)
        )
    from .mission_annual_research_launcher import MissionAnnualResearchLauncher

    return MissionAnnualResearchLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        staging_path=required["candidate staging"],
        web_fetch_governance_path=required["web fetch governance"],
        spool_dir=required["transcript spool"],
        draft_model_config_path=getattr(
            args, "annual_report_draft_model_config", None
        ),
        verifier_model_config_path=getattr(
            args, "annual_report_verifier_model_config", None
        ),
    )


def argv_fragment(context: Any) -> list[str]:
    path = context.state / LANE_CONFIG
    return [] if not path.is_file() else ["--mission-annual-research-lane", str(path)]


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_annual_research",
    order=151,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="Execute exact mission annual-research admissions out of process; "
         "orphaned or failed admissions remain held without blind paid retry.",
))


__all__ = [
    "DRIVER_KEY", "LANE", "LANE_CONFIG", "LAUNCHER_KWARG",
    "MissionAnnualResearchCoordinator", "MissionAnnualResearchLaneError",
    "add_arguments", "argv_fragment", "build_launcher", "lane_configuration",
]
