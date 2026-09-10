"""P12d child: answer one company's twelve gate questions and submit them.

Out of process, like every lane that calls a model, because the writer abandons
a request after 30 seconds and four drafting calls are allowed 180 each.

One run does seven things and stops:

1. check the grant before spending anything -- drafting and then being refused
   costs the mission real model calls;
2. choose one company that has passed its Initial Screen, has a dossier, and has
   no gate draft waiting for a person -- a second draft under an undecided one
   would be asking the owner the same question twice;
3. stop early when the file and the debate map have not moved since the draft on
   the chain, which is the lane's idempotency key;
4. draft the four question groups, one bounded call each, refusing any reply
   that leaves its contract;
5. check question one against the dossier's own classification, and refuse the
   whole draft if they disagree;
6. verify the whole draft once, with a model of a different family, and run the
   gate rubric's deterministic checks and the Constitution's ``output_rubric``;
7. publish -- where ADR-0008 decides whether this is a version at all -- and,
   because publishing is what opens the human checkpoint, stop.  Nothing here
   decides the gate.

Exit 0 when the run completed, including when it decided nothing needed doing.

``formal_authority_writes`` is always 0.  A gate answer is assembled *from*
Claims and never writes one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cockpit_model import CockpitModel
from .call_budget import resolve_call_budget, resolve_run_budget
from .company_dossier import load_policy, policy_hash
from .company_dossier_cli import number_material, table_exists
from .coverage_mission import CoverageMissionAuthority
from .deep_insight_gate import (
    DELIVERABLE_KIND,
    DOSSIER_SOURCES,
    EXTRA_SOURCES,
    GROUPS,
    GROUP_QUESTIONS,
    HARD_CHECKS,
    QUESTION_REFS,
    QUESTION_SOURCE_MAP_HASH,
    QUESTION_SOURCE_MAP_REF,
    SOURCE_VERSION_KEY,
    STAGE_REF,
    WRITE_SCOPE,
    DeepInsightGateAuthority,
    DeepInsightGateError,
    answer_body,
    classification_agrees,
    evidence_fingerprint,
    fingerprint_of,
    gate_artefact,
    gate_questions,
    new_refs,
    output_rubric_findings,
    questions_hash,
)
from .deep_insight_gate_draft import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_NUMBER_ROWS,
    MAX_OUTPUT_TOKENS,
    MAX_RUN_COST_USD,
    TIMEOUT_SECONDS,
    block_rows,
    debate_rows,
    dossier_gap_notes,
    dossier_rows,
    draft_group,
    independence,
    independence_precheck,
    material_rows,
    rejected_debate_notes,
    router_family_resolver,
    summarise_answers,
    verify,
)
from .store import DaltonStore, canonical_json, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
# The groups whose questions are about numbers.  The other two are about how a
# business works and who runs it; a filed cash-flow line offered there is prompt
# budget spent on distraction.
NUMBER_GROUPS: frozenset[str] = frozenset({"market", "thesis"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def granted_scope(mission: Mapping[str, Any]) -> str | None:
    """The word that lets this run write, or None.

    ``deliverable`` and no fallback.  The gate draft is the Playbook's own exit
    document for a stage, which is what that word already means; inventing a
    thirteenth write scope for it would have asked the owner to grant the same
    thing twice.
    """

    may_write = set((mission.get("autonomy") or {}).get("may_write") or [])
    return WRITE_SCOPE if WRITE_SCOPE in may_write else None


def screened_companies(missions: Any, mission: Mapping[str, Any]) -> list[str]:
    """Companies whose Initial Screen has passed, in mission priority order."""

    passed = {
        record["company_ref"]
        for record in missions.stage_records(mission["id"])
        if record["stage_ref"] == "initial_screen" and record["status"] == "gate_passed"
    }
    return [
        str(member["company_ref"])
        for member in mission.get("universe") or []
        if member.get("company_ref") in passed
    ]


def valuation_rows(store: DaltonStore, company_ref: str) -> list[dict[str, Any]]:
    """The computed multiples and where each sits in its own history.

    Citable because the snapshot is an append-only authority with a frozen
    formula: ``valuation-metric:<version>:<metric>`` names one cell of one
    published snapshot, and a reader can open it and re-run the arithmetic.
    """

    if not table_exists(store.connection, "valuation_snapshot_versions"):
        return []
    from .valuation_snapshot import ValuationSnapshotAuthority

    snapshot = ValuationSnapshotAuthority(store).latest_version(company_ref)
    if snapshot is None:
        return []
    rows: list[dict[str, Any]] = []
    for metric in snapshot.get("metrics") or []:
        if metric.get("status") != "available" or metric.get("value") is None:
            continue
        percentile = metric.get("percentile") or {}
        tail = ""
        if isinstance(percentile, Mapping) and percentile.get("value") is not None:
            tail = f"，处于自身历史的第 {percentile['value']} 百分位"
        rows.append({
            "kind": "valuation_metric",
            "ref": f"valuation-metric:{snapshot['id']}:{metric['metric']}",
            "text": (f"{metric['label']} 为 {metric['value']} {metric['unit']}"
                     f"（{metric['formula']}）{tail}"),
            "period": snapshot.get("as_of"),
            "importance": "derived_deterministic",
        })
    return rows


def deep_insight_company_source_fingerprint(connection: Any, company_ref: str) -> str:
    """Hash the bounded authority inputs the selected company's gate can read."""

    class ReadView:
        def __init__(self, supplied: Any) -> None:
            self.connection = supplied

    view = ReadView(connection)

    def latest(table: str, company_column: str) -> dict[str, Any] | None:
        if not table_exists(connection, table):
            return None
        row = connection.execute(
            f"SELECT record_json FROM {table} WHERE {company_column}=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,),
        ).fetchone()
        return None if row is None else json.loads(row["record_json"])

    dossier = latest("company_dossier_versions", "company_ref")
    debate = latest("debate_map_versions", "subject_ref")
    gate = latest("deep_insight_gate_versions", "company_ref")
    decision = None
    if gate is not None and table_exists(connection, "deep_insight_gate_decisions"):
        row = connection.execute(
            "SELECT record_json FROM deep_insight_gate_decisions "
            "WHERE gate_version_ref=?", (gate["id"],),
        ).fetchone()
        decision = None if row is None else json.loads(row["record_json"])
    return content_hash({
        "schema_version": "0.1", "company_ref": company_ref,
        "dossier": dossier, "debate_map": debate, "prior_gate": gate,
        "prior_decision": decision,
        "numbers": number_material(view, company_ref, limit=MAX_NUMBER_ROWS),
        "valuation": valuation_rows(view, company_ref),
    })


