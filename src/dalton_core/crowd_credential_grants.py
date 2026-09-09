"""S3: how a crowd-source child learns a credential slot is bound, without reading it.

Two of the three crowd sources need a secret the host owns: Xueqiu needs the
cookie `agent-reach` keeps in its own configuration, and `xreach` needs the two
X cookies it keeps in its own. Neither secret is Dalton's, and the connector
protocol is explicit that a child never receives credential material, a
configuration path, or a database it could go and find one in.

So the child is told, and the thing that tells it is the object the protocol
already defines for the purpose: a `CredentialGrantEnvelope`. It carries refs,
two timestamps, a slot list and a call ceiling. It carries no value, and
`CredentialGrantEnvelope.from_dict` refuses anything that is not exactly that
closed shape. Presented with one, a child can answer "is this slot bound" and
"for how long" and nothing else -- which is the whole question it has.

A run without a grant is not an error to be explained afterwards. It is a
refusal, taken before a process is spawned or a byte is fetched, whose reason
names the slots that were not covered. That sentence is the interface: the
coordinator reads it, and a person reading the summary is told which secret to
go and bind.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .credential_authority import CredentialGrantEnvelope, CredentialGrantRejected


class CrowdCredentialSlotUnbound(RuntimeError):
    """No host grant covers the slots this operation needs."""


def load_credential_grant(path: str | Path) -> CredentialGrantEnvelope:
    """Read one grant envelope from disk, refusing anything else.

    The envelope's own validator does the work: closed shape, logical slot
    refs, an expiry after its issuance, and a content hash over the rest. What
    is added here is a readable failure, because this is the one file an owner
    hand-places and therefore the one most likely to be malformed.
    """

    location = Path(path).expanduser().resolve()
    try:
        wire = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CrowdCredentialSlotUnbound(
            f"no credential grant at {location.name}; the host has not bound "
            "the slots this operation needs"
        ) from exc
    except (OSError, ValueError) as exc:
        raise CrowdCredentialSlotUnbound(
            f"credential grant {location.name} is not readable JSON: {exc}"
        ) from exc
    try:
        return CredentialGrantEnvelope.from_dict(wire)
    except CredentialGrantRejected as exc:
        raise CrowdCredentialSlotUnbound(
            f"credential grant {location.name} is not a valid grant envelope: {exc}"
        ) from exc


def require_slots(
    grant: CredentialGrantEnvelope | None,
    *,
    slot_refs: Sequence[str],
    operation: str,
    target_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check that a grant covers this operation's slots, or refuse saying which.

    Everything checked here is checked again by the credential authority at use
    time, and deliberately so: this is the cheap local gate that keeps a child
    from spawning a host tool it was never authorised to drive. It is not the
    authority, and it does not pretend to be one -- it holds no handle and
    resolves no value.
    """

    wanted = sorted(set(slot_refs))
    if not wanted:
        raise ValueError("require_slots needs at least one slot ref")
    if grant is None:
        raise CrowdCredentialSlotUnbound(
            f"{operation} needs credential slots {wanted} and this run carries "
            "no host grant; bind them with the host tool and pass the grant"
        )
    missing = sorted(set(wanted) - set(grant.credential_slot_refs))
    if missing:
        raise CrowdCredentialSlotUnbound(
            f"{operation} needs credential slots {missing}, which the host "
            f"grant {grant.id} does not cover"
        )
    if operation not in grant.allowed_operations:
        raise CrowdCredentialSlotUnbound(
            f"host grant {grant.id} does not allow {operation}"
        )
    if grant.target_ref != target_ref:
        raise CrowdCredentialSlotUnbound(
            f"host grant {grant.id} is for {grant.target_ref}, not {target_ref}"
        )
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires_at = datetime.fromisoformat(grant.expires_at)
    if expires_at <= moment:
        raise CrowdCredentialSlotUnbound(
            f"host grant {grant.id} expired at {grant.expires_at}"
        )
    return {
        "grant_ref": grant.id,
        "grant_hash": grant.content_hash,
        "credential_slot_refs": list(grant.credential_slot_refs),
        "expires_at": grant.expires_at,
        "max_calls": grant.max_calls,
    }


def slot_binding_summary(grant: CredentialGrantEnvelope | None) -> dict[str, Any]:
    """What a summary may say about credentials: refs and an expiry, never more."""

    if grant is None:
        return {"grant_ref": None, "grant_hash": None,
                "credential_slot_refs": [], "expires_at": None}
    return {
        "grant_ref": grant.id,
        "grant_hash": grant.content_hash,
        "credential_slot_refs": list(grant.credential_slot_refs),
        "expires_at": grant.expires_at,
    }


# Exact keys, not substrings. The first version matched substrings, and
# "auth" is a substring of "author" and "author_id" -- so a single Xueqiu post,
# whose top level *is* the post, arrived with its author silently deleted. A
# filter that quietly removes data is worse than no filter: nothing failed, the
# post was simply anonymous from then on.
CREDENTIAL_SHAPED_KEYS = frozenset({
    "cookie", "cookies", "set_cookie", "set-cookie",
    "token", "auth_token", "authtoken", "auth", "authorization",
    "ct0", "csrf_token", "session", "session_id",
    "api_key", "apikey", "secret", "password", "credential", "credentials",
    "bearer", "access_token", "refresh_token",
})


def redacted(value: Mapping[str, Any]) -> dict[str, Any]:
    """A shallow copy with credential-shaped *keys* removed.

    Belt and braces for the raw artifact path: the host tools are asked for
    read-only data and do not echo cookies, but a tool that starts doing so
    should not be able to write one into the spool through this lane.

    Matching is on the whole key, case-insensitively. Anything narrower than
    that deletes real data, and anything wider is not a filter but a guess.
    """

    return {
        key: item for key, item in value.items()
        if str(key).strip().lower().replace("-", "_") not in CREDENTIAL_SHAPED_KEYS
    }


__all__ = [
    "CREDENTIAL_SHAPED_KEYS",
    "CrowdCredentialSlotUnbound",
    "load_credential_grant",
    "redacted",
    "require_slots",
    "slot_binding_summary",
]
