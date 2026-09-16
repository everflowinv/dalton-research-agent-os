"""P14d-D3: forty-seven demands on the owner's attention, none of them current.

The weekly reopen lane asks one question of every company whose Initial Screen
has passed: has the evidence under it thickened enough to be worth re-issuing?
When the answer is yes it writes a proposal, and a person decides.  That is the
right shape and it is not what the live Core looks like.  The live Core holds
forty-seven undecided proposals across four companies -- fourteen for one of
them -- every single one of which names a screen version that is no longer the
passed one.  A newer screen was published on the 14th, passed, and became the
version any sensible reopen would be measured against; the proposals still
point at the version before it.

Two defects, and they are different.

**A proposal whose ``passed_version_ref`` is not the currently passed version
is about a document nobody would re-issue.**  Approving it would ask the ladder
to reopen a gate that was already superseded; declining it is a decision about
nothing.  Either way it is noise, and it is *decidably* noise: the predicate is
one read of the stage ladder, and it does not need a model, a policy or a
person.  So it is computed rather than decided -- the approvals page stops
offering it, says how many it stopped offering and why, and the human decision
ledger stays what it is, which is a record of decisions humans made.  Writing a
machine verdict into ``gate_reopen_decisions`` would have been the other way to
"close" these, and it would have made the one table that proves a person looked
at something into a table that no longer proves it.

**And the same proposal keeps arriving.**  The authority is idempotent on
(company, assessment hash), which sounds like the guard for this and is not:
the assessment hash includes the evidence counts, so every new filing, every
extraction run, every claim indexed produces a *different* hash for the same
"the evidence thickened" proposal.  Fourteen of them in five days.  The guard
that actually holds is the one this module states: while an undecided proposal
already exists for this company, this stage and this passed version, there is
nothing new to ask.  When the screen is re-issued and passes, the passed version
moves and the question becomes a different question -- which is exactly when it
should be asked again.

Nothing here writes.  It is a predicate and a count, read by the approvals page
and by the lane that proposes.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

SCHEMA_VERSION = "0.1"
STAGE_REF = "initial_screen"
SUPERSEDED_REASON = "已被新版初步筛查取代"
SUPERSEDED_DETAIL = (
    "这条重新评估提案针对的是旧版初步筛查报告，而这家公司之后已经出过新版本并重新过闸。"
    "对旧版本作决定改变不了任何事，所以不再放进待办；等新版本下真的又有证据变厚，"
    "系统会重新提一次。"
)
DUPLICATE_DETAIL = (
    "这家公司在当前这版初步筛查报告下已经有一条还没裁决的重新评估提案，"
    "同一个问题不重复问第二遍。"
)


def _has_table(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def current_passed_version_ref(
    connection: Any, company_ref: str, *, stage_ref: str = STAGE_REF
) -> str | None:
    """The deliverable version this company's gate is passed against right now.

    Delegated to the reopen authority's own ``passed_version`` rather than
    re-derived: it already folds the ladder, already knows that an approved but
    un-issued reopen means there *is* no passed version, and two answers to
    "which version is the passed one" is how a de-noiser starts hiding real
    work.
    """

    from .deliverable_reopen import passed_version

    try:
        passed = passed_version(connection, company_ref=company_ref,
                                stage_ref=stage_ref)
    except Exception:  # noqa: BLE001 - an absent or older ladder is a valid state
        return None
    return None if passed is None else str(passed["version_id"])


def is_superseded(connection: Any, proposal: Mapping[str, Any]) -> bool:
    """Does this proposal name a screen version that is no longer the passed one?

    A company with no currently passed version at all counts as superseded:
    its gate is already open (somebody approved a reopen and the re-issued
    screen has not landed), so a proposal to open it again is answering a
    question that has been answered.
    """

    named = str(proposal.get("passed_version_ref") or "")
    if not named:
        return True
    current = current_passed_version_ref(
        connection, str(proposal.get("company_ref") or ""),
        stage_ref=str(proposal.get("stage_ref") or STAGE_REF))
    return current != named


def undecided_proposals(connection: Any) -> list[dict[str, Any]]:
    """Every gate-reopen proposal nobody has answered, oldest first.

    Plain SQL rather than the authority, because the cockpit process holds no
    Core write handle (ADR-0006) and opening a write authority to read four
    columns would create tables on a read path.
    """

    if not _has_table(connection, "gate_reopen_proposals"):
        return []
    rows = connection.execute(
        "SELECT p.proposal_id, p.company_ref, p.stage_ref, p.passed_version_ref, "
        "p.change_reason, p.created_at, p.content_hash, p.record_json "
        "FROM gate_reopen_proposals p "
        "LEFT JOIN gate_reopen_decisions d ON d.proposal_ref=p.proposal_id "
        "WHERE d.decision_id IS NULL ORDER BY p.created_at, p.proposal_id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError):
            record = {}
        out.append({
            "proposal_ref": row["proposal_id"],
            "company_ref": row["company_ref"],
            "stage_ref": row["stage_ref"] or STAGE_REF,
            "passed_version_ref": row["passed_version_ref"],
            "change_reason": row["change_reason"],
            "created_at": row["created_at"],
            "content_hash": row["content_hash"],
            "record": record,
        })
    return out


def partition(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Split the undecided proposals into the live ones and the stale ones.

    One pass and one cached answer per company: on the live Core this is
    forty-seven proposals over four companies, and asking the ladder
    forty-seven times for four answers is the kind of thing that makes a page
    slow for no reason.
    """

    current: dict[tuple[str, str], str | None] = {}
    live: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for proposal in undecided_proposals(connection):
        key = (str(proposal["company_ref"]), str(proposal["stage_ref"]))
        if key not in current:
            current[key] = current_passed_version_ref(
                connection, key[0], stage_ref=key[1])
        passed = current[key]
        if not proposal["passed_version_ref"] or passed != proposal["passed_version_ref"]:
            superseded.append({**proposal, "current_passed_version_ref": passed,
                               "reason": SUPERSEDED_REASON})
        else:
            live.append({**proposal, "current_passed_version_ref": passed})
    return {"live": live, "superseded": superseded}


