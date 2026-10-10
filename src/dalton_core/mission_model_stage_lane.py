"""Advance the deterministic stage records the playbook allows automation to write.

Three things, all without model calls (ADR-0011):

- **Deep Insight Gate, entered.**  A company whose Initial Screen passed and
  that has a gate draft on the chain has *entered* the Deep Insight Gate, and
  the stage ledger says so the moment the draft exists.  Before this, the only
  ``entered`` row was written by the person deciding, in the same breath as
  the decision, so a company whose draft had been returned twice (CTSH and DXC
  between 09-13 and 10-03) showed no Deep Insight stage at all.  Entering is
  not passing: ``gate_passed`` on this human checkpoint still needs a human
  principal, and ``record_stage`` refuses anything else.
- **industry_model / company_model, entered and decided.**  Each company's
  exit checks are evaluated every tick; a company that passes one gate is
  evaluated against the next in the same tick, so every company that qualifies
  is recorded on the first tick after a deploy rather than one per tick.
- **Why not.**  A company that does not pass is reported with the exact
  criteria it is missing, in the owner's words, in the tick reason.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from .coverage_mission import CoverageMissionError
from .lane_registry import LaneSpec, register_lane
from .model_stage_readiness import (
    company_model_readiness,
    criterion_label,
    industry_model_readiness,
)

ACTOR_REF = "automation:coverage-mission"
MODEL_STAGES = ("industry_model", "company_model")
DEEP_INSIGHT_STAGE = "deep_insight_gate"
STAGE_WORDS = {"industry_model": "行业模型", "company_model": "公司模型",
               DEEP_INSIGHT_STAGE: "深度认知门"}
# The tick ledger keeps a 200-character reason; the per-company keys below
# carry the rest.
REASON_LIMIT = 200


def _reader(connection: Any, authority_type: type[Any]) -> Any:
    authority = authority_type.__new__(authority_type)
    authority.connection = connection
    return authority


def _absent_table(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc)


def _latest(connection: Any, authority_type: type[Any], subject_ref: str) -> dict[str, Any] | None:
    """Read through an authority's checked public reader, without running DDL.

    A table that does not exist yet is an authority nothing has written to --
    a new workspace before its first framework or model -- and reads as
    "none", so the gate names the missing input instead of a read failure.
    """
    try:
        return _reader(connection, authority_type).latest(subject_ref)
    except sqlite3.OperationalError as exc:
        if _absent_table(exc):
            return None
        raise


def _latest_version(connection: Any, authority_type: type[Any],
                    subject_ref: str) -> dict[str, Any] | None:
    try:
        return _reader(connection, authority_type).latest_version(subject_ref)
    except sqlite3.OperationalError as exc:
        if _absent_table(exc):
            return None
        raise


def mission_version_refs(connection: Any, mission: Mapping[str, Any]) -> frozenset[str]:
    """Every version of the active mission's ``mission_ref`` (ADR-0008 addendum)."""

    refs = {str(mission["id"])}
    try:
        rows = connection.execute(
            "SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?",
            (mission["mission_ref"],)).fetchall()
        refs.update(str(row[0]) for row in rows)
    except Exception:  # noqa: BLE001 - a Core without the table still has the active one
        pass
    return frozenset(refs)


def latest_filed_period(connection: Any, company_ref: str) -> str | None:
    """The newest period end any financial-statement filing of this company covers."""

    try:
        row = connection.execute(
            "SELECT MAX(report_date) FROM coverage_mission_statement_filings "
            "WHERE company_ref=?", (company_ref,)).fetchone()
    except Exception:  # noqa: BLE001 - no statement authority yet: nothing filed
        return None
    return None if row is None or row[0] is None else str(row[0])


