"""Per-purpose daily ceilings and priority for publication work.

Live 2026-09-24: the scheduled publication worker spent $51.80 on 181 calls
of ``research_language_revision`` in four hours (12:30-16:40 UTC) -- 71% of
everything the mission spent in that window -- revising backlog nobody reads
closely: 88 calls on Cockpit UI text batches, 63 on NO_CHANGE event
judgements and 29 on the weekly cycle reflection.  The revision route had
moved from a flash model to Opus the evening before, and nothing but the
mission's $500 day cap stood between that and a $300 day.

This module adds the missing pieces without a second budget system:

* **The ceiling** is the one the claim-support check already uses: USD per
  purpose per UTC day, measured with
  :func:`claim_support_verification.purpose_spend_micros` (settled calls at
  what they cost, open reservations at what they hold, keyed by the
  ``work:cockpit-<purpose>-`` work order).  A purpose at its ceiling is
  *deferred*, not failed: the product stays pending and is offered again on the
  next poll, which on the next UTC day finds room.
* **Priority** decides who gets the room.  Products are prepared in a fixed
  order -- high-value first -- and low-value backlog may only spend
  :data:`LOW_PRIORITY_SHARE` of a ceiling, so a dossier refreshed at 15:00 is
  not locked out by UI text drained at 00:05.

Ceilings may be overridden per purpose in the worker configuration
(``purpose_daily_cap_usd``); absent keys keep :data:`DEFAULT_DAILY_CAP_USD`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .research_language_review import BRAIN_PURPOSE, CHECKER_PURPOSE

DRAFT_PURPOSE = "research_localization"
VERIFIER_PURPOSE = "research_localization_verifier"

#: USD per UTC day, per purpose.  Sized from the ledger (2026-09-13..24):
#:
#: * revision -- Opus now costs ~$0.29 a call.  The high-value load (a dossier
#:   is 2-3 chunks, ~$0.75 each with its repairs; the weekly brief ~$0.30;
#:   a non-NO_CHANGE judgement is 0.2% of judgements) is under $10 on a busy
#:   day, so $25 covers it more than twice and leaves ~$15 a day to drain
#:   backlog.  5% of the $500 mission day, a quarter of the coverage pool.
#: * check -- Gemini flash, ~$0.016 a call; $2-4 a normal day, $18 on the day
#:   the first 2,000-call backlog ran.  $5 is ~300 checks.
#: * draft and verifier -- cheap tiers, never above $1.3 / $2.3 a day.
#:
#: The cheap stages are capped too because the revision route moving from a
#: flash model to Opus is exactly how this happened; the same can happen to
#: any of them.
DEFAULT_DAILY_CAP_USD: Mapping[str, Decimal] = {
    BRAIN_PURPOSE: Decimal("25"),
    CHECKER_PURPOSE: Decimal("5"),
    DRAFT_PURPOSE: Decimal("3"),
    VERIFIER_PURPOSE: Decimal("3"),
}
WORKER_CONFIG_FIELD = "purpose_daily_cap_usd"
MAX_DAILY_CAP_USD = Decimal("100")

#: Low-value backlog may spend at most this share of a ceiling; the rest is
#: held for high-value products that arrive later in the day.
LOW_PRIORITY_SHARE = Decimal("0.6")

PRIORITY_HIGH = 0
PRIORITY_NORMAL = 1
PRIORITY_LOW = 2

#: What a reader acts on: the research itself, the weekly brief, and every
#: judgement that changed something.
HIGH_VALUE_KINDS = frozenset({
    "dossier", "debate_map", "initial_screen", "investment_memo", "industry_framework",
    "surface_weekly_brief", "surface_thesis", "surface_conviction",
    "surface_event_judgement", "surface_earnings_preview", "surface_earnings_calibration",
    "surface_zero_base_review", "surface_deep_insight",
})
#: Always low: the weekly self-review and Claim display strings.
LOW_VALUE_KINDS = frozenset({"surface_cycle_reflection", "ui_text"})

DEFERRED_STATUS = "deferred"
CAP_REACHED_REASON = "purpose_daily_cap_reached"


class PurposeDailyCapReached(RuntimeError):
    """A publication purpose has spent its share of today; try tomorrow.

    Not a failure: whoever catches it keeps the work pending without counting
    an attempt.
    """

    reason = CAP_REACHED_REASON

    def __init__(self, purpose: str, *, spent_micros: int, limit_micros: int,
                 cap_micros: int, priority: int, day: str) -> None:
        self.purpose = purpose
        self.spent_micros = int(spent_micros)
        self.limit_micros = int(limit_micros)
        self.cap_micros = int(cap_micros)
        self.priority = int(priority)
        self.day = day
        share = "" if limit_micros == cap_micros else (
            f", low-priority share {limit_micros} of {cap_micros}")
        super().__init__(
            f"{CAP_REACHED_REASON}: {purpose} has committed {spent_micros} micros "
            f"on {day} (limit {limit_micros}{share}); deferred to the next UTC day")


def _dollars(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number of dollars")
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - Decimal raises several types
        raise ValueError(f"{name} must be a number of dollars") from exc
    if not amount.is_finite() or not 0 <= amount <= MAX_DAILY_CAP_USD:
        raise ValueError(f"{name} must be 0..{MAX_DAILY_CAP_USD} dollars")
    return amount


def daily_caps_micros(overrides: Mapping[str, Any] | None = None) -> dict[str, int]:
    """The ceilings in micros: the defaults, with the owner's overrides.

    An override for a purpose this module does not gate is refused rather
    than ignored -- a ceiling somebody wrote and nothing enforces is worse
    than none.
    """

    caps = dict(DEFAULT_DAILY_CAP_USD)
    if overrides is not None:
        if not isinstance(overrides, Mapping) or set(overrides) - set(caps):
            raise ValueError(f"{WORKER_CONFIG_FIELD} may only set {sorted(caps)}")
        for purpose, value in overrides.items():
            caps[purpose] = _dollars(value, f"{WORKER_CONFIG_FIELD}.{purpose}")
    return {purpose: int(amount * 1_000_000) for purpose, amount in caps.items()}


def limit_micros(cap_micros: int, priority: int) -> int:
    if priority >= PRIORITY_LOW:
        return int(Decimal(cap_micros) * LOW_PRIORITY_SHARE)
    return int(cap_micros)


class PurposeSpendGate:
    """Ask the day ledger before every paid publication call.

    ``spend_today(purpose, day)`` returns committed micros; production passes
    ``purpose_spend_micros`` bound to the model configuration's ``budget_db``.
    A ledger that cannot be read defers too: a ceiling nobody can read is not
    a ceiling with room.
    """

    def __init__(self, *, caps_micros: Mapping[str, int],
                 spend_today: Callable[[str, str], int],
                 clock: Callable[[], datetime] | None = None) -> None:
        self.caps_micros = dict(caps_micros)
        self.spend_today = spend_today
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def check(self, purpose: str, *, priority: int = PRIORITY_NORMAL) -> None:
        cap = self.caps_micros.get(purpose)
        if cap is None:
            return
        day = self.clock().astimezone(timezone.utc).date().isoformat()
        limit = limit_micros(cap, priority)
        try:
            spent = int(self.spend_today(purpose, day))
        except Exception as exc:  # noqa: BLE001 - unreadable ledger defers
            raise PurposeDailyCapReached(
                purpose, spent_micros=-1, limit_micros=limit, cap_micros=cap,
                priority=priority, day=day) from exc
        if spent >= limit:
            raise PurposeDailyCapReached(
                purpose, spent_micros=spent, limit_micros=limit, cap_micros=cap,
                priority=priority, day=day)

    def bound(self, priority: int) -> Callable[[str], None]:
        return lambda purpose: self.check(purpose, priority=priority)


def ledger_gate(budget_db: str, overrides: Mapping[str, Any] | None = None,
                *, clock: Callable[[], datetime] | None = None) -> PurposeSpendGate:
    """The production gate on the shared day ledger."""

    from .claim_support_verification import purpose_spend_micros

    return PurposeSpendGate(
        caps_micros=daily_caps_micros(overrides),
        spend_today=lambda purpose, day: purpose_spend_micros(budget_db, purpose, day),
        clock=clock)


def _judgement_decision(connection: Any, judgement_id: Any) -> str | None:
    if connection is None or not isinstance(judgement_id, str):
        return None
    try:
        row = connection.execute(
            "SELECT decision FROM event_judgements WHERE judgement_id=?",
            (judgement_id,)).fetchone()
    except Exception:  # noqa: BLE001 - no ledger means no known decision
        return None
    return None if row is None else str(row[0])


def is_no_change_judgement(connection: Any, product: Mapping[str, Any]) -> bool:
    """Whether this surface product is one NO_CHANGE judgement.

    Read from the judgement ledger by the product's version ref (the
    judgement id) rather than carried on the product: adding a field would
    change every judgement product's hash and buy all of them again.
    """

    return (product.get("kind") == "surface_event_judgement"
            and _judgement_decision(connection, product.get("version_ref")) == "NO_CHANGE")


def publication_priority(connection: Any, product: Mapping[str, Any]) -> int:
    kind = product.get("kind")
    if kind in LOW_VALUE_KINDS or is_no_change_judgement(connection, product):
        return PRIORITY_LOW
    if kind in HIGH_VALUE_KINDS:
        return PRIORITY_HIGH
    return PRIORITY_NORMAL


def order_key(priority: int, product: Mapping[str, Any]) -> tuple[Any, ...]:
    """A total, deterministic preparation order: priority, then identity."""

    return (int(priority), str(product.get("kind") or ""),
            str(product.get("subject_ref") or ""), str(product.get("version_ref") or ""))


__all__ = [
    "CAP_REACHED_REASON", "DEFAULT_DAILY_CAP_USD", "DEFERRED_STATUS",
    "DRAFT_PURPOSE", "HIGH_VALUE_KINDS",
    "LOW_PRIORITY_SHARE", "LOW_VALUE_KINDS", "PRIORITY_HIGH", "PRIORITY_LOW",
    "PRIORITY_NORMAL", "PurposeDailyCapReached", "PurposeSpendGate", "VERIFIER_PURPOSE",
    "WORKER_CONFIG_FIELD", "daily_caps_micros", "is_no_change_judgement",
    "ledger_gate", "limit_micros", "order_key", "publication_priority",
]
