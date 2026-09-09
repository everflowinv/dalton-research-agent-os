"""P13al: the child that decides how one company should be modelled.

Out of process for the same reason every model call here is: the writer
abandons a request after 30 seconds and this one is allowed 300.

One run does four things and stops:

1. pick a company that has filed statements and no current specification, and
   project its filings down to the structure they disclose;
2. if a specification already exists for exactly that structure, return it and
   pay nothing -- a company that has not filed anything new does not need
   deciding about twice;
3. otherwise ask the model, and verify the answer against the filings it was
   shown. A specification resting on a concept the company never reported is
   refused whole, not repaired;
4. store it.

Exit 0 when the run completed, including when it decided nothing needed doing.
``formal_authority_writes`` is always 0: a specification is a judgement about
how to model a company, never a Claim about the world. The numbers it will
eventually produce are the things that have to be cited.

This deliberately uses the strongest model available rather than the cheapest.
Deciding that Accenture is a headcount-times-rate business and IBM is a mix
story is the kind of judgement where a weaker model produces something
plausible and generic, which is worse than nothing -- it looks like a decision.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cockpit_model import CockpitModel, CockpitModelError
from .company_model_spec import (
    CompanyModelSpecError,
    build_prompt,
    spec_from_response,
)
from .company_model_state import CompanyModelStateError, build_company_model_state
from .coverage_mission import CoverageMissionAuthority
from .scheduler import SchedulerError
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
# The state is a few hundred concept rows; the answer is a page of structured
# judgement. Both bounds are generous against that.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 6_000
# P13am: the reservation, not the price. The router estimates a call at its
# permitted output and at prompt *bytes* rather than tokens, so it reserves
# roughly four times what the call costs -- IBM's ran for $0.24 against an
# estimate of $0.46, and at 43KB of prompt the estimate was $0.73 and the call
# was refused before it was made. With the day cap at $100 the old $0.60 bought
# nothing but that refusal, so this is sized to admit a large company's
# structure rather than to look frugal.
MAX_COST_USD = 2.50
TIMEOUT_SECONDS = 300


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def choose_company(
    missions: CoverageMissionAuthority, mission: dict[str, Any],
    *, company_ref: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """The company to decide about, and the disclosure to decide from.

    Companies with statements but no current specification come first, oldest
    filing first so the queue drains in the order the lane filled it. A company
    named explicitly is used as given -- that is the hand-run path.

    The *state* is returned rather than rebuilt by the caller, and this is not
    a convenience. The ticker is part of the state and therefore part of its
    hash, so a projection built without one hashes differently. The first
    version selected companies with a tickerless projection while the run
    stored its specification against a projection with the ticker, so the
    selector could never see the answer it had just produced.

    What that cost was not money -- the child rebuilt the state with the
    ticker, found the stored specification and replayed it for nothing. It cost
    *progress*: the lane relaunched the same company every tick and the other
    four companies would have waited forever behind it. One projection, one
    hash, and the lane moves on.
    """

    universe = {
        str(item.get("company_ref")): item.get("ticker")
        for item in (mission.get("universe") or [])
        if isinstance(item, dict)
    }
    refs = ([company_ref] if company_ref is not None
            else [filing["company_ref"] for filing in missions.statement_filings()])
    for held in refs:
        if company_ref is None and held not in universe:
            continue
        try:
            state = build_company_model_state(missions, held,
                                              ticker=universe.get(held))
        except CompanyModelStateError:
            if company_ref is not None:
                raise
            continue
        if missions.company_model_spec_for_state(held, state["state_hash"]) is None:
            return held, state
    return None, None


def run_model_spec(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None,
    company_ref: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "model",
        "company_ref": company_ref,
        "state_hash": None,
        "spec_status": None,
        "revenue_drivers": 0,
        "expense_lines": 0,
        "operating_metrics": 0,
        "forecast_statements": [],
        "replayed": False,
        "cost_micros": 0,
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "spec_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        try:
            chosen, state = choose_company(missions, mission, company_ref=company_ref)
        except CompanyModelStateError as exc:
            summary.update({"status": "idle", "spec_status": "no_statements",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        if chosen is None:
            # Either every company has a current specification, or the one
            # asked for already does. Both are "nothing to decide".
            summary.update({"status": "idle", "spec_status": "nothing_to_decide"})
            return summary
        summary["company_ref"] = chosen
        summary["state_hash"] = state["state_hash"]
        summary["concepts"] = len(state["concepts"])
        if dry_run or model_config_path is None:
            summary.update({
                "status": "succeeded", "spec_status": "gated",
                "failure_reason": None if dry_run else "no model configured",
                "prompt_bytes": len(build_prompt(state).encode("utf-8")),
            })
            return summary

        model = CockpitModel(
            json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8")),
            scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
        )
        try:
            call = model.call(
                purpose="model_spec",
                # Keyed by the disclosure, so an unchanged company replays
                # instead of being paid for again.
                request_id=state["state_hash"][:32],
                prompt=build_prompt(state), mission=mission,
            )
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "spec_status": "busy",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "succeeded", "spec_status": "model_unavailable",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        summary["replayed"] = bool(call.get("replayed"))
        summary["cost_micros"] = int(call.get("cost_micros") or 0)
        try:
            spec = spec_from_response(
                state, call["text"],
                decided_by=mission["autonomy"]["automation_principal"],
            )
        except CompanyModelSpecError as exc:
            # Refused whole. A specification with the invented lines stripped
            # out is no longer the model the model meant to describe.
            summary.update({"status": "succeeded", "spec_status": "refused",
                            "failure_reason": f"{type(exc).__name__}: {exc}"})
            return summary
        stored = missions.record_company_model_spec(
            spec, mission_version_ref=mission["id"],
            work_order_ref=call.get("work_order_ref"),
        )
        summary.update({
            "status": "succeeded", "spec_status": stored["status"],
            "spec_ref": stored["spec_id"],
            "assessment": spec["assessment"],
            "revenue_drivers": len(spec["revenue_drivers"]),
            "expense_lines": len(spec["expense_lines"]),
            "operating_metrics": len(spec["operating_metrics"]),
            "forecast_statements": [
                f"{item['statement']}:{item['importance']}"
                for item in spec["forecast_statements"]
            ],
        })
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
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--company-ref", help="decide about this company rather than the next")
    parser.add_argument("--dry-run", action="store_true",
                        help="assemble the state and stop; no model call, no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_model_spec(
        state_dir=args.state_dir, model_config_path=args.model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        scheduler_db=args.scheduler_db, company_ref=args.company_ref,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["MAX_COST_USD", "build_parser", "choose_company", "main", "run_model_spec"]
