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
from .lane_change_key import (
    ChangeKeyMemo, append_probe, claim_change_keys, claim_index_change_keys,
    compose, document_figure_change_keys, head_change_keys,
    statement_change_keys,
)

MAX_FAILURE_DETAIL_CHARS = 500
#: Where the change keys of the last tick survive a restart, beside the lane's
#: own failure ledger.
CHANGE_KEY_FILE = "company-dossier-lane-change-keys.json"
MAX_REPAIR_TARGETS = 20
LAUNCHER_KWARG = "company_dossier_launcher"
# Statuses that mean "this run looked and found nothing to do". After one of
# these, an unchanged signature is a reason to stay quiet.
QUIET_STATUSES = frozenset({"nothing_new", "no_screened_company", "no_mission",
                            "no_claim_index", "insufficient_evidence"})
CONTENT_TERMINAL_STATUSES = frozenset({
    "verification_failed", "rubric_refused", "constitution_refused",
    "not_independent", "no_new_evidence",
})
# Six hours.  A content refusal is recorded in the failure budget against the
# run's *signature*, and the signature is a digest of the company's evidence:
# the quantitative-claim promotion lane admits a couple of hundred Claims a
# tick, so every tick produced a signature the hold had never seen, the hold
# never bound, and Accenture was relaunched every five minutes from
# 2026-09-17T04:17Z against the same unsupported sentence -- fifty runs, four
# paid calls each, nothing published.
#
# So the back-off is keyed on the *company* and measured in time.  It does not
# restart when new evidence lands: new evidence is exactly what was arriving
# throughout, and a cooldown a Claim can reset is not a cooldown.  Six hours is
# roughly a quarter of the working day -- long enough that a redraft is a new
# attempt rather than the same one, short enough that a company whose evidence
# really did move is not silent for a day.
#
# A module constant because the dossier policy has a closed shape with no
# cadence field in it; when one is added, ``MissionDossierLaneCoordinator``
# takes the number as a constructor argument and the policy can pass it.
CONTENT_REFUSAL_COOLDOWN_SECONDS = 6 * 60 * 60


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


def clear_obsolete_permissions(
    budget: Any, current: str, company_ref: str | None = None,
) -> None:
    for row in budget.permission_items():
        same_company = (
            company_ref is None
            or str(row["item_key"]).startswith(f"{company_ref}|")
        )
        if same_company and row["item_key"] != current:
            budget.retire(row["item_key"])


def ledger_signature(connection: Any) -> str:
    """A cheap digest of everything that could give this lane something to do.

    Three reads: how many Claims exist and which is newest, and where each
    dossier chain's head is. Deliberately not the plan -- a signature says
    "something moved", and only the child says what that means.
    """

    from .cockpit_model import verifier_provider_contract_fingerprint
    from .company_dossier_draft import (draft_contract_fingerprint,
                                        verifier_prompt_contract_fingerprint)
    from .mission_deliverable import number_source_contract_fingerprint
    from .company_dossier import output_rubric_contract_fingerprint
    row = connection.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM claim_versions"
    ).fetchone()
    parts = [str(row["n"]), str(row["newest"] or "-"),
             verifier_provider_contract_fingerprint("dossier_verifier"),
             draft_contract_fingerprint(), verifier_prompt_contract_fingerprint(),
             number_source_contract_fingerprint(), output_rubric_contract_fingerprint()]
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


