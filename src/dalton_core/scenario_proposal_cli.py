"""ADR-0012: the scenario-proposal lane's child -- one company, one model call.

Spawned by ``mission_scenario_lane.ScenarioProposalLauncher``; writes
``summary.json`` into its ticket directory whatever happens, so the lane can
settle the run and decide whether to hold the company.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lane_child_launcher import write_owner_only

SUMMARY_SCHEMA_VERSION = "scenario-proposal-run-0.1"
#: Per call: the purpose's own budget from ``call_budget_defaults.json``.
MAX_INPUT_TOKENS = 40_000
MAX_OUTPUT_TOKENS = 6_000
TIMEOUT_SECONDS = 240


def run_proposal(*, state_dir: Path, company_ref: str, summary_dir: Path,
                 model_config_path: Path | None, scheduler_db: Path | None = None,
                 model: Any = None) -> dict[str, Any]:
    from .coverage_mission import CoverageMissionAuthority
    from .store import DaltonStore

    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "company_ref": company_ref, "status": "failed", "proposal_status": None,
        "set_ref": None, "cost_micros": 0, "reason": None,
    }
    store = None
    try:
        store = DaltonStore(str(state_dir / "core.sqlite"))
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1").fetchone()
        if pointer is None:
            summary.update({"status": "idle", "proposal_status": "no_mission"})
            return summary
        mission = CoverageMissionAuthority(store).mission(pointer["mission_version_id"])
        member = next((item for item in mission["universe"]
                       if item["company_ref"] == company_ref), None)
        if member is None:
            summary.update({"status": "idle", "proposal_status": "not_in_universe"})
            return summary
        if model is None:
            if model_config_path is None:
                summary.update({"status": "held", "proposal_status": "unconfigured",
                                "reason": "no model configuration"})
                return summary
            from .cockpit_model import CockpitModel
            from .scenario_proposal import PURPOSE
            from .call_budget import default_call_budget

            config = json.loads(Path(model_config_path).read_text(encoding="utf-8"))
            model = CockpitModel(
                config, scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
                max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                max_cost_usd=default_call_budget(PURPOSE)["max_cost_usd"],
                timeout_seconds=TIMEOUT_SECONDS)
        from .cockpit_model import CockpitModelError
        from .scenario_proposal import propose_for_company
        from .scheduler import SchedulerError

        try:
            result = propose_for_company(store, mission=mission, company=member, model=model)
        except SchedulerError as exc:
            summary.update({"status": "succeeded", "proposal_status": "busy",
                            "reason": f"{type(exc).__name__}: {exc}"[:500]})
            return summary
        except CockpitModelError as exc:
            summary.update({"status": "failed", "proposal_status": "model_unavailable",
                            "reason": f"{type(exc).__name__}: {exc}"[:500]})
            return summary
        summary.update({
            "status": "failed" if result["status"] == "refused" else "succeeded",
            "proposal_status": result["status"], "set_ref": result.get("set_ref"),
            "cost_micros": int(result.get("cost_micros") or 0),
            "prompt_bytes": result.get("prompt_bytes"),
            "scenarios": result.get("scenarios"),
            "dropped_conditions": result.get("dropped_conditions"),
            "reason": result.get("reason"),
        })
        return summary
    except Exception as exc:  # noqa: BLE001 - the summary is the run's account
        summary.update({"status": "failed", "proposal_status": "error",
                        "reason": f"{type(exc).__name__}: {exc}"[:500]})
        return summary
    finally:
        if store is not None:
            store.close()
        write_owner_only(summary_dir / "summary.json", summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Propose one company's structured scenarios")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--model-config")
    parser.add_argument("--scheduler-db")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_proposal(
        state_dir=Path(args.state_dir), company_ref=args.company_ref,
        summary_dir=Path(args.summary_dir),
        model_config_path=None if args.model_config is None else Path(args.model_config),
        scheduler_db=None if args.scheduler_db is None else Path(args.scheduler_db))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
