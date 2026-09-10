"""Schedule one strict Investment Memo candidate at a time."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

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
                      "deterministic_gate_failed"})


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


def company_signature(frozen: Mapping[str, Any], launcher: Any) -> str:
    """Bind a memo attempt to its exact frozen company input and model pair."""

    from .store import content_hash

    mission = frozen["mission"]
    playbook = frozen["playbook"]
    payload = {
        "schema_version": "0.1",
        "company_ref": frozen["company"]["company_ref"],
        "mission": {"ref": mission["id"], "hash": mission["content_hash"]},
        "playbook": {"ref": playbook["id"], "hash": playbook["content_hash"]},
        "input_bindings": frozen["input_bindings"],
    }
    parts = [content_hash(payload)]
    for path in (launcher.model_config_path, launcher.verifier_model_config_path):
        try:
            parts.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except OSError:
            parts.append("config:missing")
    return f"{payload['company_ref']}|{hashlib.sha256('|'.join(parts).encode()).hexdigest()}"


def _verification_is_content_refusal(settled: Mapping[str, Any]) -> bool:
    verification = settled.get("verification") or {}
    return (verification.get("status") == "verified"
            and verification.get("verdict") == "reject")


def _settled_for_failure_classification(settled: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(settled)
    verification = settled.get("verification") or {}
    normalized["failure_reason"] = (
        settled.get("reason") or verification.get("reason")
        or settled.get("failure_reason") or f"last run: {settled.get('status')}"
    )
    return normalized


class MissionInvestmentMemoLaneCoordinator:
    def __init__(self, *, connection: Any, launcher: Any,
                 companies: Callable[[], list[str]] | None = None,
                 frozen_input: Callable[[str], Mapping[str, Any]] | None = None) -> None:
        self.connection, self.launcher = connection, launcher
        self.companies = companies
        self.frozen_input = frozen_input
        self.open_ticket: str | None = None
        self.quiet_signatures: set[str] = set()
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
                self.quiet_signatures.add(signature)
            elif memo_status in TERMINAL or _verification_is_content_refusal(settled):
                self.budget.record(signature, status=f"content_refused:{memo_status}",
                                   reason=str(settled.get("reason") or memo_status))
            elif settled.get("status") == "succeeded":
                self.budget.clear(signature)
            else:
                self.budget.record_settled(
                    signature, _settled_for_failure_classification(settled))
        if self.companies is None or self.frozen_input is None:
            candidates = [(None, ledger_signature(self.connection, self.launcher), None)]
        else:
            candidates = []
            skipped = {}
            for company_ref in self.companies():
                frozen = self.frozen_input(company_ref)
                if frozen.get("status") != "ready":
                    skipped[company_ref] = str(frozen.get("reason") or frozen.get("status"))
                    continue
                candidates.append((company_ref, company_signature(frozen, self.launcher), frozen))
        held_companies = {}
        for company_ref, signature, _frozen in candidates:
            if company_ref is not None:
                for item in self.budget.permission_items():
                    if (str(item["item_key"]).startswith(f"{company_ref}|")
                            and item["item_key"] != signature):
                        self.budget.retire(item["item_key"])
            if signature in self.quiet_signatures:
                continue
            held = self.budget.blocked(signature)
            if held:
                held_companies[str(company_ref or "-")] = held.classification.reason
                continue
            try:
                ticket = self.launcher.start(signature=signature, company_ref=company_ref)
            except LaneChildConflict as exc:
                return {"status": "busy", "reason": str(exc), "settled": settled}
            except LaneChildRejected as exc:
                return {"status": "rejected", "reason": str(exc), "settled": settled}
            self.open_ticket = ticket["id"]
            return {"status": "launched", "ticket_ref": ticket["id"],
                    "company_ref": company_ref, "signature": signature,
                    "held": held_companies, "settled": settled}
        if held_companies:
            return {"status": "held", "held": held_companies, "settled": settled,
                    "reason": "; ".join(f"{key}: {value}"
                                         for key, value in held_companies.items())}
        return {"status": "idle", "settled": settled,
                "skipped": skipped if self.companies is not None else {},
                "reason": "no eligible company memo input changed"}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    del params
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .investment_memo_cli import collect_frozen_input

        def companies() -> list[str]:
            rows = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref"
            ).fetchall()
            if len(rows) != 1:
                return []
            mission = server.coverage_mission.mission(rows[0]["mission_version_id"])
            return [str(member["company_ref"]) for member in mission["universe"]]

        coordinator = MissionInvestmentMemoLaneCoordinator(connection=server.store.connection,
            launcher=launcher, companies=companies,
            frozen_input=lambda company_ref: collect_frozen_input(
                server.store, company_ref=company_ref))
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


LANE = register_lane(LaneSpec(operation="dispatch_investment_memo", order=143,
    driver_key="investment_memo", handler=dispatch, init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments, launcher_factory=build_launcher, argv_fragment=argv_fragment,
    note="Four-group Investment Memo draft plus complete independent verification."))

__all__ = ["LANE", "MissionInvestmentMemoLaneCoordinator", "argv_fragment",
           "company_signature", "ledger_signature"]
