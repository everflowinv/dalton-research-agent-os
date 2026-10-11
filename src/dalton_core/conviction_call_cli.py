"""P15d child: decide whether one company is worth a call, and write one.

Approval first, then the gate, then -- and only then -- a model.  The run
checks the mission's ``may_write`` and its human checkpoints before it spends
anything, because a proposal made against a scope that was never granted is
money burnt to produce a refusal, and a proposal made against a mission with
no ``conviction_call`` checkpoint would be a proposal nobody has agreed to
decide.

The gate is the interesting part and it is free.  ``precheck`` reads the
theses, the debate map, the forecast-versus-consensus bridge and the company
file's own variant view, and answers a mechanical question: do we hold a view,
is it different from the market's, and can we source what the market's is.  A
company that fails any of the three is reported with the reason and no model
is called.  On the live Core today every company fails at least one, which is
the honest state of the system rather than a bug.

Nothing here decides that a call *should* be published.  It assembles the
table, asks, verifies, and hands the result to the authority, which refuses a
second call on unchanged evidence and a second call in the same week.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any, Mapping

from .catalyst_calendar import CatalystCalendarAuthority
from .cockpit_model import CockpitModel, CockpitModelError
from .conviction_call import (
    CHECKPOINT_KIND,
    POLICY_HASH,
    POLICY_REF,
    WRITE_SCOPE,
    ConvictionCallAuthority,
    ConvictionCallValidationError,
    divergent_debates,
    evidence_fingerprint,
    precheck,
    precheck_v2,
    validate_consensus_gap,
    week_key,
)
from .conviction_policy import effective_policy
from .conviction_call_draft import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
    build_input_table,
    build_prompt,
    change_evidence,
    change_reason_for,
    draft_conviction_call,
    route_family,
)
from .coverage_mission import CoverageMissionAuthority
from .research_quality_rubrics import CONVICTION_CALL
from .scheduler import SchedulerError
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"

# How far ahead a catalyst is still part of a pathway.  A year out is not a
# pathway, it is a calendar; a quarter is the horizon a person can hold.
CATALYST_HORIZON_DAYS = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)


def granted(mission: Any) -> bool:
    return WRITE_SCOPE in set((mission.get("autonomy") or {}).get("may_write") or [])


def checkpointed(mission: Any) -> bool:
    """A proposal is only worth making where someone has agreed to decide it."""

    return CHECKPOINT_KIND in set(
        (mission.get("autonomy") or {}).get("human_checkpoints") or [])


def _table_exists(connection: Any, name: str) -> bool:
    if connection is None:
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# ---------------------------------------------------------------------------
# reading the inputs out of a Core
# ---------------------------------------------------------------------------

def company_theses(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """Every current thesis for this company, in P14a's own shape."""

    from .event_judgement import company_theses as read

    try:
        return [
            {**row, "thesis_version_ref": row.get("ref")}
            for row in read(store.connection, company_ref)
        ]
    except Exception:  # noqa: BLE001 - an unreadable thesis is "no thesis"
        return []


