"""P12e: the industry's framework gets a little deeper each week, or nothing happens.

Queueless, like the dossier and specification lanes.  What needs doing is
derived from the Ledger every tick, and this lane's signature is the one place
in the cognition layer that watches *filings* as well as Claims -- because this
deliverable's central table is computed from filed statements, and a newly
filed quarter changes what the framework says without a single Claim arriving.
A signature that only counted Claims would have kept this lane silent through
every earnings season.

**Weekly, not per-event.**  The coordinator holds a minimum interval between
launches, because an industry framework that is redrafted the moment any one of
five companies files is a document nobody can read a version chain of -- and
the blueprint asks for weekly.  Between those, an explicit run (``--revise``)
is the door.

**Silence is the resting state and it is cheap.**  The coordinator does not
plan; the child does, and the child is the only thing that reads the aspect
index, the Constitution, the driver pack and the policy.  The coordinator's
whole job is to avoid spawning a child that will report ``nothing_new``: it
keeps a signature of the evidence and does not launch again on an unchanged
signature after a run that found nothing.  A signature cannot disagree with the
child, because it is not an opinion about what to do.

One child at a time, settled on the following tick.  A child inspected in the
same breath it was spawned is always still running.
"""

from __future__ import annotations

import hashlib
import time
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
LAUNCHER_KWARG = "industry_framework_launcher"
# The blueprint's cadence for this deliverable, as seconds. Held by the
# coordinator rather than by the scheduler because the scheduler's tick is what
# drives every lane and this is the only one that wants to be slower than it.
MIN_INTERVAL_SECONDS = 7 * 24 * 60 * 60
# Statuses that mean "this run looked and found nothing to do". After one of
# these, an unchanged signature is a reason to stay quiet.
QUIET_STATUSES = frozenset({
    "nothing_new", "no_mission",
    "causal_chain_unmapped", "no_driver_pack",
})
CONTENT_TERMINAL_STATUSES = frozenset({
    "rubric_refused", "constitution_refused", "not_independent",
    "unverified", "no_new_evidence",
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

    Four reads and one of them is the point: how many statement lines have been
    filed. The framework's comparison table is computed from them, so a new
    quarter is new evidence for this deliverable even in a week when nobody
    wrote a Claim -- and that is the ordinary case, not the exception.
    """

    parts: list[str] = []
    row = connection.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM claim_versions"
    ).fetchone()
    parts += [str(row["n"]), str(row["newest"] or "-")]
    for sql, label in (
        ("SELECT COUNT(*) AS n FROM statement_ingest_lines", "lines"),
        ("SELECT COUNT(*) AS n FROM company_dossier_versions", "dossiers"),
        ("SELECT COUNT(*) AS n FROM debate_map_versions", "debates"),
    ):
        try:
            parts.append(f"{label}:{connection.execute(sql).fetchone()['n']}")
        except Exception:  # noqa: BLE001 - an absent authority is a valid state
            parts.append(f"{label}:none")
    try:
        heads = connection.execute(
            "SELECT framework_ref, MAX(version_number) AS v "
            "FROM industry_framework_versions GROUP BY framework_ref "
            "ORDER BY framework_ref"
        ).fetchall()
        parts += [f"{item['framework_ref']}:{item['v']}" for item in heads]
    except Exception:  # noqa: BLE001 - no framework table yet is a valid state
        parts.append("no-frameworks")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class MissionIndustryFrameworkLaneCoordinator:
    """Launch and settle the industry-framework lane."""

    def __init__(
        self,
        *,
        connection: Any,
        launcher: Any,
        min_interval_seconds: int = MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
        failure_ledger_dir: Any | None = None,
        failure_clock: Any | None = None,
    ) -> None:
        self.connection = connection
        self.launcher = launcher
        self.min_interval_seconds = int(min_interval_seconds)
        self.clock = clock
        self._open: str | None = None
        self._last_launch: float | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed. Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have
        # fixed it.
        self._quiet_signature: str | None = None
        self.budget = lane_budget("industry_framework", state_dir=failure_ledger_dir,
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
            "industry_ref": summary.get("industry_ref"),
            "framework_status": summary.get("framework_status"),
            "version_ref": summary.get("version_ref"),
            "units_drafted": summary.get("units_drafted"),
            "new_refs": summary.get("new_refs"),
            "open_gaps": summary.get("open_gaps"),
            "comparison": summary.get("comparison"),
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
        status = str(settled.get("framework_status") or "")
        if status in {"not_authorized", "no_policy"} and signature:
            self.budget.record(permission_key(self.connection, self.launcher, str(signature)),
                               status="gated:not permitted",
                               reason=f"gated:not permitted: {status}")
        elif status in QUIET_STATUSES and signature:
            settled["resumed"] = self.budget.clear(str(signature))
            self._quiet_signature = str(signature)
        elif settled.get("status") not in ("succeeded", "orphaned"):
            if signature:
                self.budget.record_settled(str(signature), settled)
        elif status in CONTENT_TERMINAL_STATUSES and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
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
        now = self.clock()
        if self._last_launch is not None and (
            now - self._last_launch < self.min_interval_seconds
        ):
            # The cadence, and it is deliberately checked after the signature:
            # an operator reading the tick summary should be able to see that
            # there *is* something to do and that the lane is waiting, rather
            # than seeing nothing and wondering whether the lane is wired.
            return {
                "status": "waiting", "settled": settled, "signature": signature,
                "reason": ("the industry framework is a weekly deliverable; "
                           f"{int(self.min_interval_seconds - (now - self._last_launch))}"
                           "s of its interval remain"),
            }
        try:
            ticket = self.launcher.start(signature=signature)
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        self._last_launch = now
        return {"status": "launched", "ticket_ref": ticket["id"],
                "signature": signature, "settled": settled}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P12e): the industry's framework, a few parts a week."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no industry-framework lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=server.store.connection, launcher=launcher,
            failure_ledger_dir=getattr(launcher, "state_dir", None))
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    from pathlib import Path as _Path

    parser.add_argument(
        "--industry-framework-model-config", type=_Path, default=None,
        help="Model configuration the framework drafts with. Omit and the lane "
             "computes its comparison table and reports gated: the table needs "
             "no model, the prose does.",
    )
    parser.add_argument(
        "--industry-framework-policy", type=_Path, default=None,
        help="P12e policy: the causal-chain titles, the driver horizons, the gap "
             "checklist and the Constitution's output_rubric bindings.",
    )
    parser.add_argument(
        "--industry-framework-verifier-model-config", type=_Path, default=None,
        help="The configuration the independent verifier runs on. Without it "
             "the child holds: one configuration routes both calls the same "
             "way, so the verdict could never be shown to be independent.",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "industry_framework_policy", None) is None:
        return None
    from pathlib import Path as _Path

    from .industry_framework_launcher import IndustryFrameworkLauncher

    return IndustryFrameworkLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=getattr(args, "industry_framework_model_config", None),
        verifier_model_config_path=getattr(
            args, "industry_framework_verifier_model_config", None),
        scheduler_db=getattr(args, "scheduler", None),
        policy_path=args.industry_framework_policy,
    )


# What this lane needs on disk before it is worth turning on. The policy alone,
# and not the model configuration: this is the one drafting lane whose product
# is half deterministic, so a Core with the policy and no model still gets the
# cross-company table computed and reported every week -- which is exactly the
# state the live Core is in until the deliverable scope is granted.
FRAMEWORK_MODEL_CONFIG = "initial-screen-model-config.json"
FRAMEWORK_VERIFIER_MODEL_CONFIG = "company-dossier-verifier-model-config.json"
LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG = "dossier-verifier-model-config.json"
FRAMEWORK_POLICY = "p12e-industry-framework-policy-v1.json"


def argv_fragment(context: Any) -> list[str]:
    policy = context.state / FRAMEWORK_POLICY
    if not policy.is_file():
        return []
    argv = ["--industry-framework-policy", str(policy)]
    config = context.state / FRAMEWORK_MODEL_CONFIG
    if config.is_file():
        argv += ["--industry-framework-model-config", str(config)]
    for name in (FRAMEWORK_VERIFIER_MODEL_CONFIG,
                 LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG):
        verifier = context.state / name
        if verifier.is_file():
            argv += ["--industry-framework-verifier-model-config", str(verifier)]
            break
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_industry_framework",
    # 139, after the dossier at 137: the framework reads the company files'
    # demand and supply sections, so it runs behind the lane that writes them.
    order=139,
    driver_key="industry_framework",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12e: the industry framework -- the Constitution's causal chain, the "
         "driver pack's drivers, a computed five-company comparison, and a gap "
         "list derived from the source capability map. Weekly.",
))


__all__ = [
    "FRAMEWORK_MODEL_CONFIG",
    "FRAMEWORK_POLICY",
    "FRAMEWORK_VERIFIER_MODEL_CONFIG",
    "LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MIN_INTERVAL_SECONDS",
    "QUIET_STATUSES",
    "MissionIndustryFrameworkLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "ledger_signature",
]
