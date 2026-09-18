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

**A draft the submission standard held back is silence too.**  D1 refuses to
put a draft in front of a person that answers three of twelve questions; the
child records why, in a note beside the Core, and this lane treats that like
every other content refusal -- the same evidence would be drafted the same way
and refused the same way, so it waits for the evidence to move.

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
from datetime import datetime, timedelta, timezone
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
    # D1: the draft was well formed and well cited and still was not worth a
    # person's time.  It belongs with the other content refusals rather than
    # with the idle statuses because the reason is worth showing: "held back
    # because it answers three of twelve questions" is the one sentence that
    # tells the owner the gate is not stuck on a bug.
    "auto_returned",
})
# What starts the company back-off below.  Every content refusal, including the
# two this lane's terminal list happens not to name -- they take the quiet path
# instead, which is correct for an unchanged signature and useless for a
# changing one.  ``constitution_refused`` is exactly the live case: DXC's return
# path held on it 23 times.
COOLDOWN_STATUSES = CONTENT_TERMINAL_STATUSES | frozenset({
    "constitution_refused", "unresolvable_refs",
})
# Six hours, the same number and for the same reason as the dossier lane's.
# ``content_refused`` is recorded against the run's *signature*, and this
# lane's signature digests the company's dossier head, its debate map, the
# numbers and the valuation rows -- all of which move while an owner is
# thinking.  In the live tick ledger (2,147 dossier-gate ticks, 09-10 to
# 09-18) Accenture was launched 103 times under 49 distinct signatures and DXC
# 51 times under 21; the hold binds for a few ticks and then a new key lets the
# same four calls be paid for again.  Under a six-hour company-keyed cooldown
# those would have been 15 launches each.
#
# It is keyed on the company and measured in time, and new evidence neither
# restarts nor lifts it: new evidence arriving is the thing that broke the old
# hold.  A submission, an idle tick or a moved control fingerprint ends it --
# a cooldown a deploy cannot clear is a suspension.
#
# A module constant because the gate shares P12a's policy file, whose shape is
# closed and carries no cadence field; when one is added, the coordinator
# already takes the number as a constructor argument.
CONTENT_REFUSAL_COOLDOWN_SECONDS = 6 * 60 * 60


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


def clear_obsolete_permissions(
    budget: Any, current: str, company_ref: str | None = None,
) -> None:
    for row in budget.permission_items():
        same_company = company_ref is None or str(row["item_key"]).startswith(
            f"{company_ref}|")
        if same_company and row["item_key"] != current:
            budget.retire(row["item_key"])


def company_ledger_signature(connection: Any, company_ref: str,
                             launcher: Any | None = None) -> str:
    from .deep_insight_gate_cli import deep_insight_company_source_fingerprint
    paths = () if launcher is None else (
        getattr(launcher, "model_config_path", None),
        getattr(launcher, "verifier_model_config_path", None),
        getattr(launcher, "policy_path", None),
    )
    return deep_insight_company_source_fingerprint(
        connection, company_ref, input_paths=paths)


