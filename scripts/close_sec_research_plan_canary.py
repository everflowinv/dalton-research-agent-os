#!/usr/bin/env python3
"""Close an already accepted and promoted isolated SEC plan canary.

This command never creates a human decision and never promotes a candidate.
It only revalidates an existing committed review against the completed plan
tree and binds the resulting formal ClaimVersion to the exact Backlog
question.  Re-running the same command returns a deterministic duplicate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.research_coordinator import ResearchCoordinatorStore
from dalton_core.research_plan import ResearchPlanAuthority
from dalton_core.research_plan_closure import ResearchPlanClosureCoordinator
from dalton_core.research_plan_coordinator import ResearchPlanCoordinator
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Close a committed isolated SEC ResearchPlan canary"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--decision-ref", required=True)
    args = parser.parse_args()
    root = args.output_dir.expanduser().resolve()
    required = {
        "core": root / "core.sqlite",
        "review": root / "candidate-staging.sqlite",
        "coordinator": root / "research-coordinator.sqlite",
        "result": root / "result.json",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        parser.error("canary authority files are missing: " + ", ".join(missing))
    try:
        canary = json.loads(required["result"].read_text(encoding="utf-8"))
        plan_ref = canary["plan"]["ref"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        parser.error(f"canary result.json is invalid: {exc}")

    core = DaltonStore(required["core"])
    review = HumanReviewAuthority(required["review"])
    records = ResearchCoordinatorStore(required["coordinator"])
    try:
        backlog = ResearchQuestionBacklog(core)
        plan = ResearchPlanAuthority(core)
        scheduler = Scheduler(connection=core.connection)
        coordinator = ResearchPlanCoordinator(
            plan=plan,
            scheduler=scheduler,
            connector_records=records,
        )
        closure = ResearchPlanClosureCoordinator(
            plan=plan,
            backlog=backlog,
            coordinator=coordinator,
            review=review,
        )
        result = closure.close(
            plan_version_ref=plan_ref,
            review_decision_ref=args.decision_ref,
        )
        result["integrity"] = {
            "core": core.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "review": review.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "coordinator": records.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        records.close()
        review.close()
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
