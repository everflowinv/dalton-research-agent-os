"""Four capacity pools per mission per day, and the rule for borrowing between them.

A mission has one number for what a day may cost -- ``max_daily_cost_usd`` --
and until now every lane drank from it in the order it happened to run.  That
is a queue, not a budget: an extraction backlog on a Tuesday morning could
spend the whole day before the event lane had seen a single 8-K, and nothing
in the ledger afterwards said which kind of work the money went to.  C2 splits
the day into four named pools, each with its own cap:

``coverage``
    The work the mission exists for: discovery, extraction, statements,
    prices, model specs and forecasts, the research plan, Initial Screens.
``event_response``
    Reacting to what happened today: price moves, filings, news, the event
    judgement lane and the tracking cadence it adjusts.
``adhoc``
    Questions nobody wrote down in advance -- P14e's research tasks.  Its 25%
    share is the boundary the main agent set when ``adhoc_research`` was
    unfrozen, and this module is where that number now lives for everyone.
``maintenance``
    Keeping the shelves tidy: catalog work, Claim index tagging, quality
    scoring, the weekly reflection, retirement and cleanup.

Three properties are the whole point, and each one is a thing that used to be
impossible to answer:

1. **A pool is an admission gate.**  Before a paid call is admitted, the pool
   it belongs to must have room.  When it does not the admission comes back
   ``{"status": "rejected", "reason": "pool_exhausted", ...}`` and the lane
   says ``skipped:pool_exhausted``.  It is a returned decision rather than an
   exception because a lane that is out of budget has not failed -- it has
   finished for today -- and a tick summary reading ``unavailable:RuntimeError``
   would say the opposite.

2. **The pool is also a settlement scope.**  P14e's ad-hoc pool derived its
   day account from the loop authority, which is exact for admissions and
   silent about what was actually spent; P14a's and P14e's reviews both
   flagged the same gap from opposite sides.  So the pool travels with the
   admission into the day ledger, and the settlement inherits it from the
   admission row in the same transaction.  Pool and ledger cannot disagree
   because they are the same row.

3. **Unused share is borrowable, but only downhill and only late.**
   ``coverage`` may borrow another pool's unspent share, and only after the
   owning pool's day is more than half over -- before noon UTC an idle event
   pool is not idle, it is waiting.  Every borrowed admission carries
   ``borrowed_from`` naming the lender and the amount, so a day where coverage
   ate the event pool is visible rather than inferred.

The mission is where the split belongs, and the mission cannot carry it yet:
``coverage_mission``'s ``budget`` block is a closed field set of three keys and
this slice does not own that file.  So the caps are read from
``mission["budget"]["pools"]`` when it is there and derived from the default
shares when it is not, and the derived answer says ``defaulted: true`` rather
than pretending the owner chose it.  The field the mission owner should add is
documented in :data:`MISSION_POOLS_FIELD` and in this slice's report.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .store import canonical_json, content_hash


class BudgetPoolError(RuntimeError):
    """A pool name, share or cap is not one this system admits."""


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("budget_pools_schema.sql")

# The four pools, in the order a person reads them: what we came to do, what
# happened today, what we chose to ask, and keeping the shelves tidy.
POOL_NAMES: tuple[str, ...] = ("coverage", "event_response", "adhoc", "maintenance")

# The default split of a day, used when the mission has not declared one.
# ``adhoc`` is 25% because that is the boundary the main agent set when ad-hoc
# research was unfrozen (parallel plan section 1); the rest is the order of
# magnitude the lanes already run at, not a measured optimum.
DEFAULT_SHARES: dict[str, Decimal] = {
    "coverage": Decimal("0.55"),
    "event_response": Decimal("0.15"),
    "adhoc": Decimal("0.25"),
    "maintenance": Decimal("0.05"),
}

# Only coverage borrows.  A pool that could borrow from coverage would make
# the mission's own work the residual claimant on every other lane's appetite,
# which is the failure this whole module exists to prevent.
BORROWING_POOLS: frozenset[str] = frozenset({"coverage"})

# Before this much of the owning pool's day has passed, its unspent share is
# not spare -- it is unspent.
BORROW_AFTER_DAY_FRACTION = 0.5

# The word a lane says when its pool is spent.  P14e coined it for the ad-hoc
# pool; C2 generalises it, and every reader (cockpit, reflection) matches on
# this constant rather than on a literal.
POOL_EXHAUSTED_STATUS = "skipped:pool_exhausted"
POOL_EXHAUSTED_REASON = "pool_exhausted"

# The field ``coverage_mission``'s ``budget`` block should gain so that a
# mission version can declare its own split.  Until then the split is derived
# and every reader is told so.  The shape is USD caps, one per pool name,
# summing to no more than ``max_daily_cost_usd`` -- shares would have needed a
# second rounding rule and the mission already speaks in dollars.
MISSION_POOLS_FIELD = "pools"

# How many of today's pool refusals ``pool_status`` returns.  A spent pool
# refuses everything that asks for the rest of the day, so the interesting
# facts are how many and which lanes most recently -- not all of them.
MAX_EXHAUSTED_LANES = 20

# Which pool each lane operation drinks from.  Central on purpose: assigning a
# pool must not mean editing fifteen lane modules owned by other agents.  A
# lane that wants to say it itself sets ``LaneSpec(budget_pool=...)``, which
# wins over this table; a lane that says nothing and is not named here is
# coverage, which is what every lane effectively was before this module.
#
# The entries below the line are lanes that are not registered yet.  Naming
# them here now is what keeps them from landing silently in coverage the week
# they merge.
LANE_POOLS: dict[str, str] = {
    "dispatch_mission_source_discovery": "coverage",
    "dispatch_guidepoint_discovery": "coverage",
    "dispatch_document_extraction": "coverage",
    "dispatch_mission_stage": "coverage",
    "dispatch_mission_sec_quarters": "coverage",
    "dispatch_mission_statements": "coverage",
    "dispatch_mission_market_prices": "coverage",
    "dispatch_company_model_spec": "coverage",
    "dispatch_company_model_forecast": "coverage",
    "dispatch_research_plan": "coverage",
    "dispatch_initial_screen": "coverage",
    "dispatch_sales_notes_feed": "coverage",
    "dispatch_company_wiki_feed": "coverage",
    "dispatch_mission_crowd_sources": "coverage",
    # P14a and C1: what happened today, and the calendar that says what is
    # about to.  The judgement lane keeps its own book of what it spent
    # (``event_response_spend``); this is the same pool seen from the ledger.
    "dispatch_mission_tracking": "event_response",
    "dispatch_mission_catalyst_calendar": "event_response",
    "dispatch_event_judgement": "event_response",
    # P14f: the earnings season. The same pool for the same reason -- a
    # preview and a calibration are responses to a dated event, and the day
    # a company reports is the day this pool is meant to be spent.
    "dispatch_earnings_season": "event_response",
    # P14e.
    "dispatch_research_task": "adhoc",
    # P12c: drafting the debate map is coverage work, like the dossier.
    "dispatch_debate_map": "coverage",
    "dispatch_company_dossier": "coverage",
    # Keeping the shelves tidy.
    "dispatch_claim_review": "maintenance",
    "dispatch_claim_index": "maintenance",
    # P14-M2: following the gateway's model catalog. Maintenance because it is
    # bookkeeping -- it makes no model call at all -- and because the day a
    # provider outage makes the coverage pool precious is exactly the day this
    # lane must still be able to notice a model has gone.
    "dispatch_catalog_sync": "maintenance",
    # -- not registered yet -------------------------------------------------
    "dispatch_market_events": "event_response",
    "dispatch_research_reflection": "maintenance",
    "dispatch_claim_retirement": "maintenance",
}

# The same question for a cockpit-shaped model call, which is admitted by
# purpose rather than by lane operation.  ``cockpit_model`` is the one place
# every such call passes through, so this is where the pool is decided for
# anything that is not a tick.
PURPOSE_POOLS: dict[str, str] = {
    "ask": "coverage",
    "goal": "coverage",
    "steer": "coverage",
    "draft": "coverage",
    "plan": "coverage",
    "model_spec": "coverage",
    "claim_index": "maintenance",
    "quality": "maintenance",
    # P14a's judgement lane pays for two calls per event and two more per
    # reflection, and books every one of them in its own
    # ``event_response_spend`` table.  Both purposes belong to the same pool,
    # or its book and this ledger would be counting different things.
    "event_judgement": "event_response",
    "thesis_reflection": "event_response",
    # P14e's loops, when they get their own purpose (today their model call
    # goes through llm_planner_execute, which carries the pool explicitly).
    "research_task": "adhoc",
    "adhoc_research": "adhoc",
    "tracking": "event_response",
}

DEFAULT_POOL = "coverage"


def _pool_name(value: Any) -> str:
    if not isinstance(value, str) or value not in POOL_NAMES:
        raise BudgetPoolError(f"{value!r} is not one of {', '.join(POOL_NAMES)}")
    return value


def pool_for_operation(operation: str) -> str:
    """The pool a lane operation spends from.

    A lane's own ``LaneSpec.budget_pool`` wins; then this module's table; then
    coverage, which is what every lane was before pools existed.
    """

    from .lane_registry import lane_for_operation

    spec = lane_for_operation(operation)
    if spec is not None and getattr(spec, "budget_pool", None):
        return _pool_name(spec.budget_pool)
    return LANE_POOLS.get(operation, DEFAULT_POOL)


def pool_for_purpose(purpose: str) -> str:
    """The pool a cockpit-shaped model call spends from."""

    return PURPOSE_POOLS.get(purpose, DEFAULT_POOL)


# C2b: the third shape of a paid model call.  A cockpit call is admitted by
# purpose and a tick lane by operation; a Tier-1 bounded planner loop is
# neither -- it is one loop's model call, and which pool it spends from is a
# property of why the loop exists.  P14e's inquiry loops are the ad-hoc
# research the 25% pool was sized for; every other loop is the coverage work
# the mission was written to do.
def _inquiry_admission_source() -> str:
    # Imported lazily: bounded_planner_loop reads this module for pool names,
    # and the admission source is one string that must not be two.
    from .bounded_planner_loop import INQUIRY_ADMISSION_SOURCE

    return INQUIRY_ADMISSION_SOURCE


LOOP_ADMISSION_POOLS: dict[str, str] = {
    _inquiry_admission_source(): "adhoc",
}


def pool_for_loop(loop: Mapping[str, Any] | None) -> str:
    """The pool one bounded planner loop's model calls spend from.

    A loop with no ``admission`` block predates P14e and is the owner's own
    Tier-1 research, which is coverage.  Taking the source from the loop record
    rather than from the caller is what keeps the driver from being able to
    name a cheaper pool than the loop belongs to.
    """

    admission = (loop or {}).get("admission") or {}
    if not isinstance(admission, Mapping):
        return DEFAULT_POOL
    return LOOP_ADMISSION_POOLS.get(admission.get("source"), DEFAULT_POOL)


def lane_pools() -> dict[str, str]:
    """Every registered lane operation and the pool it drinks from."""

    from .lane_registry import registered_lanes

    return {spec.operation: pool_for_operation(spec.operation)
            for spec in registered_lanes()}


def classify_legacy_work_order(work_order_ref: str) -> str:
    """The pool an admission made before C2 would have belonged to.

    Only for reading history: a WorkOrder id carries its purpose
    (``work:cockpit-plan-...``) or its lane (``work:extraction-...``), which is
    enough to say which pool a day's existing spend *would* have fallen into.
    Live admissions never guess -- they carry the pool they were admitted
    under -- and the readers report unpooled spend as unpooled rather than
    quietly folding it into coverage.
    """

    ref = str(work_order_ref or "")
    if ref.startswith("work:cockpit-"):
        rest = ref[len("work:cockpit-"):]
        for purpose in sorted(PURPOSE_POOLS, key=len, reverse=True):
            if rest.startswith(purpose + "-"):
                return PURPOSE_POOLS[purpose]
        return DEFAULT_POOL
    for marker, pool in (
        ("research-task", "adhoc"), ("adhoc", "adhoc"),
        ("event", "event_response"), ("tracking", "event_response"),
        ("catalog", "maintenance"), ("retire", "maintenance"),
        ("quality", "maintenance"), ("claim-index", "maintenance"),
    ):
        if marker in ref:
            return pool
    return DEFAULT_POOL


# -- caps -------------------------------------------------------------------

def pool_caps(budget: Mapping[str, Any]) -> dict[str, Any]:
    """The four caps for one day, in micros, from a mission's budget block.

    Declared caps are USD, one per pool, summing to at most the mission's
    ``max_daily_cost_usd``; a declaration that names an unknown pool, omits
    one, goes negative or oversubscribes the day is refused rather than
    silently repaired, because a budget that quietly means something else than
    it says is worse than no budget.
    """

    try:
        daily = Decimal(str(budget["max_daily_cost_usd"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BudgetPoolError("mission budget has no max_daily_cost_usd") from exc
    if daily < 0:
        raise BudgetPoolError("max_daily_cost_usd must not be negative")
    day_cap_micros = int((daily * 1_000_000).to_integral_value())
    declared = budget.get(MISSION_POOLS_FIELD)
    if declared is None:
        caps = {}
        for name in POOL_NAMES:
            share = DEFAULT_SHARES[name]
            caps[name] = int(((daily * share) * 1_000_000).to_integral_value())
        return {
            "schema_version": SCHEMA_VERSION,
            "day_cap_micros": day_cap_micros,
            "caps_micros": caps,
            "shares": {name: str(DEFAULT_SHARES[name]) for name in POOL_NAMES},
            "defaulted": True,
            "source": "budget_pools.DEFAULT_SHARES",
        }
    if not isinstance(declared, Mapping) or set(declared) != set(POOL_NAMES):
        raise BudgetPoolError(
            "mission budget pools must name exactly " + ", ".join(POOL_NAMES)
        )
    caps = {}
    total = Decimal(0)
    for name in POOL_NAMES:
        value = declared[name]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise BudgetPoolError(f"pool {name} cap must be a number of dollars")
        try:
            amount = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - Decimal raises several types
            raise BudgetPoolError(f"pool {name} cap must be a number") from exc
        if amount < 0:
            raise BudgetPoolError(f"pool {name} cap must not be negative")
        total += amount
        caps[name] = int((amount * 1_000_000).to_integral_value())
    if total > daily:
        raise BudgetPoolError(
            "the mission's pool caps exceed its max_daily_cost_usd"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "day_cap_micros": day_cap_micros,
        "caps_micros": caps,
        "shares": {
            name: str((Decimal(caps[name]) / Decimal(1_000_000) / daily)
                      .quantize(Decimal("0.0001"))) if daily > 0 else "0"
            for name in POOL_NAMES
        },
        "defaulted": False,
        "source": f"mission.budget.{MISSION_POOLS_FIELD}",
    }


def mission_pool_scope(
    mission: Mapping[str, Any], *, pool: str | None = None,
    purpose: str | None = None, operation: str | None = None,
    lane: str | None = None,
) -> dict[str, Any]:
    """The pool keys to merge into a ``mission_binding`` for one admission.

    The caller says which pool by name, or by the purpose or lane operation it
    is acting for; saying none of them is coverage.  The caps travel with the
    admission because the day ledger is a separate authority from the mission
    and must not have to open the Core to learn what a mission version said.
    """

    if pool is None:
        if purpose is not None:
            pool = pool_for_purpose(purpose)
        elif operation is not None:
            pool = pool_for_operation(operation)
        else:
            pool = DEFAULT_POOL
    caps = pool_caps(mission["budget"])
    scope: dict[str, Any] = {
        "pool": _pool_name(pool),
        "pool_caps_micros": dict(caps["caps_micros"]),
    }
    who = lane or operation or purpose
    if who:
        scope["pool_lane"] = str(who)
    return scope


# -- the borrow rule --------------------------------------------------------

def day_fraction_elapsed(now: datetime, day: str) -> float:
    """How much of ``day`` (UTC) has passed at ``now``; 1.0 for a past day."""

    moment = now.astimezone(timezone.utc)
    today = moment.date().isoformat()
    if day < today:
        return 1.0
    if day > today:
        return 0.0
    return (
        moment.hour * 3600 + moment.minute * 60 + moment.second
    ) / 86400.0


def borrow_open(now: datetime, day: str) -> bool:
    """Whether an owning pool's unspent share is spare yet."""

    return day_fraction_elapsed(now, day) > BORROW_AFTER_DAY_FRACTION


