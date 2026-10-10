"""SEC filings-index discovery specs are owned by code, not by plan files.

The same decision as :mod:`alphaengine_query_forms` (owner, 2026-10-06, "A"):
every environment runs the same code, and a freshly created workspace is
correct without patches.  The SEC plan a workspace generator writes has had
two specs since W7 -- the 10-K and an 8-K -- while the legacy plan file, written
by hand in September, has only the 10-K.  So legacy never asked SEC for an
8-K, the earnings-release text reader had nothing to read, and the only way to
change that was a per-environment edit of an approved file.

An approved SEC plan file keeps what the owner governs:

* ``budget.max_calls_24h``;
* the company list (``{company_ref: {cik}}``);
* ``id``, ``mission_ref``, ``source_ref``, ``created_at``;
* per spec it already carries: ``lookback_days``, ``rediscovery_interval_days``,
  ``retry_interval_days``.

The *set* of specs and each spec's ``form`` come from :data:`SEC_PLAN_SPECS`.
A spec the code declares and the file does not carry runs with the code's
default intervals; a spec the file carries and the code does not still runs as
the file says (nothing the owner approved is dropped).  The rendering is an
ordinary validated 0.4 plan with its own ``content_hash``; when the file
already equals the code the rendered plan *is* the file, same hash.

``item_filter`` is not part of the 0.4 wire (its spec shape is closed): it is
read by the acquisition lane through :func:`acquisition_item_filter`, so an
8-K that is not an Item 2.02 earnings release is never fetched.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .store import canonical_json, content_hash

SEC_SOURCE_REF = "source:sec-edgar"
SEC_PLAN_SCHEMA = "0.4"
EARNINGS_RELEASE_SPEC_REF = "current-report-8k"

#: The SEC specs every environment runs, in order.  Interval defaults are the
#: generator's (``workspace_mission_setup``); a plan file that carries the spec
#: keeps its own intervals.
SEC_PLAN_SPECS: tuple[Mapping[str, Any], ...] = (
    MappingProxyType({
        "spec_ref": "annual-report-10k", "form": "10-K",
        "lookback_days": 500, "rediscovery_interval_days": 30, "retry_interval_days": 2,
        "item_filter": None,
    }),
    MappingProxyType({
        # Item 2.02 only: the company's own earnings release.  The index is
        # still asked for every 8-K (the frozen ``list_filings`` normaliser
        # filters by form, not item), and the lane fetches only the 2.02 ones.
        "spec_ref": EARNINGS_RELEASE_SPEC_REF, "form": "8-K",
        "lookback_days": 90, "rediscovery_interval_days": 2, "retry_interval_days": 1,
        "item_filter": ("2.02",),
    }),
)
_GOVERNED_SPEC_FIELDS = ("lookback_days", "rediscovery_interval_days", "retry_interval_days")


def generator_specs() -> list[dict[str, Any]]:
    """The 0.4 spec rows a new workspace's SEC plan is written with."""

    return [{key: spec[key] for key in ("spec_ref", "form", *_GOVERNED_SPEC_FIELDS)}
            for spec in SEC_PLAN_SPECS]


def _wire() -> list[dict[str, Any]]:
    return [{**{key: spec[key] for key in ("spec_ref", "form", *_GOVERNED_SPEC_FIELDS)},
             "item_filter": None if spec["item_filter"] is None else list(spec["item_filter"])}
            for spec in SEC_PLAN_SPECS]


#: Content hash of the code's SEC spec set, for ledgers and parity rows.
SEC_PLAN_SPECS_HASH = content_hash(_wire())


def code_spec(spec_ref: str) -> Mapping[str, Any] | None:
    return next((spec for spec in SEC_PLAN_SPECS if spec["spec_ref"] == spec_ref), None)


def acquisition_item_filter(spec_ref: str | None) -> tuple[str, ...] | None:
    """The 8-K item numbers a spec's filings must carry to be fetched, or None."""

    spec = code_spec(str(spec_ref or ""))
    return None if spec is None or spec["item_filter"] is None else tuple(spec["item_filter"])


def is_sec_plan(plan: Mapping[str, Any]) -> bool:
    return plan.get("source_ref") == SEC_SOURCE_REF and plan.get("schema_version") == SEC_PLAN_SCHEMA


def effective_sec_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The SEC plan an environment runs: approved governed fields, code-owned specs.

    Idempotent.  A non-SEC plan is returned validated and unchanged.
    """

    from .mission_source_discovery import validate_discovery_plan

    validated = validate_discovery_plan(plan)
    if not is_sec_plan(validated):
        return validated
    held = {spec["spec_ref"]: spec for spec in validated["specs"]}
    specs: list[dict[str, Any]] = []
    for spec in SEC_PLAN_SPECS:
        own = held.get(spec["spec_ref"])
        specs.append({
            "spec_ref": spec["spec_ref"], "form": spec["form"],
            **{key: (own[key] if own is not None else spec[key]) for key in _GOVERNED_SPEC_FIELDS},
        })
    for spec in validated["specs"]:
        if code_spec(spec["spec_ref"]) is None:
            specs.append(dict(spec))
    if canonical_json(specs) == canonical_json(validated["specs"]):
        return validated
    base = {key: validated[key] for key in validated if key != "content_hash"}
    base["specs"] = specs
    return validate_discovery_plan({**base, "content_hash": content_hash(base)})


def sec_plan_report(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What rendering does to one approved SEC plan, for parity and the ledger."""

    from .mission_source_discovery import validate_discovery_plan

    validated = validate_discovery_plan(plan)
    effective = effective_sec_plan(validated)
    held = {spec["spec_ref"]: spec for spec in validated["specs"]}
    rows: list[dict[str, Any]] = []
    for spec in SEC_PLAN_SPECS:
        own = held.get(spec["spec_ref"])
        if own is None:
            drift = "added_by_code"
        elif own["form"] != spec["form"]:
            drift = "form_from_code"
        else:
            drift = "none"
        rows.append({
            "spec_ref": spec["spec_ref"], "form": spec["form"],
            "item_filter": None if spec["item_filter"] is None else list(spec["item_filter"]),
            "in_plan_file": own is not None, "drift": drift,
            "plan_form": None if own is None else own["form"],
            **{key: (own[key] if own is not None else spec[key]) for key in _GOVERNED_SPEC_FIELDS},
            "intervals_from": "plan" if own is not None else "code",
        })
    for spec in validated["specs"]:
        if code_spec(spec["spec_ref"]) is None:
            rows.append({"spec_ref": spec["spec_ref"], "form": spec["form"], "item_filter": None,
                         "in_plan_file": True, "drift": "plan_only", "plan_form": spec["form"],
                         **{key: spec[key] for key in _GOVERNED_SPEC_FIELDS},
                         "intervals_from": "plan"})
    return {
        "plan_ref": validated["id"], "approved_plan_hash": validated["content_hash"],
        "effective_plan_hash": effective["content_hash"], "specs_hash": SEC_PLAN_SPECS_HASH,
        "rendered_from": "plan" if effective["content_hash"] == validated["content_hash"] else "code",
        "budget": dict(validated.get("budget") or {}), "specs": rows,
    }


__all__ = [
    "EARNINGS_RELEASE_SPEC_REF",
    "SEC_PLAN_SPECS",
    "SEC_PLAN_SPECS_HASH",
    "acquisition_item_filter",
    "code_spec",
    "effective_sec_plan",
    "generator_specs",
    "is_sec_plan",
    "sec_plan_report",
]
