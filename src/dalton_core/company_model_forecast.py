"""P13-M2: one company's driver model, built and published.

The pieces exist separately for good reasons -- the input table is derived and
stored nowhere, the driver model is a versioned judgement, a forecast line is
an Outcome-facing figure the reconciler will grade -- and this is the one place
that walks all three in order:

    specification + filings -> input table -> driver model version
                                           -> forecast lines the reconciler reads

Two things are worth saying about the last arrow.

**The lines are published through the existing authority, not beside it.**
``forecast_reconciliation`` already knows how to pair a ``ForecastLineVersion``
with the formal Claim that reports the actual, threshold the deviation and name
the human checkpoint. When Accenture reports on the first of October, that
machinery has to work on these numbers without being taught anything new, so
the driver model's revenue line is written as an ordinary
``model_forecast_line_versions`` row under a second frozen formula, binding the
exact model version it came out of.

**Only what the reconciler has a binding for is published.** A model computes
gross profit, operating income and free cash flow too, and they are all in the
model version; they are not published as forecast lines because there is no
frozen actual to grade them against yet. Publishing them would create rows that
look reconcilable and never are.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Mapping

from .company_model_inputs import build_model_inputs
from .forecast_reconciliation import METRIC_BINDINGS
from .model_forecast import (
    DRIVER_FORMULA_HASH,
    DRIVER_FORMULA_REF,
    ModelForecastAuthority,
)
from .model_forecast_driver import (
    AUTOMATION_ACTOR,
    GENERATOR_REF,
    REVENUE,
    ForecastModelAuthority,
    ForecastModelUnavailable,
    SOURCE_VERSION_KEY,
    actualize_model,
    build_forecast_model,
    model_readiness,
    realised_ends,
)
from .store import canonical_json, content_hash

# Which computed result becomes a forecast line, and in what shape. The metric
# ref has to be one the reconciler binds or the line is unreconcilable; the
# intersection below is taken at import so a change to either side shows up as
# nothing being published rather than as rows nobody grades.
RESULT_METRICS: dict[str, dict[str, str]] = {
    REVENUE: {"metric_or_aspect": "metric:revenue-usd", "unit": "one",
              "currency": "USD"},
}
PUBLISHED_METRICS: dict[str, dict[str, str]] = {
    role: binding for role, binding in RESULT_METRICS.items()
    if binding["metric_or_aspect"] in METRIC_BINDINGS
}
# The filed unit a forecast line's scale name corresponds to. The reconciler
# rescales the actual by the line's unit, so a unit it cannot scale is refused
# rather than assumed to be dollars.
UNIT_SCALES: dict[str, str] = {"usd": "one"}
WRITE_SCOPE = "forecast_line"


class ForecastPublishRefused(RuntimeError):
    """The model was built and its lines were not published, and why."""


def _slug(company_ref: str) -> str:
    return str(company_ref).split(":")[-1]


def missing_write_scope(mission: Mapping[str, Any] | None, scope: str = WRITE_SCOPE) -> str | None:
    """The reason this mission may not write, or nothing."""

    if not isinstance(mission, Mapping):
        return "no mission is configured"
    autonomy = mission.get("autonomy") or {}
    if scope not in (autonomy.get("may_write") or []):
        return f"the mission does not grant the {scope} write scope"
    return None


def inputs_hash(table: Mapping[str, Any]) -> str:
    """The input table's hash, computed the one way the record computes it."""

    return content_hash(json.loads(canonical_json(table)))


def model_digest(spec: Mapping[str, Any], table: Mapping[str, Any]) -> str:
    """What a model run is *about*: this specification over these filings.

    The lane names its child by this, so a tick that fires while nothing has
    moved is the same run rather than a second one.
    """

    return content_hash({
        "spec_ref": str(spec.get("spec_id") or ""),
        "spec_hash": str(spec.get("content_hash") or ""),
        "inputs_hash": inputs_hash(table),
        "generator_ref": GENERATOR_REF,
        "formula_hash": DRIVER_FORMULA_HASH,
    })


