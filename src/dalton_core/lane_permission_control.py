"""Stable control-plane keys for lane authorization failures."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .store import canonical_json
from .lane_failure_class import (
    CONTENT_REFUSED, TRANSIENT, Classification, NOT_PERMITTED, classify,
)


def authority_connection(*values: Any) -> Any | None:
    for value in values:
        direct = getattr(value, "connection", None)
        if direct is not None:
            return direct
        nested = getattr(getattr(value, "store", None), "connection", None)
        if nested is not None:
            return nested
    return None


def permission_key(business_key: str, mission: Mapping[str, Any], launcher: Any,
                   *, connection: Any | None = None) -> str:
    """Key a permission hold by its business input and mutable controls."""

    controls = [canonical_json(mission)]
    if connection is not None:
        for table, columns in (
            ("coverage_mission_pointer", "mission_ref, mission_version_id"),
            ("governance_policy_pointer", "pointer_id, policy_version_id"),
        ):
            try:
                rows = connection.execute(
                    f"SELECT {columns} FROM {table} ORDER BY 1"
                ).fetchall()
                controls.extend(f"{table}:{tuple(row)}" for row in rows)
            except Exception:
                controls.append(f"{table}:none")
    for name, value in sorted(vars(launcher).items()):
        if value is None or not any(word in name for word in ("config", "policy")):
            continue
        if not isinstance(value, (str, Path)):
            continue
        try:
            path = Path(value)
            controls.append(f"{name}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
        except (OSError, TypeError, ValueError):
            controls.append(f"{name}:missing")
    digest = hashlib.sha256("|".join(controls).encode("utf-8")).hexdigest()[:16]
    return f"{business_key}|permission:{digest}"


def clear_obsolete_permissions(budget: Any, current: str, *, scope_prefix: str) -> None:
    """Retire permission projections superseded by current controls or work."""

    for row in budget.permission_items():
        if row["item_key"].startswith(scope_prefix) and row["item_key"] != current:
            budget.retire(row["item_key"])


def record_controlled_failure(
    budget: Any, business_key: str, mission: Mapping[str, Any], launcher: Any,
    *, reason: str, status: str, connection: Any | None = None,
    control_key: str | None = None,
) -> Any:
    """Record a failure, projecting governance refusals onto mutable controls."""

    words = f"{status} {reason}".lower()
    governed = (
        status in {"gated", "not_authorized", "no_checkpoint", "no_policy"}
        or "does not grant" in words
        or "not authorized" in words
        or "not permitted" in words
        or "没有授予" in words
    )
    classification = classify(reason, status=status, lane=budget.lane)
    if governed and classification.parks is False:
        classification = Classification(
            NOT_PERMITTED, reason, "lane_governance", status=status)
    # Refusal is content-terminal only after the shared classifier had a chance
    # to recognize quota, model, transport, and other dependency vocabulary.
    if (
        classification.failure_class == TRANSIENT
        and (status == "refused" or status == "duplicate"
             or status.startswith(("refused:", "unavailable:economic_invariants")))
    ):
        classification = Classification(
            CONTENT_REFUSED, reason, "lane_content_refusal", status=status)
    if classification.failure_class == NOT_PERMITTED:
        budget.retire(business_key, reason="permission_control_changed")
        return budget.record(
            control_key or permission_key(business_key, mission, launcher, connection=connection),
            classification=classification,
        )
    return budget.record(business_key, classification=classification)


def current_permission(budget: Any, business_key: str,
                       mission: Mapping[str, Any], launcher: Any, *,
                       connection: Any | None = None) -> str:
    current = permission_key(
        business_key, mission, launcher, connection=connection)
    entity = business_key.split("|", 1)[0] + "|"
    clear_obsolete_permissions(budget, current, scope_prefix=entity)
    return current
