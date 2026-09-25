"""Withdraw a wrong Claim retirement, by hand or by re-review -- dry run first.

    # read-only: what is retired, what has been reinstated
    .venv/bin/python -m dalton_core.claim_reinstatement_cli status \\
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core"

    # read-only: what the automatic re-review would withdraw today
    .venv/bin/python -m dalton_core.claim_reinstatement_cli rereview --state-dir "..."

    # read-only: which automatic reinstatements today's stricter rule (v4)
    # would withdraw, so their retirement stands again
    .venv/bin/python -m dalton_core.claim_reinstatement_cli recheck --state-dir "..."

    # one reinstatement withdrawn by a person: dry run, then --apply
    .venv/bin/python -m dalton_core.claim_reinstatement_cli withdraw --state-dir "..." \\
        --claim-version-ref claim-version:… --reason "说的是 META，不是 GOOGL" \\
        --apply --actor human:lumos

    # one retirement, by a person: dry run, then --apply
    .venv/bin/python -m dalton_core.claim_reinstatement_cli reinstate --state-dir "..." \\
        --claim-version-ref claim-version:… --reason "原文说的就是 AWS"
    .venv/bin/python -m dalton_core.claim_reinstatement_cli reinstate --state-dir "..." \\
        --claim-version-ref claim-version:… --reason "…" --apply --actor human:lumos

A retirement is never edited or deleted (``claim_retirement_decisions`` is
append-only and one row per Claim).  Withdrawing one appends a
``claim_retirement_reinstatements`` row that names the exact decision and its
hash, and every read path treats a Claim as retired only while no such row
exists (``claim_retirement.retired_claim_version_refs``).

``reinstate --apply`` goes through the live writer's ephemeral human principal
(operation ``reinstate_claim_retirement``), because the Core is writer state.
``status`` and ``rereview`` open the Core read-only and take no writer; the
writer runs the same re-review itself on every claim-review tick, and writes
only under the mission's ``claim_challenge`` grant.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

OPERATION = "reinstate_claim_retirement"
WITHDRAW_OPERATION = "withdraw_claim_reinstatement"


def _connect(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{state / 'core.sqlite'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True))


class _MissionReader:
    """``CoverageMissionAuthority.mission`` without opening the authority."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def mission(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise LookupError(version_id)
        return json.loads(row["record_json"])


def _plans(state: Path) -> list[dict[str, Any]]:
    """The discovery and feed plans the writer builds the patrol's needles from."""

    plans: list[dict[str, Any]] = []
    for path in sorted((state / "discovery-plans").glob("*.json")):
        try:
            plans.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    from .mission_feed_lane import load_feed_discovery_plan

    for path in sorted((state / "feed-plans").glob("*.json")):
        try:
            plans.append(load_feed_discovery_plan(path))
        except Exception:  # noqa: BLE001 - an unreadable plan adds nothing
            continue
    return plans


def _extra_aliases(values: Iterable[str], connection: sqlite3.Connection) -> dict[str, list[str]]:
    """``TICKER=Name`` pairs as company_ref -> needles (dry-run what-if only)."""

    refs: dict[str, str] = {}
    for row in connection.execute("SELECT mission_version_id FROM coverage_mission_pointer"):
        for member in _MissionReader(connection).mission(row[0]).get("universe") or ():
            ticker = str(member.get("ticker") or "").strip().upper()
            if ticker and member.get("company_ref"):
                refs[ticker] = str(member["company_ref"])
    extra: dict[str, list[str]] = {}
    for value in values:
        ticker, _, name = value.partition("=")
        ref = refs.get(ticker.strip().upper())
        if not name.strip():
            raise SystemExit(f"--extra-alias {value!r}: empty name")
        if not ref:
            continue  # a ticker this workspace does not cover
        # The whole name only: ``name_needles`` would also add each word, and
        # a what-if for "Red Hat" must not quietly add "red" and "hat".
        extra.setdefault(ref, []).append(name.strip().lower())
    return extra


def _driver(state: Path, connection: sqlite3.Connection,
            extra_aliases: Iterable[str] = ()) -> Any:
    from .claim_review import ClaimReviewDriver, needles_from_plans, review_spool
    from .claim_subject import mission_subject_needles

    plans = _plans(state)
    needles = {ref: set(values) for ref, values in needles_from_plans(plans).items()}
    for ref, values in mission_subject_needles([], plans=plans).items():
        needles.setdefault(ref, set()).update(values)
    for ref, values in _extra_aliases(extra_aliases, connection).items():
        needles.setdefault(ref, set()).update(values)
    return ClaimReviewDriver.read_only(
        connection=connection, missions=_MissionReader(connection),
        spool=review_spool(state),
        needles={ref: sorted(values) for ref, values in needles.items()},
    )


def recheck(state: Path, *, max_documents: int) -> dict[str, Any]:
    connection = _connect(state)
    try:
        summary = _driver(state, connection).recheck_reinstatements(
            principal=None, max_documents=max_documents, dry_run=True)
    finally:
        connection.close()
    summary["would_withdraw_count"] = len(summary["would_withdraw"])
    return summary


