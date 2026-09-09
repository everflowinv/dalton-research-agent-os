"""S3: Xueqiu's identity and governance, one approval per operation.

Xueqiu is where Chinese retail investors argue about stocks. What it holds is
not a fact about a company; it is a reading of the crowd around one, and the
whole point of connecting it is to be able to say "the retail conversation
turned in March" with something behind the sentence. Nothing here may become
the source of a number.

**Why this is a new connector and not a wider `xueqiu`.** The 2026-08-14
shadow template (`connector:xueqiu`) covers quotes, hot posts, hot stocks and
stock search, and its content hash is one the owner has already looked at.
Adding post reading to it in place would move that hash under an approval that
was given for something else. So this is a second connector over the same
source ref, exactly as `sec-financials` is a second connector over
`source:sec-edgar`: one source, read two ways, two contracts, two approvals.

**Routes.** The target is the one the shadow template already names --
`host-tool:agent-reach-xueqiu-channel`, the host-owned channel, which reaches
the post endpoints as well as the quote ones. `hot_rank` keeps the shadow
template's one fallback, the cn-hk-findata ranking, under its own provenance
label and for the ranking alone; it is not post text and may never be shown as
any. The public web front end is named in the forbidden list because it sits
behind a challenge page that returns a shell, and the refusal should survive
somebody later "fixing" the connector by pointing it there.

**The cookie is the host's.** Reading posts needs the `xueqiu_cookie` the host
tool holds in its own configuration. This process never reads that file and
never sees the value: the slot is a logical ref, and what a run carries is a
`CredentialGrantEnvelope`, which by construction holds refs, an expiry and a
call ceiling and no material at all. `hot_rank` is the one operation that does
not require the slot, because its fallback route is credential-free.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "xueqiu-posts"

SEARCH_POSTS_OPERATION = "search_posts"
GET_POST_OPERATION = "get_post"
HOT_RANK_OPERATION = "hot_rank"
OPERATIONS = (SEARCH_POSTS_OPERATION, GET_POST_OPERATION, HOT_RANK_OPERATION)

# The operations that cannot run without the host's Xueqiu cookie. The ranking
# is absent on purpose: its fallback route needs no credential.
CREDENTIALLED_OPERATIONS = (SEARCH_POSTS_OPERATION, GET_POST_OPERATION)

KIND_BY_OPERATION = {
    SEARCH_POSTS_OPERATION: "xueqiu-search-posts",
    GET_POST_OPERATION: "xueqiu-get-post",
    HOT_RANK_OPERATION: "xueqiu-hot-rank",
}
CAPABILITY_BY_OPERATION = {
    operation: f"capability:dalton:connector:{kind}"
    for operation, kind in KIND_BY_OPERATION.items()
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
# The logical slot. The host's own name for the same secret is `xueqiu_cookie`,
# recorded here so a reader can find it without this module going to look.
CREDENTIAL_SLOT_REF = "credential-slot:xueqiu-cookie"
HOST_SLOT_NAME = "xueqiu_cookie"
SIDE_EFFECT = "read:xueqiu-posts"

# What a post is, once it has been read. Frozen here rather than in the CLI so
# the coordinator and the tests agree with the child about the shape.
POST_FIELDS = (
    "post_id", "url", "created_at", "author", "author_id", "title", "text",
    "reply_count", "like_count", "retweet_count", "view_count", "truncated",
)


class XueqiuError(RuntimeError):
    """The Xueqiu identity or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise XueqiuError(f"Xueqiu has no frozen {operation!r} operation")
    return operation


def xueqiu_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and the frozen contract of one operation."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise XueqiuError(
            f"packaged Xueqiu template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def xueqiu_source_hash() -> str:
    """Shared by all three operations: the same Xueqiu is the same source."""

    template, _ = xueqiu_contract(SEARCH_POSTS_OPERATION)
    return content_hash(dict(template["source_identity"]))


def xueqiu_schema_hash(operation: str) -> str:
    """Bound to one operation, so an approval cannot widen to another."""

    _, contract = xueqiu_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def xueqiu_adapter_hash(operation: str) -> str:
    template, _ = xueqiu_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
    })


def xueqiu_fallback_route(operation: str) -> dict[str, Any] | None:
    """The operation-scoped fallback the template allows, if it has one.

    Only ``hot_rank`` has one, and it is the cn-hk-findata ranking. Reading it
    from the packaged template rather than restating it is what keeps a
    fallback from quietly acquiring a second operation.
    """

    template, _ = xueqiu_contract(operation)
    for route in template["route_restrictions"]["fallback_routes"]:
        if route["operation"] == operation:
            return dict(route)
    return None


def xueqiu_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one Xueqiu operation."""

    template, contract = xueqiu_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": xueqiu_source_hash(),
        "schema_hash": xueqiu_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": xueqiu_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "requires_credential_slot": operation in CREDENTIALLED_OPERATIONS,
        "credential_slot_refs": [CREDENTIAL_SLOT_REF],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def xueqiu_permissions() -> dict[str, Any]:
    """Permissions of the host-owned, read-only Xueqiu channel.

    ``network: False`` because the host tool owns the network, not this
    process; the only write is into the raw sink where every acquired byte is
    hashed before anything reads it; and the credential is a *slot*, never a
    value.
    """

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [CREDENTIAL_SLOT_REF],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def xueqiu_fixture_hash() -> str:
    template, _ = xueqiu_contract(SEARCH_POSTS_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_xueqiu_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one Xueqiu operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise XueqiuError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise XueqiuError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(xueqiu_permissions()),
        "expected_source_hash": xueqiu_source_hash(),
        "expected_schema_hash": xueqiu_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "CAPABILITY_BY_OPERATION",
    "CREDENTIALLED_OPERATIONS",
    "CREDENTIAL_SLOT_REF",
    "GET_POST_OPERATION",
    "HOST_SLOT_NAME",
    "HOT_RANK_OPERATION",
    "KIND_BY_OPERATION",
    "OPERATIONS",
    "POST_FIELDS",
    "SEARCH_POSTS_OPERATION",
    "SIDE_EFFECT",
    "TEMPLATE_KEY",
    "XueqiuError",
    "build_xueqiu_governance_record",
    "xueqiu_adapter_hash",
    "xueqiu_contract",
    "xueqiu_fallback_route",
    "xueqiu_fixture_hash",
    "xueqiu_identity",
    "xueqiu_permissions",
    "xueqiu_schema_hash",
    "xueqiu_source_hash",
]
