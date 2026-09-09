"""P13a: one readable picture of where the research has got to.

Nothing in this system decides what to work on.  ``mission_stage`` computes a
checklist, and then each lane runs on a fixed cadence -- rediscover every seven
days, every fourteen days -- regardless of whether the thing that cadence feeds
is already satisfied.  That is why the loop kept fetching news pages for
subtasks that were finished while the one genuine gap sat blocked: the calendar
was deciding, not judgement.

A planner can decide instead, but only if it can see.  Today "what is done,
what is missing, what is blocked, what has it cost" is spread across the
checklist, the discovery ledger, the figure journal, the metric observations
and the budget.  This module assembles that into one compact, bounded object.

Two properties matter more than completeness:

* it is **small**.  A planner reads this every time it runs, and a state that
  grows with the document count would push out the part that matters.  Counts,
  not lists; the newest few examples, not everything.
* it is **honest about cost**.  A plan that cannot see what a source has
  already spent will happily ask for more of the most expensive thing.

Nothing here decides anything.  It is the input a decision is made from, and
keeping it a pure projection is what lets the decision be argued with.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .store import content_hash

SCHEMA_VERSION = "0.1"
# How many recent examples travel with each list. Enough to see a pattern,
# far too few to be a substitute for the counts.
MAX_EXAMPLES = 5


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def company_state(
    entry: Mapping[str, Any],
    *,
    figures: Mapping[str, Any] | None = None,
    metrics: Sequence[Mapping[str, Any]] = (),
    contested: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One company's position, as a planner needs to see it.

    The checklist entry says what is required and held.  The additions are what
    the checklist cannot know: which figures this company actually has, and
    which measures the market has been seen judging it on but nobody has
    collected yet -- the two things that decide whether more reading is worth
    anything.
    """

    items = []
    for item in entry.get("items", ()):
        required, have = _int(item.get("required")), _int(item.get("have"))
        items.append({
            "item_ref": item.get("item_ref"),
            "required": required,
            "have": have,
            "deficit": max(0, required - have),
            "read": _int(item.get("read")),
            "queued": _int(item.get("pending")),
            "failed": _int(item.get("failed")),
            "status": item.get("status"),
            "source_ref": item.get("source_ref"),
            # Why it cannot be worked on, when that is the answer. A planner
            # that cannot tell "not done" from "cannot be done" will keep
            # ordering work against a source nobody has connected.
            "note": item.get("note"),
        })
    held = dict(figures or {})
    return {
        "company_ref": entry.get("company_ref"),
        "ticker": entry.get("ticker"),
        "priority": entry.get("priority"),
        "stage": entry.get("stage"),
        "stage_status": entry.get("stage_status"),
        "items": items,
        "gaps": list(entry.get("gaps") or ()),
        "blocked_on": list(entry.get("blocked_on") or ()),
        "source_base_ready": bool(entry.get("source_base_ready")),
        "figures": {
            "total": _int(held.get("total")),
            "by_grade": dict(held.get("by_grade") or {}),
        },
        # Measures the market was seen citing for this company, most-cited
        # first. A requirement needs two documents; the uncorroborated ones are
        # shown because they are the strongest hint about what to go looking
        # for next. These are *corroboration counts*, not raw observations --
        # passing the observations put "documents: 0" beside every measure and
        # showed whichever ones happened to be recorded first.
        "metrics_watched": [
            {"metric_ref": m.get("metric_ref"), "label": m.get("label"),
             "documents": _int(m.get("citation_count"))}
            for m in sorted(metrics, key=lambda m: -_int(m.get("citation_count")))[:MAX_EXAMPLES]
        ],
        # P13y: measures several documents named and could not agree how to
        # measure. Left out of the requirements, so without this they read as
        # "nobody mentioned it" -- the opposite of what happened.
        "metrics_contested": [
            {"metric_ref": m.get("metric_ref"), "label": m.get("label"),
             "units": list(m.get("units") or ())}
            for m in list(contested or ())[:MAX_EXAMPLES]
        ],
    }