def _evaluate(connection: Any, mission: Mapping[str, Any], company_ref: str,
              stage_ref: str) -> dict[str, Any]:
    refs = mission_version_refs(connection, mission)
    try:
        if stage_ref == "industry_model":
            from .catalyst_calendar import CatalystCalendarAuthority
            from .industry_framework import IndustryFrameworkAuthority

            framework = _latest(connection, IndustryFrameworkAuthority,
                                str(mission["industry_ref"]))
            try:
                calendar = _latest_version(connection, CatalystCalendarAuthority, company_ref)
            except Exception:  # noqa: BLE001 - absent calendar authority = no calendar
                calendar = None
            return industry_model_readiness(
                framework, mission=mission, company_ref=company_ref,
                mission_version_refs=refs, update_calendar=calendar)
        from .forecast_sensitivity import SensitivityProjectionAuthority
        from .market_price import MarketPriceSeriesAuthority
        from .model_forecast_driver import ForecastModelAuthority
        from .valuation_snapshot import ValuationSnapshotAuthority

        model = _latest(connection, ForecastModelAuthority, company_ref)
        sensitivity = _latest(connection, SensitivityProjectionAuthority, company_ref)
        proof = (None if model is None else
                 _reader(connection, ForecastModelAuthority).filing_proof_recheck(model["id"]))
        try:
            valuation = _latest_version(connection, ValuationSnapshotAuthority, company_ref)
        except Exception:  # noqa: BLE001 - no valuation authority yet
            valuation = None
        try:
            price = _latest_version(connection, MarketPriceSeriesAuthority, company_ref)
        except Exception:  # noqa: BLE001 - no price authority yet
            price = None
        return company_model_readiness(
            model, sensitivity, mission=mission, company_ref=company_ref,
            filing_proof=proof, mission_version_refs=refs, valuation=valuation,
            current_price_ref=None if price is None else str(price.get("id")),
            latest_filed_period=latest_filed_period(connection, company_ref))
    except Exception as exc:
        reason = f"authority_read_failed:{type(exc).__name__}:{exc}"
        return {"passed": False, "checks": [], "reasons": [reason],
                "reason_labels": [criterion_label(reason)], "evidence_refs": []}


def latest_gate_draft(connection: Any, mission_refs: frozenset[str],
                      company_ref: str) -> str | None:
    """The newest Deep Insight Gate draft of this company under this mission."""

    try:
        rows = connection.execute(
            "SELECT version_id, record_json FROM deep_insight_gate_versions "
            "WHERE company_ref=? ORDER BY version_number DESC", (company_ref,)).fetchall()
    except Exception:  # noqa: BLE001 - no gate authority yet: no draft
        return None
    for row in rows:
        try:
            record = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        bound = (record.get("bindings") or {}).get("mission_version_ref") \
            or record.get("mission_version_ref")
        if bound in mission_refs:
            return str(row[0])
    return None


def _label(member: Mapping[str, Any]) -> str:
    return str(member.get("ticker") or str(member["company_ref"]).rsplit(":", 1)[-1])


def _enter(missions: Any, mission: Mapping[str, Any], company_ref: str, stage_ref: str,
           evidence: list[str], rationale: str, actor: str) -> None:
    missions.record_stage(
        mission_version_ref=mission["id"],
        mission_version_hash=mission["content_hash"],
        company_ref=company_ref, stage_ref=stage_ref, status="entered",
        evidence_refs=evidence, rationale=rationale, actor_ref=actor,
        idempotency_key=f"model-stage:{mission['mission_ref']}:{company_ref}:{stage_ref}:entered",
    )


def advance_once(missions: Any, connection: Any,
                 mission: Mapping[str, Any]) -> dict[str, Any]:
    """Advance every company as far as its deterministic gates allow."""
    if "stage_record" not in mission["autonomy"]["may_write"]:
        return {"status": "not_permitted", "reason": "mission lacks stage_record"}
    actor = mission["autonomy"]["automation_principal"]
    waiting: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []
    entered: list[dict[str, Any]] = []
    refs: frozenset[str] | None = None
    for member in mission["universe"]:
        try:
            refs = _advance_company(missions, connection, mission, member, actor, refs,
                                    waiting=waiting, advanced=advanced, entered=entered)
        except CoverageMissionError as exc:
            # One company's ladder refusing a write must not stop the others.
            try:
                stage_ref = missions.current_stage_state(
                    mission["mission_ref"], member["company_ref"])["next_stage"]
            except Exception:  # noqa: BLE001 - the refusal is the news, not this read
                stage_ref = None
            waiting.append({"company_ref": member["company_ref"], "label": _label(member),
                            "stage_ref": stage_ref if stage_ref in STAGE_WORDS
                            else "industry_model",
                            "readiness": {"passed": False, "checks": [],
                                          "reasons": [f"authority_read_failed:{exc}"],
                                          "evidence_refs": []}})
    return _result(mission, advanced=advanced, waiting=waiting, entered=entered)