def debate_map_version(store: DaltonStore, company_ref: str) -> dict[str, Any] | None:
    from .debate_map import DebateMapAuthority, table_exists as map_table_exists

    if not map_table_exists(store.connection):
        return None
    return DebateMapAuthority(store).current(company_ref)


def group_material(
    *,
    group: str,
    dossier: Mapping[str, Any],
    map_version: Mapping[str, Any] | None,
    numbers: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows one group is shown, and the notes it is told about.

    The union of its questions' sources, deduplicated by ref: two questions in
    the same call that both rest on ``demand_drivers`` are shown that section
    once, and both may cite it.
    """

    aspects: list[str] = []
    extras: list[str] = []
    for question in GROUP_QUESTIONS[group]:
        aspects.extend(DOSSIER_SOURCES[question])
        extras.extend(EXTRA_SOURCES.get(question, ()))
    statements = dossier_rows(dossier, aspects)
    seen = {row["ref"] for row in statements}
    for block in ("classification", "variant_view"):
        if block not in extras:
            continue
        for row in block_rows(dossier, block):
            if row["ref"] not in seen:
                seen.add(row["ref"])
                statements.append(row)
    debates = debate_rows(map_version) if "debates" in extras else []
    figures: list[dict[str, Any]] = []
    if group in NUMBER_GROUPS:
        for row in numbers:
            if row["ref"] not in seen:
                seen.add(row["ref"])
                figures.append(row)
    notes: list[str] = []
    if "gaps" in extras:
        notes.extend(dossier_gap_notes(dossier))
    if "rejected_debates" in extras:
        notes.extend(
            f"宪法拒绝的候选争议：{item}" for item in rejected_debate_notes(map_version))
    return material_rows(statements, figures, debates), notes


def fresh_evidence(
    answers: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """The rows this draft cites that the current version does not.

    ADR-0008's question, asked before a record is assembled rather than after,
    so that "there is nothing new here" is a status the run reports instead of a
    record it builds and the authority then refuses.
    """

    known: set[str] = set()
    for answer in (prior or {}).get("answers") or []:
        known.update(row["ref"] for row in answer.get("sources") or [])
    rows: dict[str, dict[str, Any]] = {}
    for answer in answers.values():
        for row in answer.get("sources") or []:
            if row["ref"] not in known:
                rows.setdefault(row["ref"], dict(row))
    return list(rows.values())


def unanswered(question_ref: str, question: str, reason: str,
               missing: str, next_step: str) -> dict[str, Any]:
    """The shape a question takes when nobody has answered it on this run."""

    from .deep_insight_gate import GROUP_OF

    return {
        "question_ref": question_ref, "question": question,
        "group": GROUP_OF[question_ref], "status": "unknown", "confidence": None,
        "unknown": {"reason": reason, "missing": missing,
                    "evidence_that_would_answer": next_step},
        "sentences": [], "sources": [], "gaps": [],
    }


def _version_scoped(ref: str, prefix: str) -> tuple[str, str] | None:
    """Split ``<prefix>:<version id>:<member>`` into the version and the member.

    The version ids these refs carry contain colons of their own
    (``company-dossier-version:acn:3``), so the split is from the *right*: the
    member is one token and everything between the prefix and it is the version.
    """

    if not ref.startswith(prefix + ":"):
        return None
    rest = ref[len(prefix) + 1:]
    version, _, member = rest.rpartition(":")
    if not version or not member:
        return None
    return version, member


def unresolved_refs(connection: Any, record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every ref the draft cites, checked against the authority that owns it.

    Six kinds and six doors.  A ref nobody can open is the one defect a
    citation-bearing document must not have, and the gate cites more kinds than
    any document before it.

    Every kind resolves against its *authority*, not against the rows this run
    happened to show.  That distinction is the whole of this function: a draft
    that carries an answer forward cites the dossier version that answer was
    written from, which is by definition not the version this run read, and
    checking against "what was on the table today" would make every redraft
    after a returned gate refuse itself -- after paying for four calls.
    """

    kinds: dict[str, str] = {}
    for answer in record.get("answers") or []:
        for row in answer.get("sources") or []:
            kinds[row["ref"]] = row["kind"]
    retired: set[str] = set()
    if table_exists(connection, "claim_retirement_decisions"):
        retired = {
            str(row[0]) for row in connection.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions "
                "WHERE decision='retired'").fetchall()
        }

    def stored(table: str, column: str, value: str) -> Mapping[str, Any] | None:
        if not table_exists(connection, table):
            return None
        return connection.execute(
            f"SELECT record_json FROM {table} WHERE {column}=?", (value,)
        ).fetchone()

    missing: list[dict[str, str]] = []
    for ref, kind in sorted(kinds.items()):
        if kind == "claim":
            row = connection.execute(
                "SELECT 1 FROM claim_versions WHERE claim_version_id=?", (ref,)
            ).fetchone()
            if row is None:
                missing.append({"ref": ref, "reason": "no such claim version"})
            elif ref in retired:
                missing.append({"ref": ref, "reason": "the Claim was retired"})
        elif kind == "dossier_section":
            parts = _version_scoped(ref, "dossier-section")
            row = None if parts is None else stored(
                "company_dossier_versions", "version_id", parts[0])
            if parts is None or row is None:
                missing.append({"ref": ref, "reason": "no such dossier version"})
            elif parts[1] not in _dossier_members(row["record_json"]):
                missing.append({"ref": ref,
                                "reason": "that dossier version has no such section"})
        elif kind == "debate":
            parts = _version_scoped(ref, "debate")
            row = None if parts is None else stored(
                "debate_map_versions", "version_id", parts[0])
            if parts is None or row is None:
                missing.append({"ref": ref, "reason": "no such debate map version"})
            elif parts[1] not in _debate_members(row["record_json"]):
                missing.append({"ref": ref,
                                "reason": "that debate map version has no such debate"})
        elif kind == "valuation_metric":
            parts = _version_scoped(ref, "valuation-metric")
            row = None if parts is None else stored(
                "valuation_snapshot_versions", "version_id", parts[0])
            if parts is None or row is None:
                missing.append({"ref": ref, "reason": "no such valuation snapshot"})
            elif parts[1] not in _metric_members(row["record_json"]):
                missing.append({"ref": ref,
                                "reason": "that snapshot has no such metric"})
        elif kind == "forecast_cell":
            if not _forecast_cell_exists(connection, ref):
                missing.append({"ref": ref, "reason": "no such forecast cell"})
        elif ref.startswith("statement-line:"):
            row = connection.execute(
                "SELECT 1 FROM coverage_mission_statement_lines WHERE line_id=?",
                (ref.split(":", 1)[1],),
            ).fetchone()
            if row is None:
                missing.append({"ref": ref, "reason": "no such statement line"})
        elif ref.startswith("mission-document-figure:"):
            row = connection.execute(
                "SELECT 1 FROM coverage_mission_document_figures WHERE figure_id=?",
                (ref,),
            ).fetchone()
            if row is None:
                missing.append({"ref": ref, "reason": "no such document figure"})
        else:
            missing.append({"ref": ref, "reason": "unrecognised figure ref shape"})
    return missing


