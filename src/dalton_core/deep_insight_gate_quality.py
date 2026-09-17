"""P12d-D1: is this draft good enough to take a person's time?

Between "the verifier says the sentences are well cited" and "a portfolio
manager should stop what they are doing and read twelve answers" there is a gap,
and the live Core is what it looks like: five drafts sitting in the approvals
queue for three days, two of which classify their company as
``insufficient_evidence`` at question one, two of which answer three of twelve
questions and cite twenty-six rows.  Every one of them passed the verifier,
because the verifier grades *the sentences that were written*, not the document
that was not.

So there is one more gate, and it is deterministic on purpose.  A model asked
"is this good enough" would answer differently on Tuesday; a rule that counts
unknowns and citations answers the same way for ever, can be argued with, and
can be changed by editing a number in a file rather than a prompt.

Five things are counted, and each of them is a thing the owner would have
noticed in the first thirty seconds of reading:

* **question one is answered and agrees with the file.**  Question one is the
  classification, the whole document is read through it, and a draft that says
  ``insufficient_evidence`` there is telling the reader it does not know what
  kind of business this is.  ``classification_agrees`` already refuses an
  outright contradiction before publication; this adds the weaker failure it
  cannot see, which is agreeing with a file that itself knows nothing.
* **at most four of the twelve are unknown.**  Eleven honest unknowns is a
  better *file* than twelve paragraphs of prose -- the rubric says so and it is
  right -- but it is not a better *submission*: there is nothing for a person to
  decide, and the decision they would make is the one this module makes for
  them, which is "go and get more evidence".
* **the draft rests on at least forty rows.**  Not a quality measure by itself;
  a floor.  Twenty-six rows across twelve questions is two per question.
* **the independent verifier passed.**  Carried in rather than re-run: the
  child already paid for that call, and a second opinion here would be a second
  answer that could drift.
* **every answered question writes at least one sentence that cites
  something.**  Structurally guaranteed by ``validate_answer`` today, and
  checked anyway, because this module is where "what makes a submission" is
  written down and a standard that omits its own foundation is not one.

A draft that fails is **not published**.  It does not become a version, so it
does not become an approvals item, so it does not become a demand on a person.
What it becomes is a note: which checks failed, and question by question what
is missing and what would settle it.  The lane then goes quiet until the
evidence moves, because re-drafting the same dossier would buy the same refusal
for four more model calls.

The thresholds are a file, not a constant.  ``deep-insight-gate-submission-
standard.json`` beside the Core overrides any of them, and the standard a note
was written under travels with the note by hash -- so "why was this returned"
is answerable after somebody has changed the numbers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .deep_insight_gate import (
    QUESTION_COUNT,
    answer_body,
    classification_agrees,
    evidence_scope,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
STANDARD_REF = "deep-insight-gate-submission-standard:p12d:v1"
# Where an installation puts its own numbers.  Beside the Core, like every other
# runtime switch this system reads, and absent on nearly every install: the
# defaults below are the standard and the file is how one owner disagrees with
# it without a deploy.
STANDARD_FILE_NAME = "deep-insight-gate-submission-standard.json"
# Where the notes live.  One file per company, owner-only, overwritten by the
# next assessment of the same company: a note is the *current* answer to "why
# is this company not in front of you", and a pile of historical notes would be
# a second version chain nobody asked for.  The version chain is for drafts
# that were published; this is for drafts that were not.
NOTES_DIR_NAME = "deep-insight-gate-returns"

# The classification a dossier reaches when the evidence does not say.  Named
# here rather than imported as a string literal so the two spellings cannot
# drift; the closed list itself is the dossier's.
UNDECIDED_CLASSIFICATION = "insufficient_evidence"
# H2: how long the return note this module offers may get.  The decision's
# ``reason`` is capped at 4000 characters by the gate authority and by the
# cockpit before it, and twelve unknowns can carry four hundred characters
# each; a suggestion the writer would refuse is not a suggestion.
MAX_RETURN_REASON_CHARS = 3600
MAX_RETURN_REASON_LINE_CHARS = 200

DEFAULT_STANDARD: Mapping[str, Any] = MappingProxyType({
    "schema_version": SCHEMA_VERSION,
    "standard_ref": STANDARD_REF,
    # Of twelve.  Four is a third of the document; past that the person is
    # being asked to approve a research plan rather than a finding.
    "max_unknown": 4,
    # Distinct refs across all twelve answers.
    "min_evidence_refs": 40,
    # Question one must be an answer, not an unknown, and not the file's own
    # "the evidence does not say".
    "require_question_one_classified": True,
    # And it must be the same word the dossier reached.
    "require_classification_agrees": True,
    # No verdict is not a pass.  Fail closed.
    "require_verifier_pass": True,
    # Every answered question carries at least one sentence and every sentence
    # cites at least one row.
    "require_sourced_sentences": True,
})

# What each failed check is called on the page.  Short noun phrases, because
# they are joined into one sentence: "系统判定草稿尚不足以提交：缺 X、Y".
SHORTFALL_LABELS: Mapping[str, str] = MappingProxyType({
    "question_one_classified": "第一问的商业模式分类",
    "classification_agrees": "第一问与公司档案一致",
    "unknown_within_cap": "未答问题数在上限内",
    "evidence_refs_sufficient": "足够的证据引用",
    "verifier_passed": "独立复核通过",
    "sourced_sentences": "每一问都有带出处的句子",
})

CHECKS: tuple[str, ...] = tuple(SHORTFALL_LABELS)


class DeepInsightGateStandardError(ValueError):
    """The submission standard file is not a submission standard."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalise_standard(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """One standard, defaults filled in, every field of the declared kind.

    Closed over the default's own field list: an installation that writes a
    field this version does not read has almost certainly mistyped one it
    meant to change, and silently ignoring it would let a threshold look set
    while the old number was still in force.
    """

    out = {key: DEFAULT_STANDARD[key] for key in DEFAULT_STANDARD}
    if value is None:
        return out
    if not isinstance(value, Mapping):
        raise DeepInsightGateStandardError("提交标准必须是一个对象")
    unknown = set(value) - set(DEFAULT_STANDARD)
    if unknown:
        raise DeepInsightGateStandardError(
            "提交标准里有无法识别的项：" + "、".join(sorted(unknown)))
    for key, item in value.items():
        if key in ("schema_version", "standard_ref"):
            if not isinstance(item, str) or not item.strip():
                raise DeepInsightGateStandardError(f"提交标准的 {key} 必须是文字")
            out[key] = item.strip()
            continue
        default = DEFAULT_STANDARD[key]
        if isinstance(default, bool):
            if not isinstance(item, bool):
                raise DeepInsightGateStandardError(f"提交标准的 {key} 必须是是/否")
            out[key] = item
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise DeepInsightGateStandardError(f"提交标准的 {key} 必须是不小于零的整数")
        out[key] = item
    if out["max_unknown"] > QUESTION_COUNT:
        raise DeepInsightGateStandardError(
            f"提交标准的 max_unknown 不能超过十二问的总数 {QUESTION_COUNT}")
    return out


def standard_hash(standard: Mapping[str, Any]) -> str:
    return content_hash({key: standard[key] for key in sorted(standard)})


def load_standard(state_dir: Path | str | None) -> dict[str, Any]:
    """The standard this Core runs under: the file beside the Core, or the default.

    An unreadable file is an error rather than a fallback.  A threshold file
    somebody edited into invalid JSON should stop the lane and say so, not
    quietly restore numbers they were trying to change.
    """

    if state_dir is None:
        return normalise_standard(None)
    path = Path(state_dir).expanduser() / STANDARD_FILE_NAME
    if not path.is_file():
        return normalise_standard(None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepInsightGateStandardError(
            f"深度认知门的提交标准文件读不出来：{path.name}") from exc
    return normalise_standard(raw)


# ---------------------------------------------------------------------------
# the assessment
# ---------------------------------------------------------------------------


def _unknown_refs(record: Mapping[str, Any]) -> list[str]:
    return [answer["question_ref"] for answer in record.get("answers") or []
            if answer.get("status") == "unknown"]


def _unsourced_refs(record: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for answer in record.get("answers") or []:
        if answer.get("status") != "answered":
            continue
        sentences = answer.get("sentences") or []
        if not sentences or not all(row.get("refs") for row in sentences):
            out.append(answer["question_ref"])
    return out


def question_gaps(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The per-question gap list: what is missing, and what would close it.

    Written from the draft's own words rather than invented here.  An unknown
    already carries ``missing`` and ``evidence_that_would_answer`` -- that is
    the whole point of the unknown branch -- and repeating them in a list the
    owner can read in one place is the cheapest useful thing this module does.
    """

    rows: list[dict[str, str]] = []
    for number, answer in enumerate(record.get("answers") or [], start=1):
        ref = str(answer.get("question_ref") or "")
        if answer.get("status") == "unknown":
            unknown = answer.get("unknown") or {}
            rows.append({
                "question_ref": ref,
                "question_number": str(number),
                "question": str(answer.get("question") or ""),
                "missing": str(unknown.get("missing") or ""),
                "next_step": str(unknown.get("evidence_that_would_answer") or ""),
            })
            continue
        if not (answer.get("sentences") or []):
            rows.append({
                "question_ref": ref,
                "question_number": str(number),
                "question": str(answer.get("question") or ""),
                "missing": "这一问标为已答，却没有写出任何一句话",
                "next_step": "重新起草这一组",
            })
    return rows