def ledger_signature(connection: Any) -> str:
    """A cheap digest of everything that could give this lane something to do.

    Three reads: where each dossier chain's head is, where each debate map's
    head is, and how many gate drafts and decisions exist.  Deliberately not the
    plan -- a signature says "something moved", and only the child says what
    that means.
    """

    from .cockpit_model import verifier_provider_contract_fingerprint
    parts: list[str] = [verifier_provider_contract_fingerprint(
        "deep_insight_gate_verifier")]
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
                 companies: Callable[[], list[str]] | None = None,
                 failure_ledger_dir: Any | None = None,
                 failure_clock: Any | None = None,
                 cooldown_seconds: int = CONTENT_REFUSAL_COOLDOWN_SECONDS) -> None:
        self.connection = connection
        self.launcher = launcher
        self.companies = companies
        self.cooldown_seconds = int(cooldown_seconds)
        # company_ref -> (when the cooldown ends, what refused, why, the
        # control fingerprint it was decided under).  Keyed on the company
        # because that is the thing that keeps being relaunched; the signature
        # the failure budget holds is a different string on every tick.
        self._cooldowns: dict[str, tuple[Any, str, str, str]] = {}
        self._cooldown_clock = failure_clock or (
            lambda: datetime.now(timezone.utc))
        self._open: str | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed.  Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have fixed
        # it.
        self._quiet_signatures: set[str] = set()
        # The same bound the company cooldown uses, one step down: a
        # signature-keyed terminal verdict otherwise lives for ever, so a
        # company whose evidence stops moving could never be asked again.
        self.budget = lane_budget("deep_insight_gate", state_dir=failure_ledger_dir,
                                  clock=failure_clock,
                                  block_ttl_seconds=self.cooldown_seconds)

    # -- the company-scoped back-off -----------------------------------------

    def control_fingerprint(self) -> str:
        """The half of the launch signature no evidence can move.

        The drafting contract, the verifier's provider contract, the mission
        and governance pointers, and the model/policy configuration files.  A
        content refusal is a statement made under these; when one of them
        moves, the statement is about a system that no longer exists and the
        back-off goes with it.  That is what makes a reviewed fix take effect
        on the next tick rather than in six hours.
        """

        from .cockpit_model import verifier_provider_contract_fingerprint
        from .deep_insight_gate_cli import GATE_DRAFTING_CONTRACT

        return permission_key(self.connection, self.launcher, "|".join([
            verifier_provider_contract_fingerprint("deep_insight_gate_verifier"),
            GATE_DRAFTING_CONTRACT,
        ]))

    def company_cooldown(self, company_ref: Any,
                         control: str | None = None) -> dict[str, Any] | None:
        """Why this company must not be relaunched yet, or ``None``.

        Expiry is read rather than swept: a cooldown nobody asks about costs
        nothing, and a sweep would need its own tick.
        """

        key = str(company_ref or "-")
        entry = self._cooldowns.get(key)
        if entry is None:
            return None
        until, status, reason, held_control = entry
        if control is None:
            control = self.control_fingerprint()
        if self._cooldown_clock() >= until or control != held_control:
            self._cooldowns.pop(key, None)
            return None
        return {
            "until": until.isoformat(timespec="seconds"),
            "seconds": self.cooldown_seconds,
            "gate_status": status,
            "reason": (f"content refused ({status}); this company is held until "
                       f"{until.isoformat(timespec='seconds')} regardless of new "
                       f"evidence: {reason}")[:MAX_FAILURE_DETAIL_CHARS],
        }

    def _start_cooldown(self, company_ref: Any, *, status: str, reason: str) -> None:
        """Begin the back-off, or leave a running one exactly where it is.

        Not restarted by a second refusal either: a company that refused twice
        inside one cooldown has not earned a longer one, and an interval every
        failure extends is a suspension.
        """

        key = str(company_ref or "-")
        control = self.control_fingerprint()
        if self.company_cooldown(key, control) is not None:
            return
        self._cooldowns[key] = (
            self._cooldown_clock() + timedelta(seconds=self.cooldown_seconds),
            str(status), str(reason)[:MAX_FAILURE_DETAIL_CHARS], control,
        )

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
        company = settled.get("company_ref")
        # The content back-off, decided before the signature-keyed bookkeeping
        # below and independently of it.  A verdict about what a draft *says*
        # is about the company, and the next tick's evidence signature has no
        # bearing on whether it is still true.
        if status in COOLDOWN_STATUSES:
            self._start_cooldown(
                company, status=status,
                reason=str(settled.get("failure_reason") or status))
            settled["cooldown"] = self.company_cooldown(company)
        elif status:
            # A submission, a duplicate, an idle tick or a gating status ends
            # it.  A submitted draft is the thing the cooldown was waiting for.
            self._cooldowns.pop(str(company or "-"), None)
        if status in {"not_authorized", "no_checkpoint", "no_policy"} and signature:
            self.budget.record(
                str(signature),
                status="gated:not permitted", reason=f"gated:not permitted: {status}")
        elif settled.get("status") not in ("succeeded", "orphaned"):
            if signature:
                decision = self.budget.record_settled(str(signature), settled)
                if decision.action == "not_permitted":
                    self.budget.clear(str(signature))
                    self.budget.record(
                        str(signature),
                        classification=decision.classification)
        elif status in CONTENT_TERMINAL_STATUSES and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
        elif status not in RELAUNCH_STATUSES and signature:
            settled["resumed"] = self.budget.clear(str(signature))
            self._quiet_signatures.add(str(signature))
        elif signature:
            self.budget.clear(str(signature))
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        try:
            companies = self.companies() if self.companies is not None else [None]
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        held_companies = {}
        held_decisions = {}
        quiet_companies = []
        cooling_down: dict[str, Any] = {}
        for company_ref in companies:
            evidence = (ledger_signature(self.connection) if company_ref is None else
                        f"{company_ref}|{company_ledger_signature(self.connection, company_ref, self.launcher)}")
            signature = permission_key(self.connection, self.launcher, evidence)
            clear_obsolete_permissions(self.budget, signature, company_ref)
            if signature in self._quiet_signatures:
                quiet_companies.append(company_ref)
                continue
            held = self.budget.blocked(signature) or self.budget.blocked(evidence)
            if held is not None:
                label = str(company_ref or "-")
                held_companies[label] = held.classification.reason
                held_decisions[label] = held
                continue
            # The company-scoped back-off, asked after the signature-keyed
            # budget and before the launch.  The budget answers first because
            # it is the more specific statement -- "this exact input is
            # terminal" -- and the cooldown catches the case the budget
            # structurally cannot: a refusal about the *content* whose key has
            # changed because the dossier, the debate map or a number moved
            # underneath it.
            cooldown = self.company_cooldown(company_ref)
            if cooldown is not None:
                label = str(company_ref or "-")
                cooling_down[label] = cooldown
                # Also in ``held``, which is where every reader of this lane
                # already looks for "why did nothing happen for this company".
                held_companies[label] = cooldown["reason"]
                continue
            try:
                ticket = self.launcher.start(
                    signature=signature, company_ref=company_ref,
                    source_fingerprint=(None if company_ref is None
                                        else evidence.rsplit("|", 1)[1]))
            except LaneChildConflict as exc:
                return {"status": "busy", "settled": settled,
                        "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                return {"status": "rejected", "settled": settled,
                        "reason": f"{type(exc).__name__}: {exc}"}
            self._open = ticket["id"]
            return {"status": "launched", "ticket_ref": ticket["id"],
                    "company_ref": company_ref, "signature": signature,
                    "settled": settled, "held": held_companies,
                    **({} if not cooling_down else {"cooling_down": cooling_down})}
        if held_companies:
            if len(companies) == 1 and len(held_decisions) == 1:
                decision = next(iter(held_decisions.values()))
                return {"status": decision.action, "settled": settled,
                        "reason": decision.classification.reason,
                        "held": held_companies,
                        **({} if not cooling_down else
                           {"cooling_down": cooling_down}),
                        "failure_budget": self.budget.summary()}
            return {"status": "held", "settled": settled, "held": held_companies,
                    "reason": "; ".join(f"{key}: {value}" for key, value in held_companies.items()),
                    **({} if not cooling_down else {"cooling_down": cooling_down}),
                    "failure_budget": self.budget.summary()}
        return {"status": "idle", "settled": settled, "companies": quiet_companies,
                "reason": "nothing has moved since each company's last quiet run"}


