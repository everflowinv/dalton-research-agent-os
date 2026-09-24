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
    financial_model: Mapping[str, Any] | None = None,
    metrics: Sequence[Mapping[str, Any]] = (),
    contested: Sequence[Mapping[str, Any]] = (),
    acquisition: Mapping[str, Any] | None = None,
    dossier_feedback: Mapping[str, Any] | None = None,
    readable_documents: Sequence[Mapping[str, Any]] = (),
    unavailable_documents: Sequence[Mapping[str, Any]] = (),
    document_research_feedback: Sequence[Mapping[str, Any]] = (),
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
        "financial_model": dict(financial_model or {"status": "unavailable", "reason": "not_projected"}),
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
        # P13z: whether the filings this company is owed have actually been
        # arriving. A deficit says what is missing; it does not say that 25
        # runs were dispatched to fetch it and every one of them failed. A
        # planner that cannot tell those apart orders the same acquisition
        # again, which is exactly what it did, for a day.
        "filing_runs": {
            "succeeded": _int((acquisition or {}).get("succeeded")),
            "unsuccessful": _int((acquisition or {}).get("unsuccessful")),
            "last_failure": (acquisition or {}).get("last_failure_detail"),
        },
        # Attention feedback, not Evidence: the exact latest successfully
        # completed Dossier child and the bounded missing-evidence findings it
        # could not turn into supported prose.
        "dossier_feedback": None if dossier_feedback is None else {
            "feedback_ref": dossier_feedback.get("id"),
            "feedback_hash": dossier_feedback.get("content_hash"),
            "dossier_status": dossier_feedback.get("dossier_status"),
            "source_ticket_ref": dossier_feedback.get("source_ticket_ref"),
            "repair_targets": list(dossier_feedback.get("repair_targets") or ()),
        },
        "readable_documents": [dict(document) for document in readable_documents],
        "unavailable_documents": [dict(document) for document in unavailable_documents],
        "document_research_feedback": [dict(item) for item in document_research_feedback],
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
    financial_models_by_company: Mapping[str, Mapping[str, Any]] | None = None,
    metrics_by_company: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    contested_by_company: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    acquisition_by_company: Mapping[str, Mapping[str, Any]] | None = None,
    dossier_feedback_by_company: Mapping[str, Mapping[str, Any]] | None = None,
    readable_documents_by_company: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    unavailable_documents_by_company: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    document_research_policy: Mapping[str, Any] | None = None,
    document_research_availability: Mapping[str, Any] | None = None,
    document_research_feedback: Mapping[str, Any] | None = None,
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
    financial_models_by_company = financial_models_by_company or {}
    metrics_by_company = metrics_by_company or {}
    contested_by_company = contested_by_company or {}
    acquisition_by_company = acquisition_by_company or {}
    dossier_feedback_by_company = dossier_feedback_by_company or {}
    readable_documents_by_company = readable_documents_by_company or {}
    unavailable_documents_by_company = unavailable_documents_by_company or {}
    document_research_feedback = document_research_feedback or {}
    companies = [
        company_state(
            entry,
            figures=figures_by_company.get(entry.get("company_ref")),
            financial_model=financial_models_by_company.get(entry.get("company_ref")),
            metrics=metrics_by_company.get(entry.get("company_ref"), ()),
            contested=contested_by_company.get(entry.get("company_ref"), ()),
            acquisition=acquisition_by_company.get(entry.get("company_ref")),
            dossier_feedback=dossier_feedback_by_company.get(entry.get("company_ref")),
            readable_documents=readable_documents_by_company.get(entry.get("company_ref"), ()),
            unavailable_documents=unavailable_documents_by_company.get(entry.get("company_ref"), ()),
            document_research_feedback=document_research_feedback.get("by_company", {}).get(entry.get("company_ref"), ()),
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
        "document_research_contract_ref": "directed-document:0.1",
        "document_research_policy": None if document_research_policy is None else dict(document_research_policy),
        "document_research_availability": dict(document_research_availability or {}),
        "document_research_feedback_status": {
            key: document_research_feedback[key] for key in ("status", "reason", "content_hash")
            if key in document_research_feedback
        },
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
    state["content_hash"] = state_content_hash(state)
    return state


# How close to a cap counts as "near" it.  A plan should be told when a source
# or the day's model budget is about to run out; it does not need to know the
# third decimal of what has been spent.
NEAR_CAP_RATIO = 0.8


def _band(spent: Any, cap: Any) -> str:
    try:
        spent_value, cap_value = float(spent or 0), float(cap or 0)
    except (TypeError, ValueError):
        return "unknown"
    if cap_value <= 0:
        return "uncapped"
    ratio = spent_value / cap_value
    if ratio >= 1:
        return "exhausted"
    if ratio >= NEAR_CAP_RATIO:
        return "near_cap"
    return "normal"


def budget_bands(spend: Mapping[str, Any] | None) -> dict[str, str]:
    """What the spend means for a plan: normal, near its cap, or exhausted.

    The raw spend moves every time any lane settles a call -- live
    2026-09-24 the model-today block changed between every pair of adjacent
    planner prompts (130 -> 139 calls, $4.91 -> $5.35, ...), and since it was
    hashed, every tick was a "new" state and every tick paid Opus again.  A
    band changes only when it matters to what the plan should ask for.  Only
    capped spends are banded; an uncapped counter (web searches) says nothing
    a plan could act on and is left out.
    """

    spend = spend or {}
    bands: dict[str, str] = {}
    model_today = spend.get("model_today")
    if isinstance(model_today, Mapping):
        cost = _band(model_today.get("cost_usd"), model_today.get("cost_cap_usd"))
        calls = _band(model_today.get("calls"), model_today.get("call_cap"))
        order = ("unknown", "uncapped", "normal", "near_cap", "exhausted")
        bands["model_today"] = max(cost, calls, key=order.index)
    alphaengine = spend.get("alphaengine_24h")
    if isinstance(alphaengine, Mapping):
        bands["alphaengine_24h"] = _band(alphaengine.get("spent"), alphaengine.get("cap"))
    return bands


def state_content_hash(state: Mapping[str, Any]) -> str:
    """The exact state a plan binds to, minus when it was read and the raw spend.

    ``spend`` is replaced by its :func:`budget_bands`: still in the hash, so a
    budget running out is a new state, but no longer a new state every time
    some lane settles a cent.  ``content_hash`` and ``prompt_projection`` are
    derived from the state and never part of it.
    """

    body = {
        key: value for key, value in state.items()
        if key not in {"as_of", "spend", "content_hash", "prompt_projection"}
    }
    body["spend_bands"] = budget_bands(state.get("spend"))
    return content_hash(body)


_ITEM_MATERIAL = ("item_ref", "source_ref", "status", "required", "deficit")


def _material_subject(subject: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(subject, Mapping):
        return None
    dossier = subject.get("dossier_feedback")
    financial = subject.get("financial_model")
    return {
        "subject": subject.get("company_ref") or subject.get("industry_ref"),
        "ticker": subject.get("ticker"),
        "priority": subject.get("priority"),
        "stage": subject.get("stage"),
        "stage_status": subject.get("stage_status"),
        "source_base_ready": bool(subject.get("source_base_ready")),
        "gaps": sorted(str(item) for item in subject.get("gaps") or ()),
        "blocked_on": sorted(
            content_hash(item) for item in subject.get("blocked_on") or ()
        ),
        "items": sorted(
            ({key: item.get(key) for key in _ITEM_MATERIAL}
             for item in subject.get("items") or ()),
            key=lambda item: (str(item["item_ref"]), str(item["source_ref"])),
        ),
        "financial_model": None if not isinstance(financial, Mapping) else {
            "status": financial.get("status"), "reason": financial.get("reason"),
            "model_version_ref": financial.get("model_version_ref"),
        },
        "metrics_contested": sorted(
            str(item.get("metric_ref")) for item in subject.get("metrics_contested") or ()
        ),
        "dossier_feedback": None if not isinstance(dossier, Mapping) else {
            "dossier_status": dossier.get("dossier_status"),
            "repair_targets": content_hash(list(dossier.get("repair_targets") or ())),
        },
    }


def planning_hash(state: Mapping[str, Any]) -> str:
    """Whether the state moved in a way that should change the plan.

    Narrower than :func:`state_content_hash` on purpose.  Live on 2026-09-24
    adjacent planner prompts on both workspaces differed only in fields that
    move every tick without changing what is worth doing: the spend, how many
    documents are readable or unavailable (45 -> 46), how many are queued or
    read or failed in a pipeline that is already running (queued 0 -> 1 -> 0),
    how many documents cite a measure -- and so which five measures are the
    "most cited" (legacy 12:51: EPS and adjusted operating margin swapped
    places on one citation) -- and the hashes over omitted rows.  What is
    kept is what a plan is *about*: the goal and sources, each subject's gaps,
    stage, blockers and item status/deficits, the contested measures, the
    financial model's status, the dossier's status and repair targets,
    document-research policy and availability, and the budget bands.  A change here is material and asks
    the planner again at once; a change only outside it waits for the
    planner's minimum re-plan interval.

    Works on the full state and on its prompt projection alike, so a recorded
    prompt can be checked after the fact.
    """

    totals = dict(state.get("totals") or {})
    totals.pop("figures_held", None)
    availability = dict(state.get("document_research_availability") or {})
    policy = state.get("document_research_policy")
    feedback_status = dict(state.get("document_research_feedback_status") or {})
    return content_hash({
        "schema_version": state.get("schema_version"),
        "document_research_contract_ref": state.get("document_research_contract_ref"),
        "document_research_policy": (
            None if not isinstance(policy, Mapping)
            else policy.get("content_hash") or content_hash(dict(policy))
        ),
        "document_research_availability": {
            key: availability.get(key)
            for key in ("status", "reason", "config_hash", "unavailable_sources")
        },
        "document_research_feedback_status": feedback_status.get("status"),
        "goal": state.get("goal"),
        "sources": state.get("sources"),
        "industry": _material_subject(state.get("industry")),
        "companies": sorted(
            (_material_subject(company) for company in state.get("companies") or ()),
            key=lambda company: str(company["subject"]),
        ),
        "totals": totals,
        "budget": state.get("budget"),
        "spend_bands": budget_bands(state.get("spend")),
    })


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
    "NEAR_CAP_RATIO",
    "SCHEMA_VERSION",
    "budget_bands",
    "build_research_state",
    "company_state",
    "industry_state",
    "planning_hash",
    "state_content_hash",
    "state_digest",
]
