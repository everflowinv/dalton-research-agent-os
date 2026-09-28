"""Keep a retired industry-level Claim as industry evidence -- dry run first.

    # read-only: how many reattributions, per industry
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli status \\
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core"

    # read-only: what the automatic backfill would append today, and why the
    # rest were refused (subject-absent and, since 2026-09-28, support
    # retirements whose verdict is supported and about the mission's industry)
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli simulate --state-dir "..."
    # ... as if the current contract's re-review had upheld every verdict in hand
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli simulate --state-dir "..." \
        --assume-rereview-upholds

    # read-only: which automatic reattributions today's rule (v2: a lone
    # "capex" is no longer the industry's) would withdraw
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli recheck --state-dir "..."

    # one reattribution withdrawn by a person: dry run, then --apply
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli withdraw \\
        --state-dir "..." --claim-version-ref claim-version:… --reason "讲的是资本市场"
        [--apply --actor human:lumos]

    # one Claim, by a person: dry run, then --apply
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli reattribute \\
        --state-dir "..." --claim-version-ref claim-version:… --reason "CIO 调查的行业结论"
    .venv/bin/python -m dalton_core.claim_industry_reattribution_cli reattribute \\
        --state-dir "..." --claim-version-ref claim-version:… --reason "…" \\
        --apply --actor human:lumos

The retirement is never touched: company-level read paths keep skipping the
Claim.  ``reattribute --apply`` appends one ``claim_industry_reattributions``
row naming the claim version, its retirement decision (and hashes) and the
covering mission's ``industry_ref``, through the live writer's ephemeral human
principal (operation ``reattribute_claim_to_industry``), because the Core is
writer state.  ``status`` and ``simulate`` open the Core read-only and take no
writer; the writer runs the same backfill itself on every claim-review tick,
bounded, and writes only under the mission's ``claim_challenge`` grant.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from .claim_reinstatement_cli import _MissionReader, _connect, _plans, _print

OPERATION = "reattribute_claim_to_industry"
WITHDRAW_OPERATION = "withdraw_industry_reattribution"


def status(state: Path) -> dict[str, Any]:
    from .claim_industry_reattribution import TABLE, _table_exists, industry_reattributions

    connection = _connect(state)
    try:
        if not _table_exists(connection, TABLE):
            return {"table": "absent", "reattributions": 0, "live": 0}
        rows = connection.execute(
            f"SELECT industry_ref, reason_code, rule_ref, COUNT(*) AS n FROM {TABLE} "
            "GROUP BY industry_ref, reason_code, rule_ref ORDER BY industry_ref"
        ).fetchall()
        live = industry_reattributions(connection)
        return {
            "reattributions": sum(row["n"] for row in rows),
            "live": len(live),
            "by_industry": [dict(row) for row in rows],
        }
    finally:
        connection.close()


def simulate(state: Path, *, max_documents: int, show: int,
             assume_rereview_upholds: bool = False) -> dict[str, Any]:
    from .claim_review import ClaimReviewDriver, needles_from_plans, review_spool
    from .claim_subject import mission_subject_needles

    connection = _connect(state)
    try:
        plans = _plans(state)
        needles = {ref: set(values) for ref, values in needles_from_plans(plans).items()}
        for ref, values in mission_subject_needles([], plans=plans).items():
            needles.setdefault(ref, set()).update(values)
        driver = ClaimReviewDriver.read_only(
            connection=connection, missions=_MissionReader(connection),
            spool=review_spool(state),
            needles={ref: sorted(values) for ref, values in needles.items()},
        )
        summary = driver.reattribute_industry_findings(
            principal=None, max_documents=max_documents, max_writes=10 ** 6, dry_run=True,
            show=None, defer_pending_rereview=not assume_rereview_upholds)
    finally:
        connection.close()
    summary["would_reattribute_count"] = len(summary.get("would_reattribute") or [])
    by_reason: dict[str, int] = {}
    for item in summary.get("would_reattribute") or []:
        reason = str(item.get("retired_reason_code"))
        by_reason[reason] = by_reason.get(reason, 0) + 1
    summary["would_reattribute_by_reason"] = by_reason
    summary["would_reattribute"] = (summary.get("would_reattribute") or [])[:show]
    summary["refused_examples"] = (summary.get("refused_examples") or [])[:show]
    return summary


def _read_only_driver(state: Path, connection: Any) -> Any:
    from .claim_review import ClaimReviewDriver, needles_from_plans, review_spool
    from .claim_subject import mission_subject_needles

    plans = _plans(state)
    needles = {ref: set(values) for ref, values in needles_from_plans(plans).items()}
    for ref, values in mission_subject_needles([], plans=plans).items():
        needles.setdefault(ref, set()).update(values)
    return ClaimReviewDriver.read_only(
        connection=connection, missions=_MissionReader(connection),
        spool=review_spool(state),
        needles={ref: sorted(values) for ref, values in needles.items()},
    )


def recheck(state: Path, *, max_documents: int) -> dict[str, Any]:
    """Read-only: what the patrol's recheck would withdraw today."""

    connection = _connect(state)
    try:
        summary = _read_only_driver(state, connection).recheck_industry_reattributions(
            principal=None, max_documents=max_documents, dry_run=True)
    finally:
        connection.close()
    summary["would_withdraw_count"] = len(summary.get("would_withdraw") or [])
    return summary


