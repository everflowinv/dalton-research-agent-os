"""P13b: decide what to work on next, and say why.

The lanes run on a calendar.  ``rediscovery_interval_days`` is 7 for industry
demand, 14 for competitive landscape, and nothing anywhere asks whether the
subtask those searches feed is already satisfied.  So the system re-searched
finished work for weeks while the one genuine gap -- quarterly financials --
sat blocked behind a stuck dispatch, and the owner watched it fetch news pages
and asked, reasonably, why.

Cadence is a ceiling on how often something *may* be redone.  It is not a
reason to do it.  The reason has to come from the state: what the goal is, what
is held, what is missing, what is blocked, what it has cost.

This module is the deciding half.  It reads one ``research_state`` object and
returns a ranked list of directives, each naming a company, an item, an action
and a reason.  Three rules keep it honest, and they are the same rules every
other model call here obeys:

* it may only name companies and items that are *in the state it was given*.
  A plan that invents a company is refused whole, not partially applied.
* it may only ask for actions the dispatcher can actually take.  A directive
  nobody can execute is a plan that looks like work and is not.
* every directive carries a reason in the owner's own terms.  A plan that
  cannot be argued with is indistinguishable from the calendar it replaces.

The model does the judging; the ranking it returns is the product.  What this
module does *not* do is execute anything: a plan is a proposal, and the
dispatcher decides separately whether it is within the bounds the owner set.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
TASK_REF = "task:research-plan-directives:0.1"
MAX_DIRECTIVES = 12

# What the dispatcher can actually do. A plan may only ask for these, because
# a directive nobody can execute is a plan that looks like work and is not.
ACTIONS: tuple[str, ...] = (
    # Go and find documents of this kind for this company.
    "search",
    # Fetch documents already discovered but not yet held.
    "acquire",
    # Read what is held for statements.
    "read",
    # Read what is held for the figures this company owes.
    "extract_figures",
    # Stop working this item: it is satisfied, or it cannot progress and
    # spending on it is waste.
    "stop",
)

OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ResearchPlanDirectivesV0.1",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "assessment", "directives"],
    "properties": {
        "schema_version": {"const": "0.1"},
        "assessment": {
            "type": "string", "minLength": 1, "maxLength": 1200,
            "description": "Where the research stands against the goal, in the owner's terms.",
        },
        "directives": {
            "type": "array",
            "maxItems": MAX_DIRECTIVES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["company_ref", "item_ref", "action", "reason"],
                "properties": {
                    "company_ref": {"type": "string", "minLength": 1, "maxLength": 120},
                    "item_ref": {"type": "string", "minLength": 1, "maxLength": 80},
                    "action": {"enum": list(ACTIONS)},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                },
            },
        },
    },
}
TASK_HASH = content_hash({
    "task": TASK_REF,
    "output": OUTPUT_SCHEMA,
    "authority": "proposes_work_only_within_the_state_it_was_given",
})


class ResearchPlanError(ValueError):
    """The plan is malformed, or names work that does not exist."""


def build_prompt(state: Mapping[str, Any]) -> str:
    return (
        "You decide what a research system works on next.\n\n"
        "RESEARCH_STATE below is the whole picture: the standing goal, every company "
        "under coverage, each company's checklist items with how many readings are "
        "required and how many are held, which items are blocked and why, what figures "
        "have been collected, which measures the market has been seen judging each "
        "company on, and what the work has cost so far against its budget.\n\n"
        "Return a ranked list of directives: the most valuable work first. For each, "
        "name the company, the checklist item, one action, and why -- in plain terms a "
        "person running this fund would accept.\n\n"
        "What matters:\n"
        "- Close gaps. An item whose deficit is 0 does not need more work; say `stop` "
        "for it rather than ordering more, and say so only when something is still "
        "being spent on it.\n"
        "- An item that is blocked cannot be unblocked by ordering more of it. If the "
        "source is not connected or the note says it cannot progress, either `stop` it "
        "or say what would actually unblock it in the reason.\n"
        "- Prefer work that makes a deliverable possible over work that adds more of "
        "something already sufficient.\n"
        "- Spend is real. If a source is near its cap, say what the remaining calls "
        "should be spent on rather than ordering everything.\n"
        "- Figures a company owes matter more than another news article about it.\n\n"
        "You may only name a company_ref and item_ref that appear in RESEARCH_STATE. "
        "Inventing either voids the whole plan. Return fewer directives rather than "
        "padding: a short plan that is right beats a long one that is thorough.\n"
        "Return raw strict JSON matching OUTPUT_SCHEMA, no markdown fence and no prose "
        "outside it.\n"
        f"OUTPUT_SCHEMA={canonical_json(OUTPUT_SCHEMA)}\n"
        f"RESEARCH_STATE={canonical_json(state)}"
    )


def build_work(state: Mapping[str, Any], *, created_at: str, state_ref: str) -> Any:
    """One routed model call asking what to do next.

    Budgeted generously compared with an extraction window, and that is the
    point of separating it: this runs a few times a day over a small object,
    while extraction runs thousands of times over large ones. The expensive
    model belongs where the judgement is, not where the volume is.
    """

    from .contracts import WorkOrder

    digest = content_hash({"task": TASK_HASH, "state": state["content_hash"]})
    return WorkOrder(
        schema_version="0.1",
        id="work:research-plan-" + digest[:32],
        created_at=created_at,
        updated_at=created_at,
        question=build_prompt(state),
        requested_capabilities=("research",),
        runtime_profile_ref="runtime-profile:dalton-model-broker:0.1",
        budget={
            "max_input_tokens": 120000, "max_output_tokens": 4000,
            "max_total_tokens": 124000, "max_cost_usd": 1.50, "max_seconds": 300,
        },
        idempotency_key="research-plan:" + digest,
        declared_side_effects=(),
        status="ready",
        input_refs=(state_ref,),
        metadata={
            "control_plane": "research-planner",
            "task_ref": TASK_REF, "task_hash": TASK_HASH,
            "state_hash": state["content_hash"],
            "candidate_only": True,
        },
    )


def parse_response(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ResearchPlanError("model response must be text")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResearchPlanError("model response is not JSON") from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version", "assessment", "directives",
    }:
        raise ResearchPlanError("plan has an invalid closed shape")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ResearchPlanError("unsupported plan schema_version")
    if not isinstance(payload["assessment"], str) or not payload["assessment"].strip():
        raise ResearchPlanError("plan must say where the research stands")
    directives = payload["directives"]
    if not isinstance(directives, list):
        raise ResearchPlanError("directives must be a list")
    if len(directives) > MAX_DIRECTIVES:
        raise ResearchPlanError(f"a plan may hold at most {MAX_DIRECTIVES} directives")
    return {"assessment": payload["assessment"].strip(), "directives": list(directives)}


def _known_work(state: Mapping[str, Any]) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {}
    for company in state.get("companies", ()):
        ref = company.get("company_ref")
        if isinstance(ref, str):
            known[ref] = {
                item.get("item_ref") for item in company.get("items", ())
                if isinstance(item.get("item_ref"), str)
            }
    return known


def plan_from_response(
    state: Mapping[str, Any], response_text: Any, *, created_at: str
) -> dict[str, Any]:
    """A verified plan, or a refusal naming what the model invented.

    Refused whole rather than filtered: a plan is a ranking, and silently
    dropping the directives that do not exist leaves a ranking that no longer
    means what the model meant. A model that invented work should be asked
    again, not partially obeyed.
    """

    parsed = parse_response(response_text)
    known = _known_work(state)
    for index, directive in enumerate(parsed["directives"]):
        if not isinstance(directive, Mapping) or set(directive) != {
            "company_ref", "item_ref", "action", "reason",
        }:
            raise ResearchPlanError(f"directive {index} has an invalid closed shape")
        company_ref, item_ref = directive["company_ref"], directive["item_ref"]
        if company_ref not in known:
            raise ResearchPlanError(
                f"plan names {company_ref!r}, which is not a company under coverage"
            )
        if item_ref not in known[company_ref]:
            raise ResearchPlanError(
                f"plan names item {item_ref!r} for {company_ref!r}, which has no such item"
            )
        if directive["action"] not in ACTIONS:
            raise ResearchPlanError(f"directive {index} asks for an action nobody can take")
        if not isinstance(directive["reason"], str) or not directive["reason"].strip():
            raise ResearchPlanError(f"directive {index} gives no reason")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "task_ref": TASK_REF,
        "created_at": created_at,
        # The state this was decided from, so a plan can be told apart from one
        # made when the world looked different.
        "state_hash": state["content_hash"],
        "mission_version_ref": (state.get("goal") or {}).get("mission_version_ref"),
        "assessment": parsed["assessment"],
        "directives": [
            {"rank": index, "company_ref": d["company_ref"], "item_ref": d["item_ref"],
             "action": d["action"], "reason": d["reason"].strip()}
            for index, d in enumerate(parsed["directives"])
        ],
    }
    plan["content_hash"] = content_hash(plan)
    return plan


def directives_for(plan: Mapping[str, Any], *, action: str) -> list[dict[str, Any]]:
    """The plan's directives asking for one kind of work, best first."""

    if action not in ACTIONS:
        raise ResearchPlanError(f"unknown action {action!r}")
    return [d for d in plan.get("directives", ()) if d.get("action") == action]


def wanted_specs(plan: Mapping[str, Any], *, item_specs: Mapping[str, Sequence[str]]) -> set[str]:
    """(company_ref, spec_ref) pairs the plan actually asks to search for.

    This is what turns a plan into a cadence: a spec nobody asked for is not
    searched, however long it has been.
    """

    wanted: set[str] = set()
    for directive in plan.get("directives", ()):
        if directive.get("action") not in ("search", "acquire"):
            continue
        for spec in item_specs.get(directive.get("item_ref"), ()):  # type: ignore[arg-type]
            wanted.add(f"{directive.get('company_ref')}|{spec}")
    return wanted


__all__ = [
    "ACTIONS",
    "MAX_DIRECTIVES",
    "OUTPUT_SCHEMA",
    "TASK_HASH",
    "TASK_REF",
    "ResearchPlanError",
    "build_prompt",
    "build_work",
    "directives_for",
    "parse_response",
    "plan_from_response",
    "wanted_specs",
]
