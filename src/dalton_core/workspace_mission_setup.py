"""Draft and publish the first mission in an otherwise blank workspace.

The module deliberately does not invent a second research OS.  A draft binds
the workspace and the reusable method foundation, while publication delegates
method-authority materialisation to a preparer and writes the resulting body
through :class:`CoverageMissionAuthority`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .coverage_mission import CoverageMissionAuthority, validate_mission_body
from .store import content_hash
from .workspace import WorkspacePaths

SCHEMA_VERSION = "workspace-first-mission-draft-0.1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_HUMAN = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_TICKER = re.compile(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9.-]{0,9})\b")
_EXCHANGE_TICKER = re.compile(
    r"\b(?:NASDAQ|NYSE|LSE|HKEX|TSX|ASX)\s*[:：]\s*([A-Z0-9][A-Z0-9.-]{0,9})\b",
    re.IGNORECASE,
)


class WorkspaceMissionSetupError(ValueError):
    pass


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceMissionSetupError(f"{name} must be non-empty text")
    return value.strip()


def _foundation(value: Mapping[str, Any], workspace: WorkspacePaths) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceMissionSetupError("method foundation must be an object")
    wire = dict(value)
    asserted = wire.pop("content_hash", None)
    if not isinstance(asserted, str) or _SHA.fullmatch(asserted) is None \
            or asserted != content_hash(wire):
        raise WorkspaceMissionSetupError("method foundation content hash differs")
    if wire.get("workspace_id") != workspace.workspace_id:
        raise WorkspaceMissionSetupError("method foundation belongs to another workspace")
    if wire.get("setup_state") != "awaiting_mission":
        raise WorkspaceMissionSetupError("method foundation is not awaiting a mission")
    methods = wire.get("methods")
    defaults = wire.get("mission_defaults")
    if not isinstance(methods, Mapping) or not isinstance(defaults, Mapping):
        raise WorkspaceMissionSetupError("method foundation lacks methods or mission defaults")
    return {**wire, "content_hash": asserted}


def _industry_from_goal(goal: str) -> str | None:
    patterns = (
        r"(?:研究|覆盖|分析)\s*([^，。；,;.]{2,40}?)(?:行业|板块)",
        r"(?:research|cover|analy[sz]e)\s+(?:the\s+)?([A-Za-z][A-Za-z &/-]{1,50}?)\s+industry\b",
    )
    for pattern in patterns:
        match = re.search(pattern, goal, re.IGNORECASE)
        if match:
            return match.group(1).strip(" ：:的")
    return None


def _slug(value: str) -> str:
    result = re.sub(r"[^\w]+", "-", value.lower(), flags=re.UNICODE).strip("-_")
    return result[:48] or "research"


def _companies_from_goal(goal: str) -> list[dict[str, str]]:
    tickers: list[str] = []
    for pattern in (_TICKER, _EXCHANGE_TICKER):
        for match in pattern.finditer(goal):
            ticker = match.group(1).upper()
            if ticker not in tickers:
                tickers.append(ticker)
    return [{"company_ref": f"company:ticker:{ticker.lower()}", "ticker": ticker}
            for ticker in tickers]


def _company_rows(companies: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, raw in enumerate(companies):
        if not isinstance(raw, Mapping):
            raise WorkspaceMissionSetupError("companies must contain objects")
        ticker = _text(raw.get("ticker"), f"companies[{index}].ticker").upper()
        company_ref = _text(raw.get("company_ref"), f"companies[{index}].company_ref")
        rows.append({
            "company_ref": company_ref, "ticker": ticker,
            "coverage_tier": str(raw.get("coverage_tier", "A")),
            "bootstrap_priority": str(raw.get("bootstrap_priority", "P0" if index == 0 else "P1")),
        })
    return rows


def _source_plan(defaults: Mapping[str, Any]) -> list[dict[str, str]]:
    configured = defaults.get("source_plan")
    if isinstance(configured, list) and configured:
        return [dict(item) for item in configured]
    refs = defaults.get("source_capability_refs")
    if isinstance(refs, list) and refs:
        return [{"source_ref": _text(ref, "source_capability_refs[]"),
                 "role": "Reusable workspace source capability", "status": "connected"}
                for ref in refs]
    return []


def draft_first_mission(
    workspace: WorkspacePaths,
    *,
    goal: str,
    method_foundation: Mapping[str, Any],
    industry: str | None = None,
    companies: Sequence[Mapping[str, Any]] | None = None,
    research_questions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Produce a bounded, reviewable proposal without writing authority."""
    goal = _text(goal, "goal")
    foundation = _foundation(method_foundation, workspace)
    defaults = dict(foundation["mission_defaults"])
    industry_name = industry.strip() if isinstance(industry, str) and industry.strip() else _industry_from_goal(goal)
    company_candidates = list(companies) if companies is not None else _companies_from_goal(goal)
    universe = _company_rows(company_candidates)
    issues: list[str] = []
    if industry_name is None:
        issues.append("industry requires review")
    if not universe:
        issues.append("at least one company and ticker require review")
    questions = ([_text(item, "research_questions[]") for item in research_questions]
                 if research_questions is not None else [
                     f"What evidence would confirm or reject this objective: {goal}",
                     "Which causal drivers explain the differences across the selected companies?",
                     "What observable events would falsify the leading view?",
                 ])
    autonomy_source = defaults.get("autonomy", {})
    autonomy = {
        "automation_principal": autonomy_source.get("automation_principal", "automation:coverage-mission"),
        "may_write": list(autonomy_source.get("may_write", autonomy_source.get("allowed_write_scopes", []))),
        "human_checkpoints": list(autonomy_source.get("human_checkpoints", [
            "deep_insight_gate", "investment_memo", "thesis_admission", "thesis_revision",
            "forecast_overturn", "scope_expansion", "budget_expansion",
        ])),
    }
    body = {
        "title": goal.splitlines()[0][:200], "objective": goal,
        "industry_ref": f"industry:{_slug(industry_name or 'pending-review')}",
        "universe": universe, "research_questions": questions,
        "deliverables": list(defaults.get("deliverables", [
            "industry_framework", "initial_screen", "industry_model", "company_model",
            "forecast_lines", "investment_memo", "weekly_brief",
        ])),
        "source_plan": _source_plan(defaults), "bindings": None,
        "autonomy": autonomy, "budget": dict(defaults.get("budget_ceilings", {})),
    }
    if not body["source_plan"]:
        issues.append("source plan requires review")
    required_budget = {"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h"}
    if not required_budget.issubset(body["budget"]):
        issues.append("bounded budget defaults require review")
    proposal_body = {
        "schema_version": SCHEMA_VERSION, "workspace_id": workspace.workspace_id,
        "workspace_manifest_hash": workspace.content_hash,
        "method_foundation_hash": foundation["content_hash"],
        "setup_state": "review_required" if issues else "ready_for_confirmation",
        "review_issues": issues, "mission_body": body,
    }
    return {**proposal_body, "content_hash": content_hash(proposal_body)}


