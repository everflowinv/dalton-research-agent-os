"""Rebuild a weekly brief's evidence pack from the Ledger before each issue.

A schedule plan used to name one exact evidence pack version, so every issue
re-rendered the same five Claims for as long as nobody hand-registered a new
pack (W36, W37 and W38 were byte-for-byte the same evidence).  A plan that
declares ``evidence_refresh`` instead names the *pack ref* and its overlay
refs; before a cycle is admitted this lane derives the pack the Ledger
supports at the scheduled instant and the cycle freezes whichever version
that is.

The lane is deterministic -- no model call:

* the template is the latest *human-authored* version of the pack ref.  Its
  boundary, coverage universe, driver pack, source plan, report contract and
  debates are the approved frame; the lane only re-selects Claims;
* a Claim is eligible when it is the latest version of its claim_ref, was
  written no later than the scheduled instant, is not retired (a retired
  Claim reattributed to the pack's industry counts, as the industry's --
  ``claim_industry_reattribution``) and not
  adjudicated superseded/retracted, is about a covered company (or the
  industry), carries exactly a driver metric as ``metric_or_aspect``, has at
  least one evidence relation, and its period ends inside the
  ``claim_window_days`` window before the scheduled instant (a Claim whose
  period is not an ISO date range is anchored on its write date instead);
* per (subject, metric) the Claim with the latest period end wins (then the
  latest write, then the id), and a numeric metric never takes a Claim whose
  only authority is an authenticated transcript;
* a covered company with no eligible Claim for some driver drops out of this
  issue's universe (the template's own boundary already excludes an issuer
  without a formal lane Claim);
* a debate position is kept verbatim only while every Claim it cites is still
  selected.  A replaced Claim becomes a ``qualifies`` position whose label
  says the stance has not been reviewed -- the lane never re-argues a human
  position;
* overlays carry the latest overlay's human text; a driver whose Claims
  changed has its stance reset to ``unknown`` for the same reason.

When the selection equals the latest pack version (whoever wrote it) nothing
new is written, so a quiet week re-uses the prior version instead of minting a
copy.  Every refreshed version is published by ``EVIDENCE_REFRESH_ACTOR`` and
records the plan hash and governance policy that authorized it; the version
ids and idempotency keys are content-derived, so a crash between the pack and
its overlays replays into the same versions.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .industry_research import (
    EVIDENCE_REFRESH_ACTOR,
    IndustryResearchAuthority,
    IndustryResearchError,
)
from .store import content_hash


SELECTION_RULE_REF = "weekly-brief-evidence-refresh:latest-period-per-subject-metric:v1"
_INACTIVE_ADJUDICATIONS = frozenset({"superseded", "retracted"})
_NUMERIC_VERIFICATION = frozenset({"numeric", "numeric_and_semantic"})
_TRANSCRIPT_SOURCE = "authenticated_transcript"
_DATE_RANGE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})\s*$")
_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*$")


class EvidenceRefreshError(RuntimeError):
    """The Ledger cannot support a valid refreshed pack for this cycle."""


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvidenceRefreshError(f"timestamp lacks a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def period_end(period: Any) -> date | None:
    """The end date of an ISO ``start..end`` (or single-date) period, else None."""

    if not isinstance(period, str):
        return None
    match = _DATE_RANGE_RE.match(period) or _DATE_RE.match(period)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(match.lastindex or 1))
    except ValueError:
        return None


def _latest_human_pack(connection: sqlite3.Connection, pack_ref: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT record_json FROM industry_evidence_pack_versions "
        "WHERE evidence_pack_ref=? ORDER BY version_number DESC",
        (pack_ref,),
    ).fetchall()
    for row in rows:
        record = json.loads(row["record_json"])
        if str(record.get("actor_ref", "")).startswith("human:"):
            return record
    raise EvidenceRefreshError(f"{pack_ref} has no human-authored template version")


def _latest_pack(connection: sqlite3.Connection, pack_ref: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT record_json FROM industry_evidence_pack_versions "
        "WHERE evidence_pack_ref=? ORDER BY version_number DESC LIMIT 1",
        (pack_ref,),
    ).fetchone()
    return None if row is None else json.loads(row["record_json"])


def _latest_overlay(connection: sqlite3.Connection, overlay_ref: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT record_json FROM company_overlay_versions "
        "WHERE overlay_ref=? ORDER BY version_number DESC LIMIT 1",
        (overlay_ref,),
    ).fetchone()
    if row is None:
        raise EvidenceRefreshError(f"{overlay_ref} has no published version")
    return json.loads(row["record_json"])


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _excluded_claim_versions(
    connection: sqlite3.Connection, *, industry_evidence: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    from .claim_retirement import retired_claim_version_refs

    # Retired less reinstated (2026-09-24), less the retired Claims recorded
    # as evidence about this pack's industry (2026-09-25): those are read as
    # the industry's, never as their company's.  Adjudication still applies.
    excluded: set[str] = set(retired_claim_version_refs(connection)) - set(industry_evidence)
    if _table_exists(connection, "adjudication_versions"):
        latest: dict[str, tuple[int, str]] = {}
        for row in connection.execute(
            "SELECT claim_version_id,version_number,adjudicated_status "
            "FROM adjudication_versions"
        ):
            prior = latest.get(row[0])
            if prior is None or int(row[1]) > prior[0]:
                latest[row[0]] = (int(row[1]), row[2])
        excluded.update(
            ref for ref, (_, status) in latest.items()
            if status in _INACTIVE_ADJUDICATIONS
        )
    return excluded


def _candidates(
    connection: sqlite3.Connection,
    *,
    metric_refs: set[str],
    subject_refs: set[str],
    scheduled_for: datetime,
    window_days: int,
    industry_ref: str | None = None,
) -> list[dict[str, Any]]:
    from .claim_industry_reattribution import reattributed_claim_version_refs

    # A retired Claim reattributed to the pack's industry is a candidate under
    # the industry's subject -- the industry row of the pack -- and never under
    # the company it was filed under, so no overlay picks it up.
    industry_evidence = (
        reattributed_claim_version_refs(connection, industry_ref)
        if industry_ref is not None else set())
    placeholders = ",".join("?" for _ in metric_refs)
    rows = connection.execute(
        "SELECT c.claim_version_id,c.claim_json,c.content_hash,c.created_at "
        "FROM claim_versions c WHERE json_extract(c.claim_json,'$.metric_or_aspect') "
        f"IN ({placeholders}) AND NOT EXISTS (SELECT 1 FROM claim_versions n "
        "WHERE n.prior_version_id=c.claim_version_id)",
        sorted(metric_refs),
    ).fetchall()
    excluded = _excluded_claim_versions(connection, industry_evidence=industry_evidence)
    window_start = (scheduled_for - timedelta(days=window_days)).date()
    result = []
    for row in rows:
        if row["claim_version_id"] in excluded:
            continue
        claim = json.loads(row["claim_json"])
        subject_ref = (industry_ref if row["claim_version_id"] in industry_evidence
                       else claim.get("subject_ref"))
        if subject_ref not in subject_refs:
            continue
        written = _instant(row["created_at"])
        if written > scheduled_for:
            continue
        anchor = period_end(claim.get("period")) or written.date()
        if not window_start <= anchor <= scheduled_for.date():
            continue
        relations = connection.execute(
            "SELECT r.relation_id,r.content_hash,e.evidence_json FROM evidence_relations r "
            "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
            "WHERE r.claim_version_id=? ORDER BY r.relation_id",
            (row["claim_version_id"],),
        ).fetchall()
        if not relations:
            continue
        result.append({
            "claim_version_ref": row["claim_version_id"],
            "claim_version_hash": row["content_hash"],
            "subject_ref": subject_ref,
            "metric_ref": claim["metric_or_aspect"],
            "period": claim.get("period"),
            "value": claim.get("value"),
            "unit": claim.get("unit"),
            "anchor": anchor.isoformat(),
            "written_at": written.isoformat(),
            "relation_refs": [
                {"ref": item["relation_id"], "hash": item["content_hash"]}
                for item in relations
            ],
            "source_types": sorted({
                json.loads(item["evidence_json"]).get("source_type", "")
                for item in relations
            }),
        })
    return result


def _select(
    candidates: Sequence[Mapping[str, Any]], verification: Mapping[str, str],
) -> dict[tuple[str, str], dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        if verification.get(item["metric_ref"]) in _NUMERIC_VERIFICATION and all(
            source == _TRANSCRIPT_SOURCE for source in item["source_types"]
        ):
            continue
        key = (item["subject_ref"], item["metric_ref"])
        rank = (item["anchor"], item["written_at"], item["claim_version_ref"])
        current = selected.get(key)
        if current is None or rank > (
            current["anchor"], current["written_at"], current["claim_version_ref"]
        ):
            selected[key] = dict(item)
    return selected


def _pack_frame(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Everything that decides a pack's content, without its version identity."""

    return {
        "industry_ref": pack["industry_ref"], "title": pack["title"],
        "boundary": pack["boundary"], "coverage_universe": pack["coverage_universe"],
        "driver_pack_version_ref": pack["driver_pack_version_ref"],
        "driver_pack_version_hash": pack["driver_pack_version_hash"],
        "evidence_bindings": sorted(
            (
                {
                    "driver_ref": item["driver_ref"], "metric_ref": item["metric_ref"],
                    "claim_version_ref": item["claim_version_ref"],
                    "claim_version_hash": item["claim_version_hash"],
                    "relation_refs": sorted(
                        item["relation_refs"], key=lambda relation: relation["ref"]
                    ),
                }
                for item in pack["evidence_bindings"]
            ),
            key=lambda item: (item["driver_ref"], item["metric_ref"], item["claim_version_ref"]),
        ),
        "debates": pack["debates"], "source_plan": pack["source_plan"],
        "report_contract": pack["report_contract"],
    }