def _dossier_members(record_json: str) -> set[str]:
    record = json.loads(record_json)
    members = {section["aspect"] for section in record.get("sections") or []}
    return members | {"industry_classification", "variant_view"}


def _debate_members(record_json: str) -> set[str]:
    return {str(item["debate_ref"])
            for item in json.loads(record_json).get("debates") or []}


def _metric_members(record_json: str) -> set[str]:
    return {str(item["metric"])
            for item in json.loads(record_json).get("metrics") or []}


def _forecast_cell_exists(connection: Any, ref: str) -> bool:
    """Whether one ``forecast-cell:`` ref still names a cell of a stored model.

    Any version of the chain, not just the head: a cell an earlier answer cited
    was real when it was cited, and a model that has since gained a version has
    not made the old citation false.
    """

    if not table_exists(connection, "forecast_model_versions"):
        return False
    from .model_forecast_driver import cell_ref

    for row in connection.execute(
        "SELECT record_json FROM forecast_model_versions"
    ).fetchall():
        record = json.loads(row["record_json"])
        for line in record.get("results") or []:
            for cell in line.get("cells") or []:
                period = cell.get("period") or {}
                if ref == cell_ref(str(line["ref"]), str(period.get("end")),
                                   str(cell.get("kind"))):
                    return True
    return False