def publish_first_mission(
    workspace: WorkspacePaths,
    *,
    proposal: Mapping[str, Any],
    proposal_hash: str,
    actor_ref: str,
    authority: CoverageMissionAuthority,
    prepare_bindings: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Confirm one exact draft, materialise existing authorities, then publish."""
    if not isinstance(proposal, Mapping):
        raise WorkspaceMissionSetupError("proposal must be an object")
    wire = dict(proposal)
    asserted = wire.pop("content_hash", None)
    if asserted != proposal_hash or asserted != content_hash(wire):
        raise WorkspaceMissionSetupError("proposal confirmation hash differs")
    if wire.get("schema_version") != SCHEMA_VERSION or wire.get("workspace_id") != workspace.workspace_id \
            or wire.get("workspace_manifest_hash") != workspace.content_hash:
        raise WorkspaceMissionSetupError("proposal belongs to another workspace")
    if wire.get("setup_state") != "ready_for_confirmation" or wire.get("review_issues") != []:
        raise WorkspaceMissionSetupError("proposal still requires review")
    actor_ref = _text(actor_ref, "actor_ref")
    if _HUMAN.fullmatch(actor_ref) is None:
        raise WorkspaceMissionSetupError("actor_ref must use the human: namespace")
    body = dict(wire["mission_body"])
    bindings = dict(prepare_bindings(proposal, actor_ref))
    body["bindings"] = bindings
    body = validate_mission_body(body)
    identity = asserted[:24]
    mission_ref = f"coverage-mission:{workspace.slug}"
    return authority.create_mission(
        mission_ref, **body, actor_ref=actor_ref,
        version_id=f"coverage-mission-version:{workspace.slug}:{identity}",
        prior_version_ref=None,
        idempotency_key=f"workspace-first-mission:{workspace.workspace_id}:{asserted}",
    )
