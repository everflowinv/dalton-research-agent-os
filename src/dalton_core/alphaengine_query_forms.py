"""AlphaEngine discovery query forms are owned by code, not by plan files.

Owner decision 2026-10-06 ("A"): every environment runs the same code and the
same behaviour, and a freshly created workspace is correct without patches.
Until then each environment searched AlphaEngine with whatever phrasings its
approved plan file happened to carry -- legacy three transcript forms without
the title form, ws-7d the six of plan v3, ws-e399 none yet -- and a better
phrasing reached an environment only through a per-environment owner script.

So the phrasings live here, once, keyed by ``spec_ref`` (and, for a plan whose
spec is named differently, by ``document_type``).  An approved plan file keeps
what the owner governs:

* ``budget.max_calls_24h``;
* per spec ``lookback_days``, ``rediscovery_interval_days``,
  ``retry_interval_days`` and ``document_type``;
* the company list with names, tickers and aliases;
* ``id``, ``mission_ref``, ``source_ref``, ``created_at``.

New plans are written at schema 0.7, which has no ``query_variants`` at all.
A 0.6 plan that still carries them is accepted unchanged -- its hash, its
selector and its owner approval stay valid -- and its forms are simply not
used for rendering.

:func:`effective_discovery_plan` renders the plan the coordinator, the search
launcher and the search child actually run: the plan file's governed fields
with this module's forms, as an ordinary validated 0.6 plan carrying its own
``content_hash``.  Every dispatch and discovery records that hash, so the form
set a search used is on the record, and the cadence
(``MissionSourceDiscoveryCoordinator._query_changed_since``) sees a form-set
change as a plan change and lets each (company, spec) whose first query
changed search once.  When the file's forms already equal these the rendered
plan *is* the file (same hash), so nothing is re-searched for nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

ALPHAENGINE_SOURCE_REF = "source:alphaengine"
#: Schema with query forms in the file (accepted; forms ignored for rendering).
FORMS_IN_FILE_SCHEMA = "0.6"
#: Schema with only owner-governed fields (what the generator writes).
GOVERNED_ONLY_SCHEMA = "0.7"


def _variants(*templates: str) -> tuple[dict[str, Any], ...]:
    return tuple({"query_template": template, "filters": {}} for template in templates)


#: The AlphaEngine query forms, per spec.  Transcripts first exactly the way
#: AlphaEngine titles a call -- the issuer's name and the fiscal quarter,
#: "Alphabet Q3 2025" -- which ranks the call first; then the bare name
#: (legacy's working form), then the longer phrasings.  "Alphabet FY2025-Q3
#: earnings call transcript" matched no AlphaEngine title and returned other
#: companies' pre-close calls (2026-10-03).  Each variant is tried only after
#: candidate selection has consumed the previous one, so a variant that works
#: costs nothing extra.
ALPHAENGINE_QUERY_FORMS: Mapping[str, Mapping[str, Any]] = {
    "earnings-call-transcripts": {
        "document_type": "meeting_minutes",
        "query_variants": _variants(
            "{name} {quarter_label}",
            "{aliases}",
            "{name} {quarter} earnings call transcript",
            "{aliases} earnings call",
            "{name} quarterly earnings conference call",
            "{ticker} {quarter} results webcast transcript"),
    },
    "sell-side-reports": {
        "document_type": "sell_side_report",
        "query_variants": _variants(
            "{ticker} analyst research report",
            "{name} equity research report"),
    },
}


def _forms_wire() -> dict[str, Any]:
    return json.loads(canonical_json({
        ref: {"document_type": entry["document_type"],
              "query_variants": [dict(v) for v in entry["query_variants"]]}
        for ref, entry in ALPHAENGINE_QUERY_FORMS.items()}))


#: Content hash of the form set, for ledgers and parity rows.
ALPHAENGINE_QUERY_FORMS_HASH = content_hash(_forms_wire())


def code_query_variants(spec_ref: str, document_type: str | None) -> list[dict[str, Any]] | None:
    """The code's forms for one spec: by ``spec_ref``, else by a unique ``document_type``."""

    entry = ALPHAENGINE_QUERY_FORMS.get(spec_ref)
    if entry is None and document_type:
        matches = [item for item in ALPHAENGINE_QUERY_FORMS.values()
                   if item["document_type"] == document_type]
        entry = matches[0] if len(matches) == 1 else None
    if entry is None:
        return None
    return [{"query_template": v["query_template"], "filters": dict(v["filters"])}
            for v in entry["query_variants"]]


def _packaged_name(ticker: str) -> str | None:
    from .document_subject import COMPANY_NAMES

    names = COMPANY_NAMES.get(ticker.strip().upper())
    return names[0] if names else None


