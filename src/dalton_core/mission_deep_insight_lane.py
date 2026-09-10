"""P12d: one company's gate gets drafted and handed to a person, or nothing happens.

Queueless, like the dossier and debate-map lanes above it.  What needs doing is
derived every tick: a company that has passed its Initial Screen, has a dossier,
and has no gate draft waiting for a decision has something to draft, and nothing
else does.

**Silence is the resting state and it is cheap.**  The coordinator does not
plan -- the child does, and the child is the only thing that reads the dossier,
the debate map and the Playbook's twelve questions.  The coordinator's whole job
is to avoid spawning a child that will report ``nothing_new``: it keeps a
signature of the evidence (where each dossier chain's head is, where each debate
map's is, how many gate drafts and decisions exist) and does not launch again on
an unchanged signature after a run that did not submit anything -- a refusal is
as good a reason to stay quiet as an idle tick, because the same evidence will
be refused the same way and the four calls will be paid for again.  A signature
cannot disagree with the child, because it is not an opinion about what to do.

**A pending decision is part of the signature.**  That is what makes the whole
lane stop while the owner is thinking: the draft is on the chain, no decision
row exists, nothing about the evidence has moved, so the coordinator is quiet.
The moment the owner decides, the decision count moves and the lane looks again
-- which is exactly right for ``return_for_more_work`` and harmless for the
other two, because the child then finds the company blocked and says so.

One child at a time, settled on the following tick.  A child inspected in the
same breath it was spawned is always still running.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "deep_insight_gate_launcher"
# The only two outcomes that are a reason to look again on an unchanged
# signature.  Everything else -- idle, held, and every one of the refusal
# statuses -- means this exact evidence has already been tried and did not
# produce a submission, so trying it again would re-pay for four model calls to
# reach the same refusal.  Stated as the short list rather than the long one
# because the long one grows every time a refusal path is added, and the day
# somebody forgets to add a word is the day the lane burns the day's budget in
# a loop.
RELAUNCH_STATUSES = frozenset({"submitted", "duplicate"})
CONTENT_TERMINAL_STATUSES = frozenset({
    "classification_conflict", "verification_failed", "rubric_refused",
    "not_independent", "unverified", "no_new_evidence",
})


def permission_key(connection: Any, launcher: Any, signature: str) -> str:
    parts = [signature]
    try:
        rows = connection.execute(
            "SELECT mission_ref, mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref").fetchall()
        parts += [f"{row['mission_ref']}:{row['mission_version_id']}" for row in rows]
    except Exception:
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
    return f"{signature}|permission:{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]}"


def clear_obsolete_permissions(budget: Any, current: str, signature: str) -> None:
    for row in budget.permission_items():
        if row["item_key"] != current:
            budget.retire(row["item_key"])


def ledger_signature(connection: Any) -> str:
    """A cheap digest of everything that could give this lane something to do.

    Three reads: where each dossier chain's head is, where each debate map's
    head is, and how many gate drafts and decisions exist.  Deliberately not the
    plan -- a signature says "something moved", and only the child says what
    that means.
    """

    parts: list[str] = []
    for table, column in (("company_dossier_versions", "dossier_ref"),
                          ("debate_map_versions", "map_ref")):
        try:
            heads = connection.execute(
                f"SELECT {column} AS ref, MAX(version_number) AS v FROM {table} "
                f"GROUP BY {column} ORDER BY {column}"
            ).fetchall()
        except Exception:  # noqa: BLE001 - an absent authority is a valid state
            parts.append(f"{table}:none")
            continue
        parts += [f"{item['ref']}:{item['v']}" for item in heads] or [f"{table}:empty"]
    for table in ("deep_insight_gate_versions", "deep_insight_gate_decisions"):
        try:
            row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            parts.append(f"{table}:{row['n']}")
        except Exception:  # noqa: BLE001 - the gate authority may not be open yet
            parts.append(f"{table}:none")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class MissionDeepInsightLaneCoordinator:
    """Launch and settle the Deep Insight Gate lane."""

    def __init__(self, *, connection: Any, launcher: Any,
                 failure_ledger_dir: Any | None = None,
                 failure_clock: Any | None = None) -> None:
        self.connection = connection
        self.launcher = launcher
        self._open: str | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed.  Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have fixed
        # it.
        self._quiet_signature: str | None = None
        self.budget = lane_budget("deep_insight_gate", state_dir=failure_ledger_dir,
                                  clock=failure_clock)

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
            "gate_status": summary.get("gate_status"),
            "version_ref": summary.get("version_ref"),
            "answered": summary.get("answered"),
            "unknown": summary.get("unknown"),
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
        status = str(settled.get("gate_status") or "")
        if status in {"not_authorized", "no_checkpoint", "no_policy"} and signature:
            self.budget.record(
                permission_key(self.connection, self.launcher, str(signature)),
                status="gated:not permitted", reason=f"gated:not permitted: {status}")
        elif settled.get("status") not in ("succeeded", "orphaned"):
            if signature:
                decision = self.budget.record_settled(str(signature), settled)
                if decision.action == "not_permitted":
                    self.budget.clear(str(signature))
                    self.budget.record(
                        permission_key(self.connection, self.launcher, str(signature)),
                        classification=decision.classification)
        elif status in CONTENT_TERMINAL_STATUSES and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
        elif status not in RELAUNCH_STATUSES and signature:
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
            signature = ledger_signature(self.connection)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if signature == self._quiet_signature:
            return {"status": "idle", "settled": settled, "signature": signature,
                    "reason": "nothing has moved since the last run found nothing"}
        permission = permission_key(self.connection, self.launcher, signature)
        clear_obsolete_permissions(self.budget, permission, signature)
        held = (self.budget.blocked(permission)
                or self.budget.blocked(signature))
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
    """Controller tick (P12d): one company's twelve answers, or silence."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no deep-insight-gate lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionDeepInsightLaneCoordinator(
            connection=server.store.connection, launcher=launcher,
            failure_ledger_dir=getattr(launcher, "state_dir", None))
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    from pathlib import Path as _Path

    parser.add_argument(
        "--deep-insight-gate-model-config", type=_Path, default=None,
        help="Model configuration the gate drafts with. Omit and the lane is "
             "absent: every one of the twelve answers is written prose.",
    )
    parser.add_argument(
        "--deep-insight-gate-policy", type=_Path, default=None,
        help="The Constitution's output_rubric bindings, as a pre-publish "
             "structural check. Shares P12a's policy file: the criteria belong "
             "to the Constitution rather than to either document.",
    )
    parser.add_argument(
        "--deep-insight-gate-verifier-model-config", type=_Path, default=None,
        help="The configuration the independent verifier runs on. Without it "
             "the child holds: one configuration routes both calls the same "
             "way, so the verdict could never be shown to be independent.",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "deep_insight_gate_model_config", None) is None:
        return None
    from .deep_insight_gate_launcher import DeepInsightGateLauncher
    from pathlib import Path as _Path

    return DeepInsightGateLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.deep_insight_gate_model_config,
        verifier_model_config_path=getattr(
            args, "deep_insight_gate_verifier_model_config", None),
        scheduler_db=getattr(args, "scheduler", None),
        policy_path=getattr(args, "deep_insight_gate_policy", None),
    )