def pending_action(
    prior: Mapping[str, Any] | None, spec: Mapping[str, Any],
    table: Mapping[str, Any],
) -> str | None:
    """What this lane may do for this company on this tick, if anything.

    A company with no model gets its first one. A newly authorized
    specification gets its own model identity. A company whose current model
    estimated a quarter the filings have now covered gets those estimates
    answered -- the actual written down beside them, the future untouched.

    What this never returns is "the world moved, re-forecast": a new Claim, a
    news item, a broker note, even a new filing, do not by themselves change
    what this system thinks the next four quarters look like. That is a
    judgement, it belongs to the event layer, and it arrives here as an
    explicit ``revise_assumptions`` call with a reason and the evidence behind
    it. A lane that quietly re-forecast on every document would leave no
    record of what it thought and when, which is the only thing that makes a
    forecast worth grading.
    """

    if prior is None:
        return "first"
    if str(prior.get("spec_ref")) != str(spec.get("spec_id")):
        # The specification is a judgement about how to model the company. A
        # new one starts a new model identity; it is not an actualisation of
        # the old specification's model.
        return "new_specification"
    return "actualize" if realised_ends(prior, table) else None


def publish_forecast_lines(
    authority: ModelForecastAuthority,
    record: Mapping[str, Any],
    *,
    actor_ref: str = AUTOMATION_ACTOR,
) -> list[dict[str, Any]]:
    """Publish the model's reconcilable *estimates* as forecast line versions.

    Three filters, each of them load-bearing:

    * only roles the reconciler has a frozen actual binding for;
    * only ``estimate`` cells for quarters still ahead. An actual is not a
      forecast, and a realised quarter's estimate was published when it was
      still a forecast -- republishing it under a later model version would
      quietly move the thing the reconciler is about to grade;
    * only values that changed. A model version that added an actual did not
      change next year's forecast, and writing an identical line version would
      say it had.
    """

    scale = UNIT_SCALES.get(str(record.get("unit") or "").lower())
    if scale is None:
        raise ForecastPublishRefused(
            f"filed unit {record.get('unit')!r} has no forecast line scale")
    company_ref = str(record["company_ref"])
    version_ref = str(record["id"])
    ahead = {str(item["end"]) for item in (record.get("forecast_periods") or [])}
    published: list[dict[str, Any]] = []
    by_role = {str(item.get("role")): item for item in (record.get("results") or [])}
    for role, binding in sorted(PUBLISHED_METRICS.items()):
        result = by_role.get(role)
        if result is None:
            continue
        for cell in result.get("cells") or []:
            end = str(cell["period"]["end"])
            if (cell.get("status") != "computed" or cell.get("kind") != "estimate"
                    or cell.get("superseded_by") or end not in ahead):
                continue
            line_ref = (
                f"forecast-line:{_slug(company_ref)}:"
                f"{binding['metric_or_aspect'].split(':')[-1]}:{end}"
            )
            prior = authority.connection.execute(
                "SELECT version_id,version_number,record_json "
                "FROM model_forecast_line_versions "
                "WHERE line_ref=? ORDER BY version_number DESC LIMIT 1", (line_ref,),
            ).fetchone()
            if prior is not None:
                held = json.loads(prior["record_json"])
                if (held.get("formula_ref") == DRIVER_FORMULA_REF
                        and Decimal(str(held.get("value"))) == Decimal(str(cell["value"]))):
                    published.append({
                        "line_ref": line_ref, "version_ref": prior["version_id"],
                        "period_end": end, "value": held["value"],
                        "metric_or_aspect": binding["metric_or_aspect"],
                        "status": "unchanged",
                    })
                    continue
            number = 1 if prior is None else int(prior["version_number"]) + 1
            line = authority.publish_line(
                line_ref,
                subject_ref=company_ref,
                metric_or_aspect=binding["metric_or_aspect"],
                period=dict(cell["period"]),
                unit=scale,
                currency=binding["currency"],
                value=str(cell["value"]),
                value_kind="derived_deterministic",
                # The scenario of a driver-model line is the model version it
                # fell out of; nothing else would let a reader replay it.
                scenario_version_ref=version_ref,
                scenario_version_hash=str(record["content_hash"]),
                formula_ref=DRIVER_FORMULA_REF,
                formula_hash=DRIVER_FORMULA_HASH,
                actor_ref=actor_ref,
                version_id=f"forecast-line-version:{line_ref.split('forecast-line:')[-1]}:{number}",
                prior_version_ref=None if prior is None else prior["version_id"],
                idempotency_key=f"driver-model:{version_ref}:{line_ref}",
            )
            published.append({
                "line_ref": line_ref, "version_ref": line["id"],
                "period_end": end, "value": line["value"],
                "metric_or_aspect": line["metric_or_aspect"],
                "status": line["status"],
            })
    return published


