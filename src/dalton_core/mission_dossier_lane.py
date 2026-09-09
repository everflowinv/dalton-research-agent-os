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
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "company_dossier_launcher"
# Statuses that mean "this run looked and found nothing to do". After one of
# these, an unchanged signature is a reason to stay quiet.
QUIET_STATUSES = frozenset({"nothing_new", "no_screened_company", "no_mission",
                            "no_claim_index", "not_authorized"})


def ledger_signature(connection: Any) -> str:
    """A cheap digest of everything that could give this lane something to do.

    Three reads: how many Claims exist and which is newest, and where each
    dossier chain's head is. Deliberately not the plan -- a signature says
    "something moved", and only the child says what that means.
    """

    row = connection.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM claim_versions"
    ).fetchone()
    parts = [str(row["n"]), str(row["newest"] or "-")]
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

    def __init__(self, *, connection: Any, launcher: Any) -> None:
        self.connection = connection
        self.launcher = launcher
        self._open: str | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed. Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have
        # fixed it.
        self._quiet_signature: str | None = None
        self._failed: dict[str, str] = {}

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
        if settled.get("status") != "succeeded" and settled.get("status") != "orphaned":
            if signature:
                self._failed[str(signature)] = (
                    settled.get("failure_reason") or f"last run: {settled.get('status')}")
        elif status in QUIET_STATUSES and signature:
            self._quiet_signature = str(signature)
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        try:
            signature = ledger_signature(self.connection)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if signature == self._quiet_signature:
            return {"status": "idle", "settled": settled, "signature": signature,
                    "reason": "nothing has moved since the last run found nothing"}
        held = self._failed.get(signature)
        if held is not None:
            return {"status": "held", "settled": settled, "signature": signature,
                    "reason": held}
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
            connection=server.store.connection, launcher=launcher)
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


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "company_dossier_model_config", None) is None:
        return None
    from pathlib import Path as _Path

    from .company_dossier_launcher import CompanyDossierLauncher

    return CompanyDossierLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.company_dossier_model_config,
        scheduler_db=getattr(args, "scheduler", None),
        policy_path=getattr(args, "company_dossier_policy", None),
    )


# The drafting configuration this lane uses when the installer has written
# one; the same file the Initial Screen drafts with, because it is the same
# route, broker and day ledger.
DOSSIER_MODEL_CONFIG = "initial-screen-model-config.json"


def argv_fragment(context: Any) -> list[str]:
    # Every lane's fragment is gated on the thing that lane needs being on
    # disk. This one needs a drafting model: without it the child can plan and
    # nothing else, and a lane that can only report "gated" every tick is a
    # lane that should be off.
    if context.extraction_model_config_path is None:
        return []
    config = context.state / DOSSIER_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--company-dossier-model-config", str(config)]


LANE = register_lane(LaneSpec(
    operation="dispatch_company_dossier",
    order=135,
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