# What this lane needs on disk before it is worth turning on: the drafting
# configuration (the same file the dossier and the Initial Screen draft with --
# same route, same broker, same day ledger), the policy that carries the
# Constitution's output-rubric bindings, and the verifier's own configuration,
# without which nothing can be submitted.
GATE_MODEL_CONFIG = "initial-screen-model-config.json"
GATE_VERIFIER_MODEL_CONFIG = "dossier-verifier-model-config.json"
GATE_POLICY = "p12a-dossier-policy-v1.json"


def argv_fragment(context: Any) -> list[str]:
    # Gated on all three files, the verifier's included.  Without a separate
    # verifier configuration the child holds on every tick without drafting
    # anything (D2: one configuration routes both calls the same way, so the
    # verdict could never be shown to be independent), and a lane switched on to
    # report the same refusal for ever is worse than a lane that is off.  The
    # policy path is passed through because its default only resolves inside a
    # source checkout.
    config = context.state / GATE_MODEL_CONFIG
    policy = context.state / GATE_POLICY
    verifier = context.state / GATE_VERIFIER_MODEL_CONFIG
    if not (config.is_file() and policy.is_file() and verifier.is_file()):
        return []
    return ["--deep-insight-gate-model-config", str(config),
            "--deep-insight-gate-policy", str(policy),
            "--deep-insight-gate-verifier-model-config", str(verifier)]


LANE = register_lane(LaneSpec(
    operation="dispatch_deep_insight_gate",
    # 138, immediately after the dossier at 137 and the debate map at 135: the
    # gate reads both of them, so it runs after both have had their tick.
    order=138,
    driver_key="deep_insight_gate",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12d: the Playbook's twelve Deep Insight Gate questions, answered from "
         "the dossier, the debate map and the numbers, one bounded call per "
         "question group, and submitted to the owner's checkpoint. Automation "
         "never decides the gate.",
))


__all__ = [
    "GATE_MODEL_CONFIG",
    "GATE_POLICY",
    "GATE_VERIFIER_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "RELAUNCH_STATUSES",
    "MissionDeepInsightLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "ledger_signature",
]