def borrowable_micros(
    pool: str, spent: Mapping[str, int], caps: Mapping[str, int],
    *, now: datetime, day: str, lent: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """What ``pool`` may borrow from each other pool right now.

    Empty for every pool but coverage, and empty for coverage until the day is
    more than half over: an event pool that has not been touched by eleven in
    the morning is not spare capacity, it is a quiet morning.  A lender's
    offer is its cap less what it has spent *and* less what it has already
    lent, or the same idle dollar would be lent once an hour.
    """

    if pool not in BORROWING_POOLS or not borrow_open(now, day):
        return {}
    offers: dict[str, int] = {}
    for name in POOL_NAMES:
        if name == pool:
            continue
        free = (int(caps.get(name, 0)) - int(spent.get(name, 0))
                - int((lent or {}).get(name, 0)))
        if free > 0:
            offers[name] = free
    return offers


def allocate_borrow(need: int, offers: Mapping[str, int]) -> dict[str, int]:
    """Take ``need`` micros from the offers, in pool order, or nothing.

    All or nothing: a partial loan would admit a call the pool cannot cover
    and leave the shortfall to whichever cap noticed second.
    """

    if need <= 0:
        return {}
    taken: dict[str, int] = {}
    remaining = need
    for name in POOL_NAMES:
        available = int(offers.get(name, 0))
        if available <= 0:
            continue
        amount = min(available, remaining)
        taken[name] = amount
        remaining -= amount
        if remaining <= 0:
            break
    if remaining > 0:
        return {}
    return taken


# -- the ledger side --------------------------------------------------------

def apply_pool_migration(connection: sqlite3.Connection) -> None:
    """Add C2's pool columns and rejection table to a day-ledger database.

    Additive and idempotent: three nullable columns on tables that already
    exist, and one new table.  Nothing already written changes, and no
    ``content_hash`` moves -- the pool is a column and a binding record, never
    a field inside a hashed wire, precisely so that an admission written last
    week still verifies today.
    """

    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, column, ddl in (
        ("thesis_impact_day_admissions", "pool", "pool TEXT"),
        ("thesis_impact_day_admissions", "borrowed_from", "borrowed_from TEXT"),
        ("thesis_impact_day_admissions", "pool_lane", "pool_lane TEXT"),
        ("thesis_impact_day_settlements", "pool", "pool TEXT"),
    ):
        existing = {
            row["name"] for row in
            connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing or column in existing:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_thesis_impact_admission_pool "
        "ON thesis_impact_day_admissions(day, pool)"
    )


def has_pool_columns(connection: Any) -> bool:
    """Whether this database has been migrated (a read-only copy may not be)."""

    try:
        rows = connection.execute(
            "PRAGMA table_info(thesis_impact_day_admissions)"
        ).fetchall()
    except sqlite3.Error:
        return False
    return "pool" in {row["name"] for row in rows}


def day_pool_spend(
    connection: Any, *, day: str, mission_ref: str | None = None,
) -> dict[str, int]:
    """Committed micros per pool for one day: settled where settled, reserved
    where still open, which is the conservative reading the day cap already
    uses.  Admissions with no pool are reported under ``unpooled``.
    """

    totals: dict[str, int] = {name: 0 for name in POOL_NAMES}
    totals["unpooled"] = 0
    if not has_pool_columns(connection):
        return totals
    rows = connection.execute(
        "SELECT a.pool AS pool, a.reserved_micros AS reserved, "
        " s.actual_micros AS actual, b.mission_ref AS mission_ref "
        "FROM thesis_impact_day_admissions a "
        "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
        "LEFT JOIN model_mission_budget_bindings b ON b.admission_id=a.admission_id "
        "WHERE a.day=?", (day,),
    ).fetchall()
    for row in rows:
        if mission_ref is not None and row["mission_ref"] not in (None, mission_ref):
            continue
        micros = int(row["reserved"] if row["actual"] is None else row["actual"])
        name = row["pool"] if row["pool"] in POOL_NAMES else "unpooled"
        totals[name] = totals.get(name, 0) + micros
    return totals


def day_pool_loans(
    connection: Any, *, day: str, mission_ref: str | None = None,
) -> dict[str, dict[str, int]]:
    """Today's loans, read from both ends: ``lent`` out and ``borrowed`` in.

    Both ends are needed and for opposite reasons, and getting only one of
    them was this module's worst bug.

    ``lent`` keeps a lender from offering the same idle dollar twice: its own
    ``spent`` never moves when coverage borrows from it, so without this the
    same dollar could be lent once an hour.

    ``borrowed`` is what a borrower's cap becomes.  A pool that has borrowed
    ten micros has spent ten micros it did not own; if the next admission
    compares that spend against the *unraised* cap it sees the same overage
    again and borrows a second time to cover a shortfall that was already
    covered.  Four ten-micro admissions against an exhausted pool borrowed a
    hundred.  The cap and the spend have to be read from the same side of the
    loan.
    """

    loans: dict[str, dict[str, int]] = {
        "lent": {name: 0 for name in POOL_NAMES},
        "borrowed": {name: 0 for name in POOL_NAMES},
    }
    detail: dict[str, dict[str, int]] = {name: {} for name in POOL_NAMES}
    loans["detail"] = detail  # type: ignore[assignment]
    if not has_pool_columns(connection):
        return loans
    sql = (
        "SELECT a.pool AS pool, a.borrowed_from AS borrowed_from, "
        " b.mission_ref AS mission_ref "
        "FROM thesis_impact_day_admissions a "
        "LEFT JOIN model_mission_budget_bindings b ON b.admission_id=a.admission_id "
        "WHERE a.day=? AND a.borrowed_from IS NOT NULL"
    )
    params: list[Any] = [day]
    if mission_ref is not None:
        # An admission with no binding at all predates mission binding and
        # belongs to no mission's pools; it can never have borrowed, but the
        # filter says so rather than relying on that.
        sql += " AND b.mission_ref=?"
        params.append(mission_ref)
    for row in connection.execute(sql, params).fetchall():
        try:
            lenders = json.loads(row["borrowed_from"])
        except ValueError:
            continue
        for lender, micros in (lenders or {}).items():
            loans["lent"][lender] = loans["lent"].get(lender, 0) + int(micros)
            if row["pool"] in POOL_NAMES:
                loans["borrowed"][row["pool"]] += int(micros)
                bucket = detail[row["pool"]]
                bucket[lender] = bucket.get(lender, 0) + int(micros)
    return loans


def day_pool_spend_at(
    path: str | Path, *, day: str, mission_ref: str | None = None,
) -> dict[str, int]:
    """:func:`day_pool_spend` against a database file that may not be there.

    The controller tick uses this to record what the day's pools moved by, and
    a tick must not fail because a ledger it does not own is absent.
    """

    target = Path(path)
    if not target.is_file():
        return {}
    from .readonly_sqlite import connect_read_only

    try:
        connection = connect_read_only(str(target))
    except Exception:  # noqa: BLE001 - an unreadable ledger is not a tick failure
        return {}
    try:
        connection.row_factory = sqlite3.Row
        return day_pool_spend(connection, day=day, mission_ref=mission_ref)
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def record_pool_rejection(
    cursor: sqlite3.Cursor, rejection: Mapping[str, Any],
) -> None:
    """Append one pool refusal, so the cockpit can list who ran out today.

    Deliberately *not* the day ledger's own rejection table: that one is a
    permanent verdict on an admission identity ("a rejected admission cannot
    later be admitted"), which is right for an owner cap that was exceeded and
    wrong for a pool that refills at midnight.  A pool refusal is a fact about
    a day, not a verdict on a WorkOrder.
    """

    cursor.execute(
        "INSERT OR IGNORE INTO model_budget_pool_rejections("
        "rejection_id,day,mission_ref,pool,pool_lane,work_order_ref,attempt_number,"
        "phase,reserved_micros,pool_spent_micros,pool_cap_micros,"
        "borrowable_micros,record_json,content_hash,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rejection["rejection_id"], rejection["day"], rejection["mission_ref"],
            rejection["pool"], rejection.get("pool_lane"),
            rejection["work_order_ref"], rejection["attempt_number"],
            rejection["phase"], rejection["reserved_micros"],
            rejection["spent"], rejection["cap"], rejection["borrowable_micros"],
            canonical_json(rejection), rejection["content_hash"],
            rejection["created_at"],
        ),
    )


