"""P13-M2: the child that builds one company's driver model.

Structured like the specification child and deliberately unlike it in one way:
**it makes no model call**. Everything here is arithmetic over the
specification and the filings, so the run costs nothing, cannot be refused by
the router, and produces the same bytes every time. The expensive judgement --
what the assumptions should actually be -- is a later slice, and the seam for
it is ``model_forecast_driver.draft_assumptions``.

One run does one of three bounded things and stops:

* a company with no driver model gets its first one -- drivers from the
  specification, trailing assumptions from the filings, the income chain from
  those -- and its estimates are published as forecast lines;
* a newly authorized specification gets one new model identity over the same
  immutable filing inputs;
* a company whose model estimated a quarter the filings now cover gets those
  estimates **answered**: the actual is written down beside the estimate,
  which keeps its value and is marked as superseded. Nothing about the
  quarters still ahead is touched.

It never re-forecasts on its own. A filing, a Claim, a broker note or a news
item does not entitle this system to a new view of next year; what an event
means for the forecast is a judgement, it carries a decision word, and it
arrives as an explicit ``revise_assumptions`` call from whoever made it.

Exit 0 when the run completed, including when it decided nothing needed doing.
``formal_authority_writes`` is 0: a forecast is not a Claim about the world.
The lines it writes are forecasts, and the thing that will eventually be
compared with a filing is the Claim, not this.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .company_model_forecast import (
    ForecastPublishRefused,
    missing_write_scope,
    model_digest,
    pending_action,
    forecast_reporting_boundary,
    run_company_forecast,
)
from .company_model_inputs import ModelInputError, build_model_inputs
from .coverage_mission import CoverageMissionAuthority
from .model_forecast import ModelForecastAuthority
from .model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelError,
    ForecastDriverExecutionRefused,
    ForecastModelUnavailable,
    is_structured_schema,
)
from .economic_invariants import (
    EconomicInvariantRefused,
    FORECAST_INVARIANT_CONTRACT_HASH,
)
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def needs_model(latest: Any, spec: Any, table: Any, *, reporting_boundary: Any = None) -> str | None:
    """First model, rule migration, or rolling after an admitted actual."""

    return pending_action(latest, spec, table, reporting_boundary=reporting_boundary)


class ForecastSpecificationPending(ForecastModelUnavailable):
    """Exact obsolete specifications that need a complete driver-plan replacement."""
    def __init__(self, specifications: dict[str, Any]):
        self.specifications = dict(specifications)
        super().__init__("模型规格尚待补齐可执行的驱动公式")


def obsolete_driver_specification(spec: dict[str, Any]) -> dict[str, Any] | None:
    from .company_model_spec import TASK_HASH

    task_hash = spec.get("task_hash")
    # Only a known obsolete persisted contract is a migration wait. Unknown
    # custom readers and current-contract errors keep their original validators.
    if not isinstance(task_hash, str) or len(task_hash) != 64 or task_hash == TASK_HASH:
        return None
    drivers = list(spec.get("revenue_drivers") or [])
    if (len(drivers) == 1 and drivers[0].get("kind") == "mix"
            and drivers[0].get("basis_concept") == spec.get("revenue_anchor_concept")
            and not spec.get("driver_formulas")):
        return None
    used = {ref for plan in spec.get("driver_formulas") or []
            for ref in plan.get("driver_refs") or []}
    missing = sorted({d["ref"] for d in drivers} - used)
    if not missing:
        return None
    return {"spec_ref": spec["spec_id"], "spec_hash": spec.get("content_hash"),
            "task_hash": task_hash, "required_task_hash": TASK_HASH,
            "missing_driver_refs": missing}


def pending_companies(
    missions: CoverageMissionAuthority,
    models: ForecastModelAuthority,
    mission: dict[str, Any],
    *, company_ref: str | None = None,
    waiting_specifications: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Every company with something to do, in order, with what to do it from.

    All of them rather than the first, because the lane has to be able to skip
    one. IBM's specification binds every revenue driver to nothing filed --
    adoption, price mix, rate mix genuinely are not in GAAP -- so its model
    cannot be built at all, and a chooser that only ever returned the first
    candidate would hand IBM back every tick while the other four waited behind
    it forever. That is not a hypothetical: it is precisely the shape of the
    bug the specification lane shipped and had to be caught in a heartbeat.

    The table is returned rather than rebuilt by the caller, for the other half
    of the same lesson: two projections built by two callers hash differently
    the moment their arguments differ, and a selector that disagrees with the
    run about the digest names a different ticket every time.
    """

    universe = {
        str(item.get("company_ref"))
        for item in (mission.get("universe") or []) if isinstance(item, dict)
    }
    if company_ref is not None:
        # Named companies go through the universe too. The mission is what
        # says which companies this automation may work on at all, and a hand
        # run that could reach past it would be a way to write a model for a
        # company nobody admitted -- with the mission's own principal on it.
        if company_ref not in universe:
            raise ForecastModelUnavailable(
                f"{company_ref} is not in this mission's universe")
        refs = [company_ref]
    else:
        refs = sorted({str(item["company_ref"])
                       for item in missions.company_model_specs()
                       if str(item["company_ref"]) in universe})
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    blocked: list[str] = []
    waiting = {} if waiting_specifications is None else waiting_specifications
    for ref in refs:
        spec = missions.latest_company_model_spec(ref)
        if spec is None:
            continue
        obsolete = obsolete_driver_specification(spec)
        if obsolete is not None:
            waiting[ref] = obsolete
            continue
        try:
            table = build_model_inputs(missions, spec)
        except ModelInputError as exc:
            if company_ref is not None:
                raise
            blocked.append(f"{ref}: {exc}")
            continue
        latest = models.latest(ref)
        pending = needs_model(latest, spec, table, reporting_boundary=forecast_reporting_boundary(missions, models.connection, ref, table=table))
        if (pending is None and latest is not None
                and is_structured_schema(latest.get("schema_version"))
                and models.annual_projection(latest["id"]) is None
                and any(item.get("form") == "10-K"
                        for item in missions.statement_filings(ref))):
            pending = "annual_projection_backfill"
        if pending is not None:
            out.append((ref, spec, table))
    if not out and waiting:
        raise ForecastSpecificationPending(waiting)
    if not out and blocked:
        raise ForecastModelUnavailable(
            "company model inputs are blocked: " + "; ".join(blocked))
    return out