def industry_state(
    entry: Mapping[str, Any] | None,
    *,
    figures: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The industry's own position, which belongs to no company.

    An industry screen rests on facts about the market -- how demand is moving,
    how the field is arranged -- and those are nobody's company file. Without
    this the planner cannot see industry work at all, and the industry queries
    run per company on a calendar: live, 91 demand documents and 132 landscape
    documents against a requirement of three each, with 83 more queued.
    """

    if entry is None:
        return None
    held = dict(figures or {})
    return {
        "industry_ref": entry.get("industry_ref"),
        "items": [
            {"item_ref": item.get("item_ref"),
             "required": _int(item.get("required")),
             "have": _int(item.get("have")),
             "deficit": max(0, _int(item.get("required")) - _int(item.get("have"))),
             "read": _int(item.get("read")),
             "queued": _int(item.get("pending")),
             "status": item.get("status"),
             "source_ref": item.get("source_ref"),
             "note": item.get("note")}
            for item in entry.get("items", ())
        ],
        "gaps": list(entry.get("gaps") or ()),
        "blocked_on": list(entry.get("blocked_on") or ()),
        "source_base_ready": bool(entry.get("source_base_ready")),
        "figures": {"total": _int(held.get("total")),
                    "by_grade": dict(held.get("by_grade") or {})},
    }


def build_research_state(
    *,
    mission: Mapping[str, Any],
    checklist: Sequence[Mapping[str, Any]],
    industry: Mapping[str, Any] | None = None,
    figures_by_company: Mapping[str, Any] | None = None,
    metrics_by_company: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    contested_by_company: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    budget: Mapping[str, Any] | None = None,
    spend: Mapping[str, Any] | None = None,
    as_of: str,
) -> dict[str, Any]:
    """The whole picture, small enough to read every time.

    ``budget`` is what the mission is allowed; ``spend`` is what has actually
    gone, per lane. Both, because a plan made without them asks for the most
    expensive thing available and then finds it refused.
    """

    figures_by_company = figures_by_company or {}
    metrics_by_company = metrics_by_company or {}
    contested_by_company = contested_by_company or {}
    companies = [
        company_state(
            entry,
            figures=figures_by_company.get(entry.get("company_ref")),
            metrics=metrics_by_company.get(entry.get("company_ref"), ()),
            contested=contested_by_company.get(entry.get("company_ref"), ()),
        )
        for entry in checklist
    ]
    industry_block = industry_state(
        industry, figures=(figures_by_company or {}).get(
            (industry or {}).get("industry_ref")))
    open_gaps = sum(len(c["gaps"]) for c in companies)
    if industry_block:
        open_gaps += len(industry_block["gaps"])
    state = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "goal": {
            "mission_ref": mission.get("mission_ref"),
            "mission_version_ref": mission.get("id"),
            "objective": mission.get("objective"),
            "research_questions": list(mission.get("research_questions") or ()),
            "deliverables": list(mission.get("deliverables") or ()),
        },
        "sources": [
            {"source_ref": s.get("source_ref"), "role": s.get("role"),
             "connected": s.get("status") == "connected"}
            for s in mission.get("source_plan") or ()
        ],
        # The industry is a subject in its own right, not a company with no
        # ticker. A directive may name it, and a figure may belong to it.
        "industry": industry_block,
        "companies": companies,
        "totals": {
            "companies": len(companies),
            "open_gaps": open_gaps,
            "ready_for_next_stage": sum(1 for c in companies if c["source_base_ready"]),
            "figures_held": sum(c["figures"]["total"] for c in companies),
        },
        "budget": dict(budget or {}),
        "spend": dict(spend or {}),
    }
    # The hash is what a plan binds to, so a plan can be told apart from the
    # state it was made against once that state has moved on.
    #
    # ``as_of`` is excluded on purpose. It is when the state was *read*, not
    # anything about the world, and hashing it made every read a different
    # state -- which would have paid an expensive model for a fresh plan on
    # every tick while nothing had changed. A test caught it; live it would
    # have looked like the planner simply being costly.
    state["content_hash"] = content_hash({k: v for k, v in state.items() if k != "as_of"})
    return state


def state_digest(state: Mapping[str, Any]) -> str:
    """One line per company, for a log or a heartbeat.

    Not for the planner -- it reads the object -- but for a person deciding
    whether the planner is looking at anything sensible.
    """

    lines = []
    industry = state.get("industry")
    if industry:
        lines.append(
            f"{industry.get('industry_ref')}: gaps="
            f"{','.join(industry['gaps']) or 'none'}"
        )
    for company in state.get("companies", ()):
        gaps = ",".join(company["gaps"]) or "none"
        lines.append(
            f"{company.get('ticker') or company.get('company_ref')}: "
            f"stage={company.get('stage') or '-'} gaps={gaps} "
            f"figures={company['figures']['total']}"
        )
    return "\n".join(lines)


__all__ = [
    "MAX_EXAMPLES",
    "SCHEMA_VERSION",
    "build_research_state",
    "company_state",
    "industry_state",
    "state_digest",
]