def _driver_pack(connection: sqlite3.Connection, template: Mapping[str, Any]) -> dict[str, Any]:
    row = connection.execute(
        "SELECT record_json,content_hash FROM driver_pack_versions WHERE version_id=?",
        (template["driver_pack_version_ref"],),
    ).fetchone()
    if row is None or row["content_hash"] != template["driver_pack_version_hash"]:
        raise EvidenceRefreshError("template driver pack is missing or drifted")
    return json.loads(row["record_json"])


def refresh_evidence_pack(
    industry: IndustryResearchAuthority,
    *,
    evidence_pack_ref: str,
    company_overlay_refs: Sequence[str],
    claim_window_days: int,
    scheduled_for: str,
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the pack and overlay versions the cycle should freeze.

    ``authority`` carries rule/plan/policy/cycle identity; the template fields
    and window are filled in here.  Raises ``EvidenceRefreshError`` (or an
    ``IndustryResearchError`` from the authority) when the Ledger cannot
    support a valid pack; the caller decides what to publish instead.
    """

    connection = industry.connection
    when = _instant(scheduled_for)
    template = _latest_human_pack(connection, evidence_pack_ref)
    driver_pack = _driver_pack(connection, template)
    driver_of_metric = {
        metric_ref: driver["driver_ref"]
        for driver in driver_pack["drivers"] for metric_ref in driver["metric_refs"]
    }
    verification = {
        item["metric_ref"]: item["verification_kind"] for item in driver_pack["metric_specs"]
    }
    overlays = {}
    for overlay_ref in company_overlay_refs:
        overlay = _latest_overlay(connection, overlay_ref)
        overlays[overlay["company_ref"]] = overlay
    universe_refs = {item["company_ref"] for item in template["coverage_universe"]}
    selected = _select(
        _candidates(
            connection, metric_refs=set(driver_of_metric),
            subject_refs=universe_refs | {template["industry_ref"]},
            scheduled_for=when, window_days=claim_window_days,
            industry_ref=template["industry_ref"],
        ),
        verification,
    )

    # A company stays in this issue only when it has an overlay to carry and
    # an eligible Claim for every driver (an overlay view cannot be empty).
    drivers = [driver["driver_ref"] for driver in driver_pack["drivers"]]
    universe = []
    dropped = []
    for company in template["coverage_universe"]:
        ref = company["company_ref"]
        covered = {
            driver_of_metric[metric] for (subject, metric) in selected if subject == ref
        }
        if ref in overlays and covered >= set(drivers):
            universe.append(company)
        else:
            dropped.append(company["ticker"])
    if not any(item["comparability_tier"] == "core" for item in universe):
        raise EvidenceRefreshError(
            "no core company has an eligible claim for every driver in the window"
        )
    kept_subjects = {item["company_ref"] for item in universe} | {template["industry_ref"]}
    selected = {key: value for key, value in selected.items() if key[0] in kept_subjects}
    ticker = {item["company_ref"]: item["ticker"] for item in template["coverage_universe"]}
    ticker[template["industry_ref"]] = "industry"

    template_bindings = {
        (item["driver_ref"], item["metric_ref"], item["claim_version_ref"]): item
        for item in template["evidence_bindings"]
    }
    bindings = []
    for (subject, metric), item in sorted(selected.items()):
        driver_ref = driver_of_metric[metric]
        carried = template_bindings.get((driver_ref, metric, item["claim_version_ref"]))
        if carried is not None:
            bindings.append(dict(carried))
            continue
        bindings.append({
            "binding_ref": f"binding:refresh:{ticker[subject].lower()}:{metric}:{item['anchor']}",
            "driver_ref": driver_ref, "metric_ref": metric,
            "claim_version_ref": item["claim_version_ref"],
            "claim_version_hash": item["claim_version_hash"],
            "relation_refs": item["relation_refs"],
        })
    if {item["driver_ref"] for item in bindings} != set(drivers):
        raise EvidenceRefreshError("the window has no eligible claim for some driver")

    selected_refs = {item["claim_version_ref"] for item in selected.values()}
    claim_key: dict[str, tuple[str, str]] = {}
    for binding in template["evidence_bindings"]:
        row = connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (binding["claim_version_ref"],),
        ).fetchone()
        if row is not None:
            claim = json.loads(row["claim_json"])
            claim_key[binding["claim_version_ref"]] = (claim["subject_ref"], claim["metric_or_aspect"])
    metric_label = {item["metric_ref"]: item["label"] for item in driver_pack["metric_specs"]}
    debates = []
    for debate in template["debates"]:
        positions = []
        cited: set[str] = set()
        for position in debate["positions"]:
            if set(position["claim_version_refs"]) <= selected_refs:
                positions.append(position)
                cited.update(position["claim_version_refs"])
        for position in debate["positions"]:
            for ref in position["claim_version_refs"]:
                replacement = selected.get(claim_key.get(ref, ("", "")))
                if replacement is None or replacement["claim_version_ref"] in cited:
                    continue
                cited.add(replacement["claim_version_ref"])
                value = replacement["value"]
                reading = "" if value is None else f" {value}" + (
                    f" {replacement['unit']}" if replacement["unit"] else ""
                )
                positions.append({
                    "label": (
                        f"{ticker[replacement['subject_ref']]}: "
                        f"{metric_label.get(replacement['metric_ref'], replacement['metric_ref'])}"
                        f"{reading} for {replacement['period']} (refreshed evidence; "
                        "stance not yet reviewed)"
                    ),
                    "stance": "qualifies",
                    "claim_version_refs": [replacement["claim_version_ref"]],
                })
        if len(positions) >= 2:
            debates.append({**debate, "positions": positions})
    if not debates:
        raise EvidenceRefreshError("no template debate keeps two positions in the window")

    frame = _pack_frame({
        **template, "coverage_universe": universe,
        "evidence_bindings": bindings, "debates": debates,
    })
    latest = _latest_pack(connection, evidence_pack_ref)
    full_authority = {
        **dict(authority),
        "claim_window_days": claim_window_days,
        "template_evidence_pack_version_ref": template["id"],
        "template_evidence_pack_version_hash": template["content_hash"],
    }
    if latest is not None and _pack_frame(latest) == frame:
        pack = latest
        pack_status = "reused"
    else:
        digest = content_hash({
            "rule": SELECTION_RULE_REF, "frame": frame,
            "prior_version_ref": None if latest is None else latest["id"],
        })
        pack = industry.register_evidence_pack(
            evidence_pack_ref, industry_ref=template["industry_ref"],
            title=template["title"], as_of=scheduled_for,
            boundary=template["boundary"], coverage_universe=universe,
            driver_pack_version_ref=template["driver_pack_version_ref"],
            driver_pack_version_hash=template["driver_pack_version_hash"],
            evidence_bindings=bindings, debates=debates,
            source_plan=template["source_plan"],
            report_contract=template["report_contract"],
            actor_ref=EVIDENCE_REFRESH_ACTOR,
            version_id=f"industry-evidence-pack-version:refresh:{digest[:32]}",
            prior_version_ref=None if latest is None else latest["id"],
            idempotency_key=f"industry-evidence-refresh:pack:{digest}",
            refresh_authority=full_authority,
        )
        pack_status = "fresh" if pack.get("status") == "fresh" else "duplicate"

    overlay_versions = []
    overlay_statuses = {}
    pack_claims_by_subject: dict[str, dict[str, list[dict[str, str]]]] = {}
    for binding in pack["evidence_bindings"]:
        subject = next(
            (key[0] for key, value in selected.items()
             if value["claim_version_ref"] == binding["claim_version_ref"]),
            None,
        )
        if subject is None:
            continue
        pack_claims_by_subject.setdefault(subject, {}).setdefault(
            binding["driver_ref"], []
        ).append({
            "ref": binding["claim_version_ref"], "hash": binding["claim_version_hash"],
            "metric_ref": binding["metric_ref"],
        })
    for company in pack["coverage_universe"]:
        overlay = overlays[company["company_ref"]]
        if (
            overlay["evidence_pack_version_ref"] == pack["id"]
            and overlay["evidence_pack_version_hash"] == pack["content_hash"]
        ):
            overlay_versions.append(overlay["id"])
            overlay_statuses[overlay["overlay_ref"]] = "reused"
            continue
        claims_by_driver = pack_claims_by_subject.get(company["company_ref"], {})
        views = []
        for view in overlay["driver_views"]:
            claims = sorted(claims_by_driver.get(view["driver_ref"], []), key=lambda item: item["ref"])
            refs = [{"ref": item["ref"], "hash": item["hash"]} for item in claims]
            unchanged = refs == sorted(view["claim_version_refs"], key=lambda item: item["ref"])
            evidence_refs = {
                row[0] for item in claims for row in connection.execute(
                    "SELECT evidence_version_id FROM evidence_relations WHERE claim_version_id=?",
                    (item["ref"],),
                )
            }
            model_inputs = []
            for input_ref in view["model_input_version_refs"]:
                row = connection.execute(
                    "SELECT record_json FROM model_input_versions WHERE version_id=?",
                    (input_ref["ref"],),
                ).fetchone()
                authorities = (
                    [] if row is None
                    else json.loads(row["record_json"]).get("payload", {}).get("source_authorities", [])
                )
                if any(
                    isinstance(item, Mapping) and item.get("authority_kind") == "evidence_version"
                    and item.get("version_ref") in evidence_refs
                    for item in authorities
                ):
                    model_inputs.append(input_ref)
            coverage = []
            for item in view["metric_coverage"]:
                metric_claims = [claim["ref"] for claim in claims if claim["metric_ref"] == item["metric_ref"]]
                if metric_claims:
                    if item["status"] == "observed" and sorted(item["claim_version_refs"]) == sorted(metric_claims):
                        coverage.append(item)
                    else:
                        coverage.append({
                            "metric_ref": item["metric_ref"], "status": "observed",
                            "claim_version_refs": metric_claims,
                            "rationale": (
                                f"Refreshed from the Ledger as of {scheduled_for}: latest-period "
                                f"eligible claim within the {claim_window_days}-day window."
                            ),
                        })
                elif item["status"] != "observed":
                    coverage.append(item)
                else:
                    coverage.append({
                        "metric_ref": item["metric_ref"],
                        "status": "not_found_in_reviewed_sources",
                        "claim_version_refs": [],
                        "rationale": (
                            f"No eligible claim within the {claim_window_days}-day window "
                            f"as of {scheduled_for}."
                        ),
                    })
            views.append({
                **view, "stance": view["stance"] if unchanged else "unknown",
                "claim_version_refs": refs, "model_input_version_refs": model_inputs,
                "metric_coverage": coverage,
            })
        digest = content_hash({
            "rule": SELECTION_RULE_REF, "overlay_prior": overlay["id"],
            "pack": pack["id"], "views": views,
        })
        fresh = industry.register_company_overlay(
            overlay["overlay_ref"], company_ref=overlay["company_ref"],
            industry_ref=overlay["industry_ref"], title=overlay["title"],
            as_of=scheduled_for, role=company["role"],
            evidence_pack_version_ref=pack["id"],
            evidence_pack_version_hash=pack["content_hash"],
            driver_views=views, key_differences=overlay["key_differences"],
            open_questions=overlay["open_questions"],
            falsifier_refs=overlay["falsifier_refs"],
            thesis_candidate_refs=overlay["thesis_candidate_refs"],
            actor_ref=EVIDENCE_REFRESH_ACTOR,
            version_id=f"company-overlay-version:refresh:{digest[:32]}",
            prior_version_ref=overlay["id"],
            idempotency_key=f"industry-evidence-refresh:overlay:{digest}",
            refresh_authority=full_authority,
        )
        overlay_versions.append(fresh["id"])
        overlay_statuses[overlay["overlay_ref"]] = (
            "fresh" if fresh.get("status") == "fresh" else "duplicate"
        )
    return {
        "status": "refreshed" if pack_status != "reused" else "unchanged",
        "selection_rule_ref": SELECTION_RULE_REF,
        "evidence_pack_version_ref": pack["id"],
        "evidence_pack_status": pack_status,
        "company_overlay_version_refs": overlay_versions,
        "company_overlay_statuses": overlay_statuses,
        "claim_version_refs": sorted(item["claim_version_ref"] for item in pack["evidence_bindings"]),
        "dropped_tickers": dropped,
        "template_evidence_pack_version_ref": template["id"],
    }


__all__ = [
    "EvidenceRefreshError", "SELECTION_RULE_REF", "IndustryResearchError",
    "period_end", "refresh_evidence_pack",
]
