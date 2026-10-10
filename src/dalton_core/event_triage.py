"""A cheap first read of each event group that can only say "the judge should see this".

2026-09-30: from 09-24 the event_response pool was spent in full every day --
about 45 USD a day on 140-190 Opus judgements (0.29 USD each), and of the 1,041
paid judgements between 09-24 and 09-30 exactly one group (EPAM, 09-29) came
back as anything but NO_CHANGE.  The judge moved to Opus on 09-23 (routing
policy v54 put ``claude-opus-5`` first in the brain chain); before that the
same lane ran on deepseek-v4-flash at about 0.0015 USD a call.

This stage puts the cheap tier in front of the brain, with one rule that
decides everything else: **triage can only escalate.**  It never writes an
``event_judgements`` row, never publishes, never proposes; the only thing it
writes is its own append-only ``event_triage_decisions`` row.  A group it
flags goes to the expensive judge exactly as before.  A group it does not flag
is recorded as ``screened_out`` and leaves the unjudged backlog -- it is not
judged NO_CHANGE, because nobody judged it.

Three things keep the ability to catch a real change:

* **fail open** -- a triage call that fails, or answers outside its closed
  contract, escalates (``route = triage_failed``): the worst case is the cost
  the lane had before this stage existed, never a missed event.  A failure is
  not a decision, so it writes no row: the run summary lists it, and the group
  is screened again if its judgement is refused and it comes back;
* **an audit sample** -- a deterministic :data:`AUDIT_SHARE` of the groups
  triage did *not* flag go to the judge anyway (``route = audit``), with the
  triage verdict recorded beside them, so the audit compares the two and
  measures what triage misses;
* **bypass kinds** -- a primary filing and a price divergence (which owes a
  reflection by design) are never screened.

Everything is recorded per event, keyed UNIQUE by event, append-only: a replay
of a run finds the row and does not pay for triage twice, and a refused
judgement retried later goes straight back to the judge.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .cockpit_model import (
    CockpitModelError,
    lane_status_for,
    model_failure_trace,
    register_purpose,
    unwrap_json_object,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
#: Registered at import like the judge's own purposes; its tier (cheap) is in
#: ``model_fallback_chain._PURPOSE_TIERS`` and its pool (event_response) in
#: ``budget_pools.PURPOSE_POOLS``.
TRIAGE_PURPOSE = register_purpose("event_triage")
TRIAGE_CONTRACT_VERSION = "event-triage-contract:0.1"
#: Named in every row, so a later policy (a different audit share, a
#: different bypass list) is told apart from this one in the audit.
TRIAGE_POLICY_REF = "event-triage-policy:v1"
#: The share of not-flagged groups that go to the judge anyway.
AUDIT_SHARE = Decimal("0.05")
_AUDIT_SALT = "event-triage-audit:v1"
#: Never screened: a company's own primary filing, and a price divergence,
#: which owes a reflection whatever the decision is.
BYPASS_KINDS: frozenset[str] = frozenset({"filing", "price_divergence", "scenario_trigger",
                                          # ADR-0015: the company's own quarter.
                                          "earnings_release"})

#: Why a group should reach the judge.  Closed, like every other vocabulary
#: a model answers in here.
TRIAGE_SIGNALS: tuple[str, ...] = (
    "results_or_guidance",
    "thesis_or_falsifier",
    "driver_assumption",
    "corporate_action",
    "risk_or_legal",
    "company_specific_move",
    "other_material",
)
MAX_SIGNALS = 4
MAX_REASON_CHARS = 400
MAX_TRIAGE_PROMPT_BYTES = 60_000

OUTCOMES: tuple[str, ...] = ("escalated", "screened_out")
ROUTES: tuple[str, ...] = ("triage", "audit", "bypass", "triage_failed")
VERDICTS: tuple[str, ...] = ("escalate", "no_signal", "none")


class EventTriageError(RuntimeError):
    pass


class EventTriageValidationError(EventTriageError, ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# the one cheap call
# ---------------------------------------------------------------------------


def _payload_lines(event: Mapping[str, Any]) -> list[str]:
    return [
        f"  {field} = {value}"
        for field, value in sorted((event.get("payload") or {}).items())
        if value is not None and field not in {"invocation_ref", "artifact_hash",
                                               "excerpt_hash"}
    ]


def build_triage_prompt(
    context: Mapping[str, Any], *, max_prompt_bytes: int = MAX_TRIAGE_PROMPT_BYTES,
) -> str:
    """What the screener reads: the inputs, and what we already think.

    The same context the judge is given, cut to what a screen needs: every
    input in full, the theses, the driver assumptions and the last decisions
    -- the last because "already reflected in a recent decision" is the most
    common honest reason to stay quiet.  No source map, no citation rules: it
    cites nothing and decides nothing.
    """

    event = context["event"]
    rows = [event, *[row for row in (context.get("grouped_events") or ())
                     if row.get("id") != event.get("id")]]
    lines = [
        "You are screening new inputs for the covering analyst of one company. You do",
        "not decide anything and nothing you say is recorded as a judgement. You answer",
        "one question: could ANY of these inputs plausibly change a thesis, a forecast",
        "driver, or the company file below? If yes, the analyst reads them in full.",
        "",
        "Escalate when an input carries new reported numbers, guidance, bookings or",
        "orders; bears on a thesis, its mechanism or what would falsify it; moves a",
        "driver assumption; announces a corporate action (M&A, divestiture, capital",
        "return, leadership change, restructuring); discloses a new material risk,",
        "legal or regulatory matter or customer loss; or is a price move with a",
        "company-specific cause. When in doubt, escalate: a needless escalation costs",
        "one read, a missed change costs the view.",
        "Do not escalate for: company self-description or boilerplate, generic",
        "industry commentary, a restatement of what the theses, drivers or recent",
        "decisions already reflect, or a sector-wide price move with no company news.",
        "Judge every input on its own: ONE input that matters is enough.",
        "",
        f"## Company: {context.get('ticker') or context['company_ref']} ({context['company_ref']})",
        "",
        f"## Inputs ({len(rows)})",
    ]
    for row in rows:
        lines.append(
            f"- {row.get('id')} ({row.get('kind')}, evidence tier: "
            f"{row.get('evidence_tier')}, occurred_at: {row.get('occurred_at')})")
        lines.extend(_payload_lines(row))
    lines.extend(context.get("derived_lines") or ())
    lines.extend(["", "## Theses in force"])
    theses = context.get("theses") or ()
    if not theses:
        lines.append("(none admitted for this company)")
    for thesis in theses:
        lines.append(f"- {thesis.get('statement')}")
        if thesis.get("mechanism"):
            lines.append(f"    mechanism: {thesis['mechanism']}")
    lines.extend(["", "## Forecast drivers and what we currently assume"])
    drivers = context.get("drivers") or ()
    if not drivers:
        lines.append("(no driver model yet)")
    for driver in drivers:
        lines.append(f"- {driver.get('label') or driver.get('ref')}")
        for assumption in driver.get("assumptions") or ():
            lines.append(f"    {assumption.get('period_end')}: {assumption.get('measure')} = "
                         f"{assumption.get('value')}")
    lines.extend(["", "## The last decisions taken on this company"])
    history = context.get("recent_judgements") or ()
    if not history:
        lines.append("(none yet)")
    for row in history:
        lines.append(f"- {row.get('kind')} -> {row.get('decision')}: "
                     f"{str(row.get('because') or '')[:240]}")
    lines.extend([
        "",
        "Return raw JSON only, nothing else, exactly these keys:",
        '{"escalate": true|false, "signals": ["' + '|'.join(TRIAGE_SIGNALS) + '"],'
        ' "reason": "<one sentence>"}',
        "escalate true needs at least one signal; escalate false needs none.",
        f"At most {MAX_SIGNALS} signals; reason at most {MAX_REASON_CHARS} characters.",
    ])
    prompt = "\n".join(lines)
    if len(prompt.encode("utf-8")) > max_prompt_bytes:
        raise EventTriageValidationError(
            f"the triage prompt exceeds its bound ({len(prompt.encode('utf-8'))}"
            f">{max_prompt_bytes} bytes)")
    return prompt


def validate_triage_output(value: Any) -> dict[str, Any]:
    """Closed: anything outside the contract is a failure, and a failure escalates."""

    if not isinstance(value, Mapping):
        raise EventTriageValidationError("the screener did not return an object")
    if set(value) != {"escalate", "signals", "reason"}:
        raise EventTriageValidationError(
            f"the screener returned keys {sorted(value)}, not escalate/signals/reason")
    escalate = value["escalate"]
    if not isinstance(escalate, bool):
        raise EventTriageValidationError("escalate must be true or false")
    signals = value["signals"]
    if (not isinstance(signals, list) or len(signals) > MAX_SIGNALS
            or len(set(signals)) != len(signals)
            or any(item not in TRIAGE_SIGNALS for item in signals)):
        raise EventTriageValidationError(
            f"signals must be at most {MAX_SIGNALS} distinct words of {list(TRIAGE_SIGNALS)}")
    if escalate and not signals:
        raise EventTriageValidationError("an escalation must name a signal")
    if not escalate and signals:
        raise EventTriageValidationError("a group with signals must be escalated")
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise EventTriageValidationError(
            f"reason must be non-empty text of at most {MAX_REASON_CHARS} characters")
    return {"escalate": escalate, "signals": list(signals), "reason": reason.strip()}


def _provenance(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
        "purpose": TRIAGE_PURPOSE,
    }


def _prompt_bound(model: Any) -> int:
    resolver = getattr(model, "budget_for", None)
    if callable(resolver):
        return min(MAX_TRIAGE_PROMPT_BYTES, int(resolver(TRIAGE_PURPOSE)["max_input_tokens"]))
    return MAX_TRIAGE_PROMPT_BYTES


def screen(
    context: Mapping[str, Any], *, model: Any, mission: Mapping[str, Any], request_id: str,
) -> dict[str, Any]:
    """One cheap call.  ``status`` is ``screened`` or ``failed``; never raises."""

    try:
        prompt = build_triage_prompt(context, max_prompt_bytes=_prompt_bound(model))
    except EventTriageValidationError as exc:
        return {"status": "failed", "reason": str(exc), "model": None}
    try:
        call = model.call(purpose=TRIAGE_PURPOSE, request_id=request_id, prompt=prompt,
                          mission=mission)
    except CockpitModelError as exc:
        return {"status": "failed", "reason": f"the triage call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "failed"),
                "failure_trace": model_failure_trace(exc), "model": None}
    provenance = _provenance(call)
    try:
        validated = validate_triage_output(unwrap_json_object(call["text"]))
    except EventTriageValidationError as exc:
        return {"status": "failed", "reason": str(exc), "model": provenance}
    return {"status": "screened", "model": provenance, **validated}


def audit_selected(event_refs: Sequence[str], share: Decimal | None = None) -> bool:
    """Deterministic: the same group is in or out of the sample on every replay."""

    share = AUDIT_SHARE if share is None else share
    material = _AUDIT_SALT + "|" + "|".join(sorted(event_refs))
    bucket = int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:8], 16)
    return Decimal(bucket) / Decimal(0x1_0000_0000) < share


def gate(
    *, event_group: Sequence[Mapping[str, Any]], verdict: Mapping[str, Any] | None,
    share: Decimal | None = None,
) -> dict[str, str]:
    """The whole policy in one place: outcome, route and the verdict recorded.

    ``verdict`` is :func:`screen`'s result, or ``None`` for a bypass kind.
    """

    if event_group[0].get("kind") in BYPASS_KINDS or verdict is None:
        return {"outcome": "escalated", "route": "bypass", "verdict": "none"}
    if verdict.get("status") != "screened":
        return {"outcome": "escalated", "route": "triage_failed", "verdict": "none"}
    if verdict.get("escalate"):
        return {"outcome": "escalated", "route": "triage", "verdict": "escalate"}
    if audit_selected([str(row["id"]) for row in event_group], share):
        return {"outcome": "escalated", "route": "audit", "verdict": "no_signal"}
    return {"outcome": "screened_out", "route": "triage", "verdict": "no_signal"}


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def triage_table_exists(connection: sqlite3.Connection) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_triage_decisions'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def screened_out_sql(alias: str = "e") -> str:
    """The anti-join every unjudged-event reader adds once the table exists."""

    return (f" AND NOT EXISTS (SELECT 1 FROM event_triage_decisions t "
            f"WHERE t.event_ref = {alias}.event_id AND t.outcome = 'screened_out')")


def _decode(row: Any) -> dict[str, Any]:
    wire = json.loads(row["record_json"])
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if (canonical_json(wire) != row["record_json"] or asserted != content_hash(body)
            or asserted != row["content_hash"]):
        raise EventTriageError("event triage record drifted")
    return wire


def triage_for(connection: sqlite3.Connection, event_ref: str) -> dict[str, Any] | None:
    if not triage_table_exists(connection):
        return None
    row = connection.execute(
        "SELECT * FROM event_triage_decisions WHERE event_ref=?", (event_ref,)).fetchone()
    return None if row is None else _decode(row)


def record_triage(
    store: Any, *, event_group: Sequence[Mapping[str, Any]], decision: Mapping[str, str],
    verdict: Mapping[str, Any] | None, mission: Mapping[str, Any], actor_ref: str,
    inherited_from: str | None = None,
) -> list[dict[str, Any]]:
    """One append-only row per member; an existing row is returned, never replaced.

    The call's cost is written on the primary's row only, so summing the
    column is what triage cost.
    """

    if decision["outcome"] not in OUTCOMES or decision["route"] not in ROUTES \
            or decision["verdict"] not in VERDICTS:
        raise EventTriageValidationError(f"invalid triage decision: {dict(decision)!r}")
    primary = event_group[0]
    model = dict((verdict or {}).get("model") or {}) or None
    written: list[dict[str, Any]] = []
    with store._transaction() as cur:
        for index, event in enumerate(event_group):
            existing = cur.execute(
                "SELECT * FROM event_triage_decisions WHERE event_ref=?", (event["id"],)
            ).fetchone()
            if existing is not None:
                written.append({**_decode(existing), "status": "duplicate"})
                continue
            cost = int((model or {}).get("cost_micros") or 0) if index == 0 else 0
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": "event-triage:" + content_hash({
                    "event_ref": event["id"], "event_hash": event.get("content_hash"),
                    "policy_ref": TRIAGE_POLICY_REF,
                })[:32],
                "created_at": _now(),
                "event_ref": event["id"],
                "event_hash": event.get("content_hash"),
                "event_kind": event.get("kind"),
                "primary_event_ref": primary["id"],
                "group_event_refs": [row["id"] for row in event_group],
                "company_ref": event["company_ref"],
                "outcome": decision["outcome"],
                "route": decision["route"],
                "verdict": decision["verdict"],
                "signals": list((verdict or {}).get("signals") or ()),
                "reason": (verdict or {}).get("reason"),
                "audit_share": str(AUDIT_SHARE),
                "policy_ref": TRIAGE_POLICY_REF,
                "contract_ref": TRIAGE_CONTRACT_VERSION,
                "model": model if index == 0 else (
                    None if model is None else {**model, "cost_micros": 0,
                                                "grouped_under_event_ref": primary["id"]}),
                "inherited_from": inherited_from,
                "mission_version_ref": mission["id"],
                "actor_ref": actor_ref,
            }
            record["content_hash"] = content_hash(record)
            cur.execute(
                "INSERT INTO event_triage_decisions(triage_id,event_ref,primary_event_ref,"
                "company_ref,outcome,route,verdict,cost_micros,policy_ref,"
                "mission_version_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], event["id"], primary["id"], event["company_ref"],
                 record["outcome"], record["route"], record["verdict"], cost,
                 TRIAGE_POLICY_REF, mission["id"], canonical_json(record),
                 record["content_hash"], actor_ref, record["created_at"]),
            )
            written.append({**record, "status": "fresh"})
    return written


def audit_report(connection: sqlite3.Connection, *, since: str | None = None) -> dict[str, Any]:
    """What triage decided, and on the audited groups what the judge then said.

    A *miss* is an audited group (triage said no_signal) whose judgement was
    anything but NO_CHANGE.  Read-only; used by the ops audit script.
    """

    if not triage_table_exists(connection):
        return {"present": False}
    params: list[Any] = []
    where = ""
    if since:
        where = " WHERE t.created_at >= ?"
        params.append(since)
    rows = connection.execute(
        "SELECT t.primary_event_ref AS primary_ref, t.event_ref AS event_ref, "
        "t.outcome AS outcome, t.route AS route, t.verdict AS verdict, "
        "t.cost_micros AS cost, j.decision AS decision "
        "FROM event_triage_decisions t LEFT JOIN event_judgements j "
        "ON j.event_ref = t.event_ref" + where, params).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    cost = 0
    for row in rows:
        cost += int(row["cost"] or 0)
        group = groups.setdefault(row["primary_ref"], {
            "outcome": row["outcome"], "route": row["route"], "verdict": row["verdict"],
            "decisions": set()})
        if row["decision"]:
            group["decisions"].add(row["decision"])
    by_route: dict[str, int] = {}
    misses: list[str] = []
    changed_escalated = 0
    audited_judged = 0
    for ref, group in groups.items():
        key = f"{group['outcome']}:{group['route']}"
        by_route[key] = by_route.get(key, 0) + 1
        changed = bool(group["decisions"] - {"NO_CHANGE"})
        if group["route"] == "audit" and group["decisions"]:
            audited_judged += 1
            if changed:
                misses.append(ref)
        elif group["outcome"] == "escalated" and changed:
            changed_escalated += 1
    return {
        "present": True,
        "groups": len(groups),
        "by_outcome_route": dict(sorted(by_route.items())),
        "triage_cost_micros": cost,
        "audited_groups_judged": audited_judged,
        "audit_misses": sorted(misses),
        "changes_caught_by_escalation": changed_escalated,
    }


__all__ = [
    "AUDIT_SHARE",
    "BYPASS_KINDS",
    "EventTriageError",
    "EventTriageValidationError",
    "OUTCOMES",
    "ROUTES",
    "TRIAGE_CONTRACT_VERSION",
    "TRIAGE_POLICY_REF",
    "TRIAGE_PURPOSE",
    "TRIAGE_SIGNALS",
    "VERDICTS",
    "audit_report",
    "audit_selected",
    "build_triage_prompt",
    "gate",
    "record_triage",
    "screen",
    "screened_out_sql",
    "triage_for",
    "triage_table_exists",
    "validate_triage_output",
]