def assess(
    record: Mapping[str, Any],
    *,
    dossier: Mapping[str, Any] | None = None,
    verifier_passed: bool | None = None,
    standard: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Is this draft worth a person's attention?  Deterministically.

    ``verifier_passed`` is carried in from the run that paid for the verifier.
    ``None`` means nobody asked, and under ``require_verifier_pass`` that is a
    failure rather than a pass: a submission standard that treats "we did not
    check" as "it checked out" is not a standard.
    """

    rules = normalise_standard(standard)
    refs = evidence_scope(record)
    unknowns = _unknown_refs(record)
    unsourced = _unsourced_refs(record)
    classification = str(record.get("classification") or "")
    first = (record.get("answers") or [{}])[0] if record.get("answers") else {}
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str, *, applicable: bool = True) -> None:
        checks.append({
            "check": name,
            "status": ("pass" if passed else "fail") if applicable else "not_applicable",
            "label": SHORTFALL_LABELS[name],
            "detail": detail,
        })

    classified = (str(first.get("status") or "") == "answered"
                  and classification not in ("", UNDECIDED_CLASSIFICATION))
    add("question_one_classified", classified,
        (f"第一问已给出分类「{classification}」。" if classified else
         "第一问没有给出可用的商业模式分类（未答，或落在「证据不足以判断」上）；"
         "十二问都是顺着这个分类读的，分类空着，后面十一问就没有参照系。"),
        applicable=bool(rules["require_question_one_classified"]))

    if dossier is None:
        add("classification_agrees", True,
            "本次没有拿到公司档案，无法比对分类。", applicable=False)
    else:
        agrees, why = classification_agrees(record, dossier)
        filed = str((dossier.get("industry_classification") or {}).get(
            "classification") or "")
        # Agreeing with a file that does not know either is not agreement about
        # anything.  ``classification_agrees`` cannot see this -- it is asked
        # whether two words match -- and it is the exact shape ACN and EPAM are
        # in on the live Core today.
        both_blank = agrees and filed == UNDECIDED_CLASSIFICATION
        add("classification_agrees", agrees and not both_blank,
            ("第一问的分类与公司档案一致。" if agrees and not both_blank else
             ("第一问与公司档案都停在「证据不足以判断」；两份文件一致地什么都没说，"
              "不构成可以据以决策的一致。" if both_blank else why)),
            applicable=bool(rules["require_classification_agrees"]))

    within = len(unknowns) <= int(rules["max_unknown"])
    add("unknown_within_cap", within,
        (f"十二问里有 {len(unknowns)} 问未答，在上限 {rules['max_unknown']} 问之内。"
         if within else
         f"十二问里有 {len(unknowns)} 问未答，超过了上限 {rules['max_unknown']} 问"
         f"（未答的是：{'、'.join(unknowns)}）。"))

    enough = len(refs) >= int(rules["min_evidence_refs"])
    add("evidence_refs_sufficient", enough,
        (f"全文引用了 {len(refs)} 条材料。" if enough else
         f"全文只引用了 {len(refs)} 条材料，低于 {rules['min_evidence_refs']} 条的下限。"))

    add("verifier_passed", verifier_passed is True,
        ("独立复核已通过。" if verifier_passed is True else
         "没有拿到独立复核通过的结论。" if verifier_passed is None else
         "独立复核没有通过。"),
        applicable=bool(rules["require_verifier_pass"]))

    add("sourced_sentences", not unsourced,
        ("每一问已答的内容都写了带出处的句子。" if not unsourced else
         f"这些问题标为已答却没有带出处的句子：{'、'.join(unsourced)}。"),
        applicable=bool(rules["require_sourced_sentences"]))

    failed = [item for item in checks if item["status"] == "fail"]
    submittable = not failed
    if submittable:
        summary = (f"草稿达到提交标准：十二问答了 {QUESTION_COUNT - len(unknowns)} 问，"
                   f"引用 {len(refs)} 条材料，独立复核通过。")
    else:
        summary = ("系统判定草稿尚不足以提交：缺"
                   + "、".join(item["label"] for item in failed) + "。")
    return {
        "schema_version": SCHEMA_VERSION,
        "standard_ref": str(rules["standard_ref"]),
        "standard_hash": standard_hash(rules),
        "standard": rules,
        "submittable": submittable,
        "summary": summary,
        "checks": checks,
        "shortfalls": [item["check"] for item in failed],
        "shortfall_labels": [item["label"] for item in failed],
        "counts": {
            "answered": QUESTION_COUNT - len(unknowns),
            "unknown": len(unknowns),
            "evidence_refs": len(refs),
        },
        "unknown_questions": unknowns,
        "question_gaps": question_gaps(record),
    }


# ---------------------------------------------------------------------------
# the note an auto-returned draft leaves behind
# ---------------------------------------------------------------------------

_NOTE_FIELDS = frozenset({
    "schema_version", "created_at", "company_ref", "evidence_fingerprint",
    "body_hash", "assessment", "reviewer_questions",
})


def notes_dir(state_dir: Path | str) -> Path:
    return Path(state_dir).expanduser() / NOTES_DIR_NAME


def _note_name(company_ref: str) -> str:
    from .deep_insight_gate import company_slug

    return f"{company_slug(company_ref)}.json"


def write_note(
    state_dir: Path | str,
    *,
    company_ref: str,
    evidence_fingerprint: str,
    body_hash: str,
    assessment: Mapping[str, Any],
    reviewer_questions: Sequence[str] = (),
) -> dict[str, Any]:
    """Record why one company's draft was not put in front of a person.

    Owner-only and atomic, like every other artefact this lane writes.  There
    is exactly one per company: the question is "why is this company not in
    front of you *now*", and yesterday's answer is not an answer to it.
    """

    note = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "company_ref": str(company_ref),
        "evidence_fingerprint": str(evidence_fingerprint),
        "body_hash": str(body_hash),
        "assessment": json.loads(canonical_json(assessment)),
        "reviewer_questions": [str(item) for item in reviewer_questions],
    }
    directory = notes_dir(state_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / _note_name(company_ref)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(note) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return note


def read_note(state_dir: Path | str, company_ref: str) -> dict[str, Any] | None:
    path = notes_dir(state_dir) / _note_name(company_ref)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping) or set(value) - _NOTE_FIELDS:
        return None
    return dict(value)


def read_notes(state_dir: Path | str) -> list[dict[str, Any]]:
    """Every auto-return note on this Core, oldest first."""

    directory = notes_dir(state_dir)
    if not directory.is_dir():
        return []
    notes: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping) and not set(value) - _NOTE_FIELDS:
            notes.append(dict(value))
    notes.sort(key=lambda item: (str(item.get("created_at") or ""),
                                 str(item.get("company_ref") or "")))
    return notes


def clear_note(state_dir: Path | str, company_ref: str) -> bool:
    """Drop one company's note.  Called when a draft *is* submitted.

    A note that outlived the condition it describes is worse than no note: the
    page would say a company is being held back at the same moment its draft
    is sitting in the approvals queue.
    """

    path = notes_dir(state_dir) / _note_name(company_ref)
    try:
        path.unlink()
    except OSError:
        return False
    return True


def note_is_current(note: Mapping[str, Any] | None, fingerprint: str) -> bool:
    """Has anything moved since this note was written?

    The lane's durable quiet.  The in-process signature guard dies with the
    controller, and a restart is the commonest event on this host; without this
    every restart would re-pay for four drafting calls to reach the same
    refusal.
    """

    if not note:
        return False
    return str(note.get("evidence_fingerprint") or "") == str(fingerprint)


def held_summary(note: Mapping[str, Any]) -> str:
    """One line for a page that has room for one line."""

    assessment = note.get("assessment") or {}
    return str(assessment.get("summary") or "系统判定草稿尚不足以提交。")


def suggested_return_reason(assessment: Mapping[str, Any]) -> str:
    """The return note this standard would write, in the reviewer's own format.

    H2.  Four drafts were published before the submission standard existed and
    are waiting for a person who can see, in one line, that the system would
    not have shown them at all.  "Not good enough" is not an instruction, so
    this is not one: every line after the first begins with a question number,
    which is exactly what ``cockpit_plane._gate_question_notes`` parses into
    the per-question instructions the redraft prompt reads (D2).  A return
    filled in from here therefore tells the next draft *which* questions to
    rewrite and what evidence would answer them.

    Offered, never sent: it lands in the reviewer's own text box, where it is
    the owner's to edit, replace or delete before they press anything.
    """

    labels = [str(label) for label in assessment.get("shortfall_labels") or []]
    tail = "（以上是系统按提交标准自动填的，改成你自己的话再提交也可以。）"
    lines = [("按提交标准复核，这份草稿还不能进入完整覆盖：缺"
              + "、".join(labels) + "。") if labels else
             "按提交标准复核，这份草稿还需要补充后再看。"]
    # The decision's own ``reason`` is capped, and twelve unknowns can each be
    # four hundred characters. A note that the writer would refuse is worse
    # than a shorter one, so the budget is spent on as many questions as fit
    # and the rest stay in the card's own gap list.
    budget = MAX_RETURN_REASON_CHARS - len(lines[0]) - len(tail) - 2
    for gap in (assessment.get("question_gaps") or [])[:QUESTION_COUNT]:
        ref = str(gap.get("question_ref") or "").strip()
        missing = str(gap.get("missing") or "").strip()[:MAX_RETURN_REASON_LINE_CHARS]
        next_step = str(gap.get("next_step") or "").strip()[:MAX_RETURN_REASON_LINE_CHARS]
        if not ref or not missing:
            continue
        line = f"{ref}：{missing}" + (f"；下一步取{next_step}。" if next_step else "。")
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
    lines.append(tail)
    return "\n".join(lines)


def answer_preview(record: Mapping[str, Any], question_ref: str) -> str:
    """What one question currently says, for a note or a card."""

    for answer in record.get("answers") or []:
        if answer.get("question_ref") == question_ref:
            if answer.get("status") == "unknown":
                unknown = answer.get("unknown") or {}
                return f"未答：{unknown.get('missing', '')}"
            return answer_body(answer)
    return ""


__all__ = [
    "CHECKS",
    "MAX_RETURN_REASON_CHARS",
    "DEFAULT_STANDARD",
    "NOTES_DIR_NAME",
    "SCHEMA_VERSION",
    "SHORTFALL_LABELS",
    "STANDARD_FILE_NAME",
    "STANDARD_REF",
    "UNDECIDED_CLASSIFICATION",
    "DeepInsightGateStandardError",
    "answer_preview",
    "assess",
    "clear_note",
    "held_summary",
    "load_standard",
    "normalise_standard",
    "note_is_current",
    "notes_dir",
    "question_gaps",
    "read_note",
    "read_notes",
    "standard_hash",
    "suggested_return_reason",
    "write_note",
]