def withdraw(state: Path, *, claim_version_ref: str, reason: str,
             apply: bool, actor: str | None) -> dict[str, Any]:
    from .claim_industry_reattribution import TABLE, withdrawn_reattribution_refs

    connection = _connect(state)
    try:
        row = connection.execute(
            f"SELECT r.record_json AS record, r.content_hash AS reattribution_hash, "
            f"v.claim_json AS claim FROM {TABLE} r "
            "JOIN claim_versions v ON v.claim_version_id=r.claim_version_ref "
            "WHERE r.claim_version_ref=?", (claim_version_ref,),
        ).fetchone()
        withdrawn = withdrawn_reattribution_refs(connection)
    finally:
        connection.close()
    if row is None:
        return {"status": "not_found", "claim_version_ref": claim_version_ref}
    record = json.loads(row["record"])
    claim = json.loads(row["claim"])
    preview = {
        "claim_version_ref": claim_version_ref,
        "statement": claim.get("normalized_statement"),
        "industry_ref": record.get("industry_ref"),
        "reattribution_ref": record.get("id"),
        "reattribution_hash": row["reattribution_hash"],
        "reattributed_rule_ref": record.get("rule_ref"),
        "already_withdrawn": record.get("id") in withdrawn,
    }
    if not apply:
        return {"status": "dry_run", **preview}
    if not actor or not actor.startswith("human:"):
        raise SystemExit("--apply needs --actor human:<name>")
    from .governance_cli import ephemeral_call

    result = ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=actor, operation=WITHDRAW_OPERATION,
        params={"claim_version_ref": claim_version_ref,
                "reattribution_hash": row["reattribution_hash"],
                "rationale": reason, "actor_ref": actor},
    )
    return {"status": "applied", "preview": preview, "result": result}


def reattribute(state: Path, *, claim_version_ref: str, reason: str,
                apply: bool, actor: str | None) -> dict[str, Any]:
    from .claim_industry_reattribution import covering_missions, industry_reattributions
    from .claim_retirement import retired_claim_version_refs

    connection = _connect(state)
    try:
        row = connection.execute(
            "SELECT d.content_hash AS decision_hash, d.decision AS decision, "
            "c.record_json AS challenge, v.claim_json AS claim "
            "FROM claim_retirement_decisions d "
            "JOIN claim_retirement_challenges c ON c.challenge_id=d.challenge_ref "
            "JOIN claim_versions v ON v.claim_version_id=d.claim_version_ref "
            "WHERE d.claim_version_ref=?", (claim_version_ref,),
        ).fetchone()
        retired_now = claim_version_ref in retired_claim_version_refs(connection)
        already = industry_reattributions(connection).get(claim_version_ref)
        missions = covering_missions(connection)
    finally:
        connection.close()
    if row is None:
        return {"status": "not_found", "claim_version_ref": claim_version_ref}
    claim = json.loads(row["claim"])
    challenge = json.loads(row["challenge"])
    mission = missions.get(str(claim.get("subject_ref") or "")) or {}
    preview = {
        "claim_version_ref": claim_version_ref,
        "subject_ref": claim.get("subject_ref"),
        "statement": claim.get("normalized_statement"),
        "decision": row["decision"], "decision_hash": row["decision_hash"],
        "retired_now": retired_now,
        "retired_because": challenge.get("rationale"),
        "retired_reason_code": challenge.get("reason_code"),
        "industry_ref": mission.get("industry_ref"),
        "mission_version_ref": mission.get("mission_version_ref"),
        "already_reattributed": already,
    }
    if not apply:
        return {"status": "dry_run", **preview}
    if not actor or not actor.startswith("human:"):
        raise SystemExit("--apply needs --actor human:<name>")
    if not preview["industry_ref"]:
        raise SystemExit("no active mission with an industry covers this Claim's company")
    from .governance_cli import ephemeral_call

    result = ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=actor, operation=OPERATION,
        params={"claim_version_ref": claim_version_ref,
                "decision_hash": row["decision_hash"],
                "industry_ref": preview["industry_ref"],
                "rationale": reason, "actor_ref": actor},
    )
    return {"status": "applied", "preview": preview, "result": result}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "simulate", "recheck", "reattribute", "withdraw"):
        command = sub.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name in ("simulate", "recheck"):
            command.add_argument("--max-documents", type=int, default=10 ** 6)
        if name == "simulate":
            command.add_argument("--show", type=int, default=50)
            command.add_argument("--assume-rereview-upholds", action="store_true")
        if name in ("reattribute", "withdraw"):
            command.add_argument("--claim-version-ref", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--apply", action="store_true")
            command.add_argument("--actor")
    args = parser.parse_args(list(argv) if argv is not None else None)
    state = args.state_dir.expanduser().resolve()
    if args.command == "status":
        _print(status(state))
    elif args.command == "simulate":
        _print(simulate(state, max_documents=args.max_documents, show=args.show,
                        assume_rereview_upholds=args.assume_rereview_upholds))
    elif args.command == "recheck":
        _print(recheck(state, max_documents=args.max_documents))
    elif args.command == "withdraw":
        _print(withdraw(state, claim_version_ref=args.claim_version_ref,
                        reason=args.reason, apply=args.apply, actor=args.actor))
    else:
        _print(reattribute(state, claim_version_ref=args.claim_version_ref,
                           reason=args.reason, apply=args.apply, actor=args.actor))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
