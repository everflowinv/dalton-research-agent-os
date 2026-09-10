"""P15a: one bounded look, when the answer says the shelf was empty.

``answer_after_refresh`` has been a route in :mod:`answer_routing` since S5 and
has run zero times live.  The reason is not that the machinery is missing --
the route, the budget reservation, the dispatch receipt and the outcome receipt
are all there -- but that it was wired to the ResearchQuestion backlog, which
the owner never uses, and the surface the owner does use had no way to reach
it.  This module is that way, and it is deliberately the smallest one that is
still honest.

**One search, then answer again, then stop.**  A refresh is one discovery call
against one source for one company, followed by a second model call that sees
the new documents' *headers* and nothing else.  Reading them is the extraction
lane's job and takes hours; pretending the headers are their contents would be
the panel inventing evidence at exactly the moment the owner was told it had
gone to look.  There is no second refresh, ever, for the same request.

**The cockpit does not get to invent a search string.**  ``run_mission_source_
discovery`` compiles its query from the owner's published discovery plan --
``{terms} earnings call transcript`` -- and the company's search terms.  So the
choice this module makes is *which of the specs the owner deployed to run for
which company*, and the spec it may choose from is one this mission has already
run, read out of ``coverage_mission_source_discoveries``.  The model's own
words for what it wants become ``query_intent``: recorded beside the answer,
never sent to a connector.  A search phrase composed by a model and billed to
the owner's AlphaEngine quota is the one thing a governed connector layer
exists to prevent.

**Two owner acts gate it.**  ``research_task`` in the mission's ``may_write``
(the word that lifted the ad-hoc ban) and an ``adhoc_research_route`` the
answer-sufficiency policy has actually budgeted.  Either absent and the answer
still says what it would have looked for -- the suggestion is the useful part
even when the door is shut -- but nothing is fetched and nothing is spent.

Nothing here writes a Claim.  The refreshed answer records
``refreshed_with: [document headers]`` and stays what ADR-0006 says an answer
is: a cockpit artefact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .ask_context import BLOCK_LABELS, BLOCK_TAGS
from .source_capability_map import CAPABILITIES

SCHEMA_VERSION = "0.1"

# Which capability-map slug can be reached by the mission's discovery lanes.
# Only two: AlphaEngine's ``search_library`` and the Gemini web search behind
# ``source:web-search``.  Everything else in the map is either a fetch (not a
# search), a lane with its own cadence, or not connected -- and a refresh that
# named one of those would be a promise the writer cannot keep.
REFRESH_SOURCE_REFS: Mapping[str, str] = {
    "alphaengine": "source:alphaengine",
    "gemini-web-search": "source:web-search",
}

# Why a refresh is not available.  Closed, because the page prints one sentence
# per reason and the answer records the list.
GRANT_REASONS: tuple[str, ...] = (
    "no_active_mission",
    "mission_does_not_grant_research_task",
    "policy_unavailable",
    "adhoc_route_disabled",
    "adhoc_budget_is_zero",
    "mission_budget_leaves_no_adhoc_pool",
    "source_not_searchable",
    "source_not_connected",
    "no_discovery_spec_used_yet",
    "company_not_resolved",
    "already_refreshed",
    "answer_suggested_no_refresh",
)

GRANT_LABELS: Mapping[str, str] = {
    "no_active_mission": "没有在跑的研究目标",
    "mission_does_not_grant_research_task": "研究目标还没授权临时补搜（may_write 里缺 research_task）",
    "policy_unavailable": "这个账本上没有回答充分性策略，补搜没有预算依据",
    "adhoc_route_disabled": "策略把临时补搜关着",
    "adhoc_budget_is_zero": "策略给临时补搜的预算是零",
    "mission_budget_leaves_no_adhoc_pool": "研究目标的日预算分给临时研究的额度是零",
    "source_not_searchable": "这个来源不是可以被检索的发现源",
    "source_not_connected": "研究目标没有接上这个来源",
    "no_discovery_spec_used_yet": "这个来源在这个目标下还没跑过任何检索规格，没有可复用的规格",
    "company_not_resolved": "这个问题没有点名一家覆盖内的公司",
    "already_refreshed": "这个问题已经补搜过一次了，不再补第二次",
    "answer_suggested_no_refresh": "答案没有说需要补搜",
}

MAX_HEADERS = 20
MAX_TITLE_CHARS = 240

# The block the second pass adds.  Registered in the context module's tables so
# that the renderer, the tag letters and the labels stay in one place.
REFRESH_BLOCK = "refreshed"


class AskRefreshError(RuntimeError):
    """A refresh was asked for in a state that does not allow one."""


# ---------------------------------------------------------------------------
# what this Core will allow
# ---------------------------------------------------------------------------

def known_specs(core: Any, mission_ref: str) -> list[dict[str, Any]]:
    """The discovery specs this mission has actually run, newest use first.

    Read rather than configured: a spec ref is a line in a plan file on the
    writer's disk, which the cockpit process cannot see, and a second table of
    them here would be a copy that goes stale the first time the owner edits
    the plan.  What the Ledger holds is what was run, which is also the only
    kind of spec it is safe to run again.

    Keyed on the mission rather than the mission *version*, and the join is the
    reason: a discovery row names the version it ran under, so reading by
    version would empty this list the moment the owner published a new one --
    which is exactly the moment they publish it for, since granting
    ``research_task`` is itself a new version.  A spec the owner deployed does
    not stop existing because the objective was reworded.
    """

    for table in ("coverage_mission_source_discoveries", "coverage_mission_versions"):
        if core.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone() is None:
            return []
    rows = core.execute(
        "SELECT d.source_ref AS source_ref, d.spec_ref AS spec_ref, "
        "d.company_ref AS company_ref, MAX(d.created_at) AS last_used, "
        "COUNT(*) AS runs FROM coverage_mission_source_discoveries d "
        "JOIN coverage_mission_versions v "
        "ON v.mission_version_id=d.mission_version_ref "
        "WHERE v.mission_ref=? GROUP BY d.source_ref, d.spec_ref, d.company_ref",
        (mission_ref,),
    ).fetchall()
    specs = [{
        "source_ref": row["source_ref"], "spec_ref": row["spec_ref"],
        "company_ref": row["company_ref"], "last_used": row["last_used"],
        "runs": int(row["runs"]),
    } for row in rows]
    specs.sort(key=lambda item: (item["last_used"], item["spec_ref"]), reverse=True)
    return specs


def grant(
    mission: Mapping[str, Any] | None,
    *,
    policy: Mapping[str, Any] | None,
    slug: str | None,
    specs: Sequence[Mapping[str, Any]] = (),
    company_ref: str | None = None,
    already_refreshed: bool = False,
) -> dict[str, Any]:
    """Whether one refresh may run right now, and every reason it may not.

    Every reason, not the first: an owner who has to fix three things learns
    all three at once rather than one per attempt.
    """

    from .research_task import GRANT_WORD, pool

    reasons: list[str] = []
    pool_wire = None
    if mission is None:
        reasons.append("no_active_mission")
    else:
        if GRANT_WORD not in ((mission.get("autonomy") or {}).get("may_write") or ()):
            reasons.append("mission_does_not_grant_research_task")
        try:
            pool_wire = pool(mission)
        except Exception:  # noqa: BLE001 - a malformed budget is "no pool", said plainly
            pool_wire = None
        if pool_wire is None or pool_wire["cap_micros"] <= 0:
            reasons.append("mission_budget_leaves_no_adhoc_pool")
    route = None
    if policy is None:
        reasons.append("policy_unavailable")
    else:
        route = policy.get("adhoc_research_route") or {}
        if not route.get("enabled"):
            reasons.append("adhoc_route_disabled")
        elif int(route.get("max_cost_units") or 0) <= 0 or int(route.get("max_rounds") or 0) <= 0:
            reasons.append("adhoc_budget_is_zero")
    source_ref = REFRESH_SOURCE_REFS.get(str(slug or ""))
    if source_ref is None:
        reasons.append("source_not_searchable")
    elif mission is not None:
        entry = next((item for item in mission.get("source_plan") or ()
                      if item.get("source_ref") == source_ref), None)
        if entry is None or entry.get("status") == "not_connected":
            reasons.append("source_not_connected")
    if company_ref is None:
        reasons.append("company_not_resolved")
    spec = None
    if source_ref is not None:
        candidates = [item for item in specs if item.get("source_ref") == source_ref]
        preferred = [item for item in candidates if item.get("company_ref") == company_ref]
        spec = (preferred or candidates or [None])[0]
        if spec is None:
            reasons.append("no_discovery_spec_used_yet")
    if already_refreshed:
        reasons.append("already_refreshed")
    unknown = [reason for reason in reasons if reason not in GRANT_REASONS]
    if unknown:  # pragma: no cover - guards the closed list against a typo
        raise AskRefreshError(f"{unknown[0]!r} is not a grant reason")
    return {
        "schema_version": SCHEMA_VERSION,
        "granted": not reasons,
        "reasons": reasons,
        "reason_labels": [GRANT_LABELS[reason] for reason in reasons],
        "source_ref": source_ref,
        "spec_ref": None if spec is None else spec["spec_ref"],
        "company_ref": company_ref,
        "budget": {
            "route_max_cost_units": None if route is None else route.get("max_cost_units"),
            "route_max_rounds": None if route is None else route.get("max_rounds"),
            "cost_units_this_refresh": 1,
            "adhoc_pool_cap_usd": None if pool_wire is None else pool_wire["cap_usd"],
        },
    }


def plan_refresh(
    answer: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    mission: Mapping[str, Any] | None,
    policy: Mapping[str, Any] | None,
    specs: Sequence[Mapping[str, Any]] = (),
    already_refreshed: bool = False,
) -> dict[str, Any]:
    """What one refresh would do, whether it may, and why not when it may not.

    Returned whether or not it is granted: "we would have gone to AlphaEngine
    for the last two quarters of sell-side reports, and we cannot because the
    mission has not granted it" is a more useful sentence than silence, and it
    is the sentence that gets the grant published.
    """

    suggestion = answer.get("refresh_suggested")
    if not isinstance(suggestion, Mapping):
        return {
            "schema_version": SCHEMA_VERSION, "available": False,
            "reasons": ["answer_suggested_no_refresh"],
            "reason_labels": [GRANT_LABELS["answer_suggested_no_refresh"]],
            "suggestion": None, "grant": None,
        }
    named = list((context.get("subjects") or {}).get("companies") or ())
    company_ref = named[0] if named else None
    decision = grant(
        mission, policy=policy, slug=suggestion.get("source"), specs=specs,
        company_ref=company_ref, already_refreshed=already_refreshed,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "available": decision["granted"],
        "reasons": decision["reasons"],
        "reason_labels": decision["reason_labels"],
        "suggestion": {
            "content_kind": suggestion.get("content_kind"),
            "source": suggestion.get("source"),
            # Recorded, never sent: the connector's query is compiled from the
            # owner's plan, not from a model's sentence.
            "query_intent": suggestion.get("query"),
            "source_note": (CAPABILITIES.get(str(suggestion.get("source")), {}) or {}).get("note"),
        },
        "grant": decision,
        "operation": None if not decision["granted"] else {
            "operation": "run_mission_source_discovery",
            "params": {
                "company_ref": decision["company_ref"],
                "source_ref": decision["source_ref"],
                "spec_ref": decision["spec_ref"],
            },
        },
    }


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------

def run_refresh(
    plan: Mapping[str, Any],
    *,
    search: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """One discovery call, and the headers it turned up.

    ``search`` is injected: the cockpit holds no Core write handle, so the real
    one goes through the writer as the owner's own principal exactly as every
    other cockpit write does, and a test hands in a fake.  It is called once.
    A plan that is not available raises rather than quietly doing nothing --
    "we refreshed" and "we did not" have to be different outcomes.
    """

    if not plan.get("available"):
        raise AskRefreshError(
            "补搜没有被允许：" + "、".join(plan.get("reason_labels") or ["原因未知"]))
    operation = plan["operation"]["params"]
    outcome = search(**operation)
    if not isinstance(outcome, Mapping):
        raise AskRefreshError("补搜没有返回可用的结果")
    headers = document_headers(
        outcome.get("documents") or (),
        titles=outcome.get("titles") or {},
        source_ref=operation["source_ref"],
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ran": True,
        "operation": dict(operation),
        "query": outcome.get("query"),
        "ticket_ref": outcome.get("ticket_ref"),
        "discovery_ref": outcome.get("discovery_ref"),
        "documents_found": len(outcome.get("documents") or ()),
        "headers": headers,
        # Said in every place the refresh appears, because it is the one thing
        # a reader will assume otherwise: nobody has read these yet.
        "note": "只拿到了这些文件的标题，正文还没有被读；抽取是流水线的活，不是这次问答的",
    }


def document_headers(
    documents: Sequence[Mapping[str, Any]],
    *,
    titles: Mapping[str, Mapping[str, Any]] | None = None,
    source_ref: str | None = None,
) -> list[dict[str, Any]]:
    """The new documents as headers: what exists, from where, when.  No bodies."""

    known = dict(titles or {})
    out: list[dict[str, Any]] = []
    for item in list(documents)[:MAX_HEADERS]:
        ref = str(item.get("document_ref") or "")
        if not ref:
            continue
        extra = known.get(ref) or {}
        out.append({
            "document_ref": ref,
            "source_ref": item.get("source_ref") or source_ref,
            "title": " ".join(str(extra.get("title") or item.get("title") or "").split())[:MAX_TITLE_CHARS],
            "host": item.get("host") or extra.get("host"),
            "url": extra.get("url"),
            "status": item.get("status"),
            "discovered_at": item.get("created_at"),
        })
    return out


def with_headers(
    context: Mapping[str, Any], headers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The same context plus one block of headers, tagged ``H1``…

    Appended rather than rebuilt, so the second answer is grounded in exactly
    the material the first one was plus the headers, and a reader comparing the
    two is looking at one change.
    """

    rows = []
    shown = [dict(row) for row in context.get("shown") or ()]
    for index, header in enumerate(headers):
        tag = f"{BLOCK_TAGS[REFRESH_BLOCK]}{index + 1}"
        text = header.get("title") or header["document_ref"]
        rows.append({
            "tag": tag, "ref": header["document_ref"], "text": text,
            "period": (header.get("discovered_at") or "")[:10] or None,
            "detail": {"host": header.get("host"), "source": header.get("source_ref")},
        })
        shown.append({
            "tag": tag, "block": REFRESH_BLOCK, "ref": header["document_ref"],
            "statement": text, "period": (header.get("discovered_at") or "")[:10] or None,
        })
    block = {
        "block": REFRESH_BLOCK, "label": BLOCK_LABELS[REFRESH_BLOCK],
        "tag_letter": BLOCK_TAGS[REFRESH_BLOCK], "available": bool(rows),
        "reason": None if rows else "no_record_for_this_company",
        "note": ("这些文件是刚刚补搜到的，只有标题；它们的正文还没有被读过，"
                 "所以只能说「存在这样一份材料」，不能引用其中的内容"),
        "rows": rows,
    }
    return {
        **dict(context),
        "blocks": [*(context.get("blocks") or ()), block],
        "shown": shown,
        "refreshed": True,
    }


__all__ = [
    "AskRefreshError",
    "GRANT_LABELS",
    "GRANT_REASONS",
    "MAX_HEADERS",
    "REFRESH_BLOCK",
    "REFRESH_SOURCE_REFS",
    "SCHEMA_VERSION",
    "document_headers",
    "grant",
    "known_specs",
    "plan_refresh",
    "run_refresh",
    "with_headers",
]
