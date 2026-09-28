"""The governance rules every workspace needs before its lanes can run.

A workspace's first mission publishes a governance policy.  Until 2026-09-27
that policy named one rule -- the qualitative document rule -- and every other
thing a lane checks for was left for an owner to discover one refusal at a
time.  On ws-7d that was four separate walls:

* the SEC company-facts lane refuses to start without
  ``research_plan_auto_start`` (``sec_company_facts_lane.
  check_core_governance_rules``) -- policy-5 was signed by hand;
* its 10-K pair and the filed-number rules were never listed, so annual
  growth candidates and re-verified figures stayed staged;
* document extraction's verification context requires the *policy* to carry a
  closed ``research_budget`` (``document_extraction``: "governance lacks
  explicit closed research_budget authority") -- it only appeared after the
  owner happened to edit the budget in the cockpit;
* the mandate's ``research_budget`` was the foundation's budget ceilings
  verbatim, including ``max_alphaengine_probe_calls_24h``, which the closed
  shape refuses.

The legacy install reached the same place over eighteen policy versions.  Its
``policy-18`` is the reference, split here into two kinds of content:

*Runtime baseline* -- identical in every workspace, and therefore published by
the creation flow: every signable ``research_candidate_auto_commit`` rule, the
SEC company-facts ``research_plan_auto_start`` rules, and a closed
``research_budget`` equal to the mission's own three caps.

*Research content* -- legacy's and nobody else's: the weekly-brief plan
bindings in ``weekly_brief_auto_publish`` (a hash of a US IT services plan),
budget values the owner raised over time, ``pools_enforcement``.  A new
workspace gets none of it; ``weekly_brief_auto_publish`` is signed when that
workspace has a weekly-brief plan of its own.

Nothing here writes.  :func:`baseline_policy_body` returns the body the
caller publishes, and :func:`governance_baseline_checks` is what the parity
check reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .coverage_mission import (
    RESEARCH_BUDGET_REQUIRED_FIELDS,
    research_budget_shape_valid,
)
from .research_auto_commit import (
    COMPANY_FACTS_ANNUAL_RULE_REF,
    COMPANY_FACTS_RULE_REF,
    DOCUMENT_QUALITATIVE_RULE_REF,
    KNOWN_RULE_REFS,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    RULE_REF as FILING_COUNT_RULE_REF,
    SEC_FY_MINUS_9M_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
)
from .research_plan import (
    PLAN_AUTO_START_RULE_REFS,
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
)

AUTO_COMMIT_BLOCK = "research_candidate_auto_commit"
PLAN_AUTO_START_BLOCK = "research_plan_auto_start"
RESEARCH_BUDGET_BLOCK = "research_budget"

#: Every auto-commit rule that may share a policy with the others, in the
#: order legacy ``policy-18`` lists them.  The filing-count rule is not here:
#: the evaluator accepts it only as the entire rule set.  The FY - 9M rule
#: (2026-09-28) came after policy-18: a new workspace signs it with the rest,
#: legacy and ws-7d get it from the owner's signing script.
BASELINE_AUTO_COMMIT_RULES: tuple[str, ...] = (
    COMPANY_FACTS_RULE_REF,
    COMPANY_FACTS_ANNUAL_RULE_REF,
    DOCUMENT_QUALITATIVE_RULE_REF,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
    SEC_FY_MINUS_9M_RULE_REF,
)
#: The SEC company-facts plan rules legacy ``policy-18`` lists.  The
#: list-filings rule is no lane's precondition and stays unsigned.
BASELINE_PLAN_AUTO_START_RULES: tuple[str, ...] = (
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
)
#: Per-window bound, not a budget; every signed policy in this codebase uses 20.
BASELINE_MAX_RECORDS = 20

#: Top-level policy keys that carry one install's research content rather
#: than the runtime baseline.  Reported by the parity check as "specific",
#: never as a gap, and never copied into a new workspace.
RESEARCH_SPECIFIC_POLICY_KEYS: frozenset[str] = frozenset({
    "weekly_brief_auto_publish",
})

#: Which lanes stop without each baseline rule, for messages an owner reads.
RULE_CONSUMERS: dict[str, str] = {
    COMPANY_FACTS_RULE_REF: "SEC company-facts lane (10-Q growth candidates)",
    COMPANY_FACTS_ANNUAL_RULE_REF: "SEC company-facts lane (10-K fourth-quarter pairs)",
    DOCUMENT_QUALITATIVE_RULE_REF: "document extraction (finished reviews become Claims)",
    MISSION_VERIFIED_FIGURE_RULE_REF: "quantitative claim promotion (re-verified document figures)",
    SEC_STATEMENT_LINE_RULE_REF: "SEC statement lines (filed financial statement lines)",
    SEC_FY_MINUS_9M_RULE_REF: (
        "SEC quarter lane (fourth quarter of a fiscal-year-only 10-K, FY - 9M)"),
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF: "SEC company-facts lane precondition (10-Q plans)",
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF: "SEC company-facts lane (10-K plans)",
}


class GovernanceBaselineError(ValueError):
    """The baseline cannot be applied to this policy without an owner."""


def closed_research_budget(budget: Mapping[str, Any]) -> dict[str, Any]:
    """The three caps every budget check reads, taken from ``budget``.

    Only the required fields: they are what both the document-extraction
    check and the annual SEC check accept, and a key a verifier does not know
    (``max_alphaengine_probe_calls_24h``) makes the whole block unreadable.
    """

    if not isinstance(budget, Mapping):
        raise GovernanceBaselineError("mission budget must be an object")
    missing = sorted(RESEARCH_BUDGET_REQUIRED_FIELDS - set(budget))
    if missing:
        raise GovernanceBaselineError(f"mission budget lacks {missing}")
    return {key: budget[key] for key in sorted(RESEARCH_BUDGET_REQUIRED_FIELDS)}


def _merged_rules(block: Any, baseline: Sequence[str], *, known: frozenset[str],
                  name: str) -> tuple[list[str], list[str]] | None:
    """``(rules, added)`` or ``None`` when the block is the owner's to change.

    ``None`` for a block an owner switched off, and for the exclusive
    filing-count rule set; extending either would publish something the owner
    did not decide or the evaluator rejects.
    """

    if block is None:
        return list(baseline), list(baseline)
    if not isinstance(block, Mapping):
        raise GovernanceBaselineError(f"policy.{name} is not an object")
    if block.get("enabled") is False:
        return None
    held = [rule for rule in (block.get("rules") or []) if isinstance(rule, str)]
    if FILING_COUNT_RULE_REF in held:
        return None
    unknown = [rule for rule in held if rule not in known]
    if unknown:
        raise GovernanceBaselineError(
            f"policy.{name} lists rules this build does not know: {unknown}")
    added = [rule for rule in baseline if rule not in held]
    return held + added, added


def baseline_policy_body(
    policy: Mapping[str, Any], *, mission_budget: Mapping[str, Any] | None,
    independence_predicates: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """``policy`` with every missing baseline rule added, and what was added.

    Everything already in ``policy`` is kept byte for byte, including an
    existing ``research_budget`` (an owner's raised caps are theirs).
    ``independence_predicates`` is carried into the body because the store
    reads it from there; a caller holding a ``GovernancePolicyVersion.to_dict``
    passes its predicates separately and they must not be lost.
    """

    body = dict(policy)
    if independence_predicates is not None and "independence_predicates" not in body:
        body["independence_predicates"] = [dict(item) for item in independence_predicates]
    added: list[str] = []

    commit = body.get(AUTO_COMMIT_BLOCK)
    merged = _merged_rules(commit, BASELINE_AUTO_COMMIT_RULES,
                           known=KNOWN_RULE_REFS, name=AUTO_COMMIT_BLOCK)
    if merged is not None and merged[1]:
        rules, new = merged
        max_records = (commit.get("max_records") if isinstance(commit, Mapping) else None)
        body[AUTO_COMMIT_BLOCK] = {
            "enabled": True, "rules": rules,
            "max_records": max_records or BASELINE_MAX_RECORDS,
        }
        added += [f"{AUTO_COMMIT_BLOCK}:{rule}" for rule in new]

    start = body.get(PLAN_AUTO_START_BLOCK)
    merged = _merged_rules(start, BASELINE_PLAN_AUTO_START_RULES,
                           known=PLAN_AUTO_START_RULE_REFS, name=PLAN_AUTO_START_BLOCK)
    if merged is not None and merged[1]:
        rules, new = merged
        body[PLAN_AUTO_START_BLOCK] = {"enabled": True, "rules": rules}
        added += [f"{PLAN_AUTO_START_BLOCK}:{rule}" for rule in new]

    # Without a mission there is no budget to derive; the parity check
    # reports the missing block rather than this inventing numbers.
    if (mission_budget is not None
            and not research_budget_shape_valid(body.get(RESEARCH_BUDGET_BLOCK))):
        body[RESEARCH_BUDGET_BLOCK] = closed_research_budget(mission_budget)
        added.append(RESEARCH_BUDGET_BLOCK)
    return body, added


def _check(name: str, ok: bool, detail: str, *, fix: str | None = None,
           severity: str = "gap") -> dict[str, Any]:
    return {"check": name, "status": "ok" if ok else severity, "detail": detail,
            **({"fix": fix} if fix and not ok else {})}


def governance_baseline_checks(
    policy_version: Mapping[str, Any], *, mission: Mapping[str, Any] | None,
    fix_command: str | None = None,
) -> list[dict[str, Any]]:
    """One ok/gap row per baseline requirement of the active policy.

    ``policy_version`` is the stored version wire (``policy`` plus
    ``independence_predicates`` beside it) or a bare policy body.
    """

    policy = policy_version.get("policy") if isinstance(
        policy_version.get("policy"), Mapping) else policy_version
    policy_id = policy_version.get("id") or policy_version.get("policy_version_id") or "?"
    rows: list[dict[str, Any]] = []
    commit = policy.get(AUTO_COMMIT_BLOCK)
    commit_rules = list(commit.get("rules") or []) if isinstance(commit, Mapping) else []
    commit_on = isinstance(commit, Mapping) and commit.get("enabled") is True
    for rule in BASELINE_AUTO_COMMIT_RULES:
        rows.append(_check(
            f"policy.{AUTO_COMMIT_BLOCK}:{rule}", commit_on and rule in commit_rules,
            f"{policy_id} {'lists' if rule in commit_rules else 'does not list'} {rule} "
            f"-- needed by {RULE_CONSUMERS[rule]}", fix=fix_command))
    start = policy.get(PLAN_AUTO_START_BLOCK)
    start_rules = list(start.get("rules") or []) if isinstance(start, Mapping) else []
    start_on = isinstance(start, Mapping) and start.get("enabled") is True
    for rule in BASELINE_PLAN_AUTO_START_RULES:
        rows.append(_check(
            f"policy.{PLAN_AUTO_START_BLOCK}:{rule}", start_on and rule in start_rules,
            f"{policy_id} {'lists' if rule in start_rules else 'does not list'} {rule} "
            f"-- needed by {RULE_CONSUMERS[rule]}", fix=fix_command))
    cap = policy.get(RESEARCH_BUDGET_BLOCK)
    budget_ok = research_budget_shape_valid(cap)
    detail = (f"{policy_id} research_budget={dict(cap) if isinstance(cap, Mapping) else cap!r}")
    if budget_ok and mission is not None:
        over = [key for key in sorted(RESEARCH_BUDGET_REQUIRED_FIELDS)
                if key in mission.get("budget", {}) and mission["budget"][key] > cap[key]]
        if over:
            budget_ok = False
            detail += f"; the mission asks for more than the policy allows: {over}"
    rows.append(_check(f"policy.{RESEARCH_BUDGET_BLOCK}", budget_ok,
                       detail + " -- document extraction and annual SEC reads refuse "
                       "without a closed policy budget covering the mission's",
                       fix=fix_command))
    predicates = policy_version.get("independence_predicates")
    if predicates is None:
        predicates = policy.get("independence_predicates")
    rows.append(_check(
        "policy.independence_predicates", bool(predicates),
        f"{policy_id} carries {len(predicates or [])} independence predicate(s); a fresh "
        "Core starts with producer.model_family != verifier.model_family, and a cockpit "
        "budget edit before 2026-09-27 dropped it silently",
        fix="owner decision: scripts/restore_independence_predicates.py --state-dir <this "
            "environment> (dry run first) republishes the policy with the default predicate "
            "and rebinds the constitution and mission",
        severity="drift"))
    return rows


def research_specific_policy_keys(policy: Mapping[str, Any]) -> list[str]:
    return sorted(key for key in policy if key in RESEARCH_SPECIFIC_POLICY_KEYS)


__all__ = [
    "AUTO_COMMIT_BLOCK",
    "BASELINE_AUTO_COMMIT_RULES",
    "BASELINE_MAX_RECORDS",
    "BASELINE_PLAN_AUTO_START_RULES",
    "GovernanceBaselineError",
    "PLAN_AUTO_START_BLOCK",
    "RESEARCH_BUDGET_BLOCK",
    "RESEARCH_SPECIFIC_POLICY_KEYS",
    "RULE_CONSUMERS",
    "baseline_policy_body",
    "closed_research_budget",
    "governance_baseline_checks",
    "research_specific_policy_keys",
]
