"""Q1: score an artefact, record PM feedback, run the golden set.

Three subcommands, one per thing a person actually does with the quality loop:

    research-quality score   --rubric initial_screen --target <deliverable ref>
    research-quality journal add --target <ref> --verdict revise --note "…"
    research-quality golden  run

``score`` always runs the deterministic layer; it runs the judge only when it
is given a model configuration, because the deterministic layer is free and
correct and the judge is neither.  ``golden run`` runs the deterministic layer
over the committed golden sets and prints one row per case, which is how you
find out that a check changed its mind about a document nobody edited.

Not wired to the tick.  Whether re-scoring every new deliverable should be a
lane is a real question with a real cost, and it belongs in the report rather
than in a scheduler.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .analyst_journal import (
    AnalystJournalAuthority,
    AnalystJournalError,
    TARGET_KINDS,
    VERDICTS,
    journal_context,
)
from .research_quality_rubrics import RUBRIC_ALIASES, rubric as get_rubric
from .research_quality_score import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    QualityScoreAuthority,
    ResearchQualityError,
    TIMEOUT_SECONDS,
    artefact_from_deliverable,
    run_deterministic,
    score_artefact,
)
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"


def _default_golden_dir() -> Path:
    """The committed golden sets, when the CLI is run from a checkout."""

    return Path(__file__).resolve().parents[2] / "tests" / "golden"


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1))


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def _resolve_deliverable(core: sqlite3.Connection, target: str) -> dict[str, Any]:
    """A deliverable version by its version id, or the latest of a deliverable."""

    row = core.execute(
        "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?", (target,),
    ).fetchone()
    if row is None:
        row = core.execute(
            "SELECT v.record_json AS record_json FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "WHERE p.deliverable_ref=?", (target,),
        ).fetchone()
    if row is None:
        raise ResearchQualityError(f"no deliverable found for {target!r}")
    return json.loads(row["record_json"])


def _mission(store: DaltonStore) -> dict[str, Any]:
    """The active mission, which is what a judge call is billed against."""

    from .coverage_mission import CoverageMissionAuthority

    pointer = store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if pointer is None:
        raise ResearchQualityError("no active mission to bill a judge call to")
    return CoverageMissionAuthority(store).mission(pointer["mission_version_id"])


def run_score(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        core = store.connection
        record = _resolve_deliverable(core, args.target)
        art = artefact_from_deliverable(record)
        rubric = get_rubric(args.rubric)
        model = None
        mission = None
        if args.model_config:
            from .cockpit_model import CockpitModel

            config = json.loads(Path(args.model_config).expanduser().read_text(encoding="utf-8"))
            mission = _mission(store)
            model = CockpitModel(
                config, scheduler_db=args.scheduler_db or str(state / "scheduler.sqlite"),
                max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
            )
        scored = score_artefact(
            art, args.rubric, core=core, model=model, mission=mission,
            request_id=f"quality:{rubric.rubric_ref}:{art['ref']}:{art['hash'][:16]}",
        )
        summary: dict[str, Any] = {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "target_ref": art["ref"], "target_hash": art["hash"],
            "rubric_ref": rubric.rubric_ref, "rubric_hash": rubric.content_hash,
            "deterministic": {
                "passed": scored["deterministic"]["passed"],
                "checks": [
                    {"check": item["check"], "status": item["status"], "count": item["count"],
                     "detail": item["detail"]}
                    for item in scored["deterministic"]["checks"]
                ],
            },
            "judge": None if scored["judge"] is None else {
                "status": scored["judge"]["status"],
                "reason": scored["judge"].get("reason"),
                "scores": scored["judge"].get("scores"),
                "summary": scored["judge"].get("summary"),
            },
            "recorded": None,
        }
        if not args.dry_run:
            authority = QualityScoreAuthority(store)
            written = authority.record(
                artefact_kind=art["artefact_kind"], target_ref=art["ref"], target_hash=art["hash"],
                rubric=rubric, deterministic=scored["deterministic"],
                judge_layer=scored["judge"], verifier_layer=scored["verifier"],
                subject_ref=art.get("subject_ref"), actor_ref=args.actor_ref,
            )
            summary["recorded"] = {
                "id": written["id"], "score_ref": written["score_ref"],
                "version": written["version"], "status": written["status"],
            }
        return summary
    finally:
        store.close()


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def run_journal_add(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        target_hash = args.target_hash
        subject = args.company_ref
        if target_hash is None:
            record = _resolve_deliverable(store.connection, args.target)
            target_hash = record["content_hash"]
            subject = subject or record.get("subject_ref")
        override = None
        if args.score is not None:
            override = {"rubric_ref": get_rubric(args.rubric).rubric_ref, "overall": args.score}
        entry = AnalystJournalAuthority(store).add(
            target_ref=args.target, target_hash=target_hash, target_kind=args.kind,
            verdict=args.verdict, actor_ref=args.actor_ref, company_ref=subject,
            note=args.note, score_override=override,
            idempotency_key=args.idempotency_key,
        )
        return {"schema_version": SUMMARY_SCHEMA_VERSION, "entry": entry}
    finally:
        store.close()


def run_journal_show(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        journal = AnalystJournalAuthority(store)
        entries = (journal.for_company(args.company_ref) if args.company_ref
                   else journal.for_target(args.target))
        return {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "entries": entries,
            "context": journal_context(entries),
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# golden
# ---------------------------------------------------------------------------


def load_golden(directory: Path, rubric_name: str | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*/*.json")):
        # One directory per rubric, named by the rubric's short name.
        if rubric_name is not None and path.parent.name != rubric_name:
            continue
        cases.append({"path": path, **json.loads(path.read_text(encoding="utf-8"))})
    return cases


def run_golden(args: argparse.Namespace) -> dict[str, Any]:
    directory = Path(args.golden_dir).expanduser().resolve()
    cases = load_golden(directory, args.rubric)
    rows: list[dict[str, Any]] = []
    for case in cases:
        rubric = get_rubric(case["rubric"])
        result = run_deterministic(case["artefact"], rubric)
        actual = {item["check"]: {"status": item["status"], "count": item["count"]}
                  for item in result["checks"]}
        expected = case["expected"]["deterministic"]
        mismatches = [
            {"check": check, "expected": expected[check], "actual": actual.get(check)}
            for check in expected if actual.get(check) != expected[check]
        ]
        rows.append({
            "case_ref": case["case_ref"], "rubric": case["rubric"],
            "checks": actual, "agrees_with_golden": not mismatches, "mismatches": mismatches,
        })
    width = max((len(row["case_ref"]) for row in rows), default=10)
    print(f"{'case':<{width}}  {'rubric':<16}  golden  checks")
    for row in rows:
        marks = " ".join(
            f"{check}={value['status'][:4]}({value['count']})"
            for check, value in sorted(row["checks"].items()) if value["status"] != "skipped"
        )
        print(f"{row['case_ref']:<{width}}  {row['rubric']:<16}  "
              f"{'ok' if row['agrees_with_golden'] else 'DIFF':<6}  {marks}")
    return {"schema_version": SUMMARY_SCHEMA_VERSION, "cases": len(rows),
            "disagreements": [row for row in rows if not row["agrees_with_golden"]]}


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    score = subparsers.add_parser("score", help="score one artefact against a rubric")
    score.add_argument("--state-dir", required=True)
    score.add_argument("--rubric", required=True, choices=sorted(RUBRIC_ALIASES))
    score.add_argument("--target", required=True,
                       help="a deliverable version id, or a deliverable ref for its latest version")
    score.add_argument("--model-config", help="run the judge layer too; without it, deterministic only")
    score.add_argument("--scheduler-db")
    score.add_argument("--actor-ref", default="automation:coverage-mission")
    score.add_argument("--dry-run", action="store_true", help="score but record nothing")
    score.set_defaults(handler=run_score)

    journal = subparsers.add_parser("journal", help="PM feedback on an artefact")
    journal_subs = journal.add_subparsers(dest="journal_command", required=True)
    add = journal_subs.add_parser("add")
    add.add_argument("--state-dir", required=True)
    add.add_argument("--target", required=True)
    add.add_argument("--target-hash", help="defaults to the target deliverable's own content hash")
    add.add_argument("--kind", default="initial_screen", choices=list(TARGET_KINDS))
    add.add_argument("--verdict", required=True, choices=list(VERDICTS))
    add.add_argument("--actor-ref", required=True, help="a human: principal")
    add.add_argument("--company-ref")
    add.add_argument("--note")
    add.add_argument("--score", type=int, choices=[0, 1, 2, 3, 4],
                     help="the PM's own overall score, recorded beside the judge's")
    add.add_argument("--rubric", default="initial_screen", choices=sorted(RUBRIC_ALIASES))
    add.add_argument("--idempotency-key")
    add.set_defaults(handler=run_journal_add)
    show = journal_subs.add_parser("show")
    show.add_argument("--state-dir", required=True)
    show.add_argument("--target")
    show.add_argument("--company-ref")
    show.set_defaults(handler=run_journal_show)

    golden = subparsers.add_parser("golden", help="run the deterministic layer over the golden sets")
    golden_subs = golden.add_subparsers(dest="golden_command", required=True)
    run = golden_subs.add_parser("run")
    run.add_argument("--golden-dir", default=str(_default_golden_dir()))
    run.add_argument("--rubric", choices=sorted(RUBRIC_ALIASES))
    run.set_defaults(handler=run_golden)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "journal" and args.journal_command == "show" and not (args.target or args.company_ref):
        parser.error("journal show needs --target or --company-ref")
    try:
        result = args.handler(args)
    except (ResearchQualityError, AnalystJournalError) as exc:
        print(json.dumps({"status": "failed", "reason": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    if args.command == "golden":
        return 1 if result["disagreements"] else 0
    _emit(result)
    if args.command == "score" and not result["deterministic"]["passed"]:
        # A failing deterministic layer is a finding, not an error: the score
        # was produced and recorded. The exit code is what a caller checks.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
