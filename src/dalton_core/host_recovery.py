"""Replay source fetch outcomes into nonblocking host quarantine state.

The rolling window establishes a failure pattern. Once established, quarantine
survives that window; only a successful fetch resets it. An expired timer permits
one low-priority probe, not the release of every queued URL on that host. A failed
probe applies the configured multiplier, which may extend or retain the interval.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


def validate_cooldown_policy(value: Any) -> dict[str, Any]:
    fields = {"minimum_distinct_urls", "window_seconds", "cooldown_seconds"}
    if not isinstance(value, Mapping) or set(value) not in (fields, fields | {"recovery"}):
        raise ValueError("failure_cooldown has an invalid closed shape")
    result = {}
    for name in sorted(fields):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"{name} must be a positive integer")
        result[name] = item
    # Storage can represent finite calendar intervals. This is not an
    # operational ceiling: the owner chooses the count and wait durations.
    now = datetime.now(timezone.utc)
    try:
        now - timedelta(seconds=result["window_seconds"])
        now + timedelta(seconds=result["cooldown_seconds"])
    except (OverflowError, ValueError) as exc:
        raise ValueError("host cooldown duration cannot be represented") from exc
    if "recovery" in value:
        result["recovery"] = validate_recovery_policy(
            value["recovery"], initial_seconds=result["cooldown_seconds"])
    return result


def validate_recovery_policy(value: Any, *, initial_seconds: int) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) != {"schema_version", "backoff_multiplier", "max_cooldown_seconds"}
            or value["schema_version"] != "0.1"):
        raise ValueError("host recovery policy has an invalid closed shape")
    multiplier, maximum = value["backoff_multiplier"], value["max_cooldown_seconds"]
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
        raise ValueError("host recovery backoff_multiplier must be a positive integer")
    if (isinstance(maximum, bool) or not isinstance(maximum, int)
            or maximum < initial_seconds):
        raise ValueError("host recovery max_cooldown_seconds must cover the initial cooldown")
    # A representable duration is a storage/runtime requirement, not a retry
    # budget. The operational policy supplies both the multiplier and maximum.
    try:
        datetime.now() + timedelta(seconds=maximum)
    except (OverflowError, ValueError) as exc:
        raise ValueError("host recovery duration cannot be represented") from exc
    return dict(value)


def host_recovery_states(events: Iterable[Mapping[str, Any]], *,
                         minimum_distinct_urls: int, window_seconds: int,
                         cooldown_seconds: int, as_of: datetime,
                         recovery: Mapping[str, Any]) -> list[dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for event in events:
        host = event["host"]
        state = states.setdefault(host, {"failures": deque(), "until": None,
                                         "delay": cooldown_seconds, "probe_failures": 0})
        when = datetime.fromisoformat(event["created_at"])
        if when > as_of:
            continue
        if event["outcome"] == "acquired":
            state.update(failures=deque(), until=None, delay=cooldown_seconds, probe_failures=0)
            continue
        if state["until"] is not None:
            # Even an indeterminate/retryable probe failure does not establish
            # reachability. Keep it out of the normal fetch queue and back off.
            state["probe_failures"] += 1
            state["delay"] = min(recovery["max_cooldown_seconds"],
                                 state["delay"] * recovery["backoff_multiplier"])
            state["until"] = when + timedelta(seconds=state["delay"])
            continue
        if event["outcome"] != "transport_terminal":
            continue
        failures = state["failures"]
        failures.append((when, event["document_ref"]))
        while failures and failures[0][0] < when - timedelta(seconds=window_seconds):
            failures.popleft()
        if len({ref for _, ref in failures}) >= minimum_distinct_urls:
            state["until"] = when + timedelta(seconds=cooldown_seconds)
    result = []
    for host, state in sorted(states.items()):
        until = state["until"]
        if until is None:
            continue
        result.append({"host": host, "reason": "host unreachable; recovery probe required",
                       "distinct_urls": len({ref for _, ref in state["failures"]}),
                       "until": until.isoformat(timespec="microseconds"),
                       "next_probe": until.isoformat(timespec="microseconds"),
                       "state": "probe_due" if as_of >= until else "quarantined",
                       "probe_failures": state["probe_failures"],
                       "cooldown_seconds": state["delay"]})
    return result
