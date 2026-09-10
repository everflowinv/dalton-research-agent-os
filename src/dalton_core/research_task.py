"""P14e: a planner inquiry becomes a bounded loop, and nothing else is new.

The research plan has been producing good inquiries for weeks -- "do EPAM's
three adjusted-margin definitions reconcile", "which IBM business do the cited
ARR measures describe" -- and they went nowhere: there was no downstream for
an inquiry, so the one thing the planning brain produced that the checklist
could not anticipate was also the one thing nothing acted on.

The temptation is to build a dispatcher for them.  There already is one.  A
``BoundedPlannerLoop`` is exactly "one exact question, a closed set of admitted
probes, a rounds/cost/seconds budget, and a terminal gate", and
``bounded_planner_driver.run_once`` already advances every active loop by one
probe per tick, executes it, records the outcome and files the observation.  A
``ResearchTask`` is therefore not an object: it is a loop whose question came
from an inquiry.  This module is the adapter that says so, and it is thin on
purpose -- it creates no table, because the loop tables are the record.

What it does own is the three rules the owner set when the ad-hoc ban was
lifted:

1. **The grant.**  Ad-hoc research runs only when the mission's ``may_write``
   carries ``research_task`` *and* the owner has published at least one ad-hoc
   ProbeTemplate the driver can actually execute.  Both are versioned, both are
   human acts, and either one absent means no task is admitted.
2. **The pool.**  A day's research tasks may reserve at most 25% of the
   mission's ``max_daily_cost_usd``.  A task can never starve extraction, and
   when the share is spent the lane says ``skipped:pool_exhausted`` rather than
   admitting one more and hoping.  The pool is named ``adhoc`` so that C2's
   general budget pools can generalise this one instead of replacing it.
3. **The hash.**  An inquiry is admitted once, ever, keyed by its own content.
   A plan that repeats an inquiry verbatim admits nothing; a plan that reissues
   it with different text is asking a different question and admits a new task.

Outputs stay inside the scopes the mission already grants: a task's findings
reach the Ledger as Claims through candidate staging, as observations through
the loop's own follow-up, or as a deliverable.  A task never writes a Thesis, a
Playbook or a Mission, and the projection this module hands the cockpit carries
source locations rather than prose.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .bounded_planner_loop import (
    INQUIRY_ADMISSION_SOURCE,
    BoundedPlannerAuthority,
)
from .budget_pools import DEFAULT_SHARES, pool_caps
from .store import content_hash

SCHEMA_VERSION = "0.1"

# The mission word that grants this lane.  Wave 0 added it to
# AUTOMATION_WRITE_SCOPES; no live mission grants it yet.
GRANT_WORD = "research_task"

# C2 generalised this pool into four, and the 25% now lives in one place for
# everyone.  The name and the share are read from there rather than repeated
# here: a mission that declares its own ``budget.pools`` moves this lane's cap
# with it, and a deployment that changes the default share does not leave P14e
# quietly enforcing the old one.
POOL_NAME = "adhoc"
POOL_SHARE = DEFAULT_SHARES[POOL_NAME]

# What one round of a task is assumed to cost before it runs.  The loop's
# planner call is the only paid model call a task makes, and the bounded
# planner driver reserves ``planner_max_cost_usd`` for it.  That number is read
# from the driver rather than repeated here: two copies of a price drift, and
# the one that drifts is always the one nobody is looking at.  The pool is
# reserved against this estimate at admission, because a pool that only counted
# settled spend would admit a day's worth of tasks before the first one had
# billed anything.
def default_planner_cost_usd() -> Decimal:
    """The per-round reservation, as the driver is configured to spend it."""

    from .bounded_planner_driver import DEFAULT_PLANNER_MAX_COST_USD

    return Decimal(str(DEFAULT_PLANNER_MAX_COST_USD))

# A task is a question, not a project.  Four rounds is three probes and one
# retry; past that the honest terminal is "not answerable within budget".
MAX_ROUNDS_PER_TASK = 4

ACTIVE_STATUS = "active"
RETIRED_STATUS = "retired"

_INQUIRY_IDENTITY_SCHEMA = "research-task-inquiry-v1"
_CIK_RE = re.compile(r"^company:sec-cik:(\d+)$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The three ad-hoc ProbeTemplates the owner publishes at deploy.  They are
# declared here rather than only in the deploy manifest so that the admission
# adapter and the manifest cannot drift apart; a test asserts they agree.
#
# ``allowed_hosts`` is not part of the Core template record -- the record is a
# closed shape and the host allowlist is enforced by the transport that runs
# the probe -- so it travels in the manifest as the governance fact it is.
ADHOC_PROBE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "template_ref": "probe-template:adhoc-sec-filings-index:v1",
        "status": ACTIVE_STATUS,
        "capability_ref": "capability:dalton:connector:sec-filings-index",
        # bounded_probe_executor's one executable operation: it reads the
        # company-facts index and answers with the accession of the latest
        # filing carrying the asked-for concept and form.  That is a filings
        # index lookup, and it is the only one of these three the driver can
        # run today.
        "operation": "get_company_facts",
        "runtime_profile_ref": "runner-runtime:sec-public:v1",
        "parameter_contract": {
            "allowed_fields": ["source_ref", "locator", "query_terms"],
            "required_fields": ["source_ref", "locator", "query_terms"],
            "constants": {"source_ref": "source:sec-edgar"},
        },
        "output_contract_ref": "schema:bounded-planner-probe-output:0.1",
        "verifier_ref": "verifier:source-level-coverage:0.1",
        "permission_scope": "public_sec_read",
        "declared_side_effects": ["read:public-http"],
        "cost": {"cost_units": 1, "max_attempts": 2, "max_seconds": 120},
        "allowed_hosts": ["data.sec.gov"],
        "cost_estimate_usd": "0.00",
        "source_ref": "source:sec-edgar",
    },
    {
        "template_ref": "probe-template:adhoc-alphaengine-search-library:v1",
        # Retired, not active: a template whose operation no executor runs and
        # whose parameters ``_parameters_for`` cannot build is not a capability
        # this system has.  Publishing it as active advertised a probe that
        # could never appear in a binding.
        "status": RETIRED_STATUS,
        "retired_reason": "no executor: bounded_alphaengine_probe runs alphaengine_get_document only",
        "capability_ref": "capability:dalton:connector:alphaengine-search-library",
        "operation": "alphaengine_search_library",
        "runtime_profile_ref": "runtime:dalton-core-trusted-runner:0.1",
        "parameter_contract": {
            "allowed_fields": ["source_ref", "query", "filters", "max_results"],
            "required_fields": ["source_ref", "query", "max_results"],
            "constants": {"source_ref": "source:alphaengine", "max_results": 5},
        },
        "output_contract_ref": "schema:bounded-planner-probe-output:0.1",
        "verifier_ref": "verifier:source-level-coverage:0.1",
        "permission_scope": "alphaengine_read",
        "declared_side_effects": ["read:alphaengine"],
        # The owner's cap is 130 AlphaEngine calls per 24 hours across the
        # whole mission; one search per task round is the most this may take.
        "cost": {"cost_units": 2, "max_attempts": 1, "max_seconds": 120},
        "allowed_hosts": ["127.0.0.1"],
        "cost_estimate_usd": "0.00",
        "source_ref": "source:alphaengine",
    },
    {
        "template_ref": "probe-template:adhoc-web-search:v1",
        # Retired, not active: a template whose operation no executor runs and
        # whose parameters ``_parameters_for`` cannot build is not a capability
        # this system has.  Publishing it as active advertised a probe that
        # could never appear in a binding.
        "status": RETIRED_STATUS,
        "retired_reason": "no executor: no bounded probe executor runs public_web_search",
        "capability_ref": "capability:dalton:connector:gemini-web-search",
        "operation": "public_web_search",
        "runtime_profile_ref": "runtime:dalton-core-trusted-runner:0.1",
        "parameter_contract": {
            "allowed_fields": ["source_ref", "query_terms", "max_results"],
            "required_fields": ["source_ref", "query_terms", "max_results"],
            "constants": {"source_ref": "source:public-web", "max_results": 5},
        },
        "output_contract_ref": "schema:bounded-planner-probe-output:0.1",
        "verifier_ref": "verifier:source-level-coverage:0.1",
        "permission_scope": "public_web_read",
        "declared_side_effects": ["read:public-http"],
        "cost": {"cost_units": 1, "max_attempts": 2, "max_seconds": 60},
        "allowed_hosts": ["*"],
        "cost_estimate_usd": "0.01",
        "source_ref": "source:public-web",
    },
)
ADHOC_TEMPLATE_REFS: tuple[str, ...] = tuple(
    spec["template_ref"] for spec in ADHOC_PROBE_TEMPLATES
)
# The Core template record's fields, in the order publish_probe_template takes
# them.  Everything else in a spec above is manifest-only governance.
_PUBLISHED_FIELDS = (
    "capability_ref", "operation", "runtime_profile_ref", "parameter_contract",
    "output_contract_ref", "verifier_ref", "permission_scope",
    "declared_side_effects", "cost",
)


class ResearchTaskError(RuntimeError):
    """A research task could not be admitted, and the reason is safe to show."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def executable_probe_contracts() -> frozenset[tuple[str, str]]:
    """The ``(operation, permission_scope)`` pairs the tick can actually run.

    Read from the two executors rather than written down, because the failure
    it prevents is severe: an admitted probe is executed by the driver, and a
    WorkOrder the executor refuses raises rather than returning a failed
    envelope.  The B2 change below catches that, but a template nothing can run
    should never be bound in the first place.

    The pair, not the operation.  Both executors gate on the scope *first*
    (``bounded_probe_executor`` line 129, ``bounded_alphaengine_probe`` line
    89) and ``permission_scope`` is free text copied out of the template, so a
    republished template with ``public-sec-read`` instead of ``public_sec_read``
    matches on operation, binds, and is then refused at execution.  Checking
    the pair is what makes the catalogue's promise the executor's promise.
    """

    from .bounded_alphaengine_probe import (
        PROBE_OPERATION as ALPHAENGINE_OPERATION,
        PROBE_PERMISSION_SCOPE as ALPHAENGINE_SCOPE,
    )
    from .bounded_probe_executor import (
        PROBE_OPERATION as SEC_OPERATION,
        PROBE_PERMISSION_SCOPE as SEC_SCOPE,
    )

    return frozenset({
        (SEC_OPERATION, SEC_SCOPE),
        (ALPHAENGINE_OPERATION, ALPHAENGINE_SCOPE),
    })