def _ticker_only(company: Mapping[str, Any]) -> bool:
    name = str(company.get("name") or "").strip()
    return not name or name.upper() == str(company.get("ticker") or "").strip().upper()


def resolve_company_names(companies: Mapping[str, Mapping[str, Any]]
                          ) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    """Give a ticker-only company its name, from code only.

    A plan the generator wrote before W7 (ws-7d v1) names each company by its
    ticker, and "{name} {quarter_label}" would then ask for "GOOGL Q3 2025",
    which AlphaEngine never titles a call.  The name comes from the packaged
    name table (``document_subject.COMPANY_NAMES``) and nothing else: the
    coordinator in the writer and the search child must render the same plan,
    so the rendering may not depend on state either of them reads separately.
    A generated plan already carries the SEC registrant name (24db1c29).

    Returns the companies, ``{company_ref: name}`` for every name filled in,
    and the refs still named only by their ticker (the owner must publish
    their names).
    """

    out: dict[str, dict[str, Any]] = {}
    filled: dict[str, str] = {}
    unresolved: list[str] = []
    for ref in sorted(companies):
        company = dict(companies[ref])
        aliases = list(company.get("aliases") or [])
        if _ticker_only(company):
            name = _packaged_name(str(company.get("ticker") or ""))
            if name:
                company["name"] = name
                filled[ref] = name
                if not aliases or all(a.strip().upper() == company["ticker"].strip().upper()
                                      for a in aliases):
                    aliases = [name]
            else:
                unresolved.append(ref)
        if not aliases:
            aliases = [company["name"]]
        company["aliases"] = aliases
        out[ref] = company
    return out, filled, unresolved


def renders_from_code(plan: Mapping[str, Any]) -> bool:
    """Whether this plan's AlphaEngine searches can be rendered from the code forms."""

    return (plan.get("source_ref") == ALPHAENGINE_SOURCE_REF
            and plan.get("schema_version") in (FORMS_IN_FILE_SCHEMA, GOVERNED_ONLY_SCHEMA))


def effective_discovery_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The plan an environment runs: approved governed fields, code-owned forms.

    Any plan that is not an AlphaEngine 0.6/0.7 plan (web search, SEC, or an
    AlphaEngine 0.1/0.2 plan of bare search terms, which has no name to put in
    a form) is returned as validated.  Idempotent: rendering a rendered plan
    returns it unchanged.
    """

    from .mission_source_discovery import validate_discovery_plan

    validated = validate_discovery_plan(plan)
    if validated.get("source_ref") == "source:sec-edgar":
        # 2026-10-08: the SEC spec set is code-owned the same way
        # (``sec_plan_specs``): legacy's file has only the 10-K.
        from .sec_plan_specs import effective_sec_plan
        return effective_sec_plan(validated)
    if not renders_from_code(validated):
        return validated
    companies, _, _ = resolve_company_names(validated["companies"])
    specs = []
    for spec in validated["specs"]:
        forms = code_query_variants(spec["spec_ref"], spec.get("document_type"))
        if forms is None:
            forms = spec.get("query_variants")
            if forms is None:
                # 0.7 never reaches here: its validation refuses a spec the
                # code has no forms for.
                raise ValueError(f"no query forms for AlphaEngine spec {spec['spec_ref']}")
        specs.append({
            "spec_ref": spec["spec_ref"], "document_type": spec["document_type"],
            "query_variants": forms, "lookback_days": spec["lookback_days"],
            "rediscovery_interval_days": spec["rediscovery_interval_days"],
            "retry_interval_days": spec["retry_interval_days"],
        })
    base = {
        "schema_version": FORMS_IN_FILE_SCHEMA,
        "id": validated["id"], "created_at": validated["created_at"],
        "mission_ref": validated["mission_ref"], "source_ref": validated["source_ref"],
        "companies": companies, "specs": specs, "budget": dict(validated["budget"]),
    }
    return validate_discovery_plan({**base, "content_hash": content_hash(base)})


def load_effective_discovery_plan(path: str | Path) -> dict[str, Any]:
    from .mission_source_discovery import load_discovery_plan

    return effective_discovery_plan(load_discovery_plan(path))


def governed_fields(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The owner-governed part of a plan, which the approval actually decides."""

    return {
        "id": plan.get("id"), "mission_ref": plan.get("mission_ref"),
        "source_ref": plan.get("source_ref"), "budget": plan.get("budget"),
        "companies": plan.get("companies"),
        "specs": [{key: spec.get(key) for key in (
            "spec_ref", "document_type", "lookback_days",
            "rediscovery_interval_days", "retry_interval_days")}
            for spec in plan.get("specs") or []],
    }


