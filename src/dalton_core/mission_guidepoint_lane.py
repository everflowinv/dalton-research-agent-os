"""The Guidepoint discovery lane: plan, cadence, budget, one tick (S2).

``mission_source_discovery`` is the AlphaEngine / web-search / SEC coordinator,
and this is the same job for Guidepoint.  It is a separate module rather than a
fifth branch in that one for two reasons that are not tidiness.

**A Guidepoint plan carries industry queries.**  Every existing plan compiles
one query per (spec, company) by substituting ``{terms}``.  Half of what an
expert network is for is the question that has no issuer in it -- "are clients
cutting billable hours because of GenAI" is asked of the industry, not of
Accenture.  Those queries still need a mission grant, and the grant is per
company, so the plan names an ``industry_anchor_company_ref``: the query is
recorded against one covered issuer while its excerpts are industry evidence.
Saying so in the plan is better than the alternative, which is five
near-identical company queries pretending to be an industry sweep.

**Acquisition spends nothing.**  The other lanes budget discovery and
acquisition separately because acquisition costs provider calls.  A Guidepoint
excerpt arrives inside the search response (see ``guidepoint_acquisition``), so
the only thing this coordinator has to ration is searches.

The lane registers itself at the bottom of this file against Wave 0's
``lane_registry``, which is the whole point of that registry: ``writer_server``,
``bounded_planner_driver`` and ``macos_launchagent`` are untouched, and adding
this lane cost one line in ``LANE_MODULES``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .guidepoint_launcher import (
    GuidepointAcquisitionLauncher,
    GuidepointSearchLauncher,
)
from .guidepoint_search import (
    SEARCH_DOCUMENT_TYPES,
    SOURCE_REF,
    count_recent_guidepoint_search_calls,
    guidepoint_daily_call_ceiling,
    guidepoint_search_spec_hash,
    validate_guidepoint_search_spec,
)
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .store import canonical_json, content_hash


PLAN_SCHEMA_VERSION = "guidepoint-0.1"
GUIDEPOINT_SOURCE_REF = SOURCE_REF
# How many searches one tick may launch. The writer abandons a request after
# thirty seconds and each child is a process; three keeps a tick inside that
# window with room for a slow proxy, and the daily quota is what actually
# bounds the day's work.
SEARCHES_PER_TICK = 3
SEARCH_WAIT_SECONDS = 120.0
MAX_PLAN_CALLS_24H = 200

_PLAN_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_ref", "source_ref", "budget",
    "industry_anchor_company_ref", "companies", "specs", "industry_specs",
    "content_hash",
})
_BUDGET_FIELDS = frozenset({"max_calls_24h", "max_calls_per_tick"})
_COMPANY_FIELDS = frozenset({"search_terms"})
_SPEC_FIELDS = frozenset({
    "spec_ref", "document_type", "query_template", "lookback_days",
    "rediscovery_interval_days", "retry_interval_days", "max_excerpts",
})
_INDUSTRY_SPEC_FIELDS = frozenset({
    "spec_ref", "document_type", "query", "industry", "lookback_days",
    "rediscovery_interval_days", "retry_interval_days", "max_excerpts",
})
_SPEC_REF_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
# One ranked page of twenty is the profile ceiling; a spec may ask for less.
MAX_EXCERPTS_PER_QUERY = 20


class GuidepointPlanError(ValueError):
    """The Guidepoint discovery plan is not a plan this lane can run."""


def _plan_text(value: Any, name: str, *, maximum: int = 400) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise GuidepointPlanError(f"discovery plan {name} must be text (<={maximum} chars)")
    return value.strip()


def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise GuidepointPlanError(f"discovery plan {name} must be 1..{maximum}")
    return value


def _plan_time(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise GuidepointPlanError("created_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise GuidepointPlanError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _cadence(raw: Mapping[str, Any], cleaned: dict[str, Any]) -> dict[str, Any]:
    cleaned["lookback_days"] = _positive_int(raw["lookback_days"], "lookback_days", maximum=3650)
    cleaned["rediscovery_interval_days"] = _positive_int(
        raw["rediscovery_interval_days"], "rediscovery_interval_days", maximum=365
    )
    cleaned["retry_interval_days"] = _positive_int(
        raw["retry_interval_days"], "retry_interval_days", maximum=365
    )
    cleaned["max_excerpts"] = _positive_int(
        raw["max_excerpts"], "max_excerpts", maximum=MAX_EXCERPTS_PER_QUERY
    )
    if raw["document_type"] not in SEARCH_DOCUMENT_TYPES:
        raise GuidepointPlanError("discovery plan document_type is not an expert transcript")
    cleaned["document_type"] = raw["document_type"]
    return cleaned


def validate_guidepoint_discovery_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Closed shape check for one Guidepoint discovery plan."""

    if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
        raise GuidepointPlanError("Guidepoint discovery plan has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != PLAN_SCHEMA_VERSION:
        raise GuidepointPlanError("unsupported Guidepoint discovery plan schema_version")
    plan_id = _plan_text(wire["id"], "id")
    if not plan_id.startswith("discovery-plan:"):
        raise GuidepointPlanError("discovery plan id must use the discovery-plan: namespace")
    mission_ref = _plan_text(wire["mission_ref"], "mission_ref")
    if not mission_ref.startswith("coverage-mission:"):
        raise GuidepointPlanError("discovery plan mission_ref must be a coverage-mission ref")
    if wire["source_ref"] != GUIDEPOINT_SOURCE_REF:
        raise GuidepointPlanError("discovery plan source_ref must be source:guidepoint")
    raw_budget = wire["budget"]
    if not isinstance(raw_budget, Mapping) or set(raw_budget) != _BUDGET_FIELDS:
        raise GuidepointPlanError(
            "discovery plan budget must have exactly max_calls_24h and max_calls_per_tick"
        )
    budget = {
        "max_calls_24h": _positive_int(
            raw_budget["max_calls_24h"], "budget.max_calls_24h", maximum=MAX_PLAN_CALLS_24H
        ),
        "max_calls_per_tick": _positive_int(
            raw_budget["max_calls_per_tick"], "budget.max_calls_per_tick",
            maximum=MAX_PLAN_CALLS_24H,
        ),
    }
    companies = wire["companies"]
    if not isinstance(companies, Mapping) or not companies:
        raise GuidepointPlanError("discovery plan companies must be a non-empty object")
    cleaned_companies: dict[str, dict[str, str]] = {}
    for company_ref in sorted(companies):
        entry = companies[company_ref]
        if not isinstance(entry, Mapping) or set(entry) != _COMPANY_FIELDS:
            raise GuidepointPlanError(f"discovery plan company {company_ref} has an invalid shape")
        cleaned_companies[_plan_text(company_ref, "company_ref")] = {
            "search_terms": _plan_text(entry["search_terms"], "search_terms", maximum=120),
        }
    anchor = _plan_text(wire["industry_anchor_company_ref"], "industry_anchor_company_ref")
    if anchor not in cleaned_companies:
        raise GuidepointPlanError(
            "the industry anchor must be a company the plan already covers, because "
            "an industry query still needs that company's mission grant"
        )
    seen: set[str] = set()
    cleaned_specs: list[dict[str, Any]] = []
    for raw in wire["specs"] if isinstance(wire["specs"], list) else []:
        if not isinstance(raw, Mapping) or set(raw) != _SPEC_FIELDS:
            raise GuidepointPlanError("discovery plan spec has an invalid closed shape")
        spec_ref = _plan_text(raw["spec_ref"], "spec_ref", maximum=64)
        if _SPEC_REF_RE.fullmatch(spec_ref) is None or spec_ref in seen:
            raise GuidepointPlanError("discovery plan spec_ref must be a unique kebab-case slug")
        seen.add(spec_ref)
        template = _plan_text(raw["query_template"], "query_template")
        if "{terms}" not in template or template.count("{") != 1 or template.count("}") != 1:
            raise GuidepointPlanError(
                f"discovery plan spec {spec_ref} query_template must contain exactly one {{terms}}"
            )
        cleaned_specs.append(
            _cadence(raw, {"spec_ref": spec_ref, "query_template": template})
        )
    if not cleaned_specs:
        raise GuidepointPlanError("discovery plan specs must be a non-empty array")
    cleaned_industry: list[dict[str, Any]] = []
    for raw in wire["industry_specs"] if isinstance(wire["industry_specs"], list) else []:
        if not isinstance(raw, Mapping) or set(raw) != _INDUSTRY_SPEC_FIELDS:
            raise GuidepointPlanError("discovery plan industry spec has an invalid closed shape")
        spec_ref = _plan_text(raw["spec_ref"], "spec_ref", maximum=64)
        if _SPEC_REF_RE.fullmatch(spec_ref) is None or spec_ref in seen:
            raise GuidepointPlanError("discovery plan spec_ref must be a unique kebab-case slug")
        seen.add(spec_ref)
        query = _plan_text(raw["query"], "query")
        if "{terms}" in query:
            raise GuidepointPlanError(
                f"industry spec {spec_ref} is asked of the industry, not of an issuer"
            )
        cleaned_industry.append(
            _cadence(
                raw,
                {
                    "spec_ref": spec_ref,
                    "query": query,
                    "industry": _plan_text(raw["industry"], "industry", maximum=120),
                },
            )
        )
    if not cleaned_industry:
        raise GuidepointPlanError("discovery plan industry_specs must be a non-empty array")
    base = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "id": plan_id,
        "created_at": _plan_time(wire["created_at"]),
        "mission_ref": mission_ref,
        "source_ref": GUIDEPOINT_SOURCE_REF,
        "budget": budget,
        "industry_anchor_company_ref": anchor,
        "companies": cleaned_companies,
        "specs": cleaned_specs,
        "industry_specs": cleaned_industry,
    }
    expected = content_hash(base)
    if wire["content_hash"] != expected:
        raise GuidepointPlanError("discovery plan content_hash does not bind its content")
    return {**base, "content_hash": expected}