def withdraw(state: Path, *, claim_version_ref: str, reason: str,
             apply: bool, actor: str | None) -> dict[str, Any]:
    from .claim_retirement import withdrawn_reinstatement_claim_version_refs

    connection = _connect(state)
    try:
        row = connection.execute(
            "SELECT r.record_json AS reinstatement, r.content_hash AS reinstatement_hash, "
            "v.claim_json AS claim FROM claim_retirement_reinstatements r "
            "JOIN claim_versions v ON v.claim_version_id=r.claim_version_ref "
            "WHERE r.claim_version_ref=? ORDER BY r.created_at DESC LIMIT 1",
            (claim_version_ref,),
        ).fetchone()
        already = claim_version_ref in withdrawn_reinstatement_claim_version_refs(connection)
    finally:
        connection.close()
    if row is None:
        return {"status": "not_found", "claim_version_ref": claim_version_ref}
    reinstatement = json.loads(row["reinstatement"])
    claim = json.loads(row["claim"])
    preview = {
        "claim_version_ref": claim_version_ref,
        "subject_ref": claim.get("subject_ref"),
        "statement": claim.get("normalized_statement"),
        "reinstatement_ref": reinstatement.get("id"),
        "reinstatement_hash": row["reinstatement_hash"],
        "reinstated_by": reinstatement.get("actor_ref"),
        "reinstated_rule_ref": reinstatement.get("rule_ref"),
        "already_withdrawn": already,
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
                "reinstatement_hash": row["reinstatement_hash"], "rationale": reason,
                "actor_ref": actor},
    )
    return {"status": "applied", "preview": preview, "result": result}


def rereview(state: Path, *, max_documents: int, extra_aliases: Iterable[str] = (),
             show: int = 50) -> dict[str, Any]:
    connection = _connect(state)
    try:
        driver = _driver(state, connection, extra_aliases)
        summary = driver.rereview_retirements(
            principal=None, max_documents=max_documents, dry_run=True)
    finally:
        connection.close()
    summary["would_reinstate_count"] = len(summary["would_reinstate"])
    summary["would_reinstate"] = summary["would_reinstate"][:show]
    return summary


def status(state: Path) -> dict[str, Any]:
    from .claim_retirement import (
        reinstated_claim_version_refs,
        retired_claim_version_refs,
        withdrawn_reinstatement_claim_version_refs,
    )

    connection = _connect(state)
    try:
        decided = connection.execute(
            "SELECT COUNT(*) FROM claim_retirement_decisions WHERE decision='retired'"
        ).fetchone()[0]
        reinstated = reinstated_claim_version_refs(connection)
        return {"retired_decisions": decided, "reinstated": len(reinstated),
                "reinstatements_withdrawn": len(
                    withdrawn_reinstatement_claim_version_refs(connection)),
                "retired_now": len(retired_claim_version_refs(connection))}
    finally:
        connection.close()


def reinstate(state: Path, *, claim_version_ref: str, reason: str,
              apply: bool, actor: str | None) -> dict[str, Any]:
    connection = _connect(state)
    try:
        row = connection.execute(
            "SELECT d.record_json AS decision, d.content_hash AS decision_hash, "
            "c.record_json AS challenge, v.claim_json AS claim "
            "FROM claim_retirement_decisions d "
            "JOIN claim_retirement_challenges c ON c.challenge_id=d.challenge_ref "
            "JOIN claim_versions v ON v.claim_version_id=d.claim_version_ref "
            "WHERE d.claim_version_ref=?", (claim_version_ref,),
        ).fetchone()
        from .claim_retirement import reinstated_claim_version_refs

        already = claim_version_ref in reinstated_claim_version_refs(connection)
    finally:
        connection.close()
    if row is None:
        return {"status": "not_found", "claim_version_ref": claim_version_ref}
    decision = json.loads(row["decision"])
    challenge = json.loads(row["challenge"])
    claim = json.loads(row["claim"])
    preview = {
        "claim_version_ref": claim_version_ref,
        "subject_ref": claim.get("subject_ref"),
        "statement": claim.get("normalized_statement"),
        "decision": decision.get("decision"), "decision_ref": decision.get("id"),
        "decision_hash": row["decision_hash"],
        "retired_because": challenge.get("rationale"),
        "retired_detector": challenge.get("detector_ref"),
        "already_reinstated": already,
    }
    if not apply:
        return {"status": "dry_run", **preview}
    if not actor or not actor.startswith("human:"):
        raise SystemExit("--apply needs --actor human:<name>")
    from .governance_cli import ephemeral_call

    result = ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=actor, operation=OPERATION,
        params={"claim_version_ref": claim_version_ref,
                "decision_hash": row["decision_hash"], "rationale": reason,
                "actor_ref": actor},
    )
    return {"status": "applied", "preview": preview, "result": result}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "rereview", "reinstate", "recheck", "withdraw"):
        command = sub.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "recheck":
            command.add_argument("--max-documents", type=int, default=10 ** 6)
        if name == "withdraw":
            command.add_argument("--claim-version-ref", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--apply", action="store_true")
            command.add_argument("--actor")
        if name == "rereview":
            command.add_argument("--max-documents", type=int, default=10 ** 6)
            command.add_argument("--show", type=int, default=50)
            command.add_argument(
                "--extra-alias", action="append", default=[],
                help="TICKER=Name, what-if only: judge as if this alias were already packaged")
        if name == "reinstate":
            command.add_argument("--claim-version-ref", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--apply", action="store_true")
            command.add_argument("--actor")
    args = parser.parse_args(list(argv) if argv is not None else None)
    state = args.state_dir.expanduser().resolve()
    if args.command == "status":
        _print(status(state))
    elif args.command == "recheck":
        _print(recheck(state, max_documents=args.max_documents))
    elif args.command == "withdraw":
        _print(withdraw(state, claim_version_ref=args.claim_version_ref, reason=args.reason,
                        apply=args.apply, actor=args.actor))
    elif args.command == "rereview":
        _print(rereview(state, max_documents=args.max_documents,
                        extra_aliases=args.extra_alias, show=args.show))
    else:
        _print(reinstate(state, claim_version_ref=args.claim_version_ref,
                         reason=args.reason, apply=args.apply, actor=args.actor))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
