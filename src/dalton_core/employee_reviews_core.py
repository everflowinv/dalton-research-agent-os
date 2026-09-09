"""S3: anonymous employee reviews, read from Blind and only from Blind.

Employee reviews are the cheapest read on whether a services company is
winning or losing the people it sells. They are also anonymous, self-selected
and unverifiable, so they can move a question and never settle one.

**Only Blind.** Three sites hold this data. Blind is plain public HTTPS with no
credential of any kind, which is why it is the one built. Indeed and Glassdoor
both require a paid scraping transport whose credits are exhausted, and a
connector that cannot run is not a connector; they are named in the forbidden
list so that adding them later is a decision and not a drift.

**The body lock is the interesting part of the contract.** Blind releases the
prose of its most recent page only. Rows past that come back with placeholder
text in place of the pros and cons -- and the ratings, the one-line summary,
the job group, the location and the date on those same rows are real. So the
child nulls the substituted prose, marks the row ``body_locked``, and keeps
everything else. A rating series built from every row is sound; a word count
built from the readable ones is a sample of the most recent thirty and has to
say so.

That is also why the completeness ceiling is ``partial`` rather than
``enumerated``. The library pages to a bounded end, but a response whose bodies
have been substituted is a truncated response, and the contract says so instead
of letting a reader assume otherwise.

Credential-free by construction: the transport is public HTTPS to one host,
the auth boundary is ``none``, and the permissions name no credential slot.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "employee-reviews"
OPERATION = "blind_reviews"
OPERATIONS = (OPERATION,)
KIND = "employee-reviews-blind"
CAPABILITY_ID = f"capability:dalton:connector:{KIND}"
KIND_BY_OPERATION = {OPERATION: KIND}
CAPABILITY_BY_OPERATION = {OPERATION: CAPABILITY_ID}

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"
BLIND_HOST = "www.teamblind.com"

# The six dimensions Blind scores, in the order it reports them. Frozen here
# because a rating series whose columns move between runs is not a series.
RATING_DIMENSIONS = (
    "overall", "career", "balance", "compensation", "culture", "management",
)
REVIEW_FIELDS = (
    "review_id", "created_at", "summary", "ratings", "body_locked", "pros",
    "cons", "jobgroup", "location",
)
# What Blind substitutes for the prose it will not release. Matched
# case-insensitively on a prefix, because the placeholder is a lorem ipsum of
# varying length rather than one fixed string.
LOCKED_BODY_PREFIX = "lorem ipsum dolor sit amet"


class EmployeeReviewsError(RuntimeError):
    """The employee-reviews identity or governance record is invalid."""


def employee_reviews_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen review-reading operation."""

    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == OPERATION]
    if len(matches) != 1:
        raise EmployeeReviewsError(
            f"packaged template lacks the frozen {OPERATION} operation"
        )
    return template, matches[0]


def employee_reviews_source_hash() -> str:
    template, _ = employee_reviews_contract()
    return content_hash(dict(template["source_identity"]))


def employee_reviews_schema_hash() -> str:
    """Bound to this operation alone, so the approval cannot widen."""

    _, contract = employee_reviews_contract()
    return content_hash({
        "allowed_operations": [OPERATION],
        "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
        "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
        "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
        "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
    })


def employee_reviews_adapter_hash() -> str:
    template, _ = employee_reviews_contract()
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": OPERATION,
    })


def employee_reviews_identity() -> dict[str, Any]:
    template, contract = employee_reviews_contract()
    return {
        "capability_id": CAPABILITY_ID,
        "source_identity": dict(template["source_identity"]),
        "source_hash": employee_reviews_source_hash(),
        "schema_hash": employee_reviews_schema_hash(),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": employee_reviews_adapter_hash(),
        "operation": OPERATION,
        "allowed_operations": [OPERATION],
        "requires_credential_slot": False,
        "credential_slot_refs": [],
        "allowed_hosts": list(template["transport"]["allowed_hosts"]),
        "completeness_ceiling": contract["completeness_ceiling"],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
        "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
        "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
        "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
    }


def employee_reviews_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS, and the raw sink.

    The same declaration the other public-web lanes carry. Which host it may
    reach is the template's allowlist -- one host -- not this shape, which has
    no field for it.
    """

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def employee_reviews_fixture_hash() -> str:
    template, _ = employee_reviews_contract()
    return template["fixture_manifest_hash"]


def body_locked(pros: Any) -> bool:
    """Whether Blind substituted this row's prose.

    Only the prose is substituted. The ratings and the date on the same row are
    real, which is why this is a flag on the row rather than a reason to drop
    it.
    """

    if not isinstance(pros, str):
        return False
    return pros.strip().lower().startswith(LOCKED_BODY_PREFIX)


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_employee_reviews_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for the Blind review read."""

    if status not in {"proposed", "approved"}:
        raise EmployeeReviewsError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise EmployeeReviewsError("approved_by must be a human: principal")
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{KIND}:v{version}",
        "status": status,
        "capability_id": CAPABILITY_ID,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{KIND}:v{version}",
        "approval_ref": f"approval:connector-governance:{KIND}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{KIND}:v{version}",
        "registry_revision_ref": f"{CAPABILITY_ID}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{KIND}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": copy.deepcopy(employee_reviews_permissions()),
        "expected_source_hash": employee_reviews_source_hash(),
        "expected_schema_hash": employee_reviews_schema_hash(),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "BLIND_HOST",
    "CAPABILITY_BY_OPERATION",
    "CAPABILITY_ID",
    "EmployeeReviewsError",
    "KIND",
    "KIND_BY_OPERATION",
    "LOCKED_BODY_PREFIX",
    "OPERATION",
    "OPERATIONS",
    "RATING_DIMENSIONS",
    "REVIEW_FIELDS",
    "SIDE_EFFECT",
    "TEMPLATE_KEY",
    "body_locked",
    "build_employee_reviews_governance_record",
    "employee_reviews_adapter_hash",
    "employee_reviews_contract",
    "employee_reviews_fixture_hash",
    "employee_reviews_identity",
    "employee_reviews_permissions",
    "employee_reviews_schema_hash",
    "employee_reviews_source_hash",
]
