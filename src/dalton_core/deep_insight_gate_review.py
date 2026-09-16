"""P12d-D2: what "退回补充" has to mean before it means anything.

Today a returned gate goes back into the same lane that wrote it, is drafted
from the same dossier under the same prompt, and the only trace of the person
who returned it is a row in a decision table nobody downstream reads.  The
reviewer's sentence -- the single most valuable piece of text in the whole
pipeline, because it is the only one a human wrote -- never reaches the model.
The next version is therefore the same version with different words, and the
owner is asked the same question twice.

This module is the loop that makes a return worth making.

**A return names something.**  Either the whole draft, in a sentence, or
individual questions: ``{"q3": "这里把订单和收入搞混了", "q7": "没有回答估值"}``.
Both are allowed because both are how people actually write: sometimes the
problem is the document and sometimes it is question seven.

**The next draft is shown the reviewer's own words, verbatim.**  Not a
paraphrase, not a code, not "the reviewer was dissatisfied".  A model that is
told what a person actually objected to can fix it; a model that is told a
category cannot.

**Only what was named is rewritten.**  The questions the reviewer did not
mention, and that already meet the submission standard, are carried forward
unchanged and say so.  This is the difference between a revision and a
re-roll: a re-roll costs four calls and loses the answers that were fine, and
the reviewer has no way to tell whether their objection was addressed or the
whole document simply came out different this time.

**And the reader can see both.**  The next version carries
``change_reason='reviewer_returned'``; the card shows the previous reviewer's
note beside the list of questions this version actually changed.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .deep_insight_gate import (
    GROUP_OF,
    QUESTION_REFS,
    DeepInsightGateValidationError,
    answer_body,
)

SCHEMA_VERSION = "0.1"
# The longest one note about one question may be.  Generous: this is a person
# typing into a textarea, and truncating what they said is the one thing this
# module exists to stop.
MAX_NOTE_CHARS = 2000
MAX_NOTES = 12

# What a rejected gate means, and what would bring the company back.  Written
# once, here, because "否决" without a re-entry condition reads as "never
# again" and that is not what the ladder does.
REJECT_FOLLOW_UP = (
    "这家公司本轮深度认知门已关闭：不会再起草新版本，也不会再出现在待办里。"
    "要重新开始，需要有人在「已有研究报告需要重新评估」里批准一次重开，"
    "或者初步筛查出新版本并重新过闸——两者都由人发起，系统不会自己回头。"
)
RETURN_FOLLOW_UP = (
    "已退回补充：系统会把你的意见原文带进下一版的起草提示，只重写你点名的问题，"
    "其余保持原样。证据没有变化之前不会重复起草，不会白花模型调用。"
)
APPROVE_FOLLOW_UP = "已通过：这家公司进入下一阶段，后续研究会自动排上。"

DECISION_FOLLOW_UP: Mapping[str, str] = MappingProxyType({
    "approve": APPROVE_FOLLOW_UP,
    "return_for_more_work": RETURN_FOLLOW_UP,
    "reject": REJECT_FOLLOW_UP,
})

CARRIED_FORWARD_NOTE = "沿用上一版（审阅未提意见）"


def validate_question_notes(value: Any) -> dict[str, str]:
    """``{"q3": "...", "q7": "..."}``, or refuse.

    Closed against the Playbook's own twelve refs.  A note about ``q13`` is
    either a typo or a different methodology, and quietly dropping it would
    lose the one sentence a person wrote.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DeepInsightGateValidationError(
            "按题号的审阅意见必须是一个对象，形如 {\"q3\": \"...\"}")
    if len(value) > MAX_NOTES:
        raise DeepInsightGateValidationError(
            f"按题号的审阅意见最多 {MAX_NOTES} 条")
    out: dict[str, str] = {}
    for key, note in value.items():
        if key not in QUESTION_REFS:
            raise DeepInsightGateValidationError(
                f"{key!r} 不是这份草稿的题号；十二问的题号是 q1 到 q12")
        if not isinstance(note, str) or not note.strip():
            raise DeepInsightGateValidationError(f"{key} 的审阅意见不能为空")
        if len(note.strip()) > MAX_NOTE_CHARS:
            raise DeepInsightGateValidationError(
                f"{key} 的审阅意见超过了 {MAX_NOTE_CHARS} 字")
        out[key] = note.strip()
    return {key: out[key] for key in QUESTION_REFS if key in out}