def _screened_companies(server: Any) -> list[str]:
    from .deep_insight_gate_cli import screened_companies
    pointer = server.store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if pointer is None:
        return []
    mission = server.coverage_mission.mission(pointer["mission_version_id"])
    return screened_companies(server.coverage_mission, mission)


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
            companies=lambda: _screened_companies(server),
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
GATE_MODEL_CONFIG = "dossier-model-config.json"
GATE_VERIFIER_MODEL_CONFIG = "company-dossier-verifier-model-config.json"
LEGACY_GATE_MODEL_CONFIG = "initial-screen-model-config.json"
LEGACY_GATE_VERIFIER_MODEL_CONFIG = "dossier-verifier-model-config.json"
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
    if not config.is_file():
        config = context.state / LEGACY_GATE_MODEL_CONFIG
    policy = context.state / GATE_POLICY
    verifier = context.state / GATE_VERIFIER_MODEL_CONFIG
    if not verifier.is_file():
        verifier = context.state / LEGACY_GATE_VERIFIER_MODEL_CONFIG
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
    "CONTENT_REFUSAL_COOLDOWN_SECONDS",
    "CONTENT_TERMINAL_STATUSES",
    "COOLDOWN_STATUSES",
    "GATE_MODEL_CONFIG",
    "LEGACY_GATE_MODEL_CONFIG",
    "LEGACY_GATE_VERIFIER_MODEL_CONFIG",
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
