"""Draft and publish the first mission in an otherwise blank workspace.

The module deliberately does not invent a second research OS.  A draft binds
the workspace and the reusable method foundation, while publication delegates
method-authority materialisation to a preparer and writes the resulting body
through :class:`CoverageMissionAuthority`.
"""
from __future__ import annotations

import re
import json
import os
import tempfile
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .coverage_mission import CoverageMissionAuthority, validate_mission_body
from .agenda import AgendaStore
from .coverage_admission import CoverageAdmissionAuthority
from .cockpit_model import (
    SETUP_PLANNING_SCHEMA_VERSION, unwrap_json_object,
    validate_setup_planning_context,
)
from .store import content_hash
from .research_constitution import ResearchConstitutionAuthority
from .research_playbook import read_exact_playbook_version
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
    title: str | None = None,
    objective: str | None = None,
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
        "title": (_text(title, "title") if title is not None else goal.splitlines()[0])[:200],
        "objective": (_text(objective, "objective") if objective is not None else goal)[:2000],
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


def setup_planning_context(
    workspace: WorkspacePaths, *, method_foundation: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Bind the pre-mission model call to its own finite setup allowance."""
    foundation = _foundation(method_foundation, workspace)
    allowance = foundation.get("setup_planning_budget")
    if not isinstance(allowance, Mapping):
        raise WorkspaceMissionSetupError("method foundation lacks setup planning budget")
    calls = allowance.get("max_model_calls")
    cost = allowance.get("max_cost_usd")
    body = {
        "schema_version": SETUP_PLANNING_SCHEMA_VERSION,
        "setup_ref": f"workspace-setup:{workspace.workspace_id}:{foundation['content_hash'][:16]}",
        "workspace_id": workspace.workspace_id,
        "foundation_ref": f"workspace-research-foundation:{workspace.workspace_id}",
        "foundation_hash": foundation["content_hash"],
        # The shared day-budget authority has historic mission-shaped names.
        # These values are setup authority, and the WorkOrder says so.
        "budget": {
            "max_daily_paid_calls": calls,
            "max_daily_cost_usd": cost,
            "max_alphaengine_calls_24h": 0,
        },
        # Stable across a retried request. Scheduler records its actual insert
        # time separately; this timestamp is part of immutable work identity.
        "created_at": _text(
            foundation.get("created_at", foundation.get(
                "generated_at", "1970-01-01T00:00:00.000000+00:00")),
            "created_at"),
    }
    return validate_setup_planning_context({**body, "content_hash": content_hash(body)})


def first_mission_goal_prompt(goal: str) -> str:
    """The structured output contract shared by blank-workspace onboarding."""
    return "\n".join([
        "You plan the first mission for a blank autonomous equity research workspace.",
        "Extract the industry and companies from the owner's words. Do not invent a ticker.",
        "Turn the goal into 3 to 8 answerable research questions and concrete subtasks.",
        "Keep the owner's language. Return raw JSON only, without a markdown fence:",
        '{"summary":"...","title":"...","objective":"...",',
        ' "industry":{"name":"...","reason":"..."},',
        ' "research_questions":["..."],"subtasks":["..."],',
        ' "suggested_companies":[{"ticker":"XYZ","name":"...","reason":"..."}]}',
        "Use an empty suggested_companies list or empty industry name when the owner did not supply enough information.",
        "Owner's research goal: " + _text(goal, "goal"),
    ])


def plan_first_mission_goal(
    model: Any, workspace: WorkspacePaths, *, goal: str,
    method_foundation: Mapping[str, Any], request_id: str, created_at: str,
) -> dict[str, Any]:
    """Make the one production model call, then normalize it into a safe draft."""
    context = setup_planning_context(
        workspace, method_foundation=method_foundation, created_at=created_at)
    call = model.call_setup(
        request_id=_text(request_id, "request_id"),
        prompt=first_mission_goal_prompt(goal), planning_context=context,
    )
    parsed = unwrap_json_object(call.get("text", "")) if isinstance(call, Mapping) else None
    if parsed is None:
        raise WorkspaceMissionSetupError("model did not return a structured first mission")
    industry_value = parsed.get("industry")
    industry = (industry_value.get("name") if isinstance(industry_value, Mapping)
                and isinstance(industry_value.get("name"), str) else None)
    companies = []
    for item in parsed.get("suggested_companies", []):
        if isinstance(item, Mapping) and isinstance(item.get("ticker"), str) \
                and item["ticker"].strip():
            ticker = item["ticker"].strip().upper()[:12]
            companies.append({"company_ref": f"company:ticker:{ticker.lower()}",
                              "ticker": ticker})
    questions = [item for item in parsed.get("research_questions", [])
                 if isinstance(item, str) and item.strip()][:12]
    return draft_first_mission(
        workspace, goal=goal, method_foundation=method_foundation,
        industry=industry, companies=companies,
        research_questions=questions or None,
        title=parsed.get("title") if isinstance(parsed.get("title"), str) else None,
        objective=(parsed.get("objective")
                   if isinstance(parsed.get("objective"), str) else None),
    )


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


def prepare_first_mission_bindings(
    store: Any, *, method_foundation: Mapping[str, Any],
    proposal: Mapping[str, Any], actor_ref: str,
) -> dict[str, dict[str, str]]:
    """Materialise the mission-specific authorities in the workspace Core."""
    if not isinstance(proposal, Mapping) or not isinstance(proposal.get("mission_body"), Mapping):
        raise WorkspaceMissionSetupError("proposal lacks a mission body")
    actor_ref = _text(actor_ref, "actor_ref")
    if _HUMAN.fullmatch(actor_ref) is None:
        raise WorkspaceMissionSetupError("actor_ref must use the human: namespace")
    foundation = dict(method_foundation)
    methods = foundation.get("methods")
    if not isinstance(methods, Mapping):
        raise WorkspaceMissionSetupError("method foundation lacks methods")
    body = dict(proposal["mission_body"])
    industry_ref = _text(body.get("industry_ref"), "industry_ref")
    digest = _text(proposal.get("content_hash"), "proposal.content_hash")
    suffix = digest[:24]

    playbook_spec = methods.get("playbook")
    playbook_binding = (playbook_spec.get("binding")
                        if isinstance(playbook_spec, Mapping) else None)
    if not isinstance(playbook_binding, Mapping):
        raise WorkspaceMissionSetupError("method foundation lacks playbook authority binding")
    playbook = read_exact_playbook_version(store.connection, playbook_binding.get("ref"))
    if playbook["content_hash"] != playbook_binding.get("hash"):
        raise WorkspaceMissionSetupError("method foundation playbook binding differs")

    driver_spec = methods.get("driver_pack_template")
    driver_value = driver_spec.get("value") if isinstance(driver_spec, Mapping) else None
    if not isinstance(driver_value, Mapping):
        raise WorkspaceMissionSetupError("method foundation lacks driver pack template value")
    template_hash = driver_spec.get("content_hash")
    if template_hash != content_hash(driver_value):
        raise WorkspaceMissionSetupError("driver pack template content hash differs")
    required_driver_fields = {"drivers", "metric_specs", "thesis_templates"}
    if not required_driver_fields.issubset(driver_value):
        raise WorkspaceMissionSetupError("driver pack template is incomplete")

    mission_budget = dict(body.get("budget", {}))
    agenda = AgendaStore(store)
    mandate_ref = f"mandate:first-mission:{suffix}"
    mandate = agenda.create_mandate(
        mandate_ref, objective=_text(body.get("objective"), "objective"),
        scope_refs=[industry_ref], constraints={"research_budget": mission_budget},
        success_criteria={"deliverables": list(body.get("deliverables", [])),
                          "research_questions": list(body.get("research_questions", []))},
        effective_from="1970-01-01T00:00:00.000000+00:00", effective_until=None,
        actor_ref=actor_ref, activate=True,
        version_id=f"mandate-version:first-mission:{suffix}:1",
        idempotency_key=f"workspace-first-mission:{digest}:mandate",
    )
    pack = CoverageAdmissionAuthority(store).register_driver_pack(
        f"driver-pack:first-mission:{suffix}", industry_ref=industry_ref,
        title=f"{body['title']} Driver Pack",
        drivers=list(driver_value["drivers"]),
        metric_specs=list(driver_value["metric_specs"]),
        thesis_templates=list(driver_value["thesis_templates"]), actor_ref=actor_ref,
        version_id=f"driver-pack-version:first-mission:{suffix}:1",
        prior_version_ref=None,
        idempotency_key=f"workspace-first-mission:{digest}:driver-pack",
    )
    method_spec = methods.get("constitution_method")
    method = method_spec.get("value") if isinstance(method_spec, Mapping) else None
    if not isinstance(method, Mapping) or method_spec.get("content_hash") != content_hash(method):
        raise WorkspaceMissionSetupError("constitution method template differs")
    policy = store.active_policy_version()
    constitution = ResearchConstitutionAuthority(store).publish_constitution(
        f"constitution:first-mission:{suffix}", industry_ref=industry_ref,
        title=f"{body['title']} Research Constitution",
        bindings={
            "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
            "driver_pack_version": {"ref": pack["id"], "hash": pack["content_hash"]},
            "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
            "doctrine_pack_version": None, "weekly_brief_plan": None,
        }, method=method, actor_ref=actor_ref,
        version_id=f"constitution-version:first-mission:{suffix}:1",
        prior_version_ref=None,
        idempotency_key=f"workspace-first-mission:{digest}:constitution",
    )
    return {
        "playbook_version": {"ref": playbook["id"], "hash": playbook["content_hash"]},
        "constitution_version": {"ref": constitution["id"], "hash": constitution["content_hash"]},
        "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
    }


def publish_first_mission_to_store(
    store: Any, workspace: WorkspacePaths, *, proposal: Mapping[str, Any],
    proposal_hash: str, actor_ref: str,
    method_foundation: Mapping[str, Any],
) -> dict[str, Any]:
    """Production entry point used by the owner-only writer operation."""
    foundation = _foundation(method_foundation, workspace)
    if proposal.get("method_foundation_hash") != foundation["content_hash"]:
        raise WorkspaceMissionSetupError("proposal method foundation binding differs")
    authority = CoverageMissionAuthority(store)
    mission = publish_first_mission(
        workspace, proposal=proposal, proposal_hash=proposal_hash,
        actor_ref=actor_ref, authority=authority,
        prepare_bindings=lambda candidate, actor: prepare_first_mission_bindings(
            store, method_foundation=foundation, proposal=candidate,
            actor_ref=actor),
    )
    materialize_first_mission_discovery_plans(workspace, mission)
    return mission


def _atomic_json(path: Any, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        try: os.unlink(temporary_name)
        except FileNotFoundError: pass


def materialize_first_mission_discovery_plans(
    workspace: WorkspacePaths, mission: Mapping[str, Any], *,
    sec_ticker_resolver: Callable[[str], Mapping[str, str]] | None = None,
) -> dict[str, str]:
    """Write the approved, mission-specific search plans selected at restart."""
    from .mission_source_discovery import (
        ALPHAENGINE_SOURCE_REF, SEC_SOURCE_REF, WEB_SEARCH_SOURCE_REF,
        build_discovery_plan, validate_discovery_plan,
    )
    from .macos_launchagent import (
        ALPHAENGINE_PLAN_SELECTOR, SEC_PLAN_SELECTOR, WEB_PLAN_SELECTOR,
    )

    connected = {item["source_ref"] for item in mission["source_plan"]
                 if item["status"] == "connected"}
    companies = {item["company_ref"]: item["ticker"] for item in mission["universe"]}
    created_at = "1970-01-01T00:00:00.000000+00:00"
    directory = workspace.state_dir / "discovery-plans"
    plans: list[tuple[str, str, str, dict[str, Any]]] = []
    paid_calls = int(mission["budget"]["max_daily_paid_calls"])
    if SEC_SOURCE_REF in connected:
        resolver = sec_ticker_resolver or (
            lambda ticker: resolve_sec_ticker(ticker, state_dir=workspace.state_dir)
        )
        resolved: dict[str, dict[str, str]] = {}
        pending: dict[str, str] = {}
        for company_ref, ticker in companies.items():
            try:
                issuer = dict(resolver(ticker))
                cik = str(issuer["cik"]).zfill(10)
            except (KeyError, TypeError, ValueError, WorkspaceMissionSetupError) as exc:
                pending[company_ref] = str(exc)
                continue
            if re.fullmatch(r"[0-9]{10}", cik):
                resolved[company_ref] = {"cik": cik}
        if resolved:
            sec_base = {
                "schema_version": "0.4",
                "id": f"discovery-plan:{workspace.slug}:sec-filings:1",
                "created_at": created_at, "mission_ref": mission["mission_ref"],
                "source_ref": SEC_SOURCE_REF, "companies": resolved,
                "specs": [
                    {"spec_ref": "annual-report-10k", "form": "10-K",
                     "lookback_days": 500, "rediscovery_interval_days": 30,
                     "retry_interval_days": 2},
                    {"spec_ref": "current-report-8k", "form": "8-K",
                     "lookback_days": 90, "rediscovery_interval_days": 2,
                     "retry_interval_days": 1},
                ],
                "budget": {"max_calls_24h": max(1, min(paid_calls, 50))},
            }
            sec_plan = validate_discovery_plan(
                {**sec_base, "content_hash": content_hash(sec_base)})
            plans.append((SEC_SOURCE_REF, SEC_PLAN_SELECTOR,
                          "sec-discovery-plan-selection-0.1", sec_plan))
        status_body = {
            "schema_version": "sec-company-resolution-status-0.1",
            "mission_ref": mission["mission_ref"],
            "resolved_company_refs": sorted(resolved),
            "pending": pending,
            "retry_after_seconds": 300,
        }
        _atomic_json(workspace.state_dir / "sec-company-resolution-status.json",
                     {**status_body, "content_hash": content_hash(status_body)})
    alpha_calls = int(mission["budget"]["max_alphaengine_calls_24h"])
    if ALPHAENGINE_SOURCE_REF in connected and alpha_calls > 0:
        alpha_companies = {ref: {"name": ticker, "ticker": ticker, "aliases": []}
                           for ref, ticker in companies.items()}
        plan = build_discovery_plan(
            plan_id=f"discovery-plan:{workspace.slug}:alphaengine:1",
            created_at=created_at, mission_ref=mission["mission_ref"],
            companies=alpha_companies, source_ref=ALPHAENGINE_SOURCE_REF,
            max_calls_24h=alpha_calls,
            specs=[
                {"spec_ref": "earnings-call-transcripts", "document_type": "meeting_minutes",
                 "query_variants": [{"query_template": "{name} {quarter} earnings call transcript", "filters": {}}],
                 "lookback_days": 400, "rediscovery_interval_days": 7, "retry_interval_days": 1},
                {"spec_ref": "sell-side-reports", "document_type": "sell_side_report",
                 "query_variants": [{"query_template": "{ticker} analyst research report", "filters": {}}],
                 "lookback_days": 180, "rediscovery_interval_days": 14, "retry_interval_days": 1},
            ])
        plans.append((ALPHAENGINE_SOURCE_REF, ALPHAENGINE_PLAN_SELECTOR,
                      "alphaengine-discovery-plan-selection-0.1", plan))
    if WEB_SEARCH_SOURCE_REF in connected and paid_calls > 0:
        terms = {ref: ticker for ref, ticker in companies.items()}
        plan = build_discovery_plan(
            plan_id=f"discovery-plan:{workspace.slug}:web-search:1",
            created_at=created_at, mission_ref=mission["mission_ref"], companies=terms,
            source_ref=WEB_SEARCH_SOURCE_REF,
            max_calls_24h=paid_calls,
            specs=[
                {"spec_ref": "industry-demand", "query_template": "{terms} industry demand outlook",
                 "lookback_days": 90, "rediscovery_interval_days": 7, "retry_interval_days": 1},
                {"spec_ref": "competitive-landscape", "query_template": "{terms} competitors market share",
                 "lookback_days": 180, "rediscovery_interval_days": 14, "retry_interval_days": 1},
                {"spec_ref": "management-changes", "query_template": "{terms} CEO CFO leadership change",
                 "lookback_days": 90, "rediscovery_interval_days": 7, "retry_interval_days": 1},
            ])
        plans.append((WEB_SEARCH_SOURCE_REF, WEB_PLAN_SELECTOR,
                      "web-discovery-plan-selection-0.1", plan))
    written: dict[str, str] = {}
    for source_ref, selector_name, selector_schema, plan in plans:
        filename = source_ref.removeprefix("source:") + "-first-mission-v1.json"
        _atomic_json(directory / filename, plan)
        selector_body = {
            "schema_version": selector_schema,
            "id": f"discovery-plan-selection:{workspace.slug}:{source_ref.split(':')[-1]}:1",
            "status": "approved", "source_ref": source_ref,
            "plan_ref": plan["id"], "plan_hash": plan["content_hash"],
            "plan_path": filename,
        }
        _atomic_json(directory / selector_name,
                     {**selector_body, "content_hash": content_hash(selector_body)})
        written[source_ref] = str(directory / filename)
    return written


def resolve_sec_ticker(
    ticker: str, *, state_dir: str | Path, timeout_seconds: float = 15.0,
) -> dict[str, str]:
    """Resolve through the SEC client in a bounded, workspace-local process."""
    ticker = _text(ticker, "ticker").upper()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "dalton_core.sec_company_resolver_cli",
             "--ticker", ticker, "--state-dir", str(Path(state_dir).resolve())],
            check=False, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceMissionSetupError(
            f"SEC ticker resolution timed out for {ticker} after {timeout_seconds:g}s"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "resolver failed"
        raise WorkspaceMissionSetupError(f"SEC could not resolve ticker {ticker}: {detail}")
    try:
        payload = json.loads(result.stdout)
        cik = str(payload["cik"])
        name = _text(payload["name"], "SEC company name")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise WorkspaceMissionSetupError(f"SEC could not resolve ticker {ticker}") from exc
    if not cik.isdigit():
        raise WorkspaceMissionSetupError(f"SEC returned an invalid CIK for {ticker}")
    return {"ticker": ticker, "cik": cik.zfill(10), "name": name}
