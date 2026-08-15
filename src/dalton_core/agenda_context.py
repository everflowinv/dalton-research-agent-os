"""Build one Agenda cycle's model input from exact Core authorities.

The Agenda coordinator used to concatenate ``MANDATE=``/``PERCEPTION=`` text
into its prompt from a mutable snapshot file.  That was a second, unaudited
path by which facts reached a model.  This module removes it: the mandate and
the perception snapshot are resolved here, inside the writer service, through
the same ``ContextMaterializer`` the research path uses, and the coordinator
receives only a rendered quoted-JSONL body plus a body-free manifest.
"""

from __future__ import annotations

from typing import Any, Mapping

from .agenda import (
    read_exact_agenda_cycle,
    read_exact_agenda_policy_version,
    read_exact_mandate_version,
    read_exact_perception_snapshot,
)
from .context_materializer import ContextMaterializer, ContextMaterializerError
from .observability import ObservabilityStore
from .research_context import build_agenda_context_binding
from .store import DaltonStore


SCHEMA_VERSION = "0.1"
MANDATE_PRIORITY = 20
PERCEPTION_PRIORITY = 10


class AgendaContextError(ContextMaterializerError):
    """The Agenda context could not be bound to exact Core authorities."""


def agenda_source_catalog(snapshot: Mapping[str, Any]) -> list[str]:
    """Derive the only source refs an Agenda candidate may cite.

    The catalog is a projection of the exact perception authority, so a model
    cannot widen it and the coordinator cannot recompute it from a file that
    may have been rewritten since the cycle started.
    """

    refs = {snapshot["snapshot_id"], f"company:{snapshot['company']['slug']}"}
    refs.update(f"event:{item['event_key']}" for item in snapshot["catalysts"])
    refs.update(f"evidence:{item['evidence_key']}" for item in snapshot["evidence"])
    refs.update(f"filing:{item['accession_no']}" for item in snapshot["filings"])
    return sorted(refs)


def build_agenda_context(
    core: DaltonStore,
    observability: ObservabilityStore,
    *,
    cycle_id: str,
    max_tokens: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Materialize the mandate and perception a cycle was started against.

    Every ref and hash below is re-read from Core.  Nothing the caller can
    say changes which mandate or which snapshot is quoted; the caller only
    names a cycle and a budget.
    """

    if type(core) is not DaltonStore:
        raise AgendaContextError("core must be the exact DaltonStore")
    cycle = read_exact_agenda_cycle(core.connection, cycle_id)
    policy = read_exact_agenda_policy_version(
        core.connection, cycle["policy_version_ref"]
    )
    mandate = read_exact_mandate_version(
        core.connection, cycle["mandate_version_ref"]
    )
    snapshot = read_exact_perception_snapshot(
        core.connection, cycle["perception_snapshot_ref"]
    )
    if snapshot["content_hash"] != cycle["perception_snapshot_hash"]:
        raise AgendaContextError(
            "cycle perception hash no longer matches Core authority"
        )
    if mandate["content_hash"] != cycle["mandate_version_hash"]:
        raise AgendaContextError(
            "cycle mandate hash no longer matches Core authority"
        )
    if policy["content_hash"] != cycle["policy_version_hash"]:
        raise AgendaContextError(
            "cycle policy hash no longer matches Core authority"
        )
    if cycle["company_ref"] not in mandate["scope_refs"]:
        raise AgendaContextError("cycle mandate no longer scopes the cycle company")
    if snapshot["company"].get("slug") != cycle["company_ref"]:
        raise AgendaContextError("cycle perception covers a different company")
    # The cycle timestamp is part of replay authority.  A caller may not
    # choose a new materialization/binding timestamp for the same cycle.
    when = cycle["created_at"]
    binding = build_agenda_context_binding(
        cycle_ref=cycle["cycle_id"],
        cycle_hash=cycle["content_hash"],
        company_ref=cycle["company_ref"],
        policy_version_ref=policy["id"],
        policy_version_hash=cycle["policy_version_hash"],
        mandate_version_ref=mandate["id"],
        mandate_version_hash=cycle["mandate_version_hash"],
        perception_snapshot_ref=snapshot["snapshot_id"],
        perception_snapshot_hash=snapshot["content_hash"],
        created_at=when,
    )
    materializer = ContextMaterializer(core, observability, None)
    pack = materializer.build_agenda_authority_context_pack(
        [
            {
                "kind": "mandate", "ref": mandate["id"],
                "hash": mandate["content_hash"], "priority": MANDATE_PRIORITY,
            },
            {
                "kind": "perception", "ref": snapshot["snapshot_id"],
                "hash": snapshot["content_hash"], "priority": PERCEPTION_PRIORITY,
            },
        ],
        agenda_binding=binding,
        created_at=when,
        max_tokens=max_tokens, max_bytes=max_bytes,
    )
    materialization = materializer.materialize(
        pack, max_rendered_tokens=max_tokens, max_rendered_bytes=max_bytes,
        compiled_plan=binding, claim_index=None, created_at=when,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_ref": cycle["cycle_id"],
        "company_ref": cycle["company_ref"],
        "binding": binding,
        "context_pack": pack,
        "manifest": materialization.manifest,
        "rendered_text": materialization.rendered_text,
        "allowed_source_refs": agenda_source_catalog(snapshot),
        "mandate_version_ref": mandate["id"],
        "mandate_version_hash": mandate["content_hash"],
        "perception_snapshot_ref": snapshot["snapshot_id"],
        "perception_snapshot_hash": snapshot["content_hash"],
        "policy_version_ref": policy["id"],
        "policy_version_hash": policy["content_hash"],
    }


__all__ = [
    "AgendaContextError", "MANDATE_PRIORITY", "PERCEPTION_PRIORITY",
    "SCHEMA_VERSION", "agenda_source_catalog", "build_agenda_context",
]