def demote_unresolved(
    record: Mapping[str, Any], broken: set[str], *, drafted: set[str]
) -> tuple[dict[str, Any], list[str]]:
    """Turn carried answers whose evidence has gone into honest unknowns.

    A ref that stopped resolving is a defect in whichever answer cites it.  In
    an answer drafted just now that is a bad draft and the run is refused.  In
    an answer carried forward from an earlier version -- a Claim retired since,
    a dossier version whose section list changed -- refusing would freeze the
    whole chain on one stale answer for ever, so that answer becomes
    ``unknown`` and says what happened.  The old version keeps what it said;
    nothing is edited.
    """

    out = dict(record)
    answers = []
    demoted: list[str] = []
    for answer in record.get("answers") or []:
        refs = {row["ref"] for row in answer.get("sources") or []}
        if answer["question_ref"] in drafted or not (refs & broken):
            answers.append(answer)
            continue
        demoted.append(answer["question_ref"])
        answers.append(unanswered(
            answer["question_ref"], answer["question"], "source_unavailable",
            "上一版这一问引用的材料现在解析不到了",
            "等这一组重新起草；本轮的运行摘要里记了具体是哪几条引用"))
    out["answers"] = answers
    return out, demoted


def rubric_gate(
    connection: Any, record: Mapping[str, Any], *, prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The gate rubric's deterministic layer, run before a draft can be a version.

    One override, and it is about a difference in what the two layers can see.
    Q1's ``new_version_cites_new_refs`` reads Claim refs, because that is what
    every artefact it grades cites.  A gate answer also rests on dossier
    sections, debates, filed lines, forecast cells and valuation metrics, and a
    version whose new evidence is a newly opened debate has learned something --
    ADR-0008 says so, and ``new_refs`` counts all six kinds.  Where the two
    disagree the broader one wins, and the summary records that it did.
    """

    from .deep_insight_gate import DEEP_INSIGHT_GATE_RUBRIC, body_hash
    from .research_quality_score import run_deterministic

    digest = body_hash(record)
    art = gate_artefact(
        {**dict(record), "id": f"deep-insight-gate-draft:{digest[:32]}",
         "content_hash": digest, "gate_ref": record.get("company_ref")},
        prior=prior,
    )
    result = run_deterministic(art, DEEP_INSIGHT_GATE_RUBRIC, core=connection)
    failed = [name for name in result["failed_checks"] if name in HARD_CHECKS]
    overridden = []
    if "new_version_cites_new_refs" in result["failed_checks"] and new_refs(record, prior):
        overridden.append("new_version_cites_new_refs")
    return {
        "failed": failed,
        "summary": {
            "passed": result["passed"],
            "failed_checks": result["failed_checks"],
            "hard_failed": failed,
            "overridden_checks": overridden,
            "checks": {item["check"]: {"status": item["status"], "count": item["count"]}
                       for item in result["checks"]},
        },
    }


def assemble(
    *,
    company_ref: str,
    answers: Mapping[str, Any],
    questions: Sequence[str],
    prior: Mapping[str, Any] | None,
    classification: str,
    dossier: Mapping[str, Any],
    map_version: Mapping[str, Any] | None,
    constitution: Mapping[str, Any],
    policy: Mapping[str, Any],
    mission: Mapping[str, Any],
    actor_ref: str,
    change_reason: str,
    drafted_at: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    group_outcomes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Freshly drafted answers, carried-forward answers, and the bindings.

    A question this run did not draft keeps the answer the chain already holds,
    provided the Playbook still asks it in the same words -- a changed question
    is a different question, and carrying the old answer under it would be the
    quietest possible way to answer the wrong thing.
    """

    from .deep_insight_gate import DEEP_INSIGHT_GATE_RUBRIC, GROUP_OF

    outcomes = dict(group_outcomes or {})
    held = {item["question_ref"]: item for item in (prior or {}).get("answers") or []}
    rows: list[dict[str, Any]] = []
    for index, ref in enumerate(QUESTION_REFS):
        question = questions[index]
        if ref in answers:
            rows.append({**dict(answers[ref]), "question": question})
            continue
        carried = held.get(ref)
        if carried is not None and carried.get("question") == question:
            rows.append(dict(carried))
            continue
        outcome = outcomes.get(GROUP_OF[ref])
        if outcome == "no_material":
            rows.append(unanswered(
                ref, question, "source_unavailable",
                "档案与争议图里没有任何材料落在这一问上",
                "先把这一问依赖的档案分节写出来；分节缺什么，档案自己的 gaps 已经写明"))
        elif outcome == "refused":
            rows.append(unanswered(
                ref, question, "group_refused",
                "这一组的回复越出了合同，整组被拒绝",
                "重跑这一组；拒绝的原因已记在运行摘要的 refused 里"))
        else:
            rows.append(unanswered(
                ref, question, "not_drafted_this_run",
                "这一问本轮没有起草，链上也没有可以沿用的旧答案",
                "下一轮由本 lane 起草；材料够不够由档案与争议图决定"))
    return {
        SOURCE_VERSION_KEY: None if prior is None else prior["id"],
        "company_ref": company_ref,
        "classification": classification,
        "answers": rows,
        "drafted_at": {
            **((prior or {}).get("drafted_at") or {}),
            **{group: drafted_at for group in GROUPS
               if any(GROUP_OF[ref] == group for ref in answers)},
        },
        "bindings": {
            "constitution_version": {"ref": constitution["id"],
                                     "hash": constitution["content_hash"]},
            "playbook_version": {
                "ref": mission["bindings"]["playbook_version"]["ref"],
                "hash": mission["bindings"]["playbook_version"]["hash"]},
            "mission_version_ref": mission["id"],
            "dossier_version_ref": dossier["id"],
            "dossier_version_hash": dossier["content_hash"],
            "debate_map_version_ref": None if map_version is None else map_version["id"],
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy_hash(policy),
            "questions_hash": questions_hash(questions),
            "source_map_ref": QUESTION_SOURCE_MAP_REF,
            "source_map_hash": QUESTION_SOURCE_MAP_HASH,
            "rubric_ref": DEEP_INSIGHT_GATE_RUBRIC.rubric_ref,
            "rubric_hash": DEEP_INSIGHT_GATE_RUBRIC.content_hash,
        },
        "actor_ref": actor_ref,
        "change_reason": change_reason,
        "evidence_refs": [dict(row) for row in evidence_refs],
    }


def deliverable_sections(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The twelve answers as a readable document, for the deliverable authority.

    ``numbers`` carries only Claim-backed rows, because that authority resolves
    every number's source against ``claim_versions``.  The other five ref kinds
    are still checked -- by this lane's own resolver, against the authorities
    that own them -- but they cannot be offered here, so a body carrying a
    figure that came from a filed line rather than a Claim is not publishable as
    a deliverable and the run says so rather than being refused.
    """

    sections = []
    for answer in record.get("answers") or []:
        claim_sources = [row for row in answer.get("sources") or []
                         if row["kind"] == "claim"]
        body = answer_body(answer)
        if answer["status"] == "unknown":
            body = (f"未答。缺的是：{answer['unknown']['missing']}"
                    f"。能定这一问的证据：{answer['unknown']['evidence_that_would_answer']}。")
        sections.append({
            "title": f"{answer['question_ref']} {answer['question']}"[:200],
            "body": body,
            "claim_refs": [row["ref"] for row in claim_sources],
            "numbers": [{"text": row["text"], "claim_version_ref": row["ref"],
                         "period": row.get("period")} for row in claim_sources],
            "gaps": list(answer.get("gaps") or []),
        })
    return sections


def publishable_as_deliverable(sections: Sequence[Mapping[str, Any]]) -> list[str]:
    """Figures the deliverable authority would refuse, checked before calling it."""

    from .mission_deliverable import unsourced_numbers

    stray: list[str] = []
    for section in sections:
        for token in unsourced_numbers(section["body"], section["numbers"]):
            stray.append(f"{section['title'][:24]}: {token}")
    return stray


def run_gate(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None = None,
    policy_path: Path | None = None,
    company_ref: str | None = None,
    change_reason: str = "evidence_thicker",
    dry_run: bool = False,
    verifier_model_config_path: Path | None = None,
    model_factory: Callable[..., Any] | None = None,
    verifier_model_factory: Callable[..., Any] | None = None,
    family_resolver: Callable[[str | None], str | None] | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "gate_status": None,
        "company_ref": company_ref,
        "write_scope": None,
        "checkpoint_kind": STAGE_REF,
        "evidence_fingerprint": None,
        "groups_drafted": [],
        "refused": [],
        "verification": None,
        "classification": None,
        "rubric": None,
        "output_rubric_findings": [],
        "dropped_groups": [],
        "demoted_questions": [],
        "demoted_refs": [],
        "version_ref": None,
        "version_status": None,
        "answered": 0,
        "unknown": 0,
        "new_refs": 0,
        "deliverable_status": None,
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
            summary.update({"status": "idle", "gate_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        scope = granted_scope(mission)
        if scope is None:
            summary.update({
                "status": "held", "gate_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "在授权前不起草，免得白花模型调用"),
            })
            return summary
        summary["write_scope"] = scope
        actor_ref = mission["autonomy"]["automation_principal"]
        if STAGE_REF not in (mission["autonomy"].get("human_checkpoints") or []):
            # Publishing a draft is what opens the checkpoint, so a mission that
            # does not carry the checkpoint would collect drafts nobody is asked
            # to decide.  The Playbook makes this impossible, which is exactly
            # why saying so here costs nothing and catches a hand-edited mission.
            summary.update({
                "status": "held", "gate_status": "no_checkpoint",
                "failure_reason": ("this mission does not list the deep_insight_gate "
                                   "human checkpoint; a draft nobody is asked to "
                                   "decide is not a submission")})
            return summary

        from .company_dossier import CompanyDossierAuthority

        if not table_exists(store.connection, "company_dossier_versions"):
            summary.update({
                "status": "held", "gate_status": "no_dossier_authority",
                "failure_reason": ("this Core holds no company dossier; the gate is "
                                   "answered from the file, not from the Ledger")})
            return summary
        dossiers = CompanyDossierAuthority(store)
        gates = DeepInsightGateAuthority(store)

        from .research_constitution import ResearchConstitutionAuthority

        binding = mission["bindings"]["constitution_version"]
        constitution = ResearchConstitutionAuthority(store).constitution(binding["ref"])
        if constitution["content_hash"] != binding["hash"]:
            summary.update({"status": "failed", "gate_status": "binding_drift",
                            "failure_reason": "the mission's constitution binding drifted"})
            return summary

        from .research_playbook import ResearchPlaybookAuthority

        playbook_binding = mission["bindings"]["playbook_version"]
        playbook = ResearchPlaybookAuthority(store).playbook(playbook_binding["ref"])
        if playbook["content_hash"] != playbook_binding["hash"]:
            summary.update({"status": "failed", "gate_status": "binding_drift",
                            "failure_reason": "the mission's playbook binding drifted"})
            return summary
        questions = gate_questions(playbook)
        question_by_ref = dict(zip(QUESTION_REFS, questions))

        try:
            policy = load_policy(policy_path)
        except (OSError, ValueError) as exc:
            summary.update({
                "status": "held", "gate_status": "no_policy",
                "failure_reason": f"the output-rubric policy could not be read: {exc}"})
            return summary

        candidates = screened_companies(missions, mission)
        if company_ref is not None:
            candidates = [company_ref] if company_ref in candidates else []
        chosen = None
        prior = None
        dossier = None
        map_version = None
        fingerprint = None
        blocked: dict[str, str] = {}
        for candidate in candidates:
            file_version = dossiers.latest(candidate)
            if file_version is None:
                blocked[candidate] = "no_dossier"
                continue
            head = gates.latest(candidate)
            if head is not None and gates.decision_for(head["id"]) is None:
                blocked[candidate] = "awaiting_decision"
                continue
            if head is not None:
                decision = gates.decision_for(head["id"])
                if decision["decision"] != "return_for_more_work":
                    # Approved or rejected.  Reopening a decided gate is
                    # ``gate_reopen``, a human checkpoint of its own (Wave 3);
                    # this lane does not reopen anything.
                    blocked[candidate] = f"decided:{decision['decision']}"
                    continue
            current_map = debate_map_version(store, candidate)
            digest = evidence_fingerprint(
                file_version, current_map, questions_hash(questions))
            if head is not None and fingerprint_of(head) == digest:
                blocked[candidate] = "nothing_new"
                continue
            chosen, prior, dossier, map_version, fingerprint = (
                candidate, head, file_version, current_map, digest)
            break
        summary["blocked"] = dict(sorted(blocked.items()))
        if chosen is None:
            summary.update({
                "status": "idle",
                "gate_status": ("no_eligible_company" if candidates
                                else "no_screened_company"),
                "failure_reason": ("no company has passed its screen with a dossier "
                                   "and no gate draft already waiting for a person"),
            })
            return summary
        summary["company_ref"] = chosen
        summary["evidence_fingerprint"] = fingerprint
        numbers = number_material(store, chosen, limit=MAX_NUMBER_ROWS)
        numbers += valuation_rows(store, chosen)
        plan = {
            group: group_material(group=group, dossier=dossier,
                                  map_version=map_version, numbers=numbers)
            for group in GROUPS
        }
        summary["material"] = {
            group: {"rows": len(rows), "notes": len(notes)}
            for group, (rows, notes) in plan.items()
        }
        if dry_run:
            summary.update({"status": "succeeded", "gate_status": "dry_run"})
            return summary
        if model_config_path is None:
            summary.update({"status": "succeeded", "gate_status": "gated",
                            "failure_reason": "no model configured"})
            return summary

        config = json.loads(
            Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        run_budget = resolve_run_budget(config, "deep_insight_gate", defaults={
            "max_cost_usd": MAX_RUN_COST_USD, "max_units": len(GROUPS)})
        run_cost_micros = int(float(run_budget["max_cost_usd"]) * 1_000_000)
        max_groups = int(run_budget.get("max_units", len(GROUPS)))

        def build(settings: Mapping[str, Any]) -> Any:
            return CockpitModel(
                settings,
                scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
                max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)

        factory = model_factory or (lambda: build(config))
        verifier_factory = verifier_model_factory
        if verifier_factory is None and verifier_model_config_path is not None:
            verifier_config = json.loads(
                Path(verifier_model_config_path).expanduser().read_text(encoding="utf-8"))
            verifier_factory = lambda: build(verifier_config)  # noqa: E731
        if verifier_factory is None:
            summary.update({
                "status": "held", "gate_status": "no_verifier",
                "failure_reason": ("no separate verifier model configuration is "
                                   "wired; a gate draft is submitted only on a "
                                   "verdict from a different model family (D2)")})
            return summary
        model = factory()
        producer_reserve = int(float(getattr(model, "budget_for", lambda p: {
            "max_cost_usd": MAX_COST_USD})("deep_insight_gate")["max_cost_usd"]) * 1_000_000)
        verifier_budget = resolve_call_budget(
            verifier_config if verifier_model_config_path is not None else {},
            "deep_insight_gate_verifier", defaults={"max_input_tokens": MAX_INPUT_TOKENS,
                "max_output_tokens": MAX_OUTPUT_TOKENS, "max_cost_usd": MAX_COST_USD,
                "timeout_seconds": TIMEOUT_SECONDS})
        verifier_reserve = int(float(verifier_budget["max_cost_usd"]) * 1_000_000)
        company = {"company_ref": chosen, "ticker": next(
            (member.get("ticker") for member in mission["universe"]
             if member.get("company_ref") == chosen), None)}
        prior_bodies = {
            item["question_ref"]: answer_body(item)
            for item in (prior or {}).get("answers") or []
            if item["status"] == "answered"
        }

        answers: dict[str, Any] = {}
        classification = None
        draft_routes: list[str | None] = []
        group_outcomes: dict[str, str] = {}
        spent = 0
        run_bound_blocked = False
        for group in GROUPS[:max_groups]:
            rows, notes = plan[group]
            if not rows:
                group_outcomes[group] = "no_material"
                summary["refused"].append(
                    {"group": group, "reason": "no material was shown for this group"})
                continue
            if spent + producer_reserve + verifier_reserve > run_cost_micros:
                run_bound_blocked = True
                group_outcomes[group] = "refused"
                summary["refused"].append(
                    {"group": group, "reason": "run cost bound reached"})
                continue
            outcome = draft_group(
                model, group=group,
                questions={ref: question_by_ref[ref] for ref in GROUP_QUESTIONS[group]},
                material=rows, company=company, mission=mission,
                prior_answers=prior_bodies, notes=notes,
            )
            spent += int((outcome.get("model") or {}).get("cost_micros") or 0)
            if outcome["status"] != "drafted":
                group_outcomes[group] = "refused"
                summary["refused"].append(
                    {"group": group, "reason": outcome["reason"]})
                continue
            group_outcomes[group] = "drafted"
            answers.update(outcome["answers"])
            if outcome.get("classification"):
                classification = outcome["classification"]
            draft_routes.append((outcome.get("model") or {}).get("route_decision_ref"))
        summary["cost_micros"] = spent
        summary["groups_drafted"] = sorted(
            {answers[ref]["group"] for ref in answers})
        if not answers:
            summary.update({"status": "succeeded", "gate_status": (
                "unverified" if run_bound_blocked else "nothing_drafted")})
            return summary

        # Question one, checked rather than trusted.  Before the verifier, so a
        # draft that contradicts the file it was made from does not also pay for
        # a second call to be told the sentences are well cited.
        filed = str((dossier.get("industry_classification") or {})
                    .get("classification") or "")
        if classification is None:
            classification = str((prior or {}).get("classification") or filed)
        summary["classification"] = classification
        agrees, why = classification_agrees({"classification": classification}, dossier)
        if not agrees:
            summary.update({
                "status": "succeeded", "gate_status": "classification_conflict",
                "failure_reason": why})
            return summary

        resolve = family_resolver or router_family_resolver(config)
        early = independence_precheck(draft_routes=draft_routes, resolve=resolve)
        if early is not None:
            summary["verification"] = {
                "status": "skipped", "verdict": None, "findings": [],
                "reason": "not attempted", "independent": False,
                "independence_reason": early["reason"], "verifier_family": None,
            }
            summary.update({"status": "succeeded", "gate_status": "not_independent"})
            return summary
        if spent + verifier_reserve > run_cost_micros:
            summary["verification"] = {
                "status": "skipped", "verdict": None, "findings": [],
                "reason": "the run's cost bound leaves no room for the verifier",
                "independent": False, "independence_reason": None,
                "verifier_family": None,
            }
            summary.update({"status": "succeeded", "gate_status": "unverified"})
            return summary
        verifier = verifier_factory()
        verdict = verify(
            verifier, answers, company=company, mission=mission,
            producer_route_decision_refs=draft_routes,
        )
        spent += int((verdict.get("model") or {}).get("cost_micros") or 0)
        summary["cost_micros"] = spent
        check = independence(
            draft_routes=draft_routes,
            verifier_route=(verdict.get("model") or {}).get("route_decision_ref"),
            resolve=resolve,
        )
        summary["verification"] = {
            "status": verdict.get("status"), "verdict": verdict.get("verdict"),
            "findings": verdict.get("findings") or [],
            "reason": verdict.get("reason"),
            "independent": check["independent"],
            "independence_reason": check["reason"],
            "verifier_family": check["verifier_family"],
        }
        if verdict.get("status") != "verified" or verdict.get("verdict") != "pass":
            summary.update({"status": "succeeded", "gate_status": "verification_failed"})
            return summary
        if not check["independent"]:
            summary.update({"status": "succeeded", "gate_status": "not_independent"})
            return summary

        # ADR-0008, asked before a record exists.  A draft that cites nothing the
        # current version does not is not a version, and saying so here costs
        # nothing; assembling one and having the authority refuse it would leave
        # the summary describing a record nobody can read.
        fresh = fresh_evidence(answers, prior)
        if not fresh:
            summary.update({
                "status": "succeeded", "gate_status": "no_new_evidence",
                "failure_reason": ("this draft cites nothing the current version "
                                   "does not")})
            return summary
        stamped = _now()
        record = assemble(
            company_ref=chosen, answers=answers, questions=questions, prior=prior,
            classification=classification, dossier=dossier, map_version=map_version,
            constitution=constitution, policy=policy, mission=mission,
            actor_ref=actor_ref, change_reason=change_reason, drafted_at=stamped,
            evidence_refs=fresh, group_outcomes=group_outcomes,
        )
        missing = unresolved_refs(store.connection, record)
        if missing:
            # A ref that stopped resolving is a defect in whichever answer cites
            # it. In an answer drafted just now that is a bad draft and the run
            # is refused whole. In an answer carried forward it is the world
            # having moved, so that answer becomes an honest unknown and the
            # lane redrafts its group next time -- refusing here would freeze
            # the chain on one stale answer for ever, and would do it *after*
            # paying for four calls.
            broken = {item["ref"] for item in missing}
            drafted_refs = {row["ref"] for answer in answers.values()
                            for row in answer.get("sources") or []}
            if broken & drafted_refs:
                summary.update({
                    "status": "succeeded", "gate_status": "unresolvable_refs",
                    "failure_reason": json.dumps(
                        [item for item in missing if item["ref"] in drafted_refs][:5],
                        ensure_ascii=False)})
                return summary
            record, demoted = demote_unresolved(
                record, broken, drafted=set(answers))
            summary["demoted_questions"] = demoted
            summary["demoted_refs"] = [dict(item) for item in missing][:5]
            still = unresolved_refs(store.connection, record)
            if still:
                summary.update({
                    "status": "succeeded", "gate_status": "unresolvable_refs",
                    "failure_reason": json.dumps(still[:5], ensure_ascii=False)})
                return summary
        gate_result = rubric_gate(store.connection, record, prior=prior)
        summary["rubric"] = gate_result["summary"]
        if gate_result["failed"]:
            summary.update({"status": "succeeded", "gate_status": "rubric_refused",
                            "failure_reason": "hard checks failed: "
                                              + ", ".join(gate_result["failed"])})
            return summary
        findings = output_rubric_findings(record, constitution=constitution,
                                          policy=policy, prior=prior)
        summary["output_rubric_findings"] = findings
        if findings:
            summary.update({"status": "succeeded", "gate_status": "constitution_refused",
                            "failure_reason": "the Constitution's output_rubric was "
                                              "not satisfied"})
            return summary
        summary["new_refs"] = len(new_refs(record, prior))
        published = gates.publish(record)
        summary.update({
            "status": "succeeded",
            "gate_status": ("submitted" if published["status"] == "fresh"
                            else "duplicate"),
            "version_ref": published["id"],
            "version_status": published["status"],
            "duplicate_reason": published.get("duplicate_reason"),
            **summarise_answers(answers),
        })
        if published["status"] == "fresh":
            summary["deliverable_status"] = _publish_deliverable(
                store, published, mission=mission, playbook=playbook,
                actor_ref=actor_ref)
        return summary
    except DeepInsightGateError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        summary["status"] = "failed"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def _publish_deliverable(
    store: DaltonStore,
    record: Mapping[str, Any],
    *,
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """Render the submitted draft as a ``deep_insight_gate`` deliverable.

    The gate authority is the record of truth -- it is what the decision binds
    and what the chain replays.  This is the reader's copy, published through
    the machinery every other document in this system already appears in, so
    the twelve answers show up wherever deliverables show up rather than only
    where somebody remembered to add a view.

    Never fatal.  A gate whose prose carries a figure that came from a filed
    line rather than a Claim cannot satisfy that authority's number rule, and
    that is a limitation of the rendering rather than a defect in the draft.
    """

    from .mission_deliverable import MissionDeliverableAuthority, MissionDeliverableError

    sections = deliverable_sections(record)
    stray = publishable_as_deliverable(sections)
    if stray:
        return {"status": "skipped", "reason": "figures with no Claim behind them "
                                               "cannot be rendered as a deliverable",
                "figures": stray[:5]}
    try:
        published = MissionDeliverableAuthority(store).publish(
            kind=DELIVERABLE_KIND,
            subject_ref=str(record["company_ref"]),
            mission=mission,
            playbook=playbook,
            template_ref="template:deep-insight-gate:p12d:v1",
            sections=sections,
            summary=(f"深度认知门十二问草稿 v{record['version']}，"
                     f"分类 {record['classification']}，等待人裁决"),
            gaps=[gap for section in sections for gap in section["gaps"]][:40],
            actor_ref=actor_ref,
            idempotency_key=f"deep-insight-gate:{record['id']}",
        )
    except MissionDeliverableError as exc:
        return {"status": "refused", "reason": f"{type(exc).__name__}: {exc}"}
    return {"status": published["status"], "ref": published["id"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path,
                        help="the configuration the independent verifier runs on. "
                             "Without it the run is held: one configuration routes "
                             "both calls the same way (D2).")
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--gate-policy", type=Path,
                        help="the output_rubric bindings; defaults to "
                             "deploy/phase9/p12a-dossier-policy-v1.json, whose "
                             "criteria are the Constitution's rather than either "
                             "document's")
    parser.add_argument("--company-ref", help="draft this company rather than choosing")
    parser.add_argument("--change-reason", default="evidence_thicker")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and stop; no model call and no write")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_gate(
        state_dir=args.state_dir, model_config_path=args.model_config,
        verifier_model_config_path=args.verifier_model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        scheduler_db=args.scheduler_db, policy_path=args.gate_policy,
        company_ref=args.company_ref, change_reason=args.change_reason,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "NUMBER_GROUPS",
    "assemble",
    "build_parser",
    "debate_map_version",
    "deliverable_sections",
    "demote_unresolved",
    "fresh_evidence",
    "granted_scope",
    "group_material",
    "main",
    "publishable_as_deliverable",
    "rubric_gate",
    "run_gate",
    "screened_companies",
    "unanswered",
    "unresolved_refs",
    "valuation_rows",
]