def review_of(decision: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The reviewer's instruction, read off a decision, or ``None``.

    Only ``return_for_more_work`` produces one.  An approval's reason is a
    record of why a company passed and has no business steering the next
    draft; a rejection closes the round outright.
    """

    if not decision or decision.get("decision") != "return_for_more_work":
        return None
    notes = validate_question_notes(decision.get("question_notes"))
    return {
        "schema_version": SCHEMA_VERSION,
        "decided_at": str(decision.get("created_at") or ""),
        "actor_ref": str(decision.get("actor_ref") or ""),
        "gate_version_ref": str(decision.get("gate_version_ref") or ""),
        "reason": str(decision.get("reason") or ""),
        "question_notes": notes,
    }


def named_questions(review: Mapping[str, Any] | None) -> list[str]:
    """The questions the reviewer pointed at, in the Playbook's order."""

    if not review:
        return []
    return [ref for ref in QUESTION_REFS
            if ref in (review.get("question_notes") or {})]


def questions_to_redraft(
    review: Mapping[str, Any] | None,
    *,
    assessment: Mapping[str, Any] | None = None,
    prior: Mapping[str, Any] | None = None,
) -> list[str]:
    """Everything the next draft has to rewrite, and nothing else.

    Three sources, unioned: what the reviewer named; every question the
    submission standard reports as a gap; and -- when a reviewer wrote only a
    whole-draft sentence with no question numbers -- every question that is
    currently unanswered, because a general objection to a document that is a
    third unknown is an objection to the unknowns.

    Empty means "the reviewer named nothing and nothing is short", and the
    caller should draft everything: a return with no handle is still a return.
    """

    wanted: set[str] = set(named_questions(review))
    for gap in (assessment or {}).get("question_gaps") or []:
        ref = str(gap.get("question_ref") or "")
        if ref in QUESTION_REFS:
            wanted.add(ref)
    if review and not named_questions(review):
        for answer in (prior or {}).get("answers") or []:
            if answer.get("status") == "unknown":
                wanted.add(str(answer["question_ref"]))
    return [ref for ref in QUESTION_REFS if ref in wanted]


def groups_to_redraft(question_refs: Iterable[str]) -> list[str]:
    """The call groups those questions fall in.

    Four calls exist because four groups read the same tables; a request to
    rewrite ``q3`` is a request to re-run the industry call, and pretending
    otherwise would mean drafting one question with three-quarters of a prompt.
    """

    from .deep_insight_gate import GROUPS

    groups = {GROUP_OF[ref] for ref in question_refs if ref in GROUP_OF}
    return [group for group in GROUPS if group in groups]


def review_prompt_lines(
    review: Mapping[str, Any] | None, *, group_questions: Sequence[str]
) -> list[str]:
    """The reviewer's words, as prompt lines for one group.

    Verbatim, and labelled as a person's. The whole-draft sentence is shown to
    every group; the per-question notes only to the group that owns them, so a
    call about the market questions is not spending its prompt on an objection
    to question three.
    """

    if not review:
        return []
    lines = [
        "A person read the previous version of this gate and returned it for more work.",
        "Their words, verbatim -- treat them as the specification for this draft:",
        f"  \"{review['reason']}\"",
    ]
    notes = review.get("question_notes") or {}
    mine = [ref for ref in group_questions if ref in notes]
    if mine:
        lines.append("They also wrote about these specific questions:")
        for ref in mine:
            # Deliberately not the two-space-then-tab shape the question list
            # uses below.  That shape is the contract for "these are the
            # questions you must answer", and a note about q3 written in it
            # would read as a fifth question in a four-question group.
            lines.append(f"  - {ref}: \"{notes[ref]}\"")
    lines += [
        "Rewrite the questions they named so that their objection is answered, and say",
        "what changed. Do not simply reword the previous answer: if the objection cannot",
        "be answered from the material shown, return that question as unknown and name",
        "the evidence that would settle it.",
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# what changed, read off the chain
# ---------------------------------------------------------------------------


def carried_forward_questions(
    record: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> list[str]:
    """Questions this version says exactly what the previous one said.

    Derived rather than recorded.  The answer shape is closed and adding a
    "carried" flag to it would put a fact about two versions inside one of
    them; the chain already holds both, so the comparison is a read.
    """

    if prior is None:
        return []
    before = {answer["question_ref"]: answer
              for answer in prior.get("answers") or []}
    same: list[str] = []
    for answer in record.get("answers") or []:
        old = before.get(answer.get("question_ref"))
        if old is not None and _same_answer(old, answer):
            same.append(str(answer["question_ref"]))
    return [ref for ref in QUESTION_REFS if ref in same]


def changed_questions(
    record: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> list[str]:
    if prior is None:
        return list(QUESTION_REFS)
    carried = set(carried_forward_questions(record, prior))
    return [ref for ref in QUESTION_REFS if ref not in carried]


def _same_answer(one: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    if one.get("status") != other.get("status"):
        return False
    if one.get("status") == "unknown":
        return (one.get("unknown") or {}) == (other.get("unknown") or {})
    return (answer_body(one) == answer_body(other)
            and [row["ref"] for row in one.get("sources") or []]
            == [row["ref"] for row in other.get("sources") or []])


def change_note(
    record: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> str:
    """"本版改了什么", in one sentence a person can read."""

    if prior is None:
        return "这是这家公司的第一版深度认知评审草稿。"
    changed = changed_questions(record, prior)
    carried = carried_forward_questions(record, prior)
    if not changed:
        return "本版与上一版的十二问内容相同。"
    parts = [f"本版重写了 {len(changed)} 问：{'、'.join(changed)}。"]
    if carried:
        parts.append(f"其余 {len(carried)} 问{CARRIED_FORWARD_NOTE}。")
    return "".join(parts)


def review_note(review: Mapping[str, Any] | None) -> str:
    """"上一版审阅意见", as one readable block."""

    if not review:
        return ""
    parts = [review.get("reason") or ""]
    for ref, note in (review.get("question_notes") or {}).items():
        parts.append(f"{ref}：{note}")
    return "；".join(part for part in parts if part)


__all__ = [
    "APPROVE_FOLLOW_UP",
    "CARRIED_FORWARD_NOTE",
    "DECISION_FOLLOW_UP",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "REJECT_FOLLOW_UP",
    "RETURN_FOLLOW_UP",
    "SCHEMA_VERSION",
    "carried_forward_questions",
    "change_note",
    "changed_questions",
    "groups_to_redraft",
    "named_questions",
    "questions_to_redraft",
    "review_note",
    "review_of",
    "review_prompt_lines",
    "validate_question_notes",
]
