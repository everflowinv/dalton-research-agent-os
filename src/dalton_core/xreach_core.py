"""S3: X read through `xreach`, one approval per operation.

Three different things are called "X" in this house and they are not
interchangeable. `xreach` is a CLI that pages a handle's timeline and a
conversation to a bounded end -- it *enumerates*, which is the only reason it
is the one built. `x_search` is a gateway-owned semantic search: synthetic,
unpageable, and unable to prove that something does not exist; it stays a
shadow and is deliberately not built here. The third, agent-reach's own
Twitter channel, is the weakest of the three and is named in the forbidden
list so that nobody reaches for it later.

**Why a new connector rather than a wider `x-xreach`.** The 2026-08-14 shadow
template covers `timeline`, `get_post` and `get_thread` and sits behind a hash
the owner has seen. What this lane needs is a keyword search alongside a
timeline, and adding it in place would move that hash. So this is a second
connector on the same `source:x` and the same `host-tool:xreach` target, with
its own contract and its own approvals -- the `sec` / `sec-financials` shape.

The Dalton operation names are `user_timeline`, `search` and `thread`; the
tool's own subcommands are `tweets`, `search` and `thread`, and that is what
`source_method` records. They are allowed to differ, and here they do.

**The tokens are the host's.** `xreach` reads `TWITTER_AUTH_TOKEN` and
`TWITTER_CT0` from its own store. This process never reads them and never sees
their values: what a run carries is a `CredentialGrantEnvelope` naming two
logical slots, an expiry and a call ceiling. Every operation needs both, so a
run without a grant covering both is refused before anything is spawned.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "x-xreach-crowd"

USER_TIMELINE_OPERATION = "user_timeline"
SEARCH_OPERATION = "search"
THREAD_OPERATION = "thread"
OPERATIONS = (USER_TIMELINE_OPERATION, SEARCH_OPERATION, THREAD_OPERATION)
# Every X read is authenticated; there is no keyless route.
CREDENTIALLED_OPERATIONS = OPERATIONS

KIND_BY_OPERATION = {
    USER_TIMELINE_OPERATION: "x-xreach-user-timeline",
    SEARCH_OPERATION: "x-xreach-search",
    THREAD_OPERATION: "x-xreach-thread",
}
CAPABILITY_BY_OPERATION = {
    operation: f"capability:dalton:connector:{kind}"
    for operation, kind in KIND_BY_OPERATION.items()
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
# Two slots, because the tool needs two cookies and either one alone is
# useless. The host's own names for them are recorded beside the refs.
AUTH_TOKEN_SLOT_REF = "credential-slot:x-auth-token"
CT0_SLOT_REF = "credential-slot:x-ct0"
CREDENTIAL_SLOT_REFS = (AUTH_TOKEN_SLOT_REF, CT0_SLOT_REF)
HOST_SLOT_NAMES = ("TWITTER_AUTH_TOKEN", "TWITTER_CT0")
SIDE_EFFECT = "read:x-timeline"

POST_FIELDS = (
    "post_id", "url", "created_at", "author", "author_id", "text",
    "reply_count", "like_count", "repost_count", "view_count", "is_reply",
)

# The operation `x_search` would have carried, listed so that "we did not build
# it" is a fact in the code and not only in a report.
NOT_BUILT_OPERATIONS = ("semantic_search",)


class XreachError(RuntimeError):
    """The xreach identity or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise XreachError(f"xreach has no frozen {operation!r} operation")
    return operation


def xreach_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and the frozen contract of one operation."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise XreachError(
            f"packaged xreach template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def xreach_source_hash() -> str:
    """Shared with the shadow template: the same X is the same source."""

    template, _ = xreach_contract(USER_TIMELINE_OPERATION)
    return content_hash(dict(template["source_identity"]))


def xreach_schema_hash(operation: str) -> str:
    """Bound to one operation, so an approval cannot widen to another."""

    _, contract = xreach_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def xreach_adapter_hash(operation: str) -> str:
    template, _ = xreach_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
    })


def xreach_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one xreach operation."""

    template, contract = xreach_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": xreach_source_hash(),
        "schema_hash": xreach_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": xreach_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "requires_credential_slot": True,
        "credential_slot_refs": list(CREDENTIAL_SLOT_REFS),
        "completeness_ceiling": contract["completeness_ceiling"],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def xreach_permissions() -> dict[str, Any]:
    """Permissions of the host-owned, read-only X CLI.

    Two credential slots and no network of our own: `xreach` reaches X, this
    process reaches `xreach`, and the only write is the raw sink.
    """

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": list(CREDENTIAL_SLOT_REFS),
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def xreach_fixture_hash() -> str:
    template, _ = xreach_contract(USER_TIMELINE_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_xreach_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one xreach operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise XreachError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise XreachError("approved_by must be a human: principal")
    kind = KIND_BY_OPERATION[operation]
    capability_id = CAPABILITY_BY_OPERATION[operation]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{kind}:v{version}",
        "status": status,
        "capability_id": capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{kind}:v{version}",
        "approval_ref": f"approval:connector-governance:{kind}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{kind}:v{version}",
        "registry_revision_ref": f"{capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{kind}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": copy.deepcopy(xreach_permissions()),
        "expected_source_hash": xreach_source_hash(),
        "expected_schema_hash": xreach_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "AUTH_TOKEN_SLOT_REF",
    "CAPABILITY_BY_OPERATION",
    "CREDENTIALLED_OPERATIONS",
    "CREDENTIAL_SLOT_REFS",
    "CT0_SLOT_REF",
    "HOST_SLOT_NAMES",
    "KIND_BY_OPERATION",
    "NOT_BUILT_OPERATIONS",
    "OPERATIONS",
    "POST_FIELDS",
    "SEARCH_OPERATION",
    "SIDE_EFFECT",
    "TEMPLATE_KEY",
    "THREAD_OPERATION",
    "USER_TIMELINE_OPERATION",
    "XreachError",
    "build_xreach_governance_record",
    "xreach_adapter_hash",
    "xreach_contract",
    "xreach_fixture_hash",
    "xreach_identity",
    "xreach_permissions",
    "xreach_schema_hash",
    "xreach_source_hash",
]
