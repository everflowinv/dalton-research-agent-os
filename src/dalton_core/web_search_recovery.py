"""Retry a proven provider-contract failure after search selection changes.

This is a new governed discovery, never replay of an unresolved call. Existing
success cadence, mission permissions and rolling call limits still apply.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .runner_journal import RunnerJournal
from .store import content_hash
from .web_search_provider import PROVIDER_SELECTION_POLICY, resolve_web_search_provider


def provider_recovery_available(launcher: Any, dispatch: Mapping[str, Any], connection: Any) -> bool:
    if not launcher.networked or dispatch.get("status") != "failed":
        return False
    ticket = dispatch.get("ticket_ref")
    if not isinstance(ticket, str) or launcher._ticket_re.fullmatch(ticket) is None:
        return False
    try:
        summary = json.loads(launcher._ticket_path(ticket).with_name("summary.json").read_text())
        if summary.get("status") != "failed" or any(
            summary.get(key) != dispatch.get(key)
            for key in ("company_ref", "spec_ref", "source_ref")
        ):
            return False
        authorization = summary.get("authorization") or {}
        if any(authorization.get(key) != dispatch.get(key)
               for key in ("mission_version_ref", "mission_version_hash", "company_ref", "source_ref")):
            return False
        provider = resolve_web_search_provider(
            networked=True, expected_provider=launcher.expected_provider,
            openclaw_config_path=launcher.openclaw_config_path, broker_socket=launcher.broker_socket,
        )
        if (summary.get("provider_selection_policy") == PROVIDER_SELECTION_POLICY
                and summary.get("expected_provider") == provider):
            return False
        invocation = (summary.get("search") or {}).get("connector_invocation_ref")
        row = connection.execute(
            "SELECT runner_request_ref FROM runner_request_journal WHERE connector_invocation_ref=?",
            (invocation,),
        ).fetchone()
        if row is None:
            return False
        # Reuse the read API without provisioning or modifying journal tables.
        journal = RunnerJournal.__new__(RunnerJournal)
        journal._connection = connection
        events = journal.history(row["runner_request_ref"])
        for event in events:
            body = {key: value for key, value in event.items() if key not in {"id", "content_hash"}}
            if content_hash(body) != event["content_hash"]:
                return False
        observed = [event["payload"] for event in events if event["state"] == "observed"]
        if not observed or events[-1]["state"] != "responded":
            return False
        response = events[-1]["payload"].get("response") or {}
        return (
            response.get("id") == (summary.get("search") or {}).get("runner_response_ref")
            and response.get("connector_invocation_ref") == invocation
            and response.get("outcome") == "failed"
            and (observed[-1].get("error") or {}).get("code") == "provider_contract_drift"
        )
    except Exception:
        # Missing/invalid historical proof keeps the existing retry cadence.
        return False