def choose_company(
    missions: CoverageMissionAuthority,
    models: ForecastModelAuthority,
    mission: dict[str, Any],
    *, company_ref: str | None = None,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """The first company with something to do, or nothing."""

    pending = pending_companies(missions, models, mission, company_ref=company_ref)
    return pending[0] if pending else (None, None, None)


def run_model_forecast(
    *,
    state_dir: Path,
    summary_dir: Path,
    company_ref: str | None = None,
    dry_run: bool = False,
    expected_model_digest: str | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "publish",
        "company_ref": company_ref,
        "forecast_status": None,
        "model_version_ref": None,
        "model_version": None,
        "spec_ref": None,
        "action": None,
        "change_reason": None,
        "realised_quarters": 0,
        "actual_cells": 0,
        "digest": None,
        "drivers": 0,
        "drivers_with_assumptions": 0,
        "assumptions": 0,
        "forecast_quarters": 0,
        "results_computed": [],
        "results_unavailable": [],
        "forecast_lines_written": 0,
        "annual_projection_ref": None,
        "annual_projection_hash": None,
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    from .financial_note_context import FinancialNoteReadContext
    note_context = FinancialNoteReadContext(store=store, state_dir=state_dir)
    try:
        missions = CoverageMissionAuthority(store)
        models = ForecastModelAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "forecast_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        try:
            chosen, spec, table = choose_company(
                missions, models, mission, company_ref=company_ref)
        except ForecastSpecificationPending as exc:
            summary.update(status="succeeded", forecast_status="awaiting_specification",
                           failure_reason=str(exc), waiting_specifications=exc.specifications)
            return summary
        except ForecastModelUnavailable as exc:
            summary.update({"status": "succeeded",
                            "forecast_status": f"refused:{exc}"})
            return summary
        if chosen is None:
            summary.update({"status": "idle",
                            "forecast_status": "nothing_to_model"})
            return summary
        summary["company_ref"] = chosen
        summary["spec_ref"] = spec.get("spec_id")

        definition = spec.get("financial_statement_structure")
        needs_note = (
            isinstance(definition, dict) and bool(definition.get("note_evidence"))
        )
        held_note_context = (
            note_context.projection(chosen) if needs_note else None
        )

        def note_resolver(ref: str) -> dict[str, Any]:
            return note_context.resolver(ref, expected_context=held_note_context)

        summary["digest"] = model_digest(
            spec, table, note_evidence_resolver=note_resolver,
            financial_note_context=held_note_context,
            reporting_boundary=forecast_reporting_boundary(missions, models.connection, chosen, table=table),
        )
        if (expected_model_digest is not None
                and summary["digest"] != expected_model_digest):
            summary.update({
                "status": "succeeded",
                "forecast_status": "refused:parent model digest drifted",
                "failure_reason": (
                    "parent model digest differs from the child authority state"
                ),
            })
            return summary
        refusal = missing_write_scope(mission)
        if refusal is not None and not dry_run:
            summary.update({"status": "succeeded",
                            "forecast_status": f"refused:{refusal}"})
            return summary
        if dry_run:
            summary.update({"status": "succeeded", "forecast_status": "gated",
                            "failure_reason": refusal})
            return summary
        lines = ModelForecastAuthority(store)
        try:
            outcome = run_company_forecast(
                missions, spec, models=models, lines=lines,
                mission_version_ref=mission["id"],
                actor_ref=mission["autonomy"]["automation_principal"],
                note_evidence_resolver=note_resolver,
            )
        except EconomicInvariantRefused as exc:
            # P17b. Not a lane failure: the gate did its job, the refusal is
            # already on the record with every reason, and the tick succeeded
            # in the only sense that matters -- no impossible number was
            # published. The reasons travel in the summary so the cockpit's
            # lane row says the same thing the company card does.
            summary.update({
                "status": "succeeded",
                "forecast_status": "unavailable:economic_invariants",
                "failure_reason": "; ".join(exc.report.reasons),
            })
            detail = getattr(exc, 'family_partition_failures', None)
            if detail is not None:
                summary['family_partition_failures'] = detail
            return summary
        except ForecastDriverExecutionRefused as exc:
            # This report passed deterministic replay but failed publication.
            # Keep the original rejection and request its source gap through
            # the same governed material path, never manufacture an override.
            summary.update(status="succeeded", forecast_status="refused:unexecuted_driver_plan",
                           failure_reason=f"{type(exc).__name__}: {exc}",
                           driver_execution=exc.driver_report,
                           execution_contract_hash=exc.contract_hash)
            from .forecast_material_gaps import open_gap_requests
            summary["material_requests"] = open_gap_requests(
                state_dir, spec, table,
                record={"execution_contract": {"drivers": exc.driver_report}},
                failure=str(exc),
            )
            return summary
        except (ForecastModelUnavailable, ForecastPublishRefused) as exc:
            # Refused whole. A model whose top line could not be identified is
            # not a model with one line missing; every other line in it is a
            # share of the line that is not there.
            summary.update({"status": "succeeded",
                            "forecast_status": f"refused:{exc}"})
            from .forecast_material_gaps import open_gap_requests
            summary["material_requests"]=open_gap_requests(state_dir,spec,table,record=models.latest(chosen),failure=str(exc))
            return summary
        from .forecast_material_gaps import open_gap_requests
        summary["material_requests"]=open_gap_requests(state_dir,spec,table,record=outcome.get("record"))
        if outcome["status"] == "nothing_to_do":
            summary.update({"status": "idle", "forecast_status": "nothing_to_model"})
            return summary
        readiness = outcome["readiness"]
        annual_projection = outcome.get("annual_projection") or {}
        summary.update({
            "status": "succeeded",
            "action": outcome["action"],
            "change_reason": outcome["change_reason"],
            "realised_quarters": readiness["realised_quarters"],
            "actual_cells": readiness["actual_cells"],
            "forecast_status": ("published" if outcome["status"] == "fresh"
                                else outcome["status"]),
            "model_version_ref": outcome["model_version_ref"],
            "model_version": outcome["model_version"],
            "drivers": readiness["drivers"],
            "drivers_with_assumptions": readiness["drivers_with_assumptions"],
            "assumptions": sum(readiness["assumption_kinds"].values()),
            "forecast_quarters": readiness["forecast_quarters"],
            "results_computed": readiness["results_computed"],
            "results_unavailable": readiness["results_unavailable"],
            "forecast_lines_written": len(outcome["lines"]),
            "annual_projection_ref": annual_projection.get("projection_ref"),
            "annual_projection_hash": annual_projection.get("content_hash"),
            "failure_reason": outcome["lines_refused"],
        })
        return summary
    except ForecastSpecificationPending as exc:
        summary.update(status="succeeded", forecast_status="awaiting_specification",
                       failure_reason=str(exc), waiting_specifications=exc.specifications)
        return summary
    except ForecastModelError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            _write_owner_only(summary_dir / "summary.json", summary)
        finally:
            note_context.close()
            store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--company-ref", help="model this company rather than the next")
    parser.add_argument("--validator-contract-hash", required=True,
                        help="exact installed economic-invariant contract hash")
    parser.add_argument("--expected-model-digest",
                        help="parent's exact model/input/note authority digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="choose and stop; no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.validator_contract_hash != FORECAST_INVARIANT_CONTRACT_HASH:
        raise SystemExit(
            "STOP: ticket validator contract does not match the installed contract")
    summary = run_model_forecast(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        company_ref=args.company_ref, dry_run=args.dry_run,
        expected_model_digest=args.expected_model_digest,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["build_parser", "choose_company", "main", "needs_model",
           "pending_companies", "run_model_forecast"]