def query_form_report(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What rendering does to one approved plan, for the ledger and parity.

    ``rendered_from`` is ``code`` when every spec renders the code forms,
    ``plan`` when the plan cannot be rendered from code at all (an AlphaEngine
    plan of bare search terms) and ``mixed`` when some spec has no code forms.
    """

    from .mission_source_discovery import (
        SEARCH_FLOOR_SHARE, search_floor_calls, validate_discovery_plan,
    )

    validated = validate_discovery_plan(plan)
    budget = dict(validated.get("budget") or {})
    if budget.get("max_calls_24h") is not None:
        # 2026-10-07: the code's search floor, rendered with the plan's own
        # budget so parity prints the same policy for every environment.
        budget["search_floor_share"] = SEARCH_FLOOR_SHARE
        budget["search_floor_calls"] = search_floor_calls(budget["max_calls_24h"])
    from .tracking_cadence import (
        DISCOVERY_CADENCE_RULE, MAX_DISCOVERY_DIVISOR, MIN_DISCOVERY_INTERVAL_DAYS,
        discovery_cadence_report,
    )

    report: dict[str, Any] = {
        # 2026-10-07: what 「优先看这家」 does to these intervals, per company,
        # in code (``MissionSourceDiscoveryCoordinator._cadence_block``).  The
        # plan file and its hash are unchanged by a priority.
        "cadence_scaling": {"max_divisor": MAX_DISCOVERY_DIVISOR,
                            "min_interval_days": MIN_DISCOVERY_INTERVAL_DAYS,
                            "rule": DISCOVERY_CADENCE_RULE},
        "plan_ref": validated["id"], "approved_plan_hash": validated["content_hash"],
        "schema_version": validated["schema_version"],
        "forms_hash": ALPHAENGINE_QUERY_FORMS_HASH,
        "budget": budget,
        "specs": [], "names_filled": {}, "names_unresolved": [],
    }
    if not renders_from_code(validated):
        report.update(rendered_from="plan", effective_plan_hash=validated["content_hash"],
                      reason="an AlphaEngine plan of bare search terms has no name or "
                             "ticker for the code forms")
        for spec in validated["specs"]:
            report["specs"].append({
                "spec_ref": spec["spec_ref"], "rendered_from": "plan",
                "plan_forms": [spec.get("query_template")],
                "plan_forms_differ": None, "plan_forms_ignored": False,
                **{key: spec.get(key) for key in (
                    "lookback_days", "rediscovery_interval_days", "retry_interval_days")},
                **discovery_cadence_report(spec)})
        return report
    effective = effective_discovery_plan(validated)
    _, filled, unresolved = resolve_company_names(validated["companies"])
    report["names_filled"] = filled
    report["names_unresolved"] = unresolved
    report["effective_plan_hash"] = effective["content_hash"]
    sources = set()
    for spec in validated["specs"]:
        code = code_query_variants(spec["spec_ref"], spec.get("document_type"))
        file_forms = spec.get("query_variants")
        source = "code" if code is not None else "plan"
        sources.add(source)
        report["specs"].append({
            "spec_ref": spec["spec_ref"], "document_type": spec.get("document_type"),
            "rendered_from": source,
            "plan_forms": None if file_forms is None else [v["query_template"] for v in file_forms],
            "code_forms": None if code is None else [v["query_template"] for v in code],
            "plan_forms_differ": (None if file_forms is None or code is None
                                  else canonical_json(file_forms) != canonical_json(code)),
            "plan_forms_ignored": file_forms is not None and code is not None,
            **{key: spec[key] for key in (
                "lookback_days", "rediscovery_interval_days", "retry_interval_days")},
            **discovery_cadence_report(spec)})
    report["rendered_from"] = sources.pop() if len(sources) == 1 else "mixed"
    return report


def query_forms_ledger_note(report: Mapping[str, Any]) -> dict[str, str]:
    """Flat fields for the coordinator's tick ledger (written once per load)."""

    ignored = [spec["spec_ref"] for spec in report.get("specs") or ()
               if spec.get("plan_forms_ignored") and spec.get("plan_forms_differ")]
    note = f"forms {str(report.get('forms_hash'))[:12]} from {report.get('rendered_from')}"
    if ignored:
        note += "; plan file query_variants ignored for " + ",".join(ignored)
    if report.get("names_unresolved"):
        note += "; ticker-only names: " + ",".join(report["names_unresolved"])
    return {"query_forms": str(report.get("rendered_from")),
            "query_forms_hash": str(report.get("effective_plan_hash") or ""),
            "query_forms_note": note}


__all__ = [
    "ALPHAENGINE_QUERY_FORMS",
    "ALPHAENGINE_QUERY_FORMS_HASH",
    "GOVERNED_ONLY_SCHEMA",
    "code_query_variants",
    "effective_discovery_plan",
    "governed_fields",
    "load_effective_discovery_plan",
    "query_form_report",
    "query_forms_ledger_note",
    "renders_from_code",
    "resolve_company_names",
]
