"""Resolve search selection from the same OpenClaw configuration as the broker.

Read for each new search, never at writer startup. Only the public provider id
leaves this module; credentials and unrelated host settings are not projected.
"""

from __future__ import annotations

import json
from pathlib import Path

from .connector_runner import RunnerValidationError
from .public_web_connector import LEGACY_WEB_SEARCH_PROVIDER, validate_web_search_provider


class WebSearchProviderConfigurationError(ValueError):
    """The host search selection cannot be determined before a call."""


def resolve_web_search_provider(
    *,
    networked: bool,
    expected_provider: str | None = None,
    openclaw_config_path: str | Path | None = None,
    broker_socket: str | Path | None = None,
) -> str:
    """Use an explicit compatibility pin, or follow the host for live calls.

    Rehearsals retain their historical Gemini default and never inspect a live
    home directory. The conventional path is relative to the configured broker
    socket, so distinct workspaces can use distinct hosts.
    """
    if expected_provider is not None:
        return validate_web_search_provider(expected_provider)
    if not networked:
        return LEGACY_WEB_SEARCH_PROVIDER
    if openclaw_config_path is not None:
        path = Path(openclaw_config_path).expanduser()
    elif broker_socket is not None:
        path = Path(broker_socket).expanduser().parent / "openclaw.json"
    else:
        raise WebSearchProviderConfigurationError("OpenClaw search configuration is not configured")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        provider = config["tools"]["web"]["search"]["provider"]
        return validate_web_search_provider(provider)
    except (OSError, ValueError, KeyError, TypeError, RunnerValidationError):
        # Do not include parse context, config contents or credentials.
        raise WebSearchProviderConfigurationError(
            "OpenClaw search provider is missing or invalid; select a provider in OpenClaw"
        ) from None
