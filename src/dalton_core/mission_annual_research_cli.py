"""Execute one existing mission annual-research admission out of process."""

from __future__ import annotations

import argparse
import faulthandler
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lane_child_launcher import write_owner_only
from .mission_annual_research_runtime import MissionAnnualResearchRuntime
from .store import content_hash


SUMMARY_SCHEMA_VERSION = "0.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--web-fetch-governance", type=Path, required=True)
    parser.add_argument("--spool-dir", type=Path, required=True)
    parser.add_argument("--annual-draft-model-config", type=Path)
    parser.add_argument("--annual-verifier-model-config", type=Path)
    parser.add_argument("--admission-ref", required=True)
    parser.add_argument("--expected-admission-hash", required=True)
    parser.add_argument("--summary-dir", type=Path, required=True)
    parser.add_argument("--stack-dump-seconds", type=int, default=60)
    parser.add_argument("--quiet", action="store_true")
    return parser


def _summary(
    *, admission_ref: str, admission_hash: str, status: str,
    outcomes: list[dict[str, Any]], error: str | None = None,
) -> dict[str, Any]:
    body = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "admission_ref": admission_ref,
        "admission_hash": admission_hash,
        "status": status,
        "outcomes": outcomes,
        "error": error,
    }
    body["content_hash"] = content_hash(body)
    return body


def run_admission(
    *, state_dir: Path, staging: Path, web_fetch_governance: Path,
    spool_dir: Path, admission_ref: str, expected_admission_hash: str,
    draft_model_config: Path | None = None,
    verifier_model_config: Path | None = None,
) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    with MissionAnnualResearchRuntime(
        state_dir=state_dir,
        staging_path=staging,
        web_fetch_governance_path=web_fetch_governance,
        spool_dir=spool_dir,
        draft_config_path=draft_model_config,
        verifier_config_path=verifier_model_config,
    ) as runtime:
        admission = runtime.authority.resolve_for_execution(admission_ref)
        if admission["content_hash"] != expected_admission_hash:
            raise ValueError("mission annual admission hash drifted before execution")
        maximum = runtime.transition_budget(admission)
        for _transition in range(maximum):
            outcome = runtime.executor.run_once(admission_ref)
            outcomes.append(outcome)
            status = outcome.get("status")
            if status in {"complete", "blocked", "failed"}:
                return _summary(
                    admission_ref=admission_ref,
                    admission_hash=expected_admission_hash,
                    status=status,
                    outcomes=outcomes,
                )
            if status in {"retryable", "pending", "waiting"}:
                work_ref = outcome.get("work_order_ref")
                if not isinstance(work_ref, str) or not runtime.wait_until_claimable(
                    work_ref
                ):
                    break
        return _summary(
            admission_ref=admission_ref,
            admission_hash=expected_admission_hash,
            status="incomplete",
            outcomes=outcomes,
            error="admission did not reach a durable terminal state within its bound",
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stack_dump_seconds < 0:
        parser.error("--stack-dump-seconds must be >= 0")
    if args.stack_dump_seconds:
        faulthandler.dump_traceback_later(
            args.stack_dump_seconds, repeat=True, file=sys.stderr
        )
    try:
        summary = run_admission(
            state_dir=args.state_dir,
            staging=args.staging,
            web_fetch_governance=args.web_fetch_governance,
            spool_dir=args.spool_dir,
            admission_ref=args.admission_ref,
            expected_admission_hash=args.expected_admission_hash,
            draft_model_config=args.annual_draft_model_config,
            verifier_model_config=args.annual_verifier_model_config,
        )
    except Exception as exc:
        summary = _summary(
            admission_ref=args.admission_ref,
            admission_hash=args.expected_admission_hash,
            status="failed",
            outcomes=[],
            error=f"{type(exc).__name__}: {exc}",
        )
    write_owner_only(args.summary_dir / "summary.json", summary)
    if not args.quiet:
        from .store import canonical_json

        print(canonical_json(summary))
    return 0 if summary["status"] in {"complete", "blocked"} else 1


if __name__ == "__main__":  # pragma: no cover - subprocess entry
    raise SystemExit(main())


__all__ = ["SUMMARY_SCHEMA_VERSION", "build_parser", "main", "run_admission"]
