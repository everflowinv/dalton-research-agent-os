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

import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .ask_context import BLOCK_LABELS, BLOCK_TAGS, context_identity
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
    "adhoc_pool_spent_today",
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
    "adhoc_pool_spent_today": "今天分给临时研究的额度已经被专项研究用光了",
    "source_not_searchable": "这个来源不是可以被检索的发现源",
    "source_not_connected": "研究目标没有接上这个来源",
    "no_discovery_spec_used_yet": "这个来源在这个目标下还没跑过任何检索规格，没有可复用的规格",
    "company_not_resolved": "这个问题没有点名一家覆盖内的公司",
    "already_refreshed": "这个问题已经补搜过一次了，不再补第二次",
    "answer_suggested_no_refresh": "答案没有说需要补搜",
}

MAX_HEADERS = 20
MAX_TITLE_CHARS = 240

# How a refresh ended.  Closed, because the page prints one sentence per word
# and the answer records it.  ``pending`` is the honest one: the discovery
# child is still running (or the writer has not settled its dispatch yet), and
# saying so beats showing whatever happened to be in the ledger already.
REFRESH_OUTCOMES: tuple[str, ...] = ("found", "empty", "pending", "failed")
OUTCOME_LABELS: Mapping[str, str] = {
    "found": "补搜到了新文件，但只有标题",
    "empty": "补搜跑完了，这个来源没有新的东西",
    "pending": "补搜还在跑，这次先按账本上已有的材料回答",
    "failed": "补搜没有跑成",
}

# How long the answer waits for the discovery child before saying ``pending``.
# A question is a request the owner is watching, so the wait is short and the
# fallback is a sentence rather than a spinner: the writer settles the dispatch
# on its next tick either way, and the documents will be in the next answer.
STATUS_POLLS = 20
STATUS_POLL_SECONDS = 0.5

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


class _ReadOnlyLoops:
    """Just enough of ``BoundedPlannerAuthority`` to count today's reservations.

    Its constructor runs a schema script, which a ``mode=ro`` handle cannot do;
    ``admitted_loops`` is one SELECT.  Borrowed rather than copied so that the
    pool this reads is the same pool P14e's lane spends.
    """

    def __init__(self, connection: Any) -> None:
        from .bounded_planner_loop import BoundedPlannerAuthority

        self.connection = connection
        self.admitted_loops = BoundedPlannerAuthority.admitted_loops.__get__(self)


def pool_balance(
    connection: Any, mission: Mapping[str, Any] | None, *, day: str,
) -> dict[str, Any] | None:
    """What is left of today's ad-hoc pool, not what the cap would have been.

    The cap is a property of the mission's budget and never moves; the balance
    is what P14e's tasks have already reserved today, and it is the only one of
    the two that can say no.  Checking the cap alone would let a question spend
    a pool the day's research tasks had already emptied -- which is the case
    the 25% share exists for.

    A Core with no loop tables has reserved nothing, which is a *full* pool and
    not an unreadable one: P14e's lane may simply never have run here.  Only a
    budget this cannot make sense of comes back ``None``, and ``None`` is never
    a licence.
    """

    if mission is None:
        return None
    from .research_task import pool, pool_state

    try:
        wire = pool(mission)
    except Exception:  # noqa: BLE001 - a malformed budget is "no pool", said plainly
        return None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='bounded_planner_loop_versions'"
    ).fetchone() is None:
        return {**wire, "day": day, "reserved_micros": 0,
                "remaining_micros": wire["cap_micros"]}
    try:
        return pool_state(_ReadOnlyLoops(connection), mission, day=day)
    except Exception:  # noqa: BLE001 - an unreadable ledger is not a full pool
        return None


