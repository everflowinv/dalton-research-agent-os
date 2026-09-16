"""C2-1: per-window isolation, window de-duplication, and the tick's batch size.

Three decisions the extraction lane used to make at the level of the whole
run, which is why 411 of 432 live runs failed and 30,372 window reads produced
517 records.

**Whose failure is it.**  The child stopped the entire batch at the first
window whose model execution failed, under the (correct) rule that a call
whose cost is unknown must never be retried blind.  But the live failures are
overwhelmingly *not* of that kind: 565 of 991 never reserved a micro at all
and 66 more had already settled their exact cost.  For those the send is a
settled fact, so ending the batch buys no safety -- it only throws away the
remaining twenty-nine windows.  :func:`classify_window_failure` separates the
two, and only the cost-uncertain kind still ends the batch with its
reservation left open and un-retried.

**Reading the same dead window forever.**  A window whose formal result is a
cached terminal failure is replayed for free on every later run -- and is
never re-attempted, because the child only calls the model when a window has
*no* result.  So it can never succeed, its review can never complete, and the
child pays the full cost of re-rendering the document to learn that again.
Live, 24% of window reads were exactly this.  :class:`ExtractionWindowLedger`
writes that fact down once, with the offset to continue from, so the next run
steps over it without touching the bytes.

**How many windows fit.**  ``max_windows_per_tick`` is a ceiling, not a plan.
When the day's document-reading quota is barely touched -- live: 246 of 2000 --
a lane that stops at a typed number is choosing to read less than the owner
authorised.  :func:`windows_for_tick` spends the quota that is actually left,
never above the configured ceiling.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import authorization_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA = Path(__file__).with_name("document_extraction_window_schema.sql")
_SHA = re.compile(r"[0-9a-f]{64}")

# What ``budget_status`` says about money that has definitely stopped moving.
#
#   not_reserved -- the refusal happened before any admission was written, so
#                   no call was sent and no headroom is held.
#   rejected     -- the day's pool refused the reservation; again, no call.
#   settled      -- the call happened and its exact cost is recorded.
#
# ``reserved`` is the one that is missing on purpose: an open reservation
# means the lane does not know whether the provider was paid.
CERTAIN_BUDGET_STATUSES = frozenset({"not_reserved", "rejected", "settled"})

# Error codes that say in so many words that the outcome of the send is
# unknown.  They override the budget status: an explicit "I do not know"
# outranks a ledger that merely has not been updated yet.
UNCERTAIN_ERROR_CODES = frozenset({
    "POST_SEND_RESULT_UNKNOWN",
    "MODEL_COST_EXCEEDED_RESERVATION",
})

# A refusal about the day rather than about the bytes.  The pool refills at
# the UTC midnight boundary, so the window is deferred, not excluded.
DEFERRED_BUDGET_STATUSES = frozenset({"rejected"})


class ExtractionWindowError(ValueError):
    pass


def classify_window_failure(
    *, error_code: Any, budget_status: Any
) -> dict[str, Any]:
    """Whether this window's failure is safe to isolate, and for how long.

    Returns ``cost_certain`` -- may the run continue past this window at all --
    together with the exclusion ``failure_class`` to record when it may.

    The discipline the 2026-09-14 design note fixed is kept exactly: a window
    whose spend is unknown still ends the batch, keeps its reservation, and is
    never retried under a new identity.  What changes is that a window which
    provably cost nothing, or which has already settled, no longer takes the
    other twenty-nine windows down with it.
    """

    code = error_code if isinstance(error_code, str) and error_code else None
    status = budget_status if isinstance(budget_status, str) and budget_status else None
    if code in UNCERTAIN_ERROR_CODES:
        return {
            "cost_certain": False, "failure_class": None,
            "error_code": code, "budget_status": status,
            "reason": f"provider outcome is unknown ({code}); the reservation stays open",
        }
    if status not in CERTAIN_BUDGET_STATUSES:
        return {
            "cost_certain": False, "failure_class": None,
            "error_code": code, "budget_status": status,
            "reason": (
                "the model reservation for this window has not settled"
                f" (budget={status or 'unknown'}); the reservation stays open"
            ),
        }
    deferred = status in DEFERRED_BUDGET_STATUSES
    return {
        "cost_certain": True,
        "failure_class": "deferred" if deferred else "permanent",
        "error_code": code,
        "budget_status": status,
        "reason": (
            "the day's budget refused this window; it is retried after the day rolls over"
            if deferred else
            f"this window failed with a settled cost (budget={status}, error={code or 'none'})"
        ),
    }


def replayed_window_failure(*, error_code: Any) -> dict[str, Any]:
    """The classification of a window whose *cached* result is a failure.

    Nothing is sent, so nothing can be uncertain.  It is permanent because the
    child only ever calls the model for a window that has no result at all: a
    window holding a terminal failure will hold it forever, and the review it
    belongs to can therefore never complete.
    """

    code = error_code if isinstance(error_code, str) and error_code else None
    return {
        "cost_certain": True,
        "failure_class": "permanent",
        "error_code": code,
        "budget_status": "replayed",
        "reason": (
            "a terminal result for this window is already stored"
            f" (error={code or 'none'}); replaying it can never succeed"
        ),
    }


class ExtractionWindowLedger:
    """The windows this lane has proved it cannot read, and what to skip to."""

    def __init__(self, connection: sqlite3.Connection,
                 *, clock: Any = None) -> None:
        self.connection = connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._authorization = authorization_flag(
            connection, "dalton_document_extraction_window_authorized")
        self.connection.executescript(_SCHEMA.read_text(encoding="utf-8"))

    # -- read ---------------------------------------------------------------

    def exclusions(
        self, review_id: str, source_review_hash: str,
        *, model_config_hash: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        """offset -> the active exclusion for this exact review revision.

        Keyed by the review hash as well as the id, so a review whose row
        changed (a re-acquisition, a reopen) is read again from scratch rather
        than inheriting a verdict about different bytes.

        ``model_config_hash`` does the same for the other half of the identity:
        a window excluded because one model could not produce a parseable
        answer is not excluded for the next model.  A caller that does not know
        its configuration passes ``None`` and every exclusion applies, which is
        the conservative reading.
        """

        now = self.clock().astimezone(timezone.utc)
        active: dict[int, dict[str, Any]] = {}
        for row in self.connection.execute(
            "SELECT * FROM document_extraction_window_exclusions "
            "WHERE review_id=? AND source_review_hash=?",
            (str(review_id), str(source_review_hash)),
        ).fetchall():
            record = dict(row)
            if (model_config_hash is not None
                    and record["model_config_hash"] is not None
                    and record["model_config_hash"] != model_config_hash):
                continue  # a different model; the verdict does not carry over
            if record["failure_class"] == "deferred":
                retry_after = record["retry_after"]
                if not retry_after:
                    continue
                try:
                    moment = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if moment <= now:
                    continue  # the day rolled over; read it again
            active[int(record["window_offset"])] = record
        return active

    def count_active(self) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM document_extraction_window_exclusions "
            "WHERE failure_class='permanent' OR retry_after > ?",
            (self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds"),),
        ).fetchone()[0])

    # -- write --------------------------------------------------------------

    def exclude(
        self,
        *,
        review_id: str,
        source_review_hash: str,
        offset: int,
        next_offset: int | None,
        classification: Mapping[str, Any],
        document_ref: str | None = None,
        company_ref: str | None = None,
        work_order_ref: str | None = None,
        model_config_hash: str | None = None,
        recorded: int = 0,
    ) -> dict[str, Any]:
        """Write down that this window failed deterministically, once.

        Idempotent by ``(review_id, source_review_hash, offset)``: a second
        pass over the same dead window bumps its hit count and its timestamp
        instead of writing a second verdict.
        """

        if not isinstance(review_id, str) or not review_id:
            raise ExtractionWindowError("review_id is required")
        if not isinstance(source_review_hash, str) or _SHA.fullmatch(source_review_hash) is None:
            raise ExtractionWindowError("source_review_hash must be a SHA-256 hex digest")
        if type(offset) is not int or offset < 0:
            raise ExtractionWindowError("offset must be a non-negative integer")
        if next_offset is not None and (type(next_offset) is not int or next_offset <= offset):
            raise ExtractionWindowError("next_offset must be greater than offset")
        if not classification.get("cost_certain"):
            raise ExtractionWindowError(
                "only a window whose spend is settled may be excluded")
        failure_class = classification.get("failure_class")
        if failure_class not in ("permanent", "deferred"):
            raise ExtractionWindowError("failure_class must be permanent or deferred")
        if type(recorded) is not int or recorded < 0:
            raise ExtractionWindowError("recorded must be a non-negative integer")
        if recorded:
            raise ExtractionWindowError(
                "a window that recorded something is evidence, not an exclusion")
        now = self.clock().astimezone(timezone.utc)
        at = now.isoformat(timespec="microseconds")
        retry_after = None
        if failure_class == "deferred":
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            retry_after = tomorrow.isoformat(timespec="microseconds")
        exclusion_id = "document-extraction-window-exclusion:" + content_hash({
            "review_id": review_id, "source_review_hash": source_review_hash,
            "offset": offset,
        })[:32]
        self._authorization.authorized = True
        try:
            self.connection.execute(
                "INSERT INTO document_extraction_window_exclusions("
                "exclusion_id,review_id,source_review_hash,window_offset,next_offset,"
                "document_ref,company_ref,work_order_ref,error_code,budget_status,"
                "model_config_hash,failure_class,retry_after,recorded,reason,hit_count,"
                "created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(review_id,source_review_hash,window_offset) DO UPDATE SET "
                "hit_count=hit_count+1, updated_at=excluded.updated_at, "
                "next_offset=COALESCE(excluded.next_offset,next_offset), "
                "model_config_hash=excluded.model_config_hash, "
                "failure_class=excluded.failure_class, retry_after=excluded.retry_after, "
                "error_code=excluded.error_code, budget_status=excluded.budget_status, "
                "reason=excluded.reason",
                (exclusion_id, review_id, source_review_hash, offset, next_offset,
                 document_ref, company_ref, work_order_ref,
                 classification.get("error_code"), classification.get("budget_status"),
                 model_config_hash, failure_class, retry_after, recorded,
                 str(classification.get("reason") or "deterministic window failure"), at, at),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self._authorization.authorized = False
        row = self.connection.execute(
            "SELECT * FROM document_extraction_window_exclusions WHERE exclusion_id=?",
            (exclusion_id,),
        ).fetchone()
        return dict(row)


def windows_for_tick(
    *,
    configured: int,
    awaiting: int,
    daily_cap: int | None,
    used_today: int,
    ticks_remaining: int,
    floor: int = 1,
    ceiling: int = 50,
) -> dict[str, Any]:
    """How many windows this tick may read, given the quota still unspent.

    ``configured`` is the owner's per-tick ceiling and is never exceeded; the
    only thing derived here is whether the lane may use all of it.  A day with
    almost all of its reading quota left has no reason to hold back, and a day
    that is nearly spent must not burn the remainder in one tick.

    Returns the number *and its arithmetic*, because a lane that silently
    resized its own batch would be worse than one that never grew.
    """

    if type(configured) is not int or isinstance(configured, bool) or configured < 1:
        raise ExtractionWindowError("configured windows per tick must be a positive integer")
    if type(ticks_remaining) is not int or isinstance(ticks_remaining, bool) or ticks_remaining < 1:
        raise ExtractionWindowError("ticks_remaining must be a positive integer")
    if type(awaiting) is not int or isinstance(awaiting, bool) or awaiting < 0:
        raise ExtractionWindowError("awaiting must be a non-negative integer")
    bounded = max(floor, min(ceiling, configured))
    if daily_cap is None:
        return {"schema_version": SCHEMA_VERSION, "windows": bounded,
                "bound_by": "configured", "quota_remaining": None,
                "configured": configured, "ticks_remaining": ticks_remaining}
    if type(daily_cap) is not int or isinstance(daily_cap, bool) or daily_cap < 0:
        raise ExtractionWindowError("daily_cap must be a non-negative integer or None")
    if type(used_today) is not int or isinstance(used_today, bool) or used_today < 0:
        raise ExtractionWindowError("used_today must be a non-negative integer")
    remaining = max(0, daily_cap - used_today)
    if remaining == 0:
        return {"schema_version": SCHEMA_VERSION, "windows": floor,
                "bound_by": "daily_quota_spent", "quota_remaining": 0,
                "configured": configured, "ticks_remaining": ticks_remaining}
    # The cap counts *documents started today*, and one tick can start at most
    # one document per window, so the quota binds a tick only once fewer
    # documents are left than the tick was configured for.  Anything cleverer
    # -- spreading the remainder evenly over the day's remaining ticks -- would
    # divide a document count by a window count and get a number that means
    # nothing, which is how a lane with 1,754 reads left would talk itself down
    # to fourteen windows.
    by_quota = remaining
    windows = max(floor, min(ceiling, min(bounded, by_quota)))
    # The queue itself is the last bound: asking for thirty windows against
    # four waiting reviews is a number nobody can spend.
    bound_by = "configured" if windows == bounded else "daily_quota"
    return {
        "schema_version": SCHEMA_VERSION, "windows": windows, "bound_by": bound_by,
        "quota_remaining": remaining, "windows_by_quota": by_quota,
        "configured": configured, "ticks_remaining": ticks_remaining,
        "awaiting": awaiting,
    }


# G1: where a review no plan named sorts against one a plan did.  Far above
# any real rank, so "the plan said nothing about this" always falls behind
# "the plan asked for this" -- and, between two documents the plan is equally
# silent about, changes nothing at all about the order they already had.
PLAN_UNDIRECTED_RANK = 1_000_000

# The subject a directive names when it is about the industry rather than one
# company.  An industry document is filed under whichever company's query
# returned it, so an industry directive has to match every company's review of
# that kind or it would match nothing.
PLAN_ANY_COMPANY = "*"


def plan_reading_ranks(
    priorities: Sequence[Mapping[str, Any]] | None,
    *,
    item_specs: Mapping[str, Sequence[str]],
    industry_ref: str | None = None,
) -> dict[str, dict[tuple[str, str], int]]:
    """Translate the plan's reading directives into (company, kind) ranks.

    A directive names a checklist item -- ``annual_report`` for Accenture --
    and not a document: which held document satisfies it belongs to the
    extraction lane, the only thing that knows what it holds and what reading
    it costs.  This is that translation and nothing else.  The plan's own rank
    is carried onto every document kind the named item is satisfied by, so the
    queue is reordered by the planner's judgement rather than by a second one
    invented here.

    ``numeric`` is the same map restricted to ``extract_figures``, because a
    plan that asks for figures from the 10-K is not asking the prose pass for
    anything and must not drag the figures pass around by its prose directives.
    """

    ranks: dict[tuple[str, str], int] = {}
    numeric: dict[tuple[str, str], int] = {}
    for entry in priorities or ():
        if not isinstance(entry, Mapping):
            continue
        company, item = entry.get("company_ref"), entry.get("item_ref")
        rank = entry.get("rank")
        if not isinstance(company, str) or not isinstance(item, str):
            continue
        if type(rank) is not int or isinstance(rank, bool) or rank < 1:
            continue
        subject = (PLAN_ANY_COMPANY
                   if industry_ref and company == industry_ref else company)
        for spec in item_specs.get(item) or ():
            if not isinstance(spec, str) or not spec:
                continue
            key = (subject, spec)
            if rank < ranks.get(key, PLAN_UNDIRECTED_RANK):
                ranks[key] = rank
            if entry.get("action") != "extract_figures":
                continue
            if rank < numeric.get(key, PLAN_UNDIRECTED_RANK):
                numeric[key] = rank
    return {"ranks": ranks, "numeric": numeric}


def plan_rank_for(
    ranks: Mapping[tuple[str, str], int] | None,
    *,
    company_ref: Any,
    spec_ref: Any,
) -> int:
    """The plan's rank for one held document, or ``PLAN_UNDIRECTED_RANK``.

    A document whose kind is unknown is undirected: a plan cannot have named a
    kind nobody can name, and guessing one would promote the wrong document.
    """

    if not ranks or not isinstance(spec_ref, str) or not spec_ref:
        return PLAN_UNDIRECTED_RANK
    return min(
        ranks.get((str(company_ref or ""), spec_ref), PLAN_UNDIRECTED_RANK),
        ranks.get((PLAN_ANY_COMPANY, spec_ref), PLAN_UNDIRECTED_RANK),
    )


__all__ = [
    "CERTAIN_BUDGET_STATUSES",
    "DEFERRED_BUDGET_STATUSES",
    "ExtractionWindowError",
    "ExtractionWindowLedger",
    "PLAN_ANY_COMPANY",
    "PLAN_UNDIRECTED_RANK",
    "SCHEMA_VERSION",
    "UNCERTAIN_ERROR_CODES",
    "classify_window_failure",
    "plan_rank_for",
    "plan_reading_ranks",
    "replayed_window_failure",
    "windows_for_tick",
]
