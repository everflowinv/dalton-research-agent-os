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
    run_company_forecast,
)
from .company_model_inputs import ModelInputError, build_model_inputs
from .coverage_mission import CoverageMissionAuthority
from .model_forecast import ModelForecastAuthority
from .model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelError,
    ForecastModelUnavailable,
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


def needs_model(latest: Any, spec: Any, table: Any) -> str | None:
    """What this company needs: its first model, a new spec model, or actuals.

    A newly authorized specification is itself an explicit modelling
    judgement, so it starts a new model identity. The lane does not re-forecast
    because a document arrived, because a broker changed a rating, or even
    because a filing landed: a filing settles the quarters it covers and says
    nothing this system is entitled to conclude about the quarters ahead. What
    a filing or an event *means* for the forecast is a judgement with a
    decision word on it, and it reaches the model as an explicit revision.
    """

    return pending_action(latest, spec, table)


def pending_companies(
    missions: CoverageMissionAuthority,
    models: ForecastModelAuthority,
    mission: dict[str, Any],
    *, company_ref: str | None = None,
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
    for ref in refs:
        spec = missions.latest_company_model_spec(ref)
        if spec is None:
            continue
        try:
            table = build_model_inputs(missions, spec)
        except ModelInputError as exc:
            if company_ref is not None:
                raise
            blocked.append(f"{ref}: {exc}")
            continue
        if needs_model(models.latest(ref), spec, table) is not None:
            out.append((ref, spec, table))
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
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
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
        summary["digest"] = model_digest(spec, table)
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
            return summary
        except (ForecastModelUnavailable, ForecastPublishRefused) as exc:
            # Refused whole. A model whose top line could not be identified is
            # not a model with one line missing; every other line in it is a
            # share of the line that is not there.
            summary.update({"status": "succeeded",
                            "forecast_status": f"refused:{exc}"})
            return summary
        if outcome["status"] == "nothing_to_do":
            summary.update({"status": "idle", "forecast_status": "nothing_to_model"})
            return summary
        readiness = outcome["readiness"]
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
            "failure_reason": outcome["lines_refused"],
        })
        return summary
    except ForecastModelError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--company-ref", help="model this company rather than the next")
    parser.add_argument("--validator-contract-hash", required=True,
                        help="exact installed economic-invariant contract hash")
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
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["build_parser", "choose_company", "main", "needs_model",
           "pending_companies", "run_model_forecast"]