def executable_probe_operations() -> frozenset[str]:
    """The operations of :func:`executable_probe_contracts`, for reading."""

    return frozenset(operation for operation, _ in executable_probe_contracts())


def publication_arguments(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The keyword arguments ``publish_probe_template`` takes for one spec."""

    return {field: spec[field] for field in _PUBLISHED_FIELDS}


def deploy_manifest() -> dict[str, Any]:
    """The proposed publication the owner approves at deploy.

    Proposed, not approved: ``publish_probe_template`` demands a ``human:``
    actor and this module never supplies one.  The manifest says what to
    publish; a person publishes it.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_ref": "probe-template-publication:p14e-adhoc:v1",
        "status": "proposed",
        "note": (
            "P14e ad-hoc research templates. Publish each with a human actor_ref; "
            "only templates whose operation an executor supports become bindable."
        ),
        "templates": [dict(spec) for spec in ADHOC_PROBE_TEMPLATES],
    }


# -- the inquiry's identity -------------------------------------------------

def _collapse(value: str) -> str:
    return " ".join(value.split())


def inquiry_content_hash(inquiry: Mapping[str, Any]) -> str:
    """The idempotency key for one inquiry.

    Its content, and only its content: the plan it arrived in is not part of
    it, so the same inquiry repeated in tomorrow's plan is the same inquiry and
    admits nothing.  ``because`` and ``rank`` are excluded because they are the
    planner's commentary and its ordering, not the question -- re-ranking the
    same question must not buy a second task.

    Internal whitespace is normalised before hashing.  A model that wraps the
    same sentence differently, or emits a newline where it emitted a space
    yesterday, has not asked a new question, and paying for one again because
    of a line break is exactly the kind of duplicate this rule exists to stop.
    """

    for field in ("question", "wants"):
        value = inquiry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ResearchTaskError(f"inquiry has no {field}")
    company_ref = inquiry.get("company_ref")
    if company_ref is not None and (
        not isinstance(company_ref, str) or not company_ref.strip()
    ):
        raise ResearchTaskError("inquiry company_ref must be text or null")
    return content_hash({
        "identity_schema": _INQUIRY_IDENTITY_SCHEMA,
        "company_ref": None if company_ref is None else _collapse(company_ref),
        "question": _collapse(inquiry["question"]),
        "wants": _collapse(inquiry["wants"]),
    })


