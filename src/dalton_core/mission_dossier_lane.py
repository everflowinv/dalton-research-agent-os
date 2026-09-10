"""P12a: one company's file gets a little deeper each tick, or nothing happens.

Queueless, like the specification and driver-model lanes. What needs doing is
derived from the Ledger every tick: a company that has passed its Initial
Screen and whose canonical Claims have moved since its last dossier version
has something to draft, and nothing else does.

**Silence is the resting state and it is cheap.** The coordinator does not
plan -- the child does, and the child is the only thing that reads the aspect
index, the Constitution and the policy. The coordinator's whole job is to
avoid spawning a child that will report ``nothing_new``: it keeps a signature
of the evidence (how many Claims, the newest one, the head of each dossier
chain) and does not launch again on an unchanged signature after a run that
found nothing. The lesson from the model-specification lane is that a
coordinator which re-derives the child's choice will eventually disagree with
it and hand back the same company forever; a signature cannot disagree,
because it is not an opinion about what to do.

One child at a time, settled on the following tick. A child inspected in the
same breath it was spawned is always still running.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "company_dossier_launcher"
# Statuses that mean "this run looked and found nothing to do". After one of
# these, an unchanged signature is a reason to stay quiet.
QUIET_STATUSES = frozenset({"nothing_new", "no_screened_company", "no_mission",
                            "no_claim_index"})
CONTENT_TERMINAL_STATUSES = frozenset({
    "verification_failed", "rubric_refused", "constitution_refused",
    "not_independent", "no_new_evidence",
})


def permission_key(connection: Any, launcher: Any, signature: str) -> str:
    """Bind a permission refusal to the control state, not new business input."""
    parts = [signature]
    try:
        rows = connection.execute(
            "SELECT mission_ref, mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref").fetchall()
        parts += [f"{row['mission_ref']}:{row['mission_version_id']}" for row in rows]
    except Exception:  # noqa: BLE001 - no mission is itself a control state
        parts.append("mission:none")
    try:
        rows = connection.execute(
            "SELECT pointer_id, policy_version_id FROM governance_policy_pointer "
            "ORDER BY pointer_id").fetchall()
        parts += [f"policy:{row['pointer_id']}:{row['policy_version_id']}" for row in rows]
    except Exception:
        parts.append("policy:none")
    for name, value in sorted(vars(launcher).items()):
        if not any(word in name for word in ("config", "policy")) or value is None:
            continue
        path = Path(value)
        try:
            parts.append(f"{name}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
        except OSError:
            parts.append(f"{name}:missing")
    return f"{signature}|permission:v2:{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]}"


def clear_obsolete_permissions(budget: Any, current: str, signature: str) -> None:
    for row in budget.permission_items():
        if row["item_key"] != current:
            budget.retire(row["item_key"])


def ledger_signature(connection: Any) -> str:
    """A cheap digest of everything that could give this lane something to do.

    Three reads: how many Claims exist and which is newest, and where each
    dossier chain's head is. Deliberately not the plan -- a signature says
    "something moved", and only the child says what that means.
    """

    from .cockpit_model import verifier_provider_contract_fingerprint
    row = connection.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM claim_versions"
    ).fetchone()
    parts = [str(row["n"]), str(row["newest"] or "-"),
             verifier_provider_contract_fingerprint("dossier_verifier")]
    try:
        heads = connection.execute(
            "SELECT dossier_ref, MAX(version_number) AS v "
            "FROM company_dossier_versions GROUP BY dossier_ref ORDER BY dossier_ref"
        ).fetchall()
        parts += [f"{item['dossier_ref']}:{item['v']}" for item in heads]
    except Exception:  # noqa: BLE001 - no dossier table yet is a valid state
        parts.append("no-dossiers")
    try:
        entries = connection.execute(
            "SELECT COUNT(*) AS n FROM claim_index_entry_versions"
        ).fetchone()
        parts.append(f"index:{entries['n']}")
    except Exception:  # noqa: BLE001 - no index yet is a valid state
        parts.append("index:none")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class MissionDossierLaneCoordinator:
    """Launch and settle the dossier lane."""

    def __init__(self, *, connection: Any, launcher: Any,
                 failure_ledger_dir: Any | None = None,
                 failure_clock: Callable[[], Any] | None = None) -> None:
        self.connection = connection
        self.launcher = launcher
        self._open: str | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed. Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have
        # fixed it.
        self._quiet_signature: str | None = None
        probe_interval = (
            launcher.capacity_probe_interval_seconds()
            if hasattr(launcher, "capacity_probe_interval_seconds") else None
        )
        kwargs = {} if probe_interval is None else {
            "probe_interval_seconds": probe_interval}
        self.budget = lane_budget("company_dossier", state_dir=failure_ledger_dir,
                                  clock=failure_clock, **kwargs)

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now; try again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            "signature": ticket.get("signature"),
            "company_ref": summary.get("company_ref") or ticket.get("company_ref"),
            "dossier_status": summary.get("dossier_status"),
            "version_ref": summary.get("version_ref"),
            "units_drafted": summary.get("units_drafted"),
            "new_refs": summary.get("new_refs"),
            "cost_micros": summary.get("cost_micros"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        signature = settled.get("signature")
        status = str(settled.get("dossier_status") or "")
        if status == "not_authorized" and signature:
            self.budget.record(str(signature),
                               status="gated:not permitted",
                               reason="gated:mission does not grant dossier")
        elif settled.get("status") != "succeeded" and settled.get("status") != "orphaned":
            if signature:
                self.budget.record_settled(str(signature), settled)
        elif status in CONTENT_TERMINAL_STATUSES and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
        elif status in QUIET_STATUSES and signature:
            settled["resumed"] = self.budget.clear(str(signature))
            self._quiet_signature = str(signature)
        elif signature:
            self.budget.clear(str(signature))
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        try:
            # Bind ticket identity before launch. A child may finish after a
            # new mission is signed; its refusal belongs to its launch state.
            signature = permission_key(
                self.connection, self.launcher, ledger_signature(self.connection))
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if signature == self._quiet_signature:
            return {"status": "idle", "settled": settled, "signature": signature,
                    "reason": "nothing has moved since the last run found nothing"}
        clear_obsolete_permissions(self.budget, signature, signature)
        held = self.budget.blocked(signature)
        if held is not None:
            return {"status": held.action, "settled": settled, "signature": signature,
                    "reason": held.classification.reason,
                    "failure_budget": self.budget.summary()}
        try:
            ticket = self.launcher.start(signature=signature)
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {"status": "launched", "ticket_ref": ticket["id"],
                "signature": signature, "settled": settled}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P12a): one company's file, a few sections at a time."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no company-dossier lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionDossierLaneCoordinator(
            connection=server.store.connection, launcher=launcher,
            failure_ledger_dir=getattr(launcher, "state_dir", None))
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    from pathlib import Path as _Path

    parser.add_argument(
        "--company-dossier-model-config", type=_Path, default=None,
        help="Model configuration the dossier drafts with. Omit and the lane "
             "is absent: every part of a dossier is written prose.",
    )
    parser.add_argument(
        "--company-dossier-policy", type=_Path, default=None,
        help="P12a policy: the causal-chain section map and the Constitution's "
             "output_rubric bindings.",
    )
    parser.add_argument(
        "--company-dossier-verifier-model-config", type=_Path, default=None,
        help="The configuration the independent verifier runs on. Without it "
             "the child holds: one configuration routes both calls the same "
             "way, so the verdict could never be shown to be independent.",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "company_dossier_model_config", None) is None:
        return None
    from pathlib import Path as _Path

    from .company_dossier_launcher import CompanyDossierLauncher

    return CompanyDossierLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.company_dossier_model_config,
        verifier_model_config_path=getattr(
            args, "company_dossier_verifier_model_config", None),
        scheduler_db=getattr(args, "scheduler", None),
        policy_path=getattr(args, "company_dossier_policy", None),
    )


# What this lane needs on disk before it is worth turning on: the drafting
# independent drafting configuration (with the old shared Initial Screen path
# supported for existing installations), the policy mapping the constitution's causal
# chain to two of the sections, and the verifier's own configuration, without
# which nothing can be published.
DOSSIER_MODEL_CONFIG = "dossier-model-config.json"
DOSSIER_VERIFIER_MODEL_CONFIG = "company-dossier-verifier-model-config.json"
LEGACY_DOSSIER_MODEL_CONFIG = "initial-screen-model-config.json"
LEGACY_DOSSIER_VERIFIER_MODEL_CONFIG = "dossier-verifier-model-config.json"
DOSSIER_POLICY = "p12a-dossier-policy-v1.json"


def argv_fragment(context: Any) -> list[str]:
    # Gated on what this lane itself needs, not on the extraction model: the
    # dossier does not extract anything, and an installation with an extraction
    # model and no dossier policy would have had the lane on and holding every
    # tick. Both files must be present, and the policy path is passed through:
    # its default only resolves inside a source checkout.
    config = context.state / DOSSIER_MODEL_CONFIG
    if not config.is_file():
        config = context.state / LEGACY_DOSSIER_MODEL_CONFIG
    policy = context.state / DOSSIER_POLICY
    if not config.is_file() or not policy.is_file():
        return []
    argv = ["--company-dossier-model-config", str(config),
            "--company-dossier-policy", str(policy)]
    verifier = context.state / DOSSIER_VERIFIER_MODEL_CONFIG
    if not verifier.is_file():
        verifier = context.state / LEGACY_DOSSIER_VERIFIER_MODEL_CONFIG
    if verifier.is_file():
        argv += ["--company-dossier-verifier-model-config", str(verifier)]
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_company_dossier",
    # 137, between the crowd-source feed and the research-task lane. The
    # debate map takes 135: it reads the dossier, so it runs after it.
    order=137,
    driver_key="company_dossier",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12a: the ten-section company file, drafted from the canonical Claims "
         "the aspect index groups, a few sections a tick, and published only "
         "when it cites something the last version did not.",
))


__all__ = [
    "DOSSIER_MODEL_CONFIG",
    "LEGACY_DOSSIER_MODEL_CONFIG",
    "LEGACY_DOSSIER_VERIFIER_MODEL_CONFIG",
    "DOSSIER_POLICY",
    "DOSSIER_VERIFIER_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "QUIET_STATUSES",
    "MissionDossierLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "ledger_signature",
]