def open_debates(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """The live debates for this company, or none when there is no map.

    The authority is only constructed when its table is already there: opening
    it installs its schema, and a lane that reads a company file should not
    leave a debate-map schema behind on a Core that has never drawn one.
    """

    from .debate_map import DebateMapAuthority, table_exists

    if not table_exists(store.connection):
        return []
    try:
        return DebateMapAuthority(store).open_debates(company_ref)
    except Exception:  # noqa: BLE001 - an unreadable map is no map
        return []


def latest_dossier(store: Any, company_ref: str) -> dict[str, Any] | None:
    if not _table_exists(store.connection, "company_dossier_versions"):
        return None
    from .company_dossier import CompanyDossierAuthority

    try:
        return CompanyDossierAuthority(store).latest(company_ref)
    except Exception:  # noqa: BLE001 - an unreadable file is no file
        return None


def latest_valuation(store: Any, company_ref: str) -> dict[str, Any] | None:
    if not _table_exists(store.connection, "valuation_snapshot_versions"):
        return None
    from .valuation_snapshot import ValuationSnapshotAuthority

    try:
        return ValuationSnapshotAuthority(store).latest_version(company_ref)
    except Exception:  # noqa: BLE001
        return None


def upcoming_catalysts(store: Any, company_ref: str) -> list[dict[str, Any]]:
    if not _table_exists(store.connection, "catalyst_calendar_versions"):
        return []
    try:
        authority = CatalystCalendarAuthority(store)
        return [
            row for row in authority.upcoming(horizon_days=CATALYST_HORIZON_DAYS)
            if row.get("company_ref") == company_ref
        ]
    except Exception:  # noqa: BLE001
        return []


def consensus_gap(store: Any, company_ref: str) -> dict[str, Any]:
    """Our forecast against the street's, or the reason there is no street.

    P11b's consensus authority is Wave 2 and is not on this branch.  It is
    resolved by name at call time rather than imported, so that this lane
    starts working the day that authority lands without a change here, and
    reports an honest ``unavailable`` until then.  A gap section that quietly
    vanished when the source was missing would read as "we agree with
    consensus", which is the one thing a conviction call must never
    accidentally say.
    """

    try:
        from . import consensus_estimate  # type: ignore[attr-defined]
    except ImportError:
        return {"status": "unavailable", "metrics": [],
                "reason": "this Core has no consensus authority (P11b is not built yet), "
                          "so the forecast-versus-street gap cannot be computed"}
    reader = getattr(consensus_estimate, "latest_consensus", None)
    if reader is None:
        return {"status": "unavailable", "metrics": [],
                "reason": "the consensus module on this Core exposes no "
                          "latest_consensus reader"}
    try:
        found = reader(store, company_ref)
    except Exception as exc:  # noqa: BLE001 - an unreadable street is no street
        return {"status": "unavailable", "metrics": [],
                "reason": f"the consensus authority could not be read: "
                          f"{type(exc).__name__}: {exc}"}
    if not found:
        return {"status": "unavailable", "metrics": [],
                "reason": f"no consensus estimate is held for {company_ref}"}
    if not (found.get("metrics") or ()):
        unavailable = found.get("unavailable_periods") or ()
        if unavailable:
            reasons = sorted({
                str(row.get("reason") or "comparable model period unavailable")
                for row in unavailable if isinstance(row, Mapping)
            })
            detail = ", ".join(reasons[:3])
            return {"status": "unavailable", "metrics": [],
                    "reason": "consensus is held, but its comparable model "
                              f"periods are unavailable for {company_ref}: {detail}"}
        return {"status": "unavailable", "metrics": [],
                "reason": "consensus is held, but no comparable forecast model "
                          f"period is available for {company_ref}"}
    comparable,comparison_reason = comparable_consensus_metrics(store,company_ref,found["metrics"])
    if not comparable:
        return {"status":"unavailable","metrics":[],"reason":comparison_reason}
    gap = {"status": "available", "reason": None,
           "metrics": comparable}
    # Checked here, before the gate reads it and long before a model call is
    # paid for. The consensus authority is on the other side of a name lookup
    # and is not this slice's code; a row of the wrong shape reaching the gate
    # would either open a call on a number nobody can validate, or -- worse --
    # be discovered by the authority *after* two model calls had been spent.
    # Any shape problem degrades to the honest answer rather than raising: a
    # malformed street is a street we cannot read.
    try:
        return validate_consensus_gap(gap)
    except ConvictionCallValidationError as exc:
        return {"status": "unavailable", "metrics": [],
                "reason": f"the consensus authority returned a shape this Core "
                          f"cannot read: {exc}"}


def gate_inputs(store: Any, company_ref: str) -> dict[str, Any]:
    """Everything the deterministic gate reads, in one place."""

    dossier = latest_dossier(store, company_ref)
    return {
        "theses": company_theses(store, company_ref),
        "open_debates": open_debates(store, company_ref),
        "consensus_gap": consensus_gap(store, company_ref),
        "dossier": dossier,
        "dossier_variant_view": None if dossier is None else dossier.get("variant_view"),
        "catalysts": upcoming_catalysts(store, company_ref),
        "valuation": latest_valuation(store, company_ref),
    }


def fingerprint_of(inputs: dict[str, Any], gate: dict[str, Any]) -> str:
    """The exact material this call would be drawn from."""

    refs = [row["thesis_version_ref"] for row in inputs["theses"]
            if row.get("thesis_version_ref")]
    refs += [row["debate_ref"] for row in gate["divergent_debates"] if row.get("debate_ref")]
    refs += [ref for row in gate["consensus_gaps"] for ref in row.get("refs") or ()]
    refs += [row["entry_ref"] for row in inputs["catalysts"] if row.get("entry_ref")]
    if inputs["dossier"] is not None:
        refs.append(inputs["dossier"]["id"])
    if inputs["valuation"] is not None:
        refs.append(inputs["valuation"]["id"])
    return evidence_fingerprint(refs)


# ---------------------------------------------------------------------------
# Conviction v2 (ADR-0013): readers, fingerprint, the daily drafting cap
# ---------------------------------------------------------------------------

def active_gate(connection: Any) -> str:
    """``v2`` unless a test (or a future rollback version) says otherwise."""

    return str(effective_policy(connection)["policy"].get("gate") or "v2")


def company_theses_v2(store: Any, company_ref: str,
                      industry_ref: str | None = None) -> list[dict[str, Any]]:
    """Every current thesis for this company, labelled company- or industry-level.

    The admission candidates name each thesis's subjects; a thesis is the
    company's own when one of them is this company, and an industry thesis
    when one of them is the mission's industry.  Unlike the event-judgement
    reader this does not depend on which candidate row happened to be read
    last, because the policy's ``thesis_scope`` turns on the difference.
    """

    connection = store.connection
    if not _table_exists(connection, "thesis_versions"):
        return []
    subjects: dict[str, set[str]] = {}
    if _table_exists(connection, "thesis_admission_candidates"):
        for row in connection.execute(
                "SELECT thesis_ref, company_ref FROM thesis_admission_candidates"):
            subjects.setdefault(str(row["thesis_ref"]), set()).add(str(row["company_ref"]))
    rows = connection.execute(
        "SELECT v.version_id, v.created_at, v.content_json FROM thesis_versions v "
        "WHERE v.version_number = (SELECT MAX(version_number) FROM thesis_versions w "
        "WHERE w.thesis_id = v.thesis_id) ORDER BY v.created_at, v.version_id").fetchall()
    found: list[dict[str, Any]] = []
    for row in rows:
        try:
            wire = json.loads(row["content_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        named = subjects.get(str(wire.get("thesis_ref")), set())
        if company_ref in named:
            kind = "company"
        elif any(ref.startswith("industry:") and (industry_ref is None or ref == industry_ref)
                 for ref in named):
            kind = "industry"
        else:
            continue
        found.append({
            "ref": row["version_id"], "thesis_version_ref": row["version_id"],
            "thesis_ref": wire.get("thesis_ref"),
            "statement": str(wire.get("statement") or "")[:1200],
            "confidence": wire.get("confidence"),
            "falsifier_refs": list(wire.get("falsifier_refs") or ()),
            "claim_refs": [str(ref) for ref in wire.get("claim_refs") or ()][:12],
            "subject_kind": kind, "created_at": row["created_at"],
        })
    return found


def latest_price_ref(store: Any, company_ref: str) -> str | None:
    if not _table_exists(store.connection, "market_price_series_versions"):
        return None
    row = store.connection.execute(
        "SELECT version_id FROM market_price_series_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    return None if row is None else str(row["version_id"])


def street_view(store: Any, company_ref: str,
                valuation: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The street's price target and ratings, read off the consensus authority."""

    if not _table_exists(store.connection, "consensus_estimate_versions"):
        return None
    row = store.connection.execute(
        "SELECT version_id, record_json FROM consensus_estimate_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    if row is None:
        return None
    try:
        record = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    target = record.get("price_target") or None
    ratings = (record.get("recommendations") or [None])[0]
    if not target and not ratings:
        return None
    return {"id": str(row["version_id"]), "as_of": record.get("as_of"),
            "close": ((valuation or {}).get("price") or {}).get("close"),
            "price_target": target, "recommendations": ratings}


def held_scenarios(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """The join point with ADR-0012's ``ScenarioVersion`` (another branch).

    Resolved by name at call time, never imported, so this lane does not
    depend on that code: when ``dalton_core.scenario_version`` exists and
    exposes ``conviction_scenarios(store, company_ref)`` returning rows of
    ``{ref, name, probability, payoff, trigger}`` (probability in (0, 1],
    payoff a signed per cent over the call's horizon, either may be ``None``),
    those rows are shown to the drafter as held scenarios and any it uses keep
    their numbers.  Until then -- or if the reader raises -- there are none,
    and the drafter writes its own table.
    """

    try:
        from . import scenario_version  # type: ignore[attr-defined]
    except ImportError:
        return []
    reader = getattr(scenario_version, "conviction_scenarios", None)
    if reader is None:
        return []
    try:
        rows = reader(store, company_ref) or []
    except Exception:  # noqa: BLE001 - an unreadable scenario is no scenario
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping) and row.get("ref")]


def gate_inputs_v2(store: Any, company_ref: str, *, industry_ref: str | None = None
                   ) -> dict[str, Any]:
    dossier = latest_dossier(store, company_ref)
    valuation = latest_valuation(store, company_ref)
    return {
        "theses": company_theses_v2(store, company_ref, industry_ref),
        "valuation": valuation,
        "latest_price_ref": latest_price_ref(store, company_ref),
        "open_debates": open_debates(store, company_ref),
        "consensus_gap": consensus_gap(store, company_ref),
        "street": street_view(store, company_ref, valuation),
        "dossier": dossier,
        "dossier_variant_view": None if dossier is None else dossier.get("variant_view"),
        "catalysts": upcoming_catalysts(store, company_ref),
        "held_scenarios": held_scenarios(store, company_ref),
    }


def precheck_for(inputs: Mapping[str, Any], company_ref: str,
                 policy: Mapping[str, Any]) -> dict[str, Any]:
    return precheck_v2(company_ref=company_ref, theses=inputs["theses"],
                       valuation=inputs["valuation"],
                       latest_price_ref=inputs["latest_price_ref"],
                       policy_effective=policy)


def fingerprint_v2(inputs: Mapping[str, Any], gate: Mapping[str, Any]) -> str:
    """The material a v2 call is drawn from, bucketed so a daily price is not news.

    The valuation snapshot, the consensus and the street's target are
    republished every trading day; naming their exact versions would make
    every day new evidence and re-draft a company the policy refused
    yesterday.  They enter as (kind, ISO week); theses, debates, the company
    file, catalysts, held scenarios and the policy itself enter exactly.
    """

    refs = [row["thesis_version_ref"] for row in gate["theses"]]
    refs += [str(row.get("debate_ref")) for row in inputs["open_debates"]
             if row.get("debate_ref")]
    refs += [row["entry_ref"] for row in inputs["catalysts"] if row.get("entry_ref")]
    refs += [row["ref"] for row in inputs["held_scenarios"]]
    if inputs["dossier"] is not None:
        refs.append(inputs["dossier"]["id"])
    valuation = inputs["valuation"] or {}
    if valuation.get("as_of"):
        refs.append(f"valuation-week:{week_key(str(valuation['as_of']))}")
    if (inputs["consensus_gap"] or {}).get("status") == "available":
        refs.append(f"consensus-week:{week_key(_now())}")
    if inputs["street"]:
        refs.append(f"street-week:{week_key(str(inputs['street'].get('as_of') or _now()))}")
    refs.append(f"conviction-policy:{gate['policy_hash']}")
    return evidence_fingerprint(refs)


def valuation_binding(valuation: Mapping[str, Any]) -> dict[str, Any]:
    price = valuation.get("price") or {}
    return {"snapshot_ref": str(valuation["id"]),
            "snapshot_hash": str(valuation["content_hash"]),
            "as_of": str(valuation.get("as_of"))[:10],
            "price_version_ref": price.get("version_ref"),
            "close": None if price.get("close") is None else str(price.get("close"))}


def drafting_spend_today(state_dir: Path, *, today: str | None = None,
                         exclude: Path | None = None) -> dict[str, Any]:
    """What the conviction lane has spent on drafting today (UTC), from disk.

    Read off the runs' own summaries rather than a coordinator's memory, so a
    restart cannot reset the day's budget.  A run counts as a draft when it
    called a model.
    """

    day = today or _now()[:10]
    spent, drafts = 0, 0
    # Batch u (2026-10-07): a run whose draft was a replay of an answer
    # already paid for (same prompt, same request) recorded cost_micros=0
    # with model_called=true, and a call paid outside a lane run (09:54,
    # US$0.30 on the budget ledger) never reached the cap at all.  Runs that
    # list their model calls are counted per work order, once, at the larger
    # of what a run paid and what the budget ledger booked, on the day it
    # was booked.
    by_work_order: dict[str, int] = {}
    root = Path(state_dir) / "conviction-call-runs"
    try:
        paths = list(root.glob("*/summary.json"))
    except OSError:
        paths = []
    for path in paths:
        if exclude is not None and path.parent == exclude:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(summary.get("created_at") or "")[:10] != day:
            continue
        cost = int(summary.get("cost_micros") or 0)
        calls = summary.get("model_calls")
        if isinstance(calls, list) and calls:
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                paid = int(call.get("cost_micros") or 0)
                booked = int(call.get("settled_cost_micros") or 0)
                if booked and str(call.get("settled_day") or day) != day:
                    booked = 0  # paid on another day; that day's cap counted it
                ref = call.get("work_order_ref")
                amount = max(paid, booked)
                if ref:
                    by_work_order[str(ref)] = max(by_work_order.get(str(ref), 0), amount)
                else:
                    spent += amount
        else:
            spent += cost
        if summary.get("model_called") or cost > 0:
            drafts += 1
    spent += sum(by_work_order.values())
    return {"day": day, "cost_micros": spent, "drafts": drafts}


def daily_cap_hold(spend: Mapping[str, Any], policy: Mapping[str, Any]) -> str | None:
    cap_micros = int(Decimal(str(policy["daily_drafting_cost_cap_usd"])) * 1_000_000)
    if int(policy["max_drafts_per_day"]) <= spend["drafts"]:
        return (f"今天已起草 {spend['drafts']} 次投资建议，达到策略上限 "
                f"{policy['max_drafts_per_day']} 次")
    if spend["cost_micros"] >= cap_micros:
        return (f"今天的投资建议起草预算已用完（US${spend['cost_micros'] / 1e6:.2f} / "
                f"US${policy['daily_drafting_cost_cap_usd']}）")
    return None


def _run_v2(*, store: Any, mission: Mapping[str, Any], actor_ref: str, company_ref: str,
            summary: dict[str, Any], summary_dir: Path, state_dir: Path,
            model_config_path: Path | None, scheduler_db: Path | None,
            dry_run: bool) -> dict[str, Any]:
    from .conviction_call_draft_v2 import build_input_table_v2, build_prompt_v2, \
        draft_conviction_call_v2

    policy = effective_policy(store.connection)
    summary.update({"gate_version": "v2", "policy_ref": policy["version_ref"],
                    "policy_hash": policy["policy_hash"],
                    "policy_source": policy["source"]})
    inputs = gate_inputs_v2(store, company_ref, industry_ref=mission.get("industry_ref"))
    gate = precheck_for(inputs, company_ref, policy)
    summary.update({"eligible": gate["eligible"], "gate_reasons": list(gate["reasons"]),
                    "divergent_debates": len(divergent_debates(inputs["open_debates"]))})
    if not gate["eligible"]:
        summary.update({"status": "succeeded", "call_status": "not_eligible",
                        "failure_reason": "；".join(gate["reason_labels"])})
        return summary
    if len(inputs["held_scenarios"])!=3:
        summary.update(status="succeeded",call_status="not_eligible",eligible=False,
            failure_reason="完整且当前的三情景 EPS×PE 收益尚不可用；不使用手填回报替代")
        return summary
    fingerprint = fingerprint_v2(inputs, gate)
    summary["evidence_fingerprint"] = fingerprint
    table = build_input_table_v2(
        company_ref=company_ref, precheck_record=gate, theses=gate["theses"],
        open_debates=inputs["open_debates"], consensus_gap=inputs["consensus_gap"],
        street=inputs["street"], dossier_variant_view=inputs["dossier_variant_view"],
        dossier_version_ref=None if inputs["dossier"] is None else inputs["dossier"]["id"],
        catalyst_entries=inputs["catalysts"], valuation=inputs["valuation"],
        held_scenarios=inputs["held_scenarios"], policy_effective=policy)
    summary["prompt_bytes"] = len(build_prompt_v2(table).encode("utf-8"))
    if dry_run:
        summary.update({"status": "succeeded", "call_status": "dry_run"})
        return summary
    if model_config_path is None:
        summary.update({"status": "succeeded", "call_status": "gated",
                        "failure_reason": "no model configured"})
        return summary
    held = daily_cap_hold(drafting_spend_today(state_dir, exclude=summary_dir),
                          policy["policy"])
    if held is not None:
        summary.update({"status": "held", "call_status": "daily_cap",
                        "failure_reason": held})
        return summary
    authority = ConvictionCallAuthority(store)
    previous = authority.current(company_ref)
    config = json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8"))
    router_db = config.get("model_router_db")
    model = CockpitModel(
        config, scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
        max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)
    created_at = _now()
    try:
        drafted = draft_conviction_call_v2(
            table=table, valuation_binding=valuation_binding(inputs["valuation"]),
            model=model, mission=mission,
            family_of=(lambda ref: None if router_db is None else route_family(router_db, ref)),
            rubric_ref=CONVICTION_CALL.rubric_ref, rubric_hash=CONVICTION_CALL.content_hash)
    except SchedulerError as exc:
        summary.update({"status": "succeeded", "call_status": "busy",
                        "failure_reason": f"{type(exc).__name__}: {exc}"})
        return summary
    except CockpitModelError as exc:
        summary.update({"status": "failed", "call_status": "model_unavailable",
                        "failure_reason": f"{type(exc).__name__}: {exc}"})
        return summary
    summary["cost_micros"] = int(drafted.get("cost_micros") or 0)
    summary["model_called"] = bool(drafted.get("model_called"))
    summary["model_calls"] = list(drafted.get("model_calls") or [])
    summary["rubric_findings"] = list(drafted["rubric"]["findings"])
    if drafted.get("gate"):
        summary["policy_refusals"] = list(drafted["gate"]["reasons"])
        if drafted.get("call"):
            summary["expected_return"] = drafted["call"]["expected_return"]
    if drafted["status"] != "verified":
        summary.update({
            # A governed answer -- the policy refused the draft, or the drafter
            # said "no call today" -- is a completed run, not a failure.
            "status": ("succeeded" if drafted["status"] in ("policy_refused", "no_call")
                       else "failed"),
            "call_status": drafted["status"], "failure_reason": drafted.get("reason")})
        return summary
    call = drafted["call"]
    refs = change_evidence(call, previous)
    if not refs:
        summary.update({"status": "succeeded", "call_status": "duplicate",
                        "failure_reason": "this call cites nothing the standing one "
                                          "does not already cite"})
        return summary
    published = authority.propose_v2(
        company_ref=company_ref, direction=call["direction"], decision=call["decision"],
        time_horizon=call["time_horizon"], our_view=call["our_view"],
        evidence_confidence=call["evidence_confidence"], scenarios=call["scenarios"],
        market_view_alignment=call["market_view_alignment"],
        consensus_gap=call["consensus_gap"], valuation=call["valuation"],
        invalidators=call["invalidators"], thesis_refs=call["thesis_refs"],
        debate_refs=call["debate_refs"], evidence_fingerprint=fingerprint,
        change_reason=change_reason_for(previous), change_evidence_refs=refs,
        precheck_record=gate, rubric=drafted["rubric"], policy_effective=policy,
        mission=mission, actor_ref=actor_ref, created_at=created_at,
        drafted_by=drafted["drafted_by"], verified_by=drafted["verified_by"])
    summary.update({"status": "succeeded", "call_status": published["status"],
                    "proposal_ref": published.get("id"),
                    "version": published.get("version"),
                    "direction": published.get("direction"),
                    "failure_reason": published.get("reason")})
    return summary


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def run_conviction_call(
    *,
    state_dir: Path,
    company_ref: str,
    summary_dir: Path,
    model_config_path: Path | None = None,
    scheduler_db: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "mode": "dry_run" if dry_run else "model",
        "company_ref": company_ref,
        "call_status": None,
        "proposal_ref": None,
        "version": None,
        "direction": None,
        "eligible": None,
        "gate_reasons": [],
        "evidence_fingerprint": None,
        "divergent_debates": 0,
        "consensus_gaps": 0,
        "cost_micros": 0,
        "prompt_bytes": 0,
        "rubric_findings": [],
        "failure_reason": None,
        "gate_version": None,
        "model_called": False,
        "policy_refusals": [],
        "expected_return": None,
        "policy_ref": POLICY_REF,
        "policy_hash": POLICY_HASH,
        "rubric_ref": CONVICTION_CALL.rubric_ref,
        "rubric_hash": CONVICTION_CALL.content_hash,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "call_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        if not granted(mission):
            summary.update({
                "status": "held", "call_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "在授权前不起草，免得白花模型调用"),
            })
            return summary
        if not checkpointed(mission):
            summary.update({
                "status": "held", "call_status": "no_checkpoint",
                "failure_reason": (
                    f"任务 {mission['id']} 没有 {CHECKPOINT_KIND} 人类检查点；"
                    "没人答应裁决的提案不值得提"),
            })
            return summary
        actor_ref = mission["autonomy"]["automation_principal"]

        if active_gate(store.connection) == "v2":
            # ADR-0013: every workspace drafts under the v2 policy in force
            # (the code default until the owner publishes one).
            return _run_v2(store=store, mission=mission, actor_ref=actor_ref,
                           company_ref=company_ref, summary=summary,
                           summary_dir=summary_dir, state_dir=state_dir,
                           model_config_path=model_config_path,
                           scheduler_db=scheduler_db, dry_run=dry_run)
        summary["gate_version"] = "v1"
        inputs = gate_inputs(store, company_ref)
        gate = precheck(
            company_ref=company_ref,
            theses=inputs["theses"],
            open_debates=inputs["open_debates"],
            consensus_gap=inputs["consensus_gap"],
            dossier_variant_view=inputs["dossier_variant_view"],
        )
        summary.update({
            "eligible": gate["eligible"],
            "gate_reasons": list(gate["reasons"]),
            "divergent_debates": len(gate["divergent_debates"]),
            "consensus_gaps": len(gate["consensus_gaps"]),
        })
        if not gate["eligible"]:
            # The cheapest refusal there is, and the most common one. Agreeing
            # with the market has no value, so a company that cannot be shown
            # to differ from it is not drafted at all.
            summary.update({"status": "succeeded", "call_status": "not_eligible",
                            "failure_reason": ", ".join(gate["reasons"])})
            return summary

        fingerprint = fingerprint_of(inputs, gate)
        summary["evidence_fingerprint"] = fingerprint
        table = build_input_table(
            company_ref=company_ref,
            precheck_record=gate,
            theses=inputs["theses"],
            open_debates=inputs["open_debates"],
            consensus_gap=inputs["consensus_gap"],
            dossier_variant_view=inputs["dossier_variant_view"],
            dossier_version_ref=None if inputs["dossier"] is None else inputs["dossier"]["id"],
            catalyst_entries=inputs["catalysts"],
            valuation=inputs["valuation"],
        )
        summary["prompt_bytes"] = len(build_prompt(table).encode("utf-8"))
        if dry_run:
            summary.update({"status": "succeeded", "call_status": "dry_run"})
            return summary
        if model_config_path is None:
            summary.update({"status": "succeeded", "call_status": "gated",
                            "failure_reason": "no model configured"})
            return summary

        authority = ConvictionCallAuthority(store)
        previous = authority.current(company_ref)
        config = json.loads(
            Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        # Fail closed on a configuration with no router: ``route_family`` reads
        # the routing authority to establish which family each call ran on, and
        # a missing key here would raise out of the lambda *after* the drafting
        # call was paid for. ``None`` makes the family unknowable, and an
        # unknowable family is refused by the independence predicate -- which
        # is the outcome we want when we cannot tell.
        router_db = config.get("model_router_db")
        model = CockpitModel(
            config, scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        created_at = _now()
        try:
            drafted = draft_conviction_call(
                table=table, model=model, mission=mission,
                family_of=(lambda ref: None if router_db is None
                           else route_family(router_db, ref)),
                rubric_ref=CONVICTION_CALL.rubric_ref,
                rubric_hash=CONVICTION_CALL.content_hash,
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "call_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "failed", "call_status": "model_unavailable",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["cost_micros"] = int(drafted.get("cost_micros") or 0)
        summary["model_called"] = drafted.get("drafted_by") is not None
        summary["rubric_findings"] = list(drafted["rubric"]["findings"])
        if drafted["status"] != "verified":
            run_status = (
                "succeeded"
                if drafted["status"] == "no_variant_view"
                else "failed"
            )
            summary.update({"status": run_status, "call_status": drafted["status"],
                            "failure_reason": drafted.get("reason")})
            return summary
        call = drafted["call"]
        refs = change_evidence(call, previous)
        if not refs:
            # ADR-0008: a version exists only when it can say what it learned.
            summary.update({"status": "succeeded", "call_status": "duplicate",
                            "failure_reason": "this call cites nothing the "
                                              "standing one does not already cite"})
            return summary
        published = authority.propose(
            company_ref=company_ref,
            direction=call["direction"],
            decision=call["decision"],
            confidence=call["confidence"],
            time_horizon=call["time_horizon"],
            variant_view=call["variant_view"],
            consensus_gap=call["consensus_gap"],
            event_pathway=call["event_pathway"],
            risk_reward=call["risk_reward"],
            falsifiers=call["falsifiers"],
            thesis_refs=call["thesis_refs"],
            debate_refs=call["debate_refs"],
            evidence_fingerprint=fingerprint,
            change_reason=change_reason_for(previous),
            change_evidence_refs=refs,
            precheck_record=gate,
            rubric=drafted["rubric"],
            mission=mission,
            actor_ref=actor_ref,
            created_at=created_at,
            drafted_by=drafted["drafted_by"],
            verified_by=drafted["verified_by"],
        )
        summary.update({
            "status": "succeeded", "call_status": published["status"],
            "proposal_ref": published.get("id"),
            "version": published.get("version"),
            "direction": published.get("direction"),
            "failure_reason": published.get("reason"),
        })
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Propose one company's conviction call, or say why not")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--model-config")
    parser.add_argument("--scheduler-db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_conviction_call(
        state_dir=Path(args.state_dir),
        company_ref=args.company_ref,
        summary_dir=Path(args.summary_dir),
        model_config_path=None if args.model_config is None else Path(args.model_config),
        scheduler_db=None if args.scheduler_db is None else Path(args.scheduler_db),
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


__all__ = [
    "active_gate",
    "company_theses_v2",
    "daily_cap_hold",
    "drafting_spend_today",
    "fingerprint_v2",
    "gate_inputs_v2",
    "held_scenarios",
    "latest_price_ref",
    "precheck_for",
    "street_view",
    "valuation_binding",
    "CATALYST_HORIZON_DAYS",
    "SUMMARY_SCHEMA_VERSION",
    "WRITE_SCOPE",
    "build_parser",
    "checkpointed",
    "company_theses",
    "consensus_gap",
    "fingerprint_of",
    "gate_inputs",
    "granted",
    "latest_dossier",
    "latest_valuation",
    "main",
    "open_debates",
    "run_conviction_call",
    "upcoming_catalysts",
]


def comparable_consensus_metrics(store,company_ref,metrics,*,today=None,max_age_days=30):
    """A gap may drive conviction only after current seasonal/calendar/basis checks."""
    from datetime import date
    from .model_forecast_driver import validate_new_forecast_rules
    con=store.connection
    if not _table_exists(con,"consensus_estimate_versions") or not _table_exists(con,"forecast_model_versions"):
        return [],"held consensus/model authority is unavailable for gap verification"
    row=con.execute("SELECT record_json FROM consensus_estimate_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1",(company_ref,)).fetchone()
    modelrow=con.execute("SELECT record_json FROM forecast_model_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1",(company_ref,)).fetchone()
    if row is None or modelrow is None:return [],"held consensus/model authority is unavailable"
    held=json.loads(row[0]);model=json.loads(modelrow[0]);day=today or datetime.now(timezone.utc).date()
    try:
        observed=date.fromisoformat(held["as_of"])
        if not 0<=(day-observed).days<=max_age_days:return [],"consensus observation is stale or future-dated"
        validate_new_forecast_rules(model)
    except (ValueError,KeyError) as exc:return [],"forecast seasonality/reporting boundary is unverified: "+str(exc)
    current_model=model.get("id");current_consensus=held.get("id") or held.get("version_ref")
    valid=[]
    for item in metrics:
        refs=set(item.get("refs") or [])
        if current_model not in refs or current_consensus not in refs:continue
        unit=str(item.get("unit") or "").lower();currency=str(model.get("currency") or "").lower()
        metric=str(item.get("metric") or "").lower()
        if "eps" in metric or "每股" in metric:
            if held.get("earnings_basis")!="gaap":continue
            if unit not in {currency+"_per_share",currency+"/share"}:continue
        elif "revenue" in metric or "收入" in metric:
            if unit!=currency:continue
        else:continue
        valid.append(dict(item))
    return valid,None if valid else "no current gap has matching fiscal periods, units and explicit earnings basis; adjusted/unknown EPS is not compared with GAAP EPS"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