def inquiry_ref_for(inquiry_hash: str) -> str:
    return f"research-task-inquiry:{inquiry_hash[:32]}"


def task_loop_ref(inquiry_hash: str) -> str:
    return f"bounded-loop:adhoc:{inquiry_hash[:24]}"


def coverage_item_ref(inquiry_hash: str, template_ref: str) -> str:
    return f"coverage:adhoc:{inquiry_hash[:16]}:{template_ref.split(':')[1]}"


# -- the catalogue ----------------------------------------------------------

def admitted_adhoc_templates(
    authority: BoundedPlannerAuthority,
) -> dict[str, dict[str, Any]]:
    """The latest published version of each ad-hoc template, by template ref."""

    admitted: dict[str, dict[str, Any]] = {}
    for template_ref in ADHOC_TEMPLATE_REFS:
        row = authority.connection.execute(
            "SELECT version_id FROM bounded_probe_template_versions "
            "WHERE template_ref=? ORDER BY version_number DESC LIMIT 1",
            (template_ref,),
        ).fetchone()
        if row is not None:
            admitted[template_ref] = authority.probe_template(row["version_id"])
    return admitted


def _withdrawn_template_refs(retired: Sequence[str] = ()) -> set[str]:
    """Every ad-hoc template ref that must not be bound, from either source."""

    return {
        spec["template_ref"] for spec in ADHOC_PROBE_TEMPLATES
        if spec.get("status") == RETIRED_STATUS
    } | {str(item) for item in retired}


