"""What a host-tool provider said when it refused a call, and what kind of refusal it was.

2026-10-08 (ws-7d and legacy, 08:03-08:09Z): every AlphaEngine search failed
with ``E_UPSTREAM_API: 用户状态发生变更，刷新 token`` -- the AlphaEngine server
had invalidated the desktop session while ``auth_status`` still reported a
valid token.  The text reached the runner journal and was then dropped: the
ticket summary said ``search outcome retryable``, the dispatch ledger said the
same, the cadence waited a whole ``retry_interval_days`` and nobody was told.

This module is the one shared answer to "is this a login/session problem?",
used by the live MCP adapters (AlphaEngine, Guidepoint), the search and
acquisition children (``failure_detail``), the discovery coordinator's health
hold, the 「需要你处理」 list, the parity report and the cockpit.

Classes:

``provider_auth``
    The provider refused because the login/session/token is no longer valid.
    Repeating the same call cannot help; a person has to re-login in the host
    application.  Distinct from transport retryable failures.
``transport``
    The host tool or the loopback endpoint was not reachable / timed out.
``provider_error``
    Anything else the provider said.  Its text is preserved verbatim.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

PROVIDER_AUTH = "provider_auth"
TRANSPORT = "transport"
PROVIDER_ERROR = "provider_error"
FAILURE_CLASSES = (PROVIDER_AUTH, TRANSPORT, PROVIDER_ERROR)

#: The error code the live adapters write for a login/session refusal.
PROVIDER_AUTH_CODE = PROVIDER_AUTH

# Matched case-insensitively as substrings.  Chinese first: the AlphaEngine
# server speaks Chinese ("用户状态发生变更，刷新 token").
_AUTH_PHRASES: tuple[str, ...] = (
    "刷新 token", "刷新token", "用户状态发生变更", "登录失效", "登录已失效",
    "登录已过期", "登录过期", "请重新登录", "重新登录", "未登录", "token 失效",
    "token失效", "token 过期", "token过期",
    "token_expired", "token expired", "expired token", "invalid token",
    "invalid_token", "token invalid", "refresh token", "refresh_token",
    "session_expired", "session expired", "session invalid", "not_signed_in",
    "not signed in", "login required", "unauthorized", "unauthenticated",
    "rejected host authorization",
)
_AUTH_STATUS_RE = re.compile(
    r"(?:\bhttp\s*|\bstatus(?:[ _]code)?\s*[:=]?\s*|\berror\s*|\bE_)(401|403)\b",
    re.IGNORECASE,
)
_AUTH_CODES = frozenset({PROVIDER_AUTH_CODE, "token_expired", "session_expired",
                         "unauthorized", "revoked", "not_signed_in"})
_TRANSPORT_CODES = frozenset({"deadline_exceeded", "timeout", "transport_error"})
_TRANSPORT_PHRASES: tuple[str, ...] = (
    "mcp endpoint is unavailable", "connection refused", "timed out", "timeout",
    "is not answering", "urlerror", "remotedisconnected", "deadline",
)
# The prefix every summary failure_reason carries when the provider text is
# known, so readers that only see the ledger string can still classify it.
REASON_TAG = "[{cls}]"


def is_provider_auth_text(text: Any) -> bool:
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    if any(phrase.lower() in lowered for phrase in _AUTH_PHRASES):
        return True
    return _AUTH_STATUS_RE.search(text) is not None


def classify_provider_failure(code: Any = None, message: Any = None, *,
                              status_code: Any = None) -> str:
    """``provider_auth`` / ``transport`` / ``provider_error`` for one refusal."""

    code_text = str(code or "").strip().lower()
    if code_text in _AUTH_CODES:
        return PROVIDER_AUTH
    if is_provider_auth_text(message):
        return PROVIDER_AUTH
    if status_code in (401, "401") and code_text != "permission_denied":
        return PROVIDER_AUTH
    if code_text in _TRANSPORT_CODES:
        return TRANSPORT
    lowered = str(message or "").lower()
    if any(phrase in lowered for phrase in _TRANSPORT_PHRASES):
        return TRANSPORT
    return PROVIDER_ERROR


def classify_reason(reason: Any) -> str | None:
    """Classify a stored ``failure_reason`` string; ``None`` when it says nothing.

    Reads the ``[provider_auth]`` tag the children now write, and falls back to
    the phrases for anything older that happened to keep the provider text.
    """

    if not isinstance(reason, str) or not reason:
        return None
    for cls in FAILURE_CLASSES:
        if REASON_TAG.format(cls=cls) in reason:
            return cls
    if is_provider_auth_text(reason):
        return PROVIDER_AUTH
    return None


def failure_detail(error: Mapping[str, Any] | None, *,
                   status_code: Any = None) -> dict[str, Any] | None:
    """The closed ``failure_detail`` block a ticket summary carries."""

    if not isinstance(error, Mapping):
        return None
    message = str(error.get("message") or "")[:1000]
    code = str(error.get("code") or "")
    return {
        "class": classify_provider_failure(code, message, status_code=status_code),
        "code": code,
        "message": message,
        "retryable": error.get("retryable") if type(error.get("retryable")) is bool else None,
    }


def failure_reason_text(prefix: str, detail: Mapping[str, Any] | None) -> str:
    """``search outcome failed [provider_auth] source=provider_auth: E_UPSTREAM_API: …``."""

    if not detail:
        return prefix
    message = str(detail.get("message") or "").strip()
    code = str(detail.get("code") or "").strip()
    text = f"{prefix} {REASON_TAG.format(cls=detail.get('class') or PROVIDER_ERROR)}"
    if code:
        text += f" {code}"
    if message:
        text += f": {message}"
    return text[:500]


def journal_failure(connection: Any, connector_invocation_ref: str | None) -> dict[str, Any] | None:
    """The provider error the runner journal observed for one invocation.

    The journal's ``observed`` event has always held the adapter's error -- it
    was the summary and the ledger that lost it.  Read-only; ``None`` when the
    invocation has no failed observation.
    """

    if not connector_invocation_ref or connection is None:
        return None
    import json

    try:
        row = connection.execute(
            "SELECT e.payload_json FROM runner_attempt_journal_events e "
            "JOIN runner_request_journal r ON r.runner_request_ref=e.runner_request_ref "
            "WHERE r.connector_invocation_ref=? AND e.state='observed' "
            "ORDER BY e.event_seq DESC LIMIT 1",
            (connector_invocation_ref,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - a missing journal table is "nothing observed"
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    observation = payload.get("observation") or {}
    status = observation.get("provider_status_code") if isinstance(observation, Mapping) else None
    return failure_detail(error, status_code=status)


def attempt_failure(connection: Any, physical_attempt_ref: str | None) -> dict[str, Any] | None:
    """The provider error recorded on (or journalled for) one physical attempt."""

    if not physical_attempt_ref or connection is None:
        return None
    import json

    try:
        row = connection.execute(
            "SELECT connector_invocation_ref, record_json FROM connector_physical_attempts "
            "WHERE physical_attempt_id=?", (physical_attempt_ref,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    try:
        record = json.loads(row[1])
    except (TypeError, ValueError):
        record = {}
    if isinstance(record.get("error"), Mapping):
        return failure_detail(record["error"])
    return journal_failure(connection, row[0])


__all__ = [
    "attempt_failure",
    "FAILURE_CLASSES",
    "PROVIDER_AUTH",
    "PROVIDER_AUTH_CODE",
    "PROVIDER_ERROR",
    "REASON_TAG",
    "TRANSPORT",
    "classify_provider_failure",
    "classify_reason",
    "failure_detail",
    "failure_reason_text",
    "is_provider_auth_text",
    "journal_failure",
]
