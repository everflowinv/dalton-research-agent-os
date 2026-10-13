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
from .company_financial_statement_structure import (
    forecast_structure_binding,
    materialize_financial_statement_structure,
)
from .forecast_reconciliation import METRIC_BINDINGS
from .model_forecast import (
    DRIVER_FORMULA_HASH,
    DRIVER_FORMULA_REF,
    ModelForecastAuthority,
    STRUCTURED_DRIVER_FORMULA_HASH,
    STRUCTURED_DRIVER_FORMULA_REF,
)
from .model_forecast_driver import (
    AUTOMATION_ACTOR,
    COVERAGE_GENERATOR_REF,
    GENERATOR_REF,
    STRUCTURE_GENERATOR_REF,
    REVENUE,
    ForecastModelAuthority,
    ForecastModelUnavailable,
    SOURCE_VERSION_KEY,
    actualize_model,
    build_cash_flow_companion,
    build_forecast_model,
    build_structured_forecast_model,
    model_readiness,
    is_structured_schema,
    realised_ends,
    structure_formula_hash,
    structure_topology_changed,
    cash_flow_formula_hash,
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


def model_digest(
    spec: Mapping[str, Any], table: Mapping[str, Any], *,
    note_evidence_resolver: Any | None = None,
    financial_note_context: Mapping[str, Any] | None = None,
    reporting_boundary: Mapping[str, Any] | None = None,
) -> str:
    """What a model run is *about*: this specification over these filings.

    The lane names its child by this, so a tick that fires while nothing has
    moved is the same run rather than a second one.
    """

    statement_binding = None
    note_context_binding = None
    if isinstance(spec.get("financial_statement_structure"), Mapping):
        held_notes = spec["financial_statement_structure"].get("note_evidence") or []
        if held_notes:
            if financial_note_context is None:
                raise FinancialStatementStructureError(
                    "forecast financial note context is unavailable"
                )
            from .financial_note_context import (
                FinancialNoteContextError,
                forecast_financial_note_context_binding,
            )
            try:
                note_context_binding = forecast_financial_note_context_binding(
                    financial_note_context,
                    company_ref=str(spec.get("company_ref") or ""),
                    evidence_refs=sorted(str(item.get("ref") or "")
                                         for item in held_notes),
                )
            except FinancialNoteContextError as exc:
                raise FinancialStatementStructureError(str(exc)) from exc
        structure, replay = materialize_financial_statement_structure(
            spec, table, note_evidence_resolver=note_evidence_resolver,
        )
        statement_binding = forecast_structure_binding(structure, replay, table)
        cash_companion = (
            build_cash_flow_companion(spec, table, structure)
            if spec.get("schema_version") == "0.4" else None
        )
    else:
        cash_companion = None
    from .forecast_financial_execution import (
        RULE_REF as FINANCIAL_EXECUTION_RULE, DERIVED_HISTORY_UNIT_RULE_REF,
    )
    digest_body = {
        "financial_execution_rule": FINANCIAL_EXECUTION_RULE,
        "spec_ref": str(spec.get("spec_id") or ""),
        "spec_hash": str(spec.get("content_hash") or ""),
        "inputs_hash": inputs_hash(table),
        "generator_ref": (
            GENERATOR_REF if statement_binding is None else
            COVERAGE_GENERATOR_REF
            if str(structure.get("schema_version")) in {"0.6", "0.7"} else
            STRUCTURE_GENERATOR_REF
        ),
        "formula_hash": (
            DRIVER_FORMULA_HASH if statement_binding is None else
            cash_flow_formula_hash(structure, statement_binding, cash_companion)
            if cash_companion is not None else
            structure_formula_hash(structure, statement_binding)
        ),
        "forecast_structure_binding": statement_binding,
        "cash_flow_companion": cash_companion,
    }
    if statement_binding is not None:
        # A corrected builder must not replay the old refused child of the
        # same spec/filings. This changes only the new structured run identity,
        # not any stored Work, model, source fact or validation arithmetic.
        digest_body["derived_history_unit_rule"] = DERIVED_HISTORY_UNIT_RULE_REF
        from .forecast_financial_execution import ASSUMPTION_EXECUTION_REF
        digest_body["assumption_execution_rule"] = ASSUMPTION_EXECUTION_REF
        from .forecast_presentation_partition import RULE_REF as PRESENTATION_PARTITION_RULE
        digest_body["filed_presentation_partition_rule"] = PRESENTATION_PARTITION_RULE
        from .forecast_consumed_source_groups import RULE_REF as CONSUMED_SOURCE_GROUPS_RULE
        digest_body["filed_consumed_source_groups_rule"] = CONSUMED_SOURCE_GROUPS_RULE
        from .forecast_execution_statement_scope import RULE_REF as EXECUTION_STATEMENT_SCOPE_RULE
        digest_body["filed_execution_statement_scope_rule"] = EXECUTION_STATEMENT_SCOPE_RULE
        from .forecast_revenue_timing_partition import declared, CONTRACT_REF as TIMING_CONTRACT
        if declared(spec):
            from .forecast_execution_statement_scope import TIMING_RULE_REF
            digest_body["filed_execution_statement_scope_rule"] = TIMING_RULE_REF
            digest_body["filed_revenue_recognition_timing_rule"] = TIMING_CONTRACT
        from .forecast_source_accuracy import RULE_REF as SOURCE_ACCURACY_RULE
        digest_body["filed_source_accuracy_rule"] = SOURCE_ACCURACY_RULE
        from .forecast_family_failure_feedback import CONTRACT_REF as FAMILY_FEEDBACK_RULE
        digest_body["filed_family_failure_feedback_rule"] = FAMILY_FEEDBACK_RULE
        from .company_financial_statement_structure import (
            CASH_LEAF_DURATION_UNIT_VALIDATION_REF, _TYPED_NOTE_STRUCTURE_VERSIONS,
        )
        if structure.get("schema_version") in _TYPED_NOTE_STRUCTURE_VERSIONS:
            digest_body["typed_filed_leaf_unit_rule"] = CASH_LEAF_DURATION_UNIT_VALIDATION_REF
    if reporting_boundary is not None:
        digest_body["forecast_reporting_boundary"] = dict(reporting_boundary)
    # Legacy unstructured run identities remain byte-for-byte stable. Only a
    # note-backed structure adds this authority to its digest.
    if note_context_binding is not None:
        digest_body["financial_note_context_binding"] = note_context_binding
    return content_hash(digest_body)


def pending_action(
    prior: Mapping[str, Any] | None, spec: Mapping[str, Any],
    table: Mapping[str, Any], *, reporting_boundary: Mapping[str, Any] | None = None,
) -> str | None:
    """First model, rule migration, or rolling after a newly reported actual.

    The prior version remains immutable. New reported facts move the next
    forecast's boundary; news and claims still require an explicit revision.
    """

    if prior is None:
        return "first"
    if str(prior.get("spec_ref")) != str(spec.get("spec_id")):
        # The specification is a judgement about how to model the company. A
        # new one starts a new model identity; it is not an actualisation of
        # the old specification's model.
        return "new_specification"
    if "execution_contract" not in prior:
        return "rules_upgrade"
    from .model_forecast_driver import validate_new_forecast_rules
    try:
        validate_new_forecast_rules(prior)
    except (ValueError, ForecastModelUnavailable):
        return "rules_upgrade"
    from .forecast_financial_execution import ASSUMPTION_EXECUTION_REF
    if (prior.get("execution_contract",{}).get("assumption_execution_ref") != ASSUMPTION_EXECUTION_REF
            and (any(r.get("role") == "income_tax_expense" and r.get("status") == "unavailable"
                     for r in prior.get("results") or [])
                 or any(r.get("status") == "unexecuted" and r.get("ref") in {
                     e["ref"] for e in spec.get("expense_lines") or [] if e.get("behaviour") == "one_off"}
                     for r in prior.get("execution_contract",{}).get("drivers",{}).get("drivers",[])))):
        return "rules_upgrade"
    if realised_ends(prior, table):
        return "actualize"
    if reporting_boundary is not None:
        from .model_forecast_driver import forecast_policy, revenue_anchor
        desired = forecast_policy(revenue_anchor(prior["drivers"]), reporting_boundary)
        if prior.get("forecast_policy") != desired:
            return "rules_upgrade"
    if coverage_upgrade_due(prior, spec, table):
        # The forecast coverage rules (every reported line gets a method,
        # filed subtotals become tied formulas, EPS, cash roll, source-ranked
        # assumptions) are a new modelling rule, not a new view of the
        # company: the lane rebuilds once, on its own, after deploy.
        return "rules_upgrade"
    return "actualize" if realised_ends(prior, table) else None


def coverage_upgrade_due(prior: Mapping[str, Any], spec: Mapping[str, Any],
                         table: Mapping[str, Any] | None = None) -> bool:
    """A structured model built before the coverage rules, for a spec they reach.

    Due only when the current spec really materializes as a coverage (0.6)
    structure against these filings; otherwise a rebuild would reproduce the
    held model and the company would stay "pending" every tick.
    """

    from .company_financial_statement_structure import is_coverage_structure
    from .forecast_coverage import UPGRADEABLE_STRUCTURE_VERSIONS

    definition = spec.get("financial_statement_structure")
    if not (
        is_structured_schema(prior.get("schema_version"))
        and isinstance(definition, Mapping)
        and definition.get("schema_version") in UPGRADEABLE_STRUCTURE_VERSIONS
        and prior.get("generator_ref") != COVERAGE_GENERATOR_REF
    ):
        return False
    if table is None or definition.get("note_evidence"):
        # A note-backed structure needs its note resolver to materialize; the
        # run itself decides (and an actualisation then rebuilds it).
        return table is None
    try:
        structure, _replay = materialize_financial_statement_structure(spec, table)
    except Exception:
        return False
    return is_coverage_structure(structure.get("schema_version"))


def coverage_sources(
    connection: Any, spec: Mapping[str, Any], table: Mapping[str, Any],
    structure: Mapping[str, Any] | None, prior: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Held preliminary actuals, guidance/consensus and prior judgements."""

    from .company_financial_statement_structure import is_coverage_structure

    if structure is None or not is_coverage_structure(structure.get("schema_version")):
        return None
    from .forecast_assumption_sources import load_assumption_sources
    from .forecast_coverage import filed_values

    anchor = str(spec.get("revenue_anchor_concept") or "")
    revenue = next((item for item in table.get("filed_lines") or []
                    if item.get("concept") == anchor), None)
    history = {} if revenue is None else filed_values(revenue)
    unit = next((str(line.get("unit")) for line in structure.get("lines") or []
                 if line.get("role") == "revenue"), "usd")
    sources = load_assumption_sources(
        connection, str(spec.get("company_ref")), revenue_history=history,
        prior=prior, reporting_unit=unit)
    # 2026-10-09 (P0): the lines the spec itself calls one-off.  A recurring
    # line so flagged (META's G&A, carrying a legal accrual) keeps its growth
    # or ratio method on a normalised base instead of compounding the charge.
    flagged = sorted({
        str(item.get("basis_concept")) for item in spec.get("expense_lines") or []
        if isinstance(item, Mapping) and item.get("behaviour") == "one_off"
        and item.get("basis_concept")})
    sources["one_offs"] = {**(sources.get("one_offs") or {}), "concepts": flagged}
    return sources


def forecast_reporting_boundary(missions: Any, connection: Any, company_ref: str, *, table: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One reporting contract shared by selection, ticket digest and generation."""
    from .forecast_assumption_sources import preliminary_quarter
    filings = missions.statement_filings(company_ref)
    actual_dates = [str(f["report_date"]) for f in filings
                    if f.get("form") in {"10-Q", "10-K", "10-Q/A", "10-K/A"} and f.get("report_date")]
    annual_dates = [str(f["report_date"]) for f in filings
                    if f.get("form") in {"10-K", "10-K/A"} and f.get("report_date")]
    result = {"latest_actual_end": max(actual_dates) if actual_dates else None,
              "fiscal_year_end": max(annual_dates) if annual_dates else None}
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE
    revenue = next((line for line in (table or {}).get("filed_lines") or []
                    if CONCEPT_ROLES.get(str(line.get("concept"))) == REVENUE), {})
    unit = next((str(c.get("unit")).casefold() for c in (revenue.get("cells") or {}).values()
                 if c.get("unit")), "usd")
    preliminary = preliminary_quarter(connection, company_ref, reporting_unit=unit)
    result["preliminary"] = preliminary
    return result


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
    line_formula_ref, line_formula_hash = (
        (STRUCTURED_DRIVER_FORMULA_REF, STRUCTURED_DRIVER_FORMULA_HASH)
        if is_structured_schema(record.get("schema_version")) else
        (DRIVER_FORMULA_REF, DRIVER_FORMULA_HASH)
    )
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
                if (held.get("formula_ref") == line_formula_ref
                        and held.get("formula_hash") == line_formula_hash
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
                formula_ref=line_formula_ref,
                formula_hash=line_formula_hash,
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


def _annual_projection(
    missions: Any, models: ForecastModelAuthority,
    record: Mapping[str, Any], table: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Persist the model's annual view from one exact annual filing calendar."""

    if not is_structured_schema(record.get("schema_version")):
        return None
    annual = [item for item in missions.statement_filings(record["company_ref"])
              if item.get("form") == "10-K"]
    if not annual:
        return None
    filing = max(annual, key=lambda item: (
        str(item.get("report_date") or ""), str(item.get("accession") or "")))
    # statement_filings() is a useful read API but its row mapping alone is
    # not the content authority. Rebuild the filing hash over the stored lines
    # before its report date becomes the projection's fiscal calendar.
    from .company_model_annual_projection import (
        build_annual_projection, calendar_binding_from_annual_filing,
        verify_statement_filing,
    )

    verify_statement_filing(models.connection, filing)
    calendar = calendar_binding_from_annual_filing(
        filing, company_ref=str(record["company_ref"]))
    projection = build_annual_projection(
        model=record, inputs=table, calendar_binding=calendar)
    return models.publish_annual_projection(
        projection, inputs=table, calendar_binding=calendar)


def run_company_forecast(
    missions: Any,
    spec: Mapping[str, Any],
    *,
    models: ForecastModelAuthority,
    lines: ModelForecastAuthority | None = None,
    mission_version_ref: str | None = None,
    actor_ref: str = AUTOMATION_ACTOR,
    note_evidence_resolver: Any | None = None,
) -> dict[str, Any]:
    """Do the one thing this company needs, or say there is nothing to do.

    Returns what a summary needs and nothing a summary would have to compute:
    the action taken, the version, the readiness counts, the lines written.
    """

    company_ref = str(spec.get("company_ref"))
    table = build_model_inputs(missions, spec)
    structure = replay = binding = None
    if isinstance(spec.get("financial_statement_structure"), Mapping):
        structure, replay = materialize_financial_statement_structure(
            spec, table, note_evidence_resolver=note_evidence_resolver,
        )
        binding = forecast_structure_binding(structure, replay, table)
    prior = models.latest(company_ref)
    sources = coverage_sources(models.connection, spec, table, structure, prior) or {}
    boundary = forecast_reporting_boundary(missions, models.connection, company_ref, table=table)
    sources.update(boundary)
    import sqlite3
    from .forecast_company_fcf import load_company_fcf_definition
    try:
        sources["fcf_definition"] = load_company_fcf_definition(models.connection, company_ref, table, boundary.get("fiscal_year_end"))
    except (ValueError, OSError, sqlite3.Error):
        sources["fcf_definition"] = None
    action = pending_action(prior, spec, table, reporting_boundary=boundary)
    if (action == "actualize" and structure is not None
            and str(structure.get("schema_version")) in {"0.6", "0.7"}
            and structure_topology_changed(prior, structure)):
        # The completed structure moved (a line reached enough history, a
        # subtotal became provable): a rebuilt model, not an actualisation.
        action = "rules_upgrade"
    if (action in {None, 'actualize'} and structure is not None and prior is not None
            and isinstance(prior.get('cash_flow_companion'), Mapping)):
        from .model_forecast_driver import cash_companion_topology_changed
        if cash_companion_topology_changed(prior, spec, table, structure):
            # The historical source-basis contract changed. Rebuild freely;
            # never relabel an old model or buy another specification.
            action = 'rules_upgrade'
    backfill_proof = (
        action is None and prior is not None
        and models.filing_proof(prior["id"]) is None
    )
    if action is None and not backfill_proof:
        annual_projection = models.annual_projection(prior["id"])
        if annual_projection is None:
            annual_projection = _annual_projection(missions, models, prior, table)
            if annual_projection is not None:
                return {
                    "company_ref": company_ref,
                    "status": "annual_projection_backfilled",
                    "action": "annual_projection_backfill",
                    "model_version_ref": prior["id"],
                    "model_version": prior["version"],
                    "prior_version_ref": prior["prior_version_ref"],
                    "change_reason": prior["change_reason"],
                    "evidence_refs": prior["evidence_refs"],
                    "spec_ref": prior["spec_ref"],
                    "readiness": model_readiness(prior),
                    "lines": [], "lines_refused": None,
                    "record": prior, "annual_projection": annual_projection,
                }
        return {"company_ref": company_ref, "status": "nothing_to_do",
                "action": None, "lines": [], "lines_refused": None,
                "record": prior, "annual_projection": annual_projection}
    if action is not None and not backfill_proof and structure is not None:
        from pathlib import Path
        from .forecast_source_accuracy import prepare_accuracy
        filename = next((str(row[2]) for row in models.connection.execute("PRAGMA database_list") if row[1] == "main"), "")
        if filename:
            sources["source_accuracy"] = prepare_accuracy(models.connection, Path(filename).parent, spec, table)
    if backfill_proof:
        body = prior
        action = "filing_proof_backfill"
    elif action in {"first", "new_specification", "rules_upgrade"}:
        builder = (build_forecast_model if structure is None
                   else build_structured_forecast_model)
        structured = ({} if structure is None else {
            "structure": structure, "replay": replay, "binding": binding,
        })
        structured["assumption_sources"] = sources
        body = builder(
            spec, table, actor_ref=actor_ref,
            mission_version_ref=mission_version_ref,
            change_reason=("assumption_review"
                           if action in {"new_specification", "rules_upgrade"}
                           else "evidence_thicker"), **structured)
        if action in {"new_specification", "rules_upgrade"}:
            # This is a fresh model identity, while the authority still owns
            # one append-only company history. Bind the append to the head we
            # inspected so a concurrent revision cannot be overwritten.
            body[SOURCE_VERSION_KEY] = str(prior["id"])
    else:
        body = actualize_model(
            prior, table, actor_ref=actor_ref, structure=structure,
            replay=replay, binding=binding, source_accuracy=sources.get("source_accuracy"))
        body["forecast_policy"]["fiscal_year_end"] = boundary.get("fiscal_year_end")
    source_groups = None
    if structure is not None:
        from .forecast_consumed_source_groups import select_rows
        filings = missions.statement_filings(company_ref)
        statement_rows, source_groups = select_rows(
            body, filings, {str(f["ingest_id"]): missions.statement_lines(f["ingest_id"])
                            for f in filings})
    else:
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
    stored = models.publish(body, statement_rows=statement_rows,
                            **({} if source_groups is None else {"source_groups": source_groups}))
    annual_projection = _annual_projection(missions, models, stored, table)
    from .research_followup_automation import FollowupAutomationAuthority, binding as redraft_binding
    if redraft_binding(models.connection, stored["spec_ref"]) is not None:
        FollowupAutomationAuthority(models.store).complete_model(stored["id"])
    if backfill_proof:
        return {"company_ref": company_ref, "status": "proof_backfilled",
                "action": action, "lines": [], "lines_refused": None,
                "record": stored, "annual_projection": annual_projection}
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
        "annual_projection": annual_projection,
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