def superseded_refs(connection: Any) -> set[str]:
    """Just the ids, for a page that only needs to know what to leave out."""

    return {str(item["proposal_ref"]) for item in partition(connection)["superseded"]}


def superseded_summary(connection: Any) -> dict[str, Any] | None:
    """One line about everything that was left out, or ``None`` when nothing was.

    Shown rather than silently dropped.  A page that quietly stops displaying
    forty-seven items is indistinguishable from a page that is broken, and the
    owner has no way to ask why their to-do list emptied overnight.
    """

    stale = partition(connection)["superseded"]
    if not stale:
        return None
    by_company: dict[str, int] = {}
    for item in stale:
        by_company[str(item["company_ref"])] = by_company.get(
            str(item["company_ref"]), 0) + 1
    return {
        "count": len(stale),
        "companies": dict(sorted(by_company.items())),
        "reason": SUPERSEDED_REASON,
        "detail": SUPERSEDED_DETAIL,
        "proposal_refs": sorted(str(item["proposal_ref"]) for item in stale),
        "at": max(str(item["created_at"]) for item in stale),
    }


def already_open_for_current(
    connection: Any, *, company_ref: str, stage_ref: str = STAGE_REF
) -> dict[str, Any] | None:
    """The undecided proposal that already asks this question, if there is one.

    The guard the weekly lane needs before it proposes.  "The evidence
    thickened again" is not a second question while the first one is still
    unanswered: the person who answers it will be reading the same ladder and
    the same screen version either way.
    """

    passed = current_passed_version_ref(connection, company_ref, stage_ref=stage_ref)
    if passed is None:
        return None
    for proposal in undecided_proposals(connection):
        if (str(proposal["company_ref"]) == str(company_ref)
                and str(proposal["stage_ref"]) == str(stage_ref)
                and str(proposal["passed_version_ref"]) == passed):
            return proposal
    return None


# ---------------------------------------------------------------------------
# the low-information conviction call
# ---------------------------------------------------------------------------

# A call that says "avoid", proposes no change and grades its own confidence
# low is not a recommendation; it is the machine reporting that it has nothing
# to say about this company yet.  It is worth showing -- the owner asked for a
# call and this is the answer -- but it is not worth the same weight as a real
# one, and it should be dismissable in one click rather than requiring a
# written rationale for declining to act on nothing.
LOW_INFORMATION_DIRECTIONS = frozenset({"avoid"})
LOW_INFORMATION_STANDARDS = frozenset({"NO_CHANGE", "no_change"})
LOW_INFORMATION_CONFIDENCE = frozenset({"low"})
LOW_INFORMATION_NOTE = (
    "这是一条信息量很低的提案：方向是「回避」、相对上一次判断没有变化、"
    "系统自己给的把握也只有「低」。它不构成建议，只说明系统目前对这家公司还没有形成看法；"
    "直接驳回不会丢失任何研究结论。"
)


def low_information_call(row: Any) -> bool:
    """Is this conviction-call proposal one of those?  All three, or none.

    Takes a mapping or a ``sqlite3.Row``, because the page reads rows and the
    tests read dictionaries and a predicate that only accepts one of them is a
    predicate with a second copy somewhere.
    """

    def field(name: str) -> str:
        try:
            value = row[name]
        except (KeyError, IndexError, TypeError):
            return ""
        return "" if value is None else str(value)

    return (field("direction") in LOW_INFORMATION_DIRECTIONS
            and field("risk_reward_status") in LOW_INFORMATION_STANDARDS
            and field("confidence") in LOW_INFORMATION_CONFIDENCE)


__all__ = [
    "DUPLICATE_DETAIL",
    "LOW_INFORMATION_CONFIDENCE",
    "LOW_INFORMATION_DIRECTIONS",
    "LOW_INFORMATION_NOTE",
    "LOW_INFORMATION_STANDARDS",
    "SCHEMA_VERSION",
    "STAGE_REF",
    "SUPERSEDED_DETAIL",
    "SUPERSEDED_REASON",
    "already_open_for_current",
    "current_passed_version_ref",
    "is_superseded",
    "low_information_call",
    "partition",
    "superseded_refs",
    "superseded_summary",
    "undecided_proposals",
]
