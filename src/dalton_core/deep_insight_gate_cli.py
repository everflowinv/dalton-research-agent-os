"""P12d child: answer one company's twelve gate questions and submit them.

Out of process, like every lane that calls a model, because the writer abandons
a request after 30 seconds and four drafting calls are allowed 180 each.

One run does eight things and stops:

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
7. weigh the whole draft against the submission standard (D1) -- twelve
   answers that are well cited and say almost nothing are a research plan, not
   a finding -- and, when it falls short, leave a note saying question by
   question what is missing instead of publishing anything;
8. publish -- where ADR-0008 decides whether this is a version at all -- and,
   because publishing is what opens the human checkpoint, stop.  Nothing here
   decides the gate.

When the previous version was returned by a person (D2), the reviewer's own
words are carried into the prompt, only the questions they named are re-drafted,
and the new version's ``change_reason`` is ``reviewer_returned``.

A return gets two things an ordinary run does not, and for one reason: a return
has no second door.  An ordinary draft that is refused is redrafted whenever the
evidence next moves and costs nobody anything in the meantime; a returned one
has already spent the owner's attention, and the lane cannot ask them again.

* **A carried-forward first answer that the file has moved past is re-drafted
  rather than carried.**  Question one joins the set the return rewrites, with
  the file's current classification in front of the model, so the refusal in
  step 5 fires on what this run drafted rather than on what the last run said.
* **A figure nothing cites is repaired once before it is a refusal.**  Between
  steps 5 and 6, the group that wrote it is shown the exact digits, its own
  reply and the rows it may cite, and is asked to attach a reference or drop the
  number -- the same bargain ``draft_contract_repair`` strikes for a broken
  reply shape.  One call, same model, same run budget; if a figure is still
  uncited the draft is refused in step 6 exactly as before.

Either way, what a held return leaves behind says which questions failed and
why, because the person who reads that line is the only one who can act on it.

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
from typing import Any, Callable, Collection, Mapping, Sequence

from .cockpit_model import CockpitModel
from .call_budget import resolve_call_budget, resolve_run_budget
from .company_dossier import load_policy, policy_hash
from .company_dossier_cli import number_material, table_exists
from .coverage_mission import CoverageMissionAuthority
from .deep_insight_gate import (
    DELIVERABLE_KIND,
    REVIEWER_RETURNED,
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
    unsourced_figures,
)
from .deep_insight_gate_quality import (
    DeepInsightGateStandardError,
    assess,
    clear_note,
    load_standard,
    note_is_current,
    read_note,
    standard_hash,
    write_note,
)
from .deep_insight_gate_review import (
    change_note,
    changed_questions,
    carried_forward_questions,
    groups_to_redraft,
    questions_to_redraft,
    review_of,
    with_stale_classification,
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

    # Stage progress belongs to the mission, rather than only to the active
    # version that happened to record it.  The authority owns the folded,
    # monotone ladder semantics, including reopened and later failed gates.
    passed = set(missions.companies_at_or_past(
        "deep_insight_gate", mission["mission_ref"]))
    return [
        str(member["company_ref"])
        for member in mission.get("universe") or []
        if member.get("company_ref") in passed
    ]


def valuation_rows(store: Any, company_ref: str) -> list[dict[str, Any]]:
    """The computed multiples and where each sits in its own history.

    Citable because the snapshot is an append-only authority with a frozen
    formula: ``valuation-metric:<version>:<metric>`` names one cell of one
    published snapshot, and a reader can open it and re-run the arithmetic.
    """

    if not table_exists(store.connection, "valuation_snapshot_versions"):
        return []
    from .valuation_snapshot import latest_snapshot

    snapshot = latest_snapshot(store.connection, company_ref)
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


#: The drafting rules this lane runs under, as one word in every company's
#: signature.  A ``content_refused`` hold in the failure ledger is keyed by the
#: signature, and the signature used to name only the *inputs* -- so a draft
#: refused under an old rule stayed refused after the rule was fixed, until the
#: dossier happened to move.  Bump this when the drafting, repair or refusal
#: rules change and every such hold lifts on the next tick.
GATE_DRAFTING_CONTRACT = "deep-insight-gate-drafting:2026-09-17-d2"


def deep_insight_company_source_fingerprint(
    connection: Any, company_ref: str, *, input_paths: tuple[Path | None, ...] = (),
) -> str:
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
    mission = None
    pointer = connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer "
        "ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if pointer is not None:
        row = connection.execute(
            "SELECT record_json FROM coverage_mission_versions "
            "WHERE mission_version_id=?", (pointer["mission_version_id"],)
        ).fetchone()
        mission = None if row is None else json.loads(row["record_json"])
    files = []
    for path in input_paths:
        if path is None:
            files.append(None)
        else:
            target = Path(path).expanduser().resolve()
            files.append({"path": str(target), "hash": content_hash(
                json.loads(target.read_text(encoding="utf-8")))})
    from .cockpit_model import verifier_provider_contract_fingerprint
    return content_hash({
        "schema_version": "0.1", "company_ref": company_ref,
        "mission": mission, "input_files": files,
        "provider_contract": verifier_provider_contract_fingerprint(
            "deep_insight_gate_verifier"),
        "drafting_contract": GATE_DRAFTING_CONTRACT,
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


def retired_claim_refs(connection: Any) -> set[str]:
    """Every Claim version the Ledger has retired; empty on a Core without the table."""

    if not table_exists(connection, "claim_retirement_decisions"):
        return set()
    return {
        str(row[0]) for row in connection.execute(
            "SELECT claim_version_ref FROM claim_retirement_decisions "
            "WHERE decision='retired'").fetchall()
    }


CLASSIFICATION_PIN_NOTE = (
    "公司档案（本评审据以起草的文件）把这家公司归为「{filed}」。第一问的 classification "
    "必须与档案一致，填「{filed}」；档案的分类是独立复核过的，第一问与它不一致的草稿会被整份拒绝。"
    "如果材料明显支持另一类，在第一问的正文里说明分歧，但 classification 仍填档案的那一类。"
)


def classification_pin_note(filed: str) -> str:
    """The sentence the industry group is shown about the file's classification."""

    return CLASSIFICATION_PIN_NOTE.format(filed=filed)