def grant(
    mission: Mapping[str, Any] | None,
    *,
    policy: Mapping[str, Any] | None,
    slug: str | None,
    specs: Sequence[Mapping[str, Any]] = (),
    company_ref: str | None = None,
    already_refreshed: bool = False,
    pool: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether one refresh may run right now, and every reason it may not.

    Every reason, not the first: an owner who has to fix three things learns
    all three at once rather than one per attempt.

    ``pool`` is today's balance from :func:`pool_balance`; ``None`` means the
    caller could not read it, which is not a licence.
    """

    from .research_task import GRANT_WORD

    reasons: list[str] = []
    if mission is None:
        reasons.append("no_active_mission")
    else:
        if GRANT_WORD not in ((mission.get("autonomy") or {}).get("may_write") or ()):
            reasons.append("mission_does_not_grant_research_task")
        if pool is None or int(pool.get("cap_micros") or 0) <= 0:
            reasons.append("mission_budget_leaves_no_adhoc_pool")
        elif int(pool.get("remaining_micros") or 0) <= 0:
            reasons.append("adhoc_pool_spent_today")
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
            "adhoc_pool_cap_usd": None if pool is None else pool.get("cap_usd"),
            "adhoc_pool_remaining_micros": (
                None if pool is None else pool.get("remaining_micros")),
            "adhoc_pool_day": None if pool is None else pool.get("day"),
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
    pool: Mapping[str, Any] | None = None,
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
        company_ref=company_ref, already_refreshed=already_refreshed, pool=pool,
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
    start: Callable[..., Mapping[str, Any]],
    status: Callable[[str], Mapping[str, Any]],
    documents: Callable[[str], Sequence[Mapping[str, Any]]],
    sleep: Callable[[float], Any] = time.sleep,
    polls: int | None = None,
    interval: float | None = None,
) -> dict[str, Any]:
    """One discovery, waited for, and exactly the documents it recorded.

    The three calls are injected: the cockpit holds no Core write handle, so
    the real ``start`` and ``status`` go through the writer as the owner's own
    principal exactly as every other cockpit write does, and ``documents``
    reads the Core.  A test hands in the same three against a real launcher
    with a fake child.

    **Why the wait exists.**  ``run_mission_source_discovery`` spawns a child
    and returns a *ticket*, not results.  An earlier draft read the mission's
    discovered documents straight afterwards and got the oldest rows already in
    the ledger, then showed them to the owner under the heading "just fetched"
    -- the worst failure available here, because it is the one that looks like
    success.  So this polls the ticket, and when the child has not finished
    inside the bound it says ``pending`` rather than showing anything.

    **Why the documents come from the discovery.**  The settled ticket's
    summary names the ``discovery_ref`` the child recorded, and that record
    carries the exact document refs *this search* returned.  Reading the
    company's documents by any other filter cannot distinguish them from what
    was already there.
    """

    if not plan.get("available"):
        raise AskRefreshError(
            "补搜没有被允许：" + "、".join(plan.get("reason_labels") or ["原因未知"]))
    # Read from the module rather than bound as defaults, so a deployment (or
    # a test) that changes the bound changes it for calls already written.
    polls = STATUS_POLLS if polls is None else polls
    interval = STATUS_POLL_SECONDS if interval is None else interval
    operation = dict(plan["operation"]["params"])
    ticket = start(**operation)
    if not isinstance(ticket, Mapping) or not ticket.get("id"):
        return _outcome("failed", operation, reason="补搜没有拿到票据")
    ticket_ref = str(ticket["id"])
    settled: Mapping[str, Any] = ticket
    for attempt in range(max(1, polls)):
        if str(settled.get("status")) != "running":
            break
        if attempt:
            sleep(interval)
        current = status(ticket_ref)
        if isinstance(current, Mapping):
            settled = current
    state = str(settled.get("status") or "running")
    if state == "running":
        return _outcome("pending", operation, ticket_ref=ticket_ref,
                        reason="检索子进程还在跑，写入端会在下一轮把它结掉")
    if state != "succeeded":
        summary = settled.get("summary") or {}
        return _outcome("failed", operation, ticket_ref=ticket_ref,
                        reason=str(summary.get("failure_reason")
                                   or f"子进程以 {state} 结束"))
    discovery_ref = (settled.get("summary") or {}).get("discovery_ref")
    if not discovery_ref:
        # Succeeded without a discovery record: the child wrote nothing this
        # process can point at, and the writer settles the dispatch on its next
        # tick. Pending is the true word.
        return _outcome("pending", operation, ticket_ref=ticket_ref,
                        reason="子进程跑完了但还没落下发现记录")
    rows = documents(str(discovery_ref))
    headers = document_headers(rows, source_ref=operation.get("source_ref"))
    return _outcome(
        "found" if headers else "empty", operation, ticket_ref=ticket_ref,
        discovery_ref=str(discovery_ref), headers=headers,
        documents_found=len(list(rows)))


def _outcome(
    status: str, operation: Mapping[str, Any], *, ticket_ref: str | None = None,
    discovery_ref: str | None = None, headers: Sequence[Mapping[str, Any]] = (),
    documents_found: int = 0, reason: str | None = None,
) -> dict[str, Any]:
    if status not in REFRESH_OUTCOMES:
        raise AskRefreshError(f"{status!r} is not a refresh outcome")
    return {
        "schema_version": SCHEMA_VERSION,
        # The search *ran* whenever a ticket was started, whatever it turned
        # up. "We looked and found nothing" and "we did not look" are the two
        # sentences this flag has to keep apart.
        "ran": ticket_ref is not None,
        "status": status,
        "status_label": OUTCOME_LABELS[status],
        "operation": dict(operation),
        "ticket_ref": ticket_ref,
        "discovery_ref": discovery_ref,
        "documents_found": documents_found,
        "headers": [dict(header) for header in headers],
        "reason": reason,
        # Said in every place the refresh appears, because it is the one thing
        # a reader will assume otherwise: nobody has read these yet.
        "note": ("只拿到了这些文件的标题，正文还没有被读；抽取是流水线的活，不是这次问答的"
                 if status == "found" else OUTCOME_LABELS[status]),
    }


def document_headers(
    documents: Sequence[Mapping[str, Any]],
    *,
    titles: Mapping[str, Mapping[str, Any]] | None = None,
    source_ref: str | None = None,
) -> list[dict[str, Any]]:
    """The new documents as headers: what exists, from where, when.  No bodies.

    Newly discovered rows first: a search that returned four documents this
    system already held and one it did not should lead with the one it did not.
    """

    known = dict(titles or {})
    rows = sorted(documents, key=lambda item: (not item.get("new"),
                                               str(item.get("document_ref") or "")))
    out: list[dict[str, Any]] = []
    for item in rows[:MAX_HEADERS]:
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
            "new": bool(item.get("new")),
            "discovered_at": item.get("created_at"),
        })
    return out


def with_headers(
    context: Mapping[str, Any], headers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The same context plus one block of headers, tagged ``H1``…

    Appended rather than rebuilt, so the second answer is grounded in exactly
    the material the first one was plus the headers, and a reader comparing the
    two is looking at one change.  The identity is recomputed: a context that
    kept the first pass's hash after gaining a block would be claiming the two
    answers were built from the same material.
    """

    rows = []
    shown = [dict(row) for row in context.get("shown") or ()]
    for index, header in enumerate(headers):
        tag = f"{BLOCK_TAGS[REFRESH_BLOCK]}{index + 1}"
        text = header.get("title") or header["document_ref"]
        period = (header.get("discovered_at") or "")[:10] or None
        rows.append({
            "tag": tag, "ref": header["document_ref"], "text": text,
            "period": period,
            "detail": {"host": header.get("host"), "source": header.get("source_ref")},
        })
        shown.append({
            "tag": tag, "block": REFRESH_BLOCK, "ref": header["document_ref"],
            "statement": text, "period": period,
            "company": "", "at": period or "",
        })
    block = {
        "block": REFRESH_BLOCK, "label": BLOCK_LABELS[REFRESH_BLOCK],
        "tag_letter": BLOCK_TAGS[REFRESH_BLOCK], "available": bool(rows),
        "reason": None if rows else "no_record_for_this_company",
        "note": ("这些文件是刚刚补搜到的，只有标题；它们的正文还没有被读过，"
                 "所以只能说「存在这样一份材料」，不能引用其中的内容"),
        "rows": rows,
    }
    again = {
        **dict(context),
        "blocks": [*(context.get("blocks") or ()), block],
        "shown": shown,
        "refreshed": True,
    }
    again["context_hash"] = context_identity(again)
    return again


__all__ = [
    "AskRefreshError",
    "GRANT_LABELS",
    "GRANT_REASONS",
    "MAX_HEADERS",
    "REFRESH_BLOCK",
    "REFRESH_SOURCE_REFS",
    "SCHEMA_VERSION",
    "OUTCOME_LABELS",
    "REFRESH_OUTCOMES",
    "STATUS_POLLS",
    "STATUS_POLL_SECONDS",
    "document_headers",
    "grant",
    "pool_balance",
    "known_specs",
    "plan_refresh",
    "run_refresh",
    "with_headers",
]
