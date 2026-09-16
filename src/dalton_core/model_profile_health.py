"""WP-A/A2: when a model endpoint stops answering, stop routing to it.

The chain already knows what to do when a call fails: try the next link.  What
it did not know was how to stop asking.  ``profile:gpt-6-astra`` answered HTTP
429 for every brain-tier call from 2026-09-14T19:58 onwards and stayed first in
five routing policies' brain chains.  Every attempt still routed to it, still
reserved the chain's ceiling against the day ledger, and -- because a
provider-completed failure was settled at that ceiling -- still spent money.
Six hours of it booked 286 USD against zero served calls and left every other
lane refused by an exhausted pool.

A cooldown is the missing half.  It is deliberately a property of the
*selection* rather than of the call: a cooled profile is refused as a route
candidate, so nothing is dispatched, nothing is admitted against a budget, and
the refusal is recorded in the route decision's own candidate snapshot like
every other reason a model was not chosen.

Three rules, and no fourth:

* **Any rate limit trips it immediately.**  A 429 is the provider saying "not
  now" in as many words.  Waiting for a fifth one is five more reservations.
* **Otherwise it takes a streak.**  Five provider failures inside fifteen
  minutes, which a healthy endpoint does not produce and a dead one produces in
  a minute.
* **Coming back is a probation, not a pardon.**  The cooldown simply expires,
  which is what lets one probe call through; if that probe fails the profile is
  cooled again immediately -- no second streak required -- and the next window
  is twice as long, up to six hours.  That is what keeps a day-long outage from
  costing one probe every thirty minutes forever.

Only the thresholds and the arithmetic live here.  The evidence and the
decisions are rows in the router's own database (``model_profile_health_events``
and ``model_profile_cooldowns``), because "why was this model not asked" has to
be answerable months later from the same place the route decisions are.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

# Five failures inside a quarter of an hour. A healthy endpoint under load does
# not do this; a dead one does it in under a minute.
FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 900
# Half an hour is long enough that a provider-side incident is usually over,
# and short enough that a false positive costs one stage one cycle.
BASE_COOLDOWN_SECONDS = 1800
# Six hours. Past that, a longer cooldown is not a routing decision any more --
# it is a catalog decision, and the owner makes those on the model page.
MAX_COOLDOWN_SECONDS = 21_600

#: The skip reason a cooled profile carries, in route decisions and chain links.
COOLDOWN_SKIP_REASON = "provider_cooldown"

#: Broker error codes that are the provider refusing to start, not failing part
#: way through. One of these is enough on its own: the call cost nothing and the
#: next one will cost nothing too, so there is nothing to learn from repeating
#: it.
IMMEDIATE_COOLDOWN_NEEDLES: tuple[str, ...] = (
    "RATE_LIMIT", "RATE_LIMITED", "TOO_MANY_REQUESTS", "THROTTLED",
    "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED",
)

COOLDOWN_REASONS: frozenset[str] = frozenset({
    "provider_rate_limited",
    "provider_failure_streak",
    "probe_failed",
})


class ModelProfileHealthError(RuntimeError):
    """The health record cannot be read or written as asked."""


def is_immediate_cooldown_code(code: Any) -> bool:
    """Whether this broker error code trips a cooldown on its own."""

    if not isinstance(code, str) or not code:
        return False
    upper = code.upper()
    if any(needle in upper for needle in IMMEDIATE_COOLDOWN_NEEDLES):
        return True
    # A code that carries its own HTTP status, which the broker's provider
    # adapters do for pass-through upstream errors.
    digits = "".join(character for character in upper if character.isdigit())
    return len(digits) == 3 and digits == "429"


def cooldown_seconds(streak: int) -> int:
    """How long the ``streak``-th consecutive cooldown for a profile lasts.

    Doubling, capped. The cap is not politeness: an endpoint that has been down
    for six hours needs an owner, and a backoff that grew to days would hide
    that the chain has silently been running on its fallback the whole time.
    """

    if streak < 1:
        raise ModelProfileHealthError("a cooldown streak starts at 1")
    shift = min(streak - 1, 32)
    return min(BASE_COOLDOWN_SECONDS * (2 ** shift), MAX_COOLDOWN_SECONDS)


def cooldown_decision(
    *,
    failure_code: Any,
    now: datetime,
    recent_failures: int,
    previous_streak: int,
    on_probation: bool,
) -> dict[str, Any] | None:
    """Whether this provider failure cools the profile, and for how long.

    ``recent_failures`` counts this failure and every earlier one inside the
    window that has not been cleared by a served call.  ``on_probation`` is
    true when the profile's last cooldown has expired and nothing has served
    since: the call that just failed *was* the probe.
    """

    if now.tzinfo is None:
        raise ModelProfileHealthError("a cooldown decision needs an aware moment")
    if recent_failures < 1:
        raise ModelProfileHealthError("a cooldown decision needs the failure that caused it")
    if is_immediate_cooldown_code(failure_code):
        reason = "provider_rate_limited"
    elif on_probation:
        reason = "probe_failed"
    elif recent_failures >= FAILURE_THRESHOLD:
        reason = "provider_failure_streak"
    else:
        return None
    streak = max(0, int(previous_streak)) + 1
    seconds = cooldown_seconds(streak)
    return {
        "reason": reason,
        "failure_code": (str(failure_code)[:96] if isinstance(failure_code, str)
                         and failure_code else None),
        "failure_count": int(recent_failures),
        "streak": streak,
        "cooldown_seconds": seconds,
        "started_at": now.astimezone(timezone.utc),
        "until": (now + timedelta(seconds=seconds)).astimezone(timezone.utc),
    }


def cooldown_message(cooldown: Mapping[str, Any]) -> str:
    """One sentence, for the owner, about why a model is not being asked."""

    labels = {
        "provider_rate_limited": "供应商限流（HTTP 429）",
        "provider_failure_streak": "连续多次供应商失败",
        "probe_failed": "冷却到期后的探测调用又失败",
    }
    reason = labels.get(str(cooldown.get("reason")), str(cooldown.get("reason")))
    minutes = max(1, int(cooldown.get("cooldown_seconds") or 0) // 60)
    return (
        f"{cooldown.get('profile_id')} 因{reason}进入冷却，"
        f"{minutes} 分钟内不再选它（第 {cooldown.get('streak')} 次，"
        f"到 {cooldown.get('until')} 为止）。冷却期内不发起调用、不预留预算。"
    )


def active(cooldowns: Sequence[Mapping[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """The cooldowns in ``cooldowns`` that have not expired at ``now``."""

    moment = now.astimezone(timezone.utc)
    live: list[dict[str, Any]] = []
    for item in cooldowns:
        until = item.get("until")
        if not isinstance(until, str):
            continue
        try:
            expires = datetime.fromisoformat(until)
        except ValueError:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > moment:
            live.append(dict(item))
    return live


__all__ = [
    "BASE_COOLDOWN_SECONDS",
    "COOLDOWN_REASONS",
    "COOLDOWN_SKIP_REASON",
    "FAILURE_THRESHOLD",
    "FAILURE_WINDOW_SECONDS",
    "IMMEDIATE_COOLDOWN_NEEDLES",
    "MAX_COOLDOWN_SECONDS",
    "ModelProfileHealthError",
    "active",
    "cooldown_decision",
    "cooldown_message",
    "cooldown_seconds",
    "is_immediate_cooldown_code",
]