def run_company_forecast(
    missions: Any,
    spec: Mapping[str, Any],
    *,
    models: ForecastModelAuthority,
    lines: ModelForecastAuthority | None = None,
    mission_version_ref: str | None = None,
    actor_ref: str = AUTOMATION_ACTOR,
) -> dict[str, Any]:
    """Do the one thing this company needs, or say there is nothing to do.

    Returns what a summary needs and nothing a summary would have to compute:
    the action taken, the version, the readiness counts, the lines written.
    """

    company_ref = str(spec.get("company_ref"))
    table = build_model_inputs(missions, spec)
    prior = models.latest(company_ref)
    action = pending_action(prior, spec, table)
    backfill_proof = (
        action is None and prior is not None
        and models.filing_proof(prior["id"]) is None
    )
    if action is None and not backfill_proof:
        return {"company_ref": company_ref, "status": "nothing_to_do",
                "action": None, "lines": [], "lines_refused": None,
                "record": prior}
    if backfill_proof:
        body = prior
        action = "filing_proof_backfill"
    elif action in {"first", "new_specification"}:
        body = build_forecast_model(
            spec, table, actor_ref=actor_ref,
            mission_version_ref=mission_version_ref,
            change_reason=("assumption_review" if action == "new_specification"
                           else "evidence_thicker"))
        if action == "new_specification":
            # This is a fresh model identity, while the authority still owns
            # one append-only company history. Bind the append to the head we
            # inspected so a concurrent revision cannot be overwritten.
            body[SOURCE_VERSION_KEY] = str(prior["id"])
    else:
        body = actualize_model(prior, table, actor_ref=actor_ref)
    # A comparative quarter can occur in several filings.  Segment arithmetic
    # must use one coherent filing's consolidated row and breakdown members;
    # flattening every filing would add repeated members while the normalizer
    # retained only one consolidated value.  Prefer the newest filing for each
    # concept and economic period, matching the series/restatement rule.
    chosen: dict[tuple[str, Any, Any], tuple[str, list[Mapping[str, Any]]]] = {}
    for filing in missions.statement_filings(company_ref):
        by_period: dict[tuple[str, Any, Any], list[Mapping[str, Any]]] = {}
        for line in missions.statement_lines(filing["ingest_id"]):
            key = (str(line.get("concept")), line.get("period_start"),
                   line.get("period_end"))
            by_period.setdefault(key, []).append(line)
        filed = str(filing.get("filed") or "")
        for key, rows in by_period.items():
            if key not in chosen or filed > chosen[key][0]:
                chosen[key] = (filed, rows)
    statement_rows = [line for _filed, rows in chosen.values() for line in rows]
    stored = models.publish(body, statement_rows=statement_rows)
    if backfill_proof:
        return {"company_ref": company_ref, "status": "proof_backfilled",
                "action": action, "lines": [], "lines_refused": None,
                "record": stored}
    published: list[dict[str, Any]] = []
    refused: str | None = None
    if lines is not None:
        try:
            published = publish_forecast_lines(lines, stored, actor_ref=actor_ref)
        except ForecastPublishRefused as exc:
            refused = str(exc)
    return {
        "company_ref": company_ref,
        "status": stored["status"],
        "action": action,
        "model_version_ref": stored["id"],
        "model_version": stored["version"],
        "prior_version_ref": stored["prior_version_ref"],
        "change_reason": stored["change_reason"],
        "evidence_refs": stored["evidence_refs"],
        "spec_ref": stored["spec_ref"],
        "readiness": model_readiness(stored),
        "lines": published,
        "lines_refused": refused,
        "record": stored,
    }


def unavailable_reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "PUBLISHED_METRICS",
    "inputs_hash",
    "model_digest",
    "RESULT_METRICS",
    "UNIT_SCALES",
    "WRITE_SCOPE",
    "ForecastPublishRefused",
    "ForecastModelUnavailable",
    "missing_write_scope",
    "pending_action",
    "publish_forecast_lines",
    "run_company_forecast",
    "unavailable_reason",
]
