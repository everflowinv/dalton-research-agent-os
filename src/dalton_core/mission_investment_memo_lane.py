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
CONTENT_REFUSAL_HOLD_SECONDS = 24 * 60 * 60
REASON_LIMIT = 200


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

    from .cockpit_model import verifier_provider_contract_fingerprint
    from .investment_memo_cli import anchor_bindings
    from .investment_memo_draft import MEMO_PROMPT_CONTRACT_VERSION
    from .investment_memo_timeout_recovery import CONTRACT_REF as TIMEOUT_CONTINUATION_REF
    from .investment_memo_macro_accounting import CONTRACT_REF as MACRO_ACCOUNTING_REF
    from .investment_memo_legacy_accounting import CONTRACT_REF as LEGACY_ACCOUNTING_REF
    from .product_passage_validation import PRODUCT_VALIDATION_CONTRACT
    from .mission_deliverable import SECTION_NUMBERS_VALIDATION_CONTRACT
    from .store import content_hash

    mission = frozen["mission"]
    playbook = frozen["playbook"]
    payload = {
        "schema_version": "0.1",
        "company_ref": frozen["company"]["company_ref"],
        "mission": {"ref": mission["id"], "hash": mission["content_hash"]},
        "playbook": {"ref": playbook["id"], "hash": playbook["content_hash"]},
        # The anchors only (ADR-0011): new claims, prices and events arrive
        # daily and must not make yesterday's refusal look like new work.
        "input_bindings": [
            {"kind": kind, "ref": ref, "hash": digest}
            for kind, ref, digest in anchor_bindings(frozen["input_bindings"])],
        # The drafting rules too: a draft refused under an old prompt contract
        # must not stay held for a day after the contract that refused it is
        # fixed (same rule as the gate's GATE_DRAFTING_CONTRACT).
        "drafting_contract": MEMO_PROMPT_CONTRACT_VERSION,
        "timeout_continuation_contract": TIMEOUT_CONTINUATION_REF,
        "validation_contract": PRODUCT_VALIDATION_CONTRACT,
        # Compatible validation change: wake the lane, while the draft prompt,
        # Work identity, existing paid answers and one repair slot stay bound.
        "section_numbers_validation_contract": SECTION_NUMBERS_VALIDATION_CONTRACT,
        "macro_accounting_contract": MACRO_ACCOUNTING_REF,
        "legacy_accounting_contract": LEGACY_ACCOUNTING_REF,
    }
    parts = [content_hash(payload), verifier_provider_contract_fingerprint(
        "investment_memo_verifier")]
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
        # A content refusal holds the company for a day, not for ever: the
        # signature binds only the memo's anchors, so without a bound a refused
        # memo would wait for the next filing to be tried again.
        self.budget = lane_budget("investment_memo", state_dir=launcher.state_dir,
                                  block_ttl_seconds=CONTENT_REFUSAL_HOLD_SECONDS)

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
            elif memo_status == "macro_accounting_pending":
                from .investment_memo_accounting_progress import resume_checkpoint
                try:
                    resume_checkpoint(ticket.get("summary"), ticket,
                                      self.launcher.scheduler_db or self.launcher.state_dir / "scheduler.sqlite")
                except (ValueError, OSError) as exc:
                    return {"status":"held","reason":"memo accounting progress is unproved: "+str(exc),"settled":settled}
                # Free persistent paging must remain eligible on the next
                # normal tick, including after a writer restart.
            elif memo_status == "quiesced":
                from .investment_memo_quiesce import quiesced_summary_matches
                if not quiesced_summary_matches(ticket.get("summary"), ticket):
                    return {"status": "held", "reason": "memo pause proof is invalid", "settled": settled}
                # A deployment pause is not a content/fee failure. The next
                # normal tick resumes via the launcher's exact successor.
            elif memo_status in TERMINAL or _verification_is_content_refusal(settled):
                self.budget.record(signature, status=f"content_refused:{memo_status}",
                                   reason=str(settled.get("reason") or memo_status))
            elif settled.get("status") == "succeeded":
                self.budget.clear(signature)
            else:
                self.budget.record_settled(
                    signature, _settled_for_failure_classification(settled))
        skipped = {}
        if self.companies is None or self.frozen_input is None:
            candidates = [(None, ledger_signature(self.connection, self.launcher), None)]
        else:
            candidates = []
            for company_ref in self.companies():
                frozen = self.frozen_input(company_ref)
                if not isinstance(frozen, Mapping):
                    skipped[company_ref] = "frozen input unavailable"
                    continue
                if frozen.get("status") != "ready":
                    skipped[company_ref] = str(frozen.get("reason") or frozen.get("status"))
                    continue
                candidates.append((company_ref, company_signature(frozen, self.launcher), frozen))
        held_companies = {}
        rejected_companies = {}
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
                return {"status": "busy", "reason": str(exc), "settled": settled,
                        **({"rejected": rejected_companies} if rejected_companies else {})}
            except LaneChildRejected as exc:
                if company_ref is None:
                    return {"status": "rejected", "reason": str(exc), "settled": settled}
                # A company's unproved pause/configuration must remain refused,
                # but does not occupy the shared child slot for other companies.
                rejected_companies[company_ref] = str(exc)[:REASON_LIMIT]
                continue
            self.open_ticket = ticket["id"]
            return {"status": "launched", "ticket_ref": ticket["id"],
                    "company_ref": company_ref, "signature": signature,
                    "held": held_companies, "settled": settled,
                    **({"rejected": rejected_companies} if rejected_companies else {})}
        if rejected_companies:
            return {"status": "rejected", "rejected": rejected_companies,
                    "held": held_companies, "settled": settled,
                    "reason": "; ".join(f"{key}: {value}" for key, value
                                        in rejected_companies.items())[:REASON_LIMIT]}
        if held_companies:
            return {"status": "held", "held": held_companies, "settled": settled,
                    "reason": "; ".join(f"{key}: {value}"
                                         for key, value in held_companies.items())}
        reason = "no eligible company memo input changed"
        if skipped:
            # Say what each company is waiting on, in the owner's words: the
            # stage it is still in, or the input its memo still lacks.
            waits = list(dict.fromkeys(str(value) for value in skipped.values()))
            reason = ("投资备忘录等待：" + "；".join(waits))[:REASON_LIMIT]
        return {"status": "idle", "settled": settled,
                "skipped": skipped if self.companies is not None else {},
                "reason": reason}


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
    failure_ledger="investment_memo",
    driver_key="investment_memo", handler=dispatch, init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments, launcher_factory=build_launcher, argv_fragment=argv_fragment,
    note="Four-group Investment Memo draft plus complete independent verification."))

__all__ = ["LANE", "MissionInvestmentMemoLaneCoordinator", "argv_fragment",
           "company_signature", "ledger_signature"]