def group_material(
    *,
    group: str,
    dossier: Mapping[str, Any],
    map_version: Mapping[str, Any] | None,
    numbers: Sequence[Mapping[str, Any]],
    retired: Collection[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows one group is shown, and the notes it is told about.

    The union of its questions' sources, deduplicated by ref: two questions in
    the same call that both rest on ``demand_drivers`` are shown that section
    once, and both may cite it.

    ``retired`` is the set of Claim versions the Ledger has since retired.  The
    dossier's sections still name them -- the file was drafted before the
    retirement -- and a row shown to the model is a row it may cite; a gate
    that cited one was refused whole for "the Claim was retired" (IBM, live
    2026-09-17).  Those rows are not shown, so they cannot be cited.
    """

    aspects: list[str] = []
    extras: list[str] = []
    for question in GROUP_QUESTIONS[group]:
        aspects.extend(DOSSIER_SOURCES[question])
        extras.extend(EXTRA_SOURCES.get(question, ()))
    gone = set(retired)
    statements = [row for row in dossier_rows(dossier, aspects)
                  if not (row.get("kind") == "claim" and row.get("ref") in gone)]
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


def attempt_key(
    *,
    evidence_fingerprint: str,
    standard: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
) -> str:
    """What "this exact attempt" is, and therefore what a held note is about.

    Three things, because all three change what this run would conclude:

    * **the evidence** -- the dossier version, the debate map, the twelve
      questions -- which is the lane's own idempotency key;
    * **the submission standard**, an input to the decision rather than only to
      the judgement of it: an owner who relaxes a threshold has changed what
      this run would conclude, and a note written under the old numbers must
      not hold the lane quiet against the new ones;
    * **the reviewer's return**, by identity *and by content*.  A decision's id
      is ``hash(version, verdict)`` -- the same string for every return on one
      draft -- so a return whose words changed would otherwise be keyed as the
      return the lane already attempted, and the note written under the first
      set of per-question notes would hold the lane silent against the second.
      A held return has to lift when any of the three moves; a company that can
      only be unstuck by a filing nobody expects is a company nobody can help.

    Changing the *shape* of this key retires every note written under the old
    one, which is intended: a deploy that changes what a redraft would do is a
    reason to attempt one more redraft, not a reason to stay quiet on the
    strength of a refusal the old code reached.
    """

    return content_hash([
        str(evidence_fingerprint), standard_hash(standard),
        "-" if decision is None else str(decision.get("id") or ""),
        "-" if decision is None else str(decision.get("content_hash") or "")])


def group_reply_wire(
    answers: Mapping[str, Any],
    *,
    group: str,
    material: Sequence[Mapping[str, Any]],
    classification: str | None,
) -> dict[str, Any]:
    """One group's accepted answers, back in the shape the model sent them.

    A repair prompt shows a model its own previous reply.  The raw reply text
    is not kept past the drafting call -- the run holds the parsed answers --
    so it is rebuilt here, and rebuilt in the model's vocabulary rather than
    ours: the sentences carry the row *tags* that were shown, not the refs they
    resolved to, because a model asked to fix a citation can only cite a tag.
    """

    tag_of = {row["ref"]: row["tag"] for row in material}
    rows: list[dict[str, Any]] = []
    for ref in GROUP_QUESTIONS[group]:
        answer = answers[ref]
        if answer.get("status") == "unknown":
            unknown = answer.get("unknown") or {}
            rows.append({
                "question_ref": ref, "status": "unknown",
                "missing": unknown.get("missing", ""),
                "evidence_that_would_answer": unknown.get(
                    "evidence_that_would_answer", ""),
                "gaps": list(answer.get("gaps") or []),
            })
            continue
        rows.append({
            "question_ref": ref, "status": "answered",
            "confidence": answer.get("confidence"),
            "sentences": [
                {"text": row["text"],
                 "refs": [tag_of.get(item, item) for item in row["refs"]]}
                for row in answer.get("sentences") or []
            ],
            "gaps": list(answer.get("gaps") or []),
        })
    out: dict[str, Any] = {"answers": rows}
    if "q1" in GROUP_QUESTIONS[group]:
        out["classification"] = classification
    return out


def numbers_violations(
    answers: Mapping[str, Any], *, group: str
) -> tuple[list[Any], dict[str, list[str]]]:
    """Every figure in this group's prose that none of its cited rows carries.

    Returned as ``draft_contract_repair`` violations because that is the shape
    a repair prompt lists, and as a per-question map because that is what the
    run summary and the owner's note have to say.
    """

    from .draft_contract_repair import Violation

    found: dict[str, list[str]] = {}
    violations: list[Any] = []
    for index, ref in enumerate(GROUP_QUESTIONS[group]):
        answer = answers.get(ref)
        if answer is None or answer.get("status") != "answered":
            continue
        stray = unsourced_figures(answer)
        if not stray:
            continue
        found[ref] = stray
        violations.append(Violation(
            path=f"answers[{index}].sentences[].text",
            rule="numbers_without_refs",
            detail=(f"{ref} 的正文写了 {'、'.join(stray[:6])}，"
                    "但这一问引用的材料里没有任何一行带这个数字。"
                    "要么补上一条确实带这个数字的引用（必须是下面列出的行标签），"
                    "要么把这个数字从句子里删掉；其余内容一字不改。"),
        ))
    return violations, found


# The extra rules a numbers repair is reminded of.  Deliberately short and
# deliberately *only* about figures: the shape contract is restated beside it,
# and a repair shown two long rule sets tends to satisfy the last one.
_NUMBERS_REMINDER_LINES = (
    "Every digit in a sentence must appear, verbatim, in one of the rows that "
    "sentence cites. Do not convert units or scales, do not round, do not "
    "recompute a percentage.",
    "A figure you cannot cite is a figure you must remove. Removing it is "
    "always allowed; inventing a citation for it never is.",
    "Change nothing else: no judgement, no confidence, no question's status, "
    "and no sentence the violations do not name.",
)


def repair_group_numbers(
    model: Any,
    *,
    group: str,
    questions: Mapping[str, str],
    material: Sequence[Mapping[str, Any]],
    company: Mapping[str, Any],
    mission: Mapping[str, Any],
    answers: Mapping[str, Any],
    classification: str | None = None,
    prior_answers: Mapping[str, str] | None = None,
    notes: Sequence[str] = (),
    review: Mapping[str, Any] | None = None,
    budget_remaining_micros: int | None = None,
    repair_reserve_micros: int = 0,
) -> dict[str, Any]:
    """One repair call for a group whose prose carries a figure nothing cites.

    ``numbers_without_refs`` is a hard check and stays one: a published answer
    whose figure traces to nothing is the failure this whole system is built to
    prevent.  But it is also the one failure a model can fix without reading
    anything again -- the digits either came from a row it forgot to cite or
    from nowhere at all -- and on a **returned** draft, refusing the document
    whole spends the owner's return on nothing.  Live on 2026-09-17, CTSH's
    redraft of nine named questions was refused for three such figures and the
    return produced no version at all.

    So the same bargain the dossier lane strikes for a broken reply shape is
    struck here for a broken citation: the violations are enumerated, the model
    is shown its own reply once, and the repaired reply is checked by the same
    deterministic code.  If a figure is still uncited the draft is refused
    exactly as before.  One call, on the same model, out of the same run
    budget; a repair that will not fit is refused unmade with the numbers.

    A repair that moves question one's classification is refused rather than
    accepted: it was asked to fix digits, and a repair that changes a judgement
    is a second draft nobody authorised.
    """

    from .cockpit_model import CockpitModelError
    from .deep_insight_gate_draft import (
        DRAFT_PURPOSE,
        GateDraftRefused,
        build_group_prompt,
        group_contract_reminder,
        group_repair_context,
        parse_group_output,
    )
    from .draft_contract_repair import (
        ContractRepairError,
        build_repair_prompt,
        contract_reminder_lines,
        repair_request_id,
    )

    # Only a group this run drafted whole can be repaired: the repair prompt is
    # the model's own reply handed back to it, and a partial reply is not one.
    if any(ref not in answers for ref in GROUP_QUESTIONS[group]):
        return {"status": "ok", "group": group, "figures": {},
                "answers": dict(answers), "cost_micros": 0,
                "repair_attempts": 0, "reason": None}
    violations, figures = numbers_violations(answers, group=group)
    if not violations:
        return {"status": "ok", "group": group, "figures": {},
                "answers": dict(answers), "cost_micros": 0,
                "repair_attempts": 0, "reason": None}
    result: dict[str, Any] = {
        "status": "refused", "group": group, "figures": figures,
        "answers": dict(answers), "cost_micros": 0, "repair_attempts": 0,
        "reason": None,
    }
    if (budget_remaining_micros is not None
            and budget_remaining_micros < repair_reserve_micros):
        result["status"] = "budget_refused"
        result["reason"] = (
            "run cost bound reached before the numbers repair: "
            f"{max(0, budget_remaining_micros)} micros left, "
            f"{repair_reserve_micros} reserved for one repair call")
        return result
    prompt = build_group_prompt(
        group=group, questions=questions, material=material, company=company,
        prior_answers=prior_answers, notes=notes, review=review)
    request_id = content_hash({
        "group": group, "company": company.get("company_ref"),
        "prompt_sha": content_hash(prompt),
    })[:32]
    try:
        repair_prompt = build_repair_prompt(
            original_prompt=prompt,
            reply_text=group_reply_wire(answers, group=group, material=material,
                                        classification=classification),
            violations=violations,
            contract_reminder=contract_reminder_lines(_NUMBERS_REMINDER_LINES)
            + "\n" + group_contract_reminder(group),
            context=group_repair_context(material))
    except ContractRepairError as exc:
        result["reason"] = str(exc)
        return result
    try:
        call = model.call(
            purpose=DRAFT_PURPOSE, mission=mission, prompt=repair_prompt,
            request_id=repair_request_id(
                request_id, contract_name=f"deep-insight-gate-numbers:{group}",
                violations=violations))
    except CockpitModelError as exc:
        result["status"] = "unavailable"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result
    result["repair_attempts"] = 1
    result["cost_micros"] = int(call.get("cost_micros") or 0)
    result["route_decision_ref"] = call.get("route_decision_ref")
    try:
        parsed = parse_group_output(call.get("text"), group=group,
                                    questions=questions, material=material)
    except GateDraftRefused as exc:
        result["reason"] = f"the numbers repair left the reply contract: {exc}"
        return result
    if "q1" in GROUP_QUESTIONS[group] and parsed.get("classification") != classification:
        result["reason"] = (
            "the numbers repair changed question one's classification from "
            f"{classification!r} to {parsed.get('classification')!r}; a repair "
            "fixes digits, it does not re-decide the question")
        return result
    repaired = {**dict(answers), **parsed["answers"]}
    still, remaining = numbers_violations(repaired, group=group)
    if still:
        result["figures"] = remaining
        result["reason"] = ("figures are still uncited after one repair: "
                            + "; ".join(item.line() for item in still[:3]))[:500]
        return result
    result["status"] = "repaired"
    result["answers"] = repaired
    return result


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
            # Which *question* each hard failure is about.  The counts above are
            # what the summary carried before, and a count is not something an
            # owner can act on: "3 numbers" sends them to read twelve answers
            # looking for digits.  Bounded, because a refusal is a refusal
            # however long the list is.
            "hard_findings": {
                item["check"]: [dict(row) for row in item["findings"][:10]]
                for item in result["checks"] if item["check"] in failed},
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
    expected_source_fingerprint: str | None = None,
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
        "attempt_fingerprint": None,
        "groups_drafted": [],
        "refused": [],
        # What each group's reply broke and whether one repair fixed it.  A
        # group refused whole over a stray key on an unknown answer is the
        # commonest thing this lane does, and it was invisible here.
        "contract_repair": [],
        "verification": None,
        "classification": None,
        "rubric": None,
        "output_rubric_findings": [],
        "dropped_groups": [],
        "demoted_questions": [],
        "demoted_refs": [],
        "version_ref": None,
        "version_status": None,
        # D1/D2: what the pre-submission standard said, and what the person who
        # returned the previous version asked for.  Always present so that a
        # run that ended before either was computed is distinguishable from one
        # where nothing was asked.
        "quality": None,
        "review": None,
        "redrafted_questions": [],
        "carried_forward_questions": [],
        # D2: question one was redrafted because the file moved under the
        # answer that would otherwise have been carried forward.
        "classification_refresh": None,
        # D2: which groups wrote a figure nothing cites, and whether one repair
        # call fixed it.  A hard check refusal that says only "3 numbers" is
        # not something an owner can act on.
        "numbers_repair": [],
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
        if expected_source_fingerprint is not None:
            observed = deep_insight_company_source_fingerprint(
                store.connection, company_ref or "",
                input_paths=(model_config_path, verifier_model_config_path, policy_path),
            )
            if observed != expected_source_fingerprint:
                summary.update({
                    "status": "idle", "gate_status": "input_changed",
                    "failure_reason": "selected Deep Insight inputs changed before drafting",
                })
                return summary
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
        # D1: the numbers a draft has to clear before it is worth a person's
        # time.  Read before anything is drafted, and a broken file stops the
        # lane rather than restoring defaults somebody was trying to change.
        try:
            standard = load_standard(state_dir)
        except DeepInsightGateStandardError as exc:
            summary.update({
                "status": "held", "gate_status": "no_submission_standard",
                "failure_reason": str(exc)})
            return summary

        candidates = screened_companies(missions, mission)
        if company_ref is not None:
            candidates = [company_ref] if company_ref in candidates else []
        chosen = None
        prior = None
        prior_decision = None
        dossier = None
        map_version = None
        fingerprint = None
        attempt_fingerprint = None
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
            decision = None
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
            # D2: a person's return is itself a reason to draft again.
            #
            # It was not, and that was the whole defect.  The lane's
            # idempotency key is the evidence -- the dossier version, the debate
            # map, the twelve questions -- so a returned gate sat at
            # ``nothing_new`` until a filing landed, however specific the
            # reviewer had been about what was wrong.  "Question three confuses
            # bookings with revenue" is not a request for more evidence; it is a
            # request to read the evidence again.  So the attempt is keyed on
            # the evidence *and* the decision, which means exactly one redraft
            # per return and no loop.
            #
            # The standard is in the key too.  It is an input to the decision
            # rather than only to the judgement of it: an owner who relaxes a
            # threshold has changed what this run would conclude, and a note
            # written under the old numbers must not hold the lane quiet
            # against the new ones.
            attempt = attempt_key(evidence_fingerprint=digest,
                                  standard=standard, decision=decision)
            if head is not None and decision is None and fingerprint_of(head) == digest:
                blocked[candidate] = "nothing_new"
                continue
            # D1: the last attempt was drafted and then held back -- as not good
            # enough to submit, or as a rewrite that changed nothing -- and
            # nothing it rests on has moved since.  The coordinator's own quiet
            # signature would catch this too, but it lives in the controller's
            # memory and a restart is the commonest event on this host: without
            # a durable note every restart buys four drafting calls to reach the
            # same refusal.
            if note_is_current(read_note(state_dir, candidate), attempt):
                blocked[candidate] = ("auto_returned_awaiting_evidence"
                                      if decision is None
                                      else "returned_redraft_already_attempted")
                continue
            chosen, prior, prior_decision, dossier, map_version = (
                candidate, head, decision, file_version, current_map)
            fingerprint, attempt_fingerprint = digest, attempt
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
        # What this exact attempt is keyed on: the evidence, the standard and
        # the reviewer's decision.  Separate from the evidence fingerprint
        # above, which is what the draft binds and what ADR-0008 reads.
        summary["attempt_fingerprint"] = attempt_fingerprint
        # D2: the person who returned the previous version, and what they said.
        # Everything below reads this: which groups are re-drafted, what the
        # prompt is told, and the word the new version carries for why it
        # exists.
        review = review_of(prior_decision)
        if review is not None:
            summary["review"] = {
                "returned_version_ref": review["gate_version_ref"],
                "decided_at": review["decided_at"],
                "reason": review["reason"],
                "question_notes": dict(review["question_notes"]),
            }
            change_reason = REVIEWER_RETURNED
        numbers = number_material(store, chosen, limit=MAX_NUMBER_ROWS)
        numbers += valuation_rows(store, chosen)
        plan = {
            group: group_material(group=group, dossier=dossier,
                                  map_version=map_version, numbers=numbers,
                                  retired=retired_claim_refs(store.connection))
            for group in GROUPS
        }
        # The file's own classification, said to the group that answers q1.
        # ``classification_agrees`` refuses a draft whole when q1 differs from
        # the dossier; a model that was never told what the dossier says can
        # only agree by luck (EPAM, live 2026-09-17: the owner returned q1, the
        # redraft chose contract_compounder, the file says turnaround).
        filed_now = str((dossier.get("industry_classification") or {})
                        .get("classification") or "")
        if filed_now:
            rows, notes = plan["industry"]
            plan["industry"] = (rows, [*notes, classification_pin_note(filed_now)])
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
        # D2: a return rewrites what was named and leaves the rest alone.  The
        # four calls exist because four groups read the same tables, so a
        # request to rewrite one question is a request to re-run one group --
        # and a return that named two questions in one group costs one call
        # rather than four.
        wanted = list(GROUPS[:max_groups])
        # What the drafting calls are shown.  The reviewer's own words, plus --
        # and only when the file has moved under a carried answer -- the lane's
        # own note saying so.  ``summary["review"]`` above keeps the person's
        # notes unmixed with ours.
        draft_review = review
        if review is not None:
            assessment = (assess(prior, dossier=dossier, verifier_passed=True,
                                 standard=standard) if prior else None)
            targeted = questions_to_redraft(
                review, assessment=assessment, prior=prior)
            # D2, live 2026-09-17 (ACN).  A return that does not name question
            # one leaves question one to be carried forward, and a carried
            # classification is an answer written against a dossier version the
            # file may have moved several versions past.  When it has, carrying
            # it forward is carrying an answer already known to be wrong -- and
            # ``classification_agrees`` below then refuses the *whole* redraft
            # over it, so the owner's return produces nothing and nothing it
            # could produce would ever change that.  So question one is
            # redrafted too, with the file's current word in front of the
            # model.  The refusal keeps its teeth; it now fires on what this
            # run drafted rather than on what the last run said.
            filed_now = str((dossier.get("industry_classification") or {})
                            .get("classification") or "")
            carried_word = str((prior or {}).get("classification") or "")
            if (targeted and "q1" not in targeted and prior is not None
                    and not classification_agrees(
                        {"classification": carried_word}, dossier)[0]):
                draft_review = with_stale_classification(
                    review, filed=filed_now, carried=carried_word)
                targeted = questions_to_redraft(
                    draft_review, assessment=assessment, prior=prior)
                summary["classification_refresh"] = {
                    "filed": filed_now,
                    "carried": carried_word,
                    "reason": ("上一版第一问沿用的分类与公司档案已经不一致，"
                               "这一轮把第一问一并重写"),
                }
            limited = [group for group in groups_to_redraft(targeted)
                       if group in wanted]
            # A return with no handle -- no question numbers, nothing short of
            # the standard -- is still a return, and the honest reading of it
            # is "the whole document".
            if limited:
                wanted = limited
            summary["redrafted_questions"] = list(targeted)
        for group in wanted:
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
                prior_answers=prior_bodies, notes=notes, review=draft_review,
                # WP-C1: one repair of a broken reply shape, on the same model,
                # out of what is left of *this run's* bound -- and it has to
                # leave the verifying call its own or it is refused unmade.
                budget_remaining_micros=run_cost_micros - spent,
                repair_reserve_micros=producer_reserve + verifier_reserve,
            )
            if outcome.get("contract_repair") is not None:
                summary["contract_repair"].append(
                    {"group": group, **outcome["contract_repair"]})
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

        # D2, live 2026-09-17 (CTSH): a figure with no source is repaired once
        # before it is a refusal.  Here rather than after ``rubric_gate``,
        # where the hard check itself runs, for two reasons: the verifier has
        # not been paid for yet, so a repair costs one call instead of two; and
        # what the verifier signs off has to be the body that is published,
        # which it would not be if the prose were edited after the verdict.
        #
        # Only on a return.  A first draft that fails this check is refused and
        # redrafted whenever the evidence next moves, which costs nobody
        # anything; a returned draft has no such door -- the owner has already
        # spent their attention, and the lane cannot ask them again.
        if review is not None:
            for group in [name for name in wanted
                          if group_outcomes.get(name) == "drafted"]:
                rows, notes = plan[group]
                outcome = repair_group_numbers(
                    model, group=group,
                    questions={ref: question_by_ref[ref]
                               for ref in GROUP_QUESTIONS[group]},
                    material=rows, company=company, mission=mission,
                    answers=answers, classification=classification,
                    prior_answers=prior_bodies, notes=notes, review=draft_review,
                    budget_remaining_micros=run_cost_micros - spent,
                    repair_reserve_micros=producer_reserve + verifier_reserve,
                )
                if outcome["status"] == "ok":
                    continue
                spent += int(outcome.get("cost_micros") or 0)
                summary["numbers_repair"].append({
                    "group": outcome["group"],
                    "status": outcome["status"],
                    "repair_attempts": outcome["repair_attempts"],
                    "figures": {ref: list(items) for ref, items
                                in (outcome.get("figures") or {}).items()},
                    "cost_micros": int(outcome.get("cost_micros") or 0),
                    "reason": outcome.get("reason"),
                })
                if outcome["status"] == "repaired":
                    answers.update(outcome["answers"])
                    # The repair is a producer call like any other, so the
                    # verifier has to bind it or the independence check would
                    # be signing off a reply it never saw named.
                    if outcome.get("route_decision_ref") is not None:
                        draft_routes.append(outcome["route_decision_ref"])
            summary["cost_micros"] = spent

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
        if not fresh and review is not None:
            # D2: a reviewer's return is the new information.  The rows are the
            # same rows -- that is usually the point, "you misread what is
            # already here" -- so the version names what the rewritten answers
            # rest on rather than claiming a novelty it does not have, and the
            # authority lets a ``reviewer_returned`` version through on an
            # unchanged evidence set provided the body actually moved.
            fresh = [dict(row) for row in {
                row["ref"]: row
                for answer in answers.values()
                for row in answer.get("sources") or []
            }.values()]
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
                                          policy=policy, prior=prior,
                                          reviewer_returned=review is not None)
        summary["output_rubric_findings"] = findings
        if findings:
            summary.update({"status": "succeeded", "gate_status": "constitution_refused",
                            "failure_reason": "the Constitution's output_rubric was "
                                              "not satisfied"})
            return summary
        # D1: the last gate before a person's attention is spent.  Everything
        # above asked "is this a well-formed, well-cited document"; this asks
        # "is there anything here to decide".  A draft that answers three of
        # twelve questions from twenty-six rows is both of the first and none
        # of the second, and publishing it would put a research plan in the
        # approvals queue and call it a finding.
        quality = assess(record, dossier=dossier, verifier_passed=True,
                         standard=standard)
        summary["quality"] = quality
        summary["carried_forward_questions"] = carried_forward_questions(record, prior)
        if not quality["submittable"]:
            note = write_note(
                state_dir, company_ref=chosen,
                evidence_fingerprint=attempt_fingerprint,
                body_hash=content_hash(record.get("answers") or []),
                assessment=quality,
                reviewer_questions=summary.get("redrafted_questions") or [],
            )
            summary.update({
                "status": "succeeded",
                "gate_status": "auto_returned",
                "auto_return_note_at": note["created_at"],
                "failure_reason": quality["summary"],
            })
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
        summary["change_note"] = change_note(record, prior)
        summary["changed_questions"] = changed_questions(record, prior)
        if published["status"] == "fresh":
            # The company is in front of a person now, so the note that said it
            # was being held back is no longer true.  A note that outlives its
            # condition is worse than no note.
            clear_note(state_dir, chosen)
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
        _hold_after_return(state_dir, summary)
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


# What a reviewer-returned run that produced no version leaves behind.  Without
# it the redraft would be attempted again on the next process, because the
# in-memory quiet signature dies with the controller and the decision has not
# changed.  One note, keyed on the same (evidence, decision) attempt the loop
# above checks, is what makes "one redraft per return" true across restarts.
_RETURN_HOLD_REASONS: Mapping[str, str] = {
    "duplicate": "按审阅意见重写后，这一版与上一版说的是同一件事，没有形成新版本。",
    "no_new_evidence": "按审阅意见重写后，没有引用到任何新的材料，也没有改变结论。",
    "nothing_drafted": "按审阅意见重写时，一组也没有起草成功。",
    "unverified": "这一轮的费用上限不够做独立复核，所以没有提交。",
    "verification_failed": "按审阅意见重写的内容没有通过独立复核。",
    "not_independent": "复核模型与起草模型同族，这一版的复核不算独立。",
    "rubric_refused": "按审阅意见重写的内容没有通过发布前的结构检查。",
    "constitution_refused": "按审阅意见重写的内容不满足宪法的产出标准。",
    "unresolvable_refs": "按审阅意见重写的内容引用了现在解析不到的材料。",
    "classification_conflict": "第一问的分类与公司档案不一致，整份草稿被拒绝。",
}


# What a failed hard check is called in the one line the owner reads.  The
# rubric's own names are check names -- "numbers_without_refs" tells a reader
# what passed, not what to do -- and this page is the one place the two
# vocabularies meet.
_HARD_CHECK_SHORTFALLS: Mapping[str, str] = {
    "numbers_without_refs": "正文里有数字没有可核验的出处",
    "residual_citation_artefacts": "正文里留下了引用标记的残迹",
}


def _hold_assessment(summary: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """What a held return says, beyond "it did not get through".

    The reason sentence used to be the whole note, so the owner's list
    ("gate_auto_returned") and the approvals page could say a draft was held
    without saying what was wrong with it -- and the person reading that line
    is the only one who can do anything about it.  Everything here is already
    in the run summary: the hard checks that failed, the groups whose replies
    were refused, and, question by question, the figures nothing cited.

    Written in the shape ``assess`` produces, because the two are read by the
    same page and a second shape would be a second thing to keep in step.
    """

    from .deep_insight_gate import GROUP_OF

    labels: list[str] = []
    shortfalls: list[str] = []
    gaps: list[dict[str, str]] = []
    numbers: dict[str, list[str]] = {}
    for item in summary.get("numbers_repair") or []:
        for ref, figures in (item.get("figures") or {}).items():
            numbers.setdefault(str(ref), []).extend(str(one) for one in figures)
    rubric = summary.get("rubric") or {}
    findings = rubric.get("hard_findings") or {}
    # A figure in an answer this run did not draft fails the same check and
    # cannot be repaired -- the group that wrote it was not called -- so it is
    # named here from the rubric's own findings rather than from the repair.
    for row in findings.get("numbers_without_refs") or []:
        ref = str(row.get("section") or "")
        figure = str(row.get("figure") or "")
        if ref in QUESTION_REFS and figure:
            listed = numbers.setdefault(ref, [])
            if figure not in listed:
                listed.append(figure)
    other: dict[str, list[str]] = {}
    for name, rows in findings.items():
        if name == "numbers_without_refs":
            continue
        for row in rows:
            ref = str(row.get("section") or "")
            if ref in QUESTION_REFS:
                other.setdefault(ref, []).append(
                    _HARD_CHECK_SHORTFALLS.get(str(name), str(name)))
    for name in rubric.get("hard_failed") or []:
        shortfalls.append(str(name))
        labels.append(_HARD_CHECK_SHORTFALLS.get(str(name), str(name)))
    refused = [str(row.get("group") or "") for row in summary.get("refused") or []]
    for group in refused:
        shortfalls.append(f"group_refused:{group}")
        labels.append(f"{group} 这一组的回复被整组拒绝")
    for ref in QUESTION_REFS:
        number = QUESTION_REFS.index(ref) + 1
        if ref in numbers:
            gaps.append({
                "question_ref": ref, "question_number": str(number), "question": "",
                "missing": (f"这一问写了 {'、'.join(numbers[ref][:6])}，"
                            "但没有任何被引用的材料带这个数字"),
                "next_step": "补一条确实带这个数字的引用，或者把这个数字从这一问里去掉",
            })
        elif ref in other:
            gaps.append({
                "question_ref": ref, "question_number": str(number), "question": "",
                "missing": "、".join(dict.fromkeys(other[ref])),
                "next_step": "重写这一问；本轮运行摘要的 rubric.hard_findings 里记了具体位置",
            })
        elif GROUP_OF.get(ref) in refused:
            gaps.append({
                "question_ref": ref, "question_number": str(number), "question": "",
                "missing": f"这一问所在的 {GROUP_OF[ref]} 组整组被拒绝，本轮没有重写成",
                "next_step": "重跑这一组；拒绝的原因记在本轮运行摘要的 refused 里",
            })
    detail = ""
    if labels:
        detail += "未通过的是：" + "、".join(dict.fromkeys(labels)) + "。"
    if gaps:
        detail += "涉及：" + "、".join(row["question_ref"] for row in gaps) + "。"
    failure = str(summary.get("failure_reason") or "")
    return {
        "schema_version": "0.1",
        "submittable": False,
        "summary": (reason + detail)[:1000],
        "checks": [],
        "shortfalls": shortfalls,
        "shortfall_labels": list(dict.fromkeys(labels)),
        "counts": {},
        "question_gaps": gaps,
        # For whoever reads the file rather than the page: the run's own words
        # for why it stopped, untranslated.
        "gate_status": str(summary.get("gate_status") or ""),
        "failure_reason": failure[:500],
        "refused_groups": refused,
    }


def _hold_after_return(state_dir: Path, summary: Mapping[str, Any]) -> None:
    status = str(summary.get("gate_status") or "")
    company_ref = summary.get("company_ref")
    fingerprint = summary.get("attempt_fingerprint")
    if (not summary.get("review") or status in ("submitted", "auto_returned")
            or not company_ref or not fingerprint):
        return
    reason = _RETURN_HOLD_REASONS.get(status)
    if reason is None:
        return
    try:
        write_note(
            state_dir, company_ref=str(company_ref),
            evidence_fingerprint=str(fingerprint),
            body_hash=content_hash([status, str(company_ref)]),
            assessment=_hold_assessment(summary, reason),
            reviewer_questions=summary.get("redrafted_questions") or [],
        )
    except OSError:  # a note is a convenience; a run is not failed by one
        return


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
    parser.add_argument("--source-fingerprint")
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
        expected_source_fingerprint=args.source_fingerprint,
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