def build_guidepoint_discovery_plan(
    *,
    plan_id: str,
    created_at: str,
    mission_ref: str,
    companies: Mapping[str, str],
    specs: Sequence[Mapping[str, Any]],
    industry_specs: Sequence[Mapping[str, Any]],
    industry_anchor_company_ref: str,
    max_calls_24h: int,
    max_calls_per_tick: int = SEARCHES_PER_TICK,
) -> dict[str, Any]:
    base = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "id": plan_id,
        # Normalized here, not just in the validator: the hash has to be taken
        # over the same bytes the validator will re-derive it from.
        "created_at": _plan_time(created_at),
        "mission_ref": mission_ref,
        "source_ref": GUIDEPOINT_SOURCE_REF,
        "budget": {
            "max_calls_24h": max_calls_24h,
            "max_calls_per_tick": max_calls_per_tick,
        },
        "industry_anchor_company_ref": industry_anchor_company_ref,
        "companies": {ref: {"search_terms": terms} for ref, terms in companies.items()},
        "specs": [dict(spec) for spec in specs],
        "industry_specs": [dict(spec) for spec in industry_specs],
    }
    return validate_guidepoint_discovery_plan({**base, "content_hash": content_hash(base)})


def load_guidepoint_discovery_plan(path: str | Path) -> dict[str, Any]:
    return validate_guidepoint_discovery_plan(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def guidepoint_plan_spec(plan: Mapping[str, Any], spec_ref: str) -> tuple[str, dict[str, Any]]:
    """Return ``("company" | "industry", spec)`` for one spec ref."""

    for spec in plan["specs"]:
        if spec["spec_ref"] == spec_ref:
            return "company", dict(spec)
    for spec in plan["industry_specs"]:
        if spec["spec_ref"] == spec_ref:
            return "industry", dict(spec)
    raise GuidepointPlanError(f"discovery plan has no spec {spec_ref}")


def build_guidepoint_parameters(
    plan: Mapping[str, Any], *, spec_ref: str, company_ref: str, as_of: date
) -> dict[str, Any]:
    """Deterministic ``search_library`` parameters for one plan spec.

    Deterministic on purpose: the same plan, spec, company and calendar day
    compile to the same parameters, so the query hash is a stable identity the
    cadence can compare against rather than a new document every tick.
    """

    kind, spec = guidepoint_plan_spec(plan, spec_ref)
    company = plan["companies"].get(company_ref)
    if company is None:
        raise GuidepointPlanError(f"discovery plan does not cover {company_ref}")
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise GuidepointPlanError("as_of must be a calendar date")
    window_start = (as_of - timedelta(days=spec["lookback_days"])).isoformat()
    filters: dict[str, Any] = {
        "document_type": spec["document_type"],
        "date_from": window_start,
        "date_to": as_of.isoformat(),
    }
    if kind == "company":
        query = spec["query_template"].replace("{terms}", company["search_terms"])
        filters["company"] = company["search_terms"]
    else:
        if company_ref != plan["industry_anchor_company_ref"]:
            raise GuidepointPlanError(
                "an industry spec runs under the plan's anchor company only"
            )
        query = spec["query"]
        filters["industry"] = spec["industry"]
    return validate_guidepoint_search_spec(
        {"query": query, "filters": filters, "cursor": None}
    )


def guidepoint_query_hash(plan: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    del plan
    return guidepoint_search_spec_hash(parameters)


def guidepoint_plan_queries(plan: Mapping[str, Any], *, as_of: date) -> list[dict[str, Any]]:
    """Every query the plan compiles today, in a stable order.

    Company specs first, in plan order, each across the covered companies in
    sorted order; then the industry sweep under the anchor.  Stability is what
    makes the rotation in ``launch_discovery`` fair rather than accidental.
    """

    queries: list[dict[str, Any]] = []
    for spec in plan["specs"]:
        for company_ref in sorted(plan["companies"]):
            parameters = build_guidepoint_parameters(
                plan, spec_ref=spec["spec_ref"], company_ref=company_ref, as_of=as_of
            )
            queries.append(
                {
                    "kind": "company",
                    "spec_ref": spec["spec_ref"],
                    "company_ref": company_ref,
                    "parameters": parameters,
                    "query_hash": guidepoint_search_spec_hash(parameters),
                    "rediscovery_interval_days": spec["rediscovery_interval_days"],
                    "retry_interval_days": spec["retry_interval_days"],
                    "max_excerpts": spec["max_excerpts"],
                }
            )
    anchor = plan["industry_anchor_company_ref"]
    for spec in plan["industry_specs"]:
        parameters = build_guidepoint_parameters(
            plan, spec_ref=spec["spec_ref"], company_ref=anchor, as_of=as_of
        )
        queries.append(
            {
                "kind": "industry",
                "spec_ref": spec["spec_ref"],
                "company_ref": anchor,
                "parameters": parameters,
                "query_hash": guidepoint_search_spec_hash(parameters),
                "rediscovery_interval_days": spec["rediscovery_interval_days"],
                "retry_interval_days": spec["retry_interval_days"],
                "max_excerpts": spec["max_excerpts"],
            }
        )
    return queries


# ---------------------------------------------------------------------------
# coordinator
# ---------------------------------------------------------------------------
class GuidepointLaneCoordinator:
    """One bounded, quota-aware Guidepoint tick.

    The coordinator never calls Guidepoint itself.  It decides *whether* a
    call may be spent and *which* query is next, then hands the work to a
    child launcher, exactly as ``MissionSourceDiscoveryCoordinator`` does --
    the provider call needs a process main thread for the transport watchdog.

    A tick is four things in order: settle the children the last tick
    launched, settle finished acquisitions, drain the acquisition queue (free,
    so it runs even when search quota is gone), and only then spend a search.
    Settling first is what stops the ledger filling with rows that say
    ``launched`` about processes that died three days ago.
    """

    def __init__(
        self,
        *,
        missions: Any,
        connection: Any,
        launcher: GuidepointSearchLauncher,
        plan: Mapping[str, Any],
        acquisition_launcher: Any | None = None,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
        requested_by: str | None = None,
        clock: Any | None = None,
    ) -> None:
        self.missions = missions
        self.connection = connection
        self.launcher = launcher
        self.acquisition_launcher = acquisition_launcher
        self.plan = validate_guidepoint_discovery_plan(plan)
        self._mission_version_ref = mission_version_ref
        self._mission_version_hash = mission_version_hash
        self._requested_by = requested_by
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- the mission this plan is written against --------------------------
    def _resolve_mission(self) -> tuple[str, str, str]:
        """The active mission version, resolved every tick rather than cached.

        Publishing a new mission version is how an owner connects or
        disconnects this source, so a lane holding last week's version would
        notice neither.  Resolution is deliberately *not* done in ``__init__``:
        a Core with no mission at all is an unconfigured writer, not a broken
        one, and constructing the coordinator must not be the thing that
        raises.
        """

        if (
            self._mission_version_ref is not None
            and self._mission_version_hash is not None
            and self._requested_by is not None
        ):
            return (self._mission_version_ref, self._mission_version_hash,
                    self._requested_by)
        mission = self.missions.active_mission(self.plan["mission_ref"])
        return (
            self._mission_version_ref or mission["id"],
            self._mission_version_hash or mission["content_hash"],
            self._requested_by or mission["autonomy"]["automation_principal"],
        )

    @property
    def mission_version_ref(self) -> str:
        return self._resolve_mission()[0]

    @property
    def mission_version_hash(self) -> str:
        return self._resolve_mission()[1]

    @property
    def requested_by(self) -> str:
        return self._resolve_mission()[2]

    # -- budget ------------------------------------------------------------
    def budget(self) -> dict[str, int]:
        """What today's remaining allowance actually is.

        Three ceilings, and the smallest wins: the owner-approved daily quota
        in ``connector_quota_policy``, the plan's own ``max_calls_24h``, and
        the per-tick cap.  Read before a call is spent rather than discovered
        as a 429 -- an expert network noticing a client hammering its search
        endpoint is a relationship problem, not a retry.
        """

        governed = guidepoint_daily_call_ceiling()
        planned = int(self.plan["budget"]["max_calls_24h"])
        spent = count_recent_guidepoint_search_calls(self.connection, as_of=self.clock())
        ceiling = min(governed, planned)
        remaining = max(0, ceiling - spent)
        per_tick = int(self.plan["budget"]["max_calls_per_tick"])
        return {
            "governed_daily_limit": governed,
            "plan_daily_limit": planned,
            "spent_24h": spent,
            "remaining_24h": remaining,
            "max_calls_per_tick": per_tick,
            "launchable": min(remaining, per_tick),
        }

    # -- grants ------------------------------------------------------------
    def grant(self, company_ref: str) -> dict[str, Any]:
        """Re-derive the mission grant for one company before spending a call.

        ``authorize_source_discovery`` is what checks that the mission marks
        ``source:guidepoint`` connected and grants ``source_discovery`` and
        ``observation``.  The lane does not keep its own copy of that answer.
        """

        mission_ref, mission_hash, requested_by = self._resolve_mission()
        return self.missions.authorize_source_discovery(
            company_ref=company_ref,
            source_ref=GUIDEPOINT_SOURCE_REF,
            requested_by=requested_by,
            mission_version_ref=mission_ref,
            mission_version_hash=mission_hash,
        )

    # -- cadence -----------------------------------------------------------
    def _last_discovery(self, company_ref: str, spec_ref: str) -> dict[str, Any] | None:
        records = self.missions.source_discoveries(
            self.mission_version_ref, company_ref=company_ref, spec_ref=spec_ref, limit=1
        )
        for record in records:
            if record["source_ref"] == GUIDEPOINT_SOURCE_REF:
                return record
        return None

    def due_queries(self, *, as_of: date | None = None) -> list[dict[str, Any]]:
        """The plan's queries the cadence says are due, in plan order.

        Two intervals, because two things can have happened.  A query that
        returned excerpts waits ``rediscovery_interval_days`` before asking
        again -- the library does not gain transcripts on a topic every day.
        A query that returned *nothing* waits ``retry_interval_days``, which
        is short: an empty page usually means the phrasing missed, and the
        cheapest correction is to try again soon rather than to leave a hole
        in the coverage for three weeks.
        """

        now = self.clock()
        today = as_of or now.date()
        due: list[dict[str, Any]] = []
        for query in guidepoint_plan_queries(self.plan, as_of=today):
            record = self._last_discovery(query["company_ref"], query["spec_ref"])
            if record is None:
                due.append({**query, "reason": "never_run"})
                continue
            try:
                last = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            except ValueError:
                due.append({**query, "reason": "unreadable_last_run"})
                continue
            empty = not record.get("document_refs")
            days = (
                query["retry_interval_days"] if empty
                else query["rediscovery_interval_days"]
            )
            if now - last.astimezone(timezone.utc) >= timedelta(days=days):
                due.append({
                    **query,
                    "reason": "retry_due" if empty else "cadence_due",
                })
        return due

    # -- settling last tick's children -------------------------------------
    def settle_dispatches(self) -> list[dict[str, Any]]:
        """Reconcile every search child this lane has open.

        A launched child whose ticket says ``orphaned`` -- writer restarted,
        process gone -- is neither a success nor a running job, and a lane that
        never looked would leave that row ``launched`` forever and block
        nothing while claiming to be busy.  Success is not inferred from the
        exit code alone either: the child writes ``discovery_ref`` only when
        the mission ledger actually accepted the discovery.
        """

        settled: list[dict[str, Any]] = []
        for dispatch in self.missions.open_discovery_dispatches(
            source_ref=GUIDEPOINT_SOURCE_REF
        ):
            try:
                ticket = self.launcher.status(dispatch["ticket_ref"])
            except LaneChildTicketNotFound:
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="failed", reason="ticket is missing"
                )
                settled.append(
                    {"dispatch_ref": dispatch["dispatch_id"], "status": result["status"]}
                )
                continue
            if ticket["status"] == "running":
                continue
            summary = ticket.get("summary") or {}
            if ticket["status"] == "succeeded" and summary.get("discovery_ref"):
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="succeeded"
                )
            else:
                reason = (
                    summary.get("failure_reason")
                    or f"child ended {ticket['status']} (exit {ticket.get('exit_code')})"
                )
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="failed", reason=str(reason)[:500]
                )
            settled.append({
                "dispatch_ref": dispatch["dispatch_id"],
                "status": result["status"],
                "ticket_ref": dispatch["ticket_ref"],
                "discovery_ref": summary.get("discovery_ref"),
                "new_document_count": summary.get("new_document_count"),
            })
        return settled

    def settle_documents(self) -> list[dict[str, Any]]:
        """Reconcile finished excerpt acquisitions and open their reviews."""

        settled: list[dict[str, Any]] = []
        if self.acquisition_launcher is None:
            return settled
        for document in self.missions.launched_discovered_documents(
            source_ref=GUIDEPOINT_SOURCE_REF
        ):
            try:
                ticket = self.acquisition_launcher.status(document["ticket_ref"])
            except LookupError:
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquisition_failed",
                    reason="ticket is missing",
                )
                settled.append(
                    {"record_id": document["record_id"], "status": result["status"]}
                )
                continue
            if ticket.get("status") == "running":
                continue
            summary = ticket.get("summary") or {}
            entry: dict[str, Any] = {
                "record_id": document["record_id"],
                "document_ref": document["document_ref"],
                "ticket_ref": document["ticket_ref"],
            }
            if ticket.get("status") == "succeeded" and summary.get("manifest_ref"):
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquired"
                )
                try:
                    review = self.missions.register_document_review(
                        document["record_id"],
                        requested_by=self.missions.mission(
                            document["mission_version_ref"]
                        )["autonomy"]["automation_principal"],
                    )
                    entry["review_status"] = review["status"]
                    entry["review_id"] = review["review_id"]
                except Exception as exc:  # noqa: BLE001 - a refusal, not a crash
                    entry["review_status"] = f"not_registered:{type(exc).__name__}"
            else:
                reason = (
                    summary.get("failure_reason")
                    or f"acquisition ended {ticket.get('status')} "
                       f"(exit {ticket.get('exit_code')})"
                )
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquisition_failed",
                    reason=str(reason)[:500],
                )
            entry["status"] = result["status"]
            settled.append(entry)
        return settled

    # -- acquisition (free) ------------------------------------------------
    def launch_acquisition(self) -> dict[str, Any]:
        """Turn one queued excerpt into a manifest.  Spends no Guidepoint call.

        Which is why it runs before the search and regardless of the quota: a
        day whose search allowance is gone can still be a day that finishes
        reading what yesterday found.
        """

        if self.acquisition_launcher is None:
            return {"status": "unconfigured",
                    "reason": "no Guidepoint acquisition launcher on this writer"}
        if self.missions.launched_discovered_documents(
            limit=1, source_ref=GUIDEPOINT_SOURCE_REF
        ):
            return {"status": "busy", "reason": "an excerpt acquisition is still open"}
        document = self.missions.next_discovered_document(
            source_ref=GUIDEPOINT_SOURCE_REF
        )
        if document is None:
            return {"status": "idle", "reason": "no queued excerpt"}
        discovery = self.missions.discovery_record(document["discovery_ref"])
        try:
            ticket = self.acquisition_launcher.start(
                source_envelope_ref=discovery["source_envelope_ref"],
                document_ref=document["document_ref"],
            )
        except LaneChildConflict as exc:
            return {"status": "busy", "reason": str(exc)}
        except LaneChildRejected as exc:
            return {"status": "rejected", "reason": f"{type(exc).__name__}: {exc}"}
        self.missions.mark_discovered_document_launched(
            document["record_id"], ticket["id"]
        )
        return {
            "status": "launched",
            "record_id": document["record_id"],
            "document_ref": document["document_ref"],
            "ticket_ref": ticket["id"],
        }

    # -- one search tick ---------------------------------------------------
    def launch_discovery(self, *, as_of: date | None = None) -> dict[str, Any]:
        """Launch at most ``launchable`` searches; return what the tick did.

        Returns rather than raises when there is nothing to do: an exhausted
        quota, a mission that has not connected the source, and a cadence with
        nothing due are all normal states, and a lane that raised on them
        would read as broken in the cockpit every time it behaved correctly.
        """

        budget = self.budget()
        result: dict[str, Any] = {
            "source_ref": GUIDEPOINT_SOURCE_REF,
            "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"],
            "budget": budget,
            "status": "idle",
            "reason": None,
            "launched": [],
            "skipped": [],
        }
        if budget["launchable"] < 1:
            result["reason"] = (
                "quota_exhausted" if budget["remaining_24h"] < 1 else "tick_cap_reached"
            )
            return result
        due = self.due_queries(as_of=as_of)
        if not due:
            result["reason"] = "nothing_due"
            return result
        for query in due[: budget["launchable"]]:
            try:
                authorization = self.grant(query["company_ref"])
            except Exception as exc:  # the mission's refusal, recorded not raised
                result["skipped"].append({
                    "spec_ref": query["spec_ref"],
                    "company_ref": query["company_ref"],
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            try:
                ticket = self.launcher.start(
                    plan=self.plan,
                    spec_ref=query["spec_ref"],
                    company_ref=query["company_ref"],
                    parameters=query["parameters"],
                    query_hash=query["query_hash"],
                    requested_by=authorization["requested_by"],
                    mission_version_ref=authorization["mission_version_ref"],
                    mission_version_hash=authorization["mission_version_hash"],
                    as_of=as_of or self.clock().date(),
                )
            except LaneChildConflict:
                # One child at a time is the lane's contract, not a failure:
                # the remaining queries are still due on the next tick.
                result["skipped"].append({
                    "spec_ref": query["spec_ref"],
                    "company_ref": query["company_ref"],
                    "reason": "child_slot_busy",
                })
                break
            except LaneChildRejected as exc:
                # A withdrawn or unapproved record, or a plan that moved under
                # the coordinator. Not a fault of this tick and not something
                # to keep retrying at speed, so it is reported and the tick
                # stops rather than walking the rest of the queue into the
                # same refusal.
                result["skipped"].append({
                    "spec_ref": query["spec_ref"],
                    "company_ref": query["company_ref"],
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                result["reason"] = "not_approved"
                break
            dispatch = self.missions.record_discovery_dispatch(
                authorization=authorization,
                discovery_plan_ref=self.plan["id"],
                discovery_plan_hash=self.plan["content_hash"],
                spec_ref=query["spec_ref"],
                query_hash=query["query_hash"],
                ticket_ref=ticket["id"],
            )
            result["launched"].append({
                "spec_ref": query["spec_ref"],
                "company_ref": query["company_ref"],
                "kind": query["kind"],
                "reason": query["reason"],
                "query_hash": query["query_hash"],
                "ticket_ref": ticket["id"],
                "dispatch_ref": dispatch["dispatch_id"],
            })
        result["status"] = "launched" if result["launched"] else "idle"
        if not result["launched"] and result["reason"] is None and result["skipped"]:
            reasons = {item["reason"] for item in result["skipped"]}
            result["reason"] = (
                "child_slot_busy" if reasons == {"child_slot_busy"}
                else "all_grants_refused"
            )
        return result

    # -- the whole tick ----------------------------------------------------
    def run_once(self, *, as_of: date | None = None) -> dict[str, Any]:
        """Settle, acquire, search -- and never raise out of a controller tick."""

        # The mission first: without one there is nothing to settle *for*, and
        # failing here rather than three calls later is what makes the reason
        # in the tick summary the actual reason.
        self._resolve_mission()
        return {
            "settled_dispatches": self.settle_dispatches(),
            "settled_documents": self.settle_documents(),
            "acquisition": self.launch_acquisition(),
            **self.launch_discovery(as_of=as_of),
        }



# ---------------------------------------------------------------------------
# lane registration (Wave 0's LaneSpec; one line in LANE_MODULES)
# ---------------------------------------------------------------------------
LAUNCHER_KWARG = "guidepoint_search_launcher"
# Named by version rather than discovered, so a future v2 record is a
# deliberate edit here and not something the writer picks up because a file
# appeared in the governance directory.
GUIDEPOINT_LANE_GOVERNANCE = "guidepoint-search-library-v1.json"
GUIDEPOINT_LANE_PLAN = "us-it-services-guidepoint-v1.json"
GUIDEPOINT_MCP_ENDPOINT = "http://127.0.0.1:8943/mcp"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (S2): one bounded, quota-aware Guidepoint sweep."""

    del params
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no Guidepoint lane on this writer"}
    try:
        coordinator = GuidepointLaneCoordinator(
            missions=server.coverage_mission,
            connection=server.store.connection,
            launcher=launcher,
            plan=launcher.plan,
            acquisition_launcher=getattr(launcher, "acquisition_launcher", None),
        )
        return coordinator.run_once()
    except Exception as exc:  # noqa: BLE001
        # A tick reports; it does not raise. A missing mission, a plan that
        # was replaced with something malformed, a Core mid-migration -- all of
        # them mean this lane has nothing it may do right now, and an
        # exception out of here takes the whole controller tick with it.
        return {
            "status": "unconfigured",
            "reason": f"{type(exc).__name__}: {exc}",
            "source_ref": GUIDEPOINT_SOURCE_REF,
        }


def add_arguments(parser: Any) -> None:
    # Off unless an approved governance record and a plan are both named,
    # like every other connector lane on this writer.
    parser.add_argument(
        "--guidepoint-search-governance",
        help="approved guidepoint-search-library governance record",
    )
    parser.add_argument(
        "--guidepoint-discovery-plan",
        help="hash-bound Guidepoint discovery plan "
             "(deploy/phase9/p9-us-it-services-guidepoint-v1.json)",
    )
    parser.add_argument("--guidepoint-mcp-endpoint", default=GUIDEPOINT_MCP_ENDPOINT)
    parser.add_argument(
        "--guidepoint-fixture",
        help="rehearsal only: serve packaged rows instead of reaching the proxy",
    )


def build_launcher(args: Any) -> Any | None:
    governance = getattr(args, "guidepoint_search_governance", None)
    plan_path = getattr(args, "guidepoint_discovery_plan", None)
    if governance is None or plan_path is None:
        return None
    fixture = getattr(args, "guidepoint_fixture", None)
    state_dir = Path(args.db).expanduser().resolve().parent
    return GuidepointSearchLauncher(
        state_dir=state_dir,
        governance_path=governance,
        plan_path=plan_path,
        mode_args=() if fixture is not None else ("--allow-network",),
        fake_search_file=fixture,
        mcp_endpoint=getattr(args, "guidepoint_mcp_endpoint", GUIDEPOINT_MCP_ENDPOINT),
        # The acquisition child rides along on the search lane's single
        # registry keyword. It is a separate process slot -- acquiring costs
        # no Guidepoint call and must not wait behind a search -- but it is
        # the same lane, installed and shut down together.
        acquisition_launcher=GuidepointAcquisitionLauncher(state_dir=state_dir),
    )


def argv_fragment(context: Any) -> list[str]:
    """Both files or nothing.

    The lane writes into the mission ledger rather than the Cockpit inbox, so
    it is enabled by its own approved record *and* its plan being installed.
    Either one alone would give a writer a lane that starts and then refuses
    every tick, which reads like a fault instead of an absence.
    """

    governance = context.state / "connector-governance" / GUIDEPOINT_LANE_GOVERNANCE
    plan = context.state / "discovery-plans" / GUIDEPOINT_LANE_PLAN
    if not governance.is_file() or not plan.is_file():
        return []
    return [
        "--guidepoint-search-governance", str(governance),
        "--guidepoint-discovery-plan", str(plan),
        "--guidepoint-mcp-endpoint", GUIDEPOINT_MCP_ENDPOINT,
    ]


LANE = register_lane(LaneSpec(
    # Right after the three existing discovery coordinators and before
    # extraction: what this lane queues is what extraction reads.
    operation="dispatch_guidepoint_discovery",
    order=35,
    driver_key="guidepoint_discovery",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="S2: expert-network transcript excerpts. One search child at a time, "
         "bounded by the owner-approved daily quota and the plan's own "
         "budget; acquisition spends no further call.",
))


__all__ = [
    "GUIDEPOINT_LANE_GOVERNANCE",
    "GUIDEPOINT_LANE_PLAN",
    "GUIDEPOINT_MCP_ENDPOINT",
    "GUIDEPOINT_SOURCE_REF",
    "LANE",
    "LAUNCHER_KWARG",
    "GuidepointLaneCoordinator",
    "GuidepointPlanError",
    "MAX_EXCERPTS_PER_QUERY",
    "PLAN_SCHEMA_VERSION",
    "SEARCHES_PER_TICK",
    "SEARCH_WAIT_SECONDS",
    "add_arguments",
    "argv_fragment",
    "build_guidepoint_discovery_plan",
    "build_guidepoint_parameters",
    "build_launcher",
    "dispatch",
    "guidepoint_plan_queries",
    "guidepoint_plan_spec",
    "guidepoint_query_hash",
    "load_guidepoint_discovery_plan",
    "validate_guidepoint_discovery_plan",
]
