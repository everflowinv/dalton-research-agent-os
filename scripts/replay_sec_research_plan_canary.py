#!/usr/bin/env python3
"""Replay one persisted SEC ResearchPlan closure without network access."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.research_coordinator import ResearchCoordinatorStore  # noqa: E402
from dalton_core.research_plan import ResearchPlanAuthority  # noqa: E402
from dalton_core.research_plan_closure import (  # noqa: E402
    ResearchPlanClosureCoordinator,
)
from dalton_core.research_plan_coordinator import (  # noqa: E402
    ResearchPlanCoordinator,
)
from dalton_core.research_question_backlog import (  # noqa: E402
    ResearchQuestionBacklog,
)
from dalton_core.research_review import HumanReviewAuthority  # noqa: E402
from dalton_core.scheduler import Scheduler  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402


class ReplayVerificationError(RuntimeError):
    """The persisted canary no longer replays to its exact authority."""


def _load_result(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayVerificationError(f"invalid result.json: {exc}") from exc
    if not isinstance(result, dict) or result.get("status") != "autonomous-closed":
        raise ReplayVerificationError("result.json is not an autonomous closed canary")
    return result


def replay(root: Path) -> dict:
    root = root.expanduser().resolve()
    required = {
        "core": root / "core.sqlite",
        "review": root / "candidate-staging.sqlite",
        "coordinator": root / "research-coordinator.sqlite",
        "result": root / "result.json",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise ReplayVerificationError(
            "canary authority files are missing: " + ", ".join(missing)
        )
    result = _load_result(required["result"])
    try:
        plan_ref = result["plan"]["ref"]
        authorization = result["promotion"]["authorization"]
        claim_ref = result["promotion"]["claim_version_ref"]
        claim_hash = result["promotion"]["claim_version_hash"]
        answer_binding_ref = result["closure"]["answer_binding_ref"]
    except (KeyError, TypeError) as exc:
        raise ReplayVerificationError(
            f"result.json is missing replay authority: {exc}"
        ) from exc

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
        replayed = ResearchPlanClosureCoordinator(
            plan=plan,
            backlog=backlog,
            coordinator=coordinator,
            review=review,
        ).close_policy_authorized(
            plan_version_ref=plan_ref,
            authorization=authorization,
        )
        if replayed.get("status") != "duplicate":
            raise ReplayVerificationError(
                f"closure replay was not duplicate: {replayed.get('status')!r}"
            )
        if replayed.get("answer_binding_ref") != answer_binding_ref:
            raise ReplayVerificationError("closure replay changed answer binding")
        formal = core.get_claim(claim_ref)
        if formal is None or formal["claim"].get("content_hash") != claim_hash:
            raise ReplayVerificationError("formal ClaimVersion ref/hash does not match")
        counts = {
            table: core.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        if counts != {
            "evidence_versions": 1,
            "claim_versions": 1,
            "thesis_versions": 0,
        }:
            raise ReplayVerificationError(
                f"formal Ledger cardinality changed: {counts}"
            )
        human_gates = {
            "plan_approvals": core.connection.execute(
                "SELECT COUNT(*) FROM research_plan_approvals"
            ).fetchone()[0],
            "claim_reviews": review.connection.execute(
                "SELECT COUNT(*) FROM human_review_decisions"
            ).fetchone()[0],
        }
        if human_gates != {"plan_approvals": 0, "claim_reviews": 0}:
            raise ReplayVerificationError(f"unexpected human gate rows: {human_gates}")
        integrity = {
            "core": core.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "review": review.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "coordinator": records.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
        }
        if set(integrity.values()) != {"ok"}:
            raise ReplayVerificationError(f"SQLite integrity failed: {integrity}")
        return {
            "schema_version": "0.1",
            "status": "replayed",
            "network_calls": 0,
            "plan_ref": plan_ref,
            "plan_hash": result["plan"]["hash"],
            "claim_version_ref": claim_ref,
            "claim_version_hash": claim_hash,
            "answer_binding_ref": answer_binding_ref,
            "closure_status": replayed["status"],
            "formal_ledger_counts": counts,
            "human_gate_counts": human_gates,
            "integrity": integrity,
        }
    finally:
        records.close()
        review.close()
        core.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = replay(args.output_dir)
    except ReplayVerificationError as exc:
        print(f"SEC ResearchPlan replay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