def pool_rejection_wire(
    *, day: str, mission_ref: str, pool: str, pool_lane: str | None,
    work_order_ref: str, attempt_number: int, phase: str, reserved_micros: int,
    spent: int, cap: int, borrowable: int, created_at: str,
) -> dict[str, Any]:
    """The returned-and-recorded shape of one ``pool_exhausted`` refusal."""

    identity = {
        "day": day, "mission_ref": mission_ref, "pool": pool,
        "work_order_ref": work_order_ref, "attempt_number": attempt_number,
        "phase": phase,
    }
    wire = {
        "schema_version": SCHEMA_VERSION,
        "status": "rejected",
        "reason": POOL_EXHAUSTED_REASON,
        "rejection_id": "budget-pool-rejection:" + content_hash(identity)[:32],
        **identity,
        "pool_lane": pool_lane,
        "reserved_micros": reserved_micros,
        "spent": spent,
        "cap": cap,
        "borrowable_micros": borrowable,
        "created_at": created_at,
    }
    wire["content_hash"] = content_hash(wire)
    return wire


def pool_decision(
    cursor: Any, *, mission_binding: Mapping[str, Any], day: str,
    reserved_micros: int, work_order_ref: str, attempt_number: int, phase: str,
    now: datetime,
) -> dict[str, Any]:
    """Admit, borrow, or refuse one reservation against its pool.

    Called from inside the day ledger's own admission transaction, on the same
    cursor, so the spend it reads is the spend the insert will join -- there is
    no window in which two lanes both see room for the last dollar.

    Returns ``{"status": "admitted", "pool": ..., "borrowed_from": {...}|None}``
    or the ``pool_exhausted`` rejection wire.  A binding with no ``pool`` (every
    caller that predates C2) is admitted untouched: pools are opt-in per
    admission, and an unpooled admission is reported as unpooled rather than
    charged to a pool nobody chose.
    """

    pool = mission_binding.get("pool")
    caps = mission_binding.get("pool_caps_micros")
    if pool is None or caps is None:
        return {"status": "admitted", "pool": None, "borrowed_from": None}
    pool = _pool_name(pool)
    mission_ref = mission_binding["mission_ref"]
    spend = day_pool_spend(cursor, day=day, mission_ref=mission_ref)
    loans = day_pool_loans(cursor, day=day, mission_ref=mission_ref)
    lent, borrowed_in = loans["lent"], loans["borrowed"]
    # Both sides of every loan already made today, or the same overage is
    # borrowed for again on the next admission: what a pool borrowed raises
    # its cap, what it lent lowers what it has left.
    cap = int(caps.get(pool, 0)) + int(borrowed_in.get(pool, 0))
    spent = int(spend.get(pool, 0)) + int(lent.get(pool, 0))
    shortfall = spent + int(reserved_micros) - cap
    borrowed: dict[str, int] = {}
    if shortfall > 0:
        offers = borrowable_micros(
            pool, spend, caps, now=now, day=day, lent=lent,
        )
        borrowed = allocate_borrow(shortfall, offers)
        if not borrowed:
            return pool_rejection_wire(
                day=day, mission_ref=mission_ref, pool=pool,
                pool_lane=mission_binding.get("pool_lane"),
                work_order_ref=work_order_ref, attempt_number=attempt_number,
                phase=phase, reserved_micros=int(reserved_micros),
                spent=spent, cap=cap,
                borrowable=sum(offers.values()),
                created_at=now.astimezone(timezone.utc).isoformat(
                    timespec="microseconds"),
            )
    return {
        "status": "admitted", "pool": pool,
        "borrowed_from": dict(sorted(borrowed.items())) or None,
    }


