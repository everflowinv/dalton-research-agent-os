"""Schedule one strict Investment Memo candidate at a time."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .lane_child_launcher import LaneChildConflict, LaneChildRejected, LaneChildTicketNotFound
from .lane_failure_ledger import lane_budget
from .lane_registry import LaneSpec, register_lane

LAUNCHER_KWARG = "investment_memo_launcher"
MODEL_CONFIG = "dossier-model-config.json"
LEGACY_MODEL_CONFIG = "initial-screen-model-config.json"
VERIFIER_CONFIG = "company-dossier-verifier-model-config.json"
LEGACY_VERIFIER_CONFIG = "dossier-verifier-model-config.json"
QUIET = frozenset({"no_company", "nothing_new"})
TERMINAL = frozenset({"draft_refused", "authority_validation_failed",
                      "deterministic_gate_failed", "verification_failed"})


def ledger_signature(connection: Any, launcher: Any) -> str:
    parts = []
    for table in ("coverage_mission_pointer", "coverage_mission_versions", "coverage_mission_stage_records",
                  "claim_versions", "mission_deliverable_versions", "company_dossier_versions",
                  "deep_insight_gate_versions", "forecast_model_versions", "sensitivity_projections",
                  "valuation_snapshot_versions"):
        try:
            row = connection.execute(f"SELECT COUNT(*) n,MAX(rowid) newest FROM {table}").fetchone()
            parts.append(f"{table}:{row['n']}:{row['newest']}")
        except Exception:
            parts.append(f"{table}:missing")
    for path in (launcher.model_config_path, launcher.verifier_model_config_path):
        try:
            parts.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except OSError:
            parts.append("config:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class MissionInvestmentMemoLaneCoordinator:
    def __init__(self, *, connection: Any, launcher: Any) -> None:
        self.connection, self.launcher = connection, launcher
        self.open_ticket: str | None = None
        self.quiet_signature: str | None = None
        self.budget = lane_budget("investment_memo", state_dir=launcher.state_dir)

    def dispatch_once(self) -> dict[str, Any]:
        settled = None
        if self.open_ticket:
            try:
                ticket = self.launcher.status(self.open_ticket)
            except LaneChildTicketNotFound:
                ticket = {"status": "orphaned", "summary": {}}
            if ticket.get("status") == "running":
                return {"status": "running", "ticket_ref": self.open_ticket}
            settled = {"status": ticket.get("status"), **(ticket.get("summary") or {})}
            signature = str(ticket.get("signature") or ledger_signature(self.connection, self.launcher))
            self.open_ticket = None
            memo_status = settled.get("memo_status")
            if memo_status in QUIET:
                self.quiet_signature = signature
            elif memo_status in TERMINAL:
                self.budget.record(signature, status=f"content_refused:{memo_status}",
                                   reason=str(settled.get("reason") or memo_status))
            elif settled.get("status") == "succeeded":
                self.budget.clear(signature)
            else:
                self.budget.record_settled(signature, settled)
        signature = ledger_signature(self.connection, self.launcher)
        if signature == self.quiet_signature:
            return {"status": "idle", "signature": signature, "settled": settled}
        held = self.budget.blocked(signature)
        if held:
            return {"status": held.action, "reason": held.classification.reason,
                    "signature": signature, "settled": settled}
        try:
            ticket = self.launcher.start(signature=signature)
        except LaneChildConflict as exc:
            return {"status": "busy", "reason": str(exc), "settled": settled}
        except LaneChildRejected as exc:
            return {"status": "rejected", "reason": str(exc), "settled": settled}
        self.open_ticket = ticket["id"]
        return {"status": "launched", "ticket_ref": ticket["id"], "signature": signature,
                "settled": settled}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    del params
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionInvestmentMemoLaneCoordinator(connection=server.store.connection,
                                                             launcher=launcher)
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument("--investment-memo-model-config", type=Path)
    parser.add_argument("--investment-memo-verifier-model-config", type=Path)


def build_launcher(args: Any) -> Any | None:
    if not getattr(args, "investment_memo_model_config", None) or not getattr(args, "investment_memo_verifier_model_config", None):
        return None
    from .investment_memo_launcher import InvestmentMemoLauncher
    return InvestmentMemoLauncher(state_dir=Path(args.db).expanduser().resolve().parent,
        model_config_path=args.investment_memo_model_config,
        verifier_model_config_path=args.investment_memo_verifier_model_config,
        scheduler_db=getattr(args, "scheduler", None))


def argv_fragment(context: Any) -> list[str]:
    producer = context.state / MODEL_CONFIG
    if not producer.is_file(): producer = context.state / LEGACY_MODEL_CONFIG
    verifier = context.state / VERIFIER_CONFIG
    if not verifier.is_file(): verifier = context.state / LEGACY_VERIFIER_CONFIG
    if not producer.is_file() or not verifier.is_file(): return []
    return ["--investment-memo-model-config", str(producer),
            "--investment-memo-verifier-model-config", str(verifier)]


LANE = register_lane(LaneSpec(operation="dispatch_investment_memo", order=142,
    driver_key="investment_memo", handler=dispatch, init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments, launcher_factory=build_launcher, argv_fragment=argv_fragment,
    note="Four-group Investment Memo draft plus complete independent verification."))

__all__ = ["LANE", "MissionInvestmentMemoLaneCoordinator", "argv_fragment", "ledger_signature"]