def company_ledger_signature(
    connection: Any, company_ref: str, *,
    snapshot: Mapping[str, Any] | None = None,
) -> str:
    """Digest only the authority inputs that can change one company's file.

    ``snapshot`` is one Claim snapshot shared by every company of one tick:
    reading and hashing the Ledger is the expensive part of this signature,
    and it does not depend on the company.
    """
    from .cockpit_model import verifier_provider_contract_fingerprint
    from .company_dossier_cli import dossier_company_source_fingerprint
    from .company_dossier_draft import (draft_contract_fingerprint,
                                        verifier_prompt_contract_fingerprint)
    from .mission_deliverable import number_source_contract_fingerprint
    from .company_dossier import output_rubric_contract_fingerprint

    parts = [company_ref,
             dossier_company_source_fingerprint(connection, company_ref, snapshot=snapshot),
             verifier_provider_contract_fingerprint("dossier_verifier"),
             draft_contract_fingerprint(), verifier_prompt_contract_fingerprint(),
             number_source_contract_fingerprint(), output_rubric_contract_fingerprint()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def company_change_keys(
    connection: Any, companies: Any, *, control: Any = (),
) -> dict[str, str]:
    """A cheap key per company that moves whenever its signature could.

    Every table ``dossier_company_source_fingerprint`` reads is represented
    here: the company's Claims and index entries, the filed statements and
    document figures its numbers come from, and the heads of its forecast and
    dossier chains.  Retirement decisions and evidence relations carry no
    company of their own and are aggregated whole -- conservative, and cheap
    enough that the conservatism is free.

    ``control`` is the half of the signature no row can move: the drafting,
    verifier, number-source and rubric contracts.  They only change on a
    deploy, but they are in the signature, so they are in the key.

    One grouped scan per table for every company at once -- 0.1 s on the live
    Core against the 9.7 s of signatures it gates.
    """

    claims = claim_change_keys(connection)
    entries = claim_index_change_keys(connection)
    figures = document_figure_change_keys(connection)
    statements = statement_change_keys(connection)
    forecasts = head_change_keys(connection, "forecast_model_versions", "company_ref")
    dossiers = head_change_keys(connection, "company_dossier_versions", "company_ref")
    shared = [
        append_probe(connection, "claim_retirement_decisions"),
        append_probe(connection, "evidence_relations"),
        append_probe(connection, "coverage_mission_statement_lines"),
        *[str(item) for item in control],
    ]
    keys: dict[str, str] = {}
    for company_ref in companies:
        if company_ref is None:
            continue
        label = str(company_ref)
        keys[label] = compose([
            "dossier-change-key:v1", label,
            claims.get(label), entries.get(label), figures.get(label),
            statements.get(label), forecasts.get(label), dossiers.get(label),
            *shared,
        ])
    return keys


class MissionDossierLaneCoordinator:
    """Launch and settle the dossier lane."""

    def __init__(self, *, connection: Any, launcher: Any,
                 companies: Callable[[], list[str]] | None = None,
                 mission: Callable[[], Mapping[str, Any] | None] | None = None,
                 failure_ledger_dir: Any | None = None,
                 failure_clock: Callable[[], Any] | None = None,
                 cooldown_seconds: int = CONTENT_REFUSAL_COOLDOWN_SECONDS) -> None:
        self.connection = connection
        self.launcher = launcher
        self.companies = companies
        self.mission = mission
        self.cooldown_seconds = int(cooldown_seconds)
        # company_ref -> (when the cooldown ends, what refused, why).  Keyed on
        # the company because that is the thing that keeps being relaunched,
        # and the signature the failure budget holds is a different string on
        # every tick.
        self._cooldowns: dict[str, tuple[Any, str, str, str]] = {}
        self._cooldown_clock = failure_clock or (
            lambda: datetime.now(timezone.utc))
        self._open: str | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed. Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have
        # fixed it.
        self._quiet_signatures: set[str] = set()
        # B1-5: the last cheap key each company's signature was computed
        # under, so an unchanged company costs one grouped scan instead of a
        # full fingerprint.  Persisted beside the failure ledger: the first
        # tick after a deploy would otherwise recompute all five at once,
        # which is the 30 s timeout this gate exists to remove.
        self.change_keys = ChangeKeyMemo(
            None if failure_ledger_dir is None
            else Path(failure_ledger_dir) / CHANGE_KEY_FILE)
        probe_interval = (
            launcher.capacity_probe_interval_seconds()
            if hasattr(launcher, "capacity_probe_interval_seconds") else None
        )
        kwargs = {} if probe_interval is None else {
            "probe_interval_seconds": probe_interval}
        # The same bound the company cooldown uses, for the same reason one
        # step down: a signature-keyed terminal verdict, or an exhausted
        # transient budget, otherwise lives for ever -- so a company whose
        # evidence stops moving can never be asked about again even after the
        # refusal has been fixed.
        self.budget = lane_budget("company_dossier", state_dir=failure_ledger_dir,
                                  clock=failure_clock,
                                  block_ttl_seconds=self.cooldown_seconds, **kwargs)

    # -- the company-scoped back-off -----------------------------------------

    def control_fingerprint(self) -> str:
        """The half of the launch signature no Claim can move.

        The drafting contract, the verifier's prompt contract, the provider
        contract, the number-source contract, the output-rubric contract, the
        mission, the governance policy and the model configuration files --
        everything a deploy or a person changes, and nothing the evidence
        changes.  A content refusal is a statement made under *these*; when one
        of them moves, the statement is about a system that no longer exists
        and the back-off goes with it.  That is the difference between a
        cooldown and a suspension, and it is what makes a reviewed fix take
        effect on the next tick rather than in six hours.
        """

        from .cockpit_model import verifier_provider_contract_fingerprint
        from .company_dossier import output_rubric_contract_fingerprint
        from .company_dossier_draft import (draft_contract_fingerprint,
                                            verifier_prompt_contract_fingerprint)
        from .mission_deliverable import number_source_contract_fingerprint

        return permission_key(self.connection, self.launcher, "|".join([
            verifier_provider_contract_fingerprint("dossier_verifier"),
            draft_contract_fingerprint(), verifier_prompt_contract_fingerprint(),
            number_source_contract_fingerprint(), output_rubric_contract_fingerprint(),
        ]))

    def _change_keys(self, companies: Any) -> dict[str, str]:
        """This tick's cheap keys, or nothing -- which recomputes everything.

        A key that cannot be read is not a reason to skip a company: an empty
        map makes every signature a miss, which is exactly the behaviour this
        lane had before the gate existed.
        """

        from .cockpit_model import verifier_provider_contract_fingerprint
        from .company_dossier import output_rubric_contract_fingerprint
        from .company_dossier_draft import (draft_contract_fingerprint,
                                            verifier_prompt_contract_fingerprint)
        from .mission_deliverable import number_source_contract_fingerprint

        try:
            control = (
                verifier_provider_contract_fingerprint("dossier_verifier"),
                draft_contract_fingerprint(), verifier_prompt_contract_fingerprint(),
                number_source_contract_fingerprint(),
                output_rubric_contract_fingerprint(),
            )
            keys = company_change_keys(self.connection, companies, control=control)
        except Exception:  # noqa: BLE001 - an unreadable key recomputes, never refuses
            return {}
        if keys:
            self.change_keys.forget(keys)
        return keys

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
            "dossier_status": status,
            "reason": (f"content refused ({status}); this company is held until "
                       f"{until.isoformat(timespec='seconds')} regardless of new "
                       f"evidence: {reason}")[:MAX_FAILURE_DETAIL_CHARS],
        }

    def _start_cooldown(self, company_ref: Any, *, status: str, reason: str) -> None:
        """Begin the back-off, or leave a running one exactly where it is.

        Not restarted by a second refusal either: a company that refused twice
        inside one cooldown has not earned a longer one, and an interval that
        every failure extends is a suspension.
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
            "dossier_status": summary.get("dossier_status"),
            "verification_status": (summary.get("verification") or {}).get("status"),
            "verification_verdict": (summary.get("verification") or {}).get("verdict"),
            "version_ref": summary.get("version_ref"),
            "units_drafted": summary.get("units_drafted"),
            "new_refs": summary.get("new_refs"),
            "cost_micros": summary.get("cost_micros"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        targets = []
        for item in (summary.get("repair_targets") or [])[:MAX_REPAIR_TARGETS]:
            if not isinstance(item, Mapping):
                continue
            target = {}
            for key in ("unit", "code", "slot_id", "check", "section", "figure",
                        "detail"):
                value = item.get(key)
                if isinstance(value, (str, int, float, bool)):
                    target[key] = str(value)[:MAX_FAILURE_DETAIL_CHARS]
            if target:
                targets.append(target)
        if targets:
            settled["repair_targets"] = targets
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
        company = settled.get("company_ref")
        # The content back-off, decided before the signature-keyed bookkeeping
        # below and independently of it.  A verdict about what a draft *says*
        # is about the company, and the next tick's evidence signature has no
        # bearing on whether it is still true.
        if status in CONTENT_TERMINAL_STATUSES and (
                status != "verification_failed"
                or settled.get("verification_status") in {"refused", "verified"}):
            self._start_cooldown(
                company, status=status,
                reason=str(settled.get("failure_reason") or status))
            settled["cooldown"] = self.company_cooldown(company)
        elif status and status not in CONTENT_TERMINAL_STATUSES:
            # Anything that is not a content refusal -- a publication, an idle
            # tick, a transport failure -- ends it.  A published version is the
            # thing the cooldown was waiting for.
            self._cooldowns.pop(str(company or "-"), None)
        if status == "not_authorized" and signature:
            self.budget.record(str(signature),
                               status="gated:not permitted",
                               reason="gated:mission does not grant dossier")
        # A child whose producer output itself broke the closed rubric exits
        # failed, but that exact input is terminal.  Other failed children
        # (notably verification_failed when the verifier transport never ran)
        # must still reach the shared classifier instead of being mistaken for
        # a content verdict.
        elif (
            status == "rubric_refused"
            or (
                status == "verification_failed"
                and settled.get("verification_status") in {"refused", "verified"}
            )
        ) and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
        elif settled.get("status") != "succeeded" and settled.get("status") != "orphaned":
            if signature:
                self.budget.record_settled(str(signature), settled)
        elif status in CONTENT_TERMINAL_STATUSES and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
        elif status in QUIET_STATUSES and signature:
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
            # Bind ticket identity before launch. A child may finish after a
            # new mission is signed; its refusal belongs to its launch state.
            companies = self.companies() if self.companies is not None else [None]
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        held_companies = {}
        held_decisions = {}
        quiet_companies = []
        cooling_down: dict[str, Any] = {}
        try:
            active_mission = self.mission() if self.mission is not None else None
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        # One Ledger snapshot for every company this tick.  Each company's
        # signature used to read and hash the whole Ledger for itself -- five
        # companies, five snapshots, on the writer's store thread, past the
        # request timeout every tick once the Ledger reached ten thousand
        # Claims.  The snapshot is taken only if a company needs it.
        claim_snapshot: Mapping[str, Any] | None = None
        # B1-5: one grouped scan answers "could any of these five have moved"
        # before a single signature is built.  A company whose key is the one
        # its last signature was computed under reuses that signature: the
        # answer cannot have changed, and asking again costs a second of the
        # store thread to be told so.
        keys = self._change_keys(companies)
        recomputed: list[str] = []

        def fresh() -> dict[str, Any]:
            """Which companies paid for a full signature this tick, if any.

            Absent on the ordinary tick, so the answer stays the shape every
            reader already knows, and present exactly when someone asking why
            a tick was slow wants to know.
            """

            return {} if not recomputed else {"recomputed": list(recomputed)}

        for company_ref in companies:
            if company_ref is None:
                evidence = ledger_signature(self.connection)
            else:
                label = str(company_ref)
                key = keys.get(label)
                signed = self.change_keys.cached(label, key)
                if signed is None:
                    if claim_snapshot is None:
                        from .company_dossier_cli import _ReadOnlyStoreView
                        claim_snapshot = _ReadOnlyStoreView(
                            self.connection).claim_index_snapshot()
                    signed = company_ledger_signature(
                        self.connection, company_ref, snapshot=claim_snapshot)
                    self.change_keys.remember(label, key, signed)
                    recomputed.append(label)
                evidence = f"{company_ref}|{signed}"
            signature = permission_key(self.connection, self.launcher, evidence)
            clear_obsolete_permissions(self.budget, signature, company_ref)
            if signature in self._quiet_signatures:
                quiet_companies.append(company_ref)
                continue
            held = self.budget.blocked(signature)
            controlled_reentry = None
            if held is not None:
                recovery = getattr(self.launcher, "controlled_reentry", None)
                authorization = (
                    None if recovery is None or active_mission is None else
                    recovery(signature=signature, company_ref=company_ref,
                             mission=dict(active_mission)))
                if authorization is not None:
                    held = None
                    controlled_reentry = authorization
                else:
                    label = str(company_ref or "-")
                    held_companies[label] = held.classification.reason
                    held_decisions[label] = held
                    continue
            # The company-scoped back-off, asked after the signature-keyed
            # budget and before the launch.  The budget answers first because
            # it is the more specific statement -- "this exact input is
            # terminal" -- and the cooldown is what catches the case the
            # budget structurally cannot: a refusal about the *content* whose
            # key has changed because a Claim landed since.  A controlled
            # re-entry, which is a reviewed human decision, releases both.
            cooldown = (None if controlled_reentry is not None
                        else self.company_cooldown(company_ref))
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
                    controlled_reentry=controlled_reentry)
            except LaneChildConflict as exc:
                return {"status": "busy", "settled": settled,
                        "reason": f"{type(exc).__name__}: {exc}"}
            except LaneChildRejected as exc:
                return {"status": "rejected", "settled": settled,
                        "reason": f"{type(exc).__name__}: {exc}"}
            self._open = ticket["id"]
            return {"status": "launched", "ticket_ref": ticket["id"],
                    "company_ref": company_ref, "signature": signature,
                    "settled": settled, "held": held_companies, **fresh(),
                    **({} if not cooling_down else {"cooling_down": cooling_down})}
        if not companies:
            return {"status": "idle", "settled": settled, **fresh(),
                    "reason": "no screened company needs a dossier"}
        if held_companies:
            if len(held_decisions) == 1 and len(companies) == 1:
                decision = next(iter(held_decisions.values()))
                return {"status": decision.action, "settled": settled,
                        "reason": decision.classification.reason,
                        "held": held_companies, **fresh(),
                        **({} if not cooling_down else
                           {"cooling_down": cooling_down}),
                        "failure_budget": self.budget.summary()}
            return {"status": "held", "settled": settled, "held": held_companies,
                    "reason": "; ".join(
                        f"{company}: {reason}"
                        for company, reason in held_companies.items()),
                    **fresh(),
                    **({} if not cooling_down else {"cooling_down": cooling_down}),
                    "failure_budget": self.budget.summary()}
        return {"status": "idle", "settled": settled,
                "companies": quiet_companies, **fresh(),
                "reason": "nothing has moved since each company's last quiet run"}


def _screened_companies(server: Any) -> list[str]:
    from .company_dossier_cli import screened_companies

    mission = _current_mission(server)
    if mission is None:
        return []
    return screened_companies(server.coverage_mission, mission)


def _current_mission(server: Any) -> Mapping[str, Any] | None:
    pointer = server.store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer "
        "ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    return (None if pointer is None else
            server.coverage_mission.mission(pointer["mission_version_id"]))


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
            companies=lambda: _screened_companies(server),
            mission=lambda: _current_mission(server),
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
    "CHANGE_KEY_FILE",
    "CONTENT_REFUSAL_COOLDOWN_SECONDS",
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
    "company_change_keys",
    "company_ledger_signature",
    "dispatch",
    "ledger_signature",
]
