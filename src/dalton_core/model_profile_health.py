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

#: Broker codes that are the host's own framing failures -- the provider or its
#: gateway returned no usable frame, and no model content came back.  Every
#: consumer that treats one of them as "the provider did not answer" (fall
#: back, bounded re-drive, keep the chain alive) treats all of them that way.
#: The last two refine what used to arrive as the generic INVALID_HOST_RESULT
#: "host returned invalid text" (broker 2026-09-30, ``emptyCompletion``).
HOST_FRAME_FAILURE_CODES: frozenset[str] = frozenset({
    "HOST_COMPLETION_FAILED", "INVALID_HOST_RESULT",
    "HOST_EMPTY_COMPLETION", "HOST_OUTPUT_LIMIT_REACHED",
})

#: Exact broker codes that mean the provider produced nothing at all -- no
#: text and no output tokens.  Live 2026-09-29 this was the Claude CLI
#: gateway's weekly usage limit ("You've hit your weekly limit"), answered
#: in-band on an open stream: 62 such calls in a day, each one a wasted
#: route and a lane stall, and none of them tripped a cooldown because the
#: generic code only counts towards the five-in-fifteen-minutes streak.  One of
#: these is enough, for the same reason one HTTP 429 is.
UNAVAILABLE_COOLDOWN_CODES: frozenset[str] = frozenset({"HOST_EMPTY_COMPLETION"})

#: Broker codes that mean the provider account has no credit left: HTTP 402
#: "Payment Required" (DeepSeek's "Insufficient Balance"), which the broker
#: names ``PROVIDER_INSUFFICIENT_BALANCE`` once the host forwards the status
#: (``patch_provider_failure_bridge``).  Live 2026-10-04 onwards every
#: deepseek-v4-flash call was a 402 and Dalton saw only ``INVALID_HOST_RESULT``
#: -- fourteen cooldowns, each one labelled "the probe failed again", none of
#: them saying "top the account up".  One is enough, and the cooldown carries
#: its own reason so the owner reads the actual fix.
BILLING_COOLDOWN_NEEDLES: tuple[str, ...] = (
    "INSUFFICIENT_BALANCE", "PAYMENT_REQUIRED", "INSUFFICIENT_FUNDS",
    "INSUFFICIENT_CREDIT", "BILLING",
)

COOLDOWN_REASONS: frozenset[str] = frozenset({
    "provider_billing",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_failure_streak",
    "probe_failed",
})

#: Each cooldown reason in the owner's words.  One table for the to-do list,
#: the model page and the parity report, so the three never disagree.
COOLDOWN_REASON_LABELS: Mapping[str, str] = {
    "provider_billing": "供应商账户余额不足（HTTP 402）",
    "provider_rate_limited": "供应商限流（HTTP 429）",
    "provider_unavailable": "供应商或网关不出结果（空回复，常见于 CLI 订阅用量上限）",
    "provider_failure_streak": "连续多次供应商失败",
    "probe_failed": "冷却到期后的探测调用又失败",
}

#: The broker failure codes a cooldown can carry, in the owner's words.  The
#: generic ``INVALID_HOST_RESULT`` says so: on a broker or host older than the
#: 402 bridge, an exhausted balance, a refused key and an empty reply all
#: arrive as that one code.
FAILURE_CODE_LABELS: Mapping[str, str] = {
    "PROVIDER_INSUFFICIENT_BALANCE": "供应商账户余额不足（HTTP 402）",
    "RATE_LIMITED": "供应商限流（HTTP 429）",
    "PROVIDER_RATE_LIMITED": "供应商限流（HTTP 429）",
    "PROVIDER_INTERNAL_ERROR": "供应商服务端错误（HTTP 5xx）",
    "PROVIDER_OVERLOADED": "供应商过载",
    "HOST_EMPTY_COMPLETION": "供应商什么都没返回（常见于用量上限）",
    "HOST_OUTPUT_LIMIT_REACHED": "推理把输出上限用完，没有正文",
    "HOST_COMPLETION_FAILED": "宿主调用失败",
    "INVALID_HOST_RESULT": ("宿主返回了空或无效的文本（旧版 broker/宿主的笼统代码："
                            "余额不足、密钥被拒、空回复都会显示成它）"),
    "TIMEOUT": "调用超时",
}


def failure_code_label(code: Any) -> str:
    """One Chinese phrase for a broker failure code; unknown codes say so."""

    if not isinstance(code, str) or not code:
        return "未记录失败代码"
    if code in FAILURE_CODE_LABELS:
        return FAILURE_CODE_LABELS[code]
    if is_billing_code(code):
        return "供应商账户余额不足（HTTP 402）"
    if is_immediate_cooldown_code(code):
        return "供应商限流（HTTP 429）"
    return f"供应商返回失败（代码 {code}）"


def is_billing_code(code: Any) -> bool:
    """Whether this broker error code says the provider account has no credit."""

    if not isinstance(code, str) or not code:
        return False
    upper = code.upper()
    if any(needle in upper for needle in BILLING_COOLDOWN_NEEDLES):
        return True
    digits = "".join(character for character in upper if character.isdigit())
    return len(digits) == 3 and digits == "402"


class ModelProfileHealthError(RuntimeError):
    """The health record cannot be read or written as asked."""


def is_immediate_cooldown_code(code: Any) -> bool:
    """Whether this broker error code trips a cooldown on its own."""

    if not isinstance(code, str) or not code:
        return False
    upper = code.upper()
    if upper in UNAVAILABLE_COOLDOWN_CODES or is_billing_code(upper):
        return True
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
    if is_billing_code(failure_code):
        # Before ``probe_failed`` on purpose: a probe that meets the same 402
        # is still "fund the account", not "the probe failed again".
        reason = "provider_billing"
    elif (isinstance(failure_code, str)
            and failure_code.upper() in UNAVAILABLE_COOLDOWN_CODES):
        reason = "provider_unavailable"
    elif is_immediate_cooldown_code(failure_code):
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

    reason = COOLDOWN_REASON_LABELS.get(str(cooldown.get("reason")),
                                        str(cooldown.get("reason")))
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
    "BILLING_COOLDOWN_NEEDLES",
    "COOLDOWN_REASONS",
    "COOLDOWN_REASON_LABELS",
    "FAILURE_CODE_LABELS",
    "HOST_FRAME_FAILURE_CODES",
    "UNAVAILABLE_COOLDOWN_CODES",
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
    "failure_code_label",
    "is_billing_code",
    "is_immediate_cooldown_code",
]