def _advance_company(missions: Any, connection: Any, mission: Mapping[str, Any],
                     member: Mapping[str, Any], actor: str, refs: frozenset[str] | None,
                     *, waiting: list[dict[str, Any]], advanced: list[dict[str, Any]],
                     entered: list[dict[str, Any]]) -> frozenset[str] | None:
    company_ref = member["company_ref"]
    for _ in range(len(MODEL_STAGES) + 1):
        state = missions.current_stage_state(mission["mission_ref"], company_ref)
        stage_ref = state["next_stage"]
        if stage_ref == DEEP_INSIGHT_STAGE:
            if state["current_stage"] == DEEP_INSIGHT_STAGE:
                break
            if refs is None:
                refs = mission_version_refs(connection, mission)
            draft = latest_gate_draft(connection, refs, company_ref)
            if draft is not None:
                _enter(missions, mission, company_ref, DEEP_INSIGHT_STAGE, [draft],
                       "深度认知门十二问草稿已提交，进入本阶段，等待人工裁决。", actor)
                entered.append({"company_ref": company_ref,
                                "stage_ref": DEEP_INSIGHT_STAGE, "evidence_ref": draft})
            break
        if stage_ref not in MODEL_STAGES:
            break
        verdict = _evaluate(connection, mission, company_ref, stage_ref)
        if state["current_stage"] != stage_ref:
            _enter(missions, mission, company_ref, stage_ref, verdict["evidence_refs"],
                   f"Deterministic {stage_ref} exit checks began.", actor)
            entered.append({"company_ref": company_ref, "stage_ref": stage_ref})
        if not verdict["passed"]:
            waiting.append({"company_ref": company_ref, "label": _label(member),
                            "stage_ref": stage_ref, "readiness": verdict})
            break
        record = missions.record_stage(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=company_ref, stage_ref=stage_ref, status="gate_passed",
            evidence_refs=verdict["evidence_refs"],
            rationale=f"All deterministic {stage_ref} exit checks passed (ADR-0011).",
            actor_ref=actor,
            idempotency_key=(f"model-stage:{mission['mission_ref']}:{company_ref}:"
                             f"{stage_ref}:passed:{':'.join(verdict['evidence_refs'])}"),
        )
        advanced.append({"company_ref": company_ref, "stage_ref": stage_ref,
                         "stage_record_ref": record["id"], "readiness": verdict})
    return refs


def _result(mission: Mapping[str, Any], *, advanced: list[dict[str, Any]],
            waiting: list[dict[str, Any]], entered: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"advanced": advanced, "waiting": waiting, "entered": entered}
    for item in waiting:
        labels = item["readiness"].get("reason_labels") or [
            criterion_label(reason) for reason in item["readiness"]["reasons"]]
        result[f"waiting:{item['label']}"] = (
            f"{STAGE_WORDS[item['stage_ref']]}缺：" + "、".join(labels))[:REASON_LIMIT]
    if advanced:
        first = advanced[0]
        result.update(status="advanced", company_ref=first["company_ref"],
                      stage_ref=first["stage_ref"],
                      stage_record_ref=first["stage_record_ref"],
                      readiness=first["readiness"])
    elif waiting:
        first = waiting[0]
        result.update(status="waiting", company_ref=first["company_ref"],
                      stage_ref=first["stage_ref"], readiness=first["readiness"])
    elif entered:
        result.update(status="entered", company_ref=entered[0]["company_ref"],
                      stage_ref=entered[0]["stage_ref"])
    else:
        result.update(status="idle", reason="no deterministic model stage is due")
        return result
    parts = [f"{STAGE_WORDS[row['stage_ref']]}通过：" + _short(mission, row["company_ref"])
             for row in advanced]
    parts += [f"{_short(mission, row['company_ref'])} 进入{STAGE_WORDS[row['stage_ref']]}"
              for row in entered if row["stage_ref"] == DEEP_INSIGHT_STAGE]
    parts += [f"{item['label']} " + result[f"waiting:{item['label']}"] for item in waiting]
    result["reason"] = "；".join(parts)[:REASON_LIMIT]
    return result


def _short(mission: Mapping[str, Any], company_ref: str) -> str:
    member = next((row for row in mission["universe"]
                   if row["company_ref"] == company_ref), {"company_ref": company_ref})
    return _label(member)


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    del params
    row = server.store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "idle", "reason": "no active mission"}
    mission = server.coverage_mission.mission(row["mission_version_id"])
    return advance_once(server.coverage_mission, server.store.connection, mission)


LANE = register_lane(LaneSpec(
    operation="dispatch_model_stage_bridge", order=142,
    driver_key="model_stage_bridge", handler=dispatch,
    note=("Records Deep Insight Gate entry when a draft exists and deterministically "
          "closes industry_model and company_model on their ADR-0011 exit checks."),
))

__all__ = ["LANE", "advance_once", "dispatch", "latest_filed_period", "latest_gate_draft",
           "mission_version_refs"]