def bindable_templates(
    authority: BoundedPlannerAuthority,
    *,
    retired: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    """The admitted ad-hoc templates a loop may actually bind.

    Three ways a template stops being bindable, and the owner needs all three
    because the Core template record is append-only and has no status column:

    * its ``(operation, permission_scope)`` pair is not one an executor
      accepts -- which is also how *republishing* a template with a retired
      contract revokes it in the authority itself;
    * the catalogue declares it ``retired`` here and in the deploy manifest;
    * this deployment's lane configuration names it in ``retired_templates``,
      which is the one an owner can use tonight without a release.
    """

    executable = executable_probe_contracts()
    withdrawn = _withdrawn_template_refs(retired)
    return {
        ref: template
        for ref, template in admitted_adhoc_templates(authority).items()
        if ref not in withdrawn
        and (template["operation"], template["permission_scope"]) in executable
    }


# -- the switch -------------------------------------------------------------

def pool(mission: Mapping[str, Any]) -> dict[str, Any]:
    """The mission's ad-hoc share of one day, in dollars and micros.

    The cap is C2's, not this module's.  ``budget_pools.pool_caps`` reads the
    mission's declared ``budget.pools`` when there is one and falls back to the
    default shares when there is not, and it says which of the two it did.
    What stays here is the *reservation* side of the same pool: this number
    gates admission before any model call exists, while ``pool_status`` reports
    what the day ledger actually settled against it.
    """

    caps = pool_caps(mission["budget"])
    cap_micros = int(caps["caps_micros"][POOL_NAME])
    return {
        "name": POOL_NAME,
        "share": caps["shares"][POOL_NAME],
        "caps_defaulted": caps["defaulted"],
        "mission_max_daily_cost_usd": str(
            Decimal(str(mission["budget"]["max_daily_cost_usd"]))
        ),
        "cap_usd": str(
            (Decimal(cap_micros) / Decimal(1_000_000)).quantize(Decimal("0.000001"))
        ),
        "cap_micros": cap_micros,
    }


def grant(
    mission: Mapping[str, Any] | None,
    templates: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Whether ad-hoc research is granted right now, and why not when it is not.

    Two owner acts, both versioned, both revocable by publishing again:
    ``research_task`` in the mission version's ``may_write``, and at least one
    ad-hoc ProbeTemplate published with an executable operation.  Neither is a
    flag in code, which is the point -- ``adhoc_research_enabled`` used to be
    the literal ``False``.
    """

    reasons: list[str] = []
    if mission is None:
        reasons.append("no_active_mission")
    elif GRANT_WORD not in (mission.get("autonomy") or {}).get("may_write", ()):
        reasons.append("mission_does_not_grant_research_task")
    if not templates:
        reasons.append("no_executable_adhoc_template_published")
    pool_wire = None
    if mission is not None:
        pool_wire = pool(mission)
        if pool_wire["cap_micros"] <= 0:
            reasons.append("mission_budget_leaves_no_adhoc_pool")
    return {
        "granted": not reasons,
        "reasons": reasons,
        "pool": pool_wire,
        "template_refs": sorted(templates or ()),
    }


def read_grant(store: Any, *, retired: Sequence[str] = ()) -> dict[str, Any]:
    """The grant as the live Core states it, without a model or a network call."""

    from .coverage_mission import CoverageMissionAuthority

    missions = CoverageMissionAuthority(store)
    pointer = store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer "
        "ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    mission = None if pointer is None else missions.mission(pointer["mission_version_id"])
    authority = BoundedPlannerAuthority(store)
    return {
        **grant(mission, bindable_templates(authority, retired=retired)),
        "mission": mission,
    }


def readonly_grant(
    core_db: Any, *, retired: Sequence[str] = (), mission_ref: str | None = None,
) -> dict[str, Any]:
    """The same grant, read from a process that holds no Core write handle.

    The cockpit is such a process.  ``read_grant`` above goes through the two
    authorities, and both of them run their schema script on construction --
    harmless against a live Core, impossible against a read-only connection.
    So this one asks the two questions directly and verifies each record
    against the hash its own row carries.

    Anything it cannot answer is not a licence: a missing table, a missing
    pointer or a record whose hash drifted all come back ungranted with a
    reason, never granted by default.
    """

    import sqlite3

    path = Path(str(core_db)).expanduser()
    reasons_prefix: list[str] = []
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError):
        return {
            "granted": False, "reasons": ["core_unreadable"], "pool": None,
            "template_refs": [],
        }
    connection.row_factory = sqlite3.Row
    try:
        mission = _readonly_mission(connection, mission_ref)
        templates = _readonly_templates(connection, retired=retired)
    except sqlite3.Error:
        return {
            "granted": False, "reasons": ["core_unreadable"], "pool": None,
            "template_refs": [],
        }
    finally:
        connection.close()
    decision = grant(mission, templates)
    decision["reasons"] = reasons_prefix + decision["reasons"]
    return decision


def _readonly_row_record(row: Any) -> dict[str, Any] | None:
    """One stored record, or ``None`` when its hash does not match its row."""

    import json

    try:
        wire = json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(wire, dict) or wire.get("content_hash") != row["content_hash"]:
        return None
    return wire


def _readonly_mission(connection: Any, mission_ref: str | None) -> dict[str, Any] | None:
    if mission_ref is None:
        pointer = connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
    else:
        pointer = connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "WHERE mission_ref=?", (mission_ref,),
        ).fetchone()
    if pointer is None:
        return None
    row = connection.execute(
        "SELECT record_json, content_hash FROM coverage_mission_versions "
        "WHERE mission_version_id=?", (pointer["mission_version_id"],),
    ).fetchone()
    return None if row is None else _readonly_row_record(row)


def _readonly_templates(
    connection: Any, *, retired: Sequence[str] = ()
) -> dict[str, dict[str, Any]]:
    executable = executable_probe_contracts()
    withdrawn = _withdrawn_template_refs(retired)
    templates: dict[str, dict[str, Any]] = {}
    for template_ref in ADHOC_TEMPLATE_REFS:
        if template_ref in withdrawn:
            continue
        row = connection.execute(
            "SELECT record_json, content_hash FROM bounded_probe_template_versions "
            "WHERE template_ref=? ORDER BY version_number DESC LIMIT 1",
            (template_ref,),
        ).fetchone()
        if row is None:
            continue
        wire = _readonly_row_record(row)
        if wire is None:
            continue
        if (wire.get("operation"), wire.get("permission_scope")) in executable:
            templates[template_ref] = wire
    return templates


def cockpit_grant_resolver(
    core_db: Any, *, retired: Sequence[str] = (), mission_ref: str | None = None,
) -> Any:
    """A callable the cockpit can hold: "is ad-hoc research granted right now".

    Read at call time rather than at start-up, because the two owner acts that
    grant it -- a mission version and a published template -- happen while the
    cockpit is running, and a flag decided at boot would be wrong until the
    next restart.
    """

    def resolve() -> dict[str, Any]:
        return readonly_grant(core_db, retired=retired, mission_ref=mission_ref)

    return resolve


# -- the pool ---------------------------------------------------------------

def task_estimate_micros(
    budget: Mapping[str, Any], *, planner_cost_usd: Decimal | None = None
) -> int:
    """What one task reserves from the pool: its rounds times a planner call."""

    if planner_cost_usd is None:
        planner_cost_usd = default_planner_cost_usd()
    return int(Decimal(int(budget["max_rounds"])) * planner_cost_usd * 1_000_000)


def day_reserved_micros(
    authority: BoundedPlannerAuthority,
    *,
    day: str,
    planner_cost_usd: Decimal | None = None,
) -> int:
    """What today's admitted tasks have already reserved from the pool.

    Derived from the loop authority, not from a second ledger: an inquiry loop
    records its own budget and its own admission day, so the pool needs no
    table of its own and cannot disagree with what was admitted.
    """

    if not _DAY_RE.fullmatch(day or ""):
        raise ResearchTaskError("day must be YYYY-MM-DD")
    total = 0
    for loop in authority.admitted_loops(INQUIRY_ADMISSION_SOURCE):
        if loop["created_at"][:10] != day:
            continue
        total += task_estimate_micros(loop["budget"], planner_cost_usd=planner_cost_usd)
    return total


def day_settled_micros(
    mission: Mapping[str, Any], *, day: str, budget_db: str | Path | None,
) -> int:
    """What the day ledger has actually booked to the ad-hoc pool today.

    C2b closes P14e's open item: until the planner's model calls were admitted
    against the day ledger there was nothing here to read, so the pool was a
    reservation and only a reservation.  Now every ``llm_planner_execute`` call
    carries its pool into ``thesis_impact_day_admissions`` and its settlement
    inherits that pool, so this is the other half of the same number.

    The reading is C2's own: settled where settled, reserved where a call is
    still open.  A ledger that is absent, unreadable or not yet migrated
    contributes zero -- the pool is still a gate, it just has one fewer input.

    "Unreadable" includes one case worth naming: the ledger is WAL, and C2's
    read-only open refuses a database with no sidecars, which is a ledger no
    process is holding open.  In the deployment the writer holds it while this
    lane reads beside it; with the writer down the reading falls back to
    reservations alone, which is the pre-C2b number rather than a wrong one.
    """

    if budget_db is None:
        return 0
    from .budget_pools import day_pool_spend_at

    spend = day_pool_spend_at(
        budget_db, day=day, mission_ref=mission["mission_ref"])
    return int(spend.get(POOL_NAME, 0))


def pool_state(
    authority: BoundedPlannerAuthority,
    mission: Mapping[str, Any],
    *,
    day: str,
    planner_cost_usd: Decimal | None = None,
    budget_db: str | Path | None = None,
) -> dict[str, Any]:
    """The ad-hoc pool today: its cap, what is reserved, what is spent.

    ``remaining`` subtracts both, which is deliberately conservative and
    deliberately not exact.  The two numbers measure different things -- a
    reservation is every round today's tasks may still run, a settlement is
    money already gone -- and they overlap wherever a task admitted today has
    already made a call.  The alternative to double counting that overlap is
    to admit against a cap that has already been spent, and of the two errors
    only one of them can exceed the owner's boundary.

    ``budget_db`` absent keeps the pre-C2b reading (reservations only), so an
    installation whose planner is still unbudgeted is not told it has spent
    money nobody can find.
    """

    wire = pool(mission)
    reserved = day_reserved_micros(
        authority, day=day, planner_cost_usd=planner_cost_usd
    )
    settled = day_settled_micros(mission, day=day, budget_db=budget_db)
    return {
        **wire,
        "day": day,
        "reserved_micros": reserved,
        "settled_micros": settled,
        "remaining_micros": max(wire["cap_micros"] - reserved - settled, 0),
    }


# -- admission --------------------------------------------------------------

def mandate_scope_refs(
    authority: BoundedPlannerAuthority, mission: Mapping[str, Any]
) -> frozenset[str]:
    """The exact MandateVersion's scope, which the backlog enforces anyway.

    Checked here so an inquiry about a company the mandate does not cover is
    refused with a reason a person can act on, rather than by an exception from
    two authorities down at the moment of writing.
    """

    from .agenda import AgendaNotFound, read_exact_mandate_version

    try:
        mandate = read_exact_mandate_version(
            authority.connection.cursor(),
            mission["bindings"]["mandate_version"]["ref"],
        )
    except AgendaNotFound:
        return frozenset()
    return frozenset(mandate["scope_refs"])


def _company_scope(
    inquiry: Mapping[str, Any],
    mission: Mapping[str, Any],
    scope: frozenset[str],
) -> tuple[str | None, str | None]:
    """The company this inquiry is about, or the reason it is out of scope."""

    company_ref = inquiry.get("company_ref")
    if company_ref is None:
        company_ref = mission["industry_ref"]
    else:
        universe = {member["company_ref"] for member in mission["universe"]}
        if company_ref not in universe and company_ref != mission["industry_ref"]:
            return None, "out_of_universe"
    if company_ref not in scope:
        return None, "out_of_mandate_scope"
    return company_ref, None


def _bindings_for(
    inquiry_hash: str,
    company_ref: str,
    templates: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """The probes this inquiry may run, parameterised deterministically."""

    bindings: list[dict[str, Any]] = []
    refs: list[str] = []
    for template_ref in ADHOC_TEMPLATE_REFS:
        template = templates.get(template_ref)
        if template is None:
            continue
        parameters = _parameters_for(template, company_ref)
        if parameters is None:
            continue
        refs.append(template_ref)
        bindings.append({
            "coverage_item_ref": coverage_item_ref(inquiry_hash, template_ref),
            "template_version_ref": template["id"],
            "parameters": parameters,
        })
    return bindings, refs


def _parameters_for(
    template: Mapping[str, Any], company_ref: str
) -> dict[str, Any] | None:
    operation = template["operation"]
    if operation == "get_company_facts":
        matched = _CIK_RE.fullmatch(company_ref)
        if matched is None:
            # An industry-wide inquiry has no CIK, so the filings index has
            # nothing to look up.  Absent, not faked.
            return None
        # A company ref carries the CIK as it was written; the EDGAR locator
        # wants exactly ten digits.  DXC is the live universe's nine-digit
        # ref, and an unpadded locator is a 404 the day the grant opens.
        return {
            "source_ref": "source:sec-edgar",
            "locator": f"company-facts/CIK{matched.group(1).zfill(10)}",
            # The executor reads concept candidates and the form out of these.
            "query_terms": [
                "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "10-Q",
            ],
        }
    # Every other operation in the catalogue is retired for exactly this
    # reason: there is no parameter this module could build that any executor
    # would accept.  A branch here without an executor would only move the
    # failure later.
    return None


def task_budget(
    bindings: Sequence[Mapping[str, Any]],
    templates: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    """Rounds, cost units and seconds, derived from the probes actually bound.

    One round per bound probe plus one spare, so a source that is briefly
    unavailable does not end the task; the cost and time allowances are the
    templates' own declared numbers, so a template that gets more expensive
    makes its tasks smaller rather than quietly costing more.
    """

    by_version = {template["id"]: template for template in templates.values()}
    units = [by_version[b["template_version_ref"]]["cost"] for b in bindings]
    if not units:
        raise ResearchTaskError("a task needs at least one bound probe")
    rounds = min(len(bindings) + 1, MAX_ROUNDS_PER_TASK)
    spare_cost = max(cost["cost_units"] for cost in units)
    spare_seconds = max(cost["max_seconds"] for cost in units)
    return {
        "max_rounds": rounds,
        "max_cost_units": sum(cost["cost_units"] for cost in units) + spare_cost,
        "max_seconds": sum(cost["max_seconds"] for cost in units) + spare_seconds,
    }


def plan_admissions(
    authority: BoundedPlannerAuthority,
    *,
    mission: Mapping[str, Any],
    plan: Mapping[str, Any],
    templates: Mapping[str, Mapping[str, Any]] | None = None,
    day: str | None = None,
    planner_cost_usd: Decimal | None = None,
    scope: frozenset[str] | None = None,
    limit: int | None = None,
    retired: Sequence[str] = (),
    budget_db: str | Path | None = None,
) -> list[dict[str, Any]]:
    """What this plan's inquiries would become, in the plan's own order.

    Read-only and deterministic: every entry says either that it is admissible
    and what it would cost, or exactly why it is not.  The lane admits from
    this list; the smoke script prints it.

    ``limit`` is how many the caller will actually admit this pass.  Only those
    spend the pool here: charging the day for tasks that will not be created
    until tomorrow would report a pool as exhausted when it is merely busy, and
    the reason a later entry gives ("deferred", not "exhausted") is the
    difference between "come back next tick" and "come back next day".
    """

    if templates is None:
        templates = bindable_templates(authority, retired=retired)
    day = day or datetime.now(timezone.utc).date().isoformat()
    state = pool_state(
        authority, mission, day=day, planner_cost_usd=planner_cost_usd,
        budget_db=budget_db,
    )
    if scope is None:
        scope = mandate_scope_refs(authority, mission)
    remaining = state["remaining_micros"]
    seen: set[str] = set()
    admissible_so_far = 0
    results: list[dict[str, Any]] = []
    for ordinal, inquiry in enumerate(plan.get("inquiries", ())):
        entry: dict[str, Any] = {
            # The inquiry's position in this plan's list, which is how the
            # caller finds it again.  ``rank`` is the planner's own number and
            # is not required to be an index.
            "ordinal": ordinal,
            "rank": inquiry.get("rank"),
            "question": inquiry.get("question"),
            "company_ref": inquiry.get("company_ref"),
        }
        try:
            digest = inquiry_content_hash(inquiry)
        except ResearchTaskError as exc:
            results.append({**entry, "admissible": False, "reason": str(exc)})
            continue
        entry["inquiry_hash"] = digest
        entry["inquiry_ref"] = inquiry_ref_for(digest)
        entry["loop_ref"] = task_loop_ref(digest)
        if digest in seen or authority.loop_for_admission(digest) is not None:
            results.append({**entry, "admissible": False, "reason": "already_admitted"})
            continue
        company_ref, refusal = _company_scope(inquiry, mission, scope)
        if refusal is not None:
            results.append({**entry, "admissible": False, "reason": refusal})
            continue
        entry["subject_ref"] = company_ref
        bindings, template_refs = _bindings_for(digest, company_ref, templates)
        if not bindings:
            # Two different facts, and reporting them as one sent the reader
            # looking for a missing template that is not missing.  An
            # industry-wide inquiry is refused because every probe this
            # catalogue can bind fetches by CIK, and an industry is not a
            # company -- the planner schema allows the question and this
            # system cannot yet go and answer it.
            results.append({
                **entry, "admissible": False,
                "reason": (
                    "industry_inquiry_has_no_company_probe"
                    if inquiry.get("company_ref") is None
                    else "no_bindable_template"
                ),
            })
            continue
        budget = task_budget(bindings, templates)
        estimate = task_estimate_micros(budget, planner_cost_usd=planner_cost_usd)
        if limit is not None and admissible_so_far >= limit:
            results.append({
                **entry, "admissible": False, "reason": "deferred_to_a_later_tick",
                "estimated_micros": estimate,
            })
            continue
        if estimate > remaining:
            results.append({
                **entry, "admissible": False, "reason": "pool_exhausted",
                "estimated_micros": estimate, "pool": state,
            })
            continue
        remaining -= estimate
        admissible_so_far += 1
        seen.add(digest)
        results.append({
            **entry,
            "admissible": True,
            "reason": None,
            "bindings": bindings,
            "template_refs": template_refs,
            "budget": budget,
            "estimated_micros": estimate,
        })
    return results


def admit_inquiry(
    authority: BoundedPlannerAuthority,
    backlog: Any,
    *,
    mission: Mapping[str, Any],
    plan_ref: str,
    inquiry: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Turn one admissible inquiry into a bounded loop.

    Two authority writes and no third: the question is recorded in the backlog
    under the mission's exact MandateVersion, and the loop is created against
    that question version with the admission record that makes it un-repeatable.
    """

    if not entry.get("admissible"):
        raise ResearchTaskError(f"inquiry is not admissible: {entry.get('reason')}")
    digest = entry["inquiry_hash"]
    principal = mission["autonomy"]["automation_principal"]
    mandate_version_ref = mission["bindings"]["mandate_version"]["ref"]
    recorded = backlog.record_question(
        mandate_version_ref=mandate_version_ref,
        company_ref=entry["subject_ref"],
        question=inquiry["question"].strip(),
        answer_criteria=inquiry["wants"].strip(),
        source_refs=sorted({
            spec["source_ref"] for spec in ADHOC_PROBE_TEMPLATES
            if spec["template_ref"] in set(entry["template_refs"])
        }),
        actor_ref=principal,
        idempotency_key=f"research-task:question:{digest[:32]}",
    )
    loop = authority.create_loop(
        task_loop_ref(digest),
        question_version_ref=recorded["question_version_ref"],
        template_bindings=entry["bindings"],
        required_coverage_items=[b["coverage_item_ref"] for b in entry["bindings"]],
        budget=entry["budget"],
        actor_ref=principal,
        admission={
            "source": INQUIRY_ADMISSION_SOURCE,
            "content_hash": digest,
            "inquiry_ref": inquiry_ref_for(digest),
            "plan_ref": plan_ref,
        },
    )
    return {
        "status": loop["status"],
        "inquiry_hash": digest,
        "inquiry_ref": inquiry_ref_for(digest),
        "question_ref": recorded["question_ref"],
        "question_version_ref": recorded["question_version_ref"],
        "loop_ref": loop["loop_ref"],
        "loop_version_ref": loop["id"],
        "subject_ref": entry["subject_ref"],
        "budget": loop["budget"],
        "estimated_micros": entry["estimated_micros"],
    }


# -- the cockpit's projection ----------------------------------------------

_TERMINAL_LABELS = {
    "evidence_observed_for_review": "有发现，待复核",
    "coverage_complete_unobservable_candidate": "查遍了，没有可观察到的证据",
    "budget_exhausted": "预算用完，未答完",
    "human_replan_required": "等人重新规划",
    "human_deprioritized": "人已降级",
}


def research_task_view(
    store: Any,
    *,
    day: str | None = None,
    mission: Mapping[str, Any] | None = None,
    retired: Sequence[str] = (),
    budget_db: str | Path | None = None,
) -> dict[str, Any]:
    """"正在专项研究 X / 预算用了多少 / 结论或缺口", per company.

    Every finding is a source location the loop's own coverage manifest
    recorded, never prose: an ad-hoc answer with no ref is not an answer.
    """

    authority = BoundedPlannerAuthority(store)
    day = day or datetime.now(timezone.utc).date().isoformat()
    if mission is None:
        from .coverage_mission import CoverageMissionAuthority

        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is not None:
            mission = CoverageMissionAuthority(store).mission(
                pointer["mission_version_id"]
            )
    by_company: dict[str, list[dict[str, Any]]] = {}
    for loop in authority.admitted_loops(INQUIRY_ADMISSION_SOURCE):
        question, subject = _question_of(store, loop["question_version_ref"])
        rounds = authority.rounds(loop["id"])
        outcomes = authority.outcomes(loop["id"])
        terminal = authority.terminal(loop["id"])
        citations: list[str] = []
        for outcome in outcomes:
            manifest = authority.coverage_manifest(
                outcome["coverage_manifest_ref"]
            )
            for row in manifest["entries"]:
                citations.extend(row.get("matched_source_locations") or [])
        by_company.setdefault(subject or "unknown", []).append({
            "task_ref": loop["loop_ref"],
            "loop_version_ref": loop["id"],
            "inquiry_ref": loop["admission"]["inquiry_ref"],
            "question": question,
            "admitted_at": loop["created_at"],
            "state": (
                "terminal" if terminal is not None
                else ("running" if rounds else "admitted")
            ),
            "terminal_state": None if terminal is None else terminal["terminal_state"],
            "conclusion": (
                None if terminal is None
                else _TERMINAL_LABELS.get(terminal["terminal_state"])
            ),
            "rounds_used": len(rounds),
            "rounds_budget": loop["budget"]["max_rounds"],
            "estimated_micros": task_estimate_micros(loop["budget"]),
            "citations": sorted(set(citations)),
            "gap": (
                None if citations
                else "还没有引用得上的来源" if rounds
                else "已排队，尚未开跑"
            ),
        })
    view: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": _now(),
        "day": day,
        "companies": [
            {"company_ref": company, "tasks": tasks}
            for company, tasks in sorted(by_company.items())
        ],
    }
    # What the cockpit is told this lane can do is the bindable set, never the
    # published one: a template in the catalogue that no executor runs is not
    # a capability, and showing it as one is how an owner comes to believe the
    # web search happened.
    bindable = bindable_templates(authority, retired=retired)
    view["templates"] = sorted(bindable)
    if mission is not None:
        view["pool"] = pool_state(
            authority, mission, day=day, budget_db=budget_db)
        view["grant"] = grant(mission, bindable)
    return view


def _question_of(
    store: Any, question_version_ref: str
) -> tuple[str | None, str | None]:
    """The question's own text and subject, read from the exact version."""

    from .research_question_backlog import (
        ResearchQuestionError,
        read_exact_backlog_question_version,
    )

    try:
        wire = read_exact_backlog_question_version(
            store.connection.cursor(), question_version_ref
        )
    except ResearchQuestionError:
        return None, None
    return wire.get("question"), wire.get("company_ref")


__all__ = [
    "ADHOC_PROBE_TEMPLATES",
    "ADHOC_TEMPLATE_REFS",
    "RETIRED_STATUS",
    "GRANT_WORD",
    "MAX_ROUNDS_PER_TASK",
    "POOL_NAME",
    "POOL_SHARE",
    "ResearchTaskError",
    "admit_inquiry",
    "admitted_adhoc_templates",
    "bindable_templates",
    "cockpit_grant_resolver",
    "coverage_item_ref",
    "day_reserved_micros",
    "day_settled_micros",
    "default_planner_cost_usd",
    "executable_probe_contracts",
    "deploy_manifest",
    "executable_probe_operations",
    "grant",
    "inquiry_content_hash",
    "inquiry_ref_for",
    "mandate_scope_refs",
    "plan_admissions",
    "pool",
    "pool_state",
    "publication_arguments",
    "read_grant",
    "readonly_grant",
    "research_task_view",
    "task_budget",
    "task_estimate_micros",
    "task_loop_ref",
]