def pool_rejections(
    connection: Any, *, day: str, mission_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Every pool refusal recorded on one day, oldest first."""

    try:
        sql = "SELECT record_json FROM model_budget_pool_rejections WHERE day=?"
        params: list[Any] = [day]
        if mission_ref is not None:
            sql += " AND mission_ref=?"
            params.append(mission_ref)
        rows = connection.execute(sql + " ORDER BY created_at", params).fetchall()
    except sqlite3.Error:
        return []
    return [json.loads(row["record_json"]) for row in rows]


def pool_status(
    store: Any, *, mission: Mapping[str, Any] | None = None,
    mission_ref: str | None = None, day: str,
    caps: Mapping[str, int] | None = None, now: datetime | None = None,
    max_exhausted_lanes: int = MAX_EXHAUSTED_LANES,
) -> dict[str, Any]:
    """Per pool: cap, spent, borrowed, remaining -- and who ran out today.

    Takes either the mission (whose budget block supplies the caps and the
    reference) or an explicit reference and cap table, so the cockpit can read
    a day without opening the Core.
    """

    if mission is not None:
        derived = pool_caps(mission["budget"])
        caps = caps if caps is not None else derived["caps_micros"]
        mission_ref = mission_ref or mission.get("mission_ref")
        defaulted = derived["defaulted"]
    else:
        defaulted = caps is None
        caps = caps or {}
    if caps is None:
        caps = {}
    moment = now or datetime.now(timezone.utc)
    connection = getattr(store, "connection", store)
    spend = day_pool_spend(connection, day=day, mission_ref=mission_ref)
    loans = day_pool_loans(connection, day=day, mission_ref=mission_ref)
    borrowed = loans["detail"]
    lent = loans["lent"]
    refusals = pool_rejections(connection, day=day, mission_ref=mission_ref)
    pools: dict[str, Any] = {}
    for name in POOL_NAMES:
        cap = int(caps.get(name, 0))
        spent = int(spend.get(name, 0))
        borrowed_total = int(loans["borrowed"].get(name, 0))
        pools[name] = {
            "cap_micros": cap,
            "spent_micros": spent,
            "borrowed_micros": borrowed_total,
            "borrowed_from": dict(sorted(borrowed[name].items())),
            "lent_micros": int(lent.get(name, 0)),
            "remaining_micros": max(
                cap + borrowed_total - spent - int(lent.get(name, 0)), 0),
            "exhausted": bool(
                any(item["pool"] == name for item in refusals)
            ),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "day": day,
        "mission_ref": mission_ref,
        "caps_defaulted": bool(defaulted),
        "borrow_open": borrow_open(moment, day),
        "pools": pools,
        "unpooled_micros": int(spend.get("unpooled", 0)),
        # The most recent few, newest first, and the count.  A pool that is
        # spent refuses every lane that asks for the rest of the day, so on a
        # bad day this list is thousands of rows long and a cockpit panel that
        # renders all of them is a cockpit panel nobody opens twice.  The
        # count is the number that matters; the rows are the examples.
        "exhausted_lane_count": len(refusals),
        "exhausted_lanes": [
            {
                "lane": item.get("pool_lane"),
                "pool": item["pool"],
                "work_order_ref": item["work_order_ref"],
                "at": item["created_at"],
                "spent": item["spent"],
                "cap": item["cap"],
            }
            for item in list(reversed(refusals))[:max(0, int(max_exhausted_lanes))]
        ],
    }


def summarise_shares(caps_micros: Mapping[str, int]) -> dict[str, str]:
    """Caps as human dollars, for a report or a cockpit tile."""

    return {
        name: str((Decimal(int(caps_micros.get(name, 0))) / Decimal(1_000_000))
                  .quantize(Decimal("0.000001")))
        for name in POOL_NAMES
    }


__all__ = [
    "BORROWING_POOLS",
    "BORROW_AFTER_DAY_FRACTION",
    "BudgetPoolError",
    "DEFAULT_POOL",
    "DEFAULT_SHARES",
    "LANE_POOLS",
    "LOOP_ADMISSION_POOLS",
    "MAX_EXHAUSTED_LANES",
    "MISSION_POOLS_FIELD",
    "POOL_EXHAUSTED_REASON",
    "POOL_EXHAUSTED_STATUS",
    "POOL_NAMES",
    "PURPOSE_POOLS",
    "SCHEMA_VERSION",
    "allocate_borrow",
    "apply_pool_migration",
    "borrow_open",
    "borrowable_micros",
    "classify_legacy_work_order",
    "day_fraction_elapsed",
    "day_pool_loans",
    "day_pool_spend",
    "day_pool_spend_at",
    "has_pool_columns",
    "lane_pools",
    "mission_pool_scope",
    "pool_caps",
    "pool_decision",
    "pool_for_loop",
    "pool_for_operation",
    "pool_for_purpose",
    "pool_rejection_wire",
    "pool_rejections",
    "pool_status",
    "record_pool_rejection",
    "summarise_shares",
]
